"""Deterministic post-delivery weather adjustment proposals.

Weather refresh may change facts and impacts, but it never mutates the
canonical itinerary.  This module turns newly material, sourced impacts into
typed operations that can later be applied by one Workspace mutation.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import timedelta
from typing import Optional

from ..entities.delivery_bundle import (
    SELECTION_SLOT_ENTITY_TYPES,
    DeliveryBundle,
    DiningCandidate,
    EntityRef,
    EntityType,
    LodgingCandidate,
    TransportMode,
    VisitCandidate,
    WeatherAdjustmentOperation,
    WeatherAdjustmentProposal,
    WeatherBufferOperation,
    WeatherContextSnapshot,
    WeatherImpact,
    WeatherRescheduleOperation,
    WeatherSelectionOperation,
    WeatherTransportModeOperation,
    WeatherVisitReplacementOperation,
)
from .weather_impact_engine import WeatherImpactEngine, day_weather_fit


_CONDITION_LABELS = {
    "rain": "降雨",
    "heat": "高温",
    "cold": "低温",
    "wind": "强风",
    "thunderstorm": "雷暴",
    "snow": "降雪",
    "visibility": "能见度",
}


def _material_change(previous: dict[str, WeatherImpact], impact: WeatherImpact) -> bool:
    if impact.data_kind != "forecast" or impact.severity not in {"medium", "high"}:
        return False
    if impact.action not in {
        "move_time",
        "replace",
        "change_transport",
        "add_buffer",
    }:
        return False
    old = previous.get(impact.weather_impact_id)
    return old is None or (old.severity, old.action) != (impact.severity, impact.action)


def _entity_id(impact: WeatherImpact) -> Optional[str]:
    target = impact.target_ref
    return getattr(target, "entity_id", None) or getattr(target, "transport_leg_id", None)


def _day_for_target(bundle: DeliveryBundle, target_id: str, impact: WeatherImpact):
    return next(
        (
            day
            for day in bundle.workspace.itinerary.day_plans
            if day.date == impact.date
            and any(item.entity_id == target_id for item in day.timeline)
        ),
        None,
    )


def _reschedule_operation(
    bundle: DeliveryBundle,
    weather: WeatherContextSnapshot,
    impact: WeatherImpact,
    target_id: str,
) -> Optional[WeatherRescheduleOperation]:
    itinerary = bundle.workspace.itinerary
    entity = next(
        (
            item
            for item in [*itinerary.visit_stops, *itinerary.dining_stops]
            if item.item_id == target_id
        ),
        None,
    )
    if entity is None or entity.planned_start is None:
        return None
    day = next((item for item in weather.days if item.date == impact.date), None)
    if day is None or not day.hourly_windows:
        return None
    windows = [
        item
        for item in day.hourly_windows
        if item.start_at.date() == impact.date and 6 <= item.start_at.hour <= 20
    ]
    if impact.condition_type == "heat":
        windows = [item for item in windows if item.apparent_temperature_c is not None]
        chosen = min(windows, key=lambda item: (item.apparent_temperature_c, item.start_at), default=None)
    elif impact.condition_type == "cold":
        windows = [item for item in windows if item.apparent_temperature_c is not None]
        chosen = max(windows, key=lambda item: (item.apparent_temperature_c, -item.start_at.timestamp()), default=None)
    else:
        chosen = None
    if chosen is None or chosen.start_at == entity.planned_start:
        return None
    duration = entity.duration_minutes or 60
    return WeatherRescheduleOperation(
        item_id=entity.item_id,
        expected_planned_start=entity.planned_start,
        expected_planned_end=entity.planned_end,
        planned_start=chosen.start_at,
        planned_end=chosen.start_at + timedelta(minutes=duration),
    )


def _candidate_day_fit(candidate, day, entity_type: EntityType, entity_id: str) -> float:
    """Score how well a replacement suits the affected day, 0 (poor) to 1 (clear)."""
    return day_weather_fit(
        WeatherImpactEngine().evaluate(
            weather_day=day,
            target_ref=EntityRef(entity_type=entity_type, entity_id=entity_id),
            sensitivity=candidate.weather_sensitivity,
        )
    )


def _replacement_operation(
    bundle: DeliveryBundle,
    weather: WeatherContextSnapshot,
    impact: WeatherImpact,
    target_id: str,
) -> Optional[WeatherAdjustmentOperation]:
    workspace = bundle.workspace
    candidates = workspace.recommendation_catalog.candidate_index()
    admissions = workspace.recommendation_catalog.admission_index()
    day = next((item for item in weather.days if item.date == impact.date), None)
    if day is None:
        return None
    slot = next(
        (item for item in workspace.selection_slots if item.target_entity_id == target_id),
        None,
    )
    # The impact scores the day for what is currently planned; only an option
    # that suits the day better is worth proposing.
    current_fit = day_weather_fit([impact])
    # Visit and transport targets now have slots too, and neither is answered
    # here.  A visit is answered below from the whole admitted pool, because the
    # slot only carries the three curated options a reader is offered and weather
    # wants the best stand-in there is.  A transport leg is answered by
    # ``_transport_operation``, which changes the *mode* rather than picking
    # another route — going through the slot here would propose a route swap
    # where the weather problem is the mode.
    if slot is not None and slot.slot_type not in {"visit", "transport"}:
        expected_type = SELECTION_SLOT_ENTITY_TYPES[slot.slot_type]
        ranked: list[tuple[float, int, str]] = []
        for option in slot.options:
            if option.option_id == slot.selected_option_id:
                continue
            candidate = candidates.get(option.candidate_id)
            admission = admissions.get((option.candidate_id, slot.selection_slot_id))
            if (
                not isinstance(candidate, (DiningCandidate, LodgingCandidate))
                or admission is None
                or admission.status != "passed"
            ):
                continue
            fit = _candidate_day_fit(candidate, day, expected_type, target_id)
            if fit > current_fit:
                ranked.append((-fit, option.rank, option.option_id))
        if ranked:
            return WeatherSelectionOperation(
                selection_slot_id=slot.selection_slot_id,
                expected_option_id=slot.selected_option_id,
                option_id=min(ranked)[2],
            )

    visit = next((item for item in workspace.itinerary.visit_stops if item.item_id == target_id), None)
    if visit is None or visit.lineage.lineage_kind != "candidate_entity":
        return None
    alternatives = sorted(
        (
            (
                -_candidate_day_fit(item, day, EntityType.VISIT_STOP, target_id),
                item.candidate_id,
            )
            for item in candidates.values()
            if isinstance(item, VisitCandidate)
            and item.candidate_id != visit.lineage.candidate_id
            and admissions.get((item.candidate_id, None)) is not None
            and admissions[(item.candidate_id, None)].status == "passed"
        )
    )
    if not alternatives or -alternatives[0][0] <= current_fit:
        return None
    return WeatherVisitReplacementOperation(
        item_id=target_id,
        expected_candidate_id=visit.lineage.candidate_id,
        candidate_id=alternatives[0][1],
    )


def _transport_operation(
    bundle: DeliveryBundle,
    target_id: str,
) -> Optional[WeatherTransportModeOperation]:
    leg = next(
        (item for item in bundle.workspace.itinerary.transport_legs if item.transport_leg_id == target_id),
        None,
    )
    if leg is None or leg.transport_class == "long_distance":
        return None
    for mode in (
        TransportMode.RIDE_HAILING,
        TransportMode.TAXI,
        TransportMode.METRO,
        TransportMode.BUS,
    ):
        if mode != leg.selected_mode and mode not in leg.mode_preference.excluded_modes:
            return WeatherTransportModeOperation(
                transport_leg_id=leg.transport_leg_id,
                expected_mode=leg.selected_mode,
                selected_mode=mode,
            )
    return None


def _buffer_operation(
    bundle: DeliveryBundle,
    impact: WeatherImpact,
    target_id: str,
) -> Optional[WeatherBufferOperation]:
    day = _day_for_target(bundle, target_id, impact)
    if day is None:
        return None
    digest = hashlib.sha256(
        f"{impact.date}|{target_id}|{impact.condition_type}|buffer".encode()
    ).hexdigest()[:16]
    return WeatherBufferOperation(
        target_entity_id=target_id,
        day_id=day.day_id,
        block_id=f"weather_buffer_{digest}",
        duration_minutes=30,
    )


def _operation_for(
    bundle: DeliveryBundle,
    weather: WeatherContextSnapshot,
    impact: WeatherImpact,
) -> Optional[WeatherAdjustmentOperation]:
    target_id = _entity_id(impact)
    if target_id is None:
        return None
    if impact.action == "move_time":
        return _reschedule_operation(bundle, weather, impact, target_id)
    if impact.action == "replace":
        return _replacement_operation(bundle, weather, impact, target_id)
    if impact.action == "change_transport":
        return _transport_operation(bundle, target_id)
    if impact.action == "add_buffer":
        return _buffer_operation(bundle, impact, target_id)
    return None


def build_weather_adjustment_proposals(
    bundle: DeliveryBundle,
    weather: WeatherContextSnapshot,
    *,
    carried_over_impact_ids: frozenset[str] = frozenset(),
    previous_impacts: Optional[dict[str, WeatherImpact]] = None,
) -> list[WeatherAdjustmentProposal]:
    """Build one concrete proposal per affected day without mutating Workspace.

    ``carried_over_impact_ids`` names impacts that are present in ``weather`` only
    because a frozen record still points at them — this refresh did not re-evaluate
    them, and the condition they describe may well have cleared.  They must read as
    *absent* here, or the surviving-proposal pass below would rebase a suggestion
    for a storm that is no longer forecast onto the new weather revision, and tell
    the traveller to change their plans for weather nobody expects any more.  The
    set is passed between functions for the length of one refresh call and stored
    nowhere: it is re-derived from set arithmetic on every refresh.

    ``previous_impacts`` overrides the baseline against which ``_material_change``
    measures whether an impact is new.      The refresh path leaves it ``None`` and
    derives the baseline from ``bundle.weather_snapshot.impacts`` (proposals are a
    *delta* over what was already delivered).  The delivery path passes ``{}`` to
    express there is no prior baseline — delivery is the first projection, so every
    qualifying forecast impact is material and becomes a proposal.
    """

    previous = (
        {item.weather_impact_id: item for item in bundle.weather_snapshot.impacts}
        if previous_impacts is None
        else previous_impacts
    )
    grouped: dict[object, list[tuple[WeatherImpact, WeatherAdjustmentOperation]]] = defaultdict(list)
    for impact in weather.impacts:
        if not _material_change(previous, impact):
            continue
        operation = _operation_for(bundle, weather, impact)
        if operation is not None:
            grouped[impact.date].append((impact, operation))

    proposals: list[WeatherAdjustmentProposal] = []
    handled = {item.proposal_id for item in bundle.workspace.weather_proposal_decisions}
    for proposal_date, items in sorted(grouped.items()):
        unique: dict[str, tuple[WeatherImpact, WeatherAdjustmentOperation]] = {}
        for impact, operation in items:
            unique.setdefault(operation.model_dump_json(), (impact, operation))
        impacts = [item[0] for item in unique.values()]
        operations = [item[1] for item in unique.values()]
        signature = "|".join(sorted(item.model_dump_json() for item in operations))
        proposal_id = "weather_proposal_" + hashlib.sha256(
            f"{proposal_date}|{signature}".encode()
        ).hexdigest()[:20]
        condition_labels = list(
            dict.fromkeys(_CONDITION_LABELS[item.condition_type] for item in impacts)
        )
        summary = f"{'、'.join(condition_labels)}变化，建议调整 {len(operations)} 项安排"
        fact_ids = list(
            dict.fromkeys(fact_id for item in impacts for fact_id in item.fact_assertion_ids)
        )
        time_delta = sum(
            item.duration_minutes
            for item in operations
            if isinstance(item, WeatherBufferOperation)
        )
        if proposal_id in handled:
            continue
        proposals.append(
            WeatherAdjustmentProposal(
                proposal_id=proposal_id,
                date=proposal_date,
                base_workspace_revision=bundle.workspace.workspace_revision,
                base_weather_data_revision=weather.weather_data_revision,
                severity=("high" if any(item.severity == "high" for item in impacts) else "medium"),
                summary=summary,
                weather_impact_ids=[item.weather_impact_id for item in impacts],
                fact_assertion_ids=fact_ids,
                operations=operations,
                time_delta_minutes=time_delta or None,
            )
        )
    proposal_index = {item.proposal_id: item for item in proposals}
    current_impacts = {
        item.weather_impact_id: item
        for item in weather.impacts
        if item.weather_impact_id not in carried_over_impact_ids
    }
    for existing in bundle.weather_snapshot.adjustment_proposals:
        if existing.proposal_id in handled or existing.proposal_id in proposal_index:
            continue
        impacts = [current_impacts.get(item) for item in existing.weather_impact_ids]
        if any(
            item is None
            or item.data_kind != "forecast"
            or item.severity not in {"medium", "high"}
            for item in impacts
        ):
            continue
        concrete_impacts = [item for item in impacts if item is not None]
        proposal_index[existing.proposal_id] = existing.model_copy(
            update={
                "base_weather_data_revision": weather.weather_data_revision,
                "severity": (
                    "high"
                    if any(item.severity == "high" for item in concrete_impacts)
                    else "medium"
                ),
                "fact_assertion_ids": list(
                    dict.fromkeys(
                        fact_id
                        for item in concrete_impacts
                        for fact_id in item.fact_assertion_ids
                    )
                ),
            }
        )
    return sorted(proposal_index.values(), key=lambda item: (item.date, item.proposal_id))
