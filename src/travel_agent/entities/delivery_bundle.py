"""JourneyPilot v2 delivery-domain contracts.

This module is intentionally independent from the v1 workspace reducer.  It
defines the immutable facts and revision manifest that the v2 stores and
projectors will consume.  Runtime wiring moves to these contracts in later
implementation units; no compatibility reader is provided here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Dict, Iterable, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


# These strings are the only signal a reader of a stored payload has about the
# shape it is about to be parsed as, so a shape change must move them.  That did
# not happen for the v3 generation: every row that fails to parse today still
# carries ``journeypilot.delivery_bundle.v3``, fourteen distinct field-level
# breakages coexisting under one stamp.  Bump the version string whenever the
# shape below changes.
DELIVERY_BUNDLE_CONTRACT_VERSION = "journeypilot.delivery_bundle.v7"
TRIP_WORKSPACE_CONTRACT_VERSION = "journeypilot.trip_workspace.v7"
FACT_SNAPSHOT_CONTRACT_VERSION = "journeypilot.fact_store_snapshot.v4"
WEATHER_SNAPSHOT_CONTRACT_VERSION = "journeypilot.weather_context_snapshot.v2"
RESEARCH_PACKET_CONTRACT_VERSION = "journeypilot.research_packet.v4"
RECOMMENDATION_CATALOG_CONTRACT_VERSION = "journeypilot.recommendation_catalog.v5"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EntityType(str, Enum):
    VISIT_STOP = "visit_stop"
    DINING_STOP = "dining_stop"
    LODGING_STAY = "lodging_stay"
    TRANSPORT_LEG = "transport_leg"
    TRANSPORT_SEGMENT = "transport_segment"
    CUSTOM_BLOCK = "custom_block"
    WEATHER_DAY = "weather_day"


class EntityRef(StrictModel):
    entity_type: EntityType
    entity_id: str = Field(min_length=1)


class SelectionSlotRef(StrictModel):
    selection_slot_id: str = Field(min_length=1)


class TransportLegRef(StrictModel):
    transport_leg_id: str = Field(min_length=1)


class WeatherSensitivity(StrictModel):
    exposure: Literal["indoor", "outdoor", "mixed"]
    rain_sensitivity: Literal["none", "low", "high"]
    heat_sensitivity: Literal["none", "low", "high"]
    cold_sensitivity: Literal["none", "low", "high"]
    wind_sensitivity: Literal["none", "low", "high"]
    requires_clear_visibility: bool


class CandidateConstraintEvaluation(StrictModel):
    constraint_id: str = Field(min_length=1)
    status: Literal["passed", "failed", "unknown"]
    fact_assertion_ids: List[str] = Field(default_factory=list)
    reason_code: Optional[str] = None

    @model_validator(mode="after")
    def validate_evidence(self) -> "CandidateConstraintEvaluation":
        if self.status in {"passed", "failed"} and not self.fact_assertion_ids:
            raise ValueError("decided constraint evaluation requires supporting facts")
        if self.status in {"failed", "unknown"} and self.reason_code is None:
            object.__setattr__(
                self,
                "reason_code",
                "missing_evidence" if self.status == "unknown" else "hard_constraint_failed",
            )
        return self


class CandidateConstraintGateAttestation(StrictModel):
    """Server-owned proof that a Candidate Gate evaluated exact live inputs.

    A model may describe constraint evidence, but it must never claim that a
    particular run accepted those facts against the current durable constraint
    pack.  Candidate Gate mints this small, typed tuple after parsing; later
    Workspace operations verify it before they materialize a Candidate.
    """

    schema_version: Literal["candidate_constraint_gate.v1"] = "candidate_constraint_gate.v1"
    run_id: str = Field(min_length=1)
    research_packet_id: str = Field(min_length=1)
    worker_kind: Literal[
        "destination_researcher", "accommodation_researcher", "transport_researcher"
    ]
    candidate_id: str = Field(min_length=1)
    fact_data_revision: int = Field(ge=0)
    scoped_constraint_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"
    )
    candidate_facts_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"
    )
    evaluation_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"
    )


class EntityLineage(StrictModel):
    """Where one canonical itinerary entity came from.

    ``candidate_entity`` binds the entity to an admitted Research Candidate and
    its facts and sources.  ``authored_entity`` marks an entry the Itinerary
    Planner wrote itself; its place identity is resolved against the global
    place provider before projection, and it carries no candidate, fact, or
    source reference.

    ``reference_entity`` is the third: a real service a provider returned, whose
    claims are explicitly *not* confirmed for the requested date — the timetable
    edge of the 12306 pre-sale window is the case it exists for.  It names the
    packet and candidate it came from, because the service is real, but it holds
    no fact or source ids, because none of its claims were admitted as facts for
    this date.  This is the only lawful channel for that data: flipping
    ``evidence_allowed`` instead would give unconfirmed values ordinary standing
    at every evidence consumer in the repo.
    """

    lineage_kind: Literal[
        "candidate_entity", "authored_entity", "reference_entity"
    ] = "candidate_entity"
    research_packet_id: Optional[str] = None
    candidate_id: Optional[str] = None
    selection_slot_id: Optional[str] = None
    fact_assertion_ids: List[str] = Field(default_factory=list)
    source_record_ids: List[str] = Field(default_factory=list)
    planning_decision_ids: List[str] = Field(default_factory=list)
    weather_impact_ids: List[str] = Field(default_factory=list)
    personalization_influence_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lineage(self) -> "EntityLineage":
        if self.lineage_kind == "candidate_entity":
            if not self.research_packet_id or not self.candidate_id:
                raise ValueError("candidate lineage requires a packet and candidate id")
            if not self.fact_assertion_ids or not self.source_record_ids:
                raise ValueError("candidate lineage requires fact and source ids")
        elif self.lineage_kind == "reference_entity":
            if not self.research_packet_id or not self.candidate_id:
                raise ValueError("reference lineage requires a packet and candidate id")
            if self.fact_assertion_ids or self.source_record_ids:
                raise ValueError(
                    "reference lineage carries no fact or source ids: its claims are "
                    "not confirmed for the requested date"
                )
        else:
            if (
                self.research_packet_id
                or self.candidate_id
                or self.selection_slot_id
                or self.fact_assertion_ids
                or self.source_record_ids
            ):
                raise ValueError(
                    "authored lineage carries no candidate, fact, or source reference"
                )
        return self


class UserInputAnchor(StrictModel):
    """A controlled user-provided planning input, never external evidence."""

    anchor_id: str = Field(min_length=1)
    field_path: str = Field(min_length=1)
    value: Any
    input_kind: Literal[
        "controlled_identity",
        "hard_constraint",
        "preference",
        "fixed_transport",
        "planning_authorization",
    ]
    constraint_id: Optional[str] = None
    public_summary: Optional[str] = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_constraint_anchor(self) -> "UserInputAnchor":
        if self.input_kind == "hard_constraint" and not self.constraint_id:
            raise ValueError("hard-constraint user input requires a constraint id")
        if self.input_kind == "hard_constraint" and not self.public_summary:
            raise ValueError("hard-constraint user input requires a public summary")
        if self.input_kind != "hard_constraint" and self.constraint_id is not None:
            raise ValueError("only hard-constraint input may name a constraint id")
        if self.input_kind != "hard_constraint" and self.public_summary is not None:
            raise ValueError("only hard-constraint input may define a public summary")
        return self


class ResearchDomain(str, Enum):
    """The independent research surface a candidate, gap or entity belongs to."""

    VISIT = "visit"
    DINING = "dining"
    LODGING = "lodging"
    LOCAL_TRANSPORT = "local_transport"
    LONG_DISTANCE_TRANSPORT = "long_distance_transport"


class GateClass(str, Enum):
    """The single owner of a planning gap's handling semantics."""

    COMPOSITION = "composition"


class GateDisposition(str, Enum):
    """A durable next-step intent; it is not a user-facing status."""

    TARGETED_RESEARCH = "targeted_research"
    COMPOSITION_REPAIR = "composition_repair"
    # The targeted-research budget for this gap's owner is spent: the gap is a
    # settled fact the run carries forward, not a target anyone still funds.
    RESEARCH_EXHAUSTED = "research_exhausted"


class DeliveryContractViolation(RuntimeError):
    """A gate found the run in a state its own contract declares impossible.

    Deterministic and authored by the gate that raises it, so it carries the
    classification with it: the terminal record names the gate class and reason
    code instead of filing the run under the code reserved for genuinely
    unexpected failures.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        gate_class: GateClass,
    ) -> None:
        self.reason_code = reason_code
        self.gate_class = gate_class
        super().__init__(message)


class GateFailureAttribution(StrictModel):
    """A checkpoint-safe classification for one deterministic gate outcome.

    The record deliberately stores only a reason code, scoped gap ids, and a
    normalized failure signature. It never stores a provider payload or turns
    an unavailable external fact into a constraint conflict.
    """

    attribution_id: str = Field(min_length=1)
    # Completion-guarantee Runs bind every classified outcome to the sealed
    # Minimum Delivery Draft.  ``None`` remains valid for legacy/direct-node
    # diagnostics that do not participate in the completion lifecycle.
    draft_id: Optional[str] = Field(default=None, min_length=1)
    gate_class: GateClass
    disposition: GateDisposition
    reason_code: str = Field(min_length=1)
    research_domain: Optional[ResearchDomain] = None
    gap_ids: List[str] = Field(default_factory=list)
    failure_signature: Optional[str] = None
    deterministic: bool = False
    retry_attempt: int = Field(default=0, ge=0)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_gate_attribution(self) -> "GateFailureAttribution":
        if self.recorded_at.tzinfo is None:
            raise ValueError("gate failure attribution timestamps must be timezone-aware")
        if len(self.gap_ids) != len(set(self.gap_ids)):
            raise ValueError("gate failure attribution gap ids must be unique")
        return self


class MinimumDeliveryDayShell(StrictModel):
    """One deterministic, user-input-only day structure retained before research."""

    day_id: str = Field(min_length=1)
    day: int = Field(ge=1)
    date: Date
    destination_id: str = Field(min_length=1)
    # These are controlled-input planning boundaries, not researched transport
    # or accommodation facts.  They let the minimum delivery draft preserve a
    # usable overnight shape and an inter-city hand-off before any provider is
    # called.
    lodging_night: bool = False
    arrival_from_destination_id: Optional[str] = None
    time_structure: List[Literal["morning", "lunch", "afternoon", "evening"]] = Field(
        default_factory=lambda: ["morning", "lunch", "afternoon", "evening"]
    )

    @model_validator(mode="after")
    def validate_time_structure(self) -> "MinimumDeliveryDayShell":
        expected = ["morning", "lunch", "afternoon", "evening"]
        if self.time_structure != expected:
            raise ValueError("minimum delivery day shell must retain the four canonical time blocks")
        return self


class MinimumDeliveryDraft(StrictModel):
    """Durable internal seed for the one eventual DeliveryBundle, never a Bundle itself."""

    draft_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    controlled_trip_identity_revision: int = Field(ge=0)
    constraint_pack_revision: int = Field(ge=0)
    plan_revision: int = Field(ge=0)
    policy_version: str = Field(min_length=1)
    day_shells: List[MinimumDeliveryDayShell] = Field(min_length=1)
    preserved_constraint_ids: List[str] = Field(default_factory=list)
    user_input_anchors: List[UserInputAnchor] = Field(default_factory=list)
    planning_authorized: bool = False
    planning_authorized_at: Optional[datetime] = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_draft(self) -> "MinimumDeliveryDraft":
        if [item.day for item in self.day_shells] != list(
            range(1, len(self.day_shells) + 1)
        ):
            raise ValueError("minimum delivery draft day shells must be contiguous from one")
        dates = [item.date for item in self.day_shells]
        if dates != [dates[0] + timedelta(days=index) for index in range(len(dates))]:
            raise ValueError("minimum delivery draft day dates must be contiguous")
        ids = [item.day_id for item in self.day_shells]
        if len(ids) != len(set(ids)):
            raise ValueError("minimum delivery draft day ids must be unique")
        expected_lodging_nights = [index < len(self.day_shells) - 1 for index in range(len(self.day_shells))]
        if [item.lodging_night for item in self.day_shells] != expected_lodging_nights:
            raise ValueError("minimum delivery draft must retain every required lodging night")
        for index, item in enumerate(self.day_shells):
            if index == 0 and item.arrival_from_destination_id is not None:
                raise ValueError("first minimum delivery day cannot have an inter-city arrival")
            if index > 0:
                previous = self.day_shells[index - 1]
                changed_destination = previous.destination_id != item.destination_id
                if changed_destination != (item.arrival_from_destination_id is not None):
                    raise ValueError("minimum delivery draft must retain each inter-city boundary")
                if item.arrival_from_destination_id not in (None, previous.destination_id):
                    raise ValueError("minimum delivery inter-city boundary must reference the prior destination")
        if len(self.preserved_constraint_ids) != len(set(self.preserved_constraint_ids)):
            raise ValueError("minimum delivery draft constraints must be unique")
        anchor_ids = [item.anchor_id for item in self.user_input_anchors]
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("minimum delivery draft user-input anchors must be unique")
        hard_constraint_ids = {
            item.constraint_id
            for item in self.user_input_anchors
            if item.input_kind == "hard_constraint" and item.constraint_id is not None
        }
        if not set(self.preserved_constraint_ids) <= hard_constraint_ids:
            raise ValueError("minimum delivery draft preserves an unanchored hard constraint")
        if self.planning_authorized != (self.planning_authorized_at is not None):
            raise ValueError("draft authorization timestamp must match authorization state")
        return self


class RunDeadlineSnapshot(StrictModel):
    """Durable wall-clock audit points; live monotonic anchors are rebuilt per process.

    Window lengths are embedded on the snapshot so validation never consults the
    current process environment.  Builders stamp the seconds that were active
    when planning was authorized.
    """

    draft_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    planning_authorized_at: datetime
    target_at: datetime
    closeout_at: datetime
    composition_at: datetime
    delivery_deadline_at: datetime
    target_seconds: int = Field(ge=1)
    closeout_seconds: int = Field(ge=1)
    composition_seconds: int = Field(ge=1)
    delivery_deadline_seconds: int = Field(ge=1)
    checkpointed_elapsed_seconds: float = Field(ge=0)
    last_observed_at: datetime

    @model_validator(mode="after")
    def validate_deadlines(self) -> "RunDeadlineSnapshot":
        if any(
            value.tzinfo is None
            for value in (
                self.planning_authorized_at,
                self.target_at,
                self.closeout_at,
                self.composition_at,
                self.delivery_deadline_at,
                self.last_observed_at,
            )
        ):
            raise ValueError("run deadlines must use timezone-aware timestamps")
        if not (
            self.planning_authorized_at
            < self.target_at
            < self.closeout_at
            < self.composition_at
            < self.delivery_deadline_at
        ):
            raise ValueError("run deadlines must be strictly ordered")
        if not (
            self.target_seconds
            < self.closeout_seconds
            < self.composition_seconds
            < self.delivery_deadline_seconds
        ):
            raise ValueError("run deadline windows must be strictly increasing")
        if (self.target_at - self.planning_authorized_at).total_seconds() != self.target_seconds:
            raise ValueError("run target must match the snapshot target window")
        if (self.closeout_at - self.planning_authorized_at).total_seconds() != self.closeout_seconds:
            raise ValueError("run closeout must match the snapshot closeout window")
        if (
            self.composition_at - self.planning_authorized_at
        ).total_seconds() != self.composition_seconds:
            raise ValueError("run composition must match the snapshot composition window")
        if (
            self.delivery_deadline_at - self.planning_authorized_at
        ).total_seconds() != self.delivery_deadline_seconds:
            raise ValueError("run delivery deadline must match the snapshot delivery window")
        if self.last_observed_at < self.planning_authorized_at:
            raise ValueError("deadline observation predates planning authorization")
        return self


# Audit closure for a Draft — intentionally includes awaiting_input, which is
# NOT a TripRun terminal status (see trip_run.TERMINAL_TRIP_RUN_STATUSES).
AuditClosureStatus = Literal["completed", "cancelled", "failed"]


class TerminalAttribution(StrictModel):
    """Durable audit closure for one Draft (not the TripRun lifecycle terminal set)."""

    draft_id: str = Field(min_length=1)
    closure_status: AuditClosureStatus
    reason_code: str = Field(min_length=1)
    recorded_at: datetime
    delivery_bundle_id: Optional[str] = None
    gate_class: Optional[GateClass] = None


class ProviderSnapshotProvenance(StrictModel):
    """The evidence snapshot origin, never a cached Candidate or ToolEnvelope.

    ``origin`` and ``data_environment`` answer two different questions and were
    conflated while only the first existed: ``origin="live"`` means "not served
    from our cache", which under a supplier's test key does not mean the numbers
    are real.  A sandbox flight and a bookable one were indistinguishable on the
    exported PDF, and the audit metric that counted live retrievals counted
    sandbox responses among them.
    """

    origin: Literal["live", "provider_snapshot_cache"]
    # Which of the supplier's environments answered.  Required and undefaulted:
    # a default would decide, silently, that unlabelled evidence is production.
    data_environment: Literal["production", "sandbox"]
    provider_name: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    cache_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    retrieved_at: datetime
    provider_valid_until: Optional[datetime] = None
    cache_valid_until: datetime
    provider_contract_version: str = Field(min_length=1)
    payload_schema_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cache_provenance(self) -> "ProviderSnapshotProvenance":
        if self.cache_valid_until <= self.retrieved_at:
            raise ValueError("provider snapshot cache validity must follow retrieval")
        if (
            self.provider_valid_until is not None
            and self.provider_valid_until < self.observed_at
        ):
            raise ValueError("provider validity cannot predate observation")
        return self


class TransportEndpoint(StrictModel):
    name: str = Field(min_length=1)
    place_id: Optional[str] = None
    station_code: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)


class TransportMode(str, Enum):
    FLIGHT = "flight"
    HIGH_SPEED_RAIL = "high_speed_rail"
    TRAIN = "train"
    COACH = "coach"
    FERRY = "ferry"
    METRO = "metro"
    BUS = "bus"
    TRAM = "tram"
    TAXI = "taxi"
    RIDE_HAILING = "ride_hailing"
    DRIVE = "drive"
    BIKE = "bike"
    WALK = "walk"
    OTHER = "other"


class TransportSegment(StrictModel):
    segment_id: str = Field(min_length=1)
    mode: TransportMode
    from_endpoint: TransportEndpoint
    to_endpoint: TransportEndpoint
    departure_at: Optional[datetime] = None
    arrival_at: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(default=None, ge=0)
    distance_meters: Optional[int] = Field(default=None, ge=0)
    operator_name: Optional[str] = None
    service_number: Optional[str] = None
    line_name: Optional[str] = None
    cost_cny: Optional[float] = Field(default=None, ge=0)


class TransportModePreference(StrictModel):
    locked_mode: Optional[TransportMode] = None
    excluded_modes: List[TransportMode] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lock(self) -> "TransportModePreference":
        if self.locked_mode is not None and self.locked_mode in self.excluded_modes:
            raise ValueError("locked transport mode cannot also be excluded")
        return self


class TransportLeg(StrictModel):
    type: Literal["transport_leg"] = "transport_leg"
    transport_leg_id: str = Field(min_length=1)
    transport_class: Literal["long_distance", "public_transit", "flexible"]
    selected_mode: TransportMode
    from_endpoint: TransportEndpoint
    to_endpoint: TransportEndpoint
    departure_at: Optional[datetime] = None
    arrival_at: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(default=None, ge=0)
    distance_meters: Optional[int] = Field(default=None, ge=0)
    total_cost_cny: Optional[float] = Field(default=None, ge=0)
    transfer_count: int = Field(ge=0)
    segments: List[TransportSegment] = Field(default_factory=list)
    booking_status: Literal["not_required", "recommended", "required", "booked", "unknown"]
    route_status: Literal["pending", "ready", "unavailable"]
    mode_preference: TransportModePreference = Field(default_factory=TransportModePreference)
    lineage: EntityLineage

    @model_validator(mode="after")
    def validate_route(self) -> "TransportLeg":
        expected = max(len(self.segments) - 1, 0)
        if self.transfer_count != expected:
            raise ValueError(f"transfer_count must equal {expected}")
        if self.route_status == "ready" and not self.segments:
            raise ValueError("ready transport leg requires at least one segment")
        if self.segments:
            first = self.segments[0]
            last = self.segments[-1]
            if first.from_endpoint != self.from_endpoint or last.to_endpoint != self.to_endpoint:
                raise ValueError("transport segment endpoints must match leg endpoints")
        return self


class ScheduledStopBase(StrictModel):
    item_id: str = Field(min_length=1)
    day_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: Optional[str] = None
    # Coordinates carried by an authored entry, resolved against the global
    # place provider.  A candidate-backed stop leaves them unset and the map
    # projection reads its coordinates from verified facts instead.
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    duration_minutes: int = Field(ge=1)
    estimated_cost_cny: Optional[float] = Field(default=None, ge=0)
    selection_reason: str = Field(min_length=1)
    lineage: EntityLineage


class VisitStop(ScheduledStopBase):
    type: Literal["visit_stop"] = "visit_stop"
    visit_type: Literal["attraction", "experience", "culture", "shopping", "nature", "other"]
    opening_window: Optional[str] = None
    reservation_required: Optional[bool] = None
    visit_highlights: List[str] = Field(default_factory=list)


class DiningStop(ScheduledStopBase):
    type: Literal["dining_stop"] = "dining_stop"
    meal_type: Literal["breakfast", "lunch", "dinner", "snack", "other"]
    cuisine_types: List[str] = Field(default_factory=list)
    average_spend_cny: Optional[float] = Field(default=None, ge=0)
    recommended_dishes: List[str] = Field(default_factory=list)
    reservation_required: Optional[bool] = None
    opening_window: Optional[str] = None
    dining_reminders: List[str] = Field(default_factory=list)


class LodgingStay(StrictModel):
    type: Literal["lodging_stay"] = "lodging_stay"
    stay_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    check_in_date: Date
    check_out_date: Date
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    nights: int = Field(ge=1)
    room_type: Optional[str] = None
    nightly_price_cny: Optional[float] = Field(default=None, ge=0)
    total_price_cny: Optional[float] = Field(default=None, ge=0)
    # A reference estimate is useful for planning but must never be rendered
    # as a current quote.  Live price/inventory retrieval is opt-in evidence,
    # not a prerequisite for a usable accommodation recommendation.
    price_kind: Literal["reference_estimate", "live_quote"] = "reference_estimate"
    availability_status: Literal["confirmed", "needs_confirmation", "unavailable"] = (
        "needs_confirmation"
    )
    address: str = Field(min_length=1)
    selection_reason: str = Field(min_length=1)
    lineage: EntityLineage

    @model_validator(mode="after")
    def validate_nights(self) -> "LodgingStay":
        expected = (self.check_out_date - self.check_in_date).days
        if expected < 1 or self.nights != expected:
            raise ValueError("nights must equal check-out date minus check-in date")
        if (
            self.price_kind == "reference_estimate"
            and self.availability_status == "confirmed"
        ):
            raise ValueError(
                "reference_estimate lodging cannot be availability confirmed; "
                "use needs_confirmation or a live_quote"
            )
        return self


class CustomBlock(StrictModel):
    type: Literal["custom_block"] = "custom_block"
    item_id: str = Field(min_length=1)
    day_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    note: Optional[str] = None
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(default=None, ge=1)


class TimelineEntryRef(StrictModel):
    entry_id: str = Field(min_length=1)
    entity_type: EntityType
    entity_id: str = Field(min_length=1)
    projection_role: Literal["full", "departure", "arrival", "check_in", "check_out"] = "full"


TransportProjectionShape = Literal[
    "single_full",
    "cross_night_split",
    "cross_night_boundary_full",
    "outside_itinerary",
]


def classify_transport_projection_shape(
    *,
    transport_class: str,
    departure_at: Optional[datetime],
    arrival_at: Optional[datetime],
    itinerary_dates: Iterable[Date],
) -> TransportProjectionShape:
    """Classify one transport entity against the user-planned Day boundary.

    Cross-night transport wholly inside the itinerary is projected on both
    service dates.  If exactly one service date is a planned Day, the complete
    entity is projected once on that boundary Day; the other service date does
    not become a synthetic itinerary Day.
    """

    if (
        transport_class != "long_distance"
        or departure_at is None
        or arrival_at is None
        or departure_at.date() == arrival_at.date()
    ):
        return "single_full"
    dates = set(itinerary_dates)
    matched_dates = {departure_at.date(), arrival_at.date()} & dates
    if len(matched_dates) == 2:
        return "cross_night_split"
    if len(matched_dates) == 1:
        return "cross_night_boundary_full"
    return "outside_itinerary"


class DayPlanV2(StrictModel):
    day_id: str = Field(min_length=1)
    day: int = Field(ge=1)
    date: Date
    destination_id: str = Field(min_length=1)
    # Derived from this Day's actual placements by ``entities/day_theme.py``, **not
    # written by a model**.  Free text an LLM writes once is carried forward unchanged
    # through every later edit and never re-checked against the Day it describes —
    # that is how a Day with no canal entry ends up titled 「运河遗韵与返程」.
    # ``min_length`` closes the other half: an empty theme would otherwise reach the
    # workspace card, the report and the PDF unremarked.
    theme: str = Field(min_length=1)
    timeline: List[TimelineEntryRef]
    time_structure: List[Literal["morning", "lunch", "afternoon", "evening"]] = Field(
        default_factory=lambda: ["morning", "lunch", "afternoon", "evening"]
    )
    estimated_cost_cny: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_time_structure(self) -> "DayPlanV2":
        if self.time_structure != ["morning", "lunch", "afternoon", "evening"]:
            raise ValueError("day plan must retain the four canonical time blocks")
        return self


def coverage_from_component_counts(
    *,
    priced_component_count: int,
    budget_relevant_component_count: int,
) -> Literal["none", "partial", "complete"]:
    """How much of the trip's money the plan actually knows.

    One spelling of the rule, read twice: ``build_cost_coverage_summary`` derives
    the value and ``CostCoverageSummary.validate_coverage`` refuses a hand-built
    summary that disagrees with its own counts.  The two used to spell the same
    three-way conditional separately — they agreed, which is exactly why a drift
    between them would have been silent.
    """

    if priced_component_count == 0:
        return "none"
    if priced_component_count == budget_relevant_component_count:
        return "complete"
    return "partial"


def itinerary_price_components(
    *,
    visit_stops: Iterable["VisitStop"],
    dining_stops: Iterable["DiningStop"],
    lodging_stays: Iterable["LodgingStay"],
    transport_legs: Iterable["TransportLeg"],
) -> List[Optional[float]]:
    """Every price the trip is allowed to count, in the one order that counts it.

    Which entities are budget-relevant, and which field on each holds the money,
    is decided here and nowhere else.  Three call sites rebuild a cost summary —
    the itinerary's own validator, materialization
    (``entities/itinerary_composition_v2``) and every workspace mutation
    (``entities/workspace_v2_mutations``) — and each used to carry its own copy of
    this four-line list.  ``StructuredItineraryV2.validate_references`` would have
    caught a divergence loudly, but three copies of a rule is three places to
    forget a new priced entity in.
    """

    return [
        *(item.estimated_cost_cny for item in visit_stops),
        *(item.estimated_cost_cny for item in dining_stops),
        *(item.total_price_cny for item in lodging_stays),
        *(item.total_cost_cny for item in transport_legs),
    ]


class CostCoverageSummary(StrictModel):
    known_subtotal_cny: Optional[float] = Field(default=None, ge=0)
    estimated_total_cny: Optional[float] = Field(default=None, ge=0)
    priced_component_count: int = Field(ge=0)
    budget_relevant_component_count: int = Field(ge=0)
    coverage: Literal["none", "partial", "complete"]
    budget_cap_cny: Optional[float] = Field(default=None, ge=0)
    budget_status: Literal["unknown", "within_cap", "over_cap"]
    # What the whole trip is likely to cost, written by one fast-tier call over
    # the itinerary (``workflows/budget_estimate.py``).  A different kind of
    # number from every other field here, which is why it is its own:
    # ``known_subtotal_cny`` and ``estimated_total_cny`` add up prices suppliers
    # actually returned, so they may only ever move when a price does.  This one
    # is a guess about the parts nobody quoted, and mixing it into either of them
    # would make a supplier total that no supplier stands behind.  It takes no
    # part in ``coverage`` or ``budget_status`` for the same reason.
    llm_estimated_total_cny: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_coverage(self) -> "CostCoverageSummary":
        if self.priced_component_count > self.budget_relevant_component_count:
            raise ValueError("priced component count cannot exceed budget-relevant count")
        expected_coverage = coverage_from_component_counts(
            priced_component_count=self.priced_component_count,
            budget_relevant_component_count=self.budget_relevant_component_count,
        )
        if self.coverage != expected_coverage:
            raise ValueError("cost coverage does not match component counts")
        if self.coverage == "none" and self.known_subtotal_cny is not None:
            raise ValueError("cost coverage none cannot publish a known subtotal")
        if self.coverage != "none" and self.known_subtotal_cny is None:
            raise ValueError("priced components require a known subtotal")
        if self.coverage == "complete":
            if self.estimated_total_cny != self.known_subtotal_cny:
                raise ValueError("complete cost coverage requires an exact estimated total")
        elif self.estimated_total_cny is not None:
            raise ValueError("incomplete cost coverage cannot publish an estimated total")
        expected_budget_status = "unknown"
        if (
            self.budget_cap_cny is not None
            and self.known_subtotal_cny is not None
            and self.known_subtotal_cny > self.budget_cap_cny
        ):
            expected_budget_status = "over_cap"
        elif self.budget_cap_cny is not None and self.coverage == "complete":
            expected_budget_status = "within_cap"
        if self.budget_status != expected_budget_status:
            raise ValueError("budget status does not match cost coverage and cap")
        return self

    @classmethod
    def empty(cls) -> "CostCoverageSummary":
        return cls(
            priced_component_count=0,
            budget_relevant_component_count=0,
            coverage="none",
            budget_status="unknown",
        )


def build_cost_coverage_summary(
    costs: Iterable[Optional[float]],
    *,
    budget_cap_cny: Optional[float] = None,
    llm_estimated_total_cny: Optional[float] = None,
) -> CostCoverageSummary:
    """Total the prices the itinerary holds, and carry the two inputs it cannot derive.

    ``budget_cap_cny`` comes from the traveller and ``llm_estimated_total_cny``
    from the budget estimator; neither is a function of the component prices, so
    both are passed through rather than recomputed.  Every caller that rebuilds a
    summary must forward them, because
    ``StructuredItineraryV2.validate_references`` rebuilds one and compares — a
    caller that dropped either would silently reset it and then fail that check.
    """

    components = list(costs)
    known = [float(cost) for cost in components if cost is not None]
    coverage = coverage_from_component_counts(
        priced_component_count=len(known),
        budget_relevant_component_count=len(components),
    )
    subtotal = sum(known) if known else None
    budget_status = (
        "over_cap"
        if budget_cap_cny is not None and subtotal is not None and subtotal > budget_cap_cny
        else "within_cap"
        if budget_cap_cny is not None and coverage == "complete"
        else "unknown"
    )
    return CostCoverageSummary(
        known_subtotal_cny=subtotal,
        estimated_total_cny=subtotal if coverage == "complete" else None,
        priced_component_count=len(known),
        budget_relevant_component_count=len(components),
        coverage=coverage,
        budget_cap_cny=budget_cap_cny,
        budget_status=budget_status,
        llm_estimated_total_cny=llm_estimated_total_cny,
    )


class StructuredItineraryV2(StrictModel):
    itinerary_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    destination_ids: List[str] = Field(min_length=1)
    duration_days: int = Field(ge=1)
    day_plans: List[DayPlanV2] = Field(min_length=1)
    visit_stops: List[VisitStop] = Field(default_factory=list)
    dining_stops: List[DiningStop] = Field(default_factory=list)
    lodging_stays: List[LodgingStay] = Field(default_factory=list)
    transport_legs: List[TransportLeg] = Field(default_factory=list)
    custom_blocks: List[CustomBlock] = Field(default_factory=list)
    cost_summary: CostCoverageSummary = Field(default_factory=CostCoverageSummary.empty)
    highlights: List[str] = Field(default_factory=list)
    important_notes: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "StructuredItineraryV2":
        expected_cost_summary = build_cost_coverage_summary(
            itinerary_price_components(
                visit_stops=self.visit_stops,
                dining_stops=self.dining_stops,
                lodging_stays=self.lodging_stays,
                transport_legs=self.transport_legs,
            ),
            budget_cap_cny=self.cost_summary.budget_cap_cny,
            llm_estimated_total_cny=self.cost_summary.llm_estimated_total_cny,
        )
        if "cost_summary" not in self.model_fields_set:
            object.__setattr__(self, "cost_summary", expected_cost_summary)
        elif self.cost_summary != expected_cost_summary:
            raise ValueError("cost summary must match itinerary price coverage")
        for label, identifiers in (
            ("visit stop", [item.item_id for item in self.visit_stops]),
            ("dining stop", [item.item_id for item in self.dining_stops]),
            ("lodging stay", [item.stay_id for item in self.lodging_stays]),
            ("transport leg", [item.transport_leg_id for item in self.transport_legs]),
            ("custom block", [item.item_id for item in self.custom_blocks]),
        ):
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{label} ids must be unique")
        entities: Dict[EntityType, set[str]] = {
            EntityType.VISIT_STOP: {item.item_id for item in self.visit_stops},
            EntityType.DINING_STOP: {item.item_id for item in self.dining_stops},
            EntityType.LODGING_STAY: {item.stay_id for item in self.lodging_stays},
            EntityType.TRANSPORT_LEG: {item.transport_leg_id for item in self.transport_legs},
            EntityType.CUSTOM_BLOCK: {item.item_id for item in self.custom_blocks},
            EntityType.TRANSPORT_SEGMENT: set(),
        }
        seen: set[tuple[str, EntityType, str, str]] = set()
        transport_projections: Dict[
            str, List[tuple[Optional[Date], str]]
        ] = {}
        for day in self.day_plans:
            for ref in day.timeline:
                if ref.entity_type == EntityType.TRANSPORT_SEGMENT:
                    raise ValueError("transport segments cannot be timeline roots")
                if ref.entity_id not in entities[ref.entity_type]:
                    raise ValueError(f"dangling timeline reference: {ref.entity_type}/{ref.entity_id}")
                key = (day.day_id, ref.entity_type, ref.entity_id, ref.projection_role)
                if key in seen:
                    raise ValueError("duplicate entity projection in day timeline")
                seen.add(key)
                if ref.entity_type == EntityType.TRANSPORT_LEG:
                    if ref.projection_role not in {"full", "departure", "arrival"}:
                        raise ValueError("transport timeline has an invalid projection role")
                    transport_projections.setdefault(ref.entity_id, []).append(
                        (day.date, ref.projection_role)
                    )
                elif ref.entity_type == EntityType.LODGING_STAY:
                    if ref.projection_role not in {"check_in", "check_out"}:
                        raise ValueError("lodging timeline has an invalid projection role")
                elif ref.projection_role != "full":
                    raise ValueError("ordinary timeline entities require the full projection role")
        for leg in self.transport_legs:
            projections = transport_projections.get(leg.transport_leg_id, [])
            departure_date = leg.departure_at.date() if leg.departure_at else None
            arrival_date = leg.arrival_at.date() if leg.arrival_at else None
            projection_shape = classify_transport_projection_shape(
                transport_class=leg.transport_class,
                departure_at=leg.departure_at,
                arrival_at=leg.arrival_at,
                itinerary_dates=(day.date for day in self.day_plans),
            )
            if projection_shape == "cross_night_split":
                expected_projections = {
                    (departure_date, "departure"),
                    (arrival_date, "arrival"),
                }
                if len(projections) != 2 or set(projections) != expected_projections:
                    raise ValueError(
                        "cross-night long-distance transport requires departure and arrival Day projections"
                    )
            elif projection_shape == "cross_night_boundary_full":
                itinerary_dates = {day.date for day in self.day_plans}
                boundary_date = next(
                    item
                    for item in (departure_date, arrival_date)
                    if item in itinerary_dates
                )
                if projections != [(boundary_date, "full")]:
                    raise ValueError(
                        "boundary-crossing long-distance transport requires one full projection on its itinerary Day"
                    )
            elif projection_shape == "outside_itinerary":
                raise ValueError(
                    "long-distance transport service dates fall outside the itinerary"
                )
            elif len(projections) != 1 or projections[0][1] != "full":
                raise ValueError(
                    "same-day or local transport requires exactly one full Day projection"
                )
        day_dates = {day.day_id: day.date for day in self.day_plans}
        if len(day_dates) != len(self.day_plans):
            raise ValueError("day plan ids must be unique")
        if self.duration_days != len(self.day_plans):
            raise ValueError("duration_days must equal number of day plans")
        return self


class CandidateBase(StrictModel):
    candidate_id: str = Field(min_length=1)
    research_packet_id: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)
    fact_assertion_ids: List[str] = Field(min_length=1)
    source_record_ids: List[str] = Field(min_length=1)
    field_paths: List[str] = Field(min_length=1)
    active_constraint_ids: List[str] = Field(default_factory=list)
    constraint_evaluations: List[CandidateConstraintEvaluation] = Field(default_factory=list)
    # This is populated exclusively by Candidate Gate after the model output
    # has been bound to authoritative packet metadata and constraints.
    constraint_gate_attestation: Optional[CandidateConstraintGateAttestation] = None
    weather_sensitivity: WeatherSensitivity
    selection_reasons: List[str] = Field(min_length=2, max_length=3)
    tradeoff: str = Field(min_length=1)
    planning_decision_ids: List[str] = Field(default_factory=list)
    weather_impact_ids: List[str] = Field(default_factory=list)
    personalization_influence_ids: List[str] = Field(default_factory=list)
    freshness_status: Literal["current", "refreshing", "stale"]
    observed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_candidate_gate_inputs(self) -> "CandidateBase":
        if len(self.active_constraint_ids) != len(set(self.active_constraint_ids)):
            raise ValueError("active candidate constraint ids must be unique")
        evaluation_ids = [item.constraint_id for item in self.constraint_evaluations]
        if len(evaluation_ids) != len(set(evaluation_ids)):
            raise ValueError("candidate constraint evaluations must be unique")
        if not set(evaluation_ids) <= set(self.active_constraint_ids):
            raise ValueError("candidate cannot evaluate an inactive hard constraint")
        if any(
            not set(item.fact_assertion_ids) <= set(self.fact_assertion_ids)
            for item in self.constraint_evaluations
        ):
            raise ValueError("constraint evaluation facts must belong to the candidate")
        return self


class VisitCandidate(CandidateBase):
    candidate_kind: Literal["visit"] = "visit"
    place_id: str = Field(min_length=1)
    provider_place_type: str = Field(min_length=1)
    provider_country_code: str = Field(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    visit_type: Literal["attraction", "experience", "culture", "shopping", "nature", "other"]
    recommended_duration_minutes: int = Field(ge=1)
    estimated_cost_cny: Optional[float] = Field(default=None, ge=0)
    opening_window: Optional[str] = None
    reservation_required: Optional[bool] = None
    # Descriptive claims about the place, not part of the Provider-bound identity.
    # They are empty when nothing this round supports them: a mandatory non-empty
    # list here would leave the model no move except to invent one, which is the
    # move the packet contract forbids.  See ``_strip_unevidenced_descriptions``.
    highlights: List[str] = Field(default_factory=list)


class DiningCandidate(CandidateBase):
    candidate_kind: Literal["dining"] = "dining"
    place_id: str = Field(min_length=1)
    provider_place_type: str = Field(min_length=1)
    provider_country_code: str = Field(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    branch_name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    meal_types: List[Literal["breakfast", "lunch", "dinner", "snack", "other"]] = Field(
        min_length=1
    )
    # Same reading as ``VisitCandidate.highlights``: descriptive claims about the
    # branch, empty when unsupported.  ``opening_window`` matches its Visit twin —
    # it was the one mandatory member of this family, and a mandatory opening time
    # for a branch no Provider published hours for is a fabricated opening time.
    cuisine_types: List[str] = Field(default_factory=list)
    average_spend_cny: Optional[float] = Field(default=None, ge=0)
    recommended_dishes: List[str] = Field(default_factory=list)
    opening_window: Optional[str] = None
    reservation_required: Optional[bool] = None
    availability_status: Literal["confirmed", "needs_confirmation", "unavailable"]


class LodgingCandidate(CandidateBase):
    candidate_kind: Literal["lodging"] = "lodging"
    place_id: str = Field(min_length=1)
    provider_place_type: str = Field(min_length=1)
    provider_country_code: str = Field(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    property_name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    check_in_date: Date
    check_out_date: Date
    nights: int = Field(ge=1)
    room_type: Optional[str] = None
    nightly_price_cny: Optional[float] = Field(default=None, ge=0)
    total_price_cny: Optional[float] = Field(default=None, ge=0)
    price_kind: Literal["reference_estimate", "live_quote"] = "reference_estimate"
    facilities: List[str] = Field(default_factory=list)
    anchor_travel_minutes: Dict[str, int] = Field(default_factory=dict)
    availability_status: Literal["confirmed", "needs_confirmation", "unavailable"]

    @model_validator(mode="after")
    def validate_stay(self) -> "LodgingCandidate":
        expected = (self.check_out_date - self.check_in_date).days
        if expected < 1 or self.nights != expected:
            raise ValueError("candidate nights must equal check-out date minus check-in date")
        if (
            self.price_kind == "reference_estimate"
            and self.availability_status == "confirmed"
        ):
            raise ValueError(
                "reference_estimate lodging cannot be availability confirmed; "
                "use needs_confirmation or a live_quote"
            )
        return self


class TransportCandidate(CandidateBase):
    candidate_kind: Literal["transport"] = "transport"
    route_id: str = Field(min_length=1)
    transport_class: Literal["long_distance", "public_transit", "flexible"]
    provider_evidence_scope_id: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    selected_mode: TransportMode
    from_endpoint: TransportEndpoint
    to_endpoint: TransportEndpoint
    departure_at: Optional[datetime] = None
    arrival_at: Optional[datetime] = None
    duration_minutes: int = Field(ge=0)
    distance_meters: Optional[int] = Field(default=None, ge=0)
    total_cost_cny: Optional[float] = Field(default=None, ge=0)
    segments: List[TransportSegment] = Field(min_length=1)
    booking_status: Literal["not_required", "recommended", "required", "booked", "unknown"]

    @model_validator(mode="after")
    def validate_segments(self) -> "TransportCandidate":
        if (
            self.transport_class == "long_distance"
            and self.provider_evidence_scope_id is None
        ):
            raise ValueError(
                "long-distance candidate requires exact Provider evidence scope"
            )
        if (
            self.transport_class != "long_distance"
            and self.provider_evidence_scope_id is not None
        ):
            raise ValueError(
                "local transport candidate cannot carry long-distance scope"
            )
        if self.segments[0].from_endpoint != self.from_endpoint:
            raise ValueError("transport candidate first segment must match origin")
        if self.segments[-1].to_endpoint != self.to_endpoint:
            raise ValueError("transport candidate last segment must match destination")
        return self


ResearchCandidate = Annotated[
    Union[VisitCandidate, DiningCandidate, LodgingCandidate, TransportCandidate],
    Field(discriminator="candidate_kind"),
]


class CandidateFitScores(StrictModel):
    """Ranking signals admission attaches to every candidate.

    Each score is a 0–1 suitability value. Composition reads them to order
    candidates within a domain: a cheaper-than-cap property outranks one that
    overshoots, and an indoor option outranks an exposed one on a stormy day.
    """

    budget_fit: float = Field(default=1.0, ge=0.0, le=1.0)
    weather_fit: float = Field(default=1.0, ge=0.0, le=1.0)
    constraint_fit: float = Field(default=1.0, ge=0.0, le=1.0)


class CandidateAdmissionResult(StrictModel):
    candidate_id: str = Field(min_length=1)
    selection_slot_id: Optional[str] = None
    status: Literal["passed", "insufficient_for_admission"]
    checked_constraint_ids: List[str] = Field(default_factory=list)
    missing_field_paths: List[str] = Field(default_factory=list)
    fit_scores: CandidateFitScores = Field(default_factory=CandidateFitScores)
    evaluated_fact_revision: int = Field(ge=0)
    evaluated_weather_revision: int = Field(ge=0)
    weather_impact_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_outcome(self) -> "CandidateAdmissionResult":
        if self.status == "passed" and self.missing_field_paths:
            raise ValueError("passed admission cannot contain missing-field reasons")
        if self.status == "insufficient_for_admission" and not self.missing_field_paths:
            raise ValueError("insufficient admission requires missing field paths")
        return self


class CandidateResearchGap(StrictModel):
    gap_id: str = Field(min_length=1)
    worker_kind: Literal[
        "destination_researcher", "accommodation_researcher", "transport_researcher"
    ]
    reason: Literal[
        "missing_candidate",
        "missing_comparison_fact",
    ]
    candidate_id: Optional[str] = None
    selection_slot_id: Optional[str] = None
    field_path: Optional[str] = None
    destination_id: Optional[str] = None
    provider_evidence_scope_id: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    gate_class: GateClass = GateClass.COMPOSITION
    research_domain: Optional[ResearchDomain] = None
    status: Literal[
        "open",
        "researching",
        "resolved",
        "exhausted",
    ] = "open"
    attempted_signatures: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scope(self) -> "CandidateResearchGap":
        if self.reason != "missing_candidate" and self.candidate_id is None:
            raise ValueError("candidate-scoped gap requires candidate id")
        if self.reason == "missing_comparison_fact" and self.field_path is None:
            raise ValueError("field-scoped candidate gap requires field path")
        is_long_distance = self.field_path == "transport_class.long_distance"
        if is_long_distance and self.provider_evidence_scope_id is None:
            raise ValueError(
                "long-distance candidate gap requires exact Provider evidence scope"
            )
        if not is_long_distance and self.provider_evidence_scope_id is not None:
            raise ValueError(
                "only long-distance candidate gaps may bind Provider evidence scope"
            )
        return self


class DeliveryQualityGap(StrictModel):
    gap_id: str = Field(min_length=1)
    gate: Literal[
        "slot",
        "itinerary",
        "source_weather",
        "integrity",
    ]
    reason: Literal[
        "slot_option_count",
        "slot_comparison_mismatch",
        "missing_lodging_night",
        "missing_dining_day",
        "route_infeasible",
        "route_endpoint_mismatch",
        "physical_candidate_reused",
        "destination_transfer_missing",
        "day_date_discontinuity",
        "day_timeline_missing",
        "fact_not_verified",
        "source_missing",
        "weather_lineage_missing",
        "weather_unavailable_refreshable",
        "workspace_missing",
        "catalog_missing",
    ]
    field_path: str = Field(min_length=1)
    entity_id: Optional[str] = None
    candidate_id: Optional[str] = None
    worker_kind: Optional[
        Literal["destination_researcher", "accommodation_researcher", "transport_researcher"]
    ] = None
    repair_context: Dict[str, Any] = Field(default_factory=dict)
    gate_class: GateClass = GateClass.COMPOSITION
    research_domain: Optional[ResearchDomain] = None
    blocking: bool = True
    retry_target: Literal[
        "itinerary_planner",
        "destination_researcher",
        "accommodation_researcher",
        "transport_researcher",
        "weather_refresh",
        "candidate_gate",
        "composition_repair",
    ]


class RecommendationQualityState(StrictModel):
    schema_gate: Literal["pending", "passed", "failed"]
    candidate_gate: Literal["pending", "passed", "failed"]
    slot_gate: Literal["pending", "passed", "failed"]
    itinerary_gate: Literal["pending", "passed", "failed"]
    source_weather_gate: Literal["pending", "passed", "failed"]
    active_gap_ids: List[str] = Field(default_factory=list)


class PersonalizationInfluence(StrictModel):
    influence_id: str = Field(min_length=1)
    target_ref: Union[EntityRef, SelectionSlotRef]
    constraint_id: str = Field(min_length=1)
    effect: Literal["candidate_filter", "option_ranking", "selection_reason"]
    source_kind: Literal["current_request", "saved_preference", "trip_context"]
    display_text: str = Field(min_length=1)


class SelectionOption(StrictModel):
    option_id: str = Field(min_length=1)
    selection_slot_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    candidate_entity_ref: EntityRef
    rank: int = Field(ge=1, le=3)
    constraint_result: Literal["passed"] = "passed"
    selection_reasons: List[str] = Field(min_length=2, max_length=3)
    tradeoff: Optional[str] = None
    comparison_facts: List[str] = Field(min_length=1)
    availability_status: Literal["confirmed", "needs_confirmation"]
    fact_assertion_ids: List[str] = Field(min_length=1)
    source_record_ids: List[str] = Field(min_length=1)
    personalization_influence_ids: List[str] = Field(default_factory=list)


# The four domains a slot can offer alternatives in, and the canonical itinerary
# entity each one swaps.  **Written once** — spell this mapping as a
# ``"lodging"``-or-else ternary at the call sites instead and a fifth domain cannot be
# added without finding every one of them.
SELECTION_SLOT_ENTITY_TYPES: dict[str, EntityType] = {
    "lodging": EntityType.LODGING_STAY,
    "dining": EntityType.DINING_STOP,
    "visit": EntityType.VISIT_STOP,
    "transport": EntityType.TRANSPORT_LEG,
}

SelectionSlotType = Literal["lodging", "dining", "visit", "transport"]


class SelectionSlot(StrictModel):
    selection_slot_id: str = Field(min_length=1)
    slot_type: SelectionSlotType
    target_entity_id: str = Field(min_length=1)
    context: Dict[str, Any]
    options: List[SelectionOption] = Field(default_factory=list, max_length=3)
    recommended_option_id: Optional[str] = None
    selected_option_id: Optional[str] = None
    status: Literal["researching", "ready", "refreshing", "needs_user_decision"]

    @model_validator(mode="after")
    def validate_options(self) -> "SelectionSlot":
        ids = [item.option_id for item in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("selection option ids must be unique")
        if not ids:
            if self.status == "ready":
                raise ValueError("ready selection slot requires at least one passed option")
            if self.recommended_option_id is not None or self.selected_option_id is not None:
                raise ValueError("empty selection slot cannot name recommended or selected options")
            return self
        if self.recommended_option_id not in ids:
            raise ValueError("recommended option must belong to the slot")
        if self.selected_option_id is not None and self.selected_option_id not in ids:
            raise ValueError("selected option must belong to the slot")
        if self.status == "ready" and self.selected_option_id is None:
            raise ValueError("ready selection slot requires a selected option")
        expected_ranks = list(range(1, len(self.options) + 1))
        if sorted(item.rank for item in self.options) != expected_ranks:
            raise ValueError("selection option ranks must be contiguous from 1")
        if any(item.selection_slot_id != self.selection_slot_id for item in self.options):
            raise ValueError("selection option belongs to a different slot")
        return self


class SourceRecord(StrictModel):
    source_record_id: str = Field(min_length=1)
    source_kind: Literal["external_web", "external_tool", "rag_chunk"]
    title: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    # Structured evidence class — self-attested sources are never external evidence.
    attestation: Literal["external", "self"] = "external"
    canonical_url: Optional[str] = None
    public_excerpt: str
    published_at: Optional[datetime] = None
    retrieved_at: datetime
    observed_at: Optional[datetime] = None
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    provider_valid_until: Optional[datetime] = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot: Dict[str, Any]
    lifecycle_status: Literal["active", "superseded", "withdrawn", "rejected"] = "active"
    tool_audit_id: Optional[str] = Field(default=None, min_length=1)
    cache_provenance: Optional[ProviderSnapshotProvenance] = None

    @model_validator(mode="after")
    def reject_self_attested_sources(self) -> "SourceRecord":
        if self.attestation == "self":
            raise ValueError("source record cannot use self-attested evidence")
        if self.cache_provenance is not None:
            provenance = self.cache_provenance
            if provenance.provider_name != self.provider_name:
                raise ValueError("cache provenance provider does not match the source record")
            if provenance.content_hash != self.content_hash:
                raise ValueError("cache provenance content hash does not match the source record")
            if provenance.observed_at != self.observed_at:
                raise ValueError("cache provenance observation does not match the source record")
            if provenance.retrieved_at != self.retrieved_at:
                raise ValueError("cache provenance retrieval does not match the source record")
            if provenance.provider_valid_until != self.provider_valid_until:
                raise ValueError("cache provenance validity does not match the source record")
        return self


class FactSourceLink(StrictModel):
    source_record_id: str = Field(min_length=1)
    relation: Literal["supports", "qualifies", "contradicts"]
    source_locator: str = Field(min_length=1)


class FactAssertion(StrictModel):
    fact_assertion_id: str = Field(min_length=1)
    entity_ref: EntityRef
    field_path: str = Field(min_length=1)
    asserted_value: Any
    unit: Optional[str] = None
    currency: Optional[str] = None
    criticality: Literal["execution_critical", "decision_critical", "auxiliary"]
    status: Literal["verified", "refreshing", "stale", "conflict", "missing", "superseded"]
    observed_at: Optional[datetime] = None
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    source_links: List[FactSourceLink] = Field(default_factory=list)
    supersedes_assertion_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_verified_sources(self) -> "FactAssertion":
        if self.status == "verified" and not any(link.relation == "supports" for link in self.source_links):
            raise ValueError("verified fact requires a supporting source")
        return self


_SELF_ATTESTED_LOCATOR_MARKERS = (
    "user input",
    "user_input",
    "derived from user",
    "derived_from_user",
    "deterministic:",
    "journeypilot",
    "assistant",
    "model context",
)
_STABLE_IDENTITY_FIELD_PATHS = {
    "name",
    "property_name",
    "address",
    "place_id",
    "provider_place_type",
    "provider_country_code",
    "transport_class",
    "selected_mode",
    "from_endpoint",
    "to_endpoint",
}
_CONTROLLED_OR_COMPUTED_FIELDS_BY_ENTITY = {
    EntityType.LODGING_STAY: {"check_in_date", "check_out_date", "nights"},
}
_UNCERTAIN_IDENTITY_MARKERS = (
    "not verified",
    "unverified",
    "reference only",
    "placeholder",
    "unknown",
    "no verified specific",
    "未检索",
    "未确认",
    "待确认",
    "不详",
    "未確認",
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _is_self_attested_fact(fact: FactAssertion) -> bool:
    if fact.field_path in _CONTROLLED_OR_COMPUTED_FIELDS_BY_ENTITY.get(
        fact.entity_ref.entity_type,
        set(),
    ):
        return True
    return any(
        link.relation == "supports"
        and any(marker in link.source_locator.lower() for marker in _SELF_ATTESTED_LOCATOR_MARKERS)
        for link in fact.source_links
    )


def _is_failed_source(source: SourceRecord | None) -> bool:
    if source is None or source.lifecycle_status != "active":
        return True
    title = source.title.lower().strip()
    return (
        source.snapshot.get("error") is not None
        or source.snapshot.get("degradation_reason") is not None
        or title.endswith(" failure")
        or title.endswith(" degradation")
    )


def _is_supported_only_by_failed_sources(
    fact: FactAssertion,
    *,
    sources: Dict[str, SourceRecord],
) -> bool:
    supporting_source_ids = [
        link.source_record_id
        for link in fact.source_links
        if link.relation == "supports"
    ]
    return bool(supporting_source_ids) and all(
        _is_failed_source(sources.get(source_id))
        for source_id in supporting_source_ids
    )


def _source_snapshot_contains_value(source: SourceRecord, value: Any) -> bool:
    """Require provider-returned stable IDs instead of model-authored evidence."""
    if not isinstance(value, str) or not value.strip():
        return False
    snapshot_text = json.dumps(
        source.snapshot,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    normalized_snapshot = " ".join(snapshot_text.split()).casefold()
    normalized_value = " ".join(value.split()).casefold()
    return normalized_value in normalized_snapshot


def _source_snapshot_contains_exact_scalar(source: SourceRecord, value: str) -> bool:
    expected = " ".join(value.split()).casefold()

    def contains(item: Any) -> bool:
        if isinstance(item, dict):
            return any(contains(child) for child in item.values())
        if isinstance(item, list):
            return any(contains(child) for child in item)
        return isinstance(item, str) and " ".join(item.split()).casefold() == expected

    return contains(source.snapshot)


def _source_snapshot_contains_exact_value(source: SourceRecord, value: Any) -> bool:
    expected = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def contains(item: Any) -> bool:
        if json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) == expected:
            return True
        if isinstance(item, dict):
            return any(contains(child) for child in item.values())
        if isinstance(item, list):
            return any(contains(child) for child in item)
        return False

    return contains(source.snapshot)


def _has_raw_source_value(
    fact: FactAssertion,
    *,
    sources: Dict[str, SourceRecord],
) -> bool:
    return any(
        link.relation == "supports"
        and (source := sources.get(link.source_record_id)) is not None
        and not _is_failed_source(source)
        and (
            _source_snapshot_contains_exact_scalar(source, fact.asserted_value)
            if fact.field_path == "provider_country_code"
            else _source_snapshot_contains_value(source, fact.asserted_value)
        )
        for link in fact.source_links
    )


def _can_verify_stable_identity_fact(
    fact: FactAssertion,
    *,
    sources: Dict[str, SourceRecord],
    as_of: datetime,
) -> bool:
    if fact.status != "stale" or fact.field_path not in _STABLE_IDENTITY_FIELD_PATHS:
        return False
    if isinstance(fact.asserted_value, str) and any(
        marker in fact.asserted_value.lower() for marker in _UNCERTAIN_IDENTITY_MARKERS
    ):
        return False
    now = _aware(as_of)
    if fact.effective_from is not None and _aware(fact.effective_from) > now:
        return False
    if any(
        boundary is not None and _aware(boundary) <= now
        for boundary in (fact.effective_to, fact.expires_at)
    ):
        return False
    supporting_sources = [
        sources.get(link.source_record_id)
        for link in fact.source_links
        if link.relation == "supports"
    ]
    return any(
        source is not None
        and source.lifecycle_status == "active"
        and (source.effective_from is None or _aware(source.effective_from) <= now)
        and all(
            boundary is None or _aware(boundary) > now
            for boundary in (source.effective_to, source.provider_valid_until)
        )
        for source in supporting_sources
    )


class FieldProvenance(StrictModel):
    origin: Literal["external_fact", "planning_decision", "deterministic_computation", "user_input"]
    entity_ref: EntityRef
    field_path: str = Field(min_length=1)
    reference_ids: List[str] = Field(min_length=1)


class FactStoreSnapshot(StrictModel):
    contract_version: Literal[FACT_SNAPSHOT_CONTRACT_VERSION] = FACT_SNAPSHOT_CONTRACT_VERSION
    fact_data_revision: int = Field(ge=0)
    source_records: List[SourceRecord] = Field(default_factory=list)
    fact_assertions: List[FactAssertion] = Field(default_factory=list)
    field_provenance: List[FieldProvenance] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_links(self) -> "FactStoreSnapshot":
        source_ids = {source.source_record_id for source in self.source_records}
        assertion_ids = {fact.fact_assertion_id for fact in self.fact_assertions}
        if len(source_ids) != len(self.source_records):
            raise ValueError("fact snapshot source record ids must be unique")
        if len(assertion_ids) != len(self.fact_assertions):
            raise ValueError("fact snapshot assertion ids must be unique")
        for fact in self.fact_assertions:
            missing = {link.source_record_id for link in fact.source_links} - source_ids
            if missing:
                raise ValueError(f"fact assertion references missing sources: {sorted(missing)}")
        for provenance in self.field_provenance:
            if provenance.origin == "external_fact" and not set(provenance.reference_ids) <= assertion_ids:
                raise ValueError("external fact provenance references missing assertion")
        return self


class ResearchPacket(StrictModel):
    # LangGraph may reconstruct an invalid checkpoint with model_construct.
    # Revalidate this packet boundary so nested candidate/fact dicts cannot
    # masquerade as the current typed research contract.
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    contract_version: Literal[RESEARCH_PACKET_CONTRACT_VERSION] = RESEARCH_PACKET_CONTRACT_VERSION
    research_packet_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    worker_kind: Literal[
        "destination_researcher",
        "accommodation_researcher",
        "transport_researcher",
    ]
    constraint_pack_revision: int = Field(ge=0)
    fact_data_revision: int = Field(ge=0)
    query_context: Dict[str, Any]
    candidates: List[ResearchCandidate] = Field(default_factory=list)
    source_records: List[SourceRecord] = Field(min_length=1)
    fact_assertions: List[FactAssertion] = Field(default_factory=list)
    field_provenance: List[FieldProvenance] = Field(default_factory=list)
    generated_at: datetime

    @model_validator(mode="after")
    def normalize_and_validate_lineage(self) -> "ResearchPacket":
        """Normalize packet boundaries then enforce lineage invariants.

        This is intentionally *not* an identity transform: self-attested or
        failed-source facts are stripped, freshness may be rewritten, and
        candidate fact/source id lists are canonicalized.  Callers that need
        the pre-normalize worker dump must keep a separate copy before
        construction.
        """
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("research packet candidate ids must be unique")
        allowed_kinds = {
            "destination_researcher": {"visit", "dining"},
            "accommodation_researcher": {"lodging"},
            "transport_researcher": {"transport"},
        }[self.worker_kind]
        if any(candidate.candidate_kind not in allowed_kinds for candidate in self.candidates):
            raise ValueError("research packet contains a candidate from another worker domain")

        unique_sources: Dict[str, SourceRecord] = {}
        for source in self.source_records:
            existing = unique_sources.get(source.source_record_id)
            if existing is not None and existing != source:
                raise ValueError("research packet has conflicting duplicate source records")
            unique_sources.setdefault(source.source_record_id, source)
        source_index = unique_sources
        # An unknown source id is a broken packet boundary, not a recoverable
        # Provider failure.  Check it before filtering failed-source facts so
        # a dangling link can never be silently removed and turn a malformed
        # candidate closure into an apparently valid empty packet.
        if any(
            link.source_record_id not in source_index
            for fact in self.fact_assertions
            for link in fact.source_links
        ):
            raise ValueError("research packet fact references a source outside the packet")
        removed_fact_ids = {
            fact.fact_assertion_id
            for fact in self.fact_assertions
            if _is_self_attested_fact(fact)
            or _is_supported_only_by_failed_sources(fact, sources=source_index)
        }
        removed_fact_entities = {
            fact.entity_ref.entity_id
            for fact in self.fact_assertions
            if fact.fact_assertion_id in removed_fact_ids
        }
        normalized_status_ids: set[str] = set()
        eligible_facts: List[FactAssertion] = []
        for fact in self.fact_assertions:
            if fact.fact_assertion_id in removed_fact_ids:
                continue
            if _can_verify_stable_identity_fact(
                fact,
                sources=source_index,
                as_of=self.generated_at,
            ):
                fact = fact.model_copy(update={"status": "verified"})
                normalized_status_ids.add(fact.fact_assertion_id)
            eligible_facts.append(fact)
        if not eligible_facts and self.candidates:
            raise ValueError("research packet requires external identity-bound facts")
        if not self.candidates and eligible_facts:
            raise ValueError("zero-candidate research packet cannot contain facts")
        if not self.candidates and self.field_provenance:
            raise ValueError("zero-candidate research packet cannot contain field provenance")
        if removed_fact_ids or normalized_status_ids:
            object.__setattr__(self, "fact_assertions", eligible_facts)
            object.__setattr__(
                self,
                "field_provenance",
                [
                    item.model_copy(
                        update={
                            "reference_ids": [
                                reference_id
                                for reference_id in item.reference_ids
                                if reference_id not in removed_fact_ids
                            ]
                        }
                    )
                    for item in self.field_provenance
                    if any(
                        reference_id not in removed_fact_ids
                        for reference_id in item.reference_ids
                    )
                ],
            )

        unique_facts: Dict[str, FactAssertion] = {}
        for fact in self.fact_assertions:
            existing = unique_facts.get(fact.fact_assertion_id)
            if existing is not None and existing != fact:
                raise ValueError("research packet has conflicting duplicate fact assertions")
            unique_facts.setdefault(fact.fact_assertion_id, fact)
        if len(unique_sources) != len(self.source_records):
            object.__setattr__(self, "source_records", list(unique_sources.values()))
        if len(unique_facts) != len(self.fact_assertions):
            object.__setattr__(self, "fact_assertions", list(unique_facts.values()))
        fact_index = unique_facts
        provenance_keys = {
            (item.entity_ref.entity_id, item.field_path) for item in self.field_provenance
        }
        if any(
            fact.status == "verified"
            and fact.field_path
            in {"place_id", "provider_place_type", "provider_country_code"}
            and not _has_raw_source_value(fact, sources=source_index)
            for fact in self.fact_assertions
        ):
            raise ValueError(
                "verified provider identity value must occur in a supporting external source snapshot"
            )
        if any(
            fact.status == "verified"
            and fact.entity_ref.entity_type == EntityType.TRANSPORT_LEG
            and fact.field_path
            in {
                "route_id",
                "selected_mode",
                "departure_at",
                "arrival_at",
                "duration_minutes",
                "distance_meters",
                "segments",
            }
            and not any(
                link.relation == "supports"
                and (source := source_index.get(link.source_record_id)) is not None
                and not _is_failed_source(source)
                and _source_snapshot_contains_exact_value(source, fact.asserted_value)
                for link in fact.source_links
            )
            for fact in self.fact_assertions
        ):
            raise ValueError(
                "verified transport value must exactly occur in a supporting route snapshot"
            )
        if any(
            item.origin == "external_fact" and not set(item.reference_ids) <= fact_index.keys()
            for item in self.field_provenance
        ):
            raise ValueError("research packet provenance references a fact outside the packet")
        normalized_candidates: List[ResearchCandidate] = []
        source_order = [source.source_record_id for source in self.source_records]
        for candidate in self.candidates:
            candidate_facts = [
                fact
                for fact in self.fact_assertions
                if fact.entity_ref.entity_id == candidate.candidate_id
            ]
            if not candidate_facts:
                raise ValueError("candidate requires at least one identity-bound fact assertion")
            canonical_fact_ids = [fact.fact_assertion_id for fact in candidate_facts]
            canonical_field_paths = list(
                dict.fromkeys(fact.field_path for fact in candidate_facts)
            )
            if any(
                not set(evaluation.fact_assertion_ids) <= set(canonical_fact_ids)
                for evaluation in candidate.constraint_evaluations
            ):
                raise ValueError(
                    "constraint evaluation references a fact outside its research packet"
                )
            fact_statuses = {fact.status for fact in candidate_facts}
            expected_freshness = (
                "current"
                if fact_statuses == {"verified"}
                else "refreshing"
                if "refreshing" in fact_statuses
                else "stale"
            )
            if candidate.freshness_status != expected_freshness:
                if candidate.candidate_id in removed_fact_entities or (
                    set(canonical_fact_ids) & normalized_status_ids
                ):
                    candidate = candidate.model_copy(
                        update={"freshness_status": expected_freshness}
                    )
                else:
                    raise ValueError(
                        "candidate freshness status must be derived from its fact assertions"
                    )
            supported_sources = {
                link.source_record_id
                for fact in candidate_facts
                for link in fact.source_links
                if link.relation == "supports"
            }
            canonical_source_ids = [
                source_id for source_id in source_order if source_id in supported_sources
            ]
            if (
                candidate.research_packet_id != self.research_packet_id
                or candidate.fact_assertion_ids != canonical_fact_ids
                or candidate.source_record_ids != canonical_source_ids
                or candidate.field_paths != canonical_field_paths
            ):
                candidate = candidate.model_copy(
                    update={
                        "research_packet_id": self.research_packet_id,
                        "fact_assertion_ids": canonical_fact_ids,
                        "source_record_ids": canonical_source_ids,
                        "field_paths": canonical_field_paths,
                    }
                )
            if any(
                (candidate.candidate_id, field_path) not in provenance_keys
                for field_path in candidate.field_paths
            ):
                raise ValueError("candidate field path lacks field provenance")
            normalized_candidates.append(candidate)
        if normalized_candidates != self.candidates:
            object.__setattr__(self, "candidates", normalized_candidates)
        return self


class RecommendationCatalog(StrictModel):
    # Catalogs are also persisted as a single Pydantic channel value and may be
    # model_construct'ed when one nested packet fails current validation.
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    contract_version: Literal[RECOMMENDATION_CATALOG_CONTRACT_VERSION] = (
        RECOMMENDATION_CATALOG_CONTRACT_VERSION
    )
    fact_data_revision: int = Field(ge=0)
    weather_data_revision: int = Field(ge=0)
    research_packets: List[ResearchPacket] = Field(default_factory=list)
    admission_results: List[CandidateAdmissionResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_catalog(self) -> "RecommendationCatalog":
        packet_ids = [packet.research_packet_id for packet in self.research_packets]
        if len(packet_ids) != len(set(packet_ids)):
            raise ValueError("recommendation catalog packet ids must be unique")
        candidates = [candidate for packet in self.research_packets for candidate in packet.candidates]
        candidate_ids = [candidate.candidate_id for candidate in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate identity cannot be duplicated across research packets")
        if any(packet.fact_data_revision != self.fact_data_revision for packet in self.research_packets):
            raise ValueError("research packet fact revision does not match recommendation catalog")
        keys: set[tuple[str, Optional[str]]] = set()
        candidate_index = {candidate.candidate_id: candidate for candidate in candidates}
        for admission in self.admission_results:
            candidate = candidate_index.get(admission.candidate_id)
            if candidate is None:
                raise ValueError("admission result references a missing candidate")
            if admission.evaluated_fact_revision != self.fact_data_revision:
                raise ValueError("admission result uses a different fact revision")
            if admission.evaluated_weather_revision != self.weather_data_revision:
                raise ValueError("admission result uses a different weather revision")
            key = (admission.candidate_id, admission.selection_slot_id)
            if key in keys:
                raise ValueError("candidate admission result is duplicated for a slot")
            keys.add(key)
            if admission.status == "passed" and not set(
                candidate.active_constraint_ids
            ) <= set(admission.checked_constraint_ids):
                raise ValueError("passed admission did not check every active hard constraint")
            if set(admission.weather_impact_ids) != set(candidate.weather_impact_ids):
                raise ValueError("candidate weather impact lineage differs from admission")
        return self

    def candidate_index(self) -> Dict[str, ResearchCandidate]:
        return {
            candidate.candidate_id: candidate
            for packet in self.research_packets
            for candidate in packet.candidates
        }

    def admission_index(self) -> Dict[tuple[str, Optional[str]], CandidateAdmissionResult]:
        return {
            (result.candidate_id, result.selection_slot_id): result
            for result in self.admission_results
        }


class WeatherTimeWindow(StrictModel):
    start_at: datetime
    end_at: datetime
    precipitation_probability_pct: Optional[float] = Field(default=None, ge=0, le=100)
    apparent_temperature_c: Optional[float] = None
    wind_speed_kph: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_window(self) -> "WeatherTimeWindow":
        if self.end_at <= self.start_at:
            raise ValueError("weather time window must end after it starts")
        return self


class WeatherCoverage(StrictModel):
    destination_id: str = Field(min_length=1)
    start_date: Date
    end_date: Date
    status: Literal["complete", "partial", "unavailable"]
    available_dates: List[Date] = Field(default_factory=list)
    unavailable_dates: List[Date] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dates(self) -> "WeatherCoverage":
        if self.end_date < self.start_date:
            raise ValueError("weather coverage end date must not precede start date")
        available = set(self.available_dates)
        unavailable = set(self.unavailable_dates)
        if available & unavailable:
            raise ValueError("weather coverage date cannot be both available and unavailable")
        if self.status == "complete" and unavailable:
            raise ValueError("complete weather coverage cannot contain unavailable dates")
        if self.status == "unavailable" and available:
            raise ValueError("unavailable weather coverage cannot contain available dates")
        return self


class WeatherDayContext(StrictModel):
    destination_id: str = Field(min_length=1)
    date: Date
    timezone: Optional[str] = Field(default=None, min_length=1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    data_kind: Literal["forecast", "seasonal_baseline", "unavailable"]
    condition_code: Optional[int] = None
    condition_label: Optional[str] = None
    high_c: Optional[float] = None
    low_c: Optional[float] = None
    apparent_high_c: Optional[float] = None
    precipitation_probability_pct: Optional[float] = Field(default=None, ge=0, le=100)
    precipitation_mm: Optional[float] = Field(default=None, ge=0)
    wind_speed_kph: Optional[float] = Field(default=None, ge=0)
    wind_gust_kph: Optional[float] = Field(default=None, ge=0)
    hourly_windows: List[WeatherTimeWindow] = Field(default_factory=list)
    alert_ids: List[str] = Field(default_factory=list)
    fact_assertion_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unavailable(self) -> "WeatherDayContext":
        factual_values = (
            self.condition_code,
            self.condition_label,
            self.high_c,
            self.low_c,
            self.apparent_high_c,
            self.precipitation_probability_pct,
            self.precipitation_mm,
            self.wind_speed_kph,
            self.wind_gust_kph,
        )
        if self.data_kind == "unavailable" and any(value is not None for value in factual_values):
            raise ValueError("unavailable weather must not contain fabricated facts")
        if self.data_kind == "seasonal_baseline" and (
            self.condition_code is not None
            or self.condition_label is not None
            or self.apparent_high_c is not None
            or self.precipitation_probability_pct is not None
            or self.hourly_windows
        ):
            raise ValueError("seasonal baseline cannot masquerade as a daily forecast")
        if self.data_kind != "unavailable" and self.timezone is None:
            raise ValueError("available weather requires a resolved timezone")
        return self


class WeatherImpact(StrictModel):
    weather_impact_id: str = Field(min_length=1)
    date: Date
    target_ref: Union[EntityRef, SelectionSlotRef, TransportLegRef]
    condition_type: Literal[
        "rain", "heat", "cold", "wind", "thunderstorm", "snow", "visibility"
    ]
    severity: Literal["low", "medium", "high"]
    action: Literal[
        "keep",
        "move_time",
        "rerank",
        "replace",
        "change_transport",
        "add_buffer",
        "require_plan_b",
    ]
    fact_assertion_ids: List[str] = Field(min_length=1)
    affected_constraint_ids: List[str] = Field(default_factory=list)
    data_kind: Literal["forecast", "seasonal_baseline"]
    trigger_code: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_action(self) -> "WeatherImpact":
        if self.data_kind == "seasonal_baseline" and self.action in {
            "move_time",
            "replace",
            "change_transport",
            "add_buffer",
            "require_plan_b",
        }:
            raise ValueError("seasonal baseline cannot drive a day-specific itinerary change")
        if self.severity == "high" and self.action not in {"replace", "change_transport"}:
            raise ValueError("high weather impact must remove an unsafe candidate or transport mode")
        return self


class WeatherRescheduleOperation(StrictModel):
    type: Literal["reschedule_item"] = "reschedule_item"
    item_id: str = Field(min_length=1)
    expected_planned_start: Optional[datetime] = None
    expected_planned_end: Optional[datetime] = None
    planned_start: datetime
    planned_end: datetime

    @model_validator(mode="after")
    def validate_schedule(self) -> "WeatherRescheduleOperation":
        if self.planned_end <= self.planned_start:
            raise ValueError("weather reschedule must end after it starts")
        if (
            self.expected_planned_start == self.planned_start
            and self.expected_planned_end == self.planned_end
        ):
            raise ValueError("weather reschedule must change the current schedule")
        return self


class WeatherSelectionOperation(StrictModel):
    type: Literal["select_option"] = "select_option"
    selection_slot_id: str = Field(min_length=1)
    expected_option_id: Optional[str] = Field(default=None, min_length=1)
    option_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_option_change(self) -> "WeatherSelectionOperation":
        if self.expected_option_id == self.option_id:
            raise ValueError("weather selection must choose another option")
        return self


class WeatherVisitReplacementOperation(StrictModel):
    type: Literal["replace_visit_candidate"] = "replace_visit_candidate"
    item_id: str = Field(min_length=1)
    expected_candidate_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidate_change(self) -> "WeatherVisitReplacementOperation":
        if self.expected_candidate_id == self.candidate_id:
            raise ValueError("weather visit replacement must use another candidate")
        return self


class WeatherTransportModeOperation(StrictModel):
    type: Literal["set_transport_mode"] = "set_transport_mode"
    transport_leg_id: str = Field(min_length=1)
    expected_mode: TransportMode
    selected_mode: TransportMode

    @model_validator(mode="after")
    def validate_mode_change(self) -> "WeatherTransportModeOperation":
        if self.expected_mode == self.selected_mode:
            raise ValueError("weather transport adjustment must change mode")
        return self


class WeatherBufferOperation(StrictModel):
    type: Literal["add_buffer"] = "add_buffer"
    target_entity_id: str = Field(min_length=1)
    day_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    duration_minutes: int = Field(ge=5, le=180)


WeatherAdjustmentOperation = Annotated[
    Union[
        WeatherRescheduleOperation,
        WeatherSelectionOperation,
        WeatherVisitReplacementOperation,
        WeatherTransportModeOperation,
        WeatherBufferOperation,
    ],
    Field(discriminator="type"),
]


class WeatherAdjustmentProposal(StrictModel):
    proposal_id: str = Field(min_length=1)
    date: Date
    base_workspace_revision: int = Field(ge=0)
    base_weather_data_revision: int = Field(ge=1)
    severity: Literal["medium", "high"]
    summary: str = Field(min_length=1)
    weather_impact_ids: List[str] = Field(min_length=1)
    fact_assertion_ids: List[str] = Field(min_length=1)
    operations: List[WeatherAdjustmentOperation] = Field(min_length=1)
    cost_delta_cny: Optional[float] = None
    time_delta_minutes: Optional[int] = None

    @model_validator(mode="after")
    def validate_unique_refs(self) -> "WeatherAdjustmentProposal":
        if len(self.weather_impact_ids) != len(set(self.weather_impact_ids)):
            raise ValueError("weather proposal impact ids must be unique")
        if len(self.fact_assertion_ids) != len(set(self.fact_assertion_ids)):
            raise ValueError("weather proposal fact ids must be unique")
        operation_keys = [item.model_dump_json() for item in self.operations]
        if len(operation_keys) != len(set(operation_keys)):
            raise ValueError("weather proposal operations must be unique")
        return self


class WeatherContextSnapshot(StrictModel):
    contract_version: Literal[WEATHER_SNAPSHOT_CONTRACT_VERSION] = WEATHER_SNAPSHOT_CONTRACT_VERSION
    weather_data_revision: int = Field(ge=0)
    trip_start_date: Date
    trip_end_date: Date
    days: List[WeatherDayContext]
    coverage: List[WeatherCoverage] = Field(default_factory=list)
    impacts: List[WeatherImpact] = Field(default_factory=list)
    adjustment_proposals: List[WeatherAdjustmentProposal] = Field(default_factory=list)
    retrieved_at: datetime

    @model_validator(mode="after")
    def validate_coverage(self) -> "WeatherContextSnapshot":
        if self.trip_end_date < self.trip_start_date:
            raise ValueError("weather trip end date must not precede start date")
        day_keys = [(day.destination_id, day.date) for day in self.days]
        if len(day_keys) != len(set(day_keys)):
            raise ValueError("weather destination/date pairs must be unique")
        if any(day.date < self.trip_start_date or day.date > self.trip_end_date for day in self.days):
            raise ValueError("weather day falls outside trip date range")
        coverage_ids = [item.destination_id for item in self.coverage]
        if len(coverage_ids) != len(set(coverage_ids)):
            raise ValueError("weather coverage destination ids must be unique")
        for item in self.coverage:
            expected = {
                day.date
                for day in self.days
                if day.destination_id == item.destination_id
            }
            declared = set(item.available_dates) | set(item.unavailable_dates)
            if expected != declared:
                raise ValueError("weather coverage dates must match destination days")
        impact_ids = [item.weather_impact_id for item in self.impacts]
        if len(impact_ids) != len(set(impact_ids)):
            raise ValueError("weather impact ids must be unique")
        proposal_ids = [item.proposal_id for item in self.adjustment_proposals]
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("weather adjustment proposal ids must be unique")
        impact_index = {item.weather_impact_id: item for item in self.impacts}
        for proposal in self.adjustment_proposals:
            if proposal.base_weather_data_revision != self.weather_data_revision:
                raise ValueError("weather proposal must bind the containing weather revision")
            if not set(proposal.weather_impact_ids) <= impact_index.keys():
                raise ValueError("weather proposal references a missing impact")
            if any(impact_index[item].date != proposal.date for item in proposal.weather_impact_ids):
                raise ValueError("weather proposal may only combine impacts from one day")
            expected_severity = (
                "high"
                if any(impact_index[item].severity == "high" for item in proposal.weather_impact_ids)
                else "medium"
            )
            if proposal.severity != expected_severity:
                raise ValueError("weather proposal severity does not match its impacts")
        return self


class WeatherProposalDecision(StrictModel):
    proposal_id: str = Field(min_length=1)
    decision: Literal["applied", "dismissed"]


class TripWorkspaceV2(StrictModel):
    contract_version: Literal[TRIP_WORKSPACE_CONTRACT_VERSION] = TRIP_WORKSPACE_CONTRACT_VERSION
    run_id: str = Field(min_length=1)
    workspace_revision: int = Field(ge=0)
    itinerary: StructuredItineraryV2
    recommendation_catalog: RecommendationCatalog
    user_input_anchors: List[UserInputAnchor] = Field(default_factory=list)
    selection_slots: List[SelectionSlot] = Field(default_factory=list)
    personalization_influences: List[PersonalizationInfluence] = Field(default_factory=list)
    weather_proposal_decisions: List[WeatherProposalDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_candidate_lineage(self) -> "TripWorkspaceV2":
        catalog = self.recommendation_catalog
        candidates = catalog.candidate_index()
        admissions = catalog.admission_index()
        anchor_ids = [item.anchor_id for item in self.user_input_anchors]
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("workspace user-input anchor ids must be unique")
        hard_constraint_ids = {
            item.constraint_id
            for item in self.user_input_anchors
            if item.input_kind == "hard_constraint" and item.constraint_id is not None
        }
        slot_status = {
            slot.selection_slot_id: slot.status for slot in self.selection_slots
        }
        packet_by_candidate = {
            candidate.candidate_id: packet.research_packet_id
            for packet in catalog.research_packets
            for candidate in packet.candidates
        }
        passed_candidate_ids = {
            candidate_id
            for (candidate_id, _slot_id), admission in admissions.items()
            if admission.status == "passed"
        }
        unadmitted_candidate_fact_ids = {
            fact_id
            for candidate_id, candidate in candidates.items()
            if candidate_id not in passed_candidate_ids
            for fact_id in candidate.fact_assertion_ids
        }
        unadmitted_candidate_source_ids = {
            source_id
            for candidate_id, candidate in candidates.items()
            if candidate_id not in passed_candidate_ids
            for source_id in candidate.source_record_ids
        }
        influence_ids = {item.influence_id for item in self.personalization_influences}
        if len(influence_ids) != len(self.personalization_influences):
            raise ValueError("personalization influence ids must be unique")
        proposal_decision_ids = [item.proposal_id for item in self.weather_proposal_decisions]
        if len(proposal_decision_ids) != len(set(proposal_decision_ids)):
            raise ValueError("weather proposal decisions must be unique")

        entities: List[Union[VisitStop, DiningStop, LodgingStay, TransportLeg]] = [
            *self.itinerary.visit_stops,
            *self.itinerary.dining_stops,
            *self.itinerary.lodging_stays,
            *self.itinerary.transport_legs,
        ]
        for entity in entities:
            lineage = entity.lineage
            if lineage.lineage_kind == "authored_entity":
                # An authored entry has no catalog counterpart; its place
                # identity was resolved against the global place provider
                # before the workspace was built.
                continue
            candidate = candidates.get(lineage.candidate_id)
            if candidate is None:
                raise ValueError("canonical entity references a missing candidate")
            expected_candidate_type = {
                VisitStop: VisitCandidate,
                DiningStop: DiningCandidate,
                LodgingStay: LodgingCandidate,
                TransportLeg: TransportCandidate,
            }[type(entity)]
            if not isinstance(candidate, expected_candidate_type):
                raise ValueError("canonical entity references a cross-domain candidate")
            if packet_by_candidate[lineage.candidate_id] != lineage.research_packet_id:
                raise ValueError("canonical entity research packet lineage is inconsistent")
            admission = admissions.get((lineage.candidate_id, lineage.selection_slot_id))
            invalid_selected_allowed = (
                lineage.selection_slot_id is not None
                and slot_status.get(lineage.selection_slot_id) == "needs_user_decision"
            )
            if (admission is None or admission.status != "passed") and not invalid_selected_allowed:
                raise ValueError("canonical entity candidate did not pass admission")
            if not set(lineage.fact_assertion_ids) <= set(candidate.fact_assertion_ids):
                raise ValueError("canonical entity fact lineage exceeds candidate facts")
            if not set(lineage.source_record_ids) <= set(candidate.source_record_ids):
                raise ValueError("canonical entity source lineage exceeds candidate sources")
            if not set(lineage.personalization_influence_ids) <= influence_ids:
                raise ValueError("canonical entity references a missing personalization influence")

        slot_ids = [slot.selection_slot_id for slot in self.selection_slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("selection slot ids must be unique")
        slot_targets: Dict[str, Dict[str, Any]] = {
            "dining": {item.item_id: item for item in self.itinerary.dining_stops},
            "lodging": {item.stay_id: item for item in self.itinerary.lodging_stays},
            "visit": {item.item_id: item for item in self.itinerary.visit_stops},
            "transport": {
                item.transport_leg_id: item for item in self.itinerary.transport_legs
            },
        }
        for slot in self.selection_slots:
            target = slot_targets[slot.slot_type].get(slot.target_entity_id)
            if target is None:
                raise ValueError("selection slot target is missing from canonical itinerary")
            if not slot.options:
                continue
            for option in slot.options:
                candidate = candidates.get(option.candidate_id)
                expected_kind = slot.slot_type
                if candidate is None or candidate.candidate_kind != expected_kind:
                    raise ValueError("selection option references a missing or cross-domain candidate")
                admission = admissions.get((option.candidate_id, slot.selection_slot_id))
                if admission is None or admission.status != "passed":
                    raise ValueError("selection option candidate did not pass admission for the slot")
                if option.candidate_entity_ref.entity_id != slot.target_entity_id:
                    raise ValueError("selection option entity ref must target the slot canonical entity")
                expected_entity_type = SELECTION_SLOT_ENTITY_TYPES[slot.slot_type]
                if option.candidate_entity_ref.entity_type != expected_entity_type:
                    raise ValueError("selection option entity ref has the wrong domain type")
                if not set(option.fact_assertion_ids) <= set(candidate.fact_assertion_ids):
                    raise ValueError("selection option fact refs exceed candidate facts")
                if not set(option.source_record_ids) <= set(candidate.source_record_ids):
                    raise ValueError("selection option source refs exceed candidate sources")
                if not set(option.personalization_influence_ids) <= influence_ids:
                    raise ValueError("selection option references a missing personalization influence")
            if slot.selected_option_id is None:
                if slot.status != "needs_user_decision":
                    raise ValueError("unselected non-empty slot must require a user decision")
                continue
            selected = next(option for option in slot.options if option.option_id == slot.selected_option_id)
            if target.lineage.candidate_id != selected.candidate_id:
                raise ValueError("canonical selected entity does not match slot selected option")
            if target.lineage.selection_slot_id != slot.selection_slot_id:
                raise ValueError("canonical selected entity is missing its selection slot lineage")
        return self


class PublicSourceSummary(StrictModel):
    source_record_id: str = Field(min_length=1)
    source_kind: Literal["external_web", "external_tool", "rag_chunk"]
    title: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    public_excerpt: str
    canonical_url: Optional[str] = None
    retrieved_at: datetime
    observed_at: Optional[datetime] = None
    content_hash: str = Field(min_length=1)
    rag_chunk_content: Optional[str] = None
    rag_document_locator: Optional[str] = None


class PublicSupportedValue(StrictModel):
    label: str = Field(min_length=1)
    value: Any
    unit: Optional[str] = None
    currency: Optional[str] = None


class PublicCitationProjection(StrictModel):
    citation_id: str = Field(min_length=1)
    entity_ref: EntityRef
    field_paths: List[str] = Field(min_length=1)
    fact_status: Literal["verified", "refreshing", "stale", "conflict", "missing"]
    supported_values: List[PublicSupportedValue] = Field(min_length=1)
    sources: List[PublicSourceSummary] = Field(min_length=1)
    fact_assertion_ids: List[str] = Field(min_length=1)


class ReportEntityBlock(StrictModel):
    entity_ref: EntityRef
    day_id: Optional[str] = None
    projection_role: Literal["full", "departure", "arrival", "check_in", "check_out"]
    title: str = Field(min_length=1)
    entity_kind: Literal[
        "visit",
        "dining",
        "lodging",
        "transport",
        "custom",
    ]
    summary: str = Field(min_length=1)
    details: Dict[str, Any] = Field(default_factory=dict)
    citation_ids: List[str] = Field(default_factory=list)
    weather_impact_ids: List[str] = Field(default_factory=list)
    personalization_influence_ids: List[str] = Field(default_factory=list)


class ReportDaySection(StrictModel):
    day_id: str = Field(min_length=1)
    day: int = Field(ge=1)
    date: Optional[Date] = None
    destination_id: str = Field(min_length=1)
    destination_name: str = Field(min_length=1)
    # Copied from :attr:`DayPlanV2.theme`, never re-derived: the workspace card
    # and the report describe one Day and must not be able to title it differently.
    theme: str = Field(min_length=1)
    blocks: List[ReportEntityBlock] = Field(default_factory=list)


class ReportDestination(StrictModel):
    destination_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)


class ReportSelectionOption(StrictModel):
    option_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    rank: int = Field(ge=1, le=3)
    selected: bool
    recommended: bool
    selection_reasons: List[str] = Field(min_length=2, max_length=3)
    tradeoff: Optional[str] = None
    comparison_facts: List[str] = Field(min_length=1)
    availability_status: Literal["confirmed", "needs_confirmation"]
    citation_ids: List[str] = Field(min_length=1)


class ReportSelectionSection(StrictModel):
    """Report-surface selection state (decision only; not workspace process states).

    Workspace :class:`SelectionSlot` may be researching/refreshing; the report
    projection collapses those into ready|needs_user_decision intentionally.
    """

    selection_slot_id: str = Field(min_length=1)
    slot_type: SelectionSlotType
    context: Dict[str, Any]
    status: Literal["ready", "needs_user_decision"]
    options: List[ReportSelectionOption] = Field(min_length=1, max_length=3)


class ReportWeatherDay(StrictModel):
    """One Day's weather as a reader sees it, including how old it is.

    A traveller used to be shown a forecast with nothing saying when it was
    observed, so a Run whose weather refresh had been refusing for days looked
    exactly like one refreshed a minute ago.  ``weather_data_state`` is decided
    once here, during projection, rather than inferred by each of the three
    surfaces: a mixed Day (current forecast alongside a carried-forward historical
    impact) reads ``historical``, and no renderer gets to disagree.
    """

    destination_id: str = Field(min_length=1)
    destination_name: str = Field(min_length=1)
    date: Date
    data_kind: Literal["forecast", "seasonal_baseline", "unavailable"]
    # When the underlying observation was taken.  ``None`` only where there is no
    # weather at all; required and undefaulted so a projection cannot omit it.
    observed_at: Optional[datetime]
    weather_data_state: Literal["current", "historical"]
    condition_label: Optional[str] = None
    high_c: Optional[float] = None
    low_c: Optional[float] = None
    precipitation_probability_pct: Optional[float] = None
    wind_speed_kph: Optional[float] = None
    citation_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_timeliness(self) -> "ReportWeatherDay":
        if (self.observed_at is None) != (self.data_kind == "unavailable"):
            raise ValueError(
                "weather observation timestamp must be present exactly when the day has weather"
            )
        if self.data_kind == "unavailable" and self.weather_data_state != "current":
            # Nothing is shown for this Day, so nothing about it was carried
            # forward — calling it historical would invent an old reading.
            raise ValueError("a day without weather cannot be in the historical state")
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ValueError("weather observation timestamps must be timezone-aware")
        return self


class TripReportDocument(StrictModel):
    title: str = Field(min_length=1)
    overview: str = Field(min_length=1)
    destinations: List[ReportDestination] = Field(min_length=1)
    duration_days: int = Field(ge=1)
    cost_summary: CostCoverageSummary = Field(default_factory=CostCoverageSummary.empty)
    # One sentence stating the money the plan knows, written once by
    # ``entities/cost_coverage.py``.  Undefaulted on purpose: let each surface write
    # its own label from ``cost_summary`` and the three of them drift apart.
    # ``None`` is the explicit "there is no number to state" — every surface then
    # renders no line at all, which is the honest shape when no supplier published a
    # price.  Do **not** make it default to words like 「费用待确认」 to fill that hole.
    # Still required, so a projection cannot omit it by accident; ``None`` has to be
    # passed on purpose, and an empty string is rejected either way.
    cost_coverage_statement: Optional[str] = Field(min_length=1)
    days: List[ReportDaySection] = Field(min_length=1)
    selections: List[ReportSelectionSection] = Field(default_factory=list)
    weather: List[ReportWeatherDay] = Field(default_factory=list)
    highlights: List[str] = Field(default_factory=list)
    important_notes: List[str] = Field(default_factory=list)


class TripReportProjection(StrictModel):
    source_workspace_revision: int = Field(ge=0)
    source_fact_data_revision: int = Field(ge=0)
    source_weather_data_revision: int = Field(ge=0)
    status: Literal["pending", "building", "ready", "stale", "failed"]
    document: Optional[TripReportDocument] = None
    citations: List[PublicCitationProjection] = Field(default_factory=list)
    generated_at: Optional[datetime] = None
    failure_reason: Optional[str] = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "TripReportProjection":
        if self.status == "ready" and (self.document is None or self.generated_at is None):
            raise ValueError("ready report requires a document and generated timestamp")
        if self.status == "failed" and not self.failure_reason:
            raise ValueError("failed report requires a failure reason")
        return self


class MapPlaceProjection(StrictModel):
    entity_ref: EntityRef
    name: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    citation_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_coordinates(self) -> "MapPlaceProjection":
        if self.entity_ref.entity_type not in {
            EntityType.VISIT_STOP,
            EntityType.DINING_STOP,
            EntityType.LODGING_STAY,
        }:
            raise ValueError("map place projection must reference a real place entity")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("map place coordinates must be both present or both absent")
        return self


class MapRouteProjection(StrictModel):
    entity_ref: EntityRef
    transport_class: Literal["long_distance", "public_transit", "flexible"]
    selected_mode: TransportMode
    route_status: Literal["pending", "ready", "unavailable"]
    from_endpoint: TransportEndpoint
    to_endpoint: TransportEndpoint
    segments: List[TransportSegment] = Field(default_factory=list)
    citation_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_route_state(self) -> "MapRouteProjection":
        if self.entity_ref.entity_type != EntityType.TRANSPORT_LEG:
            raise ValueError("map route projection must reference a real transport leg")
        if self.route_status == "ready" and not self.segments:
            raise ValueError("ready map route requires segments")
        if self.route_status != "ready" and self.segments:
            raise ValueError("non-ready map route cannot expose stale segments")
        return self


class MapProjectionContent(StrictModel):
    places: List[MapPlaceProjection] = Field(default_factory=list)
    routes: List[MapRouteProjection] = Field(default_factory=list)


class MapProjection(StrictModel):
    source_workspace_revision: int = Field(ge=0)
    content: MapProjectionContent = Field(default_factory=MapProjectionContent)


class SourceIndexDocument(StrictModel):
    citations: List[PublicCitationProjection] = Field(default_factory=list)


class SourceIndexProjection(StrictModel):
    source_fact_data_revision: int = Field(ge=0)
    content: SourceIndexDocument = Field(default_factory=SourceIndexDocument)


class DeliveryRevisionManifest(StrictModel):
    contract_version: Literal[DELIVERY_BUNDLE_CONTRACT_VERSION] = DELIVERY_BUNDLE_CONTRACT_VERSION
    run_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    workspace_revision: int = Field(ge=0)
    fact_data_revision: int = Field(ge=0)
    weather_data_revision: int = Field(ge=0)
    contract_versions: Dict[str, str]
    content_hashes: Dict[str, str]
    created_at: datetime


class InternalFailureClass(str, Enum):
    TRANSIENT_DEPENDENCY = "transient_dependency"
    RESEARCH_GAP = "research_gap"
    CONTRACT_VIOLATION = "contract_violation"
    PROJECTION_FAILURE = "projection_failure"
    PERSISTENCE_FAILURE = "persistence_failure"
    SAFETY_BLOCK = "safety_block"
    USER_DECISION_REQUIRED = "user_decision_required"


class DeliveryFailureRecord(StrictModel):
    failure_class: InternalFailureClass
    operation: str = Field(min_length=1)
    attempts: int = Field(ge=1)
    retry_exhausted: bool
    public_message: str = Field(min_length=1)


class RunCoverageDisclosure(StrictModel):
    """Which research domains this Run could not deliver usable results for.

    The Run already computed this and threw it away: the non-blocking
    ``delivery_quality_gaps`` and the durable ``GateFailureAttribution`` records
    both know, and neither reached a reader — a plan simply had fewer entries than
    the traveller asked for, with nothing saying so.

    Deliberately only the domain.  ``reason_code``, provider names and worker
    names are audit vocabulary; on a product surface they read as blame for a
    supplier or as an internal error, which is a new honesty problem rather than a
    fix for this one.  It lives on the Bundle because the exported PDF is rendered
    from the Bundle and never passes through the public projection.

    An empty disclosure is the normal case and means every domain delivered.
    """

    domains_without_results: List[ResearchDomain] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_domains(self) -> "RunCoverageDisclosure":
        values = [item.value for item in self.domains_without_results]
        if len(values) != len(set(values)):
            raise ValueError("coverage disclosure domains must be unique")
        return self


def _report_entity_kind(entity_type: EntityType) -> str:
    return {
        EntityType.VISIT_STOP: "visit",
        EntityType.DINING_STOP: "dining",
        EntityType.LODGING_STAY: "lodging",
        EntityType.TRANSPORT_LEG: "transport",
        EntityType.CUSTOM_BLOCK: "custom",
    }[entity_type]


def _immutable_source_payload(source: Optional[SourceRecord]) -> Optional[Dict[str, Any]]:
    if source is None:
        return None
    return source.model_dump(mode="json", exclude={"lifecycle_status"})


def _immutable_fact_payload(fact: Optional[FactAssertion]) -> Optional[Dict[str, Any]]:
    if fact is None:
        return None
    return fact.model_dump(mode="json", exclude={"status"})


class DeliveryBundle(StrictModel):
    manifest: DeliveryRevisionManifest
    workspace: TripWorkspaceV2
    fact_snapshot: FactStoreSnapshot
    weather_snapshot: WeatherContextSnapshot
    report_projection: TripReportProjection
    map_projection: MapProjection
    source_index: SourceIndexProjection
    # Required and undefaulted: a Bundle that does not state its coverage is
    # indistinguishable from one with full coverage, which is the defect.
    coverage_disclosure: RunCoverageDisclosure

    @model_validator(mode="after")
    def validate_manifest(self) -> "DeliveryBundle":
        revisions = (
            self.workspace.workspace_revision,
            self.fact_snapshot.fact_data_revision,
            self.weather_snapshot.weather_data_revision,
        )
        manifest_revisions = (
            self.manifest.workspace_revision,
            self.manifest.fact_data_revision,
            self.manifest.weather_data_revision,
        )
        if revisions != manifest_revisions:
            raise ValueError("bundle snapshots do not match manifest revisions")
        if self.workspace.run_id != self.manifest.run_id:
            raise ValueError("workspace run id does not match manifest")
        expected_contract_versions = {
            "workspace": self.workspace.contract_version,
            "facts": self.fact_snapshot.contract_version,
            "weather": self.weather_snapshot.contract_version,
        }
        if self.manifest.contract_versions != expected_contract_versions:
            raise ValueError("bundle manifest contract versions do not match snapshots")
        catalog = self.workspace.recommendation_catalog
        if catalog.fact_data_revision > self.fact_snapshot.fact_data_revision:
            raise ValueError("recommendation catalog cannot use a future fact revision")
        source_index = {
            item.source_record_id: item for item in self.fact_snapshot.source_records
        }
        fact_index = {
            item.fact_assertion_id: item for item in self.fact_snapshot.fact_assertions
        }
        provenance = set(
            json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            for item in self.fact_snapshot.field_provenance
        )
        for packet in catalog.research_packets:
            if packet.run_id != self.manifest.run_id:
                raise ValueError("research packet belongs to another run")
            if any(
                _immutable_source_payload(source_index.get(item.source_record_id))
                != _immutable_source_payload(item)
                for item in packet.source_records
            ):
                raise ValueError("research packet source is absent from current fact snapshot")
            if any(
                _immutable_fact_payload(fact_index.get(item.fact_assertion_id))
                != _immutable_fact_payload(item)
                for item in packet.fact_assertions
            ):
                raise ValueError("research packet fact is absent from current fact snapshot")
            if any(
                json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                not in provenance
                for item in packet.field_provenance
            ):
                raise ValueError("research packet provenance is absent from current fact snapshot")
        for weather_day in self.weather_snapshot.days:
            expected_entity_id = f"weather:{weather_day.destination_id}:{weather_day.date.isoformat()}"
            for assertion_id in weather_day.fact_assertion_ids:
                assertion = fact_index.get(assertion_id)
                if assertion is None:
                    raise ValueError("weather context fact is absent from current fact snapshot")
                if (
                    assertion.entity_ref.entity_type != EntityType.WEATHER_DAY
                    or assertion.entity_ref.entity_id != expected_entity_id
                ):
                    raise ValueError("weather context fact targets another destination/date")
        for impact in self.weather_snapshot.impacts:
            if not set(impact.fact_assertion_ids) <= fact_index.keys():
                raise ValueError("weather impact references a missing fact assertion")
        weather_impact_ids = {
            impact.weather_impact_id for impact in self.weather_snapshot.impacts
        }
        for candidate in catalog.candidate_index().values():
            if not set(candidate.weather_impact_ids) <= weather_impact_ids:
                raise ValueError("candidate references a missing weather impact")
        for admission in catalog.admission_results:
            if not set(admission.weather_impact_ids) <= weather_impact_ids:
                raise ValueError("candidate admission references a missing weather impact")
        for entity in [
            *self.workspace.itinerary.visit_stops,
            *self.workspace.itinerary.dining_stops,
            *self.workspace.itinerary.lodging_stays,
            *self.workspace.itinerary.transport_legs,
        ]:
            if not set(entity.lineage.weather_impact_ids) <= weather_impact_ids:
                raise ValueError("canonical entity references a missing weather impact")
        itinerary = self.workspace.itinerary
        scheduled_ids = {
            item.item_id for item in [
                *itinerary.visit_stops,
                *itinerary.dining_stops,
                *itinerary.custom_blocks,
            ]
        }
        visit_ids = {item.item_id for item in itinerary.visit_stops}
        transport_ids = {item.transport_leg_id for item in itinerary.transport_legs}
        slot_ids = {item.selection_slot_id for item in self.workspace.selection_slots}
        timeline_by_day = {
            day.day_id: {item.entity_id for item in day.timeline}
            for day in itinerary.day_plans
        }
        candidates = catalog.candidate_index()
        admissions = catalog.admission_index()
        for proposal in self.weather_snapshot.adjustment_proposals:
            if not set(proposal.fact_assertion_ids) <= fact_index.keys():
                raise ValueError("weather proposal references a missing fact assertion")
            if proposal.base_workspace_revision > self.workspace.workspace_revision:
                raise ValueError("weather proposal cannot reference a future workspace revision")
            for operation in proposal.operations:
                if isinstance(operation, WeatherRescheduleOperation):
                    if operation.item_id not in scheduled_ids:
                        raise ValueError("weather reschedule references a missing scheduled item")
                elif isinstance(operation, WeatherSelectionOperation):
                    if operation.selection_slot_id not in slot_ids:
                        raise ValueError("weather selection references a missing slot")
                elif isinstance(operation, WeatherVisitReplacementOperation):
                    candidate = candidates.get(operation.candidate_id)
                    admission = admissions.get((operation.candidate_id, None))
                    if (
                        operation.item_id not in visit_ids
                        or not isinstance(candidate, VisitCandidate)
                        or admission is None
                        or admission.status != "passed"
                    ):
                        raise ValueError("weather visit replacement is not an admitted visit")
                elif isinstance(operation, WeatherTransportModeOperation):
                    if operation.transport_leg_id not in transport_ids:
                        raise ValueError("weather transport adjustment references a missing leg")
                elif operation.target_entity_id not in timeline_by_day.get(
                    operation.day_id, set()
                ):
                    raise ValueError("weather buffer target is missing from its day")
        projection_revisions = (
            self.report_projection.source_workspace_revision,
            self.report_projection.source_fact_data_revision,
            self.report_projection.source_weather_data_revision,
        )
        if self.report_projection.status == "ready" and projection_revisions != revisions:
            raise ValueError("ready report projection is not based on bundle revisions")
        if self.report_projection.status == "stale":
            if any(source > current for source, current in zip(projection_revisions, revisions)):
                raise ValueError("stale report projection cannot reference a future revision")
        if self.map_projection.source_workspace_revision != revisions[0]:
            raise ValueError("map projection is not based on workspace revision")
        if self.source_index.source_fact_data_revision != revisions[1]:
            raise ValueError("source index is not based on fact revision")
        real_workspace_entity_refs = {
            (EntityType.VISIT_STOP, item.item_id)
            for item in self.workspace.itinerary.visit_stops
        } | {
            (EntityType.DINING_STOP, item.item_id)
            for item in self.workspace.itinerary.dining_stops
        } | {
            (EntityType.LODGING_STAY, item.stay_id)
            for item in self.workspace.itinerary.lodging_stays
        } | {
            (EntityType.TRANSPORT_LEG, item.transport_leg_id)
            for item in self.workspace.itinerary.transport_legs
        } | {
            (EntityType.CUSTOM_BLOCK, item.item_id)
            for item in self.workspace.itinerary.custom_blocks
        }
        workspace_entity_refs = real_workspace_entity_refs
        projection_fact_ids_by_ref: Dict[tuple[EntityType, str], set[str]] = {
            **{
                (EntityType.VISIT_STOP, item.item_id): set(item.lineage.fact_assertion_ids)
                for item in self.workspace.itinerary.visit_stops
            },
            **{
                (EntityType.DINING_STOP, item.item_id): set(item.lineage.fact_assertion_ids)
                for item in self.workspace.itinerary.dining_stops
            },
            **{
                (EntityType.LODGING_STAY, item.stay_id): set(item.lineage.fact_assertion_ids)
                for item in self.workspace.itinerary.lodging_stays
            },
            **{
                (EntityType.TRANSPORT_LEG, item.transport_leg_id): set(item.lineage.fact_assertion_ids)
                if item.route_status == "ready"
                else set()
                for item in self.workspace.itinerary.transport_legs
            },
            **{
                (EntityType.CUSTOM_BLOCK, item.item_id): set()
                for item in self.workspace.itinerary.custom_blocks
            },
            **{
                (
                    EntityType.WEATHER_DAY,
                    f"weather:{day.destination_id}:{day.date.isoformat()}",
                ): set(day.fact_assertion_ids)
                for day in self.weather_snapshot.days
            },
        }
        for slot in self.workspace.selection_slots:
            entity_type = SELECTION_SLOT_ENTITY_TYPES[slot.slot_type]
            for option in slot.options:
                if option.option_id == slot.selected_option_id:
                    continue
                projection_fact_ids_by_ref[(entity_type, option.candidate_id)] = set(
                    option.fact_assertion_ids
                )
        citations = self.source_index.content.citations
        citation_ids = [item.citation_id for item in citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("source projection citation ids must be unique")
        for citation in citations:
            ref = (citation.entity_ref.entity_type, citation.entity_ref.entity_id)
            allowed_fact_ids = projection_fact_ids_by_ref.get(ref)
            if allowed_fact_ids is None:
                raise ValueError("source projection contains a dangling entity reference")
            if not set(citation.fact_assertion_ids) <= allowed_fact_ids:
                raise ValueError(
                    "source projection attributes facts to the wrong entity or selection option"
                )
            projected_source_ids = {
                source.source_record_id for source in citation.sources
            }
            if not projected_source_ids <= source_index.keys():
                raise ValueError("source projection references a missing source record")
        map_refs = {
            (item.entity_ref.entity_type, item.entity_ref.entity_id)
            for item in [*self.map_projection.content.places, *self.map_projection.content.routes]
        }
        if not map_refs <= real_workspace_entity_refs:
            raise ValueError("map projection references an entity outside the itinerary")
        known_citations = set(citation_ids)
        map_citations = {
            citation_id
            for item in [*self.map_projection.content.places, *self.map_projection.content.routes]
            for citation_id in item.citation_ids
        }
        if not map_citations <= known_citations:
            raise ValueError("map projection references a missing public citation")
        if self.report_projection.status == "ready":
            if self.report_projection.citations != citations:
                raise ValueError("report and source index must share one citation projection")
            document = self.report_projection.document
            assert document is not None
            report_refs = {
                (block.entity_ref.entity_type, block.entity_ref.entity_id)
                for day in document.days
                for block in day.blocks
            }
            if not report_refs <= workspace_entity_refs:
                raise ValueError("report projection contains a dangling workspace entity reference")
            if any(
                block.entity_kind != _report_entity_kind(block.entity_ref.entity_type)
                for day in document.days
                for block in day.blocks
            ):
                raise ValueError("report projection renders an entity with the wrong content type")
            if any(
                not set(block.weather_impact_ids) <= weather_impact_ids
                for day in document.days
                for block in day.blocks
            ):
                raise ValueError("report projection references a missing weather impact")
            expected_timeline = [
                (day.day_id, ref.entity_type, ref.entity_id, ref.projection_role)
                for day in self.workspace.itinerary.day_plans
                for ref in day.timeline
            ]
            projected_timeline = [
                (day.day_id, block.entity_ref.entity_type, block.entity_ref.entity_id, block.projection_role)
                for day in document.days
                for block in day.blocks
            ]
            if projected_timeline != expected_timeline:
                raise ValueError("report projection must preserve the canonical timeline and projection roles")
            report_citations = {
                citation_id
                for day in document.days
                for block in day.blocks
                for citation_id in block.citation_ids
            } | {
                citation_id
                for selection in document.selections
                for option in selection.options
                for citation_id in option.citation_ids
            } | {
                citation_id
                for day in document.weather
                for citation_id in day.citation_ids
            }
            if not report_citations <= known_citations:
                raise ValueError("report projection references a missing public citation")
        expected_hashes = bundle_content_hashes(
            workspace=self.workspace,
            fact_snapshot=self.fact_snapshot,
            weather_snapshot=self.weather_snapshot,
            report_projection=self.report_projection,
            map_projection=self.map_projection,
            source_index=self.source_index,
            coverage_disclosure=self.coverage_disclosure,
        )
        if self.manifest.content_hashes != expected_hashes:
            raise ValueError("bundle content hashes do not match payload")
        return self


def canonical_content_hash(value: BaseModel) -> str:
    payload = value.model_dump(mode="json", by_alias=True, exclude_none=False)
    encoded = json.dumps(
        _normalize_canonical_json_numbers(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_canonical_json_numbers(value: Any) -> Any:
    """Match semantic JSON numbers across in-memory and PostgreSQL JSONB.

    JSONB normalizes ``-0.0`` to ``0.0``.  Treating those equal numeric values
    as different hash inputs makes an otherwise immutable Bundle impossible to
    read after a successful commit.  Normalize recursively before hashing while
    leaving the actual Provider snapshot untouched.
    """

    if isinstance(value, dict):
        return {
            key: _normalize_canonical_json_numbers(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_canonical_json_numbers(item) for item in value]
    if isinstance(value, float) and value == 0.0:
        return 0.0
    return value


def bundle_content_hashes(
    *,
    workspace: TripWorkspaceV2,
    fact_snapshot: FactStoreSnapshot,
    weather_snapshot: WeatherContextSnapshot,
    report_projection: TripReportProjection,
    map_projection: MapProjection,
    source_index: SourceIndexProjection,
    coverage_disclosure: "RunCoverageDisclosure",
) -> Dict[str, str]:
    # Every Bundle component is hashed, including the coverage disclosure: an
    # unhashed component sits outside the immutability the manifest exists to
    # assert, and "what this Run failed to cover" is exactly the field someone
    # would want to be able to change after the fact.
    return {
        "workspace": canonical_content_hash(workspace),
        "fact_snapshot": canonical_content_hash(fact_snapshot),
        "weather_snapshot": canonical_content_hash(weather_snapshot),
        "report_projection": canonical_content_hash(report_projection),
        "map_projection": canonical_content_hash(map_projection),
        "source_index": canonical_content_hash(source_index),
        "coverage_disclosure": canonical_content_hash(coverage_disclosure),
    }
