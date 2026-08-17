"""CLI / 迁移侧的同步数据库连接。

**与 `infrastructure/database.py` 的引擎刻意分开**：那个是 API 进程的 asyncpg 连接池，
生命周期绑在 ASGI 应用上。这里的连接属于一个短命的维护进程，而且要能连**不是配置里
那个库**（restore 会先恢复到一个临时库再切换），所以库名是参数而不是全局单例。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class DatabaseTarget:
    """一个可连的 PostgreSQL 库。`database` 可覆盖，其余取自配置。"""

    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_settings(cls, settings: Any, *, database: str | None = None) -> "DatabaseTarget":
        db = settings.database
        return cls(
            host=db.host,
            port=db.port,
            user=db.user,
            password=db.password,
            database=database or db.name,
        )

    def with_database(self, database: str) -> "DatabaseTarget":
        return DatabaseTarget(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=database,
        )

    @property
    def sqlalchemy_url(self) -> str:
        """Alembic 用。psycopg3 同步驱动。"""
        return (
            f"postgresql+psycopg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def asyncpg_url(self) -> str:
        """API 侧那条异步引擎用的 URL（与 `DatabaseConfig.url` 同一个形状）。"""
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def libpq_dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    def describe(self) -> str:
        """给人看的，**不含口令**。"""
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


@contextmanager
def connect(target: DatabaseTarget, *, autocommit: bool = True) -> Iterator[Any]:
    """psycopg3 连接。

    默认 autocommit：迁移锁是 session 级的 advisory lock，census 与备份是只读的，
    两者都不需要外层事务；而 `CREATE DATABASE` / `DROP DATABASE` 在事务里根本不能跑。
    需要事务语义的地方（迁移本身）由 Alembic 自己开。
    """

    import psycopg

    conn = psycopg.connect(target.libpq_dsn, autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def connect_maintenance(target: DatabaseTarget) -> Iterator[Any]:
    """连到 `postgres` 维护库，用来 CREATE / DROP / RENAME 其他库。"""

    with connect(target.with_database("postgres")) as conn:
        yield conn
