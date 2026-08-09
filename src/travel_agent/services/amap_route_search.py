"""Mainland-China street and public-transit routing backed by amap Directions.

Why this exists at all: the global aggregator behind
:mod:`travel_agent.services.global_route_search` (Transitous/MOTIS) carries no
transit feed for mainland China. Measured against the exact endpoint
pair a real run asked for — 深圳 华强北 → 世界之窗, ~7.9 km apart — MOTIS returned
``itineraries: 0`` with ``n_routes_visited: 0`` and ``n_start_offsets: 0``, i.e.
no stop and no route anywhere near either end; the same query shape in Berlin
returns ten itineraries. Every mainland local connector therefore failed
Provider research, and the Itinerary Planner authored one instead: of six legs
in that run's Shenzhen itinerary, four were ``authored_entity`` with invented
mode and duration, all stamped ``route_status: ready``. One of them was a
150-minute bus for a 24.96 km hop; another was an 80-minute metro from Shenzhen
to Shanghai.

amap answers the same three questions with real data — the 华强北 → 世界之窗 hop is
41 minutes and ¥4 on 地铁1号线 — so inside the China coordinate box it is the
provider, and Transitous keeps the rest of the world. One tool name, one
normalized route shape, two upstreams; ``provider`` on the result says which one
answered.

**Absolute times are deliberately absent.** amap Directions returns durations,
not a timetable: ``station_start_time`` is empty on every busline in the
responses measured here. ``TransportSegment.departure_at``/``arrival_at`` are
optional, and a local connector has never needed them (the Day's own schedule
comes from the stops it sits between), so this normalizer leaves them null
rather than synthesising clock times by adding durations to the requested
departure. A synthesised time is indistinguishable from a provider-attested one
at every downstream reader, which is exactly the confusion this file must not
create.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx

from ..config import get_settings
from ..utils.coordinates import gcj02_to_wgs84, wgs84_to_gcj02
from ..utils.log_redaction import install_query_secret_redaction
from ..utils.rate_gate import rate_gate_for

# amap takes its key as a query parameter and offers no header form, and ``httpx``
# logs the full URL of every request at INFO. Installed at import — before this
# module can make its first request — so the key never reaches ``backend.log``.
install_query_secret_redaction()


AmapRouteMode = Literal["public_transit", "walk", "drive", "bike"]

# One path per mode. Transit and the two street modes are v3; bicycling only
# exists on v4, and answers under a different envelope (``data`` not ``route``).
_MODE_PATHS: Dict[AmapRouteMode, str] = {
    "public_transit": "/v3/direction/transit/integrated",
    "walk": "/v3/direction/walking",
    "drive": "/v3/direction/driving",
    "bike": "/v4/direction/bicycling",
}

# amap words a transit line's kind in Chinese prose rather than a code, so the
# match is on a contained keyword. Anything unrecognised stays ``bus`` — the
# generic surface vehicle — instead of guessing at a rail mode the traveller
# would then be told to look for on a platform.
_LINE_KIND_MODES: Tuple[Tuple[str, str], ...] = (
    ("地铁", "metro"),
    ("轻轨", "metro"),
    ("magnet", "train"),
    ("磁悬浮", "train"),
    ("有轨电车", "tram"),
    ("电车", "tram"),
    ("城际", "train"),
    ("动车", "high_speed_rail"),
    ("高铁", "high_speed_rail"),
)

# The MCP path spaces its amap calls with ``rate_gate_for("mcp:amap-maps")``.
# The quota being protected belongs to the *API key*, not to the transport that
# carries the request, so this direct caller has to share that one gate — two
# gates would each honour an interval amap never agreed to.
_RATE_GATE_KEY = "mcp:amap-maps"


# Geometry and per-step narration, dropped from the response before it leaves this
# module.  Nothing downstream reads them — the map draws its lines from the typed
# endpoints — and they are almost the whole payload: measured, one
# Shenzhen transit answer is 53,652 bytes raw against 12,135 for the equivalent
# MOTIS answer, and 32,014 for a driving answer whose every byte over ~1 KB is
# ``steps[].polyline``.
#
# This is not an optimization, it is the same bounding MOTIS gets from
# ``numItineraries=1`` / ``detailedLegs=false``, which amap's transit endpoint has
# no request-side equivalent for.  Leaving it unbounded is not free-and-generous:
# the snapshot travels into the worker's context and the worker then has to copy
# ``routes[0]`` into a JSON packet.  Measured on a real run before this bound: the
# transport worker reached a 73,230-token prompt, hit its 4,096-token output cap
# twice, failed schema repair, and produced **no** transport candidate — so every
# local connector went back to being authored by the model, which is the failure
# this bounding exists to prevent.
_UNBOUNDED_KEYS = frozenset({"polyline", "steps", "via_stops", "tmcs"})


def _bounded(value: Any) -> Any:
    """Drop geometry and per-step narration at every depth."""
    if isinstance(value, dict):
        return {key: _bounded(item) for key, item in value.items() if key not in _UNBOUNDED_KEYS}
    if isinstance(value, list):
        return [_bounded(item) for item in value]
    return value


def _bounded_payload(payload: Dict[str, Any], *, mode: AmapRouteMode) -> Dict[str, Any]:
    """One plan, no geometry — the exact answer this tool promises to return.

    amap offers five transit plans and the normalizer takes the first; the other
    four are competing options nothing reads. Keeping them would put four unread
    itineraries into the evidence snapshot and the model's context.
    """
    bounded = _bounded(payload)
    envelope = bounded.get("data") if mode == "bike" else bounded.get("route")
    if not isinstance(envelope, dict):
        return bounded
    for key in ("transits", "paths"):
        plans = envelope.get(key)
        if isinstance(plans, list) and len(plans) > 1:
            envelope[key] = plans[:1]
    return bounded


class AmapRouteSearchError(RuntimeError):
    """amap could not return an executable route for this query."""


def _api_key() -> str:
    """The amap web-service key, read from the one place it is configured.

    It lives on the ``amap-maps`` MCP server entry because that is where it was
    first needed; this reader does not add a second config field for the same
    secret. ``_env_mapping`` already lets the process environment win there, so
    a deployment that sets ``AMAP_MAPS_API_KEY`` needs no file change.
    """
    server = get_settings().mcp_servers.get("amap-maps")
    key = str((server.env or {}).get("AMAP_MAPS_API_KEY") or "").strip() if server else ""
    if not key:
        raise AmapRouteSearchError("amap route provider is not configured")
    return key


def _endpoint(
    name: str,
    place_id: str,
    latitude: float,
    longitude: float,
) -> Dict[str, Any]:
    return {
        "name": name,
        "place_id": place_id,
        "latitude": latitude,
        "longitude": longitude,
    }


def _stop_endpoint(raw: Any, *, fallback: Dict[str, Any]) -> Dict[str, Any]:
    """One amap transit stop as an endpoint, with its GCJ-02 point converted."""
    if not isinstance(raw, dict):
        return dict(fallback)
    name = str(raw.get("name") or "").strip()
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location = str(raw.get("location") or "")
    if "," in location:
        try:
            raw_longitude, raw_latitude = (float(part) for part in location.split(",", 1))
        except ValueError:
            raw_longitude = raw_latitude = None  # type: ignore[assignment]
        if raw_longitude is not None and raw_latitude is not None:
            longitude, latitude = gcj02_to_wgs84(raw_longitude, raw_latitude)
    if not name:
        return dict(fallback)
    stop_id = str(raw.get("id") or "").strip()
    return {
        "name": name,
        "place_id": f"amap:stop:{stop_id}" if stop_id else fallback["place_id"],
        "latitude": latitude if latitude is not None else fallback["latitude"],
        "longitude": longitude if longitude is not None else fallback["longitude"],
    }


def _seconds_to_minutes(value: Any) -> Optional[int]:
    # Ceil, matching the Transitous normalizer: a travel time is never rounded
    # down, because the number is used to fit a connector into a real gap.
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return max(0, math.ceil(seconds / 60.0))


def _meters(value: Any) -> Optional[int]:
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return None


def _yuan(value: Any) -> Optional[float]:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _line_mode(line_kind: str) -> str:
    for keyword, mode in _LINE_KIND_MODES:
        if keyword in line_kind:
            return mode
    return "bus"


def _segment(
    index: int,
    *,
    mode: str,
    from_endpoint: Dict[str, Any],
    to_endpoint: Dict[str, Any],
    duration_minutes: Optional[int],
    distance_meters: Optional[int],
    line_name: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "segment_id": f"segment_{index}",
        "mode": mode,
        "from_endpoint": from_endpoint,
        "to_endpoint": to_endpoint,
        # Null on purpose — see the module docstring. amap gives durations, not a
        # timetable, and a synthesised clock time would read as an attested one.
        "departure_at": None,
        "arrival_at": None,
        "duration_minutes": duration_minutes,
        "distance_meters": distance_meters,
        "operator_name": None,
        "service_number": None,
        "line_name": line_name,
        "cost_cny": None,
    }


def _transit_segments(
    transit: Dict[str, Any],
    *,
    origin: Dict[str, Any],
    destination: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Flatten one amap transit plan into ordered walk and line segments.

    amap nests a walk and a ridden line inside a single ``segments`` entry, so
    one entry can become two of ours. Zero-distance walks are dropped: amap emits
    them between a station exit and the next entrance, and a 0 m leg is not a
    journey anybody takes.
    """
    segments: List[Dict[str, Any]] = []
    cursor = dict(origin)
    for entry in transit.get("segments") or ():
        if not isinstance(entry, dict):
            continue
        walking = entry.get("walking")
        lines = ((entry.get("bus") or {}).get("buslines") or []) if isinstance(entry.get("bus"), dict) else []
        ridden = next((line for line in lines if isinstance(line, dict)), None)
        if isinstance(walking, dict):
            distance = _meters(walking.get("distance"))
            if distance:
                target = (
                    _stop_endpoint(ridden.get("departure_stop"), fallback=destination)
                    if ridden is not None
                    else dict(destination)
                )
                segments.append(
                    _segment(
                        len(segments) + 1,
                        mode="walk",
                        from_endpoint=cursor,
                        to_endpoint=target,
                        duration_minutes=_seconds_to_minutes(walking.get("duration")),
                        distance_meters=distance,
                    )
                )
                cursor = target
        if ridden is None:
            continue
        boarding = _stop_endpoint(ridden.get("departure_stop"), fallback=cursor)
        alighting = _stop_endpoint(ridden.get("arrival_stop"), fallback=destination)
        line_name = str(ridden.get("name") or "").strip() or None
        segments.append(
            _segment(
                len(segments) + 1,
                mode=_line_mode(str(ridden.get("type") or "")),
                from_endpoint=boarding,
                to_endpoint=alighting,
                duration_minutes=_seconds_to_minutes(ridden.get("duration")),
                distance_meters=_meters(ridden.get("distance")),
                line_name=line_name,
            )
        )
        cursor = alighting
    if not segments:
        return []
    # The requested endpoints are the ones the itinerary asked about, and the
    # traveller's own chain is checked against them, so the outer ends are the
    # request's and not amap's nearest stop.
    segments[0] = {**segments[0], "from_endpoint": dict(origin)}
    segments[-1] = {**segments[-1], "to_endpoint": dict(destination)}
    return segments


def _route_id(material: Any) -> str:
    return f"amap:{hashlib.sha256(repr(material).encode()).hexdigest()[:24]}"


def _normalize_route(
    payload: Dict[str, Any],
    *,
    mode: AmapRouteMode,
    origin: Dict[str, Any],
    destination: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if mode == "public_transit":
        transits = (payload.get("route") or {}).get("transits") or []
        transit = next((item for item in transits if isinstance(item, dict)), None)
        if transit is None:
            return None
        segments = _transit_segments(transit, origin=origin, destination=destination)
        if not segments:
            return None
        ridden = [item for item in segments if item["mode"] != "walk"]
        selected_mode = ridden[0]["mode"] if ridden else "walk"
        duration_minutes = _seconds_to_minutes(transit.get("duration"))
        return {
            "route_id": _route_id(segments),
            "provider": "amap",
            # A transit plan with no ridden line at all is a walk amap answered
            # under the transit endpoint; calling it public_transit would put a
            # walk on a line-chain ticket face.
            "transport_class": "public_transit" if ridden else "flexible",
            "selected_mode": selected_mode,
            "from_endpoint": segments[0]["from_endpoint"],
            "to_endpoint": segments[-1]["to_endpoint"],
            "departure_at": None,
            "arrival_at": None,
            "duration_minutes": duration_minutes if duration_minutes is not None else 0,
            "distance_meters": _meters(transit.get("distance")),
            "total_cost_cny": _yuan(transit.get("cost")),
            "segments": segments,
            "booking_status": "not_required",
        }

    envelope = payload.get("data") if mode == "bike" else payload.get("route")
    paths = (envelope or {}).get("paths") or []
    path = next((item for item in paths if isinstance(item, dict)), None)
    if path is None:
        return None
    street_mode = {"walk": "walk", "drive": "drive", "bike": "bike"}[mode]
    duration_minutes = _seconds_to_minutes(path.get("duration"))
    segment = _segment(
        1,
        mode=street_mode,
        from_endpoint=dict(origin),
        to_endpoint=dict(destination),
        duration_minutes=duration_minutes,
        distance_meters=_meters(path.get("distance")),
    )
    return {
        "route_id": _route_id(segment),
        "provider": "amap",
        "transport_class": "flexible",
        "selected_mode": street_mode,
        "from_endpoint": dict(origin),
        "to_endpoint": dict(destination),
        "departure_at": None,
        "arrival_at": None,
        "duration_minutes": duration_minutes if duration_minutes is not None else 0,
        "distance_meters": segment["distance_meters"],
        # No cost: amap's ``taxi_cost`` prices a taxi, and this mode is the
        # traveller driving. Tolls are the only fare here and they are not one.
        "total_cost_cny": None,
        "segments": [segment],
        "booking_status": "not_required",
    }


async def search_amap_route_raw(
    *,
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
    mode: AmapRouteMode,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Return the complete response for one bounded amap Directions query."""
    settings = get_settings().routing
    server = get_settings().mcp_servers.get("amap-maps")
    min_interval = float(server.min_interval_seconds) if server is not None else 0.0
    key = _api_key()
    # amap speaks GCJ-02 in both directions; the itinerary's endpoints are WGS-84.
    origin_longitude, origin_latitude = wgs84_to_gcj02(from_longitude, from_latitude)
    target_longitude, target_latitude = wgs84_to_gcj02(to_longitude, to_latitude)
    params: Dict[str, Any] = {
        "origin": f"{origin_longitude:.6f},{origin_latitude:.6f}",
        "destination": f"{target_longitude:.6f},{target_latitude:.6f}",
        "key": key,
    }
    if mode == "public_transit":
        # ``city`` is documented as required and is not: measured, the
        # v3 transit endpoint answers the same five plans with it, with an adcode,
        # and with nothing. Omitting it saves a reverse-geocode call per gap.
        params["strategy"] = 0
    owns_client = client is None
    resolved_client = client or httpx.AsyncClient(
        timeout=settings.timeout_seconds,
        headers={"User-Agent": settings.user_agent},
    )
    try:
        await rate_gate_for(_RATE_GATE_KEY).acquire(min_interval)
        response = await resolved_client.get(
            f"{settings.amap_base_url.rstrip('/')}{_MODE_PATHS[mode]}",
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AmapRouteSearchError("amap route provider unavailable") from exc
    finally:
        if owns_client:
            await resolved_client.aclose()
    if not isinstance(payload, dict):
        raise AmapRouteSearchError("amap route provider returned an invalid payload")
    # v3 states success as ``status: "1"``; v4 as ``errcode: 0``. A refusal names
    # itself in ``info``/``errmsg`` (INVALID_USER_KEY, CUQPS_HAS_EXCEEDED…), and
    # that is a different fact from "there is no route", so it fails here.
    if mode == "bike":
        if str(payload.get("errcode") or "0") != "0":
            raise AmapRouteSearchError("amap route provider refused the query")
    elif str(payload.get("status") or "") != "1":
        raise AmapRouteSearchError("amap route provider refused the query")
    # Bounded here rather than at the normalizer: the snapshot, the evidence record
    # and the worker's context all read this one value, so there is one shape.
    return _bounded_payload(payload, mode=mode)


async def amap_route_search(
    *,
    from_name: str,
    from_place_id: str,
    from_latitude: float,
    from_longitude: float,
    to_name: str,
    to_place_id: str,
    to_latitude: float,
    to_longitude: float,
    departure_time: str,
    mode: AmapRouteMode,
) -> Dict[str, Any]:
    """One exact normalized amap route plus its full provider snapshot."""
    raw = await search_amap_route_raw(
        from_latitude=from_latitude,
        from_longitude=from_longitude,
        to_latitude=to_latitude,
        to_longitude=to_longitude,
        mode=mode,
    )
    route = _normalize_route(
        raw,
        mode=mode,
        origin=_endpoint(from_name, from_place_id, from_latitude, from_longitude),
        destination=_endpoint(to_name, to_place_id, to_latitude, to_longitude),
    )
    if route is None:
        raise AmapRouteSearchError("amap route provider found no executable route")
    observed_at = datetime.now(timezone.utc).isoformat()
    return {
        "success": True,
        "provider": "amap",
        "provider_api_version": "amap-v3",
        "source_attribution_url": "https://lbs.amap.com/api/webservice/guide/api/direction",
        "request": {
            "from_name": from_name,
            "from_place_id": from_place_id,
            "to_name": to_name,
            "to_place_id": to_place_id,
            "departure_time": departure_time,
            "mode": mode,
        },
        "routes": [route],
        "provider_response": raw,
        "observed_at": observed_at,
        "retrieved_at": observed_at,
    }
