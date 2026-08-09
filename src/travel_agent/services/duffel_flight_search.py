"""Duffel v2 flight search normalized into JourneyPilot transport routes."""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .currency_conversion import (
    CurrencyConversionUnavailable,
    convert_to_cny,
)


logger = logging.getLogger(__name__)

DUFFEL_OFFER_REQUEST_URL = "https://api.duffel.com/air/offer_requests"
_IATA_CODE = re.compile(r"^[A-Z]{3}$")
_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


class DuffelFlightSearchError(RuntimeError):
    """A Duffel request or response cannot support a formal route fact."""


def _duration_minutes(value: object) -> int:
    match = _ISO_DURATION.fullmatch(str(value or ""))
    if match is None:
        raise DuffelFlightSearchError("Duffel offer contains an invalid ISO duration")
    parts = {key: int(raw or 0) for key, raw in match.groupdict().items()}
    return parts["days"] * 1440 + parts["hours"] * 60 + parts["minutes"] + (
        1 if parts["seconds"] else 0
    )


def _iata(value: object, *, field: str) -> str:
    code = str(value or "").strip().upper()
    if not _IATA_CODE.fullmatch(code):
        raise DuffelFlightSearchError(f"{field} must be one exact IATA airport or city code")
    return code


def _endpoint(place: Mapping[str, Any]) -> dict[str, Any]:
    code = _iata(place.get("iata_code"), field="provider endpoint")
    return {
        "name": str(place.get("name") or code),
        "place_id": f"iata:{code}",
        "station_code": code,
        "latitude": place.get("latitude"),
        "longitude": place.get("longitude"),
    }


def _provider_datetime(value: object, place: Mapping[str, Any]) -> str:
    try:
        timestamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise DuffelFlightSearchError("Duffel segment contains an invalid timestamp") from exc
    if timestamp.tzinfo is None:
        timezone_name = str(place.get("time_zone") or "").strip()
        try:
            timestamp = timestamp.replace(tzinfo=ZoneInfo(timezone_name))
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise DuffelFlightSearchError(
                "Duffel local timestamp has no valid airport time zone"
            ) from exc
    return timestamp.isoformat()


def _segment(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    carrier = raw.get("operating_carrier") or raw.get("marketing_carrier") or {}
    carrier_code = str(carrier.get("iata_code") or "").strip()
    flight_number = str(
        raw.get("operating_carrier_flight_number")
        or raw.get("marketing_carrier_flight_number")
        or ""
    ).strip()
    service_number = f"{carrier_code}{flight_number}" if carrier_code and flight_number else None
    return {
        "segment_id": str(raw.get("id") or f"segment_{index}"),
        "mode": "flight",
        "from_endpoint": _endpoint(raw.get("origin") or {}),
        "to_endpoint": _endpoint(raw.get("destination") or {}),
        "departure_at": _provider_datetime(
            raw.get("departing_at"), raw.get("origin") or {}
        ),
        "arrival_at": _provider_datetime(
            raw.get("arriving_at"), raw.get("destination") or {}
        ),
        "duration_minutes": _duration_minutes(raw.get("duration")),
        "distance_meters": None,
        "operator_name": carrier.get("name"),
        "service_number": service_number,
        "line_name": carrier.get("name"),
        "cost_cny": None,
    }


def _quote(offer: Mapping[str, Any]) -> Optional[tuple[float, str]]:
    """The offer's own price, in the currency Duffel priced it in.

    Duffel prices an offer in the *account's* currency — measured USD on this
    project's account, for a PVG→HND search, on every one of 86 offers.  This
    used to read ``total_amount`` only when ``total_currency == "CNY"``, so every
    real quote was dropped and both flights of an international trip reached the
    traveller with no fare at all.  Converting is
    ``_price_routes_in_cny``'s job; naming what arrived is this one's.
    """

    amount = offer.get("total_amount")
    currency = str(offer.get("total_currency") or "").strip().upper()
    if amount is None or not currency:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value, currency


def _route(offer: Mapping[str, Any]) -> tuple[dict[str, Any], Optional[tuple[float, str]]]:
    slices = offer.get("slices")
    if not isinstance(slices, list) or len(slices) != 1:
        raise DuffelFlightSearchError("formal one-way route requires exactly one Duffel slice")
    raw_segments = slices[0].get("segments") if isinstance(slices[0], Mapping) else None
    if not isinstance(raw_segments, list) or not raw_segments:
        raise DuffelFlightSearchError("Duffel offer contains no flight segments")
    segments = [_segment(item, index) for index, item in enumerate(raw_segments, 1)]
    offer_id = str(offer.get("id") or "").strip()
    if not offer_id:
        raise DuffelFlightSearchError("Duffel offer has no stable offer id")
    route = {
        "route_id": f"duffel:{offer_id}",
        "provider": "duffel",
        "transport_class": "long_distance",
        "selected_mode": "flight",
        "from_endpoint": segments[0]["from_endpoint"],
        "to_endpoint": segments[-1]["to_endpoint"],
        "departure_at": segments[0]["departure_at"],
        "arrival_at": segments[-1]["arrival_at"],
        "duration_minutes": _duration_minutes(slices[0].get("duration")),
        "distance_meters": None,
        # Filled by ``_price_routes_in_cny`` once, after the offers are chosen, so
        # one rate lookup serves them all.  ``segments[*].cost_cny`` stays ``None``
        # on purpose: Duffel prices the *offer*, not the leg of a connection, and
        # splitting one total across two flights would publish a per-flight fare no
        # airline quoted.  (12306 differs — it quotes a fare per train — which is
        # why the rail adapter does set it.)
        "total_cost_cny": None,
        "segments": segments,
    }
    return route, _quote(offer)


async def _price_routes_in_cny(
    routes: list[dict[str, Any]],
    quotes: list[Optional[tuple[float, str]]],
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Put every quote Duffel returned onto its route, in CNY.

    Returns the conversion basis for ``provider_response``, so the retained
    Provider snapshot says which currency and which day's rate produced the
    numbers rather than leaving them unexplained.

    A quote that arrives and cannot be expressed **announces itself**.  It does
    not fail the search — a flight with no fare is still a flight the traveller
    can take, whereas losing the flight loses the day — but an unannounced
    ``None`` here is indistinguishable from an offer the supplier never priced,
    and that is the confusion this function must not create.
    """

    basis: dict[str, Any] = {
        "fare_currency": None,
        "fare_rate_to_cny": None,
        "fare_rate_date": None,
    }
    for route, quote in zip(routes, quotes):
        if quote is None:
            continue
        amount, currency = quote
        try:
            converted = await convert_to_cny(amount, currency, client=client)
        except CurrencyConversionUnavailable as exc:
            logger.warning(
                "[duffel] offer %s is quoted %.2f %s and ships unpriced: %s",
                route["route_id"],
                amount,
                currency,
                exc,
            )
            continue
        route["total_cost_cny"] = converted.amount_cny
        basis = {
            "fare_currency": converted.source_currency,
            "fare_rate_to_cny": converted.rate_to_cny,
            "fare_rate_date": converted.rate_date,
        }
    return basis


def _is_cross_day_route(route: Mapping[str, Any]) -> bool:
    try:
        departure = datetime.fromisoformat(str(route["departure_at"]))
        arrival = datetime.fromisoformat(str(route["arrival_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    return departure.date() != arrival.date()


def _slices(
    params: Mapping[str, Any],
    *,
    today: date,
) -> list[dict[str, Any]]:
    flight_type = str(params.get("type") or "one_way")
    if flight_type != "one_way":
        raise DuffelFlightSearchError("search_flights currently requires type=one_way")
    departure_date = str(params.get("departure_date") or "")
    try:
        parsed_departure_date = date.fromisoformat(departure_date)
    except ValueError as exc:
        raise DuffelFlightSearchError("departure_date must use YYYY-MM-DD") from exc
    if parsed_departure_date < today:
        raise DuffelFlightSearchError(
            "departure_date cannot be earlier than the current date"
        )
    return [
        {
            "origin": _iata(params.get("origin"), field="origin"),
            "destination": _iata(params.get("destination"), field="destination"),
            "departure_date": departure_date,
        }
    ]


async def search_duffel_flights(
    params: Mapping[str, Any],
    *,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
    fx_client: httpx.AsyncClient | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Call Duffel v2 and retain at most five complete one-way Provider routes."""
    resolved_token = token or os.getenv("DUFFEL_API_KEY_LIVE", "")
    if not resolved_token:
        raise DuffelFlightSearchError("DUFFEL_API_KEY_LIVE is not configured")
    adults = int(params.get("adults", 1))
    if adults < 1 or adults > 9:
        raise DuffelFlightSearchError("adults must be between 1 and 9")
    cabin_class = str(params.get("cabin_class") or "economy")
    if cabin_class not in {"first", "business", "premium_economy", "economy"}:
        raise DuffelFlightSearchError("unsupported cabin_class")
    max_connections = params.get("max_connections")
    if max_connections is not None:
        max_connections = int(max_connections)
        if max_connections < 0 or max_connections > 4:
            raise DuffelFlightSearchError("max_connections must be between 0 and 4")

    request_data: dict[str, Any] = {
        "data": {
            "slices": _slices(params, today=today or date.today()),
            "passengers": [{"type": "adult"} for _ in range(adults)],
            "cabin_class": cabin_class,
        }
    }
    if max_connections is not None:
        request_data["data"]["max_connections"] = max_connections
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    try:
        response = await active_client.post(
            DUFFEL_OFFER_REQUEST_URL,
            params={"return_offers": "true", "supplier_timeout": 15000},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Duffel-Version": "v2",
                "Authorization": f"Bearer {resolved_token}",
                "Content-Type": "application/json",
            },
            json=request_data,
        )
        if response.is_error:
            try:
                errors = response.json().get("errors")
            except (ValueError, AttributeError):
                errors = None
            raise DuffelFlightSearchError(
                f"Duffel API returned HTTP {response.status_code}: {errors or 'unknown error'}"
            )
        payload = response.json()
    finally:
        if owns_client:
            await active_client.aclose()

    data = payload.get("data") if isinstance(payload, Mapping) else None
    offers = data.get("offers") if isinstance(data, Mapping) else None
    if not isinstance(offers, list):
        raise DuffelFlightSearchError("Duffel response has no offers list")
    require_cross_day = params.get("require_cross_day", False)
    if not isinstance(require_cross_day, bool):
        raise DuffelFlightSearchError("require_cross_day must be a boolean")
    routes: list[dict[str, Any]] = []
    quotes: list[Optional[tuple[float, str]]] = []
    for offer in offers:
        if not isinstance(offer, Mapping):
            continue
        try:
            route, quote = _route(offer)
        except (DuffelFlightSearchError, TypeError, ValueError):
            continue
        if require_cross_day and not _is_cross_day_route(route):
            continue
        routes.append(route)
        quotes.append(quote)
        if len(routes) == 5:
            break
    if not routes:
        if require_cross_day:
            raise DuffelFlightSearchError(
                "Duffel returned no complete cross-day one-way routes"
            )
        raise DuffelFlightSearchError("Duffel returned no complete one-way routes")
    fare_basis = await _price_routes_in_cny(routes, quotes, client=fx_client)
    return {
        "success": True,
        "provider": "duffel",
        "provider_api_version": "v2",
        # Which of Duffel's environments answered.  Named ``data_environment``
        # rather than ``provider_mode`` because ``mode`` already means transport
        # mode one module over (``global_route_search``), and the two collided in
        # every discussion of this field.  The values are the contract's own
        # (``ProviderSnapshotProvenance.data_environment``), so the disclosure a
        # traveller eventually reads is driven by this and not by hardcoded copy.
        "data_environment": "production" if data.get("live_mode") is True else "sandbox",
        "routes": routes,
        "provider_response": {
            "request_id": data.get("id"),
            "live_mode": data.get("live_mode"),
            "offer_count": len(offers),
            # The conversion basis travels with the snapshot that is retained as
            # this route's Provider evidence, so ``routes[*].total_cost_cny`` can
            # always be traced back to the amount the airline quoted and the day's
            # published rate.  ``fare_rate_date`` is the rate publisher's own date.
            **fare_basis,
        },
    }
