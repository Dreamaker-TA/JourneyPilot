"""One place that spaces out requests to a single upstream.

Three call sites needed this before a fourth did: Nominatim, Overpass and the
Transitous route API each kept an identical private class, and adding a per-MCP
gate would have made a third copy of the same eight lines.  A gate is trivial to
write and exactly that is the problem — a copy is where the next fix does not
land.

The gate is not a quota accountant and not a semaphore.  It answers one question:
has enough wall clock passed since this upstream's last request?  Concurrency is
somebody else's invariant — the MCP manager already holds a per-server lock, and
the HTTP callers are serialized by their own gate's lock.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict


class RateGate:
    """Delay until ``min_interval_seconds`` has passed since the last request."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def acquire(self, min_interval_seconds: float) -> None:
        async with self._lock:
            wait = min_interval_seconds - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()


_GATES: Dict[str, RateGate] = {}


def rate_gate_for(key: str) -> RateGate:
    """The process-wide gate for one upstream, created on first use.

    Keyed rather than per-caller: two call sites that talk to the same upstream
    have to share the spacing, or each one honours an interval the upstream never
    agreed to.
    """

    gate = _GATES.get(key)
    if gate is None:
        gate = RateGate()
        _GATES[key] = gate
    return gate
