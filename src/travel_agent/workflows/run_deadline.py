"""Durable 5/6/7/8 minute planning-deadline primitives.

The persisted :class:`RunDeadlineSnapshot` is the cross-process source of
truth.  Monotonic readings are used while a process is alive, while the UTC
authorization instant prevents a restart or SSE reconnect from granting a
fresh eight-minute window.

Minute six is the research boundary: past it the dispatcher and every gate stop
opening research calls and hand the admitted catalog to composition.  Minute
seven is the composition boundary: the itinerary composition that turns that
catalog into the delivered itinerary owns minutes six through seven, so a run
whose research spent the whole research window still gets one composition pass.
Minutes seven through eight are the projection and persistence budget.
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Literal, Optional, Tuple

from ..entities.delivery_bundle import MinimumDeliveryDraft, RunDeadlineSnapshot
from ..entities.run_deadline_policy import (
    RUN_DEADLINE_POLICY_VERSION,
    current_closeout_seconds,
    current_composition_seconds,
    current_delivery_deadline_seconds,
    current_target_seconds,
)

# 只导出策略版本与观察原语。**没有 import 期常量**：一份在 import 时定格的窗口值
# 会在配置改动之后继续被相信，而它读起来和当前配置一模一样。
__all__ = [
    "RUN_DEADLINE_POLICY_VERSION",
    "DeadlineObservation",
    "DeadlinePhase",
    "build_run_deadline_snapshot",
    "clear_process_deadline_anchor",
    "observe_run_deadline",
    "utc_now",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("run deadline timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def build_run_deadline_snapshot(
    draft: MinimumDeliveryDraft,
    *,
    authorized_at: Optional[datetime] = None,
    policy_version: str = RUN_DEADLINE_POLICY_VERSION,
) -> RunDeadlineSnapshot:
    """Bind one sealed Draft to immutable deadline audit points."""
    if not draft.planning_authorized or draft.planning_authorized_at is None:
        raise ValueError("run deadline requires a sealed minimum delivery draft")
    authorization = _as_utc(authorized_at or draft.planning_authorized_at)
    if authorization != _as_utc(draft.planning_authorized_at):
        raise ValueError("run deadline authorization time must match the sealed draft")
    target_seconds = current_target_seconds()
    closeout_seconds = current_closeout_seconds()
    composition_seconds = current_composition_seconds()
    delivery_deadline_seconds = current_delivery_deadline_seconds()
    return RunDeadlineSnapshot(
        draft_id=draft.draft_id,
        policy_version=policy_version,
        planning_authorized_at=authorization,
        target_at=authorization + timedelta(seconds=target_seconds),
        closeout_at=authorization + timedelta(seconds=closeout_seconds),
        composition_at=authorization + timedelta(seconds=composition_seconds),
        delivery_deadline_at=authorization + timedelta(seconds=delivery_deadline_seconds),
        target_seconds=target_seconds,
        closeout_seconds=closeout_seconds,
        composition_seconds=composition_seconds,
        delivery_deadline_seconds=delivery_deadline_seconds,
        checkpointed_elapsed_seconds=0.0,
        last_observed_at=authorization,
    )


DeadlinePhase = Literal[
    "research", "target_missed", "closeout", "composition_closed", "expired"
]


@dataclass(frozen=True)
class DeadlineObservation:
    """One reading of the run clock.

    ``research`` and ``target_missed`` are the windows in which a research call
    may still start.  ``closeout`` closes research while itinerary composition
    stays open; ``composition_closed`` closes that too and leaves only
    projection and persistence; ``expired`` means the delivery budget is gone.

    Read the two predicates rather than comparing phases: they name the question
    each call site is actually asking.
    """

    elapsed_seconds: float
    remaining_seconds: float
    phase: DeadlinePhase

    @property
    def research_closed(self) -> bool:
        """No worker, gate repair or provider sweep may open another call."""
        return self.phase in {"closeout", "composition_closed", "expired"}

    @property
    def composition_closed(self) -> bool:
        """No itinerary composition call may start; only projection remains."""
        return self.phase in {"composition_closed", "expired"}


# Each async execution context gets its own process-local monotonic anchors.
# They are intentionally not persisted.  On a restart an anchor is restored
# from the durable elapsed/wall-clock baseline rather than reset to zero.
_process_monotonic_anchors: contextvars.ContextVar[Dict[str, float]] = contextvars.ContextVar(
    "process_monotonic_deadline_anchors",
    default={},
)


def clear_process_deadline_anchor(draft_id: Optional[str] = None) -> None:
    anchors = dict(_process_monotonic_anchors.get())
    if draft_id is None:
        anchors.clear()
    else:
        anchors.pop(draft_id, None)
    _process_monotonic_anchors.set(anchors)


def observe_run_deadline(
    snapshot: RunDeadlineSnapshot,
    *,
    now: Optional[datetime] = None,
    monotonic_now: Optional[float] = None,
    process_elapsed_seconds: Optional[float] = None,
) -> Tuple[RunDeadlineSnapshot, DeadlineObservation]:
    """Advance, but never reset or shorten, a persisted deadline observation.

    Phase boundaries use the seconds embedded on ``snapshot``, so a later change
    to the ``run_deadline`` config cannot reclassify an older run.
    """
    observed_at = _as_utc(now or utc_now())
    authorization = _as_utc(snapshot.planning_authorized_at)
    wall_elapsed = max(0.0, (observed_at - authorization).total_seconds())
    local_now = time.monotonic() if monotonic_now is None else monotonic_now

    if process_elapsed_seconds is None:
        anchors = dict(_process_monotonic_anchors.get())
        anchor = anchors.get(snapshot.draft_id)
        if anchor is None:
            baseline = max(snapshot.checkpointed_elapsed_seconds, wall_elapsed)
            anchor = local_now - baseline
            anchors[snapshot.draft_id] = anchor
            _process_monotonic_anchors.set(anchors)
        process_elapsed = max(0.0, local_now - anchor)
    else:
        process_elapsed = max(0.0, process_elapsed_seconds)

    elapsed = max(snapshot.checkpointed_elapsed_seconds, wall_elapsed, process_elapsed)
    last_observed = max(_as_utc(snapshot.last_observed_at), observed_at)
    updated = snapshot.model_copy(
        update={
            "checkpointed_elapsed_seconds": round(elapsed, 6),
            "last_observed_at": last_observed,
        }
    )
    if elapsed >= snapshot.delivery_deadline_seconds:
        phase: DeadlinePhase = "expired"
    elif elapsed >= snapshot.composition_seconds:
        phase = "composition_closed"
    elif elapsed >= snapshot.closeout_seconds:
        phase = "closeout"
    elif elapsed >= snapshot.target_seconds:
        phase = "target_missed"
    else:
        phase = "research"
    return updated, DeadlineObservation(
        elapsed_seconds=elapsed,
        remaining_seconds=max(0.0, snapshot.delivery_deadline_seconds - elapsed),
        phase=phase,
    )
