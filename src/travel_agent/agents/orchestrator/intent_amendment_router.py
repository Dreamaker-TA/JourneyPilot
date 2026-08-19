"""Runtime amendment policy node."""

from __future__ import annotations

from typing import Any, Dict

from ...entities.state import TravelAgentState
from ...workflows.intent_amendments import apply_runtime_amendments


async def intent_amendment_router_node(state: TravelAgentState) -> Dict[str, Any]:
    return apply_runtime_amendments(state)
