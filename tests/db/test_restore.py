"""恢复：验证通过才切换，失败绝不动当前数据库。

dev docs 02 §6 的那条禁令（「不要直接覆盖当前 volume 后祈祷成功」）只有一种验证方式：
**让恢复失败，然后检查当前数据库有没有被改**。所以这里的重点不是「恢复成功」那条路，
而是坏备份、坏 checksum、活动连接这三种失败下当前数据仍然完好。
"""

from __future__ import annotations

import pytest

from travel_agent.db import migrate
from travel_agent.db.backup import DUMP_FILENAME, create_backup
from travel_agent.db.connection import connect, connect_maintenance
from travel_agent.db.restore import RestoreError, drop_retained, list_retained, restore

pytestmark = pytest.mark.postgres


def _profile_count(target) -> int:
    with connect(target) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM user_profiles")
            return int(cur.fetchone()[0])


def _add_profile(target, user_id: str) -> None:
    with connect(target) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_profiles (user_id, display_name) VALUES (%s, %s)",
                (user_id, f"用户{user_id}"),
            )


@pytest.fixture
def cleanup_retained(base_target):
    """测试结束后删掉恢复留档库 —— 它们刻意不自动删除，所以测试要自己收拾。"""

    created: list[str] = []
    yield created
    with connect_maintenance(base_target) as conn:
        for name in created:
            with conn.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_restore_replaces_database_and_retains_the_old_one(
    temp_database, settings, scratch_dir, cleanup_retained
):
    migrate.upgrade(temp_database)
    _add_profile(temp_database, "local")
    _add_profile(temp_database, "second")

    with connect(temp_database) as conn:
        backup = create_backup(
            conn,
            temp_database,
            embedding_dimensions=settings.embedding.dimensions,
            output_root=scratch_dir,
            label="before-change",
        )

    # 备份之后又写了一行：恢复必须把它抹掉（回到备份那一刻）。
    _add_profile(temp_database, "third")
    assert _profile_count(temp_database) == 3

    result = restore(
        temp_database,
        backup.directory,
        embedding_dimensions=settings.embedding.dimensions,
    )
    cleanup_retained.append(result.retained_database)

    assert result.restored_database == temp_database.database
    assert _profile_count(temp_database) == 2, "恢复后的行数应回到备份那一刻"
    assert result.checks["schema_matches_backup"] is True
    assert result.checks["row_counts_match"] is True
    assert result.checks["migration_revision"] == migrate.revision_line()[-1]

    # 旧库还在，且保留着恢复前的那三行 —— 「保留旧数据库直到用户确认」。
    retained = temp_database.with_database(result.retained_database)
    assert _profile_count(retained) == 3
    assert result.retained_database in list_retained(temp_database)


def test_corrupted_backup_is_refused_and_current_database_untouched(
    temp_database, settings, scratch_dir
):
    """checksum 不符 → 拒绝恢复，当前数据库一行都不变。"""

    migrate.upgrade(temp_database)
    _add_profile(temp_database, "local")

    with connect(temp_database) as conn:
        backup = create_backup(
            conn,
            temp_database,
            embedding_dimensions=settings.embedding.dimensions,
            output_root=scratch_dir,
        )

    dump = backup.directory / DUMP_FILENAME
    data = bytearray(dump.read_bytes())
    data[len(data) // 3] ^= 0xFF
    dump.write_bytes(bytes(data))

    with pytest.raises(RestoreError) as excinfo:
        restore(
            temp_database,
            backup.directory,
            embedding_dimensions=settings.embedding.dimensions,
        )
    assert "备份校验未通过" in str(excinfo.value)

    assert _profile_count(temp_database) == 1, "被拒绝的恢复改动了当前数据库"
    assert list_retained(temp_database) == [], "被拒绝的恢复留下了留档库"


def test_active_connections_block_the_switch(
    temp_database, settings, scratch_dir, cleanup_retained
):
    """当前库上有活动连接时拒绝切换，并且清掉自己建的 staging 库。"""

    migrate.upgrade(temp_database)
    _add_profile(temp_database, "local")

    with connect(temp_database) as conn:
        backup = create_backup(
            conn,
            temp_database,
            embedding_dimensions=settings.embedding.dimensions,
            output_root=scratch_dir,
        )

    with connect(temp_database):  # 一个「还在跑的 API」
        with pytest.raises(RestoreError) as excinfo:
            restore(
                temp_database,
                backup.directory,
                embedding_dimensions=settings.embedding.dimensions,
            )
    assert "活动连接" in str(excinfo.value)
    assert "--terminate-active" in str(excinfo.value)
    assert _profile_count(temp_database) == 1

    # staging 库必须已经被收掉，不能留一堆 `_restore_<时间戳>` 垃圾。
    with connect_maintenance(temp_database) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_database WHERE datname LIKE %s",
                (f"{temp_database.database}_restore_%",),
            )
            assert int(cur.fetchone()[0]) == 0

    # --terminate-active 之后可以恢复。
    result = restore(
        temp_database,
        backup.directory,
        embedding_dimensions=settings.embedding.dimensions,
        terminate_active=True,
    )
    cleanup_retained.append(result.retained_database)
    assert _profile_count(temp_database) == 1


def test_drop_retained_refuses_arbitrary_database_names(temp_database):
    """`--drop-retained` 只能删自己造的留档库。它执行的是 DROP DATABASE。"""

    with pytest.raises(RestoreError) as excinfo:
        drop_retained(temp_database, "travel_agent")
    assert "拒绝删除" in str(excinfo.value)
