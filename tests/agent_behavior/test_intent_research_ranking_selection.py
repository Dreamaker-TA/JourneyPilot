import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from travel_agent.entities.candidate_discovery import (
    CandidateDiscoveryOrigin,
    CandidateDiscoveryRecord,
)
from travel_agent.entities.candidate_intent import (
    CandidateIntentMatch,
    IntentMatchStatus,
)
from travel_agent.entities.candidate_selection import SelectionPolicy
from travel_agent.entities.delivery_bundle import (
    CandidateAdmissionResult,
    EntityRef,
    EntityLineage,
    EntityType,
    FactAssertion,
    FactSourceLink,
    FieldProvenance,
    RecommendationCatalog,
    ResearchPacket,
    SelectionOption,
    SelectionSlot,
    SourceRecord,
    TransportCandidate,
    TransportEndpoint,
    TransportLeg,
    TransportMode,
    TransportSegment,
    VisitCandidate,
    WeatherSensitivity,
)
from travel_agent.entities.intent_spec import (
    CategoryIntentValue,
    CountIntentValue,
    IntentItem,
    IntentKind,
    IntentSpec,
    IntentStrength,
    IntentTarget,
    VerificationMode,
    canonical_json_hash,
)
from travel_agent.entities.research_brief import (
    DomainResearchObjective,
    ResearchBriefV2,
)
from travel_agent.entities.research_domain import ResearchDomain
from travel_agent.entities.research_query_plan import ResearchQueryKind
from travel_agent.entities.trip_input import ControlledTripIdentity
from travel_agent.entities.itinerary_composition_v2 import (
    DayComposition,
    ItineraryCompositionDraft,
    ItineraryCompositionError,
    VisitPlacement,
    _transport_alternatives,
    validate_placement_skeleton,
)
from travel_agent.services.candidate_intent_evaluation import (
    evaluate_candidate_intents,
)
from travel_agent.services.candidate_ranking import rank_candidates
from travel_agent.services.candidate_selection import (
    build_candidate_selection_plan,
    catalog_for_candidate_selection,
    catalog_for_workspace_materialization,
    selected_candidate_capabilities,
)
from travel_agent.services.composition_rule_compiler import compile_composition_rules
from travel_agent.services.research_query_planner import (
    append_structural_connector_query,
    append_targeted_repair_query,
    build_research_query_plan,
)
from travel_agent.services.fallback_query_policy import FallbackQueryPolicy
from travel_agent.services.delivery_projection import _reportable_selection_options
from travel_agent.agents.destination_researcher import node as destination_node
from travel_agent.agents.research_packet_output import (
    _bind_candidate_discovery_lineage,
)
from travel_agent.agents.orchestrator.candidate_gate import _apply_candidate_caps


def _identity() -> ControlledTripIdentity:
    return ControlledTripIdentity.model_validate(
        {
            "origin": {
                "place_id": "origin_shanghai",
                "provider": "manual_verified",
                "kind": "city",
                "name": "上海",
                "display_name": "上海",
                "country_code": "CN",
                "latitude": 31.23,
                "longitude": 121.47,
                "admin_path": ["上海"],
            },
            "destinations": [
                {
                    "place_id": "destination_tokyo",
                    "provider": "manual_verified",
                    "kind": "city",
                    "name": "Tokyo",
                    "display_name": "Tokyo, Japan",
                    "country_code": "JP",
                    "latitude": 35.68,
                    "longitude": 139.76,
                    "admin_path": ["Tokyo"],
                }
            ],
            "start_date": "2026-10-01",
            "end_date": "2026-10-04",
            "party": {
                "adults": 2,
                "children": 0,
                "elderly_companions": False,
                "accessibility_required": False,
            },
            "style": {
                "primary": "architecture",
                "secondary_interests": ["photography"],
                "source": "current",
            },
        }
    )


def _intent(
    intent_id: str,
    summary: str,
    *,
    kind: IntentKind = IntentKind.THEME,
    strength: IntentStrength = IntentStrength.SOFT,
    priority: int = 90,
    verification: VerificationMode = VerificationMode.SEMANTIC,
) -> IntentItem:
    return IntentItem(
        intent_id=intent_id,
        kind=kind,
        target=IntentTarget.VISIT,
        strength=strength,
        priority=priority,
        value=CategoryIntentValue(categories=[summary]),
        source_kind="current_request",
        source_ref_id=f"source_{intent_id}",
        source_text=summary,
        linked_constraint_ids=[],
        verification_mode=verification,
        impact_stages=["research", "ranking"],
        public_summary=summary,
    )


def _spec(*intents: IntentItem) -> IntentSpec:
    material = [intent.model_dump(mode="json") for intent in intents]
    return IntentSpec(
        intent_spec_id="intent_spec_test",
        revision=1,
        generation_id="generation_test",
        content_hash=canonical_json_hash(material),
        active_items=list(intents),
        objective_summary="Tokyo intent test",
    )


def _brief(spec: IntentSpec) -> ResearchBriefV2:
    objective = DomainResearchObjective(
        objective_id="objective_visit",
        domain=ResearchDomain.VISIT,
        summary="research visit candidates",
        must_cover_intent_ids=[
            intent.intent_id
            for intent in spec.active_items
            if intent.strength is IntentStrength.HARD
        ],
        optional_intent_ids=[
            intent.intent_id
            for intent in spec.active_items
            if intent.strength is not IntentStrength.HARD
        ],
        excluded_categories=[
            category
            for intent in spec.active_items
            if intent.kind is IntentKind.MUST_EXCLUDE
            and isinstance(intent.value, CategoryIntentValue)
            for category in intent.value.categories
        ],
    )
    material = objective.model_dump(mode="json")
    return ResearchBriefV2(
        brief_id="brief_test",
        generation_id=spec.generation_id,
        controlled_trip_identity_revision=1,
        intent_spec_revision=spec.revision,
        constraint_pack_revision=1,
        objective_summary=spec.objective_summary,
        controlled_trip_identity=_identity(),
        domain_objectives=[objective],
        hard_intent_ids=[
            intent.intent_id
            for intent in spec.active_items
            if intent.strength is IntentStrength.HARD
        ],
        soft_intent_ids=[
            intent.intent_id
            for intent in spec.active_items
            if intent.strength is IntentStrength.SOFT
        ],
        content_hash=canonical_json_hash(material),
    )


def _packet(
    candidate_id: str,
    name: str,
    *,
    origin: CandidateDiscoveryOrigin,
    query_id: str,
    intent_id: str,
    opening_window: str | None = None,
) -> ResearchPacket:
    source_id = f"source_{candidate_id}"
    fact_id = f"fact_{candidate_id}"
    source = SourceRecord(
        source_record_id=source_id,
        source_kind="external_tool",
        title=name,
        provider_name="fixture",
        public_excerpt=name,
        retrieved_at=datetime.now(timezone.utc),
        content_hash=canonical_json_hash({"name": name}),
        snapshot={"name": name},
        tool_audit_id=f"audit_{candidate_id}",
    )
    fact = FactAssertion(
        fact_assertion_id=fact_id,
        entity_ref=EntityRef(
            entity_type=EntityType.VISIT_STOP,
            entity_id=candidate_id,
        ),
        field_path="name",
        asserted_value=name,
        criticality="decision_critical",
        status="verified",
        source_links=[
            FactSourceLink(
                source_record_id=source_id,
                relation="supports",
                source_locator="fixture.name",
            )
        ],
    )
    candidate = VisitCandidate(
        candidate_id=candidate_id,
        research_packet_id=f"packet_{candidate_id}",
        destination_id="destination_tokyo",
        fact_assertion_ids=[fact_id],
        source_record_ids=[source_id],
        field_paths=["name"],
        weather_sensitivity=WeatherSensitivity(
            exposure="mixed",
            rain_sensitivity="low",
            heat_sensitivity="low",
            cold_sensitivity="low",
            wind_sensitivity="low",
            requires_clear_visibility=False,
        ),
        selection_reasons=["verified identity", "intent candidate"],
        tradeoff="fixture",
        freshness_status="current",
        place_id=f"place_{candidate_id}",
        provider_place_type="tourism;attraction",
        provider_country_code="JP",
        name=name,
        address="Tokyo",
        visit_type="culture",
        recommended_duration_minutes=90,
        opening_window=opening_window,
    )
    record = CandidateDiscoveryRecord(
        candidate_id=candidate_id,
        generation_id="generation_test",
        query_ids=[query_id],
        intent_ids=[intent_id],
        origins=[origin],
        provider_audit_ids=[f"audit_{candidate_id}"],
        discovered_at_rounds=[0],
    )
    return ResearchPacket(
        research_packet_id=f"packet_{candidate_id}",
        run_id="run_test",
        generation_id="generation_test",
        intent_spec_revision=1,
        research_query_plan_id="query_plan_generation_test",
        executed_query_ids=[query_id],
        candidate_discovery_records=[record],
        task_id=f"task_{candidate_id}",
        worker_kind="destination_researcher",
        constraint_pack_revision=1,
        fact_data_revision=1,
        query_context={},
        candidates=[candidate],
        source_records=[source],
        fact_assertions=[fact],
        field_provenance=[
            FieldProvenance(
                origin="external_fact",
                entity_ref=fact.entity_ref,
                field_path="name",
                reference_ids=[fact_id],
            )
        ],
        generated_at=datetime.now(timezone.utc),
    )


def _catalog(*packets: ResearchPacket) -> RecommendationCatalog:
    admissions = [
        CandidateAdmissionResult(
            candidate_id=packet.candidates[0].candidate_id,
            status="passed",
            evaluated_fact_revision=1,
            evaluated_weather_revision=0,
        )
        for packet in packets
    ]
    return RecommendationCatalog(
        generation_id="generation_test",
        intent_spec_revision=1,
        research_query_plan_id="query_plan_generation_test",
        fact_data_revision=1,
        weather_data_revision=0,
        research_packets=list(packets),
        admission_results=admissions,
        candidate_discovery_records=[
            record
            for packet in packets
            for record in packet.candidate_discovery_records
        ],
    )


def _single_visit_skeleton(
    candidate_id: str,
    *,
    planned_start: datetime,
    planned_end: datetime,
) -> ItineraryCompositionDraft:
    return ItineraryCompositionDraft(
        itinerary_id="itinerary_opening_window",
        title="Opening window fixture",
        duration_days=1,
        days=[
            DayComposition(
                day_id="day_1",
                day=1,
                date=planned_start.date(),
                destination_id="destination_tokyo",
                placements=[
                    VisitPlacement(
                        candidate_id=candidate_id,
                        planned_start=planned_start,
                        planned_end=planned_end,
                        duration_minutes=int(
                            (planned_end - planned_start).total_seconds() // 60
                        ),
                    )
                ],
            )
        ],
    )


def _transport_candidate(
    candidate_id: str,
    *,
    scope_id: str,
    origin: str,
    destination: str,
) -> TransportCandidate:
    from_endpoint = TransportEndpoint(name=origin, station_code=origin)
    to_endpoint = TransportEndpoint(name=destination, station_code=destination)
    segment = TransportSegment(
        segment_id=f"segment_{candidate_id}",
        mode=TransportMode.HIGH_SPEED_RAIL,
        from_endpoint=from_endpoint,
        to_endpoint=to_endpoint,
        duration_minutes=60,
    )
    return TransportCandidate(
        candidate_id=candidate_id,
        research_packet_id=f"packet_{candidate_id}",
        destination_id="destination_hangzhou",
        fact_assertion_ids=[f"fact_{candidate_id}"],
        source_record_ids=[f"source_{candidate_id}"],
        field_paths=["segments"],
        weather_sensitivity=WeatherSensitivity(
            exposure="indoor",
            rain_sensitivity="low",
            heat_sensitivity="low",
            cold_sensitivity="low",
            wind_sensitivity="low",
            requires_clear_visibility=False,
        ),
        selection_reasons=["exact route", "current provider result"],
        tradeoff="fixture",
        freshness_status="current",
        route_id=f"route_{candidate_id}",
        transport_class="long_distance",
        provider_evidence_scope_id=scope_id,
        selected_mode=TransportMode.HIGH_SPEED_RAIL,
        from_endpoint=from_endpoint,
        to_endpoint=to_endpoint,
        duration_minutes=60,
        segments=[segment],
        booking_status="recommended",
    )


def _selection_option(
    slot_id: str, candidate_id: str, rank: int, *, target_id: str
) -> SelectionOption:
    return SelectionOption(
        option_id=f"option_{slot_id}_{candidate_id}",
        selection_slot_id=slot_id,
        candidate_id=candidate_id,
        candidate_entity_ref=EntityRef(
            entity_type=EntityType.VISIT_STOP, entity_id=target_id
        ),
        rank=rank,
        selection_reasons=["verified identity", "comparison fixture"],
        tradeoff="fixture",
        comparison_facts=["name"],
        availability_status="confirmed",
        fact_assertion_ids=[f"fact_{candidate_id}"],
        source_record_ids=[f"source_{candidate_id}"],
    )


def test_structural_connector_query_is_server_owned_and_idempotent():
    spec = _spec(_intent("intent_architecture", "architecture"))
    plan = build_research_query_plan(intent_spec=spec, brief=_brief(spec))

    updated, query = append_structural_connector_query(
        plan,
        destination_id="destination_tokyo",
        destination_name="Tokyo",
        route_pairs=[("place_station", "place_garden"), ("place_garden", "place_cafe")],
    )
    replayed, replayed_query = append_structural_connector_query(
        updated,
        destination_id="destination_tokyo",
        destination_name="Tokyo",
        route_pairs=[("place_garden", "place_cafe"), ("place_station", "place_garden")],
    )

    assert query.domain is ResearchDomain.LOCAL_TRANSPORT
    assert query.query_kind is ResearchQueryKind.STRUCTURAL
    assert query.provider_route == "route_provider"
    assert query.query_id in updated.query_index()
    assert updated.content_hash != plan.content_hash
    assert replayed == updated
    assert replayed_query == query


def test_long_distance_alternatives_must_share_the_exact_route_scope():
    outbound_scope = "a" * 64
    return_scope = "b" * 64
    selected = _transport_candidate(
        "outbound_primary",
        scope_id=outbound_scope,
        origin="SHH",
        destination="HZH",
    )
    same_direction = _transport_candidate(
        "outbound_alternative",
        scope_id=outbound_scope,
        origin="AOH",
        destination="HZH",
    )
    reverse = _transport_candidate(
        "return_train",
        scope_id=return_scope,
        origin="HZH",
        destination="SHH",
    )
    leg = TransportLeg(
        transport_leg_id="leg_outbound",
        transport_class="long_distance",
        selected_mode=selected.selected_mode,
        from_endpoint=selected.from_endpoint,
        to_endpoint=selected.to_endpoint,
        duration_minutes=selected.duration_minutes,
        transfer_count=0,
        segments=selected.segments,
        booking_status=selected.booking_status,
        route_status="ready",
        lineage=EntityLineage(
            research_packet_id=selected.research_packet_id,
            candidate_id=selected.candidate_id,
            fact_assertion_ids=selected.fact_assertion_ids,
            source_record_ids=selected.source_record_ids,
        ),
    )

    alternatives = _transport_alternatives(
        leg,
        {
            item.candidate_id: item
            for item in (selected, same_direction, reverse)
        },
    )

    assert {item.candidate_id for item in alternatives} == {
        "outbound_primary",
        "outbound_alternative",
    }


def test_report_alternatives_are_unique_and_never_already_selected():
    slot_one_options = [
        _selection_option("slot_one", "selected_one", 1, target_id="target_one"),
        _selection_option("slot_one", "shared_alternative", 2, target_id="target_one"),
    ]
    slot_two_options = [
        _selection_option("slot_two", "selected_two", 1, target_id="target_two"),
        _selection_option("slot_two", "shared_alternative", 2, target_id="target_two"),
        _selection_option("slot_two", "selected_one", 3, target_id="target_two"),
    ]
    slots = [
        SelectionSlot(
            selection_slot_id="slot_one",
            slot_type="visit",
            target_entity_id="target_one",
            context={},
            options=slot_one_options,
            recommended_option_id=slot_one_options[0].option_id,
            selected_option_id=slot_one_options[0].option_id,
            status="ready",
        ),
        SelectionSlot(
            selection_slot_id="slot_two",
            slot_type="visit",
            target_entity_id="target_two",
            context={},
            options=slot_two_options,
            recommended_option_id=slot_two_options[0].option_id,
            selected_option_id=slot_two_options[0].option_id,
            status="ready",
        ),
    ]

    reportable = _reportable_selection_options(slots)

    assert [[option.candidate_id for option in row] for row in reportable] == [
        ["selected_one", "shared_alternative"],
        ["selected_two"],
    ]


def test_visit_must_fit_inside_published_opening_window():
    packet = _packet(
        "candidate_garden",
        "郭庄",
        origin=CandidateDiscoveryOrigin.INTENT_QUERY,
        query_id="query_garden",
        intent_id="intent_garden",
        opening_window="08:00-17:00",
    )
    catalog = _catalog(packet)
    local_tz = ZoneInfo("Asia/Tokyo")

    with pytest.raises(ItineraryCompositionError, match="outside the published"):
        validate_placement_skeleton(
            _single_visit_skeleton(
                "candidate_garden",
                planned_start=datetime(2026, 8, 29, 7, 30, tzinfo=local_tz),
                planned_end=datetime(2026, 8, 29, 9, 0, tzinfo=local_tz),
            ),
            catalog,
        )

    validate_placement_skeleton(
        _single_visit_skeleton(
            "candidate_garden",
            planned_start=datetime(2026, 8, 29, 8, 0, tzinfo=local_tz),
            planned_end=datetime(2026, 8, 29, 9, 30, tzinfo=local_tz),
        ),
        catalog,
    )


def test_visit_rejects_published_weekday_closure():
    packet = _packet(
        "candidate_museum",
        "博物馆",
        origin=CandidateDiscoveryOrigin.INTENT_QUERY,
        query_id="query_museum",
        intent_id="intent_museum",
        opening_window="09:00-17:00（周一闭馆）",
    )
    catalog = _catalog(packet)
    local_tz = ZoneInfo("Asia/Tokyo")

    with pytest.raises(ItineraryCompositionError, match="published closed day"):
        validate_placement_skeleton(
            _single_visit_skeleton(
                "candidate_museum",
                planned_start=datetime(2026, 8, 24, 9, 0, tzinfo=local_tz),
                planned_end=datetime(2026, 8, 24, 10, 30, tzinfo=local_tz),
            ),
            catalog,
        )


def test_theme_changes_query_plan_and_museum_exclusion_blocks_fallback():
    architecture = _spec(_intent("intent_arch", "contemporary architecture"))
    temple = _spec(_intent("intent_temple", "traditional temple garden"))
    architecture_plan = build_research_query_plan(
        intent_spec=architecture,
        brief=_brief(architecture),
    )
    temple_plan = build_research_query_plan(intent_spec=temple, brief=_brief(temple))

    assert architecture_plan.content_hash != temple_plan.content_hash
    assert "contemporary architecture" in " ".join(
        query.query_text for query in architecture_plan.queries
    )
    assert "traditional temple garden" in " ".join(
        query.query_text for query in temple_plan.queries
    )

    exclusion = _spec(
        _intent(
            "intent_no_museum",
            "不要博物馆",
            kind=IntentKind.MUST_EXCLUDE,
            strength=IntentStrength.HARD,
            verification=VerificationMode.MIXED,
        )
    )
    excluded_plan = build_research_query_plan(
        intent_spec=exclusion,
        brief=_brief(exclusion),
    )
    assert all(
        "museum" not in query.query_text.casefold() for query in excluded_plan.queries
    )
    assert all(
        query.query_kind is not ResearchQueryKind.GENERIC_FALLBACK
        for query in excluded_plan.queries
    )


def test_admission_is_stable_while_ranking_and_selection_change_with_theme():
    architecture = _intent("intent_arch", "contemporary architecture")
    temple = _intent("intent_temple", "traditional temple garden")
    packet_arch = _packet(
        "candidate_arch",
        "Tokyo International Forum contemporary architecture",
        origin=CandidateDiscoveryOrigin.INTENT_QUERY,
        query_id="query_arch",
        intent_id=architecture.intent_id,
    )
    packet_temple = _packet(
        "candidate_temple",
        "Sensoji traditional temple garden",
        origin=CandidateDiscoveryOrigin.INTENT_QUERY,
        query_id="query_temple",
        intent_id=temple.intent_id,
    )
    catalog = _catalog(packet_arch, packet_temple)
    admission_snapshot = [result.model_dump() for result in catalog.admission_results]

    architecture_matches = [
        CandidateIntentMatch(
            candidate_id="candidate_arch",
            intent_id=architecture.intent_id,
            status=IntentMatchStatus.MATCHED,
            score=1,
            method="semantic_batch_evaluation",
            supporting_fact_assertion_ids=["fact_candidate_arch"],
            supporting_source_record_ids=["source_candidate_arch"],
            reason_code="semantic_match",
        ),
        CandidateIntentMatch(
            candidate_id="candidate_temple",
            intent_id=architecture.intent_id,
            status=IntentMatchStatus.NOT_MATCHED,
            score=0,
            method="semantic_batch_evaluation",
            supporting_fact_assertion_ids=["fact_candidate_temple"],
            supporting_source_record_ids=["source_candidate_temple"],
            reason_code="semantic_not_match",
        ),
    ]
    architecture_spec = _spec(architecture)
    architecture_ranking = rank_candidates(
        catalog=catalog,
        intent_spec=architecture_spec,
        matches=architecture_matches,
    )
    architecture_plan = build_candidate_selection_plan(
        catalog=catalog,
        intent_spec=architecture_spec,
        ranking_scores=architecture_ranking,
        duration_days=1,
        destination_count=1,
    )

    temple_matches = [
        match.model_copy(
            update={
                "intent_id": temple.intent_id,
                "status": (
                    IntentMatchStatus.MATCHED
                    if match.candidate_id == "candidate_temple"
                    else IntentMatchStatus.NOT_MATCHED
                ),
                "score": 1 if match.candidate_id == "candidate_temple" else 0,
            }
        )
        for match in architecture_matches
    ]
    temple_spec = _spec(temple)
    temple_ranking = rank_candidates(
        catalog=catalog,
        intent_spec=temple_spec,
        matches=temple_matches,
    )
    temple_plan = build_candidate_selection_plan(
        catalog=catalog,
        intent_spec=temple_spec,
        ranking_scores=temple_ranking,
        duration_days=1,
        destination_count=1,
    )

    assert admission_snapshot == [
        result.model_dump() for result in catalog.admission_results
    ]
    assert architecture_plan.entries[0].candidate_id == "candidate_arch"
    assert temple_plan.entries[0].candidate_id == "candidate_temple"
    assert architecture_plan.content_hash != temple_plan.content_hash
    architecture_only = architecture_plan.model_copy(
        update={"entries": [architecture_plan.entries[0]]}
    )
    composition_catalog = catalog_for_candidate_selection(
        catalog,
        architecture_only,
    )
    assert [
        result.candidate_id for result in composition_catalog.admission_results
    ] == ["candidate_arch"]


def test_selection_never_promotes_a_gate_rejected_candidate():
    intent = _intent("intent_arch", "contemporary architecture")
    spec = _spec(intent)
    catalog = _catalog(
        _packet(
            "candidate_rejected",
            "Unverified architecture candidate",
            origin=CandidateDiscoveryOrigin.INTENT_QUERY,
            query_id="query_arch",
            intent_id=intent.intent_id,
        )
    )
    matches = [
        CandidateIntentMatch(
            candidate_id="candidate_rejected",
            intent_id=intent.intent_id,
            status=IntentMatchStatus.MATCHED,
            score=1,
            method="semantic_batch_evaluation",
            supporting_fact_assertion_ids=["fact_candidate_rejected"],
            supporting_source_record_ids=["source_candidate_rejected"],
            reason_code="semantic_match",
        )
    ]
    ranking = rank_candidates(catalog=catalog, intent_spec=spec, matches=matches)
    assert ranking[0].hard_eligible is True

    plan_from_passed_catalog = build_candidate_selection_plan(
        catalog=catalog,
        intent_spec=spec,
        ranking_scores=ranking,
        duration_days=1,
        destination_count=1,
    )
    rejected_catalog = catalog.model_copy(
        update={
            "admission_results": [
                CandidateAdmissionResult(
                    candidate_id="candidate_rejected",
                    status="insufficient_for_admission",
                    missing_field_paths=["provider_evidence.name"],
                    evaluated_fact_revision=1,
                    evaluated_weather_revision=0,
                )
            ]
        }
    )

    rejected_plan = build_candidate_selection_plan(
        catalog=rejected_catalog,
        intent_spec=spec,
        ranking_scores=ranking,
        duration_days=1,
        destination_count=1,
    )

    assert rejected_plan.entries == []
    assert rejected_plan.uncovered_intent_ids == [intent.intent_id]
    with pytest.raises(ValueError, match="non-admitted candidate"):
        catalog_for_candidate_selection(rejected_catalog, plan_from_passed_catalog)


def test_composition_rules_preserve_prohibition_and_daily_capacity_semantics():
    exclusion = _intent(
        "intent_no_museum",
        "museum",
        kind=IntentKind.MUST_EXCLUDE,
        strength=IntentStrength.HARD,
        verification=VerificationMode.DETERMINISTIC,
    ).model_copy(update={"impact_stages": ["research", "admission", "composition"]})
    capacity = IntentItem(
        intent_id="intent_two_visits",
        kind=IntentKind.QUANTITY,
        target=IntentTarget.VISIT,
        strength=IntentStrength.HARD,
        priority=100,
        value=CountIntentValue(operator="at_most", count=2, unit="day"),
        source_kind="current_request",
        source_ref_id="source_capacity",
        linked_constraint_ids=[],
        verification_mode=VerificationMode.DETERMINISTIC,
        impact_stages=["composition"],
        public_summary="每天最多两个主要景点",
    )

    rules = compile_composition_rules(_spec(exclusion, capacity))

    assert [(rule.rule_kind.value, rule.policy_on_failure) for rule in rules] == [
        ("must_not_place", "never_violate"),
        ("max_per_day", "never_violate"),
    ]
    assert rules[1].parameters.model_dump() == {
        "parameter_kind": "count",
        "count": 2,
        "unit": "day",
    }


def test_selected_capabilities_exclude_alternatives_from_composition_pool():
    intent = _intent("intent_arch", "contemporary architecture")
    spec = _spec(intent)
    catalog = _catalog(
        _packet(
            "candidate_arch",
            "Tokyo International Forum",
            origin=CandidateDiscoveryOrigin.INTENT_QUERY,
            query_id="query_arch",
            intent_id=intent.intent_id,
        )
    )
    matches = [
        CandidateIntentMatch(
            candidate_id="candidate_arch",
            intent_id=intent.intent_id,
            status=IntentMatchStatus.MATCHED,
            score=1,
            method="semantic_batch_evaluation",
            supporting_fact_assertion_ids=["fact_candidate_arch"],
            supporting_source_record_ids=["source_candidate_arch"],
            reason_code="semantic_match",
        )
    ]
    ranking = rank_candidates(catalog=catalog, intent_spec=spec, matches=matches)
    plan = build_candidate_selection_plan(
        catalog=catalog,
        intent_spec=spec,
        ranking_scores=ranking,
        duration_days=1,
        destination_count=1,
    )
    catalog = catalog.model_copy(update={"candidate_ranking_scores": ranking})

    selected, alternatives = selected_candidate_capabilities(
        catalog=catalog,
        selection_plan=plan,
    )

    assert [item.candidate_id for item in selected] == ["candidate_arch"]
    assert alternatives == []
    assert selected[0].matched_intent_ids == [intent.intent_id]
    assert selected[0].schedule_capabilities["recommended_duration_minutes"] == 90


def test_explore_selection_is_seeded_reproducible_and_diverse():
    intent = _intent("intent_arch", "contemporary architecture")
    spec = _spec(intent)
    catalog = _catalog(
        *[
            _packet(
                f"candidate_{index}",
                f"Architecture {index}",
                origin=CandidateDiscoveryOrigin.INTENT_QUERY,
                query_id=f"query_{index}",
                intent_id=intent.intent_id,
            )
            for index in range(1, 6)
        ]
    )
    matches = [
        CandidateIntentMatch(
            candidate_id=f"candidate_{index}",
            intent_id=intent.intent_id,
            status=IntentMatchStatus.MATCHED,
            score=1,
            method="semantic_batch_evaluation",
            supporting_fact_assertion_ids=[f"fact_candidate_{index}"],
            supporting_source_record_ids=[f"source_candidate_{index}"],
            reason_code="semantic_match",
        )
        for index in range(1, 6)
    ]
    ranking = rank_candidates(catalog=catalog, intent_spec=spec, matches=matches)

    def selected(seed: int):
        return build_candidate_selection_plan(
            catalog=catalog,
            intent_spec=spec,
            ranking_scores=ranking,
            duration_days=1,
            destination_count=1,
            selection_policy=SelectionPolicy(
                mode="explore",
                selection_seed=seed,
                diversity_strength=1,
                policy_version="candidate_selection.v1",
            ),
        )

    first = selected(1)
    replay = selected(1)
    variants = {
        tuple(
            entry.candidate_id
            for entry in selected(seed).entries
            if entry.eligible_for_composition
        )
        for seed in range(1, 8)
    }

    assert first == replay
    assert len(variants) > 1
    assert all(len(candidate_ids) == 2 for candidate_ids in variants)
    alternative_ids = {
        entry.candidate_id
        for entry in first.entries
        if not entry.eligible_for_composition
    }
    assert alternative_ids
    composition_catalog = catalog_for_candidate_selection(catalog, first)
    workspace_catalog = catalog_for_workspace_materialization(catalog, first)
    assert alternative_ids.isdisjoint(
        {item.candidate_id for item in composition_catalog.admission_results}
    )
    assert alternative_ids <= {
        item.candidate_id
        for item in workspace_catalog.admission_results
        if item.status == "passed"
    }


def test_fallback_candidate_is_penalized_and_targeted_repair_is_stable():
    intent = _intent("intent_arch", "contemporary architecture")
    spec = _spec(intent)
    plan = build_research_query_plan(intent_spec=spec, brief=_brief(spec))
    repeated, targeted = append_targeted_repair_query(
        plan,
        intent=intent,
        destination_id="destination_tokyo",
        destination_name="Tokyo",
        domain=ResearchDomain.VISIT,
        desired_candidate_count=2,
    )
    repeated_again, targeted_again = append_targeted_repair_query(
        repeated,
        intent=intent,
        destination_id="destination_tokyo",
        destination_name="Tokyo",
        domain=ResearchDomain.VISIT,
        desired_candidate_count=2,
    )
    assert targeted.query_id == targeted_again.query_id
    assert repeated.content_hash == repeated_again.content_hash
    fallback = next(
        query
        for query in plan.queries
        if query.query_kind is ResearchQueryKind.GENERIC_FALLBACK
    )
    policy = FallbackQueryPolicy()
    assert not policy.is_allowed(
        fallback,
        executed_query_ids=set(fallback.fallback_after_query_ids),
        admitted_candidate_count=0,
        required_candidate_count=1,
        research_window_open=False,
        run_budget_available=True,
    )
    assert policy.is_allowed(
        fallback,
        executed_query_ids=set(fallback.fallback_after_query_ids),
        admitted_candidate_count=0,
        required_candidate_count=1,
        research_window_open=True,
        run_budget_available=True,
    )
    excluded_fallback = fallback.model_copy(
        update={"excluded_categories": ["不要博物馆"]}
    )
    assert not policy.is_allowed(
        excluded_fallback,
        executed_query_ids=set(excluded_fallback.fallback_after_query_ids),
        admitted_candidate_count=0,
        required_candidate_count=1,
        research_window_open=True,
        run_budget_available=True,
    )

    intent_packet = _packet(
        "candidate_intent",
        "Intent Place",
        origin=CandidateDiscoveryOrigin.INTENT_QUERY,
        query_id="query_intent",
        intent_id=intent.intent_id,
    )
    fallback_packet = _packet(
        "candidate_fallback",
        "Fallback Place",
        origin=CandidateDiscoveryOrigin.GENERIC_FALLBACK,
        query_id="query_fallback",
        intent_id=intent.intent_id,
    )
    catalog = _catalog(intent_packet, fallback_packet)
    matches = [
        CandidateIntentMatch(
            candidate_id=candidate_id,
            intent_id=intent.intent_id,
            status=IntentMatchStatus.MATCHED,
            score=1,
            method="semantic_batch_evaluation",
            supporting_fact_assertion_ids=[f"fact_{candidate_id}"],
            supporting_source_record_ids=[f"source_{candidate_id}"],
            reason_code="semantic_match",
        )
        for candidate_id in ("candidate_intent", "candidate_fallback")
    ]
    ranking = rank_candidates(catalog=catalog, intent_spec=spec, matches=matches)
    by_id = {score.candidate_id: score for score in ranking}
    assert by_id["candidate_intent"].generic_fallback_penalty == 0
    assert by_id["candidate_fallback"].generic_fallback_penalty > 0
    selection = build_candidate_selection_plan(
        catalog=catalog,
        intent_spec=spec,
        ranking_scores=ranking,
        duration_days=1,
        destination_count=1,
    )
    assert selection.entries[0].candidate_id == "candidate_intent"


@pytest.mark.asyncio
async def test_semantic_evaluator_cannot_claim_a_match_without_evidence():
    intent = _intent("intent_arch", "contemporary architecture")
    spec = _spec(intent)
    packet = _packet(
        "candidate_one",
        "A place",
        origin=CandidateDiscoveryOrigin.INTENT_QUERY,
        query_id="query_one",
        intent_id=intent.intent_id,
    )

    class FakeModel:
        async def ainvoke(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "matches": [
                        {
                            "candidate_id": "candidate_one",
                            "intent_id": intent.intent_id,
                            "status": "matched",
                            "score": 1,
                            "supporting_fact_assertion_ids": [],
                            "supporting_source_record_ids": [],
                            "reason_code": "unsupported_claim",
                            "public_reason": "looks suitable",
                        }
                    ]
                }
            )

    matches, _cache = await evaluate_candidate_intents(
        catalog=_catalog(packet),
        intent_spec=spec,
        llm=FakeModel(),
    )
    assert matches[0].status is IntentMatchStatus.UNKNOWN
    assert matches[0].score is None


@pytest.mark.asyncio
async def test_semantic_exclusion_becomes_a_hard_ranking_violation():
    intent = _intent(
        "intent_no_museum",
        "不要博物馆",
        kind=IntentKind.MUST_EXCLUDE,
        strength=IntentStrength.HARD,
        verification=VerificationMode.SEMANTIC,
    )
    spec = _spec(intent)
    packet = _packet(
        "candidate_museum",
        "Museum Place",
        origin=CandidateDiscoveryOrigin.INTENT_QUERY,
        query_id="query_museum",
        intent_id=intent.intent_id,
    )
    catalog = _catalog(packet)

    class FakeModel:
        async def ainvoke(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "matches": [
                        {
                            "candidate_id": "candidate_museum",
                            "intent_id": intent.intent_id,
                            "status": "matched",
                            "score": 1,
                            "supporting_fact_assertion_ids": ["fact_candidate_museum"],
                            "supporting_source_record_ids": ["source_candidate_museum"],
                            "reason_code": "excluded_category_present",
                            "public_reason": "候选属于博物馆",
                        }
                    ]
                }
            )

    matches, _cache = await evaluate_candidate_intents(
        catalog=catalog,
        intent_spec=spec,
        llm=FakeModel(),
    )
    ranking = rank_candidates(catalog=catalog, intent_spec=spec, matches=matches)
    assert matches[0].status is IntentMatchStatus.VIOLATED
    assert ranking[0].hard_eligible is False
    assert ranking[0].hard_violation_intent_ids == [intent.intent_id]


@pytest.mark.asyncio
async def test_worker_executes_intent_and_structural_queries_before_fallback(
    monkeypatch,
):
    intent = _intent("intent_arch", "contemporary architecture")
    spec = _spec(intent)
    plan = build_research_query_plan(intent_spec=spec, brief=_brief(spec))
    queries = [query for query in plan.queries if query.domain is ResearchDomain.VISIT]
    calls = []

    async def fake_execute_tool(_name, arguments, **_kwargs):
        calls.append(arguments["query"])
        results = (
            [
                {
                    "place_id": "osm:node:1",
                    "name": "Museum Place",
                    "provider_place_type": "tourism;museum",
                    "provider_country_code": "JP",
                    "address": "Tokyo",
                }
            ]
            if arguments["query"].startswith("museum in")
            else []
        )
        return {
            "status": "success",
            "audit_id": f"audit_{len(calls)}",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "sanitized_result": {"results": results},
        }

    monkeypatch.setattr(destination_node, "execute_tool", fake_execute_tool)
    executed = []
    await destination_node.discover_and_resolve_required_places(
        messages=[],
        available_tools=[{"schema": {"function": {"name": "global_place_search"}}}],
        required_candidate_kinds=["visit"],
        destination_boundaries=[
            {
                "destination_id": "destination_tokyo",
                "name": "Tokyo",
                "country_code": "jp",
                "latitude": 35.68,
                "longitude": 139.76,
            }
        ],
        tool_context={},
        authoritative_tool_results=[],
        planned_queries=queries,
        executed_query_ids=executed,
    )
    kinds = [plan.query_index()[query_id].query_kind for query_id in executed]
    assert kinds == [
        ResearchQueryKind.INTENT_PRIMARY,
        ResearchQueryKind.STRUCTURAL,
        ResearchQueryKind.GENERIC_FALLBACK,
    ]
    assert "contemporary architecture" in calls[0]
    assert calls[-1].startswith("museum in")


def test_packet_compiler_owns_and_merges_candidate_discovery_lineage():
    packet = _packet(
        "candidate_one",
        "Candidate One",
        origin=CandidateDiscoveryOrigin.GENERIC_FALLBACK,
        query_id="model_query",
        intent_id="model_intent",
    )
    payload = packet.model_dump(mode="python")
    payload["executed_query_ids"] = ["query_primary", "query_structural"]
    payload["query_context"] = {
        "research_round": 2,
        "query_lineage": [
            {
                "query_id": "query_primary",
                "domain": "visit",
                "query_kind": "intent_primary",
                "intent_ids": ["intent_arch"],
            },
            {
                "query_id": "query_structural",
                "domain": "visit",
                "query_kind": "structural",
                "intent_ids": [],
            },
        ],
    }

    _bind_candidate_discovery_lineage(payload)

    assert payload["candidate_discovery_records"] == [
        {
            "candidate_id": "candidate_one",
            "generation_id": "generation_test",
            "query_ids": ["query_primary", "query_structural"],
            "intent_ids": ["intent_arch"],
            "origins": ["intent_query", "structural_query"],
            "provider_audit_ids": ["audit_candidate_one"],
            "discovered_at_rounds": [2],
        }
    ]


def test_domain_cap_keeps_targeted_and_intent_candidates_before_fallback():
    targeted = _packet(
        "candidate_targeted",
        "Targeted Place",
        origin=CandidateDiscoveryOrigin.TARGETED_REPAIR,
        query_id="query_targeted",
        intent_id="intent_arch",
    )
    intent = _packet(
        "candidate_intent",
        "Intent Place",
        origin=CandidateDiscoveryOrigin.INTENT_QUERY,
        query_id="query_intent",
        intent_id="intent_arch",
    )
    fallback = _packet(
        "candidate_fallback",
        "Fallback Place",
        origin=CandidateDiscoveryOrigin.GENERIC_FALLBACK,
        query_id="query_fallback",
        intent_id="intent_arch",
    )

    capped = _apply_candidate_caps(
        [fallback, intent, targeted],
        {ResearchDomain.VISIT.value: 2},
    )

    assert {
        candidate.candidate_id for packet in capped for candidate in packet.candidates
    } == {"candidate_targeted", "candidate_intent"}
