"""Deterministic, strongly typed mutations for ``TripWorkspaceV2``."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal, Optional, Union

from pydantic import Field, model_validator

from .delivery_bundle import (
    CustomBlock,
    DiningCandidate,
    DiningStop,
    EntityLineage,
    EntityRef,
    EntityType,
    LodgingCandidate,
    LodgingStay,
    SelectionOption,
    SelectionSlot,
    StrictModel,
    StructuredItineraryV2,
    TimelineEntryRef,
    TransportEndpoint,
    TransportCandidate,
    TransportLeg,
    TransportMode,
    TransportModePreference,
    TripWorkspaceV2,
    VisitCandidate,
    VisitStop,
    WeatherBufferOperation,
    WeatherContextSnapshot,
    WeatherProposalDecision,
    WeatherRescheduleOperation,
    WeatherSelectionOperation,
    WeatherTransportModeOperation,
    WeatherVisitReplacementOperation,
    build_cost_coverage_summary,
    itinerary_price_components,
)
from .candidate_options import candidate_option_availability
from .trip_highlights import with_derived_highlights


class SelectOptionMutation(StrictModel):
    type: Literal["select_option"] = "select_option"
    selection_slot_id: str = Field(min_length=1)
    option_id: str = Field(min_length=1)


class MoveTimelineItemMutation(StrictModel):
    type: Literal["move_timeline_item"] = "move_timeline_item"
    item_id: str = Field(min_length=1)
    to_day_id: str = Field(min_length=1)
    before_entry_id: Optional[str] = Field(default=None, min_length=1)


class UpdateStopScheduleMutation(StrictModel):
    type: Literal["update_stop_schedule"] = "update_stop_schedule"
    item_id: str = Field(min_length=1)
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_change(self) -> "UpdateStopScheduleMutation":
        if not ({"planned_start", "planned_end", "duration_minutes"} & self.model_fields_set):
            raise ValueError("schedule mutation requires at least one changed field")
        return self


class CreateCustomBlockMutation(StrictModel):
    type: Literal["create_custom_block"] = "create_custom_block"
    block: CustomBlock
    before_entry_id: Optional[str] = Field(default=None, min_length=1)


class UpdateCustomBlockMutation(StrictModel):
    type: Literal["update_custom_block"] = "update_custom_block"
    item_id: str = Field(min_length=1)
    title: Optional[str] = Field(default=None, min_length=1)
    note: Optional[str] = None
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_change(self) -> "UpdateCustomBlockMutation":
        if not ({"title", "note", "planned_start", "planned_end", "duration_minutes"} & self.model_fields_set):
            raise ValueError("custom block mutation requires at least one changed field")
        return self


class DeleteCustomBlockMutation(StrictModel):
    type: Literal["delete_custom_block"] = "delete_custom_block"
    item_id: str = Field(min_length=1)


class DeleteTransportLegMutation(StrictModel):
    type: Literal["delete_transport_leg"] = "delete_transport_leg"
    transport_leg_id: str = Field(min_length=1)


class SetTransportModeMutation(StrictModel):
    type: Literal["set_transport_mode"] = "set_transport_mode"
    transport_leg_id: str = Field(min_length=1)
    selected_mode: TransportMode
    lock_mode: bool = True


class UpdateTransportModePreferenceMutation(StrictModel):
    type: Literal["update_transport_mode_preference"] = "update_transport_mode_preference"
    locked_mode: Optional[TransportMode] = None
    excluded_modes: Optional[list[TransportMode]] = None
    transport_leg_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_change(self) -> "UpdateTransportModePreferenceMutation":
        if not ({"locked_mode", "excluded_modes"} & self.model_fields_set):
            raise ValueError("transport preference mutation requires a changed field")
        return self


class DeleteLodgingStayMutation(StrictModel):
    type: Literal["delete_lodging_stay"] = "delete_lodging_stay"
    stay_id: str = Field(min_length=1)


class ApplyWeatherAdjustmentMutation(StrictModel):
    type: Literal["apply_weather_adjustment"] = "apply_weather_adjustment"
    proposal_id: str = Field(min_length=1)


class DismissWeatherAdjustmentMutation(StrictModel):
    type: Literal["dismiss_weather_adjustment"] = "dismiss_weather_adjustment"
    proposal_id: str = Field(min_length=1)


WorkspaceV2Mutation = Annotated[
    Union[
        SelectOptionMutation,
        MoveTimelineItemMutation,
        UpdateStopScheduleMutation,
        CreateCustomBlockMutation,
        UpdateCustomBlockMutation,
        DeleteCustomBlockMutation,
        DeleteTransportLegMutation,
        SetTransportModeMutation,
        UpdateTransportModePreferenceMutation,
        DeleteLodgingStayMutation,
        ApplyWeatherAdjustmentMutation,
        DismissWeatherAdjustmentMutation,
    ],
    Field(discriminator="type"),
]

SelectableEntity = Annotated[Union[DiningStop, LodgingStay, VisitStop, TransportLeg], Field(discriminator="type")]


class SelectionInversePatch(StrictModel):
    type: Literal["selection"] = "selection"
    selection_slot_id: str = Field(min_length=1)
    applied_option_id: str = Field(min_length=1)
    applied_workspace_revision: int = Field(ge=1)
    previous_selected_option_id: Optional[str] = Field(default=None, min_length=1)
    previous_slot_status: Literal["researching", "ready", "refreshing", "needs_user_decision"]
    previous_entity: SelectableEntity


class ItineraryInversePatch(StrictModel):
    type: Literal["itinerary"] = "itinerary"
    applied_workspace_revision: int = Field(ge=1)
    previous_itinerary: StructuredItineraryV2


class WorkspaceSnapshotInversePatch(StrictModel):
    """Typed semantic inverse used by undo/restore commits.

    The snapshot only represents user-owned workspace state.  Fact and weather
    snapshots remain outside this patch and are always taken from the current
    Delivery Bundle when the inverse is applied.
    """

    type: Literal["workspace_snapshot"] = "workspace_snapshot"
    applied_workspace_revision: int = Field(ge=1)
    previous_workspace: TripWorkspaceV2


WorkspaceV2InversePatch = Annotated[
    Union[SelectionInversePatch, ItineraryInversePatch, WorkspaceSnapshotInversePatch],
    Field(discriminator="type"),
]


class WorkspaceV2MutationApplication(StrictModel):
    workspace: TripWorkspaceV2
    changed: bool
    label: Optional[str] = None
    inverse: Optional[WorkspaceV2InversePatch] = None


class WorkspaceV2MutationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _slot(workspace: TripWorkspaceV2, selection_slot_id: str) -> SelectionSlot:
    for slot in workspace.selection_slots:
        if slot.selection_slot_id == selection_slot_id:
            return slot
    raise WorkspaceV2MutationError("selection_slot_not_found", "SelectionSlot does not exist")


def _option(slot: SelectionSlot, option_id: str) -> SelectionOption:
    for option in slot.options:
        if option.option_id == option_id:
            return option
    raise WorkspaceV2MutationError("selection_option_not_found", "Option does not belong to SelectionSlot")


def _lineage(candidate: DiningCandidate | LodgingCandidate, slot: SelectionSlot, option: SelectionOption) -> EntityLineage:
    return EntityLineage(
        research_packet_id=candidate.research_packet_id,
        candidate_id=candidate.candidate_id,
        selection_slot_id=slot.selection_slot_id,
        fact_assertion_ids=option.fact_assertion_ids,
        source_record_ids=option.source_record_ids,
        planning_decision_ids=candidate.planning_decision_ids,
        weather_impact_ids=candidate.weather_impact_ids,
        personalization_influence_ids=option.personalization_influence_ids,
    )


def _selection_reason(option: SelectionOption) -> str:
    return "；".join(reason.strip() for reason in option.selection_reasons if reason.strip())


def _replace_dining(workspace: TripWorkspaceV2, slot: SelectionSlot, option: SelectionOption, candidate: DiningCandidate) -> tuple[DiningStop, DiningStop]:
    current = next((item for item in workspace.itinerary.dining_stops if item.item_id == slot.target_entity_id), None)
    if current is None:
        raise WorkspaceV2MutationError("canonical_entity_not_found", "DiningStop target does not exist")
    if current.meal_type not in candidate.meal_types:
        raise WorkspaceV2MutationError("meal_context_mismatch", "Candidate does not serve the slot meal")
    return current, DiningStop(
        item_id=current.item_id, day_id=current.day_id, place_id=candidate.place_id,
        name=candidate.branch_name, address=candidate.address,
        planned_start=current.planned_start,
        planned_end=current.planned_end, duration_minutes=current.duration_minutes,
        estimated_cost_cny=candidate.average_spend_cny, selection_reason=_selection_reason(option),
        lineage=_lineage(candidate, slot, option), meal_type=current.meal_type,
        cuisine_types=candidate.cuisine_types, average_spend_cny=candidate.average_spend_cny,
        recommended_dishes=candidate.recommended_dishes,
        reservation_required=candidate.reservation_required, opening_window=candidate.opening_window,
    )


def _replace_lodging(workspace: TripWorkspaceV2, slot: SelectionSlot, option: SelectionOption, candidate: LodgingCandidate) -> tuple[LodgingStay, LodgingStay]:
    current = next((item for item in workspace.itinerary.lodging_stays if item.stay_id == slot.target_entity_id), None)
    if current is None:
        raise WorkspaceV2MutationError("canonical_entity_not_found", "LodgingStay target does not exist")
    if candidate.check_in_date != current.check_in_date or candidate.check_out_date != current.check_out_date:
        raise WorkspaceV2MutationError("stay_context_mismatch", "Candidate does not cover the complete stay")
    return current, LodgingStay(
        stay_id=current.stay_id, place_id=candidate.place_id, name=candidate.property_name,
        check_in_date=candidate.check_in_date, check_out_date=candidate.check_out_date,
        check_in_time=current.check_in_time, check_out_time=current.check_out_time,
        nights=candidate.nights, room_type=candidate.room_type,
        nightly_price_cny=candidate.nightly_price_cny, total_price_cny=candidate.total_price_cny,
        price_kind=candidate.price_kind,
        availability_status=candidate.availability_status,
        address=candidate.address, selection_reason=_selection_reason(option),
        lineage=_lineage(candidate, slot, option),
    )


def _replace_visit_stop(
    workspace: TripWorkspaceV2,
    slot: SelectionSlot,
    option: SelectionOption,
    candidate: VisitCandidate,
) -> tuple[VisitStop, VisitStop]:
    """Swap the attraction standing in one plan position.

    The stop keeps its window: a visit slot offers a different place for the same
    hole in the Day, so the schedule the composition solved for stays put and only
    the duration follows the new candidate — the same division dining and lodging
    already make.
    """

    current = next(
        (item for item in workspace.itinerary.visit_stops if item.item_id == slot.target_entity_id),
        None,
    )
    if current is None:
        raise WorkspaceV2MutationError("canonical_entity_not_found", "VisitStop target does not exist")
    planned_end = (
        current.planned_start + timedelta(minutes=candidate.recommended_duration_minutes)
        if current.planned_start is not None
        else current.planned_end
    )
    return current, VisitStop(
        item_id=current.item_id, day_id=current.day_id, place_id=candidate.place_id,
        name=candidate.name, address=candidate.address,
        planned_start=current.planned_start, planned_end=planned_end,
        duration_minutes=candidate.recommended_duration_minutes,
        estimated_cost_cny=candidate.estimated_cost_cny,
        selection_reason=_selection_reason(option),
        lineage=_lineage(candidate, slot, option), visit_type=candidate.visit_type,
        opening_window=candidate.opening_window,
        reservation_required=candidate.reservation_required,
        visit_highlights=candidate.highlights,
    )


def _replace_transport_leg(
    workspace: TripWorkspaceV2,
    slot: SelectionSlot,
    option: SelectionOption,
    candidate: TransportCandidate,
) -> tuple[TransportLeg, TransportLeg]:
    """Bind one journey in the plan to a different admitted route.

    The eligibility rules live in ``_check_transport_replacement`` rather than
    inline, so the offering side (``itinerary_composition_v2``) has one named
    thing to restate.  ``lineage.selection_slot_id`` is carried over from the leg
    the slot points at, because the workspace contract requires a slot's target
    to name it.
    """

    current = next(
        (
            item
            for item in workspace.itinerary.transport_legs
            if item.transport_leg_id == slot.target_entity_id
        ),
        None,
    )
    if current is None:
        raise WorkspaceV2MutationError("canonical_entity_not_found", "TransportLeg target does not exist")
    _check_transport_replacement(current, candidate)
    updated = _transport_from_candidate(current, candidate)
    return current, updated.model_copy(
        update={
            "lineage": updated.lineage.model_copy(
                update={"selection_slot_id": slot.selection_slot_id}
            )
        }
    )


def _recompute_derived_itinerary_fields(
    itinerary: StructuredItineraryV2,
) -> StructuredItineraryV2:
    """Refresh every field that is a pure function of the four entity lists.

    A mutation rewrites those lists, so anything summarizing them is stale the
    moment it returns.  ``cost_summary`` had to be refreshed here because
    ``StructuredItineraryV2.validate_references`` recomputes and rejects a
    mismatch; ``highlights`` cannot be enforced that way — the derivation reads
    the transport mode words from ``entities/delivery_presentation.py``, which
    imports ``delivery_bundle``, so a validator there would close an import
    cycle.  Both therefore refresh together in this one place, so the
    round-trip is nailed at the mutation boundary.
    """

    return with_derived_highlights(itinerary).model_copy(
        update={
            "cost_summary": build_cost_coverage_summary(
                itinerary_price_components(
                    visit_stops=itinerary.visit_stops,
                    dining_stops=itinerary.dining_stops,
                    lodging_stays=itinerary.lodging_stays,
                    transport_legs=itinerary.transport_legs,
                ),
                budget_cap_cny=itinerary.cost_summary.budget_cap_cny,
                llm_estimated_total_cny=itinerary.cost_summary.llm_estimated_total_cny,
            ),
        }
    )


def _validated_workspace(workspace: TripWorkspaceV2, itinerary: StructuredItineraryV2) -> TripWorkspaceV2:
    itinerary = _recompute_derived_itinerary_fields(itinerary)
    return TripWorkspaceV2.model_validate(workspace.model_copy(update={
        "workspace_revision": workspace.workspace_revision + 1,
        "itinerary": itinerary,
    }).model_dump(mode="json"))


def _itinerary_result(workspace: TripWorkspaceV2, itinerary: StructuredItineraryV2, label: str) -> WorkspaceV2MutationApplication:
    if itinerary == workspace.itinerary:
        return WorkspaceV2MutationApplication(workspace=workspace, changed=False)
    next_workspace = _validated_workspace(workspace, itinerary)
    return WorkspaceV2MutationApplication(
        workspace=next_workspace,
        changed=True,
        label=label,
        inverse=ItineraryInversePatch(
            applied_workspace_revision=next_workspace.workspace_revision,
            previous_itinerary=workspace.itinerary,
        ),
    )


def _snapshot_result(
    workspace: TripWorkspaceV2,
    following: TripWorkspaceV2,
    label: str,
) -> WorkspaceV2MutationApplication:
    if following == workspace:
        return WorkspaceV2MutationApplication(workspace=workspace, changed=False)
    following = following.model_copy(
        update={"itinerary": _recompute_derived_itinerary_fields(following.itinerary)}
    )
    next_workspace = TripWorkspaceV2.model_validate(
        following.model_copy(
            update={"workspace_revision": workspace.workspace_revision + 1}
        ).model_dump(mode="json")
    )
    return WorkspaceV2MutationApplication(
        workspace=next_workspace,
        changed=True,
        label=label,
        inverse=WorkspaceSnapshotInversePatch(
            applied_workspace_revision=next_workspace.workspace_revision,
            previous_workspace=workspace,
        ),
    )


def _timeline_location(itinerary: StructuredItineraryV2, entity_id: str) -> tuple[int, int, TimelineEntryRef]:
    matches = [
        (day_index, entry_index, ref)
        for day_index, day in enumerate(itinerary.day_plans)
        for entry_index, ref in enumerate(day.timeline)
        if ref.entity_id == entity_id
    ]
    if len(matches) != 1:
        raise WorkspaceV2MutationError("timeline_item_not_unique", "Timeline item must have exactly one projection")
    return matches[0]


def _move_scheduled_item_to_day(item, day):
    updates = {"day_id": day.day_id}
    if day.date is not None:
        for field in ("planned_start", "planned_end"):
            value = getattr(item, field, None)
            if value is not None:
                updates[field] = value.replace(
                    year=day.date.year,
                    month=day.date.month,
                    day=day.date.day,
                )
    return item.model_copy(update=updates)


def _endpoint_for_ref(itinerary: StructuredItineraryV2, ref: TimelineEntryRef) -> Optional[TransportEndpoint]:
    collections = {
        EntityType.VISIT_STOP: (itinerary.visit_stops, "item_id"),
        EntityType.DINING_STOP: (itinerary.dining_stops, "item_id"),
        EntityType.LODGING_STAY: (itinerary.lodging_stays, "stay_id"),
    }
    target = collections.get(ref.entity_type)
    if target is None:
        return None
    items, identity = target
    entity = next((item for item in items if getattr(item, identity) == ref.entity_id), None)
    return None if entity is None else TransportEndpoint(name=entity.name, place_id=entity.place_id)


def _invalidate_changed_connectors(itinerary: StructuredItineraryV2) -> StructuredItineraryV2:
    legs = {leg.transport_leg_id: leg for leg in itinerary.transport_legs}
    for day in itinerary.day_plans:
        for index, ref in enumerate(day.timeline):
            if ref.entity_type != EntityType.TRANSPORT_LEG:
                continue
            leg = legs[ref.entity_id]
            if leg.transport_class == "long_distance":
                continue
            previous = next((_endpoint_for_ref(itinerary, item) for item in reversed(day.timeline[:index]) if _endpoint_for_ref(itinerary, item)), None)
            following = next((_endpoint_for_ref(itinerary, item) for item in day.timeline[index + 1:] if _endpoint_for_ref(itinerary, item)), None)
            if previous is None or following is None or (previous == leg.from_endpoint and following == leg.to_endpoint):
                continue
            legs[ref.entity_id] = leg.model_copy(update={
                "from_endpoint": previous, "to_endpoint": following,
                "segments": [], "transfer_count": 0, "route_status": "pending",
                "duration_minutes": None, "distance_meters": None, "total_cost_cny": None,
            })
    return _recompute_derived_itinerary_fields(
        itinerary.model_copy(update={"transport_legs": list(legs.values())})
    )


def _selection_label(entity: SelectableEntity) -> str:
    """What the traveller sees this selection called in the undo list.

    A leg has no ``name`` of its own — it is a journey between two places — so it
    is named by the journey rather than crashing on a field only the three place
    entities carry.
    """

    if isinstance(entity, TransportLeg):
        return f"选择{entity.from_endpoint.name} → {entity.to_endpoint.name}"
    return f"选择{entity.name}"


def _selection_is_unrealized(workspace: TripWorkspaceV2, slot: SelectionSlot) -> bool:
    """Whether the itinerary does *not* yet reflect the slot's own selection.

    Re-picking the option a slot already names is normally nothing to do.  A
    transport leg is the exception: a leg can be selected and still hold no route
    — that is exactly the state ``set_transport_mode`` leaves behind, and the
    state a provider outage leaves behind — and re-picking it there means "bind a
    real route for this choice".

    Answering it this way is what makes rebinding reachable from a client at all,
    and it is the whole reason no second identifier is needed for it: the slot's
    ``option_id`` is the only identity this boundary publishes.
    """

    if slot.slot_type != "transport":
        return False
    leg = next(
        (
            item
            for item in workspace.itinerary.transport_legs
            if item.transport_leg_id == slot.target_entity_id
        ),
        None,
    )
    return leg is not None and leg.route_status != "ready"


def _apply_selection(workspace: TripWorkspaceV2, mutation: SelectOptionMutation) -> WorkspaceV2MutationApplication:
    slot = _slot(workspace, mutation.selection_slot_id)
    option = _option(slot, mutation.option_id)
    if slot.selected_option_id == option.option_id and not _selection_is_unrealized(
        workspace, slot
    ):
        return WorkspaceV2MutationApplication(workspace=workspace, changed=False)
    if slot.status not in {"ready", "needs_user_decision"}:
        raise WorkspaceV2MutationError("selection_slot_not_ready", "SelectionSlot is not selectable")
    candidate = workspace.recommendation_catalog.candidate_index().get(option.candidate_id)
    admission = workspace.recommendation_catalog.admission_index().get((option.candidate_id, slot.selection_slot_id))
    if candidate is None or admission is None or admission.status != "passed":
        raise WorkspaceV2MutationError("candidate_not_admitted", "Candidate did not pass slot admission")
    if getattr(candidate, "availability_status", None) == "unavailable":
        raise WorkspaceV2MutationError("candidate_unavailable", "Unavailable candidate cannot be selected")
    # Re-derived, not read off the candidate: a visit candidate has no
    # ``availability_status`` of its own, and the one authority for what an option
    # may say is ``entities/candidate_options.py`` — the same function composition
    # minted the option with.
    if candidate_option_availability(candidate) != option.availability_status:
        raise WorkspaceV2MutationError("availability_mismatch", "Option availability differs from candidate")
    itinerary = workspace.itinerary
    if slot.slot_type == "dining":
        if not isinstance(candidate, DiningCandidate) or option.candidate_entity_ref.entity_type != EntityType.DINING_STOP:
            raise WorkspaceV2MutationError("candidate_type_mismatch", "Dining slot requires DiningCandidate")
        previous, updated = _replace_dining(workspace, slot, option, candidate)
        itinerary = itinerary.model_copy(update={
            "dining_stops": [updated if item.item_id == previous.item_id else item for item in itinerary.dining_stops],
        })
    elif slot.slot_type == "visit":
        if not isinstance(candidate, VisitCandidate) or option.candidate_entity_ref.entity_type != EntityType.VISIT_STOP:
            raise WorkspaceV2MutationError("candidate_type_mismatch", "Visit slot requires VisitCandidate")
        previous, updated = _replace_visit_stop(workspace, slot, option, candidate)
        itinerary = itinerary.model_copy(update={
            "visit_stops": [updated if item.item_id == previous.item_id else item for item in itinerary.visit_stops],
        })
    elif slot.slot_type == "transport":
        if not isinstance(candidate, TransportCandidate) or option.candidate_entity_ref.entity_type != EntityType.TRANSPORT_LEG:
            raise WorkspaceV2MutationError("candidate_type_mismatch", "Transport slot requires TransportCandidate")
        previous, updated = _replace_transport_leg(workspace, slot, option, candidate)
        itinerary = itinerary.model_copy(update={
            "transport_legs": [updated if item.transport_leg_id == previous.transport_leg_id else item for item in itinerary.transport_legs],
        })
    else:
        if not isinstance(candidate, LodgingCandidate) or option.candidate_entity_ref.entity_type != EntityType.LODGING_STAY:
            raise WorkspaceV2MutationError("candidate_type_mismatch", "Lodging slot requires LodgingCandidate")
        previous, updated = _replace_lodging(workspace, slot, option, candidate)
        itinerary = itinerary.model_copy(update={
            "lodging_stays": [updated if item.stay_id == previous.stay_id else item for item in itinerary.lodging_stays],
        })
    itinerary = _recompute_derived_itinerary_fields(itinerary)
    next_slot = slot.model_copy(update={"selected_option_id": option.option_id, "status": "ready"})
    next_workspace = TripWorkspaceV2.model_validate(workspace.model_copy(update={
        "workspace_revision": workspace.workspace_revision + 1,
        "itinerary": itinerary,
        "selection_slots": [next_slot if item.selection_slot_id == slot.selection_slot_id else item for item in workspace.selection_slots],
    }).model_dump(mode="json"))
    return WorkspaceV2MutationApplication(
        workspace=next_workspace, changed=True, label=_selection_label(updated),
        inverse=SelectionInversePatch(
            selection_slot_id=slot.selection_slot_id, applied_option_id=option.option_id,
            applied_workspace_revision=next_workspace.workspace_revision,
            previous_selected_option_id=slot.selected_option_id, previous_slot_status=slot.status,
            previous_entity=previous,
        ),
    )


def _proposal(
    workspace: TripWorkspaceV2,
    weather: Optional[WeatherContextSnapshot],
    proposal_id: str,
):
    if weather is None:
        raise WorkspaceV2MutationError(
            "weather_snapshot_required", "Weather adjustment requires the current Weather snapshot"
        )
    proposal = next(
        (item for item in weather.adjustment_proposals if item.proposal_id == proposal_id),
        None,
    )
    if proposal is None:
        raise WorkspaceV2MutationError(
            "weather_proposal_not_found", "Weather adjustment proposal does not exist"
        )
    if any(item.proposal_id == proposal_id for item in workspace.weather_proposal_decisions):
        raise WorkspaceV2MutationError(
            "weather_proposal_resolved", "Weather adjustment proposal was already handled"
        )
    return proposal


def _replace_visit_candidate(
    workspace: TripWorkspaceV2,
    operation: WeatherVisitReplacementOperation,
) -> WorkspaceV2MutationApplication:
    itinerary = workspace.itinerary
    current = next(
        (item for item in itinerary.visit_stops if item.item_id == operation.item_id),
        None,
    )
    if current is None:
        raise WorkspaceV2MutationError("visit_not_found", "Visit target does not exist")
    if current.lineage.candidate_id != operation.expected_candidate_id:
        raise WorkspaceV2MutationError(
            "weather_proposal_stale", "Visit changed after the weather proposal"
        )
    candidate = workspace.recommendation_catalog.candidate_index().get(operation.candidate_id)
    admission = workspace.recommendation_catalog.admission_index().get(
        (operation.candidate_id, None)
    )
    if not isinstance(candidate, VisitCandidate) or admission is None or admission.status != "passed":
        raise WorkspaceV2MutationError(
            "candidate_not_admitted", "Weather replacement candidate did not pass admission"
        )
    planned_end = (
        current.planned_start
        + timedelta(minutes=candidate.recommended_duration_minutes)
        if current.planned_start is not None
        else current.planned_end
    )
    updated = VisitStop(
        item_id=current.item_id,
        day_id=current.day_id,
        place_id=candidate.place_id,
        name=candidate.name,
        address=candidate.address,
        planned_start=current.planned_start,
        planned_end=planned_end,
        duration_minutes=candidate.recommended_duration_minutes,
        estimated_cost_cny=candidate.estimated_cost_cny,
        selection_reason=_selection_reason(
            SelectionOption(
                option_id=f"weather_{candidate.candidate_id}",
                selection_slot_id="weather_adjustment",
                candidate_id=candidate.candidate_id,
                candidate_entity_ref=EntityRef(
                    entity_type=EntityType.VISIT_STOP, entity_id=current.item_id
                ),
                rank=1,
                selection_reasons=candidate.selection_reasons,
                comparison_facts=candidate.field_paths,
                availability_status="confirmed",
                fact_assertion_ids=candidate.fact_assertion_ids,
                source_record_ids=candidate.source_record_ids,
                personalization_influence_ids=candidate.personalization_influence_ids,
            )
        ),
        lineage=EntityLineage(
            research_packet_id=candidate.research_packet_id,
            candidate_id=candidate.candidate_id,
            fact_assertion_ids=candidate.fact_assertion_ids,
            source_record_ids=candidate.source_record_ids,
            planning_decision_ids=candidate.planning_decision_ids,
            weather_impact_ids=candidate.weather_impact_ids,
            personalization_influence_ids=candidate.personalization_influence_ids,
        ),
        visit_type=candidate.visit_type,
        opening_window=candidate.opening_window,
        reservation_required=candidate.reservation_required,
        visit_highlights=candidate.highlights,
    )
    return _itinerary_result(
        workspace,
        itinerary.model_copy(
            update={
                "visit_stops": [
                    updated if item.item_id == current.item_id else item
                    for item in itinerary.visit_stops
                ],
            }
        ),
        f"替换为{updated.name}",
    )


def _transport_from_candidate(
    current: TransportLeg,
    candidate: TransportCandidate,
) -> TransportLeg:
    return TransportLeg(
        transport_leg_id=current.transport_leg_id,
        transport_class=candidate.transport_class,
        selected_mode=candidate.selected_mode,
        from_endpoint=candidate.from_endpoint,
        to_endpoint=candidate.to_endpoint,
        departure_at=candidate.departure_at,
        arrival_at=candidate.arrival_at,
        duration_minutes=candidate.duration_minutes,
        distance_meters=candidate.distance_meters,
        total_cost_cny=candidate.total_cost_cny,
        transfer_count=max(len(candidate.segments) - 1, 0),
        segments=candidate.segments,
        booking_status=candidate.booking_status,
        route_status="ready",
        mode_preference=current.mode_preference,
        lineage=EntityLineage(
            research_packet_id=candidate.research_packet_id,
            candidate_id=candidate.candidate_id,
            fact_assertion_ids=candidate.fact_assertion_ids,
            source_record_ids=candidate.source_record_ids,
            planning_decision_ids=candidate.planning_decision_ids,
            weather_impact_ids=candidate.weather_impact_ids,
            personalization_influence_ids=candidate.personalization_influence_ids,
        ),
    )


def _check_transport_replacement(
    current: TransportLeg,
    candidate: TransportCandidate,
) -> None:
    """Refuse a transport candidate that cannot lawfully stand in for this leg.

    The composition asks the same questions when it decides which candidates to
    *offer* (``itinerary_composition_v2._transport_alternatives``), so a slot
    normally never presents an option this function would then reject.  The check
    still has to run at apply time: a slot is built once, and a mode preference
    set afterwards can make an already-offered option unusable.
    """

    preference = current.mode_preference
    if (
        preference.locked_mode is not None
        and candidate.selected_mode != preference.locked_mode
    ):
        raise WorkspaceV2MutationError(
            "transport_mode_locked",
            "Replacement candidate conflicts with the locked transport mode",
        )
    if candidate.selected_mode in preference.excluded_modes:
        raise WorkspaceV2MutationError(
            "transport_mode_excluded",
            "Replacement candidate uses an excluded transport mode",
        )
    same_local_connector = (
        current.from_endpoint.place_id is not None
        and current.to_endpoint.place_id is not None
        and candidate.from_endpoint.place_id == current.from_endpoint.place_id
        and candidate.to_endpoint.place_id == current.to_endpoint.place_id
    )
    if current.transport_class == "long_distance":
        if candidate.transport_class != "long_distance":
            raise WorkspaceV2MutationError(
                "transport_replacement_context_mismatch",
                "Long-distance transport can only be replaced by another long-distance option",
            )
    elif (
        candidate.transport_class not in {"public_transit", "flexible"}
        or not same_local_connector
    ):
        raise WorkspaceV2MutationError(
            "transport_replacement_context_mismatch",
            "Local transport replacement must connect the same ordered endpoints",
        )


def _check_preference_leaves_a_way_back(
    workspace: TripWorkspaceV2,
    current: TransportLeg,
    preference: TransportModePreference,
) -> None:
    """Refuse a mode preference that would strand this leg with no route.

    The invariant this defends: **after any accepted preference change, the leg
    still has at least one entrance that can put it back to ``ready``.**  That
    entrance is always the same one — ``select_option`` on the leg's slot — and
    ``_check_transport_replacement`` decides which of the slot's options it will
    accept.  Lock a mode the catalog cannot serve, or exclude every mode it can,
    and every option is refused: the leg's ``route_status`` sticks at
    ``pending`` with no duration and no cost, and nothing short of undoing the
    preference reaches it again.

    Checked here rather than at the two call sites because both of them set the
    same field, and the previous split — ``set_transport_mode`` guarding nothing
    and ``update_transport_mode_preference`` guarding nothing either — is how the
    hole stayed open through two rounds.

    A leg with no slot is exempt, and deliberately so: it has no options to
    refuse, so the preference cannot be what strands it.  Those are the
    planner-authored connectors, which never had a selection entrance to lose.
    """

    slot = next(
        (
            item
            for item in workspace.selection_slots
            if item.slot_type == "transport"
            and item.target_entity_id == current.transport_leg_id
        ),
        None,
    )
    if slot is None:
        return
    probe = current.model_copy(update={"mode_preference": preference})
    candidates = workspace.recommendation_catalog.candidate_index()
    admissions = workspace.recommendation_catalog.admission_index()
    for option in slot.options:
        candidate = candidates.get(option.candidate_id)
        admission = admissions.get((option.candidate_id, slot.selection_slot_id))
        if (
            not isinstance(candidate, TransportCandidate)
            or admission is None
            or admission.status != "passed"
        ):
            continue
        try:
            _check_transport_replacement(probe, candidate)
        except WorkspaceV2MutationError:
            continue
        return
    raise WorkspaceV2MutationError(
        "transport_preference_strands_leg",
        "No admitted option for this leg survives the requested mode preference",
    )


def _operation_mutation(workspace: TripWorkspaceV2, operation):
    if isinstance(operation, WeatherRescheduleOperation):
        current = next(
            (
                item
                for item in [
                    *workspace.itinerary.visit_stops,
                    *workspace.itinerary.dining_stops,
                    *workspace.itinerary.custom_blocks,
                ]
                if item.item_id == operation.item_id
            ),
            None,
        )
        if (
            current is None
            or current.planned_start != operation.expected_planned_start
            or current.planned_end != operation.expected_planned_end
        ):
            raise WorkspaceV2MutationError(
                "weather_proposal_stale", "Schedule changed after the weather proposal"
            )
        return UpdateStopScheduleMutation(
            item_id=operation.item_id,
            planned_start=operation.planned_start,
            planned_end=operation.planned_end,
        )
    if isinstance(operation, WeatherSelectionOperation):
        slot = _slot(workspace, operation.selection_slot_id)
        if slot.selected_option_id != operation.expected_option_id:
            raise WorkspaceV2MutationError(
                "weather_proposal_stale", "Selection changed after the weather proposal"
            )
        return SelectOptionMutation(
            selection_slot_id=operation.selection_slot_id,
            option_id=operation.option_id,
        )
    if isinstance(operation, WeatherTransportModeOperation):
        leg = next(
            (
                item
                for item in workspace.itinerary.transport_legs
                if item.transport_leg_id == operation.transport_leg_id
            ),
            None,
        )
        if leg is None or leg.selected_mode != operation.expected_mode:
            raise WorkspaceV2MutationError(
                "weather_proposal_stale", "Transport changed after the weather proposal"
            )
        # Explicitly unlocked.  A weather adjustment is advice about one day, not
        # a standing preference, and ``_transport_operation`` picks the new mode
        # off a fixed list without ever asking whether the catalog can serve it.
        # Inheriting ``lock_mode=True`` here would pin the leg to a mode with no
        # admitted candidate — the very state ``_check_preference_leaves_a_way_back``
        # exists to refuse, which would make every weather transport proposal
        # unappliable instead of merely unhelpful.
        return SetTransportModeMutation(
            transport_leg_id=operation.transport_leg_id,
            selected_mode=operation.selected_mode,
            lock_mode=False,
        )
    if isinstance(operation, WeatherBufferOperation):
        day = next(
            (item for item in workspace.itinerary.day_plans if item.day_id == operation.day_id),
            None,
        )
        if day is None:
            raise WorkspaceV2MutationError("weather_proposal_stale", "Weather buffer day changed")
        target_index = next(
            (
                index
                for index, item in enumerate(day.timeline)
                if item.entity_id == operation.target_entity_id
            ),
            None,
        )
        if target_index is None:
            raise WorkspaceV2MutationError(
                "weather_proposal_stale", "Weather buffer target changed"
            )
        before = (
            day.timeline[target_index + 1].entry_id
            if target_index + 1 < len(day.timeline)
            else None
        )
        return CreateCustomBlockMutation(
            block=CustomBlock(
                item_id=operation.block_id,
                day_id=operation.day_id,
                title="天气机动时间",
                note="为当天具体天气变化预留的执行缓冲",
                duration_minutes=operation.duration_minutes,
            ),
            before_entry_id=before,
        )
    return None


def _apply_weather_adjustment(
    workspace: TripWorkspaceV2,
    weather: Optional[WeatherContextSnapshot],
    mutation: ApplyWeatherAdjustmentMutation,
) -> WorkspaceV2MutationApplication:
    proposal = _proposal(workspace, weather, mutation.proposal_id)
    working = workspace
    changed = False
    for operation in proposal.operations:
        if isinstance(operation, WeatherVisitReplacementOperation):
            application = _replace_visit_candidate(working, operation)
        else:
            nested = _operation_mutation(working, operation)
            if nested is None:
                raise WorkspaceV2MutationError(
                    "weather_operation_unsupported", "Weather proposal operation is unsupported"
                )
            application = apply_workspace_v2_mutation(working, nested)
        working = application.workspace
        changed = changed or application.changed
    if not changed:
        raise WorkspaceV2MutationError(
            "weather_proposal_no_change", "Weather proposal no longer changes the itinerary"
        )
    decisions = [
        *working.weather_proposal_decisions,
        WeatherProposalDecision(proposal_id=proposal.proposal_id, decision="applied"),
    ]
    return _snapshot_result(
        workspace,
        working.model_copy(update={"weather_proposal_decisions": decisions}),
        f"应用{proposal.date.isoformat()}天气调整",
    )


def apply_workspace_v2_mutation(
    workspace: TripWorkspaceV2,
    mutation: WorkspaceV2Mutation,
    weather_snapshot: Optional[WeatherContextSnapshot] = None,
) -> WorkspaceV2MutationApplication:
    if isinstance(mutation, ApplyWeatherAdjustmentMutation):
        return _apply_weather_adjustment(workspace, weather_snapshot, mutation)
    if isinstance(mutation, DismissWeatherAdjustmentMutation):
        proposal = _proposal(workspace, weather_snapshot, mutation.proposal_id)
        return _snapshot_result(
            workspace,
            workspace.model_copy(
                update={
                    "weather_proposal_decisions": [
                        *workspace.weather_proposal_decisions,
                        WeatherProposalDecision(
                            proposal_id=proposal.proposal_id, decision="dismissed"
                        ),
                    ]
                }
            ),
            "暂不采用天气调整",
        )
    if isinstance(mutation, SelectOptionMutation):
        return _apply_selection(workspace, mutation)
    itinerary = workspace.itinerary
    if isinstance(mutation, MoveTimelineItemMutation):
        source_day, source_index, ref = _timeline_location(itinerary, mutation.item_id)
        if ref.entity_type not in {EntityType.VISIT_STOP, EntityType.DINING_STOP, EntityType.CUSTOM_BLOCK}:
            raise WorkspaceV2MutationError("item_not_movable", "Only Visit, Dining, and Custom items can be moved")
        target_day = next((index for index, day in enumerate(itinerary.day_plans) if day.day_id == mutation.to_day_id), None)
        if target_day is None:
            raise WorkspaceV2MutationError("day_not_found", "Target day does not exist")
        day_plans = [day.model_copy(deep=True) for day in itinerary.day_plans]
        moved = day_plans[source_day].timeline.pop(source_index)
        target_timeline = day_plans[target_day].timeline
        if mutation.before_entry_id is None:
            target_timeline.append(moved)
        else:
            before = next((index for index, item in enumerate(target_timeline) if item.entry_id == mutation.before_entry_id), None)
            if before is None:
                raise WorkspaceV2MutationError("before_entry_not_found", "Insertion anchor does not exist in target day")
            target_timeline.insert(before, moved)
        updates = {"day_plans": day_plans}
        key = {EntityType.VISIT_STOP: "visit_stops", EntityType.DINING_STOP: "dining_stops", EntityType.CUSTOM_BLOCK: "custom_blocks"}[ref.entity_type]
        target = day_plans[target_day]
        updates[key] = [
            _move_scheduled_item_to_day(item, target)
            if item.item_id == ref.entity_id
            else item
            for item in getattr(itinerary, key)
        ]
        next_itinerary = _invalidate_changed_connectors(itinerary.model_copy(update=updates))
        return _itinerary_result(workspace, next_itinerary, "移动行程安排")
    if isinstance(mutation, UpdateStopScheduleMutation):
        for key in ("visit_stops", "dining_stops", "custom_blocks"):
            items = getattr(itinerary, key)
            current = next((item for item in items if item.item_id == mutation.item_id), None)
            if current is None:
                continue
            values = {name: getattr(mutation, name) for name in ("planned_start", "planned_end", "duration_minutes") if name in mutation.model_fields_set}
            updated = current.model_copy(update=values)
            if updated.planned_start and updated.planned_end and updated.planned_end <= updated.planned_start:
                raise WorkspaceV2MutationError("invalid_schedule", "planned_end must be after planned_start")
            return _itinerary_result(workspace, itinerary.model_copy(update={key: [updated if item.item_id == current.item_id else item for item in items]}), "调整时间")
        raise WorkspaceV2MutationError("scheduled_item_not_found", "Scheduled item does not exist")
    if isinstance(mutation, CreateCustomBlockMutation):
        if mutation.block.day_id not in {day.day_id for day in itinerary.day_plans}:
            raise WorkspaceV2MutationError("day_not_found", "Custom block day does not exist")
        if any(item.item_id == mutation.block.item_id for item in itinerary.custom_blocks):
            raise WorkspaceV2MutationError("item_id_conflict", "Custom block id already exists")
        day_plans = [day.model_copy(deep=True) for day in itinerary.day_plans]
        day = next(item for item in day_plans if item.day_id == mutation.block.day_id)
        ref = TimelineEntryRef(entry_id=f"entry_{mutation.block.item_id}", entity_type=EntityType.CUSTOM_BLOCK, entity_id=mutation.block.item_id)
        if mutation.before_entry_id is None:
            day.timeline.append(ref)
        else:
            before = next((i for i, item in enumerate(day.timeline) if item.entry_id == mutation.before_entry_id), None)
            if before is None:
                raise WorkspaceV2MutationError("before_entry_not_found", "Insertion anchor does not exist")
            day.timeline.insert(before, ref)
        return _itinerary_result(workspace, itinerary.model_copy(update={"day_plans": day_plans, "custom_blocks": [*itinerary.custom_blocks, mutation.block]}), f"添加{mutation.block.title}")
    if isinstance(mutation, UpdateCustomBlockMutation):
        current = next((item for item in itinerary.custom_blocks if item.item_id == mutation.item_id), None)
        if current is None:
            raise WorkspaceV2MutationError("custom_block_not_found", "Custom block does not exist")
        values = {name: getattr(mutation, name) for name in ("title", "note", "planned_start", "planned_end", "duration_minutes") if name in mutation.model_fields_set}
        updated = current.model_copy(update=values)
        if updated.planned_start and updated.planned_end and updated.planned_end <= updated.planned_start:
            raise WorkspaceV2MutationError("invalid_schedule", "planned_end must be after planned_start")
        return _itinerary_result(workspace, itinerary.model_copy(update={"custom_blocks": [updated if item.item_id == current.item_id else item for item in itinerary.custom_blocks]}), f"更新{updated.title}")
    if isinstance(mutation, DeleteCustomBlockMutation):
        if not any(item.item_id == mutation.item_id for item in itinerary.custom_blocks):
            raise WorkspaceV2MutationError("custom_block_not_found", "Custom block does not exist")
        day_plans = [day.model_copy(update={"timeline": [ref for ref in day.timeline if not (ref.entity_type == EntityType.CUSTOM_BLOCK and ref.entity_id == mutation.item_id)]}) for day in itinerary.day_plans]
        return _itinerary_result(workspace, itinerary.model_copy(update={"day_plans": day_plans, "custom_blocks": [item for item in itinerary.custom_blocks if item.item_id != mutation.item_id]}), "删除自定义安排")
    if isinstance(mutation, DeleteTransportLegMutation):
        current = next((item for item in itinerary.transport_legs if item.transport_leg_id == mutation.transport_leg_id), None)
        if current is None:
            raise WorkspaceV2MutationError("transport_leg_not_found", "Transport leg does not exist")
        day_plans = [day.model_copy(update={"timeline": [ref for ref in day.timeline if not (ref.entity_type == EntityType.TRANSPORT_LEG and ref.entity_id == mutation.transport_leg_id)]}) for day in itinerary.day_plans]
        next_itinerary = itinerary.model_copy(update={"day_plans": day_plans, "transport_legs": [item for item in itinerary.transport_legs if item.transport_leg_id != mutation.transport_leg_id]})
        # 腿走了，它的槽位也要走——槽位指向一个不存在的实体，workspace 合同直接拒。
        # 与「删除整段住宿」同一处置。
        return _snapshot_result(
            workspace,
            workspace.model_copy(
                update={
                    "selection_slots": [
                        item
                        for item in workspace.selection_slots
                        if item.target_entity_id != mutation.transport_leg_id
                    ],
                    "itinerary": next_itinerary,
                }
            ),
            "删除交通安排",
        )
    if isinstance(mutation, DeleteLodgingStayMutation):
        current = next(
            (item for item in itinerary.lodging_stays if item.stay_id == mutation.stay_id),
            None,
        )
        if current is None:
            raise WorkspaceV2MutationError(
                "lodging_stay_not_found", "Lodging stay does not exist"
            )
        day_plans = [
            day.model_copy(
                update={
                    "timeline": [
                        ref
                        for ref in day.timeline
                        if not (
                            ref.entity_type == EntityType.LODGING_STAY
                            and ref.entity_id == mutation.stay_id
                        )
                    ]
                }
            )
            for day in itinerary.day_plans
        ]
        selection_slots = [
            item
            for item in workspace.selection_slots
            if item.target_entity_id != mutation.stay_id
        ]
        next_itinerary = itinerary.model_copy(
            update={
                "day_plans": day_plans,
                "lodging_stays": [
                    item
                    for item in itinerary.lodging_stays
                    if item.stay_id != mutation.stay_id
                ],
            }
        )
        return _snapshot_result(
            workspace,
            workspace.model_copy(
                update={
                    "selection_slots": selection_slots,
                    "itinerary": next_itinerary,
                }
            ),
            "删除整段住宿",
        )
    if isinstance(mutation, UpdateTransportModePreferenceMutation):
        current = next(
            (
                item
                for item in itinerary.transport_legs
                if item.transport_leg_id == mutation.transport_leg_id
            ),
            None,
        )
        if current is None:
            raise WorkspaceV2MutationError(
                "transport_leg_not_found", "Transport leg does not exist"
            )
        values = {
            name: getattr(mutation, name)
            for name in ("locked_mode", "excluded_modes")
            if name in mutation.model_fields_set
        }
        preference = current.mode_preference.model_copy(update=values)
        preference = type(current.mode_preference).model_validate(
            preference.model_dump(mode="json")
        )
        if preference == current.mode_preference:
            return WorkspaceV2MutationApplication(workspace=workspace, changed=False)
        _check_preference_leaves_a_way_back(workspace, current, preference)
        updated = current.model_copy(update={"mode_preference": preference})
        return _itinerary_result(
            workspace,
            itinerary.model_copy(
                update={
                    "transport_legs": [
                        updated
                        if item.transport_leg_id == current.transport_leg_id
                        else item
                        for item in itinerary.transport_legs
                    ]
                }
            ),
            "更新交通偏好",
        )
    if isinstance(mutation, SetTransportModeMutation):
        current = next((item for item in itinerary.transport_legs if item.transport_leg_id == mutation.transport_leg_id), None)
        if current is None:
            raise WorkspaceV2MutationError("transport_leg_not_found", "Transport leg does not exist")
        if current.transport_class == "long_distance":
            raise WorkspaceV2MutationError("long_distance_requires_option", "Long-distance transport requires explicit option replacement")
        if mutation.selected_mode in current.mode_preference.excluded_modes:
            raise WorkspaceV2MutationError("transport_mode_excluded", "Selected mode is excluded by user preference")
        preference = current.mode_preference.model_copy(update={"locked_mode": mutation.selected_mode if mutation.lock_mode else None})
        if preference != current.mode_preference:
            _check_preference_leaves_a_way_back(workspace, current, preference)
        if current.selected_mode == mutation.selected_mode:
            if preference == current.mode_preference:
                return WorkspaceV2MutationApplication(workspace=workspace, changed=False)
            updated = current.model_copy(update={"mode_preference": preference})
            return _itinerary_result(
                workspace,
                itinerary.model_copy(
                    update={
                        "transport_legs": [
                            updated
                            if item.transport_leg_id == current.transport_leg_id
                            else item
                            for item in itinerary.transport_legs
                        ]
                    }
                ),
                "更新交通偏好",
            )
        updated = current.model_copy(update={
            "selected_mode": mutation.selected_mode, "mode_preference": preference,
            "segments": [], "transfer_count": 0, "route_status": "pending",
            "duration_minutes": None, "distance_meters": None, "total_cost_cny": None,
        })
        return _itinerary_result(workspace, itinerary.model_copy(update={"transport_legs": [updated if item.transport_leg_id == current.transport_leg_id else item for item in itinerary.transport_legs]}), "切换交通方式")
    raise WorkspaceV2MutationError("unsupported_operation", "Unsupported workspace v2 mutation")


def apply_workspace_v2_inverse(
    workspace: TripWorkspaceV2,
    inverse: WorkspaceV2InversePatch,
    *,
    allow_data_refresh_rebase: bool = False,
) -> TripWorkspaceV2:
    if (
        workspace.workspace_revision != inverse.applied_workspace_revision
        and not (
            allow_data_refresh_rebase
            and not isinstance(inverse, WorkspaceSnapshotInversePatch)
            and workspace.workspace_revision > inverse.applied_workspace_revision
        )
    ):
        raise WorkspaceV2MutationError("undo_head_mismatch", "Workspace changed since this mutation")
    if isinstance(inverse, ItineraryInversePatch):
        return _validated_workspace(workspace, inverse.previous_itinerary)
    if isinstance(inverse, WorkspaceSnapshotInversePatch):
        if inverse.previous_workspace.run_id != workspace.run_id:
            raise WorkspaceV2MutationError(
                "inverse_run_mismatch", "Inverse workspace belongs to another run"
            )
        return TripWorkspaceV2.model_validate(
            inverse.previous_workspace.model_copy(
                update={"workspace_revision": workspace.workspace_revision + 1}
            ).model_dump(mode="json")
        )
    return apply_selection_inverse(
        workspace,
        inverse,
        allow_data_refresh_rebase=allow_data_refresh_rebase,
    )


def apply_selection_inverse(
    workspace: TripWorkspaceV2,
    inverse: SelectionInversePatch,
    *,
    allow_data_refresh_rebase: bool = False,
) -> TripWorkspaceV2:
    if (
        workspace.workspace_revision != inverse.applied_workspace_revision
        and not (
            allow_data_refresh_rebase
            and workspace.workspace_revision > inverse.applied_workspace_revision
        )
    ):
        raise WorkspaceV2MutationError("undo_head_mismatch", "Workspace changed since this mutation")
    slot = _slot(workspace, inverse.selection_slot_id)
    if slot.selected_option_id != inverse.applied_option_id:
        raise WorkspaceV2MutationError("undo_head_mismatch", "Selection changed since this mutation")
    if inverse.previous_selected_option_id is not None:
        previous_option = _option(slot, inverse.previous_selected_option_id)
        if previous_option.candidate_id != inverse.previous_entity.lineage.candidate_id:
            raise WorkspaceV2MutationError("inverse_lineage_mismatch", "Inverse entity does not match previous option")
    itinerary = workspace.itinerary
    if isinstance(inverse.previous_entity, DiningStop):
        itinerary = itinerary.model_copy(update={
            "dining_stops": [inverse.previous_entity if item.item_id == inverse.previous_entity.item_id else item for item in itinerary.dining_stops],
        })
    elif isinstance(inverse.previous_entity, VisitStop):
        itinerary = itinerary.model_copy(update={
            "visit_stops": [inverse.previous_entity if item.item_id == inverse.previous_entity.item_id else item for item in itinerary.visit_stops],
        })
    elif isinstance(inverse.previous_entity, TransportLeg):
        itinerary = itinerary.model_copy(update={
            "transport_legs": [inverse.previous_entity if item.transport_leg_id == inverse.previous_entity.transport_leg_id else item for item in itinerary.transport_legs],
        })
    else:
        itinerary = itinerary.model_copy(update={
            "lodging_stays": [inverse.previous_entity if item.stay_id == inverse.previous_entity.stay_id else item for item in itinerary.lodging_stays],
        })
    itinerary = _recompute_derived_itinerary_fields(itinerary)
    next_slot = slot.model_copy(update={"selected_option_id": inverse.previous_selected_option_id, "status": inverse.previous_slot_status})
    return TripWorkspaceV2.model_validate(workspace.model_copy(update={
        "workspace_revision": workspace.workspace_revision + 1,
        "itinerary": itinerary,
        "selection_slots": [next_slot if item.selection_slot_id == slot.selection_slot_id else item for item in workspace.selection_slots],
    }).model_dump(mode="json"))
