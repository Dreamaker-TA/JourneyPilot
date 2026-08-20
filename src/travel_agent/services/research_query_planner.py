from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from ..entities.intent_spec import (
    CategoryIntentValue,
    IntentItem,
    IntentKind,
    IntentSpec,
    IntentTarget,
    ScalarIntentValue,
)
from ..entities.research_brief import ResearchBriefV2
from ..entities.research_domain import ResearchDomain
from ..entities.research_query_plan import (
    ResearchQuery,
    ResearchQueryKind,
    ResearchQueryPlan,
)
from ..entities.intent_spec import canonical_json_hash
from .fallback_query_policy import fallback_template


RESEARCH_QUERY_POLICY_VERSION = "research_query_planner.v1"
PER_DOMAIN_QUERY_LIMITS = {
    ResearchQueryKind.INTENT_PRIMARY: 2,
    ResearchQueryKind.STRUCTURAL: 1,
    ResearchQueryKind.GENERIC_FALLBACK: 1,
}
DEFAULT_DOMAIN_CANDIDATE_CAPS = {
    ResearchDomain.VISIT.value: 8,
    ResearchDomain.DINING.value: 6,
    ResearchDomain.LODGING.value: 4,
    ResearchDomain.LOCAL_TRANSPORT.value: 8,
    ResearchDomain.LONG_DISTANCE_TRANSPORT.value: 6,
}


_TARGET_DOMAIN = {
    IntentTarget.VISIT: ResearchDomain.VISIT,
    IntentTarget.DINING: ResearchDomain.DINING,
    IntentTarget.LODGING: ResearchDomain.LODGING,
    IntentTarget.LOCAL_TRANSPORT: ResearchDomain.LOCAL_TRANSPORT,
    IntentTarget.LONG_DISTANCE_TRANSPORT: ResearchDomain.LONG_DISTANCE_TRANSPORT,
}


def _destination_rows(brief: ResearchBriefV2) -> list[tuple[str, str]]:
    rows = []
    for destination in brief.controlled_trip_identity.destinations:
        rows.append((destination.place_id, destination.name))
    return rows


def _aliases(intent: IntentItem) -> list[str]:
    if isinstance(intent.value, CategoryIntentValue):
        return list(dict.fromkeys(intent.value.categories))
    if isinstance(intent.value, ScalarIntentValue):
        return [intent.value.value]
    return [intent.public_summary]


def _query_text(
    destination_name: str,
    domain: ResearchDomain,
    intent: IntentItem,
) -> str:
    domain_label = {
        ResearchDomain.VISIT: "places",
        ResearchDomain.DINING: "restaurants cafes",
        ResearchDomain.LODGING: "hotels",
        ResearchDomain.LOCAL_TRANSPORT: "local transport",
        ResearchDomain.LONG_DISTANCE_TRANSPORT: "intercity transport",
    }[domain]
    terms = " ".join(_aliases(intent))
    return f"{destination_name} {terms} {domain_label}".strip()


def _structural_query(destination_name: str, domain: ResearchDomain) -> str:
    suffix = {
        ResearchDomain.VISIT: "specific attractions and experiences",
        ResearchDomain.DINING: "specific dining venues",
        ResearchDomain.LODGING: "lodging properties",
        ResearchDomain.LOCAL_TRANSPORT: "local route options",
        ResearchDomain.LONG_DISTANCE_TRANSPORT: "dated intercity route options",
    }[domain]
    return f"{destination_name} {suffix}"


def _provider_route(domain: ResearchDomain) -> str:
    if domain is ResearchDomain.LONG_DISTANCE_TRANSPORT:
        return "rail_provider"
    if domain is ResearchDomain.LOCAL_TRANSPORT:
        return "route_provider"
    return "mixed"


def _query_id(material: Mapping[str, object]) -> str:
    return "query_" + canonical_json_hash(material)[:24]


def build_research_query_plan(
    *,
    intent_spec: IntentSpec,
    brief: ResearchBriefV2,
) -> ResearchQueryPlan:
    queries: list[ResearchQuery] = []
    active_by_id = {item.intent_id: item for item in intent_spec.active_items}
    excluded_by_domain: dict[ResearchDomain, list[str]] = defaultdict(list)
    for item in intent_spec.active_items:
        domain = _TARGET_DOMAIN.get(item.target)
        if domain is None or item.kind is not IntentKind.MUST_EXCLUDE:
            continue
        excluded_by_domain[domain].extend(_aliases(item))

    for destination_id, destination_name in _destination_rows(brief):
        for objective in brief.domain_objectives:
            domain = objective.domain
            intent_ids = list(
                dict.fromkeys(
                    [
                        *objective.must_cover_intent_ids,
                        *objective.optional_intent_ids,
                    ]
                )
            )
            domain_exclusions = list(
                dict.fromkeys(
                    [
                        *objective.excluded_categories,
                        *excluded_by_domain.get(domain, []),
                    ]
                )
            )
            primary_ids: list[str] = []
            primary_intents = [
                active_by_id[intent_id]
                for intent_id in intent_ids
                if intent_id in active_by_id
                and active_by_id[intent_id].kind is not IntentKind.MUST_EXCLUDE
                and "research" in active_by_id[intent_id].impact_stages
            ]
            primary_intents.sort(key=lambda item: (-item.priority, item.intent_id))
            for intent in primary_intents[
                : PER_DOMAIN_QUERY_LIMITS[ResearchQueryKind.INTENT_PRIMARY]
            ]:
                material = {
                    "generation_id": brief.generation_id,
                    "destination_id": destination_id,
                    "domain": domain.value,
                    "kind": ResearchQueryKind.INTENT_PRIMARY.value,
                    "intent_ids": [intent.intent_id],
                    "query_text": _query_text(destination_name, domain, intent),
                }
                query_id = _query_id(material)
                primary_ids.append(query_id)
                queries.append(
                    ResearchQuery(
                        query_id=query_id,
                        generation_id=brief.generation_id,
                        domain=domain,
                        destination_id=destination_id,
                        query_kind=ResearchQueryKind.INTENT_PRIMARY,
                        query_text=str(material["query_text"]),
                        aliases=_aliases(intent),
                        intent_ids=[intent.intent_id],
                        excluded_categories=domain_exclusions,
                        desired_candidate_count=3,
                        provider_route=_provider_route(domain),
                        priority=intent.priority,
                    )
                )

            structural_material = {
                "generation_id": brief.generation_id,
                "destination_id": destination_id,
                "domain": domain.value,
                "kind": ResearchQueryKind.STRUCTURAL.value,
                "query_text": _structural_query(destination_name, domain),
            }
            structural_id = _query_id(structural_material)
            queries.append(
                ResearchQuery(
                    query_id=structural_id,
                    generation_id=brief.generation_id,
                    domain=domain,
                    destination_id=destination_id,
                    query_kind=ResearchQueryKind.STRUCTURAL,
                    query_text=str(structural_material["query_text"]),
                    intent_ids=intent_ids,
                    excluded_categories=domain_exclusions,
                    desired_candidate_count=2,
                    provider_route=_provider_route(domain),
                    priority=60,
                )
            )
            fallback = fallback_template(
                domain,
                destination_name,
                domain_exclusions,
            )
            dependencies = [*primary_ids, structural_id]
            if fallback and dependencies:
                fallback_material = {
                    "generation_id": brief.generation_id,
                    "destination_id": destination_id,
                    "domain": domain.value,
                    "kind": ResearchQueryKind.GENERIC_FALLBACK.value,
                    "query_text": fallback,
                }
                queries.append(
                    ResearchQuery(
                        query_id=_query_id(fallback_material),
                        generation_id=brief.generation_id,
                        domain=domain,
                        destination_id=destination_id,
                        query_kind=ResearchQueryKind.GENERIC_FALLBACK,
                        query_text=fallback,
                        excluded_categories=domain_exclusions,
                        desired_candidate_count=2,
                        provider_route=_provider_route(domain),
                        priority=10,
                        fallback_after_query_ids=dependencies,
                    )
                )

    queries.sort(
        key=lambda query: (
            query.destination_id,
            query.domain.value,
            {
                ResearchQueryKind.INTENT_PRIMARY: 0,
                ResearchQueryKind.STRUCTURAL: 1,
                ResearchQueryKind.EVIDENCE_ENRICHMENT: 2,
                ResearchQueryKind.GENERIC_FALLBACK: 3,
                ResearchQueryKind.TARGETED_REPAIR: 4,
            }[query.query_kind],
            -query.priority,
            query.query_id,
        )
    )
    material = {
        "generation_id": brief.generation_id,
        "intent_spec_revision": intent_spec.revision,
        "queries": [query.model_dump(mode="json") for query in queries],
        "per_domain_candidate_caps": DEFAULT_DOMAIN_CANDIDATE_CAPS,
        "policy_version": RESEARCH_QUERY_POLICY_VERSION,
    }
    content_hash = canonical_json_hash(material)
    return ResearchQueryPlan(
        query_plan_id=f"query_plan_{brief.generation_id}",
        generation_id=brief.generation_id,
        intent_spec_revision=intent_spec.revision,
        queries=queries,
        per_domain_candidate_caps=DEFAULT_DOMAIN_CANDIDATE_CAPS,
        policy_version=RESEARCH_QUERY_POLICY_VERSION,
        content_hash=content_hash,
    )


def queries_by_ids(
    plan: ResearchQueryPlan,
    query_ids: Sequence[str],
) -> list[ResearchQuery]:
    index = plan.query_index()
    return [index[query_id] for query_id in query_ids if query_id in index]


def query_ids_for_domains(
    plan: ResearchQueryPlan,
    domains: Iterable[ResearchDomain],
) -> list[str]:
    allowed = set(domains)
    return [query.query_id for query in plan.queries if query.domain in allowed]


def append_targeted_repair_query(
    plan: ResearchQueryPlan,
    *,
    intent: IntentItem,
    destination_id: str,
    destination_name: str,
    domain: ResearchDomain,
    desired_candidate_count: int,
) -> tuple[ResearchQueryPlan, ResearchQuery]:
    related = [
        query
        for query in plan.queries
        if query.destination_id == destination_id and query.domain is domain
    ]
    material = {
        "generation_id": plan.generation_id,
        "destination_id": destination_id,
        "domain": domain.value,
        "kind": ResearchQueryKind.TARGETED_REPAIR.value,
        "intent_ids": [intent.intent_id],
        "desired_candidate_count": desired_candidate_count,
        "query_text": _query_text(destination_name, domain, intent),
    }
    query = ResearchQuery(
        query_id=_query_id(material),
        generation_id=plan.generation_id,
        domain=domain,
        destination_id=destination_id,
        query_kind=ResearchQueryKind.TARGETED_REPAIR,
        query_text=str(material["query_text"]),
        aliases=_aliases(intent),
        intent_ids=[intent.intent_id],
        desired_candidate_count=desired_candidate_count,
        provider_route=_provider_route(domain),
        priority=max(intent.priority, 80),
        fallback_after_query_ids=[item.query_id for item in related],
    )
    existing = plan.query_index().get(query.query_id)
    if existing is not None:
        return plan, existing
    queries = [*plan.queries, query]
    material = {
        "generation_id": plan.generation_id,
        "intent_spec_revision": plan.intent_spec_revision,
        "queries": [item.model_dump(mode="json") for item in queries],
        "per_domain_candidate_caps": plan.per_domain_candidate_caps,
        "policy_version": plan.policy_version,
    }
    return (
        plan.model_copy(
            update={
                "queries": queries,
                "content_hash": canonical_json_hash(material),
            }
        ),
        query,
    )


def append_structural_connector_query(
    plan: ResearchQueryPlan,
    *,
    destination_id: str,
    destination_name: str,
    route_pairs: Sequence[tuple[str, str]],
) -> tuple[ResearchQueryPlan, ResearchQuery]:
    """Append the server-owned query that authorizes an exact connector sweep.

    Local connector responsibilities do not exist until the itinerary skeleton has
    placed adjacent stops, so the initial Research Query Plan cannot name them.  A
    Candidate Gate repair round must extend that plan before the route Provider is
    called; otherwise real route results have no executed-query lineage and the
    Research Packet correctly rejects them.

    The ordered endpoint pairs are part of the query identity.  Replaying the same
    skeleton therefore reuses the same query, while a changed adjacency receives a
    new lineage record instead of borrowing the old one.
    """

    ordered_pairs = sorted(set(route_pairs))
    if not ordered_pairs:
        raise ValueError("structural connector query requires an endpoint pair")
    pair_text = "; ".join(f"{origin} -> {destination}" for origin, destination in ordered_pairs)
    query_text = f"{destination_name} exact local routes: {pair_text}"[:500]
    material = {
        "generation_id": plan.generation_id,
        "destination_id": destination_id,
        "domain": ResearchDomain.LOCAL_TRANSPORT.value,
        "kind": ResearchQueryKind.STRUCTURAL.value,
        "route_pairs": ordered_pairs,
        "query_text": query_text,
    }
    query = ResearchQuery(
        query_id=_query_id(material),
        generation_id=plan.generation_id,
        domain=ResearchDomain.LOCAL_TRANSPORT,
        destination_id=destination_id,
        query_kind=ResearchQueryKind.STRUCTURAL,
        query_text=query_text,
        desired_candidate_count=min(len(ordered_pairs), 20),
        provider_route="route_provider",
        priority=90,
    )
    existing = plan.query_index().get(query.query_id)
    if existing is not None:
        return plan, existing
    queries = [*plan.queries, query]
    plan_material = {
        "generation_id": plan.generation_id,
        "intent_spec_revision": plan.intent_spec_revision,
        "queries": [item.model_dump(mode="json") for item in queries],
        "per_domain_candidate_caps": plan.per_domain_candidate_caps,
        "policy_version": plan.policy_version,
    }
    return (
        plan.model_copy(
            update={
                "queries": queries,
                "content_hash": canonical_json_hash(plan_material),
            }
        ),
        query,
    )
