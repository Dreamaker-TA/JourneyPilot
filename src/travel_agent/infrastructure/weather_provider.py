"""Typed weather provider boundary for the JourneyPilot v2 planning context."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import fmean
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, model_validator

from travel_agent.entities.delivery_bundle import StrictModel, WeatherTimeWindow
from travel_agent.tools.temporal import EXACT_WEATHER_FORECAST_DAYS


class WeatherProviderError(RuntimeError):
    """Base error whose retry semantics are explicit at the provider boundary."""

    retryable = False


class WeatherProviderTransientError(WeatherProviderError):
    retryable = True


class WeatherProviderRateLimited(WeatherProviderTransientError):
    pass


class WeatherProviderInvalidResponse(WeatherProviderError):
    pass


class WeatherProviderNotApplicable(WeatherProviderError):
    """The provider is intentionally not valid for the requested date window."""


class WeatherProviderRequest(StrictModel):
    destination_id: str = Field(min_length=1)
    destination_name: str = Field(min_length=1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: Optional[str] = Field(default=None, min_length=1)
    start_date: date
    end_date: date


class ProviderWeatherDay(StrictModel):
    date: date
    condition_code: Optional[int] = None
    condition_label: Optional[str] = None
    high_c: Optional[float] = None
    low_c: Optional[float] = None
    apparent_high_c: Optional[float] = None
    precipitation_probability_pct: Optional[float] = Field(default=None, ge=0, le=100)
    precipitation_mm: Optional[float] = Field(default=None, ge=0)
    wind_speed_kph: Optional[float] = Field(default=None, ge=0)
    wind_gust_kph: Optional[float] = Field(default=None, ge=0)
    hourly_windows: List[WeatherTimeWindow] = Field(default_factory=list)

    def has_facts(self) -> bool:
        return any(
            value is not None
            for value in (
                self.condition_code,
                self.high_c,
                self.low_c,
                self.apparent_high_c,
                self.precipitation_probability_pct,
                self.precipitation_mm,
                self.wind_speed_kph,
                self.wind_gust_kph,
            )
        )


class WeatherProviderResponse(StrictModel):
    provider_name: str = Field(min_length=1)
    source_title: str = Field(min_length=1)
    canonical_url: Optional[str] = None
    timezone: str = Field(min_length=1)
    retrieved_at: datetime
    provider_valid_until: datetime
    data_kind: Literal["forecast", "seasonal_baseline"] = "forecast"
    source_effective_from: Optional[date] = None
    source_effective_to: Optional[date] = None
    days: List[ProviderWeatherDay]
    raw_snapshot: Dict[str, Any]

    @model_validator(mode="after")
    def validate_kind(self) -> "WeatherProviderResponse":
        if len({item.date for item in self.days}) != len(self.days):
            raise ValueError("weather provider response contains duplicate dates")
        if self.data_kind == "seasonal_baseline":
            if self.source_effective_from is None or self.source_effective_to is None:
                raise ValueError("seasonal baseline requires its historical effective period")
            if self.source_effective_to < self.source_effective_from:
                raise ValueError("seasonal baseline source period is reversed")
            for day in self.days:
                if (
                    day.condition_code is not None
                    or day.condition_label is not None
                    or day.apparent_high_c is not None
                    or day.precipitation_probability_pct is not None
                    or day.hourly_windows
                ):
                    raise ValueError(
                        "seasonal baseline cannot contain date-specific forecast fields"
                    )
        return self


class WeatherProvider(Protocol):
    name: str

    async def fetch_weather(self, request: WeatherProviderRequest) -> WeatherProviderResponse: ...


_WMO_LABELS = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴轻微冰雹",
    99: "雷暴伴强冰雹",
}


def wmo_condition_label(code: Optional[int]) -> Optional[str]:
    """Return no claim for unknown WMO codes instead of fabricating cloud cover."""

    return _WMO_LABELS.get(code) if code is not None else None


class OpenMeteoWeatherProvider:
    name = "open_meteo"

    def __init__(
        self,
        *,
        client: Optional[httpx.AsyncClient] = None,
        base_url: str = "https://api.open-meteo.com/v1/forecast",
        timeout_seconds: float = 10.0,
        forecast_window_days: int = EXACT_WEATHER_FORECAST_DAYS,
        forecast_ttl_hours: int = 3,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if forecast_window_days < 1:
            raise ValueError("weather forecast window must be positive")
        self._client = client
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._forecast_window_days = forecast_window_days
        self._forecast_ttl = timedelta(hours=forecast_ttl_hours)
        self._now = now or (lambda: datetime.now().astimezone())

    async def fetch_weather(self, request: WeatherProviderRequest) -> WeatherProviderResponse:
        if request.end_date < request.start_date:
            raise WeatherProviderInvalidResponse("weather request end date precedes start date")
        if request.timezone is not None:
            try:
                ZoneInfo(request.timezone)
            except ZoneInfoNotFoundError as exc:
                raise WeatherProviderInvalidResponse(f"unknown destination timezone: {request.timezone}") from exc

        now = self._now()
        local_today = (
            now.astimezone(ZoneInfo(request.timezone)).date()
            if request.timezone is not None
            else now.date()
        )
        forecast_end = local_today + timedelta(days=self._forecast_window_days - 1)
        if request.start_date > forecast_end:
            raise WeatherProviderNotApplicable(
                "forecast provider is not applicable outside its date horizon"
            )
        forecast_request = request.model_copy(
            update={"end_date": min(request.end_date, forecast_end)}
        )

        params = {
            "latitude": request.latitude,
            "longitude": request.longitude,
            "timezone": request.timezone or "auto",
            "start_date": forecast_request.start_date.isoformat(),
            "end_date": forecast_request.end_date.isoformat(),
            "daily": ",".join(
                (
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "apparent_temperature_max",
                    "precipitation_probability_max",
                    "precipitation_sum",
                    "wind_speed_10m_max",
                    "wind_gusts_10m_max",
                )
            ),
            "hourly": ",".join(
                (
                    "apparent_temperature",
                    "precipitation_probability",
                    "wind_speed_10m",
                )
            ),
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            response = await client.get(self._base_url, params=params)
        except httpx.TimeoutException as exc:
            raise WeatherProviderTransientError("weather provider timed out") from exc
        except httpx.TransportError as exc:
            raise WeatherProviderTransientError("weather provider transport failed") from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code == 429:
            raise WeatherProviderRateLimited("weather provider rate limited the request")
        if response.status_code >= 500:
            raise WeatherProviderTransientError(f"weather provider returned {response.status_code}")
        if response.status_code >= 400:
            raise WeatherProviderError(f"weather provider rejected request with {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WeatherProviderInvalidResponse("weather provider returned non-JSON data") from exc

        daily = payload.get("daily")
        if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
            raise WeatherProviderInvalidResponse("weather provider omitted daily dates")
        resolved_timezone = str(payload.get("timezone") or request.timezone or "")
        try:
            ZoneInfo(resolved_timezone)
        except ZoneInfoNotFoundError as exc:
            raise WeatherProviderInvalidResponse("weather provider omitted a valid IANA timezone") from exc
        days = self._normalize_days(
            daily,
            payload.get("hourly"),
            forecast_request,
            resolved_timezone,
        )
        if not days:
            raise WeatherProviderInvalidResponse("weather provider returned no requested dates")
        retrieved_at = self._now()
        return WeatherProviderResponse(
            provider_name=self.name,
            source_title=f"Open-Meteo forecast for {request.destination_name}",
            canonical_url="https://open-meteo.com/",
            timezone=resolved_timezone,
            retrieved_at=retrieved_at,
            provider_valid_until=retrieved_at + self._forecast_ttl,
            days=days,
            raw_snapshot=payload,
        )

    def _normalize_days(
        self,
        daily: Dict[str, Any],
        hourly: Any,
        request: WeatherProviderRequest,
        resolved_timezone: str,
    ) -> List[ProviderWeatherDay]:
        field_map = {
            "condition_code": "weather_code",
            "high_c": "temperature_2m_max",
            "low_c": "temperature_2m_min",
            "apparent_high_c": "apparent_temperature_max",
            "precipitation_probability_pct": "precipitation_probability_max",
            "precipitation_mm": "precipitation_sum",
            "wind_speed_kph": "wind_speed_10m_max",
            "wind_gust_kph": "wind_gusts_10m_max",
        }
        windows_by_date = self._normalize_hourly(hourly, resolved_timezone)
        result: List[ProviderWeatherDay] = []
        for index, raw_date in enumerate(daily["time"]):
            try:
                day_date = date.fromisoformat(str(raw_date))
            except ValueError:
                continue
            if day_date < request.start_date or day_date > request.end_date:
                continue
            values: Dict[str, Any] = {}
            for target, source in field_map.items():
                items = daily.get(source)
                value = items[index] if isinstance(items, list) and index < len(items) else None
                values[target] = value
            code = values["condition_code"]
            if code is not None:
                try:
                    values["condition_code"] = int(code)
                except (TypeError, ValueError):
                    values["condition_code"] = None
            values["condition_label"] = wmo_condition_label(values["condition_code"])
            item = ProviderWeatherDay(
                date=day_date,
                hourly_windows=windows_by_date.get(day_date, []),
                **values,
            )
            if item.has_facts():
                result.append(item)
        return result

    @staticmethod
    def _normalize_hourly(hourly: Any, timezone: str) -> Dict[date, List[WeatherTimeWindow]]:
        if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
            return {}
        zone = ZoneInfo(timezone)
        result: Dict[date, List[WeatherTimeWindow]] = {}
        for index, raw_time in enumerate(hourly["time"]):
            try:
                start = datetime.fromisoformat(str(raw_time)).replace(tzinfo=zone)
            except ValueError:
                continue
            values: Dict[str, Any] = {}
            for target, source in (
                ("apparent_temperature_c", "apparent_temperature"),
                ("precipitation_probability_pct", "precipitation_probability"),
                ("wind_speed_kph", "wind_speed_10m"),
            ):
                items = hourly.get(source)
                values[target] = items[index] if isinstance(items, list) and index < len(items) else None
            if all(value is None for value in values.values()):
                continue
            result.setdefault(start.date(), []).append(
                WeatherTimeWindow(start_at=start, end_at=start + timedelta(hours=1), **values)
            )
        return result


class OpenMeteoSeasonalBaselineProvider:
    """Historical same-month climatology for dates outside a forecast horizon.

    This provider deliberately produces no date-specific condition, probability,
    or hourly claim. Each returned target date receives the same deterministic
    same-month aggregate backed by the complete external response snapshot.
    """

    name = "open_meteo_historical_baseline"

    def __init__(
        self,
        *,
        client: Optional[httpx.AsyncClient] = None,
        base_url: str = "https://archive-api.open-meteo.com/v1/archive",
        timeout_seconds: float = 20.0,
        forecast_window_days: int = EXACT_WEATHER_FORECAST_DAYS,
        history_years: int = 10,
        baseline_ttl_days: int = 30,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if forecast_window_days < 1:
            raise ValueError("seasonal baseline forecast window must be positive")
        if history_years < 5:
            raise ValueError("seasonal baseline requires at least five complete years")
        self._client = client
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._forecast_window_days = forecast_window_days
        self._history_years = history_years
        self._baseline_ttl = timedelta(days=baseline_ttl_days)
        self._now = now or (lambda: datetime.now().astimezone())

    async def fetch_weather(self, request: WeatherProviderRequest) -> WeatherProviderResponse:
        if request.end_date < request.start_date:
            raise WeatherProviderInvalidResponse("weather request end date precedes start date")
        now = self._now()
        local_today = (
            now.astimezone(ZoneInfo(request.timezone)).date()
            if request.timezone is not None
            else now.date()
        )
        cutoff = local_today + timedelta(days=self._forecast_window_days - 1)
        target_dates = [
            request.start_date + timedelta(days=offset)
            for offset in range((request.end_date - request.start_date).days + 1)
            if request.start_date + timedelta(days=offset) > cutoff
        ]
        if not target_dates:
            raise WeatherProviderNotApplicable(
                "seasonal baseline is not applicable inside the forecast horizon"
            )

        last_complete_year = now.year - 1
        first_year = last_complete_year - self._history_years + 1
        historical_start = date(first_year, 1, 1)
        historical_end = date(last_complete_year, 12, 31)
        params = {
            "latitude": request.latitude,
            "longitude": request.longitude,
            "timezone": request.timezone or "auto",
            "start_date": historical_start.isoformat(),
            "end_date": historical_end.isoformat(),
            "daily": ",".join(
                (
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_sum",
                    "wind_speed_10m_max",
                )
            ),
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            response = await client.get(self._base_url, params=params)
        except httpx.TimeoutException as exc:
            raise WeatherProviderTransientError("seasonal baseline provider timed out") from exc
        except httpx.TransportError as exc:
            raise WeatherProviderTransientError("seasonal baseline provider transport failed") from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code == 429:
            raise WeatherProviderRateLimited("seasonal baseline provider rate limited the request")
        if response.status_code >= 500:
            raise WeatherProviderTransientError(
                f"seasonal baseline provider returned {response.status_code}"
            )
        if response.status_code >= 400:
            raise WeatherProviderError(
                f"seasonal baseline provider rejected request with {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WeatherProviderInvalidResponse(
                "seasonal baseline provider returned non-JSON data"
            ) from exc

        daily = payload.get("daily")
        if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
            raise WeatherProviderInvalidResponse(
                "seasonal baseline provider omitted historical daily dates"
            )
        resolved_timezone = str(payload.get("timezone") or request.timezone or "")
        try:
            ZoneInfo(resolved_timezone)
        except ZoneInfoNotFoundError as exc:
            raise WeatherProviderInvalidResponse(
                "seasonal baseline provider omitted a valid IANA timezone"
            ) from exc

        aggregates = self._aggregate_months(daily, {item.month for item in target_dates})
        days = [
            ProviderWeatherDay(date=target_date, **aggregates.get(target_date.month, {}))
            for target_date in target_dates
        ]
        days = [item for item in days if item.has_facts()]
        if not days:
            raise WeatherProviderInvalidResponse(
                "seasonal baseline provider returned no qualified same-month samples"
            )

        retrieved_at = self._now()
        try:
            request_url = str(response.request.url)
        except RuntimeError as exc:
            raise WeatherProviderInvalidResponse(
                "seasonal baseline provider response omitted its exact request URL"
            ) from exc
        snapshot = {
            "provider_response": payload,
            "journeypilot_baseline": {
                "method": "same_month_mean_over_complete_calendar_years",
                "request_url": request_url,
                "historical_start": historical_start.isoformat(),
                "historical_end": historical_end.isoformat(),
                "history_years": self._history_years,
                "target_dates": [item.isoformat() for item in target_dates],
                "monthly_aggregates": {str(key): value for key, value in aggregates.items()},
            },
        }
        return WeatherProviderResponse(
            provider_name=self.name,
            source_title=f"Open-Meteo historical seasonal baseline for {request.destination_name}",
            canonical_url=request_url,
            timezone=resolved_timezone,
            retrieved_at=retrieved_at,
            provider_valid_until=retrieved_at + self._baseline_ttl,
            data_kind="seasonal_baseline",
            source_effective_from=historical_start,
            source_effective_to=historical_end,
            days=days,
            raw_snapshot=snapshot,
        )

    def _aggregate_months(
        self,
        daily: Dict[str, Any],
        target_months: set[int],
    ) -> Dict[int, Dict[str, float]]:
        field_map = {
            "high_c": "temperature_2m_max",
            "low_c": "temperature_2m_min",
            "precipitation_mm": "precipitation_sum",
            "wind_speed_kph": "wind_speed_10m_max",
        }
        values: Dict[int, Dict[str, List[float]]] = {
            month: {target: [] for target in field_map} for month in target_months
        }
        for index, raw_date in enumerate(daily["time"]):
            try:
                historical_date = date.fromisoformat(str(raw_date))
            except ValueError:
                continue
            if historical_date.month not in target_months:
                continue
            for target, source in field_map.items():
                items = daily.get(source)
                value = items[index] if isinstance(items, list) and index < len(items) else None
                if isinstance(value, (int, float)):
                    values[historical_date.month][target].append(float(value))

        minimum_samples = self._history_years * 20
        result: Dict[int, Dict[str, float]] = {}
        for month, month_fields in values.items():
            aggregates = {
                target: round(fmean(samples), 1)
                for target, samples in month_fields.items()
                if len(samples) >= minimum_samples
            }
            if aggregates:
                result[month] = aggregates
        return result


def default_weather_providers() -> List[WeatherProvider]:
    """Production ordering: exact forecast first, qualified far-future baseline second."""

    return [OpenMeteoWeatherProvider(), OpenMeteoSeasonalBaselineProvider()]
