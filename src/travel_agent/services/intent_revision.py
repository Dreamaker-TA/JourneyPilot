"""Build immutable request-contract revisions and planning generations."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..entities.intent_spec import (
    CadenceIntentValue,
    CategoryIntentValue,
    CountIntentValue,
    IntentItem,
    IntentKind,
    IntentSourceKind,
    IntentSpec,
    OutputRequirementValue,
    TimeWindowIntentValue,
    canonical_json_hash,
    stable_intent_id,
)
from ..entities.planning_generation import PlanningGeneration
from ..entities.request_contract import (
    ClauseDisposition,
    InputClauseRecord,
    RequestContract,
)
from .intent_conflicts import detect_intent_conflicts
from .intent_normalization import (
    RequestContractNormalizationResult,
    SourceClause,
    is_material_clause,
)


SOURCE_PRECEDENCE: Dict[str, int] = {
    "current_request": 700,
    "plan_gate_amendment": 600,
    "run_supplement": 500,
    "trip_context": 400,
    "preset": 400,
    "saved_preference": 300,
    "system_default": 100,
}
REQUEST_CONTRACT_POLICY_VERSION = "request_contract_revision.v1"


def build_request_contract_revision(
    *,
    run_id: str,
    identity: Mapping[str, Any],
    identity_revision: int,
    constraint_pack: Dict[str, Any],
    constraint_pack_revision: int,
    clauses: List[SourceClause],
    normalized: RequestContractNormalizationResult,
    previous: Optional[RequestContract] = None,
    plan_revision: int = 0,
    preserve_generation_id: bool = False,
) -> tuple[RequestContract, PlanningGeneration]:
    normalized_by_id = {item.clause_id: item for item in normalized.clauses}
    constraint_links = _constraint_links(constraint_pack)
    new_items: List[IntentItem] = []
    ledger: List[InputClauseRecord] = []
    unresolved = []

    for clause in clauses:
        draft = normalized_by_id[clause.clause_id]
        source_kind = _source_kind(clause.source_kind)
        mapped_ids: List[str] = []
        for intent_draft in draft.intents:
            intent_id = stable_intent_id(
                source_ref_id=clause.source_ref_id,
                kind=intent_draft.kind,
                target=intent_draft.target,
                value=intent_draft.value,
            )
            mapped_ids.append(intent_id)
            new_items.append(
                IntentItem(
                    intent_id=intent_id,
                    kind=intent_draft.kind,
                    target=intent_draft.target,
                    strength=intent_draft.strength,
                    priority=intent_draft.priority,
                    value=intent_draft.value,
                    source_kind=source_kind,
                    source_ref_id=clause.source_ref_id,
                    source_text=clause.source_text,
                    source_span_start=clause.span_start,
                    source_span_end=clause.span_end,
                    linked_constraint_ids=constraint_links.get(clause.source_ref_id, []),
                    verification_mode=intent_draft.verification_mode,
                    impact_stages=intent_draft.impact_stages,
                    public_summary=intent_draft.public_summary,
                    status="active",
                )
            )
        ledger.append(
            InputClauseRecord(
                clause_id=clause.clause_id,
                source_ref_id=clause.source_ref_id,
                source_text=clause.source_text,
                material=is_material_clause(clause.source_text),
                disposition=draft.disposition,
                mapped_intent_ids=mapped_ids,
                reason_code=draft.reason_code,
            )
        )
        if draft.disposition in {
            ClauseDisposition.UNRESOLVED,
            ClauseDisposition.UNSUPPORTED,
        }:
            from ..entities.intent_spec import UnresolvedClause

            unresolved.append(
                UnresolvedClause(
                    clause_id=clause.clause_id,
                    source_ref_id=clause.source_ref_id,
                    source_text=clause.source_text,
                    reason_code=draft.reason_code or "unresolved",
                )
            )

    previous_items = list(previous.intent_spec.active_items) if previous else []
    active, newly_superseded = _merge_items(previous_items, new_items)
    superseded = _merge_superseded_items(
        list(previous.intent_spec.superseded_items) if previous else [],
        newly_superseded,
        active_ids={item.intent_id for item in active},
    )
    current_clause_ids = {item.clause_id for item in ledger}
    if previous is not None:
        ledger = _merge_clause_ledger(previous.clause_ledger, ledger)
        unresolved = [
            item
            for item in previous.intent_spec.unresolved_clauses
            if item.clause_id not in current_clause_ids
        ] + unresolved
    intent_revision = (previous.intent_spec.revision + 1) if previous else 1
    intent_material = {
        "revision": intent_revision,
        "active_items": [item.model_dump(mode="json") for item in active],
        "superseded_items": [item.model_dump(mode="json") for item in superseded],
        "unresolved_clauses": [item.model_dump(mode="json") for item in unresolved],
    }
    intent_hash = canonical_json_hash(intent_material)
    identity_hash = canonical_json_hash(identity)
    constraint_hash = canonical_json_hash(_constraint_semantic_payload(constraint_pack))
    generation_id = (
        previous.generation_id
        if preserve_generation_id and previous is not None
        else "generation_"
        + canonical_json_hash(
            {
                "run_id": run_id,
                "identity_revision": identity_revision,
                "intent_revision": intent_revision,
                "constraint_revision": constraint_pack_revision,
                "identity_hash": identity_hash,
                "intent_hash": intent_hash,
                "constraint_hash": constraint_hash,
            }
        )[:24]
    )
    intent_spec = IntentSpec(
        intent_spec_id=f"intent_spec_{intent_hash[:24]}",
        revision=intent_revision,
        generation_id=generation_id,
        content_hash=intent_hash,
        active_items=active,
        superseded_items=superseded,
        conflicts=detect_intent_conflicts(active),
        unresolved_clauses=unresolved,
        objective_summary=_objective_summary(active, identity),
        generated_from_message_ids=list(
            dict.fromkeys(
                [
                    *(previous.intent_spec.generated_from_message_ids if previous else []),
                    *_unique_source_ids(clauses, "message"),
                ]
            )
        ),
        generated_from_command_ids=list(
            dict.fromkeys(
                [
                    *(previous.intent_spec.generated_from_command_ids if previous else []),
                    *_unique_source_ids(clauses, "command"),
                ]
            )
        ),
    )
    request_material = {
        "generation_id": generation_id,
        "identity_revision": identity_revision,
        "constraint_revision": constraint_pack_revision,
        "intent_spec": intent_spec.model_dump(mode="json"),
        "constraint_pack": _constraint_semantic_payload(constraint_pack),
        "clause_ledger": [item.model_dump(mode="json") for item in ledger],
    }
    request_hash = canonical_json_hash(request_material)
    contract = RequestContract(
        request_contract_id=f"request_contract_{request_hash[:24]}",
        generation_id=generation_id,
        controlled_trip_identity_revision=identity_revision,
        constraint_pack_revision=constraint_pack_revision,
        intent_spec=intent_spec,
        constraint_pack=constraint_pack,
        clause_ledger=ledger,
        content_hash=request_hash,
    )
    generation = PlanningGeneration(
        generation_id=generation_id,
        controlled_trip_identity_revision=identity_revision,
        intent_spec_revision=intent_revision,
        constraint_pack_revision=constraint_pack_revision,
        plan_revision=plan_revision,
        identity_hash=identity_hash,
        intent_hash=intent_hash,
        constraint_hash=constraint_hash,
    )
    return contract, generation


def _source_kind(value: str) -> IntentSourceKind:
    mapping: Dict[str, IntentSourceKind] = {
        "current_request": "current_request",
        "plan_gate_amendment": "plan_gate_amendment",
        "run_supplement": "run_supplement",
        "preset": "preset",
        "saved_preference": "saved_preference",
        "trip_context": "trip_context",
        "system_default": "system_default",
    }
    return mapping.get(value, "trip_context")


def _merge_items(
    existing: Iterable[IntentItem], incoming: Iterable[IntentItem]
) -> tuple[List[IntentItem], List[IntentItem]]:
    active: Dict[str, IntentItem] = {item.intent_id: item for item in existing}
    superseded: Dict[str, IntentItem] = {}
    for item in incoming:
        if item.intent_id in active:
            continue
        precedence = SOURCE_PRECEDENCE[item.source_kind]
        item_slot = _intent_slot(item)
        competing = [
            current
            for current in active.values()
            if (item_slot is not None and _intent_slot(current) == item_slot)
            or _directly_contradicts(current, item)
        ]
        if any(
            SOURCE_PRECEDENCE[current.source_kind] > precedence
            for current in competing
        ):
            superseded[item.intent_id] = item.model_copy(
                update={"status": "superseded"}
            )
            continue
        for current in competing:
            current_precedence = SOURCE_PRECEDENCE[current.source_kind]
            if current_precedence < precedence or (
                current_precedence == precedence
                and item_slot is not None
                and _intent_slot(current) == item_slot
            ):
                active.pop(current.intent_id, None)
                superseded[current.intent_id] = current.model_copy(
                    update={"status": "superseded"}
                )
        active[item.intent_id] = item
    return list(active.values()), list(superseded.values())


def _merge_superseded_items(
    existing: Iterable[IntentItem],
    incoming: Iterable[IntentItem],
    *,
    active_ids: set[str],
) -> List[IntentItem]:
    merged = {
        item.intent_id: item
        for item in existing
        if item.intent_id not in active_ids
    }
    merged.update({item.intent_id: item for item in incoming})
    return list(merged.values())


def _intent_slot(item: IntentItem) -> tuple[object, ...] | None:
    value = item.value
    if item.kind in {IntentKind.OBJECTIVE, IntentKind.PACE, IntentKind.ALTERNATIVES}:
        return item.kind, item.target
    if isinstance(value, CountIntentValue):
        return item.kind, item.target, value.unit, value.operator
    if isinstance(value, CadenceIntentValue):
        return item.kind, item.target, value.frequency, value.time_window
    if isinstance(value, TimeWindowIntentValue):
        return item.kind, item.target, value.applies_to
    if isinstance(value, OutputRequirementValue):
        return item.kind, item.target, value.applies_to, value.required_field.casefold()
    return None


def _directly_contradicts(left: IntentItem, right: IntentItem) -> bool:
    if left.target != right.target:
        return False
    if {left.kind, right.kind} != {
        IntentKind.MUST_INCLUDE,
        IntentKind.MUST_EXCLUDE,
    }:
        return False
    if not isinstance(left.value, CategoryIntentValue) or not isinstance(
        right.value, CategoryIntentValue
    ):
        return False
    return bool(_category_tokens(left.value.categories) & _category_tokens(right.value.categories))


def _category_tokens(values: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        normalized = str(value).casefold()
        for marker in (
            "不要",
            "必须",
            "安排",
            "加入",
            "排除",
            "避开",
            "avoid",
            "include",
            "exclude",
        ):
            normalized = normalized.replace(marker, " ")
        tokens.update(
            re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z][a-zA-Z_-]+", normalized)
        )
    return tokens


def _merge_clause_ledger(
    existing: Iterable[InputClauseRecord], incoming: Iterable[InputClauseRecord]
) -> List[InputClauseRecord]:
    merged = {item.clause_id: item for item in existing}
    for item in incoming:
        merged[item.clause_id] = item
    return list(merged.values())


def _constraint_links(pack: Dict[str, Any]) -> Dict[str, List[str]]:
    links: Dict[str, List[str]] = {}
    for item in pack.get("constraints") or []:
        if not isinstance(item, dict) or item.get("status") != "active":
            continue
        constraint_id = str(item.get("constraint_id") or "")
        for ref in item.get("source_refs") or []:
            if not isinstance(ref, dict):
                continue
            origin = str(ref.get("origin_ref") or "")
            if origin and constraint_id:
                links.setdefault(origin, []).append(constraint_id)
    return {key: list(dict.fromkeys(values)) for key, values in links.items()}


def _constraint_semantic_payload(pack: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(pack)
    meta = dict(payload.get("pack_meta") or {})
    meta.pop("built_at", None)
    payload["pack_meta"] = meta
    normalized_constraints: List[Dict[str, Any]] = []
    for raw in payload.get("constraints") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item.pop("updated_at", None)
        refs = []
        for ref in item.get("source_refs") or []:
            if isinstance(ref, dict):
                normalized = dict(ref)
                normalized.pop("updated_at", None)
                refs.append(normalized)
        item["source_refs"] = refs
        normalized_constraints.append(item)
    payload["constraints"] = normalized_constraints
    by_id = {item.get("constraint_id"): item for item in normalized_constraints}
    payload["hard_constraints"] = [
        by_id.get(item.get("constraint_id"), item)
        for item in payload.get("hard_constraints") or []
        if isinstance(item, dict)
    ]
    payload["soft_preferences"] = [
        by_id.get(item.get("constraint_id"), item)
        for item in payload.get("soft_preferences") or []
        if isinstance(item, dict)
    ]
    return payload


def constraint_pack_semantic_hash(pack: Dict[str, Any]) -> str:
    return canonical_json_hash(_constraint_semantic_payload(pack))


def _objective_summary(items: List[IntentItem], identity: Mapping[str, Any]) -> str:
    objectives = [
        item.public_summary
        for item in sorted(items, key=lambda value: value.priority, reverse=True)
        if item.kind is IntentKind.OBJECTIVE
    ]
    if objectives:
        return objectives[0]
    destinations = [
        str(item.get("display_name") or item.get("name") or "")
        for item in identity.get("destinations") or []
        if isinstance(item, dict)
    ]
    return "规划 " + "、".join(value for value in destinations if value) + " 的可执行行程"


def _unique_source_ids(clauses: List[SourceClause], kind: str) -> List[str]:
    source_kinds = (
        {"plan_gate_amendment", "run_supplement"}
        if kind == "command"
        else {"current_request"}
    )
    return list(
        dict.fromkeys(
            item.source_ref_id
            for item in clauses
            if item.source_kind in source_kinds
        )
    )
