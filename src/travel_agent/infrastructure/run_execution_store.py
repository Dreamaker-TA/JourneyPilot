"""TripRun 执行租约的持久化。

**最终事实在 `trip_run_executions`**，进程内的 registry 只是低延迟唤醒通道。租约的
时间判断全部交给 PostgreSQL 的 `NOW()`：两个进程的墙钟可以差几秒，而「租约过期了吗」
必须只有一个答案。进程内的时长测量仍用 monotonic，那是另一回事。
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text

from ..entities.trip_run import (
    RunExecution,
    RunRecoveryStatus,
    TripRunStatus,
    coerce_recovery_status,
    coerce_status,
)
from .database import get_db_session


def _build_executor_id() -> str:
    """hostname + 进程随机 id + 进程启动时刻。

    **不用 PID**：PID 会被复用，而复用的 PID 会让一个新进程冒充上一个进程的租约持有者。
    """

    started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}:{started_at}"


#: 本进程的执行器身份。进程存活期间不变。
EXECUTOR_ID = _build_executor_id()

#: 本进程的启动时刻，写进执行行供诊断读取。
PROCESS_STARTED_AT = datetime.now(timezone.utc)

#: 恢复扫描已经处理完的状态。非活跃 Run 停在这些状态上就不再进入候选集。
_SETTLED_RECOVERY_STATUSES = (
    RunRecoveryStatus.IDLE,
    RunRecoveryStatus.RELEASED,
    RunRecoveryStatus.RESUME_AVAILABLE,
    RunRecoveryStatus.NON_RESUMABLE,
    RunRecoveryStatus.RECOVERY_CONTRACT_FAILURE,
    RunRecoveryStatus.SHUTDOWN_REQUESTED,
)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.isoformat()
    text_value = str(value).strip()
    return text_value or None


def _execution_from_row(row: Dict[str, Any]) -> RunExecution:
    return RunExecution(
        run_id=row["run_id"],
        executor_id=row.get("executor_id"),
        lease_token=row.get("lease_token"),
        lease_acquired_at=_iso(row.get("lease_acquired_at")),
        lease_expires_at=_iso(row.get("lease_expires_at")),
        heartbeat_at=_iso(row.get("heartbeat_at")),
        process_started_at=_iso(row.get("process_started_at")),
        last_safe_checkpoint_id=row.get("last_safe_checkpoint_id"),
        recovery_status=coerce_recovery_status(row.get("recovery_status") or "idle"),
        recovery_reason=row.get("recovery_reason"),
        updated_at=_iso(row.get("updated_at")) or "",
    )


@dataclass(frozen=True)
class RunRecoveryCandidate:
    """一个需要恢复判定的 run：业务状态 + 执行归属 + 完成审计，一次读齐。"""

    run_id: str
    status: TripRunStatus
    mode: str
    resume_policy: str
    current_node: Optional[str]
    execution: Optional[RunExecution]
    completion_audit: Dict[str, Any]


class RunExecutionStore:
    """`trip_run_executions` 的仓储。"""

    async def get(self, run_id: str) -> Optional[RunExecution]:
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT * FROM trip_run_executions WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            row = result.mappings().first()
            return _execution_from_row(dict(row)) if row else None

    async def claim(
        self,
        run_id: str,
        *,
        lease_seconds: int,
        last_safe_checkpoint_id: Optional[str] = None,
    ) -> Optional[RunExecution]:
        """抢占执行权。返回 None 表示已有活着的其他执行器。

        条件写而不是「先读再写」：单机产品也可能被开两个终端，而那两个进程之间没有
        任何协调手段，只有这一条 UPDATE 的 WHERE 子句。
        """

        lease_token = uuid.uuid4().hex
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO trip_run_executions
                        (run_id, executor_id, lease_token, lease_acquired_at,
                         lease_expires_at, heartbeat_at, process_started_at,
                         last_safe_checkpoint_id, recovery_status, recovery_reason,
                         updated_at)
                    VALUES
                        (:run_id, :executor_id, :lease_token, NOW(),
                         NOW() + (:lease_seconds * INTERVAL '1 second'), NOW(),
                         :process_started_at, :checkpoint_id, :claimed, NULL, NOW())
                    ON CONFLICT (run_id) DO UPDATE
                    SET executor_id = EXCLUDED.executor_id,
                        lease_token = EXCLUDED.lease_token,
                        lease_acquired_at = NOW(),
                        lease_expires_at = EXCLUDED.lease_expires_at,
                        heartbeat_at = NOW(),
                        process_started_at = EXCLUDED.process_started_at,
                        last_safe_checkpoint_id = COALESCE(
                            EXCLUDED.last_safe_checkpoint_id,
                            trip_run_executions.last_safe_checkpoint_id
                        ),
                        recovery_status = EXCLUDED.recovery_status,
                        recovery_reason = NULL,
                        updated_at = NOW()
                    WHERE trip_run_executions.lease_expires_at IS NULL
                       OR trip_run_executions.lease_expires_at < NOW()
                       OR trip_run_executions.executor_id = EXCLUDED.executor_id
                    RETURNING *
                    """
                ),
                {
                    "run_id": run_id,
                    "executor_id": EXECUTOR_ID,
                    "lease_token": lease_token,
                    "lease_seconds": max(1, lease_seconds),
                    "process_started_at": PROCESS_STARTED_AT,
                    "checkpoint_id": last_safe_checkpoint_id,
                    "claimed": RunRecoveryStatus.CLAIMED.value,
                },
            )
            row = result.mappings().first()
            return _execution_from_row(dict(row)) if row else None

    async def heartbeat(
        self,
        run_id: str,
        *,
        lease_token: str,
        lease_seconds: int,
        recovery_status: RunRecoveryStatus = RunRecoveryStatus.RUNNING,
    ) -> bool:
        """续租。租约 token 不匹配（已被别人接管）时返回 False。"""

        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE trip_run_executions
                    SET lease_expires_at = NOW() + (:lease_seconds * INTERVAL '1 second'),
                        heartbeat_at = NOW(),
                        recovery_status = :recovery_status,
                        updated_at = NOW()
                    WHERE run_id = :run_id
                      AND lease_token = :lease_token
                      AND executor_id = :executor_id
                    RETURNING run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "lease_token": lease_token,
                    "executor_id": EXECUTOR_ID,
                    "lease_seconds": max(1, lease_seconds),
                    "recovery_status": recovery_status.value,
                },
            )
            return result.mappings().first() is not None

    async def mark_safe_checkpoint(
        self,
        run_id: str,
        *,
        lease_token: str,
        checkpoint_id: str,
    ) -> bool:
        """记录一个已经落盘的安全边界。LLM 调用**开始**时不算安全边界。"""

        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE trip_run_executions
                    SET last_safe_checkpoint_id = :checkpoint_id,
                        updated_at = NOW()
                    WHERE run_id = :run_id
                      AND lease_token = :lease_token
                      AND executor_id = :executor_id
                    RETURNING run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "lease_token": lease_token,
                    "executor_id": EXECUTOR_ID,
                    "checkpoint_id": checkpoint_id,
                },
            )
            return result.mappings().first() is not None

    async def release(
        self,
        run_id: str,
        *,
        lease_token: Optional[str] = None,
        recovery_status: RunRecoveryStatus = RunRecoveryStatus.RELEASED,
        recovery_reason: Optional[str] = None,
    ) -> bool:
        """放弃租约。`lease_token=None` 由恢复扫描使用：它接管的正是没主的行。"""

        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE trip_run_executions
                    SET lease_token = NULL,
                        lease_expires_at = NULL,
                        recovery_status = :recovery_status,
                        recovery_reason = :recovery_reason,
                        updated_at = NOW()
                    WHERE run_id = :run_id
                      AND (
                        CAST(:lease_token AS TEXT) IS NULL
                        OR lease_token = CAST(:lease_token AS TEXT)
                      )
                    RETURNING run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "lease_token": lease_token,
                    "recovery_status": recovery_status.value,
                    "recovery_reason": recovery_reason,
                },
            )
            return result.mappings().first() is not None

    async def record_recovery(
        self,
        run_id: str,
        *,
        recovery_status: RunRecoveryStatus,
        recovery_reason: str,
    ) -> RunExecution:
        """写下恢复判定。没有执行行的历史 run 在这里补一行，判定才有落点。"""

        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO trip_run_executions
                        (run_id, recovery_status, recovery_reason, updated_at)
                    VALUES (:run_id, :recovery_status, :recovery_reason, NOW())
                    ON CONFLICT (run_id) DO UPDATE
                    SET lease_token = NULL,
                        lease_expires_at = NULL,
                        recovery_status = EXCLUDED.recovery_status,
                        recovery_reason = EXCLUDED.recovery_reason,
                        updated_at = NOW()
                    RETURNING *
                    """
                ),
                {
                    "run_id": run_id,
                    "recovery_status": recovery_status.value,
                    "recovery_reason": recovery_reason,
                },
            )
            return _execution_from_row(dict(result.mappings().first()))

    async def list_recovery_candidates(self, *, limit: int = 200) -> List[RunRecoveryCandidate]:
        """所有「没有活着的执行器」的 run。

        两类都要：活跃状态却没人在跑（真孤儿），以及已经收口却留着租约的行（残留）。
        判据是数据库自己的 `NOW()`，不是调用方的墙钟。

        **已经判定过的不再返回**：否则每一轮扫描都会重新报告同一批已收口的 Run，
        「扫两次结果相同」就退化成「扫两次各说一遍」。
        """

        statement = text(
            """
            SELECT r.run_id,
                   r.status,
                   r.mode,
                   r.resume_policy,
                   r.current_node,
                   s.completion_audit,
                   e.run_id AS execution_run_id,
                   e.executor_id,
                   e.lease_token,
                   e.lease_acquired_at,
                   e.lease_expires_at,
                   e.heartbeat_at,
                   e.process_started_at,
                   e.last_safe_checkpoint_id,
                   e.recovery_status,
                   e.recovery_reason,
                   e.updated_at
            FROM trip_runs r
            LEFT JOIN trip_run_executions e ON e.run_id = r.run_id
            LEFT JOIN trip_run_states s ON s.run_id = r.run_id
            WHERE (e.lease_expires_at IS NULL OR e.lease_expires_at < NOW())
              AND (r.status IN :active_statuses OR e.run_id IS NOT NULL)
              AND NOT (
                    r.status NOT IN :active_statuses
                AND e.lease_token IS NULL
                AND e.recovery_status IN :settled_statuses
              )
            ORDER BY r.updated_at ASC
            LIMIT :limit
            """
        ).bindparams(
            bindparam("active_statuses", expanding=True),
            bindparam("settled_statuses", expanding=True),
        )
        async with get_db_session() as session:
            result = await session.execute(
                statement,
                {
                    "active_statuses": [
                        TripRunStatus.RUNNING.value,
                        TripRunStatus.CANCEL_REQUESTED.value,
                    ],
                    "settled_statuses": [status.value for status in _SETTLED_RECOVERY_STATUSES],
                    "limit": max(1, min(limit, 1000)),
                },
            )
            candidates: List[RunRecoveryCandidate] = []
            for row in result.mappings().all():
                data = dict(row)
                execution = (
                    _execution_from_row(data) if data.get("execution_run_id") else None
                )
                audit = data.get("completion_audit")
                candidates.append(
                    RunRecoveryCandidate(
                        run_id=data["run_id"],
                        status=coerce_status(data["status"]),
                        mode=str(data["mode"]),
                        resume_policy=str(data["resume_policy"]),
                        current_node=data.get("current_node"),
                        execution=execution,
                        completion_audit=audit if isinstance(audit, dict) else {},
                    )
                )
            return candidates

    async def has_live_lease(self, run_id: str) -> bool:
        """这个 run 现在有没有活着的执行器。

        判据是数据库的 `NOW()` 与租约到期时间，不是「本进程内存里有没有 handle」——
        后者只说明不是这个进程在跑，把它当成「没人在跑」会让另一个进程的执行被当作不存在。
        """

        async with get_db_session() as session:
            result = await session.execute(
                text(
                    "SELECT 1 FROM trip_run_executions "
                    "WHERE run_id = :run_id "
                    "  AND lease_expires_at IS NOT NULL "
                    "  AND lease_expires_at >= NOW()"
                ),
                {"run_id": run_id},
            )
            return result.mappings().first() is not None

    async def count_active_leases(self) -> int:
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    "SELECT count(*) FROM trip_run_executions "
                    "WHERE lease_expires_at IS NOT NULL AND lease_expires_at >= NOW()"
                )
            )
            return int(result.scalar() or 0)


_store: Optional[RunExecutionStore] = None


def get_run_execution_store() -> RunExecutionStore:
    global _store
    if _store is None:
        _store = RunExecutionStore()
    return _store
