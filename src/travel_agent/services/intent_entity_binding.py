from __future__ import annotations

from collections import defaultdict

from ..entities.candidate_intent import IntentMatchStatus
from ..entities.delivery_bundle import (
    DiningStop,
    EntityRef,
    EntityType,
    LodgingStay,
    PersonalizationInfluence,
    TransportLeg,
    TripWorkspaceV2,
    VisitStop,
)
from ..entities.intent_coverage import (
    EntityIntentExplanation,
    IntentContractRequirement,
    IntentContractSnapshot,
)
from ..entities.intent_spec import (
    IntentItem,
    IntentKind,
    IntentSpec,
    IntentTarget,
    OutputRequirementValue,
    canonical_json_hash,
)


def _entity_ref(entity) -> EntityRef:
    if isinstance(entity, VisitStop):
        return EntityRef(entity_type=EntityType.VISIT_STOP, entity_id=entity.item_id)
    if isinstance(entity, DiningStop):
        return EntityRef(entity_type=EntityType.DINING_STOP, entity_id=entity.item_id)
    if isinstance(entity, LodgingStay):
        return EntityRef(entity_type=EntityType.LODGING_STAY, entity_id=entity.stay_id)
    return EntityRef(
        entity_type=EntityType.TRANSPORT_LEG,
        entity_id=entity.transport_leg_id,
    )


def _target_matches(intent: IntentItem | IntentContractRequirement, entity) -> bool:
    if (
        intent.kind is IntentKind.OUTPUT_REQUIREMENT
        and intent.target is IntentTarget.DELIVERY
    ):
        return True
    target = {
        VisitStop: IntentTarget.VISIT,
        DiningStop: IntentTarget.DINING,
        LodgingStay: IntentTarget.LODGING,
        TransportLeg: IntentTarget.LOCAL_TRANSPORT,
    }[type(entity)]
    if isinstance(entity, TransportLeg) and entity.transport_class == "long_distance":
        target = IntentTarget.LONG_DISTANCE_TRANSPORT
    return intent.target in {target, IntentTarget.TRIP, IntentTarget.ITINERARY}


def _effect(intent: IntentItem | IntentContractRequirement) -> str:
    if intent.kind is IntentKind.OUTPUT_REQUIREMENT:
        return "output_requirement"
    if intent.kind in {
        IntentKind.QUANTITY,
        IntentKind.CADENCE,
        IntentKind.TIME_WINDOW,
        IntentKind.SEQUENCING,
        IntentKind.PACE,
    }:
        return "schedule_rule"
    if intent.kind is IntentKind.MUST_EXCLUDE:
        return "candidate_filter"
    if intent.kind in {IntentKind.THEME, IntentKind.ATTRIBUTE_PREFERENCE}:
        return "option_ranking"
    return "selection_reason"


def _source_kind(intent: IntentItem | IntentContractRequirement) -> str:
    source_kind = getattr(intent, "source_kind", "current_request")
    if source_kind == "saved_preference":
        return "saved_preference"
    if source_kind == "trip_context":
        return "trip_context"
    return "current_request"


def _default_explanation(entity, intent: IntentItem | IntentContractRequirement) -> str:
    selection_reason = getattr(entity, "selection_reason", None)
    if selection_reason:
        return f"{intent.public_summary}：{selection_reason}"
    if isinstance(entity, TransportLeg):
        return f"{intent.public_summary}：已按行程结构选择{entity.selected_mode.value}"
    return f"已按要求落实：{intent.public_summary}"


def bind_intent_context(
    workspace: TripWorkspaceV2,
    intent_spec: IntentSpec | IntentContractSnapshot,
) -> TripWorkspaceV2:
    intent_by_id = {intent.intent_id: intent for intent in intent_spec.active_items}
    matches_by_candidate: dict[str, list] = defaultdict(list)
    for match in workspace.recommendation_catalog.candidate_intent_matches:
        if (
            match.status is IntentMatchStatus.MATCHED
            and match.intent_id in intent_by_id
        ):
            matches_by_candidate[match.candidate_id].append(match)

    influences: dict[str, PersonalizationInfluence] = {}

    def bind_entity(entity):
        entity_ref = _entity_ref(entity)
        explanations: dict[str, EntityIntentExplanation] = {}
        lineage_ids: set[str] = set()
        candidate_id = entity.lineage.candidate_id
        for match in matches_by_candidate.get(candidate_id, []):
            intent = intent_by_id[match.intent_id]
            if not _target_matches(intent, entity):
                continue
            explanation = match.public_reason or _default_explanation(entity, intent)
            explanations[intent.intent_id] = EntityIntentExplanation(
                intent_id=intent.intent_id,
                label=intent.public_summary[:160],
                explanation=explanation,
                evidence_basis=(
                    "verified_fact"
                    if match.supporting_fact_assertion_ids
                    else "supported_description"
                ),
            )
            influence_id = f"influence_{canonical_json_hash({'generation_id': workspace.generation_id, 'entity_id': entity_ref.entity_id, 'intent_id': intent.intent_id})[:24]}"
            influences[influence_id] = PersonalizationInfluence(
                influence_id=influence_id,
                target_ref=entity_ref,
                intent_id=intent.intent_id,
                effect=_effect(intent),
                source_kind=_source_kind(intent),
                display_text=intent.public_summary,
            )
            lineage_ids.add(influence_id)

        for intent in intent_spec.active_items:
            if (
                intent.kind is not IntentKind.OUTPUT_REQUIREMENT
                or not isinstance(intent.value, OutputRequirementValue)
                or intent.value.applies_to not in {"each_item", "each_day"}
                or not _target_matches(intent, entity)
            ):
                continue
            explanations[intent.intent_id] = EntityIntentExplanation(
                intent_id=intent.intent_id,
                label=intent.public_summary[:160],
                explanation=_default_explanation(entity, intent),
                evidence_basis="planning_judgment",
            )
            influence_id = f"influence_{canonical_json_hash({'generation_id': workspace.generation_id, 'entity_id': entity_ref.entity_id, 'intent_id': intent.intent_id})[:24]}"
            influences[influence_id] = PersonalizationInfluence(
                influence_id=influence_id,
                target_ref=entity_ref,
                intent_id=intent.intent_id,
                effect="output_requirement",
                source_kind=_source_kind(intent),
                display_text=intent.public_summary,
            )
            lineage_ids.add(influence_id)

        return entity.model_copy(
            update={
                "intent_explanations": sorted(
                    explanations.values(), key=lambda item: item.intent_id
                ),
                "lineage": entity.lineage.model_copy(
                    update={
                        "personalization_influence_ids": sorted(lineage_ids),
                    }
                ),
            }
        )

    itinerary = workspace.itinerary.model_copy(
        update={
            "visit_stops": [
                bind_entity(item) for item in workspace.itinerary.visit_stops
            ],
            "dining_stops": [
                bind_entity(item) for item in workspace.itinerary.dining_stops
            ],
            "lodging_stays": [
                bind_entity(item) for item in workspace.itinerary.lodging_stays
            ],
            "transport_legs": [
                bind_entity(item) for item in workspace.itinerary.transport_legs
            ],
        }
    )
    return workspace.model_copy(
        update={
            "itinerary": itinerary,
            "personalization_influences": sorted(
                influences.values(), key=lambda item: item.influence_id
            ),
        }
    )
