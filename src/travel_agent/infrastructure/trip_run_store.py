"""TripRun persistence store.

The SQL store is the production path. The in-memory implementation keeps the
same async contract for unit tests and local API contract checks.
"""

from __future__ import annotations

import json
from copy import deepcopy
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import bindparam, text

from ..entities.delivery_bundle import DeliveryRevisionManifest
from ..entities.trip_run import (
    TripRun,
    TripRunDetail,
    TripRunEvent,
    TripRunMode,
    TripRunResumePolicy,
    TripRunState,
    TripRunStatus,
    assert_status_transition_allowed,
    build_trip_run_state_summary,
    build_trip_run_completion_audit,
    completion_audit_from_state_summary,
    coerce_mode,
    coerce_resume_policy,
    coerce_status,
    generate_trip_run_id,
    is_terminal_status,
    mark_cancelled_pending_choice,
    utc_now_iso,
    with_pending_choice_summary,
)
from ..local_profile import LOCAL_USER_ID
from .database import get_db_session


@dataclass(frozen=True)
class TripRunEventWindow:
    events: List[TripRunEvent]
    requested_after_sequence: int
    replay_floor_sequence: int
    latest_sequence: int
    window_expired: bool


class DeliveryCompletionBoundary(str, Enum):
    READY_EVENT = "delivery_ready_event"
    COMPLETED_STATUS = "completed_status"
    TERMINAL_EVENT = "run_terminal_event"


TripRunFailureInjector = Callable[[str], None]


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _validated_delivery_manifest(
    *,
    run_id: str,
    bundle_id: str,
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    """Return one canonical immutable manifest bound to this completion call."""

    if not isinstance(bundle_id, str) or not bundle_id.strip():
        raise ValueError("bundle_id is required")
    if not isinstance(manifest, dict):
        raise ValueError("delivery manifest must be an object")
    try:
        parsed = DeliveryRevisionManifest.model_validate(manifest)
    except Exception as exc:
        raise ValueError("delivery manifest is invalid") from exc
    if parsed.run_id != run_id:
        raise ValueError("delivery manifest run_id does not match TripRun")
    if parsed.bundle_id != bundle_id:
        raise ValueError("delivery manifest bundle_id does not match completion bundle_id")
    return parsed.model_dump(mode="json")


def _require_deep_completion_evidence(
    *,
    run_id: str,
    bundle_id: str,
    audit_value: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate the durable equivalent of a sealed Draft before deep completion.

    ``complete_delivery`` intentionally receives a compact audit rather than a
    full LangGraph checkpoint.  The authorization timestamp, immutable 5/6/8
    deadline schedule, and final Bundle projection are therefore the minimum
    evidence needed to prove that this completion is bound to one sealed Draft.
    """

    audit = completion_audit_from_state_summary(audit_value)
    if str(audit.get("run_id") or "") != run_id:
        raise ValueError("deep completion audit run_id does not match TripRun")
    draft_id = str(audit.get("draft_id") or "").strip()
    authorized_at = _parse_iso_datetime(audit.get("planning_authorized_at"))
    deadline = audit.get("deadline") if isinstance(audit.get("deadline"), dict) else {}
    target_at = _parse_iso_datetime(deadline.get("target_at"))
    closeout_at = _parse_iso_datetime(deadline.get("closeout_at"))
    composition_at = _parse_iso_datetime(deadline.get("composition_at"))
    delivery_deadline_at = _parse_iso_datetime(deadline.get("delivery_deadline_at"))
    observed_at = _parse_iso_datetime(deadline.get("last_observed_at"))
    if not draft_id or authorized_at is None:
        raise ValueError("deep completion requires sealed Draft authorization evidence")
    if any(
        value is None
        for value in (
            target_at,
            closeout_at,
            composition_at,
            delivery_deadline_at,
            observed_at,
        )
    ):
        raise ValueError("deep completion requires the sealed Draft deadline audit")
    assert target_at is not None
    assert closeout_at is not None
    assert composition_at is not None
    assert delivery_deadline_at is not None
    assert observed_at is not None
    # Validate against the seconds embedded in the completion audit (stamped when
    # the Draft was sealed), not the current ``run_deadline`` config.
    try:
        target_seconds = int(deadline.get("target_seconds") or 0)
        closeout_seconds = int(deadline.get("closeout_seconds") or 0)
        composition_seconds = int(deadline.get("composition_seconds") or 0)
        delivery_deadline_seconds = int(deadline.get("delivery_deadline_seconds") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("deep completion deadline audit is missing embedded window seconds") from exc
    if not (
        target_seconds
        and closeout_seconds
        and composition_seconds
        and delivery_deadline_seconds
    ):
        raise ValueError("deep completion deadline audit is missing embedded window seconds")
    if (
        (target_at - authorized_at).total_seconds() != target_seconds
        or (closeout_at - authorized_at).total_seconds() != closeout_seconds
        or (composition_at - authorized_at).total_seconds() != composition_seconds
        or (delivery_deadline_at - authorized_at).total_seconds()
        != delivery_deadline_seconds
        or observed_at < authorized_at
    ):
        raise ValueError("deep completion deadline audit is not bound to the sealed Draft")

    formal_delivery = (
        audit.get("formal_delivery")
        if isinstance(audit.get("formal_delivery"), dict)
        else {}
    )
    if (
        formal_delivery.get("bundle_id") != bundle_id
        or not bool(formal_delivery.get("has_bundle"))
        or not bool(formal_delivery.get("report_ready"))
        or not bool(formal_delivery.get("report_content_nonempty"))
        or not bool(formal_delivery.get("projection_consistent"))
    ):
        raise ValueError("deep completion requires an auditable ready Delivery Bundle")

    terminal = (
        audit.get("terminal_attribution")
        if isinstance(audit.get("terminal_attribution"), dict)
        else None
    )
    if terminal is not None and (
        terminal.get("draft_id") != draft_id
        or terminal.get("closure_status") != TripRunStatus.COMPLETED.value
        or terminal.get("delivery_bundle_id") != bundle_id
    ):
        raise ValueError("deep completion terminal attribution does not bind the Delivery Bundle")
    return deepcopy(audit)


def _completion_milestones(summary: Dict[str, Any]) -> List[tuple[str, str, Dict[str, Any], str]]:
    """Return due 0/5/6/8-minute events from a durable completion audit.

    The events use stable per-draft idempotency keys. Their durable
    event timestamp records persistence time; the payload contains only
    immutable schedule data so later checkpoints cannot conflict with an
    already-recorded milestone. A restart may observe several overdue
    boundaries in one snapshot, but it can never duplicate or move a boundary
    by granting a fresh planning window.
    """

    audit = completion_audit_from_state_summary(summary)
    draft_id = str(audit.get("draft_id") or "").strip()
    authorized_at = _parse_iso_datetime(audit.get("planning_authorized_at"))
    deadline = audit.get("deadline") if isinstance(audit.get("deadline"), dict) else {}
    observed_at = _parse_iso_datetime(deadline.get("last_observed_at"))
    if not draft_id or authorized_at is None:
        return []
    observed_at = observed_at or authorized_at
    # threshold_seconds 必须与 completion audit 内嵌秒数同源，禁止写死 300/360/480。
    try:
        target_seconds = int(deadline.get("target_seconds") or 0)
        closeout_seconds = int(deadline.get("closeout_seconds") or 0)
        delivery_seconds = int(deadline.get("delivery_deadline_seconds") or 0)
    except (TypeError, ValueError):
        return []
    definitions = (
        ("authorized", "run.planning_authorized", authorized_at, 0),
        (
            "target_missed",
            "run.target_missed",
            _parse_iso_datetime(deadline.get("target_at")),
            target_seconds,
        ),
        (
            "closeout_entered",
            "run.closeout_entered",
            _parse_iso_datetime(deadline.get("closeout_at")),
            closeout_seconds,
        ),
        (
            "delivery_deadline_exhausted",
            "run.delivery_deadline_exhausted",
            _parse_iso_datetime(deadline.get("delivery_deadline_at")),
            delivery_seconds,
        ),
    )
    milestones: List[tuple[str, str, Dict[str, Any], str]] = []
    for milestone, event_type, scheduled_at, threshold_seconds in definitions:
        if scheduled_at is None or observed_at < scheduled_at:
            continue
        # 非 authorized 里程碑缺少内嵌秒数时跳过（fail-closed，不写假 threshold）。
        if milestone != "authorized" and not threshold_seconds:
            continue
        key = f"completion_milestone:{draft_id}:{milestone}"
        milestones.append(
            (
                event_type,
                key,
                {
                    "draft_id": draft_id,
                    "milestone": milestone,
                    "threshold_seconds": threshold_seconds,
                    "scheduled_at": scheduled_at.isoformat(),
                },
                milestone,
            )
        )
    return milestones


def _completion_audit_for_status(
    audit_value: Dict[str, Any],
    target: TripRunStatus,
    *,
    reason_code: Optional[str] = None,
    delivery_bundle_id: Optional[str] = None,
    gate_class: Optional[str] = None,
) -> Dict[str, Any]:
    """Keep terminal attribution aligned with the durable lifecycle state."""

    audit = completion_audit_from_state_summary(audit_value)
    if not audit:
        return {}
    next_audit = deepcopy(audit)
    if target == TripRunStatus.RUNNING:
        next_audit["terminal_attribution"] = None
    elif target in {TripRunStatus.CANCELLED, TripRunStatus.FAILED}:
        existing = next_audit.get("terminal_attribution")
        existing_status = (
            existing.get("closure_status") if isinstance(existing, dict) else None
        )
        if existing_status != target.value:
            next_audit["terminal_attribution"] = {
                "draft_id": next_audit.get("draft_id"),
                "closure_status": target.value,
                "reason_code": reason_code
                or (
                    "user_cancelled"
                    if target == TripRunStatus.CANCELLED
                    else "delivery_integrity_failure"
                ),
                "recorded_at": utc_now_iso(),
                "delivery_bundle_id": delivery_bundle_id,
                "gate_class": gate_class,
            }
    elif target == TripRunStatus.COMPLETED:
        existing = next_audit.get("terminal_attribution")
        existing_status = (
            existing.get("closure_status") if isinstance(existing, dict) else None
        )
        if existing_status != TripRunStatus.COMPLETED.value:
            next_audit["terminal_attribution"] = {
                "draft_id": next_audit.get("draft_id"),
                "closure_status": TripRunStatus.COMPLETED.value,
                "reason_code": reason_code or "delivery_bundle_ready",
                "recorded_at": utc_now_iso(),
                "delivery_bundle_id": delivery_bundle_id,
                "gate_class": gate_class,
            }
    return next_audit


def _preserve_terminal_attribution(
    previous_audit_value: Dict[str, Any],
    next_audit_value: Dict[str, Any],
) -> Dict[str, Any]:
    """Do not let a lagging graph frame erase committed completion truth."""

    previous_audit = completion_audit_from_state_summary(previous_audit_value)
    next_audit = completion_audit_from_state_summary(next_audit_value)
    previous_terminal = previous_audit.get("terminal_attribution")
    if not previous_terminal:
        return next_audit
    if not next_audit:
        return deepcopy(previous_audit)
    merged_audit = deepcopy(next_audit)
    if not merged_audit.get("terminal_attribution"):
        merged_audit["terminal_attribution"] = deepcopy(previous_terminal)
    if (
        isinstance(previous_terminal, dict)
        and previous_terminal.get("closure_status") == TripRunStatus.COMPLETED.value
    ):
        previous_formal_delivery = previous_audit.get("formal_delivery")
        if isinstance(previous_formal_delivery, dict):
            # A completed Deep run has exactly one ready Bundle.  A delayed
            # pre-finalizer checkpoint must never replace that immutable
            # projection with its older no-bundle state.
            merged_audit["formal_delivery"] = deepcopy(previous_formal_delivery)
    return merged_audit


@asynccontextmanager
async def _session_scope(session: Any | None):
    """Reuse a caller-owned transaction without committing it prematurely."""

    if session is not None:
        yield session
        return
    async with get_db_session() as owned_session:
        yield owned_session


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _run_from_row(row: Dict[str, Any]) -> TripRun:
    return TripRun(
        run_id=row["run_id"],
        session_id=row.get("session_id") or "",
        user_id=row.get("user_id") or LOCAL_USER_ID,
        mode=coerce_mode(row.get("mode") or TripRunMode.DEEP.value),
        status=coerce_status(row.get("status") or TripRunStatus.CREATED.value),
        request_message_id=row.get("request_message_id") or "",
        assistant_message_id=row.get("assistant_message_id") or "",
        parent_run_id=row.get("parent_run_id"),
        current_node=row.get("current_node"),
        resume_token_hash=row.get("resume_token_hash"),
        resume_policy=coerce_resume_policy(
            row.get("resume_policy") or TripRunResumePolicy.CLARIFY_ONLY.value
        ),
        controlled_trip_identity=_json_loads(
            row.get("controlled_trip_identity"), None
        ),
        last_error_code=row.get("last_error_code"),
        last_error_message=row.get("last_error_message"),
        attempt=int(row.get("attempt") or 1),
        created_at=_iso(row.get("created_at")) or utc_now_iso(),
        updated_at=_iso(row.get("updated_at")) or utc_now_iso(),
        started_at=_iso(row.get("started_at")),
        completed_at=_iso(row.get("completed_at")),
        cancelled_at=_iso(row.get("cancelled_at")),
    )


def _state_from_row(row: Dict[str, Any]) -> TripRunState:
    return TripRunState(
        run_id=row["run_id"],
        status=coerce_status(row.get("status") or TripRunStatus.CREATED.value),
        current_node=row.get("current_node"),
        completed_nodes=list(_json_loads(row.get("completed_nodes"), [])),
        latest_state_summary=dict(_json_loads(row.get("latest_state_summary"), {})),
        completion_audit=dict(_json_loads(row.get("completion_audit"), {})),
        pending_user_choice=_json_loads(row.get("pending_user_choice"), None),
        trace_event_count=int(row.get("trace_event_count") or 0),
        pending_monitor_trigger_count=int(row.get("pending_monitor_trigger_count") or 0),
        last_error=_json_loads(row.get("last_error"), None),
        updated_at=_iso(row.get("updated_at")) or utc_now_iso(),
    )


def _event_from_row(row: Dict[str, Any]) -> TripRunEvent:
    return TripRunEvent(
        event_id=row.get("event_id"),
        run_id=row["run_id"],
        sequence=int(row.get("sequence") or 0),
        event_type=row.get("event_type") or "",
        payload=dict(_json_loads(row.get("payload"), {})),
        created_at=_iso(row.get("created_at")) or utc_now_iso(),
    )


class TripRunStore:
    """PostgreSQL-backed TripRun repository."""

    def __init__(
        self,
        *,
        failure_injector: Optional[TripRunFailureInjector] = None,
    ) -> None:
        self._failure_injector = failure_injector

    def _inject(self, boundary: DeliveryCompletionBoundary) -> None:
        if self._failure_injector is not None:
            self._failure_injector(boundary.value)

    async def create_run(
        self,
        *,
        session_id: str,
        user_id: str,
        mode: str | TripRunMode,
        request_message_id: str = "",
        assistant_message_id: str = "",
        run_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        resume_policy: str | TripRunResumePolicy = TripRunResumePolicy.CLARIFY_ONLY,
        controlled_trip_identity: Optional[Dict[str, Any]] = None,
        route_decision: Optional[Dict[str, Any]] = None,
    ) -> TripRun:
        run = TripRun(
            run_id=run_id or generate_trip_run_id(),
            session_id=session_id,
            user_id=user_id or LOCAL_USER_ID,
            mode=coerce_mode(mode),
            request_message_id=request_message_id,
            assistant_message_id=assistant_message_id,
            parent_run_id=parent_run_id,
            resume_policy=coerce_resume_policy(resume_policy),
            controlled_trip_identity=controlled_trip_identity,
        )
        initial_summary = {
            "route_decision": route_decision,
            "identity_locked": bool(controlled_trip_identity),
        }
        state = TripRunState(run_id=run.run_id, status=run.status, latest_state_summary=initial_summary)
        async with get_db_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO trip_runs
                        (run_id, session_id, user_id, mode, status,
                         request_message_id, assistant_message_id, parent_run_id,
                         current_node, resume_token_hash,
                         resume_policy, controlled_trip_identity,
                         last_error_code, last_error_message,
                         attempt, created_at, updated_at)
                    VALUES
                        (:run_id, :session_id, :user_id, :mode, :status,
                         :request_message_id, :assistant_message_id, :parent_run_id,
                         NULL, NULL, :resume_policy,
                         CAST(:controlled_trip_identity AS jsonb), NULL, NULL,
                         :attempt, NOW(), NOW())
                    """
                ),
                {
                    "run_id": run.run_id,
                    "session_id": run.session_id,
                    "user_id": run.user_id,
                    "mode": run.mode.value,
                    "status": run.status.value,
                    "request_message_id": run.request_message_id,
                    "assistant_message_id": run.assistant_message_id,
                    "parent_run_id": run.parent_run_id,
                    "resume_policy": run.resume_policy.value,
                    "controlled_trip_identity": (
                        _json_dumps(run.controlled_trip_identity)
                        if run.controlled_trip_identity is not None
                        else None
                    ),
                    "attempt": run.attempt,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO trip_run_states
                        (run_id, status, current_node, completed_nodes,
                         latest_state_summary, completion_audit, pending_user_choice,
                         trace_event_count,
                         pending_monitor_trigger_count, last_error, updated_at)
                    VALUES
                        (:run_id, :status, NULL, CAST(:completed_nodes AS jsonb),
                         CAST(:summary AS jsonb), CAST(:completion_audit AS jsonb), NULL, 0, 0, NULL, NOW())
                    """
                ),
                {
                    "run_id": state.run_id,
                    "status": state.status.value,
                    "completed_nodes": "[]",
                    "summary": _json_dumps(initial_summary),
                    "completion_audit": "{}",
                },
            )
            await self._append_event_in_session(
                session,
                run.run_id,
                "run.created",
                {
                    "mode": run.mode.value,
                    "session_id": run.session_id,
                    "user_id": run.user_id,
                    "resume_policy": run.resume_policy.value,
                    "route_decision": route_decision,
                },
            )
        return run

    async def get_run(self, run_id: str) -> Optional[TripRun]:
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT * FROM trip_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            row = result.mappings().first()
            return _run_from_row(dict(row)) if row else None

    async def get_detail(self, run_id: str, *, event_limit: int = 50) -> Optional[TripRunDetail]:
        async with get_db_session() as session:
            run_result = await session.execute(
                text("SELECT * FROM trip_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            run_row = run_result.mappings().first()
            if not run_row:
                return None
            state_result = await session.execute(
                text("SELECT * FROM trip_run_states WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            state_row = state_result.mappings().first()
            event_result = await session.execute(
                text(
                    """
                    SELECT *
                    FROM trip_run_events
                    WHERE run_id = :run_id
                    ORDER BY sequence DESC
                    LIMIT :limit
                    """
                ),
                {"run_id": run_id, "limit": event_limit},
            )
            events = [_event_from_row(dict(r)) for r in reversed(event_result.mappings().all())]
            return TripRunDetail(
                run=_run_from_row(dict(run_row)),
                state=_state_from_row(dict(state_row)) if state_row else TripRunState(run_id=run_id),
                events=events,
            )

    async def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> List[TripRunEvent]:
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT * FROM trip_run_events
                    WHERE run_id = :run_id AND sequence > :after_sequence
                    ORDER BY sequence ASC
                    LIMIT :limit
                    """
                ),
                {
                    "run_id": run_id,
                    "after_sequence": max(0, after_sequence),
                    "limit": max(1, min(limit, 500)),
                },
            )
            return [_event_from_row(dict(row)) for row in result.mappings().all()]

    async def list_delivery_events(
        self,
        run_id: str,
        bundle_id: str,
    ) -> List[TripRunEvent]:
        """Read the complete formal delivery pair without an event-window limit."""
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT * FROM trip_run_events
                    WHERE run_id = :run_id
                      AND event_type IN ('delivery.ready', 'run.terminal')
                      AND payload ->> 'bundle_id' = :bundle_id
                    ORDER BY sequence ASC
                    """
                ),
                {"run_id": run_id, "bundle_id": bundle_id},
            )
            return [_event_from_row(dict(row)) for row in result.mappings().all()]

    async def read_event_window(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        retention_events: int = 500,
    ) -> TripRunEventWindow:
        after_sequence = max(0, after_sequence)
        limit = max(1, min(limit, 500))
        retention_events = max(1, min(retention_events, 5000))
        async with get_db_session() as session:
            aggregate = await session.execute(
                text(
                    """
                    SELECT COALESCE(MIN(sequence), 0) AS first_sequence,
                           COALESCE(MAX(sequence), 0) AS latest_sequence
                    FROM trip_run_events
                    WHERE run_id = :run_id
                    """
                ),
                {"run_id": run_id},
            )
            row = aggregate.mappings().first()
            latest = int(row["latest_sequence"] if row else 0)
            first = int(row["first_sequence"] if row else 0)
            floor = max(first, latest - retention_events + 1) if latest else 0
            expired = after_sequence > 0 and floor > 0 and after_sequence < floor - 1
            events: List[TripRunEvent] = []
            if not expired:
                result = await session.execute(
                    text(
                        """
                        SELECT * FROM trip_run_events
                        WHERE run_id = :run_id
                          AND sequence > :after_sequence
                          AND sequence >= :floor
                        ORDER BY sequence ASC
                        LIMIT :limit
                        """
                    ),
                    {
                        "run_id": run_id,
                        "after_sequence": after_sequence,
                        "floor": floor,
                        "limit": limit,
                    },
                )
                events = [
                    _event_from_row(dict(event_row))
                    for event_row in result.mappings().all()
                ]
            return TripRunEventWindow(
                events=events,
                requested_after_sequence=after_sequence,
                replay_floor_sequence=floor,
                latest_sequence=latest,
                window_expired=expired,
            )

    async def list_runs(
        self,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        status: Optional[str | TripRunStatus] = None,
        mode: Optional[str | TripRunMode] = None,
        limit: int = 50,
    ) -> List[TripRun]:
        clauses: List[str] = []
        params: Dict[str, Any] = {"limit": max(1, min(limit, 200))}
        if user_id:
            clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if session_id:
            clauses.append("session_id = :session_id")
            params["session_id"] = session_id
        if status:
            clauses.append("status = :status")
            params["status"] = coerce_status(status).value
        if mode:
            clauses.append("mode = :mode")
            params["mode"] = coerce_mode(mode).value
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    f"""
                    SELECT *
                    FROM trip_runs
                    {where}
                    ORDER BY updated_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
            return [_run_from_row(dict(row)) for row in result.mappings().all()]

    async def count_runs(
        self,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        status: Optional[str | TripRunStatus] = None,
        mode: Optional[str | TripRunMode] = None,
    ) -> int:
        """Count every row ``list_runs`` would match, ignoring its LIMIT.

        The filter clauses must stay byte-for-byte equivalent to ``list_runs``:
        a total computed under different filters than the page it accompanies
        is worse than no total at all.
        """
        clauses: List[str] = []
        params: Dict[str, Any] = {}
        if user_id:
            clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if session_id:
            clauses.append("session_id = :session_id")
            params["session_id"] = session_id
        if status:
            clauses.append("status = :status")
            params["status"] = coerce_status(status).value
        if mode:
            clauses.append("mode = :mode")
            params["mode"] = coerce_mode(mode).value
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM trip_runs
                    {where}
                    """
                ),
                params,
            )
            return int(result.mappings().first()["count"])

    async def list_run_titles(self, session_ids: List[str], *, max_len: int = 60) -> Dict[str, str]:
        """Return a {session_id: title} map built from each session's first user message.

        The title is the raw first user message (truncated), which is the phrase a
        traveler actually typed. One batched ``DISTINCT ON`` query over
        chat_session_events keyed by (session_id, event_order) — the same unique
        index the events table already carries — so listing N runs stays a single
        round-trip instead of N per-run lookups.
        """
        unique_ids = [sid for sid in dict.fromkeys(session_ids) if sid]
        if not unique_ids:
            return {}
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (session_id) session_id, payload
                    FROM chat_session_events
                    WHERE session_id IN :session_ids
                      AND event_type = 'message.user'
                    ORDER BY session_id, event_order ASC
                    """
                ).bindparams(bindparam("session_ids", expanding=True)),
                {"session_ids": unique_ids},
            )
            titles: Dict[str, str] = {}
            for row in result.mappings().all():
                payload = _json_loads(row.get("payload"), {})
                content = str(payload.get("content") or "").strip()
                if not content:
                    continue
                titles[row["session_id"]] = (
                    content[:max_len] + "…" if len(content) > max_len else content
                )
            return titles

    async def transition_status(
        self,
        run_id: str,
        target_status: str | TripRunStatus,
        *,
        current_node: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        pending_user_choice: Optional[Dict[str, Any]] = None,
        event_type: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        allow_same: bool = True,
        terminal_reason_code: Optional[str] = None,
        terminal_gate_class: Optional[str] = None,
        completion_audit: Optional[Dict[str, Any]] = None,
    ) -> TripRun:
        target = coerce_status(target_status)
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT * FROM trip_runs WHERE run_id = :run_id FOR UPDATE"),
                {"run_id": run_id},
            )
            row = result.mappings().first()
            if not row:
                raise KeyError(f"TripRun not found: {run_id}")
            current = coerce_status(row["status"])
            assert_status_transition_allowed(current, target, allow_same=allow_same)
            if (
                target == TripRunStatus.COMPLETED
                and coerce_mode(row["mode"]) == TripRunMode.DEEP
            ):
                raise ValueError(
                    "deep TripRun completion requires complete_delivery() so the Bundle, "
                    "delivery.ready, and run.terminal remain one atomic boundary"
                )
            attempt_increment = (
                1
                if target == TripRunStatus.RUNNING
                and current in {TripRunStatus.FAILED, TripRunStatus.INTERRUPTED}
                else 0
            )
            state_result = await session.execute(
                text("SELECT latest_state_summary, completion_audit, pending_user_choice FROM trip_run_states WHERE run_id = :run_id FOR UPDATE"),
                {"run_id": run_id},
            )
            state_row = state_result.mappings().first()
            current_summary = _json_loads(
                state_row.get("latest_state_summary") if state_row else None,
                {},
            )
            current_pending = _json_loads(
                state_row.get("pending_user_choice") if state_row else None,
                None,
            )
            current_audit = _json_loads(
                state_row.get("completion_audit") if state_row else None,
                {},
            )
            resolved_pending = (
                mark_cancelled_pending_choice(pending_user_choice or current_pending)
                if target == TripRunStatus.CANCELLED
                else pending_user_choice
            )
            summary_pending = bool(resolved_pending) and target == TripRunStatus.AWAITING_INPUT
            if not pending_user_choice and target != TripRunStatus.AWAITING_INPUT:
                summary_pending = False
            next_summary = (
                with_pending_choice_summary(current_summary, pending=summary_pending)
                if resolved_pending or target != TripRunStatus.AWAITING_INPUT
                else current_summary
            )
            next_audit = _completion_audit_for_status(
                completion_audit or current_audit,
                target,
                reason_code=terminal_reason_code,
                gate_class=terminal_gate_class,
            )

            timestamps = {
                "started_at": "NOW()" if target == TripRunStatus.RUNNING else "started_at",
                "completed_at": "NOW()" if target == TripRunStatus.COMPLETED else "completed_at",
                "cancelled_at": "NOW()" if target == TripRunStatus.CANCELLED else "cancelled_at",
            }
            await session.execute(
                text(
                    f"""
                    UPDATE trip_runs
                    SET status = :status,
                        current_node = COALESCE(:current_node, current_node),
                        last_error_code = :error_code,
                        last_error_message = :error_message,
                        attempt = attempt + :attempt_increment,
                        started_at = COALESCE(started_at, {timestamps["started_at"]}),
                        completed_at = {timestamps["completed_at"]},
                        cancelled_at = {timestamps["cancelled_at"]},
                        updated_at = NOW()
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "status": target.value,
                    "current_node": current_node,
                    "error_code": error_code,
                    "error_message": error_message,
                    "attempt_increment": attempt_increment,
                },
            )
            last_error = None
            if error_code or error_message:
                last_error = {"code": error_code or "error", "message": error_message or ""}
            await session.execute(
                text(
                    """
                    UPDATE trip_run_states
                    SET status = :status,
                        current_node = COALESCE(:current_node, current_node),
                        latest_state_summary = CAST(:summary AS jsonb),
                        completion_audit = CAST(:completion_audit AS jsonb),
                        pending_user_choice = CAST(:pending_user_choice AS jsonb),
                        last_error = CAST(:last_error AS jsonb),
                        updated_at = NOW()
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "status": target.value,
                    "current_node": current_node,
                    "summary": _json_dumps(next_summary),
                    "completion_audit": _json_dumps(next_audit),
                    "pending_user_choice": _json_dumps(resolved_pending) if resolved_pending else None,
                    "last_error": _json_dumps(last_error) if last_error else None,
                },
            )
            terminal_attribution = completion_audit_from_state_summary(
                next_audit
            ).get("terminal_attribution")
            await self._append_event_in_session(
                session,
                run_id,
                event_type or f"run.{target.value}",
                {
                    "from_status": current.value,
                    "to_status": target.value,
                    **(payload or {}),
                    **({"current_node": current_node} if current_node else {}),
                    **({"last_error": last_error} if last_error else {}),
                    **(
                        {"terminal_attribution": terminal_attribution}
                        if terminal_attribution is not None
                        else {}
                    ),
                },
            )
            refreshed = await session.execute(
                text("SELECT * FROM trip_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            return _run_from_row(dict(refreshed.mappings().first()))

    async def complete_delivery(
        self,
        run_id: str,
        *,
        bundle_id: str,
        manifest: Dict[str, Any],
        current_node: str = "delivery_finalizer",
        completion_audit: Optional[Dict[str, Any]] = None,
        session: Any | None = None,
    ) -> tuple[TripRun, TripRunEvent, TripRunEvent]:
        """Atomically expose ready, COMPLETED, and terminal durable truth."""
        manifest_payload = _validated_delivery_manifest(
            run_id=run_id,
            bundle_id=bundle_id,
            manifest=manifest,
        )
        ready_key = f"{run_id}:delivery_ready:{bundle_id}"
        terminal_key = f"{run_id}:run_terminal:{bundle_id}"
        async with _session_scope(session) as session:
            result = await session.execute(
                text("SELECT * FROM trip_runs WHERE run_id = :run_id FOR UPDATE"),
                {"run_id": run_id},
            )
            row = result.mappings().first()
            if row is None:
                raise KeyError(f"TripRun not found: {run_id}")
            current = coerce_status(row["status"])
            state_result = await session.execute(
                text(
                    """
                    SELECT latest_state_summary, completion_audit
                    FROM trip_run_states
                    WHERE run_id = :run_id
                    FOR UPDATE
                    """
                ),
                {"run_id": run_id},
            )
            state_row = state_result.mappings().first()
            if state_row is None:
                raise KeyError(f"TripRunState not found: {run_id}")
            summary = with_pending_choice_summary(
                _json_loads(state_row.get("latest_state_summary"), {}),
                pending=False,
            )
            audit_input = (
                deepcopy(completion_audit)
                if completion_audit is not None
                else _json_loads(state_row.get("completion_audit"), {})
            )
            audit = (
                _require_deep_completion_evidence(
                    run_id=run_id,
                    bundle_id=bundle_id,
                    audit_value=audit_input,
                )
                if coerce_mode(row["mode"]) == TripRunMode.DEEP
                else completion_audit_from_state_summary(audit_input)
            )

            existing_result = await session.execute(
                text(
                    """
                    SELECT * FROM trip_run_events
                    WHERE run_id = :run_id
                      AND event_type IN ('delivery.ready', 'run.terminal')
                    ORDER BY sequence ASC
                    """
                ),
                {"run_id": run_id},
            )
            existing = [
                _event_from_row(dict(event_row))
                for event_row in existing_result.mappings().all()
            ]
            if current == TripRunStatus.COMPLETED:
                ready_events = [
                    event for event in existing if event.event_type == "delivery.ready"
                ]
                terminal_events = [
                    event for event in existing if event.event_type == "run.terminal"
                ]
                if (
                    len(ready_events) != 1
                    or len(terminal_events) != 1
                    or ready_events[0].payload.get("bundle_id") != bundle_id
                    or terminal_events[0].payload.get("bundle_id") != bundle_id
                    or ready_events[0].payload.get("manifest") != manifest_payload
                    or terminal_events[0].payload.get("manifest") != manifest_payload
                    or ready_events[0].sequence >= terminal_events[0].sequence
                ):
                    raise RuntimeError("completed delivery events are missing or duplicated")
                return _run_from_row(dict(row)), ready_events[0], terminal_events[0]
            assert_status_transition_allowed(
                current,
                TripRunStatus.COMPLETED,
                allow_same=False,
            )
            if existing:
                raise RuntimeError("non-completed run already exposes delivery events")

            ready_event = await self._append_event_in_session(
                session,
                run_id,
                "delivery.ready",
                {
                    "bundle_id": bundle_id,
                    "manifest": manifest_payload,
                    "idempotency_key": ready_key,
                },
            )
            self._inject(DeliveryCompletionBoundary.READY_EVENT)
            audit = _completion_audit_for_status(
                audit,
                TripRunStatus.COMPLETED,
                reason_code="delivery_bundle_ready",
                delivery_bundle_id=bundle_id,
            )
            terminal_attribution = completion_audit_from_state_summary(
                audit
            ).get("terminal_attribution")
            await session.execute(
                text(
                    """
                    UPDATE trip_runs
                    SET status = :status,
                        current_node = :current_node,
                        last_error_code = NULL,
                        last_error_message = NULL,
                        completed_at = NOW(),
                        updated_at = NOW()
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "status": TripRunStatus.COMPLETED.value,
                    "current_node": current_node,
                },
            )
            await session.execute(
                text(
                    """
                    UPDATE trip_run_states
                    SET status = :status,
                        current_node = :current_node,
                        latest_state_summary = CAST(:summary AS jsonb),
                        completion_audit = CAST(:completion_audit AS jsonb),
                        pending_user_choice = NULL,
                        last_error = NULL,
                        updated_at = NOW()
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "status": TripRunStatus.COMPLETED.value,
                    "current_node": current_node,
                    "summary": _json_dumps(summary),
                    "completion_audit": _json_dumps(audit),
                },
            )
            self._inject(DeliveryCompletionBoundary.COMPLETED_STATUS)

            terminal_event = await self._append_event_in_session(
                session,
                run_id,
                "run.terminal",
                {
                    "from_status": current.value,
                    "to_status": TripRunStatus.COMPLETED.value,
                    "status": TripRunStatus.COMPLETED.value,
                    "bundle_id": bundle_id,
                    "manifest": manifest_payload,
                    "delivery_event_sequence": ready_event.sequence,
                    "current_node": current_node,
                    "idempotency_key": terminal_key,
                    **(
                        {"terminal_attribution": terminal_attribution}
                        if terminal_attribution is not None
                        else {}
                    ),
                },
            )
            self._inject(DeliveryCompletionBoundary.TERMINAL_EVENT)
            refreshed = await session.execute(
                text("SELECT * FROM trip_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            return (
                _run_from_row(dict(refreshed.mappings().first())),
                ready_event,
                terminal_event,
            )

    async def claim_checkpoint_resume(
        self,
        run_id: str,
        *,
        allowed_statuses: Optional[List[str | TripRunStatus]] = None,
        current_node: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[TripRun]:
        """Atomically claim a checkpoint-backed resume attempt.

        Returns the updated run when this caller won the compare-and-swap, or
        None when another caller already moved the run out of a resumable state.
        """
        statuses = [
            coerce_status(status).value
            for status in (allowed_statuses or [TripRunStatus.FAILED, TripRunStatus.INTERRUPTED])
        ]
        async with get_db_session() as session:
            existing = await session.execute(
                text("SELECT * FROM trip_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            existing_row = existing.mappings().first()
            if existing_row is None:
                raise KeyError(f"TripRun not found: {run_id}")
            previous_status = coerce_status(existing_row["status"])

            claim_stmt = text(
                """
                UPDATE trip_runs
                SET status = :running,
                    current_node = COALESCE(:current_node, current_node),
                    last_error_code = NULL,
                    last_error_message = NULL,
                    attempt = attempt + 1,
                    started_at = COALESCE(started_at, NOW()),
                    updated_at = NOW()
                WHERE run_id = :run_id
                  AND status IN :allowed_statuses
                  AND resume_policy = :resume_policy
                RETURNING *
                """
            ).bindparams(bindparam("allowed_statuses", expanding=True))
            result = await session.execute(
                claim_stmt,
                {
                    "run_id": run_id,
                    "running": TripRunStatus.RUNNING.value,
                    "current_node": current_node,
                    "allowed_statuses": statuses,
                    "resume_policy": TripRunResumePolicy.CHECKPOINT.value,
                },
            )
            row = result.mappings().first()
            if not row:
                return None
            state_summary_result = await session.execute(
                text("SELECT latest_state_summary, completion_audit FROM trip_run_states WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            state_summary_row = state_summary_result.mappings().first()
            resume_summary = with_pending_choice_summary(
                _json_loads(
                    state_summary_row.get("latest_state_summary") if state_summary_row else None,
                    {},
                ),
                pending=False,
            )
            resume_audit = _completion_audit_for_status(
                _json_loads(
                    state_summary_row.get("completion_audit") if state_summary_row else None,
                    {},
                ),
                TripRunStatus.RUNNING,
            )

            await session.execute(
                text(
                    """
                    UPDATE trip_run_states
                    SET status = :running,
                        current_node = COALESCE(:current_node, current_node),
                        latest_state_summary = CAST(:summary AS jsonb),
                        completion_audit = CAST(:completion_audit AS jsonb),
                        pending_user_choice = NULL,
                        last_error = NULL,
                        updated_at = NOW()
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "running": TripRunStatus.RUNNING.value,
                    "current_node": current_node,
                    "summary": _json_dumps(resume_summary),
                    "completion_audit": _json_dumps(resume_audit),
                },
            )
            updated = _run_from_row(dict(row))
            await self._append_event_in_session(
                session,
                run_id,
                "run.resumed",
                {
                    "from_status": previous_status.value,
                    "to_status": TripRunStatus.RUNNING.value,
                    "resume_policy": TripRunResumePolicy.CHECKPOINT.value,
                    "attempt": updated.attempt,
                    **(payload or {}),
                    **({"current_node": current_node} if current_node else {}),
                },
            )
            return updated

    async def list_checkpoint_prune_candidates(
        self,
        *,
        completed_days: int = 30,
        cancelled_days: int = 30,
        failed_interrupted_days: int = 90,
        limit: int = 100,
    ) -> List[TripRun]:
        """Return terminal/resumable runs whose checkpoint threads may be pruned."""
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT *
                    FROM trip_runs
                    WHERE resume_policy = :resume_policy
                      AND (
                        (status = :completed AND updated_at < NOW() - (:completed_days * INTERVAL '1 day'))
                        OR (status = :cancelled AND updated_at < NOW() - (:cancelled_days * INTERVAL '1 day'))
                        OR (
                            status IN (:failed, :interrupted)
                            AND updated_at < NOW() - (:failed_interrupted_days * INTERVAL '1 day')
                        )
                      )
                    ORDER BY updated_at ASC
                    LIMIT :limit
                    """
                ),
                {
                    "resume_policy": TripRunResumePolicy.CHECKPOINT.value,
                    "completed": TripRunStatus.COMPLETED.value,
                    "cancelled": TripRunStatus.CANCELLED.value,
                    "failed": TripRunStatus.FAILED.value,
                    "interrupted": TripRunStatus.INTERRUPTED.value,
                    "completed_days": max(0, completed_days),
                    "cancelled_days": max(0, cancelled_days),
                    "failed_interrupted_days": max(0, failed_interrupted_days),
                    "limit": max(1, min(limit, 1000)),
                },
            )
            return [_run_from_row(dict(row)) for row in result.mappings().all()]

    async def record_node_lifecycle(
        self,
        run_id: str,
        *,
        node: str,
        status: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[TripRunEvent]:
        """Persist one node execution fact and advance current_node on start."""
        if not node.strip():
            raise ValueError("Node lifecycle requires a node name")
        if status not in {"started", "completed", "retrying", "failed"}:
            raise ValueError(f"Unsupported node lifecycle status: {status}")
        event_payload = {**(payload or {}), "node": node, "status": status}
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT status FROM trip_runs WHERE run_id = :run_id FOR UPDATE"),
                {"run_id": run_id},
            )
            row = result.mappings().first()
            if row is None:
                raise KeyError(f"TripRun not found: {run_id}")
            run_status = coerce_status(row["status"])
            if is_terminal_status(run_status):
                return None
            if status == "started":
                if run_status != TripRunStatus.RUNNING:
                    return None
                await session.execute(
                    text(
                        """
                        UPDATE trip_runs
                        SET current_node = :node, updated_at = NOW()
                        WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": run_id, "node": node},
                )
                await session.execute(
                    text(
                        """
                        UPDATE trip_run_states
                        SET current_node = :node, updated_at = NOW()
                        WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": run_id, "node": node},
                )
            return await self._append_event_in_session(
                session,
                run_id,
                f"node.{status}",
                event_payload,
            )

    async def record_node_update(
        self,
        run_id: str,
        *,
        node: str,
        trace_event_count: Optional[int] = None,
    ) -> TripRunState:
        """Note that one graph node finished, and how many traces it has produced.

        **It must not touch ``pending_user_choice``.**  Overwriting that column from the
        graph state on every node event silently nulls a plan gate's durable decision as
        soon as any other node runs — the column has one writer (the gate, through
        ``transition_status``) and one reader (the read side, which republishes only the
        gate shape), and a node update cannot serve either.  What a node update does
        decide is the one thing it knows: a terminal Run has no decision left to make.
        """

        async with get_db_session() as session:
            # Every transaction that touches both rows must lock the parent Run
            # before its state row. complete_delivery() uses the same order;
            # reversing it here can deadlock exactly when the final state event
            # races the atomic delivery transaction.
            run_result = await session.execute(
                text("SELECT run_id FROM trip_runs WHERE run_id = :run_id FOR UPDATE"),
                {"run_id": run_id},
            )
            if run_result.mappings().first() is None:
                raise KeyError(f"TripRun not found: {run_id}")
            result = await session.execute(
                text("SELECT * FROM trip_run_states WHERE run_id = :run_id FOR UPDATE"),
                {"run_id": run_id},
            )
            row = result.mappings().first()
            if not row:
                raise KeyError(f"TripRunState not found: {run_id}")
            state = _state_from_row(dict(row))
            pending_choice = state.pending_user_choice
            if is_terminal_status(state.status):
                pending_choice = (
                    mark_cancelled_pending_choice(pending_choice)
                    if state.status == TripRunStatus.CANCELLED
                    else None
                )
            summary = with_pending_choice_summary(
                state.latest_state_summary,
                pending=bool(pending_choice) and not is_terminal_status(state.status),
            )
            completed_nodes = list(state.completed_nodes)
            if node and node not in completed_nodes:
                completed_nodes.append(node)
            await session.execute(
                text(
                    """
                    UPDATE trip_run_states
                    SET completed_nodes = CAST(:completed_nodes AS jsonb),
                        latest_state_summary = CAST(:summary AS jsonb),
                        pending_user_choice = CAST(:pending_user_choice AS jsonb),
                        trace_event_count = COALESCE(:trace_event_count, trace_event_count),
                        updated_at = NOW()
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "node": node,
                    "completed_nodes": _json_dumps(completed_nodes),
                    "summary": _json_dumps(summary),
                    "pending_user_choice": _json_dumps(pending_choice) if pending_choice else None,
                    "trace_event_count": trace_event_count,
                },
            )
            await session.execute(
                text("UPDATE trip_runs SET updated_at = NOW() WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            refreshed = await session.execute(
                text("SELECT * FROM trip_run_states WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            return _state_from_row(dict(refreshed.mappings().first()))

    async def record_state_snapshot(
        self,
        run_id: str,
        *,
        state_snapshot: Any,
    ) -> TripRunState:
        summary = build_trip_run_state_summary(state_snapshot)
        audit = build_trip_run_completion_audit(state_snapshot)
        async with get_db_session() as session:
            # Keep the parent -> state lock order consistent with
            # complete_delivery(), transition_status(), and node lifecycle.
            run_result = await session.execute(
                text("SELECT run_id FROM trip_runs WHERE run_id = :run_id FOR UPDATE"),
                {"run_id": run_id},
            )
            if run_result.mappings().first() is None:
                raise KeyError(f"TripRun not found: {run_id}")
            result = await session.execute(
                text("SELECT * FROM trip_run_states WHERE run_id = :run_id FOR UPDATE"),
                {"run_id": run_id},
            )
            row = result.mappings().first()
            if not row:
                raise KeyError(f"TripRunState not found: {run_id}")
            state = _state_from_row(dict(row))
            audit = _preserve_terminal_attribution(state.completion_audit, audit)
            audit = _completion_audit_for_status(audit, state.status)
            summary = with_pending_choice_summary(
                summary,
                pending=(
                    state.status == TripRunStatus.AWAITING_INPUT
                    and bool(state.pending_user_choice)
                ),
            )
            await session.execute(
                text(
                    """
                    UPDATE trip_run_states
                    SET latest_state_summary = CAST(:summary AS jsonb),
                        completion_audit = CAST(:completion_audit AS jsonb),
                        updated_at = NOW()
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "summary": _json_dumps(summary),
                    "completion_audit": _json_dumps(audit),
                },
            )
            await session.execute(
                text("UPDATE trip_runs SET updated_at = NOW() WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            for event_type, idempotency_key, payload, _milestone in _completion_milestones(
                audit
            ):
                await self._append_event_once_in_session(
                    session,
                    run_id,
                    event_type,
                    payload,
                    idempotency_key=idempotency_key,
                )
            refreshed = await session.execute(
                text("SELECT * FROM trip_run_states WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            return _state_from_row(dict(refreshed.mappings().first()))

    async def append_event(self, run_id: str, event_type: str, payload: Dict[str, Any]) -> TripRunEvent:
        async with get_db_session() as session:
            return await self._append_event_in_session(session, run_id, event_type, payload)

    async def append_event_once(
        self,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
    ) -> TripRunEvent:
        if not idempotency_key.strip():
            raise ValueError("event idempotency_key is required")
        async with get_db_session() as session:
            return await self._append_event_once_in_session(
                session,
                run_id,
                event_type,
                payload,
                idempotency_key=idempotency_key,
            )

    async def _append_event_once_in_session(
        self,
        session: Any,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
    ) -> TripRunEvent:
        if not idempotency_key.strip():
            raise ValueError("event idempotency_key is required")
        durable_payload = {**payload, "idempotency_key": idempotency_key}
        lock_result = await session.execute(
            text("SELECT run_id FROM trip_runs WHERE run_id = :run_id FOR UPDATE"),
            {"run_id": run_id},
        )
        if lock_result.mappings().first() is None:
            raise KeyError(f"TripRun not found: {run_id}")
        existing_result = await session.execute(
            text(
                """
                SELECT * FROM trip_run_events
                WHERE run_id = :run_id
                  AND payload ->> 'idempotency_key' = :idempotency_key
                ORDER BY sequence ASC
                LIMIT 1
                """
            ),
            {"run_id": run_id, "idempotency_key": idempotency_key},
        )
        existing = existing_result.mappings().first()
        if existing is not None:
            event = _event_from_row(dict(existing))
            if event.event_type != event_type or event.payload != durable_payload:
                raise ValueError("event idempotency key was used for another payload")
            return event
        return await self._append_event_in_session(
            session, run_id, event_type, durable_payload
        )

    async def request_cancel(
        self,
        run_id: str,
        *,
        current_node: Optional[str] = None,
        event_type: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TripRun:
        for _ in range(3):
            run = await self.get_run(run_id)
            if run is None:
                raise KeyError(f"TripRun not found: {run_id}")
            target = (
                TripRunStatus.CANCEL_REQUESTED
                if run.status == TripRunStatus.RUNNING
                else TripRunStatus.CANCELLED
                if run.status in {
                    TripRunStatus.CREATED,
                    TripRunStatus.AWAITING_INPUT,
                    TripRunStatus.CANCEL_REQUESTED,
                }
                else None
            )
            if target is None:
                return run
            try:
                return await self.transition_status(
                    run_id,
                    target,
                    current_node=current_node,
                    event_type=event_type,
                    payload=payload,
                )
            except ValueError:
                continue
        run = await self.get_run(run_id)
        if run is None:
            raise KeyError(f"TripRun not found: {run_id}")
        if run.status in {
            TripRunStatus.CREATED,
            TripRunStatus.RUNNING,
            TripRunStatus.AWAITING_INPUT,
            TripRunStatus.CANCEL_REQUESTED,
        }:
            raise RuntimeError(f"TripRun cancellation did not converge: {run_id}")
        return run

    async def _append_event_in_session(
        self,
        session: Any,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> TripRunEvent:
        lock_result = await session.execute(
            text("SELECT run_id FROM trip_runs WHERE run_id = :run_id FOR UPDATE"),
            {"run_id": run_id},
        )
        if lock_result.mappings().first() is None:
            raise KeyError(f"TripRun not found: {run_id}")
        seq_result = await session.execute(
            text("SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM trip_run_events WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
        sequence = int(seq_result.mappings().first()["next_sequence"])
        result = await session.execute(
            text(
                """
                INSERT INTO trip_run_events
                    (run_id, sequence, event_type, payload, created_at)
                VALUES
                    (:run_id, :sequence, :event_type, CAST(:payload AS jsonb), NOW())
                RETURNING *
                """
            ),
            {
                "run_id": run_id,
                "sequence": sequence,
                "event_type": event_type,
                "payload": _json_dumps(payload),
            },
        )
        return _event_from_row(dict(result.mappings().first()))


class InMemoryTripRunStore(TripRunStore):
    """In-memory TripRunStore with the same async contract."""

    def __init__(
        self,
        *,
        failure_injector: Optional[TripRunFailureInjector] = None,
    ) -> None:
        super().__init__(failure_injector=failure_injector)
        self.runs: Dict[str, TripRun] = {}
        self.states: Dict[str, TripRunState] = {}
        self.events: Dict[str, List[TripRunEvent]] = defaultdict(list)

    def snapshot_for_initial_delivery(
        self,
    ) -> tuple[Dict[str, TripRun], Dict[str, TripRunState], Dict[str, List[TripRunEvent]]]:
        """Capture mutable lifecycle state for a coordinated in-memory transaction."""

        return (
            deepcopy(self.runs),
            deepcopy(self.states),
            deepcopy(dict(self.events)),
        )

    def restore_initial_delivery(
        self,
        snapshot: tuple[
            Dict[str, TripRun],
            Dict[str, TripRunState],
            Dict[str, List[TripRunEvent]],
        ],
    ) -> None:
        runs, states, events = snapshot
        self.runs = runs
        self.states = states
        self.events = defaultdict(list, events)

    async def create_run(
        self,
        *,
        session_id: str,
        user_id: str,
        mode: str | TripRunMode,
        request_message_id: str = "",
        assistant_message_id: str = "",
        run_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        resume_policy: str | TripRunResumePolicy = TripRunResumePolicy.CLARIFY_ONLY,
        controlled_trip_identity: Optional[Dict[str, Any]] = None,
        route_decision: Optional[Dict[str, Any]] = None,
    ) -> TripRun:
        run = TripRun(
            run_id=run_id or generate_trip_run_id(),
            session_id=session_id,
            user_id=user_id or LOCAL_USER_ID,
            mode=coerce_mode(mode),
            request_message_id=request_message_id,
            assistant_message_id=assistant_message_id,
            parent_run_id=parent_run_id,
            resume_policy=coerce_resume_policy(resume_policy),
            controlled_trip_identity=controlled_trip_identity,
        )
        if run.run_id in self.runs:
            raise ValueError(f"TripRun already exists: {run.run_id}")
        self.runs[run.run_id] = run
        initial_summary = {
            "route_decision": route_decision,
            "identity_locked": bool(controlled_trip_identity),
        }
        self.states[run.run_id] = TripRunState(
            run_id=run.run_id,
            status=run.status,
            latest_state_summary=initial_summary,
        )
        await self.append_event(
            run.run_id,
            "run.created",
            {
                "mode": run.mode.value,
                "session_id": run.session_id,
                "user_id": run.user_id,
                "resume_policy": run.resume_policy.value,
                "route_decision": route_decision,
            },
        )
        return run

    async def get_run(self, run_id: str) -> Optional[TripRun]:
        return self.runs.get(run_id)

    async def get_detail(self, run_id: str, *, event_limit: int = 50) -> Optional[TripRunDetail]:
        run = self.runs.get(run_id)
        state = self.states.get(run_id)
        if not run or not state:
            return None
        return TripRunDetail(
            run=run,
            state=state,
            events=list(self.events.get(run_id, []))[-event_limit:],
        )

    async def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> List[TripRunEvent]:
        if run_id not in self.runs:
            raise KeyError(f"TripRun not found: {run_id}")
        return [
            event
            for event in self.events[run_id]
            if event.sequence > max(0, after_sequence)
        ][: max(1, min(limit, 500))]

    async def list_delivery_events(
        self,
        run_id: str,
        bundle_id: str,
    ) -> List[TripRunEvent]:
        if run_id not in self.runs:
            raise KeyError(f"TripRun not found: {run_id}")
        return [
            event
            for event in self.events[run_id]
            if event.event_type in {"delivery.ready", "run.terminal"}
            and event.payload.get("bundle_id") == bundle_id
        ]

    async def read_event_window(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        retention_events: int = 500,
    ) -> TripRunEventWindow:
        if run_id not in self.runs:
            raise KeyError(f"TripRun not found: {run_id}")
        after_sequence = max(0, after_sequence)
        limit = max(1, min(limit, 500))
        retention_events = max(1, min(retention_events, 5000))
        all_events = self.events[run_id]
        latest = all_events[-1].sequence if all_events else 0
        first = all_events[0].sequence if all_events else 0
        floor = max(first, latest - retention_events + 1) if latest else 0
        expired = after_sequence > 0 and floor > 0 and after_sequence < floor - 1
        events = [] if expired else [
            event
            for event in all_events
            if event.sequence > after_sequence and event.sequence >= floor
        ][:limit]
        return TripRunEventWindow(
            events=events,
            requested_after_sequence=after_sequence,
            replay_floor_sequence=floor,
            latest_sequence=latest,
            window_expired=expired,
        )

    async def list_runs(
        self,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        status: Optional[str | TripRunStatus] = None,
        mode: Optional[str | TripRunMode] = None,
        limit: int = 50,
    ) -> List[TripRun]:
        status_value = coerce_status(status).value if status else None
        mode_value = coerce_mode(mode).value if mode else None
        runs = list(self.runs.values())
        if user_id:
            runs = [run for run in runs if run.user_id == user_id]
        if session_id:
            runs = [run for run in runs if run.session_id == session_id]
        if status_value:
            runs = [run for run in runs if run.status.value == status_value]
        if mode_value:
            runs = [run for run in runs if run.mode.value == mode_value]
        runs.sort(key=lambda run: run.updated_at, reverse=True)
        return runs[: max(1, min(limit, 200))]

    async def count_runs(
        self,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        status: Optional[str | TripRunStatus] = None,
        mode: Optional[str | TripRunMode] = None,
    ) -> int:
        status_value = coerce_status(status).value if status else None
        mode_value = coerce_mode(mode).value if mode else None
        runs = list(self.runs.values())
        if user_id:
            runs = [run for run in runs if run.user_id == user_id]
        if session_id:
            runs = [run for run in runs if run.session_id == session_id]
        if status_value:
            runs = [run for run in runs if run.status.value == status_value]
        if mode_value:
            runs = [run for run in runs if run.mode.value == mode_value]
        return len(runs)

    async def list_run_titles(self, session_ids: List[str], *, max_len: int = 60) -> Dict[str, str]:
        # The in-memory store has no chat-event backing table; titles come from
        # the SQL path in production.
        return {}

    async def transition_status(
        self,
        run_id: str,
        target_status: str | TripRunStatus,
        *,
        current_node: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        pending_user_choice: Optional[Dict[str, Any]] = None,
        event_type: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        allow_same: bool = True,
        terminal_reason_code: Optional[str] = None,
        terminal_gate_class: Optional[str] = None,
        completion_audit: Optional[Dict[str, Any]] = None,
    ) -> TripRun:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(f"TripRun not found: {run_id}")
        target = coerce_status(target_status)
        assert_status_transition_allowed(run.status, target, allow_same=allow_same)
        if target == TripRunStatus.COMPLETED and run.mode == TripRunMode.DEEP:
            raise ValueError(
                "deep TripRun completion requires complete_delivery() so the Bundle, "
                "delivery.ready, and run.terminal remain one atomic boundary"
            )
        now = utc_now_iso()
        previous = run.status
        attempt_increment = (
            1
            if target == TripRunStatus.RUNNING
            and previous in {TripRunStatus.FAILED, TripRunStatus.INTERRUPTED}
            else 0
        )
        run.status = target
        run.current_node = current_node or run.current_node
        run.last_error_code = error_code
        run.last_error_message = error_message
        run.attempt += attempt_increment
        run.updated_at = now
        if target == TripRunStatus.RUNNING and not run.started_at:
            run.started_at = now
        if target == TripRunStatus.COMPLETED:
            run.completed_at = now
        if target == TripRunStatus.CANCELLED:
            run.cancelled_at = now

        state = self.states[run_id]
        state.status = target
        state.current_node = current_node or state.current_node
        state.pending_user_choice = (
            mark_cancelled_pending_choice(pending_user_choice or state.pending_user_choice)
            if target == TripRunStatus.CANCELLED
            else pending_user_choice
        )
        if state.pending_user_choice or target != TripRunStatus.AWAITING_INPUT:
            state.latest_state_summary = with_pending_choice_summary(
                state.latest_state_summary,
                pending=bool(state.pending_user_choice) and target == TripRunStatus.AWAITING_INPUT,
            )
        state.completion_audit = _completion_audit_for_status(
            completion_audit or state.completion_audit,
            target,
            reason_code=terminal_reason_code,
            gate_class=terminal_gate_class,
        )
        state.last_error = (
            {"code": error_code or "error", "message": error_message or ""}
            if error_code or error_message
            else None
        )
        state.updated_at = now
        terminal_attribution = completion_audit_from_state_summary(
            state.completion_audit
        ).get("terminal_attribution")
        await self.append_event(
            run_id,
            event_type or f"run.{target.value}",
            {
                "from_status": previous.value,
                "to_status": target.value,
                **(payload or {}),
                **({"current_node": current_node} if current_node else {}),
                **({"last_error": state.last_error} if state.last_error else {}),
                **(
                    {"terminal_attribution": terminal_attribution}
                    if terminal_attribution is not None
                    else {}
                ),
            },
        )
        return run

    async def complete_delivery(
        self,
        run_id: str,
        *,
        bundle_id: str,
        manifest: Dict[str, Any],
        current_node: str = "delivery_finalizer",
        completion_audit: Optional[Dict[str, Any]] = None,
    ) -> tuple[TripRun, TripRunEvent, TripRunEvent]:
        manifest_payload = _validated_delivery_manifest(
            run_id=run_id,
            bundle_id=bundle_id,
            manifest=manifest,
        )
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(f"TripRun not found: {run_id}")
        state = self.states.get(run_id)
        if state is None:
            raise KeyError(f"TripRunState not found: {run_id}")
        audit_input = (
            deepcopy(completion_audit)
            if completion_audit is not None
            else deepcopy(state.completion_audit)
        )
        audit = (
            _require_deep_completion_evidence(
                run_id=run_id,
                bundle_id=bundle_id,
                audit_value=audit_input,
            )
            if run.mode == TripRunMode.DEEP
            else completion_audit_from_state_summary(audit_input)
        )
        existing = [
            event
            for event in self.events[run_id]
            if event.event_type in {"delivery.ready", "run.terminal"}
        ]
        if run.status == TripRunStatus.COMPLETED:
            ready_events = [
                event for event in existing if event.event_type == "delivery.ready"
            ]
            terminal_events = [
                event for event in existing if event.event_type == "run.terminal"
            ]
            if (
                len(ready_events) != 1
                or len(terminal_events) != 1
                or ready_events[0].payload.get("bundle_id") != bundle_id
                or terminal_events[0].payload.get("bundle_id") != bundle_id
                or ready_events[0].payload.get("manifest") != manifest_payload
                or terminal_events[0].payload.get("manifest") != manifest_payload
                or ready_events[0].sequence >= terminal_events[0].sequence
            ):
                raise RuntimeError("completed delivery events are missing or duplicated")
            return run, ready_events[0], terminal_events[0]
        assert_status_transition_allowed(
            run.status,
            TripRunStatus.COMPLETED,
            allow_same=False,
        )
        if existing:
            raise RuntimeError("non-completed run already exposes delivery events")

        run_before = deepcopy(run)
        state_before = deepcopy(self.states[run_id])
        events_before = list(self.events[run_id])
        try:
            ready_event = await self.append_event(
                run_id,
                "delivery.ready",
                {
                    "bundle_id": bundle_id,
                    "manifest": manifest_payload,
                    "idempotency_key": f"{run_id}:delivery_ready:{bundle_id}",
                },
            )
            self._inject(DeliveryCompletionBoundary.READY_EVENT)
            previous = run.status
            now = utc_now_iso()
            run.status = TripRunStatus.COMPLETED
            run.current_node = current_node
            run.last_error_code = None
            run.last_error_message = None
            run.completed_at = now
            run.updated_at = now
            state.status = TripRunStatus.COMPLETED
            state.current_node = current_node
            state.pending_user_choice = None
            state.last_error = None
            state.latest_state_summary = with_pending_choice_summary(
                state.latest_state_summary,
                pending=False,
            )
            state.completion_audit = audit
            state.completion_audit = _completion_audit_for_status(
                state.completion_audit,
                TripRunStatus.COMPLETED,
                reason_code="delivery_bundle_ready",
                delivery_bundle_id=bundle_id,
            )
            terminal_attribution = completion_audit_from_state_summary(
                state.completion_audit
            ).get("terminal_attribution")
            state.updated_at = now
            self._inject(DeliveryCompletionBoundary.COMPLETED_STATUS)
            terminal_event = await self.append_event(
                run_id,
                "run.terminal",
                {
                    "from_status": previous.value,
                    "to_status": TripRunStatus.COMPLETED.value,
                    "status": TripRunStatus.COMPLETED.value,
                    "bundle_id": bundle_id,
                    "manifest": manifest_payload,
                    "delivery_event_sequence": ready_event.sequence,
                    "current_node": current_node,
                    "idempotency_key": f"{run_id}:run_terminal:{bundle_id}",
                    **(
                        {"terminal_attribution": terminal_attribution}
                        if terminal_attribution is not None
                        else {}
                    ),
                },
            )
            self._inject(DeliveryCompletionBoundary.TERMINAL_EVENT)
            return run, ready_event, terminal_event
        except Exception:
            self.runs[run_id] = run_before
            self.states[run_id] = state_before
            self.events[run_id] = events_before
            raise

    async def claim_checkpoint_resume(
        self,
        run_id: str,
        *,
        allowed_statuses: Optional[List[str | TripRunStatus]] = None,
        current_node: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[TripRun]:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(f"TripRun not found: {run_id}")
        statuses = {
            coerce_status(status)
            for status in (allowed_statuses or [TripRunStatus.FAILED, TripRunStatus.INTERRUPTED])
        }
        if run.status not in statuses or run.resume_policy != TripRunResumePolicy.CHECKPOINT:
            return None

        previous = run.status
        now = utc_now_iso()
        run.status = TripRunStatus.RUNNING
        run.current_node = current_node or run.current_node
        run.last_error_code = None
        run.last_error_message = None
        run.attempt += 1
        run.updated_at = now
        if not run.started_at:
            run.started_at = now

        state = self.states[run_id]
        state.status = TripRunStatus.RUNNING
        state.current_node = current_node or state.current_node
        state.pending_user_choice = None
        state.latest_state_summary = with_pending_choice_summary(
            state.latest_state_summary,
            pending=False,
        )
        state.completion_audit = _completion_audit_for_status(
            state.completion_audit,
            TripRunStatus.RUNNING,
        )
        state.last_error = None
        state.updated_at = now
        await self.append_event(
            run_id,
            "run.resumed",
            {
                "from_status": previous.value,
                "to_status": TripRunStatus.RUNNING.value,
                "resume_policy": TripRunResumePolicy.CHECKPOINT.value,
                "attempt": run.attempt,
                **(payload or {}),
                **({"current_node": current_node} if current_node else {}),
            },
        )
        return run

    async def list_checkpoint_prune_candidates(
        self,
        *,
        completed_days: int = 30,
        cancelled_days: int = 30,
        failed_interrupted_days: int = 90,
        limit: int = 100,
    ) -> List[TripRun]:
        now = datetime.now(timezone.utc)

        def _older_than(run: TripRun, days: int) -> bool:
            try:
                updated = datetime.fromisoformat(run.updated_at)
            except Exception:
                return False
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            return updated < now - timedelta(days=max(0, days))

        candidates: List[TripRun] = []
        for run in self.runs.values():
            if run.resume_policy != TripRunResumePolicy.CHECKPOINT:
                continue
            if run.status == TripRunStatus.COMPLETED and _older_than(run, completed_days):
                candidates.append(run)
            elif run.status == TripRunStatus.CANCELLED and _older_than(run, cancelled_days):
                candidates.append(run)
            elif run.status in {TripRunStatus.FAILED, TripRunStatus.INTERRUPTED} and _older_than(
                run, failed_interrupted_days
            ):
                candidates.append(run)
        candidates.sort(key=lambda run: run.updated_at)
        return candidates[: max(1, min(limit, 1000))]

    async def record_node_lifecycle(
        self,
        run_id: str,
        *,
        node: str,
        status: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[TripRunEvent]:
        if not node.strip():
            raise ValueError("Node lifecycle requires a node name")
        if status not in {"started", "completed", "retrying", "failed"}:
            raise ValueError(f"Unsupported node lifecycle status: {status}")
        run = self.runs.get(run_id)
        state = self.states.get(run_id)
        if run is None or state is None:
            raise KeyError(f"TripRun not found: {run_id}")
        if is_terminal_status(run.status):
            return None
        if status == "started":
            if run.status != TripRunStatus.RUNNING:
                return None
            now = utc_now_iso()
            run.current_node = node
            run.updated_at = now
            state.current_node = node
            state.updated_at = now
        return await self.append_event(
            run_id,
            f"node.{status}",
            {**(payload or {}), "node": node, "status": status},
        )

    async def record_node_update(
        self,
        run_id: str,
        *,
        node: str,
        trace_event_count: Optional[int] = None,
    ) -> TripRunState:
        state = self.states.get(run_id)
        run = self.runs.get(run_id)
        if state is None or run is None:
            raise KeyError(f"TripRunState not found: {run_id}")
        if node and node not in state.completed_nodes:
            state.completed_nodes.append(node)
        if is_terminal_status(run.status):
            state.pending_user_choice = (
                mark_cancelled_pending_choice(state.pending_user_choice)
                if run.status == TripRunStatus.CANCELLED
                else None
            )
        state.latest_state_summary = with_pending_choice_summary(
            state.latest_state_summary,
            pending=bool(state.pending_user_choice) and not is_terminal_status(run.status),
        )
        if trace_event_count is not None:
            state.trace_event_count = trace_event_count
        state.updated_at = utc_now_iso()
        run.updated_at = state.updated_at
        return state

    async def record_state_snapshot(
        self,
        run_id: str,
        *,
        state_snapshot: Any,
    ) -> TripRunState:
        state = self.states.get(run_id)
        run = self.runs.get(run_id)
        if state is None or run is None:
            raise KeyError(f"TripRunState not found: {run_id}")
        summary = build_trip_run_state_summary(state_snapshot)
        audit = build_trip_run_completion_audit(state_snapshot)
        audit = _preserve_terminal_attribution(state.completion_audit, audit)
        audit = _completion_audit_for_status(audit, state.status)
        state.latest_state_summary = with_pending_choice_summary(
            summary,
            pending=(
                run.status == TripRunStatus.AWAITING_INPUT
                and bool(state.pending_user_choice)
            ),
        )
        state.completion_audit = audit
        state.updated_at = utc_now_iso()
        run.updated_at = state.updated_at
        for event_type, idempotency_key, payload, _milestone in _completion_milestones(
            audit
        ):
            await self.append_event_once(
                run_id,
                event_type,
                payload,
                idempotency_key=idempotency_key,
            )
        return state

    async def append_event(self, run_id: str, event_type: str, payload: Dict[str, Any]) -> TripRunEvent:
        if run_id not in self.runs:
            raise KeyError(f"TripRun not found: {run_id}")
        event = TripRunEvent(
            event_id=len(self.events[run_id]) + 1,
            run_id=run_id,
            sequence=len(self.events[run_id]) + 1,
            event_type=event_type,
            payload=payload,
        )
        self.events[run_id].append(event)
        return event

    async def append_event_once(
        self,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
    ) -> TripRunEvent:
        if not idempotency_key.strip():
            raise ValueError("event idempotency_key is required")
        durable_payload = {**payload, "idempotency_key": idempotency_key}
        for event in self.events.get(run_id, []):
            if event.payload.get("idempotency_key") != idempotency_key:
                continue
            if event.event_type != event_type or event.payload != durable_payload:
                raise ValueError("event idempotency key was used for another payload")
            return event
        return await self.append_event(run_id, event_type, durable_payload)

    async def request_cancel(
        self,
        run_id: str,
        *,
        current_node: Optional[str] = None,
        event_type: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TripRun:
        for _ in range(3):
            run = self.runs.get(run_id)
            if run is None:
                raise KeyError(f"TripRun not found: {run_id}")
            target = (
                TripRunStatus.CANCEL_REQUESTED
                if run.status == TripRunStatus.RUNNING
                else TripRunStatus.CANCELLED
                if run.status in {
                    TripRunStatus.CREATED,
                    TripRunStatus.AWAITING_INPUT,
                    TripRunStatus.CANCEL_REQUESTED,
                }
                else None
            )
            if target is None:
                return run
            try:
                return await self.transition_status(
                    run_id,
                    target,
                    current_node=current_node,
                    event_type=event_type,
                    payload=payload,
                )
            except ValueError:
                continue
        run = self.runs[run_id]
        if run.status in {
            TripRunStatus.CREATED,
            TripRunStatus.RUNNING,
            TripRunStatus.AWAITING_INPUT,
            TripRunStatus.CANCEL_REQUESTED,
        }:
            raise RuntimeError(f"TripRun cancellation did not converge: {run_id}")
        return run


_trip_run_store_singleton: Optional[TripRunStore] = None


def get_trip_run_store() -> TripRunStore:
    global _trip_run_store_singleton
    if _trip_run_store_singleton is None:
        _trip_run_store_singleton = TripRunStore()
    return _trip_run_store_singleton
