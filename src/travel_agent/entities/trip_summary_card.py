"""Structured, consumer-facing summary for the active TripRun card."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class TripSummaryFact(BaseModel):
    label: str = Field(min_length=1, max_length=24)
    value: str = Field(min_length=1, max_length=80)
    state: Literal["confirmed", "default", "deferred"]


class TripSummaryCard(BaseModel):
    """Stable payload for the compact and expanded research-summary states."""

    headline: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=240)
    facts: list[TripSummaryFact] = Field(default_factory=list, max_length=5)
    priorities: list[str] = Field(default_factory=list, max_length=6)
    current_focus: str = Field(min_length=1, max_length=140)
    next_milestone: Optional[str] = Field(default=None, max_length=120)
    compact_line: str = Field(min_length=1, max_length=140)
    requires_user_confirmation: bool = False
