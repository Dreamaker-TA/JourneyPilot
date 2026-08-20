from __future__ import annotations

from typing import Any, Dict

from ...entities.delivery_bundle import DeliveryContractViolation, GateClass
from ...entities.intent_coverage import IntentFidelityGap
from ...entities.intent_spec import IntentTarget
from ...entities.state import TravelAgentState
from ...services.composition_rule_compiler import compile_composition_rules
from ...services.composition_mutations import mark_mutations_revalidated
from ...services.intent_verification import evaluate_intent_fidelity
from ...services.intent_entity_binding import bind_intent_context
from ...workflows.composition_repair import (
    apply_composition_repair_budget,
    composition_repair_budget_exhausted,
)
from .candidate_gate import worker_targeted_research_exhausted


_TARGET_RESEARCH_WORKER = {
    IntentTarget.VISIT: "destination_researcher",
    IntentTarget.DINING: "destination_researcher",
    IntentTarget.LODGING: "accommodation_researcher",
    IntentTarget.LOCAL_TRANSPORT: "transport_researcher",
    IntentTarget.LONG_DISTANCE_TRANSPORT: "transport_researcher",
}


def _candidate_retry_available(
    state: TravelAgentState,
    gaps: list[IntentFidelityGap],
) -> bool:
    """Return whether Candidate Gate can still buy evidence for these gaps."""

    intent_by_id = {
        intent.intent_id: intent
        for intent in (state.intent_spec.active_items if state.intent_spec else [])
    }
    workers = {
        worker
        for gap in gaps
        if gap.retry_target in {"candidate_gate", "candidate_selection"}
        if (intent := intent_by_id.get(gap.intent_id)) is not None
        if (worker := _TARGET_RESEARCH_WORKER.get(intent.target)) is not None
    }
    return any(
        not worker_targeted_research_exhausted(state, worker) for worker in workers
    )


async def intent_fidelity_gate_node(state: TravelAgentState) -> Dict[str, Any]:
    if state.intent_spec is None:
        raise DeliveryContractViolation(
            "intent fidelity gate requires intent state",
            reason_code="intent_fidelity_intent_missing",
            gate_class=GateClass.COMPOSITION,
        )
    if state.trip_workspace_v2 is None:
        # Artifact Gate deliberately carries a missing Workspace forward after
        # the research/composition closeout.  Delivery Quality Gate owns that
        # state: it can spend the remaining composition repair or emit the
        # precise window/budget terminal reason.  Raising a second generic
        # input error here only masks the failure that prevented materialization.
        return {"intent_fidelity_route": "passed"}
    rules = compile_composition_rules(state.intent_spec)
    workspace = bind_intent_context(state.trip_workspace_v2, state.intent_spec)
    report, gaps = evaluate_intent_fidelity(
        intent_spec=state.intent_spec,
        rules=rules,
        workspace=workspace,
        repair_budget_exhausted=composition_repair_budget_exhausted(state),
    )
    rules_by_id = {rule.rule_id: rule for rule in rules}
    never_violated = [
        gap
        for gap in gaps
        if any(
            rules_by_id[rule_id].policy_on_failure == "never_violate"
            for rule_id in gap.violated_rule_ids
        )
    ]
    if never_violated:
        raise DeliveryContractViolation(
            "intent fidelity gate detected a never-violate rule breach",
            reason_code="intent_hard_prohibition_violated",
            gate_class=GateClass.COMPOSITION,
        )
    coverage_after = {item.intent_id: item.status.value for item in report.items}
    mutations = mark_mutations_revalidated(
        workspace.composition_mutations,
        coverage_after=coverage_after,
    )
    update: Dict[str, Any] = {
        "intent_coverage_report": report,
        "intent_fidelity_gaps": gaps,
        "composition_mutations": mutations,
        "trip_workspace_v2": type(workspace).model_validate(
            workspace.model_copy(
                update={
                    "intent_coverage_report": report,
                    "composition_mutations": mutations,
                }
            ).model_dump(mode="json")
        ),
    }
    blocking = [gap for gap in gaps if gap.blocking]
    if not blocking:
        update["intent_fidelity_route"] = "passed"
        return update
    candidate_gaps = [
        gap
        for gap in blocking
        if gap.retry_target in {"candidate_gate", "candidate_selection"}
    ]
    if candidate_gaps and _candidate_retry_available(state, candidate_gaps):
        update["intent_fidelity_route"] = "candidate_gate"
        return update
    if candidate_gaps:
        # Candidate Gate already settled these domains.  Sending the same
        # immutable catalog back around the graph cannot change fidelity; carry
        # the audited gap forward.  If composition also has a distinct gap
        # (for example missing alternatives), the bounded composition repair
        # below may still improve that independent part of the delivery.
        if len(candidate_gaps) == len(blocking):
            update["intent_fidelity_route"] = "passed"
            return update
    update["intent_fidelity_route"] = "composition_repair"
    return apply_composition_repair_budget(
        state,
        update,
        route_key="intent_fidelity_route",
        exhausted_route="passed",
    )


def route_after_intent_fidelity_gate(state: TravelAgentState) -> str:
    route = state.intent_fidelity_route or "passed"
    if route not in {"passed", "candidate_gate", "composition_repair"}:
        raise RuntimeError("intent fidelity gate produced an invalid route")
    return route
