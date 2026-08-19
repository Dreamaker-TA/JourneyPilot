"""Invalidate planning products at their owning dependency boundary."""

from __future__ import annotations

from typing import Any, Dict

from ..entities.request_contract import AmendmentImpact


def invalidation_update(impact: AmendmentImpact) -> Dict[str, Any]:
    projection = {
        "report_projection": None,
        "map_projection": None,
        "source_index_projection": None,
        "delivery_bundle": None,
        "delivery_failure": None,
        "delivery_persisted": False,
        "is_completed": False,
    }
    if impact is AmendmentImpact.PROJECTION_ONLY:
        return projection
    composition = {
        **projection,
        "candidate_selection_plan": None,
        "placement_skeleton": None,
        "composition_mutations": [],
        "intent_coverage_report": None,
        "intent_fidelity_gaps": [],
        "intent_fidelity_route": None,
        "trip_workspace_v2": None,
        "recommendation_quality": None,
        "delivery_quality_gaps": [],
        "delivery_quality_route": None,
        "composition_failure_context": None,
        "placement_skeleton_failure_context": None,
        "local_connector_gaps": [],
    }
    if impact is AmendmentImpact.COMPOSITION_AFFECTING:
        return composition
    ranking = {
        **composition,
        "recommendation_catalog": None,
        "candidate_intent_evaluation_cache": {},
        "candidate_research_gaps": [],
        "candidate_gate_status": None,
        "candidate_gate_route": None,
        "candidate_gate_attempts": {},
        "candidate_gate_failure_signatures": {},
    }
    if impact is AmendmentImpact.RANKING_AFFECTING:
        return ranking
    return {
        **ranking,
        "research_brief": None,
        "capability_plan": None,
        "research_query_plan": None,
        "execution_plan": [],
        "agent_assignments": {},
        "current_plan_step": 0,
        "agent_status": {},
        "artifact_status": {},
        "artifact_gate_route": None,
        "minimum_delivery_draft": None,
        "run_deadline": None,
        "run_budget": None,
        "gate_failure_attributions": {},
        "terminal_attribution": None,
        "provider_evidence_outcomes": {},
        "weather_impacts": {},
        "composition_repair_attempts": 0,
        "unlocatable_authored_places": [],
    }


def classify_intent_impact(impact_stages: list[str]) -> AmendmentImpact:
    stages = set(impact_stages)
    if "research" in stages:
        return AmendmentImpact.RESEARCH_AFFECTING
    if "admission" in stages:
        return AmendmentImpact.ADMISSION_AFFECTING
    if "ranking" in stages:
        return AmendmentImpact.RANKING_AFFECTING
    if "composition" in stages:
        return AmendmentImpact.COMPOSITION_AFFECTING
    if "projection" in stages:
        return AmendmentImpact.PROJECTION_ONLY
    return AmendmentImpact.UNSUPPORTED


def generation_packet_key(worker_key: str, generation_id: str) -> str:
    return f"{worker_key}@{generation_id}"


def packet_key_generation(key: str) -> str | None:
    if "@" not in key:
        return None
    return key.rsplit("@", 1)[1]
