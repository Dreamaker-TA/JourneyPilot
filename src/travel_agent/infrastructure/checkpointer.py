"""LangGraph Postgres checkpointer bootstrap.

The application keeps its existing SQLAlchemy/asyncpg pool for business data.
LangGraph's AsyncPostgresSaver requires psycopg3, so checkpointing owns a
small separate pool against the same Postgres database.

**Truth source:** User-visible trip truth is ``TripRun`` + Delivery
Bundle store (SQLAlchemy).  Checkpoint rows are execution-recovery aids only.
There is **no shared transaction** with Bundle/TripRun commits (separate pool,
``autocommit=True``).  After a crash, resume and finalizer must treat store
state as authoritative; ``run_id:delivery:create`` idempotency absorbs double
finalizer attempts.  Do not assume checkpoint "past finalizer" means Bundle
was committed (or vice versa).
"""

from __future__ import annotations

import logging
from typing import Tuple

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..config import Settings
from ..db.checkpoint_schema import missing_checkpoint_tables_async
from ..utils.log_filters import install_checkpoint_serde_log_dedup

logger = logging.getLogger(__name__)


async def build_checkpointer(
    settings: Settings,
) -> Tuple[AsyncPostgresSaver, AsyncConnectionPool]:
    """Bind the LangGraph Postgres checkpointer to an already-migrated database.

    Deliberately does not call ``setup()`` — creating those tables is DDL, which
    ``journeypilot migrate`` owns. Missing tables raise here so an unmigrated
    database surfaces at startup rather than on the first interrupt.

    Constructing the saver is also where its serializer's log noise starts, so
    the one de-duplicating filter is installed here: this is the single place any
    entrypoint — the served app via ``AppBuilder.build()``, a walkthrough harness,
    a seeding script — builds a checkpointer, so none of them has to remember to
    do it. The install is idempotent and touches logging only; the serializer
    itself is deliberately left alone (an ``allowed_msgpack_modules`` allowlist
    would switch it to strict mode, where an unlisted type is silently downgraded
    to a raw dict on resume instead of raising).
    """
    install_checkpoint_serde_log_dedup()

    connection_kwargs = {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
    }
    pool = AsyncConnectionPool(
        conninfo=settings.database.sync_url,
        min_size=1,
        max_size=settings.database.pool_size,
        kwargs=connection_kwargs,
        open=False,
    )
    await pool.open()

    try:
        async with pool.connection() as conn:
            missing = await missing_checkpoint_tables_async(conn)
        if missing:
            raise RuntimeError(
                "LangGraph checkpoint 表缺失（" + "、".join(missing) + "）。"
                "它们由启动编排器建立，API 进程不建表 —— 先跑 `journeypilot migrate`。"
            )
        checkpointer = AsyncPostgresSaver(pool)
        logger.info("LangGraph Postgres checkpointer 已就绪")
        return checkpointer, pool
    except Exception:
        await pool.close()
        raise
