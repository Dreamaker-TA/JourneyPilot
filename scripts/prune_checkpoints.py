"""Prune expired LangGraph checkpoint threads.

Intended for cron or one-off maintenance:
    python scripts/prune_checkpoints.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from travel_agent.builders import AppBuilder
from travel_agent.config import get_settings
from travel_agent.workflows.checkpoint_pruning import CheckpointPruningService


async def main() -> None:
    builder = AppBuilder()
    components = await builder.build()
    try:
        retention = get_settings().checkpoint_retention
        service = CheckpointPruningService(
            trip_run_store=components.trip_run_store,
            checkpointer=components.checkpointer,
            retention=retention,
        )
        result = await service.prune_once()
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    finally:
        await builder.teardown()


if __name__ == "__main__":
    asyncio.run(main())
