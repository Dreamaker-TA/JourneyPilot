"""
语义检索器 + Hybrid Search (Application Layer)

HybridRetriever：向量检索（pgvector 余弦相似度）+ lexical full-text 检索（PostgreSQL tsvector/ts_rank）
融合策略：Reciprocal Rank Fusion (RRF)
  fusion_score = sum(1 / (k + rank_i)) across each retrieval list
  k=60 是 RRF 经验常数，减少排名靠前的绝对领先效应

**每个分数都用它自己的名字**，没有一个通用的 ``score`` 字段：

| 字段 | 含义 | 可比性 |
|---|---|---|
| ``vector_score`` | pgvector 余弦相似度，0–1 | 同一 embedder 下跨查询可比，是唯一一个能施加"相关性下限"的量 |
| ``lexical_score`` | PostgreSQL ``ts_rank`` | 只在同一次查询内可比 |
| ``fusion_score`` | RRF，``1/(60+rank)`` 之和 | **只编码排名，不编码相关性**；top-1 恒为 0.0164，跨列表的 rank-1 全部同分 |
| ``rerank_score`` | 精排器给的绝对相关性（LLM 1–5 / CrossEncoder logit） | 跨查询可比，是排序权威 |

融合只**新增** ``fusion_score``，两条臂各自的分数原样带下去。**不许**把 ``score`` 覆盖成
rank 派生分：相似度会在融合那一步丢掉，下游既无法再施加相关性下限，也分不清"排第一"和
"真的相关"。

Step 4：Reranking 二阶段精排
  初检：Hybrid Search 扩大候选集；精排：CrossEncoderReranker / LLMReranker
  精排**不在这一层做**——它属于 ``rag.retrieval_pipeline``，那里一次查询只精排
  一次、在融合后的完整候选池上排，排完的顺序就是最终顺序。本层的
  ``retrieve(rerank=True)`` 只服务"单查询单集合"这种直接调用。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from ..config import get_settings
from ..infrastructure.database import get_db_session
from .reranker import get_reranker

logger = logging.getLogger(__name__)

# RRF 经验常数：降低排名绝对优势的影响
_RRF_K = 60

_TS_CONFIG_CTE = """
WITH ts_cfg AS (
    SELECT COALESCE(
        (SELECT oid FROM pg_catalog.pg_ts_config WHERE cfgname = 'chinese'),
        'simple'::regconfig::oid
    )::regconfig AS cfg
), ts_query AS (
    -- 把整句切成词之后用 ``|`` 连起来，也就是**任一词命中**即算候选。
    --
    -- 此前这里是 ``plainto_tsquery``，而它把每个词用 ``&`` 连起来 —— 一句
    -- 「锂电池和三脚架能不能带上高铁」要求同一段正文里同时出现全部六个词，
    -- 于是这条臂对任何一句真实提问都返回 0 条：日志里每一行都是
    -- ``向量 N + lexical 0``，「向量 + BM25 + RRF 混合检索」实际只有一条臂在跑，
    -- 而 RRF 融合一条臂等于原样照抄它的排名。单词实测 27 命中、两个词 0 命中，
    -- 改成 ``|`` 之后同一句 65 命中。
    --
    -- 分词只有库里那份配置做得到（``tsv`` 也是用同一个 config 生成的列），所以
    -- 词是用 ``to_tsvector`` 切出来再拼回 tsquery 的；``quote_literal`` 负责让
    -- 每个词作为字面量进 ``to_tsquery``。一个词都切不出来时拼出空串，
    -- ``to_tsquery`` 给出空 tsquery（匹配不到任何行），不报错。
    SELECT to_tsquery(
        (SELECT cfg FROM ts_cfg),
        COALESCE((
            SELECT string_agg(quote_literal(lexeme), ' | ')
            FROM unnest(
                tsvector_to_array(to_tsvector((SELECT cfg FROM ts_cfg), :query))
            ) AS lexeme
        ), '')
    ) AS tsq
)
"""


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick_published_at(meta: Dict[str, Any]) -> str:
    if not isinstance(meta, dict):
        return ""
    for key in (
        "published_at",
        "publishedAt",
        "updated_at",
        "updatedAt",
        "last_updated",
        "lastUpdated",
        "update_time",
        "timestamp",
    ):
        value = meta.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _parse_row_metadata(row) -> tuple:
    """解析数据库行的 metadata JSON，返回 (meta_dict, published_at)。"""
    import json as _json
    meta = row["metadata"]
    if isinstance(meta, str):
        meta = _json.loads(meta)
    if not isinstance(meta, dict):
        meta = {}
    return meta, _pick_published_at(meta)


def _row_to_doc(row, *, arm: str, retrieved_at: str) -> dict:
    """将数据库行转为标准文档字典。

    分数写在以检索臂命名的键上（``vector_score`` / ``lexical_score``），而不是
    一个通用的 ``score``：两条臂的分数量纲不同，合并之后没有名字就没法再分辨
    手里这个数到底是相似度还是排名。
    """
    meta, published_at = _parse_row_metadata(row)
    score_key = f"{arm}_score"
    return {
        "content": row["content"],
        "source": row["source"],
        score_key: float(row[score_key]),
        "metadata": meta,
        "retrieval_method": arm,
        "retrieved_at": retrieved_at,
        "published_at": published_at,
    }


def _build_filter_sql(filters: Optional[Dict[str, Any]]) -> tuple[str, Dict[str, Any]]:
    """Build safe SQL snippets for supported source and metadata exact filters."""
    if not filters:
        return "", {}
    clauses: List[str] = []
    params: Dict[str, Any] = {}
    idx = 0

    source = filters.get("source")
    if source:
        idx += 1
        if isinstance(source, list):
            values = [str(v) for v in source if str(v)]
            if values:
                placeholders = []
                for value_idx, value in enumerate(values):
                    name = f"filter_{idx}_{value_idx}"
                    params[name] = value
                    placeholders.append(f":{name}")
                clauses.append(f"source IN ({', '.join(placeholders)})")
        else:
            params[f"filter_{idx}"] = str(source)
            clauses.append(f"source = :filter_{idx}")

    metadata_filters = filters.get("metadata") if isinstance(filters.get("metadata"), dict) else {
        k: v for k, v in filters.items() if k not in {"source", "metadata"}
    }
    for key, value in metadata_filters.items():
        if value in (None, "", [], {}):
            continue
        idx += 1
        params[f"filter_key_{idx}"] = str(key)
        if isinstance(value, list):
            values = [str(v) for v in value if str(v)]
            if not values:
                continue
            placeholders = []
            for value_idx, item in enumerate(values):
                name = f"filter_val_{idx}_{value_idx}"
                params[name] = item
                placeholders.append(f":{name}")
            clauses.append(f"(metadata ->> :filter_key_{idx}) IN ({', '.join(placeholders)})")
        else:
            params[f"filter_val_{idx}"] = str(value)
            clauses.append(f"(metadata ->> :filter_key_{idx}) = :filter_val_{idx}")

    return (" AND " + " AND ".join(clauses)) if clauses else "", params


class HybridRetriever:
    """
    Hybrid Search 检索器：向量检索 + PostgreSQL lexical full-text 检索 + RRF 融合。

    检索策略：
    1. 向量检索：基于 pgvector 余弦相似度，捕获语义相关性
    2. 词法全文检索：基于 PostgreSQL tsvector/ts_rank，精准命中专有名词（地名、酒店名、景点名等）
    3. RRF 融合：合并两路结果，去重后按融合分数排序
    """

    def __init__(self, embedder=None) -> None:
        self._embedder = embedder

    def _get_embedder(self):
        if self._embedder is None:
            from ..models.embedder import get_embedder
            self._embedder = get_embedder()
        return self._embedder

    async def retrieve(
        self,
        query: str,
        collection: str = "default",
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
        filters: Optional[Dict[str, Any]] = None,
        use_hybrid: bool = True,
        rerank: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """
        混合检索，返回相关文档块列表。

        每个文档块：``{"content", "source", "metadata", "retrieval_method",
        "retrieved_at", "published_at"}``，外加它实际拿到的分数字段——
        ``vector_score`` / ``lexical_score``（来自哪条臂就有哪个）、融合后多一个
        ``fusion_score``、精排后多一个 ``rerank_score``。没有通用 ``score``。

        Args:
            use_hybrid: True=向量+lexical full-text 融合（默认），False=纯向量
            rerank: True=强制开启精排，False=强制关闭，None=读 config（默认）。
                多查询/多集合的调用方应当传 False，并把精排交给
                ``rag.retrieval_pipeline``——那里一次查询只排一次，且排完的顺序
                就是最终顺序。
        """
        settings = get_settings()
        rerank_cfg = settings.rerank

        # 确定是否开启 reranking
        do_rerank = rerank if rerank is not None else rerank_cfg.enabled
        reranker = get_reranker() if do_rerank else None

        if reranker is not None:
            # 二阶段检索：扩大初检候选集，精排后缩减
            initial_k = rerank_cfg.initial_top_k
            final_k = top_k or rerank_cfg.final_top_k
            raw_docs = await (
                self._hybrid_retrieve(query, collection, initial_k, score_threshold, filters=filters)
                if use_hybrid
                else self._vector_retrieve(query, collection, initial_k, score_threshold, filters=filters)
            )
            reranked = await reranker.rerank(query, raw_docs, top_k=final_k)
            return reranked

        # 单阶段检索（无精排）
        if use_hybrid:
            return await self._hybrid_retrieve(query, collection, top_k, score_threshold, filters=filters)
        return await self._vector_retrieve(query, collection, top_k, score_threshold, filters=filters)

    async def _vector_retrieve(
        self,
        query: str,
        collection: str,
        top_k: Optional[int],
        score_threshold: Optional[float],
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """纯向量检索。

        ``threshold`` 是**召回下限**，不是相关性判据：它只作用在这一条臂上，且
        当前 embedder 的噪声地板实测在 0.44–0.54（完全无关的文本也能拿到这个
        分），所以任何低于噪声地板的取值都咬不住东西。相关性由 reranker 与
        CRAG 分级器判。
        """
        settings = get_settings()
        k = top_k or settings.rag.top_k
        threshold = score_threshold if score_threshold is not None else settings.rag.score_threshold

        embedder = self._get_embedder()
        query_vec = await embedder.embed(query)
        vec_str = f"[{','.join(str(v) for v in query_vec)}]"
        filter_sql, filter_params = _build_filter_sql(filters)

        async with get_db_session() as session:
            result = await session.execute(
                text(f"""
                    SELECT
                        content,
                        source,
                        metadata,
                        1 - (embedding <=> CAST(:query_vec AS vector)) AS vector_score
                    FROM knowledge_chunks
                    WHERE collection = :col
                      AND 1 - (embedding <=> CAST(:query_vec AS vector)) >= :threshold
                      {filter_sql}
                    ORDER BY embedding <=> CAST(:query_vec AS vector)
                    LIMIT :k
                """),
                {
                    "query_vec": vec_str,
                    "col": collection,
                    "threshold": threshold,
                    "k": k,
                    **filter_params,
                },
            )
            rows = result.mappings().fetchall()

        retrieved_at = _utc_iso_now()
        docs = [_row_to_doc(row, arm="vector", retrieved_at=retrieved_at) for row in rows]

        best_score = max((d["vector_score"] for d in docs), default=0.0)
        logger.debug(
            f"向量检索 [{collection}]: '{query[:30]}' → {len(docs)} 个结果, "
            f"best_similarity={best_score:.3f}"
        )
        return docs

    async def _lexical_retrieve(
        self,
        query: str,
        collection: str,
        top_k: int,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        PostgreSQL lexical full-text 检索（tsvector + ts_rank）。

        查询词由 ``_TS_CONFIG_CTE`` 的 ``ts_query`` 拼成「任一词命中」的 tsquery，
        排序仍归 ``ts_rank``（命中得多、命中稀有词的段排在前面）。
        """
        filter_sql, filter_params = _build_filter_sql(filters)
        async with get_db_session() as session:
            result = await session.execute(
                text(f"""
                    {_TS_CONFIG_CTE}
                    SELECT
                        content,
                        source,
                        metadata,
                        ts_rank(tsv, (SELECT tsq FROM ts_query)) AS lexical_score
                    FROM knowledge_chunks
                    WHERE collection = :col
                      AND tsv @@ (SELECT tsq FROM ts_query)
                      {filter_sql}
                    ORDER BY lexical_score DESC
                    LIMIT :k
                """),
                {
                    "query": query,
                    "col": collection,
                    "k": top_k * 2,  # 多取一些，RRF 融合时保证覆盖
                    **filter_params,
                },
            )
            rows = result.mappings().fetchall()

        retrieved_at = _utc_iso_now()
        docs = [_row_to_doc(row, arm="lexical", retrieved_at=retrieved_at) for row in rows]

        logger.debug(f"Lexical full-text 检索 [{collection}]: '{query[:30]}' → {len(docs)} 个结果")
        return docs

    async def _hybrid_retrieve(
        self,
        query: str,
        collection: str,
        top_k: Optional[int],
        score_threshold: Optional[float],
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        混合检索：向量 + lexical full-text + RRF 融合。

        RRF 公式：rrf_score(d) = Σ (1 / (k + rank_i(d)))
        其中 rank_i(d) 是文档 d 在第 i 路检索中的排名（从 1 开始）
        """
        settings = get_settings()
        k = top_k or settings.rag.top_k

        # 两路并行；单路失败隔离为 []，保留另一路结果。
        # 双路皆挂 → 返回 []（与空检索一致；上层可标 retrieval_mode=unavailable）。
        vector_raw, lexical_raw = await asyncio.gather(
            self._vector_retrieve(
                query, collection, top_k=k * 2, score_threshold=score_threshold, filters=filters
            ),
            self._lexical_retrieve(query, collection, top_k=k, filters=filters),
            return_exceptions=True,
        )

        vector_results: List[Dict[str, Any]]
        lexical_results: List[Dict[str, Any]]
        if isinstance(vector_raw, BaseException):
            logger.warning(
                "Hybrid 检索向量路失败 [%s]: %s: %s",
                collection,
                type(vector_raw).__name__,
                vector_raw,
            )
            vector_results = []
        else:
            vector_results = list(vector_raw or [])

        if isinstance(lexical_raw, BaseException):
            logger.warning(
                "Hybrid 检索 lexical 路失败 [%s]: %s: %s",
                collection,
                type(lexical_raw).__name__,
                lexical_raw,
            )
            lexical_results = []
        else:
            lexical_results = list(lexical_raw or [])

        if (
            isinstance(vector_raw, BaseException)
            and isinstance(lexical_raw, BaseException)
        ):
            logger.error(
                "Hybrid 检索双路均失败 [%s]: vector=%s lexical=%s",
                collection,
                type(vector_raw).__name__,
                type(lexical_raw).__name__,
            )
            return []

        # RRF 融合（单路非空时 rrf_fuse 仍合法）
        fused = rrf_fuse([vector_results, lexical_results], top_k=k)

        best_score = max((d["fusion_score"] for d in fused), default=0.0)
        logger.info(
            f"Hybrid 检索 [{collection}]: '{query[:30]}' → "
            f"向量 {len(vector_results)} + lexical {len(lexical_results)} → 融合 {len(fused)} 个, best_rrf={best_score:.4f}"
        )
        return fused

    def format_docs_for_prompt(self, docs: List[Dict[str, Any]]) -> str:
        """将检索结果格式化为 Prompt 可用的字符串。

        每条只写它**真有**的那个数，并且写清那个数是什么。旧文案把 RRF 融合分
        标成「相关度」，而那个数对所有结果几乎逐字相同、只编码排名——模型据此
        判断可信度就是被误导。融合分不写给模型：名次已经由
        ``[参考i]`` 的序号表达了。
        """
        if not docs:
            return "暂无相关知识库内容。"
        parts = []
        for i, doc in enumerate(docs, 1):
            method = doc.get("retrieval_method", "vector")
            method_tag = "语义" if method == "vector" else "关键词" if method == "lexical" else "融合"

            # 优先展示 original_content（不含 LLM 生成的上下文前缀）
            display_content = doc.get("original_content") or doc["content"]

            if doc.get("rerank_score") is not None:
                score_info = f"相关性评分：{doc['rerank_score']:.1f}（精排判定）"
            elif doc.get("vector_score") is not None:
                score_info = f"向量相似度：{doc['vector_score']:.3f}"
            else:
                score_info = "关键词命中"

            freshness_bits = []
            if doc.get("published_at"):
                freshness_bits.append(f"发布时间：{doc['published_at']}")
            if doc.get("retrieved_at"):
                freshness_bits.append(f"检索时间：{doc['retrieved_at']}")
            freshness_info = "，".join(freshness_bits)
            if freshness_info:
                freshness_info = f", {freshness_info}"

            # 引用标识由服务端铸造（``rag.source_records``）：模型引用它，服务端
            # 据此接地并自己写这条 SourceRecord。没有标识就说明这段内容不是可
            # 引用证据（例如快速路径的答案上下文），此时不打印，免得邀请模型
            # 去引用一个落不了地的东西。
            citation = doc.get("source_record_id")
            citation_info = f"引用标识：{citation}, " if citation else ""
            parts.append(
                f"[参考{i}] {citation_info}来源：{doc['source']} "
                f"({score_info}, {method_tag}检索{freshness_info})\n{display_content}"
            )
        return "\n\n---\n\n".join(parts)


_ARM_SCORE_KEYS = ("vector_score", "lexical_score", "rerank_score")


def rrf_fuse(
    ranked_lists: List[List[Dict[str, Any]]],
    top_k: int,
    k: int = _RRF_K,
) -> List[Dict[str, Any]]:
    """
    Reciprocal Rank Fusion：把任意多条已排序的检索结果合成一条。

    两处都用它：检索器内部融合 向量 / lexical 两条臂，检索管线融合
    (集合 × 查询变体) 的每一条结果。一个文档被越多条列表命中，
    ``fusion_score`` 越高——这正是跨列表合并唯一能用的信号。

    去重键 = ``content`` 精确匹配。合并规则：
    - ``fusion_score`` 累加（新增字段，**不覆盖**任何已有分数）
    - 各臂原始分数（``vector_score`` / ``lexical_score`` / ``rerank_score``）
      取所见的最大值带下去，好让下游还能施加相关性下限
    - 其余字段以首见为准
    - ``retrieval_method``：命中多于一条列表时记 ``hybrid``，否则保留它自己的臂名
    """
    doc_map: Dict[str, Dict[str, Any]] = {}
    rrf_scores: Dict[str, float] = {}
    hit_lists: Dict[str, int] = {}

    for result_list in ranked_lists:
        for rank, doc in enumerate(result_list, start=1):
            key = doc["content"]
            if key not in doc_map:
                doc_map[key] = dict(doc)
                rrf_scores[key] = 0.0
                hit_lists[key] = 0
            else:
                merged = doc_map[key]
                for score_key in _ARM_SCORE_KEYS:
                    incoming = doc.get(score_key)
                    if incoming is None:
                        continue
                    existing = merged.get(score_key)
                    merged[score_key] = (
                        incoming if existing is None else max(existing, incoming)
                    )
            rrf_scores[key] += 1.0 / (k + rank)
            hit_lists[key] += 1

    sorted_keys = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
    result = []
    for key in sorted_keys[:top_k]:
        doc = dict(doc_map[key])
        doc["fusion_score"] = rrf_scores[key]
        if hit_lists[key] > 1:
            doc["retrieval_method"] = "hybrid"
        result.append(doc)

    return result
