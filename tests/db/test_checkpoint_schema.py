"""checkpoint 表由编排器建、由 API 只读探测。"""

from __future__ import annotations

import pytest

from travel_agent.db import migrate
from travel_agent.db.checkpoint_schema import (
    create_checkpoint_schema,
    missing_checkpoint_tables,
)
from travel_agent.db.connection import connect
from travel_agent.db.schema_contract import LANGGRAPH_CHECKPOINT_TABLES

pytestmark = pytest.mark.postgres


def test_migration_alone_does_not_create_checkpoint_tables(temp_database):
    """它们不在 `alembic_version` 的管辖里 —— 所以 upgrade head 之后仍然缺。"""

    migrate.upgrade(temp_database)

    with connect(temp_database) as conn:
        assert missing_checkpoint_tables(conn) == LANGGRAPH_CHECKPOINT_TABLES


def test_create_checkpoint_schema_is_idempotent(temp_database):
    migrate.upgrade(temp_database)

    created = create_checkpoint_schema(temp_database)
    assert set(created) == set(LANGGRAPH_CHECKPOINT_TABLES)

    assert create_checkpoint_schema(temp_database) == ()
    with connect(temp_database) as conn:
        assert missing_checkpoint_tables(conn) == ()


async def test_checkpointer_refuses_a_database_without_its_tables(temp_database, settings):
    """缺表时 `build_checkpointer` 抛错并指出下一步，而不是等第一次 interrupt。"""

    from travel_agent.infrastructure.checkpointer import build_checkpointer

    migrate.upgrade(temp_database)
    original = settings.database.name
    settings.database.name = temp_database.database
    try:
        with pytest.raises(RuntimeError, match="journeypilot migrate"):
            await build_checkpointer(settings)
    finally:
        settings.database.name = original
