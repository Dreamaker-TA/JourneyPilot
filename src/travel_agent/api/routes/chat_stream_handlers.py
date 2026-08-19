"""SSE stream handlers and context for chat."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from ...tools.governance import (
    CAPABILITY_DECLARATION_STATUSES,
    ToolExecutionStatus,
)
from ...utils.display_names import get_agent_display_name, get_step_display_name
from ...utils.json_helpers import strip_think_blocks
from ...services.public_delivery import public_event_manifest
from ..streaming import StreamingStripper, strip_non_display_blocks
from ...workflows.trace import make_trace_event, summarize_state_update
from ...workflows.run_control import node_timing_registry, run_ts_ms
from .chat_helpers import strip_thinking_text

# Node sets used by handlers (kept local to avoid circular import with chat.py).
_DEEP_WORKER_NODES = {
    "destination_researcher",
    "transport_researcher",
    "accommodation_researcher",
    "itinerary_planner",
}
_FINAL_OUTPUT_NODES = {"fast_answer_agent"}
_CHECKPOINT_GATE_NODES = {"plan_gate"}
_PROGRESS_STEP_NODES = {
    "request_contract_normalizer",
    "research_brief_builder",
    "intent_amendment_router",
    "destination_geo_resolver",
    "weather_context_builder",
    "dispatcher",
}
_DISPATCHER_PROGRESS_TEXT = "正在分派并行调研任务"

# ──────────────────────────────────────────────────────────────────────────
# 工具轮次结论：ToolExecutionStatus 是唯一权威
#
# ``tool_done`` / ``tool_result`` 上曾有一个布尔 ``success``，它把三值真相
# （成功 / 失败 / 能力判定）压成两值，于是 ``tools/gateway.py`` 明确写下的不变量
# ——日期能力判定「刻意不是 error」——在 SSE 上被翻译成 Provider 失败：
# ``success=false`` + trace ``status="failed"`` + ``risk_flags=["tool_failed"]``。
# 布尔已被删除；帧上原样携带的 ``status``（一个只由 Tool Gateway 写入的封闭枚举）
# 就是第四态的来源，不需要发明新数据。
#
# 「哪些 status 属于能力判定」只有一个权威：
# ``tools.governance.CAPABILITY_DECLARATION_STATUSES``（前端在
# ``frontend/src/lib/toolDisplay.ts`` 里镜像它，且只镜像一处）。
_TOOL_OUTCOME_CAPABILITY_DECLARED = "capability_declared"
_TOOL_OUTCOME_COMPLETED = "completed"
_TOOL_OUTCOME_FAILED = "failed"
# 执行确实没拿到结果的 status；空 status 同样按失败处理（非信封结果无从证明成功）。
_FAILED_TOOL_STATUSES = frozenset({
    ToolExecutionStatus.FAILED.value,
    ToolExecutionStatus.BLOCKED.value,
})


def _tool_outcome(tool_status: str) -> str:
    """把一个 ToolExecutionStatus 映射成面向用户的工具轮次结论。

    ``degraded`` 仍然是「拿到了可用结果」，所以结论是 completed，降级事实由
    ``risk_flags`` 的 ``tool_degraded`` 与帧上的 ``degraded`` 承载（行为不变）。
    """
    if tool_status in CAPABILITY_DECLARATION_STATUSES:
        return _TOOL_OUTCOME_CAPABILITY_DECLARED
    if not tool_status or tool_status in _FAILED_TOOL_STATUSES:
        return _TOOL_OUTCOME_FAILED
    return _TOOL_OUTCOME_COMPLETED


# ──────────────────────────────────────────────────────────────────────────
# SSE 事件分发：SSEContext + handler 表
#
# 旧实现把所有 kind 分支塞在 generate_sse 一个 while True 循环里(300+ 行),
# 现在拆分为:
#   1. SSEContext: 聚合可变状态(message_id、task_type 等)
#   2. _handle_*: 每个 kind 独立 async generator, 只读/写 ctx
#   3. _SSE_HANDLERS: kind → handler 字典, 主循环只做分发
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class SSEContext:
    """SSE 流处理器的可变状态容器。

    由 generate_sse 创建, 跨 handler 共享状态。
    所有 handler 只通过 ctx 读写状态, 避免闭包变量散落。
    """
    message_id: str
    session_id: str
    mode: str
    use_deep_research: bool
    task_type: str = ""
    full_response: str = ""
    final_agent_name: str = ""
    final_step_name: str = ""
    streamed_nodes: Set[str] = field(default_factory=set)
    progress_nodes_emitted: Set[str] = field(default_factory=set)
    stripper: StreamingStripper = field(default_factory=StreamingStripper)
    thinking_steps: List[Dict[str, Any]] = field(default_factory=list)
    trace_run_id: str = ""
    trace_sequence: int = 0
    trace_events: List[Dict[str, Any]] = field(default_factory=list)
    approval_gate: Optional[Dict[str, Any]] = None
    context_report: Optional[Dict[str, Any]] = None
    final_grounding: Dict[str, Any] = field(default_factory=dict)
    trip_summary_card: Optional[Dict[str, Any]] = None
    delivery_bundle_id: Optional[str] = None
    delivery_manifest: Optional[Dict[str, Any]] = None

    def next_trace(
        self,
        *,
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
    ) -> Dict[str, Any]:
        """Create and remember a trace event SSE payload."""
        self.trace_sequence += 1
        event = make_trace_event(
            run_id=self.trace_run_id or self.message_id,
            sequence=self.trace_sequence,
            node=node,
            status=status,
            phase=phase,
            input_summary=input_summary,
            output_summary=output_summary,
            route_decision=route_decision,
            agent=agent,
            tool_calls=tool_calls,
            risk_flags=risk_flags,
            ts_ms=ts_ms,
            duration_ms=duration_ms,
        ).to_dict()
        self.trace_events.append(event)
        return {"type": "trace_event", "message_id": self.message_id, **event}


def _trace_event_payload(event: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist the trace event fields that are safe for durable run events."""
    allowed = {
        "schema_version",
        "event_id",
        "run_id",
        "sequence",
        "node",
        "phase",
        "status",
        "input_summary",
        "output_summary",
        "route_decision",
        "agent",
        "tool_calls",
        "risk_flags",
        "ts_ms",
        "duration_ms",
    }
    return {key: event.get(key) for key in allowed if key in event}


async def _handle_token(item: tuple, ctx: SSEContext) -> AsyncIterator[Dict[str, Any]]:
    """'token' kind: Worker 节点 astream() 的实时 token(synthesizer 最终输出)。"""
    _, node_name, chunk = item
    ctx.streamed_nodes.add(node_name)
    ctx.final_agent_name = get_agent_display_name(node_name)
    ctx.final_step_name = get_step_display_name(node_name)
    # Deep Research has one formal consumer boundary: the immutable Delivery
    # Bundle.  Never stream a node's provisional/model text into the normal
    # response, even if a future deep node starts yielding tokens.
    if ctx.use_deep_research:
        return
    display = ctx.stripper.feed(chunk)
    if display:
        yield {
            "type": "chat_chunk",
            "message_id": ctx.message_id,
            "content": display,
            "show_content": display,
            "ts_ms": run_ts_ms(),
        }


async def _handle_react_thinking(item: tuple, ctx: SSEContext) -> AsyncIterator[Dict[str, Any]]:
    """'react_thinking' kind: Worker Agent ReAct 推理文本(流式)。"""
    _, node_name, text_chunk = item
    yield {
        "type": "agent_thinking",
        "message_id": ctx.message_id,
        "agent_name": get_agent_display_name(node_name),
        "content": text_chunk,
    }


async def _handle_tool_start(item: tuple, ctx: SSEContext) -> AsyncIterator[Dict[str, Any]]:
    """'tool_start' kind: Worker Agent 工具调用开始 + 记录 thinking_steps。"""
    _, node_name, tool_info = item
    tool_name = tool_info.get("name", "")
    tool_call_id = tool_info.get("tool_call_id") or str(uuid.uuid4())
    tool_category = tool_info.get("category", "other")
    tool_from_cache = tool_info.get("from_cache", False)
    ts_ms = tool_info.get("ts_ms")
    yield {
        "type": "tool_start",
        "message_id": ctx.message_id,
        "agent_name": get_agent_display_name(node_name),
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "args_summary": tool_info.get("args_summary", ""),
        "category": tool_category,
        "from_cache": tool_from_cache,
        "ts_ms": ts_ms,
    }
    yield ctx.next_trace(
        node=node_name,
        phase="tool",
        status="started",
        agent=get_agent_display_name(node_name),
        input_summary=tool_info.get("args_summary", ""),
        tool_calls=[{
            "name": tool_name,
            "status": "started",
            "category": tool_category,
            "from_cache": tool_from_cache,
        }],
        ts_ms=ts_ms,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    ctx.thinking_steps.append({
        "step_id": tool_call_id,
        "tool_call_id": tool_call_id,
        "agent_name": get_agent_display_name(node_name),
        "step_name": f"调用 {tool_name}",
        "content": tool_info.get("args_summary", ""),
        "timestamp": now_iso,
        "is_tool_call": True,
        "tool_name": tool_name,
        "tool_status": "running",
        "tool_args": tool_info.get("args_summary", ""),
        "tool_category": tool_category,
        "from_cache": tool_from_cache,
    })


async def _handle_tool_done(item: tuple, ctx: SSEContext) -> AsyncIterator[Dict[str, Any]]:
    """'tool_done' kind: Worker Agent 工具调用完成 + 更新 thinking_steps 状态。"""
    _, node_name, tool_info = item
    tool_name = tool_info.get("name", "")
    tool_call_id = tool_info.get("tool_call_id")
    tool_summary = tool_info.get("summary", "")
    tool_category = tool_info.get("category", "other")
    tool_from_cache = tool_info.get("from_cache", False)
    tool_status = str(tool_info.get("status") or "")
    tool_outcome = _tool_outcome(tool_status)
    audit_id = tool_info.get("audit_id")
    degraded = bool(tool_info.get("degraded"))
    duration_ms = tool_info.get("duration_ms")
    ts_ms = tool_info.get("ts_ms")
    fallback_from = tool_info.get("fallback_from")
    fallback_to = tool_info.get("fallback_to")
    tool_result_event: Dict[str, Any] = {
        "type": "tool_result",
        "message_id": ctx.message_id,
        "agent_name": get_agent_display_name(node_name),
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "summary": tool_summary,
        # 唯一权威：ToolExecutionStatus 值。消费者据此推导四态展示
        # （completed / degraded / failed / capability_declared）。
        "status": tool_status,
        # `audit_id` / `degraded` are deliberately absent from the client frame:
        # `degraded` is `status == 'degraded'` restated, and `audit_id` is
        # an internal identifier no user surface prints.  Both still ride the
        # durable trace below (`trace_tool_call`), which is where an operator
        # correlates a tool call with its audit row.
        "category": tool_category,
        "from_cache": tool_from_cache,
        "duration_ms": duration_ms,
        "ts_ms": ts_ms,
    }
    if fallback_from:
        tool_result_event["fallback_from"] = fallback_from
    if fallback_to:
        tool_result_event["fallback_to"] = fallback_to
    yield tool_result_event
    trace_tool_call = {
        "name": tool_name,
        "status": tool_status,
        "category": tool_category,
        "from_cache": tool_from_cache,
    }
    if audit_id:
        trace_tool_call["audit_id"] = audit_id
    if degraded:
        trace_tool_call["degraded"] = True
    yield ctx.next_trace(
        node=node_name,
        phase="tool",
        status=tool_outcome,
        agent=get_agent_display_name(node_name),
        output_summary=tool_summary,
        tool_calls=[trace_tool_call],
        # 能力判定不是风险：它是服务端对「该数据源答不了这个日期」的设计内声明，
        # trace 的 status=capability_declared 与 tool_calls[].status 已经完整记录它。
        risk_flags=(
            ["tool_degraded"] if degraded
            else (["tool_failed"] if tool_outcome == _TOOL_OUTCOME_FAILED else [])
        ),
        ts_ms=ts_ms,
        duration_ms=duration_ms,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    for _ts in reversed(ctx.thinking_steps):
        same_call = bool(tool_call_id) and _ts.get("tool_call_id") == tool_call_id
        fallback_match = (
            not tool_call_id
            and _ts.get("is_tool_call")
            and _ts.get("tool_name") == tool_name
            and _ts.get("tool_status") == "running"
        )
        if same_call or fallback_match:
            # 落库的展示态与实时态同源：恢复会话看到的第四态与直播一致。
            _ts["tool_status"] = tool_outcome
            _ts["tool_result"] = tool_summary
            _ts["end_time"] = now_iso
            _ts["duration_ms"] = duration_ms
            break


async def _handle_synthesis_start(item: tuple, ctx: SSEContext) -> AsyncIterator[Dict[str, Any]]:
    """'synthesis_start' kind: Synthesizer 开始 LLM 调用前的过渡信号。"""
    _, node_name, _ = item
    # 这一帧是一个**信号**，不是一份数据：客户端收到它就把「正在成稿」点亮。
    # 内部节点名（`node`）不下发：这条 kind 走
    # passthrough 投影，所以不下发的唯一办法就是不写进来。
    yield {
        "type": "synthesis_start",
        "message_id": ctx.message_id,
    }
    yield ctx.next_trace(
        node=node_name,
        phase="synthesis",
        status="started",
        output_summary="synthesis started",
    )


# 这里**没有** ``_handle_compaction``：压缩的三个进度事件
# （``compaction.started`` / ``.completed`` / ``.warning``）到不了任何人手里 ——
# 它们不在 ``sse_projection`` 的任何一张白名单里，出门那一步就被丢掉；
# ``useSendMessage`` 的 switch 里也没有它们的 case。它还是全仓唯一一处
# SSE ``type`` 由变量决定的地方，所以跨端合同判据从来没看见过这三个名字。
# 压缩这件事对用户可见的出口只有两个，都还在：当轮的 ``context_report``
# （透镜那句「较早的对话已整理」）与落时间线的 ``context_compaction`` 快照。


async def _handle_context_report(item: tuple, ctx: SSEContext) -> AsyncIterator[Dict[str, Any]]:
    """'context_report' kind: 六层记忆按预算装配的上下文报告。

    随本轮助手消息持久化(save_turn)，会话历史加载时可取回。
    """
    _, _node, report = item
    ctx.context_report = report
    yield {
        "type": "context_report",
        "message_id": ctx.message_id,
        **report,
    }


async def _handle_context_compaction(item: tuple, ctx: SSEContext) -> AsyncIterator[Dict[str, Any]]:
    """Persist and emit the full, user-visible compaction snapshot for this turn."""
    _, _node, event = item
    # 这枚快照由 `CompactionService` 与摘要、新边界同事务写进会话事件表，这里只负责
    # 让它当场出现在屏幕上 —— 落库不是这条路径的事。
    yield {
        "type": "context_compaction",
        "message_id": ctx.message_id,
        **event,
    }


async def _handle_state_update(item: tuple, ctx: SSEContext) -> AsyncIterator[Dict[str, Any]]:
    """'state' kind: 节点完成事件(最复杂的 handler)。

    内部分为 4 个阶段:
      1. stripper flush 残留(若节点曾流式输出)
      2. planner 节点: 发送 thinking / 记录执行计划
      3. 其他节点: 处理 pending_user_choice / deep worker / 最终输出
      4. 记录节点完成 trace
    """
    _, node_name, state_update = item
    if state_update is None:
        return
    if isinstance(state_update, dict) and state_update.get("run_id"):
        ctx.trace_run_id = str(state_update.get("run_id"))
    if isinstance(state_update, dict) and isinstance(state_update.get("final_grounding"), dict):
        ctx.final_grounding = state_update["final_grounding"]
    delivery = state_update.get("delivery_bundle") if isinstance(state_update, dict) else None
    if hasattr(delivery, "model_dump"):
        delivery = delivery.model_dump(mode="json")
    if isinstance(delivery, dict) and isinstance(delivery.get("manifest"), dict):
        ctx.delivery_manifest = public_event_manifest(delivery["manifest"])
        ctx.delivery_bundle_id = str(delivery["manifest"].get("bundle_id") or "") or None

    # 全量 final state 只提取 Bundle manifest，不构造另一份交付 payload。
    if node_name == "__final_state__":
        yield ctx.next_trace(
            node="workflow",
            phase="postprocess",
            status="completed",
            output_summary="workflow final state received",
        )
        # No ``trace_summary`` frame is produced.  The workflow
        # trace is **internal observability**, and the thinking chain is already the
        # user-visible summary of the same run — so there is no second surface to
        # build and nothing to send.  The events
        # themselves stay durable via ``trace.event`` in ``api/routes/chat.py``.
        return

    summary_card = state_update.get("trip_summary_card") if isinstance(state_update, dict) else None
    if isinstance(summary_card, dict):
        ctx.trip_summary_card = summary_card
        yield {
            "type": "trip_summary_card",
            "message_id": ctx.message_id,
            "run_id": ctx.trace_run_id,
            "summary_card": summary_card,
            "ts_ms": run_ts_ms(),
        }

    # 阶段 1: stripper flush
    if node_name in ctx.streamed_nodes:
        remaining = ctx.stripper.flush()
        if remaining and not ctx.use_deep_research:
            yield {
                "type": "chat_chunk",
                "message_id": ctx.message_id,
                "content": remaining,
                "show_content": remaining,
                "ts_ms": run_ts_ms(),
            }
        ctx.stripper = StreamingStripper()  # 重置供下一个节点使用

    # 阶段 1.5: 静默前置节点 / dispatcher 的透明度补步（每节点仅一次，dispatcher fan-out
    # 多轮也只发首轮），让思维链在外部调用与 worker 启动的空窗期持续有进度可见。
    if node_name in _PROGRESS_STEP_NODES and node_name not in ctx.progress_nodes_emitted:
        ctx.progress_nodes_emitted.add(node_name)
        progress_step_name = get_step_display_name(node_name)
        # 这些静默前置节点没有推理正文可说，而 `content` 会上屏 —— 避免把步骤名
        # 并排印两遍。一件事说一次：只有 dispatcher
        # 有一句步骤名说不出的话（它在讲并行分派这件事），其余留空。
        progress_content = (
            _DISPATCHER_PROGRESS_TEXT if node_name == "dispatcher" else ""
        )
        yield {
            "type": "thinking",
            "message_id": ctx.message_id,
            "agent_name": get_agent_display_name(node_name),
            "content": progress_content,
            "step_name": progress_step_name,
            "ts_ms": run_ts_ms(),
        }
        now_iso = datetime.now(timezone.utc).isoformat()
        ctx.thinking_steps.append({
            "step_id": str(uuid.uuid4()),
            "agent_name": get_agent_display_name(node_name),
            "step_name": progress_step_name,
            "content": progress_content,
            "timestamp": now_iso,
            "end_time": now_iso,
        })

    # 阶段 2: planner 节点
    if node_name == "planner":
        tt = state_update.get("task_type")
        if tt is not None:
            ctx.task_type = tt.value if hasattr(tt, "value") else str(tt)

        execution_plan = state_update.get("execution_plan", [])
        if execution_plan:
            agents_desc = " → ".join(
                "/".join(get_agent_display_name(a) for a in g)
                for g in execution_plan
            )
            thinking_content = f"制定调研计划：{agents_desc}"
        else:
            thinking_content = "制定调研计划中..."

        yield {
            "type": "thinking",
            "message_id": ctx.message_id,
            "agent_name": get_agent_display_name("planner"),
            "content": thinking_content,
            "step_name": get_step_display_name("planning"),
            "ts_ms": run_ts_ms(),
        }
        now_iso = datetime.now(timezone.utc).isoformat()
        ctx.thinking_steps.append({
            "step_id": str(uuid.uuid4()),
            "agent_name": get_agent_display_name("supervisor"),
            "step_name": get_step_display_name("orchestrating"),
            "content": thinking_content,
            "timestamp": now_iso,
            "end_time": now_iso,
        })

    else:
        # 阶段 3: 其他节点
        if ctx.use_deep_research and node_name in _DEEP_WORKER_NODES:
            # 深度模式 Worker 中间结果 → agent_progress(不追加到主消息)
            messages_list = state_update.get("messages", [])
            if messages_list:
                last_msg = messages_list[-1]
                raw_content = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
                content = raw_content if isinstance(raw_content, str) else str(raw_content)
                display_content = strip_non_display_blocks(
                    strip_thinking_text(strip_think_blocks(content))
                )
                if display_content:
                    ctx.final_agent_name = get_agent_display_name(node_name)
                    ctx.final_step_name = get_step_display_name(node_name)
                    yield {
                        "type": "agent_progress",
                        "message_id": ctx.message_id,
                        "agent_name": get_agent_display_name(node_name),
                        "step_name": get_step_display_name(node_name),
                        "content": display_content,
                        "ts_ms": run_ts_ms(),
                    }
                    now_iso = datetime.now(timezone.utc).isoformat()
                    ctx.thinking_steps.append({
                        "step_id": str(uuid.uuid4()),
                        "agent_name": get_agent_display_name(node_name),
                        "step_name": get_step_display_name(node_name),
                        "content": display_content,
                        "timestamp": now_iso,
                        "end_time": now_iso,
                    })
        elif node_name not in ctx.streamed_nodes and not ctx.use_deep_research:
            # 最终输出节点(非流式): 发送 chat_chunk
            messages_list = state_update.get("messages", [])
            if messages_list:
                last_msg = messages_list[-1]
                raw_content = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
                content = raw_content if isinstance(raw_content, str) else str(raw_content)
                ctx.full_response = content
                display_content = strip_non_display_blocks(
                    strip_thinking_text(strip_think_blocks(content))
                )
                ctx.final_agent_name = get_agent_display_name(node_name)
                ctx.final_step_name = get_step_display_name(node_name)
                yield {
                    "type": "chat_chunk",
                    "message_id": ctx.message_id,
                    "content": display_content,
                    "show_content": display_content,
                    "ts_ms": run_ts_ms(),
                }
        else:
            # 流式节点完成: 从 state_update 更新 full_response(用于面板提取)
            # 只有最终输出节点才更新 full_response(排除深度模式的 Worker)
            if node_name in _FINAL_OUTPUT_NODES:
                messages_list = state_update.get("messages", [])
                if messages_list:
                    last_msg = messages_list[-1]
                    raw_content = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
                    ctx.full_response = raw_content if isinstance(raw_content, str) else str(raw_content)
                    ctx.final_agent_name = get_agent_display_name(node_name)
                    ctx.final_step_name = get_step_display_name(node_name)

    node_timing = node_timing_registry.pop(ctx.trace_run_id, node_name)
    trace_bits = summarize_state_update(node_name, state_update)
    yield ctx.next_trace(
        node=node_name,
        status="completed",
        output_summary=trace_bits.get("output_summary"),
        route_decision=trace_bits.get("route_decision"),
        risk_flags=trace_bits.get("risk_flags"),
        ts_ms=node_timing.get("ts_ms") if node_timing else None,
        duration_ms=node_timing.get("duration_ms") if node_timing else None,
    )


_SSE_HANDLERS = {
    "token": _handle_token,
    "react_thinking": _handle_react_thinking,
    "tool_start": _handle_tool_start,
    "tool_done": _handle_tool_done,
    "synthesis_start": _handle_synthesis_start,
    "context_compaction": _handle_context_compaction,
    "context_report": _handle_context_report,
    "state": _handle_state_update,
}
