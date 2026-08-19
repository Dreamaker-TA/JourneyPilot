from __future__ import annotations

from enum import Enum
from typing import List, Literal, Optional

from pydantic import Field, model_validator

from .contract_base import StrictModel


class IntentMatchStatus(str, Enum):
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    VIOLATED = "violated"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class CandidateIntentMatch(StrictModel):
    candidate_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    status: IntentMatchStatus
    score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    method: Literal["deterministic", "semantic_batch_evaluation"]
    supporting_fact_assertion_ids: List[str] = Field(default_factory=list)
    supporting_source_record_ids: List[str] = Field(default_factory=list)
    reason_code: str = Field(min_length=1)
    public_reason: Optional[str] = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_evidence(self) -> "CandidateIntentMatch":
        if self.status in {
            IntentMatchStatus.MATCHED,
            IntentMatchStatus.NOT_MATCHED,
            IntentMatchStatus.VIOLATED,
        } and not (
            self.supporting_fact_assertion_ids or self.supporting_source_record_ids
        ):
            raise ValueError("decided intent match requires supporting evidence")
        if (
            self.status
            in {
                IntentMatchStatus.UNKNOWN,
                IntentMatchStatus.NOT_APPLICABLE,
            }
            and self.score is not None
        ):
            raise ValueError(
                "unknown or inapplicable intent match cannot carry a score"
            )
        return self
