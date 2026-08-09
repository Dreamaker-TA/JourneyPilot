"""Authoritative Provider-evidence responsibility and outcome contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Sequence,
    cast,
)

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .delivery_bundle import ResearchDomain


ProviderEvidenceWorkerKind = Literal[
    "destination_researcher",
    "accommodation_researcher",
    "transport_researcher",
]
ProviderEvidenceCandidateKind = Literal["visit", "dining", "lodging"]
ProviderEvidenceTransportClass = Literal[
    "long_distance",
    "public_transit",
    "flexible",
]
ProviderEvidenceStatus = Literal[
    "no_option",
    "unresolved_loss",
    "materialized",
]
ProviderRouteLegRole = Literal["outbound", "return", "inter_destination"]

# The transport classes whose product *is* a timetable, and therefore the only ones
# that owe ``departure_at``/``arrival_at``.
#
# One definition, two readers: ``research_packet_output`` decides which Provider routes
# may be harvested at all, and ``services/candidate_admission`` decides which of them are
# admitted.  Neither may spell the rule out as its own literal set.
#
# For a long-distance leg the times are the product: G269 上海虹桥 15:00 → 深圳北 21:35
# is what the traveller buys, and the placement skeleton anchors a whole Day on them.
# A local transit connector is not that. The traveller walks to the platform and takes
# the next one; ``TransportCandidate.departure_at`` is optional precisely because such
# a leg has no scheduled instant, and even a timetabled provider's answer there is
# "the next departure after the time you asked about" — an illustration, not a
# commitment.
#
# Requiring the pair anyway makes the stricter layer unsatisfiable for any provider that
# answers in *durations*, which is the norm for mainland-China transit: amap returns
# 41 minutes and ¥4 on 地铁1号线 and no clock at all.  Such routes get dropped silently at
# harvest (``route_options=0`` with a successful call in the log), and the connector goes
# back to being authored by the model with an invented duration.
TIMETABLED_TRANSPORT_CLASSES: frozenset[str] = frozenset({"long_distance"})
_EXPLICIT_CROSS_DAY_RETURN = re.compile(
    r"跨日|跨夜|次日|翌日|第二天到达|凌晨到达|overnight|red[- ]?eye",
    re.IGNORECASE,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderRouteLegScope(_StrictFrozenModel):
    """One exact current-Run long-distance research responsibility."""

    leg_role: ProviderRouteLegRole
    from_place_id: str = Field(min_length=1)
    to_place_id: str = Field(min_length=1)
    service_date: date
    cross_day_required: bool = False

    @model_validator(mode="after")
    def validate_endpoints(self) -> "ProviderRouteLegScope":
        if self.from_place_id == self.to_place_id:
            raise ValueError("Provider route leg requires distinct endpoints")
        return self


class ProviderEvidenceScope(_StrictFrozenModel):
    """One independently resolvable Provider research responsibility."""

    scope_id: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    run_id: str = Field(min_length=1)
    constraint_pack_revision: int = Field(ge=0)
    worker_kind: ProviderEvidenceWorkerKind
    research_domain: ResearchDomain
    candidate_kind: Optional[ProviderEvidenceCandidateKind] = None
    transport_class: Optional[ProviderEvidenceTransportClass] = None
    target_identity: Optional[str] = Field(default=None, min_length=1)
    route_leg: Optional[ProviderRouteLegScope] = None

    @model_validator(mode="after")
    def validate_dimension(self) -> "ProviderEvidenceScope":
        if self.worker_kind == "transport_researcher":
            if self.transport_class is None or self.candidate_kind is not None:
                raise ValueError(
                    "transport Provider scope requires only transport_class"
                )
            expected_domain = (
                ResearchDomain.LONG_DISTANCE_TRANSPORT
                if self.transport_class == "long_distance"
                else ResearchDomain.LOCAL_TRANSPORT
            )
            if self.research_domain != expected_domain:
                raise ValueError("transport Provider scope domain is inconsistent")
            if self.transport_class == "long_distance":
                if self.route_leg is None or self.target_identity is not None:
                    raise ValueError(
                        "long-distance Provider scope requires only exact route_leg"
                    )
            elif self.route_leg is not None or self.target_identity is None:
                raise ValueError(
                    "local transport Provider scope requires only target_identity"
                )
        else:
            if self.candidate_kind is None or self.transport_class is not None:
                raise ValueError(
                    "place Provider scope requires only candidate_kind"
                )
            expected = {
                "visit": ResearchDomain.VISIT,
                "dining": ResearchDomain.DINING,
                "lodging": ResearchDomain.LODGING,
            }[self.candidate_kind]
            if self.research_domain != expected:
                raise ValueError("place Provider scope domain is inconsistent")
            expected_worker = (
                "accommodation_researcher"
                if self.candidate_kind == "lodging"
                else "destination_researcher"
            )
            if self.worker_kind != expected_worker:
                raise ValueError("candidate kind does not belong to Provider worker")
            if self.route_leg is not None or self.target_identity is None:
                raise ValueError(
                    "place Provider scope requires only target_identity"
                )
        expected_scope_id = provider_evidence_scope_id(
            run_id=self.run_id,
            constraint_pack_revision=self.constraint_pack_revision,
            worker_kind=self.worker_kind,
            research_domain=self.research_domain,
            candidate_kind=self.candidate_kind,
            transport_class=self.transport_class,
            target_identity=self.target_identity,
            route_leg=self.route_leg,
        )
        if self.scope_id != expected_scope_id:
            raise ValueError("Provider evidence scope id is not authoritative")
        return self


class ProviderEvidenceAssignment(_StrictFrozenModel):
    """Server-owned assignment of one scope to a numbered worker attempt."""

    scope: ProviderEvidenceScope
    attempt_number: int = Field(ge=0)


class ProviderEvidenceOutcome(_StrictFrozenModel):
    """Current result reported for one explicit Provider-evidence scope."""

    scope: ProviderEvidenceScope
    attempt_number: int = Field(ge=0)
    provider_option_count: int = Field(ge=0)
    provider_option_materialized_count: int = Field(ge=0)
    unresolved_loss_count: int = Field(ge=0)
    status: ProviderEvidenceStatus

    @model_validator(mode="after")
    def validate_status_counts(self) -> "ProviderEvidenceOutcome":
        if self.status == "no_option" and any(
            (
                self.provider_option_count,
                self.provider_option_materialized_count,
                self.unresolved_loss_count,
            )
        ):
            raise ValueError("no-option Provider outcome must have zero counts")
        if self.status == "materialized":
            if (
                self.provider_option_materialized_count < 1
                or self.unresolved_loss_count
            ):
                raise ValueError(
                    "materialized Provider outcome requires materialized evidence"
                )
        if self.status == "unresolved_loss":
            if (
                self.unresolved_loss_count < 1
                or self.provider_option_materialized_count
            ):
                raise ValueError(
                    "unresolved Provider outcome requires only a positive loss"
                )
        return self


class ProviderEvidenceSummary(_StrictFrozenModel):
    current_scope_count: int = Field(ge=0)
    unresolved_scope_count: int = Field(ge=0)
    provider_option_count: int = Field(ge=0)
    provider_option_materialized_count: int = Field(ge=0)
    provider_salvage_loss_count: int = Field(ge=0)


def explicit_cross_day_return_required(text: str) -> bool:
    """Classify an explicit cross-calendar-day return requirement."""

    return bool(_EXPLICIT_CROSS_DAY_RETURN.search(text or ""))


def provider_evidence_scope_id(
    *,
    run_id: str,
    constraint_pack_revision: int,
    worker_kind: ProviderEvidenceWorkerKind,
    research_domain: ResearchDomain,
    candidate_kind: Optional[ProviderEvidenceCandidateKind],
    transport_class: Optional[ProviderEvidenceTransportClass],
    target_identity: Optional[str],
    route_leg: Optional[ProviderRouteLegScope],
) -> str:
    material = {
        "run_id": run_id,
        "constraint_pack_revision": constraint_pack_revision,
        "worker_kind": worker_kind,
        "research_domain": research_domain.value,
        "candidate_kind": candidate_kind,
        "transport_class": transport_class,
        "target_identity": target_identity,
        "route_leg": (
            route_leg.model_dump(mode="json")
            if route_leg is not None
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _identity_parts(
    controlled_trip_identity: Mapping[str, Any],
) -> tuple[str, list[str]]:
    origin = controlled_trip_identity.get("origin")
    destinations = controlled_trip_identity.get("destinations")
    origin_id = (
        str(origin.get("place_id") or "").strip()
        if isinstance(origin, Mapping)
        else ""
    )
    destination_ids = [
        str(item.get("place_id") or "").strip()
        for item in destinations or ()
        if isinstance(item, Mapping) and str(item.get("place_id") or "").strip()
    ]
    return origin_id, list(dict.fromkeys(destination_ids))


def build_required_long_distance_legs(
    controlled_trip_identity: Mapping[str, Any],
    *,
    cross_day_return_required: bool,
) -> list[ProviderRouteLegScope]:
    """Derive the itinerary chain's exact long-distance leg responsibilities.

    Multi-destination trips are a **chain over the ordered visit list**, not a
    simple round trip.  For ``[origin, *destinations, origin]`` every adjacent
    pair is owed a long-distance leg: ``origin→city_1`` (outbound),
    ``city_i→city_{i+1}`` (inter_destination) for each interior boundary, and
    ``city_M→origin`` (return) when the last destination is not the origin.
    The interior city-to-city legs (e.g. 杭州→苏州 in a 上海→杭州→苏州 trip) are
    built too, not just the outbound and return: they must be researched and bound.

    Each destination's stay is assigned **evenly** over the trip span:
    ``span_days`` is divided across the
    destinations by far-forward positions, with the remainder given to the
    earliest cities.  The handover (inter_destination) leg for ``city_i``
    departs on the **first day of the next city's stay**, so transport research
    happens on the actual handover day.
    """

    origin_id, destination_ids = _identity_parts(controlled_trip_identity)
    if not origin_id or not destination_ids:
        return []
    if origin_id == destination_ids[0] and destination_ids[-1] == origin_id:
        return []
    try:
        start_date = date.fromisoformat(
            str(controlled_trip_identity.get("start_date") or "")[:10]
        )
        end_date = date.fromisoformat(
            str(controlled_trip_identity.get("end_date") or "")[:10]
        )
    except ValueError:
        return []
    if end_date < start_date:
        return []

    span_days = (end_date - start_date).days + 1
    num_destinations = len(destination_ids)
    # Even split of the whole span across the ordered destinations; the first
    # ``remainder`` cities get one extra day.
    base, remainder = divmod(span_days, num_destinations)
    per_city: list[int] = [base + (1 if i < remainder else 0) for i in range(num_destinations)]

    # Cumulative first-day offset of each destination's stay (0-based from start_date).
    starts: list[date] = []
    cursor = start_date
    for n in per_city:
        starts.append(cursor)
        cursor += timedelta(days=n)

    legs: list[ProviderRouteLegScope] = [
        ProviderRouteLegScope(
            leg_role="outbound",
            from_place_id=origin_id,
            to_place_id=destination_ids[0],
            service_date=start_date,
            cross_day_required=False,
        )
    ]
    # Interior city-to-city handover legs: depart on the first day of the next
    # destination's stay.
    for i in range(num_destinations - 1):
        legs.append(
            ProviderRouteLegScope(
                leg_role="inter_destination",
                from_place_id=destination_ids[i],
                to_place_id=destination_ids[i + 1],
                service_date=starts[i + 1],
                cross_day_required=False,
            )
        )
    if destination_ids[-1] != origin_id:
        legs.append(
            ProviderRouteLegScope(
                leg_role="return",
                from_place_id=destination_ids[-1],
                to_place_id=origin_id,
                service_date=end_date,
                cross_day_required=cross_day_return_required,
            )
        )
    return legs


def missing_long_distance_leg_roles(
    required_legs: Sequence[ProviderRouteLegScope],
    *,
    day_dates: AbstractSet[date],
    dates_with_long_distance: AbstractSet[date],
) -> Optional[list[str]]:
    """Name the required long-distance legs no planned Day ever delivered.

    ``None`` means the round trip is not assessable and every reader must stay
    silent.  Three ways that happens:

    - the Run owes no long-distance leg at all;
    - the composed Days do not even cover a required service date, in which case
      the identity and the itinerary describe different trips and no per-leg
      verdict read from them would be honest;
    - one service date owes more than one leg and carries at least one.  A
      same-day round trip is the real case: both owed legs fall on the trip's
      single date, so "this date has a long-distance leg" cannot separate *one
      of the two arrived* from *both arrived*, and an empty list would assert
      the stronger of the two.  A date that owes two and carries none is still
      assessable — nothing arrived — and both roles are named.

    That silence is deliberately distinct from an empty list, which asserts
    that every owed leg was delivered.

    Naming which of two same-day legs is the missing one needs per-leg delivery
    identity, and the delivered leg does not carry one: its endpoints are
    station-scoped ids minted by the provider binder (``12306:{telecode}``,
    airport codes) while a required leg's endpoints are the city-scoped ids of
    the controlled identity (``osm:relation:…``).  The two never compare equal,
    so matching a delivered leg to a direction would have to be guessed.
    """

    if not required_legs or any(
        leg.service_date not in day_dates for leg in required_legs
    ):
        return None
    owed_per_date: dict[date, int] = {}
    for leg in required_legs:
        owed_per_date[leg.service_date] = owed_per_date.get(leg.service_date, 0) + 1
    if any(
        owed > 1 and service_date in dates_with_long_distance
        for service_date, owed in owed_per_date.items()
    ):
        return None
    return [
        leg.leg_role
        for leg in required_legs
        if leg.service_date not in dates_with_long_distance
    ]


def scope_attempt_numbers(
    outcomes: Mapping[str, "ProviderEvidenceOutcome"],
) -> dict[str, int]:
    """The run's highest recorded attempt per Provider evidence scope."""
    return {
        scope_id: outcome.attempt_number
        for scope_id, outcome in (outcomes or {}).items()
    }


def build_provider_evidence_assignments(
    *,
    run_id: str,
    constraint_pack_revision: int,
    worker_kind: ProviderEvidenceWorkerKind,
    controlled_trip_identity: Mapping[str, Any],
    prior_scope_attempts: Mapping[str, int],
    candidate_kinds: Optional[Sequence[str]] = None,
    transport_classes: Optional[Sequence[str]] = None,
    long_distance_legs: Optional[Sequence[ProviderRouteLegScope]] = None,
) -> list[ProviderEvidenceAssignment]:
    """Build explicit business scopes; callers must persist them on assignment.

    ``prior_scope_attempts`` is the run's durable ``scope_id -> attempt_number``
    map, so each assignment is numbered **per scope**: one past this scope's own
    highest attempt, or ``0`` on its first.  The number must come from the scope
    and not from the caller's retry budget, because those are keyed differently —
    the targeted-research ledger is keyed per *product gap* while a local-transport
    scope covers the whole destination set.  Number it from the caller's budget and two
    connector rounds motivated by two different adjacencies both report "attempt 1" on
    the same scope with different option counts, at which point
    `merge_provider_evidence_outcomes` correctly refuses to merge them and
    `same Provider evidence attempt produced conflicting outcomes` kills the run.
    """

    origin_id, destination_ids = _identity_parts(controlled_trip_identity)
    destination_target = (
        f"destinations:{','.join(destination_ids)}"
        if destination_ids
        else f"run:{run_id}"
    )
    dimensions: list[
        tuple[
            ResearchDomain,
            Optional[ProviderEvidenceCandidateKind],
            Optional[ProviderEvidenceTransportClass],
            Optional[str],
            Optional[ProviderRouteLegScope],
        ]
    ] = []
    if worker_kind == "destination_researcher":
        kinds = (
            ("visit", "dining")
            if candidate_kinds is None
            else candidate_kinds
        )
        for raw_kind in dict.fromkeys(kinds):
            if raw_kind not in {"visit", "dining"}:
                raise ValueError(
                    f"unsupported destination Provider scope: {raw_kind}"
                )
            kind = cast(ProviderEvidenceCandidateKind, raw_kind)
            dimensions.append(
                (
                    ResearchDomain.VISIT
                    if kind == "visit"
                    else ResearchDomain.DINING,
                    kind,
                    None,
                    destination_target,
                    None,
                )
            )
    elif worker_kind == "accommodation_researcher":
        dimensions.append(
            (
                ResearchDomain.LODGING,
                "lodging",
                None,
                destination_target,
                None,
            )
        )
    else:
        classes = (
            ("long_distance", "public_transit", "flexible")
            if transport_classes is None
            else transport_classes
        )
        for raw_class in dict.fromkeys(classes):
            if raw_class not in {
                "long_distance",
                "public_transit",
                "flexible",
            }:
                raise ValueError(
                    f"unsupported transport Provider scope: {raw_class}"
                )
            transport_class = cast(
                ProviderEvidenceTransportClass,
                raw_class,
            )
            if transport_class == "long_distance":
                if not long_distance_legs:
                    raise ValueError(
                        "long-distance Provider scopes require exact route legs"
                    )
                dimensions.extend(
                    (
                        ResearchDomain.LONG_DISTANCE_TRANSPORT,
                        None,
                        transport_class,
                        None,
                        route_leg,
                    )
                    for route_leg in long_distance_legs
                )
            else:
                dimensions.append(
                    (
                        ResearchDomain.LOCAL_TRANSPORT,
                        None,
                        transport_class,
                        destination_target,
                        None,
                    )
                )
    assignments = []
    for (
        domain,
        candidate_kind,
        transport_class,
        target_identity,
        route_leg,
    ) in dimensions:
        scope_id = provider_evidence_scope_id(
            run_id=run_id,
            constraint_pack_revision=constraint_pack_revision,
            worker_kind=worker_kind,
            research_domain=domain,
            candidate_kind=candidate_kind,
            transport_class=transport_class,
            target_identity=target_identity,
            route_leg=route_leg,
        )
        assignments.append(
            ProviderEvidenceAssignment(
                scope=ProviderEvidenceScope(
                    scope_id=scope_id,
                    run_id=run_id,
                    constraint_pack_revision=constraint_pack_revision,
                    worker_kind=worker_kind,
                    research_domain=domain,
                    candidate_kind=candidate_kind,
                    transport_class=transport_class,
                    target_identity=target_identity,
                    route_leg=route_leg,
                ),
                attempt_number=prior_scope_attempts.get(scope_id, -1) + 1,
            )
        )
    return assignments


def parse_provider_evidence_assignments(
    assignment: Mapping[str, Any],
    *,
    expected_worker: ProviderEvidenceWorkerKind,
    expected_run_id: str,
    expected_constraint_pack_revision: int,
) -> list[ProviderEvidenceAssignment]:
    raw = assignment.get("provider_evidence_assignments")
    if not isinstance(raw, list) or not raw:
        raise ValueError("research assignment requires Provider evidence scopes")
    parsed = [ProviderEvidenceAssignment.model_validate(item) for item in raw]
    scope_ids = [item.scope.scope_id for item in parsed]
    if len(scope_ids) != len(set(scope_ids)):
        raise ValueError("Provider evidence assignment scopes must be unique")
    if any(
        item.scope.worker_kind != expected_worker
        or item.scope.run_id != expected_run_id
        or item.scope.constraint_pack_revision
        != expected_constraint_pack_revision
        for item in parsed
    ):
        raise ValueError("Provider evidence assignment authority mismatch")
    return parsed


def merge_provider_evidence_outcomes(
    a: Dict[str, ProviderEvidenceOutcome],
    b: Dict[str, ProviderEvidenceOutcome],
) -> Dict[str, ProviderEvidenceOutcome]:
    """Merge current outcomes by explicit attempt, independent of fan-in order."""

    merged = dict(a or {})
    for scope_id, incoming in (b or {}).items():
        if scope_id != incoming.scope.scope_id:
            raise ValueError("Provider evidence outcome map key mismatch")
        previous = merged.get(scope_id)
        if previous is None:
            merged[scope_id] = incoming
            continue
        if previous.scope != incoming.scope:
            raise ValueError("Provider evidence scope identity conflict")
        if previous.attempt_number == incoming.attempt_number:
            if previous != incoming:
                raise ValueError(
                    "same Provider evidence attempt produced conflicting outcomes"
                )
            continue
        newer, older = (
            (incoming, previous)
            if incoming.attempt_number > previous.attempt_number
            else (previous, incoming)
        )
        if newer.status == "no_option" and older.status == "unresolved_loss":
            newer = newer.model_copy(
                update={
                    "status": "unresolved_loss",
                    "unresolved_loss_count": older.unresolved_loss_count,
                }
            )
        merged[scope_id] = newer
    return merged


def current_provider_evidence_summary(
    outcomes: Mapping[str, ProviderEvidenceOutcome],
    *,
    constraint_pack_revision: int,
) -> ProviderEvidenceSummary:
    current = [
        outcome
        for outcome in outcomes.values()
        if outcome.scope.constraint_pack_revision == constraint_pack_revision
    ]
    unresolved = [
        outcome for outcome in current if outcome.status == "unresolved_loss"
    ]
    return ProviderEvidenceSummary(
        current_scope_count=len(current),
        unresolved_scope_count=len(unresolved),
        provider_option_count=sum(
            outcome.provider_option_count for outcome in current
        ),
        provider_option_materialized_count=sum(
            outcome.provider_option_materialized_count for outcome in current
        ),
        provider_salvage_loss_count=sum(
            outcome.unresolved_loss_count for outcome in unresolved
        ),
    )


def dump_provider_evidence_assignments(
    assignments: Iterable[ProviderEvidenceAssignment],
) -> list[dict[str, Any]]:
    return [assignment.model_dump(mode="json") for assignment in assignments]
