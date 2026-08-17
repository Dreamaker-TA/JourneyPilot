"""离线 SQL 生成（`journeypilot migrate --sql`）。

**这组测试不需要 PostgreSQL** —— 离线模式的全部意义就是不连库。它们因此也是这个
PR 里唯一在没有数据库的环境下仍然会跑的测试。

离线模式最容易坏的地方是「迁移里偷偷问了数据库一句话」：那一句在联机时正常，
在离线时抛 `MockConnection has no attribute …`，而没人会注意到 `--sql` 坏了 ——
直到有人想先看看升级要做什么。
"""

from __future__ import annotations

import io
import contextlib

from travel_agent.db import migrate
from travel_agent.db.connection import DatabaseTarget

_TARGET = DatabaseTarget(
    host="localhost", port=5432, user="u", password="p", database="offline_only"
)


def _offline_sql() -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        migrate.upgrade_sql(_TARGET)
    return buffer.getvalue()


def test_offline_sql_covers_every_revision():
    sql = _offline_sql()

    for revision in migrate.revision_line():
        assert revision in sql, f"离线 SQL 里没有 {revision}"
    assert "CREATE TABLE alembic_version" in sql
    assert sql.strip().endswith("COMMIT;")


def test_offline_sql_creates_every_managed_table():
    from travel_agent.db.schema_contract import MANAGED_TABLES

    sql = _offline_sql()
    for table in MANAGED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, f"离线 SQL 没有建 {table}"


def test_offline_sql_states_its_text_search_assumption():
    """离线时无法探测 zhparser —— 那个假设必须写在输出里，不能悄悄取一个值。"""

    sql = _offline_sql()
    assert "离线生成" in sql and "zhparser" in sql
    assert "to_tsvector('chinese'" in sql


def test_revision_names_fit_the_alembic_version_column():
    """`alembic_version.version_num` 是 VARCHAR(32)。超长的 revision 名会在写版本号时炸。

    这条约束不在任何文档里，只在 Alembic 建的那张表上，所以在这里钉住它 ——
    否则下一条名字很长的迁移会在跑完 DDL 之后、写版本号那一刻失败。
    """

    for revision in migrate.revision_line():
        assert len(revision) <= 32, f"revision 名超过 32 字符：{revision}"


def test_readiness_blocks_on_the_schema_contract():
    """`report.GATES_READINESS` 与 `_NON_BLOCKING_COMPONENTS` 必须同源。

    分叉的结果是一处报警一处放行。这条把「同源」变成一个会红的断言。
    """

    from travel_agent.api.routes.system import _NON_BLOCKING_COMPONENTS, _probe_schema_report
    from travel_agent.db.report import GATES_READINESS

    assert GATES_READINESS is True
    assert ("database_schema" in _NON_BLOCKING_COMPONENTS) is (not GATES_READINESS)

    # 校验没跑成时载荷仍要结构完整，且按不就绪处理 —— 门禁的默认值是关着的。
    class _NoReport:
        schema_report = None

    payload = _probe_schema_report(_NoReport())
    assert payload["ready"] is False
    assert payload["available"] is False
    assert payload["gates_readiness"] is True


def test_every_revision_declares_its_gates():
    """每条迁移都要声明 `destructive` 与 `reversible`。

    缺声明时 `is_destructive()` 按 False 处理 —— 一条会删数据但忘了声明的迁移
    因此会绕过 `--allow-destructive` 那道闸门。所以「忘了写」必须在这里红，
    而不是在用户的数据上红。
    """

    for revision in migrate.revision_line():
        module = migrate.revision_module(None, revision)
        assert isinstance(getattr(module, "destructive", None), bool), (
            f"{revision} 没有声明 destructive"
        )
        assert isinstance(getattr(module, "reversible", None), bool), (
            f"{revision} 没有声明 reversible"
        )
