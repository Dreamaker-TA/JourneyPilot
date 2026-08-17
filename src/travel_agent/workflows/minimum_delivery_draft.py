"""Deterministic, checkpointable Minimum Delivery Draft construction.

This module deliberately consumes only the controlled trip identity and the
already-normalized hard constraints.  It never reads a Candidate, Fact,
Provider response, Planner response, or raw user text.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

from ..entities.delivery_bundle import (
    MinimumDeliveryDayShell,
    MinimumDeliveryDraft,
    UserInputAnchor,
)
from ..entities.trip_input import ControlledTripIdentity
from .run_budget import build_run_budget_snapshot
from .run_deadline import build_run_deadline_snapshot, utc_now


MINIMUM_DELIVERY_POLICY_VERSION = "minimum_delivery.v1"


def _canonical_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    """Keep Draft anchors JSON-safe for checkpoint replay."""
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _stable_id(prefix: str, payload: Any) -> str:
    digest = _canonical_hash({"prefix": prefix, "payload": _json_value(payload)})
    return f"{prefix}_{digest[:24]}"


def _active_hard_constraints(constraint_pack: Any) -> list[Dict[str, Any]]:
    if not isinstance(constraint_pack, dict):
        return []
    active: list[Dict[str, Any]] = []
    for item in constraint_pack.get("hard_constraints") or []:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in (None, "active"):
            continue
        constraint_id = str(item.get("constraint_id") or "").strip()
        if constraint_id:
            active.append(_json_value(item))
    return sorted(active, key=lambda item: str(item["constraint_id"]))


def _identity_revision(state: Any, identity_payload: Dict[str, Any]) -> int:
    requested = int(getattr(state, "controlled_trip_identity_revision", 0) or 0)
    existing = getattr(state, "minimum_delivery_draft", None)
    if existing is None:
        return requested or 1
    prior_identity = next(
        (
            anchor.value
            for anchor in existing.user_input_anchors
            if anchor.input_kind == "controlled_identity"
            and anchor.field_path == "controlled_trip_identity"
        ),
        None,
    )
    if prior_identity == identity_payload:
        return max(requested, existing.controlled_trip_identity_revision)
    return max(requested, existing.controlled_trip_identity_revision + 1)


def _day_shells(identity: ControlledTripIdentity) -> list[MinimumDeliveryDayShell]:
    total_days = identity.duration_days
    destinations = list(identity.destinations)
    if total_days < len(destinations):
        raise ValueError("controlled trip duration cannot retain every destination boundary")

    shells: list[MinimumDeliveryDayShell] = []
    previous_destination_id: Optional[str] = None
    for index in range(total_days):
        destination_index = min((index * len(destinations)) // total_days, len(destinations) - 1)
        destination_id = destinations[destination_index].place_id
        shells.append(
            MinimumDeliveryDayShell(
                day_id=f"day_{index + 1}",
                day=index + 1,
                date=identity.start_date + timedelta(days=index),
                destination_id=destination_id,
                lodging_night=index < total_days - 1,
                arrival_from_destination_id=(
                    previous_destination_id
                    if previous_destination_id is not None and previous_destination_id != destination_id
                    else None
                ),
            )
        )
        previous_destination_id = destination_id
    return shells


def _anchors(
    identity_payload: Dict[str, Any],
    hard_constraints: Iterable[Dict[str, Any]],
) -> tuple[list[UserInputAnchor], list[str]]:
    anchors = [
        UserInputAnchor(
            anchor_id=_stable_id("anchor_identity", identity_payload),
            field_path="controlled_trip_identity",
            value=identity_payload,
            input_kind="controlled_identity",
        )
    ]
    origin = identity_payload.get("origin")
    if isinstance(origin, dict) and str(origin.get("place_id") or "").strip():
        anchors.append(
            UserInputAnchor(
                anchor_id=_stable_id("anchor_origin", origin),
                field_path="controlled_trip_identity.origin",
                value=origin,
                input_kind="controlled_identity",
            )
        )
    destinations = identity_payload.get("destinations")
    if isinstance(destinations, list):
        for index, destination in enumerate(destinations):
            if not isinstance(destination, dict) or not str(
                destination.get("place_id") or ""
            ).strip():
                continue
            anchors.append(
                UserInputAnchor(
                    anchor_id=_stable_id(
                        "anchor_destination",
                        {"index": index, "destination": destination},
                    ),
                    field_path=(
                        "controlled_trip_identity.destinations."
                        f"{index}"
                    ),
                    value=destination,
                    input_kind="controlled_identity",
                )
            )
    constraint_ids: list[str] = []
    for constraint in hard_constraints:
        constraint_id = str(constraint["constraint_id"])
        public_summary = str(constraint.get("public_summary") or "").strip()
        if not public_summary:
            raise ValueError(
                f"hard constraint {constraint_id} requires a canonical user-visible value"
            )
        constraint_ids.append(constraint_id)
        anchors.append(
            UserInputAnchor(
                anchor_id=_stable_id("anchor_constraint", constraint_id),
                field_path=f"constraint_pack.hard_constraints.{constraint_id}",
                value=constraint,
                input_kind="hard_constraint",
                constraint_id=constraint_id,
                public_summary=public_summary,
            )
        )
    return anchors, constraint_ids


def _draft_material(
    *,
    run_id: str,
    identity_revision: int,
    constraint_revision: int,
    plan_revision: int,
    day_shells: list[MinimumDeliveryDayShell],
    anchors: list[UserInputAnchor],
    constraint_ids: list[str],
    policy_version: str,
) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "controlled_trip_identity_revision": identity_revision,
        "constraint_pack_revision": constraint_revision,
        "plan_revision": plan_revision,
        "policy_version": policy_version,
        "day_shells": [item.model_dump(mode="json") for item in day_shells],
        "preserved_constraint_ids": constraint_ids,
        "user_input_anchors": [item.model_dump(mode="json") for item in anchors],
    }


def build_minimum_delivery_draft(
    state: Any,
    *,
    policy_version: str = MINIMUM_DELIVERY_POLICY_VERSION,
) -> Optional[MinimumDeliveryDraft]:
    """Build a stable unsealed Draft for the current state generation.

    ``None`` means the run has not acquired a controlled identity and therefore
    is not eligible for the completion-guarantee path.
    """
    raw_identity = getattr(state, "controlled_trip_identity", None)
    if not raw_identity:
        return None
    identity = ControlledTripIdentity.model_validate(raw_identity)
    identity_payload = identity.model_dump(mode="json")
    identity_revision = _identity_revision(state, identity_payload)
    constraint_revision = int(getattr(state, "constraint_pack_revision", 0) or 0)
    plan_revision = int(getattr(state, "plan_gate_revision_count", 0) or 0)
    hard_constraints = _active_hard_constraints(getattr(state, "constraint_pack", None))
    anchors, constraint_ids = _anchors(identity_payload, hard_constraints)
    shells = _day_shells(identity)
    material = _draft_material(
        run_id=str(getattr(state, "run_id", "") or ""),
        identity_revision=identity_revision,
        constraint_revision=constraint_revision,
        plan_revision=plan_revision,
        day_shells=shells,
        anchors=anchors,
        constraint_ids=constraint_ids,
        policy_version=policy_version,
    )
    if not material["run_id"]:
        raise ValueError("minimum delivery draft requires a run id")
    content_hash = _canonical_hash(material)
    return MinimumDeliveryDraft(
        draft_id=f"draft_{content_hash[:24]}",
        content_hash=content_hash,
        planning_authorized=False,
        planning_authorized_at=None,
        **material,
    )


def _clear_completion_generation() -> Dict[str, Any]:
    return {
        "run_deadline": None,
        "run_budget": None,
        "gate_failure_attributions": {},
        "candidate_research_gaps": [],
        "candidate_gate_attempts": {},
        "candidate_gate_failure_signatures": {},
        "terminal_attribution": None,
    }


async def minimum_delivery_draft_builder_node(state: Any) -> Dict[str, Any]:
    """Persist the deterministic seed before geo/weather/planner work begins."""
    draft = build_minimum_delivery_draft(state)
    existing = getattr(state, "minimum_delivery_draft", None)
    if draft is None:
        return _clear_completion_generation() if existing is not None else {}
    if existing is not None and existing.draft_id == draft.draft_id:
        # A replay after seal must keep the original authorization instant.
        return {
            "controlled_trip_identity_revision": draft.controlled_trip_identity_revision,
        }
    return {
        "controlled_trip_identity_revision": draft.controlled_trip_identity_revision,
        "minimum_delivery_draft": draft,
        **_clear_completion_generation(),
    }


def _sealed_content_hash(
    draft: MinimumDeliveryDraft,
    anchors: list[UserInputAnchor],
    authorization: datetime,
) -> str:
    payload = draft.model_dump(mode="json")
    payload.pop("content_hash", None)
    payload["planning_authorized"] = True
    payload["planning_authorized_at"] = authorization.isoformat()
    payload["user_input_anchors"] = [item.model_dump(mode="json") for item in anchors]
    return _canonical_hash(payload)


def seal_minimum_delivery_draft(
    state: Any,
    *,
    authorized_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Seal the current Draft after an explicit user approval.

    A replay of an already sealed state retains its original timestamp, deadline
    and resource budget.  It never grants a new window and never refills the
    call/token/cost allowance.
    """
    draft = getattr(state, "minimum_delivery_draft", None)
    if draft is None:
        raise ValueError("cannot authorize a run without a minimum delivery draft")
    if draft.planning_authorized:
        deadline = getattr(state, "run_deadline", None) or build_run_deadline_snapshot(draft)
        budget = getattr(state, "run_budget", None) or build_run_budget_snapshot()
        return {
            "minimum_delivery_draft": draft,
            "run_deadline": deadline,
            "run_budget": budget,
        }

    authorization = authorized_at or utc_now()
    if authorization.tzinfo is None:
        raise ValueError("planning authorization timestamp must be timezone-aware")
    authorization = authorization.astimezone(timezone.utc)
    authorization_anchor = UserInputAnchor(
        anchor_id=f"authorization_{draft.draft_id}",
        field_path="planning_authorization",
        value=authorization.isoformat(),
        input_kind="planning_authorization",
    )
    anchors = [*draft.user_input_anchors, authorization_anchor]
    sealed_hash = _sealed_content_hash(draft, anchors, authorization)
    sealed = MinimumDeliveryDraft.model_validate(
        {
            **draft.model_dump(mode="json"),
            "planning_authorized": True,
            "planning_authorized_at": authorization,
            "user_input_anchors": [item.model_dump(mode="json") for item in anchors],
            "content_hash": sealed_hash,
        }
    )
    return {
        "minimum_delivery_draft": sealed,
        "run_deadline": build_run_deadline_snapshot(sealed),
        "run_budget": build_run_budget_snapshot(),
        "terminal_attribution": None,
    }
