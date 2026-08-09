"""Build a planning-time Weather Context and its external fact lineage."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

from pydantic import Field

from travel_agent.entities.delivery_bundle import (
    EntityRef,
    EntityType,
    FactAssertion,
    FactSourceLink,
    FieldProvenance,
    SourceRecord,
    StrictModel,
    WeatherContextSnapshot,
    WeatherCoverage,
    WeatherDayContext,
)
from travel_agent.infrastructure.weather_provider import (
    ProviderWeatherDay,
    WeatherProvider,
    WeatherProviderError,
    WeatherProviderRequest,
    WeatherProviderResponse,
)


class WeatherContextBuildResult(StrictModel):
    weather_snapshot: WeatherContextSnapshot
    source_records: List[SourceRecord] = Field(default_factory=list)
    fact_assertions: List[FactAssertion] = Field(default_factory=list)
    field_provenance: List[FieldProvenance] = Field(default_factory=list)


class WeatherContextBuildError(ValueError):
    """Raised when provider evidence cannot form a truthful public source."""


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _dates(start: date, end: date) -> List[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


class WeatherContextBuilder:
    """Query ordered providers and materialize forecast/unavailable days.

    The builder never invents a seasonal baseline. A baseline must arrive through
    a separate qualified external provider before it can be represented as facts.
    """

    def __init__(
        self,
        providers: Sequence[WeatherProvider],
        *,
        max_retries_per_provider: int = 2,
        retry_backoff_seconds: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if not providers:
            raise ValueError("weather context builder requires at least one provider")
        if max_retries_per_provider < 0:
            raise ValueError("weather provider retries cannot be negative")
        self._providers = list(providers)
        self._max_retries = max_retries_per_provider
        self._retry_backoff = retry_backoff_seconds
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def build(
        self,
        *,
        requests: Sequence[WeatherProviderRequest],
        weather_data_revision: int,
        trip_start_date: date,
        trip_end_date: date,
    ) -> WeatherContextBuildResult:
        if trip_end_date < trip_start_date:
            raise ValueError("trip end date must not precede start date")
        if not requests:
            raise ValueError("weather context requires at least one destination request")
        destination_ids = [request.destination_id for request in requests]
        if len(destination_ids) != len(set(destination_ids)):
            raise ValueError("weather requests must contain unique destination ids")
        if any(
            request.start_date < trip_start_date or request.end_date > trip_end_date
            for request in requests
        ):
            raise ValueError("destination weather request falls outside trip range")

        days: List[WeatherDayContext] = []
        coverage: List[WeatherCoverage] = []
        sources: List[SourceRecord] = []
        assertions: List[FactAssertion] = []
        provenance: List[FieldProvenance] = []
        retrieved_times: List[datetime] = []

        for request in requests:
            responses = await self._fetch_with_fallback(request)
            expected_dates = _dates(request.start_date, request.end_date)
            provider_days: Dict[date, tuple[ProviderWeatherDay, WeatherProviderResponse, SourceRecord]] = {}
            available_dates: List[date] = []
            unavailable_dates: List[date] = []
            for response in responses:
                source = self._source_record(request, response)
                sources.append(source)
                retrieved_times.append(response.retrieved_at)
                for item in response.days:
                    if item.has_facts() and item.date not in provider_days:
                        provider_days[item.date] = (item, response, source)

            for day_date in expected_dates:
                provider_result = provider_days.get(day_date)
                if provider_result is None:
                    unavailable_dates.append(day_date)
                    days.append(
                        WeatherDayContext(
                            destination_id=request.destination_id,
                            date=day_date,
                            timezone=request.timezone,
                            latitude=request.latitude,
                            longitude=request.longitude,
                            data_kind="unavailable",
                        )
                    )
                    continue
                provider_day, response, source = provider_result
                day_assertions, day_provenance = self._fact_lineage(
                    request=request,
                    day=provider_day,
                    source=source,
                    response=response,
                )
                available_dates.append(day_date)
                assertions.extend(day_assertions)
                provenance.extend(day_provenance)
                days.append(
                    WeatherDayContext(
                        destination_id=request.destination_id,
                        date=day_date,
                        timezone=response.timezone,
                        latitude=request.latitude,
                        longitude=request.longitude,
                        data_kind=response.data_kind,
                        condition_code=provider_day.condition_code,
                        condition_label=provider_day.condition_label,
                        high_c=provider_day.high_c,
                        low_c=provider_day.low_c,
                        apparent_high_c=provider_day.apparent_high_c,
                        precipitation_probability_pct=provider_day.precipitation_probability_pct,
                        precipitation_mm=provider_day.precipitation_mm,
                        wind_speed_kph=provider_day.wind_speed_kph,
                        wind_gust_kph=provider_day.wind_gust_kph,
                        hourly_windows=provider_day.hourly_windows,
                        fact_assertion_ids=[item.fact_assertion_id for item in day_assertions],
                    )
                )
            status = (
                "complete"
                if available_dates and not unavailable_dates
                else "partial"
                if available_dates
                else "unavailable"
            )
            coverage.append(
                WeatherCoverage(
                    destination_id=request.destination_id,
                    start_date=request.start_date,
                    end_date=request.end_date,
                    status=status,
                    available_dates=available_dates,
                    unavailable_dates=unavailable_dates,
                )
            )

        return WeatherContextBuildResult(
            weather_snapshot=WeatherContextSnapshot(
                weather_data_revision=weather_data_revision,
                trip_start_date=trip_start_date,
                trip_end_date=trip_end_date,
                days=days,
                coverage=coverage,
                retrieved_at=max(retrieved_times, default=self._now()),
            ),
            source_records=sources,
            fact_assertions=assertions,
            field_provenance=provenance,
        )

    async def _fetch_with_fallback(
        self, request: WeatherProviderRequest
    ) -> List[WeatherProviderResponse]:
        expected_dates = set(_dates(request.start_date, request.end_date))
        covered_dates: set[date] = set()
        responses: List[WeatherProviderResponse] = []
        for provider in self._providers:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await provider.fetch_weather(request)
                    responses.append(response)
                    covered_dates.update(day.date for day in response.days if day.has_facts())
                    break
                except WeatherProviderError as exc:
                    if not exc.retryable or attempt >= self._max_retries:
                        break
                    await self._sleep(self._retry_backoff * (2**attempt))
            if expected_dates <= covered_dates:
                break
        return responses

    @staticmethod
    def _source_record(
        request: WeatherProviderRequest,
        response: WeatherProviderResponse,
    ) -> SourceRecord:
        content_hash = _canonical_hash(response.raw_snapshot)
        source_id = f"weather-source-{_canonical_hash([request.destination_id, response.provider_name, content_hash])[:24]}"
        public_excerpt = (
            f"{request.destination_name} {request.start_date.isoformat()}–{request.end_date.isoformat()} "
            "weather forecast"
        )
        if response.data_kind == "seasonal_baseline":
            baseline = response.raw_snapshot.get("journeypilot_baseline")
            if not isinstance(baseline, dict):
                raise WeatherContextBuildError(
                    "seasonal baseline source omitted its aggregation metadata"
                )
            aggregates = baseline.get("monthly_aggregates")
            if not isinstance(aggregates, dict) or not aggregates:
                raise WeatherContextBuildError(
                    "seasonal baseline source omitted its monthly aggregates"
                )
            method = baseline.get("method")
            historical_start = str(baseline.get("historical_start") or "").strip()
            historical_end = str(baseline.get("historical_end") or "").strip()
            request_url = str(baseline.get("request_url") or "").strip()
            if method != "same_month_mean_over_complete_calendar_years":
                raise WeatherContextBuildError(
                    "seasonal baseline source omitted its canonical aggregation method"
                )
            if not historical_start or not historical_end:
                raise WeatherContextBuildError(
                    "seasonal baseline source omitted its historical interval"
                )
            if not request_url or request_url != response.canonical_url:
                raise WeatherContextBuildError(
                    "seasonal baseline source URL does not match its exact provider request"
                )
            month_parts: list[str] = []
            for month, values in sorted(aggregates.items(), key=lambda item: int(item[0])):
                if not isinstance(values, dict):
                    raise WeatherContextBuildError(
                        "seasonal baseline aggregate has an invalid public shape"
                    )
                fact_parts = []
                for key, label, unit in (
                    ("high_c", "高温", "°C"),
                    ("low_c", "低温", "°C"),
                    ("precipitation_mm", "降水", "mm"),
                    ("wind_speed_kph", "风速", "km/h"),
                ):
                    value = values.get(key)
                    if isinstance(value, (int, float)):
                        fact_parts.append(f"{label} {value:g}{unit}")
                if fact_parts:
                    month_parts.append(f"{int(month)} 月：{'、'.join(fact_parts)}")
            if not month_parts:
                raise WeatherContextBuildError(
                    "seasonal baseline source omitted public aggregate values"
                )
            public_excerpt = (
                f"{request.destination_name}（{request.latitude:g}, {request.longitude:g}）；"
                f"历史样本 {historical_start} 至 {historical_end}；"
                f"按完整年份的同月日均值聚合；{'；'.join(month_parts)}。"
            )
        return SourceRecord(
            source_record_id=source_id,
            source_kind="external_tool",
            title=response.source_title,
            provider_name=response.provider_name,
            canonical_url=response.canonical_url,
            public_excerpt=public_excerpt,
            retrieved_at=response.retrieved_at,
            effective_from=datetime.combine(
                response.source_effective_from or request.start_date,
                datetime.min.time(),
                tzinfo=timezone.utc,
            ),
            effective_to=datetime.combine(
                response.source_effective_to or request.end_date,
                datetime.max.time(),
                tzinfo=timezone.utc,
            ),
            provider_valid_until=response.provider_valid_until,
            content_hash=content_hash,
            snapshot=response.raw_snapshot,
        )

    @staticmethod
    def _fact_lineage(
        *,
        request: WeatherProviderRequest,
        day: ProviderWeatherDay,
        source: SourceRecord,
        response: WeatherProviderResponse,
    ) -> tuple[List[FactAssertion], List[FieldProvenance]]:
        entity = EntityRef(
            entity_type=EntityType.WEATHER_DAY,
            entity_id=f"weather:{request.destination_id}:{day.date.isoformat()}",
        )
        fields: Dict[str, object] = {
            "condition_code": day.condition_code,
            "high_c": day.high_c,
            "low_c": day.low_c,
            "apparent_high_c": day.apparent_high_c,
            "precipitation_probability_pct": day.precipitation_probability_pct,
            "precipitation_mm": day.precipitation_mm,
            "wind_speed_kph": day.wind_speed_kph,
            "wind_gust_kph": day.wind_gust_kph,
            "hourly_windows": [item.model_dump(mode="json") for item in day.hourly_windows] or None,
        }
        assertions: List[FactAssertion] = []
        provenance: List[FieldProvenance] = []
        for field_path, value in fields.items():
            if value is None:
                continue
            assertion_id = f"weather-fact-{_canonical_hash([entity.entity_id, field_path, value, source.source_record_id])[:24]}"
            assertion = FactAssertion(
                fact_assertion_id=assertion_id,
                entity_ref=entity,
                field_path=field_path,
                asserted_value=value,
                unit={
                    "high_c": "celsius",
                    "low_c": "celsius",
                    "apparent_high_c": "celsius",
                    "precipitation_probability_pct": "percent",
                    "precipitation_mm": "millimeter",
                    "wind_speed_kph": "kilometer_per_hour",
                    "wind_gust_kph": "kilometer_per_hour",
                }.get(field_path),
                criticality="decision_critical",
                status="verified",
                observed_at=response.retrieved_at,
                effective_from=datetime.combine(day.date, datetime.min.time(), tzinfo=timezone.utc),
                effective_to=datetime.combine(day.date, datetime.max.time(), tzinfo=timezone.utc),
                expires_at=response.provider_valid_until,
                source_links=[
                    FactSourceLink(
                        source_record_id=source.source_record_id,
                        relation="supports",
                        source_locator=(
                            f"seasonal_baseline[month={day.date.month:02d}].{field_path}"
                            if response.data_kind == "seasonal_baseline"
                            else f"daily[{day.date.isoformat()}].{field_path}"
                        ),
                    )
                ],
            )
            assertions.append(assertion)
            provenance.append(
                FieldProvenance(
                    origin="external_fact",
                    entity_ref=entity,
                    field_path=field_path,
                    reference_ids=[assertion_id],
                )
            )
        return assertions, provenance
