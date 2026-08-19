"""Typed itinerary composition decisions and deterministic workspace materialization."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from pydantic import Field, model_validator

if TYPE_CHECKING:
    # 只给类型标注用：运行期 import 它会与 provider_evidence 成环。
    # 不 import 的话那个字符串前向引用指向不存在的名字，读起来像有类型其实没有。
    from .provider_evidence import ProviderRouteLegScope

from .provider_reference_service import (
    ProviderReferenceService,
    reference_service_note,
)
from .delivery_bundle import (
    DayPlanV2,
    DiningCandidate,
    DiningStop,
    EntityLineage,
    EntityRef,
    EntityType,
    LodgingCandidate,
    LodgingStay,
    RecommendationCatalog,
    ResearchCandidate,
    SELECTION_SLOT_ENTITY_TYPES,
    SelectionOption,
    SelectionSlot,
    SelectionSlotType,
    StrictModel,
    StructuredItineraryV2,
    TimelineEntryRef,
    TransportCandidate,
    TransportEndpoint,
    TransportLeg,
    TransportMode,
    TransportSegment,
    TripWorkspaceV2,
    UserInputAnchor,
    VisitCandidate,
    VisitStop,
    build_cost_coverage_summary,
    itinerary_price_components,
    classify_transport_projection_shape,
)
from .candidate_options import candidate_option_availability
from .controlled_place_name import (
    CONTROLLED_DESTINATION_ANCHOR_PREFIX,
    controlled_public_place_name,
)
from .day_theme import derive_day_theme
from .trip_highlights import derive_trip_highlights

logger = logging.getLogger(__name__)


def _total_budget_cap_cny(anchors: Optional[list[UserInputAnchor]]) -> Optional[float]:
    for anchor in anchors or []:
        if anchor.input_kind != "hard_constraint" or not isinstance(anchor.value, Mapping):
            continue
        if str(anchor.value.get("category") or "") != "budget_cap":
            continue
        params = anchor.value.get("params")
        if not isinstance(params, Mapping):
            continue
        if str(params.get("currency") or "CNY").upper() != "CNY" or str(params.get("per") or "") != "total":
            continue
        amount = params.get("amount")
        if isinstance(amount, (int, float)) and amount >= 0:
            return float(amount)
    return None


def _controlled_destination_names(
    anchors: Optional[list[UserInputAnchor]],
) -> dict[str, str]:
    """Read the controlled public name of each destination, where one is anchored.

    The anchor is the authority and **which field on it is the public name is
    decided in one place**, :func:`controlled_public_place_name` — see that module
    for why.  Do not write the field down here as well: two readers each naming
    their own field is how they drift apart.  A caller without anchors simply gets
    no names, and the Day theme falls back to naming the Day by its own places.
    """

    names: dict[str, str] = {}
    for anchor in anchors or []:
        if (
            anchor.input_kind != "controlled_identity"
            or not anchor.field_path.startswith(CONTROLLED_DESTINATION_ANCHOR_PREFIX)
            or not isinstance(anchor.value, Mapping)
        ):
            continue
        destination_id = str(anchor.value.get("place_id") or "").strip()
        public_name = controlled_public_place_name(anchor.value)
        if destination_id and public_name:
            names[destination_id] = public_name
    return names


class AuthoredPlaceBase(StrictModel):
    """A named place the Itinerary Planner wrote and the server then located.

    ``name`` / ``local_name`` / ``address`` / ``city`` are the model's four
    required identity fields.  ``local_name`` is the place's own sign in the
    local language, which is the name place providers index it under;
    ``name`` is what the traveller reads.  The ``resolved_*`` fields are filled
    only by the place-provider resolution step; a composition may not enter
    workspace materialization while any of them is still unset.
    """

    name: str = Field(min_length=1)
    local_name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    city: str = Field(min_length=1)
    selection_reason: str = Field(min_length=1)
    resolved_place_id: Optional[str] = None
    resolved_address: Optional[str] = None
    resolved_latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    resolved_longitude: Optional[float] = Field(default=None, ge=-180, le=180)

    @property
    def is_located(self) -> bool:
        return (
            bool(self.resolved_place_id)
            and self.resolved_latitude is not None
            and self.resolved_longitude is not None
        )


class AuthoredVisitPlace(AuthoredPlaceBase):
    visit_type: Literal[
        "attraction", "experience", "culture", "shopping", "nature", "other"
    ]
    highlights: list[str] = Field(min_length=1, max_length=4)


class AuthoredDiningPlace(AuthoredPlaceBase):
    cuisine_types: list[str] = Field(min_length=1, max_length=4)
    recommended_dishes: list[str] = Field(min_length=1, max_length=4)


# A day is a chain, not a set of independent windows.  Each connector gap is a
# real trip between two stops, and a provider route only fills it when it can
# depart at or after the preceding stop ends and arrive at or before the next one
# starts — so adjacent stops must leave a window a typical local leg fits into.
MIN_LOCAL_TRANSFER_MINUTES = 30
# The shortest stay a stop may be compressed to when the chain is pushed forward
# to open those windows.
MIN_PHYSICAL_STAY_MINUTES = 30

AUTHORED_ROUTE_CLASS_BY_MODE: dict[str, str] = {
    "walk": "flexible",
    "bike": "flexible",
    "drive": "flexible",
    "taxi": "flexible",
    "ride_hailing": "flexible",
    "metro": "public_transit",
    "bus": "public_transit",
    "tram": "public_transit",
}


class AuthoredRoute(StrictModel):
    """A written connector between the two itinerary stops it sits between.

    Endpoints are never written by the model: materialization derives them from
    the adjacent stops, so only the mode and the door-to-door minutes come from
    the composition.
    """

    mode: Literal["walk", "bike", "drive", "taxi", "ride_hailing", "metro", "bus", "tram"]
    duration_minutes: int = Field(ge=1, le=360)
    selection_reason: str = Field(min_length=1)

    @property
    def transport_class(self) -> str:
        return AUTHORED_ROUTE_CLASS_BY_MODE[self.mode]


def _validate_placement_origin(
    candidate_id: Optional[str],
    authored: Optional[object],
    kind: str,
) -> None:
    if bool(candidate_id) == (authored is not None):
        raise ValueError(
            f"{kind} placement takes either an admitted candidate_id or an authored entry"
        )


class VisitPlacement(StrictModel):
    placement_kind: Literal["visit"] = "visit"
    candidate_id: Optional[str] = Field(default=None, min_length=1)
    authored_place: Optional[AuthoredVisitPlace] = None
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    duration_minutes: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_origin(self) -> "VisitPlacement":
        _validate_placement_origin(self.candidate_id, self.authored_place, "visit")
        return self


class DiningPlacement(StrictModel):
    placement_kind: Literal["dining"] = "dining"
    candidate_id: Optional[str] = Field(default=None, min_length=1)
    authored_place: Optional[AuthoredDiningPlace] = None
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    duration_minutes: int = Field(ge=1)
    meal_type: Literal["breakfast", "lunch", "dinner", "snack", "other"]

    @model_validator(mode="after")
    def validate_origin(self) -> "DiningPlacement":
        _validate_placement_origin(self.candidate_id, self.authored_place, "dining")
        return self


class TransportPlacement(StrictModel):
    placement_kind: Literal["transport"] = "transport"
    candidate_id: Optional[str] = Field(default=None, min_length=1)
    authored_route: Optional[AuthoredRoute] = None

    @model_validator(mode="after")
    def validate_origin(self) -> "TransportPlacement":
        _validate_placement_origin(self.candidate_id, self.authored_route, "transport")
        return self


CompositionPlacement = Annotated[
    Union[VisitPlacement, DiningPlacement, TransportPlacement],
    Field(discriminator="placement_kind"),
]

PhysicalPlacement = Union[VisitPlacement, DiningPlacement]


def is_authored_placement(placement: CompositionPlacement) -> bool:
    if placement.placement_kind in {"visit", "dining"}:
        return placement.authored_place is not None
    if placement.placement_kind == "transport":
        return placement.authored_route is not None
    return False


def placement_identity(placement: CompositionPlacement) -> Optional[str]:
    """Return the key that makes one placement unique, or None when unbounded.

    Authored connectors are unbounded: the same short walk may legitimately
    appear between several different pairs of stops.
    """
    if placement.candidate_id:
        return f"candidate:{placement.candidate_id}"
    if placement.placement_kind == "transport":
        return None
    authored = placement.authored_place
    return f"authored:{authored.city}:{authored.name}"


class DayComposition(StrictModel):
    day_id: str = Field(min_length=1)
    day: int = Field(ge=1)
    date: date
    destination_id: str = Field(min_length=1)
    # No ``theme``: the Day's title is derived from its placements at
    # materialization (``entities/day_theme.py``).  It was free text the composing
    # model wrote with no guidance and nothing ever reconciled it against the Day.
    placements: list[CompositionPlacement] = Field(min_length=1)
    time_structure: list[Literal["morning", "lunch", "afternoon", "evening"]] = Field(
        default_factory=lambda: ["morning", "lunch", "afternoon", "evening"]
    )

    @model_validator(mode="after")
    def validate_time_structure(self) -> "DayComposition":
        if self.time_structure != ["morning", "lunch", "afternoon", "evening"]:
            raise ValueError("composition day must retain the four canonical time blocks")
        return self


class ItineraryCompositionDraft(StrictModel):
    itinerary_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    duration_days: int = Field(ge=1)
    days: list[DayComposition] = Field(min_length=1)
    lodging_candidate_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_days(self) -> "ItineraryCompositionDraft":
        if self.duration_days != len(self.days):
            raise ValueError("composition duration must equal day count")
        if [day.day for day in self.days] != list(range(1, len(self.days) + 1)):
            raise ValueError("composition days must be contiguous from one")
        ids = [day.day_id for day in self.days]
        if len(ids) != len(set(ids)):
            raise ValueError("composition day ids must be unique")
        physical_days: dict[str, list[str]] = {}
        for day in self.days:
            placement_keys = [
                key
                for placement in day.placements
                if (key := placement_identity(placement)) is not None
            ]
            if len(placement_keys) != len(set(placement_keys)):
                raise ValueError("an entry cannot be placed more than once in the same day")
            for placement in day.placements:
                if placement.placement_kind in {"visit", "dining"}:
                    physical_days.setdefault(placement_identity(placement), []).append(
                        day.day_id
                    )
                start = getattr(placement, "planned_start", None)
                end = getattr(placement, "planned_end", None)
                if (start is None) != (end is None):
                    raise ValueError("scheduled placement requires both start and end")
                if start is not None and (end <= start or start.date() != day.date or end.date() != day.date):
                    raise ValueError("placement schedule must be ordered within its local day")
            scheduled = [
                placement
                for placement in day.placements
                if placement.placement_kind in {"visit", "dining"}
                and getattr(placement, "planned_start", None) is not None
            ]
            transfer = timedelta(minutes=MIN_LOCAL_TRANSFER_MINUTES)
            for left, right in zip(scheduled, scheduled[1:]):
                if right.planned_start - left.planned_end < transfer:
                    raise ValueError(
                        "adjacent scheduled placements must follow itinerary order and "
                        f"leave a {MIN_LOCAL_TRANSFER_MINUTES}-minute transfer window"
                    )
        repeated_physical = {
            key: day_ids
            for key, day_ids in physical_days.items()
            if len(day_ids) > 1
        }
        if repeated_physical:
            raise ValueError(
                "a visit/dining entry cannot be placed more than once in one itinerary: "
                f"{repeated_physical}"
            )
        return self


class ItineraryCompositionError(ValueError):
    pass


FlexibleRouteMode = Literal["walk", "bike", "drive", "taxi", "ride_hailing"]
_FLEXIBLE_ROUTE_MODES = {"walk", "bike", "drive", "taxi", "ride_hailing"}


class LocalConnectorGap(StrictModel):
    """One exact missing route between adjacent physical skeleton placements."""

    gap_id: str = Field(min_length=1)
    day_id: str = Field(min_length=1)
    day_date: date
    destination_id: str = Field(min_length=1)
    # ``placement_identity`` of the two adjacent stops: ``candidate:{id}`` for an
    # admitted candidate, ``authored:{city}:{name}`` for an authored entry.
    from_entry_key: str = Field(min_length=1)
    from_place_id: str = Field(min_length=1)
    to_entry_key: str = Field(min_length=1)
    to_place_id: str = Field(min_length=1)
    departure_time: datetime
    latest_arrival_time: datetime
    allowed_transport_classes: list[
        Literal["public_transit", "flexible"]
    ] = Field(min_length=1, max_length=2)
    requested_flexible_modes: list[FlexibleRouteMode] = Field(default_factory=list)
    preferred_transport_class: Literal["public_transit", "flexible"]
    weather_data_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_gap(self) -> "LocalConnectorGap":
        if self.from_entry_key == self.to_entry_key:
            raise ValueError("connector gap requires two distinct entries")
        if self.from_place_id == self.to_place_id:
            raise ValueError("connector gap requires two distinct place ids")
        if len(self.allowed_transport_classes) != len(
            set(self.allowed_transport_classes)
        ):
            raise ValueError("connector gap transport classes must be unique")
        if self.preferred_transport_class not in self.allowed_transport_classes:
            raise ValueError("preferred connector class must be allowed")
        if (
            "flexible" in self.allowed_transport_classes
            and not self.requested_flexible_modes
        ):
            raise ValueError("flexible connector requires an explicit requested mode")
        if (
            "flexible" not in self.allowed_transport_classes
            and self.requested_flexible_modes
        ):
            raise ValueError("flexible modes require the flexible transport class")
        if self.departure_time.utcoffset() is None or self.latest_arrival_time.utcoffset() is None:
            raise ValueError("connector gap times must include the destination UTC offset")
        if (
            self.departure_time.date() != self.day_date
            or self.latest_arrival_time.date() != self.day_date
            or self.latest_arrival_time <= self.departure_time
        ):
            raise ValueError("connector gap window must be ordered within its Day")
        return self


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _lineage(
    candidate: ResearchCandidate,
    selection_slot_id: Optional[str] = None,
    *,
    additional_planning_decision_ids: Optional[list[str]] = None,
) -> EntityLineage:
    return EntityLineage(
        research_packet_id=candidate.research_packet_id,
        candidate_id=candidate.candidate_id,
        selection_slot_id=selection_slot_id,
        fact_assertion_ids=candidate.fact_assertion_ids,
        source_record_ids=candidate.source_record_ids,
        planning_decision_ids=list(
            dict.fromkeys(
                [
                    *candidate.planning_decision_ids,
                    *(additional_planning_decision_ids or []),
                ]
            )
        ),
        weather_impact_ids=candidate.weather_impact_ids,
        personalization_influence_ids=candidate.personalization_influence_ids,
    )


def _passed_candidates(catalog: RecommendationCatalog) -> dict[str, ResearchCandidate]:
    candidates = catalog.candidate_index()
    passed = {
        admission.candidate_id
        for admission in catalog.admission_results
        if admission.status == "passed"
    }
    # Iterate the catalog, not the membership set: option ``rank`` is projected to
    # the traveller from this order, and set iteration follows randomized string
    # hashes, which would make ranks 2 and 3 differ between processes.
    return {
        candidate_id: candidate
        for candidate_id, candidate in candidates.items()
        if candidate_id in passed
    }


def _authored_lineage() -> EntityLineage:
    """The only lineage an authored entry may carry."""
    return EntityLineage(lineage_kind="authored_entity")


def _physical_place_id(candidate: ResearchCandidate) -> str:
    if isinstance(candidate, (VisitCandidate, DiningCandidate)):
        return candidate.place_id
    raise ItineraryCompositionError(
        "physical itinerary placement references another candidate kind"
    )


def _located_authored_place(placement: PhysicalPlacement) -> AuthoredPlaceBase:
    authored = placement.authored_place
    if not authored.is_located:
        raise ItineraryCompositionError(
            f"authored entry 「{authored.name}」 has no resolved map location"
        )
    return authored


def placement_place_id(
    placement: PhysicalPlacement,
    candidates: Mapping[str, ResearchCandidate],
) -> str:
    """Return the map-resolvable place id behind one physical placement."""
    if placement.candidate_id:
        candidate = candidates.get(placement.candidate_id)
        if candidate is None:
            raise ItineraryCompositionError(
                f"placement references an unadmitted candidate: {placement.candidate_id}"
            )
        return _physical_place_id(candidate)
    return _located_authored_place(placement).resolved_place_id


def _topology_place_id(
    placement: PhysicalPlacement,
    candidates: Mapping[str, ResearchCandidate],
) -> str:
    if placement.candidate_id:
        candidate = candidates.get(placement.candidate_id)
        return _physical_place_id(candidate) if candidate else ""
    return _located_authored_place(placement).resolved_place_id


def placement_display_name(
    placement: PhysicalPlacement,
    candidates: Mapping[str, ResearchCandidate],
) -> str:
    if placement.candidate_id:
        candidate = candidates[placement.candidate_id]
        if isinstance(candidate, VisitCandidate):
            return candidate.name
        if isinstance(candidate, DiningCandidate):
            return candidate.branch_name
        raise ItineraryCompositionError(
            "physical itinerary placement references another candidate kind"
        )
    return placement.authored_place.name


@dataclass(frozen=True)
class AuthoredPlaceRef:
    """One authored place together with the Day context that resolves it."""

    place: AuthoredPlaceBase
    destination_id: str
    kind: str


def authored_places(composition: ItineraryCompositionDraft) -> list[AuthoredPlaceRef]:
    """List every authored place in itinerary order, with its Day context."""
    return [
        AuthoredPlaceRef(
            place=placement.authored_place,
            destination_id=day.destination_id,
            kind=placement.placement_kind,
        )
        for day in composition.days
        for placement in day.placements
        if placement.placement_kind in {"visit", "dining"}
        and placement.authored_place is not None
    ]


def authored_place_key(place: AuthoredPlaceBase) -> str:
    return f"authored:{place.city}:{place.name}"


def map_authored_places(
    composition: ItineraryCompositionDraft,
    replace: Callable[[AuthoredPlaceBase], AuthoredPlaceBase],
) -> ItineraryCompositionDraft:
    """Rebuild the composition with every authored place passed through ``replace``."""
    days: list[DayComposition] = []
    for day in composition.days:
        placements: list[CompositionPlacement] = []
        for placement in day.placements:
            if (
                placement.placement_kind in {"visit", "dining"}
                and placement.authored_place is not None
            ):
                updated = replace(placement.authored_place)
                if updated is not placement.authored_place:
                    placement = placement.model_copy(
                        update={"authored_place": updated}
                    )
            placements.append(placement)
        days.append(day.model_copy(update={"placements": placements}))
    return composition.model_copy(update={"days": days})


def drop_authored_places(
    composition: ItineraryCompositionDraft,
    unwanted: Callable[[AuthoredPlaceBase], bool],
) -> ItineraryCompositionDraft:
    """Rebuild the composition without the authored stops ``unwanted`` selects.

    An entry no provider can place on the map cannot be delivered, and the day
    carrying one fewer stop is a smaller loss than the run carrying none.  A day
    left with no placements at all is not an itinerary, so that fails instead.
    """
    days: list[DayComposition] = []
    for day in composition.days:
        placements = [
            placement
            for placement in day.placements
            if not (
                placement.placement_kind in {"visit", "dining"}
                and placement.authored_place is not None
                and unwanted(placement.authored_place)
            )
        ]
        if not placements:
            raise ItineraryCompositionError(
                f"day {day.day_id} has no placement left once unlocatable "
                "authored entries are dropped"
            )
        days.append(day.model_copy(update={"placements": placements}))
    return composition.model_copy(update={"days": days})


def drop_placements_the_traveller_has_already_left(
    composition: ItineraryCompositionDraft,
    catalog: RecommendationCatalog,
) -> ItineraryCompositionDraft:
    """Drop a local stop that begins after the traveller has already left the city.

    ``validate_long_distance_anchor_day_order`` refuses a day whose local stop ends
    after the departing anchor departs, and that refusal costs a whole composition
    repair round — it is the most frequent single way this shape fails.

    **The boundary between what may be normalized and what must still fail is the
    whole point of this function**, and it is the same boundary drawn for empty
    transport entries:

    * ``planned_start >= departure_at`` — the stop lies **entirely** after the
      train left.  It cannot happen at all; there is nothing inside it to preserve
      and dropping it invents nothing.  Deterministic, so it is dropped here.
    * ``planned_start < departure_at < planned_end`` — the stop **straddles** the
      departure.  Trimming it would author a new ``planned_end`` the model never
      wrote (turning "an error" into "a wrong itinerary" is worse
      than the error), and keeping it asserts the traveller is in two places at
      once.  Genuinely ambiguous, so it still fails the gate.

    **Why this cannot live with the payload normalizers** in
    ``itinerary_planner.parse_itinerary_composition``: those run on the raw,
    not-yet-validated dict, and ``departure_at`` is not in that dict — it belongs to
    the *admitted* ``TransportCandidate`` and is only reachable through the catalog.
    Written there, the rule would select nothing and read like a guard that isn't
    one.

    A day left with no placement at all is not an itinerary, so that fails instead —
    same rule as :func:`drop_authored_places` right above.
    """
    index = catalog.candidate_index()
    days: list[DayComposition] = []
    changed = False
    for day in composition.days:
        departures = [
            candidate.departure_at
            for placement in day.placements
            if placement.placement_kind == "transport" and placement.candidate_id
            for candidate in [index.get(placement.candidate_id)]
            if isinstance(candidate, TransportCandidate)
            and candidate.transport_class == "long_distance"
            and candidate.departure_at is not None
            and long_distance_anchor_day_role(candidate, day, composition.days)
            == "departs"
        ]
        if not departures:
            days.append(day)
            continue
        # The earliest departure is the one that ends the traveller's time here.
        departure_at = min(departures)
        placements = [
            placement
            for placement in day.placements
            if not (
                placement.placement_kind in {"visit", "dining"}
                and placement.planned_start is not None
                and placement.planned_start >= departure_at
            )
        ]
        if len(placements) != len(day.placements):
            changed = True
        if not placements:
            raise ItineraryCompositionError(
                f"day {day.day_id} has no placement left once stops beginning after "
                f"the traveller leaves {departure_at.isoformat()} are dropped"
            )
        days.append(day.model_copy(update={"placements": placements}))
    return composition.model_copy(update={"days": days}) if changed else composition


def long_distance_anchor_day_role(
    candidate: TransportCandidate,
    day: DayComposition,
    days: Sequence[DayComposition],
) -> Literal["arrives", "departs"]:
    """Whether this Day's anchor brings the traveller *to* its destination or *out* of it.

    A long-distance leg is not a busy window — it **moves** the traveller between
    two cities.  Before it departs they are at ``from_endpoint``; after it arrives
    they are at ``to_endpoint``.  Only one of those two is in the Day's own
    destination, and which one decides the only side of the anchor on which a
    local stop can physically happen.

    Two signals answer it, in order of how directly they state it:

    - A **cross-night** leg states its side by date.  The Day that is its arrival
      date is the Day the traveller lands on; the Day that is its departure date
      is the Day they leave on.
    - A **same-day** leg is on one Day at both ends, so the direction comes from
      where that Day sits in the stay: the first Day at a destination is reached
      by arriving there, any later one is left by departing.  This is exactly the
      round trip ``build_required_long_distance_legs`` derives — outbound on the
      start date, return on the end date — read off the skeleton itself instead
      of re-deriving it from the trip identity.
    """

    assert candidate.departure_at is not None and candidate.arrival_at is not None
    departure_date = candidate.departure_at.date()
    arrival_date = candidate.arrival_at.date()
    if departure_date != arrival_date:
        return "arrives" if day.date == arrival_date else "departs"
    first_date_at_destination = min(
        other.date for other in days if other.destination_id == day.destination_id
    )
    return "arrives" if day.date == first_date_at_destination else "departs"


def validate_long_distance_anchor_day_order(
    day: DayComposition,
    anchors: Sequence[tuple[int, TransportCandidate]],
    physical_placements: Sequence[tuple[int, PhysicalPlacement]],
    days: Sequence[DayComposition],
) -> None:
    """A stop on a travel day sits on exactly one side of the anchor.

    A travel day **may** carry local stops.  Banning them outright makes a two-day
    trip impossible to compose: both of its days carry an anchor, so no day would be
    allowed to hold the dining and visit that ``required_candidate_kinds`` demands
    somewhere.

    What such a ban protects is real, though — a stop cannot happen while the
    traveller is on the train.  So the day is allowed, and a local stop sharing a day
    with an anchor must say *when* it happens, which is why
    ``planned_start``/``planned_end`` are not optional here.

    **Non-overlap is not enough.**  A window is the wrong model of a long-distance
    leg: the anchor **moves the traveller between two cities**, so a stop in this
    Day's destination is possible on exactly one side of it — after arriving, or
    before departing, never both.  Checking overlap alone accepts a Shenzhen museum
    at 10:00 and a Shenzhen lunch at 12:30 on a Day whose only anchor is the
    15:00→21:35 Shanghai→Shenzhen train: nothing overlaps, and the traveller is
    1,200 km away for all of it.

    Placement *order* is checked in the same breath, because it is not decoration:
    ``day_connector_adjacencies`` reads the anchor's direction off it — an anchor
    listed after a stop is taken to be one the traveller boards, so it is entered at
    ``from_endpoint``.  List an arriving train last and the connector owed becomes
    「蘩楼(华强北总店) → 上海虹桥」, to be filled with an 80-minute metro across the
    country.  Order and direction have to agree or every downstream reader inherits
    the contradiction.

    This lives on its own because the skeleton is **not the only shape that can break
    it**.  Written inline in ``validate_placement_skeleton``, it would miss the
    composition-repair pass (``recompose``, which rewrites a whole composition once a
    workspace already exists) — that path validates only transport topology, so it
    could still deliver a Shenzhen lunch before the Shenzhen train arrived with
    nothing to stop it.  One rule, one implementation, every path that produces a
    composition.
    """

    if len(anchors) > 2:
        raise ItineraryCompositionError(
            f"day {day.day_id} can carry at most two long-distance anchors "
            "(one that arrives and one that departs, never more)"
        )
    if len(anchors) == 1:
        anchor_position, anchor = anchors[0]
        role = long_distance_anchor_day_role(anchor, day, days)
        arrival_at = anchor.arrival_at
        departure_at = anchor.departure_at
        assert arrival_at is not None and departure_at is not None
        for position, placement in physical_placements:
            if placement.planned_start is None or placement.planned_end is None:
                raise ItineraryCompositionError(
                    f"day {day.day_id} shares a long-distance anchor, so every "
                    f"local placement must carry planned_start and planned_end"
                )
            # Both messages name the instant to move past and the side to move to:
            # they are quoted verbatim into the composition repair prompt, and
            # "does not fit" is not something a model can act on.
            if role == "arrives":
                if position < anchor_position:
                    raise ItineraryCompositionError(
                        f"day {day.day_id} lists a local stop before the "
                        f"long-distance anchor that arrives at this destination; "
                        f"the anchor must be this day's first placement and every "
                        f"stop must follow it"
                    )
                if placement.planned_start < arrival_at:
                    raise ItineraryCompositionError(
                        f"day {day.day_id} local placement starts "
                        f"{placement.planned_start.isoformat()}, before the "
                        f"traveller arrives at this destination "
                        f"{arrival_at.isoformat()}; move it later or to another day"
                    )
            elif position > anchor_position:
                raise ItineraryCompositionError(
                    f"day {day.day_id} lists a local stop after the long-distance "
                    f"anchor that departs from this destination; the anchor must be "
                    f"this day's last placement and every stop must precede it"
                )
            elif placement.planned_end > departure_at:
                raise ItineraryCompositionError(
                    f"day {day.day_id} local placement ends "
                    f"{placement.planned_end.isoformat()}, after the traveller "
                    f"leaves this destination {departure_at.isoformat()}; move it "
                    f"earlier or to another day"
                )
        return
    # Same-day two-long-distance-leg day (E-04 same-day round trip, or the last
    # handover day of a multi-destination run where the return shares it): the
    # first anchor **arrives** at this destination, the last one **departs** from
    # it, and every local stop sits strictly between them — after the arrive
    # anchor's arrival and before the depart anchor's departure.  This is the
    # same anti-overlap rule as the single-anchor branch, applied to the only
    # two sides that can physically hold a stop.
    pos_arrive, anchor_in = anchors[0]
    pos_depart, anchor_out = anchors[1]
    if not (pos_arrive < pos_depart):
        raise ItineraryCompositionError(
            f"day {day.day_id} with two long-distance anchors must list the one "
            f"that arrives first and the one that departs last"
        )
    arr_at = anchor_in.arrival_at
    dep_at = anchor_out.departure_at
    assert arr_at is not None and dep_at is not None
    for position, placement in physical_placements:
        if placement.planned_start is None or placement.planned_end is None:
            raise ItineraryCompositionError(
                f"day {day.day_id} carries two long-distance anchors, so every "
                f"local placement must carry planned_start and planned_end"
            )
        if not (pos_arrive < position < pos_depart):
            raise ItineraryCompositionError(
                f"day {day.day_id} keeps a local stop outside the corridor "
                f"between the arriving anchor and the departing anchor; "
                f"the arrive anchor must be first, the depart anchor last, "
                f"and every stop between them"
            )
        if placement.planned_start < arr_at:
            raise ItineraryCompositionError(
                f"day {day.day_id} local placement starts "
                f"{placement.planned_start.isoformat()}, before the traveller "
                f"arrives on {arr_at.isoformat()}; move it later"
            )
        if placement.planned_end > dep_at:
            raise ItineraryCompositionError(
                f"day {day.day_id} local placement ends "
                f"{placement.planned_end.isoformat()}, after the traveller "
                f"leaves on {dep_at.isoformat()}; move it earlier"
            )


def validate_placement_skeleton(
    skeleton: ItineraryCompositionDraft,
    catalog: RecommendationCatalog,
    *,
    required_candidate_kinds: Optional[set[str]] = None,
    required_long_distance_legs: Optional[
        list["ProviderRouteLegScope"]
    ] = None,
    required_leg_scope_ids: Optional[dict[str, str]] = None,
) -> None:
    """Validate physical ordering before any local connector research exists."""
    candidates = _passed_candidates(catalog)
    expected_dates = [
        skeleton.days[0].date + timedelta(days=index)
        for index in range(len(skeleton.days))
    ]
    if [day.date for day in skeleton.days] != expected_dates:
        raise ItineraryCompositionError(
            "placement skeleton Day dates must be contiguous"
        )
    day_dates = {day.date for day in skeleton.days}
    admitted_long_distance = {
        candidate_id
        for candidate_id, candidate in candidates.items()
        if isinstance(candidate, TransportCandidate)
        and candidate.transport_class == "long_distance"
    }
    placed_long_distance: set[str] = set()
    placed_physical_kinds: set[str] = set()
    for lodging_id in skeleton.lodging_candidate_ids:
        if not isinstance(candidates.get(lodging_id), LodgingCandidate):
            raise ItineraryCompositionError(
                "placement skeleton lodging must reference an admitted LodgingCandidate"
            )
    for day in skeleton.days:
        physical_count = 0
        long_distance_count = 0
        # Positions within ``day.placements``, because the order is not decoration:
        # ``day_connector_adjacencies`` reads the anchor's direction off it.
        physical_placements: list[tuple[int, PhysicalPlacement]] = []
        anchors: list[tuple[int, TransportCandidate]] = []
        for position, placement in enumerate(day.placements):
            if is_authored_placement(placement):
                if placement.placement_kind == "transport":
                    raise ItineraryCompositionError(
                        "placement skeleton cannot contain a local transport connector"
                    )
                physical_count += 1
                physical_placements.append((position, placement))
                placed_physical_kinds.add(placement.placement_kind)
                continue
            candidate = candidates.get(placement.candidate_id)
            if placement.placement_kind == "visit":
                if not isinstance(candidate, VisitCandidate):
                    raise ItineraryCompositionError(
                        "placement skeleton visit references another candidate kind"
                    )
                physical_count += 1
                physical_placements.append((position, placement))
                placed_physical_kinds.add("visit")
            elif placement.placement_kind == "dining":
                if not isinstance(candidate, DiningCandidate):
                    raise ItineraryCompositionError(
                        "placement skeleton dining references another candidate kind"
                    )
                # The meal domain is checked *here*, next to its sibling, and not
                # only where the workspace is materialized.  Checking it only there,
                # one phase later, lets the skeleton clear this gate with a lunch on a
                # dinner-only branch — and the round that then fails re-authors local
                # connectors and can never reach a meal type, so the repair budget
                # burns on rewriting the same mistake and the whole run fails.
                # Failing at the skeleton hands it to the one round that *can* fix it
                # — which is why the message names the branch and the meals it does
                # serve rather than just saying no: it is quoted verbatim into that
                # repair prompt.
                if placement.meal_type not in candidate.meal_types:
                    raise ItineraryCompositionError(
                        "placement skeleton dining places a meal the candidate does "
                        f"not serve: {placement.candidate_id} serves "
                        f"{sorted(candidate.meal_types)}, placed as {placement.meal_type}"
                    )
                physical_count += 1
                physical_placements.append((position, placement))
                placed_physical_kinds.add("dining")
            else:
                if not isinstance(candidate, TransportCandidate):
                    raise ItineraryCompositionError(
                        "placement skeleton transport references another candidate kind"
                    )
                if candidate.transport_class != "long_distance":
                    raise ItineraryCompositionError(
                        "placement skeleton cannot contain a local transport candidate"
                    )
                if candidate.departure_at is None or candidate.arrival_at is None:
                    raise ItineraryCompositionError(
                        "long-distance skeleton anchor requires provider departure and arrival times"
                    )
                projection_shape = classify_transport_projection_shape(
                    transport_class=candidate.transport_class,
                    departure_at=candidate.departure_at,
                    arrival_at=candidate.arrival_at,
                    itinerary_dates=day_dates,
                )
                if projection_shape == "outside_itinerary":
                    raise ItineraryCompositionError(
                        "long-distance anchor service dates fall outside the itinerary"
                    )
                service_dates = {
                    candidate.departure_at.date(),
                    candidate.arrival_at.date(),
                }
                if day.date not in service_dates:
                    raise ItineraryCompositionError(
                        f"day {day.day_id} long-distance skeleton placement does not match provider date"
                    )
                long_distance_count += 1
                placed_long_distance.add(candidate.candidate_id)
                anchors.append((position, candidate))
            if candidate is not None and candidate.destination_id != day.destination_id:
                raise ItineraryCompositionError(
                    f"day {day.day_id} placement belongs to another destination"
                )
        if long_distance_count > 2:
            raise ItineraryCompositionError(
                f"day {day.day_id} can select only up to two long-distance "
                f"options (one arriving and one departing)"
            )
        if physical_count and anchors:
            validate_long_distance_anchor_day_order(
                day, anchors, physical_placements, skeleton.days
            )
    if admitted_long_distance and not (admitted_long_distance & placed_long_distance):
        raise ItineraryCompositionError(
            "placement skeleton must select one admitted long-distance anchor"
        )
    # Multi-destination handover legs are authoritative: every required
    # long-distance leg (outbound, each inter_destination move, return) owes a
    # travel day on its handover service_date.  A model that drops the interior
    # 杭州→苏州 move would otherwise leave it researched-but-undelivered.
    # A same-day two-leg date (E-04 round trip, or a handover day that also
    # carries the return) must verify *each* leg is placed, not just that the
    # date is covered once: the date-only view lets the outbound satisfy the
    # return's service_date and the return never reaches the itinerary.
    if required_long_distance_legs:
        placed_service_dates: set[date] = set()
        placed_scope_ids: set[str] = set()
        for cid in placed_long_distance:
            c = candidates.get(cid)
            if isinstance(c, TransportCandidate):
                if c.provider_evidence_scope_id:
                    placed_scope_ids.add(c.provider_evidence_scope_id)
                if c.departure_at is not None:
                    placed_service_dates.add(c.departure_at.date())
                    if c.arrival_at is not None:
                        placed_service_dates.add(c.arrival_at.date())
        missing_legs: list[str] = []
        for leg in required_long_distance_legs:
            leg_key = f"{leg.leg_role}@{leg.service_date.isoformat()}"
            leg_scope_id = (
                required_leg_scope_ids.get(leg_key)
                if required_leg_scope_ids is not None
                else None
            )
            if leg_scope_id is not None:
                if leg_scope_id not in placed_scope_ids:
                    missing_legs.append(leg_key)
            elif leg.service_date not in placed_service_dates:
                missing_legs.append(leg_key)
        if missing_legs:
            raise ItineraryCompositionError(
                "placement skeleton omits long-distance legs owed on handover "
                f"dates: {', '.join(missing_legs)}"
            )
    missing_required = set(required_candidate_kinds or set()) - placed_physical_kinds
    if missing_required:
        raise ItineraryCompositionError(
            "placement skeleton omits required physical candidate kinds: "
            f"{sorted(missing_required)}"
        )


def _long_distance_anchor(
    placement: CompositionPlacement,
    candidates: Mapping[str, ResearchCandidate],
) -> Optional[TransportCandidate]:
    """The admitted long-distance leg this placement selects, if it is one."""

    if placement.placement_kind != "transport" or not placement.candidate_id:
        return None
    candidate = candidates.get(placement.candidate_id)
    if (
        isinstance(candidate, TransportCandidate)
        and candidate.transport_class == "long_distance"
        and candidate.departure_at is not None
        and candidate.arrival_at is not None
    ):
        return candidate
    return None


def day_chain_node_pairs(
    day: DayComposition,
) -> list[tuple[CompositionPlacement, CompositionPlacement]]:
    """The adjacent pairs on one Day that owe a local connector, in placement order.

    A Day's *chain nodes* are its physical stops **and** its long-distance anchor.
    On a placement skeleton the only legal transport placement is that anchor —
    ``validate_placement_skeleton`` rejects a local connector outright — so the
    nodes can be read off ``placement_kind`` alone, with no catalog.

    Two anchors on one Day is already a skeleton contract error, so such a pair is
    not offered a connector here.

    One definition, two readers: this one and
    :func:`day_connector_adjacencies`, which adds the endpoints and times.
    """

    nodes = [
        placement
        for placement in day.placements
        if placement.placement_kind in {"visit", "dining", "transport"}
    ]
    return [
        (left, right)
        for left, right in zip(nodes, nodes[1:])
        if not (
            left.placement_kind == "transport" and right.placement_kind == "transport"
        )
    ]


def day_connector_adjacencies(
    day: DayComposition,
    candidates: Mapping[str, ResearchCandidate],
) -> list[tuple[CompositionPlacement, CompositionPlacement, str, str, datetime, datetime]]:
    """Every adjacency on one Day that owes a local connector, with its endpoints.

    A Day's *chain nodes* are its physical stops **and** its long-distance anchor,
    because a traveller has to get from the last stop to the platform just as much
    as from one stop to the next.  The skeleton carries no connectors, so each of
    these adjacencies is a gap for the materialization pass to fill.

    **Anchors must not be left out.**  A Day carrying one is not travel-only: a
    two-day return trip has an anchor on both Days, so the stops must share a Day
    with one, and the hop between them is a real journey somebody has to plan.
    Leaving it out makes ``validate_itinerary_transport_topology`` report "transport
    chain origin does not match the preceding stop" — the leg departs from a station,
    the stop before it is a temple, and the missing connector between them reads as a
    contradiction rather than as work to do.

    The endpoint that faces the stop is the one that matters: a departing leg is
    entered at its ``from_endpoint``, an arriving one is left at its
    ``to_endpoint``.  Times come from the provider on the anchor's side and from
    the composition on the stop's side.
    """

    adjacencies: list[
        tuple[CompositionPlacement, CompositionPlacement, str, str, datetime, datetime]
    ] = []
    for left, right in day_chain_node_pairs(day):
        left_anchor = _long_distance_anchor(left, candidates)
        right_anchor = _long_distance_anchor(right, candidates)
        if left_anchor is not None:
            from_place_id = left_anchor.to_endpoint.place_id
            departure_time = left_anchor.arrival_at
        else:
            from_place_id = placement_place_id(left, candidates)
            departure_time = getattr(left, "planned_end", None)
        if right_anchor is not None:
            to_place_id = right_anchor.from_endpoint.place_id
            latest_arrival_time = right_anchor.departure_at
        else:
            to_place_id = placement_place_id(right, candidates)
            latest_arrival_time = getattr(right, "planned_start", None)
        if departure_time is None or latest_arrival_time is None:
            raise ItineraryCompositionError(
                f"day {day.day_id} adjacent skeleton placements require scheduled times"
            )
        if not from_place_id or not to_place_id:
            raise ItineraryCompositionError(
                f"day {day.day_id} adjacency requires stable endpoint place ids"
            )
        if from_place_id == to_place_id and (
            left_anchor is not None or right_anchor is not None
        ):
            # The stop is at the station — dinner in the departure hall before
            # boarding.  There is no journey to route, so there is no gap, and the
            # chain check is already satisfied because the two endpoints agree.
            # Two *stops* sharing one place id stays an error: that is a modelling
            # mistake, not a real shape.
            continue
        adjacencies.append(
            (left, right, from_place_id, to_place_id, departure_time, latest_arrival_time)
        )
    return adjacencies


def extract_local_connector_gaps(
    skeleton: ItineraryCompositionDraft,
    catalog: RecommendationCatalog,
    *,
    weather_data_revision: int,
    flexible_mode_requests: Optional[
        dict[tuple[str, str], list[FlexibleRouteMode]]
    ] = None,
    required_flexible_mode_pairs: Optional[set[tuple[str, str]]] = None,
    required_candidate_kinds: Optional[set[str]] = None,
) -> list[LocalConnectorGap]:
    """Derive exact ordered adjacency gaps from a validated placement skeleton."""
    validate_placement_skeleton(
        skeleton,
        catalog,
        required_candidate_kinds=required_candidate_kinds,
    )
    candidates = _passed_candidates(catalog)
    requests = flexible_mode_requests or {}
    required_pairs = required_flexible_mode_pairs or set()
    gaps: list[LocalConnectorGap] = []
    for day in skeleton.days:
        for (
            left,
            right,
            from_place_id,
            to_place_id,
            departure_time,
            latest_arrival_time,
        ) in day_connector_adjacencies(day, candidates):
            left_key = placement_identity(left)
            right_key = placement_identity(right)
            if latest_arrival_time - departure_time < timedelta(
                minutes=MIN_LOCAL_TRANSFER_MINUTES
            ):
                # Name the pair and its window: an unordered or back-to-back
                # chain is the one composition defect a stack trace cannot show.
                raise ItineraryCompositionError(
                    f"day {day.day_id} connector gap {left_key} -> {right_key} leaves "
                    f"{(latest_arrival_time - departure_time).total_seconds() / 60:.0f}min "
                    f"between {departure_time.isoformat()} and "
                    f"{latest_arrival_time.isoformat()}; "
                    f"{MIN_LOCAL_TRANSFER_MINUTES}min required"
                )
            requested_modes = list(
                dict.fromkeys(requests.get((left_key, right_key), []))
            )
            pair = (left_key, right_key)
            flexible_required = pair in required_pairs
            if flexible_required and not requested_modes:
                raise ItineraryCompositionError(
                    f"day {day.day_id} requires an explicit flexible mode"
                )
            allowed_classes: list[Literal["public_transit", "flexible"]] = (
                [] if flexible_required else ["public_transit"]
            )
            if requested_modes:
                allowed_classes.append("flexible")
            gap_id = _stable_id(
                "connector_gap",
                day.day_id,
                left_key,
                right_key,
            )
            gaps.append(
                LocalConnectorGap(
                    gap_id=gap_id,
                    day_id=day.day_id,
                    day_date=day.date,
                    destination_id=day.destination_id,
                    from_entry_key=left_key,
                    from_place_id=from_place_id,
                    to_entry_key=right_key,
                    to_place_id=to_place_id,
                    departure_time=departure_time,
                    latest_arrival_time=latest_arrival_time,
                    allowed_transport_classes=allowed_classes,
                    requested_flexible_modes=requested_modes,
                    preferred_transport_class=(
                        "flexible" if requested_modes else "public_transit"
                    ),
                    weather_data_revision=weather_data_revision,
                )
            )
    return gaps


def connector_mode_requests_from_constraint_pack(
    skeleton: ItineraryCompositionDraft,
    constraint_pack: Mapping[str, Any] | None,
) -> tuple[
    dict[tuple[str, str], list[FlexibleRouteMode]],
    set[tuple[str, str]],
]:
    """Project normalized explicit user intent onto actual skeleton adjacency only."""
    actual_pairs: list[tuple[str, str]] = []
    for day in skeleton.days:
        actual_pairs.extend(
            (placement_identity(left), placement_identity(right))
            for left, right in day_chain_node_pairs(day)
        )
    if not actual_pairs or not isinstance(constraint_pack, Mapping):
        return {}, set()
    items = [
        item
        for key in ("soft_preferences", "hard_constraints")
        for item in constraint_pack.get(key) or []
        if isinstance(item, Mapping)
        and item.get("status") == "active"
        and item.get("visibility") == "user_visible"
        and item.get("category") == "transport_constraint"
        # 来源白名单。``manual_memory``（「个性记忆」里手写的那几条）**刻意不在**里面：
        # 那一层进 prompt 的身份是「用户可见的偏好」，不产生新的硬性要求。一条
        # 「我只坐出租车」若能从这里投影出 required connector pair，就会连锁出
        # gap → 定向补研 → 顶掉 ``workflows/run_deadline.py`` 那份写死的墙钟预算。
        # 它在 pack 里落在 ``other`` 这一档（``panels/constraint.py::
        # _map_manual_memory_facts``），所以今天连 category 这一关都过不来 ——
        # 这两道闸都不该被拆：拆掉任何一道，来源白名单就会放它进来。
        and any(
            isinstance(ref, Mapping)
            and ref.get("source") in {"memory_fact", "manual_profile", "session_anchor", "current_query"}
            for ref in item.get("source_refs") or []
        )
    ]
    # 这是**第二张**来源优先级表（第一张在 ``panels/constraint.py::
    # _SINGULAR_SOURCE_PRECEDENCE``）。两张表管的不是同一件事 —— 那张裁单值类冲突，
    # 这张定这里的应用顺序（大者后应用、后应用者胜）—— 但它们对「谁比谁更算数」
    # 必须给同一个答案，共有来源上的相对次序不许在这两处互相打架。
    source_priority = {
        "memory_fact": 0,
        "manual_profile": 1,
        "session_anchor": 2,
        "current_query": 3,
    }
    def item_source_priority(item: Mapping[str, Any]) -> int:
        return max(
            (
                source_priority.get(str(ref.get("source")), -1)
                for ref in item.get("source_refs") or []
                if isinstance(ref, Mapping)
            ),
            default=-1,
        )

    items.sort(key=lambda item: (item_source_priority(item), str(item.get("updated_at") or "")))
    requested: dict[tuple[str, str], list[FlexibleRouteMode]] = {}
    required: set[tuple[str, str]] = set()
    locked_by_pair: dict[tuple[str, str], str] = {}
    excluded: dict[tuple[str, str], set[str]] = {
        pair: set() for pair in actual_pairs
    }
    for item in items:
        params = item.get("params")
        if not isinstance(params, Mapping):
            continue
        raw_pair = (
            str(params.get("from_candidate_id") or ""),
            str(params.get("to_candidate_id") or ""),
        )
        explicit_pair = tuple(f"candidate:{value}" if value else "" for value in raw_pair)
        target_pairs = (
            [explicit_pair]
            if all(explicit_pair) and explicit_pair in actual_pairs
            else actual_pairs
            if not any(explicit_pair)
            else []
        )
        raw_preferred_modes = params.get("preferred_local_modes")
        if not isinstance(raw_preferred_modes, list):
            raw_preferred_modes = []
        raw_excluded_modes = params.get("excluded_local_modes")
        if not isinstance(raw_excluded_modes, list):
            raw_excluded_modes = []
        preferred_modes = [
            mode
            for mode in raw_preferred_modes
            if mode in _FLEXIBLE_ROUTE_MODES
        ]
        excluded_modes = {
            mode
            for mode in raw_excluded_modes
            if mode in _FLEXIBLE_ROUTE_MODES
        }
        locked_mode = str(params.get("locked_local_mode") or "")
        if locked_mode not in _FLEXIBLE_ROUTE_MODES:
            locked_mode = ""
        for pair in target_pairs:
            if excluded_modes:
                excluded[pair].update(excluded_modes)
                requested[pair] = [
                    mode
                    for mode in requested.get(pair, [])
                    if mode not in excluded[pair]
                ]
                if locked_by_pair.get(pair) in excluded[pair]:
                    required.discard(pair)
                    locked_by_pair.pop(pair, None)
            if locked_mode and locked_mode not in excluded[pair]:
                requested[pair] = [locked_mode]
                required.add(pair)
                locked_by_pair[pair] = locked_mode
            elif pair not in required:
                requested[pair] = list(
                    dict.fromkeys(
                        [
                            *[
                                mode
                                for mode in preferred_modes
                                if mode not in excluded[pair]
                            ],
                            *requested.get(pair, []),
                        ]
                    )
                )
    return (
        {pair: modes for pair, modes in requested.items() if modes},
        required,
    )


def connector_route_arrival(
    candidate: TransportCandidate,
    gap: LocalConnectorGap,
) -> datetime:
    """When the traveller reaches the gap's far end if this route is taken.

    One reading for both upstreams.  A local connector's load-bearing fact is its
    **duration**: leaving the preceding stop the moment it ends, the traveller
    arrives that many minutes later.  A Provider that also publishes a timetable
    (MOTIS does) pins the clock exactly, so that clock wins when it is there —
    but its absence is not missing data, it is how mainland-China transit answers
    (amap: 41 minutes, ¥4, 地铁1号线, no clock at all).
    """

    if candidate.arrival_at is not None:
        return candidate.arrival_at
    return gap.departure_time + timedelta(minutes=candidate.duration_minutes)


def connector_candidate_quality_error(
    candidate: TransportCandidate,
    gap: LocalConnectorGap,
) -> Optional[str]:
    """Return a deterministic rejection reason for a route that cannot fill a gap."""
    if candidate.transport_class not in gap.allowed_transport_classes:
        return "transport_class_not_requested"
    if candidate.destination_id != gap.destination_id:
        return "destination_mismatch"
    if (
        candidate.from_endpoint.place_id != gap.from_place_id
        or candidate.to_endpoint.place_id != gap.to_place_id
    ):
        return "endpoint_mismatch"
    if candidate.transport_class == "flexible" and candidate.selected_mode.value not in set(
        gap.requested_flexible_modes
    ):
        return "flexible_mode_not_requested"
    # Route-intrinsic plausibility comes before anything about *this* gap: the
    # duration is what the window check below is computed from, so an implausible
    # one has to be named as implausible rather than surfacing as "arrives late"
    # — the aligner acts on that second code and would otherwise try to shift the
    # rest of the Day by a nonsense delay.
    if candidate.duration_minutes <= 0 or candidate.duration_minutes > 360:
        return "local_route_duration_implausible"
    if candidate.distance_meters is not None:
        if candidate.distance_meters <= 0 or candidate.distance_meters > 150_000:
            return "local_route_distance_implausible"
        speed_kmh = candidate.distance_meters / 1000 / (
            candidate.duration_minutes / 60
        )
        speed_limits = {
            "walk": 12,
            "bike": 45,
            "drive": 180,
            "taxi": 180,
            "ride_hailing": 180,
        }
        limit = speed_limits.get(candidate.selected_mode.value, 220)
        if speed_kmh > limit:
            return "local_route_speed_implausible"
    # A timetable is a *strengthening* of a local route, never what makes it usable.
    # **Do not demand one here.**  Harvest and admission accept duration-only mainland
    # routes, so amap's 41-minute 地铁1号线 is an admitted candidate; rejecting it
    # ``route_schedule_missing`` at this one remaining reader leaves the gap counted as
    # unfilled and sends the connector back to being authored by the model.
    # One contract, three readers — this is the third.
    #
    # Half a timetable is still rejected, and hard: a route carrying one clock and
    # not the other is neither shape, and picking whichever field happens to be
    # present is exactly the silent-fallback reading this contract forbids.
    if (candidate.departure_at is None) != (candidate.arrival_at is None):
        return "route_half_scheduled"
    if candidate.departure_at is not None and candidate.arrival_at is not None:
        if candidate.departure_at.utcoffset() is None or candidate.arrival_at.utcoffset() is None:
            return "route_timezone_missing"
        if (
            candidate.departure_at.date() != gap.day_date
            or candidate.arrival_at.date() != gap.day_date
            or candidate.departure_at.utcoffset() != gap.departure_time.utcoffset()
            or candidate.arrival_at.utcoffset() != gap.latest_arrival_time.utcoffset()
        ):
            return "route_day_or_timezone_mismatch"
        if candidate.departure_at < gap.departure_time:
            return "route_departs_before_preceding_stop"
    # Same code for both shapes, because it is the same journey fact: this route
    # reaches the far end after the following stop was due to start.
    # ``align_skeleton_to_provider_routes`` reads that code to decide whether the
    # tentative downstream times may move, so splitting it in two would have hidden
    # the duration-only half from the aligner.
    if connector_route_arrival(candidate, gap) > gap.latest_arrival_time:
        return "route_arrives_after_following_stop"
    return None


def align_skeleton_to_provider_routes(
    skeleton: ItineraryCompositionDraft,
    catalog: RecommendationCatalog,
    gaps: list[LocalConnectorGap],
) -> ItineraryCompositionDraft:
    """Shift tentative downstream times once an exact Provider route is known.

    The skeleton is pre-delivery planning state, not a user mutation.  Only a
    passed route whose sole topology error is arriving after the tentative next
    stop may move that stop and the remaining scheduled placements.  One shift
    per call keeps downstream connector gaps subject to a fresh deterministic
    extraction and Provider check.
    """
    candidates = _passed_candidates(catalog)
    routes = [
        candidate
        for candidate in candidates.values()
        if isinstance(candidate, TransportCandidate)
    ]
    for gap in gaps:
        options = [
            candidate
            for candidate in routes
            if connector_candidate_quality_error(candidate, gap)
            == "route_arrives_after_following_stop"
        ]
        if not options:
            continue
        options.sort(
            key=lambda candidate: (
                connector_route_arrival(candidate, gap),
                candidate.duration_minutes,
                candidate.candidate_id,
            )
        )
        route = options[0]
        route_arrival = connector_route_arrival(route, gap)
        updated_days: list[DayComposition] = []
        shifted = False
        for day in skeleton.days:
            if day.day_id != gap.day_id:
                updated_days.append(day)
                continue
            placements = list(day.placements)
            right_index = next(
                (
                    index
                    for index, placement in enumerate(placements)
                    if placement.placement_kind in {"visit", "dining"}
                    and placement_identity(placement) == gap.to_entry_key
                ),
                None,
            )
            if right_index is None:
                updated_days.append(day)
                continue
            right_start = getattr(placements[right_index], "planned_start", None)
            if right_start is None or route_arrival <= right_start:
                updated_days.append(day)
                continue
            delay = route_arrival - right_start
            for index in range(right_index, len(placements)):
                placement = placements[index]
                if placement.placement_kind not in {"visit", "dining"}:
                    continue
                start = getattr(placement, "planned_start", None)
                end = getattr(placement, "planned_end", None)
                if start is None or end is None:
                    continue
                shifted_start = start + delay
                shifted_end = end + delay
                if shifted_start.date() != day.date or shifted_end.date() != day.date:
                    return skeleton
                placements[index] = placement.model_copy(
                    update={
                        "planned_start": shifted_start,
                        "planned_end": shifted_end,
                    }
                )
            updated_days.append(day.model_copy(update={"placements": placements}))
            shifted = True
        if shifted:
            aligned = skeleton.model_copy(update={"days": updated_days})
            validate_placement_skeleton(aligned, catalog)
            return aligned
    return skeleton


def connector_gap_rejection_reasons(
    catalog: RecommendationCatalog,
    gap: LocalConnectorGap,
) -> dict[str, str]:
    """Why each admitted local route failed to fill this one adjacency.

    ``connector_candidate_quality_error`` returns a precise reason that must reach
    the surface: otherwise a Provider route could be measured, admitted, and then
    silently lose its gap, and the delivered itinerary — a model-invented duration —
    would look exactly like the case where no Provider answered at all.
    """

    reasons: dict[str, str] = {}
    for candidate_id, candidate in _passed_candidates(catalog).items():
        if not isinstance(candidate, TransportCandidate):
            continue
        if candidate.transport_class == "long_distance":
            continue
        reason = connector_candidate_quality_error(candidate, gap)
        if reason is not None:
            reasons[candidate_id] = reason
    return reasons


def unfilled_connector_gaps(
    catalog: RecommendationCatalog,
    gaps: list[LocalConnectorGap],
) -> list[LocalConnectorGap]:
    """Return the adjacencies no admitted Provider route can currently fill."""
    candidates = _passed_candidates(catalog)
    unfilled: list[LocalConnectorGap] = []
    for gap in gaps:
        if any(
            isinstance(candidate, TransportCandidate)
            and connector_candidate_quality_error(candidate, gap) is None
            for candidate in candidates.values()
        ):
            continue
        unfilled.append(gap)
        reasons = connector_gap_rejection_reasons(catalog, gap)
        logger.info(
            "[connector] gap %s (%s -> %s) unfilled; local route verdicts: %s",
            gap.gap_id,
            gap.from_place_id,
            gap.to_place_id,
            reasons or "no admitted local route at all",
        )
    return unfilled


def materialize_skeleton_connectors(
    skeleton: ItineraryCompositionDraft,
    catalog: RecommendationCatalog,
    gaps: list[LocalConnectorGap],
    authored_routes: Mapping[str, AuthoredRoute],
) -> ItineraryCompositionDraft:
    """Insert one connector per skeleton adjacency.

    A quality-passed Provider route always wins.  Where the catalog has none,
    the adjacency takes the authored route supplied for that gap id.
    """
    validate_placement_skeleton(skeleton, catalog)
    candidates = _passed_candidates(catalog)
    gaps_by_pair = {
        (gap.day_id, gap.from_entry_key, gap.to_entry_key): gap
        for gap in gaps
    }
    if len(gaps_by_pair) != len(gaps):
        raise ItineraryCompositionError("connector gaps must be unique per Day adjacency")
    selected: dict[str, TransportCandidate | AuthoredRoute] = {}
    for gap in gaps:
        options = [
            candidate
            for candidate in candidates.values()
            if isinstance(candidate, TransportCandidate)
            and connector_candidate_quality_error(candidate, gap) is None
        ]
        if not options:
            authored = authored_routes.get(gap.gap_id)
            if authored is None:
                raise ItineraryCompositionError(
                    f"connector gap {gap.gap_id} has neither a Provider route nor an authored route"
                )
            selected[gap.gap_id] = authored
            continue
        options.sort(
            key=lambda candidate: (
                candidate.transport_class != gap.preferred_transport_class,
                candidate.duration_minutes,
                candidate.total_cost_cny is None,
                candidate.total_cost_cny or 0,
                candidate.candidate_id,
            )
        )
        selected[gap.gap_id] = options[0]

    updated_days: list[DayComposition] = []
    consumed: set[str] = set()
    for day in skeleton.days:
        # The connector goes in front of the node it leads to, which is what makes
        # a stop→anchor hop land between the stop and the platform rather than
        # after the leg.
        successor = {
            id(left): right
            for left, right, *_endpoints in day_connector_adjacencies(day, candidates)
        }
        updated: list[CompositionPlacement] = []
        for placement in day.placements:
            updated.append(placement)
            next_node = successor.get(id(placement))
            if next_node is None:
                continue
            key = (
                day.day_id,
                placement_identity(placement),
                placement_identity(next_node),
            )
            gap = gaps_by_pair.get(key)
            if gap is None:
                raise ItineraryCompositionError(
                    f"day {day.day_id} adjacency has no connector gap"
                )
            route = selected[gap.gap_id]
            updated.append(
                TransportPlacement(authored_route=route)
                if isinstance(route, AuthoredRoute)
                else TransportPlacement(candidate_id=route.candidate_id)
            )
            consumed.add(gap.gap_id)
        updated_days.append(day.model_copy(update={"placements": updated}))
    if consumed != set(selected):
        raise ItineraryCompositionError("connector gaps do not match the skeleton")
    composition = skeleton.model_copy(update={"days": updated_days})
    validate_itinerary_transport_topology(composition, catalog)
    return composition


def _validate_transport_chain(
    *,
    day_id: str,
    candidates: dict[str, ResearchCandidate],
    placements: list[CompositionPlacement],
    start_place_id: Optional[str],
    end_place_id: Optional[str],
    require_local_connector: bool,
) -> None:
    if not placements:
        if require_local_connector:
            raise ItineraryCompositionError(
                f"day {day_id} adjacent physical stops require a local transport connector"
            )
        return

    if any(is_authored_placement(placement) for placement in placements):
        # An authored connector is defined by the two stops it sits between, so
        # it is legal only as the single route inside that adjacency.
        if len(placements) != 1 or not require_local_connector:
            raise ItineraryCompositionError(
                f"day {day_id} authored connector must be the only route between two adjacent stops"
            )
        return

    transport_candidates: list[TransportCandidate] = []
    for placement in placements:
        if placement.placement_kind != "transport":
            raise ItineraryCompositionError(
                f"day {day_id} transport chain contains a non-transport placement"
            )
        candidate = candidates.get(placement.candidate_id)
        if not isinstance(candidate, TransportCandidate):
            raise ItineraryCompositionError(
                "transport placement references another candidate kind"
            )
        if not candidate.from_endpoint.place_id or not candidate.to_endpoint.place_id:
            raise ItineraryCompositionError(
                f"day {day_id} transport chain requires stable endpoint place ids"
            )
        transport_candidates.append(candidate)

    if require_local_connector and not any(
        candidate.transport_class in {"public_transit", "flexible"}
        for candidate in transport_candidates
    ):
        raise ItineraryCompositionError(
            f"day {day_id} long-distance transport cannot replace a local connector"
        )
    if (
        start_place_id is not None
        and transport_candidates[0].from_endpoint.place_id != start_place_id
    ):
        raise ItineraryCompositionError(
            f"day {day_id} transport chain origin does not match the preceding stop"
        )
    for previous, following in zip(transport_candidates, transport_candidates[1:]):
        if previous.to_endpoint.place_id != following.from_endpoint.place_id:
            raise ItineraryCompositionError(
                f"day {day_id} transport chain endpoints are not continuous"
            )
    if (
        end_place_id is not None
        and transport_candidates[-1].to_endpoint.place_id != end_place_id
    ):
        raise ItineraryCompositionError(
            f"day {day_id} transport chain destination does not match the following stop"
        )


def validate_itinerary_transport_topology(
    composition: ItineraryCompositionDraft,
    catalog: RecommendationCatalog,
) -> None:
    """Require executable typed transport chains around physical itinerary stops.

    This also re-checks the travel-day rule (a stop sits on exactly one side of
    the long-distance anchor).  Every path that produces a composition ends up
    here, which the skeleton validator cannot claim: the composition-repair pass
    rewrites a whole composition and only ever called this function, so the rule
    had a door standing open on it.
    """
    candidates = _passed_candidates(catalog)
    for day in composition.days:
        physical_placements: list[tuple[int, PhysicalPlacement]] = []
        anchors: list[tuple[int, TransportCandidate]] = []
        for position, placement in enumerate(day.placements):
            if placement.placement_kind in {"visit", "dining"}:
                physical_placements.append((position, placement))
                continue
            anchor = _long_distance_anchor(placement, candidates)
            if anchor is not None:
                anchors.append((position, anchor))
        if physical_placements and anchors:
            validate_long_distance_anchor_day_order(
                day, anchors, physical_placements, composition.days
            )
    admitted_long_distance_ids = {
        candidate_id
        for candidate_id, candidate in candidates.items()
        if isinstance(candidate, TransportCandidate)
        and candidate.transport_class == "long_distance"
    }
    placed_transport_ids = {
        placement.candidate_id
        for day in composition.days
        for placement in day.placements
        if placement.placement_kind == "transport" and placement.candidate_id
    }
    if admitted_long_distance_ids and not (
        admitted_long_distance_ids & placed_transport_ids
    ):
        raise ItineraryCompositionError(
            "itinerary must include one admitted long-distance transport anchor"
        )

    long_distance_service_groups: dict[
        tuple[str, Optional[date], Optional[date]], list[str]
    ] = {}
    for day in composition.days:
        for placement in day.placements:
            if placement.placement_kind != "transport":
                continue
            candidate = candidates.get(placement.candidate_id)
            if not isinstance(candidate, TransportCandidate):
                continue
            if candidate.transport_class != "long_distance":
                continue
            departure_date = (
                candidate.departure_at.date()
                if candidate.departure_at is not None
                else None
            )
            arrival_date = (
                candidate.arrival_at.date()
                if candidate.arrival_at is not None
                else None
            )
            service_dates = {departure_date, arrival_date} - {None}
            if not service_dates:
                raise ItineraryCompositionError(
                    "long-distance transport requires a provider-bound service date"
                )
            if day.date not in service_dates:
                raise ItineraryCompositionError(
                    f"day {day.day_id} long-distance transport date does not match "
                    "its provider schedule"
                )
            group = (
                candidate.from_endpoint.place_id,
                candidate.to_endpoint.place_id,
                departure_date,
                arrival_date,
            )
            long_distance_service_groups.setdefault(group, []).append(
                candidate.candidate_id
            )
    competing_groups = {
        group: candidate_ids
        for group, candidate_ids in long_distance_service_groups.items()
        if len(candidate_ids) > 1
    }
    if competing_groups:
        raise ItineraryCompositionError(
            "itinerary can select only one long-distance option per service window: "
            f"{competing_groups}"
        )

    for day in composition.days:
        # Chain nodes are the Day's physical stops **and** its long-distance anchor —
        # the same definition ``day_chain_node_pairs`` uses to derive connector gaps.
        # The two must not diverge: deriving gaps between stops only, while this
        # validator requires a long-distance leg to start where the preceding stop
        # ended, is unsatisfiable on a mixed Day — a leg departs from a station and a
        # stop is a temple, so the hop between them (a real journey) reads as a
        # contradiction instead of as work to do.  One definition, or they disagree.
        node_indexes = [
            index
            for index, placement in enumerate(day.placements)
            if placement.placement_kind in {"visit", "dining"}
            or _long_distance_anchor(placement, candidates) is not None
        ]
        if not node_indexes:
            if any(
                placement.placement_kind == "transport"
                and (
                    placement.authored_route is not None
                    or (
                        isinstance(
                            candidates.get(placement.candidate_id), TransportCandidate
                        )
                        and candidates[placement.candidate_id].transport_class
                        != "long_distance"
                    )
                )
                for placement in day.placements
            ):
                raise ItineraryCompositionError(
                    f"day {day.day_id} local transport requires a physical itinerary stop"
                )
            _validate_transport_chain(
                day_id=day.day_id,
                candidates=candidates,
                placements=list(day.placements),
                start_place_id=None,
                end_place_id=None,
                require_local_connector=False,
            )
            continue

        if not any(
            day.placements[index].placement_kind in {"visit", "dining"}
            for index in node_indexes
        ) and any(
            placement.placement_kind == "transport"
            and (
                placement.authored_route is not None
                or (
                    isinstance(
                        candidates.get(placement.candidate_id), TransportCandidate
                    )
                    and candidates[placement.candidate_id].transport_class
                    != "long_distance"
                )
            )
            for placement in day.placements
        ):
            raise ItineraryCompositionError(
                f"day {day.day_id} local transport requires a physical itinerary stop"
            )

        def _inbound(index: int) -> str:
            """Where the traveller arrives at this node."""
            placement = day.placements[index]
            anchor = _long_distance_anchor(placement, candidates)
            if anchor is not None:
                return anchor.from_endpoint.place_id
            return _topology_place_id(placement, candidates)

        def _outbound(index: int) -> str:
            """Where the traveller leaves this node from."""
            placement = day.placements[index]
            anchor = _long_distance_anchor(placement, candidates)
            if anchor is not None:
                return anchor.to_endpoint.place_id
            return _topology_place_id(placement, candidates)

        first_index = node_indexes[0]
        _validate_transport_chain(
            day_id=day.day_id,
            candidates=candidates,
            placements=list(day.placements[:first_index]),
            start_place_id=None,
            end_place_id=_inbound(first_index),
            require_local_connector=False,
        )

        for left_index, right_index in zip(node_indexes, node_indexes[1:]):
            left_placement = day.placements[left_index]
            right_placement = day.placements[right_index]
            left_is_anchor = (
                _long_distance_anchor(left_placement, candidates) is not None
            )
            right_is_anchor = (
                _long_distance_anchor(right_placement, candidates) is not None
            )
            if not left_is_anchor and not isinstance(
                left_placement, (VisitPlacement, DiningPlacement)
            ):
                raise ItineraryCompositionError(
                    f"day {day.day_id} physical topology references an invalid placement"
                )
            if not right_is_anchor and not isinstance(
                right_placement, (VisitPlacement, DiningPlacement)
            ):
                raise ItineraryCompositionError(
                    f"day {day.day_id} physical topology references an invalid placement"
                )
            start_place_id = _outbound(left_index)
            end_place_id = _inbound(right_index)
            between = day.placements[left_index + 1 : right_index]
            _validate_transport_chain(
                day_id=day.day_id,
                candidates=candidates,
                placements=list(between),
                start_place_id=start_place_id,
                end_place_id=end_place_id,
                # A stop standing at the station it boards from is not a journey,
                # so it owes no connector; every other adjacency does.
                require_local_connector=start_place_id != end_place_id,
            )

        last_index = node_indexes[-1]
        _validate_transport_chain(
            day_id=day.day_id,
            candidates=candidates,
            placements=list(day.placements[last_index + 1 :]),
            start_place_id=_outbound(last_index),
            end_place_id=None,
            require_local_connector=False,
        )


def _authored_transport_leg(
    *,
    day: DayComposition,
    index: int,
    candidates: Mapping[str, ResearchCandidate],
) -> TransportLeg:
    """Build the leg for an authored connector from the nodes it sits between.

    Both endpoints come from the adjacent chain nodes, so the composition supplies
    only the mode and the door-to-door minutes.

    A node is a physical stop **or** a long-distance anchor: the hop from the
    platform to the first stop of the day is an authored connector as ordinary as
    any other, and reading only stops made it "a connector with no stop on one
    side".  The endpoint facing the connector is the one taken —
    you leave an arriving leg at its ``to_endpoint`` and enter a departing one at
    its ``from_endpoint``.
    """
    placement = day.placements[index]
    node_kinds = {"visit", "dining"}

    def _node(items: list[CompositionPlacement]) -> Optional[CompositionPlacement]:
        for item in items:
            if item.placement_kind in node_kinds:
                return item
            if _long_distance_anchor(item, candidates) is not None:
                return item
        return None

    previous = _node(list(reversed(day.placements[:index])))
    following = _node(list(day.placements[index + 1 :]))
    if previous is None or following is None:
        raise ItineraryCompositionError(
            f"day {day.day_id} authored connector needs a stop on both sides"
        )
    route = placement.authored_route

    def _endpoint(node: CompositionPlacement, *, leaving: bool) -> TransportEndpoint:
        anchor = _long_distance_anchor(node, candidates)
        if anchor is not None:
            return anchor.to_endpoint if leaving else anchor.from_endpoint
        return TransportEndpoint(
            name=placement_display_name(node, candidates),
            place_id=placement_place_id(node, candidates),
        )

    from_endpoint = _endpoint(previous, leaving=True)
    to_endpoint = _endpoint(following, leaving=False)
    leg_id = _stable_id(
        "transport",
        day.day_id,
        from_endpoint.place_id,
        to_endpoint.place_id,
        route.mode,
    )
    return TransportLeg(
        transport_leg_id=leg_id,
        transport_class=route.transport_class,
        selected_mode=TransportMode(route.mode),
        from_endpoint=from_endpoint,
        to_endpoint=to_endpoint,
        duration_minutes=route.duration_minutes,
        transfer_count=0,
        segments=[
            TransportSegment(
                segment_id=_stable_id("segment", leg_id),
                mode=TransportMode(route.mode),
                from_endpoint=from_endpoint,
                to_endpoint=to_endpoint,
                duration_minutes=route.duration_minutes,
            )
        ],
        booking_status="not_required",
        route_status="ready",
        lineage=_authored_lineage(),
    )


def _option(
    candidate: LodgingCandidate | DiningCandidate | VisitCandidate | TransportCandidate,
    *,
    slot_id: str,
    target_entity_id: str,
    rank: int,
) -> SelectionOption:
    entity_type = SELECTION_SLOT_ENTITY_TYPES[candidate.candidate_kind]
    availability_status = candidate_option_availability(candidate)
    return SelectionOption(
        option_id=_stable_id("option", slot_id, candidate.candidate_id),
        selection_slot_id=slot_id,
        candidate_id=candidate.candidate_id,
        candidate_entity_ref=EntityRef(entity_type=entity_type, entity_id=target_entity_id),
        rank=rank,
        selection_reasons=candidate.selection_reasons,
        tradeoff=candidate.tradeoff,
        comparison_facts=candidate.field_paths,
        availability_status=availability_status,
        fact_assertion_ids=candidate.fact_assertion_ids,
        source_record_ids=candidate.source_record_ids,
        personalization_influence_ids=candidate.personalization_influence_ids,
    )


def _slot(
    *,
    selected: LodgingCandidate | DiningCandidate | VisitCandidate | TransportCandidate,
    eligible: Sequence[LodgingCandidate | DiningCandidate | VisitCandidate | TransportCandidate],
    slot_id: str,
    target_entity_id: str,
    slot_type: SelectionSlotType,
    context: dict[str, object],
) -> SelectionSlot:
    ordered = [selected, *[item for item in eligible if item.candidate_id != selected.candidate_id]][:3]
    options = [
        _option(item, slot_id=slot_id, target_entity_id=target_entity_id, rank=index)
        for index, item in enumerate(ordered, 1)
    ]
    selected_option_id = options[0].option_id
    return SelectionSlot(
        selection_slot_id=slot_id,
        slot_type=slot_type,
        target_entity_id=target_entity_id,
        context=context,
        options=options,
        recommended_option_id=selected_option_id,
        selected_option_id=selected_option_id,
        status="ready",
    )


def _transport_alternatives(
    leg: TransportLeg,
    candidates: Mapping[str, ResearchCandidate],
) -> list[TransportCandidate]:
    """Every admitted candidate that could lawfully stand in for this leg.

    The filter is the mutation's own admission rule, restated on the offering
    side so a slot never shows an option that ``_check_transport_replacement``
    would then refuse: a long-distance leg may only be replaced by another long-distance
    service, and a local connector must join the *same ordered* pair of places —
    a route between two other points is a different journey, not an alternative.
    The mode preference is checked too, because a locked or excluded mode makes a
    candidate unusable no matter how well it fits.
    """

    preference = leg.mode_preference
    eligible: list[TransportCandidate] = []
    for candidate in candidates.values():
        if not isinstance(candidate, TransportCandidate):
            continue
        if preference.locked_mode is not None and candidate.selected_mode != preference.locked_mode:
            continue
        if candidate.selected_mode in preference.excluded_modes:
            continue
        if leg.transport_class == "long_distance":
            if candidate.transport_class != "long_distance":
                continue
        elif candidate.transport_class not in {"public_transit", "flexible"}:
            continue
        elif not (
            leg.from_endpoint.place_id is not None
            and leg.to_endpoint.place_id is not None
            and candidate.from_endpoint.place_id == leg.from_endpoint.place_id
            and candidate.to_endpoint.place_id == leg.to_endpoint.place_id
        ):
            continue
        eligible.append(candidate)
    return eligible


def _transport_slots(
    legs: Sequence[TransportLeg],
    candidates: Mapping[str, ResearchCandidate],
) -> tuple[list[SelectionSlot], list[TransportLeg]]:
    """One slot per candidate-backed leg, plus the legs stamped with their slot id.

    Only candidate-backed legs get one.  An authored connector has no admitted
    candidate standing in it, so there is no option to select and nothing for the
    slot to be about — the same rule the visit slots follow.

    The legs come back rewritten because ``TripWorkspaceV2`` requires the entity a
    slot points at to name that slot in its own lineage; a slot whose target
    stayed silent about it is rejected outright.  Returning both keeps that pairing
    in one place instead of leaving a caller to remember the second half.
    """

    slots: list[SelectionSlot] = []
    stamped: list[TransportLeg] = []
    for leg in legs:
        candidate_id = leg.lineage.candidate_id
        selected = candidates.get(candidate_id) if candidate_id else None
        if leg.lineage.lineage_kind != "candidate_entity" or not isinstance(
            selected, TransportCandidate
        ):
            stamped.append(leg)
            continue
        # The leg id already names one journey in the plan, so it is the slot's
        # identity too — unlike a visit, a leg is not one of several
        # interchangeable positions on a Day.
        slot_id = _stable_id("slot_transport", leg.transport_leg_id)
        slots.append(
            _slot(
                selected=selected,
                eligible=_transport_alternatives(leg, candidates),
                slot_id=slot_id,
                target_entity_id=leg.transport_leg_id,
                slot_type="transport",
                context={
                    "transport_class": leg.transport_class,
                    "from_endpoint": leg.from_endpoint.name,
                    "to_endpoint": leg.to_endpoint.name,
                },
            )
        )
        stamped.append(
            leg.model_copy(
                update={
                    "lineage": leg.lineage.model_copy(
                        update={"selection_slot_id": slot_id}
                    )
                }
            )
        )
    return slots, stamped


def _reference_service_notes(
    reference_services: list[ProviderReferenceService],
    *,
    transport_legs: list[TransportLeg],
    day_plans: list[DayPlanV2],
) -> list[str]:
    """State a reference service only where the plan has no real path for it.

    A reference service stands in for a responsibility the provider could not answer
    *for the traveller's date*.  Once a real leg exists on that date — a flight, or
    rail once the date comes inside the window — the reference is no longer the
    reader's best information about it, and printing both reads as two competing
    answers to one question.  Delivery counts by Day, the same way the report's gap
    note does, so the two never disagree.
    """

    if not reference_services:
        return []
    long_distance_ids = {
        leg.transport_leg_id
        for leg in transport_legs
        if leg.transport_class == "long_distance"
    }
    dates_with_long_distance = {
        day.date
        for day in day_plans
        if any(
            entry.entity_type == EntityType.TRANSPORT_LEG
            and entry.entity_id in long_distance_ids
            for entry in day.timeline
        )
    }
    return [
        reference_service_note(service)
        for service in reference_services
        if service.requested_date not in dates_with_long_distance
    ]


def _destination_nights(
    composition: "ItineraryCompositionDraft", destination_id: str
) -> tuple[list[date], list[date]]:
    """(nights this destination holds, nights some *other* destination holds).

    A night is indexed by the date the traveller goes to sleep, so the trip's last
    Day is nobody's night.  Both lists come from ``day_plans`` because the Day
    scaffold **is** the authority on which night belongs to which city -- deriving
    it a second time from the controlled identity would be the same number with
    two owners.
    """

    dates = sorted({day.date for day in composition.days})
    if not dates:
        return [], []
    trip_last = dates[-1]
    mine: list[date] = []
    others: list[date] = []
    for day in composition.days:
        if day.date == trip_last:
            continue
        (mine if day.destination_id == destination_id else others).append(day.date)
    return sorted(mine), sorted(others)


def _destination_stay_window(
    composition: "ItineraryCompositionDraft",
    destination_id: str,
    check_in: date,
    check_out: date,
) -> Optional[tuple[date, date]]:
    """The nights this itinerary actually spends in one destination.

    A lodging candidate authors its own ``check_in_date``/``check_out_date``, and
    on a multi-destination trip the composing model has written them across the
    **whole trip** — one property in the first city covering nights the traveller
    spends in the second.

    Visit / dining / transport placements are already checked against
    ``day.destination_id`` (see the placement sweep above); lodging was exempt
    because it is not a Day placement but a flat, Day-spanning list — so no layer
    owned the question "is this bed in the city we sleep in tonight".  This is
    that layer, and the answer comes from the composition itself rather than from
    a second copy of the trip's day allocation: ``day_plans`` **is** the authority
    on which night belongs to which city.

    Returns ``None`` when the destination has no night at all (the traveller only
    passes through on the trip's final day).  A stay is then dropped rather than
    projected onto another city's nights, and the resulting uncovered night is
    reported by ``delivery_quality_gate``'s ``missing_lodging_night`` gap — a
    coverage disclosure the traveller can act on, instead of a bed 128 km away.

    **It only moves the end when the authored window covers another city's night.**
    An end that merely runs past the itinerary keeps the date the candidate wrote,
    so the long-standing "a stay booked around the itinerary edge has no projection
    on that end" behaviour is untouched -- pulling that end inward would stamp a
    check-out onto a Day the traveller arrives on by overnight transport, which
    manufactures exactly the unroutable adjacency this gate forbids.
    """

    mine, others = _destination_nights(composition, destination_id)
    if not mine:
        return None
    authored: list[date] = []
    cursor = check_in
    while cursor < check_out:
        authored.append(cursor)
        cursor += timedelta(days=1)
    mine_set, others_set = set(mine), set(others)
    kept = [night for night in authored if night in mine_set] or mine
    start = kept[0]
    last_owed = kept[-1] + timedelta(days=1)
    if any(night in others_set for night in authored):
        return start, last_owed
    # Nothing crosses another city: keep whichever end reaches further, so an
    # itinerary-edge booking keeps the candidate's own check-out date.
    return start, max(check_out, last_owed)


def materialize_trip_workspace(
    *,
    run_id: str,
    workspace_revision: int,
    composition: ItineraryCompositionDraft,
    catalog: RecommendationCatalog,
    user_input_anchors: Optional[list[UserInputAnchor]] = None,
    reference_services: Optional[list[ProviderReferenceService]] = None,
) -> TripWorkspaceV2:
    candidates = _passed_candidates(catalog)
    used_ids = {
        placement.candidate_id
        for day in composition.days
        for placement in day.placements
        if placement.candidate_id
    } | set(composition.lodging_candidate_ids)
    missing = used_ids - set(candidates)
    if missing:
        raise ItineraryCompositionError(
            f"composition references candidates that did not pass admission: {sorted(missing)}"
        )
    validate_itinerary_transport_topology(composition, catalog)

    visits: list[VisitStop] = []
    dining: list[DiningStop] = []
    lodging: list[LodgingStay] = []
    transport: list[TransportLeg] = []
    day_plans: list[DayPlanV2] = []
    composition_day_dates = {day.date for day in composition.days}
    deferred_transport_projections: dict[date, list[TimelineEntryRef]] = {}
    slots: list[SelectionSlot] = []
    destination_names = _controlled_destination_names(user_input_anchors)
    # Every visit candidate this composition already stands a stop on, collected
    # before the loop because a Day's alternatives must exclude stops placed on
    # *later* Days too.  A visit alternative that is already in the plan is not an
    # alternative: swapping to it would put the same place in the itinerary twice,
    # which ``validate_days`` rejects outright.  Dining does not need this — a
    # meal slot's alternatives are restaurants for that meal, and the same
    # restaurant standing on another Day is a legal thing to offer.
    placed_visit_candidate_ids = {
        placement.candidate_id
        for day in composition.days
        for placement in day.placements
        if placement.candidate_id and placement.placement_kind == "visit"
    }
    for day in composition.days:
        timeline: list[TimelineEntryRef] = []
        # Where this Day's own entities start, so the theme can be derived from
        # exactly what this Day holds.
        day_visit_start = len(visits)
        day_dining_start = len(dining)
        day_transport_start = len(transport)
        for placement_index, placement in enumerate(day.placements):
            if is_authored_placement(placement):
                if placement.placement_kind == "transport":
                    leg = _authored_transport_leg(
                        day=day,
                        index=placement_index,
                        candidates=candidates,
                    )
                    transport.append(leg)
                    timeline.append(
                        TimelineEntryRef(
                            entry_id=_stable_id("entry", leg.transport_leg_id),
                            entity_type=EntityType.TRANSPORT_LEG,
                            entity_id=leg.transport_leg_id,
                        )
                    )
                    continue
                authored = _located_authored_place(placement)
                if placement.placement_kind == "visit":
                    item_id = _stable_id("visit", day.day_id, authored.resolved_place_id)
                    visits.append(
                        VisitStop(
                            item_id=item_id,
                            day_id=day.day_id,
                            place_id=authored.resolved_place_id,
                            name=authored.name,
                            address=authored.resolved_address or authored.address,
                            latitude=authored.resolved_latitude,
                            longitude=authored.resolved_longitude,
                            planned_start=placement.planned_start,
                            planned_end=placement.planned_end,
                            duration_minutes=placement.duration_minutes,
                            selection_reason=authored.selection_reason,
                            lineage=_authored_lineage(),
                            visit_type=authored.visit_type,
                            visit_highlights=list(authored.highlights),
                        )
                    )
                    timeline.append(
                        TimelineEntryRef(
                            entry_id=_stable_id("entry", item_id),
                            entity_type=EntityType.VISIT_STOP,
                            entity_id=item_id,
                        )
                    )
                else:
                    item_id = _stable_id(
                        "dining",
                        day.day_id,
                        placement.meal_type,
                        authored.resolved_place_id,
                    )
                    dining.append(
                        DiningStop(
                            item_id=item_id,
                            day_id=day.day_id,
                            place_id=authored.resolved_place_id,
                            name=authored.name,
                            address=authored.resolved_address or authored.address,
                            latitude=authored.resolved_latitude,
                            longitude=authored.resolved_longitude,
                            planned_start=placement.planned_start,
                            planned_end=placement.planned_end,
                            duration_minutes=placement.duration_minutes,
                            selection_reason=authored.selection_reason,
                            lineage=_authored_lineage(),
                            meal_type=placement.meal_type,
                            cuisine_types=list(authored.cuisine_types),
                            recommended_dishes=list(authored.recommended_dishes),
                        )
                    )
                    timeline.append(
                        TimelineEntryRef(
                            entry_id=_stable_id("entry", item_id),
                            entity_type=EntityType.DINING_STOP,
                            entity_id=item_id,
                        )
                    )
                continue
            candidate = candidates[placement.candidate_id]
            if placement.placement_kind == "visit":
                if not isinstance(candidate, VisitCandidate):
                    raise ItineraryCompositionError("visit placement references another candidate kind")
                item_id = _stable_id("visit", day.day_id, candidate.candidate_id)
                # The slot id names the *place in the plan*, not the candidate
                # standing in it, so swapping the candidate leaves the slot
                # identity alone — the same reason a dining slot keys on
                # ``(day_id, meal_type)``.  Here the plan position is the Day plus
                # the placement index.
                slot_id = _stable_id("slot_visit", day.day_id, str(placement_index))
                eligible = [
                    item
                    for item in candidates.values()
                    if isinstance(item, VisitCandidate)
                    and item.destination_id == day.destination_id
                    and item.candidate_id not in placed_visit_candidate_ids
                ]
                slots.append(
                    _slot(
                        selected=candidate,
                        eligible=eligible,
                        slot_id=slot_id,
                        target_entity_id=item_id,
                        slot_type="visit",
                        context={
                            "day_id": day.day_id,
                            "destination_id": day.destination_id,
                            "visit_type": candidate.visit_type,
                        },
                    )
                )
                visits.append(
                    VisitStop(
                        item_id=item_id,
                        day_id=day.day_id,
                        place_id=candidate.place_id,
                        name=candidate.name,
                        address=candidate.address,
                        planned_start=placement.planned_start,
                        planned_end=placement.planned_end,
                        duration_minutes=placement.duration_minutes,
                        estimated_cost_cny=candidate.estimated_cost_cny,
                        selection_reason=candidate.selection_reasons[0],
                        lineage=_lineage(candidate, slot_id),
                        visit_type=candidate.visit_type,
                        opening_window=candidate.opening_window,
                        reservation_required=candidate.reservation_required,
                        visit_highlights=candidate.highlights,
                    )
                )
                timeline.append(TimelineEntryRef(entry_id=_stable_id("entry", item_id), entity_type=EntityType.VISIT_STOP, entity_id=item_id))
            elif placement.placement_kind == "dining":
                if not isinstance(candidate, DiningCandidate) or placement.meal_type not in candidate.meal_types:
                    raise ItineraryCompositionError("dining placement does not match candidate meal domain")
                item_id = _stable_id("dining", day.day_id, placement.meal_type, candidate.candidate_id)
                slot_id = _stable_id("slot_dining", day.day_id, placement.meal_type)
                eligible = [
                    item for item in candidates.values()
                    if isinstance(item, DiningCandidate)
                    and item.destination_id == day.destination_id
                    and placement.meal_type in item.meal_types
                ]
                slots.append(_slot(selected=candidate, eligible=eligible, slot_id=slot_id, target_entity_id=item_id, slot_type="dining", context={"day_id": day.day_id, "meal_type": placement.meal_type, "destination_id": day.destination_id}))
                dining.append(
                    DiningStop(
                        item_id=item_id,
                        day_id=day.day_id,
                        place_id=candidate.place_id,
                        name=candidate.branch_name,
                        address=candidate.address,
                        planned_start=placement.planned_start,
                        planned_end=placement.planned_end,
                        duration_minutes=placement.duration_minutes,
                        estimated_cost_cny=candidate.average_spend_cny,
                        selection_reason=candidate.selection_reasons[0],
                        lineage=_lineage(candidate, slot_id),
                        meal_type=placement.meal_type,
                        cuisine_types=candidate.cuisine_types,
                        average_spend_cny=candidate.average_spend_cny,
                        recommended_dishes=candidate.recommended_dishes,
                        reservation_required=candidate.reservation_required,
                        opening_window=candidate.opening_window,
                    )
                )
                timeline.append(TimelineEntryRef(entry_id=_stable_id("entry", item_id), entity_type=EntityType.DINING_STOP, entity_id=item_id))
            else:
                if not isinstance(candidate, TransportCandidate):
                    raise ItineraryCompositionError("transport placement references another candidate kind")
                leg_id = (
                    _stable_id("transport", candidate.candidate_id)
                    if candidate.transport_class == "long_distance"
                    else _stable_id("transport", day.day_id, candidate.candidate_id)
                )
                transport.append(
                    TransportLeg(
                        transport_leg_id=leg_id,
                        transport_class=candidate.transport_class,
                        selected_mode=candidate.selected_mode,
                        from_endpoint=candidate.from_endpoint,
                        to_endpoint=candidate.to_endpoint,
                        departure_at=candidate.departure_at,
                        arrival_at=candidate.arrival_at,
                        duration_minutes=candidate.duration_minutes,
                        distance_meters=candidate.distance_meters,
                        total_cost_cny=candidate.total_cost_cny,
                        transfer_count=max(len(candidate.segments) - 1, 0),
                        segments=candidate.segments,
                        booking_status=candidate.booking_status,
                        route_status="ready",
                        lineage=_lineage(candidate),
                    )
                )
                departure_date = (
                    candidate.departure_at.date()
                    if candidate.departure_at is not None
                    else None
                )
                arrival_date = (
                    candidate.arrival_at.date()
                    if candidate.arrival_at is not None
                    else None
                )
                projection_shape = classify_transport_projection_shape(
                    transport_class=candidate.transport_class,
                    departure_at=candidate.departure_at,
                    arrival_at=candidate.arrival_at,
                    itinerary_dates=composition_day_dates,
                )
                if projection_shape == "cross_night_split":
                    current_role = (
                        "departure" if day.date == departure_date else "arrival"
                    )
                    other_date = (
                        arrival_date if current_role == "departure" else departure_date
                    )
                    other_role = (
                        "arrival" if current_role == "departure" else "departure"
                    )
                    timeline.append(
                        TimelineEntryRef(
                            entry_id=_stable_id("entry", leg_id, current_role),
                            entity_type=EntityType.TRANSPORT_LEG,
                            entity_id=leg_id,
                            projection_role=current_role,
                        )
                    )
                    deferred_transport_projections.setdefault(other_date, []).append(
                        TimelineEntryRef(
                            entry_id=_stable_id("entry", leg_id, other_role),
                            entity_type=EntityType.TRANSPORT_LEG,
                            entity_id=leg_id,
                            projection_role=other_role,
                        )
                    )
                elif projection_shape == "outside_itinerary":
                    raise ItineraryCompositionError(
                        "long-distance transport service dates fall outside the itinerary"
                    )
                else:
                    timeline.append(
                        TimelineEntryRef(
                            entry_id=_stable_id("entry", leg_id),
                            entity_type=EntityType.TRANSPORT_LEG,
                            entity_id=leg_id,
                        )
                    )
        day_plans.append(
            DayPlanV2(
                day_id=day.day_id,
                day=day.day,
                date=day.date,
                destination_id=day.destination_id,
                theme=derive_day_theme(
                    destination_name=destination_names.get(day.destination_id),
                    visits=visits[day_visit_start:],
                    dining=dining[day_dining_start:],
                    long_distance_legs=[
                        leg
                        for leg in transport[day_transport_start:]
                        if leg.transport_class == "long_distance"
                    ],
                ),
                timeline=timeline,
                time_structure=day.time_structure,
            )
        )

    if deferred_transport_projections:
        day_by_date = {day.date: day for day in day_plans}
        missing_dates = set(deferred_transport_projections) - set(day_by_date)
        if missing_dates:
            raise ItineraryCompositionError(
                f"cross-night transport projection dates are missing: {sorted(missing_dates)}"
            )
        day_plans = [
            day.model_copy(
                update={
                    "timeline": [
                        *[
                            ref
                            for ref in deferred_transport_projections.get(day.date, [])
                            if ref.projection_role == "arrival"
                        ],
                        *day.timeline,
                        *[
                            ref
                            for ref in deferred_transport_projections.get(day.date, [])
                            if ref.projection_role == "departure"
                        ],
                    ]
                }
            )
            for day in day_plans
        ]

    # One check-in window is one lodging slot: ``stay_id`` and ``slot_id`` are that
    # window's identity, not a running number, so the first choice for a window is
    # the stay and any later choice for the same window is one of its alternatives.
    # JSON Schema cannot express this across ``lodging_candidate_ids`` entries, so
    # resolve it here in composition order.  The eligible sweep below reads every
    # passed LodgingCandidate sharing the window, so a property that does not
    # become the stay still reaches the traveller as a ranked option on that slot.
    placed_stay_ids: set[str] = set()
    lodging_projections: dict[date, list[TimelineEntryRef]] = {}
    for candidate_id in composition.lodging_candidate_ids:
        candidate = candidates[candidate_id]
        if not isinstance(candidate, LodgingCandidate):
            raise ItineraryCompositionError("lodging choice references another candidate kind")
        window = _destination_stay_window(
            composition,
            candidate.destination_id,
            candidate.check_in_date,
            candidate.check_out_date,
        )
        if window is None:
            # A stay for a destination this itinerary never spends a night in has
            # nowhere to be: skip it rather than project it onto someone else's
            # nights.  The uncovered night is then reported by
            # ``delivery_quality_gate``'s ``missing_lodging_night`` gap, which is
            # the honest outcome -- see ``_destination_stay_window``.
            continue
        check_in_date, check_out_date = window
        nights = (check_out_date - check_in_date).days
        stay_id = _stable_id("lodging", candidate.destination_id, check_in_date, check_out_date)
        if stay_id in placed_stay_ids:
            continue
        placed_stay_ids.add(stay_id)
        slot_id = _stable_id("slot_lodging", candidate.destination_id, check_in_date, check_out_date)
        eligible = [
            item for item in candidates.values()
            if isinstance(item, LodgingCandidate)
            and item.destination_id == candidate.destination_id
            and _destination_stay_window(
                composition, item.destination_id, item.check_in_date, item.check_out_date
            ) == window
        ]
        slots.append(_slot(selected=candidate, eligible=eligible, slot_id=slot_id, target_entity_id=stay_id, slot_type="lodging", context={"destination_id": candidate.destination_id, "check_in_date": check_in_date.isoformat(), "check_out_date": check_out_date.isoformat()}))
        lodging.append(
            LodgingStay(
                stay_id=stay_id,
                place_id=candidate.place_id,
                name=candidate.property_name,
                check_in_date=check_in_date,
                check_out_date=check_out_date,
                nights=nights,
                room_type=candidate.room_type,
                nightly_price_cny=candidate.nightly_price_cny,
                total_price_cny=candidate.total_price_cny,
                price_kind=candidate.price_kind,
                availability_status=candidate.availability_status,
                address=candidate.address,
                selection_reason=candidate.selection_reasons[0],
                lineage=_lineage(candidate, slot_id),
            )
        )
        # A stay is a Day-spanning entity, so it projects on the two Days the
        # traveller actually acts on it: check-in closes its arrival Day and
        # check-out opens its departure Day.  An end whose date is not a planned
        # Day (a stay booked around the itinerary edge) simply has no projection
        # on that end; the stay itself still ships in ``lodging_stays``.
        for role, projection_date in (
            ("check_in", check_in_date),
            ("check_out", check_out_date),
        ):
            if projection_date not in composition_day_dates:
                continue
            lodging_projections.setdefault(projection_date, []).append(
                TimelineEntryRef(
                    entry_id=_stable_id("entry", stay_id, role),
                    entity_type=EntityType.LODGING_STAY,
                    entity_id=stay_id,
                    projection_role=role,
                )
            )

    if lodging_projections:
        # Applied after the cross-night transport merge above rebuilt the Day
        # list, so these refs land on the final timelines instead of being
        # dropped by that ``model_copy``.
        day_plans = [
            day.model_copy(
                update={
                    "timeline": [
                        *[
                            ref
                            for ref in lodging_projections.get(day.date, [])
                            if ref.projection_role == "check_out"
                        ],
                        *day.timeline,
                        *[
                            ref
                            for ref in lodging_projections.get(day.date, [])
                            if ref.projection_role == "check_in"
                        ],
                    ]
                }
            )
            for day in day_plans
        ]

    # Transport slots are minted from the *final* leg list, after the Day loop has
    # finished producing it: a slot bound to a leg the loop later rewrites would target
    # an entity the itinerary does not hold.  The legs come back stamped with their
    # slot id, which the workspace contract requires of a slot target.
    transport_slots, transport = _transport_slots(transport, candidates)
    slots.extend(transport_slots)
    itinerary = StructuredItineraryV2(
        itinerary_id=composition.itinerary_id,
        title=composition.title,
        destination_ids=list(dict.fromkeys(day.destination_id for day in composition.days)),
        duration_days=composition.duration_days,
        day_plans=day_plans,
        visit_stops=visits,
        dining_stops=dining,
        lodging_stays=lodging,
        transport_legs=transport,
        cost_summary=build_cost_coverage_summary(
            itinerary_price_components(
                visit_stops=visits,
                dining_stops=dining,
                lodging_stays=lodging,
                transport_legs=transport,
            ),
            budget_cap_cny=_total_budget_cap_cny(user_input_anchors),
        ),
        # Derived here, once, from the entities this very call just materialized
        # (``entities/trip_highlights.py``).  Five surfaces read the stored list;
        # none of them derives its own.
        highlights=derive_trip_highlights(
            visits=visits,
            dining=dining,
            lodging=lodging,
            transport_legs=transport,
        ),
        important_notes=_reference_service_notes(
            reference_services or [], transport_legs=transport, day_plans=day_plans
        ),
    )
    admissions = list(catalog.admission_results)
    admission_index = catalog.admission_index()
    for slot in slots:
        for option in slot.options:
            key = (option.candidate_id, slot.selection_slot_id)
            if key in admission_index:
                continue
            base = admission_index.get((option.candidate_id, None)) or next(
                (
                    item
                    for item in catalog.admission_results
                    if item.candidate_id == option.candidate_id and item.status == "passed"
                ),
                None,
            )
            if base is None or base.status != "passed":
                raise ItineraryCompositionError(
                    "selection slot option lacks a passed candidate admission"
                )
            scoped = base.model_copy(update={"selection_slot_id": slot.selection_slot_id})
            admissions.append(scoped)
            admission_index[key] = scoped
    catalog = catalog.model_copy(
        update={"admission_results": admissions}
    )
    return TripWorkspaceV2(
        run_id=run_id,
        generation_id=catalog.generation_id,
        workspace_revision=workspace_revision,
        itinerary=itinerary,
        recommendation_catalog=catalog,
        user_input_anchors=user_input_anchors or [],
        selection_slots=slots,
    )
