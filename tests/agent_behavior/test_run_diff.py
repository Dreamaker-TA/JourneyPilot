import json

from travel_agent.entities.trip_run import build_trip_run_completion_audit
from travel_agent.services.run_diff import build_run_diff


def _audit_state() -> dict:
    return {
        "run_id": "run_test",
        "minimum_delivery_draft": {
            "draft_id": "draft_test",
            "run_id": "run_test",
            "planning_authorized": True,
            "planning_authorized_at": "2026-08-19T10:00:00Z",
            "planning_generation_id": "generation_test",
            "intent_spec_revision": 1,
            "intent_spec_hash": "a" * 64,
            "constraint_pack_revision": 1,
            "controlled_trip_identity_revision": 1,
            "plan_revision": 0,
            "preserved_constraint_ids": [],
        },
        "run_deadline": {
            "draft_id": "draft_test",
            "planning_authorized_at": "2026-08-19T10:00:00Z",
        },
        "planning_generation": {
            "generation_id": "generation_test",
            "identity_hash": "b" * 64,
            "intent_hash": "a" * 64,
            "constraint_hash": "c" * 64,
        },
        "intent_spec": {
            "schema_version": "journeypilot.intent_spec.v1",
            "revision": 1,
            "content_hash": "a" * 64,
            "active_items": [
                {
                    "intent_id": "intent_arch",
                    "impact_stages": ["ranking", "composition"],
                    "source_text": "private raw user sentence",
                }
            ],
            "unresolved_clauses": [],
        },
        "intent_spec_revision": 1,
        "constraint_pack": {"pack_meta": {}},
        "constraint_pack_revision": 1,
        "plan_gate_revision_count": 0,
        "model_versions": {"primary": "model-primary", "fast": "model-fast"},
        "research_query_plan": {
            "content_hash": "d" * 64,
            "policy_version": "research_query.v1",
        },
        "trip_workspace_v2": {
            "workspace_revision": 0,
            "itinerary": {
                "visit_stops": [{}],
                "dining_stops": [],
                "lodging_stays": [],
                "transport_legs": [],
            },
            "recommendation_catalog": {
                "research_packets": [
                    {
                        "executed_query_ids": ["query_arch"],
                        "source_records": [{"content_hash": "e" * 64}],
                    }
                ],
                "candidate_discovery_records": [],
                "admission_results": [
                    {"candidate_id": "candidate_arch", "status": "passed"}
                ],
                "candidate_ranking_scores": [
                    {"candidate_id": "candidate_arch", "policy_version": "ranking.v1"}
                ],
            },
            "candidate_selection_plan": {
                "content_hash": "f" * 64,
                "covered_intent_ids": ["intent_arch"],
                "entries": [
                    {
                        "candidate_id": "candidate_arch",
                        "eligible_for_composition": True,
                    }
                ],
                "selection_policy": {
                    "mode": "deterministic",
                    "selection_seed": None,
                    "policy_version": "candidate_selection.v1",
                },
            },
            "intent_coverage_report": {
                "content_hash": "1" * 64,
                "hard_satisfaction_rate": 1.0,
                "soft_coverage_rate": 1.0,
                "items": [{"intent_id": "intent_arch", "status": "satisfied"}],
            },
            "composition_mutations": [],
            "user_input_anchors": [],
        },
    }


def test_completion_audit_records_replay_layers_without_raw_user_text():
    audit = build_trip_run_completion_audit(_audit_state())
    serialized = json.dumps(audit, ensure_ascii=False)

    assert audit["research"]["query_plan_hash"] == "d" * 64
    assert audit["selection"]["selected_candidate_ids"] == ["candidate_arch"]
    assert audit["intent_fidelity"]["hard_satisfaction_rate"] == 1.0
    assert audit["behavior_metrics"]["intent_assignment_coverage_rate"] == 1.0
    assert audit["versions"]["model_versions"]["primary"] == "model-primary"
    assert audit["versions"]["prompt_versions"]["itinerary_composition"] == (
        "itinerary_composition.v1"
    )
    assert "private raw user sentence" not in serialized


def test_run_diff_locates_selection_and_composition_delta():
    left = build_trip_run_completion_audit(_audit_state())
    right = json.loads(json.dumps(left))
    right["planning_generation"]["intent_hash"] = "2" * 64
    right["selection"].update(
        {
            "mode": "explore",
            "selection_plan_hash": "3" * 64,
            "selected_candidate_ids": ["candidate_other"],
        }
    )
    right["composition"]["workspace_hash"] = "4" * 64

    report = build_run_diff(left, right)

    assert report.sections["intent"].changed is True
    assert report.sections["query"].changed is False
    assert report.sections["selection"].added_ids == ["candidate_other"]
    assert report.sections["selection"].removed_ids == ["candidate_arch"]
    assert report.semantic_delta_sensitivity == 1.0
    assert report.selection_overlap_rate == 0.0
    assert report.explore_diversity_rate == 1.0
