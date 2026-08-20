"""Typed-artifact integrity validation after Candidate admission.

Candidate Gate owns provider/content retry budgets. This gate only decides
whether a typed artifact is internally valid, or hands a content failure back
to Candidate Gate for classification while that gate still holds a targeted
research call for the worker. It never retries a provider itself.

A malformed or absent artifact is recorded and handed forward to the delivery
quality gate. The only route this gate spends itself is one bounded
composition repair for an itinerary model failure; once that budget is gone,
and past the closeout boundary, everything goes forward.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict

from pydantic import ValidationError

from ...entities.delivery_bundle import (
    GateClass,
    GateDisposition,
    GateFailureAttribution,
    TripWorkspaceV2,
)
from ...entities.state import TravelAgentState
from ...workflows.composition_repair import apply_composition_repair_budget
from ...workflows.run_deadline import observe_run_deadline
from ..utils import strip_round_suffix
from .candidate_gate import (
    _latest_packets,
    worker_research_satisfied_by_a_later_round,
    worker_targeted_research_exhausted,
)
from .provider_failure import classify_provider_failure, is_provider_or_model_failure


_RESEARCH_WORKERS = {
    "destination_researcher",
    "transport_researcher",
    "accommodation_researcher",
}


def _round_trip_workspace(workspace: Any) -> TripWorkspaceV2 | None:
    """Return only a freshly schema-validated workspace artifact.

    LangGraph checkpoints and direct-node tests can carry a ``model_construct``
    instance, whose nested values have not necessarily crossed Pydantic's
    validation boundary.  Re-validating the Python dump keeps Artifact Gate as
    the final typed-artifact boundary without changing a legal, empty-candidate
    recommendation catalog into an error.
    """

    if not isinstance(workspace, TripWorkspaceV2):
        return None
    try:
        return TripWorkspaceV2.model_validate(workspace.model_dump(mode="python"))
    except (ValidationError, TypeError, ValueError, AttributeError):
        return None


def _planned_agent_keys(state: TravelAgentState) -> list[str]:
    return [agent for group in state.execution_plan or [] for agent in group]


def _current_research_packet(
    state: TravelAgentState,
    agent_key: str,
):
    """Resolve a worker packet through its generation-qualified state key.

    Worker writes are keyed ``task[@generation]`` so stale generations can
    coexist safely during recovery.  Execution plans and statuses remain keyed
    by task.  Artifact Gate used to join those maps by raw key and therefore
    called a completed, valid packet "missing" on every pass, creating an
    artifact → intent → candidate → dispatcher loop.
    """

    if state.planning_generation is None:
        return None
    return _latest_packets(
        state.research_packets or {},
        generation_id=state.planning_generation.generation_id,
    ).get(strip_round_suffix(agent_key))


def _attribution(
    state: TravelAgentState,
    *,
    gate_class: GateClass,
    disposition: GateDisposition,
    reason_code: str,
    gap_id: str,
    deterministic: bool = False,
) -> Dict[str, GateFailureAttribution]:
    draft_id = (
        state.minimum_delivery_draft.draft_id if state.minimum_delivery_draft else None
    )
    material = "|".join(
        [
            draft_id or "",
            gate_class.value,
            disposition.value,
            reason_code,
            gap_id,
        ]
    )
    attribution_id = (
        f"gate_failure_{hashlib.sha256(material.encode()).hexdigest()[:24]}"
    )
    records = dict(state.gate_failure_attributions or {})
    records[attribution_id] = GateFailureAttribution(
        attribution_id=attribution_id,
        gate_class=gate_class,
        disposition=disposition,
        reason_code=reason_code,
        draft_id=draft_id,
        gap_ids=[gap_id],
        deterministic=deterministic,
        recorded_at=datetime.now(timezone.utc),
    )
    return records


def _integrity_result(
    state: TravelAgentState,
    *,
    agent_key: str,
    reason_code: str,
    message: str,
) -> Dict[str, Any]:
    """Record a typed-artifact gap and hand the run to the delivery quality gate."""
    return {
        "artifact_gate_route": "accepted",
        "artifact_status": {agent_key: "integrity_failed"},
        "gate_failure_attributions": _attribution(
            state,
            gate_class=GateClass.COMPOSITION,
            disposition=GateDisposition.COMPOSITION_REPAIR,
            reason_code=reason_code,
            gap_id=f"artifact:{agent_key}:{reason_code}",
        ),
        "last_error": message,
    }


def _content_failure_result(
    state: TravelAgentState,
    *,
    agent_key: str,
    reason_code: str,
    deterministic: bool,
) -> Dict[str, Any]:
    """Hand one worker's content failure to the owner of the research budget.

    Candidate Gate funds targeted research. While it still holds a call for
    this worker the failure goes back to it for classification; once that
    budget is spent the gap is a settled fact and the run carries it forward
    to the delivery quality gate.

    A budget a *successful* retry spent settles the domain instead of exhausting
    it. The round key under judgment keeps its own terminal failure either way,
    so the audit names what actually closed the domain rather than the failure of
    the round this loop happens to be looking at.
    """
    exhausted = worker_targeted_research_exhausted(state, strip_round_suffix(agent_key))
    satisfied_by_retry = exhausted and worker_research_satisfied_by_a_later_round(
        state, agent_key
    )
    return {
        "artifact_gate_route": "accepted" if exhausted else "candidate_gate",
        "artifact_status": {agent_key: "content_gap"},
        "gate_failure_attributions": _attribution(
            state,
            gate_class=GateClass.COMPOSITION,
            disposition=(
                GateDisposition.RESEARCH_EXHAUSTED
                if exhausted
                else GateDisposition.TARGETED_RESEARCH
            ),
            reason_code=(
                "research_satisfied_by_scoped_retry"
                if satisfied_by_retry
                else reason_code
            ),
            gap_id=f"artifact:{agent_key}:content_failure",
            deterministic=deterministic,
        ),
    }


async def artifact_gate_node(state: TravelAgentState) -> Dict[str, Any]:
    """Validate typed artifacts without turning content gaps into run failures."""
    for agent_key in _planned_agent_keys(state):
        base = strip_round_suffix(agent_key)
        if base not in _RESEARCH_WORKERS and base != "itinerary_planner":
            continue
        status = str((state.agent_status or {}).get(agent_key) or "")

        if base in _RESEARCH_WORKERS:
            packet = _current_research_packet(state, agent_key)
            if packet is not None:
                if packet.worker_kind != base or packet.run_id != state.run_id:
                    return _integrity_result(
                        state,
                        agent_key=agent_key,
                        reason_code="research_packet_identity_invalid",
                        message=f"research packet identity does not match {agent_key}",
                    )
                if status in {"completed", "partial"}:
                    continue
                # Packet present but worker not in a legal completion status:
                # this is a content failure, never "packet missing".
                if status == "failed" or status not in {"completed", "partial"}:
                    if is_provider_or_model_failure(state.last_error):
                        classification = classify_provider_failure(state.last_error)
                        reason_code = classification.reason_code
                        deterministic = classification.category == "deterministic"
                    else:
                        # Worker wrote a typed packet then failed without an
                        # explicit provider marker (e.g. schema/JSON parse).
                        # Candidate Gate still owns the retry budget.
                        reason_code = "research_packet_present_but_worker_failed"
                        deterministic = False
                    return _content_failure_result(
                        state,
                        agent_key=agent_key,
                        reason_code=reason_code,
                        deterministic=deterministic,
                    )
            if status == "failed" and is_provider_or_model_failure(state.last_error):
                classification = classify_provider_failure(state.last_error)
                return _content_failure_result(
                    state,
                    agent_key=agent_key,
                    reason_code=classification.reason_code,
                    deterministic=classification.category == "deterministic",
                )
            return _integrity_result(
                state,
                agent_key=agent_key,
                reason_code="required_research_packet_missing",
                message=(
                    f"required typed research packet is absent or malformed: "
                    f"agent={agent_key}, status={status or 'missing'}"
                ),
            )

        if status == "failed" and is_provider_or_model_failure(state.last_error):
            classification = classify_provider_failure(state.last_error)
            return apply_composition_repair_budget(
                state,
                {
                    "artifact_gate_route": "composition_repair",
                    "artifact_status": {agent_key: "composition_gap"},
                    "gate_failure_attributions": _attribution(
                        state,
                        gate_class=GateClass.COMPOSITION,
                        disposition=GateDisposition.COMPOSITION_REPAIR,
                        reason_code="itinerary_provider_or_model_failure",
                        gap_id=f"artifact:{agent_key}:{classification.reason_code}",
                        deterministic=classification.category == "deterministic",
                    ),
                },
                route_key="artifact_gate_route",
                exhausted_route="accepted",
            )

        workspace = _round_trip_workspace(state.trip_workspace_v2)
        if (
            status in {"completed", "partial"}
            and workspace is not None
            and workspace.run_id == state.run_id
            and state.recommendation_catalog is not None
            and workspace.recommendation_catalog == state.recommendation_catalog
            and workspace.itinerary.day_plans
        ):
            continue
        return _integrity_result(
            state,
            agent_key=agent_key,
            reason_code="required_workspace_artifact_missing",
            message=(
                f"required typed workspace artifact is absent or malformed: "
                f"agent={agent_key}, status={status or 'missing'}"
            ),
        )

    return {
        "artifact_gate_route": "accepted",
        "artifact_status": {key: "accepted" for key in _planned_agent_keys(state)},
    }


def route_after_artifact_gate(state: TravelAgentState) -> str:
    route = state.artifact_gate_route or "accepted"
    deadline = state.run_deadline
    if deadline is None:
        return route
    _observed, observation = observe_run_deadline(deadline)
    if observation.research_closed:
        # Past the boundary the current artifacts go to the delivery quality
        # gate and on to projection; research and repair routes are model paths.
        return "accepted"
    return route
