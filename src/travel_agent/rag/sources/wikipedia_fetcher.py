"""
离线知识库构建脚本 — 非主调用路径。

由 scripts/index_knowledge.py --wikipedia 调用；FastAPI 运行时不会触达此模块。
通过 Wikipedia REST API 的 ``/page/summary/`` 端点获取目的地摘要并索引到知识库。
API 文档: https://www.mediawiki.org/wiki/API:REST_API

**一篇文章一个摘要块，就是这个模块能做到的全部。** 分段抓取路径已删除：它打的
``/page/mobile-sections/`` 被 Wikimedia 下线（``403 Mobile Content Service is
decommissioned``，phabricator T328036），中英双语臂返回同一个 403，恒定产出 0 块
并记为 failed。留着一条 100% 失败的路径只会让下一个人以为自己用错了参数。

需要整篇正文（也就是需要有深度的语料库）时走另一条已在仓内且健康的路径：
``scripts/dump_wikimedia.py`` 用 MediaWiki Action API（``prop=extracts&explaintext=1``）
把条目落成本地 JSON，再用 ``scripts/index_knowledge.py --from-jsondir`` 入库。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Wikipedia REST API 端点
WIKI_API = {
    "zh": "https://zh.wikipedia.org/api/rest_v1",
    "en": "https://en.wikipedia.org/api/rest_v1",
}

# 内容集合映射：文章类型 → 知识库集合
COLLECTION_MAP = {
    "destination": "destinations",      # 目的地基础信息
    "visa": "visa_policies",            # 签证政策
    "culture": "local_culture",         # 文化习俗
    "tips": "travel_tips",             # 旅行攻略
}

# 请求配置
REQUEST_TIMEOUT = 30.0
REQUEST_DELAY = 0.5  # 每次请求间隔，防止过于频繁


class WikipediaFetcher:
    """
    Wikipedia 文章抓取器。
    支持中英文 Wikipedia，通过官方 REST API 获取文章内容。
    """

    def __init__(
        self,
        lang: str = "zh",
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        if lang not in WIKI_API:
            raise ValueError(f"不支持的 Wikipedia 语言: {lang}（支持: {list(WIKI_API.keys())}）")
        self.lang = lang
        self._base_url = WIKI_API[lang]
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "JourneyPilot/1.0 (research bot)",
                "Accept": "application/json",
            },
        )

    async def fetch_summary(self, title: str) -> Optional[Dict[str, Any]]:
        """
        获取 Wikipedia 文章摘要（约 500-1000 字）。
        返回：{"title": str, "content": str, "url": str} 或 None
        """
        encoded = httpx.URL(f"{self._base_url}/page/summary/{title}")
        try:
            resp = await self._client.get(encoded)
            resp.raise_for_status()
            data = resp.json()
            extract = data.get("extract", "").strip()
            if not extract:
                return None
            return {
                "title": data.get("title", title),
                "content": extract,
                "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
                "description": data.get("description", ""),
            }
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Wikipedia 未找到页面: {title}")
            else:
                logger.warning(f"Wikipedia 摘要获取失败 [{title}]: HTTP {e.response.status_code}")
            return None
        except Exception as e:
            logger.warning(f"Wikipedia 摘要获取异常 [{title}]: {e}")
            return None

    async def fetch_and_index(
        self,
        titles: List[str],
        collection: str = "destinations",
        content_type: str = "destination",
        lang_fallback: bool = True,
    ) -> Dict[str, int]:
        """
        批量抓取 Wikipedia 文章并写入知识库。

        参数：
          titles        - 文章标题列表（中文或英文名称）
          collection    - 目标知识库集合
          content_type  - 内容类型（destination / visa / culture / tips）
          lang_fallback - 中文找不到时是否尝试英文版

        返回：{"indexed": N, "failed": M}
        """
        from ..indexer import KnowledgeIndexer
        indexer = KnowledgeIndexer()

        indexed = 0
        failed = 0

        for title in titles:
            logger.info(f"正在获取 Wikipedia [{self.lang}]: {title}")
            docs = await self._fetch_as_docs(title, collection, content_type)

            # 中文找不到时尝试英文
            if not docs and lang_fallback and self.lang == "zh":
                logger.info(f"中文 Wikipedia 无内容，尝试英文: {title}")
                en_fetcher = WikipediaFetcher(lang="en")
                docs = await en_fetcher._fetch_as_docs(
                    title, collection, content_type
                )
                await en_fetcher.close()

            if not docs:
                logger.warning(f"Wikipedia 无内容可索引: {title}")
                failed += 1
                continue

            try:
                count = await indexer.index_documents(docs, collection=collection)
                indexed += count
                logger.info(f"已索引 [{title}] → {count} 个块 (集合: {collection})")
            except Exception as e:
                logger.error(f"索引失败 [{title}]: {e}")
                failed += 1

            await asyncio.sleep(REQUEST_DELAY)

        return {"indexed": indexed, "failed": failed}

    async def close(self) -> None:
        await self._client.aclose()

    # -----------------------------------------------------------------------
    # 内部辅助方法
    # -----------------------------------------------------------------------

    async def _fetch_as_docs(
        self,
        title: str,
        collection: str,
        content_type: str,
    ) -> List[Dict[str, Any]]:
        """抓取文章并组装为文档列表"""
        docs = []
        summary = await self.fetch_summary(title)
        if summary:
            content = self._format_summary(title, summary, content_type)
            docs.append({
                "content": content,
                "source": summary.get("url") or f"wikipedia/{self.lang}/{title}",
                "metadata": {
                    "title": title,
                    "lang": self.lang,
                    "type": content_type,
                    "description": summary.get("description", ""),
                },
            })
        return docs

    def _format_summary(
        self, title: str, summary: Dict[str, Any], content_type: str
    ) -> str:
        """将摘要格式化为便于 RAG 检索的文本"""
        lines = [f"目的地：{title}"]
        desc = summary.get("description", "")
        if desc:
            lines.append(f"类别：{desc}")
        lines.append("")
        lines.append(summary["content"])
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 预定义的热门旅游目的地列表
# ---------------------------------------------------------------------------

POPULAR_DESTINATIONS_ZH = [
    # 亚洲
    "日本", "东京", "京都", "大阪",
    "泰国", "曼谷", "清迈", "普吉岛",
    "新加坡",
    "马来西亚", "吉隆坡",
    "印度尼西亚", "巴厘岛",
    "越南", "河内", "胡志明市", "会安",
    "韩国", "首尔", "济州岛",
    "土耳其", "伊斯坦布尔", "卡帕多西亚",
    "印度", "孟买", "新德里", "泰姬陵",
    "尼泊尔", "加德满都",
    "阿联酋", "迪拜",
    "以色列", "耶路撒冷",
    # 欧洲
    "法国", "巴黎",
    "意大利", "罗马", "威尼斯", "佛罗伦萨",
    "西班牙", "巴塞罗那", "马德里",
    "希腊", "雅典", "圣托里尼",
    "英国", "伦敦",
    "德国", "柏林", "慕尼黑",
    "荷兰", "阿姆斯特丹",
    "瑞士", "苏黎世",
    "葡萄牙", "里斯本",
    "捷克", "布拉格",
    "奥地利", "维也纳",
    "北欧", "冰岛", "雷克雅未克",
    # 美洲
    "美国", "纽约", "洛杉矶", "旧金山", "拉斯维加斯",
    "加拿大", "温哥华", "多伦多",
    "墨西哥", "墨西哥城", "坎昆",
    "秘鲁", "马丘比丘",
    "阿根廷", "布宜诺斯艾利斯",
    "巴西", "里约热内卢",
    # 大洋洲
    "澳大利亚", "悉尼", "墨尔本",
    "新西兰", "奥克兰",
    # 非洲
    "埃及", "开罗", "卢克索",
    "摩洛哥", "马拉喀什",
    "南非", "开普敦",
    "肯尼亚", "内罗毕",
]
