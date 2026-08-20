import json
from types import SimpleNamespace

import pytest

from travel_agent.agents.orchestrator.candidate_gate import _latest_packets
from travel_agent.entities.delivery_bundle import ResearchPacket
from travel_agent.entities.intent_spec import (
    AlternativeIntentValue,
    CategoryIntentValue,
    IntentItem,
    IntentConflict,
    IntentKind,
    IntentStrength,
    IntentTarget,
    OutputRequirementValue,
    ScalarIntentValue,
    VerificationMode,
)
from travel_agent.entities.request_contract import ClauseDisposition
from travel_agent.entities.request_contract import IntentAmendment
from travel_agent.entities.state import TravelAgentState
from travel_agent.services.capability_planning import (
    build_capability_plan,
    build_research_brief,
)
from travel_agent.services.intent_conflicts import detect_intent_conflicts
from travel_agent.services.intent_normalization import (
    IntentDraft,
    NormalizedClauseDraft,
    RequestContractNormalizationResult,
    SourceClause,
    normalize_clauses,
)
from travel_agent.services.intent_revision import build_request_contract_revision
from travel_agent.services.research_query_planner import build_research_query_plan
from travel_agent.workflows import intent_amendments as intent_amendment_workflow
from travel_agent.workflows.intent_amendments import apply_runtime_amendments
from travel_agent.workflows.travel_planning import _build_plan_gate_payload


def _identity():
    return {
        "origin": {
            "place_id": "origin_shanghai",
            "provider": "manual_verified",
            "kind": "city",
            "name": "上海",
            "display_name": "上海",
            "country_code": "CN",
            "latitude": 31.2304,
            "longitude": 121.4737,
            "admin_path": ["上海"],
        },
        "destinations": [
            {
                "place_id": "destination_hangzhou",
                "provider": "manual_verified",
                "kind": "city",
                "name": "杭州",
                "display_name": "杭州",
                "country_code": "CN",
                "latitude": 30.2741,
                "longitude": 120.1551,
                "admin_path": ["浙江", "杭州"],
            }
        ],
        "start_date": "2026-09-01",
        "end_date": "2026-09-03",
        "party": {
            "adults": 2,
            "children": 0,
            "elderly_companions": False,
            "accessibility_required": False,
        },
        "style": {
            "primary": "建筑与本地文化",
            "secondary_interests": ["咖啡"],
            "source": "current",
        },
    }


def _clause(index: int, text: str, source_kind: str = "current_request"):
    return SourceClause(
        clause_id=f"clause_{index}",
        source_ref_id="query_main" if source_kind == "current_request" else "command_1",
        source_kind=source_kind,
        source_text=text,
        span_start=index * 20,
        span_end=index * 20 + len(text),
    )


def _intent(
    kind: IntentKind,
    target: IntentTarget,
    value,
    summary: str,
    stages: list[str],
):
    return IntentDraft(
        kind=kind,
        target=target,
        strength=IntentStrength.HARD,
        priority=90,
        value=value,
        verification_mode=VerificationMode.MIXED,
        impact_stages=stages,
        public_summary=summary,
    )


def _contract():
    clauses = [
        _clause(0, "帮我规划杭州行程"),
        _clause(1, "不要连锁咖啡"),
        _clause(2, "每个景点说明选择理由"),
        _clause(3, "给两套方案"),
    ]
    normalized = RequestContractNormalizationResult(
        clauses=[
            NormalizedClauseDraft(
                clause_id="clause_0",
                disposition=ClauseDisposition.MAPPED_TO_INTENT,
                intents=[
                    _intent(
                        IntentKind.OBJECTIVE,
                        IntentTarget.TRIP,
                        ScalarIntentValue(value="规划杭州可执行行程"),
                        "规划杭州可执行行程",
                        ["research", "composition", "projection"],
                    )
                ],
            ),
            NormalizedClauseDraft(
                clause_id="clause_1",
                disposition=ClauseDisposition.MAPPED_TO_INTENT,
                intents=[
                    _intent(
                        IntentKind.MUST_EXCLUDE,
                        IntentTarget.DINING,
                        CategoryIntentValue(categories=["连锁咖啡"]),
                        "排除连锁咖啡",
                        ["research", "admission", "composition"],
                    )
                ],
            ),
            NormalizedClauseDraft(
                clause_id="clause_2",
                disposition=ClauseDisposition.MAPPED_TO_INTENT,
                intents=[
                    _intent(
                        IntentKind.OUTPUT_REQUIREMENT,
                        IntentTarget.DELIVERY,
                        OutputRequirementValue(
                            required_field="景点选择理由",
                            applies_to="each_item",
                        ),
                        "每个景点说明选择理由",
                        ["projection"],
                    )
                ],
            ),
            NormalizedClauseDraft(
                clause_id="clause_3",
                disposition=ClauseDisposition.MAPPED_TO_INTENT,
                intents=[
                    _intent(
                        IntentKind.ALTERNATIVES,
                        IntentTarget.DELIVERY,
                        AlternativeIntentValue(count=2),
                        "交付两套可区分方案",
                        ["composition", "projection"],
                    )
                ],
            ),
        ]
    )
    return build_request_contract_revision(
        run_id="run_intent_contract",
        identity=_identity(),
        identity_revision=1,
        constraint_pack={
            "constraints": [],
            "hard_constraints": [],
            "soft_preferences": [],
            "pack_meta": {},
        },
        constraint_pack_revision=1,
        clauses=clauses,
        normalized=normalized,
    )


def test_contract_and_capability_plan_are_deterministic_and_cover_hard_intents():
    contract, generation = _contract()
    repeated, repeated_generation = _contract()

    assert contract.content_hash == repeated.content_hash
    assert generation == repeated_generation
    assert len(contract.clause_ledger) == 4
    assert not contract.has_unresolved_material_clauses

    brief = build_research_brief(contract, _identity())
    query_plan = build_research_query_plan(
        intent_spec=contract.intent_spec,
        brief=brief,
    )
    plan = build_capability_plan(
        request_contract=contract,
        brief=brief,
        plan_revision=generation.plan_revision,
        research_query_plan=query_plan,
    )
    hard_ids = {
        item.intent_id
        for item in contract.intent_spec.active_items
        if item.strength is IntentStrength.HARD
    }
    owned_ids = {
        intent_id
        for assignment in plan.assignments.values()
        for intent_id in assignment.must_cover_intent_ids
    }
    assert hard_ids == owned_ids
    assert plan.execution_plan[-1] == ["itinerary_planner"]
    assert all(
        assignment.generation_id == generation.generation_id
        for assignment in plan.assignments.values()
    )
    assert {
        query_id
        for assignment in plan.assignments.values()
        for query_id in assignment.research_query_ids
    } == set(query_plan.query_index())


def test_plan_gate_separates_hard_preferences_and_attention():
    contract, _generation = _contract()
    hard = contract.intent_spec.active_items[0]
    soft = contract.intent_spec.active_items[1].model_copy(
        update={"intent_id": "intent_soft", "strength": IntentStrength.SOFT}
    )
    intent_spec = contract.intent_spec.model_copy(
        update={
            "active_items": [hard, soft],
            "conflicts": [
                IntentConflict(
                    conflict_id="conflict_test",
                    intent_ids=[hard.intent_id, soft.intent_id],
                    conflict_type="direct_contradiction",
                    blocking=False,
                    user_visible_summary="两项要求需要取舍",
                )
            ],
        }
    )
    payload = _build_plan_gate_payload(
        SimpleNamespace(
            execution_plan=[["destination_researcher"]],
            agent_assignments={
                "destination_researcher": {"objective": "research destinations"}
            },
            plan_gate_revision_count=0,
            constraint_pack={"hard_constraints": []},
            intent_spec=intent_spec,
        )
    )

    recognized = payload["recognized_requirements"]
    assert [item["requirement_id"] for item in recognized["hard"]] == [
        hard.intent_id
    ]
    assert [item["requirement_id"] for item in recognized["preferences"]] == [
        soft.intent_id
    ]
    assert recognized["attention"] == [
        {"requirement_id": "conflict_test", "summary": "两项要求需要取舍"}
    ]
    assert "must_obey" not in payload


@pytest.mark.asyncio
async def test_material_clause_cannot_be_silently_classified_as_background():
    clause = _clause(0, "每个景点都要说明选择理由")

    class _Model:
        async def ainvoke(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "clauses": [
                        {
                            "clause_id": clause.clause_id,
                            "disposition": "background_context",
                            "reason_code": None,
                            "intents": [],
                            "constraints": [],
                        }
                    ]
                }
            )

    result = await normalize_clauses(
        clauses=[clause], controlled_identity=_identity(), llm=_Model()
    )
    assert result.clauses[0].disposition is ClauseDisposition.UNRESOLVED
    assert result.clauses[0].reason_code == "material_clause_not_mapped"


@pytest.mark.asyncio
async def test_numeric_budget_cap_survives_model_omission():
    clause = _clause(0, "总预算人民币3000元以内")

    class _Model:
        async def ainvoke(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "clauses": [
                        {
                            "clause_id": clause.clause_id,
                            "disposition": "background_context",
                            "reason_code": None,
                            "intents": [],
                            "constraints": [],
                        }
                    ]
                }
            )

    result = await normalize_clauses(
        clauses=[clause], controlled_identity=_identity(), llm=_Model()
    )

    budget = result.clauses[0].constraints[0]
    assert budget.category == "budget_cap"
    assert budget.value == "总预算不超过 3000 CNY"
    assert budget.params.amount == 3000
    assert budget.params.currency == "CNY"
    assert budget.params.per == "total"


def test_include_exclude_conflict_blocks_the_contract():
    common = {
        "target": IntentTarget.VISIT,
        "strength": IntentStrength.HARD,
        "priority": 90,
        "source_kind": "current_request",
        "source_ref_id": "query_main",
        "verification_mode": VerificationMode.MIXED,
        "impact_stages": ["admission"],
    }
    include = IntentItem(
        intent_id="intent_include",
        kind=IntentKind.MUST_INCLUDE,
        value=CategoryIntentValue(categories=["故宫"]),
        public_summary="必须安排故宫",
        **common,
    )
    exclude = IntentItem(
        intent_id="intent_exclude",
        kind=IntentKind.MUST_EXCLUDE,
        value=CategoryIntentValue(categories=["故宫"]),
        public_summary="不要故宫",
        **common,
    )

    conflicts = detect_intent_conflicts([include, exclude])
    assert len(conflicts) == 1
    assert conflicts[0].blocking is True


def test_current_exclusion_supersedes_a_saved_include_preference():
    clauses = [
        _clause(0, "不要故宫"),
        _clause(1, "喜欢安排故宫", "saved_preference"),
    ]
    normalized = RequestContractNormalizationResult(
        clauses=[
            NormalizedClauseDraft(
                clause_id="clause_0",
                disposition=ClauseDisposition.MAPPED_TO_INTENT,
                intents=[
                    _intent(
                        IntentKind.MUST_EXCLUDE,
                        IntentTarget.VISIT,
                        CategoryIntentValue(categories=["故宫"]),
                        "不要故宫",
                        ["research", "admission", "composition"],
                    )
                ],
            ),
            NormalizedClauseDraft(
                clause_id="clause_1",
                disposition=ClauseDisposition.MAPPED_TO_INTENT,
                intents=[
                    _intent(
                        IntentKind.MUST_INCLUDE,
                        IntentTarget.VISIT,
                        CategoryIntentValue(categories=["故宫"]),
                        "喜欢安排故宫",
                        ["ranking"],
                    )
                ],
            ),
        ]
    )

    contract, _generation = build_request_contract_revision(
        run_id="run_precedence",
        identity=_identity(),
        identity_revision=1,
        constraint_pack={
            "constraints": [],
            "hard_constraints": [],
            "soft_preferences": [],
            "pack_meta": {},
        },
        constraint_pack_revision=1,
        clauses=clauses,
        normalized=normalized,
    )

    assert [item.kind for item in contract.intent_spec.active_items] == [
        IntentKind.MUST_EXCLUDE
    ]
    assert [item.kind for item in contract.intent_spec.superseded_items] == [
        IntentKind.MUST_INCLUDE
    ]
    assert contract.intent_spec.conflicts == []


def test_latest_packets_rejects_an_obsolete_generation():
    old_packet = ResearchPacket.model_construct(
        generation_id="generation_old",
        worker_kind="destination_researcher",
    )
    current_packet = ResearchPacket.model_construct(
        generation_id="generation_current",
        worker_kind="destination_researcher",
    )

    latest = _latest_packets(
        {
            "destination_researcher@generation_old": old_packet,
            "destination_researcher_r2@generation_current": current_packet,
        },
        generation_id="generation_current",
    )
    assert latest == {"destination_researcher": current_packet}


def test_runtime_supplement_routes_through_contract_normalization():
    amendment = IntentAmendment(
        command_id="command_runtime_1",
        category="must_do",
        content="必须安排一家本地早餐店",
        source_kind="run_supplement",
    )
    update = apply_runtime_amendments(
        TravelAgentState(
            run_id="run_amendment",
            pending_intent_amendments=[amendment],
            intent_amendment_resume_node="dispatcher",
        )
    )

    assert update["intent_amendment_route"] == "request_contract_normalizer"
    assert update["pending_intent_amendments"][0].impact.value == "research_affecting"
    assert update["plan_gate_revision_count"] == 1


def test_identity_amendment_is_rejected_and_resumes_the_interrupted_node():
    amendment = IntentAmendment(
        command_id="command_runtime_identity",
        category="other",
        content="把目的地改成苏州",
        source_kind="run_supplement",
    )
    update = apply_runtime_amendments(
        TravelAgentState(
            run_id="run_amendment",
            pending_intent_amendments=[amendment],
            intent_amendment_resume_node="candidate_gate",
        )
    )

    assert update["pending_intent_amendments"] == []
    assert update["intent_amendment_route"] == "candidate_gate"
    assert update["rejected_intent_amendments"][0].reason_code == "identity_change_requires_new_run"


def test_unsupported_transaction_amendment_is_rejected():
    amendment = IntentAmendment(
        command_id="command_runtime_purchase",
        category="other",
        content="帮我购买机票并直接付款",
        source_kind="run_supplement",
    )
    update = apply_runtime_amendments(
        TravelAgentState(
            run_id="run_amendment",
            pending_intent_amendments=[amendment],
            intent_amendment_resume_node="dispatcher",
        )
    )

    assert update["pending_intent_amendments"] == []
    assert update["intent_amendment_route"] == "dispatcher"
    assert update["rejected_intent_amendments"][0].reason_code == "unsupported_amendment"


def test_research_amendment_is_rejected_after_the_research_window(monkeypatch):
    amendment = IntentAmendment(
        command_id="command_runtime_late_research",
        category="must_do",
        content="必须安排一家本地早餐店",
        source_kind="run_supplement",
    )
    state = TravelAgentState(
        run_id="run_amendment",
        pending_intent_amendments=[amendment],
        intent_amendment_resume_node="itinerary_planner",
    )
    state.run_deadline = object()
    monkeypatch.setattr(
        intent_amendment_workflow,
        "observe_run_deadline",
        lambda deadline: (
            deadline,
            SimpleNamespace(research_closed=True, composition_closed=False),
        ),
    )

    update = apply_runtime_amendments(state)

    assert update["pending_intent_amendments"] == []
    assert update["intent_amendment_route"] == "itinerary_planner"
    assert update["rejected_intent_amendments"][0].reason_code == "research_window_closed"
    assert update["rejected_intent_amendments"][0].requires_new_run is True


def test_projection_only_revision_preserves_the_research_generation():
    contract, generation = _contract()
    clause = _clause(4, "报告输出要简洁", "plan_gate_amendment")
    normalized = RequestContractNormalizationResult(
        clauses=[
            NormalizedClauseDraft(
                clause_id=clause.clause_id,
                disposition=ClauseDisposition.MAPPED_TO_INTENT,
                intents=[
                    _intent(
                        IntentKind.OUTPUT_REQUIREMENT,
                        IntentTarget.DELIVERY,
                        OutputRequirementValue(
                            required_field="简洁报告",
                            applies_to="delivery",
                        ),
                        "报告输出要简洁",
                        ["projection"],
                    )
                ],
            )
        ]
    )
    revised, revised_generation = build_request_contract_revision(
        run_id="run_intent_contract",
        identity=_identity(),
        identity_revision=1,
        constraint_pack=contract.constraint_pack,
        constraint_pack_revision=1,
        clauses=[clause],
        normalized=normalized,
        previous=contract,
        plan_revision=1,
        preserve_generation_id=True,
    )

    assert revised.generation_id == generation.generation_id
    assert revised.intent_spec.revision == contract.intent_spec.revision + 1
    assert len(revised.clause_ledger) == len(contract.clause_ledger) + 1
