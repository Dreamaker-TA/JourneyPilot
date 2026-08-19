from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..entities.research_domain import ResearchDomain
from ..entities.research_query_plan import ResearchQuery
from ..workflows.run_budget import RunBudgetExhausted
from ..workflows.run_control import (
    ModelWindowClosed,
    current_budget_ledger,
    remaining_model_seconds,
)


FALLBACK_QUERY_POLICY_VERSION = "fallback_query_policy.v1"
_FALLBACK_CATEGORY_TOKENS: Mapping[ResearchDomain, tuple[str, ...]] = {
    ResearchDomain.VISIT: ("museum", "博物馆", "美术馆"),
    ResearchDomain.DINING: ("restaurant", "餐厅", "餐馆"),
    ResearchDomain.LODGING: ("hotel", "酒店", "宾馆"),
}


def runtime_fallback_capacity() -> tuple[bool, bool]:
    try:
        remaining_model_seconds("research.generic_fallback")
        research_window_open = True
    except ModelWindowClosed:
        research_window_open = False
    ledger = current_budget_ledger()
    try:
        if ledger is not None:
            ledger.guard("research.generic_fallback", tool_calls=1)
        run_budget_available = True
    except RunBudgetExhausted:
        run_budget_available = False
    return research_window_open, run_budget_available


@dataclass(frozen=True)
class FallbackQueryPolicy:
    policy_version: str = FALLBACK_QUERY_POLICY_VERSION
    fallback_penalty: float = 0.2

    def is_allowed(
        self,
        query: ResearchQuery,
        *,
        executed_query_ids: set[str],
        admitted_candidate_count: int,
        required_candidate_count: int,
        research_window_open: bool,
        run_budget_available: bool,
    ) -> bool:
        if query.query_kind.value != "generic_fallback":
            return False
        excluded = " ".join(query.excluded_categories).casefold()
        category_tokens = _FALLBACK_CATEGORY_TOKENS.get(query.domain, ())
        if any(token.casefold() in excluded for token in category_tokens):
            return False
        return (
            set(query.fallback_after_query_ids) <= executed_query_ids
            and admitted_candidate_count < required_candidate_count
            and research_window_open
            and run_budget_available
        )


def fallback_template(
    domain: ResearchDomain,
    destination_name: str,
    excluded_categories: list[str],
) -> str | None:
    excluded = " ".join(excluded_categories).casefold()
    templates: Mapping[ResearchDomain, str] = {
        ResearchDomain.VISIT: (f"museum in {destination_name}"),
        ResearchDomain.DINING: (f"restaurant in {destination_name}"),
        ResearchDomain.LODGING: (f"hotel in {destination_name}"),
    }
    template = templates.get(domain)
    if template is None:
        return None
    if any(token.casefold() in excluded for token in _FALLBACK_CATEGORY_TOKENS[domain]):
        return None
    return template
