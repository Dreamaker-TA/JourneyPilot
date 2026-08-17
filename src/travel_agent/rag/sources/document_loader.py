"""
结构化文档导入 (Application Layer)
支持 Markdown / TXT / JSON / PDF / DOCX 格式的知识库数据导入。

PDF/DOCX 走 `document_parse.py`：批量导入与上传共用同一组输入边界与同一个受限
解析子进程。给批量那一路开一条没有上限的旁路，等于让「本地目录」成为绕过全部
边界的入口。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from .document_parse import DocumentRejected, parse_file

logger = logging.getLogger(__name__)


class DocumentLoader:
    """
    批量文档加载器。
    支持从本地目录批量导入文档到知识库。
    """

    async def load_directory(
        self,
        directory: str,
        collection: str = "default",
        extensions: List[str] = None,
    ) -> Dict[str, int]:
        """
        扫描目录中的文档并批量索引。
        支持的格式：.txt / .md / .json
        返回：{"indexed": N, "failed": M, "files": K}
        """
        from ..indexer import KnowledgeIndexer
        indexer = KnowledgeIndexer()

        exts = extensions or [".txt", ".md", ".json", ".pdf", ".docx"]
        path = Path(directory)
        if not path.exists():
            logger.warning(f"目录不存在: {directory}")
            return {"indexed": 0, "failed": 0, "files": 0}

        files = [f for f in path.rglob("*") if f.suffix.lower() in exts]
        logger.info(f"发现 {len(files)} 个文档待索引")

        indexed_total = 0
        failed = 0
        for file_path in files:
            try:
                docs = await self._load_file(file_path)
                count = await indexer.index_documents(docs, collection=collection)
                indexed_total += count
                logger.debug(f"已索引: {file_path.name} → {count} 个块")
            except Exception as e:
                logger.error(f"文件索引失败 [{file_path}]: {e}")
                failed += 1

        return {"indexed": indexed_total, "failed": failed, "files": len(files)}

    async def load_file(
        self,
        file_path: str,
        collection: str = "default",
    ) -> int:
        """加载单个文件并索引，返回 chunk 数量"""
        from ..indexer import KnowledgeIndexer
        indexer = KnowledgeIndexer()

        path = Path(file_path)
        docs = await self._load_file(path)
        return await indexer.index_documents(docs, collection=collection)

    async def load_structured_destinations(
        self,
        data: List[Dict[str, Any]],
        collection: str = "destinations",
    ) -> int:
        """
        加载结构化目的地数据（JSON 格式）。
        每个条目会被转换为自然语言文本再索引。
        格式示例：
        {
            "name": "日本·京都",
            "highlights": ["金阁寺", "岚山竹林"],
            "best_season": "3月（樱花）/ 11月（红叶）",
            "budget_per_day": "500-800元",
            "tips": "JR Pass 最划算..."
        }
        """
        from ..indexer import KnowledgeIndexer
        indexer = KnowledgeIndexer()

        documents = []
        for item in data:
            text = self._destination_to_text(item)
            documents.append({
                "content": text,
                "source": f"destinations/{item.get('name', 'unknown')}",
                "metadata": {
                    "type": "destination",
                    "name": item.get("name", ""),
                    "country": item.get("country", ""),
                },
            })

        return await indexer.index_documents(documents, collection=collection)

    # -----------------------------------------------------------------------
    # 内部辅助
    # -----------------------------------------------------------------------

    async def _load_file(self, path: Path) -> List[Dict[str, Any]]:
        """将文件内容解析为文档列表"""
        suffix = path.suffix.lower()
        source = str(path)

        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                docs = []
                for item in data:
                    if isinstance(item, dict):
                        text = item.get("content") or self._dict_to_text(item)
                        docs.append({
                            "content": text,
                            "source": source,
                            "metadata": {k: v for k, v in item.items() if k != "content"},
                        })
                return docs
            else:
                return [{"content": json.dumps(data, ensure_ascii=False), "source": source}]

        try:
            parsed = await parse_file(path)
        except DocumentRejected as exc:
            raise ValueError(f"文档解析被拒（{exc.code}）：{path.name}") from exc
        if parsed.truncated:
            logger.warning("批量导入正文被截断 [%s]", path)
        return [
            {
                "content": parsed.text,
                "source": source,
                "metadata": {"file": path.name},
            }
        ]

    def _dict_to_text(self, d: Dict[str, Any]) -> str:
        """将字典转换为自然语言文本"""
        parts = []
        for k, v in d.items():
            if isinstance(v, list):
                v = "、".join(str(x) for x in v)
            parts.append(f"{k}：{v}")
        return "\n".join(parts)

    def _destination_to_text(self, item: Dict[str, Any]) -> str:
        """将目的地数据格式化为便于检索的文本"""
        parts = []
        name = item.get("name", "")
        if name:
            parts.append(f"目的地：{name}")

        for key, label in [
            ("country", "国家"),
            ("region", "地区"),
            ("description", "简介"),
            ("highlights", "必游景点"),
            ("best_season", "最佳旅行时间"),
            ("budget_per_day", "人均日预算"),
            ("transportation", "交通"),
            ("accommodation", "住宿"),
            ("food", "美食推荐"),
            ("tips", "旅行小贴士"),
            ("visa", "签证信息"),
        ]:
            val = item.get(key)
            if val:
                if isinstance(val, list):
                    val = "、".join(str(x) for x in val)
                parts.append(f"{label}：{val}")

        return "\n".join(parts)
