from __future__ import annotations

from enum import Enum
from typing import List

from pydantic import Field, model_validator

from .contract_base import StrictModel


class CandidateDiscoveryOrigin(str, Enum):
    INTENT_QUERY = "intent_query"
    STRUCTURAL_QUERY = "structural_query"
    GENERIC_FALLBACK = "generic_fallback"
    TARGETED_REPAIR = "targeted_repair"
    COMPOSER_AUTHORED_FALLBACK = "composer_authored_fallback"


class CandidateDiscoveryRecord(StrictModel):
    candidate_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    query_ids: List[str] = Field(default_factory=list)
    intent_ids: List[str] = Field(default_factory=list)
    origins: List[CandidateDiscoveryOrigin] = Field(min_length=1)
    provider_audit_ids: List[str] = Field(default_factory=list)
    discovered_at_rounds: List[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_record(self) -> "CandidateDiscoveryRecord":
        for values, label in (
            (self.query_ids, "query ids"),
            (self.intent_ids, "intent ids"),
            (self.origins, "origins"),
            (self.provider_audit_ids, "provider audit ids"),
            (self.discovered_at_rounds, "research rounds"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"candidate discovery {label} must be unique")
        return self
