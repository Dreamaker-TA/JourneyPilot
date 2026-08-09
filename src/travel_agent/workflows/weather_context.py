"""Pre-planning destination geography and weather-context workflow nodes."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig

from travel_agent.entities.state import TravelAgentState
from travel_agent.entities.trip_input import ControlledTripIdentity
from travel_agent.entities.weather_planning import DestinationGeoPoint
from travel_agent.infrastructure.weather_provider import (
    WeatherProviderRequest,
    default_weather_providers,
)
from travel_agent.services.weather_context_builder import WeatherContextBuilder


def destination_geo_resolver_node(state: TravelAgentState) -> Dict[str, Any]:
    """Use confirmed PlaceIdentity coordinates; never re-geocode or guess destinations."""

    identity = ControlledTripIdentity.model_validate(state.controlled_trip_identity)
    existing_timezones = {item.destination_id: item.timezone for item in state.destination_geo}
    destinations = [
        DestinationGeoPoint(
            destination_id=item.place_id,
            name=item.name,
            display_name=item.display_name,
            country_code=item.country_code,
            latitude=item.latitude,
            longitude=item.longitude,
            trip_start_date=identity.start_date,
            trip_end_date=identity.end_date,
            timezone=existing_timezones.get(item.place_id),
        )
        for item in identity.destinations
    ]
    return {"destination_geo": destinations}


def _configured_builder(config: Optional[RunnableConfig]) -> WeatherContextBuilder:
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    injected = configurable.get("weather_context_builder")
    if injected is not None:
        return injected
    return WeatherContextBuilder(default_weather_providers())


async def weather_context_builder_node(
    state: TravelAgentState,
    config: Optional[RunnableConfig] = None,
) -> Dict[str, Any]:
    """Build one destination/date weather grid before any planning or research."""

    identity = ControlledTripIdentity.model_validate(state.controlled_trip_identity)
    if not state.destination_geo:
        raise ValueError("weather context requires resolved controlled destinations")
    expected_destination_ids = {item.destination_id for item in state.destination_geo}
    if (
        state.weather_context is not None
        and state.weather_context.trip_start_date == identity.start_date
        and state.weather_context.trip_end_date == identity.end_date
        and {item.destination_id for item in state.weather_context.coverage}
        == expected_destination_ids
    ):
        return {}
    requests = [
        WeatherProviderRequest(
            destination_id=item.destination_id,
            destination_name=item.display_name,
            latitude=item.latitude,
            longitude=item.longitude,
            timezone=item.timezone,
            start_date=item.trip_start_date,
            end_date=item.trip_end_date,
        )
        for item in state.destination_geo
    ]
    result = await _configured_builder(config).build(
        requests=requests,
        weather_data_revision=0,
        trip_start_date=identity.start_date,
        trip_end_date=identity.end_date,
    )
    resolved_timezones = {
        day.destination_id: day.timezone
        for day in result.weather_snapshot.days
        if day.timezone is not None
    }
    destination_geo = [
        item.model_copy(update={"timezone": resolved_timezones.get(item.destination_id)})
        for item in state.destination_geo
    ]
    return {
        "destination_geo": destination_geo,
        "weather_context": result.weather_snapshot,
        "weather_source_records": result.source_records,
        "weather_fact_assertions": result.fact_assertions,
        "weather_field_provenance": result.field_provenance,
    }


def format_weather_context_for_planning(state: TravelAgentState) -> str:
    """The one rendering of weather any prompt gets; unavailable means do not infer.

    This used to be the *second* rendering in the composition prompt, which
    already carried ``WeatherContext.model_dump_json()`` verbatim.  Measured, the
    two together were 36% of that prompt and the day records in them were
    byte-identical.  This one is the survivor because it is the only one with a
    rule attached and the only one every worker already reads; the composition
    prompt's own copy is gone.

    ``hourly_windows`` is not here either, and that is not a truncation — it has
    **no model-facing consumer**.  Every real reader of the hourly detail is
    deterministic server code holding the typed object
    (``candidate_gate._scheduled_transport_weather_day``,
    ``services.weather_adjustment``, ``services.candidate_readmission_freshness``,
    ``services.weather_context_builder``), and no prompt in this repo mentions an
    hourly window.  It was 30% of the composition prompt across both copies, paid
    per call, read by nobody who was reading a prompt.
    """

    context = state.weather_context
    if context is None:
        return ""
    destination_names = {item.destination_id: item.display_name for item in state.destination_geo}
    days: List[Dict[str, Any]] = []
    for item in context.days:
        days.append(
            {
                "destination_id": item.destination_id,
                "destination": destination_names.get(item.destination_id, item.destination_id),
                "date": item.date.isoformat(),
                "timezone": item.timezone,
                "data_kind": item.data_kind,
                "condition_code": item.condition_code,
                "condition_label": item.condition_label,
                "high_c": item.high_c,
                "low_c": item.low_c,
                "apparent_high_c": item.apparent_high_c,
                "precipitation_probability_pct": item.precipitation_probability_pct,
                "precipitation_mm": item.precipitation_mm,
                "wind_speed_kph": item.wind_speed_kph,
                "wind_gust_kph": item.wind_gust_kph,
                "fact_assertion_ids": item.fact_assertion_ids,
            }
        )
    payload = {
        "weather_data_revision": context.weather_data_revision,
        # The trip window used to reach the composition prompt only inside the
        # second copy, so it is stated here now rather than lost with it.
        "trip_start_date": context.trip_start_date.isoformat(),
        "trip_end_date": context.trip_end_date.isoformat(),
        "days": days,
    }
    return (
        "【规划前天气事实】\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n规则：forecast 可用于对应日期；seasonal_baseline 只能调整整体节奏、室内外平衡、"
        "穿衣与临行复查提醒，不得写成当天条件或触发逐日改动；data_kind=unavailable 时不得"
        "根据季节常识补写当天条件。"
    )
