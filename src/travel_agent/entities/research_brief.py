"""Deterministic research brief projected from the normalized request."""

from __future__ import annotations

from typing import List, Literal

from pydantic import Field, model_validator

from .delivery_bundle import ResearchDomain, StrictModel
from .trip_input import ControlledTripIdentity


class SuccessCriterion(StrictModel):
    criterion_id: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=300)
    verification: Literal["deterministic", "semantic", "mixed"]
    intent_ids: List[str] = Field(default_factory=list)


class DomainResearchObjective(StrictModel):
    objective_id: str = Field(min_length=1)
    domain: ResearchDomain
    summary: str = Field(min_length=1, max_length=500)
    must_cover_intent_ids: List[str] = Field(default_factory=list)
    optional_intent_ids: List[str] = Field(default_factory=list)
    excluded_categories: List[str] = Field(default_factory=list)
    success_criteria: List[SuccessCriterion] = Field(default_factory=list)


class ResearchBriefV2(StrictModel):
    schema_version: Literal["journeypilot.research_brief.v2"] = (
        "journeypilot.research_brief.v2"
    )
    brief_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    controlled_trip_identity_revision: int = Field(ge=0)
    intent_spec_revision: int = Field(ge=1)
    constraint_pack_revision: int = Field(ge=0)
    objective_summary: str = Field(min_length=1, max_length=500)
    controlled_trip_identity: ControlledTripIdentity
    domain_objectives: List[DomainResearchObjective] = Field(default_factory=list)
    delivery_requirements: List[str] = Field(default_factory=list)
    hard_intent_ids: List[str] = Field(default_factory=list)
    soft_intent_ids: List[str] = Field(default_factory=list)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_brief(self) -> "ResearchBriefV2":
        objective_ids = [item.objective_id for item in self.domain_objectives]
        if len(objective_ids) != len(set(objective_ids)):
            raise ValueError("research objective ids must be unique")
        hard = set(self.hard_intent_ids)
        soft = set(self.soft_intent_ids)
        if hard & soft:
            raise ValueError("an intent cannot be both hard and soft in one brief")
        referenced = {
            intent_id
            for objective in self.domain_objectives
            for intent_id in [
                *objective.must_cover_intent_ids,
                *objective.optional_intent_ids,
            ]
        }
        if not referenced <= hard | soft:
            raise ValueError("research objective references an unknown intent")
        return self
