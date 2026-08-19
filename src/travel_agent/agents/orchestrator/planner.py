"""Deterministic capability planner for deep travel planning."""

from __future__ import annotations

from typing import Any, Dict

from ...entities.state import TaskType, TravelAgentState
from ...entities.provider_evidence import (
    build_provider_evidence_assignments,
    build_required_long_distance_legs,
    dump_provider_evidence_assignments,
    explicit_cross_day_return_required,
    scope_attempt_numbers,
)
from ...services.capability_planning import (
    CAPABILITY_PLAN_POLICY_VERSION,
    build_capability_plan,
)


def _attach_initial_provider_evidence_scopes(
    assignments: Dict[str, Dict[str, Any]],
    state: TravelAgentState,
) -> Dict[str, Dict[str, Any]]:
    scoped = {key: dict(value) for key, value in assignments.items()}
    long_distance_legs = build_required_long_distance_legs(
        state.controlled_trip_identity or {},
        cross_day_return_required=explicit_cross_day_return_required(
            state.user_query or ""
        ),
    )
    for worker in (
        "destination_researcher",
        "transport_researcher",
        "accommodation_researcher",
    ):
        if worker not in scoped:
            continue
        if worker == "transport_researcher" and not long_distance_legs:
            scoped[worker]["provider_evidence_assignments"] = []
            continue
        scoped[worker]["provider_evidence_assignments"] = (
            dump_provider_evidence_assignments(
                build_provider_evidence_assignments(
                    run_id=state.run_id,
                    constraint_pack_revision=state.constraint_pack_revision,
                    worker_kind=worker,
                    controlled_trip_identity=state.controlled_trip_identity or {},
                    prior_scope_attempts=scope_attempt_numbers(
                        state.provider_evidence_outcomes
                    ),
                    long_distance_legs=(
                        long_distance_legs
                        if worker == "transport_researcher"
                        else None
                    ),
                )
            )
        )
    return scoped


async def planner_node(state: TravelAgentState) -> Dict[str, Any]:
    if state.request_contract is None or state.research_brief is None:
        raise ValueError("capability planning requires a request contract and research brief")
    if state.planning_generation is None:
        raise ValueError("capability planning requires a planning generation")
    plan = build_capability_plan(
        request_contract=state.request_contract,
        brief=state.research_brief,
        plan_revision=state.planning_generation.plan_revision,
    )
    assignments = {
        key: value.model_dump(mode="json") for key, value in plan.assignments.items()
    }
    assignments = _attach_initial_provider_evidence_scopes(assignments, state)
    return {
        "capability_plan": plan,
        "execution_plan": plan.execution_plan,
        "current_plan_step": 0,
        "agent_assignments": assignments,
        "task_type": TaskType.TRAVEL_PLANNING,
        "policy_versions": {
            "capability_plan": CAPABILITY_PLAN_POLICY_VERSION,
        },
    }
