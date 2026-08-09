"""Deterministic delivery-gap classification before projection.

Every gap is recorded. Only a gap that breaks the deterministic projection's
structural invariants — a broken day chain, a connector whose endpoints miss
the adjacent stops, an adjacency with no connector at all, one candidate
materialized twice — blocks. Source coverage, weather coverage and slot
richness are recorded and shipped.

A blocking gap buys at most one repair round; once that budget is spent the
current composition goes to projection. The gate never spends a provider retry
budget: Candidate Gate is the only owner of targeted research attempts.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping

from ...entities.delivery_bundle import (
    SelectionSlot,
    DeliveryContractViolation,
    DeliveryQualityGap,
    EntityType,
    GateClass,
    GateDisposition,
    GateFailureAttribution,
    RecommendationQualityState,
    ResearchDomain,
)
from ...entities.state import TravelAgentState
from ...workflows.composition_repair import (
    apply_composition_repair_budget,
    composition_repair_attempts,
    composition_repair_budget_exhausted,
)
from ...workflows.run_deadline import observe_run_deadline


def _gap_id(reason: str, field_path: str, entity_id: str | None) -> str:
    digest = hashlib.sha256(f"{reason}|{field_path}|{entity_id or ''}".encode()).hexdigest()[:18]
    return f"delivery_gap_{digest}"


def _gap(
    *,
    gate: str,
    reason: str,
    field_path: str,
    retry_target: str,
    entity_id: str | None = None,
    candidate_id: str | None = None,
    worker_kind: str | None = None,
    repair_context: dict[str, Any] | None = None,
    blocking: bool = True,
    gate_class: GateClass | None = None,
    research_domain: ResearchDomain | None = None,
) -> DeliveryQualityGap:
    inferred = gate_class or GateClass.COMPOSITION
    return DeliveryQualityGap(
        gap_id=_gap_id(reason, field_path, entity_id),
        gate=gate,
        reason=reason,
        field_path=field_path,
        retry_target=retry_target,
        entity_id=entity_id,
        candidate_id=candidate_id,
        worker_kind=worker_kind,
        repair_context=repair_context or {},
        gate_class=inferred,
        research_domain=research_domain,
        blocking=blocking,
    )


def _attribution(
    state: TravelAgentState,
    *,
    gate_class: GateClass,
    disposition: GateDisposition,
    reason_code: str,
    gaps: list[DeliveryQualityGap],
) -> Dict[str, GateFailureAttribution]:
    records = dict(state.gate_failure_attributions or {})
    draft_id = state.minimum_delivery_draft.draft_id if state.minimum_delivery_draft else None
    grouped: dict[ResearchDomain | None, list[DeliveryQualityGap]] = {}
    for gap in gaps:
        grouped.setdefault(gap.research_domain, []).append(gap)
    for domain, scoped_gaps in grouped.items():
        gap_ids = sorted({gap.gap_id for gap in scoped_gaps})
        material = "|".join(
            [
                draft_id or "",
                gate_class.value,
                disposition.value,
                reason_code,
                domain.value if domain else "",
                *gap_ids,
            ]
        )
        attribution_id = f"gate_failure_{hashlib.sha256(material.encode()).hexdigest()[:24]}"
        records[attribution_id] = GateFailureAttribution(
            attribution_id=attribution_id,
            draft_id=draft_id,
            gate_class=gate_class,
            disposition=disposition,
            reason_code=reason_code,
            research_domain=domain,
            gap_ids=gap_ids,
            recorded_at=datetime.now(timezone.utc),
        )
    return records


def _slot_research_domain(
    slot: SelectionSlot, leg_classes: Mapping[str, str]
) -> ResearchDomain:
    """Name the research domain a slot's gap actually came from.

    Place slots need no translation: ``lodging`` / ``dining`` / ``visit`` are
    spelled exactly like their domains, which is why the old
    ``lodging``-or-else-``dining`` ternary reported every visit slot as dining.
    Transport is the one that splits — the two transport domains have separate
    research budgets and separate repair routes, so a gap has to say which of
    them it belongs to, and only the leg's own class knows.
    """

    if slot.slot_type != "transport":
        return ResearchDomain(slot.slot_type)
    return (
        ResearchDomain.LONG_DISTANCE_TRANSPORT
        if leg_classes.get(slot.target_entity_id) == "long_distance"
        else ResearchDomain.LOCAL_TRANSPORT
    )


def _slot_gaps(state: TravelAgentState) -> list[DeliveryQualityGap]:
    """Record selection-slot shape. A slot projects whatever options it holds."""

    workspace = state.trip_workspace_v2
    if workspace is None:
        return []
    gaps: list[DeliveryQualityGap] = []
    leg_classes = {
        leg.transport_leg_id: leg.transport_class
        for leg in workspace.itinerary.transport_legs
    }
    for slot in workspace.selection_slots:
        domain = _slot_research_domain(slot, leg_classes)
        if not 1 <= len(slot.options) <= 3:
            gaps.append(
                _gap(
                    gate="slot",
                    reason="slot_option_count",
                    field_path=f"selection_slots.{slot.selection_slot_id}.options",
                    retry_target="composition_repair",
                    entity_id=slot.selection_slot_id,
                    blocking=False,
                    gate_class=GateClass.COMPOSITION,
                    research_domain=domain,
                )
            )
            continue
        comparison_sets = {tuple(sorted(option.comparison_facts)) for option in slot.options}
        if len(comparison_sets) != 1:
            gaps.append(
                _gap(
                    gate="slot",
                    reason="slot_comparison_mismatch",
                    field_path=f"selection_slots.{slot.selection_slot_id}.options.comparison_facts",
                    retry_target="composition_repair",
                    entity_id=slot.selection_slot_id,
                    blocking=False,
                    gate_class=GateClass.COMPOSITION,
                    research_domain=domain,
                )
            )
    return gaps


def _itinerary_gaps(state: TravelAgentState) -> list[DeliveryQualityGap]:
    """Classify itinerary composition.

    Blocking gaps are the ones the deterministic projection cannot render: a
    broken day chain, a connector bound to the wrong endpoints, an adjacency
    with no connector, a city change with no long-distance leg, one candidate
    materialized as two entities. Coverage gaps are recorded and shipped.
    """

    workspace = state.trip_workspace_v2
    if workspace is None:
        return []
    itinerary = workspace.itinerary
    gaps: list[DeliveryQualityGap] = []
    dates = [day.date for day in itinerary.day_plans]
    for day in itinerary.day_plans:
        if not day.timeline:
            gaps.append(
                _gap(
                    gate="itinerary",
                    reason="day_timeline_missing",
                    field_path=f"itinerary.day_plans.{day.day_id}.timeline",
                    retry_target="composition_repair",
                    entity_id=day.day_id,
                    blocking=False,
                    gate_class=GateClass.COMPOSITION,
                )
            )
    for index in range(1, len(dates)):
        if dates[index] != dates[index - 1] + timedelta(days=1):
            gaps.append(
                _gap(
                    gate="itinerary",
                    reason="day_date_discontinuity",
                    field_path=f"itinerary.day_plans.{index}.date",
                    retry_target="composition_repair",
                    entity_id=itinerary.day_plans[index].day_id,
                    gate_class=GateClass.COMPOSITION,
                )
            )

    if len(dates) > 1:
        covered_nights = {
            stay.check_in_date + timedelta(days=offset)
            for stay in itinerary.lodging_stays
            for offset in range(stay.nights)
        }
        for night in dates[:-1]:
            if night not in covered_nights:
                gaps.append(
                    _gap(
                        gate="itinerary",
                        reason="missing_lodging_night",
                        field_path=f"itinerary.lodging_stays[{night.isoformat()}]",
                        retry_target="composition_repair",
                        entity_id=night.isoformat(),
                        blocking=False,
                        gate_class=GateClass.COMPOSITION,
                        research_domain=ResearchDomain.LODGING,
                    )
                )

    dining_day_ids = {stop.day_id for stop in itinerary.dining_stops}
    for day in itinerary.day_plans:
        if day.day_id not in dining_day_ids:
            gaps.append(
                _gap(
                    gate="itinerary",
                    reason="missing_dining_day",
                    field_path=f"itinerary.dining_stops[{day.day_id}]",
                    retry_target="composition_repair",
                    entity_id=day.day_id,
                    blocking=False,
                    gate_class=GateClass.COMPOSITION,
                    research_domain=ResearchDomain.DINING,
                )
            )

    physical_entities = {
        **{
            (EntityType.VISIT_STOP, item.item_id): item
            for item in itinerary.visit_stops
        },
        **{
            (EntityType.DINING_STOP, item.item_id): item
            for item in itinerary.dining_stops
        },
        **{
            (EntityType.LODGING_STAY, item.stay_id): item
            for item in itinerary.lodging_stays
        },
    }
    candidate_entities: dict[str, list[str]] = {}
    for (entity_type, entity_id), entity in physical_entities.items():
        # A lodging stay intentionally projects on both check-in and check-out
        # Days; it is not the visit/dining reuse invariant this gate owns.
        if entity_type == EntityType.LODGING_STAY:
            continue
        if entity.lineage.lineage_kind != "candidate_entity":
            continue
        candidate_entities.setdefault(entity.lineage.candidate_id, []).append(entity_id)
    for candidate_id, entity_ids in candidate_entities.items():
        if len(entity_ids) > 1:
            gaps.append(
                _gap(
                    gate="itinerary",
                    reason="physical_candidate_reused",
                    field_path=f"itinerary.physical_candidates.{candidate_id}",
                    retry_target="composition_repair",
                    entity_id=entity_ids[-1],
                    candidate_id=candidate_id,
                    repair_context={"occurrence_entity_ids": entity_ids},
                    gate_class=GateClass.COMPOSITION,
                )
            )

    transport_index = {item.transport_leg_id: item for item in itinerary.transport_legs}
    physical_types = {
        EntityType.VISIT_STOP,
        EntityType.DINING_STOP,
        EntityType.LODGING_STAY,
    }
    for day in itinerary.day_plans:
        previous_physical: tuple[EntityType, Any] | None = None
        local_connectors = []
        for ref in day.timeline:
            if ref.entity_type == EntityType.TRANSPORT_LEG:
                leg = transport_index.get(ref.entity_id)
                if leg and leg.transport_class in {"public_transit", "flexible"}:
                    local_connectors.append(leg)
            elif ref.entity_type in physical_types:
                current_physical = physical_entities.get((ref.entity_type, ref.entity_id))
                if previous_physical is not None and current_physical is not None:
                    previous_type, previous_entity = previous_physical
                    # "How does the traveller get from dinner to the hotel" is a
                    # real routing question, so a lodging endpoint stays in the
                    # adjacency walk and its missing connector stays in the gap
                    # list. It must not block, though: a lodging stay is not a
                    # composition placement, so the composer never plans a
                    # connector to it and no repair round can produce one. A
                    # blocking gap here would burn the run's single composition
                    # repair on work that cannot be done, on nearly every run.
                    # A stop-to-stop adjacency is different: both ends are
                    # placements the composer owns, so a missing connector there
                    # is a defect the repair round can actually fix, and it
                    # keeps blocking.
                    lodging_endpoint = EntityType.LODGING_STAY in {
                        previous_type,
                        ref.entity_type,
                    }
                    has_ready_exact_connector = any(
                        leg.route_status == "ready"
                        and leg.from_endpoint.place_id == previous_entity.place_id
                        and leg.to_endpoint.place_id == current_physical.place_id
                        for leg in local_connectors
                    )
                    if local_connectors and not has_ready_exact_connector:
                        leg = local_connectors[-1]
                        gaps.append(
                            _gap(
                                gate="itinerary",
                                reason="route_endpoint_mismatch",
                                field_path=f"itinerary.transport_legs.{leg.transport_leg_id}.endpoints",
                                retry_target="composition_repair",
                                entity_id=leg.transport_leg_id,
                                candidate_id=leg.lineage.candidate_id,
                                worker_kind="transport_researcher",
                                repair_context={
                                    "from_entity_id": getattr(previous_entity, "item_id", getattr(previous_entity, "stay_id", None)),
                                    "from_candidate_id": previous_entity.lineage.candidate_id,
                                    "from_place_id": previous_entity.place_id,
                                    "from_name": getattr(previous_entity, "name", getattr(previous_entity, "property_name", None)),
                                    "to_entity_id": getattr(current_physical, "item_id", getattr(current_physical, "stay_id", None)),
                                    "to_candidate_id": current_physical.lineage.candidate_id,
                                    "to_place_id": current_physical.place_id,
                                    "to_name": getattr(current_physical, "name", getattr(current_physical, "property_name", None)),
                                },
                                blocking=not lodging_endpoint,
                                gate_class=GateClass.COMPOSITION,
                                research_domain=ResearchDomain.LOCAL_TRANSPORT,
                            )
                        )
                    elif not local_connectors:
                        gaps.append(
                            _gap(
                                gate="itinerary",
                                reason="route_infeasible",
                                field_path=f"itinerary.day_plans.{day.day_id}.timeline",
                                retry_target="composition_repair",
                                entity_id=ref.entity_id,
                                blocking=not lodging_endpoint,
                                gate_class=GateClass.COMPOSITION,
                                research_domain=ResearchDomain.LOCAL_TRANSPORT,
                            )
                        )
                if current_physical is not None:
                    previous_physical = (ref.entity_type, current_physical)
                local_connectors = []

    for index in range(1, len(itinerary.day_plans)):
        previous = itinerary.day_plans[index - 1]
        current = itinerary.day_plans[index]
        if previous.destination_id == current.destination_id:
            continue
        transition_legs = [
            transport_index[ref.entity_id]
            for day in (previous, current)
            for ref in day.timeline
            if ref.entity_type == EntityType.TRANSPORT_LEG and ref.entity_id in transport_index
        ]
        if not any(leg.transport_class == "long_distance" for leg in transition_legs):
            gaps.append(
                _gap(
                    gate="itinerary",
                    reason="destination_transfer_missing",
                    field_path=f"itinerary.day_plans.{current.day_id}.timeline",
                    retry_target="composition_repair",
                    entity_id=current.day_id,
                    gate_class=GateClass.COMPOSITION,
                    research_domain=ResearchDomain.LONG_DISTANCE_TRANSPORT,
                )
            )
    return gaps


def _source_weather_gaps(state: TravelAgentState) -> list[DeliveryQualityGap]:
    """Record source and weather coverage per entity.

    Sources and weather are what an entry shows on top of itself, not the
    condition of its existence, so coverage and freshness are recorded and
    shipped. The exception is a lineage id the projection cannot dereference
    at all: that is a broken pointer, and it blocks.
    """

    workspace = state.trip_workspace_v2
    if workspace is None:
        return []
    catalog = workspace.recommendation_catalog
    packet_by_candidate = {
        candidate.candidate_id: packet
        for packet in catalog.research_packets
        for candidate in packet.candidates
    }
    # The projection resolves lineage ids against the whole catalog, so a fact
    # or source living in a sibling packet still renders.
    catalog_fact_ids = {
        fact.fact_assertion_id
        for packet in catalog.research_packets
        for fact in packet.fact_assertions
    }
    catalog_source_ids = {
        source.source_record_id
        for packet in catalog.research_packets
        for source in packet.source_records
    }
    weather_facts = {fact.fact_assertion_id for fact in state.weather_fact_assertions}
    impacts = state.weather_impacts
    gaps: list[DeliveryQualityGap] = []
    entities = [
        *workspace.itinerary.visit_stops,
        *workspace.itinerary.dining_stops,
        *workspace.itinerary.lodging_stays,
        *workspace.itinerary.transport_legs,
    ]
    for entity in entities:
        if entity.lineage.lineage_kind != "candidate_entity":
            # An authored entry has no packet: its place identity comes from the
            # global place provider, not from research evidence.
            continue
        candidate_id = entity.lineage.candidate_id
        packet = packet_by_candidate.get(candidate_id)
        if packet is None:
            gaps.append(
                _gap(
                    gate="integrity",
                    reason="catalog_missing",
                    field_path=f"entities.{candidate_id}",
                    retry_target="composition_repair",
                    entity_id=getattr(entity, "item_id", getattr(entity, "stay_id", getattr(entity, "transport_leg_id", None))),
                    gate_class=GateClass.COMPOSITION,
                )
            )
            continue
        facts = {fact.fact_assertion_id: fact for fact in packet.fact_assertions}
        sources = {source.source_record_id for source in packet.source_records}
        candidate = next(
            (item for item in packet.candidates if item.candidate_id == candidate_id),
            None,
        )
        domain = {
            "visit": ResearchDomain.VISIT,
            "dining": ResearchDomain.DINING,
            "lodging": ResearchDomain.LODGING,
            "transport": ResearchDomain.LOCAL_TRANSPORT,
        }.get(candidate.candidate_kind if candidate is not None else "")
        if candidate is not None and getattr(candidate, "transport_class", None) == "long_distance":
            domain = ResearchDomain.LONG_DISTANCE_TRANSPORT
        for fact_id in entity.lineage.fact_assertion_ids:
            fact = facts.get(fact_id)
            if fact is None or fact.status != "verified":
                gaps.append(
                    _gap(
                        gate="source_weather",
                        reason="fact_not_verified",
                        field_path=f"entities.{candidate_id}.facts.{fact_id}",
                        retry_target="composition_repair",
                        entity_id=getattr(entity, "item_id", getattr(entity, "stay_id", getattr(entity, "transport_leg_id", None))),
                        candidate_id=candidate_id,
                        worker_kind=packet.worker_kind,
                        # An unverified or stale fact projects with its own
                        # status; one the catalog does not hold cannot project.
                        blocking=fact_id not in catalog_fact_ids,
                        gate_class=GateClass.COMPOSITION,
                        research_domain=domain,
                    )
                )
                continue
            unsourced = [
                link.source_record_id
                for link in fact.source_links
                if link.relation == "supports" and link.source_record_id not in sources
            ]
            if unsourced:
                gaps.append(
                    _gap(
                        gate="source_weather",
                        reason="source_missing",
                        field_path=f"entities.{candidate_id}.facts.{fact_id}.source_links",
                        retry_target="composition_repair",
                        entity_id=getattr(entity, "item_id", getattr(entity, "stay_id", getattr(entity, "transport_leg_id", None))),
                        candidate_id=candidate_id,
                        worker_kind=packet.worker_kind,
                        blocking=any(
                            source_id not in catalog_source_ids for source_id in unsourced
                        ),
                        gate_class=GateClass.COMPOSITION,
                        research_domain=domain,
                    )
                )
        for impact_id in entity.lineage.weather_impact_ids:
            impact = impacts.get(impact_id)
            if impact is None or not set(impact.fact_assertion_ids) <= weather_facts:
                gaps.append(
                    _gap(
                        gate="source_weather",
                        reason="weather_lineage_missing",
                        field_path=f"entities.{candidate_id}.weather_impact_ids.{impact_id}",
                        retry_target="composition_repair",
                        entity_id=getattr(entity, "item_id", getattr(entity, "stay_id", getattr(entity, "transport_leg_id", None))),
                        candidate_id=candidate_id,
                        worker_kind=packet.worker_kind,
                        blocking=False,
                        gate_class=GateClass.COMPOSITION,
                        research_domain=domain,
                    )
                )

    if state.weather_context is not None:
        for day in state.weather_context.days:
            if day.data_kind == "unavailable":
                gaps.append(
                    _gap(
                        gate="source_weather",
                        reason="weather_unavailable_refreshable",
                        field_path=f"weather.days.{day.destination_id}.{day.date.isoformat()}",
                        retry_target="composition_repair",
                        entity_id=f"{day.destination_id}:{day.date.isoformat()}",
                        blocking=False,
                        gate_class=GateClass.COMPOSITION,
                    )
                )
    return gaps


def evaluate_delivery_quality(state: TravelAgentState) -> tuple[RecommendationQualityState, list[DeliveryQualityGap]]:
    if state.trip_workspace_v2 is None or state.recommendation_catalog is None:
        missing = "workspace_missing" if state.trip_workspace_v2 is None else "catalog_missing"
        gap = _gap(
            gate="integrity",
            reason=missing,
            field_path=missing,
            retry_target="composition_repair",
            gate_class=GateClass.COMPOSITION,
        )
        return (
            RecommendationQualityState(
                schema_gate="failed",
                candidate_gate="passed",
                slot_gate="pending",
                itinerary_gate="pending",
                source_weather_gate="pending",
                active_gap_ids=[gap.gap_id],
            ),
            [gap],
        )
    slot = _slot_gaps(state)
    itinerary = _itinerary_gaps(state)
    source_weather = _source_weather_gaps(state)
    gaps = [*slot, *itinerary, *source_weather]
    quality = RecommendationQualityState(
        schema_gate="passed",
        # A workspace that reached this gate carries the current catalog; the
        # admission subgate reports that rather than re-deciding it.
        candidate_gate="passed",
        slot_gate="failed" if any(gap.blocking for gap in slot) else "passed",
        itinerary_gate="failed" if any(gap.blocking for gap in itinerary) else "passed",
        source_weather_gate="failed" if any(gap.blocking for gap in source_weather) else "passed",
        active_gap_ids=[gap.gap_id for gap in gaps],
    )
    return quality, gaps


_RELEASED_SUBGATES = {
    "schema_gate": "passed",
    "candidate_gate": "passed",
    "slot_gate": "passed",
    "itinerary_gate": "passed",
    "source_weather_gate": "passed",
}


def _composition_window_closed(state: TravelAgentState) -> bool:
    """Whether the itinerary composition window has run out for this run."""
    if state.run_deadline is None:
        return False
    _observed, observation = observe_run_deadline(state.run_deadline)
    return observation.composition_closed


async def delivery_quality_gate_node(state: TravelAgentState) -> Dict[str, Any]:
    quality, gaps = evaluate_delivery_quality(state)
    blocking = [gap for gap in gaps if gap.blocking]
    update: Dict[str, Any] = {
        "recommendation_quality": quality,
        "delivery_quality_gaps": gaps,
    }
    if state.trip_workspace_v2 is None:
        # There is no composition to release. Releasing on a spent budget is the
        # right answer to an open quality gap; it is not an answer to a
        # composition that was never materialized, so this gate spends the
        # repair round on materialization instead and fails loudly once either
        # that round or the composition window is gone.  The window is checked
        # first because it binds harder: with the clock gone no remaining repair
        # round could have composed anything, so it names the real cause.
        if _composition_window_closed(state):
            raise DeliveryContractViolation(
                "composition window closed with no materialized workspace",
                reason_code="composition_window_exhausted",
                gate_class=GateClass.COMPOSITION,
            )
        if composition_repair_budget_exhausted(state):
            raise DeliveryContractViolation(
                "delivery quality gate reached with no materialized workspace",
                reason_code="composition_repair_budget_exhausted",
                gate_class=GateClass.COMPOSITION,
            )
        update.update(
            delivery_quality_route="composition_repair",
            composition_repair_attempts=composition_repair_attempts(state) + 1,
            gate_failure_attributions=_attribution(
                state,
                gate_class=GateClass.COMPOSITION,
                disposition=GateDisposition.COMPOSITION_REPAIR,
                reason_code="delivery_quality_workspace_unmaterialized",
                gaps=blocking,
            ),
        )
        return update
    if not blocking or composition_repair_budget_exhausted(state):
        # Either nothing structural is open, or the single repair round is
        # spent. Both release the current composition to projection, which
        # reads every subgate as passed.
        update["recommendation_quality"] = quality.model_copy(update=_RELEASED_SUBGATES)
        update["delivery_quality_route"] = "passed"
        return update

    update.update(
        delivery_quality_route="composition_repair",
        gate_failure_attributions=_attribution(
            state,
            gate_class=GateClass.COMPOSITION,
            disposition=GateDisposition.COMPOSITION_REPAIR,
            reason_code="delivery_quality_composition_gap",
            gaps=blocking,
        ),
    )
    return apply_composition_repair_budget(
        state,
        update,
        route_key="delivery_quality_route",
        exhausted_route="passed",
    )


def route_after_delivery_quality_gate(state: TravelAgentState) -> str:
    """Route to projection unless the node granted this run's one repair round.

    The budget is enforced at write time in the node, so this edge must not
    re-read ``composition_repair_attempts``: the granted round already set it
    to one, and folding here would cancel the hop the node just paid for.
    """

    if state.delivery_quality_route != "composition_repair":
        return "passed"
    deadline = state.run_deadline
    if deadline is None:
        return "composition_repair"
    _observed, observation = observe_run_deadline(deadline)
    if observation.composition_closed:
        # Past its own window the current composition ships as-is; repair would
        # open another itinerary model call. A run with nothing materialized
        # never reaches here — the node raises instead of shipping an empty
        # projection.
        return "passed"
    return "composition_repair"
