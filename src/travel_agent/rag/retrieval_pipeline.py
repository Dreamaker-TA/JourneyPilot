"""一次查询 = 一次扩写 + 一次融合 + 一次精排。

这个模块存在的理由是三条同族缺陷，它们是同一个病：
**买到了东西，然后在下一步把它丢掉。**

旧形状是「按集合循环，循环体里做全套」：

    for col in collections:
        rewrite_and_retrieve(query, col, ...)   # 内部：改写 → HyDE → N 路检索 → N 次精排

于是
- 改写与 HyDE 只取决于 query、与集合无关，却被重算 ``len(collections)`` 遍
  （实测四条逐字相同的改写日志），空集合也照付这笔 LLM 钱；
- 精排按 query 变体各跑一次、每路各自截到 3 条再合并，比在融合后的候选池上
  排一次贵 N 倍，还在融合前就丢掉了本可胜出的候选；
- 合并那一步按 ``score``（RRF 分）排序，而精排把分写在 ``rerank_score`` 上，
  于是刚花钱买到的精排顺序**在最终顺序里完全不生效**，只用来做了每路的截断。

新形状把每件事各做一次，并且让最后做的那件事说了算：

    变体 = expand_query(query)                    ← 每查询一次，不是每集合一次
    列表 = retrieve(变体 × 集合)                   ← 纯检索，不精排
    候选池 = rrf_fuse(所有列表)                    ← 被越多变体/集合命中的排越前
    结果 = rerank(原始 query, 候选池)[:top_k]      ← 每查询一次，且它就是最终顺序

精排用**原始 query** 而不是某个变体：相关性是相对用户真正问的那句话而言的。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config import get_settings
from .query_rewriter import QueryRewriter
from .reranker import get_reranker
from .retriever import rrf_fuse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievalOutcome:
    """一次检索管线的产出，连同它实际做过的事。

    ``query_variants`` 是真正打进检索的那几条查询，调用方要把它写进
    ``build_retrieval_summary(rewritten_queries=...)``——两个调用方过去都硬写
    ``[query]``，等于让检索摘要谎报自己没有改写过。
    """

    docs: List[Dict[str, Any]]
    query_variants: List[str] = field(default_factory=list)
    pool: List[Dict[str, Any]] = field(default_factory=list)
    reranked: bool = False


async def expand_query(
    query: str,
    *,
    use_rewrite: bool,
    use_multi_query: bool,
    use_hyde: bool,
) -> List[str]:
    """把一条 query 展开成若干检索变体。**每条 query 只做一次。**

    ``use_rewrite`` 是策略层算出来的决定（"这条 query 含糊或初检不佳吗"），**必须读它**：
    只看 ``use_multi_rewrite``、False 就无条件走单路改写的话，一条明确的 query 也要付一次
    改写钱。
    """

    if not use_rewrite:
        return [query]

    rewriter = QueryRewriter()
    if use_multi_query:
        variants = await rewriter.multi_rewrite(query)
    else:
        single = await rewriter.single_rewrite(query)
        variants = [query, single]

    variants = [v for v in dict.fromkeys(variants) if v and v.strip()]
    if not variants:
        variants = [query]

    if use_hyde:
        hypothetical = await rewriter.hyde_expand(variants[0])
        if hypothetical != variants[0]:
            variants = list(dict.fromkeys(variants + [hypothetical]))
            logger.info("HyDE: 已加入假设文档查询（共 %d 路）", len(variants))

    return variants


async def retrieve_for_query(
    query: str,
    *,
    retriever,
    collections: Sequence[str],
    top_k: int,
    use_rewrite: bool = True,
    use_multi_query: bool = True,
    use_hyde: bool = False,
    use_rerank: bool = False,
    score_threshold: Optional[float] = None,
) -> RetrievalOutcome:
    """跨集合、跨变体检索一次，融合一次，精排一次。"""

    settings = get_settings()
    variants = await expand_query(
        query,
        use_rewrite=use_rewrite,
        use_multi_query=use_multi_query,
        use_hyde=use_hyde,
    )

    probes = [(col, variant) for col in collections for variant in variants]
    if not probes:
        return RetrievalOutcome(docs=[], query_variants=variants)

    per_probe_k = max(top_k, settings.rag.top_k)
    raw_lists = await asyncio.gather(
        *[
            retriever.retrieve(
                variant,
                collection=col,
                top_k=per_probe_k,
                score_threshold=score_threshold,
                use_hybrid=True,
                # 精排属于整个候选池，不属于单条探针。
                rerank=False,
            )
            for col, variant in probes
        ],
        return_exceptions=True,
    )

    ranked_lists: List[List[Dict[str, Any]]] = []
    for (col, variant), raw in zip(probes, raw_lists):
        if isinstance(raw, BaseException):
            logger.warning(
                "检索探针失败 [%s] '%s': %s: %s",
                col,
                variant[:30],
                type(raw).__name__,
                raw,
            )
            continue
        docs = list(raw or [])
        for doc in docs:
            doc.setdefault("collection", col)
        ranked_lists.append(docs)

    pool = rrf_fuse(ranked_lists, top_k=settings.rerank.initial_top_k)
    if not pool:
        logger.info(
            "检索管线: '%s' → %d 变体 × %d 集合 → 候选池空",
            query[:30],
            len(variants),
            len(collections),
        )
        return RetrievalOutcome(docs=[], query_variants=variants)

    reranker = get_reranker() if use_rerank else None
    if reranker is not None:
        ordered = await reranker.rerank(query, pool, top_k=top_k)
        logger.info(
            "检索管线: '%s' → %d 变体 × %d 集合 → 候选池 %d → 精排取前 %d",
            query[:30],
            len(variants),
            len(collections),
            len(pool),
            len(ordered),
        )
        return RetrievalOutcome(
            docs=ordered, query_variants=variants, pool=pool, reranked=True
        )

    logger.info(
        "检索管线: '%s' → %d 变体 × %d 集合 → 候选池 %d → 按融合排名取前 %d（未精排）",
        query[:30],
        len(variants),
        len(collections),
        len(pool),
        top_k,
    )
    return RetrievalOutcome(docs=pool[:top_k], query_variants=variants, pool=pool)


__all__ = ["RetrievalOutcome", "expand_query", "retrieve_for_query"]
