import asyncio
from datetime import datetime
from types import SimpleNamespace

from travel_agent.entities.candidate_intent import (
    CandidateIntentMatch,
    IntentMatchStatus,
)
from travel_agent.entities.candidate_selection import (
    CandidateSelectionRole,
    SelectedCandidateCapability,
)
from travel_agent.entities.intent_spec import (
    CadenceIntentValue,
    CountIntentValue,
    IntentItem,
    IntentKind,
    IntentSpec,
    IntentStrength,
    IntentTarget,
    ScalarIntentValue,
    VerificationMode,
    canonical_json_hash,
)
from travel_agent.entities.composition_rules import (
    CompositionRule,
    CompositionRuleKind,
    CountRuleParameters,
    PlacementRuleParameters,
)
from travel_agent.entities.intent_coverage import EntityIntentExplanation
from travel_agent.entities.research_domain import ResearchDomain
from travel_agent.services.composition_backfill import (
    assert_never_violate_rules,
    build_legal_open_slots,
    order_backfill_candidates,
)
from travel_agent.services.composition_mutations import (
    diff_composition_mutations,
    mark_mutations_revalidated,
)
from travel_agent.services.composition_rule_compiler import compile_composition_rules
from travel_agent.services.intent_verification import evaluate_intent_fidelity
from travel_agent.services.public_delivery import _public_intent_explanations
from travel_agent.agents.utils import (
    assignment_research_round,
    resolve_agent_assignment,
)
from travel_agent.api.schemas import PublicBundleWorkspaceResponse
from travel_agent.agents.orchestrator.intent_fidelity_gate import (
    intent_fidelity_gate_node,
)


def _capability(
    candidate_id: str,
    *,
    rank: int,
    matched_intent_ids: list[str],
    hard_violation_intent_ids: list[str] | None = None,
) -> SelectedCandidateCapability:
    return SelectedCandidateCapability(
        candidate_id=candidate_id,
        candidate_kind="visit",
        destination_id="destination_tokyo",
        selection_role=CandidateSelectionRole.PRIMARY,
        rank=rank,
        matched_intent_ids=matched_intent_ids,
        hard_violation_intent_ids=hard_violation_intent_ids or [],
        selection_reasons=["intent fit"],
        evidence_confidence=1,
        budget_fit=1,
        weather_fit=1,
        constraint_fit=1,
        place_id=f"place_{candidate_id}",
        schedule_capabilities={"recommended_duration_minutes": 90},
    )


def _rule(
    rule_id: str,
    intent_id: str,
    kind: CompositionRuleKind,
    parameters,
    *,
    policy: str = "never_violate",
) -> CompositionRule:
    return CompositionRule(
        rule_id=rule_id,
        intent_id=intent_id,
        generation_id="generation_test",
        rule_kind=kind,
        target_domain=ResearchDomain.VISIT,
        hard=True,
        policy_on_failure=policy,
        parameters=parameters,
    )


def test_fidelity_defers_a_missing_workspace_to_delivery_quality_gate():
    update = asyncio.run(
        intent_fidelity_gate_node(
            SimpleNamespace(intent_spec=object(), trip_workspace_v2=None)
        )
    )

    assert update == {"intent_fidelity_route": "passed"}


def test_slot_backfill_prefers_pending_intent_and_respects_daily_maximum():
    rules = [
        _rule(
            "rule_theme",
            "intent_architecture",
            CompositionRuleKind.MUST_PLACE,
            PlacementRuleParameters(values=["contemporary architecture"]),
            policy="repair_then_deviate",
        ),
        _rule(
            "rule_max",
            "intent_daily_max",
            CompositionRuleKind.MAX_PER_DAY,
            CountRuleParameters(count=2, unit="day"),
        ),
    ]
    payload = {
        "days": [
            {
                "day_id": "day_1",
                "destination_id": "destination_tokyo",
                "placements": [
                    {"placement_kind": "visit", "candidate_id": "candidate_existing"}
                ],
            }
        ]
    }
    slot = build_legal_open_slots(payload, rules)[0]
    ordered = order_backfill_candidates(
        [
            _capability("candidate_generic", rank=1, matched_intent_ids=[]),
            _capability(
                "candidate_architecture",
                rank=2,
                matched_intent_ids=["intent_architecture"],
            ),
        ],
        slot,
    )

    assert slot.remaining_capacity == 1
    assert ordered[0].candidate_id == "candidate_architecture"


def test_never_violate_rule_rejects_prohibited_and_unverified_authored_entries():
    rule = _rule(
        "rule_no_museum",
        "intent_no_museum",
        CompositionRuleKind.MUST_NOT_PLACE,
        PlacementRuleParameters(values=["museum"]),
    )
    prohibited = _capability(
        "candidate_museum",
        rank=1,
        matched_intent_ids=[],
        hard_violation_intent_ids=["intent_no_museum"],
    )

    for placement in (
        {"placement_kind": "visit", "candidate_id": "candidate_museum"},
        {
            "placement_kind": "visit",
            "authored_place": {"name": "Unverified Place"},
        },
    ):
        payload = {
            "days": [
                {
                    "day_id": "day_1",
                    "destination_id": "destination_tokyo",
                    "placements": [placement],
                }
            ]
        }
        try:
            assert_never_violate_rules(payload, [rule], [prohibited])
        except ValueError as exc:
            assert "never-violate" in str(exc)
        else:
            raise AssertionError("prohibited placement must fail closed")


def test_mutation_ledger_records_drop_move_and_backfill_then_revalidation():
    before = {
        "days": [
            {
                "day_id": "day_1",
                "placements": [
                    {"placement_kind": "visit", "candidate_id": "candidate_drop"},
                    {"placement_kind": "visit", "candidate_id": "candidate_move"},
                ],
            },
            {"day_id": "day_2", "placements": []},
        ]
    }
    after = {
        "days": [
            {"day_id": "day_1", "placements": []},
            {
                "day_id": "day_2",
                "placements": [
                    {"placement_kind": "visit", "candidate_id": "candidate_move"},
                    {"placement_kind": "visit", "candidate_id": "candidate_add"},
                ],
            },
        ]
    }

    mutations = diff_composition_mutations(
        before=before,
        after=after,
        generation_id="generation_test",
        reason_code="slot_aware_closeout",
        created_by="slot_backfill",
        intent_ids_by_entity={"candidate_add": ["intent_architecture"]},
        rule_ids=["rule_max"],
    )
    validated = mark_mutations_revalidated(
        mutations,
        coverage_before={"intent_architecture": "unsatisfied"},
        coverage_after={"intent_architecture": "satisfied"},
    )

    assert {item.mutation_type.value for item in validated} == {
        "drop",
        "move",
        "backfill",
    }
    assert all(item.hard_rules_revalidated for item in validated)
    added = next(item for item in validated if item.mutation_type.value == "backfill")
    assert added.affected_intent_ids == ["intent_architecture"]
    assert added.coverage_before == {"intent_architecture": "unsatisfied"}
    assert added.coverage_after == {"intent_architecture": "satisfied"}
    refreshed = mark_mutations_revalidated(
        [added],
        coverage_after={"intent_architecture": "partially_satisfied"},
    )[0]
    assert refreshed.coverage_before == {"intent_architecture": "unsatisfied"}
    assert refreshed.coverage_after == {
        "intent_architecture": "partially_satisfied"
    }


def test_assignment_resolution_uses_latest_explicit_research_round():
    key, assignment = resolve_agent_assignment(
        {
            "destination_researcher": {"objective": "initial"},
            "destination_researcher_r2": {"objective": "repair"},
            "destination_researcher_r3": {"objective": "latest"},
        },
        "destination_researcher",
    )

    assert key == "destination_researcher_r3"
    assert assignment["objective"] == "latest"
    assert assignment_research_round(key) == 2
    assert assignment_research_round("destination_researcher") == 0


def test_public_explanation_hides_internal_intent_identifier():
    payload = _public_intent_explanations(
        [
            EntityIntentExplanation(
                intent_id="intent_private",
                label="符合偏好",
                explanation="该地点与本次旅行主题一致。",
                evidence_basis="planning_judgment",
            )
        ]
    )

    assert payload == [
        {
            "label": "符合偏好",
            "explanation": "该地点与本次旅行主题一致。",
            "evidence_basis": "planning_judgment",
        }
    ]


def test_public_workspace_schema_requires_typed_fulfillment_and_generation():
    workspace = PublicBundleWorkspaceResponse.model_validate(
        {
            "contract_version": "journeypilot.trip_workspace.v10",
            "run_id": "run_test",
            "generation_id": "generation_test",
            "workspace_revision": 0,
            "itinerary": {},
            "fulfillment_summary": {
                "fulfilled": [
                    {
                        "requirement_id": "requirement_1",
                        "summary": "安排当代建筑",
                        "status": "satisfied",
                        "explanation": "已在行程中落实。",
                    }
                ],
                "deviations": [],
            },
        }
    )

    assert workspace.generation_id == "generation_test"
    assert workspace.fulfillment_summary.fulfilled[0].status == "satisfied"


def _intent(
    intent_id: str,
    *,
    kind: IntentKind,
    strength: IntentStrength,
    value,
) -> IntentItem:
    return IntentItem(
        intent_id=intent_id,
        kind=kind,
        target=IntentTarget.VISIT,
        strength=strength,
        priority=90,
        value=value,
        source_kind="current_request",
        source_ref_id="message_test",
        verification_mode=VerificationMode.DETERMINISTIC,
        impact_stages=["composition"],
        public_summary=f"requirement {intent_id}",
    )


def _intent_spec(*intents: IntentItem) -> IntentSpec:
    material = [intent.model_dump(mode="json") for intent in intents]
    return IntentSpec(
        intent_spec_id="intent_spec_test",
        revision=1,
        generation_id="generation_test",
        content_hash=canonical_json_hash(material),
        active_items=list(intents),
        objective_summary="test intent coverage",
    )


def _workspace(
    *,
    placements: list[tuple[str, str, int]],
    matches: list[CandidateIntentMatch] | None = None,
    day_count: int = 2,
):
    candidates = {
        candidate_id: SimpleNamespace(candidate_id=candidate_id, name=candidate_id)
        for candidate_id, _day_id, _hour in placements
    }
    catalog = SimpleNamespace(
        candidate_intent_matches=matches or [],
        candidate_index=lambda: candidates,
    )
    visit_stops = [
        SimpleNamespace(
            item_id=f"item_{candidate_id}",
            day_id=day_id,
            planned_start=datetime(2026, 9, int(day_id.rsplit("_", 1)[1]), hour),
            lineage=SimpleNamespace(
                candidate_id=candidate_id,
                fact_assertion_ids=[f"fact_{candidate_id}"],
            ),
            intent_explanations=[],
        )
        for candidate_id, day_id, hour in placements
    ]
    itinerary = SimpleNamespace(
        visit_stops=visit_stops,
        dining_stops=[],
        lodging_stays=[],
        transport_legs=[],
        day_plans=[
            SimpleNamespace(
                day_id=f"day_{day}",
                day=day,
                destination_id="destination_tokyo",
                timeline=[
                    SimpleNamespace(entity_id=f"item_{candidate_id}")
                    for candidate_id, day_id, _hour in placements
                    if day_id == f"day_{day}"
                ],
            )
            for day in range(1, day_count + 1)
        ],
    )
    return SimpleNamespace(
        workspace_revision=0,
        recommendation_catalog=catalog,
        itinerary=itinerary,
        candidate_selection_plan=SimpleNamespace(
            composition_candidate_ids=lambda: set(candidates),
            selection_policy=SimpleNamespace(mode="deterministic"),
            entries=[],
        ),
    )


def _match(candidate_id: str, intent_id: str, status: IntentMatchStatus):
    return CandidateIntentMatch(
        candidate_id=candidate_id,
        intent_id=intent_id,
        status=status,
        score=1.0,
        method="deterministic",
        supporting_fact_assertion_ids=[f"fact_{candidate_id}"],
        reason_code="test_match",
    )


def test_fidelity_detects_hard_excluded_candidate():
    intent = _intent(
        "intent_no_museum",
        kind=IntentKind.MUST_EXCLUDE,
        strength=IntentStrength.HARD,
        value=ScalarIntentValue(value="museum"),
    )
    spec = _intent_spec(intent)
    workspace = _workspace(
        placements=[("candidate_museum", "day_1", 10)],
        matches=[
            _match("candidate_museum", intent.intent_id, IntentMatchStatus.VIOLATED)
        ],
    )

    report, gaps = evaluate_intent_fidelity(
        intent_spec=spec,
        rules=compile_composition_rules(spec),
        workspace=workspace,
        repair_budget_exhausted=False,
    )

    assert report.items[0].status.value == "unsatisfied"
    assert report.items[0].blocking is True
    assert gaps[0].reason == "excluded_candidate_present"


def test_fidelity_routes_an_unselected_matching_candidate_to_reselection():
    intent = _intent(
        "intent_required_garden",
        kind=IntentKind.MUST_INCLUDE,
        strength=IntentStrength.HARD,
        value=ScalarIntentValue(value="garden"),
    )
    spec = _intent_spec(intent)
    workspace = _workspace(
        placements=[("candidate_generic", "day_1", 10)],
        matches=[
            _match("candidate_garden", intent.intent_id, IntentMatchStatus.MATCHED)
        ],
    )

    _report, gaps = evaluate_intent_fidelity(
        intent_spec=spec,
        rules=compile_composition_rules(spec),
        workspace=workspace,
        repair_budget_exhausted=False,
    )

    assert gaps[0].retry_target == "candidate_selection"


def test_fidelity_checks_daily_quantity_and_cadence_window():
    quantity = _intent(
        "intent_max_two",
        kind=IntentKind.QUANTITY,
        strength=IntentStrength.HARD,
        value=CountIntentValue(operator="at_most", count=2, unit="day"),
    )
    cadence = _intent(
        "intent_daily_afternoon",
        kind=IntentKind.CADENCE,
        strength=IntentStrength.HARD,
        value=CadenceIntentValue(frequency="once_per_day", time_window="afternoon"),
    )
    spec = _intent_spec(quantity, cadence)
    workspace = _workspace(
        placements=[
            ("candidate_a", "day_1", 13),
            ("candidate_b", "day_1", 14),
            ("candidate_c", "day_1", 15),
        ],
        matches=[_match("candidate_a", cadence.intent_id, IntentMatchStatus.MATCHED)],
    )

    report, gaps = evaluate_intent_fidelity(
        intent_spec=spec,
        rules=compile_composition_rules(spec),
        workspace=workspace,
        repair_budget_exhausted=False,
    )
    items = {item.intent_id: item for item in report.items}

    assert items[quantity.intent_id].missing_days == [1]
    assert items[cadence.intent_id].missing_days == [2]
    assert {gap.reason for gap in gaps} == {
        "quantity_rule_violated",
        "cadence_rule_missing",
    }


def test_fidelity_degrades_exhausted_hard_gap_and_soft_preference_to_deviations():
    hard = _intent(
        "intent_required_garden",
        kind=IntentKind.MUST_INCLUDE,
        strength=IntentStrength.HARD,
        value=ScalarIntentValue(value="garden"),
    )
    soft = _intent(
        "intent_architecture",
        kind=IntentKind.THEME,
        strength=IntentStrength.SOFT,
        value=ScalarIntentValue(value="architecture"),
    )
    spec = _intent_spec(hard, soft)

    report, gaps = evaluate_intent_fidelity(
        intent_spec=spec,
        rules=compile_composition_rules(spec),
        workspace=_workspace(placements=[]),
        repair_budget_exhausted=True,
    )

    assert report.blocking_gap_ids == []
    assert len(report.deviations) == 2
    assert all(gap.blocking is False for gap in gaps)


def test_fidelity_treats_a_materialized_itinerary_as_objective_fulfillment():
    objective = _intent(
        "intent_objective",
        kind=IntentKind.OBJECTIVE,
        strength=IntentStrength.HARD,
        value=ScalarIntentValue(value="create an executable trip"),
    ).model_copy(update={"target": IntentTarget.TRIP})
    spec = _intent_spec(objective)

    report, gaps = evaluate_intent_fidelity(
        intent_spec=spec,
        rules=compile_composition_rules(spec),
        workspace=_workspace(placements=[]),
        repair_budget_exhausted=False,
    )

    assert report.items[0].status.value == "satisfied"
    assert gaps == []


def test_cadence_attributes_cannot_be_satisfied_by_generic_entities():
    cadence = _intent(
        "intent_daily_cafe",
        kind=IntentKind.CADENCE,
        strength=IntentStrength.HARD,
        value=CadenceIntentValue(
            frequency="once_per_day",
            time_window="afternoon",
            required_attributes=["cafe"],
        ),
    )
    spec = _intent_spec(cadence)

    report, gaps = evaluate_intent_fidelity(
        intent_spec=spec,
        rules=compile_composition_rules(spec),
        workspace=_workspace(
            placements=[
                ("candidate_generic_1", "day_1", 14),
                ("candidate_generic_2", "day_2", 15),
            ]
        ),
        repair_budget_exhausted=False,
    )

    assert report.items[0].status.value == "unsatisfied"
    assert report.items[0].missing_days == [1, 2]
    assert gaps[0].reason == "cadence_rule_missing"
