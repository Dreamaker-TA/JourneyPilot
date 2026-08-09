"""Pure, restart-safe completion metrics for authorized TripRuns.

The TripRun store persists a compact ``completion_audit`` rather than the full
LangGraph state.  This module intentionally needs only that audit, the
trace-safe TripRun events, and (optionally) immutable Bundle metadata.  It is
therefore suitable for a developer/Eval endpoint as well as an offline audit:
it performs no I/O and never returns candidate prose, provider payloads, or
tool arguments.

Metric conventions
------------------
* An *authorized* run has a durable ``planning_authorized_at`` value.
* An *eligible* run is authorized and has an explicit, valid identity /
  constraint / sealed-Draft / delivery-capability contract. User cancellation,
  a confirmed constraint conflict, and a delivery-integrity failure are not in
  this usable-Bundle denominator.
* A *usable* Bundle is an eligible completed run with a formal non-empty,
  projection-consistent delivery and exactly one ordered ``delivery.ready`` /
  ``run.terminal`` pair for the same Bundle.
* Terminal buckets, lifecycle duplication, and unclassified outcomes use the
  broader authorized-run population so excluded outcomes cannot disappear
  from the completion ledger.
* Quality violations are read only from compact, explicit audit counters. The
  evaluator never guesses from provider error text, a lowered profile, or
  candidate prose when a durable quality summary is absent.
* Zero-tolerance counters are intentionally run-level unless their name says
  otherwise.  ``duplicate_ready_count`` is the number of excess ready events,
  so an accidental third ready event contributes two.
* Optional ``bundle_metadata_by_run`` is only a fallback for an older audit
  missing formal Bundle fields.  It must describe the immutable delivery
  Bundle, not a later refreshed/current revision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from math import ceil
from numbers import Real
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..entities.trip_run import (
    TripRunDetail,
    completion_audit_from_state_summary,
)


_TERMINAL_BUCKET_KEYS = (
    "eligible_completed",
    "user_cancelled",
    "delivery_integrity_failed",
)
_STRICT_ELIGIBILITY_CONTRACT_KEYS = (
    "controlled_identity_valid",
    "constraint_revision_valid",
    "sealed_draft_valid",
    "formal_bundle_capable",
)
_QUALITY_INDICATOR_KEYS = {
    "unsupported_fact_count": "unsupported_external_fact_leak_count",
}
_OVERDUE_UNRESOLVED_STATUSES = {
    "running",
    "cancel_requested",
    "interrupted",
    "awaiting_input",
}


class CompletionCostAggregate(BaseModel):
    """Safe aggregate reconstructed only from terminal cost event summaries."""

    recorded_run_count: int = Field(default=0, ge=0)
    record_failed_run_count: int = Field(default=0, ge=0)
    call_count: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    priced_run_count: int = Field(default=0, ge=0)
    total_cost_usd: Optional[float] = Field(default=None, ge=0)


class CompletionObservationScope(BaseModel):
    """Explicitly state whether the durable input window was complete."""

    is_complete: bool = True
    run_limit: Optional[int] = Field(default=None, ge=1)
    events_per_run_limit: Optional[int] = Field(default=None, ge=0)
    run_population_may_be_truncated: bool = False
    events_may_be_truncated: bool = False


class RunCompletionMetrics(BaseModel):
    """Recomputable run-completion quality metrics with no product payloads."""

    observed_run_count: int = Field(default=0, ge=0)
    authorized_run_count: int = Field(default=0, ge=0)
    eligible_run_count: int = Field(default=0, ge=0)
    usable_bundle_count: int = Field(default=0, ge=0)
    usable_bundle_rate: Optional[float] = Field(default=None, ge=0, le=1)
    content_empty_count: int = Field(default=0, ge=0)
    unsupported_fact_count: int = Field(default=0, ge=0)
    hard_constraint_relaxation_count: int = Field(default=0, ge=0)
    false_completed_count: int = Field(default=0, ge=0)
    projection_drift_count: int = Field(default=0, ge=0)
    duplicate_ready_count: int = Field(default=0, ge=0)
    unclassified_terminal_count: int = Field(default=0, ge=0)
    delivery_ready_timing_sample_count: int = Field(default=0, ge=0)
    delivery_ready_timing_missing_count: int = Field(default=0, ge=0)
    delivery_ready_within_target_count: int = Field(default=0, ge=0)
    delivery_ready_deadline_violation_count: int = Field(default=0, ge=0)
    median_time_to_delivery_ready_seconds: Optional[float] = Field(
        default=None, ge=0
    )
    p90_time_to_delivery_ready_seconds: Optional[float] = Field(
        default=None, ge=0
    )
    terminal_buckets: dict[str, int] = Field(
        default_factory=lambda: {key: 0 for key in _TERMINAL_BUCKET_KEYS}
    )
    source_origin_counts: dict[str, int] = Field(
        default_factory=lambda: {
            "live": 0,
            "provider_snapshot_cache": 0,
            "other": 0,
        }
    )
    cost: CompletionCostAggregate = Field(default_factory=CompletionCostAggregate)
    observation_scope: CompletionObservationScope = Field(
        default_factory=CompletionObservationScope
    )


class CompletionGuaranteeEvaluation(BaseModel):
    """A fail-closed verdict over a complete durable completion-metric export.

    The online developer endpoint intentionally supplies a bounded observation
    window, so callers must not treat its metrics as a formal pass/fail result.
    This pure evaluator is for the B13 protocol/infrastructure matrix and for
    complete offline exports only.
    """

    passed: bool
    violations: list[str] = Field(default_factory=list)


def recompute_completion_metrics(
    details: Iterable[TripRunDetail | Mapping[str, Any]],
    *,
    bundle_metadata_by_run: Optional[Mapping[str, Any]] = None,
    observation_scope: CompletionObservationScope | Mapping[str, Any] | None = None,
) -> RunCompletionMetrics:
    """Return completion metrics from durable TripRun details without I/O.

    ``details`` may be domain objects or JSON-compatible mappings, which keeps
    the evaluator usable after replay/export.  A run without a durable
    authorization observation remains visible in ``observed_run_count`` but is
    deliberately excluded from all completion denominators and fault counts.
    ``authorized_run_count`` is separate from the strict usable-Bundle
    denominator so cancellation, confirmed conflict, and integrity outcomes
    remain visible in the terminal accounting.
    """

    totals = _MetricTotals()
    metadata_by_run = bundle_metadata_by_run or {}
    scope = _coerce_observation_scope(observation_scope)

    for detail in details:
        totals.observed_run_count += 1
        run_id, status, audit, events, latest_observed_at = _detail_parts(detail)
        if not _is_authorized(audit):
            continue

        totals.authorized_run_count += 1
        _accumulate_origins(totals, audit)
        _accumulate_cost(totals, events)

        ready_events = _events_of_type(events, "delivery.ready")
        terminal_events = _events_of_type(events, "run.terminal")
        totals.duplicate_ready_count += max(0, len(ready_events) - 1)

        bucket = _terminal_bucket(status, audit)
        if bucket is not None:
            totals.terminal_buckets[bucket] += 1
        elif _is_unclassified_outcome(
            status,
            audit,
            events,
            latest_observed_at,
        ):
            totals.unclassified_terminal_count += 1

        if not _is_strictly_eligible(status, audit):
            continue

        totals.eligible_run_count += 1
        formal = _formal_delivery(audit, metadata_by_run.get(run_id))

        event_pair_valid = _has_one_ordered_delivery_pair(
            ready_events,
            terminal_events,
            formal.get("bundle_id"),
        )
        report_has_content = bool(formal.get("report_ready")) and bool(
            formal.get("report_content_nonempty")
        )
        projection_consistent = formal.get("projection_consistent") is True
        usable = (
            status == "completed"
            and bool(formal.get("has_bundle"))
            and event_pair_valid
            and report_has_content
            and projection_consistent
        )
        if usable:
            totals.usable_bundle_count += 1

        if status == "completed" and bool(formal.get("has_bundle")):
            if not report_has_content:
                totals.content_empty_count += 1
            if formal.get("projection_consistent") is False:
                totals.projection_drift_count += 1
        if status == "completed" and not usable:
            totals.false_completed_count += 1
        totals.unsupported_fact_count += _quality_indicator_count(
            audit,
            _QUALITY_INDICATOR_KEYS["unsupported_fact_count"],
        )
        if _has_hard_constraint_relaxation(audit):
            totals.hard_constraint_relaxation_count += 1
        _accumulate_timing(totals, audit, ready_events)

    usable_rate = (
        round(totals.usable_bundle_count / totals.eligible_run_count, 4)
        if totals.eligible_run_count
        else None
    )
    total_cost_usd = (
        round(totals.total_cost_usd, 6) if totals.priced_run_count else None
    )
    delivery_samples = sorted(totals.delivery_ready_elapsed_seconds)
    median_delivery_seconds = _quantile(delivery_samples, 0.5)
    p90_delivery_seconds = _quantile(delivery_samples, 0.9)
    return RunCompletionMetrics(
        observed_run_count=totals.observed_run_count,
        authorized_run_count=totals.authorized_run_count,
        eligible_run_count=totals.eligible_run_count,
        usable_bundle_count=totals.usable_bundle_count,
        usable_bundle_rate=usable_rate,
        content_empty_count=totals.content_empty_count,
        unsupported_fact_count=totals.unsupported_fact_count,
        hard_constraint_relaxation_count=totals.hard_constraint_relaxation_count,
        false_completed_count=totals.false_completed_count,
        projection_drift_count=totals.projection_drift_count,
        duplicate_ready_count=totals.duplicate_ready_count,
        unclassified_terminal_count=totals.unclassified_terminal_count,
        delivery_ready_timing_sample_count=len(delivery_samples),
        delivery_ready_timing_missing_count=totals.delivery_ready_timing_missing_count,
        delivery_ready_within_target_count=totals.delivery_ready_within_target_count,
        delivery_ready_deadline_violation_count=(
            totals.delivery_ready_deadline_violation_count
        ),
        median_time_to_delivery_ready_seconds=median_delivery_seconds,
        p90_time_to_delivery_ready_seconds=p90_delivery_seconds,
        terminal_buckets=totals.terminal_buckets,
        source_origin_counts=totals.source_origin_counts,
        cost=CompletionCostAggregate(
            recorded_run_count=totals.recorded_run_count,
            record_failed_run_count=totals.record_failed_run_count,
            call_count=totals.call_count,
            total_tokens=totals.total_tokens,
            priced_run_count=totals.priced_run_count,
            total_cost_usd=total_cost_usd,
        ),
        observation_scope=scope,
    )


# A short alias keeps callers that prefer "calculate" terminology out of the
# API route's business vocabulary while preserving one implementation.
calculate_run_completion_metrics = recompute_completion_metrics


def evaluate_completion_guarantee(
    metrics: RunCompletionMetrics,
) -> CompletionGuaranteeEvaluation:
    """Return whether a complete Eval export meets the agreed hard gates.

    This does not infer facts from provider text or UI timing. Every input is a
    durable audit field or event-derived metric. A missing sample is a failure
    rather than evidence of a pass.
    """

    violations: list[str] = []
    if not metrics.observation_scope.is_complete:
        violations.append("observation_scope_incomplete")
    # A bounded online window can be useful for diagnosis, but it cannot prove
    # a global completion guarantee: a missing run or a truncated event tail
    # could hide exactly the zero-tolerance failure this evaluator is meant to
    # catch.  Keep these independent from ``is_complete`` so callers cannot
    # accidentally mark a partial export complete while retaining either flag.
    if metrics.observation_scope.run_population_may_be_truncated:
        violations.append("run_population_may_be_truncated")
    if metrics.observation_scope.events_may_be_truncated:
        violations.append("events_may_be_truncated")
    if metrics.eligible_run_count <= 0:
        violations.append("no_eligible_runs")
    if metrics.usable_bundle_rate != 1.0:
        violations.append("usable_bundle_rate")
    for field_name in (
        "content_empty_count",
        "unsupported_fact_count",
        "hard_constraint_relaxation_count",
        "false_completed_count",
        "projection_drift_count",
        "duplicate_ready_count",
        "unclassified_terminal_count",
        "delivery_ready_timing_missing_count",
        "delivery_ready_deadline_violation_count",
    ):
        if getattr(metrics, field_name) != 0:
            violations.append(field_name)
    if metrics.delivery_ready_timing_sample_count != metrics.eligible_run_count:
        violations.append("delivery_ready_timing_sample_count")
    if (
        metrics.median_time_to_delivery_ready_seconds is None
        or metrics.median_time_to_delivery_ready_seconds > 5 * 60
    ):
        violations.append("median_time_to_delivery_ready_seconds")
    if (
        metrics.p90_time_to_delivery_ready_seconds is None
        or metrics.p90_time_to_delivery_ready_seconds > 8 * 60
    ):
        violations.append("p90_time_to_delivery_ready_seconds")
    return CompletionGuaranteeEvaluation(
        passed=not violations,
        violations=violations,
    )


def _accumulate_timing(
    totals: "_MetricTotals",
    audit: Mapping[str, Any],
    ready_events: list[Mapping[str, Any]],
) -> None:
    """Record 5/6/8-minute evidence from durable audit times and events only."""

    authorized_at = _parse_datetime(audit.get("planning_authorized_at"))
    deadline = _as_mapping(audit.get("deadline"))
    target_at = _parse_datetime(deadline.get("target_at"))
    delivery_deadline_at = _parse_datetime(deadline.get("delivery_deadline_at"))

    # A formal eligible run must have exactly one durable ready event with a
    # timestamp. A duplicate is counted by the lifecycle metric separately;
    # it is also not valid timing evidence for the single delivery contract.
    if (
        authorized_at is None
        or target_at is None
        or delivery_deadline_at is None
        or len(ready_events) != 1
    ):
        totals.delivery_ready_timing_missing_count += 1
    else:
        ready_at = _parse_datetime(ready_events[0].get("created_at"))
        if ready_at is None or ready_at < authorized_at:
            totals.delivery_ready_timing_missing_count += 1
        else:
            elapsed_seconds = (ready_at - authorized_at).total_seconds()
            totals.delivery_ready_elapsed_seconds.append(elapsed_seconds)
            if ready_at <= target_at:
                totals.delivery_ready_within_target_count += 1
            if ready_at > delivery_deadline_at:
                totals.delivery_ready_deadline_violation_count += 1


def _quantile(values: list[float], quantile: float) -> Optional[float]:
    """Nearest-rank quantile, stable for small formal acceptance samples."""

    if not values:
        return None
    index = max(0, min(len(values) - 1, ceil(len(values) * quantile) - 1))
    return round(values[index], 6)


def _coerce_observation_scope(
    value: CompletionObservationScope | Mapping[str, Any] | None,
) -> CompletionObservationScope:
    if value is None:
        return CompletionObservationScope()
    if isinstance(value, CompletionObservationScope):
        return value
    return CompletionObservationScope.model_validate(value)


class _MetricTotals:
    """Mutable local accumulator; it is never stored or returned."""

    def __init__(self) -> None:
        self.observed_run_count = 0
        self.authorized_run_count = 0
        self.eligible_run_count = 0
        self.usable_bundle_count = 0
        self.content_empty_count = 0
        self.unsupported_fact_count = 0
        self.hard_constraint_relaxation_count = 0
        self.false_completed_count = 0
        self.projection_drift_count = 0
        self.duplicate_ready_count = 0
        self.unclassified_terminal_count = 0
        self.delivery_ready_elapsed_seconds: list[float] = []
        self.delivery_ready_timing_missing_count = 0
        self.delivery_ready_within_target_count = 0
        self.delivery_ready_deadline_violation_count = 0
        self.terminal_buckets = {key: 0 for key in _TERMINAL_BUCKET_KEYS}
        self.source_origin_counts = {
            "live": 0,
            "provider_snapshot_cache": 0,
            "other": 0,
        }
        self.recorded_run_count = 0
        self.record_failed_run_count = 0
        self.call_count = 0
        self.total_tokens = 0
        self.priced_run_count = 0
        self.total_cost_usd = 0.0


def _as_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _detail_parts(
    detail: TripRunDetail | Mapping[str, Any],
) -> tuple[str, str, dict[str, Any], list[dict[str, Any]], Optional[datetime]]:
    raw = _as_mapping(detail)
    run = _as_mapping(raw.get("run"))
    state = _as_mapping(raw.get("state"))
    run_id = str(run.get("run_id") or state.get("run_id") or "")
    status = _enum_text(run.get("status"))
    direct_audit = _as_mapping(state.get("completion_audit"))
    audit = completion_audit_from_state_summary(direct_audit)
    if not audit:
        audit = completion_audit_from_state_summary(
            state.get("latest_state_summary")
        )
    events = [_as_mapping(event) for event in _as_list(raw.get("events"))]
    events = [event for event in events if event]
    return run_id, status, audit, events, _latest_durable_observation(raw, events, audit)


def _enum_text(value: Any) -> str:
    value = getattr(value, "value", value)
    return str(value or "").strip().lower()


def _is_authorized(audit: Mapping[str, Any]) -> bool:
    return bool(str(audit.get("planning_authorized_at") or "").strip())


def _is_strictly_eligible(status: str, audit: Mapping[str, Any]) -> bool:
    """Fail closed unless the durable audit proves every denominator guard."""

    if not _is_authorized(audit):
        return False
    contract = _as_mapping(audit.get("eligibility_contract"))
    if not all(contract.get(key) is True for key in _STRICT_ELIGIBILITY_CONTRACT_KEYS):
        return False
    if _is_user_cancelled(status, audit):
        return False
    return not _is_delivery_integrity_failure(status, audit)


def _is_user_cancelled(status: str, audit: Mapping[str, Any]) -> bool:
    if status in {"cancel_requested", "cancelled"}:
        return True
    contract = _as_mapping(audit.get("eligibility_contract"))
    if contract.get("user_cancelled") is True:
        return True
    attribution = _as_mapping(audit.get("terminal_attribution"))
    return (
        _enum_text(attribution.get("closure_status")) == "cancelled"
        or _enum_text(attribution.get("reason_code")) == "user_cancelled"
    )


def _is_delivery_integrity_failure(status: str, audit: Mapping[str, Any]) -> bool:
    attribution = _as_mapping(audit.get("terminal_attribution"))
    reason_code = _enum_text(attribution.get("reason_code"))
    return status == "failed" and reason_code.startswith("delivery_integrity")


def _formal_delivery(
    audit: Mapping[str, Any],
    metadata: Any,
) -> dict[str, Any]:
    """Read formal delivery fields, falling back only where audit omits them."""

    formal = _as_mapping(audit.get("formal_delivery"))
    bundle = _as_mapping(metadata)
    manifest = _as_mapping(bundle.get("manifest"))
    report = _as_mapping(bundle.get("report_projection"))
    document = _as_mapping(report.get("document"))

    result = dict(formal)
    if not result.get("bundle_id"):
        result["bundle_id"] = manifest.get("bundle_id") or bundle.get("bundle_id")
    if "has_bundle" not in result:
        result["has_bundle"] = bool(manifest or bundle.get("bundle_id"))
    if "report_ready" not in result and report:
        result["report_ready"] = report.get("status") == "ready"
    if "report_content_nonempty" not in result and report:
        result["report_content_nonempty"] = _document_has_content(document)
    return result


def _document_has_content(document: Mapping[str, Any]) -> bool:
    return bool(
        document.get("days")
        or document.get("sections")
        or document.get("summary")
        or document.get("title")
    )


def _events_of_type(events: Iterable[Mapping[str, Any]], event_type: str) -> list[dict[str, Any]]:
    matched = [event for event in events if event.get("event_type") == event_type]
    return sorted(matched, key=lambda event: _sequence(event.get("sequence")))


def _sequence(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    return _as_mapping(event.get("payload"))


def _has_one_ordered_delivery_pair(
    ready_events: list[Mapping[str, Any]],
    terminal_events: list[Mapping[str, Any]],
    bundle_id: Any,
) -> bool:
    if not bundle_id or len(ready_events) != 1 or len(terminal_events) != 1:
        return False
    expected_bundle_id = str(bundle_id)
    ready = ready_events[0]
    terminal = terminal_events[0]
    if _event_payload(ready).get("bundle_id") != expected_bundle_id:
        return False
    if _event_payload(terminal).get("bundle_id") != expected_bundle_id:
        return False
    return _sequence(ready.get("sequence")) < _sequence(terminal.get("sequence"))


def _quality_indicator_count(audit: Mapping[str, Any], indicator: str) -> int:
    """Read an explicit compact counter, never prose or a derived hunch."""

    value = _as_mapping(audit.get("quality_indicators")).get(indicator)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _has_hard_constraint_relaxation(audit: Mapping[str, Any]) -> bool:
    contract = _as_mapping(audit.get("constraint_contract"))
    expected = {
        str(value)
        for value in _as_list(contract.get("expected_ids"))
        if str(value).strip()
    }
    actual = {
        str(value)
        for value in _as_list(contract.get("workspace_ids"))
        if str(value).strip()
    }
    return bool(expected and not expected.issubset(actual))


def _is_unclassified_outcome(
    status: str,
    audit: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    latest_observed_at: Optional[datetime],
) -> bool:
    """Identify every authorized outcome that missed the four allowed buckets.

    A non-delivery terminal is already an invalid outcome. Restartable states
    are only counted after the durable deadline has been observed, which keeps
    a normal crash/resume window from being misreported as a terminal failure.
    """

    if status in {"completed", "failed", "cancelled"}:
        return True
    if status not in _OVERDUE_UNRESOLVED_STATUSES:
        return False
    return _deadline_is_exhausted(audit, events, latest_observed_at)


def _deadline_is_exhausted(
    audit: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    latest_observed_at: Optional[datetime],
) -> bool:
    deadline = _as_mapping(audit.get("deadline"))
    if deadline.get("delivery_deadline_exhausted") is True:
        return True
    if any(
        str(event.get("event_type") or "") == "run.delivery_deadline_exhausted"
        for event in events
    ):
        return True
    deadline_at = _parse_datetime(deadline.get("delivery_deadline_at"))
    return bool(
        deadline_at is not None
        and latest_observed_at is not None
        and latest_observed_at >= deadline_at
    )


def _latest_durable_observation(
    raw: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    audit: Mapping[str, Any],
) -> Optional[datetime]:
    run = _as_mapping(raw.get("run"))
    state = _as_mapping(raw.get("state"))
    deadline = _as_mapping(audit.get("deadline"))
    terminal = _as_mapping(audit.get("terminal_attribution"))
    candidates = [
        _parse_datetime(deadline.get("last_observed_at")),
        _parse_datetime(run.get("updated_at")),
        _parse_datetime(run.get("completed_at")),
        _parse_datetime(run.get("cancelled_at")),
        _parse_datetime(state.get("updated_at")),
        _parse_datetime(terminal.get("recorded_at")),
        *(_parse_datetime(event.get("created_at")) for event in events),
    ]
    known = [value for value in candidates if value is not None]
    return max(known) if known else None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _terminal_bucket(status: str, audit: Mapping[str, Any]) -> Optional[str]:
    attribution = _as_mapping(audit.get("terminal_attribution"))
    terminal_status = _enum_text(attribution.get("closure_status"))
    reason_code = _enum_text(attribution.get("reason_code"))
    if (
        status == "completed"
        and terminal_status == "completed"
        and reason_code == "delivery_bundle_ready"
        and _is_strictly_eligible(status, audit)
    ):
        return "eligible_completed"
    if (
        status == "cancelled"
        and terminal_status == "cancelled"
        and reason_code == "user_cancelled"
    ):
        return "user_cancelled"
    if (
        status == "failed"
        and terminal_status == "failed"
        and reason_code.startswith("delivery_integrity")
    ):
        return "delivery_integrity_failed"
    return None


def _accumulate_origins(totals: _MetricTotals, audit: Mapping[str, Any]) -> None:
    origins = _as_mapping(audit.get("source_origin_counts"))
    for origin, raw_count in origins.items():
        key = str(origin)
        if key not in totals.source_origin_counts:
            key = "other"
        totals.source_origin_counts[key] += _nonnegative_int(raw_count)


def _accumulate_cost(totals: _MetricTotals, events: Iterable[Mapping[str, Any]]) -> None:
    # The terminal event is idempotent per run.  If an old replay nevertheless
    # contains multiple cost events, count the latest one once rather than
    # fabricating duplicated spend.
    recorded = _events_of_type(events, "run.cost_recorded")
    failed = _events_of_type(events, "run.cost_record_failed")
    if recorded:
        payload = _event_payload(recorded[-1])
        totals.recorded_run_count += 1
        totals.call_count += _nonnegative_int(payload.get("call_count"))
        totals.total_tokens += _nonnegative_int(payload.get("total_tokens"))
        cost = _nonnegative_float_or_none(payload.get("total_cost_usd"))
        if cost is not None:
            totals.priced_run_count += 1
            totals.total_cost_usd += cost
    if failed:
        totals.record_failed_run_count += 1


def _nonnegative_int(value: Any) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(result, 0)


def _nonnegative_float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    result = float(value)
    return result if result >= 0 else None
