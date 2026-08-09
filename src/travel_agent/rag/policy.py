"""RAGModePolicy v1 for JourneyPilot retrieval bridge.

The policy keeps advanced retrieval steps conditional and explainable. It is a
pure helper: callers can use the decision to choose rewrite, HyDE, CRAG grading,
and reranking without treating every query as a full advanced-RAG path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


_HIGH_RISK_TERMS = (
    "签证",
    "visa",
    "入境",
    "immigration",
    "海关",
    "隔离",
    "疫苗",
    "安全",
    "医疗",
    "保险",
    "价格",
    "票价",
    "营业时间",
    "开放时间",
    "实时",
    "today",
    "latest",
    "current",
)
_COLLOQUIAL_TERMS = ("咋", "怎么", "有啥", "怎么玩", "推荐", "注意啥", "坑", "啥")
_EXPLORATORY_TERMS = ("怎么玩", "推荐", "攻略", "路线", "适合", "文化", "背景", "overview", "guide", "ideas")
_SPECIFIC_ID_RE = re.compile(r"\b[A-Z]{1,4}[-_ ]?\d{2,}\b|\b\d{4,}\b")


@dataclass
class RAGModeDecision:
    """Decision returned by RAGModePolicy.

    ``enabled_features`` is intentionally redundant with the booleans so eval
    reports and trace metadata can display a compact mode string.
    """

    retrieval_mode: str
    use_hybrid: bool
    use_rewrite: bool
    use_multi_query: bool
    use_hyde: bool
    use_grader: bool
    use_rerank: bool
    enabled_features: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "retrieval_mode": self.retrieval_mode,
            "use_hybrid": self.use_hybrid,
            "use_rewrite": self.use_rewrite,
            "use_multi_query": self.use_multi_query,
            "use_hyde": self.use_hyde,
            "use_grader": self.use_grader,
            "use_rerank": self.use_rerank,
            "enabled_features": list(self.enabled_features),
            "reasons": list(self.reasons),
            "limitations": list(self.limitations),
        }


@dataclass
class RAGPolicyInput:
    query: str
    execution_path: str = "deep_research"
    source_condition: str = "knowledge_base"
    high_risk: bool = False
    initial_result_count: Optional[int] = None
    initial_quality_route: Optional[str] = None
    candidate_count: Optional[int] = None
    latency_budget_ms: Optional[int] = None


class RAGModePolicy:
    """Route RAG features by query shape, risk, source, and latency budget."""

    def __init__(self, *, rerank_candidate_threshold: int = 8, low_result_threshold: int = 2) -> None:
        self.rerank_candidate_threshold = rerank_candidate_threshold
        self.low_result_threshold = low_result_threshold

    def decide(self, policy_input: RAGPolicyInput | Dict[str, Any] | str) -> RAGModeDecision:
        if isinstance(policy_input, str):
            inp = RAGPolicyInput(query=policy_input)
        elif isinstance(policy_input, dict):
            inp = RAGPolicyInput(**policy_input)
        else:
            inp = policy_input

        query = (inp.query or "").strip()
        lower = query.lower()
        execution_path = (inp.execution_path or "deep_research").strip()
        fast_path = execution_path in {"fast", "fast_answer", "low_latency"}
        high_risk = inp.high_risk or any(term in lower for term in _HIGH_RISK_TERMS)
        specific_lookup = bool(_SPECIFIC_ID_RE.search(query)) or self._looks_like_specific_entity(query)
        low_results = inp.initial_result_count is not None and inp.initial_result_count <= self.low_result_threshold
        low_quality = str(inp.initial_quality_route or "").lower() == "low_quality"
        ambiguous = self._looks_ambiguous(query)
        exploratory = any(term in lower for term in _EXPLORATORY_TERMS)
        latency_ok = inp.latency_budget_ms is None or inp.latency_budget_ms >= 800
        candidate_count = inp.candidate_count if inp.candidate_count is not None else 0

        use_hybrid = inp.source_condition != "vector_only"
        use_rewrite = (ambiguous or low_results or low_quality) and not specific_lookup
        use_multi_query = use_rewrite and not fast_path
        use_hyde = (
            not fast_path
            and not high_risk
            and (exploratory or low_results or low_quality)
            and latency_ok
        )
        # 分级器是全仓唯一一层在做**真相关性判断**的东西，**两条路径都要有**。
        # 只在 deep 路径开（``not fast_path``）的话，fast 路径既没有能咬住的相关性下限
        # （向量臂的下限低于 embedder 的噪声地板），也没有第二道防线 —— 任何主题的问题
        # 都会被塞进最多 3 个任意知识库 chunk。
        use_grader = True
        use_rerank = not fast_path and latency_ok and candidate_count >= self.rerank_candidate_threshold

        features = ["vector"]
        if use_hybrid:
            features.extend(["lexical", "hybrid", "rrf"])
        if use_rewrite:
            features.append("multi_query" if use_multi_query else "rewrite")
        if use_hyde:
            features.append("hyde")
        if use_grader:
            features.append("grader")
        if use_rerank:
            features.append("rerank")

        reasons: List[str] = []
        limitations: List[str] = []
        reasons.append("fast_path" if fast_path else "deep_research_path")
        if ambiguous:
            reasons.append("query_is_short_or_colloquial")
        if specific_lookup:
            reasons.append("specific_entity_or_policy_lookup")
        if high_risk:
            reasons.append("high_freshness_or_policy_risk")
        if low_results:
            reasons.append("low_initial_result_count")
        if low_quality:
            reasons.append("low_quality_initial_route")
        if not latency_ok:
            limitations.append("latency_budget_blocks_hyde_or_rerank")
        if fast_path:
            limitations.append("fast_path_disables_hyde_and_multi_query")
        if high_risk and not use_hyde:
            limitations.append("hyde_disabled_for_high_risk_or_time_sensitive_fact")
        if specific_lookup and not use_rewrite:
            limitations.append("rewrite_disabled_to_avoid_query_drift")

        mode_parts = [f for f in features if f not in {"vector", "lexical"}]
        retrieval_mode = "+".join(mode_parts) if mode_parts else "vector"

        return RAGModeDecision(
            retrieval_mode=retrieval_mode,
            use_hybrid=use_hybrid,
            use_rewrite=use_rewrite,
            use_multi_query=use_multi_query,
            use_hyde=use_hyde,
            use_grader=use_grader,
            use_rerank=use_rerank,
            enabled_features=features,
            reasons=list(dict.fromkeys(reasons)),
            limitations=list(dict.fromkeys(limitations)),
        )

    @staticmethod
    def _looks_ambiguous(query: str) -> bool:
        stripped = query.strip()
        if not stripped:
            return False
        lower = stripped.lower()
        if len(stripped) <= 18:
            return True
        return any(term in lower for term in _COLLOQUIAL_TERMS)

    @staticmethod
    def _looks_like_specific_entity(query: str) -> bool:
        stripped = query.strip()
        if not stripped:
            return False
        # Clear policy/entity lookup: many proper nouns, dates, or quoted titles.
        has_date = bool(re.search(r"20\d{2}|202\d|19\d{2}", stripped))
        has_quote = "《" in stripped or "\"" in stripped or "'" in stripped
        has_policy_word = any(k in stripped.lower() for k in ("政策", "policy", "visa", "签证", "官网", "official"))
        return (has_date and has_policy_word) or has_quote


__all__ = ["RAGModeDecision", "RAGModePolicy", "RAGPolicyInput"]
