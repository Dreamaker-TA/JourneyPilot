"""取消与追加要求的 API 合同：接受与否只看 durable 事实。

这一批的核心是**「registry 里有没有 handle」不再参与判断**。之前它决定一切：追加要求会被
409 拒绝，取消会被就地判成「没人在跑」——而它只说明不是这个进程在跑。所以这些用例都在
一个**没有任何进程内 handle** 的环境里跑，行为必须仍然正确。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from travel_agent.api import schemas
from travel_agent.api.routes import trip_runs as routes
from travel_agent.entities.trip_run import (
    RunCommandStatus,
    RunCommandType,
    TripRunStatus,
)
from travel_agent.infrastructure.run_command_store import RunCommandStore
from travel_agent.infrastructure.run_execution_store import RunExecutionStore
from travel_agent.infrastructure.trip_run_store import TripRunStore
from travel_agent.services.run_commands import RunCommandCoordinator
from travel_agent.workflows.run_control import run_control_registry

pytestmark = pytest.mark.postgres


class _Components:
    """路由在这几条路径上真正用到的那几个 store，一个不多。"""

    def __init__(self) -> None:
        self.trip_run_store = TripRunStore()
        self.run_command_store = RunCommandStore()
        self.run_execution_store = RunExecutionStore()


@pytest.fixture
def components(monkeypatch):
    wired = _Components()
    monkeypatch.setattr(routes, "get_components", lambda: wired)
    # 每个用例都从「本进程没有跑任何 run」开始：这正是要验证的环境。
    run_control_registry.clear()
    return wired


async def _running_run(components: _Components, *, with_lease: bool) -> str:
    run = await components.trip_run_store.create_run(
        session_id="s-control", user_id="local", mode="deep", resume_policy="checkpoint"
    )
    await components.trip_run_store.transition_status(run.run_id, TripRunStatus.RUNNING)
    if with_lease:
        await components.run_execution_store.claim(run.run_id, lease_seconds=45)
    return run.run_id


def _cancel() -> schemas.TripRunControlRequest:
    return schemas.TripRunControlRequest(action="cancel")


async def test_cancel_is_accepted_with_a_live_executor_elsewhere(
    migrated_async_database, components
):
    """有执行器在跑：命令落库并等待协作退出，状态停在 cancel_requested。"""

    run_id = await _running_run(components, with_lease=True)

    receipt = await routes.control_trip_run(run_id, _cancel())

    assert receipt.accepted is True
    assert receipt.status == RunCommandStatus.PENDING.value
    assert receipt.run_status == TripRunStatus.CANCEL_REQUESTED.value
    command = await components.run_command_store.get(run_id, receipt.command_id)
    assert command is not None and command.command_type is RunCommandType.CANCEL
    # cancel_requested 已经写进 trip_runs：完成边界从此提交不了。
    with pytest.raises(ValueError):
        await components.trip_run_store.transition_status(run_id, TripRunStatus.COMPLETED)


async def test_cancel_without_a_live_lease_converges_immediately(
    migrated_async_database, components
):
    """没有活着的执行器：就地收敛，不让用户等下一轮恢复扫描。"""

    run_id = await _running_run(components, with_lease=False)

    receipt = await routes.control_trip_run(run_id, _cancel())

    assert receipt.run_status == TripRunStatus.CANCELLED.value
    assert receipt.status == RunCommandStatus.CONSUMED.value
    run = await components.trip_run_store.get_run(run_id)
    assert run is not None and run.status is TripRunStatus.CANCELLED
    assert await components.run_command_store.list_open(run_id) == []


async def test_clicking_stop_again_returns_the_same_receipt(
    migrated_async_database, components
):
    run_id = await _running_run(components, with_lease=True)

    first = await routes.control_trip_run(run_id, _cancel())
    second = await routes.control_trip_run(run_id, _cancel())

    assert second.command_id == first.command_id
    assert second.run_status == TripRunStatus.CANCEL_REQUESTED.value


async def test_cancelling_a_finished_run_is_refused_and_leaves_nothing_pending(
    migrated_async_database, components
):
    run = await components.trip_run_store.create_run(
        session_id="s-control", user_id="local", mode="fast", resume_policy="clarify_only"
    )
    await components.trip_run_store.transition_status(run.run_id, TripRunStatus.RUNNING)
    await components.trip_run_store.transition_status(run.run_id, TripRunStatus.COMPLETED)

    with pytest.raises(HTTPException) as excinfo:
        await routes.control_trip_run(run.run_id, _cancel())

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "run_not_cancellable"
    assert excinfo.value.detail["status"] == TripRunStatus.COMPLETED.value
    assert await components.run_command_store.list_open(run.run_id) == []


async def test_supplement_is_accepted_without_any_in_process_handle(
    migrated_async_database, components
):
    """这条以前会被 409 拒绝。要求先落库，执行器下一次轮询就看得见。"""

    run_id = await _running_run(components, with_lease=True)

    receipt = await routes.add_trip_run_supplement(
        run_id,
        schemas.TripRunSupplementRequest(category="food", content="想吃本地早餐"),
    )

    assert receipt.accepted is True
    assert receipt.status == RunCommandStatus.PENDING.value
    open_commands = await components.run_command_store.list_open(run_id)
    assert [command.command_id for command in open_commands] == [receipt.command_id]
    assert open_commands[0].payload["content"] == "想吃本地早餐"
    events = await components.trip_run_store.list_events(run_id, after_sequence=0, limit=50)
    queued = [event for event in events if event.event_type == "run.supplement_queued"]
    assert len(queued) == 1
    assert queued[0].payload["command_id"] == receipt.command_id


async def test_resending_the_same_supplement_does_not_queue_it_twice(
    migrated_async_database, components
):
    run_id = await _running_run(components, with_lease=True)
    request = schemas.TripRunSupplementRequest(category="pace", content="慢一点")

    first = await routes.add_trip_run_supplement(run_id, request)
    second = await routes.add_trip_run_supplement(run_id, request)

    assert second.command_id == first.command_id
    events = await components.trip_run_store.list_events(run_id, after_sequence=0, limit=50)
    assert len([e for e in events if e.event_type == "run.supplement_queued"]) == 1


async def test_supplement_needs_a_running_run(migrated_async_database, components):
    run = await components.trip_run_store.create_run(
        session_id="s-control", user_id="local", mode="deep", resume_policy="checkpoint"
    )

    with pytest.raises(HTTPException) as excinfo:
        await routes.add_trip_run_supplement(
            run.run_id,
            schemas.TripRunSupplementRequest(category="other", content="随便"),
        )

    assert excinfo.value.status_code == 409
    assert await components.run_command_store.list_open(run.run_id) == []


async def test_a_real_executor_picks_the_command_up_from_the_table(
    migrated_async_database, components
):
    """整条链路走一遍真表：API 落库 → 执行器 claim → 协作边界看到停止 → 命令收口。"""

    run_id = await _running_run(components, with_lease=True)
    handle = run_control_registry.register(run_id)
    coordinator = RunCommandCoordinator(
        components.run_command_store,
        components.trip_run_store,
        run_id,
        handle,
        poll_seconds=0.2,
    )

    receipt = await routes.control_trip_run(run_id, _cancel())
    # 通知只是唤醒：这里连 wake_event 都不看，直接按轮询那条路走。
    claimed = await coordinator.poll_once()

    assert [command.command_id for command in claimed] == [receipt.command_id]
    assert handle.cancel_event.is_set()

    await components.trip_run_store.transition_status(run_id, TripRunStatus.CANCELLED)
    await coordinator.settle_terminal(TripRunStatus.CANCELLED)

    settled = await components.run_command_store.get(run_id, receipt.command_id)
    assert settled is not None and settled.status is RunCommandStatus.CONSUMED
    assert await components.run_command_store.list_open(run_id) == []


async def test_command_receipt_can_be_read_after_a_disconnect(
    migrated_async_database, components
):
    run_id = await _running_run(components, with_lease=False)
    receipt = await routes.control_trip_run(run_id, _cancel())

    read = await routes.read_trip_run_command(run_id, receipt.command_id, session_id=None)

    assert read.command_id == receipt.command_id
    assert read.command_type == RunCommandType.CANCEL.value
    assert read.status == RunCommandStatus.CONSUMED.value
    assert read.run_status == TripRunStatus.CANCELLED.value
    assert read.consumed_at

    with pytest.raises(HTTPException) as excinfo:
        await routes.read_trip_run_command(run_id, "cmd_does_not_exist", session_id=None)
    assert excinfo.value.status_code == 404
