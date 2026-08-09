"""Deterministic date-capability policy for external tools.

The policy runs before network execution.  An out-of-horizon request is a
capability decision, not a provider failure, and must never consume retries or
fall through to web search.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict


EXACT_WEATHER_FORECAST_DAYS = 16
RAIL_LIVE_INVENTORY_DAYS = 15
AMAP_WEATHER_FORECAST_DAYS = 4
BAIDU_WEATHER_FORECAST_DAYS = 7


class TemporalQuerySemantics(str, Enum):
    LIVE_INVENTORY = "live_inventory"
    EXACT_FORECAST = "exact_forecast"
    CURRENT_ROUTE = "current_route"
    HISTORICAL_FACT = "historical_fact"
    SEASONAL_BASELINE = "seasonal_baseline"
    PUBLICATION_SEARCH = "publication_search"
    LATEST_ONLY = "latest_only"


class TemporalReferencePolicy(str, Enum):
    NONE = "none"
    QUERY_LATEST_SUPPORTED_DAY = "query_latest_supported_day"
    HISTORICAL_SAME_MONTH_BASELINE = "historical_same_month_baseline"


class TemporalPreflightStatus(str, Enum):
    EXECUTABLE = "executable"
    REFERENCE_ONLY = "reference_only"
    NOT_APPLICABLE = "not_applicable"


class TemporalReasonCode(str, Enum):
    DATE_IN_PAST = "date_in_past"
    OUTSIDE_LIVE_INVENTORY_HORIZON = "outside_live_inventory_horizon"
    OUTSIDE_EXACT_FORECAST_HORIZON = "outside_exact_forecast_horizon"
    HISTORICAL_DATE_NOT_SUPPORTED = "historical_date_not_supported"
    LATEST_DATE_NOT_SUPPORTED = "latest_date_not_supported"


class TemporalToolCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str
    tool_name: str
    query_semantics: TemporalQuerySemantics
    clock_timezone: str
    min_offset_days: Optional[int] = None
    max_offset_days: Optional[int] = None
    provider_defined_horizon: bool = False
    reference_policy: TemporalReferencePolicy = TemporalReferencePolicy.NONE
    capability_source: str
    verified_at: date


class TemporalPreflightDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: TemporalPreflightStatus
    reason_code: Optional[TemporalReasonCode] = None
    requested_start: Optional[date] = None
    requested_end: Optional[date] = None
    supported_start: Optional[date] = None
    supported_end: Optional[date] = None
    reference_date: Optional[date] = None
    reference_kind: Optional[str] = None
    user_message: str = ""

    @property
    def executable(self) -> bool:
        return self.status == TemporalPreflightStatus.EXECUTABLE


_CAPABILITIES: dict[tuple[str, str], TemporalToolCapability] = {
    ("12306-train", "get-tickets"): TemporalToolCapability(
        provider_name="12306",
        tool_name="get-tickets",
        query_semantics=TemporalQuerySemantics.LIVE_INVENTORY,
        clock_timezone="Asia/Shanghai",
        min_offset_days=0,
        max_offset_days=RAIL_LIVE_INVENTORY_DAYS - 1,
        reference_policy=TemporalReferencePolicy.QUERY_LATEST_SUPPORTED_DAY,
        capability_source="12306 normal remaining-ticket query window",
        verified_at=date(2026, 7, 27),
    ),
    ("12306-train", "get-interline-tickets"): TemporalToolCapability(
        provider_name="12306",
        tool_name="get-interline-tickets",
        query_semantics=TemporalQuerySemantics.LIVE_INVENTORY,
        clock_timezone="Asia/Shanghai",
        min_offset_days=0,
        max_offset_days=RAIL_LIVE_INVENTORY_DAYS - 1,
        reference_policy=TemporalReferencePolicy.QUERY_LATEST_SUPPORTED_DAY,
        capability_source="12306 normal remaining-ticket query window",
        verified_at=date(2026, 7, 27),
    ),
    ("duffel-flights", "search_flights"): TemporalToolCapability(
        provider_name="duffel",
        tool_name="search_flights",
        query_semantics=TemporalQuerySemantics.LIVE_INVENTORY,
        clock_timezone="UTC",
        min_offset_days=0,
        provider_defined_horizon=True,
        capability_source="Duffel offer request; maximum supplier horizon is not fixed",
        verified_at=date(2026, 7, 27),
    ),
    ("amap-maps", "maps_weather"): TemporalToolCapability(
        provider_name="amap",
        tool_name="maps_weather",
        query_semantics=TemporalQuerySemantics.EXACT_FORECAST,
        clock_timezone="Asia/Shanghai",
        min_offset_days=0,
        max_offset_days=AMAP_WEATHER_FORECAST_DAYS - 1,
        capability_source="Amap weather today plus next three days",
        verified_at=date(2026, 7, 27),
    ),
    ("currency-exchange-mcp", "convert_currency"): TemporalToolCapability(
        provider_name="frankfurter",
        tool_name="convert_currency",
        query_semantics=TemporalQuerySemantics.LATEST_ONLY,
        clock_timezone="UTC",
        capability_source="JourneyPilot Frankfurter MCP exposes latest only",
        verified_at=date(2026, 7, 27),
    ),
    ("currency-exchange-mcp", "latest_exchange_rates"): TemporalToolCapability(
        provider_name="frankfurter",
        tool_name="latest_exchange_rates",
        query_semantics=TemporalQuerySemantics.LATEST_ONLY,
        clock_timezone="UTC",
        capability_source="JourneyPilot Frankfurter MCP exposes latest only",
        verified_at=date(2026, 7, 27),
    ),
}


_PINNED_MCP_SCHEMA_REQUIREMENTS: dict[str, dict[str, frozenset[str]]] = {
    "12306-train": {
        "get-tickets": frozenset({"date", "fromStation", "toStation"}),
        "get-interline-tickets": frozenset(
            {"date", "fromStation", "toStation"}
        ),
    },
    "open-meteo": {
        "get_weather": frozenset({"location"}),
        "get_historical_weather": frozenset(
            {"location", "start_date", "end_date"}
        ),
    },
}


def temporal_schema_contract_errors(
    *,
    server_name: str,
    tool_definitions: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return deterministic startup errors for pinned date-sensitive MCPs."""

    requirements = _PINNED_MCP_SCHEMA_REQUIREMENTS.get(server_name)
    if requirements is None:
        return []
    definitions = {
        str(item.get("name") or ""): item
        for item in tool_definitions
        if str(item.get("name") or "")
    }
    errors: list[str] = []
    for tool_name, required_properties in requirements.items():
        definition = definitions.get(tool_name)
        if definition is None:
            errors.append(f"missing required tool {tool_name}")
            continue
        schema = definition.get("parameters_schema")
        properties = (
            schema.get("properties")
            if isinstance(schema, Mapping)
            else None
        )
        available = set(properties) if isinstance(properties, Mapping) else set()
        missing = sorted(required_properties - available)
        if missing:
            errors.append(
                f"tool {tool_name} missing schema properties: {', '.join(missing)}"
            )
    return errors


def get_temporal_capability(
    *, tool_name: str, server_name: Optional[str]
) -> Optional[TemporalToolCapability]:
    return _CAPABILITIES.get((str(server_name or ""), tool_name))


def _date_value(arguments: Mapping[str, Any], tool_name: str) -> Optional[date]:
    params = arguments.get("params")
    nested = params if isinstance(params, Mapping) else {}
    keys = (
        ("departure_date", "date")
        if tool_name == "search_flights"
        else ("date", "requested_date", "target_date", "service_date", "start_date")
    )
    for key in keys:
        raw = nested.get(key) if key in nested else arguments.get(key)
        if isinstance(raw, date) and not isinstance(raw, datetime):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                return date.fromisoformat(raw.strip())
            except ValueError:
                return None
    return None


def _local_today(
    capability: TemporalToolCapability, now: Optional[datetime]
) -> date:
    clock = now or datetime.now(tz=ZoneInfo(capability.clock_timezone))
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ZoneInfo(capability.clock_timezone))
    return clock.astimezone(ZoneInfo(capability.clock_timezone)).date()


def evaluate_temporal_request(
    *,
    tool_name: str,
    server_name: Optional[str],
    arguments: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> TemporalPreflightDecision:
    capability = get_temporal_capability(
        tool_name=tool_name,
        server_name=server_name,
    )
    if capability is None:
        return TemporalPreflightDecision(status=TemporalPreflightStatus.EXECUTABLE)

    requested = _date_value(arguments, tool_name)
    if requested is None:
        # Several current-condition tools do not accept a target date at all.
        # Their returned dates remain provider-owned; there is no request date
        # to reject at this boundary.
        return TemporalPreflightDecision(status=TemporalPreflightStatus.EXECUTABLE)

    today = _local_today(capability, now)
    if capability.query_semantics == TemporalQuerySemantics.LATEST_ONLY:
        return TemporalPreflightDecision(
            status=TemporalPreflightStatus.NOT_APPLICABLE,
            reason_code=TemporalReasonCode.LATEST_DATE_NOT_SUPPORTED,
            requested_start=requested,
            requested_end=requested,
            user_message=(
                f"{capability.provider_name} 当前工具只提供 Provider 标记日期的最新数据，"
                f"不能把 latest 声明为 {requested.isoformat()} 的日期事实。"
            ),
        )
    supported_start = (
        today + timedelta(days=capability.min_offset_days)
        if capability.min_offset_days is not None
        else None
    )
    supported_end = (
        today + timedelta(days=capability.max_offset_days)
        if capability.max_offset_days is not None
        else None
    )
    if supported_start is not None and requested < supported_start:
        return TemporalPreflightDecision(
            status=TemporalPreflightStatus.NOT_APPLICABLE,
            reason_code=(
                TemporalReasonCode.HISTORICAL_DATE_NOT_SUPPORTED
                if capability.query_semantics == TemporalQuerySemantics.LATEST_ONLY
                else TemporalReasonCode.DATE_IN_PAST
            ),
            requested_start=requested,
            requested_end=requested,
            supported_start=supported_start,
            supported_end=supported_end,
            user_message=(
                f"{capability.provider_name} 不支持查询 {requested.isoformat()}："
                f"该日期早于当前可查询起点 {supported_start.isoformat()}。"
            ),
        )
    if supported_end is not None and requested > supported_end:
        if (
            capability.reference_policy
            == TemporalReferencePolicy.QUERY_LATEST_SUPPORTED_DAY
        ):
            return TemporalPreflightDecision(
                status=TemporalPreflightStatus.REFERENCE_ONLY,
                reason_code=TemporalReasonCode.OUTSIDE_LIVE_INVENTORY_HORIZON,
                requested_start=requested,
                requested_end=requested,
                supported_start=supported_start,
                supported_end=supported_end,
                reference_date=supported_end,
                reference_kind="latest_supported_day",
                user_message=(
                    f"12306 当前常规余票最远可查询到 {supported_end.isoformat()}。"
                    f"你的出发日期 {requested.isoformat()} 尚未进入查询窗口。"
                    f"以下如提供 {supported_end.isoformat()} 的班次结构，只能作为参考，"
                    "不代表目标日期的车次、时刻、票价或余票；请在进入售票窗口后重新查询。"
                ),
            )
        return TemporalPreflightDecision(
            status=TemporalPreflightStatus.NOT_APPLICABLE,
            reason_code=TemporalReasonCode.OUTSIDE_EXACT_FORECAST_HORIZON,
            requested_start=requested,
            requested_end=requested,
            supported_start=supported_start,
            supported_end=supported_end,
            user_message=(
                f"{capability.provider_name} 的精确数据只支持到 "
                f"{supported_end.isoformat()}，不会查询或复制到目标日期 "
                f"{requested.isoformat()}。"
            ),
        )
    return TemporalPreflightDecision(
        status=TemporalPreflightStatus.EXECUTABLE,
        requested_start=requested,
        requested_end=requested,
        supported_start=supported_start,
        supported_end=supported_end,
    )
