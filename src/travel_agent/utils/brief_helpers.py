"""Build worker context from an explicit capability assignment."""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from ..entities.intent_spec import IntentSpec
from ..entities.research_brief import ResearchBriefV2


def build_assignment_context(
    *,
    assignment: Mapping[str, Any],
    brief: Optional[ResearchBriefV2],
    intent_spec: Optional[IntentSpec],
    constraint_pack: Mapping[str, Any],
) -> str:
    if brief is None or intent_spec is None:
        return ""
    objective_ids = set(assignment.get("research_objective_ids") or [])
    intent_ids = set(assignment.get("must_cover_intent_ids") or []) | set(
        assignment.get("optional_intent_ids") or []
    )
    objectives = [
        item.model_dump(mode="json")
        for item in brief.domain_objectives
        if item.objective_id in objective_ids
    ]
    intents = [
        {
            "intent_id": item.intent_id,
            "strength": item.strength.value,
            "summary": item.public_summary,
            "value": item.value.model_dump(mode="json"),
        }
        for item in intent_spec.active_items
        if item.intent_id in intent_ids
    ]
    linked_constraints = {
        constraint_id
        for item in intent_spec.active_items
        if item.intent_id in intent_ids
        for constraint_id in item.linked_constraint_ids
    }
    constraints = [
        item
        for item in constraint_pack.get("constraints") or []
        if isinstance(item, dict)
        and item.get("status") == "active"
        and item.get("constraint_id") in linked_constraints
    ]
    payload = {
        "generation_id": assignment.get("generation_id"),
        "assignment_id": assignment.get("assignment_id"),
        "objective": assignment.get("objective"),
        "research_objectives": objectives,
        "intents": intents,
        "linked_constraints": constraints,
        "excluded_categories": assignment.get("excluded_categories") or [],
        "success_criteria": assignment.get("success_criteria") or [],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
