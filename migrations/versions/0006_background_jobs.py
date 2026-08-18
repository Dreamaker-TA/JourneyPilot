"""durable background jobs

Revision ID: 0006_background_jobs
Revises: 0005_run_commands
Create Date: 2026-08-17

新增 `background_jobs`（后台任务的最终事实），并为「至少一次执行」补上业务侧幂等：
`memory_facts.fact_digest` 唯一约束 + `user_profiles.revision` 画像基线。

历史事实 `fact_digest` 为 NULL，不参与唯一约束；`IF NOT EXISTS` 幂等；可逆。
"""

from __future__ import annotations

from alembic import op

revision = "0006_background_jobs"
down_revision = "0005_run_commands"
branch_labels = None
depends_on = None

destructive = False
reversible = True


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS background_jobs (
            job_id            TEXT PRIMARY KEY,
            job_type          TEXT NOT NULL,
            dedupe_key        TEXT NOT NULL,
            payload           JSONB NOT NULL DEFAULT '{}',
            status            TEXT NOT NULL DEFAULT 'pending',
            priority          INTEGER NOT NULL DEFAULT 100,
            attempts          INTEGER NOT NULL DEFAULT 0,
            max_attempts      INTEGER NOT NULL DEFAULT 5,
            available_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            lease_owner       TEXT,
            lease_expires_at  TIMESTAMPTZ,
            last_error_code   TEXT,
            last_error_summary TEXT,
            result            JSONB,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at      TIMESTAMPTZ,
            UNIQUE (job_type, dedupe_key)
        )
        """
    )
    # worker claim 的查询形状：可领取的行按优先级与创建顺序排。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_background_jobs_claimable
        ON background_jobs (priority, created_at)
        WHERE status IN ('pending', 'retry_wait', 'running')
        """
    )
    # 诊断面与清理的查询形状。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_background_jobs_type_status
        ON background_jobs (job_type, status, updated_at)
        """
    )
    # 会话删除时按 payload 里的会话引用取消待处理任务。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_background_jobs_session
        ON background_jobs ((payload ->> 'session_id'))
        WHERE status IN ('pending', 'retry_wait', 'running')
        """
    )

    # 重复消费不得产生重复事实：摘要由 (user_id, 来源消息, 正文) 决定。
    op.execute("ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS source_message_id TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS fact_digest TEXT")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_facts_digest
        ON memory_facts (user_id, fact_digest)
        WHERE fact_digest IS NOT NULL
        """
    )

    # 画像基线：延迟执行的 job 记下它入队时看到的是哪一版画像。
    op.execute("ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS background_jobs")
    op.execute("DROP INDEX IF EXISTS idx_memory_facts_digest")
    op.execute("ALTER TABLE memory_facts DROP COLUMN IF EXISTS fact_digest")
    op.execute("ALTER TABLE memory_facts DROP COLUMN IF EXISTS source_message_id")
    op.execute("ALTER TABLE user_profiles DROP COLUMN IF EXISTS revision")
