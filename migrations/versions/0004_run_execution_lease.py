"""run execution lease and recovery state

Revision ID: 0004_run_execution_lease
Revises: 0003_local_identity
Create Date: 2026-08-17

新增 `trip_run_executions`：一个 running TripRun 的执行归属（哪个进程在跑、租约、心跳、
最近一个安全 checkpoint）与重启后的恢复判定。租约是否过期由 PostgreSQL 的 `NOW()` 说。

只新增，既有表与数据不动；`IF NOT EXISTS` 幂等；可逆（丢掉它等于所有 run 都没有 executor）。
"""

from __future__ import annotations

from alembic import op

revision = "0004_run_execution_lease"
down_revision = "0003_local_identity"
branch_labels = None
depends_on = None

destructive = False
reversible = True


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS trip_run_executions (
            run_id                  TEXT PRIMARY KEY REFERENCES trip_runs(run_id) ON DELETE CASCADE,
            executor_id             TEXT,
            lease_token             TEXT,
            lease_acquired_at       TIMESTAMPTZ,
            lease_expires_at        TIMESTAMPTZ,
            heartbeat_at            TIMESTAMPTZ,
            process_started_at      TIMESTAMPTZ,
            last_safe_checkpoint_id TEXT,
            recovery_status         TEXT NOT NULL DEFAULT 'idle',
            recovery_reason         TEXT,
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # 启动 census 与周期扫描的唯一查询形状：按租约到期时间找孤儿。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trip_run_executions_lease_expires
        ON trip_run_executions (lease_expires_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trip_run_executions_recovery_status
        ON trip_run_executions (recovery_status)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trip_run_executions")
