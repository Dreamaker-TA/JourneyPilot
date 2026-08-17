"""三道闸门：迁移锁、破坏性授权、版本比代码新。

对应 dev docs 02 §14.1 测试矩阵里的「两个 migrator 竞争锁」「destructive migration
未授权时拒绝」，以及 §13.3 的「代码回滚但 Schema 已升级」。
"""

from __future__ import annotations

import pytest

from travel_agent.db import migrate
from travel_agent.db.census import take_census
from travel_agent.db.connection import connect
from travel_agent.db.lock import (
    MigrationLockTimeout,
    migration_lock,
    release,
    try_acquire,
)

pytestmark = pytest.mark.postgres


def test_two_migrators_cannot_hold_the_lock_together(temp_database):
    """第二个 migrator 拿不到锁就**超时报错**，不许无锁继续。"""

    with connect(temp_database) as first, connect(temp_database) as second:
        with migration_lock(first, timeout_seconds=1.0):
            assert try_acquire(second) is False, "两个连接同时拿到了迁移锁"
            with pytest.raises(MigrationLockTimeout) as excinfo:
                with migration_lock(second, timeout_seconds=1.0):
                    pass
            # 报错必须说清「另一个在跑」和「怎么办」，而不是一句 timeout。
            assert "并发迁移" in str(excinfo.value)

        # 第一个释放之后第二个能拿到 —— 锁不是一次性的。
        assert try_acquire(second) is True
        release(second)


def test_lock_is_released_when_connection_closes(temp_database):
    """连接断掉时 PostgreSQL 自动释放 advisory lock。

    这条决定了「上一次 migrate 崩了」之后**不需要人工清锁**。如果锁需要人工清理，
    一次崩溃就会让用户从此迁不了库，而错误信息里只有「锁被占用」。
    """

    with connect(temp_database) as holder:
        assert try_acquire(holder) is True

    with connect(temp_database) as other:
        assert try_acquire(other) is True, "连接关闭后锁没有被释放"
        release(other)


def test_destructive_migration_is_refused_without_consent(temp_database, settings):
    """非空库上的破坏性迁移必须被拒绝，直到显式授权。"""

    migrate.upgrade(temp_database, "0001_baseline")
    # 造一行数据，让它成为「非空库」——闸门保护的正是这一行。
    with connect(temp_database) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_profiles (user_id, display_name) VALUES ('local', '测试')"
            )

    dimensions = settings.embedding.dimensions
    with connect(temp_database) as conn:
        refused = migrate.plan(conn, temp_database, embedding_dimensions=dimensions)
        allowed = migrate.plan(
            conn, temp_database, embedding_dimensions=dimensions, allow_destructive=True
        )

    assert refused.decision is migrate.Decision.REFUSE_NEEDS_DESTRUCTIVE_CONSENT
    assert refused.refused
    assert "--allow-destructive" in refused.next_action
    assert refused.destructive_revisions, "拒绝时必须点名是哪几条迁移"
    assert refused.backup_required is True, "非空库执行迁移前必须先备份"

    assert allowed.decision is migrate.Decision.UPGRADE
    assert not allowed.refused


def test_empty_database_skips_the_destructive_gate(temp_database, settings):
    """空库不受破坏性闸门约束 —— 那里没有数据可丢。

    否则每一次**首次安装**都要用户敲 `--allow-destructive`，
    而那会把一个真正重要的确认动作训练成肌肉记忆。
    """

    with connect(temp_database) as conn:
        plan = migrate.plan(
            conn, temp_database, embedding_dimensions=settings.embedding.dimensions
        )

    assert plan.decision is migrate.Decision.MIGRATE_EMPTY
    assert not plan.refused
    assert plan.backup_required is False
    # head 路径上确实有破坏性迁移，这条测试才有意义。
    line = migrate.revision_line()
    assert any(migrate.is_destructive(None, rev) for rev in line), (
        "迁移历史里没有破坏性迁移，这条测试无法证明闸门被跳过"
    )


def test_unknown_revision_is_refused(temp_database, settings):
    """数据库记着一个这份代码不认识的 revision → 拒绝启动，不假装能读。"""

    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE alembic_version SET version_num = '9999_from_the_future'")
        plan = migrate.plan(
            conn, temp_database, embedding_dimensions=settings.embedding.dimensions
        )

    assert plan.decision is migrate.Decision.REFUSE_UNKNOWN_REVISION
    assert plan.refused
    assert plan.next_action, "拒绝必须附带下一步"


def test_up_to_date_database_needs_nothing(temp_database, settings):
    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        plan = migrate.plan(
            conn, temp_database, embedding_dimensions=settings.embedding.dimensions
        )
        census = take_census(conn)

    assert plan.decision is migrate.Decision.UP_TO_DATE
    assert plan.pending_revisions == ()
    assert census.alembic_revision == plan.head_revision


def test_head_revision_with_drifted_schema_is_not_up_to_date(temp_database, settings):
    """版本号说 head，结构却少一张表 → 拒绝，不许判成「已是最新」。

    这是 §4.1 里「最新 → verify」那一步。判成 UP_TO_DATE 的后果是 `migrate`
    什么都不做，而第一条写那张表的请求 500 —— 版本号存在就是为了消灭这种沉默。
    """

    migrate.upgrade(temp_database)
    with connect(temp_database) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE run_llm_calls")
        plan = migrate.plan(
            conn, temp_database, embedding_dimensions=settings.embedding.dimensions
        )

    assert plan.decision is migrate.Decision.REFUSE_UNKNOWN_SCHEMA
    assert plan.refused
    assert any("run_llm_calls" in problem for problem in plan.problems), plan.problems
    # 下一步是留档 + 恢复/重建，不是「再跑一次 migrate」（它已经在 head，不会做任何事）。
    assert "journeypilot backup" in plan.next_action
    assert "journeypilot migrate" not in plan.next_action


def test_migration_history_is_linear(temp_database):
    """迁移历史必须是一条直线（`revision_line` 在分叉时抛错）。"""

    line = migrate.revision_line()
    assert line[0] == "0001_baseline"
    assert len(set(line)) == len(line)
