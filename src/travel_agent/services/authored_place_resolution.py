"""Locate an authored itinerary entry on the map, or fail closed.

An authored entry is a place the Itinerary Planner named itself because the
research catalog was too thin to cover a slot. It only enters the itinerary once
a provider returns a stable ``place_id`` and coordinates for it, so the strength
of this resolution ladder is what decides whether such an entry ships.

The ladder is deterministic and runs per entry until one rung answers. The
destination's country decides which rung goes first, because the per-entry
budget is small enough that rung order is the whole outcome:

**CN destination**

1. **amap**. OSM's coverage of individual Chinese restaurants and shops is thin;
   amap's POI database is the one that holds them, so it leads.
   ``maps_text_search`` returns identity, ``maps_search_detail`` returns the
   point, and the GCJ-02 point is converted to WGS-84 to match every other place
   in the bundle.
2. **Nominatim text search**, for the CN entries OSM does hold — parks, temples,
   museums, most of what a Visit entry names.

**Everywhere else**

1. **Nominatim text search**, scoped to the controlled destination's country and
   tried on the local-language name first. OSM indexes a place under the name on
   its sign, so the local name is the one that hits.
2. **Overpass**. It matches ``name:en`` / ``alt_name`` / ``brand`` inside the
   destination's own area, which finds a place whose OSM name is in a different
   script than the one the planner wrote. One alias per query: a single
   alternation over the whole alias set exceeds the interpreter's timeout.

Every rung's answer must lie within the destination it was authored for, and a
provider that could not answer is logged as unavailable rather than read as an
absent place.

The amap rung is the one rung that goes through a registered tool, and it does
so through the caller's audited executor (:class:`AuthoredPlaceToolAccess`).
This service owns no tool wiring of its own: an authored identity is treated as
more trustworthy than a candidate's precisely because a real provider answered
for it, and that claim only holds while every such call leaves a durable Tool
Gateway audit row.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from ..entities.place_identity import stable_place_id_amap_poi
from ..tools.governance import ToolExecutionStatus, is_tool_execution_envelope
from ..utils.coordinates import amap_location_to_wgs84
from .destination_scope import is_within_destination
from .nominatim_place_search import (
    NominatimPlaceSearchError,
    is_concrete_dining_place,
    lookup_nominatim_osm_ids,
    normalize_country_code,
    normalize_nominatim_place,
    search_nominatim_raw,
    search_overpass_place_ids,
)

logger = logging.getLogger(__name__)

_AMAP_TEXT_SEARCH = "maps_text_search"
_AMAP_SEARCH_DETAIL = "maps_search_detail"
# amap returns its whole relevance ranking; only the top few are the same place.
_AMAP_RECORDS_PER_ALIAS = 3
# One Overpass query per alias: a single alternation over several aliases and
# several tag filters exceeds the interpreter's own timeout, which arrives as an
# unavailable provider rather than as an empty result.  Each query scans the whole
# destination bbox, so an entry no provider knows pays for every one of them —
# keep the fan-out to the spellings that actually change the outcome.
_OSM_ALIAS_QUERIES = 3
# A stop belongs to the destination it was authored for. The Overpass bounding
# box spans roughly 80km and Nominatim's text index is nationwide, so both can
# answer with the same brand in a neighbouring city.  The distance itself is
# ``destination_scope.MAX_DESTINATION_DISTANCE_KM`` — shared, because this rung is not
# the only one that enforces it.

# Branch qualifiers a planner appends to a place's own name. Stripping them
# yields the shorter alias Overpass and amap actually carry.
_BRANCH_SUFFIXES = (
    "総本店",
    "本店",
    "支店",
    "分店",
    "总店",
    "總店",
    "Honten",
    "honten",
    "Main Store",
    "Main Branch",
)
_BRANCH_PARENTHETICAL = re.compile(r"[（(][^（()）]*[)）]\s*$")


@dataclass(frozen=True)
class AuthoredPlaceToolAccess:
    """The audited tool access one authored-place ladder is handed.

    ``execute`` is ``agents.utils.execute_tool`` bound to the caller's own run
    context: it is what applies the Tool Gateway allowlist, the provider snapshot
    cache, the research-window boundary and — the reason this indirection exists
    — the durable ``tool_execution_audits`` row. It answers with a
    ToolExecutionEnvelope, never with the provider's raw dict.

    ``has_tool`` is the caller's answer to whether a tool exists in this
    deployment at all. The ladder asks before it calls, because an unregistered
    tool would otherwise cost a retry ladder per alias and log a provider failure
    for a provider that was never deployed.
    """

    execute: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]
    has_tool: Callable[[str], bool]


@dataclass(frozen=True)
class AuthoredPlaceScope:
    """The controlled destination one authored entry is resolved inside."""

    country_code: Optional[str] = None
    destination_place_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


def _normalized(value: Optional[str]) -> str:
    return " ".join(str(value or "").split())


def _strip_branch_qualifier(name: str) -> str:
    stripped = _BRANCH_PARENTHETICAL.sub("", name).strip()
    for suffix in _BRANCH_SUFFIXES:
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)].strip()
            break
    return stripped


def _search_aliases(local_name: str, name: str) -> List[str]:
    """Bounded alias set for one entry, most specific first."""
    values = [
        local_name,
        name,
        _strip_branch_qualifier(local_name),
        _strip_branch_qualifier(name),
    ]
    return list(
        dict.fromkeys(alias for alias in (_normalized(value) for value in values) if alias)
    )[:4]


def _brand_segment(value: str) -> str:
    """The leading segment of a spaced local-language signage name: its brand.

    ``すしざんまい 道頓堀店`` is written brand-then-branch, and OSM frequently
    carries only the brand on the node.  Matching it can bind a different branch
    of the same chain inside the same destination — the same trade
    :func:`_strip_branch_qualifier` already makes, and the alternative is having
    no location for a real place.

    Only local-language names split this way.  The leading word of a romanization
    is an ordinary short word (``Kani``, ``Sushi``) that would match anything.
    """
    normalized = _normalized(value)
    if normalized.isascii():
        return ""
    head = normalized.split(" ")[0]
    return head if len(head) >= 2 and head != normalized else ""


def _osm_recall_aliases(local_name: str, name: str) -> List[str]:
    """Alias set for the OSM rungs, which match structured name tags.

    Wider than :func:`_search_aliases`: Overpass matches localized/alternate tags
    inside one bounded destination, so a space-free spelling or the brand alone is
    still the same place.  amap ranks by relevance instead and keeps the narrow
    set, where a loose alias would bind a neighbour.

    Ordered most-specific-first because each alias is one Overpass query and the
    first accepted match wins.
    """
    conservative = _search_aliases(local_name, name)
    local = _normalized(local_name)
    widened = [
        local,
        # OSM carries 松阪牛焼肉M for a sign that reads 松阪牛焼肉 M.
        local.replace(" ", ""),
        _brand_segment(local_name),
        *conservative,
    ]
    return list(dict.fromkeys(alias for alias in widened if alias))[:_OSM_ALIAS_QUERIES]


def _nominatim_queries(local_name: str, name: str, city: str, address: str) -> List[str]:
    """Query phrasings, local name first and narrowing to the bare name."""
    candidates = [
        f"{local_name} {address}",
        f"{local_name} {city}",
        local_name,
        local_name.replace(" ", ""),
        f"{name} {city}",
        name,
        # A brand written without its branch still narrows to the right chain
        # inside the city, which the bare branch qualifier does not.
        f"{_brand_segment(local_name)} {city}".strip(),
    ]
    return list(
        dict.fromkeys(query for query in (_normalized(value) for value in candidates) if query)
    )


def _within_destination(place: Dict[str, Any], scope: AuthoredPlaceScope) -> bool:
    """Whether a provider answer is close enough to be the destination's own stop.

    The rule and its distance now live in ``services/destination_scope.py`` — this
    rung was the only place that had them, and the consequence was that the tool
    path reached the traveller's Day without ever asking the question.  The
    haversine here was also a second copy of ``geo_dispersion.haversine_km``
    (same formula, same radius).

    One behaviour difference is kept deliberately: a place with no usable
    coordinates is rejected *here*, because this rung's whole job is to resolve an
    authored name into a located stop and it has further rungs to try.  The shared
    helper is permissive about an unanswerable pair, so the check is explicit.
    """
    if scope.latitude is None or scope.longitude is None:
        return True
    latitude, longitude = place.get("latitude"), place.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return False
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return False
    return is_within_destination(
        latitude,
        longitude,
        scope.latitude,
        scope.longitude,
    )


def _accepts(
    place: Dict[str, Any],
    *,
    kind: str,
    country_code: Optional[str],
    scope: Optional[AuthoredPlaceScope] = None,
) -> bool:
    if country_code is not None and place["provider_country_code"] != country_code:
        return False
    if scope is not None and not _within_destination(place, scope):
        return False
    if kind == "dining":
        return is_concrete_dining_place(place)
    return True


async def _resolve_via_nominatim(
    *,
    local_name: str,
    name: str,
    city: str,
    address: str,
    kind: str,
    country_code: Optional[str],
    scope: AuthoredPlaceScope,
) -> Optional[Dict[str, Any]]:
    for query in _nominatim_queries(local_name, name, city, address):
        try:
            observation = await search_nominatim_raw(
                query,
                country_code=country_code,
                limit=5,
                # Same family as the tool path: this ladder holds the
                # destination's point too, and Nominatim's text index is
                # nationwide.  Focusing here is what makes the provider rank the
                # destination's own branch first, instead of leaving
                # ``_within_destination`` to discard a neighbouring city's after
                # the fact — which is only a rejection, never a resolution.
                focus_latitude=scope.latitude,
                focus_longitude=scope.longitude,
            )
        except (NominatimPlaceSearchError, ValueError) as exc:
            # One unavailable phrasing does not settle the entry; the remaining
            # phrasings and the rungs below still get their turn.
            logger.warning(
                "authored place nominatim lookup unavailable query=%r: %s", query, exc
            )
            continue
        for item in observation.items:
            place = normalize_nominatim_place(item)
            if place is not None and _accepts(
                place, kind=kind, country_code=country_code, scope=scope
            ):
                return place
    return None


def _provider_payload(envelope: Any, *, tool_name: str, alias: str) -> Optional[Mapping[str, Any]]:
    """The provider's own answer inside one Tool Gateway envelope.

    A blocked, failed or deadline-cut call is a well-formed envelope with no
    ``sanitized_result``. Reading the envelope itself as the payload would turn
    every such round into "the provider says there is no such place", which is
    the one conclusion this ladder must not draw from an unavailable provider.
    """
    if not is_tool_execution_envelope(envelope):
        raise TypeError(
            f"authored place resolution expects a ToolExecutionEnvelope from {tool_name}"
        )
    if envelope.get("status") != ToolExecutionStatus.SUCCESS.value:
        logger.warning(
            "authored place amap lookup unavailable tool=%s alias=%r status=%s: %s",
            tool_name,
            alias,
            envelope.get("status"),
            envelope.get("error") or envelope.get("result_summary"),
        )
        return None
    payload = envelope.get("sanitized_result")
    return payload if isinstance(payload, Mapping) else None


def _amap_records(result: Any) -> List[Mapping[str, Any]]:
    if not isinstance(result, Mapping) or not result.get("success"):
        return []
    records = result.get("results")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, Mapping)]


def _amap_point(result: Any) -> tuple[Optional[float], Optional[float]]:
    """Read one detail lookup's point location."""
    if not isinstance(result, Mapping):
        return None, None
    return amap_location_to_wgs84(result.get("location"))


def _names_the_same_place(alias: str, poi_name: str) -> bool:
    """Whether an amap POI is the place that was asked for, not a near neighbour.

    amap ranks by relevance, so a query it cannot match still returns the
    restaurants around it. Binding one of those to an authored entry would
    deliver a different place under the planner's name.
    """
    left, right = alias.casefold(), poi_name.casefold()
    return left in right or right in left


async def _resolve_via_amap(
    *,
    local_name: str,
    name: str,
    city: str,
    scope: AuthoredPlaceScope,
    tools: AuthoredPlaceToolAccess,
) -> Optional[Dict[str, Any]]:
    if not tools.has_tool(_AMAP_TEXT_SEARCH) or not tools.has_tool(_AMAP_SEARCH_DETAIL):
        return None
    for alias in _search_aliases(local_name, name):
        search = _provider_payload(
            await tools.execute(_AMAP_TEXT_SEARCH, {"keywords": alias, "city": city}),
            tool_name=_AMAP_TEXT_SEARCH,
            alias=alias,
        )
        for record in _amap_records(search)[:_AMAP_RECORDS_PER_ALIAS]:
            poi_id = _normalized(record.get("id"))
            poi_name = _normalized(record.get("name"))
            place_id = stable_place_id_amap_poi(poi_id)
            if place_id is None or not poi_name:
                continue
            if not _names_the_same_place(alias, poi_name):
                continue
            latitude, longitude = record.get("latitude"), record.get("longitude")
            if not isinstance(latitude, float) or not isinstance(longitude, float):
                # Text search often omits geometry; the detail lookup carries it.
                latitude, longitude = _amap_point(
                    _provider_payload(
                        await tools.execute(_AMAP_SEARCH_DETAIL, {"id": poi_id}),
                        tool_name=_AMAP_SEARCH_DETAIL,
                        alias=alias,
                    )
                )
            if latitude is None or longitude is None:
                continue
            candidate = {
                "place_id": place_id,
                "provider": "amap",
                "provider_place_type": _normalized(record.get("typecode")),
                "provider_country_code": "cn",
                "name": poi_name,
                "address": _normalized(record.get("address")) or poi_name,
                "latitude": latitude,
                "longitude": longitude,
            }
            if not _within_destination(candidate, scope):
                continue
            return candidate
    return None


async def _resolve_via_overpass(
    *,
    local_name: str,
    name: str,
    kind: str,
    scope: AuthoredPlaceScope,
) -> Optional[Dict[str, Any]]:
    if scope.destination_place_id is None:
        return None
    for alias in _osm_recall_aliases(local_name, name):
        try:
            osm_ids = await search_overpass_place_ids(
                [alias],
                kind=kind,
                destination_place_id=scope.destination_place_id,
                destination_latitude=scope.latitude,
                destination_longitude=scope.longitude,
                limit=5,
            )
            items = await lookup_nominatim_osm_ids(osm_ids)
        except (NominatimPlaceSearchError, ValueError) as exc:
            # A provider that could not answer is not a place that does not
            # exist. Say which it was; the ladder still moves to the next alias.
            logger.warning(
                "authored place overpass lookup unavailable alias=%r: %s", alias, exc
            )
            continue
        for item in items:
            place = normalize_nominatim_place(item)
            if place is not None and _accepts(
                place, kind=kind, country_code=scope.country_code, scope=scope
            ):
                return place
    return None


async def resolve_authored_place(
    *,
    name: str,
    local_name: str,
    city: str,
    address: str,
    kind: str,
    scope: AuthoredPlaceScope,
    tools: AuthoredPlaceToolAccess,
) -> Optional[Dict[str, Any]]:
    """Locate one authored place, or return None when no provider knows it.

    Returns the normalized place: stable ``place_id``, coordinates in WGS-84,
    and the provider's own address line.

    ``tools`` is the caller's audited tool access; the CN rung runs every one of
    its provider calls through it.
    """
    if kind not in {"visit", "dining"}:
        raise ValueError(f"unsupported authored place kind: {kind}")
    country_code = normalize_country_code(scope.country_code)
    normalized_local = _normalized(local_name)
    normalized_name = _normalized(name)
    normalized_city = _normalized(city)

    if country_code == "cn":
        # amap first, Nominatim second. OSM barely holds individual Chinese
        # restaurants and shops, so the Nominatim rung answers for almost no CN
        # entry — yet running it first spends the whole per-entry budget
        # (itinerary_planner/node.py::_AUTHORED_ENTRY_BUDGET_SECONDS) on up to
        # seven phrasings before the rung that can actually answer gets its
        # turn. Under a Nominatim brownout the first phrasing alone used to
        # exceed the budget, so ``asyncio.wait_for`` cancelled the ladder and the
        # CN rung was never reached at all. This is the same ordering
        # accommodation_researcher already applies to CN lodging.
        try:
            place = await _resolve_via_amap(
                local_name=normalized_local,
                name=normalized_name,
                city=normalized_city,
                scope=scope,
                tools=tools,
            )
        except Exception:
            logger.warning(
                "authored place amap lookup failed name=%s", normalized_name, exc_info=True
            )
            place = None
        if place is not None:
            return place
        # Nominatim still gets its turn: OSM does hold CN parks, temples and
        # museums, which is most of what a Visit entry names.
        return await _resolve_via_nominatim(
            local_name=normalized_local,
            name=normalized_name,
            city=normalized_city,
            address=_normalized(address),
            kind=kind,
            country_code=country_code,
            scope=scope,
        )

    place = await _resolve_via_nominatim(
        local_name=normalized_local,
        name=normalized_name,
        city=normalized_city,
        address=_normalized(address),
        kind=kind,
        country_code=country_code,
        scope=scope,
    )
    if place is not None:
        return place

    return await _resolve_via_overpass(
        local_name=normalized_local,
        name=normalized_name,
        kind=kind,
        scope=AuthoredPlaceScope(
            country_code=country_code,
            destination_place_id=scope.destination_place_id,
            latitude=scope.latitude,
            longitude=scope.longitude,
        ),
    )


async def resolve_station_point(
    *,
    station_name: str,
    city: str,
    scope: AuthoredPlaceScope,
    tools: AuthoredPlaceToolAccess,
) -> Optional[tuple[float, float]]:
    """Locate a rail station on the map without adopting the provider's identity.

    A 12306 station arrives with a stable identity already — its telecode — and
    the long-distance leg, the placement skeleton and the connector gap all name
    it ``12306:{telecode}``.  What it has never had is a **point**, and a hop with
    no point on one end cannot be handed to a route provider at all: it is dropped
    from the routable set and the itinerary ships an invented duration for the one
    leg the traveller is most likely to mistime — station to first stop.

    So this is the ladder :func:`resolve_authored_place` already runs, with the
    provider's own ``place_id`` deliberately never returned.  Adopting
    ``amap:poi:…`` here would rename an endpoint two upstreams identify by
    telecode, and the connector would then miss its gap on ``endpoint_mismatch``
    instead of on a missing point — the same outcome, harder to read.

    A station name is signage, not a brand: amap and OSM both index it with the
    trailing 站 that 12306 omits, so that spelling leads the alias set.
    """
    country_code = normalize_country_code(scope.country_code)
    signed = _normalized(f"{_normalized(station_name)}站")
    bare = _normalized(station_name)
    if not bare:
        return None
    place: Optional[Dict[str, Any]] = None
    if country_code == "cn":
        # amap leads for the same reason it leads for CN authored places: it is
        # the POI database that actually holds mainland places.
        try:
            place = await _resolve_via_amap(
                local_name=signed,
                name=bare,
                city=_normalized(city),
                scope=scope,
                tools=tools,
            )
        except Exception:
            logger.warning(
                "station point amap lookup failed station=%s", bare, exc_info=True
            )
            place = None
    if place is None:
        place = await _resolve_via_nominatim(
            local_name=signed,
            name=bare,
            city=_normalized(city),
            address="",
            kind="station",
            country_code=country_code,
            scope=scope,
        )
    if place is None:
        return None
    logger.info(
        "station point located station=%s provider=%s under=%r",
        bare,
        place.get("provider"),
        place.get("name"),
    )
    return float(place["latitude"]), float(place["longitude"])


def authored_place_scopes(controlled_trip_identity: Any) -> Dict[str, AuthoredPlaceScope]:
    """Map each controlled destination's place id to its resolution scope."""
    destinations = (
        controlled_trip_identity.get("destinations")
        if isinstance(controlled_trip_identity, Mapping)
        else None
    )
    scopes: Dict[str, AuthoredPlaceScope] = {}
    for item in destinations or []:
        if not isinstance(item, Mapping):
            continue
        place_id = _normalized(item.get("place_id"))
        if not place_id:
            continue
        latitude = item.get("latitude")
        longitude = item.get("longitude")
        scopes[place_id] = AuthoredPlaceScope(
            country_code=_normalized(item.get("country_code")).casefold() or None,
            destination_place_id=place_id,
            latitude=float(latitude) if isinstance(latitude, (int, float)) else None,
            longitude=float(longitude) if isinstance(longitude, (int, float)) else None,
        )
    return scopes


__all__ = [
    "AuthoredPlaceScope",
    "AuthoredPlaceToolAccess",
    "authored_place_scopes",
    "resolve_authored_place",
]
