from __future__ import annotations

from enum import Enum
from typing import Dict, List, Literal

from pydantic import Field

from .contract_base import StrictModel


class CompositionMutationType(str, Enum):
    DROP = "drop"
    MOVE = "move"
    REPLACE = "replace"
    BACKFILL = "backfill"
    REORDER = "reorder"
    TIME_ADJUST = "time_adjust"


class CompositionMutation(StrictModel):
    mutation_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    mutation_type: CompositionMutationType
    reason_code: str = Field(min_length=1)
    source_entity_ids: List[str] = Field(default_factory=list)
    target_entity_ids: List[str] = Field(default_factory=list)
    affected_intent_ids: List[str] = Field(default_factory=list)
    affected_rule_ids: List[str] = Field(default_factory=list)
    coverage_before: Dict[str, str] = Field(default_factory=dict)
    coverage_after: Dict[str, str] = Field(default_factory=dict)
    hard_rules_revalidated: bool
    created_by: Literal[
        "deterministic_pruner",
        "anchor_backfill",
        "slot_backfill",
        "composition_repair",
        "user_edit",
    ]
