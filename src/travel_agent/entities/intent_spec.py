"""Versioned user-intent contracts for one planning request."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Annotated, List, Literal, Optional, Union

from pydantic import Field, model_validator

from .contract_base import StrictModel


INTENT_SPEC_VERSION = "journeypilot.intent_spec.v1"


class IntentStrength(str, Enum):
    HARD = "hard"
    SOFT = "soft"
    INFORMATIONAL = "informational"


class IntentKind(str, Enum):
    OBJECTIVE = "objective"
    MUST_INCLUDE = "must_include"
    MUST_EXCLUDE = "must_exclude"
    THEME = "theme"
    ATTRIBUTE_PREFERENCE = "attribute_preference"
    QUANTITY = "quantity"
    CADENCE = "cadence"
    TIME_WINDOW = "time_window"
    SEQUENCING = "sequencing"
    GEOGRAPHIC = "geographic"
    PACE = "pace"
    ALTERNATIVES = "alternatives"
    OUTPUT_REQUIREMENT = "output_requirement"
    DIVERSITY = "diversity"


class IntentTarget(str, Enum):
    TRIP = "trip"
    VISIT = "visit"
    DINING = "dining"
    LODGING = "lodging"
    LOCAL_TRANSPORT = "local_transport"
    LONG_DISTANCE_TRANSPORT = "long_distance_transport"
    ITINERARY = "itinerary"
    DELIVERY = "delivery"


class VerificationMode(str, Enum):
    DETERMINISTIC = "deterministic"
    SEMANTIC = "semantic"
    MIXED = "mixed"


class ScalarIntentValue(StrictModel):
    value_type: Literal["scalar"] = "scalar"
    value: str = Field(min_length=1, max_length=500)


class CategoryIntentValue(StrictModel):
    value_type: Literal["category"] = "category"
    categories: List[str] = Field(min_length=1)


class CountIntentValue(StrictModel):
    value_type: Literal["count"] = "count"
    operator: Literal["at_least", "at_most", "exactly"]
    count: int = Field(ge=0)
    unit: Literal["trip", "day", "destination"]


class CadenceIntentValue(StrictModel):
    value_type: Literal["cadence"] = "cadence"
    frequency: Literal[
        "once_per_trip",
        "once_per_destination",
        "once_per_day",
        "selected_days",
    ]
    count: int = Field(default=1, ge=1)
    time_window: Optional[str] = Field(default=None, max_length=80)
    required_attributes: List[str] = Field(default_factory=list)


class TimeWindowIntentValue(StrictModel):
    value_type: Literal["time_window"] = "time_window"
    window: str = Field(min_length=1, max_length=80)
    applies_to: Optional[str] = Field(default=None, max_length=120)


class SequenceIntentValue(StrictModel):
    value_type: Literal["sequence"] = "sequence"
    ordered_items: List[str] = Field(min_length=2)


class GeographicIntentValue(StrictModel):
    value_type: Literal["geographic"] = "geographic"
    area: str = Field(min_length=1, max_length=160)
    relation: Literal["inside", "near", "avoid", "same_area"]


class OutputRequirementValue(StrictModel):
    value_type: Literal["output_requirement"] = "output_requirement"
    required_field: str = Field(min_length=1, max_length=160)
    applies_to: Literal["each_item", "each_day", "trip", "delivery"]


class AlternativeIntentValue(StrictModel):
    value_type: Literal["alternative"] = "alternative"
    count: int = Field(ge=2, le=10)
    distinction: Optional[str] = Field(default=None, max_length=160)


IntentValue = Annotated[
    Union[
        ScalarIntentValue,
        CategoryIntentValue,
        CountIntentValue,
        CadenceIntentValue,
        TimeWindowIntentValue,
        SequenceIntentValue,
        GeographicIntentValue,
        OutputRequirementValue,
        AlternativeIntentValue,
    ],
    Field(discriminator="value_type"),
]


IntentSourceKind = Literal[
    "current_request",
    "plan_gate_amendment",
    "run_supplement",
    "preset",
    "saved_preference",
    "trip_context",
    "system_default",
]
IntentImpactStage = Literal[
    "research",
    "admission",
    "ranking",
    "composition",
    "projection",
]


class IntentItem(StrictModel):
    intent_id: str = Field(min_length=1)
    kind: IntentKind
    target: IntentTarget
    strength: IntentStrength
    priority: int = Field(ge=0, le=100)
    value: IntentValue
    source_kind: IntentSourceKind
    source_ref_id: str = Field(min_length=1)
    source_text: Optional[str] = Field(default=None, max_length=2000)
    source_span_start: Optional[int] = Field(default=None, ge=0)
    source_span_end: Optional[int] = Field(default=None, ge=0)
    linked_constraint_ids: List[str] = Field(default_factory=list)
    verification_mode: VerificationMode
    impact_stages: List[IntentImpactStage] = Field(min_length=1)
    public_summary: str = Field(min_length=1, max_length=300)
    status: Literal["active", "superseded", "conflicted", "unsupported"] = "active"

    @model_validator(mode="after")
    def validate_source_span(self) -> "IntentItem":
        if (self.source_span_start is None) != (self.source_span_end is None):
            raise ValueError("intent source span must provide both boundaries")
        if (
            self.source_span_start is not None
            and self.source_span_end is not None
            and self.source_span_end <= self.source_span_start
        ):
            raise ValueError("intent source span must be non-empty")
        if len(self.linked_constraint_ids) != len(set(self.linked_constraint_ids)):
            raise ValueError("linked constraint ids must be unique")
        if len(self.impact_stages) != len(set(self.impact_stages)):
            raise ValueError("intent impact stages must be unique")
        return self


class IntentConflict(StrictModel):
    conflict_id: str = Field(min_length=1)
    intent_ids: List[str] = Field(min_length=1)
    conflict_type: Literal[
        "direct_contradiction",
        "quantity_infeasible",
        "identity_conflict",
        "constraint_conflict",
        "unsupported_combination",
    ]
    blocking: bool
    user_visible_summary: str = Field(min_length=1, max_length=300)


class UnresolvedClause(StrictModel):
    clause_id: str = Field(min_length=1)
    source_ref_id: str = Field(min_length=1)
    source_text: str = Field(min_length=1, max_length=2000)
    reason_code: str = Field(min_length=1)


class IntentSpec(StrictModel):
    schema_version: Literal[INTENT_SPEC_VERSION] = INTENT_SPEC_VERSION
    intent_spec_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    generation_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_items: List[IntentItem] = Field(default_factory=list)
    superseded_items: List[IntentItem] = Field(default_factory=list)
    conflicts: List[IntentConflict] = Field(default_factory=list)
    unresolved_clauses: List[UnresolvedClause] = Field(default_factory=list)
    objective_summary: str = Field(min_length=1, max_length=500)
    generated_from_message_ids: List[str] = Field(default_factory=list)
    generated_from_command_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_intent_spec(self) -> "IntentSpec":
        items = [*self.active_items, *self.superseded_items]
        identifiers = [item.intent_id for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("intent ids must be unique within a specification")
        if any(item.status != "active" for item in self.active_items):
            raise ValueError("active_items may only contain active intents")
        if any(item.status == "active" for item in self.superseded_items):
            raise ValueError("superseded_items may not contain active intents")
        known_ids = set(identifiers)
        if any(
            not set(conflict.intent_ids) <= known_ids for conflict in self.conflicts
        ):
            raise ValueError("intent conflict references an unknown intent")
        return self


def canonical_json_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stable_intent_id(
    *,
    source_ref_id: str,
    kind: IntentKind,
    target: IntentTarget,
    value: IntentValue,
) -> str:
    digest = canonical_json_hash(
        {
            "schema_version": INTENT_SPEC_VERSION,
            "source_ref_id": source_ref_id,
            "kind": kind.value,
            "target": target.value,
            "value": value.model_dump(mode="json"),
        }
    )
    return f"intent_{digest[:24]}"
