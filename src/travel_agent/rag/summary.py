"""RetrievalSummary v1 bridge helpers.

These helpers convert raw retrieved chunks into trace/eval-safe summaries:
counts, methods, source metadata, and short snippets. Raw chunks are never
required by the bridge output.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from .policy import RAGModeDecision

_SNIPPET_MAX = 180


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _snippet(text: str) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= _SNIPPET_MAX else t[:_SNIPPET_MAX].rstrip() + "..."


def _metadata(doc: Dict[str, Any]) -> Dict[str, Any]:
    meta = doc.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _source_type(doc: Dict[str, Any]) -> str:
    explicit = doc.get("source_type") or _metadata(doc).get("source_type")
    if explicit:
        return str(explicit)
    source = str(doc.get("source") or doc.get("url") or "")
    host = urlparse(source).netloc.lower()
    if ".gov" in host or "gov." in host or "embassy" in host:
        return "official"
    if "wikipedia.org" in host or "wikivoyage.org" in host:
        return "reference"
    if source.startswith("file:") or not host:
        return "knowledge_base"
    return "web"


def _authority_hint(source_type: str) -> str:
    return {
        "official": "official_or_primary",
        "tool": "tool_reported",
        "knowledge_base": "indexed_knowledge_base",
        "reference": "reference_site",
        "web": "web_source",
        "ugc": "user_generated_content",
    }.get(source_type, "unknown")


def _freshness_hint(doc: Dict[str, Any]) -> str:
    explicit = doc.get("freshness_hint") or doc.get("freshness_status") or _metadata(doc).get("freshness_hint")
    if explicit:
        return str(explicit)
    if doc.get("published_at") or _metadata(doc).get("published_at"):
        return "dated"
    if doc.get("retrieved_at"):
        return "retrieved_only"
    return "unknown"


@dataclass
class RetrievalSourceSummary:
    source_id: str
    source: str
    collection: str = ""
    retrieval_method: str = "vector"
    source_type: str = "knowledge_base"
    freshness_hint: str = "unknown"
    authority_hint: str = "unknown"
    vector_score: Optional[float] = None
    fusion_score: Optional[float] = None
    rerank_score: Optional[float] = None
    retrieved_at: str = ""
    published_at: str = ""
    snippet: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RetrievalSummary:
    summary_version: str = "retrieval_summary_v1"
    query: str = ""
    rewritten_queries: List[str] = field(default_factory=list)
    retrieval_mode: str = "vector"
    retriever_used: List[str] = field(default_factory=list)
    collections: List[str] = field(default_factory=list)
    result_count: int = 0
    selected_count: int = 0
    trimmed_count: int = 0
    grader_route: Optional[str] = None
    grade_avg_score: Optional[float] = None
    rerank_used: bool = False
    top_sources: List[RetrievalSourceSummary] = field(default_factory=list)
    coverage_by_collection: Dict[str, int] = field(default_factory=dict)
    evidence_candidates: List[Dict[str, Any]] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    missing_signals: List[str] = field(default_factory=list)
    generated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["top_sources"] = [s.to_dict() if hasattr(s, "to_dict") else dict(s) for s in self.top_sources]
        return data


def _doc_key(doc: Dict[str, Any]) -> str:
    return str(doc.get("chunk_id") or doc.get("id") or doc.get("content") or doc.get("source") or "")


def _source_summary(doc: Dict[str, Any]) -> RetrievalSourceSummary:
    source = str(doc.get("source") or doc.get("url") or "unknown_source")
    content = str(doc.get("original_content") or doc.get("content") or "")
    source_type = _source_type(doc)
    source_id = str(doc.get("source_id") or f"rs_{_short_hash(source + _doc_key(doc))}")
    return RetrievalSourceSummary(
        source_id=source_id,
        source=source,
        collection=str(doc.get("collection") or _metadata(doc).get("collection") or ""),
        retrieval_method=str(doc.get("retrieval_method") or "vector"),
        source_type=source_type,
        freshness_hint=_freshness_hint(doc),
        authority_hint=_authority_hint(source_type),
        vector_score=_safe_float(doc.get("vector_score")),
        fusion_score=_safe_float(doc.get("fusion_score")),
        rerank_score=_safe_float(doc.get("rerank_score")),
        retrieved_at=str(doc.get("retrieved_at") or ""),
        published_at=str(doc.get("published_at") or _metadata(doc).get("published_at") or ""),
        snippet=_snippet(content),
    )


def build_retrieval_summary(
    query: str,
    docs: List[Dict[str, Any]],
    *,
    selected_docs: Optional[List[Dict[str, Any]]] = None,
    rewritten_queries: Optional[List[str]] = None,
    mode_decision: Optional[RAGModeDecision | Dict[str, Any]] = None,
    grade_result: Any = None,
    collections: Optional[Iterable[str]] = None,
    top_n: int = 5,
) -> RetrievalSummary:
    """Build a trace/eval-safe RetrievalSummary v1."""
    selected = selected_docs if selected_docs is not None else docs
    mode_dict = mode_decision.to_dict() if hasattr(mode_decision, "to_dict") else (mode_decision or {})
    retriever_used = list(mode_dict.get("enabled_features") or [])
    if not retriever_used:
        methods = {str(d.get("retrieval_method") or "vector") for d in docs}
        retriever_used = sorted(methods)
        if "hybrid" in methods:
            retriever_used.extend(["rrf"])

    source_rows = [_source_summary(d) for d in selected[:top_n]]
    coverage: Dict[str, int] = {}
    for d in selected:
        col = str(d.get("collection") or _metadata(d).get("collection") or "unknown")
        coverage[col] = coverage.get(col, 0) + 1

    missing_signals: List[str] = []
    if not docs:
        missing_signals.append("no_retrieval_results")
    if any(not s.published_at for s in source_rows):
        missing_signals.append("published_at_missing_for_some_sources")
    if grade_result is None and "grader" in retriever_used:
        missing_signals.append("grader_result_missing")

    limitations = list(mode_dict.get("limitations") or [])
    if any("hyde" == f for f in retriever_used):
        limitations.append("hyde_query_is_hypothetical_not_evidence")

    grade_route = getattr(getattr(grade_result, "route", None), "value", None) or getattr(grade_result, "route", None)
    grade_avg = _safe_float(getattr(grade_result, "avg_score", None))
    rerank_used = any(d.get("rerank_score") is not None for d in selected) or bool(mode_dict.get("use_rerank"))
    selected_count = len(selected)

    summary = RetrievalSummary(
        query=query,
        rewritten_queries=list(rewritten_queries or [query]),
        retrieval_mode=str(mode_dict.get("retrieval_mode") or ("hybrid" if "hybrid" in retriever_used else "vector")),
        retriever_used=list(dict.fromkeys(retriever_used)),
        collections=list(collections or sorted({s.collection for s in source_rows if s.collection})),
        result_count=len(docs),
        selected_count=selected_count,
        trimmed_count=max(0, len(docs) - selected_count),
        grader_route=str(grade_route) if grade_route else None,
        grade_avg_score=grade_avg,
        rerank_used=rerank_used,
        top_sources=source_rows,
        coverage_by_collection=coverage,
        limitations=list(dict.fromkeys(limitations)),
        missing_signals=list(dict.fromkeys(missing_signals)),
    )
    summary.evidence_candidates = retrieval_summary_to_evidence_sources(summary)
    return summary


def retrieval_summary_to_evidence_sources(summary: RetrievalSummary) -> List[Dict[str, Any]]:
    """Project top sources into the Fast Answer external-source shape."""
    out: List[Dict[str, Any]] = []
    for src in summary.top_sources:
        ev_id = f"rag_{_short_hash(summary.query + src.source_id)}"
        authority = {
            "official": 0.9,
            "tool": 0.75,
            "knowledge_base": 0.65,
            "reference": 0.6,
            "web": 0.5,
            "ugc": 0.25,
        }.get(src.source_type, 0.4)
        if src.freshness_hint in {"stale", "unknown"}:
            authority = min(authority, 0.45)
        out.append(
            {
                "evidence_id": ev_id,
                "source_type": "rag",
                "source_name": src.source,
                "title": src.source,
                "url": src.source if src.source.startswith(("http://", "https://")) else "",
                "snippet": src.snippet,
                "retrieved_at": src.retrieved_at or summary.generated_at,
                "published_at": src.published_at or None,
                "freshness_status": src.freshness_hint,
                "authority_score": authority,
                "metadata": {
                    "retrieval_summary_version": summary.summary_version,
                    "retrieval_source_id": src.source_id,
                    "retrieval_method": src.retrieval_method,
                    "collection": src.collection,
                    "vector_similarity": src.vector_score,
                    "fusion_score": src.fusion_score,
                    "rerank_score": src.rerank_score,
                },
            }
        )
    return out


__all__ = [
    "RetrievalSourceSummary",
    "RetrievalSummary",
    "build_retrieval_summary",
    "retrieval_summary_to_evidence_sources",
]
