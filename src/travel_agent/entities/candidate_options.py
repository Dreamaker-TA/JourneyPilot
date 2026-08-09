"""What a selection option says about being able to actually use a candidate.

Its own module because two layers have to agree on it exactly.  Composition mints
the option (``entities/itinerary_composition_v2._option``) and the selection
mutation re-derives it to check the option was not tampered with
(``entities/workspace_v2_mutations._apply_selection``); if the two spelled the
rule separately, every visit option would fail that check — which is what
happened the moment visits got slots, because a ``VisitCandidate`` has no
``availability_status`` at all and the mutation read ``None``.
"""

from __future__ import annotations

from .delivery_bundle import ResearchCandidate, VisitCandidate

def candidate_option_availability(candidate: ResearchCandidate) -> str:
    """State what a reader still has to check before turning up.

    Dining and lodging candidates carry a supplier's answer about inventory, so
    the option repeats it **verbatim** — including ``"unavailable"``, which
    ``SelectionOption`` then rejects outright.  That refusal is the existing
    behaviour and is left alone: an unusable candidate must not be quietly
    relabelled into a slot a traveller can pick.

    An attraction has no inventory anyone could hold, so the honest question is
    the other one: does it have to be booked ahead.
    """

    if isinstance(candidate, VisitCandidate):
        return "needs_confirmation" if candidate.reservation_required else "confirmed"
    return str(getattr(candidate, "availability_status", "confirmed"))
