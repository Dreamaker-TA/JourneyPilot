from __future__ import annotations

from enum import Enum
from typing import List, Literal, Optional

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


class CandidateSelectionPlan(StrictModel):
    schema_version: Literal["journeypilot.candidate_selection.v1"] = (
        "journeypilot.candidate_selection.v1"
    )
    selection_plan_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    intent_spec_revision: int = Field(ge=1)
    catalog_revision: int = Field(ge=0)
    entries: List[CandidateSelectionEntry] = Field(default_factory=list)
    covered_intent_ids: List[str] = Field(default_factory=list)
    uncovered_intent_ids: List[str] = Field(default_factory=list)
    policy_version: str = Field(min_length=1)
    selection_seed: Optional[int] = None
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
        return self

    def composition_candidate_ids(self) -> set[str]:
        return {
            entry.candidate_id
            for entry in self.entries
            if entry.eligible_for_composition
        }
