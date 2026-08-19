from __future__ import annotations

from collections import defaultdict

from ..entities.candidate_discovery import CandidateDiscoveryOrigin
from ..entities.candidate_intent import CandidateIntentMatch, IntentMatchStatus
from ..entities.candidate_ranking import CandidateRankingScore
from ..entities.delivery_bundle import RecommendationCatalog
from ..entities.intent_spec import IntentKind, IntentSpec, IntentStrength


CANDIDATE_RANKING_POLICY_VERSION = "candidate_ranking.v1"


def rank_candidates(
    *,
    catalog: RecommendationCatalog,
    intent_spec: IntentSpec,
    matches: list[CandidateIntentMatch],
) -> list[CandidateRankingScore]:
    intent_index = {intent.intent_id: intent for intent in intent_spec.active_items}
    matches_by_candidate: dict[str, list[CandidateIntentMatch]] = defaultdict(list)
    for match in matches:
        matches_by_candidate[match.candidate_id].append(match)
    admissions = {
        result.candidate_id: result
        for result in catalog.admission_results
        if result.selection_slot_id is None
    }
    discovery = {
        record.candidate_id: record for record in catalog.candidate_discovery_records
    }
    scores: list[CandidateRankingScore] = []
    for candidate_id in sorted(catalog.candidate_index()):
        admission = admissions.get(candidate_id)
        candidate_matches = matches_by_candidate.get(candidate_id, [])
        violated = sorted(
            match.intent_id
            for match in candidate_matches
            if match.status is IntentMatchStatus.VIOLATED
            and intent_index.get(match.intent_id) is not None
            and intent_index[match.intent_id].strength is IntentStrength.HARD
        )
        matched = sorted(
            match.intent_id
            for match in candidate_matches
            if match.status is IntentMatchStatus.MATCHED
        )
        unknown = sorted(
            match.intent_id
            for match in candidate_matches
            if match.status is IntentMatchStatus.UNKNOWN
        )
        applicable_intent_ids = {
            match.intent_id
            for match in candidate_matches
            if match.status is not IntentMatchStatus.NOT_APPLICABLE
        }
        priority_total = sum(
            intent_index[intent_id].priority
            for intent_id in matched
            if intent_id in intent_index
            and intent_index[intent_id].kind is not IntentKind.MUST_EXCLUDE
        )
        priority_possible = sum(
            intent.priority
            for intent in intent_spec.active_items
            if intent.intent_id in applicable_intent_ids
            and intent.priority >= 70
            and intent.kind is not IntentKind.MUST_EXCLUDE
        )
        high_priority_coverage = min(
            priority_total / max(priority_possible, 1),
            1.0,
        )
        decided = [
            match
            for match in candidate_matches
            if match.status
            not in {IntentMatchStatus.UNKNOWN, IntentMatchStatus.NOT_APPLICABLE}
        ]
        semantic_scores = [
            match.score
            for match in candidate_matches
            if match.method == "semantic_batch_evaluation"
            and match.score is not None
            and intent_index.get(match.intent_id) is not None
            and intent_index[match.intent_id].kind is not IntentKind.MUST_EXCLUDE
        ]
        evidence_confidence = len(decided) / max(
            len(
                [
                    match
                    for match in candidate_matches
                    if match.status is not IntentMatchStatus.NOT_APPLICABLE
                ]
            ),
            1,
        )
        fit = admission.fit_scores if admission is not None else None
        fallback_penalty = (
            0.2
            if CandidateDiscoveryOrigin.GENERIC_FALLBACK
            in discovery[candidate_id].origins
            else 0.0
        )
        hard_eligible = bool(
            admission is not None and admission.status == "passed" and not violated
        )
        semantic_fit = (
            sum(float(value) for value in semantic_scores) / len(semantic_scores)
            if semantic_scores
            else 0.0
        )
        budget_fit = fit.budget_fit if fit is not None else 0.0
        weather_fit = fit.weather_fit if fit is not None else 0.0
        constraint_fit = fit.constraint_fit if fit is not None else 0.0
        ranking_tuple = [
            1.0 if hard_eligible else 0.0,
            float(
                len(
                    [
                        intent_id
                        for intent_id in matched
                        if intent_index.get(intent_id)
                        and intent_index[intent_id].priority >= 80
                    ]
                )
            ),
            high_priority_coverage,
            semantic_fit,
            budget_fit,
            weather_fit,
            constraint_fit,
            evidence_confidence,
            1.0,
            1.0,
            -fallback_penalty,
            0.0,
            0.0,
        ]
        scores.append(
            CandidateRankingScore(
                candidate_id=candidate_id,
                generation_id=catalog.generation_id,
                hard_eligible=hard_eligible,
                hard_violation_intent_ids=violated,
                matched_intent_ids=matched,
                unknown_intent_ids=unknown,
                high_priority_coverage_score=high_priority_coverage,
                semantic_fit=semantic_fit,
                evidence_confidence=evidence_confidence,
                budget_fit=budget_fit,
                weather_fit=weather_fit,
                constraint_fit=constraint_fit,
                regional_fit=1.0,
                diversity_potential=1.0,
                generic_fallback_penalty=fallback_penalty,
                redundancy_penalty=0.0,
                travel_cost_penalty=0.0,
                ranking_tuple=ranking_tuple,
                policy_version=CANDIDATE_RANKING_POLICY_VERSION,
            )
        )
    return scores
