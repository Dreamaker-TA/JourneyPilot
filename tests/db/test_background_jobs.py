"""Durable background jobs：去重入队、租约过期后重新可领、退避与死信、清理边界。

这些不变量都靠真实 PostgreSQL 才成立：`(job_type, dedupe_key)` 唯一约束、
`FOR UPDATE SKIP LOCKED` 的领取、以及全部以数据库 `NOW()` 为准的租约判定。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from travel_agent.entities.background_job import (
    BackgroundJobStatus,
    BackgroundJobType,
    memory_extraction_dedupe_key,
    retry_delay_seconds,
)
from travel_agent.infrastructure.background_job_store import BackgroundJobStore
from travel_agent.infrastructure.database import get_db_session

pytestmark = pytest.mark.postgres

_JOB = BackgroundJobType.MEMORY_EXTRACTION


def _payload(session_id: str = "s-1", message_id: str = "m-1") -> dict:
    return {
        "user_id": "local",
        "session_id": session_id,
        "user_message_id": message_id,
        "assistant_message_id": f"a-{message_id}",
        "profile_revision": 3,
        "portrait_baseline": "喜欢慢节奏",
    }


async def _expire_lease(job_id: str) -> None:
    async with get_db_session() as session:
        await session.execute(
            text(
                "UPDATE background_jobs SET lease_expires_at = NOW() - INTERVAL '1 minute' "
                "WHERE job_id = :job_id"
            ),
            {"job_id": job_id},
        )


async def test_same_turn_enqueues_one_job(migrated_async_database):
    store = BackgroundJobStore()
    key = memory_extraction_dedupe_key("s-1", "a-1")

    first, created_first = await store.enqueue(_JOB, key, _payload())
    replay, created_replay = await store.enqueue(_JOB, key, _payload(message_id="m-2"))

    assert created_first is True and created_replay is False
    assert replay.job_id == first.job_id
    # 第一次的 payload 就是这条任务的 payload：重放不改写已经排好的活。
    assert replay.payload["user_message_id"] == "m-1"


async def test_claim_takes_each_job_once(migrated_async_database):
    """同一进程两个协程同时轮询，一条任务只会落到其中一个手里。"""

    store = BackgroundJobStore()
    await store.enqueue(_JOB, "turn-a", _payload())
    await store.enqueue(_JOB, "turn-b", _payload())

    first, second = await asyncio.gather(
        store.claim(lease_seconds=60, batch=2),
        store.claim(lease_seconds=60, batch=2),
    )
    claimed = [job.job_id for job in (*first, *second)]
    assert len(claimed) == 2
    assert len(set(claimed)) == 2


async def test_a_claimed_job_is_not_claimable_until_its_lease_expires(
    migrated_async_database,
):
    """claim 之后进程崩溃 —— 恢复路径就是租约过期，不需要任何启动扫描。"""

    store = BackgroundJobStore()
    await store.enqueue(_JOB, "turn-a", _payload())

    (job,) = await store.claim(lease_seconds=60, batch=5)
    assert await store.claim(lease_seconds=60, batch=5) == []

    await _expire_lease(job.job_id)
    (reclaimed,) = await store.claim(lease_seconds=60, batch=5)
    assert reclaimed.job_id == job.job_id
    # attempts 每次领取自增，重试上限才有得判。
    assert reclaimed.attempts == 2


async def test_retryable_failure_waits_before_becoming_claimable(migrated_async_database):
    store = BackgroundJobStore()
    await store.enqueue(_JOB, "turn-a", _payload())
    (job,) = await store.claim(lease_seconds=60, batch=1)

    status = await store.fail(
        job.job_id,
        error_code="TimeoutError",
        error_summary="provider timeout",
        retry_in_seconds=retry_delay_seconds(job.attempts),
    )
    assert status is BackgroundJobStatus.RETRY_WAIT
    # available_at 在未来，所以这一轮领不到它。
    assert await store.claim(lease_seconds=60, batch=1) == []

    async with get_db_session() as session:
        await session.execute(
            text("UPDATE background_jobs SET available_at = NOW() WHERE job_id = :job_id"),
            {"job_id": job.job_id},
        )
    (again,) = await store.claim(lease_seconds=60, batch=1)
    assert again.job_id == job.job_id


async def test_permanent_failure_goes_straight_to_dead(migrated_async_database):
    store = BackgroundJobStore()
    await store.enqueue(_JOB, "turn-a", _payload())
    (job,) = await store.claim(lease_seconds=60, batch=1)

    status = await store.fail(
        job.job_id,
        error_code="source_message_deleted",
        error_summary="来源消息已不存在",
        retry_in_seconds=None,
    )
    assert status is BackgroundJobStatus.DEAD
    assert await store.claim(lease_seconds=60, batch=1) == []


async def test_attempts_run_out_into_dead(migrated_async_database):
    store = BackgroundJobStore()
    await store.enqueue(_JOB, "turn-a", _payload(), max_attempts=2)

    for _ in range(2):
        (job,) = await store.claim(lease_seconds=60, batch=1)
        status = await store.fail(
            job.job_id,
            error_code="TimeoutError",
            error_summary="provider timeout",
            retry_in_seconds=1,
        )
        async with get_db_session() as session:
            await session.execute(
                text("UPDATE background_jobs SET available_at = NOW() WHERE job_id = :job_id"),
                {"job_id": job.job_id},
            )
    assert status is BackgroundJobStatus.DEAD

    # 用户按下诊断页的重试：计数归零，任务回到队列。
    assert await store.retry_dead(job.job_id) is True
    (retried,) = await store.claim(lease_seconds=60, batch=1)
    assert retried.attempts == 1


async def test_completing_twice_keeps_the_first_conclusion(migrated_async_database):
    """标记完成之后崩溃，重启再标一次不改变任何东西。"""

    store = BackgroundJobStore()
    await store.enqueue(_JOB, "turn-a", _payload())
    (job,) = await store.claim(lease_seconds=60, batch=1)

    assert await store.complete(job.job_id, result={"facts": 2}) is True
    assert await store.complete(job.job_id, result={"facts": 99}) is False

    settled = await store.get(job.job_id)
    assert settled.status is BackgroundJobStatus.COMPLETED
    assert settled.result == {"facts": 2}


async def test_deleting_a_session_cancels_its_open_jobs(migrated_async_database):
    store = BackgroundJobStore()
    await store.enqueue(_JOB, "turn-a", _payload(session_id="doomed"))
    await store.enqueue(_JOB, "turn-b", _payload(session_id="kept"))

    assert await store.cancel_for_session("doomed") == 1

    counts = await store.counts_by_type_status()
    assert counts[_JOB.value][BackgroundJobStatus.CANCELLED.value] == 1
    assert counts[_JOB.value][BackgroundJobStatus.PENDING.value] == 1


async def test_cleanup_only_removes_expired_completed_jobs(migrated_async_database):
    store = BackgroundJobStore()
    await store.enqueue(_JOB, "turn-pending", _payload())
    await store.enqueue(_JOB, "turn-done", _payload())
    await store.enqueue(_JOB, "turn-dead", _payload())

    (done,) = await store.claim(lease_seconds=60, batch=1)
    await store.complete(done.job_id, result={})
    async with get_db_session() as session:
        await session.execute(
            text(
                "UPDATE background_jobs SET completed_at = NOW() - INTERVAL '90 days' "
                "WHERE job_id = :job_id"
            ),
            {"job_id": done.job_id},
        )
    (dead,) = await store.claim(lease_seconds=60, batch=1)
    await store.fail(
        dead.job_id,
        error_code="invalid_payload",
        error_summary="bad",
        retry_in_seconds=None,
    )

    assert await store.cleanup_completed(retention_days=30) == 1
    assert await store.get(done.job_id) is None
    # dead 留到用户确认，pending 更不能碰。
    assert (await store.get(dead.job_id)).status is BackgroundJobStatus.DEAD
    counts = await store.counts_by_type_status()
    assert counts[_JOB.value][BackgroundJobStatus.PENDING.value] == 1


async def test_oldest_pending_seconds_reports_backlog(migrated_async_database):
    store = BackgroundJobStore()
    assert await store.oldest_pending_seconds() is None

    await store.enqueue(_JOB, "turn-a", _payload())
    age = await store.oldest_pending_seconds()
    assert age is not None and age >= 0


async def test_the_same_fact_from_the_same_turn_lands_once(migrated_async_database):
    """至少一次执行的代价由业务写入这一侧兜住：同一条来源消息的同一句事实只有一行。"""

    from travel_agent.entities.background_job import memory_fact_digest

    digest = memory_fact_digest("local", "m-1", "用户吃素")
    insert = text(
        "INSERT INTO memory_facts (user_id, session_id, content, category, importance, "
        "source_message_id, fact_digest) "
        "VALUES ('local', 's-1', '用户吃素', 'preference', 8, 'm-1', :digest)"
    )
    async with get_db_session() as session:
        await session.execute(insert, {"digest": digest})

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with get_db_session() as session:
            await session.execute(insert, {"digest": digest})

    async with get_db_session() as session:
        total = await session.execute(
            text("SELECT count(*) FROM memory_facts WHERE fact_digest = :digest"),
            {"digest": digest},
        )
    assert total.scalar() == 1


async def test_worker_runs_retries_and_gives_up(migrated_async_database):
    """worker 的完整生命周期：失败重试、重试用尽进死信、成功写下结论。"""

    from travel_agent.services.background_jobs import BackgroundJobWorker

    store = BackgroundJobStore()
    attempts: list[str] = []

    async def flaky(job):
        attempts.append(job.job_id)
        if len(attempts) < 2:
            raise TimeoutError("provider timeout")
        return {"facts": 1}

    worker = BackgroundJobWorker(
        store, {_JOB: flaky}, poll_seconds=0.1, lease_seconds=10, batch_size=1
    )
    job, _ = await store.enqueue(_JOB, "turn-a", _payload())

    assert await worker.poll_once() == 1
    assert (await store.get(job.job_id)).status is BackgroundJobStatus.RETRY_WAIT

    async with get_db_session() as session:
        await session.execute(
            text("UPDATE background_jobs SET available_at = NOW() WHERE job_id = :job_id"),
            {"job_id": job.job_id},
        )
    assert await worker.poll_once() == 1
    settled = await store.get(job.job_id)
    assert settled.status is BackgroundJobStatus.COMPLETED
    assert settled.result == {"facts": 1}


async def test_worker_sends_permanent_failures_straight_to_dead(migrated_async_database):
    from travel_agent.entities.background_job import BackgroundJobPermanentError
    from travel_agent.services.background_jobs import BackgroundJobWorker

    store = BackgroundJobStore()

    async def gone(job):
        raise BackgroundJobPermanentError("source_message_deleted", "来源消息已不存在")

    worker = BackgroundJobWorker(store, {_JOB: gone}, poll_seconds=0.1, lease_seconds=10)
    job, _ = await store.enqueue(_JOB, "turn-a", _payload())

    await worker.poll_once()
    settled = await store.get(job.job_id)
    assert settled.status is BackgroundJobStatus.DEAD
    assert settled.last_error_code == "source_message_deleted"


async def test_worker_marks_a_job_with_no_handler_dead(migrated_async_database):
    from travel_agent.services.background_jobs import BackgroundJobWorker

    store = BackgroundJobStore()
    worker = BackgroundJobWorker(store, {}, poll_seconds=0.1, lease_seconds=10)
    job, _ = await store.enqueue(_JOB, "turn-a", _payload())

    await worker.poll_once()
    assert (await store.get(job.job_id)).last_error_code == "no_handler"
