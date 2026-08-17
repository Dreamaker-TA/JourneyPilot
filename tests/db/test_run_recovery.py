"""启动恢复：进程死掉之后每一种残局各自收敛到什么。

kill-point 在这里用**数据库层的残局**表达：一个被 SIGKILL 的进程留下的全部痕迹就是
「trip_runs 说 running、租约过期、checkpoint 在或不在」。造出这些行比真去 kill 一个
uvicorn 更准 —— 每一种残局都能精确复现，而不是靠时序碰运气。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from travel_agent.entities.trip_run import (
    RunRecoveryStatus,
    TripRunStatus,
    available_run_actions,
)
from travel_agent.infrastructure.database import get_db_session
from travel_agent.infrastructure.run_execution_store import RunExecutionStore
from travel_agent.infrastructure.trip_run_store import TripRunStore
from travel_agent.services.run_recovery import RunRecoveryService

pytestmark = pytest.mark.postgres


async def _orphan_run(
    *,
    status: TripRunStatus,
    mode: str = "deep",
    resume_policy: str = "checkpoint",
    with_execution_row: bool = True,
) -> str:
    """造一个「没有活着的执行器」的 run。"""

    store = TripRunStore()
    run = await store.create_run(
        session_id="s-recovery",
        user_id="local",
        mode=mode,
        resume_policy=resume_policy,
    )
    if status != TripRunStatus.CREATED:
        await store.transition_status(run.run_id, TripRunStatus.RUNNING)
    if status not in {TripRunStatus.CREATED, TripRunStatus.RUNNING}:
        await store.transition_status(run.run_id, status)
    if with_execution_row:
        execution_store = RunExecutionStore()
        await execution_store.claim(run.run_id, lease_seconds=45)
        async with get_db_session() as session:
            await session.execute(
                text(
                    "UPDATE trip_run_executions "
                    "SET lease_expires_at = NOW() - INTERVAL '1 second' "
                    "WHERE run_id = :run_id"
                ),
                {"run_id": run.run_id},
            )
    return run.run_id


async def _set_completion_audit(run_id: str, audit: dict) -> None:
    import json

    async with get_db_session() as session:
        await session.execute(
            text(
                "UPDATE trip_run_states SET completion_audit = CAST(:audit AS jsonb) "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id, "audit": json.dumps(audit)},
        )


def _service(*, checkpoint_available: bool | None = True) -> RunRecoveryService:
    """`checkpoint_available=None` 表示这个部署没有 checkpointer。"""

    async def probe(_run_id: str) -> bool:
        return bool(checkpoint_available)

    return RunRecoveryService(
        trip_run_store=TripRunStore(),
        execution_store=RunExecutionStore(),
        checkpoint_probe=probe if checkpoint_available is not None else None,
    )


async def test_running_with_checkpoint_becomes_resume_available(migrated_async_database):
    run_id = await _orphan_run(status=TripRunStatus.RUNNING)

    report = await _service().sweep()

    assert [outcome.run_id for outcome in report.outcomes] == [run_id]
    outcome = report.outcomes[0]
    assert outcome.resolved_status == TripRunStatus.INTERRUPTED.value
    assert outcome.recovery_status == RunRecoveryStatus.RESUME_AVAILABLE.value
    assert outcome.reason == "process_restarted"

    run = await TripRunStore().get_run(run_id)
    execution = await RunExecutionStore().get(run_id)
    assert run is not None and run.status is TripRunStatus.INTERRUPTED
    assert execution is not None
    assert execution.recovery_status is RunRecoveryStatus.RESUME_AVAILABLE
    # 恢复必须是一次显式点击，所以「继续」要出现在可用动作里。
    assert available_run_actions(run, execution) == ["resume"]


async def test_running_without_checkpoint_is_non_resumable(migrated_async_database):
    run_id = await _orphan_run(status=TripRunStatus.RUNNING)

    report = await _service(checkpoint_available=False).sweep()

    outcome = report.outcomes[0]
    assert outcome.recovery_status == RunRecoveryStatus.NON_RESUMABLE.value
    assert outcome.reason == "process_restarted_without_checkpoint"

    run = await TripRunStore().get_run(run_id)
    execution = await RunExecutionStore().get(run_id)
    assert run is not None and run.status is TripRunStatus.INTERRUPTED
    # 不可恢复的 run 不许亮着「继续」：点下去只会拿到 409。
    assert available_run_actions(run, execution) == []


async def test_running_without_checkpointer_is_non_resumable(migrated_async_database):
    await _orphan_run(status=TripRunStatus.RUNNING)

    report = await _service(checkpoint_available=None).sweep()

    assert report.outcomes[0].reason == "checkpointer_unavailable"
    assert report.outcomes[0].recovery_status == RunRecoveryStatus.NON_RESUMABLE.value


async def test_checkpoint_contract_mismatch_is_non_resumable(migrated_async_database):
    """当前合同读不懂旧 checkpoint → 明确不可恢复，而不是在工作流中途炸开。"""

    await _orphan_run(status=TripRunStatus.RUNNING)

    async def probe(_run_id: str) -> bool:
        raise RuntimeError("checkpoint does not satisfy the current contract")

    service = RunRecoveryService(
        trip_run_store=TripRunStore(),
        execution_store=RunExecutionStore(),
        checkpoint_probe=probe,
    )
    report = await service.sweep()

    assert report.outcomes[0].reason == "checkpoint_contract_mismatch"
    assert not report.failures


async def test_clarify_only_run_is_non_resumable(migrated_async_database):
    await _orphan_run(status=TripRunStatus.RUNNING, resume_policy="clarify_only")

    report = await _service().sweep()

    assert report.outcomes[0].reason == "run_has_no_checkpoint_resume_policy"


async def test_cancel_requested_converges_to_cancelled(migrated_async_database):
    run_id = await _orphan_run(status=TripRunStatus.CANCEL_REQUESTED)

    report = await _service().sweep()

    assert report.outcomes[0].resolved_status == TripRunStatus.CANCELLED.value
    run = await TripRunStore().get_run(run_id)
    assert run is not None and run.status is TripRunStatus.CANCELLED


async def test_awaiting_input_keeps_its_status_and_loses_its_lease(
    migrated_async_database,
):
    """等用户的 Run 不需要执行器：清租约，状态一个字都不动。"""

    run_id = await _orphan_run(status=TripRunStatus.AWAITING_INPUT)

    report = await _service().sweep()

    assert report.outcomes[0].resolved_status == "unchanged"
    assert report.outcomes[0].recovery_status == RunRecoveryStatus.RELEASED.value
    run = await TripRunStore().get_run(run_id)
    execution = await RunExecutionStore().get(run_id)
    assert run is not None and run.status is TripRunStatus.AWAITING_INPUT
    assert execution is not None and execution.lease_token is None


async def test_completed_run_is_never_downgraded(migrated_async_database):
    """durable completion 赢过执行记录：只清租约，绝不重跑、绝不改状态。"""

    run_id = await _orphan_run(status=TripRunStatus.RUNNING)
    store = TripRunStore()
    await _set_completion_audit(
        run_id, {"formal_delivery": {"has_bundle": True, "bundle_id": "bundle-1"}}
    )
    async with get_db_session() as session:
        # 深度 Run 的 COMPLETED 只能由 complete_delivery() 写，这里直接造终态行以
        # 表达「Bundle 已原子完成，只是执行行还留着」这个残局。
        await session.execute(
            text("UPDATE trip_runs SET status = 'completed' WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
        await session.execute(
            text("UPDATE trip_run_states SET status = 'completed' WHERE run_id = :run_id"),
            {"run_id": run_id},
        )

    report = await _service().sweep()

    assert report.outcomes[0].recovery_status == RunRecoveryStatus.RELEASED.value
    run = await store.get_run(run_id)
    assert run is not None and run.status is TripRunStatus.COMPLETED


async def test_completed_without_bundle_is_a_contract_failure(migrated_async_database):
    """状态说完成、审计里没有 Bundle → 不猜，只标诊断，状态不动。"""

    run_id = await _orphan_run(status=TripRunStatus.RUNNING)
    async with get_db_session() as session:
        await session.execute(
            text("UPDATE trip_runs SET status = 'completed' WHERE run_id = :run_id"),
            {"run_id": run_id},
        )

    report = await _service().sweep()

    outcome = report.outcomes[0]
    assert outcome.recovery_status == RunRecoveryStatus.RECOVERY_CONTRACT_FAILURE.value
    assert outcome.resolved_status == "unchanged"
    run = await TripRunStore().get_run(run_id)
    execution = await RunExecutionStore().get(run_id)
    assert run is not None and run.status is TripRunStatus.COMPLETED
    assert execution is not None
    assert execution.recovery_reason == "completed_without_durable_bundle"


async def test_running_run_without_any_execution_row_is_recovered(
    migrated_async_database,
):
    """P1 之前的历史 run 没有执行行。它们同样不许永久停在 running。"""

    run_id = await _orphan_run(status=TripRunStatus.RUNNING, with_execution_row=False)

    report = await _service().sweep()

    assert [outcome.run_id for outcome in report.outcomes] == [run_id]
    execution = await RunExecutionStore().get(run_id)
    assert execution is not None
    assert execution.recovery_status is RunRecoveryStatus.RESUME_AVAILABLE


async def test_live_lease_is_left_alone(migrated_async_database):
    """租约还活着就不是孤儿 —— 否则第二个进程一启动就会掐死第一个进程的运行。"""

    store = TripRunStore()
    run = await store.create_run(
        session_id="s-recovery", user_id="local", mode="deep", resume_policy="checkpoint"
    )
    await store.transition_status(run.run_id, TripRunStatus.RUNNING)
    await RunExecutionStore().claim(run.run_id, lease_seconds=45)

    report = await _service().sweep()

    assert report.outcomes == ()
    current = await store.get_run(run.run_id)
    assert current is not None and current.status is TripRunStatus.RUNNING


async def test_sweeping_twice_reports_nothing_the_second_time(migrated_async_database):
    """普查幂等：第二次扫描不该把同一批 Run 再判一遍、再报一遍。"""

    await _orphan_run(status=TripRunStatus.RUNNING)
    await _orphan_run(status=TripRunStatus.CANCEL_REQUESTED)
    await _orphan_run(status=TripRunStatus.AWAITING_INPUT)
    service = _service()

    first = await service.sweep()
    second = await service.sweep()

    assert len(first.outcomes) == 3
    assert second.outcomes == ()
    assert not first.failures and not second.failures
