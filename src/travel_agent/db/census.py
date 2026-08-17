"""Database census：在改任何东西之前，先说清这个库现在是什么。

census 是启动编排器状态机（dev docs 02 §4.1）的第一步，也是「不许盲 stamp」那条
禁令的执行者。它只读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schema_contract import (
    EXTERNALLY_OWNED_TABLES,
    MANAGED_TABLES,
    OPTIONAL_EXTENSIONS,
    PROBE_TABLES,
    REQUIRED_EXTENSIONS,
)


@dataclass(frozen=True)
class DatabaseCensus:
    """一个数据库的现状。字段全部是**事实**，判断留给 `migrate.plan()`。"""

    reachable: bool
    postgres_version: str = ""
    postgres_major: int = 0
    #: 是否存在 alembic_version 表
    has_alembic_version: bool = False
    #: alembic_version 里的 revision（表存在但为空时是 None）
    alembic_revision: str | None = None
    #: 合同管辖的表里，实际存在的那些
    present_managed_tables: tuple[str, ...] = ()
    #: public schema 里既不属于合同、也没有已知外部 owner 的表
    unmanaged_tables: tuple[str, ...] = ()
    installed_extensions: tuple[str, ...] = ()
    #: 业务行数（只统计合同表里存在的那些），用于判断「空库」和备份必要性
    row_counts: dict[str, int] = field(default_factory=dict)
    unreachable_reason: str = ""

    @property
    def has_core_tables(self) -> bool:
        """有没有任何一张探针表。有 = 不是空库，绝不能当首次安装跑。"""
        return any(name in self.present_managed_tables for name in PROBE_TABLES)

    @property
    def is_empty_database(self) -> bool:
        """空库：一张合同表都没有。

        注意判据是**表**而不是**行数**：有表没行的库仍然可能是「上一次迁移建到一半」，
        它需要走 upgrade 而不是「首次安装」的快路径 —— 但两者的动作恰好相同
        （`upgrade head` 幂等），所以这里不再细分。
        """
        return not self.present_managed_tables

    @property
    def total_business_rows(self) -> int:
        return sum(self.row_counts.values())

    @property
    def missing_managed_tables(self) -> tuple[str, ...]:
        return tuple(name for name in MANAGED_TABLES if name not in self.present_managed_tables)

    @property
    def missing_required_extensions(self) -> tuple[str, ...]:
        return tuple(
            name for name in REQUIRED_EXTENSIONS if name not in self.installed_extensions
        )

    @property
    def optional_capabilities(self) -> dict[str, bool]:
        return {name: name in self.installed_extensions for name in OPTIONAL_EXTENSIONS}

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "unreachable_reason": self.unreachable_reason,
            "postgres_version": self.postgres_version,
            "postgres_major": self.postgres_major,
            "alembic_revision": self.alembic_revision,
            "has_alembic_version": self.has_alembic_version,
            "is_empty_database": self.is_empty_database,
            "has_core_tables": self.has_core_tables,
            "present_managed_tables": list(self.present_managed_tables),
            "missing_managed_tables": list(self.missing_managed_tables),
            "unmanaged_tables": list(self.unmanaged_tables),
            "installed_extensions": list(self.installed_extensions),
            "missing_required_extensions": list(self.missing_required_extensions),
            "optional_capabilities": self.optional_capabilities,
            "row_counts": dict(self.row_counts),
            "total_business_rows": self.total_business_rows,
        }


_PUBLIC_TABLES_SQL = """
    SELECT c.relname AS name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
    ORDER BY c.relname
"""

_VERSION_SQL = "SELECT version(), current_setting('server_version_num')::int / 10000"

_EXTENSIONS_SQL = "SELECT extname FROM pg_extension ORDER BY extname"

_ALEMBIC_VERSION_SQL = "SELECT version_num FROM alembic_version"


def take_census(conn: Any) -> DatabaseCensus:
    """从 psycopg3 连接采集 census。只读，不建任何东西。"""

    with conn.cursor() as cur:
        cur.execute(_VERSION_SQL)
        version_text, major = cur.fetchone()

        cur.execute(_PUBLIC_TABLES_SQL)
        all_tables = [row[0] for row in cur.fetchall()]

        cur.execute(_EXTENSIONS_SQL)
        extensions = tuple(row[0] for row in cur.fetchall())

        managed = tuple(name for name in MANAGED_TABLES if name in all_tables)
        unmanaged = tuple(
            name
            for name in all_tables
            if name not in MANAGED_TABLES and name not in EXTERNALLY_OWNED_TABLES
        )

        has_alembic = "alembic_version" in all_tables
        revision: str | None = None
        if has_alembic:
            cur.execute(_ALEMBIC_VERSION_SQL)
            row = cur.fetchone()
            revision = row[0] if row else None

        # 行数只数存在的表。分开的 SELECT 而不是一条 UNION ALL：一张表读不动
        # （权限、损坏）时，不该把整份 census 拖成不可用。
        row_counts: dict[str, int] = {}
        for name in managed:
            try:
                cur.execute(f'SELECT count(*) FROM "{name}"')  # noqa: S608 — 名字来自代码常量
                row_counts[name] = int(cur.fetchone()[0])
            except Exception:  # pragma: no cover — 只在库损坏/权限异常时走到
                conn.rollback()
                row_counts[name] = -1

    return DatabaseCensus(
        reachable=True,
        postgres_version=version_text,
        postgres_major=int(major),
        has_alembic_version=has_alembic,
        alembic_revision=revision,
        present_managed_tables=managed,
        unmanaged_tables=unmanaged,
        installed_extensions=extensions,
        row_counts=row_counts,
    )
