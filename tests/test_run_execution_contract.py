"""执行归属的纯逻辑合同：可用动作、执行器身份、租约配置边界。

**不需要 PostgreSQL**：这几条是判断规则本身，没有库也必须成立。
"""

from __future__ import annotations

import pytest

from travel_agent.entities.trip_run import (
    RunExecution,
    RunRecoveryStatus,
    TripRun,
    TripRunResumePolicy,
    TripRunStatus,
    available_run_actions,
)


def _run(status: TripRunStatus, *, policy: TripRunResumePolicy = TripRunResumePolicy.CHECKPOINT) -> TripRun:
    return TripRun(run_id="trip_x", status=status, resume_policy=policy)


@pytest.mark.parametrize(
    "status, expected",
    [
        (TripRunStatus.CREATED, ["resume", "cancel"]),
        (TripRunStatus.RUNNING, ["cancel"]),
        (TripRunStatus.AWAITING_INPUT, ["resume", "cancel"]),
        (TripRunStatus.CANCEL_REQUESTED, ["cancel"]),
        (TripRunStatus.INTERRUPTED, ["resume"]),
        (TripRunStatus.FAILED, ["resume"]),
        (TripRunStatus.COMPLETED, []),
        (TripRunStatus.CANCELLED, []),
    ],
)
def test_available_actions_per_status(status: TripRunStatus, expected: list[str]) -> None:
    assert available_run_actions(_run(status)) == expected


def test_clarify_only_run_cannot_be_resumed() -> None:
    """没有 checkpoint 策略的 run 不能续跑，只能重新开始一趟。"""

    run = _run(TripRunStatus.INTERRUPTED, policy=TripRunResumePolicy.CLARIFY_ONLY)
    assert available_run_actions(run) == []


@pytest.mark.parametrize(
    "recovery_status",
    [RunRecoveryStatus.NON_RESUMABLE, RunRecoveryStatus.RECOVERY_CONTRACT_FAILURE],
)
def test_recovery_verdict_removes_resume(recovery_status: RunRecoveryStatus) -> None:
    """恢复判定说不能续跑时，动作表里就不许留着「继续」。

    留着它等于让一个亮着的按钮点下去拿 409 —— 那正是「服务端说一次」要消灭的东西。
    """

    execution = RunExecution(run_id="trip_x", recovery_status=recovery_status)
    assert available_run_actions(_run(TripRunStatus.INTERRUPTED), execution) == []


def test_resume_available_keeps_resume() -> None:
    execution = RunExecution(
        run_id="trip_x", recovery_status=RunRecoveryStatus.RESUME_AVAILABLE
    )
    assert available_run_actions(_run(TripRunStatus.INTERRUPTED), execution) == ["resume"]


def test_executor_id_is_not_just_a_pid() -> None:
    """PID 会被复用；复用的 PID 不能让新进程冒充上一个租约持有者。"""

    from travel_agent.infrastructure.run_execution_store import _build_executor_id

    first = _build_executor_id()
    second = _build_executor_id()
    assert first != second, "同一个进程里两次生成的执行器身份不该相同"


def test_heartbeat_must_fit_inside_the_lease() -> None:
    """心跳间隔逼近租约长度时拒绝启动：丢一次心跳就失去租约不是配置，是故障。"""

    from travel_agent.config import RunControlConfig

    with pytest.raises(ValueError):
        RunControlConfig(lease_seconds=10, lease_heartbeat_seconds=9)

    assert RunControlConfig(lease_seconds=45, lease_heartbeat_seconds=10)


def test_unregister_only_removes_its_own_handle():
    """被接管的那条流清理时，不能把接管者的 handle 一起摘掉。

    盲删按 run_id 摘，摘掉的是接管者 —— 此后用户的取消在任何节点边界上都观察不到。
    """

    from travel_agent.workflows.run_control import RunControlRegistry

    registry = RunControlRegistry()
    losing = registry.register("run-1")
    winning = registry.register("run-1")

    registry.unregister("run-1", losing)

    assert registry.get("run-1") is winning
    registry.unregister("run-1", winning)
    assert registry.get("run-1") is None


def test_a_superseded_lease_keeper_does_not_evict_the_active_one():
    """输的那个 keeper 失效时，关闭时要交还的那份名单里不能少了接管者。"""

    from travel_agent.services import run_lease

    class _Keeper:
        def __init__(self, run_id: str) -> None:
            self._run_id = run_id

    losing = _Keeper("run-1")
    winning = _Keeper("run-1")
    run_lease._active_keepers["run-1"] = losing
    run_lease._active_keepers["run-1"] = winning
    try:
        run_lease._drop_active_keeper(losing)
        assert run_lease._active_keepers.get("run-1") is winning
        run_lease._drop_active_keeper(winning)
        assert "run-1" not in run_lease._active_keepers
    finally:
        run_lease._active_keepers.pop("run-1", None)


def test_the_node_window_table_covers_every_worker_the_graph_fans_out_to():
    """`run_control` 按节点名字面量分窗，而名字的主人是 `travel_planning`。

    它导不进那些常量（travel_planning 反过来导 run_control），所以那五个名字在第二个
    文件里被重新敲了一遍。加一个第四个调研 worker 或改一个名字，代码照样跑：那个节点
    静默落进 research 默认档，窗口关了不拦它，交付之后也不丢它的更新 —— 它能在自己被
    审计的 closeout 之后发起模型调用，并覆盖一份已经落库的 Bundle。

    这一条把「静默」换成「红灯」。
    """

    from travel_agent.workflows import run_control
    from travel_agent.workflows.travel_planning import (
        NODE_DELIVERY_FINALIZER,
        WORKER_NODES,
    )

    declared = run_control._DEADLINE_BLOCKED_WORKER_NODES
    assert declared == set(WORKER_NODES), (
        "dispatcher 扇出的 worker 与 run_control 的分窗表不一致：\n"
        f"  只在图里：{sorted(set(WORKER_NODES) - declared)}\n"
        f"  只在表里：{sorted(declared - set(WORKER_NODES))}"
    )

    research = run_control._RESEARCH_WORKER_NODES
    composition = run_control._COMPOSITION_WORKER_NODES
    assert research | composition == declared, "每个 worker 都要恰好属于一个窗口"
    assert not research & composition, "一个 worker 不能同时属于两个窗口"
    assert run_control._COMPOSITION_MODEL_WINDOW_NODES == {
        "itinerary_planner",
        "budget_estimate",
    }

    # `mark_delivery_ready` 由这个名字触发；它一改，每一处 delivery_ready 守卫同时失效。
    assert NODE_DELIVERY_FINALIZER == "delivery_finalizer"
