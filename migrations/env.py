"""Alembic 运行环境。

两处刻意的选择：

- **URL 只有一个来源**：`travel_agent.config.get_settings().database`。测试和
  `journeypilot restore` 需要迁另一个库时，通过 `config.attributes["db_url"]`
  显式传入，而不是让 `alembic.ini` 里躺着第二份地址。
- **不用 autogenerate，也没有 target_metadata**：这个仓的 Schema 是手写 SQL、
  pgvector、zhparser 生成列和部分索引，没有 SQLAlchemy 模型作为真相
  （dev docs 02 §2.2）。给一个空的 metadata 会让 `--autogenerate` 生成
  「删掉所有表」这种迁移，比不支持它危险得多。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine

_REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (_REPO_ROOT / "src", _REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

config = context.config

if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None


def _database_url() -> str:
    """迁移要连的库。psycopg3 同步驱动 —— Alembic 是同步的。"""

    explicit = config.attributes.get("db_url")
    if explicit:
        return str(explicit)

    from travel_agent.config import get_settings

    db = get_settings().database
    return f"postgresql+psycopg://{db.user}:{db.password}@{db.host}:{db.port}/{db.name}"


def run_migrations_offline() -> None:
    """离线模式：把迁移输出成 SQL 而不执行（`journeypilot migrate --sql`）。"""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")

    if connectable is not None:
        _run(connectable)
        return

    engine = create_engine(_database_url(), poolclass=None, future=True)
    try:
        with engine.connect() as connection:
            _run(connection)
    finally:
        engine.dispose()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # transaction_per_migration：一条迁移失败只回滚它自己，前面成功的留在库里，
        # 版本号如实反映「走到哪一步」。PostgreSQL 支持事务性 DDL，所以单条迁移
        # 内部失败是干净回滚，不会留下半张表。
        transaction_per_migration=True,
        compare_type=False,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
