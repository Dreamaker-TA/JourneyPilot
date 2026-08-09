"""
离线知识库构建脚本 — 非主调用路径。

由 scripts/build_index.py 调用, FastAPI 运行时不会触达此模块。
将网络上的旅行信息抓取为文本后, 再交由索引器建入知识库。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# 请求超时配置
REQUEST_TIMEOUT = 30.0


class WebCrawler:
    """
    简单的网页内容提取器。
    通过 httpx 获取网页，使用 BeautifulSoup 提取正文。
    """

    def __init__(self, timeout: float = REQUEST_TIMEOUT) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "JourneyPilot/1.0 (research bot)"
            },
        )

    async def fetch_text(self, url: str) -> Optional[str]:
        """
        抓取网页并提取纯文本内容。
        返回清洗后的文本，失败则返回 None。
        """
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            return self._extract_text(response.text, url)
        except Exception as e:
            logger.warning(f"网页抓取失败 [{url}]: {e}")
            return None

    async def crawl_and_index(
        self,
        urls: List[str],
        collection: str = "destinations",
        metadata_extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        """
        批量爬取并索引。
        返回：{"indexed": N, "failed": M}
        """
        from ..indexer import KnowledgeIndexer
        indexer = KnowledgeIndexer()

        indexed = 0
        failed = 0
        for url in urls:
            text = await self.fetch_text(url)
            if not text:
                failed += 1
                continue
            try:
                meta = {"url": url, "crawled": True}
                if metadata_extra:
                    meta.update(metadata_extra)
                count = await indexer.index_text(
                    text=text,
                    source=url,
                    collection=collection,
                    metadata=meta,
                )
                indexed += count
                logger.info(f"已索引 {count} 个块来自: {url}")
            except Exception as e:
                logger.error(f"索引失败 [{url}]: {e}")
                failed += 1

        return {"indexed": indexed, "failed": failed}

    async def close(self) -> None:
        await self._client.aclose()

    def _extract_text(self, html: str, url: str) -> str:
        """从 HTML 提取主要文本内容"""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")

            # 移除无用标签
            for tag in soup(["script", "style", "nav", "footer", "header",
                              "aside", "iframe", "noscript", "form"]):
                tag.decompose()

            # 提取主要内容区域（优先级：main > article > body）
            main = soup.find("main") or soup.find("article") or soup.body
            if not main:
                return ""

            text = main.get_text(separator="\n", strip=True)
            # 清理多余空行
            text = re.sub(r"\n{3,}", "\n\n", text)
            # 限制长度（避免过大文档）
            return text[:50000]

        except ImportError:
            # BeautifulSoup 未安装时，简单提取纯文本
            clean = re.sub(r"<[^>]+>", " ", html)
            clean = re.sub(r"\s+", " ", clean).strip()
            return clean[:50000]
