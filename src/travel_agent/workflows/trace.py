"""Lightweight workflow trace events.

These events are an API/SSE observability contract, not a durable run log.
They intentionally keep summaries and metadata only, so prompts, raw tool
results, and user-sensitive values do not leak into the timeline.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


TRACE_SCHEMA_VERSION = "workflow_trace_event.v1"


# v2 Deep Research trunk + Fast Answer. Unknown nodes fall back to postprocess.
NODE_PHASES: Dict[str, str] = {
    # scope
    "scope_clarifier": "scope",
    "request_contract_normalizer": "scope",
    "research_brief_builder": "scope",
    # planning
    "intent_amendment_router": "planning",
    "minimum_delivery_draft_builder": "planning",
    "destination_geo_resolver": "planning",
    "weather_context_builder": "planning",
    "trip_summary_card_brief": "planning",
    "planner": "planning",
    "plan_gate": "planning",
    "dispatcher": "planning",
    # research / composition workers
    "destination_researcher": "research",
    "transport_researcher": "research",
    "accommodation_researcher": "research",
    "itinerary_planner": "research",
    # verification
    "candidate_gate": "verification",
    "artifact_gate": "verification",
    "delivery_quality_gate": "verification",
    # delivery
    "budget_estimate": "delivery",
    "delivery_projector": "delivery",
    "delivery_finalizer": "delivery",
    # fast path + envelope
    "fast_answer_agent": "synthesis",
    "workflow": "postprocess",
}


def infer_trace_phase(node: str, fallback: str = "postprocess") -> str:
    """Return a stable JourneyPilot phase for a workflow node."""
    return NODE_PHASES.get(str(node or ""), fallback)


def compact_summary(value: Any, *, limit: int = 180) -> str:
    """Build a short, single-line summary without exposing raw payloads."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


@dataclass
class WorkflowTraceEvent:
    """A lightweight v1 trace event for SSE and debug timelines."""

    run_id: str
    sequence: int
    node: str
    phase: str
    status: str
    event_id: str = field(default_factory=lambda: f"tr_{uuid.uuid4().hex[:12]}")
    schema_version: str = TRACE_SCHEMA_VERSION
    input_summary: Optional[str] = None
    output_summary: Optional[str] = None
    route_decision: Optional[str] = None
    agent: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    ts_ms: Optional[float] = None
    duration_ms: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "node": self.node,
            "phase": self.phase,
            "status": self.status,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "route_decision": self.route_decision,
            "agent": self.agent,
            "tool_calls": self.tool_calls,
            "risk_flags": self.risk_flags,
            "ts_ms": self.ts_ms,
            "duration_ms": self.duration_ms,
        }


def make_trace_event(
    *,
    run_id: str,
    sequence: int,
    node: str,
    status: str = "completed",
    phase: Optional[str] = None,
    input_summary: Any = None,
    output_summary: Any = None,
    route_decision: Optional[str] = None,
    agent: Optional[str] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    risk_flags: Optional[List[str]] = None,
    ts_ms: Optional[float] = None,
    duration_ms: Optional[float] = None,
) -> WorkflowTraceEvent:
    return WorkflowTraceEvent(
        run_id=run_id,
        sequence=sequence,
        node=node,
        phase=phase or infer_trace_phase(node),
        status=status,
        input_summary=compact_summary(input_summary) or None,
        output_summary=compact_summary(output_summary) or None,
        route_decision=route_decision,
        agent=agent,
        tool_calls=tool_calls or [],
        risk_flags=risk_flags or [],
        ts_ms=ts_ms,
        duration_ms=duration_ms,
    )


def summarize_state_update(node: str, state_update: Dict[str, Any]) -> Dict[str, Any]:
    """Derive trace-safe summaries from a LangGraph state update frame."""
    if not isinstance(state_update, dict):
        return {"output_summary": "node completed"}

    route_decision = None
    risk_flags: List[str] = []
    parts: List[str] = []

    if state_update.get("next_agent"):
        route_decision = str(state_update.get("next_agent"))
        parts.append(f"route={route_decision}")
    if state_update.get("execution_plan"):
        plan = state_update.get("execution_plan") or []
        parts.append(f"plan_steps={len(plan)}")
    if state_update.get("constraint_pack"):
        pack = state_update.get("constraint_pack") or {}
        if isinstance(pack, dict):
            parts.append(f"constraints={len(pack.get('constraints') or [])}")
    if state_update.get("agent_status"):
        parts.append(f"agent_status={len(state_update.get('agent_status') or {})}")
    if state_update.get("research_packets"):
        parts.append(f"research_packets={len(state_update.get('research_packets') or {})}")
    if state_update.get("map_projection"):
        parts.append("map_projection=ready")
    if state_update.get("pending_user_choice"):
        route_decision = "HALT"
        parts.append("pending_user_choice=true")

    return {
        "output_summary": ", ".join(parts) or "node completed",
        "route_decision": route_decision,
        "risk_flags": risk_flags,
    }


def trace_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a compact summary suitable for chat completion and developer diagnostics."""
    phase_counts: Dict[str, int] = {}
    statuses: Dict[str, int] = {}
    for event in events:
        phase = str(event.get("phase") or "unknown")
        status = str(event.get("status") or "unknown")
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "schema_version": "workflow_trace_summary.v1",
        "event_count": len(events),
        "phase_counts": phase_counts,
        "statuses": statuses,
    }
