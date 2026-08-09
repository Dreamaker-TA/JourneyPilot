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
from ..utils.log_filters import install_checkpoint_serde_log_dedup

logger = logging.getLogger(__name__)


async def build_checkpointer(
    settings: Settings,
) -> Tuple[AsyncPostgresSaver, AsyncConnectionPool]:
    """Create and initialize the LangGraph Postgres checkpointer.

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
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        logger.info("LangGraph Postgres checkpointer 初始化完成")
        return checkpointer, pool
    except Exception:
        await pool.close()
        raise
