from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from ..entities.composition_mutation import (
    CompositionMutation,
    CompositionMutationType,
)
from ..entities.intent_spec import canonical_json_hash


MutationCreator = Literal[
    "deterministic_pruner",
    "anchor_backfill",
    "slot_backfill",
    "composition_repair",
    "user_edit",
]


def _entity_key(placement: Mapping[str, Any], index: int) -> str:
    candidate_id = str(placement.get("candidate_id") or "").strip()
    if candidate_id:
        return candidate_id
    authored = placement.get("authored_place") or placement.get("authored_route")
    if isinstance(authored, Mapping):
        digest = canonical_json_hash(dict(authored))[:20]
        return f"authored:{placement.get('placement_kind')}:{digest}"
    return f"unbound:{placement.get('placement_kind')}:{index}"


def _placement_index(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    days = payload.get("days")
    if not isinstance(days, Sequence) or isinstance(days, (str, bytes)):
        return indexed
    for day in days:
        if not isinstance(day, Mapping):
            continue
        day_id = str(day.get("day_id") or "")
        placements = day.get("placements")
        if not isinstance(placements, Sequence) or isinstance(placements, (str, bytes)):
            continue
        for index, placement in enumerate(placements):
            if not isinstance(placement, Mapping):
                continue
            key = _entity_key(placement, index)
            indexed.setdefault(
                key,
                {
                    "day_id": day_id,
                    "index": index,
                    "planned_start": placement.get("planned_start"),
                    "planned_end": placement.get("planned_end"),
                },
            )
    return indexed


def _mutation(
    *,
    generation_id: str,
    mutation_type: CompositionMutationType,
    reason_code: str,
    source_entity_ids: list[str],
    target_entity_ids: list[str],
    created_by: MutationCreator,
    affected_intent_ids: list[str],
    affected_rule_ids: list[str],
) -> CompositionMutation:
    material = {
        "generation_id": generation_id,
        "mutation_type": mutation_type.value,
        "reason_code": reason_code,
        "source_entity_ids": source_entity_ids,
        "target_entity_ids": target_entity_ids,
        "created_by": created_by,
    }
    return CompositionMutation(
        mutation_id=f"composition_mutation_{canonical_json_hash(material)[:24]}",
        generation_id=generation_id,
        mutation_type=mutation_type,
        reason_code=reason_code,
        source_entity_ids=source_entity_ids,
        target_entity_ids=target_entity_ids,
        affected_intent_ids=sorted(set(affected_intent_ids)),
        affected_rule_ids=sorted(set(affected_rule_ids)),
        hard_rules_revalidated=False,
        created_by=created_by,
    )


def diff_composition_mutations(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    generation_id: str,
    reason_code: str,
    created_by: MutationCreator,
    intent_ids_by_entity: Mapping[str, Sequence[str]] | None = None,
    rule_ids: Sequence[str] = (),
) -> list[CompositionMutation]:
    old = _placement_index(before)
    new = _placement_index(after)
    intent_ids_by_entity = intent_ids_by_entity or {}
    mutations: list[CompositionMutation] = []
    for entity_id in sorted(old.keys() - new.keys()):
        mutations.append(
            _mutation(
                generation_id=generation_id,
                mutation_type=CompositionMutationType.DROP,
                reason_code=reason_code,
                source_entity_ids=[entity_id],
                target_entity_ids=[],
                created_by=created_by,
                affected_intent_ids=list(intent_ids_by_entity.get(entity_id, ())),
                affected_rule_ids=list(rule_ids),
            )
        )
    for entity_id in sorted(new.keys() - old.keys()):
        mutations.append(
            _mutation(
                generation_id=generation_id,
                mutation_type=CompositionMutationType.BACKFILL,
                reason_code=reason_code,
                source_entity_ids=[],
                target_entity_ids=[entity_id],
                created_by=created_by,
                affected_intent_ids=list(intent_ids_by_entity.get(entity_id, ())),
                affected_rule_ids=list(rule_ids),
            )
        )
    for entity_id in sorted(old.keys() & new.keys()):
        before_location = old[entity_id]
        after_location = new[entity_id]
        if before_location["day_id"] != after_location["day_id"]:
            mutation_type = CompositionMutationType.MOVE
        elif before_location["index"] != after_location["index"]:
            mutation_type = CompositionMutationType.REORDER
        elif (
            before_location["planned_start"],
            before_location["planned_end"],
        ) != (
            after_location["planned_start"],
            after_location["planned_end"],
        ):
            mutation_type = CompositionMutationType.TIME_ADJUST
        else:
            continue
        mutations.append(
            _mutation(
                generation_id=generation_id,
                mutation_type=mutation_type,
                reason_code=reason_code,
                source_entity_ids=[entity_id],
                target_entity_ids=[entity_id],
                created_by=created_by,
                affected_intent_ids=list(intent_ids_by_entity.get(entity_id, ())),
                affected_rule_ids=list(rule_ids),
            )
        )
    return mutations


def mark_mutations_revalidated(
    mutations: Sequence[CompositionMutation],
    *,
    coverage_before: Mapping[str, str] | None = None,
    coverage_after: Mapping[str, str] | None = None,
) -> list[CompositionMutation]:
    before = coverage_before or {}
    after = coverage_after or {}
    validated: list[CompositionMutation] = []
    for mutation in mutations:
        intent_ids = set(mutation.affected_intent_ids)
        update: dict[str, Any] = {"hard_rules_revalidated": True}
        if coverage_before is not None:
            update["coverage_before"] = {
                intent_id: before[intent_id]
                for intent_id in sorted(intent_ids & before.keys())
            }
        if coverage_after is not None:
            update["coverage_after"] = {
                intent_id: after[intent_id]
                for intent_id in sorted(intent_ids & after.keys())
            }
        validated.append(
            mutation.model_copy(update=update)
        )
    return validated


def user_workspace_mutation(
    *,
    generation_id: str,
    workspace_revision: int,
    operation: Mapping[str, Any],
    affected_intent_ids: Sequence[str] = (),
    affected_rule_ids: Sequence[str] = (),
) -> CompositionMutation:
    operation_type = str(operation.get("type") or "workspace_edit")
    mutation_type = {
        "select_option": CompositionMutationType.REPLACE,
        "move_timeline_item": CompositionMutationType.MOVE,
        "update_stop_schedule": CompositionMutationType.TIME_ADJUST,
        "create_custom_block": CompositionMutationType.BACKFILL,
        "update_custom_block": CompositionMutationType.REPLACE,
        "delete_custom_block": CompositionMutationType.DROP,
        "delete_transport_leg": CompositionMutationType.DROP,
        "set_transport_mode": CompositionMutationType.REPLACE,
        "update_transport_mode_preference": CompositionMutationType.REPLACE,
        "delete_lodging_stay": CompositionMutationType.DROP,
        "apply_weather_adjustment": CompositionMutationType.REPLACE,
        "dismiss_weather_adjustment": CompositionMutationType.REPLACE,
        "undo": CompositionMutationType.REPLACE,
    }.get(operation_type, CompositionMutationType.REPLACE)
    entity_ids = sorted(
        {
            str(value)
            for key, value in operation.items()
            if key.endswith("_id") and isinstance(value, str) and value
        }
    )
    material = {
        "workspace_revision": workspace_revision,
        "operation": dict(operation),
    }
    mutation = _mutation(
        generation_id=generation_id,
        mutation_type=mutation_type,
        reason_code=f"user_{operation_type}",
        source_entity_ids=entity_ids,
        target_entity_ids=entity_ids,
        created_by="user_edit",
        affected_intent_ids=list(affected_intent_ids),
        affected_rule_ids=list(affected_rule_ids),
    )
    return mutation.model_copy(
        update={
            "mutation_id": f"composition_mutation_{canonical_json_hash(material)[:24]}"
        }
    )
