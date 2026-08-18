"""In-process run control primitives for JourneyPilot workflows.

这个模块**只是进程内的低延迟通道**。最终事实在 `trip_runs`（业务状态）与
`trip_run_commands`（控制命令）：cancel 与 supplement 先落库，`RunCommandCoordinator`
再把它们搬进这里的 handle 供协作边界同步读取。registry 因此不回答「有没有这条命令」，
只回答「这个进程现在跑着这个 run，可以马上去读表」。通知丢了最多多等一个轮询间隔。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, TypeVar

from langgraph.errors import GraphInterrupt

from ..entities.run_budget import RunBudgetSnapshot
from .run_budget import RunBudgetLedger, ledger_for, peek_ledger, seed_run_budget
from .run_deadline import (
    DeadlineObservation,
    clear_process_deadline_anchor,
    observe_run_deadline,
)

logger = logging.getLogger(__name__)

current_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_run_id",
    default=None,
)
current_node: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_node",
    default=None,
)
current_agent: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_agent",
    default=None,
)
# 单调钟锚点：run 开始时置为 time.perf_counter() 的读数。步边界事件的 ts_ms 一律相对
# 此锚点取值，客户端与服务端墙钟偏差不影响计时。asyncio.create_task 复制当前 context，
# 故在 create_task 之前设锚点，工作流任务与 SSE 处理协程共用同一锚点。
run_ts_anchor: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "run_ts_anchor",
    default=None,
)

NodeLifecycleStatus = Literal["started", "completed", "retrying", "failed"]
NodeLifecycleSink = Callable[[Dict[str, Any]], Awaitable[None]]
node_lifecycle_sink: contextvars.ContextVar[Optional[NodeLifecycleSink]] = contextvars.ContextVar(
    "node_lifecycle_sink",
    default=None,
)

# Nodes already receive the durable snapshot in their state.  This context is
# the matching low-level boundary for model and tool calls, whose public APIs
# deliberately do not accept ``TravelAgentState``.  It is process-local only:
# every graph boundary writes the observed value back into state/checkpoint.
current_run_deadline: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "current_run_deadline",
    default=None,
)

# 同上，但计的是调用数 / token / 费用。快照来自 state（随 checkpoint 走），账本在
# `workflows/run_budget.py`（进程内，按 run_id 索引）。低层调用点拿不到
# ``TravelAgentState``，所以它们从这里取。
current_run_budget: contextvars.ContextVar[Optional[RunBudgetSnapshot]] = contextvars.ContextVar(
    "current_run_budget",
    default=None,
)


ModelWindow = Literal["research", "composition"]

# Which window the model and provider calls on this execution context belong to.
# Research is the default because it is what the overwhelming majority of the
# graph does; the itinerary composition node opts its own body into the
# composition window, so the boundary follows the phase of the run rather than
# the identity of the awaitable.
current_model_window: contextvars.ContextVar[ModelWindow] = contextvars.ContextVar(
    "current_model_window",
    default="research",
)

class ModelWindowClosed(RuntimeError):
    """A model/provider/tool call attempted after its own window's cutoff.

    This is intentionally distinct from :class:`RunCancelled`: the user did
    not cancel the TripRun, so callers must converge through deterministic
    closeout rather than emit a cancelled terminal state.
    """

    def __init__(
        self,
        operation: str,
        observation: DeadlineObservation,
        window: ModelWindow,
    ) -> None:
        self.operation = operation
        self.observation = observation
        self.window = window
        super().__init__(
            f"{window} window closed before {operation} "
            f"(elapsed={observation.elapsed_seconds:.3f}s)"
        )


class DeliveryDeadlineExceeded(RuntimeError):
    """A delivery-only operation crossed the durable eight-minute deadline."""

    def __init__(self, operation: str, observation: DeadlineObservation) -> None:
        self.operation = operation
        self.observation = observation
        super().__init__(
            f"delivery deadline exceeded before {operation} "
            f"(elapsed={observation.elapsed_seconds:.3f}s)"
        )


def set_run_ts_anchor() -> None:
    """在 run 起点建立单调钟锚点。"""
    run_ts_anchor.set(time.perf_counter())


def run_ts_ms() -> Optional[float]:
    """相对本 run 起点的毫秒数（单调钟）；无锚点时返回 None。"""
    anchor = run_ts_anchor.get()
    if anchor is None:
        return None
    return round((time.perf_counter() - anchor) * 1000.0, 3)


#: 为什么停。终态归属不同：用户取消收敛 CANCELLED，失去租约与响应流退出收敛 INTERRUPTED ——
#: 后两者不是用户的决定，把它们记成「已取消」就是给记录里写一件没发生的事。
RunStopReason = Literal["user_cancel", "lease_lost", "stream_exit"]


class RunCancelled(Exception):
    """Raised when a cooperative stop signal reaches a boundary."""

    def __init__(
        self,
        run_id: str,
        node_name: Optional[str] = None,
        *,
        reason: RunStopReason = "user_cancel",
    ) -> None:
        self.run_id = run_id
        self.node_name = node_name
        self.reason = reason
        label = f" at {node_name}" if node_name else ""
        super().__init__(f"TripRun {run_id} stopped ({reason}){label}")


#: 一批 supplement 落进 state 之后调用：把对应的 durable command 标成 consumed。
SupplementAppliedSink = Callable[[List[str], str], Awaitable[None]]


@dataclass
class RunControlHandle:
    run_id: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    delivery_ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    #: 已 claim 但还没落进 state 的追加要求，每条带着自己的 `command_id`。
    supplements: List[Dict[str, str]] = field(default_factory=list)
    stop_reason: RunStopReason = "user_cancel"
    #: 有新的 durable command 时被 set，让协调器立刻去读表而不必等下一个轮询周期。
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    supplement_applied_sink: Optional[SupplementAppliedSink] = None

    def request_stop(self, reason: RunStopReason = "user_cancel") -> None:
        self.stop_reason = reason
        self.cancel_event.set()

    def mark_delivery_ready(self) -> None:
        """Seal this in-process run against late research writes.

        Durable Bundle identity remains the source of truth.  This event only
        prevents a concurrently finishing worker from publishing stale content
        after the finalizer has durably committed that identity.
        """
        self.delivery_ready_event.set()

    def add_supplement(self, category: str, content: str, *, command_id: str) -> None:
        self.supplements.append(
            {"command_id": command_id, "category": category, "content": content}
        )

    def pending_supplements(self) -> List[Dict[str, str]]:
        """还没落进 state 的追加要求。

        **取走不等于清空**：节点抛异常时它返回的 state update 会被丢掉，那条要求也就没有
        生效。留在这里，下一个节点边界再试一次。
        """

        return list(self.supplements)

    async def mark_supplements_applied(self, command_ids: List[str], *, node: str) -> None:
        applied = {str(value) for value in command_ids if str(value).strip()}
        if not applied:
            return
        self.supplements = [
            item for item in self.supplements if item.get("command_id") not in applied
        ]
        if self.supplement_applied_sink is not None:
            await self.supplement_applied_sink(sorted(applied), node)


class RunControlRegistry:
    """按 run id 索引的进程内 handle。**唤醒通道，不是命令的存放处。**"""

    def __init__(self) -> None:
        self._handles: Dict[str, RunControlHandle] = {}

    def register(self, run_id: str) -> RunControlHandle:
        handle = RunControlHandle(run_id=run_id)
        self._handles[run_id] = handle
        return handle

    def get(self, run_id: Optional[str]) -> Optional[RunControlHandle]:
        if not run_id:
            return None
        return self._handles.get(run_id)

    def unregister(self, run_id: Optional[str], handle: Optional["RunControlHandle"] = None) -> None:
        """注销。给了 ``handle`` 就只在这一格还是它的时候删。

        被接管的那条流清理时按 run_id 盲删，删掉的是接管者的 handle，此后用户的
        取消在任何节点边界上都观察不到。
        """

        if not run_id:
            return
        if handle is not None and self._handles.get(run_id) is not handle:
            return
        self._handles.pop(run_id, None)

    def request_stop(self, run_id: str, reason: RunStopReason = "user_cancel") -> bool:
        """进程内的停止信号。用于失去租约这类**不来自用户**、没有 durable command 的停止。"""

        handle = self.get(run_id)
        if handle is None:
            return False
        handle.request_stop(reason)
        return True

    def notify(self, run_id: str) -> bool:
        """告诉执行器「表里有新命令」。返回本进程是否正在跑这个 run。

        返回 False **不是**拒绝：命令已经落库，执行器下一次轮询照样看得见。这个布尔值只
        用于日志与回执里的观察，不参与任何正确性判断。
        """

        handle = self.get(run_id)
        if handle is None:
            return False
        handle.wake_event.set()
        return True

    def clear(self) -> None:
        self._handles.clear()


run_control_registry = RunControlRegistry()


def _state_run_id(state: Any) -> Optional[str]:
    if isinstance(state, dict):
        value = state.get("run_id")
    else:
        value = getattr(state, "run_id", None)
    return str(value) if value else None


class NodeTimingRegistry:
    """节点计时暂存：node wrapper 落表，SSE 层建 trace_event 时读走并附到 payload。

    并发 fan-out 下同名节点可能多次完成（worker 精炼轮次），故每个 (run_id, node) 存一
    FIFO 队列，读侧按到达序 pop 对齐各次 completed 事件。进程内瞬态，run 结束时 clear。
    """

    def __init__(self) -> None:
        self._timings: Dict[str, Dict[str, List[Dict[str, float]]]] = {}
        self._lock = threading.Lock()

    def record(self, run_id: Optional[str], node: str, timing: Dict[str, float]) -> None:
        if not run_id or not node:
            return
        with self._lock:
            self._timings.setdefault(run_id, {}).setdefault(node, []).append(timing)

    def pop(self, run_id: Optional[str], node: str) -> Optional[Dict[str, float]]:
        if not run_id or not node:
            return None
        with self._lock:
            queue = self._timings.get(run_id, {}).get(node)
            if not queue:
                return None
            return queue.pop(0)

    def clear(self, run_id: Optional[str]) -> None:
        if not run_id:
            return
        with self._lock:
            self._timings.pop(run_id, None)


node_timing_registry = NodeTimingRegistry()


@contextlib.contextmanager
def run_attribution(
    run_id: Optional[str],
    *,
    node: Optional[str] = "workflow",
    agent: Optional[str] = "workflow",
    lifecycle_sink: Optional[NodeLifecycleSink] = None,
):
    """Set coarse run attribution around graph execution.

    Concrete node wrappers still override ``current_node`` and ``current_agent``.
    This outer guard covers graph/fast-path boundaries that would otherwise have
    no ``current_run_id`` and be silently skipped by the usage recorder.
    """
    token_run = current_run_id.set(str(run_id) if run_id else None)
    token_node = current_node.set(node)
    token_agent = current_agent.set(agent)
    token_lifecycle_sink = node_lifecycle_sink.set(lifecycle_sink)
    try:
        yield
    finally:
        # The durable snapshot outlives the process-local monotonic anchor.
        # A checkpoint resume reconstructs an anchor from the persisted elapsed
        # lower bound and planning_authorized_at instead of inheriting memory.
        clear_process_deadline_anchor()
        node_lifecycle_sink.reset(token_lifecycle_sink)
        current_agent.reset(token_agent)
        current_node.reset(token_node)
        current_run_id.reset(token_run)


def check_cancel_requested(node_name: Optional[str] = None) -> None:
    """Raise ``RunCancelled`` when the in-process stop flag is set.

    Stopping is **cooperative**: checks run at node entry,
    ReAct iteration boundaries, and after each tool result. An in-flight
    LLM stream or single tool HTTP call may still finish before the next
    checkpoint; latency is bounded by that in-flight round, not by a hard
    process kill.
    """
    run_id = current_run_id.get()
    handle = run_control_registry.get(run_id)
    if handle is not None and handle.cancel_event.is_set():
        raise RunCancelled(
            run_id or handle.run_id,
            node_name or current_node.get(),
            reason=handle.stop_reason,
        )


def observe_current_run_deadline() -> tuple[Optional[Any], Optional[DeadlineObservation]]:
    """Observe the active durable deadline without resetting its budget."""

    deadline = current_run_deadline.get()
    if deadline is None:
        return None, None
    observed, observation = observe_run_deadline(deadline)
    current_run_deadline.set(observed)
    return observed, observation


def remaining_model_seconds(operation: str) -> Optional[float]:
    """Return this context's model budget, or reject a new call.

    The window comes from :data:`current_model_window`: research calls must
    finish by its closeout, itinerary composition by its own later one.  Either
    way the interval past that boundary is excluded — a call may start only
    while it can still finish inside its window, and what the composition
    window leaves belongs to deterministic projection, validation and
    persistence.

    The *seconds* come from the run's own sealed snapshot, never from this
    process's policy defaults.  The snapshot is the cross-process source of
    truth, so how much budget a call gets must be decided by the same numbers
    that decide which phase the run is in: a later ``run_deadline`` change may
    neither fund an in-flight run past the closeout it was audited against nor
    cut short one whose snapshot still funds it.
    """

    check_cancel_requested(current_node.get())
    deadline, observation = observe_current_run_deadline()
    if observation is None or deadline is None:
        return None
    window = current_model_window.get()
    if window == "composition":
        window_seconds = deadline.composition_seconds
        closed = observation.composition_closed
    else:
        window_seconds = deadline.closeout_seconds
        closed = observation.research_closed
    remaining = max(0.0, window_seconds - observation.elapsed_seconds)
    if closed or remaining <= 0:
        raise ModelWindowClosed(operation, observation, window)
    return remaining


def current_budget_ledger() -> Optional[RunBudgetLedger]:
    """这个执行上下文的预算账本；没有封存过预算的 Run 返回 ``None``。

    ``None`` 是合法状态而不是错误：快问快答与授权之前的阶段不封预算，那些路径本来就
    受 Deadline 约束，给它们凭空造一份预算等于给一个没有 owner 的数字下判断。
    """

    snapshot = current_run_budget.get()
    run_id = current_run_id.get()
    if snapshot is None or not run_id:
        return peek_ledger(run_id)
    return ledger_for(run_id, snapshot)


async def ensure_budget_baseline() -> Optional[RunBudgetLedger]:
    """让这个 Run 在本进程的账本带上台账里已花的量，只做一次。

    在**第一个节点边界**做而不是在图入口：预算快照只有在 Draft 被授权之后才存在，
    而入口那一刻还不知道这个 Run 有没有预算。
    """

    snapshot = current_run_budget.get()
    run_id = current_run_id.get()
    if snapshot is None or not run_id:
        return None
    ledger = ledger_for(run_id, snapshot)
    if ledger.seeded:
        return ledger
    from ..infrastructure.cost_ledger_store import get_cost_ledger_store

    return await seed_run_budget(
        run_id, snapshot, cost_ledger_store=get_cost_ledger_store()
    )


def guard_run_budget(
    operation: str,
    *,
    llm_calls: int = 0,
    tool_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """在发起一次新调用之前判预算。超出即抛 `RunBudgetExhausted`。

    参数是**本次最坏开销**的预估。调用之后再记账拦不住超支，所以这一跳必须发生在
    调用之前。
    """

    ledger = current_budget_ledger()
    if ledger is None:
        return
    ledger.guard(
        operation,
        llm_calls=llm_calls,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def remaining_delivery_seconds(operation: str) -> Optional[float]:
    """Return the shared finalization budget, or reject post-deadline work.

    ``observation.remaining_seconds`` already *is* that budget, measured against
    the delivery deadline embedded in the run's own snapshot — for the same
    reason as :func:`remaining_model_seconds`.
    """

    check_cancel_requested(current_node.get())
    _deadline, observation = observe_current_run_deadline()
    if observation is None:
        return None
    remaining = observation.remaining_seconds
    if observation.phase == "expired" or remaining <= 0:
        raise DeliveryDeadlineExceeded(operation, observation)
    return remaining


async def await_model_operation(awaitable: Awaitable[Any], *, operation: str) -> Any:
    """Await one external model/provider operation within its own window."""

    try:
        remaining = remaining_model_seconds(operation)
    except BaseException:
        # Call sites naturally construct the coroutine before passing it here.
        # Closing an unstarted coroutine avoids an unawaited-coroutine warning
        # when the deadline rejects the operation synchronously.
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise
    if remaining is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except asyncio.TimeoutError as exc:
        _deadline, observation = observe_current_run_deadline()
        if observation is None:  # pragma: no cover - defensive context reset
            raise
        raise ModelWindowClosed(
            operation, observation, current_model_window.get()
        ) from exc


async def await_delivery_operation(awaitable: Awaitable[Any], *, operation: str) -> Any:
    """Await one finalization operation within the common eight-minute cap."""

    try:
        remaining = remaining_delivery_seconds(operation)
    except BaseException:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise
    if remaining is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except asyncio.TimeoutError as exc:
        _deadline, observation = observe_current_run_deadline()
        if observation is None:  # pragma: no cover - defensive context reset
            raise
        raise DeliveryDeadlineExceeded(operation, observation) from exc


async def emit_node_lifecycle(
    status: NodeLifecycleStatus,
    *,
    node: Optional[str] = None,
    attempt: Optional[int] = None,
    max_attempts: Optional[int] = None,
    duration_ms: Optional[float] = None,
    error_type: Optional[str] = None,
) -> None:
    """Emit an audit-safe node execution fact to the active workflow sink."""
    sink = node_lifecycle_sink.get()
    node_name = node or current_node.get()
    if sink is None or not node_name:
        return
    payload: Dict[str, Any] = {
        "node": node_name,
        "status": status,
        "ts_ms": run_ts_ms(),
    }
    if attempt is not None:
        payload["attempt"] = attempt
    if max_attempts is not None:
        payload["max_attempts"] = max_attempts
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if error_type:
        payload["error_type"] = error_type
    await sink(payload)


NodeFn = TypeVar("NodeFn", bound=Callable[..., Any])


_RESEARCH_WORKER_NODES = {
    "destination_researcher",
    "transport_researcher",
    "accommodation_researcher",
}
# Itinerary composition is a model path too, but it is the deliverable rather
# than research, so it runs on its own window and stays enterable through the
# minute the research workers have already lost.
_COMPOSITION_WORKER_NODES = {"itinerary_planner"}
_DEADLINE_BLOCKED_WORKER_NODES = _RESEARCH_WORKER_NODES | _COMPOSITION_WORKER_NODES


def _worker_window_closed(node_name: str, observation: DeadlineObservation) -> bool:
    """Whether ``node_name`` has run out of the window its own calls belong to."""

    if node_name in _COMPOSITION_WORKER_NODES:
        return observation.composition_closed
    return observation.research_closed


def _blocked_research_worker_update(
    *,
    node_name: str,
    observed_deadline: Any,
    observation: DeadlineObservation,
) -> Dict[str, Any]:
    """Make a missed research/composition boundary converge through graph routes.

    The boundary is reported on the worker's own channels only. Routing is the
    Dispatcher's to write: it reads the terminal ``agent_status`` and the same
    deadline, and a parallel Send group turns any worker-side ``next_agent``
    write into two values for one step.
    """

    if observation.phase == "expired":
        return {
            "run_deadline": observed_deadline,
            "agent_status": {node_name: "failed"},
            "last_error": (
                "delivery deadline elapsed before research worker could start"
                if node_name in _RESEARCH_WORKER_NODES
                else "delivery deadline elapsed before itinerary composition"
            ),
        }
    return {
        "run_deadline": observed_deadline,
        "agent_status": {node_name: "partial"},
    }


def with_run_control(node_name: str, fn: NodeFn) -> Callable[..., Awaitable[Any]]:
    """Wrap a LangGraph node with cancel checks and run attribution contextvars.

    ``functools.wraps`` keeps the wrapper transparent to LangGraph's signature
    inspection: ``inspect.signature`` follows ``__wrapped__``, so LangGraph still
    injects ``config`` (and ``writer``/``store`` when declared) into nodes that
    ask for them. Without this the wrapper's ``*args, **kwargs`` signature would
    hide those parameters and config-dependent nodes (workers, synthesizer,
    dispatcher) would never receive ``config``.
    """

    @functools.wraps(fn)
    async def _wrapped(state: Any, *args: Any, **kwargs: Any) -> Any:
        run_id = _state_run_id(state)
        token_run = current_run_id.set(run_id)
        token_node = current_node.set(node_name)
        token_agent = current_agent.set(node_name)
        token_deadline = current_run_deadline.set(None)
        # 预算快照是 Run 的属性、不随节点变化，所以在这里绑一次，节点里调到的每一层
        # 助手函数都免费继承它。
        token_budget = current_run_budget.set(getattr(state, "run_budget", None))
        # The window a node's model calls draw on is a property of the node, so
        # it is bound here with the other run attribution rather than inside each
        # node body — helper functions the node calls inherit it for free.
        token_window = current_model_window.set(
            "composition" if node_name in _COMPOSITION_WORKER_NODES else "research"
        )
        started = time.perf_counter()
        ts_ms = run_ts_ms()
        try:
            check_cancel_requested(node_name)
            await ensure_budget_baseline()
            deadline = getattr(state, "run_deadline", None)
            observation: Optional[DeadlineObservation] = None
            if deadline is not None and hasattr(state, "model_copy"):
                observed_deadline, observation = observe_run_deadline(deadline)
                state = state.model_copy(update={"run_deadline": observed_deadline})
                current_run_deadline.set(observed_deadline)
                if node_name in _DEADLINE_BLOCKED_WORKER_NODES and _worker_window_closed(
                    node_name, observation
                ):
                    # Do not enter a worker once the window its model calls draw
                    # on is closed — research and composition close separately.
                    return _blocked_research_worker_update(
                        node_name=node_name,
                        observed_deadline=observed_deadline,
                        observation=observation,
                    )
            handle = run_control_registry.get(run_id)
            if (
                handle is not None
                and handle.delivery_ready_event.is_set()
                and node_name in _DEADLINE_BLOCKED_WORKER_NODES
            ):
                # A detached/late worker must not overwrite a durable Bundle.
                return {
                    "run_deadline": getattr(state, "run_deadline", None),
                    "agent_status": {node_name: "ignored_after_delivery"},
                }
            claimed_supplements = handle.pending_supplements() if handle is not None else []
            fresh_supplements: List[Dict[str, str]] = []
            supplements_merged = bool(claimed_supplements) and hasattr(state, "model_copy")
            if supplements_merged:
                existing = list(getattr(state, "supplemental_requirements", None) or [])
                # 命令至少消费一次，所以去重的判据要在 state 里：同一条要求被重复 claim 时
                # （标记 consumed 之后崩溃）不该在调研提示里出现第二遍。
                merged_ids = {
                    str(item.get("command_id"))
                    for item in existing
                    if item.get("command_id")
                }
                fresh_supplements = [
                    item
                    for item in claimed_supplements
                    if str(item.get("command_id")) not in merged_ids
                ]
                if fresh_supplements:
                    state = state.model_copy(
                        update={"supplemental_requirements": [*existing, *fresh_supplements]}
                    )
            await emit_node_lifecycle("started", node=node_name)
            result = fn(state, *args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            supplements_landed = supplements_merged
            if (
                handle is not None
                and handle.delivery_ready_event.is_set()
                and node_name in _DEADLINE_BLOCKED_WORKER_NODES
            ):
                # The finalizer may win while an externally scheduled worker
                # is returning. Drop that stale update rather than merging it
                # into a checkpoint that already owns a Bundle identity.
                result = {
                    "run_deadline": getattr(state, "run_deadline", None),
                    "agent_status": {node_name: "ignored_after_delivery"},
                }
                # 这个更新被丢掉了，追加要求也就没有生效。留在 handle 里等下一个边界，
                # 或者由运行终结时的收口判成 rejected —— 不许标成已消费。
                supplements_landed = False
            elif fresh_supplements and isinstance(result, dict):
                # 只并进节点入参不算「纳入 state」：LangGraph 落盘的是节点**返回**的更新。
                # 追加要求要随这次更新进 checkpoint，后续节点才看得见（append-only reducer）。
                result = dict(result)
                result["supplemental_requirements"] = [
                    *(result.get("supplemental_requirements") or []),
                    *fresh_supplements,
                ]
            elif fresh_supplements:
                supplements_landed = False
            if (
                handle is not None
                and node_name == "delivery_finalizer"
                and isinstance(result, dict)
                and result.get("delivery_persisted") is True
            ):
                handle.mark_delivery_ready()
            # Keep a durable, non-decreasing checkpoint observation at every
            # successful graph boundary.  A node that explicitly replaces or
            # clears its deadline (approval/edit) owns that state transition.
            if (
                deadline is not None
                and isinstance(result, dict)
                and "run_deadline" not in result
            ):
                result = dict(result)
                result["run_deadline"] = observe_run_deadline(
                    getattr(state, "run_deadline")
                )[0]
            if handle is not None and supplements_landed:
                try:
                    await handle.mark_supplements_applied(
                        [
                            str(item.get("command_id"))
                            for item in claimed_supplements
                            if item.get("command_id")
                        ],
                        node=node_name,
                    )
                except Exception as settle_err:
                    # 标记生效失败不能吃掉节点已经算出来的结果：那是几分钟的模型与工具
                    # 调用。命令留在 claimed，执行器停下来时归还，下一个边界再标一次。
                    logger.warning(
                        "追加要求标记生效失败 run_id=%s node=%s error=%s",
                        run_id,
                        node_name,
                        settle_err,
                    )
            await emit_node_lifecycle(
                "completed",
                node=node_name,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )
            return result
        except (GraphInterrupt, RunCancelled, asyncio.CancelledError):
            raise
        except Exception as exc:
            await emit_node_lifecycle(
                "failed",
                node=node_name,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
                error_type=type(exc).__name__,
            )
            raise
        finally:
            node_timing_registry.record(run_id, node_name, {
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "ts_ms": ts_ms,
            })
            current_agent.reset(token_agent)
            current_node.reset(token_node)
            current_run_id.reset(token_run)
            current_run_deadline.reset(token_deadline)
            current_run_budget.reset(token_budget)
            current_model_window.reset(token_window)

    return _wrapped
