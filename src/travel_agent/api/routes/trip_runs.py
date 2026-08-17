"""TripRun lifecycle API.

Threat model
-------------------------------------------
本机单用户产品，没有登录：知道 ``run_id`` 就能读写那个 run。带 ``session_id``
时额外校验它属于该会话，避免客户端把另一段会话的 run 当成当前这段的。

TripRun records and durable events remain internal recovery data. Public routes
expose traveller actions and the current delivery workspace only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping, Optional

from fastapi import APIRouter, HTTPException, Query, Response

from ...builders import get_components
from ...entities.trip_run import (
    RunCommand,
    RunCommandStatus,
    RunCommandType,
    TripRunStatus,
    available_run_actions,
)
from ...local_profile import LOCAL_USER_ID
from ...entities.workspace_v2_mutations import WorkspaceV2MutationError
from ...entities.trip_input import classify_locked_identity_intent
from ...infrastructure.delivery_bundle_store import (
    BundleContractSuperseded,
    BundleIdempotencyMismatch,
    BundleRevisionConflict,
    BundleRevisionVector,
)
from ...infrastructure.weather_provider import default_weather_providers
from ...services.workspace_v2_service import WorkspaceV2Service
from ...services.weather_bundle_refresh import (
    WeatherBundleRefreshService,
    WeatherRefreshRefused,
)
from ...services.weather_context_builder import WeatherContextBuilder
from ...services.run_completion_metrics import (
    RunCompletionMetrics,
    recompute_completion_metrics,
)
from ...services.blocking_work import BlockingWorkBusy, run_blocking
from ...services.pdf_export import (
    PdfFontUnavailable,
    ReportOutOfDateError,
    render_trip_report_pdf,
)
from ..schemas import (
    LLMCallCostResponse,
    PublicDeliveryBundleResponse,
    RunCostResponse,
    RunCostSummaryResponse,
    ToolAuditListResponse,
    ToolAuditRecordResponse,
    TripRunCommandResponse,
    TripRunControlRequest,
    TripRunControlResponse,
    TripRunCompletionDiagnosticsResponse,
    TripRunSupplementRequest,
    TripRunSupplementResponse,
    TripReportPdfExportRequest,
    TripRunCreateRequest,
    TripRunDetailResponse,
    TripRunEventResponse,
    TripRunExecutionResponse,
    TripRunEventWindowResponse,
    TripRunListResponse,
    TripRunResponse,
    TripRunStateResponse,
    WorkspaceV2MutationImpactResponse,
    WorkspaceV2MutationPreviewResponse,
    WorkspaceV2MutationRequest,
    WorkspaceV2MutationResponse,
    WorkspaceV2UndoHeadResponse,
    WorkspaceV2UndoRequest,
    WorkspaceV2UndoResponse,
    WeatherBundleRefreshRequest,
    WeatherBundleRefreshResponse,
)
from ..audit_projection import audit_safe_value
from ..sse_projection import public_plan_gate_payload
from ...services.public_delivery import (
    public_delivery_bundle,
    public_event_manifest,
)
from ...services.run_commands import RUN_ENDED_BEFORE_CONSUMPTION
from ...workflows.run_control import run_control_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trip-runs", tags=["trip-runs"])

_SUPPLEMENT_IMPACT = {
    "food": ["目的地体验", "每日安排"],
    "transport": ["交通可行性", "每日路线"],
    "accommodation": ["住宿调研", "通勤范围"],
    "pace": ["每日强度", "行程编排"],
    "must_do": ["体验核验", "每日安排"],
    "other": ["相关调研任务"],
}


def _workspace_expected(request: WorkspaceV2MutationRequest) -> BundleRevisionVector:
    return BundleRevisionVector(
        workspace_revision=request.base_workspace_revision,
        fact_data_revision=request.base_fact_data_revision,
        weather_data_revision=request.base_weather_data_revision,
        bundle_id=request.base_bundle_id,
    )


def _undo_expected(request: WorkspaceV2UndoRequest) -> BundleRevisionVector:
    return BundleRevisionVector(
        workspace_revision=request.base_workspace_revision,
        fact_data_revision=request.base_fact_data_revision,
        weather_data_revision=request.base_weather_data_revision,
        bundle_id=request.base_bundle_id,
    )


def _weather_refresh_expected(request: WeatherBundleRefreshRequest) -> BundleRevisionVector:
    return BundleRevisionVector(
        workspace_revision=request.base_workspace_revision,
        fact_data_revision=request.base_fact_data_revision,
        weather_data_revision=request.base_weather_data_revision,
        bundle_id=request.base_bundle_id,
    )


async def _raise_workspace_mutation_conflict(run_id: str) -> None:
    """Answer a lost CAS race from head metadata alone.

    A conflict response is most needed when the run is already in an unexpected
    state, so it must not re-read and re-project the persisted Bundle: the client
    is told which Bundle is current and re-reads it through the current-Bundle
    route.  Same detail shape as ``report_out_of_date``.
    """
    current_bundle_id = await get_components().delivery_bundle_store.get_current_bundle_id(
        run_id
    )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "bundle_revision_conflict",
            "current_bundle_id": current_bundle_id,
        },
    )


def _run_response(
    run,
    *,
    title: Optional[str] = None,
) -> TripRunResponse:
    """Inspect-surface run projection: codes/nodes yes, raw error prose never."""
    return TripRunResponse(
        run_id=run.run_id,
        session_id=run.session_id,
        mode=run.mode.value,
        status=run.status.value,
        title=title,
        request_message_id=run.request_message_id,
        assistant_message_id=run.assistant_message_id,
        parent_run_id=run.parent_run_id,
        current_node=run.current_node,
        resume_policy=run.resume_policy.value,
        last_error_code=run.last_error_code,
        # Upstream/model error prose can carry request or response fragments.
        last_error_message=None,
        attempt=run.attempt,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        cancelled_at=run.cancelled_at,
    )


def _public_pending_user_choice(choice: Any) -> Optional[Dict[str, Any]]:
    """Project the awaiting-input decision, or ``None`` when nobody can act on it.

    The durable column is a JSONB blob, but it has one live producer: the plan
    approval gate.  A shape no client can restore is not republished — a returning
    browser would only be able to render it as an unactionable waiting state.
    ``read_only`` / ``terminal_status`` are carried through because they are how a
    client tells "still waiting on you" from "this decision was cancelled".
    """

    if not isinstance(choice, Mapping):
        return None
    if str(choice.get("type") or "") != "approval_gate":
        return None
    projected: Dict[str, Any] = {
        "type": "approval_gate",
        "gate": choice.get("gate"),
        "payload": public_plan_gate_payload(choice.get("payload")),
    }
    if choice.get("read_only"):
        projected["read_only"] = True
        projected["terminal_status"] = choice.get("terminal_status")
    return projected


def _state_response(state) -> TripRunStateResponse:
    """Authorized inspect projection of run state (audit-safe, no dual mode)."""
    return TripRunStateResponse(
        run_id=state.run_id,
        status=state.status.value,
        current_node=state.current_node,
        completed_nodes=list(state.completed_nodes or []),
        latest_state_summary=audit_safe_value(state.latest_state_summary or {}),
        pending_user_choice=_public_pending_user_choice(state.pending_user_choice),
        trace_event_count=state.trace_event_count,
        pending_monitor_trigger_count=state.pending_monitor_trigger_count,
        last_error=audit_safe_value(state.last_error) if state.last_error else None,
        updated_at=state.updated_at,
    )


def _execution_response(execution) -> Optional[TripRunExecutionResponse]:
    """执行归属的公开投影。`executor_id` 与 `lease_token` 留在服务端：

    前者是主机名加进程标识，后者是抢占租约的凭据，两个都不是客户端需要的东西。
    """

    if execution is None:
        return None
    return TripRunExecutionResponse(
        status=execution.recovery_status.value,
        last_heartbeat_at=execution.heartbeat_at,
        lease_expires_at=execution.lease_expires_at,
        last_safe_checkpoint_id=execution.last_safe_checkpoint_id,
        recovery_reason=execution.recovery_reason,
    )


def _event_response(event) -> Optional[TripRunEventResponse]:
    """Project durable events for product lifecycle + audit-safe inspect.

    Lifecycle boundaries stay product-shaped; other event types are included
    only after ``audit_safe_value`` strips raw I/O keys (inspect surface).
    """
    if event.event_type in {"delivery.ready", "bundle.current_changed"}:
        payload = {
            "bundle_id": event.payload.get("bundle_id"),
            "manifest": public_event_manifest(event.payload.get("manifest")),
        }
    elif event.event_type == "run.terminal":
        payload = {
            "status": event.payload.get("status"),
            "bundle_id": event.payload.get("bundle_id"),
        }
    elif event.event_type in {"run.completed", "run.cancelled", "run.interrupted"}:
        payload = {"status": event.event_type.removeprefix("run.")}
    elif event.event_type in {"run.awaiting_input", "run.gate_raised"}:
        payload = {"status": TripRunStatus.AWAITING_INPUT.value}
    elif event.event_type == "run.guard_blocked":
        # 安全策略拒绝不是系统故障：状态词汇仍是 failed，但文案必须说明是「被拒绝」，
        # 既不能说成生成失败，也不能邀请用户重试同一个被拒绝的请求。
        payload = {
            "status": TripRunStatus.FAILED.value,
            "message": "这次请求未通过安全策略检查，已被拒绝执行。",
        }
    elif event.event_type == "run.failed":
        payload = {
            "status": TripRunStatus.FAILED.value,
            "message": "旅行方案暂时无法生成，请稍后重试。",
        }
        reason = event.payload.get("terminal_reason_code") or event.payload.get(
            "reason_code"
        )
        if reason:
            payload["reason_code"] = reason
    elif event.event_type in {"run.created", "run.started", "run.control_requested"}:
        payload = {}
    else:
        # Inspect: audit-safe full payload for other durable types (trace, etc.).
        scrubbed = audit_safe_value(event.payload or {})
        payload = scrubbed if isinstance(scrubbed, dict) else {}
    return TripRunEventResponse(
        event_id=event.event_id,
        run_id=event.run_id,
        sequence=event.sequence,
        event_type=event.event_type,
        payload=payload,
        created_at=event.created_at,
    )


def _event_responses(events) -> list[TripRunEventResponse]:
    return [
        response
        for event in events
        if (response := _event_response(event)) is not None
    ]


def _tool_audit_response(record) -> ToolAuditRecordResponse:
    return ToolAuditRecordResponse(
        audit_id=record.audit_id,
        run_id=record.run_id,
        tool_name=record.tool_name,
        server_name=record.server_name,
        source_type=record.source_type,
        category=record.category,
        permission_class=record.permission_class,
        operation_sensitivity=record.operation_sensitivity,
        status=record.status,
        gateway_decision=record.gateway_decision,
        args_digest=record.args_digest,
        result_digest=record.result_digest,
        untrusted_content=record.untrusted_content,
        quarantined=record.quarantined,
        fallback_from=record.fallback_from,
        fallback_to=record.fallback_to,
        # This field can originate in an upstream exception and may therefore
        # contain request/response text.  Status, digests and safe metadata
        # are sufficient for developer diagnosis.
        degradation_reason=None,
        # Provider/tool error prose may echo an upstream request or response;
        # the normalized audit status and digests are the developer contract.
        error=None,
        evidence_allowed=record.evidence_allowed,
        metadata=audit_safe_value(record.metadata),
        created_at=record.created_at,
    )


async def _get_authorized_run(run_id: str, *, session_id: Optional[str] = None):
    components = get_components()
    detail = await components.trip_run_store.get_detail(run_id, event_limit=0)
    if detail is None:
        raise HTTPException(status_code=404, detail="TripRun 不存在")
    if session_id and detail.run.session_id != session_id:
        raise HTTPException(status_code=403, detail="无权访问该 TripRun")
    return detail.run


@router.post("", response_model=TripRunResponse)
async def create_trip_run(request: TripRunCreateRequest) -> TripRunResponse:
    if request.route_decision.route.value != "trip_planning":
        raise HTTPException(status_code=422, detail="只有已确认的旅行规划可以创建规划 TripRun")
    components = get_components()
    run = await components.trip_run_store.create_run(
        session_id=request.session_id,
        user_id=LOCAL_USER_ID,
        mode="deep",
        request_message_id=request.request_message_id,
        assistant_message_id=request.assistant_message_id,
        parent_run_id=request.parent_run_id,
        controlled_trip_identity=request.controlled_trip_identity.model_dump(mode="json"),
        route_decision=request.route_decision.model_dump(mode="json"),
    )
    return _run_response(run)


@router.get("", response_model=TripRunListResponse)
async def list_trip_runs(
    session_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    mode: Optional[Literal["deep", "fast"]] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> TripRunListResponse:
    components = get_components()
    try:
        runs = await components.trip_run_store.list_runs(
            user_id=LOCAL_USER_ID,
            session_id=session_id,
            status=status,
            mode=mode,
            limit=limit,
        )
        total = await components.trip_run_store.count_runs(
            user_id=LOCAL_USER_ID,
            session_id=session_id,
            status=status,
            mode=mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    titles = await components.trip_run_store.list_run_titles(
        [run.session_id for run in runs]
    )
    return TripRunListResponse(
        runs=[_run_response(run, title=titles.get(run.session_id)) for run in runs],
        total=total,
    )


@router.get("/metrics/completion", response_model=RunCompletionMetrics)
async def read_completion_metrics(
    session_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=200),
) -> RunCompletionMetrics:
    """Recompute eligible-run completion metrics from durable audit evidence.

    Inspect surface: available to the authorized user for their own runs
    (bounded window). Not a cross-tenant ops dashboard.
    """
    components = get_components()
    runs = await components.trip_run_store.list_runs(
        user_id=LOCAL_USER_ID,
        session_id=session_id,
        limit=limit,
    )
    details = []
    for run in runs:
        detail = await components.trip_run_store.get_detail(run.run_id, event_limit=500)
        if detail is not None:
            details.append(detail)
    # This online diagnostic endpoint intentionally bounds database work.  It
    # must never label the resulting window as a full completion-rate audit;
    # offline Eval can call the same pure calculator with the complete export.
    return recompute_completion_metrics(
        details,
        observation_scope={
            "is_complete": False,
            "run_limit": limit,
            "events_per_run_limit": 500,
            "run_population_may_be_truncated": True,
            "events_may_be_truncated": True,
        },
    )


@router.get("/{run_id}", response_model=TripRunDetailResponse)
async def read_trip_run(
    run_id: str,
    event_limit: int = Query(default=50, ge=0, le=200),
    session_id: Optional[str] = Query(default=None),
):
    components = get_components()
    await _get_authorized_run(run_id, session_id=session_id)
    detail = await components.trip_run_store.get_detail(run_id, event_limit=event_limit)
    if detail is None:
        raise HTTPException(status_code=404, detail="TripRun 不存在")
    execution = await components.run_execution_store.get(run_id)
    return TripRunDetailResponse(
        run=_run_response(detail.run),
        controlled_trip_identity=detail.run.controlled_trip_identity,
        state=_state_response(detail.state),
        execution=_execution_response(execution),
        available_actions=available_run_actions(detail.run, execution),
        events=_event_responses(detail.events),
    )


@router.get("/{run_id}/events", response_model=TripRunEventWindowResponse)
async def list_trip_run_events(
    run_id: str,
    session_id: Optional[str] = Query(default=None),
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    retention_events: int = Query(default=500, ge=1, le=5000),
) -> TripRunEventWindowResponse:
    components = get_components()
    run = await _get_authorized_run(run_id, session_id=session_id)
    window = await components.trip_run_store.read_event_window(
        run_id,
        after_sequence=after_sequence,
        limit=limit,
        retention_events=retention_events,
    )
    # Recovery polling needs the current Bundle identity, not its content: read
    # the head pointer only so an event window never depends on a payload parse.
    current_bundle_id = await components.delivery_bundle_store.get_current_bundle_id(run_id)
    next_after = (
        window.events[-1].sequence
        if window.events
        else max(after_sequence, window.latest_sequence if window.window_expired else after_sequence)
    )
    return TripRunEventWindowResponse(
        run_id=run_id,
        requested_after_sequence=window.requested_after_sequence,
        replay_floor_sequence=window.replay_floor_sequence,
        latest_sequence=window.latest_sequence,
        next_after_sequence=next_after,
        window_expired=window.window_expired,
        run_status=run.status.value,
        current_bundle_id=current_bundle_id,
        events=_event_responses(window.events),
    )


@router.get("/{run_id}/bundle/current", response_model=PublicDeliveryBundleResponse)
async def read_current_delivery_bundle(
    run_id: str,
    session_id: Optional[str] = Query(default=None),
) -> PublicDeliveryBundleResponse:
    components = get_components()
    await _get_authorized_run(run_id, session_id=session_id)
    try:
        bundle = await components.delivery_bundle_store.get_current(run_id)
    except BundleContractSuperseded as exc:
        _raise_bundle_contract_superseded(exc)
        raise AssertionError("unreachable")
    if bundle is None:
        raise HTTPException(status_code=404, detail="当前 Delivery Bundle 不存在")
    return PublicDeliveryBundleResponse.model_validate(public_delivery_bundle(bundle))


@router.post(
    "/{run_id}/bundle/current/weather/refresh",
    response_model=WeatherBundleRefreshResponse,
)
async def refresh_current_bundle_weather(
    run_id: str,
    request: WeatherBundleRefreshRequest,
) -> WeatherBundleRefreshResponse:
    components = get_components()
    await _get_authorized_run(
        run_id, session_id=request.session_id
    )
    # Identity is all this route needs to know a Bundle exists.  Parsing the whole
    # payload here would put the read cost — and, once the stamp is checked, the
    # refusal — on every request, while exactly one rarely reached branch below is
    # what actually consumes it.
    if await components.delivery_bundle_store.get_current_bundle_id(run_id) is None:
        raise HTTPException(status_code=404, detail="当前 Delivery Bundle 不存在")
    refresh_kwargs: dict[str, Any] = {}
    clock = getattr(components, "weather_refresh_clock", None)
    if clock is not None:
        refresh_kwargs["clock"] = clock
    service = WeatherBundleRefreshService(
        components.delivery_bundle_store,
        getattr(components, "weather_context_builder", None)
        or WeatherContextBuilder(default_weather_providers()),
        **refresh_kwargs,
    )
    try:
        result = await service.refresh_if_needed(
            run_id=run_id,
            expected=_weather_refresh_expected(request),
            idempotency_key=request.refresh_id,
            operation="weather_refresh_on_open",
        )
    except BundleContractSuperseded as exc:
        _raise_bundle_contract_superseded(exc)
        raise AssertionError("unreachable")
    except BundleRevisionConflict:
        await _raise_workspace_mutation_conflict(run_id)
        raise AssertionError("unreachable")
    except BundleIdempotencyMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "refresh_id_reused",
                "message": "这次天气刷新的标识已用于另一项操作。",
            },
        ) from exc
    except BundleContractSuperseded as exc:
        _raise_bundle_contract_superseded(exc)
        raise AssertionError("unreachable")
    except WeatherRefreshRefused as exc:
        # The service already converts its own refusals into a typed result;
        # this only guarantees a refusal raised anywhere below still reaches the
        # caller as an explicit, reasoned receipt rather than an opaque 500.
        logger.warning(
            "weather refresh refused at API boundary run_id=%s reason=%s: %s",
            run_id,
            exc.code,
            exc,
        )
        # Only this branch needs the unchanged current Bundle, so only this branch
        # pays for reading it.
        try:
            current = await components.delivery_bundle_store.get_current(run_id)
        except BundleContractSuperseded as superseded:
            _raise_bundle_contract_superseded(superseded)
            raise AssertionError("unreachable")
        if current is None:
            raise HTTPException(status_code=404, detail="当前 Delivery Bundle 不存在")
        return WeatherBundleRefreshResponse(
            refresh_id=request.refresh_id,
            attempted=True,
            committed=False,
            used_previous_values=False,
            refusal_reason=exc.code,
            bundle=PublicDeliveryBundleResponse.model_validate(
                public_delivery_bundle(current)
            ),
        )
    if result.committed:
        manifest = result.bundle.manifest
        await components.trip_run_store.append_event_once(
            run_id,
            "bundle.current_changed",
            {
                "reason": "weather_refresh_on_open",
                "bundle_id": manifest.bundle_id,
                "manifest": manifest.model_dump(mode="json"),
            },
            idempotency_key=f"{run_id}:bundle_current_changed:{manifest.bundle_id}",
        )
    return WeatherBundleRefreshResponse(
        refresh_id=request.refresh_id,
        attempted=result.attempted,
        committed=result.committed,
        used_previous_values=result.used_previous_values,
        refusal_reason=result.refusal_reason,
        bundle=PublicDeliveryBundleResponse.model_validate(
            public_delivery_bundle(result.bundle)
        ),
    )


@router.post("/{run_id}/bundle/current/pdf")
async def export_current_trip_report_pdf(
    run_id: str,
    request: TripReportPdfExportRequest,
) -> Response:
    components = get_components()
    await _get_authorized_run(
        run_id, session_id=request.session_id
    )
    try:
        current = await components.delivery_bundle_store.get_current(run_id)
    except BundleContractSuperseded as exc:
        _raise_bundle_contract_superseded(exc)
        raise AssertionError("unreachable")
    if current is None:
        raise HTTPException(status_code=404, detail="当前 Delivery Bundle 不存在")
    manifest = current.manifest
    requested = (
        request.bundle_id,
        request.workspace_revision,
        request.fact_data_revision,
        request.weather_data_revision,
    )
    actual = (
        manifest.bundle_id,
        manifest.workspace_revision,
        manifest.fact_data_revision,
        manifest.weather_data_revision,
    )
    if requested != actual:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "report_out_of_date",
                "current_bundle_id": manifest.bundle_id,
            },
        )
    try:
        # ReportLab 是同步的、吃满一个核，而这条路上还有别人的 SSE 保活帧要发。
        # 线程里接到的是一份 immutable Bundle 的完整快照，不读也不改任何共享状态。
        artifact = await run_blocking(
            "pdf_export",
            render_trip_report_pdf,
            current,
            exported_at=datetime.now(timezone.utc),
        )
    except ReportOutOfDateError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "report_out_of_date"},
        ) from exc
    except PdfFontUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "pdf_temporarily_unavailable",
                "message": "PDF 暂时无法生成，请稍后重试。",
            },
        ) from exc
    except BlockingWorkBusy as exc:
        # 渲染通道排满不是这份报告的问题，也不改 Run/Bundle 状态：等一会儿再导。
        raise HTTPException(
            status_code=503,
            detail={
                "code": "pdf_export_busy",
                "message": "正在导出的报告太多，稍后再试。",
            },
        ) from exc
    return Response(
        content=artifact.content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "X-JourneyPilot-Bundle-ID": artifact.bundle_id,
            "X-JourneyPilot-Workspace-Revision": str(
                artifact.build_context.source_workspace_revision
            ),
            "X-JourneyPilot-Fact-Revision": str(
                artifact.build_context.source_fact_data_revision
            ),
            "X-JourneyPilot-Weather-Revision": str(
                artifact.build_context.source_weather_data_revision
            ),
        },
    )


@router.post(
    "/{run_id}/workspace/mutations/preview",
    response_model=WorkspaceV2MutationPreviewResponse,
)
async def preview_workspace_v2_mutation(
    run_id: str,
    request: WorkspaceV2MutationRequest,
) -> WorkspaceV2MutationPreviewResponse:
    components = get_components()
    await _get_authorized_run(
        run_id, session_id=request.session_id
    )
    service = WorkspaceV2Service(components.delivery_bundle_store)
    try:
        preview = await service.preview(
            run_id=run_id,
            expected=_workspace_expected(request),
            mutation=request.operation,
        )
    except BundleContractSuperseded as exc:
        _raise_bundle_contract_superseded(exc)
        raise AssertionError("unreachable")
    except BundleRevisionConflict:
        await _raise_workspace_mutation_conflict(run_id)
        raise AssertionError("unreachable")
    except WorkspaceV2MutationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    impacts: list[WorkspaceV2MutationImpactResponse] = []
    if preview.total_cost_delta_cny not in (None, 0.0):
        delta = preview.total_cost_delta_cny
        direction = "增加" if delta > 0 else "减少"
        impacts.append(
            WorkspaceV2MutationImpactResponse(
                kind="total_cost",
                delta_cny=delta,
                summary=f"行程预计总费用{direction} ¥{abs(delta):.0f}",
            )
        )
    return WorkspaceV2MutationPreviewResponse(
        mutation_id=request.mutation_id,
        changed=preview.application.changed,
        requires_confirmation=preview.requires_confirmation,
        impacts=impacts,
    )


@router.post(
    "/{run_id}/workspace/mutations",
    response_model=WorkspaceV2MutationResponse,
)
async def apply_workspace_v2_mutation(
    run_id: str,
    request: WorkspaceV2MutationRequest,
) -> WorkspaceV2MutationResponse:
    components = get_components()
    await _get_authorized_run(
        run_id, session_id=request.session_id
    )
    service = WorkspaceV2Service(components.delivery_bundle_store)
    try:
        result = await service.apply(
            run_id=run_id,
            expected=_workspace_expected(request),
            mutation=request.operation,
            idempotency_key=request.mutation_id,
        )
    except BundleContractSuperseded as exc:
        _raise_bundle_contract_superseded(exc)
        raise AssertionError("unreachable")
    except BundleRevisionConflict:
        await _raise_workspace_mutation_conflict(run_id)
        raise AssertionError("unreachable")
    except BundleIdempotencyMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "mutation_id_reused",
                "message": "这次调整的标识已用于另一项操作。",
            },
        ) from exc
    except WorkspaceV2MutationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    if result.commit is None:
        try:
            bundle = await components.delivery_bundle_store.get_current(run_id)
        except BundleContractSuperseded as exc:
            _raise_bundle_contract_superseded(exc)
            raise AssertionError("unreachable")
        if bundle is None:
            raise HTTPException(status_code=404, detail="当前 Delivery Bundle 不存在")
        replay = False
    else:
        bundle = result.commit.bundle
        replay = result.commit.idempotent_replay
    return WorkspaceV2MutationResponse(
        mutation_id=request.mutation_id,
        changed=result.application.changed,
        idempotent_replay=replay,
        bundle=PublicDeliveryBundleResponse.model_validate(public_delivery_bundle(bundle)),
    )


@router.get(
    "/{run_id}/workspace/mutations/{mutation_id}",
    response_model=WorkspaceV2MutationResponse,
)
async def read_workspace_v2_mutation(
    run_id: str,
    mutation_id: str,
    session_id: Optional[str] = Query(default=None),
) -> WorkspaceV2MutationResponse:
    components = get_components()
    await _get_authorized_run(run_id, session_id=session_id)
    try:
        record = await components.delivery_bundle_store.get_commit(run_id, mutation_id)
    except BundleContractSuperseded as exc:
        _raise_bundle_contract_superseded(exc)
        raise AssertionError("unreachable")
    if record is None or "mutation" not in record.metadata:
        raise HTTPException(status_code=404, detail="行程调整记录不存在")
    return WorkspaceV2MutationResponse(
        mutation_id=mutation_id,
        changed=record.metadata.get("mutation_outcome") != "no_op",
        idempotent_replay=True,
        bundle=PublicDeliveryBundleResponse.model_validate(
            public_delivery_bundle(record.result.bundle)
        ),
    )


@router.get(
    "/{run_id}/workspace/undo-head",
    response_model=WorkspaceV2UndoHeadResponse,
)
async def read_workspace_v2_undo_head(
    run_id: str,
    session_id: Optional[str] = Query(default=None),
) -> WorkspaceV2UndoHeadResponse:
    components = get_components()
    await _get_authorized_run(run_id, session_id=session_id)
    head = await components.delivery_bundle_store.get_undo_head(run_id)
    if head is None:
        return WorkspaceV2UndoHeadResponse(available=False)
    return WorkspaceV2UndoHeadResponse(
        available=True,
        mutation_id=head.mutation_id,
        label=head.label,
    )


@router.post(
    "/{run_id}/workspace/undo",
    response_model=WorkspaceV2UndoResponse,
)
async def undo_workspace_v2_mutation(
    run_id: str,
    request: WorkspaceV2UndoRequest,
) -> WorkspaceV2UndoResponse:
    components = get_components()
    await _get_authorized_run(
        run_id, session_id=request.session_id
    )
    service = WorkspaceV2Service(components.delivery_bundle_store)
    try:
        result = await service.undo_current(
            run_id=run_id,
            expected=_undo_expected(request),
            undo_of_mutation_id=request.undo_of_mutation_id,
            idempotency_key=request.undo_id,
        )
    except BundleContractSuperseded as exc:
        _raise_bundle_contract_superseded(exc)
        raise AssertionError("unreachable")
    except BundleRevisionConflict:
        await _raise_workspace_mutation_conflict(run_id)
        raise AssertionError("unreachable")
    except BundleIdempotencyMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "undo_id_reused",
                "message": "这次撤销的标识已用于另一项操作。",
            },
        ) from exc
    except WorkspaceV2MutationError as exc:
        if exc.code == "undo_head_changed":
            # Same rule as the revision conflict above: report identity, let the
            # client re-read the current Bundle.
            current_bundle_id = await components.delivery_bundle_store.get_current_bundle_id(
                run_id
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": "行程已更新，请重新加载后重试。",
                    "current_bundle_id": current_bundle_id,
                },
            ) from exc
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return WorkspaceV2UndoResponse(
        undo_id=request.undo_id,
        idempotent_replay=result.idempotent_replay,
        bundle=PublicDeliveryBundleResponse.model_validate(
            public_delivery_bundle(result.bundle)
        ),
    )


@router.post("/{run_id}/supplements", response_model=TripRunSupplementResponse)
async def add_trip_run_supplement(run_id: str, request: TripRunSupplementRequest) -> TripRunSupplementResponse:
    """Queue an auxiliary requirement at the next cooperative node boundary.

    要求先落 `trip_run_commands` 再通知执行器：接受它不取决于这次请求恰好命中哪个进程的
    内存。通知丢了或换了进程，执行器下一次轮询照样看得见。

    This surface intentionally cannot mutate controlled trip identity. Identity
    changes must start a new TripRun so the brief, itinerary and map never drift.
    """
    components = get_components()
    detail = await components.trip_run_store.get_detail(run_id, event_limit=0)
    if detail is None:
        raise HTTPException(status_code=404, detail="TripRun 不存在")
    if request.session_id and detail.run.session_id != request.session_id:
        raise HTTPException(status_code=403, detail="无权修改该 TripRun")
    content = request.content.strip()
    if classify_locked_identity_intent(content).classification == "change_requested":
        raise HTTPException(status_code=409, detail="基础旅行身份已锁定。请结束当前草稿或运行，再新建旅行。")
    if detail.run.status != TripRunStatus.RUNNING:
        raise HTTPException(status_code=409, detail="当前不在运行中；请在旅行简报的调研计划区域追加要求。")
    impact_scope = _SUPPLEMENT_IMPACT[request.category]
    command, created = await components.run_command_store.enqueue(
        run_id,
        RunCommandType.SUPPLEMENT,
        {
            "category": request.category,
            "content": content,
            "impact_scope": impact_scope,
            "source": "supplement_api",
        },
    )
    if created:
        await components.trip_run_store.append_event(
            run_id,
            "run.supplement_queued",
            {
                "command_id": command.command_id,
                "category": request.category,
                "content": content,
                "impact_scope": impact_scope,
            },
        )
    run_control_registry.notify(run_id)
    return TripRunSupplementResponse(
        run_id=run_id,
        command_id=command.command_id,
        accepted=True,
        status=command.status.value,
        category=request.category,
        message=_supplement_message(command, impact_scope),
        impact_scope=impact_scope,
    )


def _supplement_message(command: RunCommand, impact_scope: list[str]) -> str:
    """回执上那句话。命令已经有结论时，说结论，不说「即将分析」。"""

    if command.status is RunCommandStatus.CONSUMED:
        return "这条追加要求已经生效。"
    if command.status is RunCommandStatus.REJECTED:
        return "这次运行已经越过可以追加要求的阶段，这条要求不会生效。"
    return f"已加入当前运行，将在下一个执行边界分析对 { '、'.join(impact_scope) } 的影响。"


def _raise_bundle_contract_superseded(exc: BundleContractSuperseded) -> None:
    """Refuse a stored Bundle the current contract no longer describes.

    A refusal, not a fallback.  Reading the old shape "just for this run" would be a
    compatibility layer; what such a row needs is a data decision, taken once, not a
    second code path.  The client is told the plain fact so it stops asking —
    re-reading returns the same answer, so no automatic resync.
    """

    raise HTTPException(
        status_code=409,
        detail={
            "code": "bundle_contract_superseded",
            "message": "这趟旅行的结果是用旧版本保存的，已经无法展示。请重新规划这趟旅行。",
        },
    ) from exc


async def _raise_run_not_cancellable(run_id: str, exc: Exception | None = None) -> None:
    """Answer a lost cancel race from the record, not with a 500.

    ``/control`` reads the Run, decides it is cancellable, and then writes — and a
    Run that finished or was cancelled in that window makes the write unlawful.
    The store rejected it with a bare ``ValueError`` and nothing caught it, so the
    caller got an unhandled 500 with no body to act on, for a state the server
    knew exactly.  Same detail shape as every other conflict here, and the
    current status travels with it so a client can render the truth instead of
    re-polling to discover it.
    """

    current = await get_components().trip_run_store.get_run(run_id)
    raise HTTPException(
        status_code=409,
        detail={
            "code": "run_not_cancellable",
            "message": "这次运行已经结束，无法再停止。请刷新后查看当前状态。",
            "status": current.status.value if current is not None else None,
        },
    ) from exc


@router.post("/{run_id}/control", response_model=TripRunControlResponse)
async def control_trip_run(run_id: str, request: TripRunControlRequest) -> TripRunControlResponse:
    """取消一次运行：先落 durable command，再动状态，最后通知执行器。

    顺序是承重的。命令先落库，所以「已接受」这句话在进程重启后依然成立；状态转换紧随其后，
    所以 `cancel_requested` 一旦写进 `trip_runs`，交付边界的 `FOR UPDATE` 就再也提交不了
    completion（cancel/complete race 的权威规则）。进程内通知是最后一步，纯粹为了少等一个
    轮询周期 —— 它没命中不改变任何结论。
    """

    components = get_components()
    detail = await components.trip_run_store.get_detail(run_id, event_limit=0)
    if detail is None:
        raise HTTPException(status_code=404, detail="TripRun 不存在")
    if request.session_id and detail.run.session_id != request.session_id:
        raise HTTPException(status_code=403, detail="无权控制该 TripRun")
    if detail.run.status not in {
        TripRunStatus.CREATED,
        TripRunStatus.RUNNING,
        TripRunStatus.AWAITING_INPUT,
        TripRunStatus.CANCEL_REQUESTED,
    }:
        # Same answer as the race below, because it is the same question — the
        # precheck and the write only differ in when they noticed.  This used to
        # interpolate the internal status word into a prose detail, which is neither
        # machine-readable nor a sentence anyone should be shown .
        await _raise_run_not_cancellable(run_id)

    # 有没有执行器在跑，只有数据库里的租约能回答。这个判断决定要不要就地收敛，所以它必须
    # 早于状态写：写完之后读到的租约可能是这次请求自己引发的收口。
    live_executor = await components.run_execution_store.has_live_lease(run_id)
    command, _created = await components.run_command_store.enqueue(
        run_id,
        RunCommandType.CANCEL,
        {"action": "cancel", "source": "control_api"},
    )
    if detail.run.status == TripRunStatus.CANCEL_REQUESTED and live_executor:
        # 重发的停止请求，而执行器正在协作退出。不要越过它把状态改写成 cancelled ——
        # 那会在一个还在跑的运行上写下终态，然后由它继续往一个已经封存的记录上写。
        run_control_registry.notify(run_id)
        return TripRunControlResponse(
            run_id=run_id,
            action=request.action,
            command_id=command.command_id,
            accepted=True,
            status=command.status.value,
            run_status=detail.run.status.value,
            message="取消请求已记录，正在等待工作流协作退出。",
        )
    try:
        run = await components.trip_run_store.request_cancel(
            run_id,
            current_node=detail.run.current_node,
            event_type="run.control_requested",
            payload={
                "action": "cancel",
                "source": "control_api",
                "command_id": command.command_id,
            },
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="TripRun 不存在") from exc
    except RuntimeError as exc:
        # The store's own retry loop could not converge the cancel.  Saying so is
        # a 409 about a state that keeps moving, not a server fault.
        await _raise_run_not_cancellable(run_id, exc)
        raise AssertionError("unreachable")
    run_control_registry.notify(run_id)

    if run.status not in {TripRunStatus.CANCEL_REQUESTED, TripRunStatus.CANCELLED}:
        # 运行在 precheck 与这次写之间自己结束了（交付原子提交先赢）。这条命令没有、也不会
        # 被执行 —— 先给它一个结论，再答「已完成，无法取消」。留着它就是让回执永远说「还在等」。
        await components.run_command_store.settle(
            [command.command_id],
            status=RunCommandStatus.REJECTED,
            error_code=RUN_ENDED_BEFORE_CONSUMPTION,
            result={"run_status": run.status.value},
        )
        await _raise_run_not_cancellable(run_id)

    if run.status == TripRunStatus.CANCEL_REQUESTED and not live_executor:
        # 没有活着的执行器：这就是恢复扫描对 `cancel_requested` 的那条判定，只是不必让
        # 用户等到下一轮扫描才看到运行停下来。判据是数据库里的租约，不是本进程的内存 ——
        # 「registry 里没有 handle」曾经被当作「没人在跑」，而它只说明不是这个进程在跑。
        try:
            run = await components.trip_run_store.transition_status(
                run_id,
                TripRunStatus.CANCELLED,
                current_node=detail.run.current_node,
                event_type="run.cancelled",
                payload={"reason": "no_live_executor", "command_id": command.command_id},
            )
        except ValueError as exc:
            await _raise_run_not_cancellable(run_id, exc)
            raise AssertionError("unreachable")

    if run.status == TripRunStatus.CANCELLED:
        # 取消已经拿到它要的效果，命令就此收口。
        await components.run_command_store.settle(
            [command.command_id],
            status=RunCommandStatus.CONSUMED,
            result={"run_status": run.status.value, "settled_by": "control_api"},
        )
        settled = await components.run_command_store.get(run_id, command.command_id)
        return TripRunControlResponse(
            run_id=run_id,
            action=request.action,
            command_id=command.command_id,
            accepted=True,
            status=(settled or command).status.value,
            run_status=run.status.value,
            message="运行已停止。",
        )
    return TripRunControlResponse(
        run_id=run_id,
        action=request.action,
        command_id=command.command_id,
        accepted=True,
        status=command.status.value,
        run_status=run.status.value,
        message="取消请求已记录，正在等待工作流协作退出。",
    )


@router.get("/{run_id}/commands/{command_id}", response_model=TripRunCommandResponse)
async def read_trip_run_command(
    run_id: str,
    command_id: str,
    session_id: Optional[str] = Query(default=None),
) -> TripRunCommandResponse:
    """一条控制命令的结论。断线之后查它，而不是重发一次取消。"""

    components = get_components()
    run = await _get_authorized_run(run_id, session_id=session_id)
    command = await components.run_command_store.get(run_id, command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="控制命令不存在")
    return TripRunCommandResponse(
        command_id=command.command_id,
        run_id=command.run_id,
        command_type=command.command_type.value,
        status=command.status.value,
        run_status=run.status.value,
        error_code=command.error_code,
        result=command.result,
        created_at=command.created_at,
        updated_at=command.updated_at,
        consumed_at=command.consumed_at,
    )
