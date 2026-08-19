from __future__ import annotations

from typing import Any, Dict

from ...entities.delivery_bundle import DeliveryContractViolation, GateClass
from ...entities.state import TravelAgentState
from ...services.composition_rule_compiler import compile_composition_rules
from ...services.composition_mutations import mark_mutations_revalidated
from ...services.intent_verification import evaluate_intent_fidelity
from ...services.intent_entity_binding import bind_intent_context
from ...workflows.composition_repair import (
    apply_composition_repair_budget,
    composition_repair_budget_exhausted,
)


async def intent_fidelity_gate_node(state: TravelAgentState) -> Dict[str, Any]:
    if state.intent_spec is None or state.trip_workspace_v2 is None:
        raise DeliveryContractViolation(
            "intent fidelity gate requires intent and workspace state",
            reason_code="intent_fidelity_inputs_missing",
            gate_class=GateClass.COMPOSITION,
        )
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
    if any(
        gap.retry_target in {"candidate_gate", "candidate_selection"}
        for gap in blocking
    ):
        update["intent_fidelity_route"] = "candidate_gate"
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
