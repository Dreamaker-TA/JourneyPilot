"""Weather and fact currentness helpers for post-delivery re-admission.

Pure functions only.  Used exclusively by ``candidate_readmission``
(delivery-time paths).  Not part of the Deep Research Candidate Gate graph.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..entities.delivery_bundle import (
    EntityRef,
    EntityType,
    FactAssertion,
    ResearchCandidate,
    SourceRecord,
    TransportCandidate,
    TransportLegRef,
    WeatherContextSnapshot,
    WeatherDayContext,
)
from .candidate_admission import is_dynamic_fact_field


def _local_timestamp(
    timestamp: datetime | None,
    day: WeatherDayContext,
) -> datetime | None:
    if timestamp is None or timestamp.utcoffset() is None:
        return None
    if not day.timezone:
        return timestamp
    try:
        return timestamp.astimezone(ZoneInfo(day.timezone))
    except ZoneInfoNotFoundError:
        # The original gate treats an unknown timezone conservatively as the
        # provider timestamp, instead of fabricating a local service date.
        return timestamp


def _transport_applies_to_weather_day(
    candidate: TransportCandidate,
    day: WeatherDayContext,
) -> bool:
    if candidate.transport_class == "long_distance":
        anchor = _local_timestamp(candidate.arrival_at or candidate.departure_at, day)
        return anchor is not None and anchor.date() == day.date

    start = _local_timestamp(candidate.departure_at, day)
    end = _local_timestamp(candidate.arrival_at, day)
    if start is None and end is None:
        return False
    start = start or end
    end = end or start
    return start.date() <= day.date <= end.date()


def _scheduled_transport_weather_day(
    candidate: TransportCandidate,
    day: WeatherDayContext,
) -> WeatherDayContext:
    """Apply hourly data only to the route interval relevant to the service."""
    if day.data_kind != "forecast" or not day.hourly_windows:
        return day

    if candidate.transport_class == "long_distance":
        anchor = _local_timestamp(candidate.arrival_at or candidate.departure_at, day)
        relevant = [
            window
            for window in day.hourly_windows
            if anchor is not None and window.start_at <= anchor < window.end_at
        ]
    else:
        start = _local_timestamp(candidate.departure_at, day)
        end = _local_timestamp(candidate.arrival_at, day)
        relevant = [
            window
            for window in day.hourly_windows
            if start is not None
            and end is not None
            and window.start_at < end
            and window.end_at > start
        ]
    if not relevant:
        return day

    probabilities = [
        item.precipitation_probability_pct
        for item in relevant
        if item.precipitation_probability_pct is not None
    ]
    apparent_temperatures = [
        item.apparent_temperature_c
        for item in relevant
        if item.apparent_temperature_c is not None
    ]
    wind_speeds = [
        item.wind_speed_kph
        for item in relevant
        if item.wind_speed_kph is not None
    ]
    return day.model_copy(
        update={
            "condition_code": None,
            "condition_label": None,
            "high_c": None,
            "low_c": None,
            "apparent_high_c": max(apparent_temperatures)
            if apparent_temperatures
            else None,
            "precipitation_probability_pct": max(probabilities)
            if probabilities
            else None,
            "precipitation_mm": None,
            "wind_speed_kph": max(wind_speeds) if wind_speeds else None,
            "wind_gust_kph": None,
            "hourly_windows": relevant,
        }
    )


def _weather_days(
    weather_snapshot: WeatherContextSnapshot,
    candidate: ResearchCandidate,
) -> list[WeatherDayContext]:
    destination_days = sorted(
        (
            day
            for day in weather_snapshot.days
            if day.destination_id == candidate.destination_id
        ),
        key=lambda item: item.date,
    )
    if not isinstance(candidate, TransportCandidate):
        return destination_days

    scheduled_days = [
        day
        for day in destination_days
        if _transport_applies_to_weather_day(candidate, day)
    ]
    if candidate.departure_at is not None or candidate.arrival_at is not None:
        return [
            _scheduled_transport_weather_day(candidate, day)
            for day in scheduled_days
        ]
    if candidate.transport_class == "long_distance":
        return [
            day
            for day in destination_days
            if day.date == weather_snapshot.trip_start_date
        ]
    return destination_days


def _target_ref(candidate: ResearchCandidate) -> EntityRef | TransportLegRef:
    if isinstance(candidate, TransportCandidate):
        return TransportLegRef(transport_leg_id=candidate.candidate_id)
    return EntityRef(
        entity_type={
            "visit": EntityType.VISIT_STOP,
            "dining": EntityType.DINING_STOP,
            "lodging": EntityType.LODGING_STAY,
        }[candidate.candidate_kind],
        entity_id=candidate.candidate_id,
    )


def _freshness_status(facts: Sequence[FactAssertion]) -> str:
    statuses = {item.status for item in facts}
    if statuses == {"verified"}:
        return "current"
    if "refreshing" in statuses:
        return "refreshing"
    return "stale"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _fact_is_current_as_of(
    fact: FactAssertion,
    *,
    sources: Mapping[str, SourceRecord],
    as_of: datetime,
) -> bool:
    """Check current evidence validity at the explicit user-refresh instant."""

    if fact.status != "verified":
        return False
    now = _aware(as_of)
    if fact.effective_from is not None and _aware(fact.effective_from) > now:
        return False
    if any(
        boundary is not None and _aware(boundary) <= now
        for boundary in (fact.effective_to, fact.expires_at)
    ):
        return False
    supporting_sources = [
        sources.get(link.source_record_id)
        for link in fact.source_links
        if link.relation == "supports"
    ]
    dynamic = is_dynamic_fact_field(fact.field_path)
    fact_has_explicit_expiry = fact.expires_at is not None
    return any(
        source is not None
        and source.lifecycle_status == "active"
        and (source.effective_from is None or _aware(source.effective_from) <= now)
        and all(
            boundary is None or _aware(boundary) > now
            for boundary in (source.effective_to, source.provider_valid_until)
        )
        and (
            source.cache_provenance is None
            or _aware(source.cache_provenance.cache_valid_until) > now
        )
        and (
            not dynamic
            or fact_has_explicit_expiry
            or source.provider_valid_until is not None
        )
        for source in supporting_sources
    )

