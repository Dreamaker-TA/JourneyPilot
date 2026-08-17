"""run execution lease and recovery state

Revision ID: 0004_run_execution_lease
Revises: 0003_local_identity
Create Date: 2026-08-17

## 这条迁移做什么

新增 `trip_run_executions`：一个 running TripRun 的**执行归属**（哪个进程在跑、
租约到什么时候、最后一次心跳、最近一个安全 checkpoint）以及重启后的恢复判定结果。

它与 `trip_runs` 分表：业务生命周期（status/终态/审计）和执行归属是两件会以完全不同
频率改写的事实，心跳每 10 秒一次，不该把它压在核心表的每一行上。

时钟以 PostgreSQL 的 `NOW()` 为准 —— 租约是否过期不能由各个 Python 进程的墙钟决定。

## 门禁

- **影响面**：只有 DB 新增。既有表结构不动，既有数据不动。
- **旧数据路径**：无。历史 run 没有执行行，启动 census 按「没有 executor」处理。
- **失败注入**：单条迁移一个事务，中途失败整体回滚，`alembic_version` 不前进。
- **幂等**：`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`。
- **观察信号**：`GET /api/trip-runs/{run_id}` 返回 `execution` 段。
- **回滚**：可逆。表里只有执行归属，丢掉它等于「所有 run 都没有 executor」，
  与首次安装的状态相同。
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
