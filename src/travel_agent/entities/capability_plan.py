"""Deterministic capability-plan and assignment contracts."""

from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import Field, model_validator

from .delivery_bundle import StrictModel
from .research_brief import SuccessCriterion


AgentName = Literal[
    "destination_researcher",
    "transport_researcher",
    "accommodation_researcher",
    "itinerary_planner",
]


class AgentAssignmentContract(StrictModel):
    assignment_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    agent_name: AgentName
    objective: str = Field(min_length=1, max_length=1000)
    must_cover_intent_ids: List[str] = Field(default_factory=list)
    optional_intent_ids: List[str] = Field(default_factory=list)
    research_objective_ids: List[str] = Field(default_factory=list)
    required_candidate_kinds: List[str] = Field(default_factory=list)
    excluded_categories: List[str] = Field(default_factory=list)
    research_query_ids: List[str] = Field(default_factory=list)
    success_criteria: List[SuccessCriterion] = Field(default_factory=list)
    recommended_tools: List[str] = Field(default_factory=list)
    upstream_assignment_ids: List[str] = Field(default_factory=list)
    intent_spec_revision: int = Field(ge=1)
    constraint_pack_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_assignment(self) -> "AgentAssignmentContract":
        if len(self.research_query_ids) != len(set(self.research_query_ids)):
            raise ValueError("assignment research query ids must be unique")
        return self


class CapabilityPlan(StrictModel):
    schema_version: Literal["journeypilot.capability_plan.v1"] = (
        "journeypilot.capability_plan.v1"
    )
    capability_plan_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    plan_revision: int = Field(ge=0)
    intent_spec_revision: int = Field(ge=1)
    constraint_pack_revision: int = Field(ge=0)
    execution_plan: List[List[AgentName]] = Field(min_length=1)
    assignments: Dict[str, AgentAssignmentContract] = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> "CapabilityPlan":
        flattened = [agent for group in self.execution_plan for agent in group]
        if len(flattened) != len(set(flattened)):
            raise ValueError("a capability may only appear once in an execution plan")
        if set(flattened) != set(self.assignments):
            raise ValueError("capability plan and assignments must cover the same agents")
        assignment_ids = {item.assignment_id for item in self.assignments.values()}
        if len(assignment_ids) != len(self.assignments):
            raise ValueError("assignment ids must be unique")
        for key, assignment in self.assignments.items():
            if assignment.agent_name != key:
                raise ValueError("assignment map key differs from agent name")
            if assignment.generation_id != self.generation_id:
                raise ValueError("assignment belongs to another generation")
            if not set(assignment.upstream_assignment_ids) <= assignment_ids:
                raise ValueError("assignment references an unknown dependency")
        return self
