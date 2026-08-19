import json
from datetime import datetime, timezone

import pytest

from travel_agent.entities.candidate_discovery import (
    CandidateDiscoveryOrigin,
    CandidateDiscoveryRecord,
)
from travel_agent.entities.candidate_intent import (
    CandidateIntentMatch,
    IntentMatchStatus,
)
from travel_agent.entities.delivery_bundle import (
    CandidateAdmissionResult,
    EntityRef,
    EntityType,
    FactAssertion,
    FactSourceLink,
    FieldProvenance,
    RecommendationCatalog,
    ResearchPacket,
    SourceRecord,
    VisitCandidate,
    WeatherSensitivity,
)
from travel_agent.entities.intent_spec import (
    CategoryIntentValue,
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
from travel_agent.services.candidate_intent_evaluation import (
    evaluate_candidate_intents,
)
from travel_agent.services.candidate_ranking import rank_candidates
from travel_agent.services.candidate_selection import (
    build_candidate_selection_plan,
    catalog_for_candidate_selection,
)
from travel_agent.services.research_query_planner import (
    append_targeted_repair_query,
    build_research_query_plan,
)
from travel_agent.services.fallback_query_policy import FallbackQueryPolicy
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
