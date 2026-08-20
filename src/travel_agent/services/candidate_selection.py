from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from ..entities.candidate_discovery import CandidateDiscoveryOrigin
from ..entities.candidate_ranking import CandidateRankingScore
from ..entities.candidate_selection import (
    CandidateSelectionEntry,
    CandidateSelectionPlan,
    CandidateSelectionRole,
    SelectedCandidateCapability,
    SelectionPolicy,
)
from ..entities.delivery_bundle import (
    DiningCandidate,
    LodgingCandidate,
    RecommendationCatalog,
    ResearchCandidate,
    TransportCandidate,
    VisitCandidate,
)
from ..entities.intent_spec import (
    AlternativeIntentValue,
    IntentKind,
    IntentSpec,
    IntentTarget,
    ScalarIntentValue,
    canonical_json_hash,
)
from ..entities.research_domain import ResearchDomain


CANDIDATE_SELECTION_POLICY_VERSION = "candidate_selection.v1"


def rebind_candidate_selection_catalog_revision(
    plan: CandidateSelectionPlan,
    catalog_revision: int,
) -> CandidateSelectionPlan:
    material = {
        "generation_id": plan.generation_id,
        "intent_spec_revision": plan.intent_spec_revision,
        "catalog_revision": catalog_revision,
        "entries": [entry.model_dump(mode="json") for entry in plan.entries],
        "covered_intent_ids": plan.covered_intent_ids,
        "uncovered_intent_ids": plan.uncovered_intent_ids,
        "selection_policy": plan.selection_policy.model_dump(mode="json"),
    }
    content_hash = canonical_json_hash(material)
    return plan.model_copy(
        update={
            "selection_plan_id": f"selection_plan_{content_hash[:24]}",
            "catalog_revision": catalog_revision,
            "content_hash": content_hash,
        }
    )


def selection_policy_from_intent_spec(
    intent_spec: IntentSpec,
    *,
    avoid_previous_candidate_ids: Iterable[str] = (),
) -> SelectionPolicy:
    explore_intents = [
        intent
        for intent in intent_spec.active_items
        if intent.kind in {IntentKind.DIVERSITY, IntentKind.ALTERNATIVES}
    ]
    if not explore_intents:
        return SelectionPolicy(policy_version=CANDIDATE_SELECTION_POLICY_VERSION)
    alternative_count = max(
        (
            intent.value.count
            for intent in explore_intents
            if isinstance(intent.value, AlternativeIntentValue)
        ),
        default=1,
    )
    theme_clusters = sorted(
        {
            intent.value.value
            for intent in explore_intents
            if isinstance(intent.value, ScalarIntentValue)
        }
    )
    seed_material = {
        "generation_id": intent_spec.generation_id,
        "intent_spec_revision": intent_spec.revision,
        "explore_intent_ids": sorted(intent.intent_id for intent in explore_intents),
    }
    seed = int(canonical_json_hash(seed_material)[:16], 16)
    return SelectionPolicy(
        mode="explore",
        selection_seed=seed,
        alternative_count=alternative_count,
        diversity_strength=0.65,
        avoid_previous_candidate_ids=sorted(set(avoid_previous_candidate_ids)),
        preferred_theme_clusters=theme_clusters,
        policy_version=CANDIDATE_SELECTION_POLICY_VERSION,
    )


def _ranking_quality(score: CandidateRankingScore) -> float:
    return (
        score.high_priority_coverage_score
        + score.semantic_fit
        + score.evidence_confidence
        + score.budget_fit
        + score.weather_fit
        + score.constraint_fit
        + score.regional_fit
        + score.diversity_potential
        - score.generic_fallback_penalty
        - score.redundancy_penalty
        - score.travel_cost_penalty
    )


def _apply_explore_order(
    candidate_ids: list[str],
    *,
    candidates: dict[str, ResearchCandidate],
    rankings: dict[str, CandidateRankingScore],
    policy: SelectionPolicy,
) -> list[str]:
    if policy.mode != "explore" or policy.selection_seed is None:
        return candidate_ids
    previous = set(policy.avoid_previous_candidate_ids)
    leader_quality: dict[tuple[ResearchDomain, str], float] = {}
    for candidate_id in candidate_ids:
        candidate = candidates[candidate_id]
        key = (_domain(candidate), candidate.destination_id)
        leader_quality.setdefault(key, _ranking_quality(rankings[candidate_id]))
    threshold = 0.2 * policy.diversity_strength

    def key(candidate_id: str):
        candidate = candidates[candidate_id]
        group = (_domain(candidate), candidate.destination_id)
        score = rankings[candidate_id]
        quality = _ranking_quality(score)
        close_to_leader = leader_quality[group] - quality <= threshold
        seeded_order = canonical_json_hash(
            {
                "seed": policy.selection_seed,
                "candidate_id": candidate_id,
                "policy_version": policy.policy_version,
            }
        )
        return (
            group[0].value,
            group[1],
            candidate_id in previous,
            not close_to_leader,
            seeded_order if close_to_leader else "",
            tuple(-value for value in score.ranking_tuple),
            candidate_id,
        )

    return sorted(candidate_ids, key=key)


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
        and intent.kind
        not in {
            IntentKind.MUST_EXCLUDE,
            IntentKind.DIVERSITY,
            IntentKind.ALTERNATIVES,
        }
        and any(stage in intent.impact_stages for stage in ("research", "ranking"))
    }


def _admitted_candidate_ids(catalog: RecommendationCatalog) -> set[str]:
    return {
        result.candidate_id
        for result in catalog.admission_results
        if result.status == "passed"
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
    selection_policy: SelectionPolicy | None = None,
) -> CandidateSelectionPlan:
    candidates = catalog.candidate_index()
    rankings = {score.candidate_id: score for score in ranking_scores}
    discovery = {
        record.candidate_id: record for record in catalog.candidate_discovery_records
    }
    admitted_candidate_ids = _admitted_candidate_ids(catalog)
    eligible = [
        candidate_id
        for candidate_id, score in rankings.items()
        if score.hard_eligible
        and candidate_id in candidates
        and candidate_id in admitted_candidate_ids
    ]
    eligible.sort(
        key=lambda candidate_id: (
            tuple(-value for value in rankings[candidate_id].ranking_tuple),
            candidate_id,
        )
    )
    policy = selection_policy or selection_policy_from_intent_spec(intent_spec)
    eligible = _apply_explore_order(
        eligible,
        candidates=candidates,
        rankings=rankings,
        policy=policy,
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
        alternative_limit = max(policy.alternative_count - 1, 1)
        alternatives = [
            candidate_id
            for candidate_id in eligible
            if candidate_id not in selected
            and _domain(candidates[candidate_id]) is domain
        ][:alternative_limit]
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
                eligible_for_composition=role is not CandidateSelectionRole.ALTERNATIVE,
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
        "selection_policy": policy.model_dump(mode="json"),
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
        selection_policy=policy,
        content_hash=content_hash,
    )


def catalog_for_candidate_selection(
    catalog: RecommendationCatalog,
    selection_plan: CandidateSelectionPlan,
) -> RecommendationCatalog:
    _validate_selection_plan_catalog(catalog, selection_plan)
    selected_ids = selection_plan.composition_candidate_ids()
    return catalog.model_copy(
        update={
            "admission_results": [
                result
                for result in catalog.admission_results
                if result.candidate_id in selected_ids
            ]
        }
    )


def catalog_for_workspace_materialization(
    catalog: RecommendationCatalog,
    selection_plan: CandidateSelectionPlan,
) -> RecommendationCatalog:
    """Keep full admitted lineage when the final Workspace crosses its contract.

    Composition uses a narrowed Catalog so an alternative cannot be placed by
    accident.  The Workspace still carries the complete Selection Plan,
    including alternatives, so materializing it against that narrowed Catalog
    falsely turns every omitted alternative into a rejected candidate.  Rebind
    to the full validated Catalog only at the Workspace boundary, after the
    composition has already been constrained and checked.
    """

    _validate_selection_plan_catalog(catalog, selection_plan)
    return catalog


def _validate_selection_plan_catalog(
    catalog: RecommendationCatalog,
    selection_plan: CandidateSelectionPlan,
) -> None:
    if selection_plan.generation_id != catalog.generation_id:
        raise ValueError("candidate selection plan belongs to another generation")
    if selection_plan.intent_spec_revision != catalog.intent_spec_revision:
        raise ValueError("candidate selection plan uses another intent revision")
    if selection_plan.catalog_revision != catalog.fact_data_revision:
        raise ValueError("candidate selection plan uses another catalog revision")
    plan_candidate_ids = {entry.candidate_id for entry in selection_plan.entries}
    if not plan_candidate_ids <= set(catalog.candidate_index()):
        raise ValueError("candidate selection plan references a missing candidate")
    if not plan_candidate_ids <= _admitted_candidate_ids(catalog):
        raise ValueError("candidate selection plan references a non-admitted candidate")


def _schedule_capabilities(candidate: ResearchCandidate) -> dict[str, object]:
    if isinstance(candidate, VisitCandidate):
        return {
            "recommended_duration_minutes": candidate.recommended_duration_minutes,
            "opening_window": candidate.opening_window,
        }
    if isinstance(candidate, DiningCandidate):
        return {
            "meal_types": list(candidate.meal_types),
            "opening_window": candidate.opening_window,
        }
    if isinstance(candidate, LodgingCandidate):
        return {
            "check_in_date": candidate.check_in_date.isoformat(),
            "check_out_date": candidate.check_out_date.isoformat(),
            "nights": candidate.nights,
        }
    assert isinstance(candidate, TransportCandidate)
    return {
        "transport_class": candidate.transport_class,
        "selected_mode": candidate.selected_mode.value,
        "departure_at": candidate.departure_at.isoformat()
        if candidate.departure_at
        else None,
        "arrival_at": candidate.arrival_at.isoformat()
        if candidate.arrival_at
        else None,
        "duration_minutes": candidate.duration_minutes,
        "from_place_id": candidate.from_endpoint.place_id,
        "to_place_id": candidate.to_endpoint.place_id,
    }


def selected_candidate_capabilities(
    *,
    catalog: RecommendationCatalog,
    selection_plan: CandidateSelectionPlan,
) -> tuple[list[SelectedCandidateCapability], list[SelectedCandidateCapability]]:
    candidates = catalog.candidate_index()
    rankings = {score.candidate_id: score for score in catalog.candidate_ranking_scores}
    selected: list[SelectedCandidateCapability] = []
    alternatives: list[SelectedCandidateCapability] = []
    for entry in selection_plan.entries:
        candidate = candidates.get(entry.candidate_id)
        ranking = rankings.get(entry.candidate_id)
        if candidate is None or ranking is None:
            raise ValueError("candidate capability is missing catalog or ranking data")
        place_id = getattr(candidate, "place_id", None)
        capability = SelectedCandidateCapability(
            candidate_id=entry.candidate_id,
            candidate_kind=candidate.candidate_kind,
            destination_id=candidate.destination_id,
            selection_role=entry.role,
            rank=entry.rank,
            matched_intent_ids=ranking.matched_intent_ids,
            unknown_intent_ids=ranking.unknown_intent_ids,
            hard_violation_intent_ids=ranking.hard_violation_intent_ids,
            selection_reasons=entry.selection_reasons,
            evidence_confidence=ranking.evidence_confidence,
            budget_fit=ranking.budget_fit,
            weather_fit=ranking.weather_fit,
            constraint_fit=ranking.constraint_fit,
            place_id=place_id,
            schedule_capabilities=_schedule_capabilities(candidate),
        )
        if entry.role is CandidateSelectionRole.ALTERNATIVE:
            alternatives.append(capability)
        else:
            selected.append(capability)
    return selected, alternatives
