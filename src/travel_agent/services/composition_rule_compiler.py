from __future__ import annotations

from ..entities.composition_rules import (
    COMPOSITION_RULE_POLICY_VERSION,
    CadenceRuleParameters,
    CompositionRule,
    CompositionRuleKind,
    CountRuleParameters,
    DestinationScopeRuleParameters,
    OutputExplanationRuleParameters,
    PlacementRuleParameters,
    SequenceRuleParameters,
    TimeWindowRuleParameters,
)
from ..entities.intent_spec import (
    CadenceIntentValue,
    CategoryIntentValue,
    CountIntentValue,
    GeographicIntentValue,
    IntentItem,
    IntentKind,
    IntentSpec,
    IntentStrength,
    IntentTarget,
    OutputRequirementValue,
    ScalarIntentValue,
    SequenceIntentValue,
    TimeWindowIntentValue,
    canonical_json_hash,
)
from ..entities.intent_coverage import IntentContractSnapshot
from ..entities.research_domain import ResearchDomain


_DOMAIN_BY_TARGET = {
    IntentTarget.VISIT: ResearchDomain.VISIT,
    IntentTarget.DINING: ResearchDomain.DINING,
    IntentTarget.LODGING: ResearchDomain.LODGING,
    IntentTarget.LOCAL_TRANSPORT: ResearchDomain.LOCAL_TRANSPORT,
    IntentTarget.LONG_DISTANCE_TRANSPORT: ResearchDomain.LONG_DISTANCE_TRANSPORT,
}


def _failure_policy(intent: IntentItem, rule_kind: CompositionRuleKind) -> str:
    if intent.strength is not IntentStrength.HARD:
        return "nonblocking_preference"
    if rule_kind in {
        CompositionRuleKind.MUST_NOT_PLACE,
        CompositionRuleKind.MAX_PER_DAY,
    }:
        return "never_violate"
    return "repair_then_deviate"


def _placement_values(intent: IntentItem) -> list[str]:
    if isinstance(intent.value, ScalarIntentValue):
        return [intent.value.value]
    if isinstance(intent.value, CategoryIntentValue):
        return list(intent.value.categories)
    return [intent.public_summary]


def _rule(
    *,
    intent: IntentItem,
    generation_id: str,
    rule_kind: CompositionRuleKind,
    parameters,
    suffix: str = "",
) -> CompositionRule:
    material = {
        "policy_version": COMPOSITION_RULE_POLICY_VERSION,
        "intent_id": intent.intent_id,
        "generation_id": generation_id,
        "rule_kind": rule_kind.value,
        "parameters": parameters.model_dump(mode="json"),
        "suffix": suffix,
    }
    return CompositionRule(
        rule_id=f"composition_rule_{canonical_json_hash(material)[:24]}",
        intent_id=intent.intent_id,
        generation_id=generation_id,
        rule_kind=rule_kind,
        target_domain=_DOMAIN_BY_TARGET.get(intent.target),
        hard=intent.strength is IntentStrength.HARD,
        policy_on_failure=_failure_policy(intent, rule_kind),
        parameters=parameters,
    )


def _compile_intent(intent: IntentItem, generation_id: str) -> list[CompositionRule]:
    if (
        "composition" not in intent.impact_stages
        and "projection" not in intent.impact_stages
    ):
        return []
    if intent.kind is IntentKind.MUST_EXCLUDE:
        return [
            _rule(
                intent=intent,
                generation_id=generation_id,
                rule_kind=CompositionRuleKind.MUST_NOT_PLACE,
                parameters=PlacementRuleParameters(values=_placement_values(intent)),
            )
        ]
    if intent.kind in {
        IntentKind.MUST_INCLUDE,
        IntentKind.THEME,
        IntentKind.ATTRIBUTE_PREFERENCE,
    }:
        return [
            _rule(
                intent=intent,
                generation_id=generation_id,
                rule_kind=CompositionRuleKind.MUST_PLACE,
                parameters=PlacementRuleParameters(values=_placement_values(intent)),
            )
        ]
    if intent.kind is IntentKind.QUANTITY and isinstance(
        intent.value, CountIntentValue
    ):
        parameters = CountRuleParameters(
            count=intent.value.count, unit=intent.value.unit
        )
        if intent.value.operator == "at_most":
            kinds = [CompositionRuleKind.MAX_PER_DAY]
        elif intent.value.operator == "at_least":
            kinds = [CompositionRuleKind.MIN_PER_DAY]
        else:
            kinds = [CompositionRuleKind.MIN_PER_DAY, CompositionRuleKind.MAX_PER_DAY]
        return [
            _rule(
                intent=intent,
                generation_id=generation_id,
                rule_kind=kind,
                parameters=parameters,
                suffix=kind.value,
            )
            for kind in kinds
        ]
    if intent.kind is IntentKind.CADENCE and isinstance(
        intent.value, CadenceIntentValue
    ):
        return [
            _rule(
                intent=intent,
                generation_id=generation_id,
                rule_kind=CompositionRuleKind.CADENCE,
                parameters=CadenceRuleParameters(
                    frequency=intent.value.frequency,
                    count=intent.value.count,
                    time_window=intent.value.time_window,
                    required_attributes=intent.value.required_attributes,
                ),
            )
        ]
    if intent.kind is IntentKind.TIME_WINDOW and isinstance(
        intent.value, TimeWindowIntentValue
    ):
        return [
            _rule(
                intent=intent,
                generation_id=generation_id,
                rule_kind=CompositionRuleKind.TIME_WINDOW,
                parameters=TimeWindowRuleParameters(
                    window=intent.value.window,
                    applies_to=intent.value.applies_to,
                ),
            )
        ]
    if intent.kind is IntentKind.SEQUENCING and isinstance(
        intent.value, SequenceIntentValue
    ):
        return [
            _rule(
                intent=intent,
                generation_id=generation_id,
                rule_kind=CompositionRuleKind.SEQUENCE,
                parameters=SequenceRuleParameters(
                    ordered_items=intent.value.ordered_items
                ),
            )
        ]
    if intent.kind is IntentKind.GEOGRAPHIC and isinstance(
        intent.value, GeographicIntentValue
    ):
        return [
            _rule(
                intent=intent,
                generation_id=generation_id,
                rule_kind=CompositionRuleKind.DESTINATION_SCOPE,
                parameters=DestinationScopeRuleParameters(
                    area=intent.value.area,
                    relation=intent.value.relation,
                ),
            )
        ]
    if intent.kind is IntentKind.OUTPUT_REQUIREMENT and isinstance(
        intent.value, OutputRequirementValue
    ):
        return [
            _rule(
                intent=intent,
                generation_id=generation_id,
                rule_kind=CompositionRuleKind.OUTPUT_EXPLANATION,
                parameters=OutputExplanationRuleParameters(
                    required_field=intent.value.required_field,
                    applies_to=intent.value.applies_to,
                ),
            )
        ]
    return []


def compile_composition_rules(
    intent_spec: IntentSpec | IntentContractSnapshot,
) -> list[CompositionRule]:
    rules = [
        rule
        for intent in intent_spec.active_items
        for rule in _compile_intent(intent, intent_spec.generation_id)
    ]
    return sorted(
        rules, key=lambda rule: (rule.intent_id, rule.rule_kind.value, rule.rule_id)
    )
