from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from travel_agent.agents import utils as agent_utils
from travel_agent.infrastructure.weather_provider import (
    OpenMeteoSeasonalBaselineProvider,
    OpenMeteoWeatherProvider,
    WeatherProviderNotApplicable,
    WeatherProviderRequest,
)
from travel_agent.services.weather_context_builder import WeatherContextBuilder
from travel_agent.tools.registry import ToolRegistry
from travel_agent.tools.temporal import (
    TemporalPreflightStatus,
    evaluate_temporal_request,
)


NOW_SHANGHAI = datetime(2026, 8, 20, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
NOW_UTC = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("requested_date", "expected_status"),
    [
        ("2026-08-20", TemporalPreflightStatus.EXECUTABLE),
        ("2026-09-03", TemporalPreflightStatus.EXECUTABLE),
        ("2026-09-04", TemporalPreflightStatus.REFERENCE_ONLY),
        ("2026-08-19", TemporalPreflightStatus.NOT_APPLICABLE),
    ],
)
def test_12306_inventory_window_is_inclusive_and_uses_the_edge_as_reference(
    requested_date: str,
    expected_status: TemporalPreflightStatus,
) -> None:
    decision = evaluate_temporal_request(
        tool_name="get-tickets",
        server_name="12306-train",
        arguments={"date": requested_date},
        now=NOW_SHANGHAI,
    )

    assert decision.status == expected_status
    assert decision.supported_start == date(2026, 8, 20)
    assert decision.supported_end == date(2026, 9, 3)
    if expected_status is TemporalPreflightStatus.REFERENCE_ONLY:
        assert decision.reference_date == date(2026, 9, 3)
        assert decision.reference_kind == "latest_supported_day"
        assert "只能作为参考" in decision.user_message
        assert "不代表目标日期" in decision.user_message


@pytest.mark.asyncio
async def test_temporal_preflight_runs_before_runtime_budget_and_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    provider_calls = 0

    async def provider(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("an out-of-window request must not reach 12306")

    registry.register(
        "get-tickets",
        "12306 ticket query",
        {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "fromStation": {"type": "string"},
                "toStation": {"type": "string"},
            },
        },
        provider,
        source="mcp",
        server_name="12306-train",
    )
    monkeypatch.setattr(agent_utils, "get_tool_registry", lambda: registry)

    def runtime_guard_must_not_run(_operation: str):
        raise AssertionError("capability preflight must precede the run-time budget")

    monkeypatch.setattr(
        agent_utils,
        "remaining_model_seconds",
        runtime_guard_must_not_run,
    )
    requested = (
        datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=15)
    ).isoformat()

    envelope = await agent_utils.execute_tool(
        "get-tickets",
        {
            "date": requested,
            "fromStation": "AOH",
            "toStation": "IOQ",
        },
        allowed_tool_names={"get-tickets"},
        max_retries=3,
        allow_fallback=True,
    )

    assert provider_calls == 0
    assert envelope["status"] == "reference_only"
    assert envelope["metadata"]["retry_allowed"] is False
    assert envelope["metadata"]["fallback_allowed"] is False
    assert envelope["metadata"]["evidence_allowed"] is False


class _NoNetworkClient:
    called = False

    async def get(self, *_args, **_kwargs):
        self.called = True
        raise AssertionError("far-future dates must not reach the forecast endpoint")


def _far_future_request() -> WeatherProviderRequest:
    return WeatherProviderRequest(
        destination_id="tokyo",
        destination_name="Tokyo",
        latitude=35.6762,
        longitude=139.6503,
        timezone="Asia/Tokyo",
        start_date=date(2027, 7, 20),
        end_date=date(2027, 7, 21),
    )


def _historical_payload() -> dict[str, object]:
    dates = [
        date(year, 7, day).isoformat()
        for year in range(2016, 2026)
        for day in range(1, 21)
    ]
    size = len(dates)
    return {
        "timezone": "Asia/Tokyo",
        "daily": {
            "time": dates,
            "temperature_2m_max": [31.0] * size,
            "temperature_2m_min": [24.0] * size,
            "precipitation_sum": [4.2] * size,
            "wind_speed_10m_max": [18.0] * size,
        },
    }


@pytest.mark.asyncio
async def test_far_future_weather_skips_forecast_and_uses_historical_baseline() -> None:
    forecast_client = _NoNetworkClient()

    def historical_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_historical_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(historical_handler)
    ) as historical_client:
        request = _far_future_request()
        result = await WeatherContextBuilder(
            [
                OpenMeteoWeatherProvider(
                    client=forecast_client,  # type: ignore[arg-type]
                    now=lambda: NOW_UTC,
                ),
                OpenMeteoSeasonalBaselineProvider(
                    client=historical_client,
                    now=lambda: NOW_UTC,
                ),
            ],
            now=lambda: NOW_UTC,
        ).build(
            requests=[request],
            weather_data_revision=1,
            trip_start_date=request.start_date,
            trip_end_date=request.end_date,
        )

    assert forecast_client.called is False
    assert result.weather_snapshot.coverage[0].status == "complete"
    assert {day.data_kind for day in result.weather_snapshot.days} == {
        "seasonal_baseline"
    }
    assert all(
        day.condition_code is None
        and day.condition_label is None
        and day.precipitation_probability_pct is None
        and not day.hourly_windows
        for day in result.weather_snapshot.days
    )
    assert {source.provider_name for source in result.source_records} == {
        "open_meteo_historical_baseline"
    }


@pytest.mark.asyncio
async def test_weather_forecast_queries_the_last_supported_local_day() -> None:
    requested = date(2026, 9, 4)
    provider_calls = 0

    def forecast_handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        assert request.url.params["start_date"] == requested.isoformat()
        assert request.url.params["end_date"] == requested.isoformat()
        return httpx.Response(
            200,
            json={
                "timezone": "Asia/Tokyo",
                "daily": {
                    "time": [requested.isoformat()],
                    "weather_code": [2],
                    "temperature_2m_max": [30.0],
                    "temperature_2m_min": [23.0],
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(forecast_handler)
    ) as client:
        response = await OpenMeteoWeatherProvider(
            client=client,
            now=lambda: NOW_UTC,
        ).fetch_weather(
            _far_future_request().model_copy(
                update={"start_date": requested, "end_date": requested}
            )
        )

    assert provider_calls == 1
    assert [day.date for day in response.days] == [requested]


@pytest.mark.asyncio
async def test_forecast_provider_rejects_far_future_before_http() -> None:
    client = _NoNetworkClient()
    provider = OpenMeteoWeatherProvider(
        client=client,  # type: ignore[arg-type]
        now=lambda: NOW_UTC,
    )

    with pytest.raises(WeatherProviderNotApplicable):
        await provider.fetch_weather(_far_future_request())

    assert client.called is False
