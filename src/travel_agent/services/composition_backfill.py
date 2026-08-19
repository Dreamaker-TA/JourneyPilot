from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from pydantic import Field

from ..entities.candidate_selection import SelectedCandidateCapability
from ..entities.composition_rules import (
    CompositionRule,
    CompositionRuleKind,
    CountRuleParameters,
)
from ..entities.contract_base import StrictModel
from ..entities.research_domain import ResearchDomain


class OpenCompositionSlot(StrictModel):
    day_id: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)
    allowed_candidate_kinds: list[str] = Field(default_factory=list)
    remaining_capacity: int = Field(ge=0)
    pending_intent_ids: list[str] = Field(default_factory=list)
    adjacent_entity_ids: list[str] = Field(default_factory=list)


_CANDIDATE_KIND_BY_DOMAIN = {
    ResearchDomain.VISIT: "visit",
    ResearchDomain.DINING: "dining",
    ResearchDomain.LODGING: "lodging",
    ResearchDomain.LOCAL_TRANSPORT: "transport",
    ResearchDomain.LONG_DISTANCE_TRANSPORT: "transport",
}


def build_legal_open_slots(
    payload: Mapping[str, Any],
    rules: Sequence[CompositionRule],
) -> list[OpenCompositionSlot]:
    max_per_day: dict[str, int] = {}
    pending_by_kind: dict[str, set[str]] = {}
    for rule in rules:
        kind = _CANDIDATE_KIND_BY_DOMAIN.get(rule.target_domain)
        if kind is None:
            continue
        if (
            rule.rule_kind is CompositionRuleKind.MAX_PER_DAY
            and isinstance(rule.parameters, CountRuleParameters)
            and rule.parameters.unit == "day"
        ):
            max_per_day[kind] = min(
                max_per_day.get(kind, rule.parameters.count),
                rule.parameters.count,
            )
        if rule.rule_kind in {
            CompositionRuleKind.MUST_PLACE,
            CompositionRuleKind.MIN_PER_DAY,
            CompositionRuleKind.CADENCE,
        }:
            pending_by_kind.setdefault(kind, set()).add(rule.intent_id)

    slots: list[OpenCompositionSlot] = []
    for day in payload.get("days", []):
        if not isinstance(day, Mapping):
            continue
        placements = [
            item
            for item in day.get("placements", [])
            if isinstance(item, Mapping)
        ]
        counts = Counter(str(item.get("placement_kind") or "") for item in placements)
        allowed = [
            kind
            for kind in ("visit", "dining")
            if counts[kind] < max_per_day.get(kind, 10**6)
        ]
        pending = sorted(
            intent_id
            for kind in allowed
            for intent_id in pending_by_kind.get(kind, set())
            if not any(
                intent_id in item.get("matched_intent_ids", []) for item in placements
            )
        )
        remaining = max(
            [max_per_day[kind] - counts[kind] for kind in allowed if kind in max_per_day]
            or [1]
        )
        slots.append(
            OpenCompositionSlot(
                day_id=str(day.get("day_id") or ""),
                destination_id=str(day.get("destination_id") or ""),
                allowed_candidate_kinds=allowed,
                remaining_capacity=max(remaining, 0),
                pending_intent_ids=pending,
                adjacent_entity_ids=[
                    str(item.get("candidate_id") or "")
                    for item in placements
                    if item.get("candidate_id")
                ],
            )
        )
    return slots


def order_backfill_candidates(
    candidates: Sequence[SelectedCandidateCapability],
    slot: OpenCompositionSlot,
) -> list[SelectedCandidateCapability]:
    return sorted(
        (
            candidate
            for candidate in candidates
            if candidate.destination_id == slot.destination_id
            and candidate.candidate_kind in slot.allowed_candidate_kinds
            and not candidate.hard_violation_intent_ids
        ),
        key=lambda candidate: (
            -len(set(candidate.matched_intent_ids) & set(slot.pending_intent_ids)),
            candidate.rank,
            candidate.candidate_id,
        ),
    )


def never_violate_rule_failures(
    payload: Mapping[str, Any],
    rules: Sequence[CompositionRule],
    candidates: Sequence[SelectedCandidateCapability],
) -> list[str]:
    candidate_index = {item.candidate_id: item for item in candidates}
    failures: list[str] = []
    for day in payload.get("days", []):
        if not isinstance(day, Mapping):
            continue
        placements = [
            item for item in day.get("placements", []) if isinstance(item, Mapping)
        ]
        for rule in rules:
            if rule.policy_on_failure != "never_violate":
                continue
            candidate_kind = _CANDIDATE_KIND_BY_DOMAIN.get(rule.target_domain)
            scoped = [
                placement
                for placement in placements
                if placement.get("placement_kind") == candidate_kind
            ]
            if (
                rule.rule_kind is CompositionRuleKind.MAX_PER_DAY
                and isinstance(rule.parameters, CountRuleParameters)
                and rule.parameters.unit == "day"
                and len(scoped) > rule.parameters.count
            ):
                failures.append(f"{rule.rule_id}:max_per_day:{day.get('day_id')}")
            if rule.rule_kind is CompositionRuleKind.MUST_NOT_PLACE:
                for placement in scoped:
                    candidate_id = str(placement.get("candidate_id") or "")
                    capability = candidate_index.get(candidate_id)
                    if not candidate_id or capability is None:
                        failures.append(f"{rule.rule_id}:unverified_authored_entry")
                    elif rule.intent_id in capability.hard_violation_intent_ids:
                        failures.append(f"{rule.rule_id}:{candidate_id}")
    return sorted(set(failures))


def assert_never_violate_rules(
    payload: Mapping[str, Any],
    rules: Sequence[CompositionRule],
    candidates: Sequence[SelectedCandidateCapability],
) -> None:
    failures = never_violate_rule_failures(payload, rules, candidates)
    if failures:
        raise ValueError(f"composition violates never-violate rules: {failures}")
