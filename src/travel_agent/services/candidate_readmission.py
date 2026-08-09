"""Post-delivery Candidate re-admission.

**Boundary:** This module is the **only** pure re-admission entry for
paths that run **after** a sealed Delivery Bundle exists:

- ``workspace_v2_service`` — user mutations
- ``weather_bundle_refresh`` — weather-driven catalog re-check

Deep Research (``candidate_gate``, workers, artifact/quality gates) must **not**
import this module.  Live admission during research uses
``candidate_admission.admit_candidate`` inside Candidate Gate.

This package deliberately has no store, tool, or provider dependency: callers
use its result to present current Candidate choices, then explicitly submit a
separate Workspace mutation if the user chooses one.

Internal layout:
- ``candidate_readmission_freshness`` — weather/fact currentness helpers
- this module — result DTOs, catalog rebind, public ``readmit_*`` facade
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from pydantic import ValidationError

from ..entities.delivery_bundle import (
    CandidateAdmissionResult,
    CandidateConstraintEvaluation,
    DeliveryBundle,
    FactAssertion,
    FieldProvenance,
    RecommendationCatalog,
    ResearchCandidate,
    ResearchPacket,
    SourceRecord,
    TripWorkspaceV2,
    WeatherImpact,
)
from .candidate_admission import (
    admit_candidate,
    normalize_lodging_price_evidence,
)
from .constraint_applicability import (
    active_hard_constraint_ids,
    build_candidate_constraint_gate_attestation,
)
from .candidate_readmission_freshness import (
    _aware,
    _fact_is_current_as_of,
    _freshness_status,
    _target_ref,
    _weather_days,
)
from .weather_impact_engine import WeatherImpactEngine, risk_profile_from_constraint_pack

__all__ = [
    "CandidateCatalogReadmissionResult",
    "CandidateCurrentnessResult",
    "CandidateReadmissionError",
    "readmit_current_catalog_candidates",
    "readmit_current_candidates",
    "workspace_destination_country_codes",
    "workspace_hard_constraint_pack",
]


class CandidateReadmissionError(RuntimeError):
    """A current Bundle cannot safely yield re-admitted Candidate evidence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ── Re-admission inputs derived from a sealed Workspace ─────────────────────
#
# Both post-delivery callers must derive these the same way, so they are built
# here rather than in either caller.  ``tests/
# test_readmission_inputs_have_one_implementation.py`` pins that: a second
# implementation anywhere under ``src`` fails, and so does a call site that
# feeds ``readmit_*`` a pack built by anything but these two functions.


def workspace_hard_constraint_pack(
    workspace: TripWorkspaceV2,
) -> dict[str, list[dict[str, object]]]:
    """Reconstruct only durable, active hard constraints from Workspace anchors.

    A post-delivery re-admission has no access to volatile LangGraph state.
    Its current Workspace anchors are therefore the only authoritative source
    for user-owned hard constraints; omitting them would make a new admission
    silently less strict than the Bundle that produced the itinerary.
    """

    constraints: list[dict[str, object]] = []
    for anchor in workspace.user_input_anchors:
        if anchor.input_kind != "hard_constraint" or not anchor.constraint_id:
            continue
        if isinstance(anchor.value, Mapping):
            item = {str(key): value for key, value in anchor.value.items()}
        else:
            item = {"value": anchor.value}
        item["constraint_id"] = anchor.constraint_id
        # Only active constraints are carried into a sealed Workspace.  Do
        # not let an untrusted embedded payload weaken that durable contract.
        item["status"] = "active"
        item.setdefault("category", anchor.field_path)
        constraints.append(item)
    return {"hard_constraints": constraints}


def workspace_destination_country_codes(workspace: TripWorkspaceV2) -> dict[str, str]:
    """Read only controlled destination identity anchors for re-admission."""

    result: dict[str, str] = {}

    def add(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        place_id = value.get("place_id") or value.get("destination_id")
        country_code = value.get("country_code")
        if place_id and country_code:
            result[str(place_id)] = str(country_code)
        destinations = value.get("destinations")
        if isinstance(destinations, list):
            for destination in destinations:
                add(destination)

    for anchor in workspace.user_input_anchors:
        if "destination" in anchor.field_path or anchor.field_path in {
            "controlled_trip_identity",
            "controlled_identity",
        }:
            add(anchor.value)
    return result


@dataclass(frozen=True)
class CandidateCatalogReadmissionResult:
    """A non-persisted, whole-Catalog re-admission view for named Candidates.

    This is the shared safety boundary for every public path that turns a
    Candidate into a canonical itinerary entity.  It deliberately does not
    choose or promote anything: callers must compare the returned catalog and
    weather impacts with the current Bundle, then require an explicit refresh
    when that comparison exposes stale evidence.
    """

    candidate_ids: tuple[str, ...]
    catalog: RecommendationCatalog
    weather_impacts: tuple[WeatherImpact, ...]


@dataclass(frozen=True)
class CandidateCurrentnessResult:
    """Currentness checks for specific Candidate-to-canonical materializations."""

    candidate_ids: tuple[str, ...]
    candidates: tuple[ResearchCandidate, ...]
    admissions: tuple[CandidateAdmissionResult, ...]
    weather_impacts: tuple[WeatherImpact, ...]
    catalog_changed: bool


def _assert_catalog_is_not_future(bundle: DeliveryBundle) -> None:
    catalog = bundle.workspace.recommendation_catalog
    if catalog.fact_data_revision > bundle.manifest.fact_data_revision:
        raise CandidateReadmissionError(
            "candidate_catalog_future_revision",
            "Recommendation Catalog uses a future fact revision",
        )
    if catalog.weather_data_revision > bundle.manifest.weather_data_revision:
        raise CandidateReadmissionError(
            "candidate_catalog_future_revision",
            "Recommendation Catalog uses a future weather revision",
        )
    packet_revisions = {
        packet.constraint_pack_revision for packet in catalog.research_packets
    }
    if len(packet_revisions) > 1:
        raise CandidateReadmissionError(
            "candidate_constraint_revision_inconsistent",
            "Candidate packets use inconsistent constraint revisions",
        )


def _rebind_constraint_evaluations(
    candidate: ResearchCandidate,
    *,
    packet: ResearchPacket,
    candidate_facts: Sequence[FactAssertion],
    candidate_sources: Sequence[SourceRecord],
    fact_data_revision: int,
    constraint_pack: Mapping[str, object] | None,
    expected_active_constraint_ids: Sequence[str],
    allow_fact_revision_rebind: bool = False,
) -> ResearchCandidate:
    """Keep a verdict only when Candidate Gate attested its exact inputs.

    Historical model verdicts are never sufficient.  The typed attestation
    binds the candidate's fact tuple, scoped semantic constraint payload,
    evaluation tuple, run/packet identity and current Fact revision.  A
    weather-only atomic refresh may carry the same verdict over a new global
    fact revision, but only when every other proof component still matches.
    """

    scoped_ids = tuple(expected_active_constraint_ids)
    if not scoped_ids:
        return candidate.model_copy(
            update={
                "active_constraint_ids": [],
                "constraint_evaluations": [],
                "constraint_gate_attestation": None,
            }
        )
    attestation_packet = packet.model_copy(
        update={
            "fact_data_revision": fact_data_revision,
            "fact_assertions": list(candidate_facts),
            "source_records": list(candidate_sources),
            "candidates": [candidate],
        }
    )
    expected = build_candidate_constraint_gate_attestation(
        attestation_packet,
        candidate,
        constraint_pack=constraint_pack,
        fact_data_revision=fact_data_revision,
    )
    actual = candidate.constraint_gate_attestation
    exact = actual == expected
    same_proof_except_revision = (
        actual is not None
        and expected is not None
        and actual.schema_version == expected.schema_version
        and actual.run_id == expected.run_id
        and actual.research_packet_id == expected.research_packet_id
        and actual.worker_kind == expected.worker_kind
        and actual.candidate_id == expected.candidate_id
        and actual.scoped_constraint_fingerprint
        == expected.scoped_constraint_fingerprint
        and actual.candidate_facts_fingerprint
        == expected.candidate_facts_fingerprint
        and actual.evaluation_fingerprint == expected.evaluation_fingerprint
    )
    if (
        expected is not None
        and tuple(candidate.active_constraint_ids) == scoped_ids
        and (exact or (allow_fact_revision_rebind and same_proof_except_revision))
    ):
        return candidate.model_copy(
            update={
                "active_constraint_ids": list(scoped_ids),
                "constraint_gate_attestation": expected,
            }
        )

    rebound = [
        CandidateConstraintEvaluation(
            constraint_id=constraint_id,
            status="unknown",
            fact_assertion_ids=[],
            reason_code="current_constraint_evaluation_required",
        )
        for constraint_id in expected_active_constraint_ids
    ]
    return candidate.model_copy(
        update={
            "active_constraint_ids": list(scoped_ids),
            "constraint_evaluations": rebound,
            "constraint_gate_attestation": None,
        }
    )


def _current_packet(
    packet: ResearchPacket,
    *,
    fact_data_revision: int,
    fact_index: Mapping[str, FactAssertion],
    source_index: Mapping[str, SourceRecord],
    field_provenance: Sequence[FieldProvenance],
    source_order: Sequence[str],
    provenance_order: Mapping[int, int],
    constraint_pack: Mapping[str, object] | None,
    expected_active_constraint_ids: Sequence[str],
    as_of: datetime,
    allow_fact_revision_rebind: bool = False,
) -> ResearchPacket:
    """Rebind one immutable packet only to facts in the current snapshot."""
    # A zero-candidate packet is a routine research outcome: a targeted
    # re-research round that ran and admitted nothing.  ``ResearchPacket``
    # already treats it as a first-class shape and forbids it from carrying
    # fact assertions or field provenance, so there is no Candidate evidence
    # here to rebind and nothing for the evidence checks below to protect.
    # Failing closed on its empty evidence set would abort re-admission of the
    # whole catalog for a packet that asserts nothing.  Carry it through with
    # only the catalog's current fact revision, which ``RecommendationCatalog``
    # requires every packet to match.
    if not packet.candidates:
        return packet.model_copy(update={"fact_data_revision": fact_data_revision})
    candidate_fact_ids: list[str] = []
    candidate_ids = {candidate.candidate_id for candidate in packet.candidates}
    for candidate in packet.candidates:
        for fact_id in candidate.fact_assertion_ids:
            if fact_id not in candidate_fact_ids:
                candidate_fact_ids.append(fact_id)

    missing_fact_ids = [
        fact_id for fact_id in candidate_fact_ids if fact_id not in fact_index
    ]
    if missing_fact_ids:
        raise CandidateReadmissionError(
            "candidate_evidence_missing",
            "Current FactStore no longer contains Candidate evidence",
        )
    facts: list[FactAssertion] = []
    invalidated_current_fact = False
    for fact_id in candidate_fact_ids:
        fact = fact_index[fact_id]
        if fact.status == "verified" and not _fact_is_current_as_of(
            fact, sources=source_index, as_of=as_of
        ):
            facts.append(fact.model_copy(update={"status": "stale"}))
            invalidated_current_fact = True
        else:
            facts.append(fact)
    if any(fact.entity_ref.entity_id not in candidate_ids for fact in facts):
        raise CandidateReadmissionError(
            "candidate_evidence_inconsistent",
            "Current Candidate facts no longer match their packet identities",
        )

    source_ids = {
        link.source_record_id for fact in facts for link in fact.source_links
    }
    if not source_ids or any(source_id not in source_index for source_id in source_ids):
        raise CandidateReadmissionError(
            "candidate_evidence_missing",
            "Current FactStore no longer contains Candidate sources",
        )
    sources = [
        source_index[source_id] for source_id in source_order if source_id in source_ids
    ]

    fact_pairs = {
        (fact.entity_ref.entity_id, fact.field_path)
        for fact in facts
    }
    current_provenance = [
        item
        for item in field_provenance
        if (item.entity_ref.entity_id, item.field_path) in fact_pairs
        and (
            item.origin != "external_fact"
            or set(item.reference_ids) <= set(candidate_fact_ids)
        )
    ]
    current_provenance.sort(key=lambda item: provenance_order[id(item)])
    provenance_pairs = {
        (item.entity_ref.entity_id, item.field_path) for item in current_provenance
    }
    if not fact_pairs <= provenance_pairs:
        raise CandidateReadmissionError(
            "candidate_evidence_missing",
            "Current FactStore lacks Candidate field provenance",
        )

    facts_by_candidate: dict[str, list[FactAssertion]] = {}
    for fact in facts:
        facts_by_candidate.setdefault(fact.entity_ref.entity_id, []).append(fact)
    candidates = []
    for candidate in packet.candidates:
        current_candidate = candidate.model_copy(
            update={
                "freshness_status": _freshness_status(
                    facts_by_candidate[candidate.candidate_id]
                )
            }
        )
        candidates.append(
            _rebind_constraint_evaluations(
                current_candidate,
                packet=packet,
                candidate_facts=facts_by_candidate[candidate.candidate_id],
                candidate_sources=[
                    source
                    for source in sources
                    if source.source_record_id in candidate.source_record_ids
                ],
                fact_data_revision=fact_data_revision,
                constraint_pack=constraint_pack,
                expected_active_constraint_ids=expected_active_constraint_ids,
                allow_fact_revision_rebind=allow_fact_revision_rebind,
            )
        )
    try:
        return ResearchPacket.model_validate(
            {
                **packet.model_dump(mode="python"),
                "fact_data_revision": fact_data_revision,
                "candidates": candidates,
                "source_records": sources,
                "fact_assertions": facts,
                "field_provenance": current_provenance,
                # Keep an unchanged packet byte-stable across repeated user
                # refreshes.  Only an evidence invalidation needs current
                # temporal validation inside the packet contract.
                "generated_at": _aware(as_of)
                if invalidated_current_fact
                else packet.generated_at,
            }
        )
    except ValidationError as exc:
        raise CandidateReadmissionError(
            "candidate_snapshot_inconsistent",
            "Current FactStore evidence cannot safely re-admit the Candidate packet",
        ) from exc


def readmit_current_catalog_candidates(
    bundle: DeliveryBundle,
    *,
    candidate_ids: Sequence[str],
    constraint_pack: Mapping[str, object] | None = None,
    destination_country_codes: Mapping[str, str] | None = None,
    as_of: datetime | None = None,
    allow_fact_revision_rebind: bool = False,
    allow_unanchored_existing_identity: bool = False,
) -> CandidateCatalogReadmissionResult:
    """Re-evaluate named Candidate evidence without a provider call or mutation.

    The full current catalog is re-admitted, rather than only the requested
    records.  This intentionally fails closed if any packet has become
    inconsistent: a catalog revision must never mix a freshly evaluated
    Candidate with historical admission or constraint verdicts.
    """
    _assert_catalog_is_not_future(bundle)
    candidate_ids = tuple(candidate_ids)
    if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
        raise CandidateReadmissionError(
            "candidate_not_current",
            "Candidate re-admission requires one or more unique candidate ids",
        )
    catalog = bundle.workspace.recommendation_catalog
    candidate_index = catalog.candidate_index()
    missing_candidate_ids = [
        candidate_id for candidate_id in candidate_ids if candidate_id not in candidate_index
    ]
    if missing_candidate_ids:
        raise CandidateReadmissionError(
            "candidate_not_current",
            "Candidate no longer exists in the current Recommendation Catalog",
        )
    # The pure function must stay replayable.  Callers performing a real user
    # action pass a clock value; direct deterministic use falls back to the
    # immutable Bundle creation timestamp rather than ambient wall time.
    as_of = _aware(as_of or bundle.manifest.created_at)

    fact_snapshot = bundle.fact_snapshot
    weather_snapshot = bundle.weather_snapshot
    fact_index = {
        item.fact_assertion_id: item for item in fact_snapshot.fact_assertions
    }
    source_index = {
        item.source_record_id: item for item in fact_snapshot.source_records
    }
    source_order = [item.source_record_id for item in fact_snapshot.source_records]
    provenance_order = {
        id(item): index for index, item in enumerate(fact_snapshot.field_provenance)
    }
    target_packet_ids = {
        packet.research_packet_id for packet in catalog.research_packets
    }
    packets = [
        _current_packet(
            packet,
            fact_data_revision=bundle.manifest.fact_data_revision,
            fact_index=fact_index,
            source_index=source_index,
            field_provenance=fact_snapshot.field_provenance,
            source_order=source_order,
            provenance_order=provenance_order,
            constraint_pack=constraint_pack,
            expected_active_constraint_ids=active_hard_constraint_ids(
                constraint_pack,
                worker_kind=packet.worker_kind,
            ),
            as_of=as_of,
            allow_fact_revision_rebind=allow_fact_revision_rebind,
        )
        if packet.research_packet_id in target_packet_ids
        else packet
        for packet in bundle.workspace.recommendation_catalog.research_packets
    ]

    impacts: list[WeatherImpact] = []
    target_candidate_ids = {
        candidate.candidate_id
        for packet in catalog.research_packets
        if packet.research_packet_id in target_packet_ids
        for candidate in packet.candidates
    }
    admissions: list[CandidateAdmissionResult] = [
        item
        for item in bundle.workspace.recommendation_catalog.admission_results
        if item.candidate_id not in target_candidate_ids
    ]
    updated_packets: list[ResearchPacket] = []
    risk_profile = risk_profile_from_constraint_pack(constraint_pack or {})
    impact_engine = WeatherImpactEngine()
    previous_scopes_by_candidate: dict[str, list[str | None]] = {}
    for previous in catalog.admission_results:
        previous_scopes_by_candidate.setdefault(previous.candidate_id, []).append(
            previous.selection_slot_id
        )
    country_codes = {
        str(destination_id): str(country_code)
        for destination_id, country_code in (destination_country_codes or {}).items()
    }

    for packet in packets:
        if packet.research_packet_id not in target_packet_ids:
            updated_packets.append(packet)
            continue
        candidates: list[ResearchCandidate] = []
        for candidate in packet.candidates:
            candidate_facts = [
                item
                for item in packet.fact_assertions
                if item.entity_ref.entity_id == candidate.candidate_id
            ]
            candidate = normalize_lodging_price_evidence(
                candidate,
                candidate_facts=candidate_facts,
                source_records=packet.source_records,
            )
            candidate_days = _weather_days(weather_snapshot, candidate)
            candidate_impacts = [
                impact
                for day in candidate_days
                for impact in impact_engine.evaluate(
                    weather_day=day,
                    target_ref=_target_ref(candidate),
                    sensitivity=candidate.weather_sensitivity,
                    risk_profile=risk_profile,
                )
            ]
            candidate = candidate.model_copy(
                update={
                    "weather_impact_ids": [
                        impact.weather_impact_id for impact in candidate_impacts
                    ]
                }
            )
            candidates.append(candidate)
            impacts.extend(candidate_impacts)
            scopes = list(
                dict.fromkeys(
                    previous_scopes_by_candidate.get(candidate.candidate_id, [])
                )
            ) or [None]
            for selection_slot_id in scopes:
                admissions.append(
                    admit_candidate(
                        candidate,
                        fact_data_revision=bundle.manifest.fact_data_revision,
                        weather_data_revision=bundle.manifest.weather_data_revision,
                        selection_slot_id=selection_slot_id,
                        weather_impacts=candidate_impacts,
                        weather_evaluated_dates=[
                            day.date
                            for day in candidate_days
                            if day.data_kind != "unavailable"
                        ],
                        expected_destination_country_code=country_codes.get(
                            candidate.destination_id
                        ),
                        require_destination_country_scope=(
                            not allow_unanchored_existing_identity
                        ),
                        identity_fact_values={
                            field_path: [
                                item.asserted_value
                                for item in candidate_facts
                                if item.field_path == field_path
                            ]
                            for field_path in {
                                item.field_path for item in candidate_facts
                            }
                        },
                        hard_constraints=(
                            constraint_pack.get("hard_constraints") or []
                            if isinstance(constraint_pack, Mapping)
                            else []
                        ),
                        candidate_facts=candidate_facts,
                        source_records=packet.source_records,
                    )
                )
        updated_packets.append(packet.model_copy(update={"candidates": candidates}))

    refreshed_catalog = RecommendationCatalog(
        fact_data_revision=bundle.manifest.fact_data_revision,
        weather_data_revision=bundle.manifest.weather_data_revision,
        research_packets=updated_packets,
        admission_results=admissions,
    )
    unique_impacts = {
        item.weather_impact_id: item for item in impacts
    }
    return CandidateCatalogReadmissionResult(
        candidate_ids=candidate_ids,
        catalog=refreshed_catalog,
        weather_impacts=tuple(unique_impacts.values()),
    )


def readmit_current_candidates(
    bundle: DeliveryBundle,
    *,
    candidate_scopes: Mapping[str, Sequence[str | None]],
    constraint_pack: Mapping[str, object] | None = None,
    destination_country_codes: Mapping[str, str] | None = None,
    as_of: datetime | None = None,
) -> CandidateCurrentnessResult:
    """Check only the Candidate lineages a pending mutation will materialize.

    Unlike a formal refresh this never manufactures a new whole Catalog.  That
    matters for an ordinary selection: an unrelated expired Candidate must not
    prevent the user from applying a currently verified one.  Each requested
    Candidate and its exact selection scope is still independently rebound to
    current facts, source/cache validity, weather, and applicable hard
    constraints.  Any difference from the durable catalog tells the caller to
    require an explicit refresh rather than silently committing it.
    """

    _assert_catalog_is_not_future(bundle)
    normalized_scopes = {
        str(candidate_id): tuple(dict.fromkeys(scopes))
        for candidate_id, scopes in candidate_scopes.items()
    }
    if (
        not normalized_scopes
        or any(not candidate_id or not scopes for candidate_id, scopes in normalized_scopes.items())
    ):
        raise CandidateReadmissionError(
            "candidate_not_current",
            "Candidate currentness requires at least one Candidate and admission scope",
        )
    candidate_ids = tuple(normalized_scopes)
    catalog = bundle.workspace.recommendation_catalog
    original_candidates = catalog.candidate_index()
    missing_candidate_ids = [
        candidate_id for candidate_id in candidate_ids if candidate_id not in original_candidates
    ]
    if missing_candidate_ids:
        raise CandidateReadmissionError(
            "candidate_not_current",
            "Candidate no longer exists in the current Recommendation Catalog",
        )
    as_of = _aware(as_of or bundle.manifest.created_at)
    fact_snapshot = bundle.fact_snapshot
    fact_index = {
        item.fact_assertion_id: item for item in fact_snapshot.fact_assertions
    }
    source_index = {
        item.source_record_id: item for item in fact_snapshot.source_records
    }
    source_order = [item.source_record_id for item in fact_snapshot.source_records]
    provenance_order = {
        id(item): index for index, item in enumerate(fact_snapshot.field_provenance)
    }
    country_codes = {
        str(destination_id): str(country_code)
        for destination_id, country_code in (destination_country_codes or {}).items()
    }
    risk_profile = risk_profile_from_constraint_pack(constraint_pack or {})
    impact_engine = WeatherImpactEngine()
    original_admissions = catalog.admission_index()
    refreshed_candidates: list[ResearchCandidate] = []
    refreshed_admissions: list[CandidateAdmissionResult] = []
    impacts: list[WeatherImpact] = []

    for packet in catalog.research_packets:
        requested = [
            candidate
            for candidate in packet.candidates
            if candidate.candidate_id in normalized_scopes
        ]
        if not requested:
            continue
        # Rebind a self-contained packet slice so unrelated Candidate evidence
        # cannot contaminate this pending materialization check.
        packet_slice = packet.model_copy(update={"candidates": requested})
        current_packet = _current_packet(
            packet_slice,
            fact_data_revision=bundle.manifest.fact_data_revision,
            fact_index=fact_index,
            source_index=source_index,
            field_provenance=fact_snapshot.field_provenance,
            source_order=source_order,
            provenance_order=provenance_order,
            constraint_pack=constraint_pack,
            expected_active_constraint_ids=active_hard_constraint_ids(
                constraint_pack,
                worker_kind=packet.worker_kind,
            ),
            as_of=as_of,
        )
        for candidate in current_packet.candidates:
            candidate_facts = [
                item
                for item in current_packet.fact_assertions
                if item.entity_ref.entity_id == candidate.candidate_id
            ]
            candidate = normalize_lodging_price_evidence(
                candidate,
                candidate_facts=candidate_facts,
                source_records=current_packet.source_records,
            )
            candidate_days = _weather_days(bundle.weather_snapshot, candidate)
            candidate_impacts = [
                impact
                for day in candidate_days
                for impact in impact_engine.evaluate(
                    weather_day=day,
                    target_ref=_target_ref(candidate),
                    sensitivity=candidate.weather_sensitivity,
                    risk_profile=risk_profile,
                )
            ]
            refreshed = candidate.model_copy(
                update={
                    "weather_impact_ids": [
                        impact.weather_impact_id for impact in candidate_impacts
                    ]
                }
            )
            refreshed_candidates.append(refreshed)
            impacts.extend(candidate_impacts)
            for selection_slot_id in normalized_scopes[candidate.candidate_id]:
                refreshed_admissions.append(
                    admit_candidate(
                        refreshed,
                        fact_data_revision=bundle.manifest.fact_data_revision,
                        weather_data_revision=bundle.manifest.weather_data_revision,
                        selection_slot_id=selection_slot_id,
                        weather_impacts=candidate_impacts,
                        weather_evaluated_dates=[
                            day.date
                            for day in candidate_days
                            if day.data_kind != "unavailable"
                        ],
                        expected_destination_country_code=country_codes.get(
                            refreshed.destination_id
                        ),
                        identity_fact_values={
                            field_path: [
                                item.asserted_value
                                for item in candidate_facts
                                if item.field_path == field_path
                            ]
                            for field_path in {
                                item.field_path for item in candidate_facts
                            }
                        },
                        hard_constraints=(
                            constraint_pack.get("hard_constraints") or []
                            if isinstance(constraint_pack, Mapping)
                            else []
                        ),
                        candidate_facts=candidate_facts,
                        source_records=current_packet.source_records,
                    )
                )

    refreshed_by_id = {
        candidate.candidate_id: candidate for candidate in refreshed_candidates
    }
    refreshed_admission_index = {
        (item.candidate_id, item.selection_slot_id): item
        for item in refreshed_admissions
    }
    catalog_changed = any(
        refreshed_by_id.get(candidate_id) != original_candidates[candidate_id]
        for candidate_id in candidate_ids
    ) or any(
        refreshed_admission_index.get((candidate_id, selection_slot_id))
        != original_admissions.get((candidate_id, selection_slot_id))
        for candidate_id, scopes in normalized_scopes.items()
        for selection_slot_id in scopes
    )
    return CandidateCurrentnessResult(
        candidate_ids=candidate_ids,
        candidates=tuple(refreshed_candidates),
        admissions=tuple(refreshed_admissions),
        weather_impacts=tuple(
            {item.weather_impact_id: item for item in impacts}.values()
        ),
        catalog_changed=catalog_changed,
    )
