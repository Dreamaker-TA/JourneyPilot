"""Bounded street and public-transit routing, one tool over two upstreams.

Transitous/MOTIS answers the world; amap answers inside the China coordinate box.
The split is a coverage fact, not a preference: measured against the
endpoint pair a real run actually asked for (深圳 华强北 → 世界之窗, 7.9 km apart)
MOTIS returned zero itineraries with ``n_routes_visited: 0`` — no stop and no
route anywhere near either end — while the same query shape in Berlin returns
ten. Every mainland local connector therefore lost its Provider research and was
authored by the model instead, with invented mode and duration.

Both branches return the same normalized ``routes[0]`` shape; ``provider`` on the
result says which upstream answered, and the caller does not choose.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

import httpx

from ..config import get_settings
from ..utils.coordinates import within_china_coordinate_box
from ..utils.rate_gate import rate_gate_for
from .amap_route_search import AmapRouteSearchError, amap_route_search


RouteMode = Literal["public_transit", "walk", "drive", "bike"]
_PROVIDER_MODE = {
    "BUS": "bus",
    "COACH": "coach",
    "FERRY": "ferry",
    "FOOT": "walk",
    "WALK": "walk",
    "BIKE": "bike",
    "CAR": "drive",
    "SUBWAY": "metro",
    "METRO": "metro",
    "TRAM": "tram",
    "HIGH_SPEED_RAIL": "high_speed_rail",
    "REGIONAL_RAIL": "train",
    "RAIL": "train",
    "TRAIN": "train",
}


class GlobalRouteSearchError(RuntimeError):
    """The global routing provider could not return an executable route."""




_RATE_GATE = rate_gate_for("transitous")


def _coordinate(value: float, *, latitude: bool) -> float:
    number = float(value)
    lower, upper = (-90.0, 90.0) if latitude else (-180.0, 180.0)
    if not lower <= number <= upper:
        raise ValueError("route coordinate is outside the valid range")
    return number


def _departure_time(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError("departure_time is required")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("departure_time must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("departure_time must include a timezone offset")
    return parsed.isoformat()


def _endpoint(
    raw: Dict[str, Any],
    *,
    fallback_name: str,
    fallback_place_id: str,
    fallback_latitude: float,
    fallback_longitude: float,
) -> Dict[str, Any]:
    stop_id = str(raw.get("stopId") or "").strip()
    return {
        "name": str(raw.get("name") or fallback_name).strip() or fallback_name,
        "place_id": f"transitous-stop:{stop_id}" if stop_id else fallback_place_id,
        "latitude": float(raw.get("lat", fallback_latitude)),
        "longitude": float(raw.get("lon", fallback_longitude)),
    }


def _minutes(seconds: Any) -> int:
    try:
        return max(0, math.ceil(float(seconds) / 60.0))
    except (TypeError, ValueError):
        return 0


def _in_request_timezone(value: Any, request_departure_time: str) -> Any:
    """Represent a Provider instant in the destination offset used by the gap."""
    if not isinstance(value, str):
        return value
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        requested = datetime.fromisoformat(
            request_departure_time.replace("Z", "+00:00")
        )
    except ValueError:
        return value
    if (
        timestamp.tzinfo is None
        or timestamp.utcoffset() is None
        or requested.tzinfo is None
        or requested.utcoffset() is None
    ):
        return value
    return timestamp.astimezone(requested.tzinfo).isoformat()


def _normalize_route(
    itinerary: Dict[str, Any],
    *,
    mode: RouteMode,
    from_name: str,
    from_place_id: str,
    from_latitude: float,
    from_longitude: float,
    to_name: str,
    to_place_id: str,
    to_latitude: float,
    to_longitude: float,
    request_departure_time: str,
) -> Optional[Dict[str, Any]]:
    raw_legs = itinerary.get("legs")
    if not isinstance(raw_legs, list) or not raw_legs:
        return None
    segments: list[Dict[str, Any]] = []
    for index, raw_leg in enumerate(raw_legs):
        if not isinstance(raw_leg, dict):
            return None
        provider_mode = str(raw_leg.get("mode") or "").upper()
        normalized_mode = _PROVIDER_MODE.get(provider_mode)
        if normalized_mode is None:
            return None
        raw_from = raw_leg.get("from") if isinstance(raw_leg.get("from"), dict) else {}
        raw_to = raw_leg.get("to") if isinstance(raw_leg.get("to"), dict) else {}
        from_endpoint = _endpoint(
            raw_from,
            fallback_name=from_name,
            fallback_place_id=from_place_id,
            fallback_latitude=from_latitude,
            fallback_longitude=from_longitude,
        )
        to_endpoint = _endpoint(
            raw_to,
            fallback_name=to_name,
            fallback_place_id=to_place_id,
            fallback_latitude=to_latitude,
            fallback_longitude=to_longitude,
        )
        if index == 0:
            from_endpoint = {
                "name": from_name,
                "place_id": from_place_id,
                "latitude": from_latitude,
                "longitude": from_longitude,
            }
        if index == len(raw_legs) - 1:
            to_endpoint = {
                "name": to_name,
                "place_id": to_place_id,
                "latitude": to_latitude,
                "longitude": to_longitude,
            }
        distance = raw_leg.get("distance")
        segments.append(
            {
                "segment_id": f"segment_{index + 1}",
                "mode": normalized_mode,
                "from_endpoint": from_endpoint,
                "to_endpoint": to_endpoint,
                "departure_at": _in_request_timezone(
                    raw_leg.get("startTime"), request_departure_time
                ),
                "arrival_at": _in_request_timezone(
                    raw_leg.get("endTime"), request_departure_time
                ),
                "duration_minutes": _minutes(raw_leg.get("duration")),
                "distance_meters": int(round(float(distance))) if distance is not None else None,
                "operator_name": raw_leg.get("agencyName"),
                "service_number": raw_leg.get("tripShortName") or raw_leg.get("routeShortName"),
                "line_name": raw_leg.get("routeLongName") or raw_leg.get("displayName"),
                "cost_cny": None,
            }
        )
    non_walk_modes = [
        segment["mode"] for segment in segments if segment["mode"] not in {"walk", "bike"}
    ]
    selected_mode = (
        non_walk_modes[0]
        if mode == "public_transit" and non_walk_modes
        else {"walk": "walk", "drive": "drive", "bike": "bike"}.get(mode)
    )
    if selected_mode is None:
        return None
    raw_id = str(itinerary.get("id") or "").strip()
    identity_material = raw_id or repr(
        (itinerary.get("startTime"), itinerary.get("endTime"), segments)
    )
    route_id = f"transitous:{hashlib.sha256(identity_material.encode()).hexdigest()[:24]}"
    distances = [segment["distance_meters"] for segment in segments]
    return {
        "route_id": route_id,
        "provider": "transitous",
        "transport_class": "public_transit" if mode == "public_transit" else "flexible",
        "selected_mode": selected_mode,
        "from_endpoint": segments[0]["from_endpoint"],
        "to_endpoint": segments[-1]["to_endpoint"],
        "departure_at": _in_request_timezone(
            itinerary.get("startTime"), request_departure_time
        ),
        "arrival_at": _in_request_timezone(
            itinerary.get("endTime"), request_departure_time
        ),
        "duration_minutes": _minutes(itinerary.get("duration")),
        "distance_meters": sum(distances) if all(item is not None for item in distances) else None,
        # No fare, because MOTIS publishes none: a Transitous itinerary carries
        # ``duration``/``legs``/``transfers`` and no fare key of any kind (measured
        # against a real Tokyo transit plan).  An unexplained ``None``
        # in a price field is indistinguishable from a quote that was dropped
        # elsewhere, which is the confusion this normalizer must not create.
        "total_cost_cny": None,
        "segments": segments,
        "booking_status": "not_required",
    }


async def search_global_route_raw(
    *,
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
    departure_time: str,
    mode: RouteMode,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Return the complete response for one deliberately bounded MOTIS query."""
    settings = get_settings().routing
    params: Dict[str, Any] = {
        "fromPlace": f"{from_latitude},{from_longitude}",
        "toPlace": f"{to_latitude},{to_longitude}",
        "time": departure_time,
        "numItineraries": 1,
        "maxItineraries": 1,
        "detailedLegs": "false",
        "language": "ja,en,zh",
        "timeout": max(1, min(int(settings.timeout_seconds), 20)),
    }
    if mode == "public_transit":
        params.update(
            transitModes="TRANSIT",
            directModes="",
            maxTransfers=3,
            maxTravelTime=180,
            maxPreTransitTime=1200,
            maxPostTransitTime=1200,
        )
    else:
        params.update(
            transitModes="",
            directModes={"walk": "WALK", "drive": "CAR", "bike": "BIKE"}[mode],
            maxDirectTime=7200,
        )
    owns_client = client is None
    resolved_client = client or httpx.AsyncClient(
        timeout=settings.timeout_seconds,
        headers={"User-Agent": settings.user_agent},
    )
    try:
        await _RATE_GATE.acquire(settings.min_interval_seconds)
        response = await resolved_client.get(
            f"{settings.transitous_base_url.rstrip('/')}/api/v6/plan",
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GlobalRouteSearchError("global route provider unavailable") from exc
    finally:
        if owns_client:
            await resolved_client.aclose()
    if not isinstance(payload, dict):
        raise GlobalRouteSearchError("global route provider returned an invalid payload")
    return payload


async def global_route_search(
    from_name: str,
    from_place_id: str,
    from_latitude: float,
    from_longitude: float,
    to_name: str,
    to_place_id: str,
    to_latitude: float,
    to_longitude: float,
    departure_time: str,
    mode: RouteMode,
) -> Dict[str, Any]:
    """Tool executor returning one exact normalized route plus its full provider snapshot."""
    if not from_name.strip() or not from_place_id.strip() or not to_name.strip() or not to_place_id.strip():
        raise ValueError("route endpoints require names and stable place ids")
    from_latitude = _coordinate(from_latitude, latitude=True)
    from_longitude = _coordinate(from_longitude, latitude=False)
    to_latitude = _coordinate(to_latitude, latitude=True)
    to_longitude = _coordinate(to_longitude, latitude=False)
    departure_time = _departure_time(departure_time)
    if mode not in {"public_transit", "walk", "drive", "bike"}:
        raise ValueError("unsupported global route mode")
    # Both ends inside the box, not either: a route that leaves the box is one
    # neither upstream can answer well, and MOTIS is the one that will say so
    # instead of returning a plausible cross-border plan.
    if within_china_coordinate_box(from_longitude, from_latitude) and within_china_coordinate_box(
        to_longitude, to_latitude
    ):
        try:
            return await amap_route_search(
                from_name=from_name.strip(),
                from_place_id=from_place_id.strip(),
                from_latitude=from_latitude,
                from_longitude=from_longitude,
                to_name=to_name.strip(),
                to_place_id=to_place_id.strip(),
                to_latitude=to_latitude,
                to_longitude=to_longitude,
                departure_time=departure_time,
                mode=mode,
            )
        except AmapRouteSearchError as exc:
            # Not a fallback to the other upstream: MOTIS has no data here, so
            # asking it next would only turn one honest failure into two and end
            # in the same place. The failure is reported as itself.
            raise GlobalRouteSearchError(str(exc)) from exc
    raw = await search_global_route_raw(
        from_latitude=from_latitude,
        from_longitude=from_longitude,
        to_latitude=to_latitude,
        to_longitude=to_longitude,
        departure_time=departure_time,
        mode=mode,
    )
    candidates = raw.get("itineraries") if mode == "public_transit" else raw.get("direct")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise GlobalRouteSearchError("global route provider found no executable route")
    route = _normalize_route(
        candidates[0],
        mode=mode,
        from_name=from_name.strip(),
        from_place_id=from_place_id.strip(),
        from_latitude=from_latitude,
        from_longitude=from_longitude,
        to_name=to_name.strip(),
        to_place_id=to_place_id.strip(),
        to_latitude=to_latitude,
        to_longitude=to_longitude,
        request_departure_time=departure_time,
    )
    if route is None:
        raise GlobalRouteSearchError("global route provider returned an unsupported route")
    result = {
        "success": True,
        "provider": "transitous",
        "provider_api_version": "motis-v6",
        "source_attribution_url": "https://transitous.org/sources/",
        "request": {
            "from_name": from_name.strip(),
            "from_place_id": from_place_id.strip(),
            "to_name": to_name.strip(),
            "to_place_id": to_place_id.strip(),
            "departure_time": departure_time,
            "mode": mode,
        },
        "routes": [route],
        "provider_response": raw,
    }
    observed_at = datetime.now(timezone.utc).isoformat()
    result["observed_at"] = observed_at
    result["retrieved_at"] = observed_at
    return result
