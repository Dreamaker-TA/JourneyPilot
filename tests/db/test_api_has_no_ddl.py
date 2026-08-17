"""API 进程不改 Schema（ADR-P0-03）。

两条判据，一条静态一条动态：`infrastructure/database.py` 里没有 DDL 语句；
一个只有 DML 权限的角色能完成 API 的启动校验并正常读写业务表。
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from travel_agent.db import migrate
from travel_agent.db.connection import connect, connect_maintenance
from travel_agent.db.report import verify_database_contract
from travel_agent.db.schema_contract import MANAGED_TABLES

_DDL = ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "CREATE INDEX", "DROP INDEX",
        "CREATE EXTENSION", "DROP COLUMN", "TRUNCATE")

_SRC = Path(__file__).resolve().parents[2] / "src" / "travel_agent"


def test_database_module_issues_no_ddl():
    source = (_SRC / "infrastructure" / "database.py").read_text(encoding="utf-8").upper()
    found = [keyword for keyword in _DDL if keyword in source]
    assert not found, f"API 侧的数据库模块里出现了 DDL：{found}"


def test_no_init_db_entry_point_remains():
    """`init_db` 已经不存在。留一个能建表的函数就等于留一条能绕过迁移的路。"""

    from travel_agent.infrastructure import database

    assert not hasattr(database, "init_db")


@pytest.fixture
def dml_only_role(temp_database):
    """一个只有 DML 权限的角色，yield 用它连接的 `DatabaseTarget`。"""

    migrate.upgrade(temp_database)

    name = f"jp_app_{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex
    with connect(temp_database) as conn:
        with conn.cursor() as cur:
            # DDL 不接受绑定参数；两个值都是本地生成的 hex，注入面为零。
            cur.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{password}'")
            cur.execute(f'REVOKE CREATE ON SCHEMA public FROM "{name}"')
            cur.execute(f'GRANT CONNECT ON DATABASE "{temp_database.database}" TO "{name}"')
            cur.execute(f'GRANT USAGE ON SCHEMA public TO "{name}"')
            cur.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                f'TO "{name}"'
            )
            cur.execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO "{name}"')
    try:
        yield dataclasses.replace(temp_database, user=name, password=password)
    finally:
        with connect(temp_database) as conn:
            with conn.cursor() as cur:
                cur.execute(f'REASSIGN OWNED BY "{name}" TO CURRENT_USER')
                cur.execute(f'DROP OWNED BY "{name}"')
        with connect_maintenance(temp_database) as conn:
            with conn.cursor() as cur:
                cur.execute(f'DROP ROLE IF EXISTS "{name}"')


@pytest.mark.postgres
async def test_dml_only_role_cannot_create_tables(dml_only_role):
    """先证明这个角色真的没有 DDL 权限，否则下一条测试什么都没证明。"""

    import psycopg

    with connect(dml_only_role) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE should_not_exist (id INT)")


@pytest.mark.postgres
async def test_api_startup_and_writes_work_without_ddl_rights(dml_only_role, settings):
    """用只有 DML 权限的角色跑合同校验，再读写一张业务表。"""

    engine = create_async_engine(dml_only_role.asyncpg_url, poolclass=None)
    try:
        report = await verify_database_contract(
            engine, embedding_dimensions=settings.embedding.dimensions
        )
        assert report.compatible, report.problems

        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO user_profiles (user_id) VALUES ('local')")
            )
            result = await conn.execute(
                text("SELECT count(*) FROM user_profiles WHERE user_id = 'local'")
            )
            assert result.scalar() == 1

        async with engine.connect() as conn:
            for table in MANAGED_TABLES:
                await conn.execute(text(f"SELECT 1 FROM {table} LIMIT 0"))
    finally:
        await engine.dispose()
