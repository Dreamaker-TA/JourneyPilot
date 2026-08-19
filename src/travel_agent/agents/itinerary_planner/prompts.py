"""Itinerary Planner prompts — composition v2 contract only.

Runtime path: ``itinerary_planner/node.py::_composition_prompt`` builds the
live system/user contract (Recommendation Catalog + ItineraryCompositionDraft /
placement skeleton schema). This module documents that single source of truth
and must not reintroduce the pre-v2 day-plan / activity list envelopes.
"""

from __future__ import annotations

# Intentionally no SYSTEM_PROMPT / TASK_TEMPLATE: the node owns the composition
# prompt so Catalog schema, skeleton-only rules, and required_candidate_kinds
# stay one implementation. Import composition helpers from node when tests need
# the live contract string.
COMPOSITION_PROMPT_OWNER = "travel_agent.agents.itinerary_planner.node._composition_prompt"

CONTRACT_SUMMARY = """
Itinerary composition outputs exactly one ItineraryCompositionDraft JSON object
(or placement skeleton subset). Rules:
- A domain with selected candidates takes only CandidateSelectionPlan primary candidate_ids,
  ordered by the fit scores the Catalog exposes
- A domain with an empty catalog takes an authored entry instead: name, address,
  city and a one-line reason; the server resolves it against the global place
  provider and asks for a different place when it has no map location
- No dayPlans/activities legacy shape, no claims/evidence envelope
- Skeleton phase: Visit/Dining order + long_distance only; connectors later
- Full phase: one connector between same-day Visit/Dining — an admitted
  public_transit/flexible route, or an authored mode plus door-to-door minutes;
  lodging slots
"""
