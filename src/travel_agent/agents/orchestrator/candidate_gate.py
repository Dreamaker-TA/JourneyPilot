"""Build the Recommendation Catalog from typed Research Packets.

The gate normalizes every candidate's identity and price evidence, scores its
budget, weather and constraint fit, and spends a bounded targeted-research
budget on the domains that still have no concrete entity.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.runnables import RunnableConfig

from ...entities.delivery_bundle import (
    CandidateResearchGap,
    EntityRef,
    EntityType,
    GateClass,
    GateDisposition,
    GateFailureAttribution,
    RecommendationCatalog,
    ResearchCandidate,
    ResearchPacket,
    TransportCandidate,
    TransportLegRef,
    WeatherDayContext,
    WeatherImpact,
    ResearchDomain,
)
from ...entities.state import TravelAgentState, bounded_repair_context
from ...entities.intent_spec import IntentStrength, IntentTarget
from ...entities.candidate_intent import IntentMatchStatus
from ...entities.provider_evidence import (
    build_provider_evidence_assignments,
    build_required_long_distance_legs,
    dump_provider_evidence_assignments,
    explicit_cross_day_return_required,
    ProviderRouteLegScope,
    scope_attempt_numbers,
)
from ...entities.itinerary_composition_v2 import (
    ItineraryCompositionError,
    LocalConnectorGap,
    align_skeleton_to_provider_routes,
    connector_mode_requests_from_constraint_pack,
    connector_candidate_quality_error,
    extract_local_connector_gaps,
)
from ...services.candidate_admission import (
    DETERMINISTIC_IDENTITY_VERDICT_FIELD_PATHS,
    admit_candidate,
    normalize_lodging_price_evidence,
)
from ...services.candidate_intent_evaluation import evaluate_candidate_intents
from ...services.candidate_ranking import rank_candidates
from ...services.candidate_selection import build_candidate_selection_plan
from ...models.router import get_model_router
from ...services.constraint_applicability import (
    bind_candidate_constraint_gate_attestations,
)
from ...services.destination_scope import destination_points
from ...services.product_requirements import required_physical_candidate_kinds
from ...services.research_query_planner import append_targeted_repair_query
from ...services.weather_impact_engine import (
    WeatherImpactEngine,
    risk_profile_from_constraint_pack,
)
from ...workflows.composition_repair import apply_composition_repair_budget
from ...workflows.run_deadline import (
    DeadlineObservation,
    observe_run_deadline,
)
from ..utils import strip_round_suffix
from .provider_failure import classify_provider_failure, is_provider_or_model_failure


logger = logging.getLogger(__name__)

_RESEARCH_WORKERS = {
    "destination_researcher",
    "accommodation_researcher",
    "transport_researcher",
}
_ROUND_SUFFIX = re.compile(r"_r(\d+)$")
_MAX_TARGETED_RESEARCH_ATTEMPTS = 1
# The one gap kind whose targeted round is widened to carry every companion
# responsibility of its domain (the companion-leg widening below reads the same
# field path).  A packed round is charged to one gap but answers for the set,
# which is why ``_gap_research_exhausted`` reads the domain budget for these and
# the per-gap ledger for everything else.
_COMPANION_PACKED_FIELD_PATH = "transport_class.long_distance"
_ALTERNATIVE_TOOLS = {
    "destination_researcher": [
        "global_place_search",
        "maps_text_search",
        "tavily_search",
        "free_web_search",
        "brave_web_search",
        "firecrawl_search",
    ],
    # ``maps_text_search`` is listed for the same reason it is listed for
    # ``destination_researcher``: it is the *only* lodging provider the
    # accommodation worker's own routing accepts for a CN destination
    # (``discover_deterministic_hotels``: "国内酒店在 OpenStreetMap 覆盖极差",
    # "Neither provider is ever retried through the other").  Omitting it meant a
    # targeted lodging round on a CN trip was handed Nominatim and nothing else,
    # which is a provider that path documents as having no usable CN coverage —
    # measured ``candidates=0`` on four consecutive Runs.
    # ``maps_search_detail`` rides along because the amap text answer carries no
    # geometry, and a record with no point cannot be scoped or pinned.
    "accommodation_researcher": [
        "global_place_search",
        "maps_text_search",
        "maps_search_detail",
        "tavily_search",
        "free_web_search",
        "brave_web_search",
    ],
    "transport_researcher": [
        "global_route_search",
        "maps_direction_transit_integrated",
        "maps_direction_walking",
        "maps_direction_driving",
        "free_web_search",
        "get-interline-tickets",
        "get-tickets",
    ],
}
# Route tools whose results are always a local commuting itinerary; they can
# never ground one exact long-distance leg.
_LOCAL_ONLY_ROUTE_TOOLS = frozenset(
    {
        "global_route_search",
        "maps_direction_transit_integrated",
        "maps_direction_walking",
        "maps_direction_driving",
    }
)
# Intercity Providers, highest-yield first, for a long-distance responsibility.
_LONG_DISTANCE_ROUTE_TOOLS = [
    "get-tickets",
    "get-interline-tickets",
    "search_flights",
]


@dataclass(frozen=True)
class ProviderFailureSignal:
    tool_name: str
    signature: str
    reason_code: str
    category: str


class CandidateGateIntegrityError(RuntimeError):
    """A stale or malformed packet cannot safely enter Candidate admission."""

    def __init__(self, reason_code: str, message: str, *, worker_kind: str | None = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.worker_kind = worker_kind


def _failure_signals(packet: ResearchPacket | None) -> list[ProviderFailureSignal]:
    """Classify the provider failures the *server* compiled for this packet.

    What makes an observation a provider failure is a server-compiled record,
    not its wording.  Both keys read here are server-owned: an
    ``external_tool`` source may enter a packet only from the authoritative
    registry ``research_packet_output`` compiles out of this round's Tool
    Gateway transcript — ``_bind_source_content_hashes`` replaces any
    model-echoed copy with the registry record and rejects an id the registry
    does not hold — and only the server stamps ``lifecycle_status="rejected"``
    on it.  The pair is therefore exactly the set of calls that really failed
    or degraded this round.

    Nothing the worker can type is read as a failure any more.  A title ending
    in ``" degradation"`` and a snapshot ``error`` key are free text on a
    model-authored ``external_web`` source, so honoring them let a model burn a
    domain's targeted-research budget and exclude a healthy tool by naming one
    of its own sources.  The tool name, provider, and error text below are read
    only *after* a server record is in hand, and every one of those fields was
    written by the server too.

    Signatures intentionally contain a normalized reason category rather than
    the raw error text. A provider that changes punctuation or request ids must
    not gain a fresh retry budget for the same domain/gap.
    """
    if packet is None:
        return []
    signals: list[ProviderFailureSignal] = []
    for source in packet.source_records:
        if (
            source.source_kind != "external_tool"
            or source.lifecycle_status != "rejected"
        ):
            continue
        snapshot = source.snapshot
        error = snapshot.get("error") or snapshot.get("degradation_reason")
        fallback_from = snapshot.get("fallback_from")
        error_tool = re.search(
            r"(?:executing\s+)?tool\s+([A-Za-z0-9_-]+)",
            str(error or ""),
            flags=re.IGNORECASE,
        )
        tool_name = (
            str(fallback_from).strip()
            if isinstance(fallback_from, str) and fallback_from.strip()
            else error_tool.group(1)
            if error_tool
            else source.title.split(" ", 1)[0].strip()
        )
        normalized_tool = tool_name.strip().casefold()
        if not re.fullmatch(r"[a-z0-9_-]+", normalized_tool):
            normalized_tool = "unknown_tool"
        classification = classify_provider_failure(error)
        signals.append(
            ProviderFailureSignal(
                tool_name=normalized_tool,
                signature=(
                    f"{classification.category}:{normalized_tool}|"
                    f"provider:{str(source.provider_name or 'unknown').casefold()}|"
                    f"reason:{classification.reason_code}"
                ),
                reason_code=classification.reason_code,
                category=classification.category,
            )
        )
    return list({signal.signature: signal for signal in signals}.values())


def _latest_packets(
    packets: Dict[str, ResearchPacket], *, generation_id: str
) -> Dict[str, ResearchPacket]:
    latest: dict[str, tuple[int, ResearchPacket]] = {}
    for key, packet in packets.items():
        if packet.generation_id != generation_id:
            continue
        task_key = key.split("@", 1)[0]
        base = strip_round_suffix(task_key)
        if base not in _RESEARCH_WORKERS:
            continue
        match = _ROUND_SUFFIX.search(task_key)
        round_number = int(match.group(1)) if match else 1
        current = latest.get(base)
        if current is None or round_number >= current[0]:
            latest[base] = (round_number, packet)
    return {base: item[1] for base, item in latest.items()}


def _catalog_packets(state: TravelAgentState) -> list[ResearchPacket]:
    """Carry forward still-current candidates across scoped research rounds.

    A worker retry replaces its entry in ``research_packets``. The previous
    Recommendation Catalog is therefore the only immutable record of candidates
    that already passed before the scoped gap was researched. Preserve unique
    candidates and let the current packet win on identity collisions.
    """
    if state.planning_generation is None:
        raise CandidateGateIntegrityError(
            "missing_planning_generation",
            "candidate gate requires a planning generation",
        )
    generation_id = state.planning_generation.generation_id
    current = _latest_packets(
        state.research_packets,
        generation_id=generation_id,
    )
    packets = [
        _packet_candidate_closure(packet, packet.candidates)
        for packet in current.values()
    ]
    previous = state.recommendation_catalog
    if (
        previous is None
        or previous.generation_id != generation_id
        or previous.fact_data_revision != state.fact_data_revision
    ):
        return packets

    current_ids_by_worker = {
        worker: {candidate.candidate_id for candidate in packet.candidates}
        for worker, packet in current.items()
    }
    current_packet_ids = {packet.research_packet_id for packet in packets}
    for packet in previous.research_packets:
        if (
            packet.worker_kind not in current
            or packet.generation_id != generation_id
            or packet.constraint_pack_revision != state.constraint_pack_revision
            or packet.fact_data_revision != state.fact_data_revision
        ):
            continue
        retained = [
            candidate
            for candidate in packet.candidates
            if candidate.candidate_id not in current_ids_by_worker[packet.worker_kind]
        ]
        if not retained or packet.research_packet_id in current_packet_ids:
            continue
        packets.append(_packet_candidate_closure(packet, retained))
        current_packet_ids.add(packet.research_packet_id)
    return packets


def _packet_candidate_closure(
    packet: ResearchPacket,
    candidates: Sequence[ResearchCandidate],
) -> ResearchPacket:
    """Revalidate the exact evidence closure reachable from selected candidates."""
    if not candidates:
        return packet
    candidate_ids = {candidate.candidate_id for candidate in candidates}
    facts = [
        fact
        for fact in packet.fact_assertions
        if fact.entity_ref.entity_id in candidate_ids
    ]
    fact_ids = {fact.fact_assertion_id for fact in facts}
    source_ids = {
        link.source_record_id
        for fact in facts
        for link in fact.source_links
    }
    sources = [
        source
        for source in packet.source_records
        if source.source_record_id in source_ids
        or source.lifecycle_status == "rejected"
    ]
    provenance = [
        item.model_copy(
            update={
                "reference_ids": [
                    reference_id
                    for reference_id in item.reference_ids
                    if reference_id in fact_ids
                ]
            }
        )
        if item.origin == "external_fact"
        else item
        for item in packet.field_provenance
        if item.entity_ref.entity_id in candidate_ids
        and (
            item.origin != "external_fact"
            or any(reference_id in fact_ids for reference_id in item.reference_ids)
        )
    ]
    return ResearchPacket.model_validate(
        {
            **packet.model_dump(mode="python"),
            "candidates": list(candidates),
            "candidate_discovery_records": [
                record
                for record in packet.candidate_discovery_records
                if record.candidate_id in candidate_ids
            ],
            "source_records": sources,
            "fact_assertions": facts,
            "field_provenance": provenance,
        }
    )


def _candidate_domain(candidate: ResearchCandidate) -> ResearchDomain:
    if candidate.candidate_kind == "visit":
        return ResearchDomain.VISIT
    if candidate.candidate_kind == "dining":
        return ResearchDomain.DINING
    if candidate.candidate_kind == "lodging":
        return ResearchDomain.LODGING
    return (
        ResearchDomain.LONG_DISTANCE_TRANSPORT
        if candidate.transport_class == "long_distance"
        else ResearchDomain.LOCAL_TRANSPORT
    )


def _apply_candidate_caps(
    packets: Sequence[ResearchPacket],
    per_domain_caps: Mapping[str, int],
) -> list[ResearchPacket]:
    origin_priority = {
        "targeted_repair": 0,
        "intent_query": 1,
        "structural_query": 2,
        "generic_fallback": 3,
        "composer_authored_fallback": 4,
    }
    rows: list[tuple[int, str, ResearchDomain]] = []
    for packet in packets:
        discovery = {
            record.candidate_id: record
            for record in packet.candidate_discovery_records
        }
        for candidate in packet.candidates:
            record = discovery[candidate.candidate_id]
            priority = min(
                origin_priority[origin.value]
                for origin in record.origins
            )
            rows.append((priority, candidate.candidate_id, _candidate_domain(candidate)))
    rows.sort()
    kept: set[str] = set()
    counts: dict[ResearchDomain, int] = defaultdict(int)
    for _priority, candidate_id, domain in rows:
        cap = max(int(per_domain_caps.get(domain.value, 0)), 0)
        if counts[domain] >= cap:
            continue
        kept.add(candidate_id)
        counts[domain] += 1
    capped: list[ResearchPacket] = []
    for packet in packets:
        candidates = [
            candidate for candidate in packet.candidates if candidate.candidate_id in kept
        ]
        if candidates:
            capped.append(_packet_candidate_closure(packet, candidates))
    return capped


def _target_ref(candidate: ResearchCandidate) -> EntityRef | TransportLegRef:
    if candidate.candidate_kind == "transport":
        return TransportLegRef(transport_leg_id=candidate.candidate_id)
    entity_type = {
        "visit": EntityType.VISIT_STOP,
        "dining": EntityType.DINING_STOP,
        "lodging": EntityType.LODGING_STAY,
    }[candidate.candidate_kind]
    return EntityRef(entity_type=entity_type, entity_id=candidate.candidate_id)


def _weather_days(state: TravelAgentState, candidate: ResearchCandidate) -> list[WeatherDayContext]:
    if state.weather_context is None:
        return []
    destination_days = sorted(
        [
            day
            for day in state.weather_context.days
            if day.destination_id == candidate.destination_id
        ],
        key=lambda day: day.date,
    )
    if not isinstance(candidate, TransportCandidate):
        return destination_days

    scheduled_days = [
        day
        for day in destination_days
        if _transport_applies_to_weather_day(candidate, day)
    ]
    if candidate.departure_at is not None or candidate.arrival_at is not None:
        return [
            _scheduled_transport_weather_day(candidate, day)
            for day in scheduled_days
        ]
    if candidate.transport_class == "long_distance":
        return [
            day
            for day in destination_days
            if day.date == state.weather_context.trip_start_date
        ]
    return destination_days


def _weather_zone(day: WeatherDayContext) -> ZoneInfo | None:
    if not day.timezone:
        return None
    try:
        return ZoneInfo(day.timezone)
    except ZoneInfoNotFoundError:
        return None


def _local_timestamp(
    timestamp: datetime | None,
    day: WeatherDayContext,
) -> datetime | None:
    if timestamp is None:
        return None
    if timestamp.utcoffset() is None:
        return None
    zone = _weather_zone(day)
    if zone is None:
        return timestamp
    return timestamp.astimezone(zone)


def _transport_applies_to_weather_day(
    candidate: TransportCandidate,
    day: WeatherDayContext,
) -> bool:
    """Bind destination weather to the route's actual local service date.

    A long-distance candidate only consumes destination weather at arrival.
    Applying Tokyo's weather to a Shanghai departure instant would invent
    coverage for an origin that the Weather Context does not contain.
    """
    if candidate.transport_class == "long_distance":
        anchor = candidate.arrival_at or candidate.departure_at
        local_anchor = _local_timestamp(anchor, day)
        return local_anchor is not None and local_anchor.date() == day.date

    start = _local_timestamp(candidate.departure_at, day)
    end = _local_timestamp(candidate.arrival_at, day)
    if start is None and end is None:
        return False
    start = start or end
    end = end or start
    return start.date() <= day.date <= end.date()


def _scheduled_transport_weather_day(
    candidate: TransportCandidate,
    day: WeatherDayContext,
) -> WeatherDayContext:
    """Narrow a forecast day to the hourly window relevant to a route.

    Daily fields are day-wide extrema.  They remain the conservative fallback
    when hourly coverage is absent, but they must not reject a scheduled route
    when the Provider supplied a safe, matching local-time window.
    """
    if day.data_kind != "forecast" or not day.hourly_windows:
        return day

    if candidate.transport_class == "long_distance":
        anchor = _local_timestamp(candidate.arrival_at or candidate.departure_at, day)
        relevant = [
            window
            for window in day.hourly_windows
            if anchor is not None and window.start_at <= anchor < window.end_at
        ]
    else:
        start = _local_timestamp(candidate.departure_at, day)
        end = _local_timestamp(candidate.arrival_at, day)
        relevant = [
            window
            for window in day.hourly_windows
            if start is not None
            and end is not None
            and window.start_at < end
            and window.end_at > start
        ]
    if not relevant:
        return day

    probabilities = [
        item.precipitation_probability_pct
        for item in relevant
        if item.precipitation_probability_pct is not None
    ]
    apparent_temperatures = [
        item.apparent_temperature_c
        for item in relevant
        if item.apparent_temperature_c is not None
    ]
    wind_speeds = [
        item.wind_speed_kph
        for item in relevant
        if item.wind_speed_kph is not None
    ]
    return day.model_copy(
        update={
            # Daily WMO code/amount/extrema are not tied to this service time.
            "condition_code": None,
            "condition_label": None,
            "high_c": None,
            "low_c": None,
            "apparent_high_c": (
                max(apparent_temperatures) if apparent_temperatures else None
            ),
            "precipitation_probability_pct": (
                max(probabilities) if probabilities else None
            ),
            "precipitation_mm": None,
            "wind_speed_kph": max(wind_speeds) if wind_speeds else None,
            "wind_gust_kph": None,
            "hourly_windows": relevant,
        }
    )


def _destination_country_codes(state: TravelAgentState) -> dict[str, str]:
    destinations = (state.controlled_trip_identity or {}).get("destinations") or []
    return {
        str(item.get("place_id") or ""): str(item.get("country_code") or "").casefold()
        for item in destinations
        if isinstance(item, dict)
        and item.get("place_id")
        and item.get("country_code")
    }


def _destination_points(state: TravelAgentState) -> dict[str, tuple[float, float]]:
    """Each controlled destination's own point, by ``place_id``.

    Sibling of :func:`_destination_country_codes` above and deliberately shaped
    like it: admission needs to know *where* a destination is, not only which
    country it is in, because a nationwide place index answers with a same-named
    place in a neighbouring city.  The reading itself lives in
    ``services/destination_scope.py`` so the tool path, the authored-place ladder
    and admission all enforce one rule.
    """
    return destination_points(state.controlled_trip_identity)


def _recommended_tools(
    state: TravelAgentState,
    worker: str,
    excluded_tools: set[str],
    domain: ResearchDomain,
) -> list[str]:
    tools = [
        tool for tool in _ALTERNATIVE_TOOLS[worker] if tool not in excluded_tools
    ]
    if worker == "transport_researcher":
        if domain != ResearchDomain.LONG_DISTANCE_TRANSPORT:
            return tools
        # A long-distance leg can only be grounded by an intercity Provider.
        # Local routing tools return a commuting itinerary that can never carry
        # this responsibility, so they are dropped and the intercity Providers
        # lead the recommendation order.
        return [
            tool
            for tool in dict.fromkeys(
                [*_LONG_DISTANCE_ROUTE_TOOLS, *_ALTERNATIVE_TOOLS[worker]]
            )
            if tool not in excluded_tools and tool not in _LOCAL_ONLY_ROUTE_TOOLS
        ]
    # amap covers mainland China and nothing else, so a trip that leaves it must
    # not be handed an amap-only round.  One rule, both place workers: the lodging
    # and the Visit/Dining paths make the *same* per-destination either/or, and
    # writing it once here is what keeps the Gate's recommendation from disagreeing
    # with the worker's own provider routing.
    country_codes = set(_destination_country_codes(state).values())
    if country_codes and country_codes != {"cn"}:
        tools = [
            tool
            for tool in tools
            if tool not in {"maps_text_search", "maps_search_detail"}
        ]
    return tools


def _gap_id(*parts: object) -> str:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:18]
    return f"candidate_gap_{digest}"


def _candidate_gaps(
    packet: ResearchPacket,
    candidate: ResearchCandidate,
    missing_fields: Iterable[str],
) -> list[CandidateResearchGap]:
    """Name the identity fields that keep one candidate out of the catalog."""
    return [
        CandidateResearchGap(
            gap_id=_gap_id(
                packet.worker_kind,
                candidate.candidate_id,
                "missing_comparison_fact",
                field_path,
            ),
            worker_kind=packet.worker_kind,
            generation_id=packet.generation_id,
            reason="missing_comparison_fact",
            candidate_id=candidate.candidate_id,
            field_path=field_path,
            destination_id=candidate.destination_id,
        )
        for field_path in missing_fields
    ]


def build_candidate_catalog(
    state: TravelAgentState,
    *,
    packets: Iterable[ResearchPacket] | None = None,
) -> tuple[RecommendationCatalog, list[WeatherImpact], list[CandidateResearchGap]]:
    """Build a deterministic catalog from current packets and Gate-owned observations."""

    packets = list(packets) if packets is not None else _catalog_packets(state)
    if state.planning_generation is None:
        raise CandidateGateIntegrityError(
            "missing_planning_generation",
            "candidate catalog requires a planning generation",
        )
    if state.intent_spec is None or state.research_query_plan is None:
        raise CandidateGateIntegrityError(
            "missing_intent_research_contract",
            "candidate catalog requires IntentSpec and ResearchQueryPlan",
        )
    if (
        state.research_query_plan.generation_id
        != state.planning_generation.generation_id
        or state.research_query_plan.intent_spec_revision != state.intent_spec_revision
    ):
        raise CandidateGateIntegrityError(
            "stale_research_query_plan",
            "candidate catalog received a stale ResearchQueryPlan",
        )
    packets = _apply_candidate_caps(
        packets,
        state.research_query_plan.per_domain_candidate_caps,
    )
    generation_id = state.planning_generation.generation_id
    risk_profile = risk_profile_from_constraint_pack(state.constraint_pack)
    engine = WeatherImpactEngine()
    updated_packets: list[ResearchPacket] = []
    admissions = []
    impacts: list[WeatherImpact] = []
    gaps: list[CandidateResearchGap] = []
    destination_country_codes = _destination_country_codes(state)
    controlled_destination_points = _destination_points(state)
    hard_constraints = (
        state.constraint_pack.get("hard_constraints") or []
        if isinstance(state.constraint_pack, Mapping)
        else []
    )

    for packet in packets:
        worker = packet.worker_kind
        if packet.generation_id != generation_id:
            raise CandidateGateIntegrityError(
                "stale_planning_generation",
                f"{worker} packet belongs to another planning generation",
                worker_kind=worker,
            )
        if packet.constraint_pack_revision != state.constraint_pack_revision:
            raise CandidateGateIntegrityError(
                "stale_constraint_revision",
                f"{worker} packet uses stale constraint revision",
                worker_kind=worker,
            )
        if packet.fact_data_revision != state.fact_data_revision:
            raise CandidateGateIntegrityError(
                "stale_fact_revision",
                f"{worker} packet uses stale fact revision",
                worker_kind=worker,
            )
        # The model may provide evidence and a proposed verdict, but only this
        # Gate may bind it to the exact durable constraint pack and Fact
        # revision that produced the catalog admission.
        packet = _normalize_lodging_price_evidence(packet)
        packet = bind_candidate_constraint_gate_attestations(
            packet,
            constraint_pack=state.constraint_pack,
        )
        updated_candidates: list[ResearchCandidate] = []
        for candidate in packet.candidates:
            candidate_facts = [
                fact
                for fact in packet.fact_assertions
                if fact.entity_ref.entity_id == candidate.candidate_id
            ]
            candidate_impacts: list[WeatherImpact] = []
            days = _weather_days(state, candidate)
            for day in days:
                candidate_impacts.extend(
                    engine.evaluate(
                        weather_day=day,
                        target_ref=_target_ref(candidate),
                        sensitivity=candidate.weather_sensitivity,
                        risk_profile=risk_profile,
                    )
                )
            candidate = candidate.model_copy(
                update={
                    "weather_impact_ids": [
                        impact.weather_impact_id for impact in candidate_impacts
                    ]
                }
            )
            updated_candidates.append(candidate)
            impacts.extend(candidate_impacts)
            admission = admit_candidate(
                candidate,
                fact_data_revision=state.fact_data_revision,
                weather_data_revision=(
                    state.weather_context.weather_data_revision if state.weather_context else 0
                ),
                weather_impacts=candidate_impacts,
                weather_evaluated_dates=[
                    day.date for day in days if day.data_kind != "unavailable"
                ],
                expected_destination_country_code=destination_country_codes.get(
                    candidate.destination_id
                ),
                destination_point=controlled_destination_points.get(
                    candidate.destination_id
                ),
                identity_fact_values={
                    field_path: [
                        fact.asserted_value
                        for fact in candidate_facts
                        if fact.field_path == field_path
                    ]
                    for field_path in {fact.field_path for fact in candidate_facts}
                },
                hard_constraints=hard_constraints,
                candidate_facts=candidate_facts,
                source_records=packet.source_records,
            )
            admissions.append(admission)
            # Admission is the last place a grounded candidate can disappear.
            # Without this line, "candidates arrived but were rejected" and
            # "no candidate ever arrived" look identical in the log.
            logger.info(
                "Candidate admission | worker=%s candidate=%s kind=%s status=%s "
                "missing=%s budget_fit=%.2f constraint_fit=%.2f",
                worker,
                candidate.candidate_id,
                candidate.candidate_kind,
                admission.status,
                ",".join(admission.missing_field_paths) or "-",
                admission.fit_scores.budget_fit,
                admission.fit_scores.constraint_fit,
            )
            gaps.extend(
                _candidate_gaps(packet, candidate, admission.missing_field_paths)
            )
        updated_packets.append(packet.model_copy(update={"candidates": updated_candidates}))

    catalog = RecommendationCatalog(
        generation_id=generation_id,
        intent_spec_revision=state.intent_spec_revision,
        research_query_plan_id=state.research_query_plan.query_plan_id,
        fact_data_revision=state.fact_data_revision,
        weather_data_revision=(
            state.weather_context.weather_data_revision if state.weather_context else 0
        ),
        research_packets=updated_packets,
        admission_results=admissions,
        candidate_discovery_records=[
            record
            for packet in updated_packets
            for record in packet.candidate_discovery_records
        ],
    )
    return catalog, list({item.weather_impact_id: item for item in impacts}.values()), gaps


def _required_workers(state: TravelAgentState) -> set[str]:
    return {
        strip_round_suffix(agent)
        for group in state.execution_plan
        for agent in group
        if strip_round_suffix(agent) in _RESEARCH_WORKERS
    }


def _required_candidate_kinds(state: TravelAgentState) -> dict[str, set[str]]:
    """Translate controlled product choices into non-compensating domain coverage.

    The default ``balanced / 经典均衡`` product choice still promises a real
    sightseeing anchor.  It must therefore require an admitted Visit instead
    of letting Dining-only research satisfy a multi-day itinerary.
    """
    required = required_physical_candidate_kinds(state.controlled_trip_identity)
    return {"destination_researcher": required} if required else {}


def _intent_research_gaps(
    state: TravelAgentState,
    catalog: RecommendationCatalog,
    uncovered_intent_ids: Iterable[str],
) -> list[CandidateResearchGap]:
    if state.intent_spec is None or state.planning_generation is None:
        return []
    target_scope = {
        IntentTarget.VISIT: ("destination_researcher", ResearchDomain.VISIT),
        IntentTarget.DINING: ("destination_researcher", ResearchDomain.DINING),
        IntentTarget.LODGING: ("accommodation_researcher", ResearchDomain.LODGING),
        IntentTarget.LOCAL_TRANSPORT: (
            "transport_researcher",
            ResearchDomain.LOCAL_TRANSPORT,
        ),
        IntentTarget.LONG_DISTANCE_TRANSPORT: (
            "transport_researcher",
            ResearchDomain.LONG_DISTANCE_TRANSPORT,
        ),
    }
    intent_index = {intent.intent_id: intent for intent in state.intent_spec.active_items}
    destination_ids = [
        str(destination.get("place_id") or "")
        for destination in (state.controlled_trip_identity or {}).get(
            "destinations", []
        )
        if isinstance(destination, Mapping) and destination.get("place_id")
    ]
    gaps: list[CandidateResearchGap] = []
    for intent_id in sorted(set(uncovered_intent_ids)):
        intent = intent_index.get(intent_id)
        scope = target_scope.get(intent.target) if intent is not None else None
        if (
            intent is None
            or scope is None
            or (
                intent.strength is not IntentStrength.HARD
                and intent.priority < 70
            )
        ):
            continue
        matches = [
            match
            for match in catalog.candidate_intent_matches
            if match.intent_id == intent_id
        ]
        reason = (
            "insufficient_intent_evidence"
            if any(match.status is IntentMatchStatus.UNKNOWN for match in matches)
            else "missing_intent_candidate"
        )
        worker, domain = scope
        destination_id = destination_ids[0] if destination_ids else None
        gaps.append(
            CandidateResearchGap(
                gap_id=_gap_id(worker, reason, intent_id, destination_id or "all"),
                worker_kind=worker,
                generation_id=state.planning_generation.generation_id,
                reason=reason,
                intent_id=intent_id,
                destination_id=destination_id,
                desired_candidate_count=2,
                research_domain=domain,
            )
        )
    return gaps


def _long_distance_gaps(
    state: TravelAgentState,
    catalog: RecommendationCatalog,
    required_workers: set[str],
) -> list[CandidateResearchGap]:
    """Require each exact outbound/return long-distance responsibility."""
    if "transport_researcher" not in required_workers:
        return []
    identity = state.controlled_trip_identity or {}
    if not isinstance(identity, dict):
        return []
    legs = build_required_long_distance_legs(
        identity,
        cross_day_return_required=explicit_cross_day_return_required(
            state.user_query or ""
        ),
    )
    if not legs:
        return []

    candidate_index = catalog.candidate_index()
    admitted_scope_ids = {
        candidate.provider_evidence_scope_id
        for admission in catalog.admission_results
        if admission.status == "passed" and admission.selection_slot_id is None
        and isinstance(
            candidate := candidate_index[admission.candidate_id],
            TransportCandidate,
        )
        and candidate.transport_class == "long_distance"
        and candidate.provider_evidence_scope_id is not None
    }
    assignments = build_provider_evidence_assignments(
        run_id=state.run_id,
        constraint_pack_revision=state.constraint_pack_revision,
        worker_kind="transport_researcher",
        controlled_trip_identity=identity,
        prior_scope_attempts=scope_attempt_numbers(state.provider_evidence_outcomes),
        transport_classes=["long_distance"],
        long_distance_legs=legs,
    )
    return [
        CandidateResearchGap(
            gap_id=_gap_id(
                "transport_researcher",
                "missing_long_distance_anchor",
                assignment.scope.scope_id,
            ),
            worker_kind="transport_researcher",
            generation_id=state.planning_generation.generation_id,
            reason="missing_candidate",
            field_path="transport_class.long_distance",
            destination_id=assignment.scope.route_leg.to_place_id,
            provider_evidence_scope_id=assignment.scope.scope_id,
        )
        for assignment in assignments
        if assignment.scope.scope_id not in admitted_scope_ids
    ]


def _trip_duration_days(state: TravelAgentState) -> int:
    identity = state.controlled_trip_identity or {}
    try:
        start = date.fromisoformat(str(identity.get("start_date") or ""))
        end = date.fromisoformat(str(identity.get("end_date") or ""))
    except ValueError:
        if state.weather_context is None:
            return 0
        start = state.weather_context.trip_start_date
        end = state.weather_context.trip_end_date
    return (end - start).days + 1 if end >= start else 0


def _long_distance_anchor_dates(
    state: TravelAgentState,
    candidates: Iterable[ResearchCandidate],
) -> set[date]:
    """Count service dates, not competing route options, as Day anchors."""
    identity = state.controlled_trip_identity or {}
    try:
        start = date.fromisoformat(str(identity.get("start_date") or ""))
        end = date.fromisoformat(str(identity.get("end_date") or ""))
    except ValueError:
        if state.weather_context is None:
            return set()
        start = state.weather_context.trip_start_date
        end = state.weather_context.trip_end_date
    if end < start:
        return set()

    anchors: set[date] = set()
    for candidate in candidates:
        if not isinstance(candidate, TransportCandidate):
            continue
        if candidate.transport_class != "long_distance":
            continue
        service_dates = [
            timestamp.date()
            for timestamp in (candidate.departure_at, candidate.arrival_at)
            if timestamp is not None and start <= timestamp.date() <= end
        ]
        if service_dates:
            # One canonical leg contributes at most one composition anchor until
            # explicit cross-Day departure/arrival projections are materialized.
            anchors.add(service_dates[0])
    return anchors


def _itinerary_coverage_gaps(
    state: TravelAgentState,
    catalog: RecommendationCatalog,
    required_workers: set[str],
) -> tuple[list[CandidateResearchGap], int]:
    """Prove the admitted catalog can populate every Day before planning.

    Visit/Dining candidates are globally single-use and a local Transport cannot
    occupy a Day without a physical stop. A long-distance leg may be a valid
    travel-only Day, so it contributes one possible Day anchor. This is a
    necessary precondition, not a ranking heuristic: if it fails, no legal
    ``ItineraryCompositionDraft`` can exist without inventing or repeating an
    entity.
    """
    planned_agents = {
        strip_round_suffix(agent)
        for group in state.execution_plan
        for agent in group
    }
    if (
        "itinerary_planner" not in planned_agents
        or "destination_researcher" not in required_workers
    ):
        return [], 0

    candidate_index = catalog.candidate_index()
    passed = [
        candidate_index[admission.candidate_id]
        for admission in catalog.admission_results
        if admission.status == "passed" and admission.selection_slot_id is None
    ]
    physical = [
        candidate
        for candidate in passed
        if candidate.candidate_kind in {"visit", "dining"}
    ]
    long_distance = [
        candidate
        for candidate in passed
        if isinstance(candidate, TransportCandidate)
        and candidate.transport_class == "long_distance"
    ]
    gaps: list[CandidateResearchGap] = []
    destination_ids = [
        str(item.get("place_id") or "")
        for item in (state.controlled_trip_identity or {}).get("destinations", [])
        if isinstance(item, dict) and item.get("place_id")
    ]
    physical_destinations = {candidate.destination_id for candidate in physical}
    for destination_id in destination_ids:
        if destination_id in physical_destinations:
            continue
        gaps.append(
            CandidateResearchGap(
                gap_id=_gap_id(
                    "destination_researcher",
                    "missing_destination_physical_stop",
                    destination_id,
                ),
                worker_kind="destination_researcher",
                generation_id=state.planning_generation.generation_id,
                reason="missing_candidate",
                field_path="itinerary.destination_physical_stop",
                destination_id=destination_id,
            )
        )

    duration_days = _trip_duration_days(state)
    long_distance_anchor_dates = _long_distance_anchor_dates(state, long_distance)
    shortfall = max(
        duration_days - len(physical) - len(long_distance_anchor_dates),
        0,
    )
    if shortfall:
        gaps.append(
            CandidateResearchGap(
                gap_id=_gap_id(
                    "destination_researcher",
                    "missing_day_anchor",
                    duration_days,
                    shortfall,
                ),
                worker_kind="destination_researcher",
                generation_id=state.planning_generation.generation_id,
                reason="missing_candidate",
                field_path="itinerary.day_coverage",
                destination_id=destination_ids[0] if len(destination_ids) == 1 else None,
            )
        )
    return gaps, shortfall


def _domain_for_candidate_gap(
    gap: CandidateResearchGap,
    candidate_index: Dict[str, ResearchCandidate],
) -> ResearchDomain:
    field_path = str(gap.field_path or "")
    if field_path.startswith("candidate_kind.dining"):
        return ResearchDomain.DINING
    if field_path.startswith("candidate_kind.visit"):
        return ResearchDomain.VISIT
    if field_path.startswith("transport_class.long_distance"):
        return ResearchDomain.LONG_DISTANCE_TRANSPORT
    if field_path.startswith("itinerary.connector."):
        return ResearchDomain.LOCAL_TRANSPORT
    candidate = candidate_index.get(str(gap.candidate_id or ""))
    if candidate is not None:
        if candidate.candidate_kind == "dining":
            return ResearchDomain.DINING
        if candidate.candidate_kind == "lodging":
            return ResearchDomain.LODGING
        if candidate.candidate_kind == "transport":
            return (
                ResearchDomain.LONG_DISTANCE_TRANSPORT
                if isinstance(candidate, TransportCandidate)
                and candidate.transport_class == "long_distance"
                else ResearchDomain.LOCAL_TRANSPORT
            )
    if gap.worker_kind == "accommodation_researcher":
        return ResearchDomain.LODGING
    if gap.worker_kind == "transport_researcher":
        return ResearchDomain.LOCAL_TRANSPORT
    return ResearchDomain.VISIT


def _classify_candidate_gaps(
    gaps: Iterable[CandidateResearchGap],
    candidate_index: Dict[str, ResearchCandidate],
    *,
    prior_gaps: Iterable[CandidateResearchGap] = (),
) -> list[CandidateResearchGap]:
    prior_by_id = {gap.gap_id: gap for gap in prior_gaps}
    classified: list[CandidateResearchGap] = []
    for gap in gaps:
        prior = prior_by_id.get(gap.gap_id)
        classified.append(
            gap.model_copy(
                update={
                    "gate_class": GateClass.COMPOSITION,
                    "research_domain": _domain_for_candidate_gap(gap, candidate_index),
                    # A graph self-transition may immediately schedule an
                    # independent domain after a circuit opens.  Keep the
                    # earlier gap's durable progress instead of rebuilding it
                    # as a fresh retry opportunity.
                    "status": prior.status if prior is not None else gap.status,
                    "attempted_signatures": list(
                        dict.fromkeys(
                            [
                                *(prior.attempted_signatures if prior is not None else []),
                                *gap.attempted_signatures,
                            ]
                        )
                    ),
                }
            )
        )
    return classified


def _stable_attribution_id(
    *,
    draft_id: str | None,
    gate_class: GateClass,
    disposition: GateDisposition,
    reason_code: str,
    domain: ResearchDomain | None,
    gap_ids: Iterable[str],
    failure_signature: str | None,
) -> str:
    material = "|".join(
        [
            draft_id or "",
            gate_class.value,
            disposition.value,
            reason_code,
            domain.value if domain else "",
            ",".join(sorted(set(gap_ids))),
            failure_signature or "",
        ]
    )
    return f"gate_failure_{hashlib.sha256(material.encode()).hexdigest()[:24]}"


def _record_attribution(
    state: TravelAgentState,
    *,
    gate_class: GateClass,
    disposition: GateDisposition,
    reason_code: str,
    domain: ResearchDomain | None = None,
    gap_ids: Iterable[str] = (),
    failure_signature: str | None = None,
    deterministic: bool = False,
    retry_attempt: int = 0,
) -> Dict[str, GateFailureAttribution]:
    ids = list(dict.fromkeys(gap_ids))
    draft_id = state.minimum_delivery_draft.draft_id if state.minimum_delivery_draft else None
    attribution = GateFailureAttribution(
        attribution_id=_stable_attribution_id(
            draft_id=draft_id,
            gate_class=gate_class,
            disposition=disposition,
            reason_code=reason_code,
            domain=domain,
            gap_ids=ids,
            failure_signature=failure_signature,
        ),
        gate_class=gate_class,
        disposition=disposition,
        reason_code=reason_code,
        draft_id=draft_id,
        research_domain=domain,
        gap_ids=ids,
        failure_signature=failure_signature,
        deterministic=deterministic,
        retry_attempt=retry_attempt,
        recorded_at=datetime.now(timezone.utc),
    )
    records = dict(state.gate_failure_attributions or {})
    records[attribution.attribution_id] = attribution
    return records


def _placement_skeleton_verdict(exc: ItineraryCompositionError) -> str:
    """The gate's bounded verdict on a skeleton no connector comes out of.

    The extraction error already names the day, the adjacency and the reason;
    the repair round needs exactly that, in the shape the planner's own failure
    context arrives in. ``last_error`` is the shared channel every node writes,
    so the verdict travels on the gate's own field instead.
    """
    return bounded_repair_context(
        "placement_skeleton/connector_extraction",
        f"{type(exc).__name__}: {exc}",
    )


def _composition_repair_update(
    state: TravelAgentState,
    update: Dict[str, Any],
) -> Dict[str, Any]:
    """Spend the run's one composition repair, or release the current catalog."""

    out = apply_composition_repair_budget(
        state,
        update,
        route_key="candidate_gate_route",
        exhausted_route="passed",
    )
    if out["candidate_gate_route"] == "passed":
        out["candidate_gate_status"] = "passed"
    return out


def _observe_deadline(
    state: TravelAgentState,
) -> tuple[Any | None, DeadlineObservation | None]:
    deadline = state.run_deadline
    if deadline is None:
        return None, None
    return observe_run_deadline(deadline)


_LONG_DISTANCE_EVIDENCE_REQUIREMENT = (
    "必须使用真实外部 Provider 结果，"
    "具体到可核验的出发/到达端点与时间。实时班次不可得时明确保留缺口，"
    "禁止用市内路线、空路线或常识描述代替。"
)


def _long_distance_leg_clause(leg: ProviderRouteLegScope) -> str:
    """Spell out one exact long-distance responsibility, field by field."""
    return (
        f"leg_role={leg.leg_role}，"
        f"from_place_id={leg.from_place_id}，"
        f"to_place_id={leg.to_place_id}，"
        f"service_date={leg.service_date.isoformat()}，"
        f"cross_day_required={str(leg.cross_day_required).lower()}"
    )


def _long_distance_instruction(legs: Sequence[ProviderRouteLegScope]) -> str:
    """Name every long-distance responsibility this one research round owes."""
    if not legs:
        return ""
    if len(legs) == 1:
        return (
            " 本轮只补一个精确 long-distance 责任："
            f"{_long_distance_leg_clause(legs[0])}。"
            f"{_LONG_DISTANCE_EVIDENCE_REQUIREMENT}"
        )
    enumerated = "；".join(
        f"[{index}] {_long_distance_leg_clause(leg)}"
        for index, leg in enumerate(legs, start=1)
    )
    return (
        f" 本轮必须补齐 {len(legs)} 个精确 long-distance 责任：{enumerated}。"
        f"{_LONG_DISTANCE_EVIDENCE_REQUIREMENT}"
        "每一条责任都要单独给出结果，不得只覆盖其中一条。"
    )


def _deterministically_rejected_candidate_ids(
    catalog: RecommendationCatalog,
    packet_workers: Mapping[str, str],
    worker: str,
) -> list[str]:
    """One worker's candidates whose rejection no further research can change.

    A targeted round is bought to produce something the previous round did not
    have.  ``require_current_candidate`` tells the Worker the round must come back
    with the candidate it is scoped to — correct while the missing field is a fact
    somebody can go and look up, and a guaranteed no-op when it is a verdict about
    the candidate's own identity: a hotel 80 km outside the destination stays 80 km
    outside it however many rounds ask.  Naming those ids here is what turns "bring
    that hotel back" into "bring a hotel that is not that one".
    ``excluded_candidate_ids`` is enforced in three places downstream — the
    selection enum stops offering them, the repair prompt names them, and
    ``parse_research_packet_output`` refuses a packet that repeats one — and it had
    **no producer at all** repo-wide until this.
    """
    return sorted(
        {
            admission.candidate_id
            for admission in catalog.admission_results
            if admission.status == "insufficient_for_admission"
            and packet_workers.get(admission.candidate_id) == worker
            and DETERMINISTIC_IDENTITY_VERDICT_FIELD_PATHS.intersection(
                admission.missing_field_paths
            )
        }
    )


def _worker_retry_scope_prefix(worker_kind: str) -> str:
    """The ledger-key prefix every gap scope of one worker shares."""
    return f"{worker_kind}|gap|"


def _retry_scope_for_gap(
    gap: CandidateResearchGap,
) -> str:
    """Return the retry ledger key for one product gap, never a whole worker."""
    material = "|".join(
        [
            gap.worker_kind,
            gap.gap_id,
            gap.field_path or "",
            gap.candidate_id or "",
            gap.research_domain.value if gap.research_domain else "",
        ]
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    domain = gap.research_domain.value if gap.research_domain else "visit"
    return f"{_worker_retry_scope_prefix(gap.worker_kind)}{digest}|domain:{domain}"


def _worker_targeted_research_call_spent(
    state: TravelAgentState,
    worker_kind: str,
) -> bool:
    """Whether the durable ledger already records a targeted call for a worker."""
    prefix = _worker_retry_scope_prefix(worker_kind)
    return any(
        scope.startswith(prefix) and int(count) >= _MAX_TARGETED_RESEARCH_ATTEMPTS
        for scope, count in (state.candidate_gate_attempts or {}).items()
    )


def _domain_research_budget(
    state: TravelAgentState,
    domain: ResearchDomain,
) -> int:
    """How many targeted research calls one domain's structural load is worth.

    The budget counts the responsibilities the *controlled identity* hands the
    domain, never the ones still unmet.  The attempt ledger is cumulative and
    durable, so a budget that shrank as responsibilities were satisfied would
    deny the last one the very second chance this budget exists to grant, and a
    re-admitted candidate could buy a fresh round every time it reopened.  A
    Run's identity is locked, which is what makes comparing it against a
    monotone ledger sound.
    """

    identity = state.controlled_trip_identity or {}
    if not isinstance(identity, dict):
        return _MAX_TARGETED_RESEARCH_ATTEMPTS
    if domain is ResearchDomain.LONG_DISTANCE_TRANSPORT:
        # Only the number of legs is read here.  ``cross_day_required`` widens
        # one leg's research scope and never changes how many legs the round
        # trip owes, so the request wording stays out of the budget.
        legs = build_required_long_distance_legs(
            identity, cross_day_return_required=False
        )
        return max(len(legs), _MAX_TARGETED_RESEARCH_ATTEMPTS)
    if domain is ResearchDomain.VISIT:
        destinations = [
            item
            for item in (identity.get("destinations") or ())
            if isinstance(item, dict) and str(item.get("place_id") or "").strip()
        ]
        return max(len(destinations), _MAX_TARGETED_RESEARCH_ATTEMPTS)
    if domain is ResearchDomain.LOCAL_TRANSPORT:
        # Same reading as the two above: the structural load is the number of
        # adjacencies the itinerary owes a route.  A single round was enough while
        # the composition never moved, and it does move — a repair that swaps one
        # dining branch for another leaves every researched endpoint pair stale, and
        # with one round spent there is no second chance to ask about the new pair;
        # the new pair then ships as a model-invented duration with the admitted
        # Provider routes sitting unused in the catalog.
        return max(
            len(state.local_connector_gaps or ()), _MAX_TARGETED_RESEARCH_ATTEMPTS
        )
    return _MAX_TARGETED_RESEARCH_ATTEMPTS


def _gap_research_exhausted(
    state: TravelAgentState,
    *,
    gap: CandidateResearchGap,
) -> bool:
    if gap.status == "exhausted":
        return True
    domain = gap.research_domain or ResearchDomain.VISIT
    attempts = state.candidate_gate_attempts or {}
    # A domain's targeted research is capped by its structural load, and each
    # gap still answers for its own key.  Reading a single spent call as the
    # whole domain's answer made the per-gap check below unreachable: the
    # second leg of a round trip was declared exhausted while its own key still
    # read zero.  Individual gap status and attribution stay separate from this
    # ledger.
    domain_marker = f"|domain:{domain.value}"
    domain_spent = sum(
        int(count) for scope, count in attempts.items() if domain_marker in scope
    )
    if domain_spent >= _domain_research_budget(state, domain):
        return True
    if gap.field_path == _COMPANION_PACKED_FIELD_PATH:
        # A packed domain researches every open responsibility in one round and
        # charges the round to whichever gap led it, and which one leads is a
        # digest ordering (``_gap_id`` over the scope id, itself keyed by
        # run_id).  Reading that charge as one leg's own answer would hand the
        # second round to a leg by luck: the outbound and the return would swap
        # places from Run to Run.  The domain budget above is the honest cap for
        # a set researched together, and every round only ever asks for the legs
        # still unadmitted.
        return False
    return (
        int(attempts.get(_retry_scope_for_gap(gap), 0))
        >= _MAX_TARGETED_RESEARCH_ATTEMPTS
    )


def _research_gap_priority(gap: CandidateResearchGap) -> int:
    if gap.intent_id:
        return 0
    field_path = gap.field_path or ""
    if field_path.startswith((
        "candidate_kind.",
        "transport_class.",
        "itinerary.connector.",
    )):
        return 0
    if gap.candidate_id:
        return 1
    if field_path.startswith("itinerary."):
        return 2
    return 3


def _ordered_research_gaps(
    gaps: Iterable[CandidateResearchGap],
    blocked_workers: Iterable[str],
) -> list[CandidateResearchGap]:
    """Order repair targets deterministically without letting summary gaps win.

    A generic ``missing_candidate`` worker marker is diagnostic context, not a
    distinct product contract.  If a worker has concrete Visit/Dining/transport
    gaps, only those may consume a targeted research attempt.
    """
    ordered: list[CandidateResearchGap] = []
    for worker in sorted(set(blocked_workers)):
        worker_gaps = [gap for gap in gaps if gap.worker_kind == worker]
        scoped = [gap for gap in worker_gaps if _research_gap_priority(gap) < 3]
        candidates = scoped or worker_gaps
        ordered.extend(
            sorted(
                candidates,
                key=lambda gap: (
                    _research_gap_priority(gap),
                    gap.research_domain.value if gap.research_domain else "",
                    gap.gap_id,
                ),
            )
        )
    return ordered


def _select_research_gap(
    state: TravelAgentState,
    *,
    gaps: Iterable[CandidateResearchGap],
    blocked_workers: Iterable[str],
    excluded_domains: Iterable[ResearchDomain] = (),
) -> CandidateResearchGap | None:
    """Return the next gap that still has a targeted research attempt left."""
    unavailable_domains = set(excluded_domains)
    return next(
        (
            gap
            for gap in _ordered_research_gaps(gaps, blocked_workers)
            if (gap.research_domain or ResearchDomain.VISIT)
            not in unavailable_domains
            and gap.status not in {"exhausted", "resolved"}
            and not _gap_research_exhausted(state, gap=gap)
        ),
        None,
    )


def worker_targeted_research_exhausted(
    state: TravelAgentState,
    worker_kind: str,
) -> bool:
    """Whether this gate would still spend a targeted research call on a worker.

    Candidate Gate owns the targeted-research budget, so the question is
    answered here for every caller. With gap rows present the answer is the
    gate's own selection: a worker is exhausted once none of its gaps can buy
    another call.

    An *empty* gap list is the ambiguous case, and the two readings route in
    opposite directions. The gap list is rebuilt from the current catalog on
    every pass, so a target the gate already funded and satisfied simply stops
    appearing in it. That worker's research is settled, and handing its stale
    round-one failure back would only produce another release — the ring
    ``artifact gate → Candidate Gate → dispatcher`` is pure code, so a bounce
    with nothing to do never terminates. A worker the gate has genuinely never
    classified is the opposite: the first hand-back is how its targeted research
    starts. The durable attempt ledger, keyed per worker, is what separates
    them.
    """
    worker_gaps = [
        gap for gap in state.candidate_research_gaps if gap.worker_kind == worker_kind
    ]
    if not worker_gaps:
        return _worker_targeted_research_call_spent(state, worker_kind)
    return (
        _select_research_gap(
            state,
            gaps=worker_gaps,
            blocked_workers=[worker_kind],
        )
        is None
    )


def worker_research_satisfied_by_a_later_round(
    state: TravelAgentState,
    agent_key: str,
) -> bool:
    """Whether a round after ``agent_key`` already answered for the same worker.

    Candidate Gate owns the targeted-research budget, so it also owns the
    difference between the two ways that budget reaches zero. ``research_packets``
    is keyed per round: the round that failed keeps its terminal status and its
    empty slot forever, while a targeted retry lands under the next key. The
    highest round is the one the catalog was built from, so a packet there under
    a legal completion status means the domain is settled — a caller judging the
    earlier key is looking at a spent budget that a *result* closed.
    """
    packets = state.research_packets or {}
    latest = _latest_packets(packets).get(strip_round_suffix(agent_key))
    if latest is None:
        return False
    statuses = state.agent_status or {}
    return any(
        key != agent_key
        and packet is latest
        and statuses.get(key) in {"completed", "partial"}
        for key, packet in packets.items()
    )


def _last_error_is_unambiguously_scoped_to_worker(
    state: TravelAgentState,
    worker: str,
) -> bool:
    """Whether the legacy global error can safely classify this worker.

    ``last_error`` predates the gate ledger and is reduced as a latest nonempty
    string, so it cannot distinguish concurrent worker failures.  It may only
    be used as a compatibility bridge when the current base worker is the
    *only* failed worker in state; packet-local failure evidence always wins.
    """
    failed_workers = {
        strip_round_suffix(agent_key)
        for agent_key, status in (state.agent_status or {}).items()
        if str(status).casefold() == "failed"
    }
    return failed_workers == {worker}


def _worker_failure_signals(
    state: TravelAgentState,
    *,
    packet: ResearchPacket | None,
    worker: str,
) -> list[ProviderFailureSignal]:
    """Classify how one worker's providers failed on its most recent round."""
    signals = _failure_signals(packet)
    if (
        not signals
        and state.last_error
        and _last_error_is_unambiguously_scoped_to_worker(state, worker)
        and is_provider_or_model_failure(state.last_error)
    ):
        classification = classify_provider_failure(str(state.last_error))
        signals = [
            ProviderFailureSignal(
                tool_name=worker,
                signature=(
                    f"{classification.category}:{worker}|provider:unknown|"
                    f"reason:{classification.reason_code}"
                ),
                reason_code=classification.reason_code,
                category=classification.category,
            )
        ]
    return signals


def _mark_scoped_gaps_exhausted(
    gaps: Iterable[CandidateResearchGap],
    *,
    gap_ids: Iterable[str],
    signatures: Iterable[str],
) -> list[CandidateResearchGap]:
    selected_ids = set(gap_ids)
    normalized_signatures = list(dict.fromkeys(signatures))
    return [
        gap.model_copy(
            update={
                "status": "exhausted",
                "attempted_signatures": list(
                    dict.fromkeys([*gap.attempted_signatures, *normalized_signatures])
                ),
            }
        )
        if gap.gap_id in selected_ids and gap.status != "resolved"
        else gap
        for gap in gaps
    ]


def _resolve_research_target(
    state: TravelAgentState,
    *,
    gaps: list[CandidateResearchGap],
    blocked_workers: Iterable[str],
    packets_by_worker: Dict[str, Any],
    attributions: Dict[str, Any],
    failure_ledger: Dict[str, list[str]],
) -> tuple[
    CandidateResearchGap | None,
    list[CandidateResearchGap],
    Dict[str, Any],
    Dict[str, list[str]],
    list[ProviderFailureSignal],
]:
    """Pick the gap this lap researches, closing deterministic dead ends on the way.

    Returns ``None`` for the gap when no blocked worker can buy another targeted
    call — every remaining gap is either spent, permanently failed, or resolved.
    That answer is needed **twice** in one lap: once before the connector pass
    (whether another domain will be researched instead) and once after it
    (whether the adjacencies it found can be researched).  It is one question, so
    it has one implementation; asking it two ways would let the two answers drift.
    """
    excluded_domains: set[ResearchDomain] = set()
    while True:
        primary_gap = _select_research_gap(
            state,
            gaps=gaps,
            blocked_workers=blocked_workers,
            excluded_domains=excluded_domains,
        )
        if primary_gap is None:
            return None, gaps, attributions, failure_ledger, []
        signals = _worker_failure_signals(
            state,
            packet=packets_by_worker.get(primary_gap.worker_kind),
            worker=primary_gap.worker_kind,
        )
        deterministic_signal = next(
            (signal for signal in signals if signal.category == "deterministic"),
            None,
        )
        if deterministic_signal is None:
            return primary_gap, gaps, attributions, failure_ledger, signals
        # A deterministic provider-contract failure repeats verbatim. Close this
        # domain and spend the remaining attempt on an independent one.
        gap_domain = primary_gap.research_domain or ResearchDomain.VISIT
        gaps = _mark_scoped_gaps_exhausted(
            gaps,
            gap_ids=[primary_gap.gap_id],
            signatures=[deterministic_signal.signature],
        )
        deterministic_scope = _retry_scope_for_gap(primary_gap)
        failure_ledger[deterministic_scope] = list(
            dict.fromkeys(
                [
                    *failure_ledger.get(deterministic_scope, []),
                    *(signal.signature for signal in signals),
                ]
            )
        )
        attributions = _record_attribution(
            state.model_copy(update={"gate_failure_attributions": attributions}),
            gate_class=GateClass.COMPOSITION,
            disposition=GateDisposition.TARGETED_RESEARCH,
            reason_code=deterministic_signal.reason_code,
            domain=gap_domain,
            gap_ids=[primary_gap.gap_id],
            failure_signature=deterministic_signal.signature,
            deterministic=True,
        )
        excluded_domains.add(gap_domain)


def _passed_update(
    state: TravelAgentState,
    *,
    base_update: Dict[str, Any],
    gaps: list[CandidateResearchGap],
    deadline: Any | None = None,
) -> Dict[str, Any]:
    """Hand the current catalog to composition and close remaining targets.

    Every candidate that named a concrete place is already in the catalog, so
    the gaps left here are research targets whose budget is spent. Marking them
    exhausted keeps the ledger honest across a checkpoint without holding the
    run at the gate.
    """
    update = {
        **base_update,
        "candidate_research_gaps": _mark_scoped_gaps_exhausted(
            gaps,
            gap_ids=[
                gap.gap_id
                for gap in gaps
                if gap.status != "resolved"
            ],
            signatures=(),
        ),
        "candidate_gate_status": "passed",
        "candidate_gate_route": "passed",
        # Explicitly retain the durable call ledger across a same-node
        # transition; direct-node callers and checkpoints must observe the
        # exact same bounded-budget state as LangGraph reducers do.
        "candidate_gate_attempts": dict(state.candidate_gate_attempts or {}),
    }
    if deadline is not None:
        update["run_deadline"] = deadline
    return update


def _catalog_contract_update(
    state: TravelAgentState,
    *,
    reason_code: str,
    message: str,
    worker_kind: str | None = None,
    base_update: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Record a catalog contract outcome and hand the run to composition.

    The reason code stays in the durable attribution ledger; the run continues
    to the typed artifact gate and on to projection.  This exit releases the
    run, so whatever the same pass already established — the catalog, its gaps,
    the aligned skeleton, the verdict it cleared — is what materialization then
    reads: callers that hold such an update pass it in.  Attributions from that
    update merge instead of being replaced; a deterministic circuit opened
    earlier in the same pass is equally durable.
    """
    accumulated = dict(base_update or {})
    gap_ids = [f"catalog:{worker_kind}:{reason_code}"] if worker_kind else [f"catalog:{reason_code}"]
    return {
        **accumulated,
        "candidate_gate_status": "passed",
        "candidate_gate_route": "passed",
        "gate_failure_attributions": _record_attribution(
            state.model_copy(
                update={
                    "gate_failure_attributions": accumulated.get(
                        "gate_failure_attributions",
                        state.gate_failure_attributions,
                    )
                }
            ),
            gate_class=GateClass.COMPOSITION,
            disposition=GateDisposition.COMPOSITION_REPAIR,
            reason_code=reason_code,
            gap_ids=gap_ids,
        ),
        "last_error": message,
    }


def _configurable_value(config: Any, key: str) -> Any:
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    return configurable.get(key)


def _normalize_lodging_price_evidence(packet: ResearchPacket) -> ResearchPacket:
    """Normalize every lodging price before Candidate Gate attests it.

    The shared admission helper governs both reference estimates and live
    quotes. This wrapper preserves packet-level ownership so the exact
    normalized Candidate is what later gets a Gate attestation.
    """

    candidates: list[ResearchCandidate] = []
    changed = False
    for candidate in packet.candidates:
        normalized = normalize_lodging_price_evidence(
            candidate,
            candidate_facts=[
                fact
                for fact in packet.fact_assertions
                if fact.entity_ref.entity_id == candidate.candidate_id
            ],
            source_records=packet.source_records,
        )
        changed = changed or normalized != candidate
        candidates.append(normalized)
    return packet.model_copy(update={"candidates": candidates}) if changed else packet


async def candidate_gate_node(
    state: TravelAgentState,
    config: Optional[RunnableConfig] = None,
) -> Dict[str, Any]:
    try:
        catalog, impacts, gaps = build_candidate_catalog(state)
    except CandidateGateIntegrityError as exc:
        return _catalog_contract_update(
            state,
            reason_code=exc.reason_code,
            message=str(exc),
            worker_kind=exc.worker_kind,
        )
    except (KeyError, ValueError) as exc:
        return _catalog_contract_update(
            state,
            reason_code="candidate_catalog_contract_invalid",
            message=str(exc),
        )

    if state.intent_spec is None:
        return _catalog_contract_update(
            state,
            reason_code="missing_intent_spec",
            message="candidate evaluation requires an IntentSpec",
            base_update={"recommendation_catalog": catalog},
        )
    matches, evaluation_cache = await evaluate_candidate_intents(
        catalog=catalog,
        intent_spec=state.intent_spec,
        llm=get_model_router().get_fast(),
        cache=state.candidate_intent_evaluation_cache,
    )
    ranking_scores = rank_candidates(
        catalog=catalog,
        intent_spec=state.intent_spec,
        matches=matches,
    )
    catalog = catalog.model_copy(
        update={
            "candidate_intent_matches": matches,
            "candidate_ranking_scores": ranking_scores,
        }
    )
    destination_count = len(
        [
            destination
            for destination in (state.controlled_trip_identity or {}).get(
                "destinations", []
            )
            if isinstance(destination, Mapping)
        ]
    )
    selection_plan = build_candidate_selection_plan(
        catalog=catalog,
        intent_spec=state.intent_spec,
        ranking_scores=ranking_scores,
        duration_days=_trip_duration_days(state),
        destination_count=destination_count,
    )

    packet_workers = {
        candidate.candidate_id: packet.worker_kind
        for packet in catalog.research_packets
        for candidate in packet.candidates
    }
    candidate_index = catalog.candidate_index()
    try:
        passed_by_worker: dict[str, int] = defaultdict(int)
        passed_kinds_by_worker: dict[str, set[str]] = defaultdict(set)
        for admission in catalog.admission_results:
            if admission.status != "passed":
                continue
            worker = packet_workers[admission.candidate_id]
            passed_by_worker[worker] += 1
            passed_kinds_by_worker[worker].add(
                candidate_index[admission.candidate_id].candidate_kind
            )
    except KeyError as exc:
        return _catalog_contract_update(
            state,
            reason_code="candidate_admission_reference_invalid",
            message=f"candidate admission references an unknown candidate: {exc}",
        )

    base_update: Dict[str, Any] = {
        "recommendation_catalog": catalog,
        "candidate_selection_plan": selection_plan,
        "candidate_intent_evaluation_cache": evaluation_cache,
        "weather_impacts": {impact.weather_impact_id: impact for impact in impacts},
    }
    required_workers = _required_workers(state)
    missing_workers = sorted(
        worker for worker in required_workers if passed_by_worker[worker] == 0
    )
    for worker in missing_workers:
        gaps.append(
            CandidateResearchGap(
                gap_id=_gap_id(worker, "missing_candidate"),
                worker_kind=worker,
                generation_id=state.planning_generation.generation_id,
                reason="missing_candidate",
            )
        )
    required_candidate_kinds = _required_candidate_kinds(state)
    coverage_workers: set[str] = set()
    for worker, required_kinds in required_candidate_kinds.items():
        if worker not in required_workers:
            continue
        for candidate_kind in sorted(
            required_kinds - passed_kinds_by_worker.get(worker, set())
        ):
            coverage_workers.add(worker)
            gaps.append(
                CandidateResearchGap(
                    gap_id=_gap_id(worker, "missing_candidate", candidate_kind),
                    worker_kind=worker,
                    generation_id=state.planning_generation.generation_id,
                    reason="missing_candidate",
                    field_path=f"candidate_kind.{candidate_kind}",
                )
            )
    long_distance_gaps = _long_distance_gaps(state, catalog, required_workers)
    itinerary_coverage_gaps, itinerary_physical_shortfall = _itinerary_coverage_gaps(
        state,
        catalog,
        required_workers,
    )
    intent_gaps = _intent_research_gaps(
        state,
        catalog,
        selection_plan.uncovered_intent_ids,
    )
    gaps.extend([*long_distance_gaps, *itinerary_coverage_gaps, *intent_gaps])
    blocked_workers = sorted(
        set(missing_workers)
        | coverage_workers
        | {gap.worker_kind for gap in long_distance_gaps}
        | {gap.worker_kind for gap in itinerary_coverage_gaps}
        | {gap.worker_kind for gap in intent_gaps}
    )

    # The research boundary is global.  Check it before any placement skeleton
    # or connector path can schedule more research, and close *all* known
    # remaining research targets in one atomic transition.
    try:
        observed_deadline, observation = _observe_deadline(state)
    except ValueError as exc:
        return _catalog_contract_update(
            state,
            reason_code="run_deadline_invalid",
            message=str(exc),
            base_update=base_update,
        )
    if observation is not None and observation.research_closed:
        return _passed_update(
            state,
            base_update=base_update,
            gaps=_classify_candidate_gaps(
                gaps,
                candidate_index,
                prior_gaps=state.candidate_research_gaps,
            ),
            deadline=observed_deadline,
        )

    planned_agents = {
        strip_round_suffix(agent)
        for group in state.execution_plan
        for agent in group
    }
    gaps = _classify_candidate_gaps(
        gaps,
        candidate_index,
        prior_gaps=state.candidate_research_gaps,
    )
    packets_by_worker = {
        packet.worker_kind: packet for packet in catalog.research_packets
    }
    attributions = dict(state.gate_failure_attributions or {})
    failure_ledger = {
        key: list(dict.fromkeys(values))
        for key, values in (state.candidate_gate_failure_signatures or {}).items()
    }
    # Which domain this lap researches is settled **before** the connector pass,
    # because that answer is the connector pass's own precondition.  Reading
    # ``blocked_workers`` here matters because that answer must be settled no
    # matter what: any other domain holding a gap it
    # can no longer research — a deterministic provider-contract failure, a spent
    # domain budget — kept this pass from ever running, and the gate then passed in
    # the same lap with ``local_connector_gaps`` empty.  The planner authors every
    # adjacency nobody measured, so one lodging dead end came out as invented
    # walking times between two Visit stops, and what the connector pass owes a
    # wait to is *actionable* research, not the bare existence of a gap.
    primary_gap, gaps, attributions, failure_ledger, signals = _resolve_research_target(
        state,
        gaps=gaps,
        blocked_workers=blocked_workers,
        packets_by_worker=packets_by_worker,
        attributions=attributions,
        failure_ledger=failure_ledger,
    )
    base_update["candidate_research_gaps"] = gaps
    connector_gaps: list[LocalConnectorGap] = []
    unresolved_connector_gaps: list[LocalConnectorGap] = []
    # A materialized workspace *is* the itinerary, and the planner reads the same
    # state: with one in hand it recomposes and writes no skeleton. Asking for a
    # skeleton here would come back identically every lap, so the composition
    # passes are owed only while no workspace exists.
    # Two things make a skeleton request worth making, and a spent domain used to
    # stand in for both of them:
    #
    #   * something to place — a skeleton is composed out of admitted candidates,
    #     so an empty catalog buys a planner call, a failed status and the run's
    #     one composition repair, on exactly the Runs with least left to give;
    #   * a composition the planner's own contract can accept — it *requires* the
    #     long-distance legs owed on the handover dates
    #     (``ItineraryPlanner v2 failed: placement skeleton omits long-distance
    #     legs owed on handover dates``), so while one of those legs is still
    #     unfilled every skeleton is refused on arrival.  Measured: with both
    #     long-distance providers off, asking anyway spent the repair budget twice
    #     and turned a degraded delivery into ``run_failed``.
    #
    # Neither clause changes a Run that is merely healthy: a healthy Run has
    # candidates and has its legs.  They only decide the case that opened here.
    if (
        primary_gap is None
        and any(passed_by_worker.values())
        and not long_distance_gaps
        and "itinerary_planner" in planned_agents
        and state.trip_workspace_v2 is None
    ):
        if state.placement_skeleton is None:
            if state.agent_status.get("itinerary_planner") == "failed":
                return _composition_repair_update(
                    state,
                    {
                        **base_update,
                        "candidate_gate_status": "composition_repair",
                        "candidate_gate_route": "composition_repair",
                        "candidate_gate_failure_signatures": failure_ledger,
                        "gate_failure_attributions": _record_attribution(
                            state.model_copy(
                                update={"gate_failure_attributions": attributions}
                            ),
                            gate_class=GateClass.COMPOSITION,
                            disposition=GateDisposition.COMPOSITION_REPAIR,
                            reason_code="placement_skeleton_unavailable",
                        ),
                        "last_error": str(
                            state.last_error or "placement skeleton unavailable"
                        ),
                    },
                )
            assignments = dict(state.agent_assignments)
            assignments["itinerary_planner"] = {
                **dict(assignments.get("itinerary_planner") or {}),
                "required_candidate_kinds": sorted(
                    required_candidate_kinds.get("destination_researcher", set())
                ),
                "objective": (
                    "只选择已准入 Visit/Dining/Lodging 与日期匹配的 long-distance 主方案，"
                    "生成不含任何市内 Transport placement 的排期骨架；不得猜路线或端点。"
                ),
            }
            return {
                **base_update,
                "candidate_gate_status": "needs_research",
                "candidate_gate_route": "itinerary_planner",
                "candidate_gate_failure_signatures": failure_ledger,
                "gate_failure_attributions": attributions,
                "local_connector_gaps": [],
                "agent_assignments": assignments,
            }
        try:
            flexible_requests, required_flexible_pairs = connector_mode_requests_from_constraint_pack(
                state.placement_skeleton,
                state.constraint_pack,
            )
            connector_gaps = extract_local_connector_gaps(
                state.placement_skeleton,
                catalog,
                weather_data_revision=catalog.weather_data_revision,
                flexible_mode_requests=flexible_requests,
                required_flexible_mode_pairs=required_flexible_pairs,
                required_candidate_kinds=required_candidate_kinds.get(
                    "destination_researcher", set()
                ),
            )
        except ItineraryCompositionError as exc:
            return _composition_repair_update(
                state,
                {
                    **base_update,
                    "candidate_gate_failure_signatures": failure_ledger,
                    # A skeleton no connector can be extracted from is not a
                    # skeleton; dropping it sends the repair back to the first
                    # pass instead of materializing a shape that just failed.
                    "placement_skeleton": None,
                    # The planner composed that skeleton successfully, so its own
                    # failure channel is empty and this verdict is the only thing
                    # standing between the repair round and a verbatim retry.
                    "placement_skeleton_failure_context": (
                        _placement_skeleton_verdict(exc)
                    ),
                    "candidate_gate_status": "composition_repair",
                    "candidate_gate_route": "composition_repair",
                    "gate_failure_attributions": _record_attribution(
                        state.model_copy(
                            update={"gate_failure_attributions": attributions}
                        ),
                        gate_class=GateClass.COMPOSITION,
                        disposition=GateDisposition.COMPOSITION_REPAIR,
                        reason_code="placement_skeleton_invalid",
                    ),
                    "last_error": str(exc),
                },
            )
        # Connectors came out of this skeleton, so any earlier verdict is spent.
        # Clearing it here — the one place the gate judges a skeleton sound — is
        # what keeps a stale judgment out of every prompt further down the run.
        base_update["placement_skeleton_failure_context"] = None
        passed_ids = {
            admission.candidate_id
            for admission in catalog.admission_results
            if admission.status == "passed" and admission.selection_slot_id is None
        }
        passed_transport = [
            candidate
            for candidate_id, candidate in candidate_index.items()
            if candidate_id in passed_ids and isinstance(candidate, TransportCandidate)
        ]
        aligned_skeleton = align_skeleton_to_provider_routes(
            state.placement_skeleton,
            catalog,
            connector_gaps,
        )
        if aligned_skeleton != state.placement_skeleton:
            base_update["placement_skeleton"] = aligned_skeleton
            connector_gaps = extract_local_connector_gaps(
                aligned_skeleton,
                catalog,
                weather_data_revision=catalog.weather_data_revision,
                flexible_mode_requests=flexible_requests,
                required_flexible_mode_pairs=required_flexible_pairs,
                required_candidate_kinds=required_candidate_kinds.get(
                    "destination_researcher", set()
                ),
            )
        unresolved_connector_gaps = [
            gap
            for gap in connector_gaps
            if not any(
                connector_candidate_quality_error(candidate, gap) is None
                for candidate in passed_transport
            )
        ]
        base_update["local_connector_gaps"] = connector_gaps
        if unresolved_connector_gaps:
            gaps = [
                *gaps,
                # Classify only the new rows: the list above already carries this
                # lap's verdicts, and re-classifying it would read every status
                # back from the prior checkpoint and undo them.
                *_classify_candidate_gaps(
                    [
                        CandidateResearchGap(
                            gap_id=pending.gap_id,
                            worker_kind="transport_researcher",
                            generation_id=state.planning_generation.generation_id,
                            reason="missing_candidate",
                            field_path=f"itinerary.connector.{pending.gap_id}",
                            destination_id=pending.destination_id,
                        )
                        for pending in unresolved_connector_gaps
                    ],
                    candidate_index,
                    prior_gaps=state.candidate_research_gaps,
                ),
            ]
            # Every other worker's gap is settled by now — that is this pass's
            # precondition — so the adjacencies are the only thing left to buy.
            blocked_workers = ["transport_researcher"]
            (
                primary_gap,
                gaps,
                attributions,
                failure_ledger,
                signals,
            ) = _resolve_research_target(
                state,
                gaps=gaps,
                blocked_workers=blocked_workers,
                packets_by_worker=packets_by_worker,
                attributions=attributions,
                failure_ledger=failure_ledger,
            )

    base_update["candidate_research_gaps"] = gaps
    if attributions != (state.gate_failure_attributions or {}):
        base_update["gate_failure_attributions"] = attributions
    if failure_ledger != (state.candidate_gate_failure_signatures or {}):
        base_update["candidate_gate_failure_signatures"] = failure_ledger
    if primary_gap is None:
        # Only when it says something new.  A pass that republishes the current
        # assignments verbatim reads, from the dispatcher's side, exactly like a
        # worker being handed another round.
        if state.placement_skeleton is not None:
            final_assignments = dict(state.agent_assignments)
            final_assignments["itinerary_planner"] = {
                **dict(final_assignments.get("itinerary_planner") or {}),
                "required_candidate_kinds": sorted(
                    required_candidate_kinds.get("destination_researcher", set())
                ),
                "objective": "只用 placement skeleton 与通过质量门的 exact Provider routes 物化正式行程。",
            }
            base_update["agent_assignments"] = final_assignments
        if not blocked_workers:
            return {
                **base_update,
                "candidate_gate_status": "passed",
                "candidate_gate_route": "passed",
            }
        # Blocked workers that can no longer research: release the catalog and
        # close their ledger in the same transition.
        return _passed_update(state, base_update=base_update, gaps=gaps)

    worker = primary_gap.worker_kind
    domain = primary_gap.research_domain or ResearchDomain.VISIT
    scoped_gaps = [primary_gap]
    connector_gap = next(
        (
            gap
            for gap in unresolved_connector_gaps
            if gap.gap_id == primary_gap.gap_id
        ),
        None,
    )
    # Every adjacency this round, not just the one that led it.  A route between
    # two known points is a **lookup**, not a research question: amap answers it
    # for any pair of coordinates in mainland China, so there is nothing here for a
    # per-round budget to ration.  Handing over one gap at a time meant a trip with
    # four local adjacencies got one Provider-measured connector and three invented
    # durations — the local-route domain is granted a single targeted round, so the
    # one gap in this list *was* the whole coverage story.
    connector_round_gaps = (
        list(unresolved_connector_gaps) if connector_gap is not None else []
    )
    required_long_distance_legs = build_required_long_distance_legs(
        state.controlled_trip_identity or {},
        cross_day_return_required=explicit_cross_day_return_required(
            state.user_query or ""
        ),
    )
    long_distance_legs: list[ProviderRouteLegScope] = []
    if domain == ResearchDomain.LONG_DISTANCE_TRANSPORT:
        long_distance_scope_id = primary_gap.provider_evidence_scope_id
        if long_distance_scope_id is None and primary_gap.candidate_id is not None:
            scoped_candidate = catalog.candidate_index().get(primary_gap.candidate_id)
            if (
                isinstance(scoped_candidate, TransportCandidate)
                and scoped_candidate.transport_class == "long_distance"
            ):
                long_distance_scope_id = (
                    scoped_candidate.provider_evidence_scope_id
                )
        scoped_assignments = build_provider_evidence_assignments(
            run_id=state.run_id,
            constraint_pack_revision=state.constraint_pack_revision,
            worker_kind="transport_researcher",
            controlled_trip_identity=state.controlled_trip_identity or {},
            prior_scope_attempts=scope_attempt_numbers(
                state.provider_evidence_outcomes
            ),
            transport_classes=["long_distance"],
            long_distance_legs=required_long_distance_legs,
        )
        legs_by_scope_id = {
            assignment.scope.scope_id: assignment.scope.route_leg
            for assignment in scoped_assignments
            if assignment.scope.route_leg is not None
        }
        primary_long_distance_leg = legs_by_scope_id.get(long_distance_scope_id or "")
        if primary_long_distance_leg is None:
            return _catalog_contract_update(
                state,
                reason_code="candidate_gate_long_distance_scope_invalid",
                message="long-distance gap does not match a current exact route leg",
                base_update=base_update,
            )
        # The domain buys exactly one targeted round, so that round must carry
        # every long-distance responsibility still open in it.  The worker's
        # provider sweep already walks all of its assigned scopes, so widening
        # the assignment costs no extra model call and no extra attempt.
        companion_legs: Dict[str, ProviderRouteLegScope] = {}
        for gap in gaps:
            if gap.gap_id == primary_gap.gap_id:
                continue
            if gap.worker_kind != worker:
                continue
            if gap.field_path != "transport_class.long_distance":
                continue
            if gap.status not in {"open", "researching"}:
                continue
            companion_scope_id = gap.provider_evidence_scope_id
            if (
                companion_scope_id is None
                or companion_scope_id == long_distance_scope_id
                or companion_scope_id in companion_legs
            ):
                continue
            companion_leg = legs_by_scope_id.get(companion_scope_id)
            if companion_leg is None:
                continue
            scoped_gaps.append(gap)
            companion_legs[companion_scope_id] = companion_leg
        long_distance_legs = [
            primary_long_distance_leg,
            *(
                leg
                for _key, leg in sorted(
                    companion_legs.items(),
                    key=lambda item: (item[1].leg_role, item[0]),
                )
            ),
        ]
    attempt_scope = _retry_scope_for_gap(primary_gap)
    accumulated_signatures = list(
        dict.fromkeys(
            [
                *failure_ledger.get(attempt_scope, []),
                *(signal.signature for signal in signals),
            ]
        )
    )
    failure_ledger[attempt_scope] = accumulated_signatures
    try:
        observed_deadline, observation = _observe_deadline(state)
    except ValueError as exc:
        return _catalog_contract_update(
            state,
            reason_code="run_deadline_invalid",
            message=str(exc),
            base_update=base_update,
        )
    if observation is not None and observation.research_closed:
        return _passed_update(
            state,
            base_update={
                **base_update,
                "candidate_gate_failure_signatures": failure_ledger,
            },
            gaps=gaps,
            deadline=observed_deadline,
        )

    attempts = dict(state.candidate_gate_attempts or {})
    attempts[attempt_scope] = attempts.get(attempt_scope, 0) + 1
    # Every gap this round actually researches says so.  Attribution and the
    # attempt ledger stay bound to the primary gap alone.
    researched_gap_ids = {gap.gap_id for gap in scoped_gaps}
    researching_gaps = [
        gap.model_copy(
            update={
                "status": "researching",
                "attempted_signatures": list(
                    dict.fromkeys([*gap.attempted_signatures, *accumulated_signatures])
                ),
            }
        )
        if gap.gap_id in researched_gap_ids and gap.status == "open"
        else gap
        for gap in gaps
    ]
    excluded_tools = sorted({signal.tool_name for signal in signals})
    recommended_tools = _recommended_tools(state, worker, set(excluded_tools), domain)
    deterministically_rejected_ids = _deterministically_rejected_candidate_ids(
        catalog, packet_workers, worker
    )
    targeted_query_ids: list[str] | None = None
    if (
        primary_gap.intent_id is not None
        and primary_gap.destination_id is not None
        and state.intent_spec is not None
        and state.research_query_plan is not None
    ):
        intent = next(
            (
                item
                for item in state.intent_spec.active_items
                if item.intent_id == primary_gap.intent_id
            ),
            None,
        )
        if intent is not None:
            destination_name = next(
                (
                    str(destination.get("name") or "")
                    for destination in (state.controlled_trip_identity or {}).get(
                        "destinations", []
                    )
                    if isinstance(destination, Mapping)
                    and destination.get("place_id") == primary_gap.destination_id
                ),
                primary_gap.destination_id,
            )
            updated_query_plan, targeted_query = append_targeted_repair_query(
                state.research_query_plan,
                intent=intent,
                destination_id=primary_gap.destination_id,
                destination_name=destination_name,
                domain=domain,
                desired_candidate_count=primary_gap.desired_candidate_count or 2,
            )
            primary_gap = primary_gap.model_copy(
                update={"query_id": targeted_query.query_id}
            )
            gaps = [
                primary_gap if gap.gap_id == primary_gap.gap_id else gap
                for gap in gaps
            ]
            researching_gaps = [
                primary_gap.model_copy(update={"status": "researching"})
                if gap.gap_id == primary_gap.gap_id
                else gap
                for gap in researching_gaps
            ]
            scoped_gaps = [
                primary_gap if gap.gap_id == primary_gap.gap_id else gap
                for gap in scoped_gaps
            ]
            targeted_query_ids = [targeted_query.query_id]
            base_update["research_query_plan"] = updated_query_plan
    gap_summary = [gap.model_dump(mode="json") for gap in scoped_gaps]
    long_distance_instruction = _long_distance_instruction(long_distance_legs)
    connector_instruction = (
        f" 本轮研究 placement skeleton 产生的 {len(connector_round_gaps)} 个精确相邻 connector gap，"
        "每一段都要有 Provider 实测路线；不得更换端点或虚构路线。"
        if connector_round_gaps
        else ""
    )
    itinerary_coverage_instruction = (
        " 当前已准入候选无法为每个旅行日提供合法 timeline anchor；"
        f"本轮至少补查 {itinerary_physical_shortfall} 个彼此不同、可准入且属于受控目的地的具体 Visit/Dining 实体；"
        "不得用本地交通单独占据一天。"
        if (
            worker == "destination_researcher"
            and domain == ResearchDomain.VISIT
            and itinerary_physical_shortfall
        )
        else ""
    )
    connector_payloads: list[dict[str, Any]] = []
    for gap in connector_round_gaps:
        payload = gap.model_dump(mode="json")
        payload["requested_mode"] = (
            gap.requested_flexible_modes[0]
            if gap.preferred_transport_class == "flexible"
            else "public_transit"
        )
        connector_payloads.append(payload)
    assignments = dict(state.agent_assignments)
    assignments[worker] = {
        **dict(assignments.get(worker) or {}),
        "objective": (
            "仅补查以下候选准入缺口；必须保留硬约束、避开已失败 Provider，"
            "并使用独立真实来源。缺少可验证事实时明确返回缺口，不得自产 evidence。"
            f"{connector_instruction}{long_distance_instruction}"
            f"{itinerary_coverage_instruction} gaps={gap_summary}; "
            f"failure_signatures={accumulated_signatures}"
        ),
        "recommended_tools": recommended_tools,
        "excluded_tools": excluded_tools,
        "failure_signatures": accumulated_signatures,
        "require_current_candidate": True,
        **(
            {"research_query_ids": targeted_query_ids}
            if targeted_query_ids is not None
            else {}
        ),
        **(
            {"excluded_candidate_ids": deterministically_rejected_ids}
            if deterministically_rejected_ids
            else {}
        ),
        **(
            {"minimum_additional_physical_candidates": itinerary_physical_shortfall}
            if (
                worker == "destination_researcher"
                and domain == ResearchDomain.VISIT
                and itinerary_physical_shortfall
            )
            else {}
        ),
        **(
            {
                "required_candidate_kinds": sorted(
                    (
                        {"visit"}
                        if domain == ResearchDomain.VISIT
                        else {"dining"}
                        if domain == ResearchDomain.DINING
                        else set()
                    )
                    & (
                        required_candidate_kinds.get(worker, set())
                        - passed_kinds_by_worker.get(worker, set())
                    )
                )
            }
            if worker in coverage_workers
            and domain in {ResearchDomain.VISIT, ResearchDomain.DINING}
            else {}
        ),
        **(
            {
                "required_transport_classes": (
                    # The union over this round's adjacencies: they may not all
                    # request the same class, and a class the round is going to ask
                    # for has to be declared or the answer is refused on arrival.
                    sorted(
                        {
                            transport_class
                            for gap in connector_round_gaps
                            for transport_class in gap.allowed_transport_classes
                        }
                    )
                    if connector_round_gaps
                    else ["long_distance"]
                ),
            }
            if connector_round_gaps or domain == ResearchDomain.LONG_DISTANCE_TRANSPORT
            else {}
        ),
        **({"connector_gaps": connector_payloads} if connector_payloads else {}),
    }
    scoped_candidate_kinds = (
        ["visit"]
        if domain == ResearchDomain.VISIT
        else ["dining"]
        if domain == ResearchDomain.DINING
        else None
    )
    scoped_transport_classes = (
        connector_gap.allowed_transport_classes
        if connector_gap is not None
        else ["long_distance"]
        if domain == ResearchDomain.LONG_DISTANCE_TRANSPORT
        else ["public_transit", "flexible"]
        if domain == ResearchDomain.LOCAL_TRANSPORT
        else None
    )
    assignments[worker]["provider_evidence_assignments"] = (
        dump_provider_evidence_assignments(
            build_provider_evidence_assignments(
                run_id=state.run_id,
                constraint_pack_revision=state.constraint_pack_revision,
                worker_kind=worker,
                controlled_trip_identity=state.controlled_trip_identity or {},
                # Per *scope*, not per gap: the retry ledger above is keyed by
                # product gap, and one local-transport scope covers every
                # adjacency, so two connector rounds would both claim the same
                # attempt on it.
                prior_scope_attempts=scope_attempt_numbers(
                    state.provider_evidence_outcomes
                ),
                candidate_kinds=scoped_candidate_kinds,
                transport_classes=scoped_transport_classes,
                long_distance_legs=long_distance_legs or None,
            )
        )
    )
    update = {
        **base_update,
        "candidate_research_gaps": researching_gaps,
        "candidate_gate_status": "needs_research",
        "candidate_gate_route": worker,
        "candidate_gate_attempts": attempts,
        "candidate_gate_failure_signatures": failure_ledger,
        "agent_assignments": assignments,
        "gate_failure_attributions": _record_attribution(
            state.model_copy(update={"gate_failure_attributions": attributions}),
            gate_class=primary_gap.gate_class,
            disposition=GateDisposition.TARGETED_RESEARCH,
            reason_code=primary_gap.reason,
            domain=domain,
            gap_ids=[primary_gap.gap_id],
            failure_signature=accumulated_signatures[-1] if accumulated_signatures else None,
            retry_attempt=attempts[attempt_scope],
        ),
    }
    if observed_deadline is not None:
        update["run_deadline"] = observed_deadline
    return update


def route_after_candidate_gate(state: TravelAgentState) -> str:
    """Route after Candidate Gate with deadline depth defense.

    The node already checks the wall clock before scheduling workers.  This
    edge mirrors artifact/quality gates so a route written just before a
    phase boundary cannot open a model worker after closeout/expired.
    """
    route = state.candidate_gate_route or "passed"
    deadline = state.run_deadline
    if deadline is None:
        return route
    try:
        _observed, observation = observe_run_deadline(deadline)
    except ValueError:
        return "passed"
    if observation.research_closed:
        # Legal non-model exits past the boundary; worker and repair routes hand
        # the current catalog straight to composition instead.
        return route if route == "passed" else "passed"
    return route
