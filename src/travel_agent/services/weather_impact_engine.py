"""Deterministic weather impacts used by candidate and itinerary gates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..entities.delivery_bundle import (
    EntityRef,
    SelectionSlotRef,
    TransportLegRef,
    WeatherDayContext,
    WeatherImpact,
    WeatherSensitivity,
)

WeatherTargetRef = EntityRef | SelectionSlotRef | TransportLegRef


@dataclass(frozen=True)
class WeatherRiskProfile:
    heat_sensitive: bool = False
    cold_sensitive: bool = False
    mobility_limited: bool = False
    vulnerable_party: bool = False
    affected_constraint_ids: tuple[str, ...] = ()


def risk_profile_from_constraint_pack(pack: Mapping[str, object]) -> WeatherRiskProfile:
    """Reduce active hard constraints to threshold modifiers without LLM judgment."""
    heat = cold = mobility = vulnerable = False
    affected: list[str] = []
    items = pack.get("hard_constraints", []) if isinstance(pack, Mapping) else []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping) or item.get("status", "active") != "active":
            continue
        constraint_id = str(item.get("constraint_id") or "")
        text = f"{item.get('category', '')} {item.get('value', '')}".lower()
        matched = False
        if any(token in text for token in ("heat", "高温", "炎热", "中暑")):
            heat = matched = True
        if any(token in text for token in ("cold", "低温", "寒冷", "保暖")):
            cold = matched = True
        if any(token in text for token in ("mobility", "wheelchair", "无障碍", "少走", "步行", "行动不便")):
            mobility = matched = True
        if any(token in text for token in ("elderly", "child", "老人", "长者", "儿童", "婴儿", "孕")):
            vulnerable = matched = True
        if matched and constraint_id:
            affected.append(constraint_id)
    return WeatherRiskProfile(
        heat_sensitive=heat,
        cold_sensitive=cold,
        mobility_limited=mobility,
        vulnerable_party=vulnerable,
        affected_constraint_ids=tuple(dict.fromkeys(affected)),
    )


# How suitable one day is for a candidate, keyed by that day's worst impact.
DAY_FIT_BY_SEVERITY = {"high": 0.0, "medium": 0.6, "low": 0.9}


def day_weather_fit(impacts: Iterable[WeatherImpact]) -> float:
    """Score how well one day suits a candidate, 0 (poor) to 1 (clear)."""

    return min(
        (DAY_FIT_BY_SEVERITY.get(impact.severity, 1.0) for impact in impacts),
        default=1.0,
    )


class WeatherImpactEngine:
    """Turn sourced weather fields into machine-enforceable impacts."""

    def evaluate(
        self,
        *,
        weather_day: WeatherDayContext,
        target_ref: WeatherTargetRef,
        sensitivity: WeatherSensitivity,
        risk_profile: WeatherRiskProfile = WeatherRiskProfile(),
    ) -> list[WeatherImpact]:
        if weather_day.data_kind == "unavailable" or not weather_day.fact_assertion_ids:
            return []
        if weather_day.data_kind == "seasonal_baseline":
            condition = self._baseline_condition(weather_day, sensitivity, risk_profile)
            return [self._impact(weather_day, target_ref, condition, "low", "keep", risk_profile)] if condition else []

        impacts: list[WeatherImpact] = []
        code = weather_day.condition_code
        if code in {95, 96, 99}:
            severity = "high" if sensitivity.exposure != "indoor" else "medium"
            action = self._unsafe_action(target_ref) if severity == "high" else "rerank"
            impacts.append(self._impact(weather_day, target_ref, "thunderstorm", severity, action, risk_profile))
        elif code in {71, 73, 75, 77, 85, 86}:
            high = sensitivity.exposure == "outdoor" and sensitivity.cold_sensitivity == "high"
            impacts.append(self._impact(weather_day, target_ref, "snow", "high" if high else "medium", self._unsafe_action(target_ref) if high else "add_buffer", risk_profile))
        elif code in {45, 48} and sensitivity.requires_clear_visibility:
            impacts.append(self._impact(weather_day, target_ref, "visibility", "high", self._unsafe_action(target_ref), risk_profile))

        for condition, level, medium_action in (
            ("rain", self._rain_level(weather_day, sensitivity, risk_profile), "rerank"),
            ("heat", self._heat_level(weather_day, sensitivity, risk_profile), "move_time"),
            ("cold", self._cold_level(weather_day, sensitivity, risk_profile), "move_time"),
            ("wind", self._wind_level(weather_day, sensitivity), "add_buffer"),
        ):
            if level:
                impacts.append(self._impact(weather_day, target_ref, condition, level, self._action(level, target_ref, medium_action), risk_profile))
        return self._deduplicate(impacts)

    @staticmethod
    def _rain_level(day: WeatherDayContext, sensitivity: WeatherSensitivity, risk: WeatherRiskProfile) -> str | None:
        if sensitivity.rain_sensitivity == "none" or sensitivity.exposure == "indoor":
            return None
        probability, amount = day.precipitation_probability_pct or 0, day.precipitation_mm or 0
        if sensitivity.rain_sensitivity == "high" and (amount >= 20 or probability >= 90):
            return "high"
        if amount >= 5 or probability >= (55 if risk.mobility_limited else 65):
            return "medium"
        return "low" if amount > 0 or probability >= 30 else None

    @staticmethod
    def _heat_level(day: WeatherDayContext, sensitivity: WeatherSensitivity, risk: WeatherRiskProfile) -> str | None:
        if sensitivity.heat_sensitivity == "none" or sensitivity.exposure == "indoor":
            return None
        apparent = day.apparent_high_c if day.apparent_high_c is not None else day.high_c
        if apparent is None:
            return None
        conservative = risk.heat_sensitive or risk.vulnerable_party
        if sensitivity.heat_sensitivity == "high" and apparent >= (38 if conservative else 40):
            return "high"
        if apparent >= (32 if conservative else 35):
            return "medium"
        return "low" if apparent >= 30 else None

    @staticmethod
    def _cold_level(day: WeatherDayContext, sensitivity: WeatherSensitivity, risk: WeatherRiskProfile) -> str | None:
        if sensitivity.cold_sensitivity == "none" or sensitivity.exposure == "indoor" or day.low_c is None:
            return None
        conservative = risk.cold_sensitive or risk.vulnerable_party
        if sensitivity.cold_sensitivity == "high" and day.low_c <= (-5 if conservative else -10):
            return "high"
        if day.low_c <= (5 if conservative else 0):
            return "medium"
        return "low" if day.low_c <= 8 else None

    @staticmethod
    def _wind_level(day: WeatherDayContext, sensitivity: WeatherSensitivity) -> str | None:
        if sensitivity.wind_sensitivity == "none" or sensitivity.exposure == "indoor":
            return None
        speed, gust = day.wind_speed_kph or 0, day.wind_gust_kph or 0
        if sensitivity.wind_sensitivity == "high" and (speed >= 45 or gust >= 60):
            return "high"
        if speed >= 30 or gust >= 45:
            return "medium"
        return "low" if speed >= 20 or gust >= 30 else None

    @staticmethod
    def _baseline_condition(day: WeatherDayContext, sensitivity: WeatherSensitivity, risk: WeatherRiskProfile) -> str | None:
        if sensitivity.heat_sensitivity != "none" and day.high_c is not None and day.high_c >= (32 if risk.heat_sensitive else 35):
            return "heat"
        if sensitivity.cold_sensitivity != "none" and day.low_c is not None and day.low_c <= (5 if risk.cold_sensitive else 0):
            return "cold"
        if sensitivity.rain_sensitivity != "none" and (day.precipitation_mm or 0) >= 5:
            return "rain"
        return None

    @staticmethod
    def _unsafe_action(target_ref: WeatherTargetRef) -> str:
        is_transport = isinstance(target_ref, TransportLegRef) or (
            isinstance(target_ref, EntityRef)
            and target_ref.entity_type == "transport_leg"
        )
        return "change_transport" if is_transport else "replace"

    def _action(self, severity: str, target_ref: WeatherTargetRef, medium: str) -> str:
        return self._unsafe_action(target_ref) if severity == "high" else medium if severity == "medium" else "keep"

    @staticmethod
    def _impact(day: WeatherDayContext, target_ref: WeatherTargetRef, condition: str, severity: str, action: str, risk: WeatherRiskProfile) -> WeatherImpact:
        digest = hashlib.sha256(f"{day.destination_id}|{day.date}|{target_ref.model_dump_json()}|{condition}".encode()).hexdigest()[:16]
        return WeatherImpact(
            weather_impact_id=f"weather_impact_{digest}", date=day.date, target_ref=target_ref,
            condition_type=condition, severity=severity, action=action,
            fact_assertion_ids=day.fact_assertion_ids,
            affected_constraint_ids=list(risk.affected_constraint_ids),
            data_kind=day.data_kind, trigger_code=f"{condition}_{severity}",
        )

    @staticmethod
    def _deduplicate(impacts: Iterable[WeatherImpact]) -> list[WeatherImpact]:
        result: list[WeatherImpact] = []
        seen: set[str] = set()
        for impact in impacts:
            if impact.weather_impact_id not in seen:
                result.append(impact)
                seen.add(impact.weather_impact_id)
        return result
