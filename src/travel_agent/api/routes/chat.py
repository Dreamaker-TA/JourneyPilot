"""
聊天 API 路由 (Serving Layer)
支持 SSE 流式响应和非流式两种模式。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ...builders import get_components
from ...config import get_settings
from ...entities.trip_run import RunRecoveryStatus, TripRunResumePolicy, TripRunStatus
from ...entities.trip_input import RouteName, classify_locked_identity_intent
from ...local_profile import LOCAL_USER_ID
from ...services.route_intent import RouteIntentUnavailable, classify_route
from ...utils.json_helpers import strip_think_blocks
from ..schemas import ChatRequest
from ..streaming import strip_non_display_blocks
from .chat_helpers import (
    check_input_guard,
    enqueue_memory_extraction,
    load_session_history,
    load_preset_context,
    load_user_profile,
    sse_event as _encode_sse_event,
    strip_thinking_text,
)
from ...services.public_delivery import (
    public_delivery_bundle,
    public_event_manifest,
)
from ..sse_projection import project_sse_payload
from ...entities.delivery_bundle import DeliveryContractViolation
from ...entities.evidence_basis import PublicProjectionContractViolation
from ...infrastructure.cost_ledger_store import cost_event_summary
from ...tools.exposure_ledger import get_tool_exposure_ledger
from ...workflows.delivery_finalizer import DeliveryFinalizationError
from ...workflows.travel_planning import CheckpointContractError
from ...workflows.run_control import (
    RunCancelled,
    node_timing_registry,
    run_control_registry,
    run_ts_ms,
    set_run_ts_anchor,
)
from ...services.run_commands import RunCommandCoordinator
from ...services.run_lease import RunLeaseKeeper
from .chat_stream_handlers import (
    SSEContext,
    _SSE_HANDLERS,
    _trace_event_payload,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])

_CHECKPOINT_GATE_NODES = {"plan_gate"}
_WORKFLOW_CANCEL_GRACE_SECONDS = 1.0

# Deep Research 可以在单个节点里静默数分钟。没有任何字节写出时，一条仍在工作的流与
# 一条已经死掉的流在 socket 层面完全无法区分（代理/浏览器/运维都只能看到静默），
# 因此空闲这么久就写一帧 SSE 注释保活。注释不是事件：它不携带 data: 行，不进入
# project_sse_payload，也不改变任何事件语义或顺序。
_SSE_HEARTBEAT_SECONDS = 15.0
_SSE_HEARTBEAT_FRAME = ": heartbeat\n\n"


def _extract_interrupt_payload(value: Any) -> Dict[str, Any]:
    """Normalize LangGraph v1 __interrupt__ update payloads for SSE."""
    interrupts = list(value or []) if isinstance(value, (list, tuple)) else [value]
    first = interrupts[0] if interrupts else None
    payload = getattr(first, "value", first)
    if not isinstance(payload, dict):
        payload = {"type": "interrupt", "payload": str(payload)}
    out = dict(payload)
    interrupt_id = getattr(first, "id", None)
    if interrupt_id:
        out["interrupt_id"] = interrupt_id
    return out


_PROJECTION_FAILURE_REASON_CODE = "delivery_integrity_projection_failure"


def _resume_blocked_by_projection_failure(detail: Any) -> bool:
    """Whether this Run failed the public projection, read from durable truth.

    The reason code is composed from the finalizer's own failure class, so the
    audit's ``terminal_attribution`` is where it lands.  ``last_error_code`` is the
    same fact recorded one field wider and is checked as well: the audit is only
    written when a sealed Draft exists, which a finalizer-stage failure always has,
    but nothing in the resume gate should depend on that.
    """

    run = getattr(detail, "run", None)
    if run is not None and getattr(run, "last_error_code", None) == "projection_failure":
        return True
    audit = getattr(getattr(detail, "state", None), "completion_audit", None) or {}
    terminal = audit.get("terminal_attribution") if isinstance(audit, dict) else None
    if not isinstance(terminal, dict):
        return False
    return terminal.get("reason_code") == _PROJECTION_FAILURE_REASON_CODE


def _format_llm_error(e: Exception) -> str:
    """
    将 LLM 提供商异常格式化为用户友好的中文消息。

    openai SDK 抛出的 APIStatusError 将错误体序列化为 str(e)：
      "Error code: 529 - {'type': 'error', 'error': {'type': 'overloaded_error', 'message': '...'}}"
    此函数从中提取内层 message 字段；若无法提取则回退到原始 str(e)。
    """
    raw = str(e)
    # 尝试从 "Error code: NNN - <dict>" 格式中提取 message 字段
    # 匹配 'message': '...' 或 "message": "..."
    m = re.search(r"['\"]message['\"]\s*:\s*['\"]([^'\"]+)['\"]", raw)
    if m:
        inner_msg = m.group(1).strip()
        # 提取 HTTP 状态码用于前缀
        code_m = re.match(r"Error code:\s*(\d+)", raw)
        code = code_m.group(1) if code_m else ""
        prefix = f"[{code}] " if code else ""
        return f"{prefix}{inner_msg}"
    return raw


@router.post(
    "/chat-stream",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat_stream(
    request: ChatRequest,
):
    """
    流式聊天 API（SSE）。
    LangGraph 工作流逐节点 yield 更新，前端实时显示进度。
    事件类型：
      chat_start    - 会话开始
      thinking      - Supervisor 编排状态
      agent_progress - 深度模式 Worker 中间结果（展示在调研过程区，不追加到主消息）
      chat_chunk    - 最终输出 token（仅 synthesizer 或 fast_answer 直接回答）
      chat_complete - 会话完成
      error         - 错误
    """
    components = get_components()
    user_msg = next(
        (m for m in reversed(request.messages) if m.role == "user" and m.content.strip()),
        None,
    )
    if not user_msg:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    user_message = user_msg.content.strip()
    user_message_id = user_msg.message_id or str(uuid.uuid4())
    session_id = request.session_id or str(uuid.uuid4())
    message_id = str(uuid.uuid4())  # assistant message_id (SSE 聚合键)
    if request.route_decision is not None:
        route_decision = request.route_decision
    else:
        try:
            # 模型由 `classify_route` 自己按需取（`get_model_router().get_fast()`）：
            # 界面已经点明路线时它一次都不调，所以这里不许提前把模型实例化出来。
            route_decision = await classify_route(
                user_message,
                explicit_route=request.route,
                has_trip_run=bool(request.run_id),
            )
        except RouteIntentUnavailable as exc:
            # 判不出意图就答不了这句话 —— 分流是回答的前提，不是它的装饰。诚实报错，
            # 不拿一条默认路线冒充判断结果。
            logger.warning("Route intent unavailable | session=%s error=%s", session_id, exc)
            raise HTTPException(status_code=503, detail="暂时无法判断这句话要走哪条处理路径，请重试")
    if route_decision.route == RouteName.TRIP_REFINEMENT and not request.run_id:
        raise HTTPException(status_code=422, detail="调整行程需要已有 TripRun")
    use_deep_research = route_decision.route in {
        RouteName.TRIP_PLANNING,
        RouteName.TRIP_REFINEMENT,
    }
    mode = "deep" if use_deep_research else "fast"
    # 会话标题的唯一输入之一（另一个是 `user_message`）。取**请求携带的**身份而不是
    # 运行身份，因为标题只在会话被创建的那一次派生，而那一次必然没有 `run_id`——
    # 两者在那一刻逐字相同。判据在 `entities/session_title.py`。
    request_trip_identity: Optional[Dict[str, Any]] = (
        request.controlled_trip_identity.model_dump(mode="json")
        if request.controlled_trip_identity
        else None
    )

    def sse_event(payload: Dict[str, Any]) -> str:
        """Encode the single scrubbed product+inspect SSE projection."""
        projected = project_sse_payload(payload)
        # Empty chunks are intentionally used for suppressed internal events;
        # Starlette does not emit an SSE frame for them.
        return _encode_sse_event(projected) if projected is not None else ""

    # Input Guardrail：检测 Prompt 注入
    _guard_result = await check_input_guard(user_message)
    if _guard_result.is_blocked:
        logger.warning(f"InputGuard 拦截请求: {_guard_result.reason} | session={session_id}")
        trip_run = await components.trip_run_store.create_run(
            session_id=session_id,
            user_id=LOCAL_USER_ID,
            mode=mode,
            request_message_id=user_message_id,
            assistant_message_id=message_id,
            resume_policy=TripRunResumePolicy.CLARIFY_ONLY,
        )
        await components.trip_run_store.transition_status(
            trip_run.run_id,
            TripRunStatus.FAILED,
            current_node="input_guard",
            error_code="safety_block",
            error_message="请求未通过安全检查",
            event_type="run.guard_blocked",
            payload={"reason": _guard_result.reason},
        )
        try:
            await components.chat_session_memory.save_turn(
                session_id=session_id,
                user_id=LOCAL_USER_ID,
                mode=mode,
                user_message=user_message,
                user_message_id=user_message_id,
                assistant_message_id=message_id,
                assistant_content=_guard_result.blocked_reply,
                assistant_display_content=_guard_result.blocked_reply,
                assistant_type="guard_blocked",
                task_type="input_guard",
                agent_name="InputGuard",
                step_name="安全检查",
                thinking_steps=[],
                run_id=trip_run.run_id,
                controlled_trip_identity=request_trip_identity,
            )
        except Exception as save_err:
            logger.warning(f"InputGuard 拦截消息保存失败: {save_err}")
        async def _blocked_sse():
            yield sse_event({"type": "chat_start", "message_id": message_id, "run_id": trip_run.run_id, "session_id": session_id})
            yield sse_event({"type": "chat_chunk", "message_id": message_id, "content": _guard_result.blocked_reply, "show_content": _guard_result.blocked_reply})
            yield sse_event({"type": "chat_complete", "message_id": message_id, "run_id": trip_run.run_id, "guard_blocked": True, "run_status": TripRunStatus.FAILED.value, "ts_ms": run_ts_ms()})
        return StreamingResponse(_blocked_sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    if route_decision.requires_confirmation and request.route_decision is None and request.route is None:
        async def _route_confirmation_sse():
            yield sse_event({
                "type": "route_confirmation",
                "message_id": message_id,
                "session_id": session_id,
                "route_decision": route_decision.model_dump(mode="json"),
            })
            yield sse_event({"type": "chat_complete", "message_id": message_id})
        return StreamingResponse(_route_confirmation_sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    if (
        request.run_id is None
        and route_decision.route == RouteName.TRIP_PLANNING
        and request.controlled_trip_identity is None
    ):
        guided = request.guided_intake.model_dump(mode="json") if request.guided_intake else {
            "raw_input": user_message,
            "route_decision": route_decision.model_dump(mode="json"),
            "controlled_identity": None,
            "missing_fields": ["origin", "destinations", "dates", "party", "style"],
            "ready_to_create": False,
        }
        async def _guided_intake_sse():
            yield sse_event({
                "type": "guided_intake",
                "message_id": message_id,
                "session_id": session_id,
                "guided_intake": guided,
            })
            yield sse_event({"type": "chat_complete", "message_id": message_id})
        return StreamingResponse(_guided_intake_sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # Validate preset before TripRun side effects so missing style is 404 without orphan runs.
    preset_context, preset_pack_constraints = await load_preset_context(
        request.preset_id, LOCAL_USER_ID
    )
    trip_run_store = components.trip_run_store
    authoritative_controlled_trip_identity: Dict[str, Any] = {}
    cost_ledger_store = getattr(components, "cost_ledger_store", None)
    usage_recorder = getattr(components, "usage_recorder", None)
    checkpoint_resume = False
    resume_payload: Optional[Dict[str, Any]] = None
    safe_checkpoint_id: Optional[str] = None
    lease_keeper: Optional[RunLeaseKeeper] = None
    gate_resume = request.gate_decision is not None

    async def claim_execution_lease(
        run_id: str,
        *,
        checkpoint_id: Optional[str] = None,
    ) -> RunLeaseKeeper:
        """抢下这个 Run 的执行权。抢不到就不开始 —— 两个执行器同时跑一个 Run 会写出
        两份互相覆盖的状态，而用户看到的是随机一份。"""

        lease_settings = get_settings().run_control
        keeper = RunLeaseKeeper(
            components.run_execution_store,
            run_id,
            lease_seconds=lease_settings.lease_seconds,
            heartbeat_seconds=lease_settings.lease_heartbeat_seconds,
            failure_threshold=lease_settings.lease_heartbeat_failure_threshold,
            # 失去租约只是「停止发起新的外部调用」的信号，终态归属由 stop reason 决定。
            on_lease_lost=lambda reason: run_control_registry.request_stop(
                run_id, "lease_lost"
            ),
        )
        if not await keeper.claim(last_safe_checkpoint_id=checkpoint_id):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "run_already_executing",
                    "message": "这次运行正在别处执行，请刷新后查看当前状态。",
                },
            )
        return keeper

    if gate_resume and not request.run_id:
        raise HTTPException(status_code=400, detail="gate_decision 需要提供 run_id")
    if request.plan_gate is True and use_deep_research and getattr(components, "checkpointer", None) is None:
        raise HTTPException(status_code=409, detail="当前服务未启用 checkpoint，无法使用计划审批门")
    if request.run_id:
        detail = await trip_run_store.get_detail(request.run_id, event_limit=1)
        if detail is None:
            raise HTTPException(status_code=404, detail="TripRun 不存在")
        authoritative_controlled_trip_identity = dict(
            detail.run.controlled_trip_identity or {}
        )
        identity_guard_text = (
            request.gate_decision.content
            if request.gate_decision is not None
            and request.gate_decision.action in {"edit", "supplement"}
            else ("" if request.gate_decision is not None else user_message)
        )
        if classify_locked_identity_intent(identity_guard_text).classification == "change_requested":
            raise HTTPException(status_code=409, detail="基础旅行身份已锁定。请结束当前草稿或运行，再新建旅行。")
        if detail.run.status not in {
            TripRunStatus.CREATED,
            TripRunStatus.AWAITING_INPUT,
            TripRunStatus.FAILED,
            TripRunStatus.INTERRUPTED,
        }:
            raise HTTPException(status_code=409, detail=f"TripRun 当前状态不可继续: {detail.run.status.value}")
        trip_run = detail.run
        gate_node = detail.run.current_node if detail.run.current_node in _CHECKPOINT_GATE_NODES else None
        if (
            use_deep_research
            and detail.run.status == TripRunStatus.AWAITING_INPUT
            and detail.run.resume_policy == TripRunResumePolicy.CHECKPOINT
            and gate_node
            and not gate_resume
        ):
            raise HTTPException(status_code=409, detail="TripRun 正在等待审批门决策，需携带 gate_decision 断点续跑")
        if gate_resume and not gate_node:
            raise HTTPException(status_code=409, detail="TripRun 当前节点不是可提交审批决策的断点")

        checkpoint_resume = (
            use_deep_research
            and detail.run.resume_policy == TripRunResumePolicy.CHECKPOINT
            and (
                detail.run.status in {TripRunStatus.FAILED, TripRunStatus.INTERRUPTED}
                or (detail.run.status == TripRunStatus.AWAITING_INPUT and gate_resume)
            )
        )
        if checkpoint_resume:
            # A projection failure is deterministic: the public projection reads
            # only the Bundle and touches no clock, provider or randomness, so a
            # resume recomputes the same refusal and fails the same way.  Refuse
            # here instead of spending a whole research window rediscovering it.
            # Checked before the checkpoint probes because the answer does not
            # depend on them.
            if _resume_blocked_by_projection_failure(detail):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "delivery_integrity_projection_failure",
                        "message": "这次运行的结果无法生成可展示的方案，断点续跑会得到同一个结果。请重新规划这趟旅行。",
                    },
                )
            if getattr(components, "checkpointer", None) is None:
                raise HTTPException(status_code=409, detail="当前服务未启用 checkpoint，无法断点续跑")
            # 恢复扫描已经判定过「这个 run 没有可用断点」时，不要再走一遍探测得到同一个
            # 答案 —— 它的判定就是为了让用户看到一句确定的话，而不是一次重试。
            recovery_execution = await components.run_execution_store.get(trip_run.run_id)
            if recovery_execution is not None and recovery_execution.recovery_status in {
                RunRecoveryStatus.NON_RESUMABLE,
                RunRecoveryStatus.RECOVERY_CONTRACT_FAILURE,
            }:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "run_not_resumable",
                        "message": "上次运行没有可继续的检查点，请重新规划这趟旅行。",
                    },
                )
            probe_checkpoint = getattr(components.travel_workflow, "probe_checkpoint", None)
            try:
                checkpoint_available, safe_checkpoint_id = (
                    await probe_checkpoint(trip_run.run_id)
                    if probe_checkpoint is not None
                    else (False, None)
                )
            except CheckpointContractError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "checkpoint_contract_mismatch",
                        "message": str(exc),
                    },
                ) from exc
            if not checkpoint_available:
                raise HTTPException(status_code=409, detail="未找到该 TripRun 的 checkpoint，无法断点续跑")
            # plan gate resume：把决策映射为 Command(resume=payload)，由被中断的门节点消费。
            if request.gate_decision is not None:
                resume_payload = request.gate_decision.model_dump()
            if gate_resume:
                allowed_statuses = [TripRunStatus.AWAITING_INPUT]
                claim_node = gate_node or "plan_gate"
            else:
                allowed_statuses = [TripRunStatus.FAILED, TripRunStatus.INTERRUPTED]
                claim_node = None
            # 租约先于状态写：状态一旦变成 RUNNING 而租约却抢不到，这个 Run 就落在
            # 「显示在跑、没人在跑」的缝里，只能等下一轮恢复扫描捞回来。
            lease_keeper = await claim_execution_lease(
                trip_run.run_id, checkpoint_id=safe_checkpoint_id
            )
            claimed = await trip_run_store.claim_checkpoint_resume(
                trip_run.run_id,
                allowed_statuses=allowed_statuses,
                current_node=claim_node,
                payload={
                    "checkpoint_thread_id": trip_run.run_id,
                    **(
                        {"gate": gate_node or "plan", "gate_decision": resume_payload}
                        if gate_resume
                        else {}
                    ),
                },
            )
            if claimed is None:
                await lease_keeper.release(reason="resume_lost_status_race")
                raise HTTPException(status_code=409, detail="TripRun 已被其他请求恢复或状态已变化")
            trip_run = claimed
        elif gate_resume:
            raise HTTPException(status_code=409, detail="TripRun 当前状态不可提交计划审批决策")
    else:
        authoritative_controlled_trip_identity = (
            request.controlled_trip_identity.model_dump(mode="json")
            if request.controlled_trip_identity
            else {}
        )
        trip_run = await trip_run_store.create_run(
            session_id=session_id,
            user_id=LOCAL_USER_ID,
            mode=mode,
            request_message_id=user_message_id,
            assistant_message_id=message_id,
            resume_policy=(
                TripRunResumePolicy.CHECKPOINT
                if use_deep_research and getattr(components, "checkpointer", None) is not None
                else TripRunResumePolicy.CLARIFY_ONLY
            ),
            controlled_trip_identity=authoritative_controlled_trip_identity or None,
            route_decision=route_decision.model_dump(mode="json"),
        )
    if lease_keeper is None:
        lease_keeper = await claim_execution_lease(trip_run.run_id)
    if checkpoint_resume:
        history, session_anchor_data, session_compressed = [], None, False
    else:
        history, session_anchor_data, session_compressed = await load_session_history(
            components.chat_session_memory, LOCAL_USER_ID, session_id, mode, user_message,
            request_trip_identity,
            load_anchor=True,
        )

    async def generate_sse() -> AsyncGenerator[str, None]:
        # event_queue 汇聚两类事件：
        #   ("token", node_name, chunk)  —— Fast Answer 或过程草稿的临时 token
        #   ("state", node_name, update) —— 节点完成时的 state update（由 run_workflow 推送）
        #   ("state_snapshot", values)   —— reducer 合并后的全量 state（仅内部持久化）
        #   ("node_lifecycle", payload)  —— 节点执行边界（内部持久化，不直接作为产品 SSE）
        #   ("error", exception)
        #   ("done",)
        event_queue: asyncio.Queue = asyncio.Queue()

        _stream_profile = await load_user_profile(
            components.user_profile_memory, LOCAL_USER_ID,
        )

        workflow = components.travel_workflow if use_deep_research else components.fast_workflow
        # 单调钟锚点：建在 generate_sse 协程上下文里，run_workflow 任务经 create_task 复制
        # context 继承同一锚点。之后所有 ts_ms 均相对此锚点取值（步边界事件在逻辑发生处取，
        # 节点/工具计时在各自 wrapper/循环里取），不在队列 drain 处取值。
        set_run_ts_anchor()
        ctx = SSEContext(
            message_id=message_id,
            session_id=session_id,
            mode=mode,
            use_deep_research=use_deep_research,
            trace_run_id=trip_run.run_id,
        )
        persisted = False
        delivery_ready_emitted = False
        run_terminal_emitted = False

        async def persist_partial_turn(assistant_type: str) -> str:
            nonlocal persisted
            assistant_content = strip_non_display_blocks(
                strip_thinking_text(strip_think_blocks(ctx.full_response))
            )
            if not persisted:
                await components.chat_session_memory.save_turn(
                    session_id=session_id,
                    user_id=LOCAL_USER_ID,
                    mode=mode,
                    user_message=user_message,
                    user_message_id=user_message_id,
                    assistant_message_id=message_id,
                    assistant_content=assistant_content,
                    assistant_display_content=assistant_content,
                    assistant_type="normal",
                    task_type=ctx.task_type or None,
                    agent_name=ctx.final_agent_name,
                    step_name=ctx.final_step_name,
                    thinking_steps=ctx.thinking_steps,
                    context_report=ctx.context_report,
                    context_compaction_event=ctx.context_compaction_event,
                    citations=list(ctx.final_grounding.get("citations") or []),
                    annotations=list(ctx.final_grounding.get("annotations") or []),
                    trip_summary_card=ctx.trip_summary_card,
                    run_id=trip_run.run_id,
                    controlled_trip_identity=request_trip_identity,
                )
                persisted = True
            return assistant_content

        async def flush_usage_updates() -> List[Dict[str, Any]]:
            """周期性 drain 捕获层 → 算成本落库 → 返回本 run 的 usage_update SSE 事件。

            recorder 是进程单例、并发 run 共用：drain 出的记录按各自 run_id 落库（都持久化），
            但只有本 run 的记录发 usage_update（audit-safe：只有计数与成本，无内容）。
            """
            if cost_ledger_store is None or usage_recorder is None:
                return []
            batch = usage_recorder.drain()
            if not batch:
                return []
            try:
                ledger = await cost_ledger_store.record_calls(batch)
            except Exception as exc:
                # 落库失败：回放进缓冲等待重试（record_calls 幂等），本轮不发 usage_update；
                # 终态 finalize 会重试并如实上报，绝不静默吞账（CB-02）。
                usage_recorder.requeue(batch)
                logger.warning(f"usage 落库失败（已回放 {len(batch)} 条待重试）: {exc}")
                return []
            events: List[Dict[str, Any]] = []
            for call in ledger:
                if call.run_id != trip_run.run_id:
                    continue
                # What a live cost ledger needs, and nothing else.  ``tier`` /
                # ``provider`` / ``model`` / ``cached_input_tokens`` /
                # ``latency_ms`` / ``ttft_ms`` are not part of the client frame:
                # the run-level ledger is what says "cost and observability" to a
                # user, not a per-call **attribution** surface.  Every one of
                # those six is still captured per call and still lands in
                # ``run_llm_calls`` — internal observability keeps them; the
                # client stopped being told.
                #
                # ``node`` / ``agent`` stay because the live ledger prints which
                # step is currently spending (``CostLedger`` ``activeLabel``).
                # Which of ``run_cost_summary``'s fields reach a screen is decided
                # in ``frontend/src/lib/costLedger.ts`` — not here.
                events.append({
                    "type": "usage_update",
                    "message_id": message_id,
                    "run_id": trip_run.run_id,
                    "node": call.node,
                    "agent": call.agent,
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "total_tokens": (call.input_tokens or 0) + (call.output_tokens or 0),
                    "cost_usd": call.cost_usd,
                    "estimated": call.estimated,
                })
            return events

        async def finalize_cost_summary(*, terminal: bool) -> Optional[Dict[str, Any]]:
            """终结时兜底 drain + 落库，返回 run 级成本汇总。

            终态按落库真实结果分流事件（CB-02）：全部落库成功发 run.cost_recorded；有本 run 记录
            落库失败则回放待重试 + 发 run.cost_record_failed（携 record_failed 计数），事件流不再撒谎。
            record_failed 一并随 run_cost_summary 下发供前端诚实呈现。
            """
            if cost_ledger_store is None:
                return None
            record_failed = 0
            record_error: Optional[str] = None
            if usage_recorder is not None:
                batch = usage_recorder.drain()
                if batch:
                    try:
                        await cost_ledger_store.record_calls(batch)
                    except Exception as exc:
                        # 回放进缓冲等待后续重试（record_calls 幂等；transient 故障可自愈），
                        # 并按本 run 记录数如实计数——只统计本 run 的失败，其余 run 由各自终态上报。
                        usage_recorder.requeue(batch)
                        record_failed = sum(1 for rec in batch if rec.run_id == trip_run.run_id)
                        record_error = exc.__class__.__name__
                        logger.warning(
                            f"usage 终结落库失败（已回放 {len(batch)} 条待重试，本 run {record_failed} 条）: {exc}"
                        )
            try:
                summary = await cost_ledger_store.run_summary(trip_run.run_id)
            except Exception as exc:
                logger.warning(f"run 成本汇总失败: {exc}")
                return None
            # 回填 Tool Search 上下文节省量（deferred 曝光实测；无 worker 走按需
            # 曝光路径时为 None，与预留字段一致）。
            try:
                summary["tool_context_saving"] = get_tool_exposure_ledger().summary(trip_run.run_id)
            except Exception as exc:
                logger.debug(f"tool_context_saving 汇总失败（不影响主流）: {exc}")
            summary["record_failed"] = record_failed
            if terminal:
                if record_failed:
                    event_type = "run.cost_record_failed"
                    payload = {**cost_event_summary(summary), "record_failed": record_failed, "error": record_error}
                else:
                    event_type = "run.cost_recorded"
                    payload = cost_event_summary(summary)
                try:
                    await trip_run_store.append_event(trip_run.run_id, event_type, payload)
                except Exception as exc:
                    logger.warning(f"{event_type} 事件写入失败: {exc}")
            return summary

        async def load_durable_completed_delivery():
            """Recover the one immutable delivery after post-commit bookkeeping fails."""
            if not ctx.use_deep_research:
                return None
            current_run = await trip_run_store.get_run(trip_run.run_id)
            current_bundle = await components.delivery_bundle_store.get_current(
                trip_run.run_id
            )
            if (
                current_run is None
                or current_run.status != TripRunStatus.COMPLETED
                or current_bundle is None
            ):
                return None
            bundle_id = current_bundle.manifest.bundle_id
            durable_events = await trip_run_store.list_delivery_events(
                trip_run.run_id,
                bundle_id,
            )
            ready_events = [
                event
                for event in durable_events
                if event.event_type == "delivery.ready"
                and event.payload.get("bundle_id") == bundle_id
            ]
            terminal_events = [
                event
                for event in durable_events
                if event.event_type == "run.terminal"
                and event.payload.get("bundle_id") == bundle_id
                and event.payload.get("status") == TripRunStatus.COMPLETED.value
            ]
            if len(ready_events) != 1 or len(terminal_events) != 1:
                raise RuntimeError(
                    "completed deep research delivery events are missing or duplicated"
                )
            ready_event = ready_events[0]
            terminal_event = terminal_events[0]
            if ready_event.sequence >= terminal_event.sequence:
                raise RuntimeError(
                    "completed deep research delivery events are out of order"
                )
            return current_bundle, ready_event, terminal_event

        async def transition_to_awaiting_input(
            *,
            current_node: str,
            pending_user_choice: Dict[str, Any],
            event_type: str,
            payload: Dict[str, Any],
            terminal_reason_code: Optional[str] = None,
            terminal_gate_class: Optional[str] = None,
        ) -> None:
            try:
                await trip_run_store.transition_status(
                    trip_run.run_id,
                    TripRunStatus.AWAITING_INPUT,
                    current_node=current_node,
                    pending_user_choice=pending_user_choice,
                    event_type=event_type,
                    payload=payload,
                    terminal_reason_code=terminal_reason_code,
                    terminal_gate_class=terminal_gate_class,
                )
            except ValueError:
                current_run = await trip_run_store.get_run(trip_run.run_id)
                if current_run and current_run.status in {
                    TripRunStatus.CANCEL_REQUESTED,
                    TripRunStatus.CANCELLED,
                }:
                    raise RunCancelled(trip_run.run_id, current_node)
                raise

        async def run_workflow() -> None:
            """在后台任务中运行工作流，将 state events 写入 event_queue。"""
            try:
                # 这个 dict 同时喂两个工作流，所以**只放两边都接的键**；
                # 一边独有的键写在下面那个分支里。往这里加一个只有深度图接的键，
                # 快问快答的每一次请求都会当场 TypeError（实测：整条快路径挂了一整轮）。
                workflow_kwargs = {
                    "user_message": user_message,
                    "session_id": session_id,
                    "user_id": LOCAL_USER_ID,
                    "selected_mcp_servers": request.selected_mcp_servers,
                    "conversation_history": history,
                    "stream_queue": event_queue,
                    "session_anchor": session_anchor_data,
                    "session_compressed": session_compressed,
                    "preset_context": preset_context,
                    # preset 里归 Constraint Pack 执行的那几项（节奏 / 预算）。两条路径
                    # 现在都装 pack，所以这个键回到共享的这一半 —— 它属于哪一半由「两个
                    # 工作流接不接得住」决定。
                    "preset_pack_constraints": preset_pack_constraints,
                    "run_id": trip_run.run_id,
                    "route_decision": route_decision.model_dump(mode="json"),
                }
                if use_deep_research:
                    workflow_kwargs["resume_from_checkpoint"] = checkpoint_resume
                    workflow_kwargs["resume_payload"] = resume_payload
                    workflow_kwargs["plan_gate_enabled"] = request.plan_gate
                    workflow_kwargs["controlled_trip_identity"] = (
                        authoritative_controlled_trip_identity
                    )
                async for state_event in workflow.astream(
                    **workflow_kwargs,
                ):
                    for node_name, state_update in state_event.items():
                        if node_name == "__interrupt__":
                            await event_queue.put(("gate", _extract_interrupt_payload(state_update)))
                            continue
                        await event_queue.put(("state", node_name, state_update))
            except Exception as exc:  # 工作流顶层边界：任何未预期异常都应转为 error 事件而非崩溃
                await event_queue.put(("error", exc))
            finally:
                await event_queue.put(("done",))

        workflow_task: Optional[asyncio.Task] = None
        control_registered = False
        command_coordinator: Optional[RunCommandCoordinator] = None
        process_cleanup_done = False
        stream_exit_reason = "stream_closed_before_terminal"

        async def converge_stream_exit(reason: str) -> None:
            """Ensure an abandoned response cannot leave an active TripRun behind."""
            for _ in range(3):
                current_run = await trip_run_store.get_run(trip_run.run_id)
                if current_run is None:
                    return
                if current_run.status == TripRunStatus.AWAITING_INPUT:
                    try:
                        await persist_partial_turn("waiting_input")
                    except Exception as save_err:
                        logger.warning(f"等待态会话保存失败: {save_err}")
                    return
                if current_run.status not in {
                    TripRunStatus.RUNNING,
                    TripRunStatus.CANCEL_REQUESTED,
                }:
                    return
                current_node_name = (
                    current_run.current_node
                    or ctx.final_step_name
                    or ctx.final_agent_name
                    or "workflow"
                )
                target = (
                    TripRunStatus.CANCELLED
                    if current_run.status == TripRunStatus.CANCEL_REQUESTED
                    else TripRunStatus.INTERRUPTED
                )
                try:
                    await trip_run_store.transition_status(
                        trip_run.run_id,
                        target,
                        current_node=current_node_name,
                        event_type=(
                            "run.cancelled"
                            if target == TripRunStatus.CANCELLED
                            else "run.interrupted"
                        ),
                        payload={
                            "reason": reason,
                            "current_node": current_node_name,
                        },
                    )
                    try:
                        await persist_partial_turn("interrupted")
                    except Exception as save_err:
                        logger.warning(f"断连会话保存失败: {save_err}")
                    return
                except ValueError:
                    # cancel/awaiting/terminal may have won after the read; re-read durable truth.
                    continue
            current_run = await trip_run_store.get_run(trip_run.run_id)
            if current_run and current_run.status in {
                TripRunStatus.RUNNING,
                TripRunStatus.CANCEL_REQUESTED,
            }:
                raise RuntimeError(f"TripRun stream exit did not converge: {trip_run.run_id}")

        def log_cleanup_result(task: asyncio.Task) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                logger.error("TripRun stream-exit cleanup task was cancelled")
            except Exception as cleanup_err:
                logger.error(f"TripRun stream-exit cleanup failed: {cleanup_err}", exc_info=True)

        def clear_process_run_state() -> None:
            nonlocal process_cleanup_done
            if process_cleanup_done:
                return
            process_cleanup_done = True
            if control_registered:
                run_control_registry.unregister(trip_run.run_id)
            # 回收本 run 的工具曝光计量（进程内瞬态，汇总已在 finalize 时读走）。
            get_tool_exposure_ledger().clear(trip_run.run_id)
            # 回收本 run 未被 trace 读走的节点计时（interrupt 的门节点等；进程内瞬态）。
            node_timing_registry.clear(trip_run.run_id)

        def finish_detached_workflow(task: asyncio.Task) -> None:
            try:
                task.result()
                logger.info(
                    "workflow_detached_after_cancel completed run_id=%s",
                    trip_run.run_id,
                )
            except asyncio.CancelledError:
                logger.info(
                    "workflow_detached_after_cancel cancelled run_id=%s",
                    trip_run.run_id,
                )
            except Exception as workflow_err:
                logger.warning(
                    "workflow_detached_after_cancel error run_id=%s error=%s",
                    trip_run.run_id,
                    workflow_err,
                )
            finally:
                clear_process_run_state()

        async def mark_safe_boundary() -> None:
            """把一个已经落盘的 checkpoint 记成安全边界。

            LLM 调用刚开始时不算 —— 那时还没有任何东西被持久化。
            """
            probe = getattr(components.travel_workflow, "probe_checkpoint", None)
            if probe is None or not use_deep_research:
                return
            try:
                _available, checkpoint_id = await probe(trip_run.run_id)
            except Exception as probe_err:
                logger.warning(
                    "安全边界探测失败 run_id=%s error=%s", trip_run.run_id, probe_err
                )
                return
            if checkpoint_id:
                await lease_keeper.mark_safe_checkpoint(checkpoint_id)

        async def settle_run_commands() -> None:
            """给这个 Run 留下的控制命令一个结论。

            **不许留成永远 pending**：一条没人再消费的追加要求，回执接口会一直答「还在等」。
            结论按 durable 状态给，不按这次响应经历了什么。
            """

            if command_coordinator is None:
                return
            try:
                current_run = await trip_run_store.get_run(trip_run.run_id)
                if current_run is None or current_run.status in {
                    TripRunStatus.RUNNING,
                    TripRunStatus.AWAITING_INPUT,
                }:
                    # 运行还没结束（等待审批门 / 收敛失败）：命令留着，下一段执行或恢复扫描
                    # 会看到它们。
                    return
                await command_coordinator.settle_terminal(current_run.status)
            except Exception as settle_err:
                logger.warning(
                    "运行控制命令收口失败 run_id=%s error=%s",
                    trip_run.run_id,
                    settle_err,
                )

        async def cleanup_stream_exit(reason: str) -> None:
            """Persist exit truth before bounded best-effort workflow shutdown."""
            if workflow_task is not None and not workflow_task.done() and control_registered:
                handle = run_control_registry.get(trip_run.run_id)
                if handle is not None:
                    # This is only an execution-stop signal. Durable status below
                    # remains INTERRUPTED for disconnects and CANCELLED only for
                    # an explicit user cancellation.
                    handle.request_stop("stream_exit")

            try:
                await converge_stream_exit(reason)
            except Exception as convergence_err:
                # Still stop the in-process workflow and release its handle;
                # durable convergence failure remains loud in server logs.
                logger.error(
                    "TripRun durable stream-exit convergence failed run_id=%s error=%s",
                    trip_run.run_id,
                    convergence_err,
                    exc_info=True,
                )

            await settle_run_commands()
            if command_coordinator is not None:
                await command_coordinator.stop()

            # 状态已经收敛，租约再没有工作可做。交还它 —— 否则「继续」按钮要等一个
            # 已经没人在用的租约自然过期才点得动。
            await lease_keeper.release(reason=reason)

            if workflow_task is not None and not workflow_task.done():
                workflow_task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(workflow_task),
                        timeout=_WORKFLOW_CANCEL_GRACE_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # Durable status already converged; process may still
                    # run briefly until the next cooperative cancel boundary.
                    logger.warning(
                        "workflow_detached_after_cancel run_id=%s grace_s=%s "
                        "reason=%s (detached task may still consume tools/LLM "
                        "until node boundary; durable TripRun already INTERRUPTED/CANCELLED)",
                        trip_run.run_id,
                        _WORKFLOW_CANCEL_GRACE_SECONDS,
                        reason,
                    )
                    workflow_task.add_done_callback(finish_detached_workflow)
                    return
                except asyncio.CancelledError:
                    pass
                except Exception as workflow_err:
                    logger.warning(
                        "Workflow shutdown failed run_id=%s error=%s",
                        trip_run.run_id,
                        workflow_err,
                    )
            clear_process_run_state()

        try:
            # 快慢两条路径都注册：控制命令的消费者不该取决于这次是深度规划还是快问快答。
            # 两条路径的节点都走 `with_run_control`，所以协作边界的语义也是同一份。
            control_handle = run_control_registry.register(trip_run.run_id)
            control_registered = True
            command_coordinator = RunCommandCoordinator(
                components.run_command_store,
                trip_run_store,
                trip_run.run_id,
                control_handle,
                poll_seconds=get_settings().run_control.command_poll_seconds,
            )
            # 先消费一次再开始：上一个进程死掉前落库的命令必须在这次执行的第一个边界就生效，
            # 而不是等一个轮询周期。
            await command_coordinator.poll_once()
            command_coordinator.start()
            # 心跳与 SSE 客户端是否在线无关：它只说明执行器这个进程还活着。
            lease_keeper.start()
            if not checkpoint_resume:
                try:
                    await trip_run_store.transition_status(
                        trip_run.run_id,
                        TripRunStatus.RUNNING,
                        event_type="run.started",
                        payload={"mode": mode, "checkpoint_thread_id": trip_run.run_id},
                    )
                except ValueError:
                    # A stop can land between the resume gate's read and this
                    # write.  Re-read and name what happened: reported as a
                    # generic failure, this looked like the workflow breaking
                    # rather than the traveller stopping it.  Same shape as
                    # ``transition_to_awaiting_input``.
                    current_run = await trip_run_store.get_run(trip_run.run_id)
                    if current_run and current_run.status in {
                        TripRunStatus.CANCEL_REQUESTED,
                        TripRunStatus.CANCELLED,
                    }:
                        raise RunCancelled(trip_run.run_id, "workflow")
                    raise
            yield sse_event({
                "type": "chat_start",
                "message_id": message_id,
                "run_id": trip_run.run_id,
                "session_id": session_id,
                # `route_decision` is what this frame is for on the client side.
                # Resume bookkeeping (`resumed` / `attempt` / `checkpoint_thread_id`)
                # is **not** sent: no client ever read it, and the authoritative
                # record of "this run was resumed, attempt N" is the TripRun row
                # plus the `run.checkpoint_resumed` durable event.
                "route_decision": route_decision.model_dump(mode="json"),
            })
            workflow_task = asyncio.create_task(run_workflow())

            # 主循环：kind 分发到对应 handler
            while True:
                try:
                    item = await asyncio.wait_for(
                        event_queue.get(), _SSE_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # 单消费者场景下超时取消 Queue.get() 不会丢事件：get() 只在自身
                    # 协程里 get_nowait()，被取消时该调用尚未发生，事件仍留在队列中，
                    # 下一轮立即取到。保活帧不参与事件流，continue 后控制流不变。
                    yield _SSE_HEARTBEAT_FRAME
                    continue
                kind = item[0]

                if kind == "done":
                    break
                if kind == "error":
                    raise item[1]
                if kind == "node_lifecycle":
                    _, payload = item
                    await trip_run_store.record_node_lifecycle(
                        trip_run.run_id,
                        node=str(payload.get("node") or ""),
                        status=str(payload.get("status") or ""),
                        payload=payload,
                    )
                    continue
                if kind == "state_snapshot":
                    _, state_snapshot = item
                    await trip_run_store.record_state_snapshot(
                        trip_run.run_id,
                        state_snapshot=state_snapshot,
                    )
                    continue
                if kind == "gate":
                    _, payload = item
                    # `gate` is the payload's own name for itself and is always
                    # present (`_build_plan_gate_payload`).
                    gate = str(payload.get("gate") or "approval")
                    if gate != "plan":
                        raise RuntimeError(f"unsupported workflow gate: {gate}")
                    gate_node = "plan_gate"
                    ctx.approval_gate = payload
                    await transition_to_awaiting_input(
                        current_node=gate_node,
                        pending_user_choice={
                            "type": "approval_gate",
                            "gate": gate,
                            "payload": payload,
                        },
                        event_type="run.gate_raised",
                        payload={
                            "gate": gate,
                            "payload": payload,
                        },
                    )
                    # interrupt 已经落盘：这是一个真正的安全边界。
                    await mark_safe_boundary()
                    yield sse_event({
                        "type": "approval_gate_raised",
                        "message_id": message_id,
                        "run_id": trip_run.run_id,
                        "gate": gate,
                        "payload": payload,
                        "run_status": TripRunStatus.AWAITING_INPUT.value,
                        "ts_ms": run_ts_ms(),
                    })
                    continue

                handler = _SSE_HANDLERS.get(kind)
                if handler is None:
                    logger.warning(f"未知 SSE 事件 kind: {kind!r}")
                    continue
                if kind == "state":
                    _, node_name, state_update = item
                    if node_name == "__final_state__":
                        await trip_run_store.record_state_snapshot(
                            trip_run.run_id,
                            state_snapshot=state_update,
                        )
                    else:
                        await trip_run_store.record_node_update(
                            trip_run.run_id,
                            node=node_name,
                            trace_event_count=len(ctx.trace_events),
                        )
                async for ev in handler(item, ctx):
                    if ev.get("type") == "trace_event":
                        await trip_run_store.append_event(
                            trip_run.run_id,
                            "trace.event",
                            _trace_event_payload(ev),
                        )
                    yield sse_event(ev)
                # drain 加密（§5.3）：除节点边界外，工具边界（tool_done）与 ReAct 单轮 LLM
                # 调用结束（tool_start——该轮已决定调工具，LLM 记录已入缓冲）后即时 drain，
                # 让 usage_update 在每次 LLM 调用落账后尽快下发，不引入忙轮询/定时器线程。
                if kind in ("state", "tool_done", "tool_start"):
                    for usage_ev in await flush_usage_updates():
                        yield sse_event(usage_ev)

            current_run = await trip_run_store.get_run(trip_run.run_id)
            if current_run and current_run.status == TripRunStatus.CANCEL_REQUESTED:
                raise RunCancelled(
                    trip_run.run_id,
                    ctx.final_step_name or ctx.final_agent_name or "workflow",
                )
            if ctx.approval_gate:
                assistant_content = await persist_partial_turn("waiting_input")
                cost_summary = await finalize_cost_summary(terminal=False)
                yield sse_event({
                    "type": "chat_complete",
                    "message_id": message_id,
                    "run_id": trip_run.run_id,
                    "run_status": TripRunStatus.AWAITING_INPUT.value,
                    "final_content": assistant_content,
                    "run_cost_summary": cost_summary,
                    "ts_ms": run_ts_ms(),
                })
                return

            assistant_content = strip_non_display_blocks(
                strip_thinking_text(strip_think_blocks(ctx.full_response))
            )
            assistant_type = "normal"
            deep_delivery_bundle = None
            if ctx.use_deep_research:
                deep_delivery_bundle = await components.delivery_bundle_store.get_current(
                    trip_run.run_id
                )

            await components.chat_session_memory.save_turn(
                session_id=session_id,
                user_id=LOCAL_USER_ID,
                mode=mode,
                user_message=user_message,
                user_message_id=user_message_id,
                assistant_message_id=message_id,
                assistant_content=assistant_content,
                assistant_display_content=assistant_content,
                assistant_type=assistant_type,
                task_type=ctx.task_type or None,
                agent_name=ctx.final_agent_name,
                step_name=ctx.final_step_name,
                thinking_steps=ctx.thinking_steps,
                context_report=ctx.context_report,
                context_compaction_event=ctx.context_compaction_event,
                citations=list(ctx.final_grounding.get("citations") or []),
                annotations=list(ctx.final_grounding.get("annotations") or []),
                trip_summary_card=ctx.trip_summary_card,
                run_id=trip_run.run_id,
                controlled_trip_identity=request_trip_identity,
            )
            persisted = True

            await enqueue_memory_extraction(
                user_id=LOCAL_USER_ID,
                session_id=session_id,
                user_message=user_message,
                user_message_id=user_message_id,
                assistant_message_id=message_id,
                profile_revision=_stream_profile.revision if _stream_profile else 0,
                portrait_baseline=_stream_profile.auto_portrait if _stream_profile else "",
                background_job_worker=components.background_job_worker,
            )

            current_bundle = None
            if ctx.use_deep_research:
                current_bundle = deep_delivery_bundle
                current_run = await trip_run_store.get_run(trip_run.run_id)
                if current_bundle is None:
                    raise RuntimeError("deep research ended without a persisted Delivery Bundle")
                if current_run is None or current_run.status != TripRunStatus.COMPLETED:
                    raise RuntimeError("deep research ended before durable TripRun completion")
                if (
                    ctx.delivery_bundle_id
                    and current_bundle.manifest.bundle_id != ctx.delivery_bundle_id
                ):
                    raise RuntimeError("workflow delivery differs from the current persisted Bundle")
                ctx.delivery_bundle_id = current_bundle.manifest.bundle_id
                ctx.delivery_manifest = public_event_manifest(
                    current_bundle.manifest.model_dump(mode="json")
                )
            else:
                try:
                    await trip_run_store.transition_status(
                        trip_run.run_id,
                        TripRunStatus.COMPLETED,
                        current_node="workflow",
                        event_type="run.completed",
                        payload={"trace_event_count": len(ctx.trace_events)},
                    )
                except ValueError:
                    # Same race as the RUNNING write, at the other end of the
                    # stream: a stop can land while the answer is being finished.
                    # Unguarded this reported the whole turn as a failure, which
                    # is neither what the record says nor what the traveller did.
                    current_run = await trip_run_store.get_run(trip_run.run_id)
                    if current_run and current_run.status in {
                        TripRunStatus.CANCEL_REQUESTED,
                        TripRunStatus.CANCELLED,
                    }:
                        raise RunCancelled(trip_run.run_id, "workflow")
                    raise
            run_status = TripRunStatus.COMPLETED.value

            # 收口即终态 COMPLETED（落 run.cost_recorded）。
            cost_summary = await finalize_cost_summary(terminal=True)

            if ctx.use_deep_research:
                durable_events = await trip_run_store.list_events(
                    trip_run.run_id,
                    after_sequence=0,
                    limit=500,
                )
                ready_events = [
                    event
                    for event in durable_events
                    if event.event_type == "delivery.ready"
                    and event.payload.get("bundle_id") == ctx.delivery_bundle_id
                ]
                terminal_events = [
                    event
                    for event in durable_events
                    if event.event_type == "run.terminal"
                    and event.payload.get("bundle_id") == ctx.delivery_bundle_id
                    and event.payload.get("status") == TripRunStatus.COMPLETED.value
                ]
                if len(ready_events) != 1 or len(terminal_events) != 1:
                    raise RuntimeError("deep research delivery events are missing or duplicated")
                ready_event = ready_events[0]
                terminal_event = terminal_events[0]
                if ready_event.sequence >= terminal_event.sequence:
                    raise RuntimeError("deep research delivery events are out of order")
                if current_bundle is None:
                    raise RuntimeError("deep research delivery bundle was not loaded")

                # Project before the flag is set: ``delivery_ready_emitted`` tells
                # the recovery branch a publishable payload exists, so a Bundle
                # the public surface refuses must never mark it.
                delivery_ready_payload = {
                    "type": "delivery_ready",
                    "message_id": message_id,
                    "run_id": trip_run.run_id,
                    "event_seq": ready_event.sequence,
                    "bundle_id": ctx.delivery_bundle_id,
                    "manifest": ctx.delivery_manifest,
                    "bundle": public_delivery_bundle(current_bundle),
                    "ts_ms": run_ts_ms(),
                }
                delivery_ready_emitted = True
                yield sse_event(delivery_ready_payload)
                run_terminal_emitted = True
                yield sse_event({
                    "type": "run_terminal",
                    "message_id": message_id,
                    "run_id": trip_run.run_id,
                    "event_seq": terminal_event.sequence,
                    "status": TripRunStatus.COMPLETED.value,
                    "bundle_id": ctx.delivery_bundle_id,
                    "run_cost_summary": cost_summary,
                    "ts_ms": run_ts_ms(),
                })
                return

            yield sse_event({
                "type": "chat_complete",
                "message_id": message_id,
                "run_id": trip_run.run_id,
                "run_status": run_status,
                # final_content：剥离 JSON 数据块后的最终正文
                # 前端收到后用此值覆盖流式累积内容，防止 JSON 尾块泄露
                "final_content": assistant_content,
                "citations": list(ctx.final_grounding.get("citations") or []),
                "annotations": list(ctx.final_grounding.get("annotations") or []),
                "run_cost_summary": cost_summary,
                "ts_ms": run_ts_ms(),
            })

        except RunCancelled as cancelled:
            try:
                assistant_content = await persist_partial_turn("interrupted")
            except Exception as save_err:
                logger.warning(f"取消会话保存失败: {save_err}")
                assistant_content = strip_non_display_blocks(
                    strip_thinking_text(strip_think_blocks(ctx.full_response))
                )
            stop_node = (
                cancelled.node_name
                or ctx.final_step_name
                or ctx.final_agent_name
                or "workflow"
            )
            # 失去租约不是用户的决定：它收敛 INTERRUPTED，并且是可继续的那一类中断。
            lease_lost = cancelled.reason == "lease_lost"
            try:
                current_run = await trip_run_store.get_run(trip_run.run_id)
                if lease_lost:
                    if current_run and current_run.status == TripRunStatus.RUNNING:
                        await trip_run_store.transition_status(
                            trip_run.run_id,
                            TripRunStatus.INTERRUPTED,
                            current_node=stop_node,
                            error_code="executor_lease_lost",
                            event_type="run.interrupted",
                            payload={"reason": "executor_lease_lost"},
                        )
                else:
                    if current_run and current_run.status == TripRunStatus.RUNNING:
                        await trip_run_store.transition_status(
                            trip_run.run_id,
                            TripRunStatus.CANCEL_REQUESTED,
                            current_node=stop_node,
                            event_type="run.control_requested",
                            payload={"action": "cancel", "source": "workflow_boundary"},
                        )
                    await trip_run_store.transition_status(
                        trip_run.run_id,
                        TripRunStatus.CANCELLED,
                        current_node=stop_node,
                        event_type="run.cancelled",
                        payload={
                            "reason": "user_cancelled",
                            "trace_event_count": len(ctx.trace_events),
                        },
                    )
            except Exception as run_err:
                logger.warning(f"TripRun 停止状态保存失败: {run_err}")
            cost_summary = await finalize_cost_summary(terminal=True)
            if lease_lost:
                yield sse_event({
                    "type": "error",
                    "message": "这次规划被中断了（本机执行记录丢失），可以稍后继续。",
                    "message_id": message_id,
                    "run_id": trip_run.run_id,
                })
            else:
                yield sse_event({
                    "type": "run_cancelled",
                    "message_id": message_id,
                    "run_id": trip_run.run_id,
                    "final_content": assistant_content,
                    "run_cost_summary": cost_summary,
                    "ts_ms": run_ts_ms(),
                })

        except asyncio.CancelledError:
            stream_exit_reason = "client_disconnected"
            raise
        except GeneratorExit:
            stream_exit_reason = "response_body_closed"
            raise
        except Exception as e:  # API 边界 catch-all：确保所有异常返回 SSE error 事件而非断连
            # The failure this boundary reports.  A durable delivery the public
            # surface refuses replaces it: the run's honest terminal reason is
            # that its Bundle cannot be published, not whatever bookkeeping
            # happened to fail first.
            terminal_failure: Exception = e
            recovered_delivery = None
            if ctx.use_deep_research:
                try:
                    recovered_delivery = await load_durable_completed_delivery()
                except Exception as recovery_err:
                    logger.error(
                        "已完成 Deep Run 的 durable delivery 恢复校验失败: "
                        f"{recovery_err}",
                        exc_info=True,
                    )
            if recovered_delivery is not None:
                current_bundle, ready_event, terminal_event = recovered_delivery
                try:
                    # Project both public payloads before emitting anything.  The
                    # projection is the gate that guarantees every published entry
                    # states an evidence basis, so a Bundle that fails it must not
                    # be published even partially — and the failure must not leave
                    # this handler, because the response headers are already sent
                    # and the client would see nothing but the body stopping.
                    recovered_manifest = public_event_manifest(
                        current_bundle.manifest.model_dump(mode="json")
                    )
                    recovered_public_bundle = public_delivery_bundle(current_bundle)
                except PublicProjectionContractViolation as projection_err:
                    logger.error(
                        "durable Bundle 无法投影为公开交付面，放弃 durable 收口: "
                        f"{projection_err}",
                        exc_info=True,
                    )
                    terminal_failure = projection_err
                else:
                    ctx.delivery_bundle_id = current_bundle.manifest.bundle_id
                    ctx.delivery_manifest = recovered_manifest
                    logger.warning(
                        "Deep Run 已原子完成，但后提交 bookkeeping 失败；"
                        "SSE 以 durable Bundle/terminal truth 收口: %s",
                        e,
                    )
                    cost_summary = await finalize_cost_summary(terminal=True)
                    if not delivery_ready_emitted:
                        delivery_ready_emitted = True
                        yield sse_event({
                            "type": "delivery_ready",
                            "message_id": message_id,
                            "run_id": trip_run.run_id,
                            "event_seq": ready_event.sequence,
                            "bundle_id": ctx.delivery_bundle_id,
                            "manifest": ctx.delivery_manifest,
                            "bundle": recovered_public_bundle,
                            "ts_ms": run_ts_ms(),
                        })
                    if not run_terminal_emitted:
                        run_terminal_emitted = True
                        yield sse_event({
                            "type": "run_terminal",
                            "message_id": message_id,
                            "run_id": trip_run.run_id,
                            "event_seq": terminal_event.sequence,
                            "status": TripRunStatus.COMPLETED.value,
                            "bundle_id": ctx.delivery_bundle_id,
                            "run_cost_summary": cost_summary,
                            "ts_ms": run_ts_ms(),
                        })
                    return
            logger.error(
                f"SSE 流式处理错误: {terminal_failure}", exc_info=terminal_failure
            )
            # This boundary is not a second content-failure policy.  A normal
            # provider/model/content failure travels through the graph's gates
            # and the formal Bundle finalizer.  Only failures that arrive already
            # classified — a typed finalizer failure, a gate's own contract
            # violation, or the public projection refusing a Bundle it cannot
            # attribute — carry a reason code; any other escaped authorized
            # workflow error stays explicitly unclassified for Eval.
            if isinstance(terminal_failure, DeliveryFinalizationError):
                terminal_reason_code = (
                    f"delivery_integrity_{terminal_failure.record.failure_class.value}"
                )
                terminal_gate_class = None
            elif isinstance(terminal_failure, DeliveryContractViolation):
                terminal_reason_code = terminal_failure.reason_code
                terminal_gate_class = terminal_failure.gate_class.value
            elif isinstance(terminal_failure, PublicProjectionContractViolation):
                # Deterministic and authored by the public surface itself, so it
                # is classified like a gate violation.  It names no gate class:
                # the refusal happens at the serving boundary, not in a
                # composition gate.
                terminal_reason_code = "public_projection_contract_violation"
                terminal_gate_class = None
            else:
                terminal_reason_code = "unclassified_workflow_failure"
                terminal_gate_class = None
            # Read the sealed record before deciding what this response may say.
            #
            # A run whose durable status is already COMPLETED or CANCELLED has a
            # terminal record no serving boundary is allowed to rewrite — and no
            # serving boundary is allowed to *contradict* either.  Publishing a
            # sealed result to this client is a different fact and has its own
            # field.
            sealed_status: Optional[TripRunStatus] = None
            try:
                durable_run = await trip_run_store.get_run(trip_run.run_id)
            except Exception as read_err:
                logger.error(
                    f"失败收口无法读取 durable TripRun 状态: {read_err}", exc_info=True
                )
            else:
                if durable_run is not None and durable_run.status in {
                    TripRunStatus.COMPLETED,
                    TripRunStatus.CANCELLED,
                }:
                    sealed_status = durable_run.status
            if sealed_status is None:
                try:
                    await trip_run_store.transition_status(
                        trip_run.run_id,
                        TripRunStatus.FAILED,
                        current_node="workflow",
                        error_code=terminal_failure.__class__.__name__,
                        error_message=_format_llm_error(terminal_failure),
                        event_type="run.failed",
                        # The reason also goes on the durable **event**, not only
                        # into ``terminal_attribution``: that attribution is only
                        # written when the run got far enough to have a minimum
                        # delivery draft (``_completion_audit_for_status`` returns
                        # ``{}`` for an empty audit).  A run that died before any
                        # draft existed would otherwise have its classification
                        # nowhere durable.  One fact, one place it is always recorded.
                        payload={
                            "reason_code": terminal_reason_code,
                            "gate_class": terminal_gate_class,
                        },
                        terminal_reason_code=terminal_reason_code,
                        terminal_gate_class=terminal_gate_class,
                    )
                except ValueError:
                    # The record reached a terminal status between the read above
                    # and this write.  Re-read rather than assume: the same rule
                    # applies to a race as to a state that was already sealed.
                    raced_run = await trip_run_store.get_run(trip_run.run_id)
                    if raced_run is not None and raced_run.status in {
                        TripRunStatus.COMPLETED,
                        TripRunStatus.CANCELLED,
                    }:
                        sealed_status = raced_run.status
                    else:
                        logger.error(
                            "TripRun 失败状态写入被拒，且 durable 状态不是封存终态: %s",
                            raced_run.status.value if raced_run else "missing",
                        )
                except Exception as run_err:
                    logger.error(f"TripRun 失败状态保存失败: {run_err}", exc_info=True)
            failed_trace = ctx.next_trace(
                node="workflow",
                phase="postprocess",
                status="failed",
                output_summary=_format_llm_error(terminal_failure),
                risk_flags=["workflow_failed"],
            )
            try:
                await trip_run_store.append_event(
                    trip_run.run_id,
                    "trace.event",
                    _trace_event_payload(failed_trace),
                )
            except Exception as run_err:
                logger.warning(f"TripRun 失败 trace 保存失败: {run_err}")
            try:
                terminal_cost_summary = await finalize_cost_summary(terminal=True)
            except Exception as cost_err:
                logger.warning(f"失败 run 成本汇总失败: {cost_err}")
                terminal_cost_summary = None
            yield sse_event(failed_trace)
            # `terminal_reason_code` stays out of every frame below.  It is
            # internal attribution, and it is already durable: the
            # `transition_status(terminal_reason_code=…)` above wrote it to the run
            # record, which is what an operator reads.  No client ever read
            # `reason_code` / `error_code` / `publish_failure_reason_code` —
            # what a user needs from this moment is the one sentence below.
            if sealed_status is TripRunStatus.COMPLETED:
                # The run finished and its result is durable.  Say so — that this
                # particular response could not publish it is in the run record.
                terminal_event = (
                    recovered_delivery[2] if recovered_delivery is not None else None
                )
                yield sse_event({
                    "type": "run_terminal",
                    "message_id": message_id,
                    "run_id": trip_run.run_id,
                    "event_seq": terminal_event.sequence if terminal_event else None,
                    "status": TripRunStatus.COMPLETED.value,
                    "bundle_id": ctx.delivery_bundle_id,
                    "run_cost_summary": terminal_cost_summary,
                    "ts_ms": run_ts_ms(),
                })
            elif sealed_status is TripRunStatus.CANCELLED:
                yield sse_event({
                    "type": "run_cancelled",
                    "message_id": message_id,
                    "run_id": trip_run.run_id,
                    "final_content": strip_non_display_blocks(
                        strip_thinking_text(strip_think_blocks(ctx.full_response))
                    ),
                    "run_cost_summary": terminal_cost_summary,
                    "ts_ms": run_ts_ms(),
                })
            else:
                yield sse_event({
                    "type": "run_failed",
                    "message": "旅行方案暂时无法生成，请稍后重试。",
                    "message_id": message_id,
                    "run_id": trip_run.run_id,
                })
            yield sse_event({
                "type": "error",
                "message": "旅行方案暂时无法生成，请稍后重试。",
                "message_id": message_id,
                "run_id": trip_run.run_id,
            })
        finally:
            # Durable state converges before waiting on Provider/LLM cancellation.
            # The independent cleanup task survives cancellation of the response body.
            cleanup_task = asyncio.create_task(cleanup_stream_exit(stream_exit_reason))
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # The response task may be cancelled again; the shielded durable cleanup continues.
                cleanup_task.add_done_callback(log_cleanup_result)
            except Exception as cleanup_err:
                logger.error(f"TripRun stream-exit cleanup failed: {cleanup_err}", exc_info=True)

    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/optimize-prompt")
async def optimize_prompt(request_body: dict):
    """
    提示词优化 API：使用 Fast 模型对用户输入的旅行提示词进行扩充和结构化优化。
    请求体：{ "prompt": string }
    成功返回：{ "optimized_prompt": string, "success": boolean, "error_message"?: string }
    依赖不可用时返回 503，避免把真实服务故障伪装成可用的业务失败。
    """
    prompt = (request_body.get("prompt") or "").strip()

    if len(prompt) < 4:
        return {
            "success": False,
            "optimized_prompt": "",
            "error_message": "提示词太短，请补充更多信息后重试",
        }

    # Same InputGuard as chat — do not let optimize-prompt bypass injection checks.
    guard_result = await check_input_guard(prompt)
    if guard_result.is_blocked:
        logger.warning("InputGuard blocked optimize-prompt: %s", guard_result.reason)
        return {
            "success": False,
            "optimized_prompt": "",
            "error_message": guard_result.blocked_reply
            or "请求未通过安全检查，请修改后重试",
        }

    system_prompt = """你是一名专业的旅行提示词优化专家。用户会给你一段旅行相关的问题或需求，你的任务是将其优化为一段结构清晰、信息丰富的高质量提示词。

优化原则：
1. 保留用户的原始意图和已知条件，不得篡改或矛盾
2. 针对旅行规划常用维度（目的地、出行天数、人数、预算、出行方式、住宿偏好、饮食偏好、兴趣爱好）进行合理补充，以引导性描述而非硬性约束的形式呈现
3. 如果某些维度用户已经明确提及，保持原有描述；未提及的维度用"希望了解……"或"期望……"等软性表述引导
4. 输出语言与用户输入语言保持一致（用户用中文则输出中文）
5. 直接输出优化后的提示词，不要任何额外说明、前缀或引号包裹

如果用户输入的内容与旅行无关、语义不明或信息量极少无法合理优化，请仅输出："CANNOT_OPTIMIZE"（不包含引号）

示例输入：杭州两天
示例输出：帮我规划一次杭州两天一夜的旅行，出发地不限，2人出行。希望了解西湖、灵隐寺等经典景点的游览路线，以及当地特色美食推荐（如西湖醋鱼、龙井虾仁）。预算中等，住宿希望靠近景区，出行方式以公共交通为主。"""

    try:
        components = get_components()
        fast_llm = components.model_router.get_fast()

        # 使用 dict 格式消息，兼容 OllamaProvider 和其他自定义 Provider
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        raw = await fast_llm.ainvoke(messages)
        # OllamaProvider.ainvoke 直接返回字符串；其他 Provider 可能返回含 .content 的对象
        if isinstance(raw, str):
            result_text = raw.strip()
        else:
            result_text = (getattr(raw, "content", None) or "").strip()

        if not result_text or result_text == "CANNOT_OPTIMIZE":
            return {
                "success": False,
                "optimized_prompt": "",
                "error_message": "提示词信息量不足，请补充目的地、天数等更多信息后重试",
            }

        return {
            "success": True,
            "optimized_prompt": result_text,
            "error_message": None,
        }

    except Exception as e:  # API 路由边界：返回结构化错误而非裸异常
        logger.error(f"提示词优化失败: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="优化服务暂时不可用，请稍后重试") from e
