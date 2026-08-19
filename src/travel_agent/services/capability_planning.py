"""Deterministic projection from a request contract to research and execution."""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping

from ..entities.capability_plan import AgentAssignmentContract, AgentName, CapabilityPlan
from ..entities.delivery_bundle import ResearchDomain
from ..entities.intent_spec import (
    CategoryIntentValue,
    IntentItem,
    IntentKind,
    IntentStrength,
    IntentTarget,
    canonical_json_hash,
)
from ..entities.request_contract import RequestContract
from ..entities.research_brief import (
    DomainResearchObjective,
    ResearchBriefV2,
    SuccessCriterion,
)
from ..entities.trip_input import ControlledTripIdentity
from .product_requirements import required_physical_candidate_kinds


RESEARCH_BRIEF_POLICY_VERSION = "research_brief_projection.v2"
CAPABILITY_PLAN_POLICY_VERSION = "capability_plan.v1"


_TARGET_DOMAIN = {
    IntentTarget.VISIT: ResearchDomain.VISIT,
    IntentTarget.DINING: ResearchDomain.DINING,
    IntentTarget.LODGING: ResearchDomain.LODGING,
    IntentTarget.LOCAL_TRANSPORT: ResearchDomain.LOCAL_TRANSPORT,
    IntentTarget.LONG_DISTANCE_TRANSPORT: ResearchDomain.LONG_DISTANCE_TRANSPORT,
}

_DOMAIN_OWNER: Dict[ResearchDomain, AgentName] = {
    ResearchDomain.VISIT: "destination_researcher",
    ResearchDomain.DINING: "destination_researcher",
    ResearchDomain.LODGING: "accommodation_researcher",
    ResearchDomain.LOCAL_TRANSPORT: "transport_researcher",
    ResearchDomain.LONG_DISTANCE_TRANSPORT: "transport_researcher",
}

_TARGET_OWNER: Dict[IntentTarget, AgentName] = {
    IntentTarget.TRIP: "itinerary_planner",
    IntentTarget.VISIT: "destination_researcher",
    IntentTarget.DINING: "destination_researcher",
    IntentTarget.LODGING: "accommodation_researcher",
    IntentTarget.LOCAL_TRANSPORT: "transport_researcher",
    IntentTarget.LONG_DISTANCE_TRANSPORT: "transport_researcher",
    IntentTarget.ITINERARY: "itinerary_planner",
    IntentTarget.DELIVERY: "itinerary_planner",
}

_TOOLS: Dict[AgentName, List[str]] = {
    "destination_researcher": ["nominatim_place_search", "search_web"],
    "transport_researcher": ["global_route_search", "rail_12306_search"],
    "accommodation_researcher": ["nominatim_place_search", "search_web"],
    "itinerary_planner": [],
}


def build_research_brief(
    request_contract: RequestContract,
    controlled_identity: Mapping[str, object],
) -> ResearchBriefV2:
    identity = ControlledTripIdentity.model_validate(controlled_identity)
    active = request_contract.intent_spec.active_items
    hard_ids = [item.intent_id for item in active if item.strength is IntentStrength.HARD]
    soft_ids = [item.intent_id for item in active if item.strength is IntentStrength.SOFT]

    domains = {
        _TARGET_DOMAIN[item.target]
        for item in active
        if item.target in _TARGET_DOMAIN
        and any(stage in item.impact_stages for stage in ("research", "admission", "ranking"))
    }
    required_kinds = required_physical_candidate_kinds(controlled_identity)
    if "visit" in required_kinds:
        domains.add(ResearchDomain.VISIT)
    if "dining" in required_kinds:
        domains.add(ResearchDomain.DINING)
    if identity.duration_days > 1:
        domains.add(ResearchDomain.LODGING)
    domains.add(ResearchDomain.LONG_DISTANCE_TRANSPORT)

    objectives: List[DomainResearchObjective] = []
    for domain in sorted(domains, key=lambda item: item.value):
        scoped = [
            item
            for item in active
            if _TARGET_DOMAIN.get(item.target) == domain
            or (domain is ResearchDomain.VISIT and item.target is IntentTarget.TRIP)
        ]
        must = [item.intent_id for item in scoped if item.strength is IntentStrength.HARD]
        optional = [item.intent_id for item in scoped if item.strength is not IntentStrength.HARD]
        excluded = [
            value
            for item in scoped
            if item.kind is IntentKind.MUST_EXCLUDE
            for value in _category_values(item)
        ]
        criteria = [_criterion(item) for item in scoped]
        objective_material = {
            "generation_id": request_contract.generation_id,
            "domain": domain.value,
            "must": must,
            "optional": optional,
            "excluded": excluded,
        }
        objectives.append(
            DomainResearchObjective(
                objective_id="objective_" + canonical_json_hash(objective_material)[:24],
                domain=domain,
                summary=_domain_summary(domain, scoped),
                must_cover_intent_ids=must,
                optional_intent_ids=optional,
                excluded_categories=excluded,
                success_criteria=criteria,
            )
        )
    delivery_requirements = [
        item.public_summary
        for item in active
        if item.target is IntentTarget.DELIVERY or "projection" in item.impact_stages
    ]
    material = {
        "generation_id": request_contract.generation_id,
        "identity_revision": request_contract.controlled_trip_identity_revision,
        "intent_revision": request_contract.intent_spec.revision,
        "constraint_revision": request_contract.constraint_pack_revision,
        "objective_summary": request_contract.intent_spec.objective_summary,
        "identity": identity.model_dump(mode="json"),
        "objectives": [item.model_dump(mode="json") for item in objectives],
        "delivery_requirements": delivery_requirements,
        "hard_intent_ids": hard_ids,
        "soft_intent_ids": soft_ids,
    }
    content_hash = canonical_json_hash(material)
    return ResearchBriefV2(
        brief_id=f"brief_{content_hash[:24]}",
        generation_id=request_contract.generation_id,
        controlled_trip_identity_revision=request_contract.controlled_trip_identity_revision,
        intent_spec_revision=request_contract.intent_spec.revision,
        constraint_pack_revision=request_contract.constraint_pack_revision,
        objective_summary=request_contract.intent_spec.objective_summary,
        controlled_trip_identity=identity,
        domain_objectives=objectives,
        delivery_requirements=delivery_requirements,
        hard_intent_ids=hard_ids,
        soft_intent_ids=soft_ids,
        content_hash=content_hash,
    )


def build_capability_plan(
    *,
    request_contract: RequestContract,
    brief: ResearchBriefV2,
    plan_revision: int,
) -> CapabilityPlan:
    active = request_contract.intent_spec.active_items
    agents: set[AgentName] = {
        _DOMAIN_OWNER[objective.domain] for objective in brief.domain_objectives
    }
    agents.add("itinerary_planner")
    assignments: Dict[str, AgentAssignmentContract] = {}

    order: List[List[AgentName]] = []
    if "destination_researcher" in agents:
        order.append(["destination_researcher"])
    middle = [
        agent
        for agent in ("transport_researcher", "accommodation_researcher")
        if agent in agents
    ]
    if middle:
        order.append(middle)
    order.append(["itinerary_planner"])

    objective_by_agent: Dict[AgentName, List[DomainResearchObjective]] = {
        agent: [] for agent in agents
    }
    for objective in brief.domain_objectives:
        objective_by_agent[_DOMAIN_OWNER[objective.domain]].append(objective)

    assignment_ids = {
        agent: "assignment_"
        + canonical_json_hash(
            {"generation_id": brief.generation_id, "agent": agent}
        )[:24]
        for agent in agents
    }
    for agent in sorted(agents):
        owned_intents = [item for item in active if _TARGET_OWNER[item.target] == agent]
        objectives = objective_by_agent.get(agent, [])
        upstream = []
        if agent in {"transport_researcher", "accommodation_researcher"} and "destination_researcher" in agents:
            upstream.append(assignment_ids["destination_researcher"])
        if agent == "itinerary_planner":
            upstream.extend(
                assignment_ids[owner]
                for owner in (
                    "destination_researcher",
                    "transport_researcher",
                    "accommodation_researcher",
                )
                if owner in agents
            )
        assignments[agent] = AgentAssignmentContract(
            assignment_id=assignment_ids[agent],
            generation_id=brief.generation_id,
            agent_name=agent,
            objective=_assignment_objective(agent, objectives, owned_intents),
            must_cover_intent_ids=[
                item.intent_id
                for item in owned_intents
                if item.strength is IntentStrength.HARD
            ],
            optional_intent_ids=[
                item.intent_id
                for item in owned_intents
                if item.strength is not IntentStrength.HARD
            ],
            research_objective_ids=[item.objective_id for item in objectives],
            required_candidate_kinds=_candidate_kinds(objectives),
            excluded_categories=list(
                dict.fromkeys(
                    value
                    for objective in objectives
                    for value in objective.excluded_categories
                )
            ),
            success_criteria=[
                criterion
                for objective in objectives
                for criterion in objective.success_criteria
            ]
            + ([_criterion(item) for item in owned_intents] if agent == "itinerary_planner" else []),
            recommended_tools=_TOOLS[agent],
            upstream_assignment_ids=upstream,
            intent_spec_revision=request_contract.intent_spec.revision,
            constraint_pack_revision=request_contract.constraint_pack_revision,
        )

    _validate_hard_intent_ownership(active, assignments)
    material = {
        "generation_id": brief.generation_id,
        "plan_revision": plan_revision,
        "intent_spec_revision": request_contract.intent_spec.revision,
        "constraint_pack_revision": request_contract.constraint_pack_revision,
        "execution_plan": order,
        "assignments": {
            key: value.model_dump(mode="json") for key, value in assignments.items()
        },
    }
    content_hash = canonical_json_hash(material)
    return CapabilityPlan(
        capability_plan_id=f"capability_plan_{content_hash[:24]}",
        generation_id=brief.generation_id,
        plan_revision=plan_revision,
        intent_spec_revision=request_contract.intent_spec.revision,
        constraint_pack_revision=request_contract.constraint_pack_revision,
        execution_plan=order,
        assignments=assignments,
        content_hash=content_hash,
    )


def _category_values(item: IntentItem) -> List[str]:
    return list(item.value.categories) if isinstance(item.value, CategoryIntentValue) else [item.public_summary]


def _criterion(item: IntentItem) -> SuccessCriterion:
    material = {"intent_id": item.intent_id, "summary": item.public_summary}
    return SuccessCriterion(
        criterion_id="criterion_" + canonical_json_hash(material)[:24],
        description=item.public_summary,
        verification=item.verification_mode.value,
        intent_ids=[item.intent_id],
    )


def _domain_summary(domain: ResearchDomain, scoped: List[IntentItem]) -> str:
    labels = [item.public_summary for item in scoped]
    base = {
        ResearchDomain.VISIT: "查明符合主题和限制的具体游览候选",
        ResearchDomain.DINING: "查明符合用餐要求的具体餐饮候选",
        ResearchDomain.LODGING: "查明覆盖过夜责任的住宿候选",
        ResearchDomain.LOCAL_TRANSPORT: "查明行程内连接段的可执行交通",
        ResearchDomain.LONG_DISTANCE_TRANSPORT: "查明出发、返程和跨城交通责任",
    }[domain]
    return (base + ("；" + "；".join(labels) if labels else ""))[:500]


def _assignment_objective(
    agent: AgentName,
    objectives: List[DomainResearchObjective],
    intents: List[IntentItem],
) -> str:
    if agent == "itinerary_planner":
        labels = [item.public_summary for item in intents]
        return ("只使用已准入候选组合可执行按天行程" + ("；" + "；".join(labels) if labels else ""))[:1000]
    return "；".join(item.summary for item in objectives)[:1000]


def _candidate_kinds(objectives: Iterable[DomainResearchObjective]) -> List[str]:
    mapping = {
        ResearchDomain.VISIT: "visit",
        ResearchDomain.DINING: "dining",
        ResearchDomain.LODGING: "lodging",
        ResearchDomain.LOCAL_TRANSPORT: "transport",
        ResearchDomain.LONG_DISTANCE_TRANSPORT: "transport",
    }
    return list(dict.fromkeys(mapping[item.domain] for item in objectives))


def _validate_hard_intent_ownership(
    intents: List[IntentItem],
    assignments: Dict[str, AgentAssignmentContract],
) -> None:
    owned = {
        intent_id
        for assignment in assignments.values()
        for intent_id in assignment.must_cover_intent_ids
    }
    missing = {
        item.intent_id
        for item in intents
        if item.strength is IntentStrength.HARD and item.intent_id not in owned
    }
    if missing:
        raise ValueError(f"active hard intents lack capability ownership: {sorted(missing)}")
