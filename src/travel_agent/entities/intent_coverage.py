from __future__ import annotations

from enum import Enum
from typing import List, Literal

from pydantic import Field, model_validator

from .contract_base import StrictModel
from .intent_spec import (
    IntentImpactStage,
    IntentKind,
    IntentSpec,
    IntentStrength,
    IntentTarget,
    IntentValue,
    VerificationMode,
    canonical_json_hash,
)


INTENT_COVERAGE_VERSION = "journeypilot.intent_coverage.v1"
INTENT_CONTRACT_SNAPSHOT_VERSION = "journeypilot.intent_contract_snapshot.v1"
INTENT_FIDELITY_POLICY_VERSION = "intent_fidelity.v1"


class IntentContractRequirement(StrictModel):
    intent_id: str = Field(min_length=1)
    kind: IntentKind
    target: IntentTarget
    strength: IntentStrength
    value: IntentValue
    verification_mode: VerificationMode
    impact_stages: List[IntentImpactStage] = Field(min_length=1)
    public_summary: str = Field(min_length=1, max_length=300)


class IntentContractSnapshot(StrictModel):
    schema_version: Literal[INTENT_CONTRACT_SNAPSHOT_VERSION] = (
        INTENT_CONTRACT_SNAPSHOT_VERSION
    )
    generation_id: str = Field(min_length=1)
    intent_spec_revision: int = Field(ge=1)
    objective_summary: str = Field(min_length=1, max_length=500)
    requirements: List[IntentContractRequirement] = Field(default_factory=list)
    conflicted_intent_ids: List[str] = Field(default_factory=list)
    conflict_summaries: List[str] = Field(default_factory=list)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_intent_spec(cls, intent_spec: IntentSpec) -> "IntentContractSnapshot":
        requirements = [
            IntentContractRequirement(
                intent_id=intent.intent_id,
                kind=intent.kind,
                target=intent.target,
                strength=intent.strength,
                value=intent.value,
                verification_mode=intent.verification_mode,
                impact_stages=intent.impact_stages,
                public_summary=intent.public_summary,
            )
            for intent in intent_spec.active_items
        ]
        material = {
            "generation_id": intent_spec.generation_id,
            "intent_spec_revision": intent_spec.revision,
            "objective_summary": intent_spec.objective_summary,
            "requirements": [item.model_dump(mode="json") for item in requirements],
            "conflict_summaries": [
                conflict.user_visible_summary for conflict in intent_spec.conflicts
            ],
            "conflicted_intent_ids": sorted(
                {
                    intent_id
                    for conflict in intent_spec.conflicts
                    for intent_id in conflict.intent_ids
                }
            ),
        }
        return cls(
            generation_id=intent_spec.generation_id,
            intent_spec_revision=intent_spec.revision,
            objective_summary=intent_spec.objective_summary,
            requirements=requirements,
            conflict_summaries=material["conflict_summaries"],
            conflicted_intent_ids=material["conflicted_intent_ids"],
            content_hash=canonical_json_hash(material),
        )

    @property
    def active_items(self) -> List[IntentContractRequirement]:
        return self.requirements

    @property
    def revision(self) -> int:
        return self.intent_spec_revision


class EntityIntentExplanation(StrictModel):
    intent_id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=160)
    explanation: str = Field(min_length=1, max_length=500)
    evidence_basis: Literal[
        "verified_fact",
        "supported_description",
        "planning_judgment",
    ]


class PublicRequirementFulfillment(StrictModel):
    requirement_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=300)
    status: Literal[
        "satisfied",
        "partially_satisfied",
        "unsatisfied",
        "unverifiable",
    ]
    explanation: str = Field(min_length=1, max_length=500)


class PublicFulfillmentSummary(StrictModel):
    fulfilled: List[PublicRequirementFulfillment] = Field(default_factory=list)
    deviations: List[PublicRequirementFulfillment] = Field(default_factory=list)


class IntentCoverageStatus(str, Enum):
    SATISFIED = "satisfied"
    PARTIALLY_SATISFIED = "partially_satisfied"
    UNSATISFIED = "unsatisfied"
    UNVERIFIABLE = "unverifiable"
    UNSUPPORTED = "unsupported"
    CONFLICTED = "conflicted"


class CoverageEntityRef(StrictModel):
    entity_type: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)


class IntentDeviation(StrictModel):
    deviation_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    status: Literal[
        "partially_satisfied",
        "unsatisfied",
        "unverifiable",
        "unsupported",
        "conflicted",
    ]
    reason_code: str = Field(min_length=1)
    public_explanation: str = Field(min_length=1, max_length=500)


class IntentCoverageItem(StrictModel):
    intent_id: str = Field(min_length=1)
    status: IntentCoverageStatus
    supporting_entity_refs: List[CoverageEntityRef] = Field(default_factory=list)
    supporting_candidate_ids: List[str] = Field(default_factory=list)
    supporting_fact_assertion_ids: List[str] = Field(default_factory=list)
    violated_entity_refs: List[CoverageEntityRef] = Field(default_factory=list)
    covered_days: List[int] = Field(default_factory=list)
    missing_days: List[int] = Field(default_factory=list)
    verification_mode: VerificationMode
    public_explanation: str = Field(min_length=1, max_length=500)
    blocking: bool


class IntentCoverageReport(StrictModel):
    schema_version: Literal[INTENT_COVERAGE_VERSION] = INTENT_COVERAGE_VERSION
    coverage_report_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    intent_spec_revision: int = Field(ge=1)
    workspace_revision: int = Field(ge=0)
    items: List[IntentCoverageItem] = Field(default_factory=list)
    hard_satisfaction_rate: float = Field(ge=0.0, le=1.0)
    soft_coverage_rate: float = Field(ge=0.0, le=1.0)
    blocking_gap_ids: List[str] = Field(default_factory=list)
    deviations: List[IntentDeviation] = Field(default_factory=list)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> "IntentCoverageReport":
        intent_ids = [item.intent_id for item in self.items]
        if len(intent_ids) != len(set(intent_ids)):
            raise ValueError("intent coverage items must be unique")
        if not set(item.intent_id for item in self.deviations) <= set(intent_ids):
            raise ValueError("intent deviation references a missing coverage item")
        return self


class IntentFidelityGap(StrictModel):
    gap_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    reason: Literal[
        "required_candidate_missing",
        "excluded_candidate_present",
        "quantity_rule_violated",
        "cadence_rule_missing",
        "time_window_violated",
        "sequence_rule_violated",
        "semantic_coverage_low",
        "output_requirement_missing",
        "intent_evidence_unavailable",
    ]
    blocking: bool
    retry_target: Literal[
        "candidate_gate",
        "candidate_selection",
        "itinerary_planner",
        "delivery_projection",
        "none",
    ]
    affected_entity_ids: List[str] = Field(default_factory=list)
    violated_rule_ids: List[str] = Field(default_factory=list)
    repair_context: dict = Field(default_factory=dict)
