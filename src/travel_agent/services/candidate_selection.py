from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from ..entities.candidate_discovery import CandidateDiscoveryOrigin
from ..entities.candidate_ranking import CandidateRankingScore
from ..entities.candidate_selection import (
    CandidateSelectionEntry,
    CandidateSelectionPlan,
    CandidateSelectionRole,
)
from ..entities.delivery_bundle import RecommendationCatalog, ResearchCandidate
from ..entities.intent_spec import (
    IntentKind,
    IntentSpec,
    IntentTarget,
    canonical_json_hash,
)
from ..entities.research_domain import ResearchDomain


CANDIDATE_SELECTION_POLICY_VERSION = "candidate_selection.v1"


def _domain(candidate: ResearchCandidate) -> ResearchDomain:
    if candidate.candidate_kind == "visit":
        return ResearchDomain.VISIT
    if candidate.candidate_kind == "dining":
        return ResearchDomain.DINING
    if candidate.candidate_kind == "lodging":
        return ResearchDomain.LODGING
    return (
        ResearchDomain.LONG_DISTANCE_TRANSPORT
        if candidate.transport_class == "long_distance"
        else ResearchDomain.LOCAL_TRANSPORT
    )


def _positive_intent_ids(intent_spec: IntentSpec) -> set[str]:
    targets = {
        IntentTarget.VISIT,
        IntentTarget.DINING,
        IntentTarget.LODGING,
        IntentTarget.LOCAL_TRANSPORT,
        IntentTarget.LONG_DISTANCE_TRANSPORT,
    }
    return {
        intent.intent_id
        for intent in intent_spec.active_items
        if intent.target in targets
        and intent.kind is not IntentKind.MUST_EXCLUDE
        and any(stage in intent.impact_stages for stage in ("research", "ranking"))
    }


def _capacity(
    domain: ResearchDomain, duration_days: int, destination_count: int
) -> int:
    if domain is ResearchDomain.VISIT:
        return max(duration_days * 2, 1)
    if domain is ResearchDomain.DINING:
        return max(duration_days, 1)
    if domain is ResearchDomain.LODGING:
        return max(destination_count, 1)
    if domain is ResearchDomain.LONG_DISTANCE_TRANSPORT:
        return max(destination_count + 1, 1)
    return max((duration_days - 1) * 3, 1)


def build_candidate_selection_plan(
    *,
    catalog: RecommendationCatalog,
    intent_spec: IntentSpec,
    ranking_scores: Iterable[CandidateRankingScore],
    duration_days: int,
    destination_count: int,
) -> CandidateSelectionPlan:
    candidates = catalog.candidate_index()
    rankings = {score.candidate_id: score for score in ranking_scores}
    discovery = {
        record.candidate_id: record for record in catalog.candidate_discovery_records
    }
    eligible = [
        candidate_id
        for candidate_id, score in rankings.items()
        if score.hard_eligible and candidate_id in candidates
    ]
    eligible.sort(
        key=lambda candidate_id: (
            tuple(-value for value in rankings[candidate_id].ranking_tuple),
            candidate_id,
        )
    )
    positive_intents = _positive_intent_ids(intent_spec)
    required_intents = {
        intent.intent_id
        for intent in intent_spec.active_items
        if intent.intent_id in positive_intents
        and intent.kind is IntentKind.MUST_INCLUDE
    }
    selected: list[str] = []
    roles: dict[str, CandidateSelectionRole] = {}
    covered: set[str] = set()
    counts: dict[ResearchDomain, int] = defaultdict(int)

    def add(candidate_id: str, role: CandidateSelectionRole) -> None:
        if candidate_id in selected:
            if role is CandidateSelectionRole.REQUIRED_PRIMARY:
                roles[candidate_id] = role
            return
        domain = _domain(candidates[candidate_id])
        selected.append(candidate_id)
        roles[candidate_id] = role
        counts[domain] += 1
        covered.update(
            set(rankings[candidate_id].matched_intent_ids) & positive_intents
        )

    selected_slot_ids = {
        result.candidate_id
        for result in catalog.admission_results
        if result.status == "passed" and result.selection_slot_id is not None
    }
    for candidate_id in eligible:
        if candidate_id in selected_slot_ids:
            add(candidate_id, CandidateSelectionRole.REQUIRED_PRIMARY)

    lodging_periods: set[tuple[str, object, object]] = set()
    transport_scopes: set[str] = set()
    for candidate_id in eligible:
        candidate = candidates[candidate_id]
        matched = set(rankings[candidate_id].matched_intent_ids)
        if matched & required_intents:
            add(candidate_id, CandidateSelectionRole.REQUIRED_PRIMARY)
        if candidate.candidate_kind == "lodging":
            period = (
                candidate.destination_id,
                candidate.check_in_date,
                candidate.check_out_date,
            )
            if period not in lodging_periods:
                lodging_periods.add(period)
                add(candidate_id, CandidateSelectionRole.REQUIRED_PRIMARY)
        elif (
            candidate.candidate_kind == "transport"
            and candidate.transport_class == "long_distance"
        ):
            scope = candidate.provider_evidence_scope_id or candidate.route_id
            if scope in transport_scopes:
                continue
            transport_scopes.add(scope)
            add(candidate_id, CandidateSelectionRole.REQUIRED_PRIMARY)

    while True:
        uncovered = positive_intents - covered
        options = [
            candidate_id
            for candidate_id in eligible
            if candidate_id not in selected
            and set(rankings[candidate_id].matched_intent_ids) & uncovered
        ]
        if not options:
            break
        options.sort(
            key=lambda candidate_id: (
                -len(set(rankings[candidate_id].matched_intent_ids) & uncovered),
                tuple(-value for value in rankings[candidate_id].ranking_tuple),
                candidate_id,
            )
        )
        add(options[0], CandidateSelectionRole.PRIMARY)

    for candidate_id in eligible:
        candidate = candidates[candidate_id]
        domain = _domain(candidate)
        if candidate_id in selected or counts[domain] >= _capacity(
            domain, duration_days, destination_count
        ):
            continue
        add(candidate_id, CandidateSelectionRole.PRIMARY)

    for domain in ResearchDomain:
        alternatives = [
            candidate_id
            for candidate_id in eligible
            if candidate_id not in selected
            and _domain(candidates[candidate_id]) is domain
        ][:2]
        for candidate_id in alternatives:
            add(candidate_id, CandidateSelectionRole.ALTERNATIVE)

    entries: list[CandidateSelectionEntry] = []
    rank_by_domain: dict[ResearchDomain, int] = defaultdict(int)
    for candidate_id in selected:
        candidate = candidates[candidate_id]
        domain = _domain(candidate)
        rank_by_domain[domain] += 1
        covered_ids = sorted(
            set(rankings[candidate_id].matched_intent_ids) & positive_intents
        )
        is_fallback = (
            CandidateDiscoveryOrigin.GENERIC_FALLBACK in discovery[candidate_id].origins
        )
        role = roles[candidate_id]
        if is_fallback and role is CandidateSelectionRole.PRIMARY:
            role = CandidateSelectionRole.FALLBACK
        reasons = [f"覆盖用户要求 {intent_id}" for intent_id in covered_ids[:2]]
        if not reasons:
            reasons = ["满足当前行程的结构性候选需求"]
        entries.append(
            CandidateSelectionEntry(
                candidate_id=candidate_id,
                domain=domain,
                destination_id=candidate.destination_id,
                role=role,
                rank=rank_by_domain[domain],
                covered_intent_ids=covered_ids,
                selection_reasons=reasons,
                eligible_for_composition=True,
            )
        )
    covered_ids = sorted(covered)
    uncovered_ids = sorted(positive_intents - covered)
    material = {
        "generation_id": catalog.generation_id,
        "intent_spec_revision": catalog.intent_spec_revision,
        "catalog_revision": catalog.fact_data_revision,
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "covered_intent_ids": covered_ids,
        "uncovered_intent_ids": uncovered_ids,
        "policy_version": CANDIDATE_SELECTION_POLICY_VERSION,
    }
    content_hash = canonical_json_hash(material)
    return CandidateSelectionPlan(
        selection_plan_id=f"selection_plan_{content_hash[:24]}",
        generation_id=catalog.generation_id,
        intent_spec_revision=catalog.intent_spec_revision,
        catalog_revision=catalog.fact_data_revision,
        entries=entries,
        covered_intent_ids=covered_ids,
        uncovered_intent_ids=uncovered_ids,
        policy_version=CANDIDATE_SELECTION_POLICY_VERSION,
        content_hash=content_hash,
    )


def catalog_for_candidate_selection(
    catalog: RecommendationCatalog,
    selection_plan: CandidateSelectionPlan,
) -> RecommendationCatalog:
    if selection_plan.generation_id != catalog.generation_id:
        raise ValueError("candidate selection plan belongs to another generation")
    if selection_plan.intent_spec_revision != catalog.intent_spec_revision:
        raise ValueError("candidate selection plan uses another intent revision")
    if selection_plan.catalog_revision != catalog.fact_data_revision:
        raise ValueError("candidate selection plan uses another catalog revision")
    selected_ids = selection_plan.composition_candidate_ids()
    if not selected_ids <= set(catalog.candidate_index()):
        raise ValueError("candidate selection plan references a missing candidate")
    return catalog.model_copy(
        update={
            "admission_results": [
                result
                for result in catalog.admission_results
                if result.candidate_id in selected_ids
            ]
        }
    )
