"""Deterministic worker scoping for durable hard constraints."""

from __future__ import annotations

import hashlib
import json
from typing import Mapping, Sequence

from .delivery_bundle import (
    CandidateConstraintGateAttestation,
    FactAssertion,
    ResearchCandidate,
    ResearchPacket,
    SourceRecord,
)


def _scoped_active_constraints(
    constraint_pack: Mapping[str, object] | None,
    *,
    worker_kind: str | None,
) -> list[Mapping[str, object]]:
    if not isinstance(constraint_pack, Mapping):
        return []
    items = constraint_pack.get("hard_constraints") or []
    if not isinstance(items, list):
        return []
    scoped: list[Mapping[str, object]] = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("status") != "active":
            continue
        scoped_workers = item.get("candidate_worker_kinds")
        if not isinstance(scoped_workers, list):
            continue
        if worker_kind is not None and worker_kind not in scoped_workers:
            continue
        constraint_id = str(item.get("constraint_id") or "").strip()
        if not constraint_id:
            continue
        scoped.append(item)
    return scoped


def active_hard_constraint_ids(
    constraint_pack: Mapping[str, object] | None,
    *,
    worker_kind: str | None = None,
) -> tuple[str, ...]:
    """Return the active hard constraints applicable to one worker domain."""

    identifiers: list[str] = []
    for item in _scoped_active_constraints(
        constraint_pack, worker_kind=worker_kind
    ):
        constraint_id = str(item.get("constraint_id") or "").strip()
        if constraint_id and constraint_id not in identifiers:
            identifiers.append(constraint_id)
    return tuple(identifiers)


def active_hard_constraints(
    constraint_pack: Mapping[str, object] | None,
    *,
    worker_kind: str,
) -> tuple[Mapping[str, object], ...]:
    """Return the canonical candidate-local hard payload for one worker."""

    return tuple(_scoped_active_constraints(constraint_pack, worker_kind=worker_kind))


def _canonical_json(value: object) -> str:
    """Serialize only data-bearing inputs in a stable, cross-process form."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def scoped_hard_constraint_fingerprint(
    constraint_pack: Mapping[str, object] | None,
    *,
    worker_kind: str,
) -> str | None:
    """Fingerprint the exact semantic hard-constraint payload in one domain."""

    scoped = _scoped_active_constraints(constraint_pack, worker_kind=worker_kind)
    if not scoped:
        return None
    normalized = sorted(
        (dict(item) for item in scoped),
        key=lambda item: str(item.get("constraint_id") or ""),
    )
    return _sha256({"schema_version": 1, "hard_constraints": normalized})


def candidate_facts_fingerprint(
    candidate: ResearchCandidate,
    fact_assertions: Sequence[FactAssertion],
    source_records: Sequence[SourceRecord],
) -> str:
    """Fingerprint the exact Fact and supporting Source payload for one Candidate."""

    candidate_fact_ids = set(candidate.fact_assertion_ids)
    facts = sorted(
        (
            fact.model_dump(mode="json")
            for fact in fact_assertions
            if fact.fact_assertion_id in candidate_fact_ids
        ),
        key=lambda item: str(item["fact_assertion_id"]),
    )
    candidate_source_ids = set(candidate.source_record_ids)
    sources = sorted(
        (
            source.model_dump(mode="json")
            for source in source_records
            if source.source_record_id in candidate_source_ids
        ),
        key=lambda item: str(item["source_record_id"]),
    )
    return _sha256(
        {
            "candidate_id": candidate.candidate_id,
            "research_packet_id": candidate.research_packet_id,
            "fact_assertion_ids": sorted(candidate.fact_assertion_ids),
            "source_record_ids": sorted(candidate.source_record_ids),
            "facts": facts,
            "sources": sources,
        }
    )


def candidate_evaluations_fingerprint(candidate: ResearchCandidate) -> str:
    """Fingerprint the exact ordered-by-id verdict/evidence tuple."""

    evaluations = sorted(
        (item.model_dump(mode="json") for item in candidate.constraint_evaluations),
        key=lambda item: str(item["constraint_id"]),
    )
    return _sha256(
        {
            "candidate_id": candidate.candidate_id,
            "active_constraint_ids": sorted(candidate.active_constraint_ids),
            "constraint_evaluations": evaluations,
        }
    )


def build_candidate_constraint_gate_attestation(
    packet: ResearchPacket,
    candidate: ResearchCandidate,
    *,
    constraint_pack: Mapping[str, object] | None,
    fact_data_revision: int | None = None,
) -> CandidateConstraintGateAttestation | None:
    """Build the server-owned Candidate Gate proof for exact current inputs."""

    scoped_fingerprint = scoped_hard_constraint_fingerprint(
        constraint_pack, worker_kind=packet.worker_kind
    )
    if scoped_fingerprint is None:
        return None
    return CandidateConstraintGateAttestation(
        run_id=packet.run_id,
        research_packet_id=packet.research_packet_id,
        worker_kind=packet.worker_kind,
        candidate_id=candidate.candidate_id,
        fact_data_revision=(
            packet.fact_data_revision
            if fact_data_revision is None
            else fact_data_revision
        ),
        scoped_constraint_fingerprint=scoped_fingerprint,
        candidate_facts_fingerprint=candidate_facts_fingerprint(
            candidate, packet.fact_assertions, packet.source_records
        ),
        evaluation_fingerprint=candidate_evaluations_fingerprint(candidate),
    )


def bind_candidate_constraint_gate_attestations(
    packet: ResearchPacket,
    *,
    constraint_pack: Mapping[str, object] | None,
) -> ResearchPacket:
    """Mint attestation only at Candidate Gate, after authoritative parsing."""

    candidates = [
        candidate.model_copy(
            update={
                "constraint_gate_attestation": build_candidate_constraint_gate_attestation(
                    packet, candidate, constraint_pack=constraint_pack
                )
            }
        )
        for candidate in packet.candidates
    ]
    return packet.model_copy(update={"candidates": candidates})
