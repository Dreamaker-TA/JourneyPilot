"""
文档索引管道 (Application Layer)
将文档块向量化后写入 pgvector 知识库。

支持三种分块策略（通过 config.rag.chunker_type 或参数控制）：
  text        — TextChunker（同步，固定窗口）
  semantic    — SemanticChunker（异步，语义边界）
  contextual  — ContextualChunker（异步，语义分块 + LLM 上下文前缀）
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from ..infrastructure.database import get_db_session
from .chunker import get_chunker

logger = logging.getLogger(__name__)

# 没有来源名的那一篇在界面上叫什么。**这个名字只在这里写一次**：它同时是写入时的
# 规范化目标（`_normalize_source`）与统计时的显示名（`get_collection_stats`）。
# 两处各写一份的后果是「列表里印着 X、按 X 去读正文却 404」——同一篇资料在两张表上
# 叫了两个名字。
UNNAMED_SOURCE = "未命名来源"


def _normalize_source(source: str) -> str:
    """一篇资料的来源名就是它的身份，所以空名在写入时就落定，不留到读取时再猜。"""

    return (source or "").strip() or UNNAMED_SOURCE


class KnowledgeIndexer:
    """
    知识库文档索引器。
    负责：文档分块 → 向量化 → 写入 PostgreSQL。

    一篇资料的正文住在 `knowledge_documents`（一篇一行，`(collection, source)` 唯一），
    段住在 `knowledge_chunks`。**正文是原件、段是投影**：入库、改写、删除都从正文那一行
    出发，段整批重算。所以同名重新入库是替换 —— 此前它是追加，同一份文件上传两次
    会得到两套段，而检索层看到的是两份彼此重叠的资料。
    """

    def __init__(self, embedder=None, chunker_type: Optional[str] = None) -> None:
        self._embedder = embedder
        self._chunker_type = chunker_type  # None → 读 config
        self._chunker = None  # 延迟初始化

    def _get_embedder(self):
        if self._embedder is None:
            from ..models.embedder import get_embedder
            self._embedder = get_embedder()
        return self._embedder

    def _get_chunker(self):
        if self._chunker is None:
            self._chunker = get_chunker(self._chunker_type)
        return self._chunker

    async def _split_chunks(
        self,
        text: str,
        source: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """统一的分块入口（自动处理同步/异步分块器）"""
        chunker = self._get_chunker()
        if asyncio.iscoroutinefunction(chunker.split):
            return await chunker.split(text, source=source, metadata=metadata)
        return chunker.split(text, source=source, metadata=metadata)

    async def index_text(
        self,
        text: str,
        source: str,
        collection: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """索引单篇资料：正文落一行、段整批重算。返回写入的 chunk 数量。

        同一个 `(collection, source)` 再进来一次是**替换**：旧段先删、正文覆盖写。
        分块（可能要调 embedding 与 fast 模型）在事务之外先做完 —— 一次失败不该让
        用户的资料只剩半份。
        """
        normalized_source = _normalize_source(source)
        chunks = await self._split_chunks(
            text, source=normalized_source, metadata=metadata
        )
        if not chunks:
            return 0
        return await self._write_document(
            content=text,
            source=normalized_source,
            collection=collection,
            metadata=metadata or {},
            chunks=chunks,
        )

    async def get_document(
        self, collection: str, source: str
    ) -> Optional[Dict[str, Any]]:
        """一篇资料的正文与它当前的段数；这一篇不存在时返回 None。

        `chunk_count` 从段表现算而不是记在正文那一行上：那个数由段表负责，
        在正文行上再存一份就是同一个数的第二个副本。
        """
        normalized_source = _normalize_source(source)
        async with get_db_session() as session:
            row = (
                await session.execute(
                    text("""
                        SELECT d.source,
                               d.content,
                               d.updated_at,
                               (
                                   SELECT COUNT(*)
                                   FROM knowledge_chunks c
                                   WHERE c.collection = d.collection
                                     AND c.source = d.source
                               ) AS chunk_count
                        FROM knowledge_documents d
                        WHERE d.collection = :col AND d.source = :src
                    """),
                    {"col": collection, "src": normalized_source},
                )
            ).mappings().first()
        if row is None:
            return None
        return {
            "source": row["source"],
            "content": row["content"],
            "chunk_count": int(row["chunk_count"]),
            "updated_at": row["updated_at"],
        }

    async def count_source_chunks(self, collection: str, source: str) -> int:
        """这个来源名下现在有多少段。

        它存在的理由是**区分「没有这一篇」与「这一篇的正文没有留存」**：
        `knowledge_documents` 之前入库的资料只有段、没有正文行，读正文必须报出那件事
        本身，而不是回落去拼段（拼出来的不是原文，见建表处的注释）。
        """
        normalized_source = _normalize_source(source)
        async with get_db_session() as session:
            row = (
                await session.execute(
                    text("""
                        SELECT COUNT(*) AS chunk_count
                        FROM knowledge_chunks
                        WHERE collection = :col AND source = :src
                    """),
                    {"col": collection, "src": normalized_source},
                )
            ).mappings().first()
        return int(row["chunk_count"]) if row else 0

    async def delete_source(self, collection: str, source: str) -> Dict[str, Any]:
        """删除一篇资料（正文行 + 它的全部段）。

        返回 `{"deleted_chunks": int, "existed": bool}`。**两个数都要交出去**：
        「删掉了 0 段」不等于「这一篇不存在」（一篇正文可以一段都没有），
        少了 `existed`，调用方只能拿段数猜，而猜错的那一次会把一次成功的删除
        报成 404。

        正文与段在同一个事务里一起消失：留下没有正文的段等于留下一份检索得到、
        界面打不开的资料。
        """
        normalized_source = _normalize_source(source)
        async with get_db_session() as session:
            chunk_rows = await session.execute(
                text("""
                    DELETE FROM knowledge_chunks
                    WHERE collection = :col AND source = :src
                    RETURNING id
                """),
                {"col": collection, "src": normalized_source},
            )
            deleted = len(chunk_rows.fetchall())
            document_rows = await session.execute(
                text("""
                    DELETE FROM knowledge_documents
                    WHERE collection = :col AND source = :src
                    RETURNING id
                """),
                {"col": collection, "src": normalized_source},
            )
            document_existed = len(document_rows.fetchall()) > 0
        logger.info(
            f"删除资料 [{normalized_source}] @ [{collection}]，共 {deleted} 个 chunk"
        )
        return {
            "deleted_chunks": deleted,
            "existed": document_existed or deleted > 0,
        }

    async def index_documents(
        self,
        documents: List[Dict[str, Any]],
        collection: str = "default",
    ) -> int:
        """
        批量索引文档列表。
        文档结构：{"content": str, "source": str, "metadata": dict}

        与 `index_text` 是同一条写入路径（正文一行、段一批、同名即替换），只是把整批
        文档的段**合成一次向量化调用** —— 语料入库一次几千段，逐篇 embed 是几千次往返。
        """
        prepared: List[Dict[str, Any]] = []
        for doc in documents:
            content = doc.get("content", "")
            source = _normalize_source(doc.get("source", ""))
            metadata = doc.get("metadata", {}) or {}
            chunks = await self._split_chunks(
                content, source=source, metadata=metadata
            )
            if not chunks:
                continue
            prepared.append(
                {
                    "content": content,
                    "source": source,
                    "metadata": metadata,
                    "chunks": chunks,
                }
            )

        if not prepared:
            return 0
        return await self._write_documents(prepared, collection)

    async def delete_collection(self, collection: str) -> int:
        """删除整个集合（全部段 + 全部正文），返回删除的段数。"""
        async with get_db_session() as session:
            result = await session.execute(
                text("DELETE FROM knowledge_chunks WHERE collection = :col RETURNING id"),
                {"col": collection},
            )
            count = len(result.fetchall())
            # 正文行跟着走。留下正文而删掉段，界面上就会有一篇「0 段」的资料参与不了
            # 任何检索却打得开 —— 整库删的语义是这个集合不存在了，不是它空了。
            await session.execute(
                text("DELETE FROM knowledge_documents WHERE collection = :col"),
                {"col": collection},
            )
        logger.info(f"删除集合 [{collection}] 共 {count} 个 chunk")
        return count

    async def get_collection_stats(self, collection: str) -> Dict[str, Any]:
        """获取集合统计信息。

        返回集合总量以及每个来源的明细（来源名 + 资料段数量），供资料库
        列表逐条渲染。
        """
        async with get_db_session() as session:
            summary_row = (
                await session.execute(
                    text("""
                        SELECT COUNT(*) as total,
                               COUNT(DISTINCT source) as sources,
                               MIN(created_at) as oldest,
                               MAX(created_at) as newest
                        FROM knowledge_chunks
                        WHERE collection = :col
                    """),
                    {"col": collection},
                )
            ).mappings().first()

            detail_rows = (
                await session.execute(
                    text("""
                        SELECT COALESCE(NULLIF(source, ''), :unnamed) as source,
                               COUNT(*) as chunk_count,
                               MAX(created_at) as updated_at
                        FROM knowledge_chunks
                        WHERE collection = :col
                        GROUP BY COALESCE(NULLIF(source, ''), :unnamed)
                        ORDER BY MAX(created_at) DESC
                    """),
                    {"col": collection, "unnamed": UNNAMED_SOURCE},
                )
            ).mappings().all()

        stats: Dict[str, Any] = dict(summary_row) if summary_row else {}
        stats["source_details"] = [
            {
                "source": row["source"],
                "chunk_count": int(row["chunk_count"]),
                "updated_at": row["updated_at"],
            }
            for row in detail_rows
        ]
        return stats

    # -----------------------------------------------------------------------
    # 内部实现
    # -----------------------------------------------------------------------

    async def _write_document(
        self,
        content: str,
        source: str,
        collection: str,
        metadata: Dict[str, Any],
        chunks: List[Dict[str, Any]],
    ) -> int:
        """一篇资料的写入 —— 单篇是批量那条路的 n=1，不是第二份实现。"""

        return await self._write_documents(
            [
                {
                    "content": content,
                    "source": source,
                    "metadata": metadata,
                    "chunks": chunks,
                }
            ],
            collection,
        )

    async def _write_documents(
        self, documents: List[Dict[str, Any]], collection: str
    ) -> int:
        """向量化并写入若干篇资料：正文覆盖写、旧段先删、新段整批插入，一个事务。

        入参每一项是 `{"content", "source", "metadata", "chunks"}` —— 分块已经做完
        （那一步可能要调模型，不该占着事务）。
        """
        embedder = self._get_embedder()
        all_chunks = [chunk for doc in documents for chunk in doc["chunks"]]
        # 用带上下文前缀的 content 做 embedding（Contextual Chunking 的关键）
        texts = [c["content"] for c in all_chunks]

        try:
            vectors = await embedder.embed_batch(texts)
        except Exception as e:
            logger.error(f"向量化失败: {e}")
            raise

        records = []
        for chunk, vec in zip(all_chunks, vectors):
            records.append({
                "collection": collection,
                "content": chunk["content"],
                # original_content 来自 ContextualChunker（无前缀的原始文本），
                # TextChunker/SemanticChunker 不提供此字段时存 NULL
                "original_content": chunk.get("original_content"),
                "source": chunk.get("source", ""),
                "metadata": json.dumps(chunk.get("metadata", {}), ensure_ascii=False),
                "embedding": f"[{','.join(str(v) for v in vec)}]",
            })

        async with get_db_session() as session:
            for doc in documents:
                await session.execute(
                    text("""
                        DELETE FROM knowledge_chunks
                        WHERE collection = :col AND source = :src
                    """),
                    {"col": collection, "src": doc["source"]},
                )
                await session.execute(
                    text("""
                        INSERT INTO knowledge_documents
                            (collection, source, content, metadata)
                        VALUES
                            (:col, :src, :content, CAST(:metadata AS jsonb))
                        ON CONFLICT (collection, source) DO UPDATE
                        SET content = EXCLUDED.content,
                            metadata = EXCLUDED.metadata,
                            updated_at = NOW()
                    """),
                    {
                        "col": collection,
                        "src": doc["source"],
                        "content": doc["content"],
                        "metadata": json.dumps(
                            doc.get("metadata", {}), ensure_ascii=False
                        ),
                    },
                )
            for r in records:
                await session.execute(
                    text("""
                        INSERT INTO knowledge_chunks
                            (collection, content, original_content, source, metadata, embedding)
                        VALUES
                            (:collection, :content, :original_content, :source,
                             CAST(:metadata AS jsonb),
                             CAST(:embedding AS vector))
                    """),
                    r,
                )

        logger.info(
            f"成功索引 {len(documents)} 篇资料 / {len(records)} 个 chunk 到集合 [{collection}]"
        )
        return len(records)
