from __future__ import annotations

from typing import List

from pydantic import Field, model_validator

from .contract_base import StrictModel


class CandidateRankingScore(StrictModel):
    candidate_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    hard_eligible: bool
    hard_violation_intent_ids: List[str] = Field(default_factory=list)
    matched_intent_ids: List[str] = Field(default_factory=list)
    unknown_intent_ids: List[str] = Field(default_factory=list)
    high_priority_coverage_score: float = Field(ge=0.0, le=1.0)
    semantic_fit: float = Field(ge=0.0, le=1.0)
    evidence_confidence: float = Field(ge=0.0, le=1.0)
    budget_fit: float = Field(ge=0.0, le=1.0)
    weather_fit: float = Field(ge=0.0, le=1.0)
    constraint_fit: float = Field(ge=0.0, le=1.0)
    regional_fit: float = Field(ge=0.0, le=1.0)
    diversity_potential: float = Field(ge=0.0, le=1.0)
    generic_fallback_penalty: float = Field(ge=0.0, le=1.0)
    redundancy_penalty: float = Field(ge=0.0, le=1.0)
    travel_cost_penalty: float = Field(ge=0.0, le=1.0)
    ranking_tuple: List[float] = Field(min_length=1)
    policy_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_intent_sets(self) -> "CandidateRankingScore":
        groups = [
            self.hard_violation_intent_ids,
            self.matched_intent_ids,
            self.unknown_intent_ids,
        ]
        if any(len(values) != len(set(values)) for values in groups):
            raise ValueError("candidate ranking intent ids must be unique")
        if set(self.hard_violation_intent_ids) & set(self.matched_intent_ids):
            raise ValueError("violated intent cannot also be matched")
        return self
