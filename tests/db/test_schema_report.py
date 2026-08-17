"""API 进程那份只读合同校验：只读、说得清下一步、不通过时拦得住。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from travel_agent.db import migrate
from travel_agent.db.connection import connect
from travel_agent.db.fingerprint import fingerprint_sync
from travel_agent.db.report import verify_database_contract

pytestmark = pytest.mark.postgres


async def _report(target, settings):
    engine = create_async_engine(target.asyncpg_url, poolclass=None)
    try:
        return await verify_database_contract(
            engine, embedding_dimensions=settings.embedding.dimensions
        )
    finally:
        await engine.dispose()


async def test_report_on_migrated_database_is_clean(temp_database, settings):
    migrate.upgrade(temp_database)

    report = await _report(temp_database, settings)

    assert report.reachable
    assert report.managed
    assert report.revision == migrate.revision_line()[-1]
    assert report.revision == report.head_revision
    assert report.schema_matches_revision is True
    assert report.compatible
    assert report.problems == [], report.problems
    assert report.missing_tables == ()
    assert report.fingerprint_sha256
    # 报告要把可选能力如实说出来（不静默假装中文词法完整）。
    assert "zhparser" in report.optional_capabilities
    assert report.optional_capabilities["text_search_config"] in {"chinese", "simple"}
    assert set(report.embedding_columns) == {
        "knowledge_chunks.embedding",
        "memory_facts.embedding",
        "memory_entities.embedding",
    }


async def test_report_is_read_only(temp_database, settings):
    """跑校验前后，结构指纹与版本号完全一致。"""

    migrate.upgrade(temp_database)
    dimensions = settings.embedding.dimensions

    with connect(temp_database) as conn:
        before = fingerprint_sync(conn, embedding_dimensions=dimensions)
        before_revision = migrate.current_revision(conn)

    await _report(temp_database, settings)

    with connect(temp_database) as conn:
        after = fingerprint_sync(conn, embedding_dimensions=dimensions)
        after_revision = migrate.current_revision(conn)

    assert before == after, "只读校验改动了结构"
    assert before_revision == after_revision


async def test_report_refuses_an_unmanaged_database(unmanaged_database, settings):
    """结构对但没有版本号 → 不兼容：没人保证过它的结构，也没人给它做过升级前备份。"""

    report = await _report(unmanaged_database, settings)

    assert report.reachable
    assert report.managed is False
    assert report.revision is None
    assert report.compatible is False, "未纳管的库必须被拦住"
    # 拦它的理由必须是「没人纳管过」。基线结构缺的是后续 revision 新增的表，
    # 那不是这条路径要说的事。
    assert any("版本化迁移纳管" in problem for problem in report.problems), report.problems
    assert "migrate" in report.next_action
    assert report.to_dict()["gates_readiness"] is True


async def test_report_flags_missing_tables(temp_database, settings):
    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE run_llm_calls")

    report = await _report(temp_database, settings)

    assert report.compatible is False
    assert report.missing_tables == ("run_llm_calls",)
    assert any("run_llm_calls" in problem for problem in report.problems)
    # 版本号已经在 head，`migrate` 不会重建这张表 —— 所以下一步不能是 migrate。
    assert "doctor" in report.next_action


async def test_report_flags_schema_drift_against_its_revision(temp_database, settings):
    """版本号说 head，结构却多了一列 → 报告说出差在哪。"""

    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE trip_runs ADD COLUMN drifted TEXT")

    report = await _report(temp_database, settings)

    assert report.schema_matches_revision is False
    assert report.compatible is False
    assert any("drifted" in problem for problem in report.problems), report.problems


async def test_report_ignores_externally_owned_tables(temp_database, settings):
    """LangGraph 的 checkpoint 表不算「合同外的表」—— 它们有已知的 owner。"""

    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE checkpoints (thread_id TEXT PRIMARY KEY)")
            cur.execute("CREATE TABLE someone_elses_table (id INT)")

    report = await _report(temp_database, settings)

    assert "checkpoints" not in report.unmanaged_tables
    assert "someone_elses_table" in report.unmanaged_tables
    # 合同外的表不影响这份代码读写自己的表 —— 它是信息，不是问题。
    assert report.compatible
    assert not any("someone_elses_table" in problem for problem in report.problems)


async def test_report_on_unreachable_database_does_not_raise(base_target, settings):
    """库连不上时返回 `reachable=False`，不把异常抛进 lifespan（否则 readiness 也读不到）。"""

    unreachable = base_target.with_database("jp_definitely_absent_db")
    report = await _report(unreachable, settings)

    assert report.reachable is False
    assert report.compatible is False
    assert report.problems
    assert report.next_action
