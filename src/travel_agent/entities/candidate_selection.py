from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, model_validator

from .contract_base import StrictModel
from .research_domain import ResearchDomain


class CandidateSelectionRole(str, Enum):
    REQUIRED_PRIMARY = "required_primary"
    PRIMARY = "primary"
    ALTERNATIVE = "alternative"
    FALLBACK = "fallback"


class CandidateSelectionEntry(StrictModel):
    candidate_id: str = Field(min_length=1)
    domain: ResearchDomain
    destination_id: str = Field(min_length=1)
    role: CandidateSelectionRole
    rank: int = Field(ge=1)
    covered_intent_ids: List[str] = Field(default_factory=list)
    selection_reasons: List[str] = Field(min_length=1)
    eligible_for_composition: bool


class SelectedCandidateCapability(StrictModel):
    candidate_id: str = Field(min_length=1)
    candidate_kind: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)
    selection_role: CandidateSelectionRole
    rank: int = Field(ge=1)
    matched_intent_ids: List[str] = Field(default_factory=list)
    unknown_intent_ids: List[str] = Field(default_factory=list)
    hard_violation_intent_ids: List[str] = Field(default_factory=list)
    selection_reasons: List[str] = Field(min_length=1)
    evidence_confidence: float = Field(ge=0.0, le=1.0)
    budget_fit: float = Field(ge=0.0, le=1.0)
    weather_fit: float = Field(ge=0.0, le=1.0)
    constraint_fit: float = Field(ge=0.0, le=1.0)
    place_id: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    longitude: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    schedule_capabilities: Dict[str, Any] = Field(default_factory=dict)


class SelectionPolicy(StrictModel):
    mode: Literal["deterministic", "explore"] = "deterministic"
    selection_seed: Optional[int] = None
    alternative_count: int = Field(default=1, ge=1, le=10)
    diversity_strength: float = Field(default=0.0, ge=0.0, le=1.0)
    avoid_previous_candidate_ids: List[str] = Field(default_factory=list)
    preferred_theme_clusters: List[str] = Field(default_factory=list)
    policy_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_mode(self) -> "SelectionPolicy":
        if self.mode == "deterministic" and self.selection_seed is not None:
            raise ValueError("deterministic selection may not carry a seed")
        if self.mode == "explore" and self.selection_seed is None:
            raise ValueError("explore selection requires a seed")
        if len(self.avoid_previous_candidate_ids) != len(
            set(self.avoid_previous_candidate_ids)
        ):
            raise ValueError("previous candidate exclusions must be unique")
        return self


class CandidateSelectionPlan(StrictModel):
    schema_version: Literal["journeypilot.candidate_selection.v2"] = (
        "journeypilot.candidate_selection.v2"
    )
    selection_plan_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    intent_spec_revision: int = Field(ge=1)
    catalog_revision: int = Field(ge=0)
    entries: List[CandidateSelectionEntry] = Field(default_factory=list)
    covered_intent_ids: List[str] = Field(default_factory=list)
    uncovered_intent_ids: List[str] = Field(default_factory=list)
    selection_policy: SelectionPolicy
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> "CandidateSelectionPlan":
        candidate_ids = [entry.candidate_id for entry in self.entries]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate selection entries must be unique")
        if set(self.covered_intent_ids) & set(self.uncovered_intent_ids):
            raise ValueError("an intent cannot be both covered and uncovered")
        if any(
            not set(entry.covered_intent_ids) <= set(self.covered_intent_ids)
            for entry in self.entries
        ):
            raise ValueError("selection entry covers an intent absent from the plan")
        if any(
            entry.eligible_for_composition
            != (entry.role is not CandidateSelectionRole.ALTERNATIVE)
            for entry in self.entries
        ):
            raise ValueError(
                "only primary selection roles may be eligible for composition"
            )
        return self

    def composition_candidate_ids(self) -> set[str]:
        return {
            entry.candidate_id
            for entry in self.entries
            if entry.eligible_for_composition
        }
