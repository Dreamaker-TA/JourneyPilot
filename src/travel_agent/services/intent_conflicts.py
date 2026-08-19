"""Deterministic conflict checks for normalized intent contracts."""

from __future__ import annotations

import json
import re
from typing import Iterable, List

from ..entities.intent_spec import (
    CategoryIntentValue,
    CountIntentValue,
    IntentConflict,
    IntentItem,
    IntentKind,
)


def detect_intent_conflicts(items: Iterable[IntentItem]) -> List[IntentConflict]:
    active = [item for item in items if item.status == "active"]
    conflicts = [*_include_exclude_conflicts(active), *_quantity_conflicts(active)]
    unique: dict[str, IntentConflict] = {}
    for conflict in conflicts:
        unique.setdefault(conflict.conflict_id, conflict)
    return list(unique.values())


def _include_exclude_conflicts(items: List[IntentItem]) -> List[IntentConflict]:
    included = [item for item in items if item.kind is IntentKind.MUST_INCLUDE]
    excluded = [item for item in items if item.kind is IntentKind.MUST_EXCLUDE]
    conflicts: List[IntentConflict] = []
    for left in included:
        for right in excluded:
            if left.target != right.target:
                continue
            overlap = _intent_tokens(left) & _intent_tokens(right)
            if not overlap:
                continue
            ids = sorted([left.intent_id, right.intent_id])
            conflicts.append(
                IntentConflict(
                    conflict_id=_conflict_id("direct_contradiction", ids),
                    intent_ids=ids,
                    conflict_type="direct_contradiction",
                    blocking=True,
                    user_visible_summary=(
                        "同一对象同时被要求加入和排除：" + "、".join(sorted(overlap))
                    )[:300],
                )
            )
    return conflicts


def _quantity_conflicts(items: List[IntentItem]) -> List[IntentConflict]:
    grouped: dict[tuple[object, str], List[IntentItem]] = {}
    for item in items:
        if item.kind is not IntentKind.QUANTITY or not isinstance(
            item.value, CountIntentValue
        ):
            continue
        grouped.setdefault((item.target, item.value.unit), []).append(item)

    conflicts: List[IntentConflict] = []
    for group in grouped.values():
        lower: tuple[int, IntentItem] | None = None
        upper: tuple[int, IntentItem] | None = None
        exact: tuple[int, IntentItem] | None = None
        for item in group:
            value = item.value
            if not isinstance(value, CountIntentValue):
                continue
            if value.operator == "at_least" and (lower is None or value.count > lower[0]):
                lower = (value.count, item)
            elif value.operator == "at_most" and (upper is None or value.count < upper[0]):
                upper = (value.count, item)
            elif value.operator == "exactly":
                exact = (value.count, item)
        offenders: List[IntentItem] = []
        if lower and upper and lower[0] > upper[0]:
            offenders = [lower[1], upper[1]]
        elif exact and lower and exact[0] < lower[0]:
            offenders = [exact[1], lower[1]]
        elif exact and upper and exact[0] > upper[0]:
            offenders = [exact[1], upper[1]]
        if not offenders:
            continue
        ids = sorted(item.intent_id for item in offenders)
        conflicts.append(
            IntentConflict(
                conflict_id=_conflict_id("quantity_infeasible", ids),
                intent_ids=ids,
                conflict_type="quantity_infeasible",
                blocking=True,
                user_visible_summary="同一安排的数量上下限无法同时满足",
            )
        )
    return conflicts


def _intent_tokens(item: IntentItem) -> set[str]:
    if isinstance(item.value, CategoryIntentValue):
        raw = item.value.categories
    else:
        raw = [json.dumps(item.value.model_dump(mode="json"), ensure_ascii=False)]
    tokens: set[str] = set()
    stop = (
        "不要",
        "必须",
        "安排",
        "加入",
        "排除",
        "避开",
        "avoid",
        "include",
        "exclude",
    )
    for value in raw:
        normalized = value.lower()
        for marker in stop:
            normalized = normalized.replace(marker, " ")
        tokens.update(
            re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z][a-zA-Z_-]+", normalized)
        )
    return tokens


def _conflict_id(kind: str, intent_ids: List[str]) -> str:
    from hashlib import sha256

    digest = sha256(
        (kind + "\0" + "\0".join(intent_ids)).encode("utf-8")
    ).hexdigest()[:24]
    return f"conflict_{digest}"
