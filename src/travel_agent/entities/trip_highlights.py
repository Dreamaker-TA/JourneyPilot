"""The single authority for a trip's whole-run highlight lines.

``StructuredItineraryV2.highlights`` was declared, projected into the report,
published on the public payload, given its own PDF chapter and its own two
surfaces in the browser — and never written by anybody.  Five consumers read a
list that materialization did not pass, so it was ``[]`` on every plan ever
delivered and all five rendered nothing.

The lines are derived from what the itinerary actually holds, for the same
reason ``entities/day_theme.py`` derives a Day's title that way: a model-written
summary of a plan is a second account of the plan, free to disagree with it, and
nothing in the repo could tell which one was wrong.  Deriving costs no model
call and cannot describe a leg or a stop the traveller will not find below.

What a line answers is "what is this trip made of" — the cross-city services
that give it its shape, the places it is built around, and where it sleeps.  It
deliberately does **not** repeat a stop's own ``visit_highlights``: those already
print on the stop's card and in its report row, and a trip-level digest that
restates them tells the reader nothing new.

The derivation runs once, at materialization, and every surface prints the
stored result.  Deriving it a second time at projection would recreate the
defect ``day_theme`` exists to remove.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .delivery_bundle import (
    DiningStop,
    LodgingStay,
    StructuredItineraryV2,
    TransportLeg,
    VisitStop,
)
from .delivery_presentation import TRANSPORT_MODE_LABELS, endpoint_label

# Five is what the surfaces were built for: the PDF chapter and the report
# section list every line, and the workspace overview joins the first two into
# one sentence — so the first two must carry the trip on their own.
MAX_TRIP_HIGHLIGHTS = 5

_MAX_NAMED_LONG_DISTANCE = 4
_MAX_VISITS = 4
_MAX_DINING = 3
_MAX_LODGING = 2

_LONG_DISTANCE_LABEL = "跨城交通"
_VISIT_LABEL = "主要景点"
_DINING_LABEL = "特色餐饮"
_LODGING_LABEL = "住宿"


def _unique(names: Sequence[str], *, limit: int) -> list[str]:
    """The first ``limit`` distinct non-empty names, in the trip's own order.

    Chronological, not ranked: the itinerary's order is a fact about the plan,
    while "most notable" would be a judgement none of these entities carries.
    Distinctness matters beyond tidiness — the report renders each line under a
    React key of its own text, so two identical lines would collide.
    """

    seen: list[str] = []
    for name in names:
        cleaned = name.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
        if len(seen) == limit:
            break
    return seen


def _long_distance_service(leg: TransportLeg) -> str:
    """Name one cross-city leg the way a reader can match it against a ticket.

    The service number leads when the leg has one, because that is what is
    printed on the ticket; the mode word stands in when it does not.  Endpoints
    carry their station code for the same reason
    (``delivery_presentation.endpoint_label``): same-named stations are
    otherwise indistinguishable.
    """

    route = f"{endpoint_label(leg.from_endpoint)} → {endpoint_label(leg.to_endpoint)}"
    service = next(
        (
            segment.service_number or segment.line_name
            for segment in leg.segments
            if segment.service_number or segment.line_name
        ),
        None,
    )
    lead = service.strip() if service and service.strip() else TRANSPORT_MODE_LABELS[leg.selected_mode]
    return f"{lead} {route}"


def _long_distance_line(legs: Sequence[TransportLeg]) -> Optional[str]:
    long_distance = [leg for leg in legs if leg.transport_class == "long_distance"]
    if not long_distance:
        return None
    named = _unique(
        [_long_distance_service(leg) for leg in long_distance],
        limit=_MAX_NAMED_LONG_DISTANCE,
    )
    if not named:
        return None
    body = " · ".join(named)
    # A multi-city chain longer than the line can hold says how many legs it
    # really has rather than silently presenting a truncated route as whole.
    if len(long_distance) > len(named):
        body = f"{body}，共 {len(long_distance)} 段"
    return f"{_LONG_DISTANCE_LABEL}：{body}"


def _named_line(label: str, names: Sequence[str], *, limit: int) -> Optional[str]:
    picked = _unique(names, limit=limit)
    if not picked:
        return None
    return f"{label}：{'、'.join(picked)}"


def _lodging_line(stays: Sequence[LodgingStay]) -> Optional[str]:
    entries: list[str] = []
    for stay in stays:
        name = stay.name.strip()
        if not name:
            continue
        entry = f"{name}（{stay.nights} 晚）"
        if entry not in entries:
            entries.append(entry)
        if len(entries) == _MAX_LODGING:
            break
    if not entries:
        return None
    return f"{_LODGING_LABEL}：{'、'.join(entries)}"


def derive_trip_highlights(
    *,
    visits: Sequence[VisitStop],
    dining: Sequence[DiningStop],
    lodging: Sequence[LodgingStay],
    transport_legs: Sequence[TransportLeg],
) -> list[str]:
    """Digest one whole itinerary into at most :data:`MAX_TRIP_HIGHLIGHTS` lines.

    Ordered so the first two lines stand alone: the workspace overview prints
    only those two, and the cross-city services plus the places the trip is
    built around are what a traveller reads a trip by.  An itinerary holding
    none of the four returns no lines, and every consumer already renders
    nothing for an empty list.
    """

    lines = [
        _long_distance_line(transport_legs),
        _named_line(_VISIT_LABEL, [stop.name for stop in visits], limit=_MAX_VISITS),
        _named_line(_DINING_LABEL, [stop.name for stop in dining], limit=_MAX_DINING),
        _lodging_line(lodging),
    ]
    return [line for line in lines if line][:MAX_TRIP_HIGHLIGHTS]


def with_derived_highlights(itinerary: StructuredItineraryV2) -> StructuredItineraryV2:
    """The same itinerary, carrying the highlights its own entities imply.

    ``StructuredItineraryV2`` cannot default this field the way it defaults
    ``cost_summary`` — the derivation reads transport wording from
    ``delivery_presentation``, which imports the entity module — so an itinerary
    built by a constructor call carries whatever ``highlights`` the caller
    passed, including nothing.  This is the one function that repairs that, and
    every writer of an existing itinerary goes through it.
    """

    return itinerary.model_copy(
        update={
            "highlights": derive_trip_highlights(
                visits=itinerary.visit_stops,
                dining=itinerary.dining_stops,
                lodging=itinerary.lodging_stays,
                transport_legs=itinerary.transport_legs,
            )
        }
    )
