"""Single product + inspect SSE projection (no developer_mode dual track).

Inspect-surface strategy: every authorized client receives the
same scrubbed event stream. UI progressive disclosure decides what is shown
by default vs behind an info affordance — transport is not dual-audience.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from ..entities.trip_input import GuidedIntakeState, RouteDecision
from ..entities.trip_run import TripRunStatus
from ..entities.trip_summary_card import TripSummaryCard
from ..services.public_delivery import public_event_manifest
from .audit_projection import audit_safe_value

# Product lifecycle + user-visible run boundaries.
_NORMAL_SSE_EVENT_TYPES = frozenset(
    {
        "chat_start",
        "chat_chunk",
        "route_confirmation",
        "guided_intake",
        "approval_gate_raised",
        "chat_complete",
        "delivery_ready",
        "run_terminal",
        "run_cancelled",
        "run_failed",
        "error",
    }
)

# Live research thinking-chain (summarized tool lifecycle + thoughts).
_RESEARCH_TRACE_EVENT_TYPES = frozenset(
    {
        "thinking",
        "agent_thinking",
        "agent_progress",
        "tool_start",
        "tool_result",
    }
)

# Inspect-surface hallmark events (always projected when scrubbed).
#
# ``trace_event`` / ``trace_summary`` are deliberately absent: the workflow
# trace is **internal observability**, and the thinking
# chain is already the higher-quality user-visible summary of the same run.  An
# internal-observability signal has no business crossing the client boundary.
# The durable record keeps it:
# ``trip_run_events`` still stores every trace event via ``_trace_event_payload``.
_INSPECT_SSE_EVENT_TYPES = frozenset(
    {
        "context_report",
        "context_compaction",
        "usage_update",
        "synthesis_start",
        "trip_summary_card",
    }
)


# ---------------------------------------------------------------------------
# Product payload constructors.
#
# **Do not put these through a recursive key blacklist.**  Every one of them is already
# authored from a validated model or an explicit projection upstream, so such a filter
# is a no-op almost everywhere — and where it is not, it is an active defect:
# ``GuidedIntakeState.raw_input`` gets dropped for containing ``raw_``, while
# ``ConversationThread.tsx`` reads that exact field to seed the trip planner.
# Re-validating through the owning model states the shape instead of guessing at it,
# and a field the model declares cannot be eaten on the way out.
# ---------------------------------------------------------------------------


def _project_route_decision(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return RouteDecision.model_validate(value).model_dump(mode="json")


def _project_guided_intake(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return GuidedIntakeState.model_validate(value).model_dump(mode="json")


def _project_summary_card(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return TripSummaryCard.model_validate(value).model_dump(mode="json")


def public_plan_gate_payload(payload: Any) -> Dict[str, Any]:
    """State the plan approval gate a client has to be able to act on or restore.

    Authored in exactly one place (``workflows/travel_planning.py``) and read in
    exactly one place (``frontend/src/lib/planApprovalGate.ts``), so the shape is
    named here once and both the live ``approval_gate_raised`` frame and the
    ``pending_user_choice`` replay in ``api/routes/trip_runs.py`` project through
    it.  Two copies of this list is how the live gate and the restored gate would
    start disagreeing about the same run.

    ``type`` (``"plan_gate"``) is **not** in the list: the frame already
    says which gate this is in its own ``gate`` field, and the durable envelope
    around this payload carries its own ``type`` (``"approval_gate"``, written in
    ``api/routes/chat.py``).  A third name for the same thing had no reader on
    either side — the normalizer reads ``payload.gate``, never ``payload.type``.
    """

    if not isinstance(payload, Mapping):
        return {}
    plan = payload.get("plan") if isinstance(payload.get("plan"), Mapping) else {}
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    return {
        "gate": payload.get("gate"),
        "revision": payload.get("revision"),
        "revision_limit": payload.get("revision_limit"),
        "revision_limit_reached": payload.get("revision_limit_reached"),
        "plan": {
            "steps": [
                {
                    "step": step.get("step"),
                    "agents": step.get("agents"),
                    # The full assignment text is deliberate: the gate asks the
                    # traveller to approve what each worker was told to do.
                    "tasks": step.get("tasks"),
                }
                for step in steps
                if isinstance(step, Mapping)
            ],
        },
        "plan_text": payload.get("plan_text"),
        "recognized_requirements": {
            key: [
                {
                    "requirement_id": item.get("requirement_id"),
                    "summary": item.get("summary"),
                }
                for item in (
                    payload.get("recognized_requirements", {}).get(key, [])
                    if isinstance(payload.get("recognized_requirements"), Mapping)
                    else []
                )
                if isinstance(item, Mapping)
            ]
            for key in ("hard", "preferences", "attention")
        },
        "decision_options": payload.get("decision_options"),
    }


def _project_answer_citations(value: Any) -> List[Dict[str, Any]]:
    """Project fast-answer citations to what a reader can actually open.

    ``claim_id`` / ``evidence_ids`` are absent on purpose: ``output_guard``
    already scrubs those very identifiers out of the prose as internal, so
    shipping them beside it in the same frame was incoherent.
    """

    if not isinstance(value, list):
        return []
    return [
        {
            "citation_id": item.get("citation_id"),
            "claim_text": item.get("claim_text"),
            "sources": [
                {
                    "title": source.get("title"),
                    "url": source.get("url"),
                    "source_name": source.get("source_name"),
                    "snippet": source.get("snippet"),
                    "authority_label": source.get("authority_label"),
                    "retrieved_at": source.get("retrieved_at"),
                }
                for source in (item.get("sources") or [])
                if isinstance(source, Mapping)
            ],
        }
        for item in value
        if isinstance(item, Mapping)
    ]


def _project_answer_annotations(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "annotation_id": item.get("annotation_id"),
            "kind": item.get("kind"),
            "label": item.get("label"),
            "detail": item.get("detail"),
        }
        for item in value
        if isinstance(item, Mapping)
    ]


def project_sse_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the single scrubbed projection for one SSE payload, or None to drop."""
    event_type = str(payload.get("type") or "")
    if event_type in _RESEARCH_TRACE_EVENT_TYPES:
        return _project_research_trace(event_type, payload)
    if event_type in _INSPECT_SSE_EVENT_TYPES:
        return _project_inspect_event(event_type, payload)
    if event_type not in _NORMAL_SSE_EVENT_TYPES:
        return None
    return _project_product_event(event_type, payload)


def _common_ids(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: payload[key]
        for key in ("type", "message_id", "run_id", "session_id", "ts_ms")
        if key in payload
    }


def _project_research_trace(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    projected: Dict[str, Any] = _common_ids(payload)
    for key in ("agent_name", "step_name"):
        if key in payload:
            projected[key] = payload[key]

    if event_type in {"thinking", "agent_thinking", "agent_progress"}:
        projected["content"] = payload.get("content", "")
        return projected

    projected.update(
        {
            "tool_name": payload.get("tool_name", ""),
            "tool_call_id": payload.get("tool_call_id"),
            "category": payload.get("category", "other"),
            "from_cache": bool(payload.get("from_cache", False)),
        }
    )
    if event_type == "tool_start":
        projected["args_summary"] = payload.get("args_summary", "")
    else:
        # ``status`` is the single authority for how this call ended, and the
        # only one.  ``success`` and ``degraded`` are not included:
        # ``success`` flattens a three-valued truth (succeeded / failed /
        # capability declared) into two — ``types/chat.ts`` says in so many words
        # "do not ship a boolean ``success`` beside it" — and ``degraded`` is
        # ``status == 'degraded'`` restated.  The consumer derives both from
        # ``status`` (``lib/toolDisplay.ts::thinkingStepStatusFromToolResult``).
        projected.update(
            {
                "summary": payload.get("summary", ""),
                "status": payload.get("status"),
                "duration_ms": payload.get("duration_ms"),
            }
        )
        # ``audit_id`` is gone with them: it is an internal identifier, and the
        # only surface that could have shown it (``ToolStepInspectPanel``)
        # deliberately does not print ids.
        for key in ("fallback_from", "fallback_to"):
            if payload.get(key) is not None and payload.get(key) != "":
                projected[key] = payload[key]
    return projected


# Only strip keys that are clearly raw I/O — not intentional inspect summaries.
_INSPECT_RAW_KEY_EXACT = frozenset(
    {
        "prompt",
        "prompts",
        "messages",
        "arguments",
        "argument",
        "args",
        "headers",
        "request_body",
        "response_body",
        "body",
    }
)


def _inspect_key_is_raw(key: str) -> bool:
    normalized = key.lower()
    if normalized in _INSPECT_RAW_KEY_EXACT or normalized.startswith("raw_"):
        return True
    return normalized.endswith(("_payload", "_body", "_headers")) or "message_content" in normalized


def _scrub_inspect_tree(value: Any) -> Any:
    """Drop raw I/O keys while keeping structured inspect summaries and counters."""
    if isinstance(value, dict):
        return {
            str(k): _scrub_inspect_tree(v)
            for k, v in value.items()
            if not _inspect_key_is_raw(str(k))
        }
    if isinstance(value, (list, tuple, set)):
        return [_scrub_inspect_tree(item) for item in value]
    return value


def _project_inspect_event(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Project inspect-surface hallmark events (context, usage, trace, summary card)."""
    projected = _scrub_inspect_tree(payload)
    if not isinstance(projected, dict):
        projected = {}
    projected["type"] = event_type
    for key in ("message_id", "run_id", "session_id", "ts_ms"):
        if key in payload:
            projected[key] = payload[key]

    if event_type == "trip_summary_card" and "summary_card" in payload:
        projected["summary_card"] = _project_summary_card(payload.get("summary_card"))
    if event_type == "usage_update":
        # Stated key by key, like ``context_compaction`` below and for the same
        # reason.  The per-call **attribution** fields (``tier`` / ``provider`` /
        # ``model`` / ``cached_input_tokens`` / ``latency_ms`` / ``ttft_ms``) are
        # internal observability: they stay in ``run_llm_calls`` and never cross
        # to a client.  The producer already
        # stopped sending them; this list is what keeps a future producer from
        # quietly re-adding one through a passthrough projection.
        #
        # ``node`` / ``agent`` remain: the live ledger names the step currently
        # spending.  Which ledger fields reach a screen is decided once, in
        # ``frontend/src/lib/costLedger.ts``.
        projected = {
            key: projected[key]
            for key in (
                "type",
                "message_id",
                "run_id",
                "node",
                "agent",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cost_usd",
                "estimated",
            )
            if key in projected
        }
    if event_type == "context_compaction":
        # Stated key by key rather than passed through.  The durable snapshot also
        # carries ``messages_compressed`` / ``tokens_before`` / ``tokens_after``,
        # and those must not ride out here: the ⓘ surface does not
        # print token counts, so shipping them means computing a readout nobody
        # shows.  They stay on the durable record, which is where
        # observability belongs.  What the notice needs is the summary, the
        # constraints it kept, and the three fields that decide whether it is a
        # valid event at all.
        projected = {
            key: projected[key]
            for key in ("type", "message_id", "event_id", "source", "occurred_at", "summary", "key_constraints")
            if key in projected
        }
    return projected


def _project_product_event(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    common = _common_ids(payload)
    if event_type == "chat_start":
        # ``route_decision`` is the point of this frame for the client: it is what
        # names the pending status line ("正在…") and seeds ``lastRouteDecision``,
        # which gates the discovery→intake follow-up card.  The producer
        # (``api/routes/chat.py``) has always sent it and ``useSendMessage`` has
        # always read it, so this projection keeps it in the frame.
        # ``mode`` / ``resumed`` / ``attempt`` / ``checkpoint_thread_id`` are not
        # sent: nothing on the client ever reads them, and resume bookkeeping
        # belongs to the durable run record, not to a chat frame.
        #
        # Absent rather than empty when there is no decision (the guard-blocked
        # stream has none): ``{}`` would be truthy on the client and it would ask
        # an empty object for its route.
        out: Dict[str, Any] = {**common}
        if isinstance(payload.get("route_decision"), Mapping):
            out["route_decision"] = _project_route_decision(payload["route_decision"])
        return out
    if event_type == "chat_chunk":
        # No ``role``: it was the constant ``"assistant"`` on every chunk ever sent,
        # and the client never read it — a chunk of the assistant's answer is the
        # only thing this frame can be.
        return {
            **common,
            "content": payload.get("content", ""),
            "show_content": payload.get("show_content", payload.get("content", "")),
        }
    if event_type == "route_confirmation":
        return {
            **common,
            "route_decision": _project_route_decision(payload.get("route_decision")),
        }
    if event_type == "guided_intake":
        # No ``task_status``: the producer sent it, the client never read it, and
        # what the intake form shows is decided by ``guided_intake`` itself.
        return {
            **common,
            "guided_intake": _project_guided_intake(payload.get("guided_intake")),
        }
    if event_type == "approval_gate_raised":
        # ``run_status`` stays and is now **read** rather than restated on the
        # client: the lifecycle status of a run is the backend's to name, and
        # ``useSendMessage`` used to hard-code ``'awaiting_input'`` next to a frame
        # that already said so — one fact with two owners.  ``mode`` is gone
        # (zero readers; the gate is deep-research-only by construction).
        return {
            **common,
            "gate": payload.get("gate"),
            "payload": public_plan_gate_payload(payload.get("payload")),
            "run_status": TripRunStatus.AWAITING_INPUT.value,
        }
    if event_type == "chat_complete":
        # ``run_status`` here is load-bearing and **not** a constant: it is what
        # tells the client whether the gate stays on screen (``!= awaiting_input``
        # clears it).  ``mode`` is not — it was never read.
        out: Dict[str, Any] = {
            **common,
            "run_status": payload.get("run_status"),
            "final_content": payload.get("final_content", ""),
            "citations": _project_answer_citations(payload.get("citations")),
            "annotations": _project_answer_annotations(payload.get("annotations")),
        }
        if payload.get("guard_blocked"):
            out["guard_blocked"] = True
        if isinstance(payload.get("run_cost_summary"), dict):
            # Whole-object key on purpose.  Which of the ledger's fields reach a
            # screen is decided in exactly one place —
            # ``frontend/src/lib/costLedger.ts`` (ON_SCREEN / UNIT / OFF_SCREEN,
            # one row per field with its reason).  Do not restate any
            # part of that verdict here.
            out["run_cost_summary"] = audit_safe_value(payload["run_cost_summary"])
        return out
    if event_type == "delivery_ready":
        # No ``message`` / ``event_id``.  The client writes its own sentence for
        # this moment ("旅行方案已准备好，可在右侧查看…") and never read ours;
        # ``event_id`` was never read either — the replay cursor is ``event_seq``.
        return {
            **common,
            "event_seq": payload.get("event_seq"),
            "bundle_id": payload.get("bundle_id"),
            "manifest": public_event_manifest(payload.get("manifest")),
            # Already the constructed public projection (``public_delivery_bundle``
            # at both producers); a second pass over it never removed anything.
            "bundle": payload.get("bundle") or {},
        }
    if event_type == "run_terminal":
        # ``status`` / ``event_seq`` / ``bundle_id`` are the atomicity contract the
        # client checks the pair of delivery frames against; ``message`` and
        # ``event_id`` were decoration nobody read.
        out = {
            **common,
            "event_seq": payload.get("event_seq"),
            "status": payload.get("status"),
            "bundle_id": payload.get("bundle_id"),
        }
        if isinstance(payload.get("run_cost_summary"), dict):
            # This frame — not ``chat_complete`` — is where a *successful* deep
            # research run ends (``chat.py`` returns right after emitting the
            # delivery pair).  The producer has always attached the settled ledger
            # here and ``useSendMessage`` has always read it, so this projection
            # keeps it.  Field-level verdicts stay in
            # ``frontend/src/lib/costLedger.ts``.
            out["run_cost_summary"] = audit_safe_value(payload["run_cost_summary"])
        return out
    if event_type == "run_cancelled":
        # ``mode`` / ``run_status`` were constants restating the frame's own name,
        # and a cancelled run has no final answer to cite — ``citations`` /
        # ``annotations`` were always empty and never read on this branch.
        # ``run_cost_summary`` is the opposite case: the producer has always sent
        # it and the client has always read it, so this projection keeps it.
        # A run you cancelled still cost what it cost.
        out = {
            **common,
            "final_content": payload.get("final_content", ""),
        }
        if isinstance(payload.get("run_cost_summary"), dict):
            out["run_cost_summary"] = audit_safe_value(payload["run_cost_summary"])
        return out
    if event_type == "run_failed":
        # ``message`` only.  ``run_status`` restated the frame name; the failure
        # codes (``reason_code`` / ``error_code``) are internal attribution with
        # no client reader — they are kept where they belong, on the durable run
        # record (``transition_status(terminal_reason_code=…)``), which is what
        # an operator reads.
        return {
            **common,
            "message": payload.get("message")
            or "旅行方案暂时无法生成，请稍后重试。",
        }
    # error and other allowlisted product failures: never leak provider prose.
    return {
        **common,
        "message": payload.get("message")
        or "旅行方案暂时无法生成，请稍后重试。",
    }
