"""0003 的硬约束：旧身份数据被清空，`user_id` 列默认值收敛为 `local`。"""

from __future__ import annotations

import pytest

from travel_agent.db import migrate
from travel_agent.db.connection import connect
from travel_agent.local_profile import LOCAL_USER_ID

pytestmark = pytest.mark.postgres

_LEGACY_ROWS = (
    "INSERT INTO user_profiles (user_id) VALUES ('u_legacy')",
    "INSERT INTO chat_sessions (session_id, user_id, title) "
    "VALUES ('s_legacy', 'u_legacy', '旧会话')",
    "INSERT INTO chat_session_events (session_id, event_order, event_type, payload) "
    "VALUES ('s_legacy', 1, 'message', '{}'::jsonb)",
    "INSERT INTO memory_facts (user_id, session_id, content) "
    "VALUES ('u_legacy', 's_legacy', '旧记忆')",
    "INSERT INTO trip_runs (run_id, session_id, user_id) "
    "VALUES ('r_legacy', 's_legacy', 'u_legacy')",
    "INSERT INTO trip_run_events (run_id, sequence, event_type, payload) "
    "VALUES ('r_legacy', 1, 'run.created', '{}'::jsonb)",
    "INSERT INTO knowledge_documents (collection, source, content) "
    "VALUES ('u_legacy__travel_knowledge', '旧资料', '正文')",
    "INSERT INTO knowledge_documents (collection, source, content) "
    "VALUES ('destinations', '出厂资料', '正文')",
    "INSERT INTO travel_presets (id, user_id, name, instructions, is_preset) "
    "VALUES ('p_legacy', 'u_legacy', '旧风格', '指令', FALSE)",
    "INSERT INTO travel_presets (id, user_id, name, instructions, is_preset) "
    "VALUES ('p_system', '__system__', '官方风格', '指令', TRUE)",
)

#: 迁移后必须一行不剩的表；后三张靠级联被清空。
_EMPTIED_TABLES = (
    "user_profiles",
    "chat_sessions",
    "chat_session_events",
    "memory_facts",
    "trip_runs",
    "trip_run_events",
)


def _scalar(conn, sql: str):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_legacy_identity_rows_are_purged(temp_database, settings):
    migrate.upgrade(temp_database, "0002_drop_superseded")
    with connect(temp_database) as conn:
        with conn.cursor() as cur:
            for statement in _LEGACY_ROWS:
                cur.execute(statement)
        conn.commit()

    migrate.upgrade(temp_database)

    with connect(temp_database) as conn:
        for table in _EMPTIED_TABLES:
            assert _scalar(conn, f"SELECT count(*) FROM {table}") == 0, (
                f"{table} 迁移后仍有旧身份数据"
            )
        # 出厂语料与系统预设不属于任何身份，必须留下。
        assert _scalar(
            conn, "SELECT count(*) FROM knowledge_documents"
        ) == 1
        assert _scalar(
            conn, "SELECT collection FROM knowledge_documents"
        ) == "destinations"
        assert _scalar(conn, "SELECT count(*) FROM travel_presets") == 1
        assert _scalar(conn, "SELECT id FROM travel_presets") == "p_system"


def test_user_id_columns_default_to_local(temp_database, settings):
    migrate.upgrade(temp_database)

    with connect(temp_database) as conn:
        defaults = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'user_id'"
            )
            defaults = dict(cur.fetchall())

    assert defaults, "没有任何 user_id 列，合同变了就要改这条测试"
    for table, default in defaults.items():
        assert default == f"'{LOCAL_USER_ID}'::text", f"{table}.user_id 默认值是 {default!r}"
