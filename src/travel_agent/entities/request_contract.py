"""The normalized request boundary shared by planning capabilities."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, model_validator

from .delivery_bundle import StrictModel
from .intent_spec import IntentSpec


REQUEST_CONTRACT_VERSION = "journeypilot.request_contract.v1"


class ClauseDisposition(str, Enum):
    MAPPED_TO_INTENT = "mapped_to_intent"
    MAPPED_TO_CONSTRAINT = "mapped_to_constraint"
    CONTROLLED_IDENTITY = "controlled_identity"
    BACKGROUND_CONTEXT = "background_context"
    NON_ACTIONABLE = "non_actionable"
    UNSUPPORTED = "unsupported"
    UNRESOLVED = "unresolved"


class AmendmentImpact(str, Enum):
    IDENTITY_CHANGE = "identity_change"
    RESEARCH_AFFECTING = "research_affecting"
    ADMISSION_AFFECTING = "admission_affecting"
    RANKING_AFFECTING = "ranking_affecting"
    COMPOSITION_AFFECTING = "composition_affecting"
    PROJECTION_ONLY = "projection_only"
    UNSUPPORTED = "unsupported"


class IntentAmendment(StrictModel):
    command_id: str = Field(min_length=1)
    category: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=2000)
    source_kind: Literal["plan_gate_amendment", "run_supplement"]
    impact: Optional[AmendmentImpact] = None


class IntentAmendmentRejection(StrictModel):
    command_id: str = Field(min_length=1)
    impact: AmendmentImpact
    reason_code: str = Field(min_length=1)
    requires_new_run: bool = False


class InputClauseRecord(StrictModel):
    clause_id: str = Field(min_length=1)
    source_ref_id: str = Field(min_length=1)
    source_text: str = Field(min_length=1, max_length=2000)
    material: bool = False
    disposition: ClauseDisposition
    mapped_intent_ids: List[str] = Field(default_factory=list)
    reason_code: Optional[str] = None

    @model_validator(mode="after")
    def validate_disposition(self) -> "InputClauseRecord":
        if self.disposition is ClauseDisposition.MAPPED_TO_INTENT:
            if not self.mapped_intent_ids:
                raise ValueError("mapped clause requires an intent id")
        elif self.mapped_intent_ids:
            raise ValueError("only mapped clauses may reference intents")
        if self.disposition in {
            ClauseDisposition.UNSUPPORTED,
            ClauseDisposition.UNRESOLVED,
        } and not self.reason_code:
            raise ValueError("unsupported or unresolved clause requires a reason code")
        if self.material and self.disposition in {
            ClauseDisposition.BACKGROUND_CONTEXT,
            ClauseDisposition.NON_ACTIONABLE,
        }:
            raise ValueError("material clauses cannot be silently classified as context")
        return self


class RequestContract(StrictModel):
    schema_version: Literal[REQUEST_CONTRACT_VERSION] = REQUEST_CONTRACT_VERSION
    request_contract_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    controlled_trip_identity_revision: int = Field(ge=0)
    constraint_pack_revision: int = Field(ge=0)
    intent_spec: IntentSpec
    constraint_pack: Dict[str, Any]
    clause_ledger: List[InputClauseRecord] = Field(default_factory=list)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_request_contract(self) -> "RequestContract":
        if self.intent_spec.generation_id != self.generation_id:
            raise ValueError("request contract and intent specification generations differ")
        clause_ids = [item.clause_id for item in self.clause_ledger]
        if len(clause_ids) != len(set(clause_ids)):
            raise ValueError("clause ids must be unique")
        known_ids = {
            item.intent_id
            for item in [
                *self.intent_spec.active_items,
                *self.intent_spec.superseded_items,
            ]
        }
        if any(not set(item.mapped_intent_ids) <= known_ids for item in self.clause_ledger):
            raise ValueError("clause ledger references an unknown intent")
        return self

    @property
    def has_blocking_conflicts(self) -> bool:
        return any(item.blocking for item in self.intent_spec.conflicts)

    @property
    def has_unresolved_material_clauses(self) -> bool:
        return any(
            item.material
            and item.disposition
            in {ClauseDisposition.UNSUPPORTED, ClauseDisposition.UNRESOLVED}
            for item in self.clause_ledger
        )
