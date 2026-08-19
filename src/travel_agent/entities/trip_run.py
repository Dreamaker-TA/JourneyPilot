"""TripOps durable run domain models.

TripRun is the business lifecycle record for a JourneyPilot trip task. It is
separate from WorkflowTraceEvent observability and intentionally stores only
trace-safe state projections.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional

from pydantic import BaseModel, Field

from ..local_profile import LOCAL_USER_ID

from ..infrastructure.row_values import iso_or_none as _iso_or_none
from .provider_evidence import (
    ProviderEvidenceOutcome,
    build_required_long_distance_legs,
    current_provider_evidence_summary,
    missing_long_distance_leg_roles,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_trip_run_id() -> str:
    return f"trip_{uuid.uuid4().hex[:16]}"


def generate_run_command_id() -> str:
    return f"cmd_{uuid.uuid4().hex[:16]}"


class TripRunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class TripRunMode(str, Enum):
    DEEP = "deep"
    FAST = "fast"


class TripRunResumePolicy(str, Enum):
    CLARIFY_ONLY = "clarify_only"
    RESTART_FROM_LATEST_INPUT = "restart_from_latest_input"
    CHECKPOINT = "checkpoint"


class RunRecoveryStatus(str, Enum):
    """执行归属的生命周期。与 TripRunStatus 正交：前者说「谁在跑」，后者说「跑到哪」。"""

    IDLE = "idle"
    CLAIMED = "claimed"
    RUNNING = "running"
    #: 进程收到关闭信号后主动放弃租约，交给下一次启动 census 判定。
    SHUTDOWN_REQUESTED = "shutdown_requested"
    RESUME_AVAILABLE = "resume_available"
    NON_RESUMABLE = "non_resumable"
    RELEASED = "released"
    #: durable 事实自相矛盾，恢复不做猜测，只留诊断。
    RECOVERY_CONTRACT_FAILURE = "recovery_contract_failure"


class RunExecution(BaseModel):
    """一个 TripRun 的执行租约与恢复判定。租约时钟以数据库 NOW() 为准。"""

    run_id: str
    executor_id: Optional[str] = None
    lease_token: Optional[str] = None
    lease_acquired_at: Optional[str] = None
    lease_expires_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    process_started_at: Optional[str] = None
    last_safe_checkpoint_id: Optional[str] = None
    recovery_status: RunRecoveryStatus = RunRecoveryStatus.IDLE
    recovery_reason: Optional[str] = None
    updated_at: str = Field(default_factory=utc_now_iso)


class RunCommandType(str, Enum):
    """可以对一个运行中 Run 下达的动作。

    只有产品面已经存在的两个。`pause` 之类没有入口的动作不提前实现 —— 一个没有消费者
    的命令类型只会让状态机多一条永远 pending 的分支。
    """

    CANCEL = "cancel"
    SUPPLEMENT = "supplement"


class RunCommandStatus(str, Enum):
    PENDING = "pending"
    #: 执行器已取走，还没落到效果上。进程此刻死掉，恢复扫描按未生效处理。
    CLAIMED = "claimed"
    CONSUMED = "consumed"
    #: 明确不再生效（节点已越过适用阶段、Run 先结束）。**不是永远 pending。**
    REJECTED = "rejected"


#: 还没有结论的命令。执行器只 claim 这两种状态里的第一种。
OPEN_RUN_COMMAND_STATUSES: set[RunCommandStatus] = {
    RunCommandStatus.PENDING,
    RunCommandStatus.CLAIMED,
}


class RunCommand(BaseModel):
    """一条持久化的运行控制命令。**最终事实在 `trip_run_commands`**。

    进程内 registry 只负责唤醒执行器去读这张表，不再是命令存不存在的判据。
    """

    command_id: str
    run_id: str
    command_type: RunCommandType
    payload: Dict[str, Any] = Field(default_factory=dict)
    request_digest: str
    status: RunCommandStatus = RunCommandStatus.PENDING
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    consumed_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_RUN_COMMAND_STATUSES


def coerce_run_command_type(value: str | RunCommandType) -> RunCommandType:
    return value if isinstance(value, RunCommandType) else RunCommandType(str(value))


def coerce_run_command_status(value: str | RunCommandStatus) -> RunCommandStatus:
    return value if isinstance(value, RunCommandStatus) else RunCommandStatus(str(value))


def run_command_digest(
    command_type: str | RunCommandType,
    payload: Mapping[str, Any],
) -> str:
    """同一个意图的重复请求必须落在同一行上。

    `cancel` 的摘要只取类型：「停下这个 Run」是一个意图，点两次不是两件事，重发只该
    拿回同一张回执。`supplement` 的摘要取类别与正文：同一句要求说两遍仍然是一条要求，
    而一次重试的 POST 不该在调研提示里出现两遍。
    """

    kind = coerce_run_command_type(command_type)
    if kind is RunCommandType.CANCEL:
        material = kind.value
    else:
        material = json.dumps(
            {
                "type": kind.value,
                "category": str(payload.get("category") or ""),
                "content": str(payload.get("content") or ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


TERMINAL_TRIP_RUN_STATUSES: set[TripRunStatus] = {
    TripRunStatus.COMPLETED,
    TripRunStatus.FAILED,
    TripRunStatus.INTERRUPTED,
    TripRunStatus.CANCELLED,
}


ALLOWED_STATUS_TRANSITIONS: Dict[TripRunStatus, set[TripRunStatus]] = {
    # CREATED → FAILED: early fail-closed before workers start (e.g. InputGuard safety_block).
    TripRunStatus.CREATED: {
        TripRunStatus.RUNNING,
        TripRunStatus.CANCELLED,
        TripRunStatus.FAILED,
    },
    TripRunStatus.RUNNING: {
        TripRunStatus.AWAITING_INPUT,
        TripRunStatus.COMPLETED,
        TripRunStatus.FAILED,
        TripRunStatus.INTERRUPTED,
        TripRunStatus.CANCEL_REQUESTED,
    },
    # Waiting for the traveller: resume, fail hard, or cancel immediately.
    # Cancellation does not pass through CANCEL_REQUESTED (product: direct cancel).
    TripRunStatus.AWAITING_INPUT: {
        TripRunStatus.RUNNING,
        TripRunStatus.FAILED,
        TripRunStatus.CANCELLED,
    },
    TripRunStatus.FAILED: {TripRunStatus.RUNNING},
    TripRunStatus.INTERRUPTED: {TripRunStatus.RUNNING},
    TripRunStatus.CANCEL_REQUESTED: {TripRunStatus.CANCELLED},
    TripRunStatus.COMPLETED: set(),
    TripRunStatus.CANCELLED: set(),
}


def coerce_status(value: str | TripRunStatus) -> TripRunStatus:
    return value if isinstance(value, TripRunStatus) else TripRunStatus(str(value))


def coerce_mode(value: str | TripRunMode) -> TripRunMode:
    return value if isinstance(value, TripRunMode) else TripRunMode(str(value))


def coerce_resume_policy(value: str | TripRunResumePolicy) -> TripRunResumePolicy:
    return value if isinstance(value, TripRunResumePolicy) else TripRunResumePolicy(str(value))


def is_status_transition_allowed(
    current: str | TripRunStatus,
    target: str | TripRunStatus,
    *,
    allow_same: bool = True,
) -> bool:
    current_status = coerce_status(current)
    target_status = coerce_status(target)
    if allow_same and current_status == target_status:
        return True
    return target_status in ALLOWED_STATUS_TRANSITIONS[current_status]


def assert_status_transition_allowed(
    current: str | TripRunStatus,
    target: str | TripRunStatus,
    *,
    allow_same: bool = True,
) -> None:
    if not is_status_transition_allowed(current, target, allow_same=allow_same):
        raise ValueError(f"invalid TripRun status transition: {current} -> {target}")


def is_terminal_status(value: str | TripRunStatus) -> bool:
    return coerce_status(value) in TERMINAL_TRIP_RUN_STATUSES


def coerce_recovery_status(value: str | RunRecoveryStatus) -> RunRecoveryStatus:
    return value if isinstance(value, RunRecoveryStatus) else RunRecoveryStatus(str(value))


#: 可取消的状态。CANCEL_REQUESTED 仍在表里：取消请求可以重发，执行器可能已经消失。
_CANCELLABLE_STATUSES = {
    TripRunStatus.CREATED,
    TripRunStatus.RUNNING,
    TripRunStatus.AWAITING_INPUT,
    TripRunStatus.CANCEL_REQUESTED,
}

_RESUMABLE_STATUSES = {
    TripRunStatus.CREATED,
    TripRunStatus.AWAITING_INPUT,
    TripRunStatus.FAILED,
    TripRunStatus.INTERRUPTED,
}


def is_cancellable(status: TripRunStatus | str) -> bool:
    """这个状态还能不能被取消。**表只有一张**：路由的前置判断与
    `available_run_actions` 通报的动作必须读同一份，否则界面亮着的按钮点下去拿 409。
    """

    return coerce_status(status) in _CANCELLABLE_STATUSES


def available_run_actions(
    run: "TripRun",
    execution: Optional["RunExecution"] = None,
) -> List[str]:
    """这个 Run 现在真正能做的动作。

    服务端说一次，客户端不从十几个字段自己推 —— 两处推导必然分叉，而分叉的表现是
    一个亮着的「继续」按钮点下去拿 409。
    """

    actions: List[str] = []
    resumable = run.status in _RESUMABLE_STATUSES and (
        run.resume_policy == TripRunResumePolicy.CHECKPOINT
        or run.status == TripRunStatus.CREATED
    )
    if execution is not None and execution.recovery_status in {
        RunRecoveryStatus.NON_RESUMABLE,
        RunRecoveryStatus.RECOVERY_CONTRACT_FAILURE,
    }:
        resumable = False
    if resumable:
        actions.append("resume")
    if run.status in _CANCELLABLE_STATUSES:
        actions.append("cancel")
    return actions


class TripRun(BaseModel):
    run_id: str
    session_id: str = ""
    user_id: str = LOCAL_USER_ID
    mode: TripRunMode = TripRunMode.DEEP
    status: TripRunStatus = TripRunStatus.CREATED
    request_message_id: str = ""
    assistant_message_id: str = ""
    parent_run_id: Optional[str] = None
    current_node: Optional[str] = None
    resume_token_hash: Optional[str] = None
    resume_policy: TripRunResumePolicy = TripRunResumePolicy.CLARIFY_ONLY
    # Immutable product input captured when this run is created. It is kept on
    # the run record rather than in the audit-safe state summary.
    controlled_trip_identity: Optional[Dict[str, Any]] = None
    last_error_code: Optional[str] = None
    last_error_message: Optional[str] = None
    attempt: int = 1
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    cancelled_at: Optional[str] = None


class TripRunState(BaseModel):
    run_id: str
    status: TripRunStatus = TripRunStatus.CREATED
    current_node: Optional[str] = None
    completed_nodes: List[str] = Field(default_factory=list)
    latest_state_summary: Dict[str, Any] = Field(default_factory=dict)
    # Developer/Eval-only durable completion projection.  Kept separate from
    # the normal state summary so ordinary API consumers cannot accidentally
    # treat deadline or provider-origin diagnostics as product data.
    completion_audit: Dict[str, Any] = Field(default_factory=dict)
    pending_user_choice: Optional[Dict[str, Any]] = None
    trace_event_count: int = 0
    pending_monitor_trigger_count: int = 0
    last_error: Optional[Dict[str, Any]] = None
    updated_at: str = Field(default_factory=utc_now_iso)


class TripRunEvent(BaseModel):
    event_id: Optional[int] = None
    run_id: str
    sequence: int
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


class TripRunDetail(BaseModel):
    run: TripRun
    state: TripRunState
    events: List[TripRunEvent] = Field(default_factory=list)


# Older state summaries may contain an embedded audit. New durable state keeps
# that audit in its own JSONB column so normal user-facing summaries remain
# intentionally small; API routes expose the audit only through developer/Eval
# surfaces.
COMPLETION_AUDIT_SUMMARY_KEY = "completion_audit"


def _as_mapping(value: Any) -> Dict[str, Any]:
    """Return a JSON-safe mapping from a Pydantic object or mapping."""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return dict(value) if isinstance(value, dict) else {}


def _as_mappings(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    values: List[Dict[str, Any]] = []
    for item in value:
        mapped = _as_mapping(item)
        if mapped:
            values.append(mapped)
    return values




def _unique_strings(values: Iterable[Any]) -> List[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value).strip()))


def _as_int(value: Any, *, default: int = -1) -> int:
    """Read a non-boolean integer without turning malformed state into truth."""

    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _controlled_identity_is_valid(value: Any) -> bool:
    """Check the compact identity shape without retaining its user-facing text.

    The audit needs a durable yes/no eligibility observation, not a duplicate of
    the complete controlled identity.  The workflow has already validated the
    rich Pydantic contract before this point; this check merely prevents a
    malformed checkpoint summary from being treated as eligible on replay.
    """

    identity = _as_mapping(value)
    origin = _as_mapping(identity.get("origin"))
    destinations = identity.get("destinations")
    party = _as_mapping(identity.get("party"))
    style = _as_mapping(identity.get("style"))
    return bool(
        origin.get("place_id")
        and isinstance(destinations, list)
        and destinations
        and all(_as_mapping(item).get("place_id") for item in destinations)
        and identity.get("start_date")
        and identity.get("end_date")
        and party
        and style
    )


def _delivery_is_intact(data: Mapping[str, Any]) -> bool:
    """Whether the persisted state still has the core ability to form a Bundle."""

    return not _as_mapping(data.get("delivery_failure"))


def _unsupported_external_fact_leak_count(
    *,
    facts: Mapping[str, Any],
    report: Mapping[str, Any],
) -> int:
    """Count only objectively unsupported projected fact ids.

    This intentionally does not classify a stale but source-linked fact as
    unsupported.  It catches the zero-tolerance case: a report citation names
    a fact that is absent from the final fact snapshot or has no live external
    supporting source record.
    """

    fact_index = {
        str(item.get("fact_assertion_id")): item
        for item in _as_mappings(facts.get("fact_assertions"))
        if item.get("fact_assertion_id")
    }
    source_ids = {
        str(item.get("source_record_id"))
        for item in _as_mappings(facts.get("source_records"))
        if item.get("source_record_id")
    }
    leaked: set[str] = set()
    for citation in _as_mappings(report.get("citations")):
        for fact_id in _unique_strings(citation.get("fact_assertion_ids") or []):
            fact = fact_index.get(fact_id)
            if not fact:
                leaked.add(fact_id)
                continue
            supporting_source_ids = {
                str(link.get("source_record_id"))
                for link in _as_mappings(fact.get("source_links"))
                if link.get("relation") == "supports" and link.get("source_record_id")
            }
            if not supporting_source_ids or not supporting_source_ids <= source_ids:
                leaked.add(fact_id)
    return len(leaked)


def _as_date(value: Any) -> Optional[date]:
    """Read a calendar date from the JSON projection without ever raising."""

    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _long_distance_leg_ledger(workspace: Mapping[str, Any]) -> tuple[int, int]:
    """Count the round trip's long-distance legs owed, and how many are missing.

    The Workspace's controlled-identity anchor is the same authority the report
    reads in ``_long_distance_path_notes``, and delivery is counted off the Day
    timelines, so a leg the composer never placed on a Day is never counted as
    delivered.  Both counts are needed: a lone missing count would read a Run
    that owed no round trip and a Run that lost both of its legs as the same 0.
    When the shared judgement declines to speak — no responsibility, or Days
    that do not cover a required service date — both stay 0, because the audit
    must not assert a gap the reader-facing note is unwilling to assert.
    """

    identity: Optional[Mapping[str, Any]] = None
    for anchor in _as_mappings(workspace.get("user_input_anchors")):
        if (
            anchor.get("input_kind") == "controlled_identity"
            and anchor.get("field_path") == "controlled_trip_identity"
            and isinstance(anchor.get("value"), Mapping)
        ):
            identity = anchor["value"]
            break
    if identity is None:
        return 0, 0

    required = build_required_long_distance_legs(
        identity, cross_day_return_required=False
    )
    itinerary = _as_mapping(workspace.get("itinerary"))
    long_distance_leg_ids = {
        str(leg.get("transport_leg_id"))
        for leg in _as_mappings(itinerary.get("transport_legs"))
        if leg.get("transport_class") == "long_distance"
        and leg.get("transport_leg_id")
    }
    day_dates: set[date] = set()
    dates_with_long_distance: set[date] = set()
    for day in _as_mappings(itinerary.get("day_plans")):
        day_date = _as_date(day.get("date"))
        if day_date is None:
            continue
        day_dates.add(day_date)
        if any(
            entry.get("entity_type") == "transport_leg"
            and str(entry.get("entity_id")) in long_distance_leg_ids
            for entry in _as_mappings(day.get("timeline"))
        ):
            dates_with_long_distance.add(day_date)

    missing = missing_long_distance_leg_roles(
        required,
        day_dates=day_dates,
        dates_with_long_distance=dates_with_long_distance,
    )
    if missing is None:
        return 0, 0
    return len(required), len(missing)


def _quality_indicators(
    *,
    data: Mapping[str, Any],
    facts: Mapping[str, Any],
    report: Mapping[str, Any],
) -> Dict[str, int]:
    """Persist only countable, non-prose quality evidence for Eval metrics."""

    catalog = _as_mapping(data.get("recommendation_catalog"))
    workspace = _as_mapping(data.get("trip_workspace_v2"))
    if not catalog:
        catalog = _as_mapping(workspace.get("recommendation_catalog"))
    candidates = {
        str(candidate.get("candidate_id")): candidate
        for packet in _as_mappings(catalog.get("research_packets"))
        for candidate in _as_mappings(packet.get("candidates"))
        if candidate.get("candidate_id")
    }
    provider_candidate_ids: set[str] = set()
    for packet in _as_mappings(catalog.get("research_packets")):
        provider_source_ids = {
            str(source.get("source_record_id"))
            for source in _as_mappings(packet.get("source_records"))
            if source.get("source_record_id") and source.get("tool_audit_id")
        }
        for candidate in _as_mappings(packet.get("candidates")):
            candidate_id = str(candidate.get("candidate_id") or "")
            if candidate_id and provider_source_ids.intersection(
                _unique_strings(candidate.get("source_record_ids") or [])
            ):
                provider_candidate_ids.add(candidate_id)
    passed_candidate_ids = {
        str(admission.get("candidate_id"))
        for admission in _as_mappings(catalog.get("admission_results"))
        if admission.get("status") == "passed" and admission.get("candidate_id")
    }

    current_constraint_revision = _as_int(data.get("constraint_pack_revision"))
    provider_outcomes = {
        str(scope_id): ProviderEvidenceOutcome.model_validate(outcome)
        for scope_id, outcome in _as_mapping(
            data.get("provider_evidence_outcomes")
        ).items()
    }
    provider_summary = current_provider_evidence_summary(
        provider_outcomes,
        constraint_pack_revision=current_constraint_revision,
    )
    constraint_pack = _as_mapping(data.get("constraint_pack"))
    pack_meta = _as_mapping(constraint_pack.get("pack_meta"))
    required_leg_count, missing_leg_count = _long_distance_leg_ledger(workspace)

    return {
        "unsupported_external_fact_leak_count": _unsupported_external_fact_leak_count(
            facts=facts,
            report=report,
        ),
        # This safe count is useful when reading the audit manually and lets
        # future evaluators distinguish absent catalog evidence from an actual
        # zero without persisting Candidate names or tool payloads.
        "passed_candidate_count": len(
            [candidate_id for candidate_id in passed_candidate_ids if candidate_id in candidates]
        ),
        "provider_evidence_current_scope_count": (
            provider_summary.current_scope_count
        ),
        "provider_evidence_unresolved_scope_count": (
            provider_summary.unresolved_scope_count
        ),
        "provider_option_count": provider_summary.provider_option_count,
        "provider_option_materialized_count": (
            provider_summary.provider_option_materialized_count
        ),
        "provider_option_admissible_count": len(
            passed_candidate_ids.intersection(provider_candidate_ids)
        ),
        "provider_salvage_loss_count": (
            provider_summary.provider_salvage_loss_count
        ),
        # An unattempted leg leaves no Provider scope behind, so the unresolved
        # scope count above stays silent about it; these two count the delivered
        # round trip itself.
        "required_long_distance_leg_count": required_leg_count,
        "required_long_distance_leg_missing_count": missing_leg_count,
        "hard_constraint_contract_complete": int(
            pack_meta.get("hard_constraint_contract_complete") is not False
        ),
    }


def _completion_audit_summary(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Project completion controls into a durable, audit-safe summary.

    Full LangGraph state is checkpointed elsewhere and must not be copied into
    TripRun.  This compact projection is deliberately sufficient to recompute
    the 5/6/8-minute and zero-tolerance metrics after a process restart while
    excluding prompts, provider payloads, candidate prose, and tool arguments.
    """

    draft = _as_mapping(data.get("minimum_delivery_draft"))
    planning_authorized_at = _iso_or_none(draft.get("planning_authorized_at"))
    if not draft or not planning_authorized_at:
        return None

    deadline = _as_mapping(data.get("run_deadline"))
    bundle = _as_mapping(data.get("delivery_bundle"))
    workspace = _as_mapping(data.get("trip_workspace_v2")) or _as_mapping(
        bundle.get("workspace")
    )
    facts = _as_mapping(data.get("fact_store_snapshot")) or _as_mapping(
        bundle.get("fact_snapshot")
    )
    report = _as_mapping(data.get("report_projection")) or _as_mapping(
        bundle.get("report_projection")
    )
    map_projection = _as_mapping(data.get("map_projection")) or _as_mapping(
        bundle.get("map_projection")
    )
    source_index = _as_mapping(data.get("source_index_projection")) or _as_mapping(
        bundle.get("source_index")
    )
    manifest = _as_mapping(bundle.get("manifest"))

    exhausted_gap_ids = _unique_strings(
        gap.get("gap_id")
        for gap in _as_mappings(data.get("candidate_research_gaps"))
        if gap.get("status") == "exhausted"
    )

    source_origin_counts = {
        "live": 0,
        "provider_snapshot_cache": 0,
        "other": 0,
    }
    for source in _as_mappings(facts.get("source_records")):
        provenance = _as_mapping(source.get("cache_provenance"))
        origin = str(provenance.get("origin") or "other")
        if origin not in source_origin_counts:
            origin = "other"
        source_origin_counts[origin] += 1

    attributions: List[Dict[str, Any]] = []
    for attribution_id, raw_attribution in _as_mapping(
        data.get("gate_failure_attributions")
    ).items():
        attribution = _as_mapping(raw_attribution)
        if not attribution:
            continue
        attributions.append(
            {
                "attribution_id": str(attribution.get("attribution_id") or attribution_id),
                "gate_class": attribution.get("gate_class"),
                "disposition": attribution.get("disposition"),
                "reason_code": attribution.get("reason_code"),
                "research_domain": attribution.get("research_domain"),
                "gap_ids": _unique_strings(attribution.get("gap_ids") or []),
                "failure_signature": attribution.get("failure_signature"),
                "deterministic": bool(attribution.get("deterministic")),
                "retry_attempt": int(attribution.get("retry_attempt") or 0),
                "recorded_at": _iso_or_none(attribution.get("recorded_at")),
            }
        )
    attributions.sort(key=lambda item: item["attribution_id"])

    terminal = _as_mapping(data.get("terminal_attribution"))
    terminal_attribution = (
        {
            "draft_id": terminal.get("draft_id"),
            "closure_status": terminal.get("closure_status"),
            "reason_code": terminal.get("reason_code"),
            "recorded_at": _iso_or_none(terminal.get("recorded_at")),
            "delivery_bundle_id": terminal.get("delivery_bundle_id"),
            "gate_class": terminal.get("gate_class"),
        }
        if terminal
        else None
    )

    controlled_identity_valid = _controlled_identity_is_valid(
        data.get("controlled_trip_identity")
    ) and (
        _as_int(draft.get("controlled_trip_identity_revision"))
        == _as_int(data.get("controlled_trip_identity_revision"))
    )
    planning_generation = _as_mapping(data.get("planning_generation"))
    intent_spec = _as_mapping(data.get("intent_spec"))
    intent_generation_valid = bool(
        planning_generation.get("generation_id")
        and draft.get("planning_generation_id")
        == planning_generation.get("generation_id")
        and _as_int(draft.get("intent_spec_revision"))
        == _as_int(data.get("intent_spec_revision"))
        == _as_int(intent_spec.get("revision"))
        and draft.get("intent_spec_hash") == intent_spec.get("content_hash")
    )
    constraint_revision_valid = (
        isinstance(data.get("constraint_pack"), dict)
        and _as_int(draft.get("constraint_pack_revision"))
        == _as_int(data.get("constraint_pack_revision"))
    )
    sealed_draft_valid = bool(
        draft.get("planning_authorized") is True
        and draft.get("draft_id")
        and draft.get("run_id")
        and str(draft.get("run_id")) == str(data.get("run_id") or draft.get("run_id"))
        and intent_generation_valid
        and _as_int(draft.get("plan_revision"))
        == _as_int(data.get("plan_gate_revision_count"))
        and deadline.get("draft_id") == draft.get("draft_id")
        and _iso_or_none(deadline.get("planning_authorized_at"))
        == planning_authorized_at
    )
    user_cancelled = bool(
        _as_mapping(terminal_attribution).get("reason_code") == "user_cancelled"
    )
    formal_bundle_capable = bool(
        sealed_draft_valid
        and _delivery_is_intact(data)
    )

    expected_constraint_ids = _unique_strings(draft.get("preserved_constraint_ids") or [])
    workspace_constraint_ids = _unique_strings(
        anchor.get("constraint_id")
        for anchor in _as_mappings(workspace.get("user_input_anchors"))
        if anchor.get("input_kind") == "hard_constraint"
    )
    workspace_revision = workspace.get("workspace_revision")
    fact_revision = facts.get("fact_data_revision")
    weather = _as_mapping(data.get("weather_context")) or _as_mapping(
        bundle.get("weather_snapshot")
    )
    weather_revision = weather.get("weather_data_revision")
    projection_consistent = bool(manifest) and (
        manifest.get("workspace_revision") == workspace_revision
        and manifest.get("fact_data_revision") == fact_revision
        and manifest.get("weather_data_revision") == weather_revision
        and report.get("source_workspace_revision") == workspace_revision
        and report.get("source_fact_data_revision") == fact_revision
        and report.get("source_weather_data_revision") == weather_revision
        and map_projection.get("source_workspace_revision") == workspace_revision
        and source_index.get("source_fact_data_revision") == fact_revision
    )
    document = _as_mapping(report.get("document"))
    report_content_nonempty = bool(
        document.get("days")
        or document.get("sections")
        or document.get("summary")
        or document.get("title")
    )

    return {
        "run_id": str(data.get("run_id") or draft.get("run_id") or ""),
        "draft_id": str(draft.get("draft_id") or ""),
        "planning_authorized_at": planning_authorized_at,
        "deadline": {
            "target_at": _iso_or_none(deadline.get("target_at")),
            "closeout_at": _iso_or_none(deadline.get("closeout_at")),
            "composition_at": _iso_or_none(deadline.get("composition_at")),
            "delivery_deadline_at": _iso_or_none(deadline.get("delivery_deadline_at")),
            "last_observed_at": _iso_or_none(deadline.get("last_observed_at")),
            "target_seconds": int(deadline.get("target_seconds") or 0),
            "closeout_seconds": int(deadline.get("closeout_seconds") or 0),
            "composition_seconds": int(deadline.get("composition_seconds") or 0),
            "delivery_deadline_seconds": int(
                deadline.get("delivery_deadline_seconds") or 0
            ),
            "checkpointed_elapsed_seconds": float(
                deadline.get("checkpointed_elapsed_seconds") or 0.0
            ),
        },
        "exhausted_gap_ids": exhausted_gap_ids,
        "candidate_gate_attempts": {
            str(key): int(value or 0)
            for key, value in _as_mapping(data.get("candidate_gate_attempts")).items()
        },
        "source_origin_counts": source_origin_counts,
        "gate_failure_attributions": attributions,
        "constraint_contract": {
            "expected_ids": expected_constraint_ids,
            "workspace_ids": workspace_constraint_ids,
        },
        "eligibility_contract": {
            "controlled_identity_valid": controlled_identity_valid,
            "intent_generation_valid": intent_generation_valid,
            "constraint_revision_valid": constraint_revision_valid,
            "sealed_draft_valid": sealed_draft_valid,
            "formal_bundle_capable": formal_bundle_capable,
            "user_cancelled": user_cancelled,
        },
        "quality_indicators": _quality_indicators(
            data=data,
            facts=facts,
            report=report,
        ),
        "formal_delivery": {
            "bundle_id": manifest.get("bundle_id"),
            "has_bundle": bool(manifest),
            "report_ready": report.get("status") == "ready",
            "report_content_nonempty": report_content_nonempty,
            "projection_consistent": projection_consistent,
        },
        "terminal_attribution": terminal_attribution,
    }


def audit_has_durable_bundle(completion_audit: Any) -> bool:
    """审计摘要里是否真的有一份已交付的 Bundle。

    判据与写它的那段（`formal_delivery` 上面那几行）放在一起：读的一方在别的模块里
    按字符串键自己拼一遍时，一次改名就让每个已完成的 deep run 都被判成没交付 ——
    而恢复扫描会照着那个结论把它们标成不可恢复。
    """

    delivery = _as_mapping(_as_mapping(completion_audit).get("formal_delivery"))
    return bool(delivery.get("has_bundle")) and bool(delivery.get("bundle_id"))


def completion_audit_from_state_summary(summary: Any) -> Dict[str, Any]:
    """Read a completion audit safely (including older embedded projections)."""

    data = _as_mapping(summary)
    embedded = _as_mapping(data.get(COMPLETION_AUDIT_SUMMARY_KEY))
    if embedded:
        return embedded
    return data if data.get("planning_authorized_at") else {}


def public_trip_run_state_summary(summary: Any) -> Dict[str, Any]:
    """Hide provider and gap diagnostics and trip identity from the REST surface."""

    safe = _as_mapping(summary)
    safe.pop(COMPLETION_AUDIT_SUMMARY_KEY, None)
    safe.pop("controlled_trip_identity", None)
    return safe


def build_trip_run_completion_audit(state: Any) -> Dict[str, Any]:
    """Return the separate developer/Eval completion projection for a state."""

    if state is None:
        return {}
    if hasattr(state, "model_dump"):
        data = state.model_dump(mode="json")
    elif isinstance(state, dict):
        data = state
    else:
        data = {}
    return _completion_audit_summary(data) or {}


def build_trip_run_state_summary(state: Any) -> Dict[str, Any]:
    """Return a trace-safe latest-state projection for TripRunState."""
    if state is None:
        return {}
    if hasattr(state, "model_dump"):
        data = state.model_dump()
    elif isinstance(state, dict):
        data = state
    else:
        data = {}

    task_type = data.get("task_type")
    if hasattr(task_type, "value"):
        task_type = task_type.value

    pending_choice = data.get("pending_user_choice")
    workspace = data.get("trip_workspace_v2")
    packets = data.get("research_packets") or {}
    fact_store = data.get("fact_store_snapshot") or {}
    source_records = fact_store.get("source_records") if isinstance(fact_store, dict) else []
    summary = {
        "task_type": task_type,
        "has_itinerary": bool(workspace),
        "has_workspace_v2": bool(workspace),
        "research_packet_count": len(packets),
        "source_record_count": len(source_records or []),
        "refinement_count": data.get("refinement_count") or 0,
        "pending_user_choice": bool(pending_choice),
        "map_ready": bool(data.get("map_projection")),
        "synthesis_mode": data.get("synthesis_mode"),
        "route_decision": data.get("route_decision") or None,
        "identity_locked": bool(data.get("controlled_trip_identity")),
    }
    return summary


def with_pending_choice_summary(
    summary: Any,
    *,
    pending: bool,
) -> Dict[str, Any]:
    """Return a copy of a trace-safe summary with the interaction flag corrected."""
    data = dict(summary) if isinstance(summary, dict) else {}
    data["pending_user_choice"] = bool(pending)
    return data


def mark_cancelled_pending_choice(value: Any) -> Optional[Dict[str, Any]]:
    """把当前决策保存为取消终态的只读审计快照。"""
    if not isinstance(value, dict) or not value:
        return None
    return {
        **value,
        "read_only": True,
        "terminal_status": TripRunStatus.CANCELLED.value,
    }
