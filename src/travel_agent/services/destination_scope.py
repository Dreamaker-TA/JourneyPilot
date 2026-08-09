"""Is a provider's answer close enough to be *this destination's* own stop?

One question, one place.  Answer it in a single rung (say, only the authored-place
ladder's ``_within_destination``) and every other path is unguarded: a nationwide text
index answers ``明治神宫`` with the Shibuya shrine first and a same-named neighbourhood
shrine in 茨城県守谷市 second, a worker picks the second, and no layer between the
provider and the traveller's Day has any opinion about a stop 36 km outside the city.

So the rule lives here and the rungs that need it import it:

===========================================  ===============================
rung                                         what it does with this rule
===========================================  ===============================
``nominatim_place_search`` 主文本搜索          a *soft* 15 km viewbox —
                                             a ranking preference, never a
                                             filter, so a named far point
                                             still resolves
``research_packet_output`` 选项枚举            drops far options out of the
                                             enum the worker may pick from,
                                             so the near one is what gets
                                             chosen rather than lost.  It
                                             reads ``DESTINATION_DISTANCE_KEY``
                                             off the record it is handed, so
                                             **every** path that binds a place
                                             record owes that annotation;
                                             :func:`annotate_destination_distance`
                                             is the one way to write it
``authored_place_resolution`` 自撰地点梯子      rejects a far answer and keeps
                                             climbing
``candidate_admission`` 候选准入                rejects, as the backstop for
                                             any path that reaches admission
                                             without passing the enum
===========================================  ===============================

Two entry points deliberately do **not** enforce it, and both are named here so
the omission is a decision rather than an oversight:

* ``api/routes/places.py`` — the request being resolved *is* the destination, so
  there is no destination to be inside of yet.
* ``candidate_readmission`` — it re-admits identities already stored in a Bundle
  and cannot introduce a new one, so it is not a way in for a far place.  It is
  handed no destination point, and an absent point is permissive by design; the
  ``require_destination_country_scope`` flag it already honours says the same
  thing about country, for the same reason.

The 15 km in ``nominatim_place_search`` is deliberately *not* this number and the
two must not be merged: that one is "look here first" (it accepts and rejects
nothing), this one is "this is not a stop in this city".  A preference and a
rejection line can share a value by coincidence and still be two quantities.

Pure: no clock, no provider, no I/O.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, MutableMapping, Optional

from .geo_dispersion import haversine_km

# The key a server-normalized place record carries its own answer under.  It lives
# beside the rule instead of beside one provider: the enum in
# ``research_packet_output`` reads this key off *every* place record it is handed,
# and a record is handed to it by four different binding paths (the
# ``global_place_search`` executor plus three deterministic in-code Provider
# bindings).  While the name lived in ``nominatim_place_search`` the three
# in-code paths simply never wrote it, and "absent means unanswerable, never far"
# turned that omission into a silent exemption from the rule: the omission let an
# 80 km stop reach the enum unannotated and be selected, taking the whole lodging
# domain to zero.
DESTINATION_DISTANCE_KEY = "destination_distance_km"

# How far from the controlled destination's own point a provider answer may sit
# and still be that destination's stop.
#
# 25 km is a city-scale radius, not a tuned threshold: it has to clear the widest
# healthy single Day measured so far (14.81 km over 48 domestic Day samples, task
# 1) with room to spare, and still exclude the neighbouring-city branch that
# ``restaurant in <行政区>`` and a nationwide name lookup both keep returning.
# Between those two it is flat — nothing in the corpus of real runs sits between
# 15 and 22 km — so the exact value inside that band is not load-bearing.
#
# Raising it past ~35 km would re-admit a same-named shrine in a neighbouring
# city; lowering it past ~15 km would start rejecting real stops on a legitimately
# wide Day.  Tests derive their fixtures from this constant rather than restating
# 25.0, because pinning the number instead of the rule is a defect.
MAX_DESTINATION_DISTANCE_KM = 25.0


def distance_from_destination_km(
    latitude: Any,
    longitude: Any,
    destination_latitude: Any,
    destination_longitude: Any,
) -> Optional[float]:
    """Great-circle km between a place and its destination, or ``None``.

    ``None`` means "not answerable", and every caller must treat that as *not a
    rejection*: half a coordinate pair, a bool posing as a number, or a
    destination the controlled identity never carried a point for are all cases
    where this rule has nothing to say.  A missing point must never read as
    "infinitely far away" — that would delete every candidate for a destination
    whose geocode is absent, which is a supply outage, not an out-of-scope stop.
    """
    values = []
    for value in (latitude, longitude, destination_latitude, destination_longitude):
        # ``bool`` is an ``int``; a True latitude is not a latitude.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        values.append(float(value))
    place_latitude, place_longitude, home_latitude, home_longitude = values
    if not (-90.0 <= place_latitude <= 90.0 and -90.0 <= home_latitude <= 90.0):
        return None
    if not (-180.0 <= place_longitude <= 180.0 and -180.0 <= home_longitude <= 180.0):
        return None
    return haversine_km(
        place_latitude, place_longitude, home_latitude, home_longitude
    )


def is_within_destination(
    latitude: Any,
    longitude: Any,
    destination_latitude: Any,
    destination_longitude: Any,
    *,
    limit_km: float = MAX_DESTINATION_DISTANCE_KM,
) -> bool:
    """Whether a place is close enough to be this destination's own stop.

    Unanswerable is permissive — see :func:`distance_from_destination_km` for why
    an absent point must not read as an out-of-scope one.
    """
    distance = distance_from_destination_km(
        latitude, longitude, destination_latitude, destination_longitude
    )
    return distance is None or distance <= limit_km


def annotate_destination_distance(
    results: Iterable[MutableMapping[str, Any]],
    *,
    destination_latitude: Any,
    destination_longitude: Any,
) -> None:
    """Write each place record's own distance from the destination it was searched for.

    Every layer that hands a server-normalized place record downstream holds both
    halves at that moment — the place and the destination it asked about — and is
    therefore the only layer that can state the distance.  Downstream reads it off
    the record; it is never recomputed, because the trip identity does not travel
    with a tool envelope.

    Written onto a server-normalized record, so it is evidence rather than a model
    claim.  Absent — not zero, not infinity — when either point is missing: an
    unanswerable distance must stay unanswerable all the way down, because "no
    destination point" is a supply gap and "80 km away" is an out-of-scope stop,
    and collapsing them would delete every candidate for a destination whose
    geocode failed.

    Idempotent by construction, so a record that already carries the annotation
    from its own provider is written with the same value rather than two.
    """
    if destination_latitude is None or destination_longitude is None:
        return
    for place in results:
        distance = distance_from_destination_km(
            place.get("latitude"),
            place.get("longitude"),
            destination_latitude,
            destination_longitude,
        )
        if distance is not None:
            place[DESTINATION_DISTANCE_KEY] = round(distance, 3)


def destination_points(
    controlled_trip_identity: Mapping[str, Any] | None,
) -> dict[str, tuple[float, float]]:
    """Index each controlled destination's own point by its ``place_id``.

    Mirrors ``candidate_gate._destination_country_codes`` deliberately: a
    destination missing either half of its pair contributes no entry at all, so a
    lookup miss and an unusable point are the same case downstream.
    """
    destinations = (controlled_trip_identity or {}).get("destinations") or []
    points: dict[str, tuple[float, float]] = {}
    for item in destinations:
        if not isinstance(item, Mapping):
            continue
        place_id = str(item.get("place_id") or "")
        latitude, longitude = item.get("latitude"), item.get("longitude")
        if not place_id:
            continue
        if isinstance(latitude, bool) or isinstance(longitude, bool):
            continue
        if not isinstance(latitude, (int, float)) or not isinstance(
            longitude, (int, float)
        ):
            continue
        points[place_id] = (float(latitude), float(longitude))
    return points
