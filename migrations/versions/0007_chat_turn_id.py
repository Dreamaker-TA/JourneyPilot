"""chat session events carry a turn id

Revision ID: 0007_chat_turn_id
Revises: 0006_background_jobs
Create Date: 2026-08-18

`chat_session_events.turn_id`：一轮对话的所有事件共用一个 id，分页按 turn 走且一个 turn
永远整份返回。回填规则：一条 `message.user` 到下一条之前算一个 turn；第一条用户消息之前
的事件落进本会话的 synthetic turn（数量在迁移里 warn 出来）。

回填是**一次性**的，读路径只认 `turn_id NOT NULL`，没有运行期兜底。
"""

from __future__ import annotations

from alembic import op

revision = "0007_chat_turn_id"
down_revision = "0006_background_jobs"
branch_labels = None
depends_on = None

destructive = False
reversible = True


def upgrade() -> None:
    op.execute("ALTER TABLE chat_session_events ADD COLUMN IF NOT EXISTS turn_id TEXT")
    op.execute(
        """
        WITH grouped AS (
            SELECT
                event_id,
                session_id,
                max(CASE WHEN event_type = 'message.user' THEN event_order END) OVER (
                    PARTITION BY session_id
                    ORDER BY event_order
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS turn_start
            FROM chat_session_events
            WHERE turn_id IS NULL
        )
        UPDATE chat_session_events e
        SET turn_id = 'turn_' || substr(
            md5(g.session_id || ':' || COALESCE(g.turn_start, 0)), 1, 16
        )
        FROM grouped g
        WHERE e.event_id = g.event_id
        """
    )
    # synthetic turn 要留个声音。写成 SQL 而不是 Python 查询：离线 `--sql` 预览也走
    # 同一条迁移，那条路径上没有可查询的连接。
    op.execute(
        """
        DO $$
        DECLARE orphan_sessions INTEGER;
        BEGIN
            -- 一次分组扫完。相关子查询里带不等式会让它按会话逐行重探，
            -- 而这条紧跟在一次全表回填后面、和它同处一个事务。
            SELECT count(*) INTO orphan_sessions FROM (
                SELECT min(event_order) AS first_any,
                       min(event_order) FILTER (WHERE event_type = 'message.user') AS first_user
                FROM chat_session_events
                GROUP BY session_id
            ) t
            WHERE t.first_user IS NULL OR t.first_any < t.first_user;
            IF orphan_sessions > 0 THEN
                RAISE WARNING 'turn 回填：% 个会话有落在首条用户消息之前的事件，已归入该会话的 synthetic turn',
                    orphan_sessions;
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE chat_session_events ALTER COLUMN turn_id SET NOT NULL")
    # 分页的查询形状：按会话取一段 turn，再按事件顺序取回那几个 turn 的全部事件。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_events_session_turn_order
        ON chat_session_events (session_id, turn_id, event_order)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chat_events_session_turn_order")
    op.execute("ALTER TABLE chat_session_events DROP COLUMN IF EXISTS turn_id")
