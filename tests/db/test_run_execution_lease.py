"""执行租约：抢占、续租、被接管、过期、交还。

这些不变量只在真实 PostgreSQL 上才有意义 —— 租约到没到期是 `NOW()` 说的，
而「两个执行器同时抢」靠的是一条 `ON CONFLICT ... WHERE` 的原子性。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from travel_agent.entities.trip_run import RunRecoveryStatus
from travel_agent.infrastructure.database import get_db_session
from travel_agent.infrastructure.run_execution_store import (
    EXECUTOR_ID,
    RunExecutionStore,
)
from travel_agent.infrastructure.trip_run_store import TripRunStore

pytestmark = pytest.mark.postgres


async def _create_run(*, mode: str = "deep", resume_policy: str = "checkpoint") -> str:
    run = await TripRunStore().create_run(
        session_id="s-lease",
        user_id="local",
        mode=mode,
        resume_policy=resume_policy,
    )
    return run.run_id


async def _expire_lease(run_id: str) -> None:
    """把租约推到过去。等 45 秒不是测试，是浪费。"""

    async with get_db_session() as session:
        await session.execute(
            text(
                "UPDATE trip_run_executions "
                "SET lease_expires_at = NOW() - INTERVAL '1 second' "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        )


async def _steal_lease(run_id: str) -> None:
    """模拟另一个进程接管：换掉 executor_id 与 token。"""

    async with get_db_session() as session:
        await session.execute(
            text(
                "UPDATE trip_run_executions "
                "SET executor_id = 'other-host:1:deadbeef', "
                "    lease_token = 'other-token', "
                "    lease_expires_at = NOW() + INTERVAL '60 seconds' "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        )


async def test_claim_records_this_executor(migrated_async_database):
    store = RunExecutionStore()
    run_id = await _create_run()

    execution = await store.claim(run_id, lease_seconds=45)

    assert execution is not None
    assert execution.executor_id == EXECUTOR_ID
    assert execution.lease_token
    assert execution.recovery_status is RunRecoveryStatus.CLAIMED
    assert execution.lease_expires_at and execution.heartbeat_at


async def test_second_executor_cannot_claim_a_live_lease(migrated_async_database):
    store = RunExecutionStore()
    run_id = await _create_run()

    assert await store.claim(run_id, lease_seconds=45) is not None
    await _steal_lease(run_id)

    # 别人的租约还活着 → 抢不到。用户误开两个终端时，这一条是唯一的拦阻。
    assert await store.claim(run_id, lease_seconds=45) is None


async def test_expired_lease_can_be_reclaimed(migrated_async_database):
    store = RunExecutionStore()
    run_id = await _create_run()

    await store.claim(run_id, lease_seconds=45)
    await _steal_lease(run_id)
    await _expire_lease(run_id)

    reclaimed = await store.claim(run_id, lease_seconds=45)
    assert reclaimed is not None
    assert reclaimed.executor_id == EXECUTOR_ID


async def test_same_executor_reclaim_is_idempotent(migrated_async_database):
    """同一个执行器重复 claim 必须成功 —— 否则一次重试就把自己锁在外面。"""

    store = RunExecutionStore()
    run_id = await _create_run()

    first = await store.claim(run_id, lease_seconds=45)
    second = await store.claim(run_id, lease_seconds=45)

    assert first is not None and second is not None
    assert second.lease_token != first.lease_token


async def test_heartbeat_extends_the_lease(migrated_async_database):
    store = RunExecutionStore()
    run_id = await _create_run()
    execution = await store.claim(run_id, lease_seconds=45)
    assert execution is not None

    await _expire_lease(run_id)
    renewed = await store.heartbeat(
        run_id, lease_token=execution.lease_token or "", lease_seconds=45
    )

    assert renewed is True
    after = await store.get(run_id)
    assert after is not None
    assert after.recovery_status is RunRecoveryStatus.RUNNING
    assert await store.count_active_leases() == 1


async def test_heartbeat_fails_after_takeover(migrated_async_database):
    """租约被接管后心跳必须失败 —— 那是「这个 run 已经不是我们的」的唯一信号。"""

    store = RunExecutionStore()
    run_id = await _create_run()
    execution = await store.claim(run_id, lease_seconds=45)
    assert execution is not None

    await _steal_lease(run_id)

    assert await store.heartbeat(
        run_id, lease_token=execution.lease_token or "", lease_seconds=45
    ) is False


async def test_release_clears_the_lease_but_keeps_the_row(migrated_async_database):
    store = RunExecutionStore()
    run_id = await _create_run()
    execution = await store.claim(run_id, lease_seconds=45)
    assert execution is not None

    released = await store.release(
        run_id,
        lease_token=execution.lease_token,
        recovery_status=RunRecoveryStatus.RELEASED,
        recovery_reason="stream_finished",
    )

    assert released is True
    after = await store.get(run_id)
    assert after is not None
    assert after.lease_token is None
    assert after.lease_expires_at is None
    assert after.recovery_status is RunRecoveryStatus.RELEASED
    assert after.recovery_reason == "stream_finished"
    assert await store.count_active_leases() == 0


async def test_release_with_a_stale_token_does_not_touch_the_new_owner(
    migrated_async_database,
):
    store = RunExecutionStore()
    run_id = await _create_run()
    execution = await store.claim(run_id, lease_seconds=45)
    assert execution is not None
    await _steal_lease(run_id)

    assert await store.release(run_id, lease_token=execution.lease_token) is False
    after = await store.get(run_id)
    assert after is not None and after.lease_token == "other-token"


async def test_safe_checkpoint_is_recorded_under_the_current_lease(
    migrated_async_database,
):
    store = RunExecutionStore()
    run_id = await _create_run()
    execution = await store.claim(run_id, lease_seconds=45)
    assert execution is not None

    assert await store.mark_safe_checkpoint(
        run_id, lease_token=execution.lease_token or "", checkpoint_id="ckpt-1"
    )
    assert not await store.mark_safe_checkpoint(
        run_id, lease_token="not-our-token", checkpoint_id="ckpt-2"
    )

    after = await store.get(run_id)
    assert after is not None and after.last_safe_checkpoint_id == "ckpt-1"


async def test_claim_preserves_a_previously_recorded_safe_checkpoint(
    migrated_async_database,
):
    """重新 claim 不该把上一段执行留下的安全边界抹掉 —— 恢复要读它。"""

    store = RunExecutionStore()
    run_id = await _create_run()
    execution = await store.claim(run_id, lease_seconds=45)
    assert execution is not None
    await store.mark_safe_checkpoint(
        run_id, lease_token=execution.lease_token or "", checkpoint_id="ckpt-1"
    )
    await _expire_lease(run_id)

    reclaimed = await store.claim(run_id, lease_seconds=45)
    assert reclaimed is not None and reclaimed.last_safe_checkpoint_id == "ckpt-1"


async def test_execution_row_disappears_with_its_run(migrated_async_database):
    """外键级联：run 被删掉时执行行不该留成孤儿。"""

    store = RunExecutionStore()
    run_id = await _create_run()
    await store.claim(run_id, lease_seconds=45)

    async with get_db_session() as session:
        await session.execute(
            text("DELETE FROM trip_runs WHERE run_id = :run_id"), {"run_id": run_id}
        )

    assert await store.get(run_id) is None
