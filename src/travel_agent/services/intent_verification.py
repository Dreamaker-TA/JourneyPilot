from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

from ..entities.composition_rules import (
    CadenceRuleParameters,
    CompositionRule,
    CompositionRuleKind,
    CountRuleParameters,
    OutputExplanationRuleParameters,
    SequenceRuleParameters,
    TimeWindowRuleParameters,
)
from ..entities.delivery_bundle import TripWorkspaceV2
from ..entities.candidate_selection import CandidateSelectionRole
from ..entities.intent_coverage import (
    CoverageEntityRef,
    IntentCoverageItem,
    IntentCoverageReport,
    IntentCoverageStatus,
    IntentDeviation,
    IntentFidelityGap,
    IntentContractSnapshot,
)
from ..entities.intent_spec import (
    AlternativeIntentValue,
    IntentKind,
    IntentSpec,
    IntentStrength,
    canonical_json_hash,
)


def _entity_rows(workspace: TripWorkspaceV2) -> list[dict]:
    candidate_index = workspace.recommendation_catalog.candidate_index()
    day_context = {
        day.day_id: (day.day, day.destination_id)
        for day in workspace.itinerary.day_plans
    }
    timeline_order: dict[str, tuple[int, int]] = {}
    timeline_day: dict[str, str] = {}
    for day in sorted(workspace.itinerary.day_plans, key=lambda item: item.day):
        for index, entry in enumerate(day.timeline):
            timeline_order.setdefault(entry.entity_id, (day.day, index))
            timeline_day.setdefault(entry.entity_id, day.day_id)
    rows: list[dict] = []
    for kind, entities in (
        ("visit", workspace.itinerary.visit_stops),
        ("dining", workspace.itinerary.dining_stops),
        ("lodging", workspace.itinerary.lodging_stays),
        ("transport", workspace.itinerary.transport_legs),
    ):
        for entity in entities:
            entity_id = getattr(
                entity,
                "item_id",
                getattr(entity, "stay_id", getattr(entity, "transport_leg_id", "")),
            )
            day_id = getattr(entity, "day_id", None) or timeline_day.get(entity_id)
            candidate = candidate_index.get(entity.lineage.candidate_id)
            rows.append(
                {
                    "kind": kind,
                    "entity_id": entity_id,
                    "day_id": day_id,
                    "destination_id": (
                        day_context.get(day_id, (None, None))[1]
                        or getattr(candidate, "destination_id", None)
                    ),
                    "candidate_id": entity.lineage.candidate_id,
                    "label": getattr(
                        entity,
                        "name",
                        getattr(
                            entity,
                            "property_name",
                            getattr(entity, "transport_leg_id", entity_id),
                        ),
                    ),
                    "fact_ids": list(entity.lineage.fact_assertion_ids),
                    "planned_start": getattr(
                        entity,
                        "planned_start",
                        getattr(entity, "departure_at", None),
                    ),
                    "intent_explanations": list(
                        getattr(entity, "intent_explanations", [])
                    ),
                }
            )
    return sorted(
        rows,
        key=lambda row: (
            timeline_order.get(row["entity_id"], (10**6, 10**6)),
            row["kind"],
            row["entity_id"],
        ),
    )


def _domain_kind(rule: CompositionRule) -> str | None:
    if rule.target_domain is None:
        return None
    return {
        "visit": "visit",
        "dining": "dining",
        "lodging": "lodging",
        "local_transport": "transport",
        "long_distance_transport": "transport",
    }[rule.target_domain.value]


def _window_matches(value: datetime | None, window: str) -> bool:
    if value is None:
        return False
    normalized = window.casefold()
    hour = value.hour
    if "afternoon" in normalized or "下午" in normalized:
        return 12 <= hour < 18
    if "morning" in normalized or "上午" in normalized:
        return 5 <= hour < 12
    if "evening" in normalized or "晚上" in normalized or "晚间" in normalized:
        return 18 <= hour <= 23
    if "lunch" in normalized or "午餐" in normalized:
        return 11 <= hour < 14
    if "dinner" in normalized or "晚餐" in normalized:
        return 17 <= hour < 22
    return True


def _gap_reason(rule: CompositionRule | None, status: IntentCoverageStatus) -> str:
    if rule is None:
        return (
            "intent_evidence_unavailable"
            if status is IntentCoverageStatus.UNVERIFIABLE
            else "semantic_coverage_low"
        )
    return {
        CompositionRuleKind.MUST_PLACE: "required_candidate_missing",
        CompositionRuleKind.MUST_NOT_PLACE: "excluded_candidate_present",
        CompositionRuleKind.MAX_PER_DAY: "quantity_rule_violated",
        CompositionRuleKind.MIN_PER_DAY: "quantity_rule_violated",
        CompositionRuleKind.CADENCE: "cadence_rule_missing",
        CompositionRuleKind.TIME_WINDOW: "time_window_violated",
        CompositionRuleKind.SEQUENCE: "sequence_rule_violated",
        CompositionRuleKind.DESTINATION_SCOPE: "semantic_coverage_low",
        CompositionRuleKind.REST_WINDOW: "time_window_violated",
        CompositionRuleKind.MAX_TRAVEL_TIME: "quantity_rule_violated",
        CompositionRuleKind.OUTPUT_EXPLANATION: "output_requirement_missing",
    }[rule.rule_kind]


def _retry_target(
    rule: CompositionRule | None,
    *,
    any_catalog_match: bool,
    any_selected_match: bool,
) -> str:
    if rule is None:
        return "none"
    if rule.rule_kind is CompositionRuleKind.OUTPUT_EXPLANATION:
        return "delivery_projection"
    if rule.rule_kind is CompositionRuleKind.MUST_PLACE:
        if not any_catalog_match:
            return "candidate_gate"
        if not any_selected_match:
            return "candidate_selection"
    if rule.rule_kind in {
        CompositionRuleKind.MUST_PLACE,
        CompositionRuleKind.MUST_NOT_PLACE,
        CompositionRuleKind.MAX_PER_DAY,
        CompositionRuleKind.MIN_PER_DAY,
        CompositionRuleKind.CADENCE,
        CompositionRuleKind.TIME_WINDOW,
        CompositionRuleKind.SEQUENCE,
    }:
        return "itinerary_planner"
    return "none"


def _evaluate_rule(
    rule: CompositionRule,
    *,
    rows: list[dict],
    matched_candidates: set[str],
    violated_candidates: set[str],
    unknown_candidates: set[str],
    day_number: dict[str, int],
    destination_ids: set[str],
    candidate_labels: dict[str, str],
) -> tuple[IntentCoverageStatus, list[dict], list[int], list[int]]:
    kind = _domain_kind(rule)
    scoped = [row for row in rows if kind is None or row["kind"] == kind]
    matched = [row for row in scoped if row["candidate_id"] in matched_candidates]
    unknown = [row for row in scoped if row["candidate_id"] in unknown_candidates]
    all_days = sorted(day_number.values())
    if rule.rule_kind is CompositionRuleKind.MUST_NOT_PLACE:
        violated = [row for row in scoped if row["candidate_id"] in violated_candidates]
        return (
            IntentCoverageStatus.UNSATISFIED
            if violated
            else IntentCoverageStatus.SATISFIED,
            violated,
            [],
            [],
        )
    if rule.rule_kind is CompositionRuleKind.MUST_PLACE:
        if matched:
            return IntentCoverageStatus.SATISFIED, matched, [], []
        if unknown:
            return IntentCoverageStatus.UNVERIFIABLE, unknown, [], []
        return IntentCoverageStatus.UNSATISFIED, [], [], []
    if rule.rule_kind in {
        CompositionRuleKind.MAX_PER_DAY,
        CompositionRuleKind.MIN_PER_DAY,
    }:
        assert isinstance(rule.parameters, CountRuleParameters)
        counts: dict[int, int] = defaultdict(int)
        for row in scoped:
            if row["day_id"] in day_number:
                counts[day_number[row["day_id"]]] += 1
        if rule.parameters.unit == "day":
            if rule.rule_kind is CompositionRuleKind.MAX_PER_DAY:
                missing = [
                    day for day in all_days if counts[day] > rule.parameters.count
                ]
            else:
                missing = [
                    day for day in all_days if counts[day] < rule.parameters.count
                ]
            covered = [day for day in all_days if day not in missing]
            return (
                IntentCoverageStatus.SATISFIED
                if not missing
                else IntentCoverageStatus.UNSATISFIED,
                scoped,
                covered,
                missing,
            )
        if rule.parameters.unit == "destination":
            destinations = sorted(destination_ids)
            counts = {
                destination_id: sum(
                    row["destination_id"] == destination_id for row in scoped
                )
                for destination_id in destinations
            }
            passed = all(
                count <= rule.parameters.count
                if rule.rule_kind is CompositionRuleKind.MAX_PER_DAY
                else count >= rule.parameters.count
                for count in counts.values()
            )
            return (
                IntentCoverageStatus.SATISFIED
                if destinations and passed
                else IntentCoverageStatus.UNSATISFIED,
                scoped,
                [],
                [],
            )
        total = len(scoped)
        passed = (
            total <= rule.parameters.count
            if rule.rule_kind is CompositionRuleKind.MAX_PER_DAY
            else total >= rule.parameters.count
        )
        return (
            IntentCoverageStatus.SATISFIED
            if passed
            else IntentCoverageStatus.UNSATISFIED,
            scoped,
            all_days if passed else [],
            [] if passed else all_days,
        )
    if rule.rule_kind is CompositionRuleKind.CADENCE:
        assert isinstance(rule.parameters, CadenceRuleParameters)
        relevant = matched if rule.parameters.required_attributes else scoped
        if rule.parameters.frequency == "once_per_day":
            counts: dict[int, int] = defaultdict(int)
            for row in relevant:
                if row["day_id"] in day_number:
                    counts[day_number[row["day_id"]]] += 1
            missing = [day for day in all_days if counts[day] < rule.parameters.count]
            if rule.parameters.time_window:
                missing.extend(
                    day_number[row["day_id"]]
                    for row in relevant
                    if row["day_id"] in day_number
                    and not _window_matches(
                        row["planned_start"], rule.parameters.time_window
                    )
                )
            missing = sorted(set(missing))
            return (
                IntentCoverageStatus.SATISFIED
                if not missing
                else IntentCoverageStatus.UNSATISFIED,
                relevant,
                [day for day in all_days if day not in missing],
                missing,
            )
        if rule.parameters.frequency == "once_per_destination":
            destinations = destination_ids
            counts = {
                destination_id: sum(
                    row["destination_id"] == destination_id for row in relevant
                )
                for destination_id in destinations
            }
            passed = bool(destinations) and all(
                count >= rule.parameters.count for count in counts.values()
            )
            return (
                IntentCoverageStatus.SATISFIED
                if passed
                else IntentCoverageStatus.UNSATISFIED,
                relevant,
                [],
                [],
            )
        passed = len(relevant) >= rule.parameters.count
        return (
            IntentCoverageStatus.SATISFIED
            if passed
            else IntentCoverageStatus.UNSATISFIED,
            relevant,
            [],
            [],
        )
    if rule.rule_kind is CompositionRuleKind.TIME_WINDOW:
        assert isinstance(rule.parameters, TimeWindowRuleParameters)
        relevant = matched if rule.parameters.applies_to else scoped
        failed = [
            row
            for row in relevant
            if not _window_matches(row["planned_start"], rule.parameters.window)
        ]
        return (
            IntentCoverageStatus.SATISFIED
            if relevant and not failed
            else IntentCoverageStatus.UNSATISFIED,
            relevant,
            [],
            sorted(
                {
                    day_number[row["day_id"]]
                    for row in failed
                    if row["day_id"] in day_number
                }
            ),
        )
    if rule.rule_kind is CompositionRuleKind.SEQUENCE:
        assert isinstance(rule.parameters, SequenceRuleParameters)
        order = [
            candidate_labels.get(row["candidate_id"], row["label"])
            for row in rows
        ]
        positions = [
            next(
                (
                    index
                    for index, value in enumerate(order)
                    if item.casefold() in value.casefold()
                ),
                -1,
            )
            for item in rule.parameters.ordered_items
        ]
        passed = all(index >= 0 for index in positions) and positions == sorted(
            positions
        )
        return (
            IntentCoverageStatus.SATISFIED
            if passed
            else IntentCoverageStatus.UNSATISFIED,
            matched,
            [],
            [],
        )
    if rule.rule_kind is CompositionRuleKind.OUTPUT_EXPLANATION:
        assert isinstance(rule.parameters, OutputExplanationRuleParameters)
        if rule.parameters.applies_to in {"trip", "delivery"}:
            return IntentCoverageStatus.SATISFIED, [], [], []
        explained = [
            row
            for row in scoped
            if any(
                explanation.intent_id == rule.intent_id
                for explanation in row.get("intent_explanations", [])
            )
        ]
        if rule.parameters.applies_to == "each_day":
            explained_days = {
                day_number[row["day_id"]]
                for row in explained
                if row["day_id"] in day_number
            }
            passed = explained_days == set(all_days)
        else:
            passed = bool(explained) and len(explained) == len(scoped)
        return (
            IntentCoverageStatus.SATISFIED
            if passed
            else IntentCoverageStatus.UNSATISFIED,
            explained,
            [],
            [],
        )
    if matched:
        return IntentCoverageStatus.SATISFIED, matched, [], []
    if unknown:
        return IntentCoverageStatus.UNVERIFIABLE, unknown, [], []
    return IntentCoverageStatus.UNSATISFIED, [], [], []


def evaluate_intent_fidelity(
    *,
    intent_spec: IntentSpec | IntentContractSnapshot,
    rules: Iterable[CompositionRule],
    workspace: TripWorkspaceV2,
    repair_budget_exhausted: bool,
) -> tuple[IntentCoverageReport, list[IntentFidelityGap]]:
    rules_by_intent: dict[str, list[CompositionRule]] = defaultdict(list)
    for rule in rules:
        rules_by_intent[rule.intent_id].append(rule)
    rows = _entity_rows(workspace)
    selected_candidate_ids = (
        workspace.candidate_selection_plan.composition_candidate_ids()
    )
    matches = workspace.recommendation_catalog.candidate_intent_matches
    matched_by_intent: dict[str, set[str]] = defaultdict(set)
    violated_by_intent: dict[str, set[str]] = defaultdict(set)
    unknown_by_intent: dict[str, set[str]] = defaultdict(set)
    facts_by_intent: dict[str, set[str]] = defaultdict(set)
    for match in matches:
        if match.status.value == "matched":
            matched_by_intent[match.intent_id].add(match.candidate_id)
            facts_by_intent[match.intent_id].update(match.supporting_fact_assertion_ids)
        elif match.status.value == "violated":
            violated_by_intent[match.intent_id].add(match.candidate_id)
            facts_by_intent[match.intent_id].update(match.supporting_fact_assertion_ids)
        elif match.status.value == "unknown":
            unknown_by_intent[match.intent_id].add(match.candidate_id)
    day_number = {day.day_id: day.day for day in workspace.itinerary.day_plans}
    destination_ids = {
        day.destination_id for day in workspace.itinerary.day_plans
    }
    candidate_labels = {
        candidate.candidate_id: getattr(
            candidate,
            "name",
            getattr(
                candidate,
                "branch_name",
                getattr(candidate, "property_name", candidate.candidate_id),
            ),
        )
        for candidate in workspace.recommendation_catalog.candidate_index().values()
    }
    conflicted_intent_ids = (
        set(intent_spec.conflicted_intent_ids)
        if isinstance(intent_spec, IntentContractSnapshot)
        else {
            intent_id
            for conflict in intent_spec.conflicts
            for intent_id in conflict.intent_ids
        }
    )
    items: list[IntentCoverageItem] = []
    gaps: list[IntentFidelityGap] = []
    deviations: list[IntentDeviation] = []
    for intent in intent_spec.active_items:
        intent_rules = rules_by_intent.get(intent.intent_id, [])
        evaluations = [
            (
                rule,
                *_evaluate_rule(
                    rule,
                    rows=rows,
                    matched_candidates=matched_by_intent[intent.intent_id],
                    violated_candidates=violated_by_intent[intent.intent_id],
                    unknown_candidates=unknown_by_intent[intent.intent_id],
                    day_number=day_number,
                    destination_ids=destination_ids,
                    candidate_labels=candidate_labels,
                ),
            )
            for rule in intent_rules
        ]
        if intent.intent_id in conflicted_intent_ids:
            evaluations = [(None, IntentCoverageStatus.CONFLICTED, [], [], [])]
        elif intent.kind is IntentKind.OBJECTIVE:
            status = (
                IntentCoverageStatus.SATISFIED
                if workspace.itinerary.day_plans
                else IntentCoverageStatus.UNSATISFIED
            )
            evaluations = [(None, status, rows, [], [])]
        elif intent.kind is IntentKind.DIVERSITY:
            selection_policy = workspace.candidate_selection_plan.selection_policy
            status = (
                IntentCoverageStatus.SATISFIED
                if selection_policy.mode == "explore"
                else IntentCoverageStatus.UNSATISFIED
            )
            evaluations = [(None, status, [], [], [])]
        elif intent.kind is IntentKind.ALTERNATIVES:
            selection_policy = workspace.candidate_selection_plan.selection_policy
            alternative_count = sum(
                entry.role is CandidateSelectionRole.ALTERNATIVE
                for entry in workspace.candidate_selection_plan.entries
            )
            requested_count = (
                intent.value.count
                if isinstance(intent.value, AlternativeIntentValue)
                else 2
            )
            status = (
                IntentCoverageStatus.SATISFIED
                if selection_policy.mode == "explore"
                and alternative_count >= requested_count - 1
                else IntentCoverageStatus.UNSATISFIED
            )
            evaluations = [(None, status, [], [], [])]
        elif not evaluations:
            matched_rows = [
                row
                for row in rows
                if row["candidate_id"] in matched_by_intent[intent.intent_id]
            ]
            if matched_rows:
                status = IntentCoverageStatus.SATISFIED
            elif unknown_by_intent[intent.intent_id]:
                status = IntentCoverageStatus.UNVERIFIABLE
            else:
                status = IntentCoverageStatus.UNSATISFIED
            evaluations = [(None, status, matched_rows, [], [])]
        statuses = [evaluation[1] for evaluation in evaluations]
        if IntentCoverageStatus.UNSATISFIED in statuses:
            status = IntentCoverageStatus.UNSATISFIED
        elif IntentCoverageStatus.CONFLICTED in statuses:
            status = IntentCoverageStatus.CONFLICTED
        elif IntentCoverageStatus.UNVERIFIABLE in statuses:
            status = IntentCoverageStatus.UNVERIFIABLE
        elif IntentCoverageStatus.PARTIALLY_SATISFIED in statuses:
            status = IntentCoverageStatus.PARTIALLY_SATISFIED
        else:
            status = IntentCoverageStatus.SATISFIED
        supporting_rows = {
            row["entity_id"]: row
            for _rule_value, _status, matched_rows, _covered, _missing in evaluations
            for row in matched_rows
        }
        failed_rules = [
            rule
            for rule, evaluation_status, _rows, _covered, _missing in evaluations
            if rule is not None
            and evaluation_status is not IntentCoverageStatus.SATISFIED
        ]
        covered_days = sorted(
            {
                day
                for _rule_value, _status, _rows, covered, _missing in evaluations
                for day in covered
            }
        )
        missing_days = sorted(
            {
                day
                for _rule_value, _status, _rows, _covered, missing in evaluations
                for day in missing
            }
        )
        hard = intent.strength is IntentStrength.HARD
        never_violate = any(
            rule.policy_on_failure == "never_violate" for rule in failed_rules
        )
        blocking = (
            hard
            and status is not IntentCoverageStatus.SATISFIED
            and (never_violate or not repair_budget_exhausted)
        )
        public_explanation = (
            f"已落实：{intent.public_summary}"
            if status is IntentCoverageStatus.SATISFIED
            else f"{intent.public_summary}：{status.value}"
        )
        item = IntentCoverageItem(
            intent_id=intent.intent_id,
            status=status,
            supporting_entity_refs=[
                CoverageEntityRef(entity_type=row["kind"], entity_id=row["entity_id"])
                for row in supporting_rows.values()
            ]
            if not never_violate
            else [],
            supporting_candidate_ids=sorted(
                {
                    row["candidate_id"]
                    for row in supporting_rows.values()
                    if row["candidate_id"]
                }
            ),
            supporting_fact_assertion_ids=sorted(facts_by_intent[intent.intent_id]),
            violated_entity_refs=[
                CoverageEntityRef(entity_type=row["kind"], entity_id=row["entity_id"])
                for row in supporting_rows.values()
            ]
            if never_violate
            else [],
            covered_days=covered_days,
            missing_days=missing_days,
            verification_mode=intent.verification_mode,
            public_explanation=public_explanation,
            blocking=blocking,
        )
        items.append(item)
        if status is IntentCoverageStatus.SATISFIED:
            continue
        primary_rule = (
            failed_rules[0]
            if failed_rules
            else (intent_rules[0] if intent_rules else None)
        )
        reason = _gap_reason(primary_rule, status)
        retry_target = _retry_target(
            primary_rule,
            any_catalog_match=bool(matched_by_intent[intent.intent_id]),
            any_selected_match=bool(
                matched_by_intent[intent.intent_id] & selected_candidate_ids
            ),
        )
        material = {
            "generation_id": intent_spec.generation_id,
            "intent_id": intent.intent_id,
            "reason": reason,
            "workspace_revision": workspace.workspace_revision,
        }
        gap_id = f"intent_gap_{canonical_json_hash(material)[:24]}"
        gaps.append(
            IntentFidelityGap(
                gap_id=gap_id,
                intent_id=intent.intent_id,
                reason=reason,
                blocking=blocking,
                retry_target=retry_target,
                affected_entity_ids=sorted(supporting_rows),
                violated_rule_ids=sorted(rule.rule_id for rule in failed_rules),
                repair_context={"missing_days": missing_days},
            )
        )
        deviations.append(
            IntentDeviation(
                deviation_id=f"intent_deviation_{canonical_json_hash(material)[:24]}",
                intent_id=intent.intent_id,
                status=status.value,
                reason_code=reason,
                public_explanation=public_explanation,
            )
        )
    strength_by_intent = {
        intent.intent_id: intent.strength for intent in intent_spec.active_items
    }
    hard_items = [
        item
        for item in items
        if strength_by_intent[item.intent_id] is IntentStrength.HARD
    ]
    soft_items = [item for item in items if item not in hard_items]
    hard_rate = (
        sum(item.status is IntentCoverageStatus.SATISFIED for item in hard_items)
        / len(hard_items)
        if hard_items
        else 1.0
    )
    soft_rate = (
        sum(
            1.0
            if item.status is IntentCoverageStatus.SATISFIED
            else 0.5
            if item.status is IntentCoverageStatus.PARTIALLY_SATISFIED
            else 0.0
            for item in soft_items
        )
        / len(soft_items)
        if soft_items
        else 1.0
    )
    material = {
        "generation_id": intent_spec.generation_id,
        "intent_spec_revision": intent_spec.revision,
        "workspace_revision": workspace.workspace_revision,
        "items": [item.model_dump(mode="json") for item in items],
        "deviations": [item.model_dump(mode="json") for item in deviations],
    }
    content_hash = canonical_json_hash(material)
    report = IntentCoverageReport(
        coverage_report_id=f"intent_coverage_{content_hash[:24]}",
        generation_id=intent_spec.generation_id,
        intent_spec_revision=intent_spec.revision,
        workspace_revision=workspace.workspace_revision,
        items=items,
        hard_satisfaction_rate=hard_rate,
        soft_coverage_rate=soft_rate,
        blocking_gap_ids=[gap.gap_id for gap in gaps if gap.blocking],
        deviations=deviations,
        content_hash=content_hash,
    )
    return report, gaps


def revalidate_workspace_intent(
    workspace: TripWorkspaceV2,
) -> tuple[TripWorkspaceV2, IntentCoverageReport, list[IntentFidelityGap]]:
    from .composition_rule_compiler import compile_composition_rules
    from .intent_entity_binding import bind_intent_context

    intent_contract = workspace.intent_contract_snapshot
    bound = bind_intent_context(workspace, intent_contract)
    rules = compile_composition_rules(intent_contract)
    report, gaps = evaluate_intent_fidelity(
        intent_spec=intent_contract,
        rules=rules,
        workspace=bound,
        repair_budget_exhausted=True,
    )
    rules_by_id = {rule.rule_id: rule for rule in rules}
    if any(
        rules_by_id[rule_id].policy_on_failure == "never_violate"
        for gap in gaps
        for rule_id in gap.violated_rule_ids
    ):
        raise ValueError("workspace mutation violates a never-violate intent rule")
    updated = bound.model_copy(update={"intent_coverage_report": report})
    return TripWorkspaceV2.model_validate(updated.model_dump(mode="json")), report, gaps
