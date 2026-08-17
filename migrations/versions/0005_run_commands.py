"""durable run control commands

Revision ID: 0005_run_commands
Revises: 0004_run_execution_lease
Create Date: 2026-08-17

## 这条迁移做什么

新增 `trip_run_commands`：cancel 与 supplement 的**持久化命令**。在此之前这两个动作
只存在于发请求那一刻命中的进程内 registry，进程一换就消失，而 API 已经答了「已接受」。

`(run_id, request_digest)` 唯一：重发同一个意图落回同一行，回执也是同一张。

## 门禁

- **影响面**：只有 DB 新增。既有表结构不动，既有数据不动。
- **旧数据路径**：无。历史 run 没有命令行，等于「没有待处理命令」。
- **失败注入**：单条迁移一个事务，中途失败整体回滚，`alembic_version` 不前进。
- **幂等**：`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`。
- **观察信号**：`GET /api/trip-runs/{run_id}/commands/{command_id}` 能查到回执；
  `GET /api/health/ready` 的 `run_execution.pending_commands` 报待处理数。
- **回滚**：可逆。表里只有控制命令，丢掉它等于「没有待处理的取消/追加要求」，
  与首次安装的状态相同。
"""

from __future__ import annotations

from alembic import op

revision = "0005_run_commands"
down_revision = "0004_run_execution_lease"
branch_labels = None
depends_on = None

destructive = False
reversible = True


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS trip_run_commands (
            command_id      TEXT PRIMARY KEY,
            run_id          TEXT NOT NULL REFERENCES trip_runs(run_id) ON DELETE CASCADE,
            command_type    TEXT NOT NULL,
            payload         JSONB NOT NULL DEFAULT '{}',
            request_digest  TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            claimed_by      TEXT,
            claimed_at      TIMESTAMPTZ,
            consumed_at     TIMESTAMPTZ,
            result          JSONB,
            error_code      TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (run_id, request_digest)
        )
        """
    )
    # 执行器每个协作边界的查询形状：这个 run 还有没有 pending 命令。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trip_run_commands_pending
        ON trip_run_commands (run_id, status, created_at)
        WHERE status = 'pending'
        """
    )
    # 诊断面的查询形状：全库还有多少未收口的命令，按类型分。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trip_run_commands_open
        ON trip_run_commands (status, command_type)
        WHERE status IN ('pending', 'claimed')
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trip_run_commands")
