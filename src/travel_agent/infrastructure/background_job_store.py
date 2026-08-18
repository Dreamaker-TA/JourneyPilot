"""后台任务的持久化。

最终事实在 `background_jobs`：入队是一次写库，进程换了任务还在。租约与可领取时间
一律由 PostgreSQL 的 `NOW()` 判定，与执行租约同一个时钟口径。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import bindparam, text

from ..entities.background_job import (
    OPEN_BACKGROUND_JOB_STATUSES,
    BackgroundJob,
    BackgroundJobStatus,
    BackgroundJobType,
    coerce_job_status,
    coerce_job_type,
    generate_background_job_id,
)
from .database import get_db_session
from .run_execution_store import EXECUTOR_ID

_OPEN_STATUS_VALUES = tuple(sorted(status.value for status in OPEN_BACKGROUND_JOB_STATUSES))
#: 可领取的状态。`running` 也在里面：一条租约已过期的 running 就是「上一个进程崩在
#: 半路」，重新领走它是唯一的恢复路径。活着的执行中任务由租约条件挡住。
_CLAIMABLE_STATUS_VALUES = (
    BackgroundJobStatus.PENDING.value,
    BackgroundJobStatus.RETRY_WAIT.value,
    BackgroundJobStatus.RUNNING.value,
)
#: 积压统计只算还没轮到的，不算正在跑的。
_BACKLOG_STATUS_VALUES = (
    BackgroundJobStatus.PENDING.value,
    BackgroundJobStatus.RETRY_WAIT.value,
)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.isoformat()
    text_value = str(value).strip()
    return text_value or None


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _job_from_row(row: Mapping[str, Any]) -> BackgroundJob:
    payload = row.get("payload")
    result = row.get("result")
    return BackgroundJob(
        job_id=row["job_id"],
        job_type=coerce_job_type(row["job_type"]),
        dedupe_key=row["dedupe_key"],
        payload=dict(payload) if isinstance(payload, Mapping) else {},
        status=coerce_job_status(row["status"]),
        priority=int(row.get("priority") or 100),
        attempts=int(row.get("attempts") or 0),
        max_attempts=int(row.get("max_attempts") or 5),
        available_at=_iso(row.get("available_at")),
        lease_owner=row.get("lease_owner"),
        lease_expires_at=_iso(row.get("lease_expires_at")),
        last_error_code=row.get("last_error_code"),
        last_error_summary=row.get("last_error_summary"),
        result=dict(result) if isinstance(result, Mapping) else None,
        created_at=_iso(row.get("created_at")) or "",
        updated_at=_iso(row.get("updated_at")) or "",
        completed_at=_iso(row.get("completed_at")),
    )


class BackgroundJobStore:
    """`background_jobs` 的仓储。"""

    async def enqueue(
        self,
        job_type: str | BackgroundJobType,
        dedupe_key: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        priority: int = 100,
        max_attempts: int = 5,
    ) -> tuple[BackgroundJob, bool]:
        """写入一条任务，返回 `(任务, 是否新建)`。

        `(job_type, dedupe_key)` 唯一：同一轮对话重发不会排出第二个抽取。
        """

        kind = coerce_job_type(job_type)
        async with get_db_session() as session:
            inserted = await session.execute(
                text(
                    """
                    INSERT INTO background_jobs
                        (job_id, job_type, dedupe_key, payload, status, priority,
                         max_attempts, available_at, created_at, updated_at)
                    VALUES
                        (:job_id, :job_type, :dedupe_key, CAST(:payload AS jsonb),
                         :status, :priority, :max_attempts, NOW(), NOW(), NOW())
                    ON CONFLICT (job_type, dedupe_key) DO NOTHING
                    RETURNING *
                    """
                ),
                {
                    "job_id": generate_background_job_id(),
                    "job_type": kind.value,
                    "dedupe_key": dedupe_key,
                    "payload": _dumps(dict(payload or {})),
                    "status": BackgroundJobStatus.PENDING.value,
                    "priority": priority,
                    "max_attempts": max(1, max_attempts),
                },
            )
            row = inserted.mappings().first()
            if row is not None:
                return _job_from_row(dict(row)), True
            existing = await session.execute(
                text(
                    "SELECT * FROM background_jobs "
                    "WHERE job_type = :job_type AND dedupe_key = :dedupe_key"
                ),
                {"job_type": kind.value, "dedupe_key": dedupe_key},
            )
            return _job_from_row(dict(existing.mappings().first())), False

    async def get(self, job_id: str) -> Optional[BackgroundJob]:
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT * FROM background_jobs WHERE job_id = :job_id"),
                {"job_id": job_id},
            )
            row = result.mappings().first()
            return _job_from_row(dict(row)) if row else None

    async def claim(self, *, lease_seconds: int, batch: int = 1) -> List[BackgroundJob]:
        """领取可执行的任务。

        `FOR UPDATE SKIP LOCKED`：同一进程里两个 worker 协程同时轮询是可能的，
        而同一个抽取跑两遍意味着一次多余的模型调用。过期租约的行会重新变得可领取，
        这就是「claim 之后崩溃」的恢复路径。
        """

        statement = text(
            """
            WITH claimable AS (
                SELECT job_id
                FROM background_jobs
                WHERE status IN :claimable_statuses
                  AND available_at <= NOW()
                  AND (lease_expires_at IS NULL OR lease_expires_at < NOW())
                ORDER BY priority ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :batch
            )
            UPDATE background_jobs j
            SET status = :running,
                attempts = j.attempts + 1,
                lease_owner = :lease_owner,
                lease_expires_at = NOW() + (:lease_seconds * INTERVAL '1 second'),
                updated_at = NOW()
            FROM claimable
            WHERE j.job_id = claimable.job_id
            RETURNING j.*
            """
        ).bindparams(bindparam("claimable_statuses", expanding=True))
        async with get_db_session() as session:
            result = await session.execute(
                statement,
                {
                    "claimable_statuses": list(_CLAIMABLE_STATUS_VALUES),
                    "running": BackgroundJobStatus.RUNNING.value,
                    "lease_owner": EXECUTOR_ID,
                    "lease_seconds": max(1, lease_seconds),
                    "batch": max(1, min(batch, 100)),
                },
            )
            jobs = [_job_from_row(dict(row)) for row in result.mappings().all()]
            jobs.sort(key=lambda job: (job.priority, job.created_at, job.job_id))
            return jobs

    async def renew_lease(self, job_id: str, *, lease_seconds: int) -> bool:
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE background_jobs
                    SET lease_expires_at = NOW() + (:lease_seconds * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE job_id = :job_id
                      AND lease_owner = :lease_owner
                      AND status = :running
                    RETURNING job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "lease_owner": EXECUTOR_ID,
                    "lease_seconds": max(1, lease_seconds),
                    "running": BackgroundJobStatus.RUNNING.value,
                },
            )
            return result.mappings().first() is not None

    async def complete(self, job_id: str, *, result: Optional[Dict[str, Any]] = None) -> bool:
        async with get_db_session() as session:
            updated = await session.execute(
                text(
                    """
                    UPDATE background_jobs
                    SET status = :completed,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        result = CAST(:result AS jsonb),
                        last_error_code = NULL,
                        last_error_summary = NULL,
                        completed_at = NOW(),
                        updated_at = NOW()
                    -- 认租约主人：租约过期后任务会被另一个 worker 领走，而先跑完的
                    -- 那个不该把后者的结果盖掉。
                    WHERE job_id = :job_id
                      AND status = :running
                      AND lease_owner = :lease_owner
                    RETURNING job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "lease_owner": EXECUTOR_ID,
                    "completed": BackgroundJobStatus.COMPLETED.value,
                    "running": BackgroundJobStatus.RUNNING.value,
                    "result": _dumps(result) if result is not None else None,
                },
            )
            return updated.mappings().first() is not None

    async def fail(
        self,
        job_id: str,
        *,
        error_code: str,
        error_summary: str,
        retry_in_seconds: Optional[int],
    ) -> BackgroundJobStatus:
        """记下失败。`retry_in_seconds=None` 表示不再重试，直接进 dead。

        重试上限也在这里判：`attempts` 已在 claim 时自增，达到 `max_attempts` 就 dead。
        """

        async with get_db_session() as session:
            updated = await session.execute(
                text(
                    """
                    UPDATE background_jobs
                    SET status = CASE
                            WHEN CAST(:retry_seconds AS INTEGER) IS NULL THEN :dead
                            WHEN attempts >= max_attempts THEN :dead
                            ELSE :retry_wait
                        END,
                        available_at =
                            NOW() + (COALESCE(CAST(:retry_seconds AS INTEGER), 0)
                                     * INTERVAL '1 second'),
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        last_error_code = :error_code,
                        last_error_summary = :error_summary,
                        updated_at = NOW()
                    WHERE job_id = :job_id
                      AND status = :running
                      AND lease_owner = :lease_owner
                    RETURNING status
                    """
                ),
                {
                    "job_id": job_id,
                    "lease_owner": EXECUTOR_ID,
                    "running": BackgroundJobStatus.RUNNING.value,
                    "dead": BackgroundJobStatus.DEAD.value,
                    "retry_wait": BackgroundJobStatus.RETRY_WAIT.value,
                    "retry_seconds": retry_in_seconds,
                    "error_code": error_code,
                    "error_summary": error_summary[:500],
                },
            )
            row = updated.mappings().first()
            return coerce_job_status(row["status"]) if row else BackgroundJobStatus.DEAD

    async def retry_dead(self, job_id: str) -> bool:
        """把一条 dead 任务放回队列（诊断页的重试按钮）。"""

        async with get_db_session() as session:
            updated = await session.execute(
                text(
                    """
                    UPDATE background_jobs
                    SET status = :pending,
                        attempts = 0,
                        available_at = NOW(),
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = NOW()
                    WHERE job_id = :job_id
                      AND status = :dead
                    RETURNING job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "pending": BackgroundJobStatus.PENDING.value,
                    "dead": BackgroundJobStatus.DEAD.value,
                },
            )
            return updated.mappings().first() is not None

    async def cancel_for_session(self, session_id: str) -> int:
        """会话被删除时取消引用它的未完成任务。

        正在跑的那一条也取消：worker 在写入前会重新校验源消息还在，读不到就自己收口。
        """

        statement = text(
            """
            UPDATE background_jobs
            SET status = :cancelled,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_error_code = 'session_deleted',
                updated_at = NOW()
            WHERE payload ->> 'session_id' = :session_id
              AND status IN :open_statuses
            RETURNING job_id
            """
        ).bindparams(bindparam("open_statuses", expanding=True))
        async with get_db_session() as session:
            updated = await session.execute(
                statement,
                {
                    "session_id": session_id,
                    "cancelled": BackgroundJobStatus.CANCELLED.value,
                    "open_statuses": list(_OPEN_STATUS_VALUES),
                },
            )
            return len(updated.mappings().all())

    async def counts_by_type_status(self) -> Dict[str, Dict[str, int]]:
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    "SELECT job_type, status, count(*) AS job_count "
                    "FROM background_jobs GROUP BY job_type, status"
                )
            )
            counts: Dict[str, Dict[str, int]] = {}
            for row in result.mappings().all():
                bucket = counts.setdefault(str(row["job_type"]), {})
                bucket[str(row["status"])] = int(row["job_count"] or 0)
            return counts

    async def oldest_pending_seconds(self) -> Optional[float]:
        statement = text(
            """
            SELECT EXTRACT(EPOCH FROM (NOW() - MIN(created_at))) AS age
            FROM background_jobs
            WHERE status IN :backlog_statuses
            """
        ).bindparams(bindparam("backlog_statuses", expanding=True))
        async with get_db_session() as session:
            result = await session.execute(
                statement, {"backlog_statuses": list(_BACKLOG_STATUS_VALUES)}
            )
            age = result.scalar()
            return float(age) if age is not None else None

    async def cleanup_completed(self, *, retention_days: int) -> int:
        """删除过期的已完成任务。pending / retry_wait / running / dead 一律不动。"""

        async with get_db_session() as session:
            deleted = await session.execute(
                text(
                    """
                    DELETE FROM background_jobs
                    WHERE status = :completed
                      AND completed_at < NOW() - (:retention_days * INTERVAL '1 day')
                    RETURNING job_id
                    """
                ),
                {
                    "completed": BackgroundJobStatus.COMPLETED.value,
                    "retention_days": max(0, retention_days),
                },
            )
            return len(deleted.mappings().all())


_store: Optional[BackgroundJobStore] = None


def get_background_job_store() -> BackgroundJobStore:
    global _store
    if _store is None:
        _store = BackgroundJobStore()
    return _store
