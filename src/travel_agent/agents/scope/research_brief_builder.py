"""Deterministic ResearchBriefV2 workflow node."""

from __future__ import annotations

from typing import Any, Dict

from ...entities.state import TravelAgentState
from ...services.capability_planning import (
    RESEARCH_BRIEF_POLICY_VERSION,
    build_research_brief,
)


async def research_brief_builder_node(state: TravelAgentState) -> Dict[str, Any]:
    if state.request_contract is None:
        raise ValueError("research brief requires a request contract")
    brief = build_research_brief(
        state.request_contract,
        state.controlled_trip_identity,
    )
    return {
        "research_brief": brief,
        "next_agent": None,
        "policy_versions": {
            "research_brief_projection": RESEARCH_BRIEF_POLICY_VERSION,
        },
    }
