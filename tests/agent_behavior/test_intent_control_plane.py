import asyncio
import json
from types import SimpleNamespace

import pytest
from openai import OpenAIError

from travel_agent.agents.orchestrator.candidate_gate import _latest_packets
from travel_agent.agents.orchestrator.artifact_gate import artifact_gate_node
from travel_agent.entities.delivery_bundle import ResearchPacket
from travel_agent.entities.planning_generation import PlanningGeneration
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
from travel_agent.panels.constraint import deterministic_budget_constraints
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
    is_material_clause,
    normalize_clauses,
    split_source_clauses,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_native_schema", [False, True])
async def test_normalizer_projects_one_schema_by_provider_capability(
    supports_native_schema,
):
    clause = _clause(0, "2名成人")

    class _Model:
        capabilities = SimpleNamespace(supports_json_schema=supports_native_schema)

        def __init__(self):
            self.response_format = None
            self.max_output_tokens = None

        async def ainvoke(self, *_args, **kwargs):
            self.response_format = kwargs["response_format"]
            self.max_output_tokens = kwargs["max_output_tokens"]
            return json.dumps(
                {
                    "clauses": [
                        {
                            "clause_id": clause.clause_id,
                            "disposition": "controlled_identity",
                        }
                    ]
                }
            )

    model = _Model()
    await normalize_clauses(
        clauses=[clause], controlled_identity=_identity(), llm=model
    )

    wrapper = model.response_format["json_schema"]
    assert model.max_output_tokens == 8192
    assert wrapper["strict"] is supports_native_schema
    clause_array = wrapper["schema"]["properties"]["clauses"]
    assert clause_array["minItems"] == clause_array["maxItems"] == 1
    assert wrapper["schema"]["$defs"]["NormalizedClauseDraft"]["properties"][
        "clause_id"
    ]["enum"] == [clause.clause_id]
    params_schema = wrapper["schema"]["$defs"]["ConstraintParamsDraft"]
    if supports_native_schema:
        assert "amount" in params_schema["required"]
    else:
        assert "required" not in params_schema


@pytest.mark.asyncio
async def test_contract_rejection_gets_one_llm_semantic_repair_before_fallback():
    clause = _clause(0, "并给出备选方案")

    class _Model:
        capabilities = SimpleNamespace(supports_json_schema=False)

        def __init__(self):
            self.calls = []

        async def ainvoke(self, messages, **_kwargs):
            self.calls.append(messages)
            strength = "soft" if len(self.calls) == 1 else "hard"
            target = "itinerary" if len(self.calls) == 1 else "delivery"
            return json.dumps(
                {
                    "clauses": [
                        {
                            "clause_id": clause.clause_id,
                            "disposition": "mapped_to_intent",
                            "intents": [
                                {
                                    "kind": "alternatives",
                                    "target": target,
                                    "strength": strength,
                                    "priority": 90,
                                    "value": {
                                        "value_type": "alternative",
                                        "count": 2,
                                    },
                                    "verification_mode": "semantic",
                                    "impact_stages": ["composition", "projection"],
                                    "public_summary": "提供备选方案",
                                }
                            ],
                        }
                    ]
                }
            )

    model = _Model()
    result = await normalize_clauses(
        clauses=[clause], controlled_identity=_identity(), llm=model
    )

    assert len(model.calls) == 2
    assert "校验反馈" in model.calls[1][-1]["content"]
    [intent] = result.clauses[0].intents
    assert intent.kind is IntentKind.ALTERNATIVES
    assert intent.strength is IntentStrength.HARD
    assert intent.target is IntentTarget.DELIVERY


@pytest.mark.asyncio
async def test_repeated_repair_failure_gets_an_unanchored_semantic_regeneration():
    clause = _clause(0, "并给出备选方案")

    class _Model:
        capabilities = SimpleNamespace(supports_json_schema=False)

        def __init__(self):
            self.calls = []

        async def ainvoke(self, messages, **_kwargs):
            self.calls.append(messages)
            valid = len(self.calls) == 3
            return json.dumps(
                {
                    "clauses": [
                        {
                            "clause_id": clause.clause_id,
                            "disposition": "mapped_to_intent",
                            "intents": [
                                {
                                    "kind": "alternatives",
                                    "target": "delivery" if valid else "itinerary",
                                    "strength": "hard" if valid else "soft",
                                    "priority": 90,
                                    "value": {
                                        "value_type": "alternative",
                                        "count": 2,
                                    },
                                    "verification_mode": "semantic",
                                    "impact_stages": ["composition", "projection"],
                                    "public_summary": "提供备选方案",
                                }
                            ],
                        }
                    ]
                }
            )

    model = _Model()
    result = await normalize_clauses(
        clauses=[clause], controlled_identity=_identity(), llm=model
    )

    assert len(model.calls) == 3
    assert not any(message["role"] == "assistant" for message in model.calls[1])
    assert not any(message["role"] == "assistant" for message in model.calls[2])
    assert "只处理当前输入列出的" in model.calls[1][-1]["content"]
    assert "只处理当前输入列出的" in model.calls[2][-1]["content"]
    [intent] = result.clauses[0].intents
    assert intent.strength is IntentStrength.HARD
    assert intent.target is IntentTarget.DELIVERY


@pytest.mark.asyncio
async def test_slow_normalization_calls_share_one_operation_budget(monkeypatch):
    from travel_agent.services import intent_normalization as normalization_module

    clause = _clause(0, "并给出备选方案")

    class _Model:
        capabilities = SimpleNamespace(supports_json_schema=False)

        def __init__(self):
            self.calls = 0

        async def ainvoke(self, *_args, **_kwargs):
            self.calls += 1
            await asyncio.sleep(1)
            raise AssertionError("operation timeout must cancel the slow call")

    monkeypatch.setattr(
        normalization_module,
        "INTENT_NORMALIZATION_CALL_TIMEOUT_SECONDS",
        0.02,
    )
    monkeypatch.setattr(
        normalization_module,
        "INTENT_NORMALIZATION_OPERATION_TIMEOUT_SECONDS",
        0.035,
    )
    model = _Model()

    result = await normalize_clauses(
        clauses=[clause],
        controlled_identity=_identity(),
        llm=model,
    )

    assert model.calls == 2
    [intent] = result.clauses[0].intents
    assert intent.kind is IntentKind.ALTERNATIVES
    assert intent.strength is IntentStrength.HARD


@pytest.mark.asyncio
async def test_semantic_repair_only_regenerates_invalid_clause_rows():
    clauses = [
        _clause(0, "偏好建筑与本地文化"),
        _clause(1, "并给出备选方案"),
    ]

    class _Model:
        capabilities = SimpleNamespace(supports_json_schema=False)

        def __init__(self):
            self.calls = []

        async def ainvoke(self, messages, **_kwargs):
            self.calls.append(messages)
            if len(self.calls) == 1:
                rows = [
                    {
                        "clause_id": clauses[0].clause_id,
                        "disposition": "mapped_to_intent",
                        "intents": [
                            {
                                "kind": "theme",
                                "target": "trip",
                                "strength": "soft",
                                "priority": 70,
                                "value": {
                                    "value_type": "category",
                                    "categories": ["建筑与本地文化"],
                                },
                                "verification_mode": "semantic",
                                "impact_stages": ["research", "ranking"],
                                "public_summary": "偏好建筑与本地文化",
                            }
                        ],
                    },
                    {
                        "clause_id": clauses[1].clause_id,
                        "disposition": "mapped_to_intent",
                        "intents": [],
                    },
                ]
            else:
                rows = [
                    {
                        "clause_id": clauses[1].clause_id,
                        "disposition": "mapped_to_intent",
                        "intents": [
                            {
                                "kind": "alternatives",
                                "target": "delivery",
                                "strength": "hard",
                                "priority": 80,
                                "value": {
                                    "value_type": "alternative",
                                    "count": 2,
                                },
                                "verification_mode": "semantic",
                                "impact_stages": ["composition", "projection"],
                                "public_summary": "提供备选方案",
                            }
                        ],
                    }
                ]
            return json.dumps({"clauses": rows})

    model = _Model()
    result = await normalize_clauses(
        clauses=clauses,
        controlled_identity=_identity(),
        llm=model,
    )

    assert len(model.calls) == 2
    retry_payload = json.loads(model.calls[1][1]["content"])
    assert [row["clause_id"] for row in retry_payload["clauses"]] == [
        clauses[1].clause_id
    ]
    assert result.clauses[0].intents[0].kind is IntentKind.THEME
    assert result.clauses[1].intents[0].kind is IntentKind.ALTERNATIVES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_root",
    [
        lambda rows: {
            "clauses": rows,
            "summary": "provider-added metadata is not semantic authority",
        },
        lambda rows: rows,
    ],
)
async def test_normalizer_accepts_only_typed_rows_from_provider_root_variants(
    provider_root,
):
    clause = _clause(0, "并给出备选方案")

    class _Model:
        capabilities = SimpleNamespace(supports_json_schema=False)

        async def ainvoke(self, *_args, **_kwargs):
            rows = [
                {
                    "clause_id": clause.clause_id,
                    "disposition": "mapped_to_intent",
                    "intents": [
                        {
                            "kind": "alternatives",
                            "target": "delivery",
                            "strength": "hard",
                            "priority": 90,
                            "value": {
                                "value_type": "alternative",
                                "count": 2,
                            },
                            "verification_mode": "semantic",
                            "impact_stages": ["composition", "projection"],
                            "public_summary": "提供备选方案",
                        }
                    ],
                }
            ]
            return json.dumps(provider_root(rows))

    result = await normalize_clauses(
        clauses=[clause], controlled_identity=_identity(), llm=_Model()
    )

    [intent] = result.clauses[0].intents
    assert intent.kind is IntentKind.ALTERNATIVES
    assert intent.target is IntentTarget.DELIVERY


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
    assert [item["requirement_id"] for item in recognized["hard"]] == [hard.intent_id]
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
    assert result.clauses[0].disposition is ClauseDisposition.MAPPED_TO_INTENT
    [intent] = result.clauses[0].intents
    assert intent.kind is IntentKind.OUTPUT_REQUIREMENT
    assert intent.value.required_field == "每个景点都要说明选择理由"


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
    assert result.clauses[0].disposition is ClauseDisposition.MAPPED_TO_CONSTRAINT
    assert budget.category == "budget_cap"
    assert budget.value == "总预算不超过 3000 CNY"
    assert budget.params.amount == 3000
    assert budget.params.currency == "CNY"
    assert budget.params.per == "total"


@pytest.mark.asyncio
async def test_party_size_cannot_override_an_explicit_total_budget():
    clauses = [
        _clause(0, "2名成人"),
        _clause(1, "总预算人民币3000元以内"),
    ]

    class _Model:
        async def ainvoke(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "clauses": [
                        {
                            "clause_id": clauses[0].clause_id,
                            "disposition": "controlled_identity",
                            "reason_code": None,
                            "intents": [],
                            "constraints": [
                                {
                                    "category": "budget_cap",
                                    "value": "每晚预算不超过 2 CNY",
                                    "params": {
                                        "amount": 2,
                                        "currency": "CNY",
                                        "per": "night",
                                    },
                                }
                            ],
                        },
                        {
                            "clause_id": clauses[1].clause_id,
                            "disposition": "background_context",
                            "reason_code": None,
                            "intents": [],
                            "constraints": [],
                        },
                    ]
                }
            )

    result = await normalize_clauses(
        clauses=clauses, controlled_identity=_identity(), llm=_Model()
    )

    assert result.clauses[0].constraints == []
    assert result.clauses[1].disposition is ClauseDisposition.MAPPED_TO_CONSTRAINT
    [budget] = result.clauses[1].constraints
    assert budget.value == "总预算不超过 3000 CNY"
    assert budget.params.amount == 3000
    assert budget.params.per == "total"


def test_full_trip_sentence_does_not_treat_party_size_as_nightly_budget():
    text = (
        "上海到杭州两天一夜行程，2名成人，偏好建筑与本地文化，"
        "必须安排两个景点，总预算人民币3000元以内"
    )

    [budget] = deterministic_budget_constraints(text)
    assert budget["params"] == {
        "amount": 3000.0,
        "currency": "CNY",
        "per": "total",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_text", "expected_terms"),
    [
        ("必须安排西湖、郭庄", ["西湖", "郭庄"]),
        ("请务必参观故宫博物院、天坛公园", ["故宫博物院", "天坛公园"]),
        ("一定要打卡良渚博物院，京杭大运河博物馆", ["良渚博物院", "京杭大运河博物馆"]),
    ],
)
async def test_valid_model_semantics_are_not_overruled_by_fallback_grammar(
    source_text, expected_terms
):
    clause = _clause(0, source_text)

    class _Model:
        async def ainvoke(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "clauses": [
                        {
                            "clause_id": clause.clause_id,
                            "disposition": "mapped_to_intent",
                            "reason_code": None,
                            "intents": [
                                {
                                    "kind": "objective",
                                    "target": "trip",
                                    "strength": "hard",
                                    "priority": 90,
                                    "value": {
                                        "value_type": "scalar",
                                        "value": source_text,
                                    },
                                    "verification_mode": "mixed",
                                    "impact_stages": [
                                        "research",
                                        "composition",
                                        "projection",
                                    ],
                                    "public_summary": source_text,
                                }
                            ],
                            "constraints": [],
                        }
                    ]
                }
            )

    result = await normalize_clauses(
        clauses=[clause], controlled_identity=_identity(), llm=_Model()
    )

    [intent] = result.clauses[0].intents
    assert expected_terms
    assert intent.kind is IntentKind.OBJECTIVE
    assert intent.target is IntentTarget.TRIP
    assert intent.value.value == source_text


@pytest.mark.asyncio
async def test_provider_failure_uses_required_items_grammar_only_as_fallback():
    clause = _clause(0, "必须安排西湖、郭庄")

    class _ProviderFailureModel:
        async def ainvoke(self, *_args, **_kwargs):
            raise OpenAIError("provider returned invalid structured output")

    result = await normalize_clauses(
        clauses=[clause],
        controlled_identity=_identity(),
        llm=_ProviderFailureModel(),
    )

    assert [intent.value.categories for intent in result.clauses[0].intents] == [
        ["西湖"],
        ["郭庄"],
    ]


@pytest.mark.asyncio
async def test_valid_model_intents_are_returned_without_rule_rewriting():
    clause = _clause(0, "必须安排甲景点、乙景点")

    class _Model:
        async def ainvoke(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "clauses": [
                        {
                            "clause_id": clause.clause_id,
                            "disposition": "mapped_to_intent",
                            "reason_code": None,
                            "intents": [
                                {
                                    "kind": "must_include",
                                    "target": "visit",
                                    "strength": "hard",
                                    "priority": 73,
                                    "value": {
                                        "value_type": "category",
                                        "categories": ["甲景点"],
                                    },
                                    "verification_mode": "mixed",
                                    "impact_stages": ["research", "ranking"],
                                    "public_summary": "甲景点必须保留",
                                },
                                {
                                    "kind": "must_include",
                                    "target": "visit",
                                    "strength": "hard",
                                    "priority": 74,
                                    "value": {
                                        "value_type": "category",
                                        "categories": ["乙景点"],
                                    },
                                    "verification_mode": "mixed",
                                    "impact_stages": ["research", "ranking"],
                                    "public_summary": "乙景点必须保留",
                                },
                            ],
                            "constraints": [],
                        }
                    ]
                }
            )

    result = await normalize_clauses(
        clauses=[clause], controlled_identity=_identity(), llm=_Model()
    )

    assert [intent.priority for intent in result.clauses[0].intents] == [73, 74]
    assert [intent.public_summary for intent in result.clauses[0].intents] == [
        "甲景点必须保留",
        "乙景点必须保留",
    ]
    assert [intent.impact_stages for intent in result.clauses[0].intents] == [
        ["research", "ranking"],
        ["research", "ranking"],
    ]


@pytest.mark.asyncio
async def test_required_transport_service_gets_the_transport_domain():
    clause = _clause(0, "必须安排去返程高铁")

    class _ProviderFailureModel:
        async def ainvoke(self, *_args, **_kwargs):
            raise OpenAIError("provider truncated structured output")

    result = await normalize_clauses(
        clauses=[clause], controlled_identity=_identity(), llm=_ProviderFailureModel()
    )

    [intent] = result.clauses[0].intents
    assert intent.kind is IntentKind.MUST_INCLUDE
    assert intent.target is IntentTarget.LONG_DISTANCE_TRANSPORT
    assert intent.value.categories == ["去返程高铁"]


@pytest.mark.asyncio
async def test_provider_failure_maps_an_unnumbered_alternative_request():
    clause = _clause(0, "并给出备选方案")

    class _ProviderFailureModel:
        async def ainvoke(self, *_args, **_kwargs):
            raise OpenAIError("provider returned invalid structured output")

    result = await normalize_clauses(
        clauses=[clause],
        controlled_identity=_identity(),
        llm=_ProviderFailureModel(),
    )

    [intent] = result.clauses[0].intents
    assert intent.kind is IntentKind.ALTERNATIVES
    assert intent.target is IntentTarget.DELIVERY
    assert intent.value.count == 2
    assert result.clauses[0].reason_code is None


@pytest.mark.asyncio
async def test_provider_failure_keeps_safe_parts_and_flags_compound_clause():
    query = (
        "规划两天一夜行程，2名成人，偏好建筑与本地文化，"
        "必须安排甲景点、乙景点，总预算人民币3000元以内。"
        "请安排去返程高铁、住宿、每天的具体时间和交通，并给出备选方案。"
    )
    clauses = split_source_clauses([("query_main", "current_request", query)])

    class _ProviderFailureModel:
        async def ainvoke(self, *_args, **_kwargs):
            raise OpenAIError("provider returned invalid structured output")

    result = await normalize_clauses(
        clauses=clauses,
        controlled_identity=_identity(),
        llm=_ProviderFailureModel(),
    )

    material_dispositions = [
        draft.disposition
        for clause, draft in zip(clauses, result.clauses)
        if is_material_clause(clause.source_text)
    ]
    assert material_dispositions.count(ClauseDisposition.UNRESOLVED) == 1
    unresolved = [
        draft
        for draft in result.clauses
        if draft.disposition is ClauseDisposition.UNRESOLVED
    ]
    assert unresolved[0].reason_code == "semantic_normalization_required"
    intents = [intent for draft in result.clauses for intent in draft.intents]
    assert [
        intent.value.categories[0]
        for intent in intents
        if intent.kind is IntentKind.MUST_INCLUDE
    ] == ["甲景点", "乙景点"]
    assert any(intent.kind is IntentKind.ALTERNATIVES for intent in intents)
    [budget] = [
        constraint
        for draft in result.clauses
        for constraint in draft.constraints
        if constraint.category == "budget_cap"
    ]
    assert budget.params.amount == 3000
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


@pytest.mark.asyncio
async def test_artifact_gate_resolves_generation_qualified_worker_packet():
    packet = ResearchPacket.model_construct(
        generation_id="generation_current",
        worker_kind="destination_researcher",
        run_id="run_current",
    )
    generation = PlanningGeneration(
        generation_id="generation_current",
        controlled_trip_identity_revision=1,
        intent_spec_revision=1,
        constraint_pack_revision=1,
        plan_revision=0,
        identity_hash="0" * 64,
        intent_hash="1" * 64,
        constraint_hash="2" * 64,
    )
    state = TravelAgentState.model_construct(
        run_id="run_current",
        planning_generation=generation,
        execution_plan=[["destination_researcher"]],
        agent_status={"destination_researcher": "completed"},
        research_packets={
            "destination_researcher@generation_current": packet,
        },
    )

    update = await artifact_gate_node(state)

    assert update["artifact_gate_route"] == "accepted"
    assert update["artifact_status"] == {"destination_researcher": "accepted"}


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
    assert (
        update["rejected_intent_amendments"][0].reason_code
        == "identity_change_requires_new_run"
    )


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
    assert (
        update["rejected_intent_amendments"][0].reason_code == "unsupported_amendment"
    )


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
    assert (
        update["rejected_intent_amendments"][0].reason_code == "research_window_closed"
    )
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
