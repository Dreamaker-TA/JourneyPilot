"""Revision tuple identifying one coherent planning generation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .delivery_bundle import StrictModel


class PlanningGeneration(StrictModel):
    schema_version: Literal["journeypilot.planning_generation.v1"] = (
        "journeypilot.planning_generation.v1"
    )
    generation_id: str = Field(min_length=1)
    controlled_trip_identity_revision: int = Field(ge=0)
    intent_spec_revision: int = Field(ge=1)
    constraint_pack_revision: int = Field(ge=0)
    plan_revision: int = Field(ge=0)
    identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    constraint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
