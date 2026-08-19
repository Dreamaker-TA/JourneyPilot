"""The one audience-safe projection of a formal JourneyPilot delivery artifact.

The durable ``DeliveryBundle`` is intentionally richer than the consumer
contract: it carries source snapshots, cache provenance and research ledger data
needed for replay and evaluation.  This module is the single boundary that turns
one into the other, and it does so by **constructing** the public payload field
by field.

**Never censor instead.**  A recursive walk that drops any mapping key whose name
matches a blacklist or contains one of ``provider`` / ``cache`` / ``snapshot`` /
``attempt`` / ``gap`` / ``raw_`` fails in both directions: it silently eats legitimate
product fields whose names happen to collide (the guided-intake ``raw_input`` a live
consumer reads is one) while a genuinely internal field named without one of those
tokens sails through — and neither direction raises, logs, or fails a test.
Construction cannot have either failure mode: a field reaches a traveller because this
module names it, and adding one to the Bundle is visible as a diff here and in
``frontend/src/types/delivery.ts``.

The module lives in ``services/`` rather than ``api/`` because the delivery
finalizer must be able to dry-run the projection before committing a Bundle, and
``workflows/`` may not import ``api/`` (see ``entities/evidence_basis.py``).  The
response *model* ``PublicDeliveryBundleResponse`` stays in the api layer: it
validates the wire encoding, which is an api concern.
"""

from __future__ import annotations

from datetime import date as Date, datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..entities.delivery_bundle import (
    CostCoverageSummary,
    CustomBlock,
    DayPlanV2,
    DeliveryBundle,
    DiningStop,
    EntityType,
    LodgingStay,
    MapProjection,
    PublicCitationProjection,
    ReportDaySection,
    ReportEntityBlock,
    ReportSelectionSection,
    ReportWeatherDay,
    SelectionSlot,
    SourceIndexProjection,
    StructuredItineraryV2,
    TransportLeg,
    TripReportDocument,
    TripReportProjection,
    TripWorkspaceV2,
    VisitStop,
    WeatherProposalDecision,
)
from ..entities.coverage_disclosure import coverage_disclosure_notes
from ..entities.evidence_basis import EvidenceBasisView
from ..entities.provider_environment import ProviderEnvironmentView


def _timestamp(value: Optional[datetime]) -> Optional[str]:
    """Encode an instant exactly as the rest of the Bundle's JSON does.

    Pydantic's JSON mode emits RFC 3339 with a ``Z`` for UTC where
    ``datetime.isoformat()`` emits ``+00:00``.  The two are equivalent instants
    but not equal strings, and clients diff these payloads.
    """

    if value is None:
        return None
    text = value.isoformat()
    return f"{text[:-6]}Z" if text.endswith("+00:00") else text


def _day(value: Optional[Date]) -> Optional[str]:
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
# Value objects.  These types hold nothing but the product facts they describe —
# an endpoint, a segment, a coordinate, a cost roll-up — so the whole typed
# model is the public shape.  Adding an internal field to one of them would be a
# contract change reviewed here, not a name the projection has to outguess.
# ---------------------------------------------------------------------------


def _public_endpoint(endpoint: Any) -> Dict[str, Any]:
    return endpoint.model_dump(mode="json")


def _public_segments(segments: Sequence[Any]) -> List[Dict[str, Any]]:
    return [segment.model_dump(mode="json") for segment in segments]


def _public_entity_ref(entity_ref: Any) -> Dict[str, Any]:
    return entity_ref.model_dump(mode="json")


def _public_cost_summary(summary: CostCoverageSummary) -> Dict[str, Any]:
    return summary.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Itinerary entities.  Each of the four lineage-bearing kinds gets its own
# constructor: ``lineage`` is absent because nothing names it, and the two
# derived product fields (``evidence_basis``, ``is_micro_transport``) are stated
# where a reader needs them.
# ---------------------------------------------------------------------------


def _public_visit_stop(stop: VisitStop, evidence_basis: str) -> Dict[str, Any]:
    return {
        "type": stop.type,
        "item_id": stop.item_id,
        "day_id": stop.day_id,
        "place_id": stop.place_id,
        "name": stop.name,
        "address": stop.address,
        "latitude": stop.latitude,
        "longitude": stop.longitude,
        "planned_start": _timestamp(stop.planned_start),
        "planned_end": _timestamp(stop.planned_end),
        "duration_minutes": stop.duration_minutes,
        "estimated_cost_cny": stop.estimated_cost_cny,
        "selection_reason": stop.selection_reason,
        "visit_type": stop.visit_type,
        "opening_window": stop.opening_window,
        "reservation_required": stop.reservation_required,
        "visit_highlights": list(stop.visit_highlights),
        "evidence_basis": evidence_basis,
    }


def _public_dining_stop(stop: DiningStop, evidence_basis: str) -> Dict[str, Any]:
    return {
        "type": stop.type,
        "item_id": stop.item_id,
        "day_id": stop.day_id,
        "place_id": stop.place_id,
        "name": stop.name,
        "address": stop.address,
        "latitude": stop.latitude,
        "longitude": stop.longitude,
        "planned_start": _timestamp(stop.planned_start),
        "planned_end": _timestamp(stop.planned_end),
        "duration_minutes": stop.duration_minutes,
        "estimated_cost_cny": stop.estimated_cost_cny,
        "selection_reason": stop.selection_reason,
        "meal_type": stop.meal_type,
        "cuisine_types": list(stop.cuisine_types),
        "average_spend_cny": stop.average_spend_cny,
        "recommended_dishes": list(stop.recommended_dishes),
        "reservation_required": stop.reservation_required,
        "opening_window": stop.opening_window,
        "dining_reminders": list(stop.dining_reminders),
        "evidence_basis": evidence_basis,
    }


def _public_lodging_stay(stay: LodgingStay, evidence_basis: str) -> Dict[str, Any]:
    return {
        "type": stay.type,
        "stay_id": stay.stay_id,
        "place_id": stay.place_id,
        "name": stay.name,
        "check_in_date": _day(stay.check_in_date),
        "check_out_date": _day(stay.check_out_date),
        "check_in_time": stay.check_in_time,
        "check_out_time": stay.check_out_time,
        "nights": stay.nights,
        "room_type": stay.room_type,
        "nightly_price_cny": stay.nightly_price_cny,
        "total_price_cny": stay.total_price_cny,
        "price_kind": stay.price_kind,
        "availability_status": stay.availability_status,
        "address": stay.address,
        "selection_reason": stay.selection_reason,
        "evidence_basis": evidence_basis,
    }


def _public_transport_leg(
    leg: TransportLeg, evidence_basis: str, *, is_micro_transport: bool
) -> Dict[str, Any]:
    """Project one leg, including the connector verdict.

    *Which* legs are connectors is domain judgement over Bundle fields, not a
    threshold a reader should re-apply, so the projection ships the verdict as
    ``is_micro_transport``: the server-rendered PDF reads
    :meth:`EvidenceBasisView.stated_basis_for` while the browser reads this
    field, and the two must not be able to reach different conclusions about the
    same leg.
    """

    return {
        "type": leg.type,
        "transport_leg_id": leg.transport_leg_id,
        "transport_class": leg.transport_class,
        "selected_mode": leg.selected_mode.value,
        "from_endpoint": _public_endpoint(leg.from_endpoint),
        "to_endpoint": _public_endpoint(leg.to_endpoint),
        "departure_at": _timestamp(leg.departure_at),
        "arrival_at": _timestamp(leg.arrival_at),
        "duration_minutes": leg.duration_minutes,
        "distance_meters": leg.distance_meters,
        "total_cost_cny": leg.total_cost_cny,
        "transfer_count": leg.transfer_count,
        "segments": _public_segments(leg.segments),
        "booking_status": leg.booking_status,
        "route_status": leg.route_status,
        "mode_preference": leg.mode_preference.model_dump(mode="json"),
        "evidence_basis": evidence_basis,
        "is_micro_transport": is_micro_transport,
    }


def _public_custom_block(block: CustomBlock) -> Dict[str, Any]:
    """A traveller's own arrangement: no lineage, and so no evidence basis."""

    return {
        "type": block.type,
        "item_id": block.item_id,
        "day_id": block.day_id,
        "title": block.title,
        "note": block.note,
        "planned_start": _timestamp(block.planned_start),
        "planned_end": _timestamp(block.planned_end),
        "duration_minutes": block.duration_minutes,
    }


def _public_day_plan(day: DayPlanV2) -> Dict[str, Any]:
    return {
        "day_id": day.day_id,
        "day": day.day,
        "date": _day(day.date),
        "destination_id": day.destination_id,
        "theme": day.theme,
        "timeline": [
            {
                "entry_id": entry.entry_id,
                "entity_type": entry.entity_type.value,
                "entity_id": entry.entity_id,
                "projection_role": entry.projection_role,
            }
            for entry in day.timeline
        ],
        "time_structure": list(day.time_structure),
        "estimated_cost_cny": day.estimated_cost_cny,
    }


def _public_itinerary(
    itinerary: StructuredItineraryV2, view: EvidenceBasisView
) -> Dict[str, Any]:
    """Project the canonical itinerary and state each entry's evidence basis.

    Every lineage-bearing entry carries ``evidence_basis``, including micro
    connectors: suppressing the *statement* on a connector is a rendering rule
    each artifact applies through :meth:`EvidenceBasisView.stated_basis_for`, not
    a hole in the transport contract.
    """

    return {
        "itinerary_id": itinerary.itinerary_id,
        "title": itinerary.title,
        "destination_ids": list(itinerary.destination_ids),
        "duration_days": itinerary.duration_days,
        "day_plans": [_public_day_plan(day) for day in itinerary.day_plans],
        "visit_stops": [
            _public_visit_stop(
                stop, view.basis_for(EntityType.VISIT_STOP, stop.item_id)
            )
            for stop in itinerary.visit_stops
        ],
        "dining_stops": [
            _public_dining_stop(
                stop, view.basis_for(EntityType.DINING_STOP, stop.item_id)
            )
            for stop in itinerary.dining_stops
        ],
        "lodging_stays": [
            _public_lodging_stay(
                stay, view.basis_for(EntityType.LODGING_STAY, stay.stay_id)
            )
            for stay in itinerary.lodging_stays
        ],
        "transport_legs": [
            _public_transport_leg(
                leg,
                view.basis_for(EntityType.TRANSPORT_LEG, leg.transport_leg_id),
                is_micro_transport=leg.transport_leg_id in view.micro_transport_leg_ids,
            )
            for leg in itinerary.transport_legs
        ],
        "custom_blocks": [
            _public_custom_block(block) for block in itinerary.custom_blocks
        ],
        "cost_summary": _public_cost_summary(itinerary.cost_summary),
        "highlights": list(itinerary.highlights),
        "important_notes": list(itinerary.important_notes),
    }


# ---------------------------------------------------------------------------
# Citations and sources.
# ---------------------------------------------------------------------------


def _public_citation(citation: PublicCitationProjection) -> Dict[str, Any]:
    """Project a citation without provider identity or raw RAG/source payloads."""

    return {
        "citation_id": citation.citation_id,
        "entity_ref": _public_entity_ref(citation.entity_ref),
        "field_paths": list(citation.field_paths),
        "fact_status": citation.fact_status,
        "supported_values": [
            value.model_dump(mode="json") for value in citation.supported_values
        ],
        "sources": [
            {
                "source_record_id": source.source_record_id,
                "source_kind": source.source_kind,
                "title": source.title,
                "public_excerpt": source.public_excerpt,
                "canonical_url": source.canonical_url,
                "retrieved_at": source.retrieved_at.isoformat(),
                "observed_at": source.observed_at.isoformat()
                if source.observed_at is not None
                else None,
            }
            for source in citation.sources
        ],
    }


# ---------------------------------------------------------------------------
# Report projection.  The report JSON and the workspace cards are two renderings
# of one itinerary, so a reader must not be able to find a sourced claim in one
# and an authored one in the other.
# ---------------------------------------------------------------------------


def _public_report_block(
    block: ReportEntityBlock, view: EvidenceBasisView
) -> Dict[str, Any]:
    """Project one report block.

    ``weather_impact_ids`` / ``personalization_influence_ids`` are deliberately
    absent: they are internal ledger references with no product meaning.  A
    ``custom`` block states no basis, for the same reason it states none in the
    workspace.
    """

    payload: Dict[str, Any] = {
        "entity_ref": _public_entity_ref(block.entity_ref),
        "day_id": block.day_id,
        "projection_role": block.projection_role,
        "title": block.title,
        "entity_kind": block.entity_kind,
        "summary": block.summary,
        # ``details`` is authored by ``services/delivery_projection.py`` from the
        # already-public itinerary fields; it is a projection product, not a
        # window back into the Bundle internals.
        "details": dict(block.details),
        "citation_ids": list(block.citation_ids),
    }
    if block.entity_kind != "custom":
        payload["evidence_basis"] = view.basis_for(
            EntityType(block.entity_ref.entity_type), block.entity_ref.entity_id
        )
    return payload


def _public_report_day(day: ReportDaySection, view: EvidenceBasisView) -> Dict[str, Any]:
    return {
        "day_id": day.day_id,
        "day": day.day,
        "date": _day(day.date),
        "destination_id": day.destination_id,
        "destination_name": day.destination_name,
        "theme": day.theme,
        "blocks": [_public_report_block(block, view) for block in day.blocks],
    }


def _public_report_selection(section: ReportSelectionSection) -> Dict[str, Any]:
    """Project one selection section without the internal candidate identity.

    ``candidate_id`` names a row in the recommendation catalog; the option is
    already identified to a reader by ``option_id`` and ``name``.
    """

    return {
        "selection_slot_id": section.selection_slot_id,
        "slot_type": section.slot_type,
        "context": dict(section.context),
        "status": section.status,
        "options": [
            {
                "option_id": option.option_id,
                "name": option.name,
                "rank": option.rank,
                "selected": option.selected,
                "recommended": option.recommended,
                "selection_reasons": list(option.selection_reasons),
                "tradeoff": option.tradeoff,
                "comparison_facts": list(option.comparison_facts),
                "availability_status": option.availability_status,
                "citation_ids": list(option.citation_ids),
            }
            for option in section.options
        ],
    }


def _public_report_weather_day(day: ReportWeatherDay) -> Dict[str, Any]:
    return {
        "destination_id": day.destination_id,
        "destination_name": day.destination_name,
        "date": _day(day.date),
        "data_kind": day.data_kind,
        # The freshness pair.  It is declared on the client type and stored on the
        # Bundle, so it has to go on the wire too — leave it off and the browser has
        # the field while the server never fills it, making a Run whose refresh has
        # been refusing for days look identical to one refreshed a minute ago.
        "observed_at": day.observed_at.isoformat() if day.observed_at is not None else None,
        "weather_data_state": day.weather_data_state,
        "condition_label": day.condition_label,
        "high_c": day.high_c,
        "low_c": day.low_c,
        "precipitation_probability_pct": day.precipitation_probability_pct,
        "wind_speed_kph": day.wind_speed_kph,
        "citation_ids": list(day.citation_ids),
    }


def _public_report_document(
    document: TripReportDocument, view: EvidenceBasisView
) -> Dict[str, Any]:
    return {
        "title": document.title,
        "overview": document.overview,
        "destinations": [
            item.model_dump(mode="json") for item in document.destinations
        ],
        "duration_days": document.duration_days,
        "cost_summary": _public_cost_summary(document.cost_summary),
        # The money sentence, computed once during projection
        # (``entities/cost_coverage.py``).  It was missing from this dict while
        # both browser surfaces read it and the PDF read the internal model, so
        # the same plan printed 「已知费用 ¥1,765 · 整趟预算估算 ¥2,788」 in the
        # exported PDF and nothing at all in the report cover and the workspace
        # overview — the exact drift centralising it was meant to end.
        "cost_coverage_statement": document.cost_coverage_statement,
        "days": [_public_report_day(day, view) for day in document.days],
        "selections": [
            _public_report_selection(section) for section in document.selections
        ],
        "weather": [_public_report_weather_day(day) for day in document.weather],
        "highlights": list(document.highlights),
        "important_notes": list(document.important_notes),
    }


def _public_report(
    report: TripReportProjection, view: EvidenceBasisView
) -> Dict[str, Any]:
    """Project the formal report.

    ``failure_reason`` is absent by construction: a ready report is the formal
    product artifact and failure strings belong to integrity diagnostics.
    """

    return {
        "source_workspace_revision": report.source_workspace_revision,
        "source_fact_data_revision": report.source_fact_data_revision,
        "source_weather_data_revision": report.source_weather_data_revision,
        "status": report.status,
        "document": (
            _public_report_document(report.document, view)
            if report.document is not None
            else None
        ),
        "citations": [_public_citation(item) for item in report.citations],
        "generated_at": _timestamp(report.generated_at),
    }


# ---------------------------------------------------------------------------
# Map and source index.
# ---------------------------------------------------------------------------


def _public_map(projection: MapProjection) -> Dict[str, Any]:
    """Project the map.  Geometry only — the evidence claim belongs to the cards."""

    return {
        "source_workspace_revision": projection.source_workspace_revision,
        "content": {
            "places": [
                {
                    "entity_ref": _public_entity_ref(place.entity_ref),
                    "name": place.name,
                    "place_id": place.place_id,
                    "latitude": place.latitude,
                    "longitude": place.longitude,
                    "citation_ids": list(place.citation_ids),
                }
                for place in projection.content.places
            ],
            "routes": [
                {
                    "entity_ref": _public_entity_ref(route.entity_ref),
                    "transport_class": route.transport_class,
                    "selected_mode": route.selected_mode.value,
                    "route_status": route.route_status,
                    "from_endpoint": _public_endpoint(route.from_endpoint),
                    "to_endpoint": _public_endpoint(route.to_endpoint),
                    "segments": _public_segments(route.segments),
                    "citation_ids": list(route.citation_ids),
                }
                for route in projection.content.routes
            ],
        },
    }


def _public_source_index(projection: SourceIndexProjection) -> Dict[str, Any]:
    return {
        "source_fact_data_revision": projection.source_fact_data_revision,
        "content": {
            "citations": [
                _public_citation(item) for item in projection.content.citations
            ]
        },
    }


# ---------------------------------------------------------------------------
# Workspace.
# ---------------------------------------------------------------------------


def _public_selection_slot(slot: SelectionSlot) -> Dict[str, Any]:
    return {
        "selection_slot_id": slot.selection_slot_id,
        "slot_type": slot.slot_type,
        "target_entity_id": slot.target_entity_id,
        "context": dict(slot.context),
        "options": [
            {
                "option_id": option.option_id,
                "rank": option.rank,
                "selected": option.option_id == slot.selected_option_id,
                "recommended": option.option_id == slot.recommended_option_id,
                "selection_reasons": list(option.selection_reasons),
                "tradeoff": option.tradeoff,
                "comparison_facts": list(option.comparison_facts),
                "availability_status": option.availability_status,
            }
            for option in slot.options
        ],
        "status": slot.status,
    }


def _public_weather_proposal_decision(
    decision: WeatherProposalDecision,
) -> Dict[str, Any]:
    return {"proposal_id": decision.proposal_id, "decision": decision.decision}


def _public_weather_adjustments(bundle: DeliveryBundle) -> List[Dict[str, Any]]:
    """Expose pending weather choices without exposing the raw weather ledger.

    A weather refresh is a user-initiated product action.  The client needs a
    stable proposal id and a concise, actionable summary in order to confirm or
    dismiss it, but it must not receive provider observations, raw operations,
    or fact identifiers from the immutable weather snapshot.
    """

    decisions = {
        item.proposal_id: item.decision
        for item in bundle.workspace.weather_proposal_decisions
    }
    return [
        {
            "proposal_id": proposal.proposal_id,
            "date": _day(proposal.date),
            "severity": proposal.severity,
            "summary": proposal.summary,
            "cost_delta_cny": proposal.cost_delta_cny,
            "time_delta_minutes": proposal.time_delta_minutes,
            "status": decisions.get(proposal.proposal_id, "pending"),
        }
        for proposal in bundle.weather_snapshot.adjustment_proposals
    ]


def _public_workspace(
    workspace: TripWorkspaceV2, bundle: DeliveryBundle, view: EvidenceBasisView
) -> Dict[str, Any]:
    """Project the workspace.

    ``recommendation_catalog``, ``user_input_anchors`` and
    ``personalization_influences`` are absent by construction: the catalog is the
    research ledger, the anchors are already restated as report notes, and the
    influences are internal ranking references.
    """

    return {
        "contract_version": workspace.contract_version,
        "run_id": workspace.run_id,
        "generation_id": workspace.generation_id,
        "workspace_revision": workspace.workspace_revision,
        "itinerary": _public_itinerary(workspace.itinerary, view),
        "selection_slots": [
            _public_selection_slot(slot) for slot in workspace.selection_slots
        ],
        "weather_proposal_decisions": [
            _public_weather_proposal_decision(item)
            for item in workspace.weather_proposal_decisions
        ],
        "weather_adjustments": _public_weather_adjustments(bundle),
    }


# ---------------------------------------------------------------------------
# Manifest.
# ---------------------------------------------------------------------------

_PUBLIC_MANIFEST_KEYS = (
    "contract_version",
    "run_id",
    "generation_id",
    "bundle_id",
    "workspace_revision",
    "fact_data_revision",
    "weather_data_revision",
    "created_at",
)


def public_manifest(bundle: DeliveryBundle) -> Dict[str, Any]:
    manifest = bundle.manifest
    return {
        "contract_version": manifest.contract_version,
        "run_id": manifest.run_id,
        "generation_id": manifest.generation_id,
        "bundle_id": manifest.bundle_id,
        "workspace_revision": manifest.workspace_revision,
        "fact_data_revision": manifest.fact_data_revision,
        "weather_data_revision": manifest.weather_data_revision,
        "created_at": _timestamp(manifest.created_at),
    }


def public_event_manifest(manifest: Any) -> Dict[str, Any]:
    """Coerce a persisted manifest payload to the public manifest subset.

    The input is a durable event payload rather than a typed model, so the
    projection reads the named keys it knows and ignores everything else.
    """

    if not isinstance(manifest, Mapping):
        return {}
    return {
        key: manifest[key] for key in _PUBLIC_MANIFEST_KEYS if key in manifest
    }


def public_delivery_bundle(bundle: DeliveryBundle) -> Dict[str, Any]:
    """Return the only DeliveryBundle shape valid on normal product surfaces.

    The persisted ``EntityLineage`` never crosses this boundary, so without a
    derived field a traveller cannot tell an entry backed by an admitted research
    candidate from one the Itinerary Planner authored against public knowledge —
    the two render identically while only one can expand its sources.
    ``evidence_basis`` states that difference in product language and is derived
    once here, so the workspace card and the formal report can never disagree
    about the same entity.
    """

    view = EvidenceBasisView.from_itinerary(bundle.workspace.itinerary)
    return {
        "manifest": public_manifest(bundle),
        "workspace": _public_workspace(bundle.workspace, bundle, view),
        "report_projection": _public_report(bundle.report_projection, view),
        "map_projection": _public_map(bundle.map_projection),
        "source_index": _public_source_index(bundle.source_index),
        # Sentences, not domain enums: the structured field stays internal and the
        # single sentence table (``entities/coverage_disclosure.py``) is shared with
        # the PDF, which reads the Bundle directly and never passes through here.
        "coverage_disclosure": {
            "notes": coverage_disclosure_notes(bundle.coverage_disclosure)
        },
        # Data-driven, not hardcoded copy: the interactive transport card used to
        # state this from a literal string, which would have become a permanent
        # untruth in an exported PDF the day a live key was installed — and never
        # appeared at all for a sandboxed leg that was not a flight.
        "provider_environment": {
            "sandbox_note": ProviderEnvironmentView.from_bundle(bundle).sandbox_note
        },
    }
