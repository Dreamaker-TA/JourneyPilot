"""测试夹具。

这一批测试是**对真实 PostgreSQL 跑的**，理由很直接：要验证的东西（pgvector 列的
typmod、生成列的表达式、advisory lock 的竞争、`pg_restore --list` 认不认这个文件、
一个只有 DML 权限的角色能不能跑起 API）在任何 mock 里都不存在。用 sqlite 或 mock
跑出来的绿灯，恰好在这些不变量上没有意义。

连不上库时**跳过而不是失败**：CI 里没有 PostgreSQL 的那一档不该因此变红，
但跳过的原因会打印出来，不会静默变成「0 个测试通过」。

每个测试用一个**新建的临时库**（`jp_test_<随机>`），跑完删掉。它绝不碰
`config.yaml` 里那个真实库 —— 那里有开发者的数据。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Iterator

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (_REPO_ROOT / "src", _REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "postgres: requires a reachable PostgreSQL instance (skipped when absent)"
    )


@pytest.fixture(scope="session")
def settings():
    from travel_agent.config import get_settings

    return get_settings()


@pytest.fixture(scope="session")
def base_target(settings):
    """配置里那个库的连接参数。**测试只用它来建/删临时库，不读写它的数据。**"""

    from travel_agent.db.connection import DatabaseTarget

    return DatabaseTarget.from_settings(settings)


@pytest.fixture(scope="session")
def postgres_available(base_target) -> bool:
    from travel_agent.db.connection import connect_maintenance

    try:
        with connect_maintenance(base_target) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001 — 任何连接失败都同样是「不可用」
        print(f"\n[tests] PostgreSQL 不可达（{base_target.describe()}）：{exc}")
        return False


@pytest.fixture
def temp_database(base_target, postgres_available) -> Iterator[object]:
    """一个空的临时库，yield 它的 `DatabaseTarget`，测试结束后删除。

    库名带随机后缀而不是固定名字：并行跑测试时固定名字会互相踩，
    而「上一次跑崩了留下一个脏库」会让下一次的结论不可信。
    """

    if not postgres_available:
        pytest.skip("需要可达的 PostgreSQL")

    from travel_agent.db.connection import connect_maintenance

    name = f"jp_test_{uuid.uuid4().hex[:12]}"
    with connect_maintenance(base_target) as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    try:
        yield base_target.with_database(name)
    finally:
        with connect_maintenance(base_target) as conn:
            with conn.cursor() as cur:
                # 先断开残留连接：测试里的异步引擎可能还没完全释放，
                # 而 DROP DATABASE 会因此失败并让后面每一次跑都留一个脏库。
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture
def unmanaged_database(temp_database) -> object:
    """结构对、没有版本号的库：迁到 baseline 后删掉 `alembic_version`。

    纳管判定与「拒绝未纳管的库」两条路径的输入。
    """

    from travel_agent.db import migrate
    from travel_agent.db.connection import connect
    from travel_agent.db.schema_contract import BASELINE_REVISION

    migrate.upgrade(temp_database, BASELINE_REVISION)
    with connect(temp_database) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE alembic_version")
    return temp_database


@pytest.fixture
def scratch_dir(tmp_path) -> Path:
    """备份类测试的输出目录。用 pytest 的 tmp_path，绝不写进仓库。"""

    path = tmp_path / "backups"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _quiet_alembic_logging():
    """Alembic 每条迁移打两行 INFO。测试输出里它们只是噪音。"""

    import logging

    logger = logging.getLogger("alembic")
    previous = logger.level
    logger.setLevel(logging.WARNING)
    yield
    logger.setLevel(previous)


def pytest_report_header(config: pytest.Config) -> str:
    return f"[tests] DB_HOST={os.getenv('DB_HOST', '(config.yaml)')}"
