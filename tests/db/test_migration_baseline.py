"""baseline 迁移的硬约束：空库能迁出完整结构、既有库纳管而不重建、结构对不上就拒绝。

迁移是现在唯一能建出业务结构的东西（API 进程不建表），所以这几条是承重墙。
"""

from __future__ import annotations

import pytest

from travel_agent.db import migrate
from travel_agent.db.connection import connect
from travel_agent.db.fingerprint import diff_fingerprints, fingerprint_sync
from travel_agent.db.schema_contract import BASELINE_REVISION, MANAGED_TABLES

pytestmark = pytest.mark.postgres


def _fingerprint(target, dimensions: int) -> dict:
    with connect(target) as conn:
        return fingerprint_sync(conn, embedding_dimensions=dimensions)


def test_empty_database_upgrades_to_head(temp_database, settings):
    """空库 → `upgrade head`：结构完整、版本号落到 head、指纹与存档一致。"""

    migrate.upgrade(temp_database)

    with connect(temp_database) as conn:
        from travel_agent.db.census import take_census

        census = take_census(conn)

    line = migrate.revision_line()
    head = line[-1]
    assert census.alembic_revision == head, "迁移后版本号必须是 head"
    assert not census.missing_managed_tables, (
        f"upgrade head 之后仍缺表：{census.missing_managed_tables}"
    )
    assert not census.missing_required_extensions, (
        f"缺核心扩展：{census.missing_required_extensions}"
    )

    actual = _fingerprint(temp_database, settings.embedding.dimensions)
    archived = migrate.expected_fingerprint_for(head, line=line)
    assert archived is not None, (
        f"缺 head（{head}）的指纹存档。"
        f"用 `journeypilot db write-fingerprint --revision {head}` 生成。"
    )
    problems = diff_fingerprints(archived, actual)
    assert not problems, "迁移建出来的结构与存档的 head 指纹不一致：\n" + "\n".join(problems)


def test_unmanaged_database_is_adopted_not_rebuilt(unmanaged_database, settings):
    """有核心表、无 alembic_version → `ADOPT_BASELINE`，stamp 后表一张不少。"""

    with connect(unmanaged_database) as conn:
        plan = migrate.plan(
            conn, unmanaged_database, embedding_dimensions=settings.embedding.dimensions
        )

    assert plan.decision is migrate.Decision.ADOPT_BASELINE, (
        f"既有库应判定为纳管，实际是 {plan.decision.value}：{plan.problems}"
    )
    assert plan.current_revision is None
    assert not plan.refused

    migrate.stamp(unmanaged_database, BASELINE_REVISION)

    with connect(unmanaged_database) as conn:
        after = migrate.plan(
            conn, unmanaged_database, embedding_dimensions=settings.embedding.dimensions
        )
    assert after.current_revision == BASELINE_REVISION
    # 表还在 —— 纳管不重复建表，也不删任何东西。判据是「与纳管前逐张相同」而不是
    # 「一张不缺」：基线结构本来就没有后续 revision 新增的表，补上它们是 stamp 之后
    # 那次 upgrade 的事，不是纳管这一步的事。
    assert after.census.present_managed_tables == plan.census.present_managed_tables


def test_unknown_schema_is_refused_not_stamped(unmanaged_database, settings):
    """结构被改动过的未纳管库 → 拒绝纳管。**这是 §2.3 那条禁令的执行现场。**"""

    with connect(unmanaged_database) as conn:
        with conn.cursor() as cur:
            # 一个合同外的列，模拟「不知道从哪来的结构」。
            cur.execute("ALTER TABLE trip_runs ADD COLUMN mystery_column TEXT")
        plan = migrate.plan(
            conn, unmanaged_database, embedding_dimensions=settings.embedding.dimensions
        )

    assert plan.decision is migrate.Decision.REFUSE_UNKNOWN_SCHEMA
    assert plan.refused
    assert any("mystery_column" in problem for problem in plan.problems), (
        f"拒绝的理由必须指出差在哪，实际：{plan.problems}"
    )
    # 拒绝时必须给出下一步，而不只是「不行」。
    assert plan.next_action


def test_baseline_downgrade_removes_managed_tables(temp_database, settings):
    """0001 的 up/down 对称：downgrade 到 base 之后一张受管表都不剩。

    只测 0001。0002 声明 `reversible = False` 且 `downgrade()` 抛异常 ——
    那是刻意的（删掉的行重建不出来），所以这里不穿过它。
    """

    migrate.upgrade(temp_database, BASELINE_REVISION)
    with connect(temp_database) as conn:
        from travel_agent.db.census import take_census

        assert take_census(conn).alembic_revision == BASELINE_REVISION

    migrate.downgrade(temp_database, "base")

    with connect(temp_database) as conn:
        from travel_agent.db.census import take_census

        census = take_census(conn)
    assert census.present_managed_tables == (), (
        f"downgrade 之后仍有受管表残留：{census.present_managed_tables}"
    )
    assert census.alembic_revision is None
    assert len(MANAGED_TABLES) == len(census.missing_managed_tables)
