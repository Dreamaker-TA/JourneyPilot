"""Discrete budget for composition_repair → itinerary loops.

Composition repair is bounded, and the bound is **three** rounds.  One was too
few to be a bound at all — it was a coin flip.  The skeleton contract is a
*set* of independent hard rules
(day count = trip span, a transport placement names a candidate or a route,
adjacent placements carry times, a dining candidate is placed at a meal it
actually serves, …), and the model violates a **different one each round**.  Two
consecutive 上海→杭州→苏州 Runs died with four distinct violations between them
(`trip_9f4b78e6fceb4d5b`, `trip_48d037b92f314789`): round one broke rule A,
round two broke rule B, budget gone, `run_failed` with 「旅行方案暂时无法生成」.
A multi-destination trip has more placements, so it hits this reproducibly.

Wall clock is the real bound, and it always was: `run_deadline` closes
composition at 7.5 minutes and delivery at 8, and a gate re-checks that boundary
before granting anything.  Both failing Runs still had ~2.5 minutes of unused
composition window when they were declared dead.  So this number buys retries
inside a window that is already enforced elsewhere — it cannot make a Run run
long, only make it more likely to finish.

Enforcement is **write-time only** (gate nodes). Do not re-check
``attempts >= MAX`` in ``route_after_*`` after a successful grant: the grant
already incremented ``attempts``, and a second fold would kill a legitimate
itinerary hop.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

# Three itinerary composition retries after a typed composition gap.  See the
# module docstring for why one was not a bound but a coin flip.
MAXIMUM_COMPOSITION_REPAIR_ATTEMPTS = 3

_COMPOSITION_REPAIR = "composition_repair"


def composition_repair_attempts(state: Any) -> int:
    return int(getattr(state, "composition_repair_attempts", 0) or 0)


def composition_repair_budget_exhausted(state: Any) -> bool:
    return composition_repair_attempts(state) >= MAXIMUM_COMPOSITION_REPAIR_ATTEMPTS


def apply_composition_repair_budget(
    state: Any,
    update: Mapping[str, Any],
    *,
    route_key: str,
    exhausted_route: str,
) -> Dict[str, Any]:
    """Enforce the discrete composition_repair budget on a gate state update.

    When ``update[route_key] == "composition_repair"``:

    - if attempts already at the maximum → rewrite the route to
      ``exhausted_route``, the gate's forward pass-through (no increment);
    - otherwise → allow repair and set ``composition_repair_attempts`` to
      ``prior + 1``.
    """
    out: Dict[str, Any] = dict(update)
    if out.get(route_key) != _COMPOSITION_REPAIR:
        return out
    attempts = composition_repair_attempts(state)
    if attempts >= MAXIMUM_COMPOSITION_REPAIR_ATTEMPTS:
        out[route_key] = exhausted_route
        return out
    out["composition_repair_attempts"] = attempts + 1
    return out
