"""Durable run commands：幂等入队、只被消费一次、结论不可改写、终结时不留 pending。

这些不变量只在真实 PostgreSQL 上才有意义：重发落回同一行靠的是一个唯一约束，
「同一条命令不会被两个协程各消费一次」靠的是 `FOR UPDATE SKIP LOCKED`。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from travel_agent.entities.trip_run import (
    RunCommandStatus,
    RunCommandType,
    TripRunStatus,
)
from travel_agent.infrastructure.database import get_db_session
from travel_agent.infrastructure.run_command_store import RunCommandStore
from travel_agent.infrastructure.run_execution_store import EXECUTOR_ID
from travel_agent.infrastructure.trip_run_store import TripRunStore

pytestmark = pytest.mark.postgres


async def _create_run() -> str:
    run = await TripRunStore().create_run(
        session_id="s-commands",
        user_id="local",
        mode="deep",
        resume_policy="checkpoint",
    )
    return run.run_id


async def test_cancel_is_one_command_however_many_times_it_is_clicked(
    migrated_async_database,
):
    """连点三次停止是一个意图：一行、一张回执。"""

    store = RunCommandStore()
    run_id = await _create_run()

    first, created_first = await store.enqueue(run_id, RunCommandType.CANCEL, {"n": 1})
    second, created_second = await store.enqueue(run_id, RunCommandType.CANCEL, {"n": 2})
    third, created_third = await store.enqueue(run_id, RunCommandType.CANCEL, {"n": 3})

    assert created_first is True
    assert created_second is False and created_third is False
    assert second.command_id == first.command_id == third.command_id
    # 第一次的 payload 就是这条命令的 payload：重放不改写已经接受的那次请求。
    assert first.payload == {"n": 1} and second.payload == {"n": 1}


async def test_same_supplement_text_replays_the_same_receipt(migrated_async_database):
    store = RunCommandStore()
    run_id = await _create_run()
    payload = {"category": "food", "content": "想吃本地早餐"}

    first, created_first = await store.enqueue(run_id, RunCommandType.SUPPLEMENT, payload)
    replay, created_replay = await store.enqueue(run_id, RunCommandType.SUPPLEMENT, dict(payload))
    other, created_other = await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "food", "content": "少走路"}
    )

    assert created_first is True and created_replay is False and created_other is True
    assert replay.command_id == first.command_id
    assert other.command_id != first.command_id


async def test_the_same_intent_on_another_run_is_another_command(migrated_async_database):
    """摘要的唯一性是**按 run** 的：两趟旅行各自的取消不能互相顶掉。"""

    store = RunCommandStore()
    first_run, second_run = await _create_run(), await _create_run()

    first, _ = await store.enqueue(first_run, RunCommandType.CANCEL, {})
    second, created = await store.enqueue(second_run, RunCommandType.CANCEL, {})

    assert created is True
    assert first.command_id != second.command_id


async def test_claim_takes_pending_in_creation_order_and_only_once(
    migrated_async_database,
):
    store = RunCommandStore()
    run_id = await _create_run()
    first, _ = await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "pace", "content": "第一条"}
    )
    second, _ = await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "pace", "content": "第二条"}
    )

    claimed = await store.claim_pending(run_id)

    assert [command.command_id for command in claimed] == [
        first.command_id,
        second.command_id,
    ]
    assert all(command.status is RunCommandStatus.CLAIMED for command in claimed)
    assert all(command.claimed_by == EXECUTOR_ID for command in claimed)
    # 取走过就不再是待处理的：第二次轮询不该把同一条要求再交付一遍。
    assert await store.claim_pending(run_id) == []


async def test_two_pollers_do_not_claim_the_same_command(migrated_async_database):
    """同一进程里两个协程同时轮询同一个 run：一条命令只能落到一边。"""

    store = RunCommandStore()
    run_id = await _create_run()
    for index in range(4):
        await store.enqueue(
            run_id, RunCommandType.SUPPLEMENT, {"category": "other", "content": f"c{index}"}
        )

    left, right = await asyncio.gather(
        store.claim_pending(run_id), store.claim_pending(run_id)
    )

    ids = [command.command_id for command in [*left, *right]]
    assert len(ids) == len(set(ids)), "同一条命令被 claim 了两次"
    assert len(ids) == 4


async def test_settling_twice_keeps_the_first_conclusion(migrated_async_database):
    """标记之后崩溃、重启再标一次 —— 什么都不该发生。"""

    store = RunCommandStore()
    run_id = await _create_run()
    command, _ = await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "must_do", "content": "看日落"}
    )
    await store.claim_pending(run_id)

    assert await store.settle(
        [command.command_id],
        status=RunCommandStatus.CONSUMED,
        result={"applied_at_node": "planner"},
    ) == 1
    assert await store.settle(
        [command.command_id],
        status=RunCommandStatus.REJECTED,
        error_code="run_ended_before_consumption",
    ) == 0

    settled = await store.get(run_id, command.command_id)
    assert settled is not None
    assert settled.status is RunCommandStatus.CONSUMED
    assert settled.result == {"applied_at_node": "planner"}
    assert settled.error_code is None
    assert settled.consumed_at


async def test_run_end_leaves_no_command_pending(migrated_async_database):
    store = RunCommandStore()
    run_id = await _create_run()
    cancel, _ = await store.enqueue(run_id, RunCommandType.CANCEL, {})
    supplement, _ = await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "food", "content": "清淡一点"}
    )
    await store.claim_pending(run_id)

    consumed = await store.settle_open_for_run(
        run_id,
        status=RunCommandStatus.CONSUMED,
        command_types=[RunCommandType.CANCEL],
        result={"run_status": TripRunStatus.CANCELLED.value},
    )
    rejected = await store.settle_open_for_run(
        run_id,
        status=RunCommandStatus.REJECTED,
        error_code="run_ended_before_consumption",
        result={"run_status": TripRunStatus.CANCELLED.value},
    )

    assert [command.command_id for command in consumed] == [cancel.command_id]
    assert [command.command_id for command in rejected] == [supplement.command_id]
    assert await store.list_open(run_id) == []
    assert (await store.get(run_id, supplement.command_id)).error_code == (
        "run_ended_before_consumption"
    )


async def test_open_counts_are_reported_per_type(migrated_async_database):
    store = RunCommandStore()
    run_id = await _create_run()
    await store.enqueue(run_id, RunCommandType.CANCEL, {})
    await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "pace", "content": "慢一点"}
    )
    await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "pace", "content": "再慢一点"}
    )

    assert await store.count_open_by_type() == {"cancel": 1, "supplement": 2}

    await store.settle_open_for_run(run_id, status=RunCommandStatus.CONSUMED)
    assert await store.count_open_by_type() == {"cancel": 0, "supplement": 0}


async def test_cancel_requested_blocks_a_later_completion(migrated_async_database):
    """cancel/complete race 的权威规则：`cancel_requested` 锁定 Run 之后，交付边界不许提交。

    判据在状态机与 `FOR UPDATE`，不在内存 Event 的先后 —— 后者只能说明这个进程先看到了谁。
    """

    store = TripRunStore()
    run_id = await _create_run()
    await store.transition_status(run_id, TripRunStatus.RUNNING)
    await store.request_cancel(
        run_id,
        event_type="run.control_requested",
        payload={"action": "cancel", "source": "control_api"},
    )

    with pytest.raises(ValueError):
        await store.transition_status(run_id, TripRunStatus.COMPLETED)

    run = await store.get_run(run_id)
    assert run is not None and run.status is TripRunStatus.CANCEL_REQUESTED


async def test_a_finished_run_is_not_cancelled_after_the_fact(migrated_async_database):
    """交付先提交：取消拿到的答案是「已完成，无法取消」，而不是把终态改写一遍。"""

    store = TripRunStore()
    run = await store.create_run(
        session_id="s-commands", user_id="local", mode="fast", resume_policy="clarify_only"
    )
    await store.transition_status(run.run_id, TripRunStatus.RUNNING)
    await store.transition_status(run.run_id, TripRunStatus.COMPLETED)

    unchanged = await store.request_cancel(run.run_id)

    assert unchanged.status is TripRunStatus.COMPLETED


async def test_commands_disappear_with_their_run(migrated_async_database):
    """外键级联：run 被删掉时命令不该留成孤儿。"""

    store = RunCommandStore()
    run_id = await _create_run()
    command, _ = await store.enqueue(run_id, RunCommandType.CANCEL, {})

    async with get_db_session() as session:
        await session.execute(
            text("DELETE FROM trip_runs WHERE run_id = :run_id"), {"run_id": run_id}
        )

    assert await store.get(run_id, command.command_id) is None
