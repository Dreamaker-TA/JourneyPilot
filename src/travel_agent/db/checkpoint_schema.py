"""LangGraph checkpoint 表的建立与探测。

这四张表的 owner 是 langgraph（自带 `checkpoint_migrations` 版本表），所以不进我们的
迁移；但建它们是 DDL，于是 `AsyncPostgresSaver.setup()` 由 `journeypilot migrate` 调用，
API 进程只做只读探测。
"""

from __future__ import annotations

import logging
from typing import Any

from .connection import DatabaseTarget, connect
from .schema_contract import LANGGRAPH_CHECKPOINT_TABLES

logger = logging.getLogger(__name__)

_PRESENT_TABLES_SQL = """
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND c.relname = ANY(%s)
"""


def missing_checkpoint_tables(conn: Any) -> tuple[str, ...]:
    """从 psycopg3 连接探测缺哪几张 checkpoint 表。只读。"""

    with conn.cursor() as cur:
        cur.execute(_PRESENT_TABLES_SQL, (list(LANGGRAPH_CHECKPOINT_TABLES),))
        present = {row[0] for row in cur.fetchall()}
    return tuple(name for name in LANGGRAPH_CHECKPOINT_TABLES if name not in present)


async def missing_checkpoint_tables_async(conn: Any) -> tuple[str, ...]:
    """同一条探测，走 psycopg3 的异步连接（checkpointer 自己的池）。

    显式 `tuple_row`：那个池配的是 `dict_row`，两个读法就会变成两份取值代码。
    """

    from psycopg.rows import tuple_row

    async with conn.cursor(row_factory=tuple_row) as cur:
        await cur.execute(_PRESENT_TABLES_SQL, (list(LANGGRAPH_CHECKPOINT_TABLES),))
        present = {row[0] for row in await cur.fetchall()}
    return tuple(name for name in LANGGRAPH_CHECKPOINT_TABLES if name not in present)


def create_checkpoint_schema(target: DatabaseTarget) -> tuple[str, ...]:
    """跑 `AsyncPostgresSaver.setup()`（幂等），返回本次新建的表名。"""

    import asyncio

    with connect(target) as conn:
        before = missing_checkpoint_tables(conn)

    asyncio.run(_run_setup(target))

    with connect(target) as conn:
        after = missing_checkpoint_tables(conn)
    if after:
        raise RuntimeError(
            "LangGraph checkpointer setup 跑完之后仍缺表："
            + "、".join(after)
            + "。checkpoint 表由 langgraph 自己建，请检查数据库权限。"
        )
    created = before
    if created:
        logger.info("已建立 LangGraph checkpoint 表：%s", "、".join(created))
    return created


async def _run_setup(target: DatabaseTarget) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=target.libpq_dsn,
        min_size=1,
        max_size=1,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    try:
        await AsyncPostgresSaver(pool).setup()
    finally:
        await pool.close()
