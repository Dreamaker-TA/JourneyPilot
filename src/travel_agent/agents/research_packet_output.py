"""Strict Research Packet transport for v2 worker runtime output."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from pydantic import ValidationError

from ..entities.delivery_bundle import (
    EntityRef,
    EntityType,
    FactAssertion,
    FactSourceLink,
    FieldProvenance,
    ProviderSnapshotProvenance,
    RecommendationCatalog,
    ResearchPacket,
    SourceRecord,
    TransportEndpoint,
    TransportMode,
    TransportSegment,
    WeatherSensitivity,
)
from ..entities.provider_environment import snapshot_data_environment
from ..models.strict_json_schema import as_strict_schema
from ..entities.provider_evidence import (
    TIMETABLED_TRANSPORT_CLASSES,
    ProviderEvidenceAssignment,
    ProviderEvidenceOutcome,
    ProviderEvidenceScope,
)
from ..services.candidate_admission import (
    provider_place_type_matches_candidate_kind,
)

# Explicit re-export for tests/workers that import constraint helpers from this module.
from ..services.constraint_applicability import (
    active_hard_constraint_ids as active_hard_constraint_ids,
)
from ..rag.place_mentions import PlaceLookup, ProviderIdentity
from ..services.destination_scope import (
    DESTINATION_DISTANCE_KEY,
    MAX_DESTINATION_DISTANCE_KM,
)
from ..rag.source_records import is_rag_chunk_source_id
from ..workflows.run_control import ModelWindowClosed
from ..tools.governance import (
    CAPABILITY_DECLARATION_STATUSES,
    COMPILED_TOOL_SOURCE_ID_PREFIX,
    PROVIDER_RESULT_OUTCOME_EMPTY_SUCCESS,
    PROVIDER_RESULT_OUTCOME_METADATA_KEY,
    QUALITY_VERIFIED_PLACE_IDS_METADATA_KEY,
    ToolExecutionStatus,
    compiled_tool_source_id,
    compiled_tool_source_id_is_about,
)
from .orchestrator.provider_failure import classify_provider_failure

logger = logging.getLogger(__name__)

ResearchWorkerKind = Literal[
    "destination_researcher",
    "accommodation_researcher",
    "transport_researcher",
]

# One extra schema-repair call, spent only on a failure that never produced
# output.  The repair conversation runs at temperature 0, so a schema, registry
# or typed-domain rejection replays verbatim and gets no retry at all.  The
# counter is function-local: finalization owns it, no durable state field does.
_SCHEMA_REPAIR_TRANSIENT_RETRIES = 1

# Finalization is the only fast-tier call that carries the whole worker
# transcript.  Provider-selection normally returns quickly, but real runs with
# 20+ verified place options have crossed 60 seconds before emitting their small
# JSON choice.  This is an operation bound, not an output allowance: the compact
# selection call below still has a task-local 4096-token ceiling, while a full
# packet repair may use the deployment's larger configured ceiling.
_PACKET_MODEL_CALL_TIMEOUT_SECONDS = 120.0
_PROVIDER_SELECTION_MAX_OUTPUT_TOKENS = 4096


def _is_transient_model_call_failure(error: BaseException) -> bool:
    """Classify a repair model call exactly as the gates classify a Provider."""
    return classify_provider_failure(str(error)).category == "transient"


async def _bounded_packet_model_call(
    llm: Any,
    messages: Sequence[Mapping[str, Any]],
    **kwargs: Any,
) -> str:
    """Cap the whole packet-repair operation to the finalization timeout.

    ``timeout`` rides the same per-call kwargs as ``temperature`` and
    ``response_format``, so the transport issues this request under the wider
    bound instead of the fast tier's default; without it the SDK would abort at
    its own default and retry underneath the wrapper.  The wrapper then keeps
    those SDK retries from multiplying that bound.  ``OpenAICompatibleLLM``
    independently enforces the research window, which stays authoritative: the
    effective cap is the smaller of the two.
    """

    timeout = _PACKET_MODEL_CALL_TIMEOUT_SECONDS
    call = llm.ainvoke(list(messages), timeout=timeout, **kwargs)
    try:
        return await asyncio.wait_for(call, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ResearchPacketOutputError(
            f"packet model operation exceeded configured timeout ({timeout:g}s)"
        ) from exc


_WORKER_CANDIDATE_MODELS: dict[ResearchWorkerKind, tuple[tuple[str, str], ...]] = {
    "destination_researcher": (
        ("visit", "VisitCandidate"),
        ("dining", "DiningCandidate"),
    ),
    "accommodation_researcher": (("lodging", "LodgingCandidate"),),
    "transport_researcher": (("transport", "TransportCandidate"),),
}

RESEARCH_PACKET_CANDIDATE_LIMITS: dict[ResearchWorkerKind, int] = {
    # How many candidates **one model-authoring act** may produce: one authored
    # packet, or one typed provider-selection batch.  A packet's total is derived
    # from this and the number of batches (see ``_repair_from_provider_selection``)
    # — there is no second constant for the total, because there used to be and
    # it is what made this whole family unmeasurable.
    #
    # These are **supply** numbers, picked off what an itinerary structurally
    # owes, and what each one costs to compose was measured.  They are not headroom
    # under a provider hard cap: ``deepseek-v4-flash-0731`` reports
    # ``max_completion_tokens=65536``, this deployment configures 32768, and the
    # measured peak completion is 7103.
    #
    # destination: every Visit/Dining entry is unique across the whole itinerary
    #   (``itinerary_planner.node.authoring_domains``), so a domain that cannot
    #   field ``day_count`` entries forces the composer to invent the rest.  Trip
    #   lengths in this deployment: 3 days ×122, 4 days ×40, 2 days ×15 — 90% are
    #   3–4 days, so 4 per kind is the smallest value that stops the invitation on
    #   the trips this product actually gets.  It was already 4 here and still
    #   delivered 3, because ``_DESTINATION_REPAIR_CANDIDATE_LIMIT = 3`` sat
    #   downstream of it and won: measured 24/28 runs admitted exactly 3 visits,
    #   and visit < day_count in 20/28.  That second constant is gone.
    # accommodation: one property per check-in interval, and 16/28 measured runs
    #   are two-destination.  2 was exactly the number of intervals such a trip
    #   owes, i.e. no alternative at all, and 5/28 runs came back with fewer
    #   properties than destinations.  4 is the first value that leaves a choice.
    # transport: deliberately **not** widened — see ``packet_candidate_limit``.
    #
    # Cost, from the measured segment table (median first-composition prompt 186k
    # chars / ~67k tokens): each extra admitted candidate adds ~11.6k chars
    # (~3.8k tok) for destination and ~10.5k chars (~3.4k tok) for lodging.
    "destination_researcher": 4,
    "accommodation_researcher": 4,
    "transport_researcher": 3,
}


def packet_candidate_limit(
    worker_kind: ResearchWorkerKind,
    *,
    required_transport_classes: Sequence[str] | None = None,
    required_route_scopes: Sequence[Any] = (),
) -> int:
    """How many candidates one model-authoring act may return, for this call.

    Every site that *enforces* a candidate count and every sentence that *tells
    the model* the count reads this one function, so the two cannot drift apart.
    They had: the packet prompt said "最多输出 4 个" while the schema handed to the
    same call enforced 3, and the typed-selection prompt said "选择 1 到 3 个" as a
    literal that no longer had anything to do with the schema beside it.

    Scoped long-distance transport is the one worker whose count is **not** a
    supply choice: it is the number of required legs, the same structural reading
    ``candidate_gate._domain_research_budget`` gives that domain (long-distance =
    required legs, LOCAL_TRANSPORT = adjacencies).  A flat constant put against a
    structural count is either dead (more legs than the constant) or an invitation
    to return alternatives the composition contract forbids — it requires exactly
    one primary option per required move.  So transport is bounded here by its
    legs, and ``RESEARCH_PACKET_CANDIDATE_LIMITS["transport_researcher"]`` only
    governs the unscoped connector packet, whose load is likewise structural.

    ``required_transport_classes`` is what makes a call scoped, not a non-empty
    ``required_route_scopes``: a scoped round with no route leg of its own owes
    exactly one candidate, and reading emptiness as "unscoped" would hand it the
    unscoped supply number instead.
    """
    if worker_kind == "transport_researcher" and required_transport_classes:
        return max(1, len(required_route_scopes))
    return RESEARCH_PACKET_CANDIDATE_LIMITS[worker_kind]


_DESTINATION_IDENTITY_FACTS_PER_CANDIDATE = 5
_TRANSPORT_PROVIDER_SELECTION_LIMIT = 2
_PLACE_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "visit": (
        "name",
        "place_id",
        "provider_place_type",
        "provider_country_code",
        "address",
    ),
    "dining": (
        "branch_name",
        "place_id",
        "provider_place_type",
        "provider_country_code",
        "address",
    ),
    "lodging": (
        "property_name",
        "place_id",
        "provider_place_type",
        "provider_country_code",
        "address",
    ),
}
_PLACE_NAME_FIELD = {
    "visit": "name",
    "dining": "branch_name",
    "lodging": "property_name",
}
_PLACE_ENTITY_TYPE = {
    "visit": EntityType.VISIT_STOP,
    "dining": EntityType.DINING_STOP,
    "lodging": EntityType.LODGING_STAY,
}


def _drop_fields_the_model_must_not_author(schema: dict[str, Any]) -> None:
    """Remove server-owned fields from the model output contract."""

    properties = schema.get("properties")
    server_fields = {
        "query_context",
        "intent_spec_revision",
        "research_query_plan_id",
        "executed_query_ids",
        "candidate_discovery_records",
    }
    if isinstance(properties, dict):
        for field_name in server_fields:
            properties.pop(field_name, None)
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [
                name for name in required if name not in server_fields
            ]
    definitions = schema.get("$defs", {})
    for definition_name, field_name in (
        ("LodgingCandidate", "anchor_travel_minutes"),
        # ``SourceRecord`` is split into per-kind branches by
        # ``_bind_external_tool_source_registry``; strip all of them.
        ("SourceRecord", "snapshot"),
        ("RetrievedSourceRecord", "snapshot"),
        ("ExternalToolSourceRecord", "snapshot"),
    ):
        definition = definitions.get(definition_name)
        if not isinstance(definition, dict):
            continue
        definition_properties = definition.get("properties")
        if isinstance(definition_properties, dict):
            definition_properties.pop(field_name, None)
        required = definition.get("required")
        if isinstance(required, list):
            definition["required"] = [name for name in required if name != field_name]


_PROVIDER_SELECTION_SERVER_FIELDS = {
    "candidate_id",
    "research_packet_id",
    "fact_assertion_ids",
    "source_record_ids",
    "field_paths",
    "active_constraint_ids",
    "constraint_evaluations",
    "constraint_gate_attestation",
    "freshness_status",
    "observed_at",
    "expires_at",
    "provider_place_type",
    "provider_country_code",
    "name",
    "branch_name",
    "property_name",
    "address",
}


def build_authoritative_research_packet_metadata(
    *,
    worker_kind: ResearchWorkerKind,
    run_id: str,
    generation_id: str,
    intent_spec_revision: int,
    research_query_plan_id: str,
    executed_queries: Sequence[Mapping[str, Any]],
    task_id: str,
    constraint_pack_revision: int,
    fact_data_revision: int,
    query_context: Mapping[str, Any],
    generated_at: datetime,
) -> dict[str, Any]:
    """Create server-owned Packet lineage; models never author these fields."""
    digest = hashlib.sha256(
        f"{run_id}\0{generation_id}\0{task_id}\0{worker_kind}".encode("utf-8")
    ).hexdigest()[:20]
    return {
        "research_packet_id": f"packet_{worker_kind}_{digest}",
        "run_id": run_id,
        "generation_id": generation_id,
        "task_id": task_id,
        "worker_kind": worker_kind,
        "intent_spec_revision": intent_spec_revision,
        "research_query_plan_id": research_query_plan_id,
        "executed_query_ids": [
            str(query.get("query_id") or "")
            for query in executed_queries
            if query.get("query_id")
        ],
        "constraint_pack_revision": constraint_pack_revision,
        "fact_data_revision": fact_data_revision,
        "query_context": {
            **dict(query_context),
            "query_lineage": [dict(query) for query in executed_queries],
        },
        "generated_at": generated_at.isoformat(),
    }


class ResearchPacketOutputError(ValueError):
    """The worker did not emit the exact v2 Research Packet contract."""


_NO_ELIGIBLE_IDENTITY_FACTS = "research packet requires external identity-bound facts"


def _canonical_snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bind_source_content_hashes(
    payload: dict[str, Any],
    *,
    authoritative_sources: Mapping[str, SourceRecord | Mapping[str, Any]] | None = None,
) -> None:
    """Canonicalize source records without trusting model-authored cache lineage.

    ``SourceRecord.content_hash`` is the SHA-256 of the canonical snapshot JSON —
    a deterministic function of the snapshot the model already emitted.  No
    language model computes SHA-256 reliably, so the system recomputes it here
    rather than trusting or rejecting the model-authored digest.  This is not an
    authenticity guard: the model authors both snapshot and digest, so a matching
    hash never proved the snapshot was real.  Real grounding stays enforced
    elsewhere — verified facts must link a live external SourceRecord, and
    provider identity is bound from the Tool Gateway transcript.

    Provider snapshot cache lineage is accepted only from the separate
    ``authoritative_sources`` registry.  Equality between two fields in the
    untrusted model payload proves nothing: the model can copy the same
    placeholder into both ``content_hash`` and cache provenance.
    """
    sources = payload.get("source_records")
    if not isinstance(sources, list):
        return
    canonical_sources: list[Any] = []
    for source in sources:
        if not isinstance(source, dict):
            canonical_sources.append(source)
            continue
        source_id = str(source.get("source_record_id") or "")
        authoritative = (authoritative_sources or {}).get(source_id)
        if authoritative is not None:
            canonical_sources.append(
                authoritative.model_dump(mode="python")
                if isinstance(authoritative, SourceRecord)
                else dict(authoritative)
            )
            continue
        if source.get("source_kind") == "external_tool":
            raise ResearchPacketOutputError(
                f"external tool source is absent from the authoritative registry: {source_id}"
            )
        snapshot = source.get("snapshot")
        if not isinstance(snapshot, dict):
            canonical_sources.append(source)
            continue
        source.pop("cache_provenance", None)
        source["content_hash"] = _canonical_snapshot_hash(snapshot)
        canonical_sources.append(source)
    payload["source_records"] = canonical_sources


def authoritative_retry_source_records(
    catalog: RecommendationCatalog | None,
    *,
    expected_worker: ResearchWorkerKind,
    constraint_pack_revision: int,
    fact_data_revision: int,
) -> tuple[SourceRecord, ...]:
    """Return the admitted prior source closure for one scoped worker retry."""
    if catalog is None or catalog.fact_data_revision != fact_data_revision:
        return ()
    passed_ids = {
        admission.candidate_id
        for admission in catalog.admission_results
        if admission.status == "passed"
    }
    source_index: dict[str, SourceRecord] = {}
    for packet in catalog.research_packets:
        if (
            packet.worker_kind != expected_worker
            or packet.constraint_pack_revision != constraint_pack_revision
            or packet.fact_data_revision != fact_data_revision
        ):
            continue
        retained_candidate_ids = {
            candidate.candidate_id
            for candidate in packet.candidates
            if candidate.candidate_id in passed_ids
        }
        retained_source_ids = {
            link.source_record_id
            for fact in packet.fact_assertions
            if fact.entity_ref.entity_id in retained_candidate_ids
            for link in fact.source_links
        }
        for source in packet.source_records:
            if source.source_record_id not in retained_source_ids:
                continue
            existing = source_index.get(source.source_record_id)
            if existing is not None and existing != source:
                raise ResearchPacketOutputError(
                    "admitted prior catalog has conflicting source identity: "
                    f"{source.source_record_id}"
                )
            source_index[source.source_record_id] = source
    return tuple(source_index.values())


def _prune_payload_to_candidate_closure(payload: dict[str, Any]) -> None:
    """Keep only facts, sources, and provenance reachable from current candidates."""
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return
    candidate_ids = {
        str(candidate.get("candidate_id") or "")
        for candidate in candidates
        if isinstance(candidate, Mapping) and candidate.get("candidate_id")
    }
    facts = payload.get("fact_assertions")
    sources = payload.get("source_records")
    provenance = payload.get("field_provenance")
    if (
        not isinstance(facts, list)
        or not isinstance(sources, list)
        or not isinstance(provenance, list)
    ):
        return
    retained_facts = [
        fact
        for fact in facts
        if isinstance(fact, Mapping)
        and str((fact.get("entity_ref") or {}).get("entity_id") or "") in candidate_ids
    ]
    retained_fact_ids = {
        str(fact.get("fact_assertion_id") or "")
        for fact in retained_facts
        if fact.get("fact_assertion_id")
    }
    retained_source_ids = {
        str(link.get("source_record_id") or "")
        for fact in retained_facts
        for link in fact.get("source_links") or []
        if isinstance(link, Mapping) and link.get("source_record_id")
    }
    available_source_ids = {
        str(source.get("source_record_id") or "")
        for source in sources
        if isinstance(source, Mapping) and source.get("source_record_id")
    }
    if not retained_source_ids <= available_source_ids:
        raise ResearchPacketOutputError(
            "research packet fact references a source outside the packet"
        )
    retained_sources = [
        source
        for source in sources
        if isinstance(source, Mapping)
        and (
            str(source.get("source_record_id") or "") in retained_source_ids
            or source.get("lifecycle_status") == "rejected"
        )
    ]
    retained_provenance: list[Any] = []
    for item in provenance:
        if (
            not isinstance(item, Mapping)
            or str((item.get("entity_ref") or {}).get("entity_id") or "")
            not in candidate_ids
        ):
            continue
        copied = dict(item)
        if copied.get("origin") == "external_fact":
            references = [
                reference_id
                for reference_id in copied.get("reference_ids") or []
                if str(reference_id) in retained_fact_ids
            ]
            if not references:
                continue
            copied["reference_ids"] = references
        retained_provenance.append(copied)
    payload["fact_assertions"] = retained_facts
    payload["source_records"] = retained_sources
    payload["field_provenance"] = retained_provenance


def _retrieved_urls(evidence_messages: Sequence[Mapping[str, Any]]) -> set[str]:
    """Every page URL a retrieval tool actually returned this round.

    A thin read over :func:`_retrieved_source_snapshots` so the set of pages and
    the record kept for each page can never answer differently.

    Read off the Tool Gateway transcript, which is server-owned: the model cannot
    add to this set by writing anything in its packet.  Collected by key name
    across the sanitized payload because the four search tools shape their results
    differently (``results[].url``, ``sources[].url``, a bare ``url``), and the
    question is the same for all of them.

    A ``degraded`` round counts.  Degradation here means a fallback answered —
    ``global_place_search`` dropping to ``free_web_search`` is the routine case —
    and the pages that fallback returned were observed by the server just as a
    successful round's are.  Reading only ``success`` would drop a citation of a
    page the round really fetched, which costs real candidates on the most common
    fallback path in the system.  ``reference_only`` deliberately does not count:
    that status marks data the Gateway already ruled out as evidence.
    """

    return set(_retrieved_source_snapshots(evidence_messages))


def _retrieved_source_snapshots(
    evidence_messages: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Each page a retrieval returned this round, mapped to what it returned.

    The keys are the same normalized URLs :func:`_retrieved_urls` compares on.
    The value is the **complete sanitized tool return** that named the page,
    which is what the contract asks a snapshot to be ("完整工具返回或完整 RAG
    chunk，禁止裁剪成模型摘要").  Keeping only the enclosing result item would be
    a crop, and a crop is the one thing that phrase rules out.

    First retrieval wins.  A later repeat of the same page cannot overwrite the
    evidence an earlier one already established, which keeps the record stable
    across a repair round that re-runs a search.
    """

    countable = {
        ToolExecutionStatus.SUCCESS.value,
        ToolExecutionStatus.DEGRADED.value,
    }
    snapshots: dict[str, dict[str, Any]] = {}

    def walk(value: Any, whole_return: Mapping[str, Any]) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).casefold() in {
                    "url",
                    "canonical_url",
                    "link",
                } and isinstance(item, str):
                    normalized = _normalized_source_url(item)
                    if normalized and normalized not in snapshots:
                        snapshots[normalized] = dict(whole_return)
                else:
                    walk(item, whole_return)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, whole_return)

    for message in evidence_messages:
        envelope = _parse_tool_envelope(message)
        if envelope is None or envelope.get("status") not in countable:
            continue
        result = envelope.get("sanitized_result")
        if isinstance(result, Mapping):
            walk(result, result)
    return snapshots


# The one source kind the model authors itself: a web retrieval has no Tool
# Gateway registry path, so its record has to come from the worker.  The tool
# kind comes from the Gateway registry and the RAG kind from the round's own
# injected chunks (:func:`_ground_rag_chunk_sources`); neither reaches here.
_RETRIEVED_SOURCE_KINDS = frozenset({"external_web"})


def _normalized_source_url(value: Any) -> Optional[str]:
    """Compare URLs by scheme+host+path: the parts that name a page."""

    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{path}"


def _ground_retrieved_sources_in_the_transcript(
    payload: dict[str, Any],
    evidence_messages: Sequence[Mapping[str, Any]],
    *,
    expected_worker: str,
) -> None:
    """Refuse a retrieved source no retrieval in this round returned, and give
    the survivors the transcript's own copy of what was retrieved.

    ``external_web`` is the one evidence channel the model authors itself, by
    design: a web retrieval has no Tool Gateway registry path, so its record has to
    come from the worker.  That is why it has to be checked here.  ``canonical_url``
    is optional on ``SourceRecord``, so an unchecked "web source" can name no page at
    all and still support verified facts — a citation with nothing behind it, which is
    worse than an uncited claim because the reader can see a source and cannot see
    that it is empty.

    Two conditions, both read against server-owned data: the source must name a
    page, and that page must be one a retrieval tool returned this round.  A
    failing source is dropped along with the facts that lean on it, which sends its
    candidate into the ordinary "not enough evidence" path rather than rejecting a
    whole round; the drop is logged, because a silent one is the shape this guard
    exists to prevent.

    The second condition is **unconditional** — do not gate it on the round having
    returned at least one URL.  That would leave the emptiest round, where no
    retrieval ran at all, as the one round where any well-formed URL is accepted,
    which is backwards: a round with no retrieval transcript has no web evidence, so
    it is exactly where a model-authored citation is least supported.  The two ways
    ``retrieved`` comes back empty — nothing ran, or something ran and returned no
    page — mean the same thing to a reader, and both drop.

    The survivors get ``snapshot`` written from the transcript, **not** from the
    packet.  The contract asks for the complete tool return verbatim and the server
    is holding it — asking the model to retype it spends output tokens to obtain a
    worse copy of something already in hand, and "verbatim" is the one property a
    re-typed copy cannot be trusted to have.  Keeping the field server-written also
    keeps it out of the model-facing schema, which is what takes the last thing a
    strict structured-output provider cannot express out of the Research Packet
    contract.

    ``rag_chunk`` is *not* judged here.  It obeys the same principle — a retrieved
    source the server did not watch come back is not evidence — but its transcript
    is a different one: RAG content reaches a worker through prompt injection, so
    it is never in the Tool Gateway messages.  :func:`_ground_rag_chunk_sources`
    grounds it against the chunks this round actually injected.
    """

    sources = payload.get("source_records")
    if not isinstance(sources, list) or not sources:
        return
    snapshots = _retrieved_source_snapshots(evidence_messages)
    dropped: dict[str, str] = {}
    kept: list[Any] = []
    for source in sources:
        if (
            not isinstance(source, Mapping)
            or source.get("source_kind") not in _RETRIEVED_SOURCE_KINDS
        ):
            kept.append(source)
            continue
        source_id = str(source.get("source_record_id") or "")
        url = _normalized_source_url(source.get("canonical_url"))
        if url is None:
            dropped[source_id] = "no canonical_url"
        elif url not in snapshots:
            dropped[source_id] = f"url not retrieved this round: {url}"
        else:
            grounded = dict(source)
            grounded["snapshot"] = snapshots[url]
            kept.append(grounded)
    payload["source_records"] = kept
    if not dropped:
        return
    logger.warning(
        "Research Packet retrieved sources dropped (uncorroborated) | worker=%s "
        "retrieved_urls=%d dropped=%s",
        expected_worker,
        len(snapshots),
        dropped,
    )
    facts = payload.get("fact_assertions")
    if isinstance(facts, list):
        payload["fact_assertions"] = [
            fact
            for fact in facts
            if not (
                isinstance(fact, Mapping)
                and any(
                    isinstance(link, Mapping)
                    and str(link.get("source_record_id") or "") in dropped
                    for link in fact.get("source_links") or []
                )
            )
        ]


def _ground_rag_chunk_sources(
    payload: dict[str, Any],
    injected_rag_sources: Mapping[str, Mapping[str, Any]],
    *,
    expected_worker: str,
) -> None:
    """Make the knowledge-base evidence channel server-owned, end to end.

    RAG content reaches a worker by prompt injection, so it never enters the Tool
    Gateway transcript the grounding guard reads.  A model-authored ``rag_chunk``
    SourceRecord therefore has no transcript to be grounded against, and gets dropped
    along with the facts leaning on it.

    **The answer is not to loosen the guard.**  It is to give the channel the
    transcript it needs: the server knows exactly which chunks it injected this
    round, so it mints their ids, prints them in the prompt, and writes the records
    itself.  The model's only job is to cite an id it was shown.

    Consequences, all deliberate:

    - every model-authored ``rag_chunk`` record is discarded, whatever it says.  The
      model is not a source of provenance here any more than it is for
      ``external_tool``;
    - a fact citing an id the server did not inject loses that link and, if the link
      was load-bearing, the fact goes with it — same disposal as an uncorroborated
      web source, so the candidate lands in the ordinary "not enough evidence" path;
    - an id the server did inject is materialized from the server's own record.
    """

    facts = payload.get("fact_assertions")
    sources = payload.get("source_records")
    if not isinstance(sources, list):
        sources = []

    model_authored = {
        str(source.get("source_record_id") or "")
        for source in sources
        if isinstance(source, Mapping) and source.get("source_kind") == "rag_chunk"
    }
    kept = [
        source
        for source in sources
        if not (
            isinstance(source, Mapping) and source.get("source_kind") == "rag_chunk"
        )
    ]

    cited: set[str] = set()
    if isinstance(facts, list):
        for fact in facts:
            if not isinstance(fact, Mapping):
                continue
            for link in fact.get("source_links") or []:
                if not isinstance(link, Mapping):
                    continue
                source_id = str(link.get("source_record_id") or "")
                if is_rag_chunk_source_id(source_id) or source_id in model_authored:
                    cited.add(source_id)

    grounded = sorted(cited & set(injected_rag_sources))
    ungrounded = sorted(cited - set(injected_rag_sources))
    for source_id in grounded:
        kept.append(dict(injected_rag_sources[source_id]))
    payload["source_records"] = kept

    if not ungrounded:
        return
    logger.warning(
        "Research Packet rag_chunk sources dropped (not injected this round) | "
        "worker=%s injected=%d dropped=%s",
        expected_worker,
        len(injected_rag_sources),
        ungrounded,
    )
    if isinstance(facts, list):
        payload["fact_assertions"] = [
            fact
            for fact in facts
            if not (
                isinstance(fact, Mapping)
                and any(
                    isinstance(link, Mapping)
                    and str(link.get("source_record_id") or "") in ungrounded
                    for link in fact.get("source_links") or []
                )
            )
        ]


def _candidate_provenance_entity_ids(payload: Mapping[str, Any]) -> dict[str, str]:
    """Each candidate id mapped to the entity its compiled sources are minted under.

    Not the candidate id: a compiled source's final segment digests the entity the
    Provider envelope was *about* — ``place_id`` for the three place domains,
    ``route_id`` for transport — which is the same key
    ``services.candidate_admission._identity_provenance_binding`` re-derives.
    Reading it off the candidate keeps one definition of "about this entity" on
    both sides of the Gate.
    """

    entity_ids: dict[str, str] = {}
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        key = (
            "route_id"
            if str(candidate.get("candidate_kind") or "") == "transport"
            else "place_id"
        )
        entity_id = str(candidate.get(key) or "").strip()
        if candidate_id and entity_id:
            entity_ids[candidate_id] = entity_id
    return entity_ids


def _drop_borrowed_compiled_source_links(
    payload: dict[str, Any],
    *,
    expected_worker: str,
) -> None:
    """Refuse a compiled source that was minted for some *other* entity.

    The repair prompt hands the model this round's compiled ``external_tool``
    source ids verbatim, because it has to: the model must name the exact record
    the server compiled rather than invent an id.  The list is flat, so any id on
    it can be attached to any fact, and only one field per candidate was checked
    for entity scope — the identity field, at admission.  Every other field was
    free to hang on a genuine compiled record about a different place: a real
    Provider response, a real server-minted id, and a value that really does
    occur in that response's snapshot, so the "verified value" check passes too.
    The result reads as strongly grounded and is about the wrong entity.

    Judged per link against server-owned data, since a compiled id carries the
    entity digest in its own final segment.  A failing link is dropped, and a
    fact left with no support at all goes with it, which is the same shape the
    ``external_web`` refusal above takes: the candidate falls into the ordinary
    "not enough evidence" path instead of the whole round being rejected.  A
    fact that still has a legitimate link keeps it and stays.
    """

    facts = payload.get("fact_assertions")
    if not isinstance(facts, list) or not facts:
        return
    entity_ids = _candidate_provenance_entity_ids(payload)
    if not entity_ids:
        return
    dropped_links: dict[str, str] = {}
    dropped_facts: list[str] = []
    kept_facts: list[Any] = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            kept_facts.append(fact)
            continue
        entity_ref = fact.get("entity_ref")
        owner = (
            str(entity_ref.get("entity_id") or "").strip()
            if isinstance(entity_ref, Mapping)
            else ""
        )
        entity_id = entity_ids.get(owner)
        links = fact.get("source_links")
        if entity_id is None or not isinstance(links, list):
            # A fact about something that is not a candidate in this packet has
            # no entity to scope against; the candidate closure prune removes it.
            kept_facts.append(fact)
            continue
        kept_links = []
        for link in links:
            source_id = (
                str(link.get("source_record_id") or "")
                if isinstance(link, Mapping)
                else ""
            )
            if source_id.startswith(
                COMPILED_TOOL_SOURCE_ID_PREFIX
            ) and not compiled_tool_source_id_is_about(source_id, entity_id):
                dropped_links[str(fact.get("fact_assertion_id") or "")] = source_id
                continue
            kept_links.append(link)
        if not kept_links:
            dropped_facts.append(str(fact.get("fact_assertion_id") or ""))
            continue
        kept_facts.append({**fact, "source_links": kept_links})
    if not dropped_links:
        return
    logger.warning(
        "Research Packet compiled source links dropped (wrong entity) | worker=%s "
        "dropped_links=%s dropped_facts=%s",
        expected_worker,
        dropped_links,
        dropped_facts or "-",
    )
    payload["fact_assertions"] = kept_facts


def _payload_generated_at(payload: Mapping[str, Any]) -> datetime:
    """Read the packet's own timestamp so derived records stay deterministic."""
    raw = payload.get("generated_at")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _failed_tool_sources(
    *,
    existing_audit_ids: set[str],
    context_messages: Sequence[Mapping[str, Any]],
    default_retrieved_at: datetime,
) -> list[SourceRecord]:
    """Compile rejected failure records directly from Tool Gateway envelopes."""
    additions: list[SourceRecord] = []
    for message in context_messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        try:
            envelope = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict):
            continue
        if str(envelope.get("status") or "") in CAPABILITY_DECLARATION_STATUSES:
            # A Gateway date-capability decision (``reference_only`` /
            # ``not_applicable``) is a server statement about what the provider
            # can answer, taken before any call.  Compiling it as a rejected
            # source made Candidate Gate spend the domain's single targeted
            # research attempt and put the healthy tool on ``excluded_tools``.
            # ``status`` is the server-owned carrier here: unlike
            # ``metadata.policy`` it is a closed enum written only by the
            # Gateway and it survives ``compact_tool_content_for_model``.
            continue
        audit_id = str(envelope.get("audit_id") or "").strip()
        failure_detail = str(
            envelope.get("degradation_reason") or envelope.get("error") or ""
        ).strip()
        fallback_from = str(envelope.get("fallback_from") or "").strip()
        if (
            not audit_id
            or audit_id in existing_audit_ids
            or not (failure_detail or fallback_from)
        ):
            continue
        tool_name = fallback_from or str(envelope.get("tool_name") or "external_tool")
        retrieved_at = default_retrieved_at
        raw_retrieved_at = envelope.get("retrieved_at")
        if isinstance(raw_retrieved_at, str) and raw_retrieved_at.strip():
            try:
                retrieved_at = datetime.fromisoformat(
                    raw_retrieved_at.strip().replace("Z", "+00:00")
                )
            except ValueError:
                pass
        snapshot = dict(envelope)
        additions.append(
            SourceRecord(
                source_record_id=f"source_tool_failure_{audit_id}",
                source_kind="external_tool",
                title=f"{tool_name} degradation",
                provider_name=str(
                    envelope.get("server_name")
                    or envelope.get("tool_name")
                    or tool_name
                ),
                public_excerpt=(
                    failure_detail
                    or str(envelope.get("result_summary") or "tool degraded")
                )[:900],
                retrieved_at=retrieved_at,
                content_hash=_canonical_snapshot_hash(snapshot),
                snapshot=snapshot,
                lifecycle_status="rejected",
            )
        )
        existing_audit_ids.add(audit_id)
    return additions


def _merge_failed_tool_sources(
    packet: ResearchPacket,
    context_messages: Sequence[Mapping[str, Any]],
) -> ResearchPacket:
    """Persist execution failures independently of model-authored packet JSON."""
    existing_audit_ids = {
        str(source.tool_audit_id or source.snapshot.get("audit_id") or "")
        for source in packet.source_records
        if source.tool_audit_id or source.snapshot.get("audit_id")
    }
    additions = _failed_tool_sources(
        existing_audit_ids=existing_audit_ids,
        context_messages=context_messages,
        default_retrieved_at=packet.generated_at,
    )
    if not additions:
        return packet
    return ResearchPacket.model_validate(
        {
            **packet.model_dump(mode="python"),
            "source_records": [*packet.source_records, *additions],
        }
    )


def _controlled_worker_return_failure_source(
    *,
    authoritative_packet_metadata: Mapping[str, Any],
    controlled_worker_return_failure: Mapping[str, Any],
    default_retrieved_at: datetime,
) -> SourceRecord | None:
    """Build one rejected source for the fixed eval worker-return boundary.

    This source is not external evidence and deliberately has no fact link or
    ToolAudit id.  It is retained solely because a worker-return adapter stops
    before a Tool Gateway envelope exists; Candidate Gate still needs the
    normal rejected Provider-failure record to perform scoped closeout.
    """

    target = controlled_worker_return_failure.get("eval_target")
    expected_worker = authoritative_packet_metadata.get("worker_kind")
    node_name = controlled_worker_return_failure.get("eval_worker_return_node")
    if not isinstance(target, Mapping) or not isinstance(expected_worker, str):
        return None
    raw_node_names = target.get("node_names")
    scope = target.get("scope")
    expected_targets = {
        "all_content": (
            "destination_researcher",
            "transport_researcher",
            "accommodation_researcher",
        ),
    }
    if (
        controlled_worker_return_failure.get("controlled_eval") is not True
        or controlled_worker_return_failure.get("natural_provider_failure") is not False
        or controlled_worker_return_failure.get("eval_boundary") != "worker_return"
        or not isinstance(raw_node_names, list)
        or not isinstance(scope, str)
        or tuple(raw_node_names) != expected_targets.get(scope)
        or node_name != expected_worker
        or expected_worker not in raw_node_names
    ):
        return None
    plan_hash = str(
        controlled_worker_return_failure.get("eval_plan_hash") or ""
    ).strip()
    plan_id = str(controlled_worker_return_failure.get("eval_plan_id") or "").strip()
    fault_kind = str(
        controlled_worker_return_failure.get("eval_fault_kind") or ""
    ).strip()
    error = str(controlled_worker_return_failure.get("error") or "").strip()
    if not plan_hash or not plan_id or not fault_kind or not error:
        return None

    snapshot = {
        **dict(controlled_worker_return_failure),
        "status": "failed",
        "evidence_allowed": False,
        "worker_kind": expected_worker,
        "source_contract": "controlled_eval_worker_return_failure.v1",
    }
    source_digest = hashlib.sha256(
        (
            f"{authoritative_packet_metadata.get('research_packet_id', '')}\0"
            f"{plan_hash}\0{expected_worker}\0{scope}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    return SourceRecord(
        source_record_id=f"source_worker_return_failure_{source_digest}",
        source_kind="external_tool",
        title=f"{scope} worker boundary failure",
        provider_name="controlled_eval_boundary",
        public_excerpt=error[:900],
        retrieved_at=default_retrieved_at,
        content_hash=_canonical_snapshot_hash(snapshot),
        snapshot=snapshot,
        lifecycle_status="rejected",
    )


def build_failure_only_research_packet(
    *,
    authoritative_packet_metadata: Mapping[str, Any],
    context_messages: Sequence[Mapping[str, Any]],
    controlled_worker_return_failure: Mapping[str, Any] | None = None,
) -> ResearchPacket | None:
    """Persist rejected execution failures when Packet serialization fails.

    ``controlled_worker_return_failure`` accepts only the fixed v4 Registry
    application payload above.  It cannot insert a candidate, fact, Bundle,
    checkpoint, or SSE frame.
    """
    generated_at_value = authoritative_packet_metadata.get("generated_at")
    if isinstance(generated_at_value, datetime):
        generated_at = generated_at_value
    elif isinstance(generated_at_value, str):
        try:
            generated_at = datetime.fromisoformat(
                generated_at_value.strip().replace("Z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None
    sources = _failed_tool_sources(
        existing_audit_ids=set(),
        context_messages=context_messages,
        default_retrieved_at=generated_at,
    )
    if controlled_worker_return_failure is not None:
        controlled_source = _controlled_worker_return_failure_source(
            authoritative_packet_metadata=authoritative_packet_metadata,
            controlled_worker_return_failure=controlled_worker_return_failure,
            default_retrieved_at=generated_at,
        )
        if controlled_source is not None:
            sources.append(controlled_source)
    if not sources:
        return None
    return ResearchPacket.model_validate(
        {
            **authoritative_packet_metadata,
            "candidates": [],
            "source_records": sources,
            "fact_assertions": [],
            "field_provenance": [],
        }
    )


def _parse_tool_envelope(message: Mapping[str, Any]) -> dict[str, Any] | None:
    if message.get("role") != "tool":
        return None
    content = message.get("content")
    try:
        envelope = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        return None
    return envelope if isinstance(envelope, dict) else None


def authoritative_tool_messages(
    tool_results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Wrap full Gateway results for deterministic evidence compilation.

    These messages are never sent to a model and therefore must not pass through
    ``compact_tool_content_for_model``.
    """

    return [
        {"role": "tool", "content": dict(result)}
        for result in tool_results
        if isinstance(result, Mapping)
    ]


def _successful_place_records(
    context_messages: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[dict[str, Any], dict[str, Any], int]]:
    """Index the latest admissible complete Provider envelope by stable place id."""
    records: dict[str, tuple[dict[str, Any], dict[str, Any], int]] = {}
    for message in context_messages:
        envelope = _parse_tool_envelope(message)
        if (
            envelope is None
            or envelope.get("tool_name") != "global_place_search"
            or envelope.get("status") != "success"
        ):
            continue
        metadata = envelope.get("metadata")
        if (
            not isinstance(metadata, dict)
            or metadata.get("evidence_allowed") is not True
            or metadata.get("quarantine_result") is True
        ):
            continue
        result = envelope.get("sanitized_result")
        if not isinstance(result, dict) or result.get("success") is not True:
            continue
        provider_results = result.get("results")
        if not isinstance(provider_results, list):
            continue
        for index, item in enumerate(provider_results):
            if not isinstance(item, dict):
                continue
            values = (
                item.get("place_id"),
                item.get("provider_place_type"),
                item.get("provider_country_code"),
                item.get("name"),
                item.get("address"),
            )
            if not all(isinstance(value, str) and value.strip() for value in values):
                continue
            records[str(item["place_id"])] = (envelope, item, index)
    return records


def _quality_verified_place_sources(
    context_messages: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Index evidence-eligible branch-level dining quality checks by place id."""
    records: dict[str, list[dict[str, Any]]] = {}
    for message in context_messages:
        envelope = _parse_tool_envelope(message)
        if envelope is None or envelope.get("status") != "success":
            continue
        metadata = envelope.get("metadata")
        if (
            not isinstance(metadata, dict)
            or metadata.get("evidence_allowed") is not True
            or metadata.get("quarantine_result") is True
        ):
            continue
        place_ids = metadata.get(QUALITY_VERIFIED_PLACE_IDS_METADATA_KEY)
        if not isinstance(place_ids, list):
            continue
        audit_id = str(envelope.get("audit_id") or "").strip()
        if not audit_id:
            continue
        for place_id in place_ids:
            normalized = str(place_id or "").strip()
            if normalized:
                records.setdefault(normalized, []).append(envelope)
    return records


def _successful_route_records(
    context_messages: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[dict[str, Any], dict[str, Any], int]]:
    """Index complete admissible Provider routes by stable route id."""
    records: dict[str, tuple[dict[str, Any], dict[str, Any], int]] = {}
    for message in context_messages:
        envelope = _parse_tool_envelope(message)
        if (
            envelope is None
            or envelope.get("tool_name")
            not in {"global_route_search", "search_flights", "domestic_rail_search"}
            or envelope.get("status") != "success"
        ):
            continue
        metadata = envelope.get("metadata")
        if (
            not isinstance(metadata, dict)
            or metadata.get("evidence_allowed") is not True
            or metadata.get("quarantine_result") is True
        ):
            continue
        result = envelope.get("sanitized_result")
        if not isinstance(result, dict) or result.get("success") is not True:
            continue
        routes = result.get("routes")
        if not isinstance(routes, list):
            continue
        for index, route in enumerate(routes):
            if not isinstance(route, dict):
                continue
            audit_id = str(envelope.get("audit_id") or "").strip()
            route_id = route.get("route_id")
            transport_class = route.get("transport_class")
            selected_mode = route.get("selected_mode")
            segments = route.get("segments")
            if (
                not audit_id
                or not isinstance(route_id, str)
                or not route_id.strip()
                or transport_class
                not in {"long_distance", "public_transit", "flexible"}
                or not isinstance(selected_mode, str)
                or not isinstance(segments, list)
                or not segments
                or not isinstance(route.get("duration_minutes"), int)
            ):
                continue
            if transport_class in TIMETABLED_TRANSPORT_CLASSES and (
                not isinstance(route.get("departure_at"), str)
                or not isinstance(route.get("arrival_at"), str)
            ):
                continue
            try:
                TransportMode(selected_mode)
                origin = TransportEndpoint.model_validate(route.get("from_endpoint"))
                destination = TransportEndpoint.model_validate(route.get("to_endpoint"))
                typed_segments = [
                    TransportSegment.model_validate(segment) for segment in segments
                ]
            except (TypeError, ValueError, ValidationError):
                continue
            if (
                typed_segments[0].from_endpoint != origin
                or typed_segments[-1].to_endpoint != destination
            ):
                continue
            records[route_id] = (envelope, route, index)
    return records


def _controlled_destination_ids(base_payload: Mapping[str, Any]) -> list[str]:
    query_context = base_payload.get("query_context")
    identity = (
        query_context.get("controlled_trip_identity")
        if isinstance(query_context, Mapping)
        else None
    )
    destinations = (
        identity.get("destinations") if isinstance(identity, Mapping) else None
    )
    if not isinstance(destinations, list):
        return []
    return list(
        dict.fromkeys(
            str(item.get("place_id") or "").strip()
            for item in destinations
            if isinstance(item, Mapping) and str(item.get("place_id") or "").strip()
        )
    )


def _eligible_route_selection_options(
    context_messages: Sequence[Mapping[str, Any]],
    *,
    required_transport_classes: Sequence[str] | None,
    excluded_candidate_ids: Sequence[str] | None,
    required_route_scopes: Sequence[ProviderEvidenceScope] = (),
) -> list[dict[str, Any]]:
    required = set(required_transport_classes or ())
    excluded = set(excluded_candidate_ids or ())
    # Scopes grouped by their route leg's service date, in declared order.  A date
    # may owe several long-distance legs (same-day round trip, or a handover day
    # that also carries the return): previously every route of such a date matched
    # *more than one* scope by date and was skipped, so the same-day case produced
    # no eligible options at all and the legs were never admitted.  Assign routes
    # to the day's scopes round-robin instead, so each scope gets at least one
    # option whenever the day has at least as many routes as legs.
    scopes_by_date: dict[date, list[ProviderEvidenceScope]] = {}
    for scope in required_route_scopes:
        if scope.transport_class == "long_distance" and scope.route_leg is not None:
            scopes_by_date.setdefault(scope.route_leg.service_date, []).append(scope)
    date_route_index: dict[date, int] = {}
    options: list[dict[str, Any]] = []
    for route_id, (envelope, route, _) in _successful_route_records(
        context_messages
    ).items():
        transport_class = str(route["transport_class"])
        candidate_id = _provider_candidate_id("transport", route_id)
        if (required and transport_class not in required) or candidate_id in excluded:
            continue
        provider_scope_id: str | None = None
        if transport_class == "long_distance":
            departure_at = route.get("departure_at")
            try:
                service_date = datetime.fromisoformat(
                    str(departure_at).replace("Z", "+00:00")
                ).date()
            except (TypeError, ValueError):
                continue
            if required_route_scopes:
                scopes_today = scopes_by_date.get(service_date, [])
                if not scopes_today:
                    continue
                index = date_route_index.get(service_date, 0)
                date_route_index[service_date] = index + 1
                provider_scope_id = scopes_today[index % len(scopes_today)].scope_id
        options.append(
            {
                "route_id": route_id,
                "transport_class": transport_class,
                "selected_mode": str(route["selected_mode"]),
                "from_endpoint": route["from_endpoint"],
                "to_endpoint": route["to_endpoint"],
                "duration_minutes": route["duration_minutes"],
                "audit_id": str(envelope.get("audit_id") or ""),
                "provider_evidence_scope_id": provider_scope_id,
            }
        )
    return options


def has_provider_route_selection_option(
    context_messages: Sequence[Mapping[str, Any]],
    *,
    required_transport_classes: Sequence[str] | None,
    excluded_candidate_ids: Sequence[str] | None = None,
    required_route_scopes: Sequence[ProviderEvidenceScope] = (),
) -> bool:
    """Return whether one complete Provider route can close this scoped round."""
    return bool(
        _eligible_route_selection_options(
            context_messages,
            required_transport_classes=required_transport_classes,
            excluded_candidate_ids=excluded_candidate_ids,
            required_route_scopes=required_route_scopes,
        )
    )


def has_required_provider_route_selection_options(
    context_messages: Sequence[Mapping[str, Any]],
    *,
    required_transport_classes: Sequence[str] | None,
    excluded_candidate_ids: Sequence[str] | None = None,
    required_route_scopes: Sequence[ProviderEvidenceScope] = (),
) -> bool:
    """Require one eligible Provider option for every assigned exact route leg."""

    options = _eligible_route_selection_options(
        context_messages,
        required_transport_classes=required_transport_classes,
        excluded_candidate_ids=excluded_candidate_ids,
        required_route_scopes=required_route_scopes,
    )
    expected_scope_ids = {
        scope.scope_id
        for scope in required_route_scopes
        if scope.transport_class == "long_distance"
    }
    if not expected_scope_ids:
        return bool(options)
    option_scope_ids = {
        str(option.get("provider_evidence_scope_id") or "") for option in options
    }
    return expected_scope_ids <= option_scope_ids


def _eligible_place_selection_options(
    context_messages: Sequence[Mapping[str, Any]],
    expected_worker: ResearchWorkerKind,
    *,
    excluded_candidate_ids: Sequence[str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Compile the bounded Provider result set the Worker may explicitly select.

    ``excluded_candidate_ids`` is applied here, not only at the packet check, for
    the same reason the far-place drop is: this list is what the repair path picks
    from, so leaving a forbidden option on it is an invitation to spend the round
    and then have the packet refused.  The route enum has always filtered them
    (:func:`_eligible_route_selection_options`); the place enum not doing so was the second table.
    """
    forbidden = set(excluded_candidate_ids or ())
    allowed_kinds = {
        candidate_kind
        for candidate_kind, _ in _WORKER_CANDIDATE_MODELS[expected_worker]
        if candidate_kind in _PLACE_IDENTITY_FIELDS
    }
    options: dict[str, list[dict[str, str]]] = {
        candidate_kind: [] for candidate_kind in allowed_kinds
    }
    seen: set[tuple[str, str]] = set()
    # **Do not filter dining here to quality-verified places only.**  A branch-level
    # review decides whether the report *marks* the option 外部评价已核验, never
    # whether the restaurant may be offered.  This list is what the repair path
    # selects from and in practice every candidate comes from that path, so a filter
    # here — not admission — is what deletes every restaurant in a destination whose
    # script the locality tokeniser cannot read.
    dropped_far: list[tuple[str, float]] = []
    for envelope, provider_place, _ in _successful_place_records(
        context_messages
    ).values():
        audit_id = str(envelope.get("audit_id") or "").strip()
        if not audit_id:
            continue
        # A place the provider found for this destination but that is not *in* it
        # never becomes selectable.  Same-named places are the reason: ``明治神宫``
        # returns the Shibuya shrine first and a neighbourhood shrine in 茨城県守谷市
        # second, and a worker can pick the second — 36 km out, into a Tokyo Day.
        # Dropping it from the enum is what makes the near one get chosen rather than
        # lost, which is why this is the first of the two layers: admission can only
        # reject, it cannot re-pick.
        #
        # The distance is the binding layer's own server-written annotation, and
        # its absence means unanswerable, never far — see
        # ``destination_scope.annotate_destination_distance``, which **every** path
        # that binds a place record must call.  The three deterministic in-code
        # bindings did not, so this rule silently skipped every place they bound
        # and only the model-driven tool path was ever guarded.
        distance = provider_place.get(DESTINATION_DISTANCE_KEY)
        if isinstance(distance, (int, float)) and not isinstance(distance, bool):
            if float(distance) > MAX_DESTINATION_DISTANCE_KM:
                dropped_far.append((str(provider_place["name"]), float(distance)))
                continue
        provider_type = str(provider_place["provider_place_type"])
        for candidate_kind in allowed_kinds:
            key = (candidate_kind, str(provider_place["place_id"]))
            if key in seen or not provider_place_type_matches_candidate_kind(
                provider_type,
                candidate_kind,
            ):
                continue
            if (
                _provider_candidate_id(candidate_kind, str(provider_place["place_id"]))
                in forbidden
            ):
                continue
            result = envelope.get("sanitized_result")
            provider_query = (
                str(result.get("query") or "").strip()
                if isinstance(result, Mapping)
                else ""
            )
            options[candidate_kind].append(
                {
                    "candidate_kind": candidate_kind,
                    "place_id": str(provider_place["place_id"]),
                    "name": str(provider_place["name"]),
                    "provider_place_type": provider_type,
                    "provider_country_code": str(
                        provider_place["provider_country_code"]
                    ),
                    "audit_id": audit_id,
                    # The query is server-echoed Provider input.  It is retained
                    # so a model timeout can reuse the LLM-authored Research
                    # Query Plan instead of choosing an arbitrary place id.
                    "provider_query": provider_query,
                }
            )
            seen.add(key)
    if dropped_far:
        # This filter is the one thing in this function that can take a domain to
        # zero, so the drop is **never silent**.  There is deliberately no "if it
        # emptied the domain, keep them anyway" branch: a domain with no option left
        # goes on to the existing missing-candidate gap and its targeted-research
        # attempt, which is a real repair, whereas re-admitting a stop 36 km outside
        # the city is the defect.
        emptied = sorted(kind for kind in allowed_kinds if not options[kind])
        logger.info(
            "Place options outside destination | dropped=%s%s",
            ", ".join(f"{name}@{km:.1f}km" for name, km in dropped_far),
            f" emptied={emptied}" if emptied else "",
        )
    return options


def _has_eligible_place_selection(
    options: Mapping[str, Sequence[Mapping[str, str]]],
) -> bool:
    return any(options.values())


@dataclass(frozen=True)
class ObservedPlaceNominations:
    """Hops 2-4 of the knowledge-base nomination chain, read off one round.

    A knowledge-base chunk can only ever *nominate* a place — identity stays the
    Provider's — so a nomination becomes real by being looked up, admitted as an
    option, and then selected. This is what actually happened, so a measurement built
    on it is exact; see ``rag.place_mentions`` for the heuristic half.

    ``lookups`` keeps each call whole — its query beside the identities it
    produced — instead of flattening into a list of queries and a list of names.
    The pairing *is* the datum: it is what lets the funnel say "this identity came
    out of a call a chunk caused" without ever comparing the Provider's spelling
    to the corpus's.

    Each query is read from the envelope's own echoed ``query``, **never** from
    ``sanitized_args_summary``: the summary is a rendered, truncated string, and
    reading a derived rendering as if it were the datum is how a hand-copied
    character-overhead constant drifts. A place call the Provider failed outright
    carries no echo, so it is absent here — those are already logged as provider
    failures in their own right.
    """

    lookups: tuple[PlaceLookup, ...]
    selectable_place_ids: tuple[str, ...]


def observed_place_nominations(
    context_messages: Sequence[Mapping[str, Any]],
    *,
    expected_worker: ResearchWorkerKind,
) -> ObservedPlaceNominations:
    """What this round asked the place Provider, got back, and may select from."""

    lookups: list[PlaceLookup] = []
    for message in context_messages:
        envelope = _parse_tool_envelope(message)
        if envelope is None or envelope.get("tool_name") != "global_place_search":
            continue
        result = envelope.get("sanitized_result")
        if not isinstance(result, Mapping):
            continue
        query = result.get("query")
        if not isinstance(query, str) or not query.strip():
            continue
        identities: list[ProviderIdentity] = []
        provider_results = result.get("results")
        if isinstance(provider_results, Sequence) and not isinstance(
            provider_results, str
        ):
            for item in provider_results:
                if not isinstance(item, Mapping):
                    continue
                place_id, name = item.get("place_id"), item.get("name")
                if not (isinstance(place_id, str) and place_id.strip()):
                    continue
                identities.append(
                    ProviderIdentity(
                        place_id=place_id,
                        name=name
                        if isinstance(name, str) and name.strip()
                        else place_id,
                    )
                )
        lookups.append(PlaceLookup(query=query, identities=tuple(identities)))
    options = _eligible_place_selection_options(context_messages, expected_worker)
    return ObservedPlaceNominations(
        lookups=tuple(lookups),
        selectable_place_ids=tuple(
            dict.fromkeys(
                str(option["place_id"])
                for kind_options in options.values()
                for option in kind_options
                if option.get("place_id")
            )
        ),
    )


def _authoritative_external_tool_sources(
    evidence_messages: Sequence[Mapping[str, Any]],
    *,
    eligible_place_options: Mapping[str, Sequence[Mapping[str, str]]],
    authoritative_source_records: Sequence[SourceRecord] = (),
) -> list[dict[str, str]]:
    """List every ``external_tool`` source id a model may bind evidence to.

    Three id families reach this list: the admitted prior closure handed to a
    scoped retry, the place identity bound from a successful Tool Gateway place
    envelope, and — for ``dining`` only — the web retrieval that corroborated
    that place, whose id is listed below under the web tool's own name.

    That third family is why ``external_web`` is rare rather than routine.  Every
    web retrieval in this repo goes through the Tool Gateway and carries an
    ``audit_id``, so when its result corroborates a dining place the server
    compiles it into an ``external_tool`` record itself and hands the model the
    id.  ``external_web`` is the channel for web evidence the server did *not*
    compile — a page the worker read that no listed id covers.  It stays a real
    channel (the grounding pass below accepts one whose URL this round retrieved),
    but a model that binds to the listed id instead is doing the better thing,
    and in practice that is what happens.

    Compiled failure records are deliberately absent.  They are merged into the
    packet server-side on every return path, so offering their ids here bought
    nothing and cost a great deal: it invited the model to hang candidate
    identity facts on a rejected source, which the packet contract then strips,
    turning a grounded round into a zero-candidate one.

    Each entry carries its ``tool_name`` so the model can match a transcript
    result to the exact id instead of composing one from the tool's name.
    """
    listed: dict[str, str] = {}
    for source in authoritative_source_records:
        if source.source_kind == "external_tool":
            listed.setdefault(source.source_record_id, source.provider_name)
    quality_sources = _quality_verified_place_sources(evidence_messages)
    for candidate_kind, options in eligible_place_options.items():
        for option in options:
            audit_id = str(option.get("audit_id") or "").strip()
            place_id = str(option.get("place_id") or "").strip()
            if not audit_id or not place_id:
                continue
            listed.setdefault(
                _provider_entity_source_id(audit_id, place_id),
                "global_place_search",
            )
            if candidate_kind != "dining":
                continue
            for envelope in quality_sources.get(place_id, ()):
                quality_audit_id = str(envelope.get("audit_id") or "").strip()
                if not quality_audit_id:
                    continue
                listed.setdefault(
                    _provider_entity_source_id(quality_audit_id, place_id),
                    str(
                        envelope.get("tool_name")
                        or envelope.get("server_name")
                        or "external_review_search"
                    ),
                )
    return [
        {"source_record_id": source_record_id, "tool_name": tool_name}
        for source_record_id, tool_name in listed.items()
    ]


def has_required_provider_place_selection(
    context_messages: Sequence[Mapping[str, Any]],
    *,
    expected_worker: ResearchWorkerKind,
    required_candidate_kinds: Sequence[str],
) -> bool:
    """Return whether every requested place domain already has a Provider option."""
    options = _eligible_place_selection_options(context_messages, expected_worker)
    return all(
        options.get(candidate_kind) for candidate_kind in required_candidate_kinds
    )


def provider_evidence_outcomes(
    context_messages: Sequence[Mapping[str, Any]],
    *,
    expected_worker: ResearchWorkerKind,
    packet: ResearchPacket | None,
    assignments: Sequence[ProviderEvidenceAssignment],
) -> dict[str, ProviderEvidenceOutcome]:
    """Project one worker attempt into independently resolvable scope outcomes."""

    if any(
        assignment.scope.worker_kind != expected_worker for assignment in assignments
    ):
        raise ValueError("Provider evidence assignment belongs to another worker")
    place_options = (
        _eligible_place_selection_options(context_messages, expected_worker)
        if expected_worker != "transport_researcher"
        else {}
    )
    route_options = (
        _eligible_route_selection_options(
            context_messages,
            required_transport_classes=None,
            excluded_candidate_ids=None,
            required_route_scopes=[
                assignment.scope
                for assignment in assignments
                if assignment.scope.route_leg is not None
            ],
        )
        if expected_worker == "transport_researcher"
        else []
    )
    source_by_id = (
        {source.source_record_id: source for source in packet.source_records}
        if packet is not None
        else {}
    )

    def is_provider_materialized(candidate: Any) -> bool:
        return any(
            source_by_id.get(source_id) is not None
            and source_by_id[source_id].tool_audit_id
            for source_id in candidate.source_record_ids
        )

    outcomes: dict[str, ProviderEvidenceOutcome] = {}
    for assignment in assignments:
        scope = assignment.scope
        if scope.transport_class is not None:
            option_count = sum(
                option.get("transport_class") == scope.transport_class
                and (
                    scope.transport_class != "long_distance"
                    or option.get("provider_evidence_scope_id") == scope.scope_id
                )
                for option in route_options
            )
            materialized_count = sum(
                candidate.candidate_kind == "transport"
                and candidate.transport_class == scope.transport_class
                and (
                    scope.transport_class != "long_distance"
                    or candidate.provider_evidence_scope_id == scope.scope_id
                )
                and is_provider_materialized(candidate)
                for candidate in (packet.candidates if packet is not None else ())
            )
        else:
            candidate_kind = scope.candidate_kind
            option_count = len(place_options.get(candidate_kind or "", ()))
            materialized_count = sum(
                candidate.candidate_kind == candidate_kind
                and is_provider_materialized(candidate)
                for candidate in (packet.candidates if packet is not None else ())
            )
        if materialized_count:
            status = "materialized"
            unresolved_loss_count = 0
        elif option_count:
            status = "unresolved_loss"
            unresolved_loss_count = option_count
        else:
            status = "no_option"
            unresolved_loss_count = 0
        outcomes[scope.scope_id] = ProviderEvidenceOutcome(
            scope=scope,
            attempt_number=assignment.attempt_number,
            provider_option_count=option_count,
            provider_option_materialized_count=materialized_count,
            unresolved_loss_count=unresolved_loss_count,
            status=status,
        )
    return outcomes


def provider_round_answered_empty(
    context_messages: Sequence[Mapping[str, Any]],
    *,
    outcomes: Mapping[str, ProviderEvidenceOutcome],
) -> bool:
    """Whether this worker round is purely "the Provider answered, found nothing".

    A Provider that completes a call and reports zero hits returns a *success*
    envelope, so ``build_failure_only_research_packet`` has no rejected record
    to persist and ``last_error`` becomes the only channel that can carry the
    reason.  Naming that reason ``schema_gate:`` is factually wrong — Research
    Packet schema validation is not what failed — and it costs the domain the
    one bounded targeted re-research the honest reason grants.

    All three facts must hold, so the answer stays a statement about reality:

    * at least one Gateway envelope carries the honest ``empty_success``
      Provider outcome stamped by ``agents.utils.execute_tool``;
    * every envelope in the round is a plain success — nothing failed,
      degraded, or fell back, which is exactly the state in which no failed
      SourceRecord exists to classify;
    * every assigned Provider evidence scope ended at ``no_option``, i.e. the
      round genuinely had nothing to admit a candidate from.

    A round that *did* hold Provider options and still produced no packet is a
    packet-layer failure and keeps its ``schema_gate:`` attribution.
    """

    saw_empty_success = False
    for message in context_messages:
        envelope = _parse_tool_envelope(message)
        if envelope is None:
            continue
        if (
            envelope.get("status") != ToolExecutionStatus.SUCCESS.value
            or envelope.get("error")
            or envelope.get("degradation_reason")
            or envelope.get("fallback_from")
        ):
            return False
        metadata = envelope.get("metadata")
        if (
            isinstance(metadata, Mapping)
            and metadata.get(PROVIDER_RESULT_OUTCOME_METADATA_KEY)
            == PROVIDER_RESULT_OUTCOME_EMPTY_SUCCESS
        ):
            saw_empty_success = True
    if not saw_empty_success:
        return False
    return all(outcome.status == "no_option" for outcome in outcomes.values())


def provider_round_capability_declared(
    context_messages: Sequence[Mapping[str, Any]],
    *,
    outcomes: Mapping[str, ProviderEvidenceOutcome],
) -> bool:
    """Whether this worker round is purely "the Provider cannot answer this date".

    The Tool Gateway takes the date-capability decision *before* any provider
    call, so the envelope carries no ``error`` and compiles into no rejected
    SourceRecord — ``build_failure_only_research_packet`` therefore has nothing
    to persist and ``last_error`` is the only channel left to carry the reason.
    Letting that reason default to ``schema_gate:`` is factually wrong twice
    over: Research Packet schema validation is not what failed, and the
    deterministic classification it implies closes the domain without the one
    bounded targeted re-research — which here is exactly the round that would
    switch modality (rail → flight) and actually produce candidates.

    All three facts must hold, mirroring ``provider_round_answered_empty``:

    * at least one envelope is a Gateway capability declaration
      (``tools.governance.CAPABILITY_DECLARATION_STATUSES``);
    * nothing in the round genuinely failed, degraded, or fell back — a real
      provider failure keeps its own honest attribution and its rejected
      SourceRecord;
    * every assigned Provider evidence scope ended at ``no_option``.
    """

    saw_capability = False
    for message in context_messages:
        envelope = _parse_tool_envelope(message)
        if envelope is None:
            continue
        if str(envelope.get("status") or "") in CAPABILITY_DECLARATION_STATUSES:
            saw_capability = True
            continue
        if (
            envelope.get("status") != ToolExecutionStatus.SUCCESS.value
            or envelope.get("error")
            or envelope.get("degradation_reason")
            or envelope.get("fallback_from")
        ):
            return False
    if not saw_capability:
        return False
    return all(outcome.status == "no_option" for outcome in outcomes.values())


def _provider_selection_base_payload(
    raw: str,
    *,
    expected_worker: ResearchWorkerKind,
    expected_run_id: str,
    authoritative_packet_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Keep only authoritative Packet metadata from a malformed model payload."""
    if authoritative_packet_metadata is not None:
        return {
            **authoritative_packet_metadata,
            "source_records": [],
        }
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("worker_kind") != expected_worker
        or payload.get("run_id") != expected_run_id
    ):
        return None
    for field_name in ("research_packet_id", "task_id"):
        if (
            not isinstance(payload.get(field_name), str)
            or not payload[field_name].strip()
        ):
            return None
    for field_name in ("constraint_pack_revision", "fact_data_revision"):
        value = payload.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
    if not isinstance(payload.get("query_context"), dict):
        return None
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        return None
    try:
        datetime.fromisoformat(generated_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return {
        "research_packet_id": payload["research_packet_id"],
        "run_id": expected_run_id,
        "task_id": payload["task_id"],
        "worker_kind": expected_worker,
        "constraint_pack_revision": payload["constraint_pack_revision"],
        "fact_data_revision": payload["fact_data_revision"],
        "query_context": payload["query_context"],
        "source_records": [],
        "generated_at": generated_at,
    }


def _source_retrieved_at(envelope: Mapping[str, Any], fallback: Any) -> Any:
    raw = envelope.get("retrieved_at")
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            pass
    return fallback


def _source_datetime(value: Any, fallback: Any) -> Any:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            pass
    return fallback


def _provider_snapshot_source_fields(
    envelope: Mapping[str, Any],
    *,
    fallback_observed_at: Any,
) -> tuple[
    dict[str, Any],
    ProviderSnapshotProvenance | None,
    Any,
    Any,
    Any,
]:
    """Project the actual Provider snapshot, not this Run's Tool Envelope.

    The envelope still owns the current audit id.  When a strict snapshot-cache
    marker is present, this helper preserves the original observation/retrieval
    window and hash so a cache hit cannot masquerade as a fresh Provider call.
    Older non-cached envelopes deliberately retain the existing representation
    until they are naturally re-run through the new Gateway boundary.
    """
    result = envelope.get("sanitized_result")
    metadata = envelope.get("metadata")
    cache_metadata = (
        metadata.get("snapshot_cache")
        if isinstance(metadata, Mapping)
        and isinstance(metadata.get("snapshot_cache"), Mapping)
        else None
    )
    if not isinstance(result, dict) or not isinstance(cache_metadata, Mapping):
        observed = _source_retrieved_at(envelope, fallback_observed_at)
        return dict(envelope), None, observed, observed, None

    origin = str(cache_metadata.get("origin") or "")
    provider_name = str(result.get("provider") or "").strip()
    tool_name = str(envelope.get("tool_name") or "").strip()
    if (
        origin not in {"live", "provider_snapshot_cache"}
        or not provider_name
        or not tool_name
    ):
        observed = _source_retrieved_at(envelope, fallback_observed_at)
        return dict(envelope), None, observed, observed, None
    observed_at = _source_datetime(
        cache_metadata.get("observed_at"),
        _source_retrieved_at(envelope, fallback_observed_at),
    )
    retrieved_at = _source_datetime(cache_metadata.get("retrieved_at"), observed_at)
    provider_valid_until = _source_datetime(cache_metadata.get("valid_until"), None)
    cache_valid_until = _source_datetime(cache_metadata.get("cache_valid_until"), None)
    try:
        provenance = ProviderSnapshotProvenance(
            origin=origin,
            data_environment=snapshot_data_environment(
                result, provider_name=provider_name
            ),
            provider_name=provider_name,
            tool_name=tool_name,
            cache_key_digest=str(cache_metadata.get("cache_key_digest") or ""),
            content_hash=str(cache_metadata.get("content_hash") or ""),
            observed_at=observed_at,
            retrieved_at=retrieved_at,
            provider_valid_until=provider_valid_until,
            cache_valid_until=cache_valid_until,
            provider_contract_version=str(cache_metadata.get("contract_version") or ""),
            payload_schema_version=str(
                cache_metadata.get("payload_schema_version") or ""
            ),
        )
    except (TypeError, ValueError):
        observed = _source_retrieved_at(envelope, fallback_observed_at)
        return dict(envelope), None, observed, observed, None
    return (
        dict(result),
        provenance,
        provenance.observed_at,
        provenance.retrieved_at,
        provenance.provider_valid_until,
    )


def _provider_fact_id(audit_id: str, candidate_id: str, field_path: str) -> str:
    digest = hashlib.sha256(
        f"{audit_id}\0{candidate_id}\0{field_path}".encode("utf-8")
    ).hexdigest()[:20]
    return f"fact_provider_{digest}"


def _provider_coordinate_pair(
    provider_place: Mapping[str, Any],
) -> tuple[float, float] | None:
    """Extract a valid (latitude, longitude) pair from a provider place record.

    Providers that resolve a physical place (nominatim, amap) carry the point
    geometry alongside the identity fields.  Binding it as verified facts is what
    lets the deterministic map projection place a real pin instead of a
    coordinate-less marker.  Returns None whenever either coordinate is missing
    or out of range so a place without geometry is simply left off the map.
    """

    latitude = provider_place.get("latitude")
    longitude = provider_place.get("longitude")
    if (
        isinstance(latitude, bool)
        or isinstance(longitude, bool)
        or not isinstance(latitude, (int, float))
        or not isinstance(longitude, (int, float))
    ):
        return None
    latitude = float(latitude)
    longitude = float(longitude)
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    return latitude, longitude


def _provider_candidate_id(candidate_kind: str, place_id: str) -> str:
    digest = hashlib.sha256(
        f"{candidate_kind}\0{place_id}".encode("utf-8")
    ).hexdigest()[:20]
    return f"candidate_{candidate_kind}_{digest}"


def _provider_entity_source_id(audit_id: str, entity_id: str) -> str:
    """Bind an entity snapshot to this Run's tool audit exactly once.

    The id layout lives in ``tools.governance`` because Candidate admission
    re-derives it to check that a compiled source is about the entity a
    candidate claims to be.
    """
    return compiled_tool_source_id(audit_id, entity_id)


def _provider_place_source_payload(
    envelope: Mapping[str, Any],
    provider_place: Mapping[str, Any],
    *,
    fallback_observed_at: Any,
) -> dict[str, Any]:
    """Build the one source record a successful place envelope authorizes.

    This is the server's own record of that Provider call: identity, snapshot,
    hash and cache lineage all come from the Tool Gateway envelope.  Candidate
    binding and registry compilation share this construction so a selected and
    an unselected place can never carry two different bodies for the same id.
    """
    audit_id = str(envelope.get("audit_id") or "").strip()
    (
        snapshot,
        cache_provenance,
        observed_at,
        retrieved_at,
        provider_valid_until,
    ) = _provider_snapshot_source_fields(
        envelope,
        fallback_observed_at=fallback_observed_at,
    )
    provider_name = str(
        (envelope.get("sanitized_result") or {}).get("provider")
        or envelope.get("server_name")
        or "global_place_search"
    )
    return {
        "source_record_id": _provider_entity_source_id(
            audit_id,
            str(provider_place["place_id"]),
        ),
        "source_kind": "external_tool",
        "title": f"{provider_name} place record: {provider_place['name']}",
        "provider_name": provider_name,
        "public_excerpt": str(provider_place["address"])[:900],
        "retrieved_at": retrieved_at,
        "observed_at": observed_at,
        "provider_valid_until": provider_valid_until,
        "content_hash": (
            cache_provenance.content_hash
            if cache_provenance is not None
            else _canonical_snapshot_hash(snapshot)
        ),
        "snapshot": snapshot,
        "lifecycle_status": "active",
        "tool_audit_id": audit_id,
        "cache_provenance": (
            cache_provenance.model_dump(mode="json")
            if cache_provenance is not None
            else None
        ),
    }


def _authoritative_tool_source_records(
    evidence_messages: Sequence[Mapping[str, Any]],
    *,
    authoritative_source_records: Sequence[SourceRecord],
    default_retrieved_at: datetime,
) -> dict[str, SourceRecord]:
    """Compile every tool source the server already holds for this packet.

    Three families reach it: the admitted prior closure handed to a scoped
    retry, the successful place envelopes of this round's Tool Gateway
    transcript, and the failure records compiled from that same transcript.
    This is the only admissible identity for an ``external_tool`` source id and
    the only provenance the server may supply on the model's behalf.

    A web retrieval reaches this registry only through a place envelope it
    corroborated — see :func:`_authoritative_external_tool_sources` for the
    dining case, which compiles the web envelope server-side and lists its id.
    A web page the server did not compile that way is ``external_web``, which the
    model authors itself and the grounding pass then checks against this round's
    retrieved URLs.
    """
    registry: dict[str, SourceRecord] = {
        source.source_record_id: source for source in authoritative_source_records
    }
    for place_id, (envelope, provider_place, _) in _successful_place_records(
        evidence_messages
    ).items():
        if not str(envelope.get("audit_id") or "").strip():
            continue
        payload = _provider_place_source_payload(
            envelope,
            provider_place,
            fallback_observed_at=default_retrieved_at,
        )
        registry.setdefault(
            str(payload["source_record_id"]),
            SourceRecord.model_validate(payload),
        )
    for source in _failed_tool_sources(
        existing_audit_ids=set(),
        context_messages=evidence_messages,
        default_retrieved_at=default_retrieved_at,
    ):
        registry.setdefault(source.source_record_id, source)
    return registry


def _provider_route_source_id(audit_id: str, route_id: str) -> str:
    """Bind a selected route snapshot to this Run's tool audit exactly once.

    A route's entity id is its ``route_id``, digested verbatim, so admission's
    identity binding for a transport candidate reads the same one id layout.
    """

    return compiled_tool_source_id(audit_id, route_id)


def _bind_successful_place_identity(
    payload: dict[str, Any],
    context_messages: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Bind Provider identity to a Worker-selected place without selecting it.

    The model remains responsible for choosing the candidate and its planning
    fields.  Reality identity, facts, provenance, and the complete source
    snapshot come only from the successful Tool Gateway transcript.
    """
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return {}
    records = _successful_place_records(context_messages)
    quality_sources = _quality_verified_place_sources(context_messages)
    if not records:
        return {}
    generated_at = payload.get("generated_at")
    existing_facts = payload.get("fact_assertions")
    facts = list(existing_facts) if isinstance(existing_facts, list) else []
    existing_provenance = payload.get("field_provenance")
    provenance = (
        list(existing_provenance) if isinstance(existing_provenance, list) else []
    )
    existing_sources = payload.get("source_records")
    sources = list(existing_sources) if isinstance(existing_sources, list) else []
    bound_sources: dict[str, dict[str, Any]] = {}
    # candidate_id -> the place_id whose Provider record contradicts its kind.
    contradicted_places: dict[str, str] = {}

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_kind = str(candidate.get("candidate_kind") or "")
        identity_fields = _PLACE_IDENTITY_FIELDS.get(candidate_kind)
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        place_id = str(candidate.get("place_id") or "").strip()
        record = records.get(place_id)
        if not identity_fields or not candidate_id or record is None:
            continue
        envelope, provider_place, result_index = record
        provider_type = str(provider_place["provider_place_type"])
        if not provider_place_type_matches_candidate_kind(
            provider_type,
            candidate_kind,
        ):
            # The Provider returned this very ``place_id`` — as something else.
            # Skipping alone leaves the model's invented identity in place while
            # a sibling candidate that *did* bind publishes a compiled source
            # about the same id, which the model's own ``place_id`` fact can then
            # cite: a museum record grounding a restaurant.  Record it and strip
            # that support below, so admission refuses on ``place_id`` — the true
            # reason — instead of admitting a place the Provider contradicted.
            contradicted_places[candidate_id] = place_id
            continue
        audit_id = str(envelope.get("audit_id") or "").strip()
        if not audit_id:
            continue
        source_id = _provider_entity_source_id(audit_id, place_id)
        (
            _snapshot,
            cache_provenance,
            observed_at,
            _retrieved_at,
            _provider_valid_until,
        ) = _provider_snapshot_source_fields(
            envelope,
            fallback_observed_at=generated_at,
        )
        bound_sources[source_id] = _provider_place_source_payload(
            envelope,
            provider_place,
            fallback_observed_at=generated_at,
        )
        provider_values = {
            _PLACE_NAME_FIELD[candidate_kind]: provider_place["name"],
            "place_id": provider_place["place_id"],
            "provider_place_type": provider_place["provider_place_type"],
            "provider_country_code": provider_place["provider_country_code"],
            "address": provider_place["address"],
        }
        candidate.update(provider_values)
        candidate["freshness_status"] = "current"
        candidate["observed_at"] = observed_at

        # Coordinates are bound only as verified facts (below), never onto the
        # candidate: the typed Candidate models forbid unknown fields and the map
        # projection resolves geometry from the entity's fact lineage, not the
        # candidate object.
        coordinate_pair = _provider_coordinate_pair(provider_place)

        replaced_field_paths = set(identity_fields)
        if candidate_kind == "dining":
            replaced_field_paths.add("external_quality_match")
        if coordinate_pair is not None:
            replaced_field_paths.update(("latitude", "longitude"))
        replaced_fact_ids = {
            str(fact.get("fact_assertion_id") or "")
            for fact in facts
            if isinstance(fact, dict)
            and (fact.get("entity_ref") or {}).get("entity_id") == candidate_id
            and fact.get("field_path") in replaced_field_paths
        }
        facts = [
            fact
            for fact in facts
            if not (
                isinstance(fact, dict)
                and (fact.get("entity_ref") or {}).get("entity_id") == candidate_id
                and fact.get("field_path") in replaced_field_paths
            )
        ]
        provenance = [
            item
            for item in provenance
            if not (
                isinstance(item, dict)
                and (item.get("entity_ref") or {}).get("entity_id") == candidate_id
                and item.get("field_path") in replaced_field_paths
            )
        ]
        new_fact_ids: list[str] = []
        for field_path in identity_fields:
            fact_id = _provider_fact_id(audit_id, candidate_id, field_path)
            new_fact_ids.append(fact_id)
            provider_field_path = (
                "name"
                if field_path == _PLACE_NAME_FIELD[candidate_kind]
                else field_path
            )
            source_locator_prefix = (
                "results"
                if cache_provenance is not None
                else "sanitized_result.results"
            )
            entity_ref = EntityRef(
                entity_type=_PLACE_ENTITY_TYPE[candidate_kind],
                entity_id=candidate_id,
            )
            facts.append(
                FactAssertion(
                    fact_assertion_id=fact_id,
                    entity_ref=entity_ref,
                    field_path=field_path,
                    asserted_value=provider_values[field_path],
                    criticality="decision_critical",
                    status="verified",
                    observed_at=observed_at,
                    source_links=[
                        FactSourceLink(
                            source_record_id=source_id,
                            relation="supports",
                            source_locator=(
                                f"{source_locator_prefix}[{result_index}].{provider_field_path}"
                            ),
                        )
                    ],
                ).model_dump(mode="python")
            )
            provenance.append(
                FieldProvenance(
                    origin="external_fact",
                    entity_ref=entity_ref,
                    field_path=field_path,
                    reference_ids=[fact_id],
                ).model_dump(mode="python")
            )
        if coordinate_pair is not None:
            # Bind the provider's point geometry as verified facts so the
            # deterministic map projection resolves a real pin for this entity.
            # These share the same successful place source snapshot as identity,
            # so they clear the delivery source/weather gate with no extra source.
            coordinate_entity_ref = EntityRef(
                entity_type=_PLACE_ENTITY_TYPE[candidate_kind],
                entity_id=candidate_id,
            )
            coordinate_source_prefix = (
                "results"
                if cache_provenance is not None
                else "sanitized_result.results"
            )
            for coordinate_field, coordinate_value in (
                ("latitude", coordinate_pair[0]),
                ("longitude", coordinate_pair[1]),
            ):
                coordinate_fact_id = _provider_fact_id(
                    audit_id, candidate_id, coordinate_field
                )
                new_fact_ids.append(coordinate_fact_id)
                facts.append(
                    FactAssertion(
                        fact_assertion_id=coordinate_fact_id,
                        entity_ref=coordinate_entity_ref,
                        field_path=coordinate_field,
                        asserted_value=coordinate_value,
                        criticality="auxiliary",
                        status="verified",
                        observed_at=observed_at,
                        source_links=[
                            FactSourceLink(
                                source_record_id=source_id,
                                relation="supports",
                                source_locator=(
                                    f"{coordinate_source_prefix}[{result_index}].{coordinate_field}"
                                ),
                            )
                        ],
                    ).model_dump(mode="python")
                )
                provenance.append(
                    FieldProvenance(
                        origin="external_fact",
                        entity_ref=coordinate_entity_ref,
                        field_path=coordinate_field,
                        reference_ids=[coordinate_fact_id],
                    ).model_dump(mode="python")
                )
        if candidate_kind == "dining":
            quality_envelopes = quality_sources.get(place_id, [])
            quality_source_links: list[FactSourceLink] = []
            quality_audit_ids: list[str] = []
            for quality_envelope in quality_envelopes:
                quality_audit_id = str(quality_envelope.get("audit_id") or "").strip()
                if not quality_audit_id:
                    continue
                quality_audit_ids.append(quality_audit_id)
                quality_source_id = _provider_entity_source_id(
                    quality_audit_id,
                    place_id,
                )
                quality_observed_at = _source_retrieved_at(
                    quality_envelope,
                    generated_at,
                )
                quality_snapshot = dict(quality_envelope)
                quality_provider_name = str(
                    quality_envelope.get("server_name")
                    or quality_envelope.get("tool_name")
                    or "external_review_search"
                )
                quality_result = quality_envelope.get("sanitized_result") or {}
                bound_sources[quality_source_id] = {
                    "source_record_id": quality_source_id,
                    "source_kind": "external_tool",
                    "title": (
                        f"{quality_provider_name} branch review match: "
                        f"{provider_place['name']}"
                    ),
                    "provider_name": quality_provider_name,
                    "public_excerpt": json.dumps(
                        quality_result,
                        ensure_ascii=False,
                        default=str,
                    )[:900],
                    "retrieved_at": quality_observed_at,
                    "observed_at": quality_observed_at,
                    "content_hash": _canonical_snapshot_hash(quality_snapshot),
                    "snapshot": quality_snapshot,
                    "lifecycle_status": "active",
                }
                quality_source_links.append(
                    FactSourceLink(
                        source_record_id=quality_source_id,
                        relation="supports",
                        source_locator="sanitized_result",
                    )
                )
            if quality_source_links:
                quality_fact_id = _provider_fact_id(
                    "+".join(quality_audit_ids),
                    candidate_id,
                    "external_quality_match",
                )
                facts.append(
                    FactAssertion(
                        fact_assertion_id=quality_fact_id,
                        entity_ref=EntityRef(
                            entity_type=EntityType.DINING_STOP,
                            entity_id=candidate_id,
                        ),
                        field_path="external_quality_match",
                        asserted_value=True,
                        criticality="decision_critical",
                        status="verified",
                        observed_at=observed_at,
                        source_links=quality_source_links,
                    ).model_dump(mode="python")
                )
                provenance.append(
                    FieldProvenance(
                        origin="external_fact",
                        entity_ref=EntityRef(
                            entity_type=EntityType.DINING_STOP,
                            entity_id=candidate_id,
                        ),
                        field_path="external_quality_match",
                        reference_ids=[quality_fact_id],
                    ).model_dump(mode="python")
                )
                new_fact_ids.append(quality_fact_id)
        replacement_id = new_fact_ids[0]
        evaluations = candidate.get("constraint_evaluations")
        if isinstance(evaluations, list):
            for evaluation in evaluations:
                if not isinstance(evaluation, dict):
                    continue
                references = evaluation.get("fact_assertion_ids")
                if isinstance(references, list) and replaced_fact_ids.intersection(
                    str(reference) for reference in references
                ):
                    evaluation["fact_assertion_ids"] = list(
                        dict.fromkeys(
                            replacement_id
                            if str(reference) in replaced_fact_ids
                            else reference
                            for reference in references
                        )
                    )
        retained_candidate_facts = [
            fact
            for fact in facts
            if isinstance(fact, dict)
            and (fact.get("entity_ref") or {}).get("entity_id") == candidate_id
            and fact.get("fact_assertion_id") not in new_fact_ids
        ]
        candidate["fact_assertion_ids"] = [
            *[
                str(fact["fact_assertion_id"])
                for fact in retained_candidate_facts
                if fact.get("fact_assertion_id")
            ],
            *new_fact_ids,
        ]
        candidate["source_record_ids"] = list(
            dict.fromkeys([*(candidate.get("source_record_ids") or []), source_id])
        )
        candidate["field_paths"] = list(
            dict.fromkeys(
                [
                    *[
                        str(fact["field_path"])
                        for fact in retained_candidate_facts
                        if fact.get("field_path")
                    ],
                    *identity_fields,
                ]
            )
        )

    if not bound_sources:
        return {}
    bound_audit_ids = {
        str(source.get("tool_audit_id") or source["snapshot"].get("audit_id") or "")
        for source in bound_sources.values()
    }
    sources = [
        source
        for source in sources
        if not (
            isinstance(source, dict)
            and (
                source.get("tool_audit_id") in bound_audit_ids
                or (
                    isinstance(source.get("snapshot"), dict)
                    and source["snapshot"].get("audit_id") in bound_audit_ids
                )
            )
        )
    ]
    if contradicted_places:
        facts = _without_contradicted_place_support(facts, contradicted_places)
    payload["source_records"] = [*sources, *bound_sources.values()]
    payload["fact_assertions"] = facts
    payload["field_provenance"] = provenance
    return bound_sources


def _without_contradicted_place_support(
    facts: list[Any],
    contradicted_places: Mapping[str, str],
) -> list[Any]:
    """Strip compiled support the Provider's own record argues against.

    A candidate whose kind the Provider record contradicts may not lean on that
    record for anything.  Dropping the link rather than the whole fact keeps the
    model's other citations intact; a fact left with no support at all goes, and
    the identity fields among those are what admission then names as missing.
    """

    kept: list[Any] = []
    dropped: dict[str, str] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            kept.append(fact)
            continue
        entity_id = str((fact.get("entity_ref") or {}).get("entity_id") or "")
        place_id = contradicted_places.get(entity_id)
        links = fact.get("source_links")
        if place_id is None or not isinstance(links, list):
            kept.append(fact)
            continue
        kept_links = [
            link
            for link in links
            if not (
                isinstance(link, Mapping)
                and compiled_tool_source_id_is_about(
                    str(link.get("source_record_id") or ""), place_id
                )
            )
        ]
        if len(kept_links) == len(links):
            kept.append(fact)
            continue
        dropped[str(fact.get("fact_assertion_id") or "")] = place_id
        if kept_links:
            kept.append({**fact, "source_links": kept_links})
    if dropped:
        logger.warning(
            "Research Packet compiled place support dropped (provider contradicts "
            "candidate kind) | contradicted=%s facts=%s",
            dict(contradicted_places),
            dropped,
        )
    return kept


def _bind_authoritative_candidate_constraints(
    payload: dict[str, Any],
    expected_active_constraint_ids: Sequence[str] | None,
) -> None:
    """Discard model-created constraint identity before typed validation.

    Constraint identity is controlled state, not a reality fact.  Evaluations for
    IDs absent from that state cannot enter Candidate or trigger a whole-packet
    model rewrite.  Missing evaluations for real active constraints remain missing
    and are rejected by the exact coverage check after validation.
    """
    if expected_active_constraint_ids is None:
        return
    authoritative_ids = list(dict.fromkeys(expected_active_constraint_ids))
    authoritative_set = set(authoritative_ids)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate["active_constraint_ids"] = authoritative_ids
        evaluations = candidate.get("constraint_evaluations")
        candidate["constraint_evaluations"] = (
            [
                evaluation
                for evaluation in evaluations
                if isinstance(evaluation, dict)
                and evaluation.get("constraint_id") in authoritative_set
            ]
            if isinstance(evaluations, list)
            else []
        )
        # Candidate Gate binds this server-owned proof only after the packet
        # has passed authoritative metadata/evidence validation.  Never let a
        # provider or model carry a prior-run attestation through parsing.
        candidate["constraint_gate_attestation"] = None


def _bind_authoritative_packet_metadata(
    payload: dict[str, Any],
    authoritative_packet_metadata: Mapping[str, Any] | None,
) -> None:
    if authoritative_packet_metadata is None:
        return
    payload.update(authoritative_packet_metadata)
    packet_id = str(authoritative_packet_metadata["research_packet_id"])
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate["research_packet_id"] = packet_id


def _normalized_semantic_key(value: Any) -> str:
    """Normalize an already-structured semantic label for exact fallback joins."""

    return re.sub(r"[^\w]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _query_row_matches_provider_identity(
    row: Mapping[str, Any],
    *,
    provider_query: str,
    candidate_name: str,
) -> bool:
    """Join Provider identity to an LLM-authored query without new NLP rules.

    The Query Plan already contains the model's canonical aliases and query
    text.  This function only performs normalized equality/containment over
    those structured fields and the Provider's echoed query/name.  It is used
    for provenance and as the last-resort selector after the semantic model
    call is unavailable; it never parses the user's prose.
    """

    provider_key = _normalized_semantic_key(provider_query)
    name_key = _normalized_semantic_key(candidate_name)
    query_key = _normalized_semantic_key(row.get("query_text"))
    if provider_key and query_key and provider_key == query_key:
        return True
    aliases = [
        _normalized_semantic_key(alias)
        for alias in row.get("aliases") or []
        if _normalized_semantic_key(alias)
    ]
    return any(
        alias == name_key
        or (provider_key and alias in provider_key)
        or (name_key and alias in name_key)
        for alias in aliases
    )


def _bind_candidate_discovery_lineage(payload: dict[str, Any]) -> None:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return
    query_context = payload.get("query_context")
    query_rows = (
        query_context.get("query_lineage") if isinstance(query_context, Mapping) else []
    )
    query_rows = [row for row in (query_rows or []) if isinstance(row, Mapping)]
    executed_ids = {
        str(query_id)
        for query_id in payload.get("executed_query_ids") or []
        if str(query_id)
    }
    origin_by_kind = {
        "intent_primary": "intent_query",
        "structural": "structural_query",
        "evidence_enrichment": "structural_query",
        "generic_fallback": "generic_fallback",
        "targeted_repair": "targeted_repair",
    }
    source_by_id = {
        str(source.get("source_record_id") or ""): source
        for source in payload.get("source_records") or []
        if isinstance(source, Mapping)
    }
    research_round = 0
    if isinstance(query_context, Mapping):
        try:
            research_round = max(int(query_context.get("research_round") or 0), 0)
        except (TypeError, ValueError):
            research_round = 0
    records = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        candidate_kind = str(candidate.get("candidate_kind") or "")
        domain_values = (
            {"visit"}
            if candidate_kind == "visit"
            else {"dining"}
            if candidate_kind == "dining"
            else {"lodging"}
            if candidate_kind == "lodging"
            else {
                "long_distance_transport"
                if candidate.get("transport_class") == "long_distance"
                else "local_transport"
            }
        )
        domain_rows = [
            row
            for row in query_rows
            if str(row.get("query_id") or "") in executed_ids
            and str(row.get("domain") or "") in domain_values
        ]
        provider_queries: list[str] = []
        for source_id in candidate.get("source_record_ids") or []:
            source = source_by_id.get(str(source_id))
            snapshot = source.get("snapshot") if isinstance(source, Mapping) else None
            query = snapshot.get("query") if isinstance(snapshot, Mapping) else None
            if isinstance(query, str) and query.strip():
                provider_queries.append(query.strip())
        candidate_name = str(
            candidate.get("name")
            or candidate.get("property_name")
            or candidate.get("route_id")
            or ""
        )
        traced_rows = [
            row
            for row in domain_rows
            if any(
                _query_row_matches_provider_identity(
                    row,
                    provider_query=provider_query,
                    candidate_name=candidate_name,
                )
                for provider_query in provider_queries
            )
        ]
        # Older or non-place providers may not echo a query in their snapshot.
        # Preserve their prior domain-level lineage rather than inventing an
        # association.  For place providers, an exact trace is authoritative:
        # a generic museum result must not inherit every hard place intent.  A
        # model-nominated place that does not exactly join an intent alias keeps
        # only structural/fallback lineage, never a fabricated hard-intent join.
        if provider_queries and candidate_kind in {"visit", "dining"}:
            relevant = traced_rows or [
                row
                for row in domain_rows
                if str(row.get("query_kind") or "")
                not in {"intent_primary", "targeted_repair"}
            ]
        else:
            relevant = domain_rows
        query_ids = list(dict.fromkeys(str(row["query_id"]) for row in relevant))
        intent_ids = list(
            dict.fromkeys(
                str(intent_id)
                for row in relevant
                for intent_id in row.get("intent_ids") or []
                if str(intent_id)
            )
        )
        origins = list(
            dict.fromkeys(
                origin_by_kind.get(str(row.get("query_kind") or ""), "structural_query")
                for row in relevant
            )
        )
        if not origins:
            raise ResearchPacketOutputError(
                "research candidate has no server-owned executed query lineage"
            )
        provider_audit_ids = list(
            dict.fromkeys(
                str(source.get("tool_audit_id") or "")
                for source_id in candidate.get("source_record_ids") or []
                if (source := source_by_id.get(str(source_id))) is not None
                and source.get("tool_audit_id")
            )
        )
        records.append(
            {
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "generation_id": str(payload.get("generation_id") or ""),
                "query_ids": query_ids,
                "intent_ids": intent_ids,
                "origins": origins,
                "provider_audit_ids": provider_audit_ids,
                "discovered_at_rounds": [research_round],
            }
        )
    payload["candidate_discovery_records"] = records


def _bind_external_tool_source_registry(
    schema: dict[str, Any],
    authoritative_external_tool_source_ids: Sequence[str],
) -> None:
    """Pin ``external_tool`` source ids to the registry the parser will accept.

    ``SourceRecord`` splits into two branches keyed by its required
    ``source_kind`` discriminator: a tool branch whose ``source_record_id`` is
    the exact registry key set, and a retrieval branch (``external_web`` /
    ``rag_chunk``) whose id stays a free string.  An empty registry removes the
    tool branch instead of emitting an unsatisfiable empty enum.

    The unsplit definition is dropped: it is the only place ``source_kind``
    could still be left unbound, and the repair schema is serialized into the
    prompt, so an unreachable duplicate only spends token budget.
    """
    definitions = schema["$defs"]
    base = definitions.pop("SourceRecord")
    definitions["RetrievedSourceRecord"] = {
        **base,
        "title": "Retrieved Source Record",
        "properties": {
            **base["properties"],
            "source_kind": {
                "enum": ["external_web", "rag_chunk"],
                "title": "Source Kind",
                "type": "string",
            },
        },
    }
    registry_ids = list(dict.fromkeys(authoritative_external_tool_source_ids))
    if not registry_ids:
        schema["properties"]["source_records"]["items"] = {
            "$ref": "#/$defs/RetrievedSourceRecord"
        }
        return
    definitions["ExternalToolSourceRecord"] = {
        **base,
        "title": "External Tool Source Record",
        "properties": {
            **base["properties"],
            "source_kind": {
                "const": "external_tool",
                "title": "Source Kind",
                "type": "string",
            },
            "source_record_id": {
                "enum": registry_ids,
                "title": "Source Record Id",
                "type": "string",
            },
        },
    }
    schema["properties"]["source_records"]["items"] = {
        "oneOf": [
            {"$ref": "#/$defs/ExternalToolSourceRecord"},
            {"$ref": "#/$defs/RetrievedSourceRecord"},
        ]
    }


def _research_packet_response_schema(
    expected_worker: ResearchWorkerKind,
    required_transport_classes: Sequence[str] | None = None,
    expected_active_constraint_ids: Sequence[str] | None = None,
    eligible_place_options: Mapping[
        str,
        Sequence[Mapping[str, str]],
    ]
    | None = None,
    required_candidate_kinds: Sequence[str] | None = None,
    required_route_scopes: Sequence[ProviderEvidenceScope] = (),
    authoritative_external_tool_source_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Require discriminators that Pydantic defaults omit from ``required``.

    A discriminated union must see ``candidate_kind`` before it can select the
    concrete candidate model, so a structured-output provider cannot rely on the
    concrete model's default value for that field.
    """
    schema = ResearchPacket.model_json_schema()
    definitions = schema.get("$defs", {})
    for model_name in (
        "VisitCandidate",
        "DiningCandidate",
        "LodgingCandidate",
        "TransportCandidate",
    ):
        candidate_schema = definitions.get(model_name)
        if not isinstance(candidate_schema, dict):
            continue
        required = candidate_schema.setdefault("required", [])
        if "candidate_kind" not in required:
            required.append("candidate_kind")
    allowed = _WORKER_CANDIDATE_MODELS[expected_worker]
    if required_candidate_kinds:
        required_kinds = set(required_candidate_kinds)
        allowed = tuple(
            (candidate_kind, model_name)
            for candidate_kind, model_name in allowed
            if candidate_kind in required_kinds
        )
    if eligible_place_options is not None and _has_eligible_place_selection(
        eligible_place_options
    ):
        allowed = tuple(
            (candidate_kind, model_name)
            for candidate_kind, model_name in allowed
            if eligible_place_options.get(candidate_kind)
        )
        schema["properties"]["candidates"]["minItems"] = 1
        for candidate_kind, model_name in allowed:
            definitions[model_name]["properties"]["place_id"] = {
                "enum": [
                    option["place_id"]
                    for option in eligible_place_options[candidate_kind]
                ],
                "title": "Place Id",
                "type": "string",
            }
    candidate_items = schema["properties"]["candidates"]["items"]
    scoped_transport = expected_worker == "transport_researcher" and bool(
        required_transport_classes
    )
    destination_repair = expected_worker == "destination_researcher"
    candidate_limit = packet_candidate_limit(
        expected_worker,
        required_transport_classes=required_transport_classes,
        required_route_scopes=required_route_scopes,
    )
    schema["properties"]["candidates"]["maxItems"] = candidate_limit
    if scoped_transport:
        schema["properties"]["fact_assertions"]["maxItems"] = 12
        schema["properties"]["field_provenance"]["maxItems"] = 12
    elif destination_repair:
        identity_fact_limit = (
            candidate_limit * _DESTINATION_IDENTITY_FACTS_PER_CANDIDATE
        )
        schema["properties"]["fact_assertions"]["maxItems"] = identity_fact_limit
        schema["properties"]["field_provenance"]["maxItems"] = identity_fact_limit
        schema["properties"]["source_records"]["maxItems"] = candidate_limit
    else:
        # Hard-bound the general worker packet (accommodation) so structured
        # output cannot exceed the configured completion ceiling: a prompt asking
        # for "concise" is only advisory, but a JSON-schema maxItems the provider
        # enforces deterministically caps how many sources/facts it can emit.
        schema["properties"]["fact_assertions"]["maxItems"] = candidate_limit * 6
        schema["properties"]["field_provenance"]["maxItems"] = candidate_limit * 6
        schema["properties"]["source_records"]["maxItems"] = candidate_limit * 3
    candidate_items["oneOf"] = [
        {"$ref": f"#/$defs/{model_name}"} for _, model_name in allowed
    ]
    candidate_items["discriminator"]["mapping"] = {
        candidate_kind: f"#/$defs/{model_name}"
        for candidate_kind, model_name in allowed
    }
    schema["properties"]["worker_kind"] = {
        "const": expected_worker,
        "title": "Worker Kind",
        "type": "string",
    }
    if required_transport_classes:
        transport_schema = definitions.get("TransportCandidate")
        if isinstance(transport_schema, dict):
            transport_schema["properties"]["transport_class"] = {
                "enum": list(required_transport_classes),
                "title": "Transport Class",
                "type": "string",
            }
    if expected_active_constraint_ids is not None:
        constraint_ids = list(dict.fromkeys(expected_active_constraint_ids))
        constraint_count = len(constraint_ids)
        for _, model_name in allowed:
            candidate_schema = definitions.get(model_name)
            if not isinstance(candidate_schema, dict):
                continue
            candidate_schema["properties"]["active_constraint_ids"] = {
                "items": (
                    {"enum": constraint_ids, "type": "string"}
                    if constraint_ids
                    else {"type": "string"}
                ),
                "minItems": constraint_count,
                "maxItems": constraint_count,
                "type": "array",
            }
            evaluation_schema = candidate_schema["properties"]["constraint_evaluations"]
            evaluation_schema["minItems"] = constraint_count
            evaluation_schema["maxItems"] = constraint_count
        evaluation_model = definitions.get("CandidateConstraintEvaluation")
        if isinstance(evaluation_model, dict):
            evaluation_model["properties"]["constraint_id"] = (
                {"enum": constraint_ids, "type": "string"}
                if constraint_ids
                else {"type": "string"}
            )
    # A packet whose evidence is only Provider failure carries no model-authored
    # source at all: the parser compiles the failure closure from the Tool
    # Gateway transcript, so the array floor belongs to the parser, not here.
    schema["properties"]["source_records"]["minItems"] = 0
    if authoritative_external_tool_source_ids is not None:
        _bind_external_tool_source_registry(
            schema,
            authoritative_external_tool_source_ids,
        )
    _drop_fields_the_model_must_not_author(schema)
    return as_strict_schema(schema)


_DEFINITION_REF_PREFIX = "#/$defs/"


def _referenced_definition_names(node: Any) -> set[str]:
    """Collect every ``$defs`` target a schema fragment can actually reach."""
    names: set[str] = set()
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_DEFINITION_REF_PREFIX):
            names.add(ref[len(_DEFINITION_REF_PREFIX) :])
        for key, value in node.items():
            if key == "$defs":
                continue
            names |= _referenced_definition_names(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            names |= _referenced_definition_names(value)
    return names


def _closed_definitions(
    body: Mapping[str, Any],
    definitions: Mapping[str, Any],
) -> dict[str, Any]:
    """Return exactly the definitions ``body`` can reference, transitively.

    ``ResearchPacket.model_json_schema()`` carries the whole packet graph.  A
    bounded selection contract that ships all of it is not merely verbose: the
    configured provider does not enforce this schema at all — direct DeepSeek
    rejects ``response_format=json_schema``, so ``models/router.py`` downgrades
    it to ``json_object`` and restates the schema as prose.  The schema *text*
    is therefore the entire contract the model reads, and the unreachable
    definitions state, in that contract, that a lodging check-in date, a
    transport class or a SourceRecord content hash belong to this answer.  The
    typed-domain check in ``_repair_from_provider_selection`` then fails the
    whole worker closed on precisely those fields, taking every grounded
    Provider option for both destination domains with it.  A contract must name
    what it accepts and nothing else.
    """

    pending = _referenced_definition_names(body)
    closed: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in closed:
            continue
        definition = definitions.get(name)
        if not isinstance(definition, Mapping):
            raise ResearchPacketOutputError(
                f"provider selection contract references an undefined model: {name}"
            )
        closed[name] = definition
        pending |= _referenced_definition_names(definition)
    return {name: closed[name] for name in sorted(closed)}


def _provider_selection_response_schema(
    expected_worker: ResearchWorkerKind,
    eligible_place_options: Mapping[str, Sequence[Mapping[str, str]]],
) -> dict[str, Any]:
    """Build a small planning-only contract over the exact eligible place ids."""
    packet_schema = ResearchPacket.model_json_schema()
    definitions = packet_schema["$defs"]
    allowed = tuple(
        (candidate_kind, model_name)
        for candidate_kind, model_name in _WORKER_CANDIDATE_MODELS[expected_worker]
        if eligible_place_options.get(candidate_kind)
    )
    for candidate_kind, model_name in allowed:
        candidate_schema = definitions[model_name]
        properties = candidate_schema["properties"]
        for field_name in _PROVIDER_SELECTION_SERVER_FIELDS:
            properties.pop(field_name, None)
        properties["candidate_kind"] = {
            "const": candidate_kind,
            "title": "Candidate Kind",
            "type": "string",
        }
        properties["place_id"] = {
            "enum": [
                option["place_id"] for option in eligible_place_options[candidate_kind]
            ],
            "title": "Place Id",
            "type": "string",
        }
        candidate_schema["required"] = [
            field_name
            for field_name in candidate_schema.get("required", [])
            if field_name not in _PROVIDER_SELECTION_SERVER_FIELDS
        ]
        if "candidate_kind" not in candidate_schema["required"]:
            candidate_schema["required"].append("candidate_kind")
    candidate_items = {
        "oneOf": [{"$ref": f"#/$defs/{model_name}"} for _, model_name in allowed],
        "discriminator": {
            "mapping": {
                candidate_kind: f"#/$defs/{model_name}"
                for candidate_kind, model_name in allowed
            },
            "propertyName": "candidate_kind",
        },
    }
    body = {
        "additionalProperties": False,
        "properties": {
            "selections": {
                "items": candidate_items,
                "maxItems": min(
                    packet_candidate_limit(expected_worker),
                    sum(len(options) for options in eligible_place_options.values()),
                ),
                "minItems": 1,
                "type": "array",
            }
        },
        "required": ["selections"],
        "type": "object",
    }
    selection_schema = {"$defs": _closed_definitions(body, definitions), **body}
    _drop_fields_the_model_must_not_author(selection_schema)
    # This one legalizes all the way: nothing left in it needs a free-form
    # mapping, so it can go to a strict provider as-is instead of only to a
    # lenient one.
    return as_strict_schema(selection_schema)


def _default_provider_place_selections(
    *,
    base_payload: Mapping[str, Any],
    scoped_options: Mapping[str, Sequence[Mapping[str, str]]],
    selection_limit: int | None = None,
) -> dict[str, Any]:
    """Safe selector when the bounded semantic planning call is unavailable.

    Semantic authority remains with the LLM: this path reads only its typed
    Query Plan aliases/query kinds.  It first preserves options that can be
    joined to intent-scoped queries, then fills remaining capacity in Provider
    result order.  No user-prose grammar or destination-specific vocabulary is
    interpreted here.
    """

    destinations = _controlled_destination_ids(base_payload)
    if not destinations:
        raise ResearchPacketOutputError(
            "provider default selection requires a controlled destination"
        )
    destination_id = destinations[0]
    query_context = base_payload.get("query_context")
    query_context = query_context if isinstance(query_context, Mapping) else {}
    selections: list[dict[str, Any]] = []
    query_rows = query_context.get("query_lineage")
    query_rows = [
        row
        for row in (query_rows or [])
        if isinstance(row, Mapping)
        and str(row.get("query_kind") or "") in {"intent_primary", "targeted_repair"}
        and row.get("intent_ids")
    ]
    ceiling = max(int(selection_limit or 0), 1)
    weather = {
        "exposure": "mixed",
        "rain_sensitivity": "low",
        "heat_sensitivity": "low",
        "cold_sensitivity": "low",
        "wind_sensitivity": "low",
        "requires_clear_visibility": False,
    }
    for candidate_kind, options in scoped_options.items():
        remaining = ceiling - len(selections)
        if not options or remaining <= 0:
            continue
        selected_options: list[Mapping[str, str]] = []
        for row in query_rows:
            domain = str(row.get("domain") or "")
            if domain != candidate_kind:
                continue
            match = next(
                (
                    option
                    for option in options
                    if option not in selected_options
                    and _query_row_matches_provider_identity(
                        row,
                        provider_query=str(option.get("provider_query") or ""),
                        candidate_name=str(option.get("name") or ""),
                    )
                ),
                None,
            )
            if match is not None:
                selected_options.append(match)
            if len(selected_options) >= remaining:
                break
        for option in options:
            if len(selected_options) >= remaining:
                break
            if option not in selected_options:
                selected_options.append(option)
        for option in selected_options:
            semantic_joined = any(
                _query_row_matches_provider_identity(
                    row,
                    provider_query=str(option.get("provider_query") or ""),
                    candidate_name=str(option.get("name") or ""),
                )
                for row in query_rows
                if str(row.get("domain") or "") == candidate_kind
            )
            common = {
                "candidate_kind": candidate_kind,
                "place_id": option["place_id"],
                "destination_id": destination_id,
                "weather_sensitivity": weather,
                "selection_reasons": [
                    (
                        "命中 LLM 结构化意图查询并完成 Provider 实体闭包"
                        if semantic_joined
                        else "Provider 实体身份闭包完整"
                    ),
                    "位于当前受控目的地范围",
                ],
                "tradeoff": "动态营业、库存与价格信息仍需临行确认",
            }
            if candidate_kind == "visit":
                common.update(
                    {
                        "visit_type": "attraction",
                        "recommended_duration_minutes": 90,
                        "highlights": [str(option.get("name") or "实体景点")],
                    }
                )
            elif candidate_kind == "dining":
                common.update(
                    {
                        "meal_types": ["lunch", "dinner"],
                        "cuisine_types": ["local"],
                        "recommended_dishes": ["按当日菜单与过敏要求现场选择"],
                        "opening_window": "营业时间待确认",
                        "availability_status": "needs_confirmation",
                    }
                )
            elif candidate_kind == "lodging":
                # The stay interval comes from the controlled identity, which every
                # worker's ``query_context`` already carries.  Whole-trip bounds are
                # later clamped per destination by the composition owner.
                identity = query_context.get("controlled_trip_identity")
                identity = identity if isinstance(identity, Mapping) else {}
                check_in = str(
                    query_context.get("check_in_date")
                    or identity.get("start_date")
                    or ""
                )[:10]
                check_out = str(
                    query_context.get("check_out_date")
                    or identity.get("end_date")
                    or ""
                )[:10]
                try:
                    nights = (
                        datetime.fromisoformat(check_out).date()
                        - datetime.fromisoformat(check_in).date()
                    ).days
                except ValueError:
                    nights = 0
                if not check_in or not check_out or nights < 1:
                    raise ResearchPacketOutputError(
                        "provider default lodging selection requires an exact stay interval"
                    )
                common.update(
                    {
                        "check_in_date": check_in,
                        "check_out_date": check_out,
                        "nights": nights,
                        "availability_status": "needs_confirmation",
                    }
                )
            selections.append(common)
    if not selections:
        raise ResearchPacketOutputError(
            "provider default selection has no eligible option"
        )
    return {"selections": selections}


def _validate_provider_place_selections(
    selection_payload: Any,
    *,
    expected_worker: ResearchWorkerKind,
    scoped_options: Mapping[str, Sequence[Mapping[str, str]]],
    selection_schema: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate the model-owned part before Provider facts are materialized."""

    selections = (
        selection_payload.get("selections")
        if isinstance(selection_payload, dict)
        else None
    )
    if not isinstance(selections, list) or not selections:
        raise ResearchPacketOutputError(
            "provider place selection requires at least one explicit choice"
        )
    selection_limit = int(selection_schema["properties"]["selections"]["maxItems"])
    if len(selections) > selection_limit:
        raise ResearchPacketOutputError(
            "provider place selection exceeds the bounded selection limit"
        )
    model_name_by_kind = dict(_WORKER_CANDIDATE_MODELS[expected_worker])
    allowed_properties = {
        candidate_kind: set(
            selection_schema["$defs"][model_name_by_kind[candidate_kind]]["properties"]
        )
        for candidate_kind in scoped_options
        if candidate_kind in model_name_by_kind
    }
    allowed_place_ids = {
        candidate_kind: {str(option.get("place_id") or "") for option in kind_options}
        for candidate_kind, kind_options in scoped_options.items()
    }
    validated: list[dict[str, Any]] = []
    selected_pairs: set[tuple[str, str]] = set()
    for selection in selections:
        if not isinstance(selection, dict):
            raise ResearchPacketOutputError(
                "provider place selection contains a non-object choice"
            )
        candidate_kind = str(selection.get("candidate_kind") or "")
        place_id = str(selection.get("place_id") or "")
        if (
            candidate_kind not in allowed_properties
            or not set(selection) <= allowed_properties[candidate_kind]
        ):
            raise ResearchPacketOutputError(
                "provider place selection contains fields outside its typed domain"
            )
        if place_id not in allowed_place_ids.get(candidate_kind, set()):
            raise ResearchPacketOutputError(
                "provider place selection contains an unavailable place"
            )
        pair = (candidate_kind, place_id)
        if pair in selected_pairs:
            raise ResearchPacketOutputError(
                "provider place selection contains a duplicate place"
            )
        try:
            WeatherSensitivity.model_validate(selection.get("weather_sensitivity"))
        except ValidationError as exc:
            raise ResearchPacketOutputError(
                "provider place selection contains invalid weather sensitivity"
            ) from exc
        selected_pairs.add(pair)
        validated.append(selection)
    return validated


def _provider_place_selections_or_default(
    selection_payload: Any,
    *,
    model_authored: bool,
    base_payload: Mapping[str, Any],
    expected_worker: ResearchWorkerKind,
    scoped_options: Mapping[str, Sequence[Mapping[str, str]]],
    selection_schema: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Use a semantic choice when valid, otherwise preserve verified supply."""

    try:
        return _validate_provider_place_selections(
            selection_payload,
            expected_worker=expected_worker,
            scoped_options=scoped_options,
            selection_schema=selection_schema,
        )
    except ResearchPacketOutputError:
        if not model_authored:
            raise
        logger.warning(
            "provider place planning selection violated its typed contract; "
            "applying deterministic policy",
            exc_info=True,
        )
    fallback_payload = _default_provider_place_selections(
        base_payload=base_payload,
        scoped_options=scoped_options,
        selection_limit=int(selection_schema["properties"]["selections"]["maxItems"]),
    )
    return _validate_provider_place_selections(
        fallback_payload,
        expected_worker=expected_worker,
        scoped_options=scoped_options,
        selection_schema=selection_schema,
    )


async def _repair_from_provider_selection(
    *,
    base_payload: Mapping[str, Any],
    expected_worker: ResearchWorkerKind,
    expected_run_id: str,
    llm: Any,
    context_messages: Sequence[Mapping[str, Any]],
    evidence_messages: Sequence[Mapping[str, Any]],
    eligible_place_options: Mapping[str, Sequence[Mapping[str, str]]],
    excluded_candidate_ids: Sequence[str] | None,
    require_current_candidate: bool,
    expected_active_constraint_ids: Sequence[str] | None,
    expected_active_constraints: Sequence[Mapping[str, Any]] | None,
    authoritative_packet_metadata: Mapping[str, Any] | None,
    required_candidate_kinds: Sequence[str] | None,
) -> ResearchPacket:
    """Ask bounded domain-scoped planning choices, then bind reality server-side."""
    # One mixed Visit/Dining response with a shared three-item limit can satisfy
    # the schema while omitting an entire required product domain.  Keep the
    # planning judgment with the model, but ask for one bounded typed selection
    # per required domain.  No Provider result is auto-ranked or auto-admitted.
    # Promised domains are asked first, then every other domain the server
    # compiled options for.  Batching only the promised domains left an eligible
    # domain unasked even though its options were already verified server-side.
    required_kinds = list(dict.fromkeys(required_candidate_kinds or ()))
    batch_kinds = list(
        dict.fromkeys(
            candidate_kind
            for candidate_kind in (
                *required_kinds,
                *(kind for kind, _ in _WORKER_CANDIDATE_MODELS[expected_worker]),
            )
            if eligible_place_options.get(candidate_kind)
        )
    )
    selection_option_batches = (
        [
            {candidate_kind: eligible_place_options[candidate_kind]}
            for candidate_kind in batch_kinds
        ]
        if required_kinds
        else [dict(eligible_place_options)]
    )
    candidates: list[dict[str, Any]] = []
    for scoped_options in selection_option_batches:
        selection_schema = _provider_selection_response_schema(
            expected_worker,
            scoped_options,
        )
        # The sentence and the schema state one number, so it is read off the
        # schema rather than written twice.  It used to read "1 到 3 个" as a
        # literal, which is how a widened limit stays invisible to the model that
        # has to act on it.
        selection_ceiling = selection_schema["properties"]["selections"]["maxItems"]
        selection_messages = [
            dict(message)
            for message in context_messages
            if message.get("role") == "system"
        ]
        selection_messages.append(
            {
                "role": "user",
                "content": (
                    "你现在只做一次强类型候选选择，不复制 SourceRecord、FactAssertion 或 provenance。"
                    "从本次 eligible_place_options 列出的领域中，按当前任务、用户偏好、天气和规划质量"
                    f"选择 1 到 {selection_ceiling} 个真实实体；"
                    "place_id 与 candidate_kind 必须逐字来自对应 option，"
                    "禁止按 Provider 排名自动选择。"
                    "列表里的每个 option 都由服务器从本轮工具回执编译，实体身份已经核验，"
                    "可以直接选用，不需要你再自行判断其真实性。"
                    "餐饮不要求另有外部评价：列表里既有评价核得上的门店，也有核不上的，"
                    "两者都可以选，不要因为你想不起某家店的口碑就跳过它；"
                    "是否在报告里标注「外部评价已核验」由服务器按证据判定，不由你判定。"
                    "禁止选择列表之外的任何对象，也禁止自撰实体；某个领域本轮没有可选项时就不为它作选择，"
                    "这不算本轮失败。"
                    "填写 schema 要求的领域规划字段；未知的动态价格使用 null，"
                    "库存或预约使用 needs_confirmation，不要生成精确但无来源的现实数值。"
                    "只输出一个可由 json.loads 解析的 JSON 对象。"
                    f"worker_kind={expected_worker}，run_id={expected_run_id}，"
                    f"query_context={json.dumps(base_payload.get('query_context') or {}, ensure_ascii=False, separators=(',', ':'))}，"
                    f"eligible_place_options={json.dumps(scoped_options, ensure_ascii=False, separators=(',', ':'))}。"
                    f"必须满足此 JSON Schema：{json.dumps(selection_schema, ensure_ascii=False, separators=(',', ':'))}"
                ),
            }
        )
        model_authored = True
        try:
            selected_raw = await _bounded_packet_model_call(
                llm,
                selection_messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "provider_place_selection",
                        "strict": True,
                        "schema": selection_schema,
                    },
                },
                temperature=0,
                max_output_tokens=_PROVIDER_SELECTION_MAX_OUTPUT_TOKENS,
            )
            selection_payload = json.loads(selected_raw)
        except (
            ResearchPacketOutputError,
            TypeError,
            json.JSONDecodeError,
            # A closed model window is the *most* important reason to fall back
            # here, not an exception to it.  Every Provider place in
            # ``scoped_options`` is already in hand — paid for, audited,
            # server-normalized — and the only thing this model call adds is which
            # of them to prefer.  Letting the window kill the round discards the
            # evidence and the itinerary then ships authored stops instead, which
            # is strictly worse than a deterministic preference: downstream cannot
            # tell "no evidence" from "evidence thrown away".  The policy below is
            # pure, so it belongs to no window at all.
            ModelWindowClosed,
        ):
            logger.warning(
                "provider place planning selection unavailable; applying deterministic policy",
                exc_info=True,
            )
            selection_payload = _default_provider_place_selections(
                base_payload=base_payload,
                scoped_options=scoped_options,
                selection_limit=selection_ceiling,
            )
            model_authored = False
        selections = _provider_place_selections_or_default(
            selection_payload,
            model_authored=model_authored,
            base_payload=base_payload,
            expected_worker=expected_worker,
            scoped_options=scoped_options,
            selection_schema=selection_schema,
        )
        for selection in selections:
            candidate_kind = str(selection.get("candidate_kind") or "")
            candidates.append(
                {
                    **selection,
                    "candidate_id": _provider_candidate_id(
                        candidate_kind,
                        str(selection["place_id"]),
                    ),
                    "research_packet_id": base_payload.get("research_packet_id"),
                    "fact_assertion_ids": ["pending_provider_fact"],
                    "source_record_ids": ["pending_provider_source"],
                    "field_paths": ["place_id"],
                    "active_constraint_ids": list(expected_active_constraint_ids or ()),
                    "constraint_evaluations": [
                        {
                            "constraint_id": str(constraint["constraint_id"]),
                            "status": "unknown",
                            "fact_assertion_ids": [],
                            "reason_code": "provider_facility_evidence_missing",
                        }
                        for constraint in expected_active_constraints or ()
                    ],
                    "freshness_status": "current",
                }
            )
    # Each domain is selected in its own bounded batch, so a multi-domain worker
    # (destination = visit + dining) can aggregate past the worker's *total*
    # candidate limit.  The re-parse below enforces that total and would
    # otherwise reject the whole repair with "exceeds candidate limit",
    # collapsing an otherwise-salvageable run to a delivery-integrity terminal.
    # Deterministically cap the aggregate here, round-robin across kinds so no
    # domain is dropped wholesale.
    #
    # The packet's total is *derived*: one authoring act's ceiling times the
    # number of acts.  It is not a second constant — it used to be, and the two
    # disagreed: a 3-visit + 2-dining selection was round-robined down to 2 + 2,
    # dropping a visit the server had already grounded.  This branch composes the
    # packet server-side and pins ``fact_assertions``/``field_provenance`` to
    # ``[]`` below, so nothing here is bounded by a completion ceiling.
    per_batch_limit = packet_candidate_limit(expected_worker)
    candidate_limit = per_batch_limit * len(selection_option_batches)
    if len(candidates) > candidate_limit:
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            by_kind.setdefault(str(candidate.get("candidate_kind") or ""), []).append(
                candidate
            )
        balanced: list[dict[str, Any]] = []
        while len(balanced) < candidate_limit and any(by_kind.values()):
            for kind in list(by_kind):
                if by_kind[kind] and len(balanced) < candidate_limit:
                    balanced.append(by_kind[kind].pop(0))
        candidates = balanced
    repaired_payload: dict[str, Any] = {
        **base_payload,
        "candidates": candidates,
        "fact_assertions": [],
        "field_provenance": [],
    }
    return parse_research_packet_output(
        json.dumps(repaired_payload, ensure_ascii=False, default=str),
        expected_worker=expected_worker,
        expected_run_id=expected_run_id,
        excluded_candidate_ids=excluded_candidate_ids,
        require_current_candidate=require_current_candidate,
        expected_active_constraint_ids=expected_active_constraint_ids,
        context_messages=context_messages,
        authoritative_tool_results=[
            envelope
            for message in evidence_messages
            if (envelope := _parse_tool_envelope(message)) is not None
        ],
        authoritative_packet_metadata=authoritative_packet_metadata,
        required_candidate_kinds=required_candidate_kinds,
        server_composed_candidate_limit=candidate_limit,
    )


def _provider_route_selection_response_schema(
    eligible_route_options: Sequence[Mapping[str, Any]],
    destination_ids: Sequence[str],
    *,
    maximum_selections: int,
) -> dict[str, Any]:
    return {
        "additionalProperties": False,
        "properties": {
            "selections": {
                "items": {
                    "additionalProperties": False,
                    "properties": {
                        "route_id": {
                            "enum": [
                                option["route_id"] for option in eligible_route_options
                            ],
                            "type": "string",
                        },
                        "destination_id": {
                            "enum": list(destination_ids),
                            "type": "string",
                        },
                        "selection_reasons": {
                            "items": {"minLength": 1, "type": "string"},
                            "minItems": 2,
                            "maxItems": 3,
                            "type": "array",
                        },
                        "tradeoff": {"minLength": 1, "type": "string"},
                        "booking_status": {
                            "enum": [
                                "not_required",
                                "recommended",
                                "required",
                                "booked",
                                "unknown",
                            ],
                            "type": "string",
                        },
                        "weather_sensitivity": WeatherSensitivity.model_json_schema(),
                    },
                    "required": [
                        "route_id",
                        "destination_id",
                        "selection_reasons",
                        "tradeoff",
                        "booking_status",
                        "weather_sensitivity",
                    ],
                    "type": "object",
                },
                "minItems": 1,
                "maxItems": min(
                    maximum_selections,
                    len(eligible_route_options),
                ),
                "type": "array",
            }
        },
        "required": ["selections"],
        "type": "object",
    }


def _provider_route_option_group_key(
    option: Mapping[str, Any],
) -> tuple[str, str, str]:
    """The exact journey responsibility represented by one route option."""

    provider_scope_id = str(option.get("provider_evidence_scope_id") or "")
    if provider_scope_id:
        return ("provider_scope", provider_scope_id, "")

    def endpoint_identity(value: Any) -> str:
        endpoint = value if isinstance(value, Mapping) else {}
        return str(
            endpoint.get("place_id")
            or endpoint.get("station_code")
            or endpoint.get("name")
            or ""
        )

    from_identity = endpoint_identity(option.get("from_endpoint"))
    to_identity = endpoint_identity(option.get("to_endpoint"))
    if from_identity and to_identity:
        return ("endpoint_pair", from_identity, to_identity)
    return ("route", str(option.get("route_id") or ""), "")


def _default_provider_route_selection(
    eligible_route_options: Sequence[Mapping[str, Any]],
    destination_ids: Sequence[str],
    *,
    selection_limit: int | None = None,
) -> dict[str, Any]:
    if not eligible_route_options or not destination_ids:
        raise ResearchPacketOutputError(
            "provider default route selection requires options and destination"
        )
    options_by_scope: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for option in eligible_route_options:
        scope_key = _provider_route_option_group_key(option)
        options_by_scope.setdefault(scope_key, []).append(option)
    selections: list[dict[str, Any]] = []
    for scoped_options in options_by_scope.values():
        if selection_limit is not None and len(selections) >= selection_limit:
            break
        option = min(
            scoped_options,
            key=lambda item: (
                int(item.get("duration_minutes") or 10**9),
                str(item.get("route_id") or ""),
            ),
        )
        transport_class = str(option["transport_class"])
        selected_mode = str(option["selected_mode"])
        exposed = transport_class == "flexible" and selected_mode in {"walk", "bike"}
        selections.append(
            {
                "route_id": option["route_id"],
                "destination_id": destination_ids[0],
                "selection_reasons": [
                    "Provider 路线闭包完整",
                    "按总时长与稳定路线标识排序",
                ],
                "tradeoff": "动态班次、库存或道路状况仍需出发前确认",
                "booking_status": "recommended"
                if transport_class == "long_distance"
                else "not_required",
                "weather_sensitivity": {
                    "exposure": "outdoor" if exposed else "mixed",
                    "rain_sensitivity": "high" if exposed else "low",
                    "heat_sensitivity": "high" if exposed else "low",
                    "cold_sensitivity": "low",
                    "wind_sensitivity": "high" if exposed else "low",
                    "requires_clear_visibility": False,
                },
            }
        )
    return {"selections": selections}


def _provider_route_selection_limit(
    eligible_route_options: Sequence[Mapping[str, Any]],
    *,
    required_transport_classes: Sequence[str] | None,
) -> int:
    """Bound one selection per exact Provider scope or local adjacency.

    Required route scopes describe the Run's complete transport contract, but
    the current authoritative results may cover only a subset of those scopes.
    Counting the required scopes would let multiple offers for one outbound leg
    consume the return leg's selection slot and then fail duplicate-scope
    validation.  The bound must therefore come from the distinct exact scopes
    represented by the eligible options in this repair batch.
    """

    represented_responsibilities = {
        _provider_route_option_group_key(option) for option in eligible_route_options
    }
    if represented_responsibilities:
        return len(represented_responsibilities)
    return _TRANSPORT_PROVIDER_SELECTION_LIMIT


def _validate_provider_route_selections(
    selection_payload: Any,
    *,
    eligible_route_options: Sequence[Mapping[str, Any]],
    destination_ids: Sequence[str],
    maximum_selections: int,
) -> list[dict[str, Any]]:
    selections = (
        selection_payload.get("selections")
        if isinstance(selection_payload, dict)
        else None
    )
    if not isinstance(selections, list) or not selections:
        raise ResearchPacketOutputError(
            "provider route selection requires at least one explicit choice"
        )
    if len(selections) > maximum_selections:
        raise ResearchPacketOutputError(
            "provider route selection exceeds the bounded selection limit"
        )
    option_by_id = {
        str(option["route_id"]): option for option in eligible_route_options
    }
    selected_ids: list[str] = []
    selected_responsibilities: set[tuple[str, str, str]] = set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {
            "route_id",
            "destination_id",
            "selection_reasons",
            "tradeoff",
            "booking_status",
            "weather_sensitivity",
        }:
            raise ResearchPacketOutputError(
                "provider route selection contains fields outside its typed domain"
            )
        route_id = str(selection.get("route_id") or "")
        if route_id not in option_by_id or route_id in selected_ids:
            raise ResearchPacketOutputError(
                "provider route selection contains an unavailable or duplicate route"
            )
        if str(selection.get("destination_id") or "") not in destination_ids:
            raise ResearchPacketOutputError(
                "provider route selection contains an uncontrolled destination"
            )
        try:
            WeatherSensitivity.model_validate(selection.get("weather_sensitivity"))
        except ValidationError as exc:
            raise ResearchPacketOutputError(
                "provider route selection contains invalid weather sensitivity"
            ) from exc
        responsibility = _provider_route_option_group_key(option_by_id[route_id])
        if responsibility in selected_responsibilities:
            raise ResearchPacketOutputError(
                "provider route selection repeated an exact journey responsibility"
            )
        selected_responsibilities.add(responsibility)
        selected_ids.append(route_id)
    return selections


def _provider_route_selections_or_default(
    selection_payload: Any,
    *,
    model_authored: bool,
    eligible_route_options: Sequence[Mapping[str, Any]],
    destination_ids: Sequence[str],
    maximum_selections: int,
) -> list[dict[str, Any]]:
    try:
        return _validate_provider_route_selections(
            selection_payload,
            eligible_route_options=eligible_route_options,
            destination_ids=destination_ids,
            maximum_selections=maximum_selections,
        )
    except ResearchPacketOutputError:
        if not model_authored:
            raise
        logger.warning(
            "provider route planning selection violated its typed contract; "
            "applying deterministic policy",
            exc_info=True,
        )
    fallback_payload = _default_provider_route_selection(
        eligible_route_options,
        destination_ids,
        selection_limit=maximum_selections,
    )
    return _validate_provider_route_selections(
        fallback_payload,
        eligible_route_options=eligible_route_options,
        destination_ids=destination_ids,
        maximum_selections=maximum_selections,
    )


def _provider_route_constraint_evaluation(
    constraint: Mapping[str, Any],
    route: Mapping[str, Any],
    fact_ids_by_field: Mapping[str, str],
) -> dict[str, Any]:
    """Evaluate canonical route constraints from server-bound Provider facts."""

    params = (
        constraint.get("params")
        if isinstance(constraint.get("params"), Mapping)
        else {}
    )
    checks: list[bool | None] = []
    evidence_fields: list[str] = []
    try:
        departure = datetime.fromisoformat(
            str(route.get("departure_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        departure = None
    try:
        arrival = datetime.fromisoformat(
            str(route.get("arrival_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        arrival = None

    if params.get("avoid_overnight"):
        checks.append(
            departure.date() == arrival.date()
            if departure is not None and arrival is not None
            else None
        )
        evidence_fields.extend(["departure_at", "arrival_at"])
    earliest = str(params.get("earliest_departure_local") or "")
    if earliest:
        checks.append(
            departure.strftime("%H:%M") >= earliest if departure is not None else None
        )
        evidence_fields.append("departure_at")
    latest = str(params.get("latest_arrival_local") or "")
    if latest:
        checks.append(
            arrival.strftime("%H:%M") <= latest if arrival is not None else None
        )
        evidence_fields.append("arrival_at")
    unsupported_avoid = [
        item
        for item in params.get("avoid") or []
        if item not in {"night_bus", "red_eye_flight"}
    ]
    if unsupported_avoid:
        checks.append(None)

    fact_ids = list(
        dict.fromkeys(
            fact_ids_by_field[field]
            for field in evidence_fields
            if field in fact_ids_by_field
        )
    )
    if False in checks:
        status, reason_code = "failed", "route_time_constraint_failed"
    elif checks and None not in checks:
        status, reason_code = "passed", None
    else:
        status, reason_code = "unknown", "route_constraint_evidence_missing"
        fact_ids = []
    return {
        "constraint_id": str(constraint["constraint_id"]),
        "status": status,
        "fact_assertion_ids": fact_ids,
        "reason_code": reason_code,
    }


async def _repair_from_provider_route_selection(
    *,
    base_payload: Mapping[str, Any],
    expected_run_id: str,
    llm: Any,
    context_messages: Sequence[Mapping[str, Any]],
    evidence_messages: Sequence[Mapping[str, Any]],
    eligible_route_options: Sequence[Mapping[str, Any]],
    destination_ids: Sequence[str],
    required_transport_classes: Sequence[str] | None,
    excluded_candidate_ids: Sequence[str] | None,
    require_current_candidate: bool,
    authoritative_packet_metadata: Mapping[str, Any] | None,
    expected_active_constraints: Sequence[Mapping[str, Any]] | None,
    required_route_scopes: Sequence[ProviderEvidenceScope],
) -> ResearchPacket:
    """Select route ids once, then materialize the complete Provider closure."""
    maximum_selections = _provider_route_selection_limit(
        eligible_route_options,
        required_transport_classes=required_transport_classes,
    )
    model_authored = False
    if (
        required_transport_classes
        and len(eligible_route_options) == 1
        and len(destination_ids) == 1
    ):
        option = eligible_route_options[0]
        transport_class = str(option["transport_class"])
        selected_mode = str(option["selected_mode"])
        is_exposed_flexible = transport_class == "flexible" and selected_mode in {
            "walk",
            "bike",
        }
        selection_payload = {
            "selections": [
                {
                    "route_id": option["route_id"],
                    "destination_id": destination_ids[0],
                    "selection_reasons": [
                        "路线端点均绑定已准入实体",
                        f"满足 {transport_class} 交通类别",
                    ],
                    "tradeoff": "当前作用域仅有这一条完整的 Provider 可执行路线",
                    "booking_status": (
                        "recommended"
                        if transport_class == "long_distance"
                        else "not_required"
                    ),
                    "weather_sensitivity": {
                        "exposure": "outdoor" if is_exposed_flexible else "mixed",
                        "rain_sensitivity": "high" if is_exposed_flexible else "low",
                        "heat_sensitivity": "high" if is_exposed_flexible else "low",
                        "cold_sensitivity": "low",
                        "wind_sensitivity": "high" if is_exposed_flexible else "low",
                        "requires_clear_visibility": False,
                    },
                }
            ]
        }
    else:
        selection_schema = _provider_route_selection_response_schema(
            eligible_route_options,
            destination_ids,
            maximum_selections=maximum_selections,
        )
        selection_messages = [
            dict(message)
            for message in context_messages
            if message.get("role") == "system"
        ]
        selection_messages.append(
            {
                "role": "user",
                "content": (
                    "你现在只做一次强类型交通方案选择，不复制路线字段、segments、SourceRecord、"
                    "FactAssertion 或 provenance。从 eligible_route_options 中根据当前任务、端点、"
                    f"天气和规划质量选择 1 至 {maximum_selections} 条真实路线；"
                    "每个 exact Provider scope 最多一条；route_id 必须逐字来自 option，"
                    "禁止按 Provider 排名自动入选。只填写选择理由、真实取舍、预订状态、天气敏感度和"
                    "受控 destination_id；不要新增现实事实。只输出一个可由 json.loads 解析的 JSON 对象。"
                    f"run_id={expected_run_id}，"
                    f"query_context={json.dumps(base_payload.get('query_context') or {}, ensure_ascii=False, separators=(',', ':'))}，"
                    f"eligible_route_options={json.dumps(eligible_route_options, ensure_ascii=False, separators=(',', ':'))}。"
                    f"必须满足此 JSON Schema：{json.dumps(selection_schema, ensure_ascii=False, separators=(',', ':'))}"
                ),
            }
        )
        model_authored = True
        try:
            selected_raw = await _bounded_packet_model_call(
                llm,
                selection_messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "provider_route_selection",
                        "strict": True,
                        "schema": selection_schema,
                    },
                },
                temperature=0,
                max_output_tokens=_PROVIDER_SELECTION_MAX_OUTPUT_TOKENS,
            )
            selection_payload = json.loads(selected_raw)
        except (
            ResearchPacketOutputError,
            TypeError,
            json.JSONDecodeError,
            # The window bounds *model* calls; once the connector round answered
            # and the options are in hand, a refused model call must not discard
            # them.  ``_default_provider_route_selection`` is a pure function over
            # options already in hand, so a closed window is a reason to take it,
            # never a reason to throw the evidence away.
            ModelWindowClosed,
        ):
            logger.warning(
                "provider route planning selection unavailable; applying deterministic policy",
                exc_info=True,
            )
            selection_payload = _default_provider_route_selection(
                eligible_route_options,
                destination_ids,
                selection_limit=maximum_selections,
            )
            model_authored = False
    selections = _provider_route_selections_or_default(
        selection_payload,
        model_authored=model_authored,
        eligible_route_options=eligible_route_options,
        destination_ids=destination_ids,
        maximum_selections=maximum_selections,
    )
    option_by_id = {
        str(option["route_id"]): option for option in eligible_route_options
    }

    records = _successful_route_records(evidence_messages)
    generated_at = base_payload.get("generated_at")
    sources: dict[str, dict[str, Any]] = {}
    facts: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for selection in selections:
        route_id = str(selection["route_id"])
        envelope, route, route_index = records[route_id]
        audit_id = str(envelope["audit_id"])
        candidate_id = _provider_candidate_id("transport", route_id)
        source_id = _provider_route_source_id(audit_id, route_id)
        result = envelope.get("sanitized_result") or {}
        (
            snapshot,
            cache_provenance,
            observed_at,
            retrieved_at,
            provider_valid_until,
        ) = _provider_snapshot_source_fields(
            envelope,
            fallback_observed_at=generated_at,
        )
        provider_name = str(
            result.get("provider")
            or envelope.get("server_name")
            or "global_route_search"
        )
        sources[source_id] = {
            "source_record_id": source_id,
            "source_kind": "external_tool",
            "title": f"{provider_name} route: {route_id}",
            "provider_name": provider_name,
            "public_excerpt": (
                f"{route['from_endpoint']['name']} → {route['to_endpoint']['name']} · "
                f"{route['duration_minutes']} min"
            )[:900],
            "retrieved_at": retrieved_at,
            "observed_at": observed_at,
            "provider_valid_until": provider_valid_until,
            "content_hash": (
                cache_provenance.content_hash
                if cache_provenance is not None
                else _canonical_snapshot_hash(snapshot)
            ),
            "snapshot": snapshot,
            "lifecycle_status": "active",
            "tool_audit_id": audit_id,
            "cache_provenance": (
                cache_provenance.model_dump(mode="json")
                if cache_provenance is not None
                else None
            ),
        }
        provider_fields = [
            "route_id",
            "transport_class",
            "selected_mode",
            "from_endpoint",
            "to_endpoint",
            "departure_at",
            "arrival_at",
            "duration_minutes",
            "distance_meters",
            "total_cost_cny",
            "segments",
        ]
        fact_ids: list[str] = []
        field_paths: list[str] = []
        entity_ref = EntityRef(
            entity_type=EntityType.TRANSPORT_LEG,
            entity_id=candidate_id,
        )
        for field_path in provider_fields:
            asserted_value = route.get(field_path)
            if asserted_value is None:
                continue
            fact_id = _provider_fact_id(audit_id, candidate_id, field_path)
            fact_ids.append(fact_id)
            field_paths.append(field_path)
            facts.append(
                FactAssertion(
                    fact_assertion_id=fact_id,
                    entity_ref=entity_ref,
                    field_path=field_path,
                    asserted_value=asserted_value,
                    criticality="execution_critical",
                    status="verified",
                    observed_at=observed_at,
                    source_links=[
                        FactSourceLink(
                            source_record_id=source_id,
                            relation="supports",
                            source_locator=(
                                f"routes[{route_index}].{field_path}"
                                if cache_provenance is not None
                                else f"sanitized_result.routes[{route_index}].{field_path}"
                            ),
                        )
                    ],
                ).model_dump(mode="python")
            )
            provenance.append(
                FieldProvenance(
                    origin="external_fact",
                    entity_ref=entity_ref,
                    field_path=field_path,
                    reference_ids=[fact_id],
                ).model_dump(mode="python")
            )
        fact_ids_by_field = dict(zip(field_paths, fact_ids))
        evaluations = [
            _provider_route_constraint_evaluation(
                constraint,
                route,
                fact_ids_by_field,
            )
            for constraint in expected_active_constraints or ()
        ]
        active_constraint_ids = [
            str(constraint["constraint_id"])
            for constraint in expected_active_constraints or ()
        ]
        candidates.append(
            {
                "candidate_id": candidate_id,
                "research_packet_id": base_payload["research_packet_id"],
                "destination_id": selection["destination_id"],
                "fact_assertion_ids": fact_ids,
                "source_record_ids": [source_id],
                "field_paths": field_paths,
                "active_constraint_ids": active_constraint_ids,
                "constraint_evaluations": evaluations,
                "weather_sensitivity": selection["weather_sensitivity"],
                "selection_reasons": selection["selection_reasons"],
                "tradeoff": selection["tradeoff"],
                "planning_decision_ids": [],
                "weather_impact_ids": [],
                "personalization_influence_ids": [],
                "freshness_status": "current",
                "observed_at": observed_at,
                "candidate_kind": "transport",
                "route_id": route_id,
                "transport_class": route["transport_class"],
                "provider_evidence_scope_id": option_by_id[route_id].get(
                    "provider_evidence_scope_id"
                ),
                "selected_mode": route["selected_mode"],
                "from_endpoint": route["from_endpoint"],
                "to_endpoint": route["to_endpoint"],
                "departure_at": route.get("departure_at"),
                "arrival_at": route.get("arrival_at"),
                "duration_minutes": route["duration_minutes"],
                "distance_meters": route.get("distance_meters"),
                "total_cost_cny": route.get("total_cost_cny"),
                "segments": route["segments"],
                "booking_status": selection["booking_status"],
            }
        )
    repaired_payload = {
        **base_payload,
        "candidates": candidates,
        "source_records": list(sources.values()),
        "fact_assertions": facts,
        "field_provenance": provenance,
    }
    return parse_research_packet_output(
        json.dumps(repaired_payload, ensure_ascii=False, default=str),
        expected_worker="transport_researcher",
        expected_run_id=expected_run_id,
        required_transport_classes=required_transport_classes,
        excluded_candidate_ids=excluded_candidate_ids,
        require_current_candidate=require_current_candidate,
        expected_active_constraint_ids=tuple(
            str(constraint["constraint_id"])
            for constraint in expected_active_constraints or ()
        ),
        context_messages=context_messages,
        authoritative_tool_results=[
            envelope
            for message in evidence_messages
            if (envelope := _parse_tool_envelope(message)) is not None
        ],
        authoritative_packet_metadata=authoritative_packet_metadata,
        authoritative_source_records=[
            SourceRecord.model_validate(source) for source in sources.values()
        ],
        required_route_scopes=required_route_scopes,
        # This closure is server-composed from ``maximum_selections`` bounded
        # Provider selections, already validated above.  Naming that bound is what
        # keeps the re-parse from applying a *model-authored* cap to it.
        server_composed_candidate_limit=maximum_selections,
    )


def _isolate_json_object(text: str) -> Optional[dict]:
    """Parse the outermost balanced `{ ... }` region of ``text`` as one JSON
    object.  Returns ``None`` when no balanced object parses — never fabricates."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                    if isinstance(obj, dict):
                        return obj
                except (TypeError, json.JSONDecodeError):
                    continue
    return None


def _coerce_exact_json_object(raw: str) -> Optional[dict]:
    """Parse one model-authored JSON object, tolerating Markdown fences and
    surrounding prose without inventing any content.

    Exact ``json.loads`` is the fast path.  When a worker wraps its answer in
    ```json fences or a sentence of prose, we strip the fence / isolate the
    outermost balanced object and parse *that same text* — this only changes how
    the model's own string is read, never what facts it carries.  It cannot
    fabricate candidates, routes, or provenance that are not already in the
    string, so the "exact JSON only / no envelope fallback" honesty contract is
    preserved.  Returning ``None`` means no balanced JSON object is present.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    obj = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, json.JSONDecodeError):
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    candidate = fence.group(1).strip() if fence else raw
    obj = _isolate_json_object(candidate)
    if obj is not None:
        return obj
    return None


def parse_research_packet_output(
    raw: str,
    *,
    expected_worker: ResearchWorkerKind,
    expected_run_id: str,
    required_transport_classes: Sequence[str] | None = None,
    excluded_candidate_ids: Sequence[str] | None = None,
    require_current_candidate: bool = False,
    expected_active_constraint_ids: Sequence[str] | None = None,
    context_messages: Sequence[Mapping[str, Any]] = (),
    authoritative_tool_results: Sequence[Mapping[str, Any]] = (),
    authoritative_packet_metadata: Mapping[str, Any] | None = None,
    required_candidate_kinds: Sequence[str] | None = None,
    authoritative_source_records: Sequence[SourceRecord] = (),
    required_route_scopes: Sequence[ProviderEvidenceScope] = (),
    server_composed_candidate_limit: int | None = None,
    injected_rag_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> ResearchPacket:
    """Parse exact JSON only; no Markdown extraction, repair, or envelope fallback.

    ``server_composed_candidate_limit`` is only for payloads this module composed
    itself from bounded Provider selections: the worker-wide candidate limit
    bounds *model-authored* packet size (facts and provenance included), which a
    server-composed candidate closure does not spend.  It therefore overrides
    every other bound, including the scoped-transport one.  While the transport
    branch came first, that override was dead for the one worker that composes
    the most server-side: a local connector round carries no exact route scope,
    so the cap fell to 1 and a two-adjacency closure the server had just measured
    against amap was thrown away whole with "exceeds candidate limit 1" — the
    worker then failed, the catalog held no local route at all, and every gap was
    delivered as an invented duration.
    """
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ResearchPacketOutputError("worker output must be one JSON object")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResearchPacketOutputError(
            "worker output is not an exact JSON object"
        ) from exc
    if not isinstance(payload, dict):
        raise ResearchPacketOutputError("worker output must be one JSON object")
    _bind_authoritative_packet_metadata(payload, authoritative_packet_metadata)
    candidates = payload.get("candidates")
    candidate_limit = (
        server_composed_candidate_limit
        if server_composed_candidate_limit is not None
        else packet_candidate_limit(
            expected_worker,
            required_transport_classes=required_transport_classes,
            required_route_scopes=required_route_scopes,
        )
    )
    if isinstance(candidates, list) and len(candidates) > candidate_limit:
        raise ResearchPacketOutputError(
            f"worker Research Packet exceeds candidate limit {candidate_limit}"
        )
    _bind_authoritative_candidate_constraints(
        payload,
        expected_active_constraint_ids,
    )
    evidence_messages = authoritative_tool_messages(authoritative_tool_results)
    # Tool provenance is server-owned.  Both the successful place records and the
    # failure records come from the Tool Gateway transcript, so the system
    # substitutes its own record for any copy echoed back from the previous
    # packet a repair conversation carries.
    compiled_tool_sources = _authoritative_tool_source_records(
        evidence_messages,
        authoritative_source_records=authoritative_source_records,
        default_retrieved_at=_payload_generated_at(payload),
    )
    authoritative_sources: dict[str, SourceRecord | Mapping[str, Any]] = dict(
        compiled_tool_sources
    )
    authoritative_sources.update(
        _bind_successful_place_identity(payload, evidence_messages)
    )
    _ground_retrieved_sources_in_the_transcript(
        payload, evidence_messages, expected_worker=expected_worker
    )
    rag_sources = dict(injected_rag_sources or {})
    _ground_rag_chunk_sources(payload, rag_sources, expected_worker=expected_worker)
    authoritative_sources.update(rag_sources)
    # After identity binding, which rewrites a returned place's identity facts
    # onto its own compiled source: judging before that would drop links the
    # server is about to correct.
    _drop_borrowed_compiled_source_links(payload, expected_worker=expected_worker)
    _prune_payload_to_candidate_closure(payload)
    _bind_source_content_hashes(
        payload,
        authoritative_sources=authoritative_sources,
    )
    if not payload.get("source_records"):
        # A packet without a source closure is an execution audit: the model
        # reports the outcome and the server writes the provenance it already
        # holds for this round, successful Provider calls included.  Nothing is
        # invented here — an empty registry means no tool source exists, and the
        # packet's own ``source_records`` floor then rejects the packet.
        payload["source_records"] = [
            source.model_dump(mode="python")
            for source in compiled_tool_sources.values()
        ]
    _bind_candidate_discovery_lineage(payload)
    try:
        packet = ResearchPacket.model_validate(payload)
    except ValidationError as exc:
        # What authorizes rewriting a candidate-bearing payload to zeros is a
        # tool failure the *server* recorded.  ``compiled_tool_sources`` is
        # built from this round's Tool Gateway transcript alone, so a rejected
        # entry here means a call really failed or degraded.  The payload's own
        # source records prove nothing: every failure signal in them — the
        # ``lifecycle_status``, the title wording, the snapshot error keys — is
        # text the model typed, so reading them let a model discard its own
        # grounded candidates by naming one source "… degradation".
        compiled_failure_source_ids = [
            source_id
            for source_id, source in compiled_tool_sources.items()
            if source.lifecycle_status == "rejected"
        ]
        if (
            _NO_ELIGIBLE_IDENTITY_FACTS not in str(exc)
            or not compiled_failure_source_ids
        ):
            raise ResearchPacketOutputError(
                f"worker Research Packet failed schema gate: {exc}"
            ) from exc
        # Failed/self-attested sources cannot support a candidate, but the exact
        # provider record is still needed by Candidate Gate to exclude the same
        # failure on the next bounded research round.  Drop only the unusable
        # candidate closure; never promote or rewrite any reality fact.
        #
        # This branch rewrites a candidate-bearing payload to zeros.  Without
        # this line the finalize log reads ``candidates=0`` and is
        # indistinguishable from the model legally emitting ``candidates: []``.
        logger.warning(
            "Research Packet candidate closure dropped (failed/degraded sources only) | "
            "worker=%s dropped_candidates=%d dropped_facts=%d dropped_provenance=%d "
            "candidate_ids=%s failed_source_ids=%s validation_error=%s",
            expected_worker,
            len(payload.get("candidates") or []),
            len(payload.get("fact_assertions") or []),
            len(payload.get("field_provenance") or []),
            ",".join(
                str(candidate.get("candidate_id") or "<unnamed>")
                for candidate in (payload.get("candidates") or [])
                if isinstance(candidate, Mapping)
            )
            or "-",
            ",".join(compiled_failure_source_ids) or "-",
            str(exc)[:400],
        )
        failed_payload = {
            **payload,
            "candidates": [],
            "fact_assertions": [],
            "field_provenance": [],
        }
        try:
            packet = ResearchPacket.model_validate(failed_payload)
        except ValidationError:
            raise ResearchPacketOutputError(
                f"worker Research Packet failed schema gate: {exc}"
            ) from exc
    if packet.worker_kind != expected_worker:
        raise ResearchPacketOutputError(
            "worker Research Packet domain does not match the node"
        )
    if packet.run_id != expected_run_id:
        raise ResearchPacketOutputError(
            "worker Research Packet run id does not match active run"
        )
    if required_candidate_kinds:
        allowed_kinds = {
            candidate_kind
            for candidate_kind, _ in _WORKER_CANDIDATE_MODELS[expected_worker]
        }
        required_kinds = set(required_candidate_kinds)
        if not required_kinds <= allowed_kinds:
            raise ResearchPacketOutputError(
                "required candidate kinds do not belong to the active worker"
            )
        missing_kinds = required_kinds - {
            candidate.candidate_kind for candidate in packet.candidates
        }
        if missing_kinds:
            raise ResearchPacketOutputError(
                "worker Research Packet is missing required candidate kinds: "
                f"{','.join(sorted(missing_kinds))}"
            )
    eligible_place_options = _eligible_place_selection_options(
        evidence_messages,
        expected_worker,
        excluded_candidate_ids=excluded_candidate_ids,
    )
    if not packet.candidates and _has_eligible_place_selection(eligible_place_options):
        raise ResearchPacketOutputError(
            "worker returned zero candidates despite eligible Provider place records"
        )
    if expected_active_constraint_ids is not None:
        expected_constraints = set(expected_active_constraint_ids)
        for candidate in packet.candidates:
            if set(candidate.active_constraint_ids) != expected_constraints:
                raise ResearchPacketOutputError(
                    "candidate active constraints do not match the authoritative constraint pack"
                )
            if {
                evaluation.constraint_id
                for evaluation in candidate.constraint_evaluations
            } != expected_constraints:
                raise ResearchPacketOutputError(
                    "candidate constraint evaluations do not cover the authoritative active constraints"
                )
    forbidden = set(excluded_candidate_ids or [])
    if forbidden & {candidate.candidate_id for candidate in packet.candidates}:
        raise ResearchPacketOutputError(
            "worker Research Packet repeated a candidate excluded by the active gap"
        )
    if required_transport_classes and any(
        getattr(candidate, "transport_class", None) not in required_transport_classes
        for candidate in packet.candidates
    ):
        raise ResearchPacketOutputError(
            "transport Research Packet does not match the scoped transport classes"
        )
    allowed_route_scope_ids = {scope.scope_id for scope in required_route_scopes}
    if required_route_scopes and any(
        candidate.candidate_kind == "transport"
        and candidate.transport_class == "long_distance"
        and candidate.provider_evidence_scope_id not in allowed_route_scope_ids
        for candidate in packet.candidates
    ):
        raise ResearchPacketOutputError(
            "long-distance candidate does not match an assigned exact route leg"
        )
    if (
        require_current_candidate
        and packet.candidates
        and not any(
            candidate.freshness_status == "current" for candidate in packet.candidates
        )
    ):
        raise ResearchPacketOutputError(
            "scoped Research Packet did not produce a current candidate"
        )
    return packet


def _parse_packet_lenient_first(raw: str, **kwargs: Any) -> ResearchPacket:
    """Initial worker-packet parse: tolerate Markdown fences / surrounding prose.

    The first read of a worker's free-text output is allowed to strip fences and
    isolate the JSON object(s) it actually contains, so a wrapper style that would
    otherwise trigger a costly schema-repair round is accepted verbatim.
    This leniency is **never** applied to a repair's own output: :func:`parse...`
    above stays exact, so a repair that returns fences/prose is still a schema
    failure (test ``test_schema_repair_remains_exact_...``).
    """
    try:
        return parse_research_packet_output(raw, **kwargs)
    except ResearchPacketOutputError:
        coerced = _coerce_exact_json_object(raw)
        if coerced is None:
            raise
        return parse_research_packet_output(
            json.dumps(coerced, ensure_ascii=False),
            **kwargs,
        )


async def parse_or_repair_research_packet_output(
    raw: str,
    *,
    expected_worker: ResearchWorkerKind,
    expected_run_id: str,
    llm: Any,
    context_messages: Sequence[Mapping[str, Any]],
    authoritative_tool_results: Sequence[Mapping[str, Any]] = (),
    required_transport_classes: Sequence[str] | None = None,
    excluded_candidate_ids: Sequence[str] | None = None,
    require_current_candidate: bool = False,
    expected_active_constraint_ids: Sequence[str] | None = None,
    expected_active_constraints: Sequence[Mapping[str, Any]] | None = None,
    authoritative_packet_metadata: Mapping[str, Any] | None = None,
    required_candidate_kinds: Sequence[str] | None = None,
    authoritative_source_records: Sequence[SourceRecord] = (),
    required_route_scopes: Sequence[ProviderEvidenceScope] = (),
    injected_rag_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> ResearchPacket:
    """Parse once, then perform one bounded schema-only model repair.

    The repair call receives the original tool transcript so it can serialize facts
    already returned by external sources.  It has no tools and its output still goes
    through the exact parser below; this is not Markdown extraction, legacy envelope
    recovery, or a permission to invent missing evidence.
    """
    evidence_messages = authoritative_tool_messages(authoritative_tool_results)
    try:
        packet = _parse_packet_lenient_first(
            raw,
            expected_worker=expected_worker,
            expected_run_id=expected_run_id,
            required_transport_classes=required_transport_classes,
            excluded_candidate_ids=excluded_candidate_ids,
            require_current_candidate=require_current_candidate,
            expected_active_constraint_ids=expected_active_constraint_ids,
            context_messages=context_messages,
            authoritative_tool_results=authoritative_tool_results,
            authoritative_packet_metadata=authoritative_packet_metadata,
            required_candidate_kinds=required_candidate_kinds,
            authoritative_source_records=authoritative_source_records,
            required_route_scopes=required_route_scopes,
            injected_rag_sources=injected_rag_sources,
        )
        return _merge_failed_tool_sources(packet, evidence_messages)
    except ResearchPacketOutputError as initial_error:
        eligible_place_options = _eligible_place_selection_options(
            evidence_messages,
            expected_worker,
            excluded_candidate_ids=excluded_candidate_ids,
        )
        provider_selection_base = _provider_selection_base_payload(
            raw,
            expected_worker=expected_worker,
            expected_run_id=expected_run_id,
            authoritative_packet_metadata=authoritative_packet_metadata,
        )
        eligible_route_options = (
            _eligible_route_selection_options(
                evidence_messages,
                required_transport_classes=required_transport_classes,
                excluded_candidate_ids=excluded_candidate_ids,
                required_route_scopes=required_route_scopes,
            )
            if expected_worker == "transport_researcher"
            else []
        )
        controlled_destination_ids = (
            _controlled_destination_ids(provider_selection_base)
            if provider_selection_base is not None
            else []
        )
        # ``required_candidate_kinds`` is the *parse-time promise* contract — the
        # domains this packet must close — not the set of domains the server's own
        # compiled Provider options may offer.  P17 split promise from discovery
        # (``destination_researcher/node.py::resolve_discovery_candidate_kinds``):
        # a non-CN round discovers and quality-verifies dining without promising
        # it.  Narrowing the *selection* domain to the promise threw those
        # server-verified options away — the initial ReAct output is almost never
        # a legal packet, so every candidate comes from this branch, and the
        # itinerary was left to invent restaurants.  Only the Candidate-Gate
        # targeted round keeps the hard scoped contract: it was dispatched to
        # close exactly one assigned gap and must close it or fail.
        scoped_place_options = eligible_place_options
        if required_candidate_kinds and require_current_candidate:
            required_kinds = set(required_candidate_kinds)
            scoped_place_options = {
                candidate_kind: options
                for candidate_kind, options in eligible_place_options.items()
                if candidate_kind in required_kinds
            }
        if provider_selection_base is not None and _has_eligible_place_selection(
            scoped_place_options
        ):
            planned_repair_branch = "provider_selection"
        elif (
            provider_selection_base is not None
            and eligible_route_options
            and controlled_destination_ids
        ):
            planned_repair_branch = "route_selection"
        else:
            planned_repair_branch = "schema_repair"
        logger.info(
            "Research Packet repair entry | worker=%s raw_len=%d place_options=%d "
            "route_options=%d branch=%s initial_error=%s",
            expected_worker,
            len(raw or ""),
            sum(len(options) for options in scoped_place_options.values()),
            len(eligible_route_options),
            planned_repair_branch,
            initial_error,
        )
        if (
            provider_selection_base is not None
            and planned_repair_branch == "provider_selection"
        ):
            try:
                logger.info(
                    "Provider selection repair | worker=%s options=%s initial_error=%s",
                    expected_worker,
                    sum(len(options) for options in eligible_place_options.values()),
                    initial_error,
                )
                packet = await _repair_from_provider_selection(
                    base_payload=provider_selection_base,
                    expected_worker=expected_worker,
                    expected_run_id=expected_run_id,
                    llm=llm,
                    context_messages=context_messages,
                    evidence_messages=evidence_messages,
                    eligible_place_options=scoped_place_options,
                    excluded_candidate_ids=excluded_candidate_ids,
                    require_current_candidate=require_current_candidate,
                    expected_active_constraint_ids=expected_active_constraint_ids,
                    expected_active_constraints=expected_active_constraints,
                    authoritative_packet_metadata=authoritative_packet_metadata,
                    required_candidate_kinds=required_candidate_kinds,
                )
                return _merge_failed_tool_sources(packet, evidence_messages)
            except Exception as selection_error:
                raise ResearchPacketOutputError(
                    f"worker Provider selection repair failed: {selection_error}"
                ) from initial_error
        if (
            provider_selection_base is not None
            and planned_repair_branch == "route_selection"
        ):
            try:
                logger.info(
                    "Provider route selection repair | options=%s initial_error=%s",
                    len(eligible_route_options),
                    initial_error,
                )
                packet = await _repair_from_provider_route_selection(
                    base_payload=provider_selection_base,
                    expected_run_id=expected_run_id,
                    llm=llm,
                    context_messages=context_messages,
                    evidence_messages=evidence_messages,
                    eligible_route_options=eligible_route_options,
                    destination_ids=controlled_destination_ids,
                    required_transport_classes=required_transport_classes,
                    excluded_candidate_ids=excluded_candidate_ids,
                    require_current_candidate=require_current_candidate,
                    authoritative_packet_metadata=authoritative_packet_metadata,
                    expected_active_constraints=expected_active_constraints,
                    required_route_scopes=required_route_scopes,
                )
                return _merge_failed_tool_sources(packet, evidence_messages)
            except Exception as selection_error:
                raise ResearchPacketOutputError(
                    f"worker Provider route selection repair failed: {selection_error}"
                ) from initial_error
        authoritative_external_tool_sources = _authoritative_external_tool_sources(
            evidence_messages,
            eligible_place_options=eligible_place_options,
            authoritative_source_records=authoritative_source_records,
        )
        packet_schema = _research_packet_response_schema(
            expected_worker,
            required_transport_classes,
            expected_active_constraint_ids,
            eligible_place_options,
            required_candidate_kinds,
            required_route_scopes,
            authoritative_external_tool_source_ids=[
                source["source_record_id"]
                for source in authoritative_external_tool_sources
            ],
        )
        schema = json.dumps(
            packet_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        scoped_transport = expected_worker == "transport_researcher" and bool(
            required_transport_classes
        )
        destination_repair = expected_worker == "destination_researcher"
        transport_scope_instruction = (
            "本次 transport_class 只能是 "
            f"{','.join(required_transport_classes)}；禁止输出 long_distance。"
            "本轮只收口一个最高质量 current 候选；segments 作为一个完整字段用一条 "
            "FactAssertion(field_path=segments) 绑定原始路线结果，禁止为每个 segment 的 mode/name/place_id "
            "分别复制 FactAssertion 与 FieldProvenance；route_id、selected_mode、departure_at、arrival_at、"
            "duration_minutes 与 segments 必须逐字复制 global_route_search.routes[0] 并分别建立外部事实"
            "（逐字包括 null：高德只给时长不给时刻表，大陆路线的 departure_at / arrival_at 就是 null，"
            "照抄 null，不得自己算一个时刻出来）；"
            "整份 Packet 最多 12 条事实与 12 条 provenance。"
            if required_transport_classes
            else ""
        )
        candidate_exclusion_instruction = (
            "本轮禁止再次输出这些已拒绝 candidate_id 或同一实体："
            f"{','.join(excluded_candidate_ids)}；必须使用 transcript 中不同的外部实体事实。"
            if excluded_candidate_ids
            else ""
        )
        current_candidate_instruction = (
            "这是定向补研收口：必须至少保留一个 freshness_status=current 的候选；"
            "只有同实体、同字段的当前外部工具结果或网页事实才能标 verified。"
            "若 transcript 没有这样的事实则不要伪造，收口必须失败。"
            if require_current_candidate
            else ""
        )
        constraint_instruction = (
            "每个候选的 active_constraint_ids 与 constraint_evaluations.constraint_id "
            "都必须恰好覆盖这些权威 active hard constraint IDs："
            f"{','.join(expected_active_constraint_ids) or '[]'}；禁止添加、遗漏或重命名。"
            if expected_active_constraint_ids is not None
            else ""
        )
        # Every number in this sentence is the one the schema enforces beside it
        # (``_research_packet_response_schema``'s destination branch), read from
        # the same place rather than typed out again.
        destination_repair_candidates = packet_candidate_limit(expected_worker)
        destination_repair_facts = (
            destination_repair_candidates * _DESTINATION_IDENTITY_FACTS_PER_CANDIDATE
        )
        destination_scope_instruction = (
            f"目的地结构化收口最多保留 {destination_repair_candidates} 个候选；"
            "优先保留能由单个完整地点 Provider 响应支持全部五个稳定身份字段的实体，"
            "Visit/Dining 都有合格实体时应同时保留两类。每个候选只输出 name 或 branch_name、place_id、"
            "provider_place_type、provider_country_code、address 这五条准入身份事实及对应 provenance；"
            f"本轮最多 {destination_repair_facts} 条事实、{destination_repair_facts} 条 provenance、"
            f"{destination_repair_candidates} 条 SourceRecord。其它字段没有本轮直接外部支持"
            "（Tool Gateway transcript 中的结果、本轮真检索到的网页，或提示词里印过「引用标识」的参考知识库 chunk）"
            "就留空数组或 null，禁止裁剪 SourceRecord.snapshot 或用摘要替代完整工具返回。"
            if destination_repair
            else ""
        )
        provider_selection_instruction = (
            "本轮 transcript 已有通过 Tool Gateway evidence 边界且与当前 Worker 领域匹配的地点记录；"
            "candidates 不得为空，必须显式选择至少一个下列 typed option，并逐字使用其 place_id。"
            "这只是可选集合，不代表按 Provider 排名自动入选；你仍须根据任务和约束作出选择。"
            f"eligible_place_options={json.dumps(eligible_place_options, ensure_ascii=False, separators=(',', ':'))}。"
            if _has_eligible_place_selection(eligible_place_options)
            else ""
        )
        authoritative_source_instruction = (
            "external_tool SourceRecord 的 source_record_id 必须逐字取自下列权威清单，"
            "按 tool_name 对上 transcript 中的同一次调用；清单之外的成功工具证据一律标 "
            'source_kind="external_web"，禁止自行拼接 external_tool id。'
            "清单只列可以承载候选事实的成功 Provider 身份；失败或降级的调用不在其中，"
            "也不要改写成 external_web 来源，系统会从 transcript 补齐这些记录。"
            f"authoritative_external_tool_sources={json.dumps(authoritative_external_tool_sources, ensure_ascii=False, separators=(',', ':'))}。"
            if authoritative_external_tool_sources
            else (
                "本轮没有任何权威 external_tool source id；所有成功的工具证据一律标 "
                'source_kind="external_web"，禁止输出 external_tool SourceRecord；'
                "失败或降级的调用不要写成任何来源，系统会从 transcript 补齐这些记录。"
            )
        )
        repair_messages = [dict(message) for message in context_messages]
        if raw.strip():
            repair_messages.append({"role": "assistant", "content": raw})
        repair_messages.append(
            {
                "role": "user",
                "content": (
                    "上一次输出没有通过 Research Packet schema gate。现在只做一次结构化收口："
                    "仅使用本对话中已有的外部工具结果和事实，不调用工具，不新增、猜测或补齐现实事实；"
                    "缺失字段按 schema 使用 null、空数组或 unknown，绝不伪造来源。"
                    "若所有候选事实都只来自失败、降级或自产来源，必须输出 candidates=[]、"
                    "fact_assertions=[]、field_provenance=[]；失败 SourceRecord 由系统从 Tool Gateway "
                    "transcript 编译，不要自行输出；"
                    "只输出一个可被 json.loads 直接解析的 JSON 对象，不要 Markdown、代码围栏、解释或旧信封。"
                    f"固定 worker_kind={expected_worker}，run_id={expected_run_id}。"
                    f"校验错误：{initial_error}。"
                    f"{transport_scope_instruction}"
                    f"{candidate_exclusion_instruction}"
                    f"{current_candidate_instruction}"
                    f"{constraint_instruction}"
                    f"{destination_scope_instruction}"
                    f"{provider_selection_instruction}"
                    f"{authoritative_source_instruction}"
                    "candidates 只能包含当前 Worker 的领域类型："
                    f"{','.join(kind for kind, _ in _WORKER_CANDIDATE_MODELS[expected_worker])}；"
                    # Scoped transport closes out one leg per round by design
                    # (``transport_scope_instruction`` above says so too); every
                    # other worker states the number its schema enforces.
                    f"最多保留 {1 if scoped_transport else packet_candidate_limit(expected_worker)} 个有直接外部支持的高质量候选；"
                    "只保留直接支持这些候选的最小 facts/sources/provenance 闭包，"
                    "但入选 SourceRecord.snapshot 仍必须是完整工具返回或完整 RAG chunk，禁止裁剪成模型摘要；"
                    "external_tool SourceRecord 的 identity、snapshot、content_hash 和 cache_provenance "
                    "由系统从 Tool Gateway transcript 编译；禁止复制或修补这些字段，禁止生成 cache_provenance。"
                    "上游 packet 只能作为研究上下文，禁止复制其候选、事实或来源。"
                    "用户输入、旅行日期、人数、偏好、模型输出和确定性计算都不是外部来源；"
                    "禁止创建 User Context/JourneyPilot/Assistant SourceRecord。受控输入直接写 typed candidate，"
                    "不要为它们创建伪外部 FactAssertion/SourceRecord。"
                    "VisitCandidate 的 name/place_id/provider_place_type/provider_country_code/address 与 DiningCandidate 的 branch_name/place_id/provider_place_type/provider_country_code/address "
                    "必须分别有同 entity_id 的外部事实，且 asserted_value 与 typed candidate 对应字段完全一致；"
                    "provider_place_type/provider_country_code 必须逐字来自外部地点 Provider；国家码必须匹配受控目的地，禁止把景点/场馆/市场身份改写成餐馆。"
                    "LodgingCandidate 的 property_name/place_id/provider_place_type/provider_country_code/address 也必须分别有同 entity_id 的外部事实，"
                    "且 asserted_value 与 typed candidate 对应字段完全一致；Provider type/country 必须逐字来自地点 Provider 并匹配受控目的地，只有酒店标题而没有稳定身份和完整地址不足以准入。"
                    "所有 candidate_kind 必须显式输出；候选引用的 fact_assertion_ids 和 source_record_ids "
                    "必须存在于同一 packet，且 source 必须由该候选事实的 supports 链路可达。"
                    "Candidate.research_packet_id、fact_assertion_ids、field_paths、source_record_ids "
                    "会由父 Packet 和同 entity_id 候选事实确定性派生；不得加入其它实体事实或无关来源。"
                    "对每个候选 C：C.fact_assertion_ids 必须恰好等于 entity_id=C.candidate_id 的事实 id 集合；"
                    "C.field_paths 必须恰好等于这些事实的 field_path 集合，禁止多写任何未被事实支持的路径；"
                    "每个 C.field_paths 路径必须有且只有一条相同 entity_id 和 field_path 的 FieldProvenance；"
                    "候选事实与 provenance 的 entity_ref.entity_id 必须等于 candidate_id；"
                    "Candidate.freshness_status 必须与其事实状态一致：全部 verified 时只能是 current；"
                    "含 refreshing 时为 refreshing；其余情况为 stale。"
                    "TransportCandidate.from_endpoint 必须与首个 segment.from_endpoint 完全一致，"
                    "to_endpoint 必须与末个 segment.to_endpoint 完全一致；多段换乘按真实顺序完整保留。"
                    "本轮检索且直接支持稳定实体身份的页面可标 verified，缺少发布时间本身不等于 stale；"
                    "只有明确过期、冲突或不再适用才标 stale。缺少实时价格/余房时使用 null/needs_confirmation，"
                    "并省略对应事实，不要把实体身份事实降为 stale。"
                    "external_fact provenance 的 reference_ids 只能引用同一 packet 的 fact_assertion_ids。"
                    "描述性字段（visit 的 highlights/opening_window/reservation_required、dining 的 "
                    "cuisine_types/recommended_dishes/opening_window/reservation_required、lodging 的 "
                    "room_type/facilities）尽量填满：有检索依据就配同 entity_id、同 field_path 的 FactAssertion，"
                    "没有依据就按常识给合理值并且不写进 field_paths，不要为它编造 FactAssertion 或 SourceRecord。"
                    f"必须满足此 JSON Schema：{schema}"
                ),
            }
        )
        transient_retries = _SCHEMA_REPAIR_TRANSIENT_RETRIES
        while True:
            try:
                repaired = await _bounded_packet_model_call(
                    llm,
                    repair_messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "research_packet",
                            "strict": True,
                            "schema": packet_schema,
                        },
                    },
                    temperature=0,
                )
                break
            except Exception as call_error:
                if transient_retries < 1 or not _is_transient_model_call_failure(
                    call_error
                ):
                    raise ResearchPacketOutputError(
                        f"worker Research Packet schema repair failed: {call_error}"
                    ) from initial_error
                transient_retries -= 1
                logger.info(
                    "Schema repair transient retry | worker=%s call_error=%s",
                    expected_worker,
                    call_error,
                )
        try:
            packet = parse_research_packet_output(
                repaired,
                expected_worker=expected_worker,
                expected_run_id=expected_run_id,
                required_transport_classes=required_transport_classes,
                excluded_candidate_ids=excluded_candidate_ids,
                require_current_candidate=require_current_candidate,
                expected_active_constraint_ids=expected_active_constraint_ids,
                context_messages=context_messages,
                authoritative_tool_results=authoritative_tool_results,
                authoritative_packet_metadata=authoritative_packet_metadata,
                required_candidate_kinds=required_candidate_kinds,
                authoritative_source_records=authoritative_source_records,
                required_route_scopes=required_route_scopes,
                injected_rag_sources=injected_rag_sources,
            )
            logger.info(
                "Research Packet schema repair parsed | worker=%s repaired_len=%d "
                "candidates=%d facts=%d sources=%d",
                expected_worker,
                len(repaired or ""),
                len(packet.candidates),
                len(packet.fact_assertions),
                len(packet.source_records),
            )
            return _merge_failed_tool_sources(packet, evidence_messages)
        except Exception as repair_error:
            raise ResearchPacketOutputError(
                f"worker Research Packet schema repair failed: {repair_error}"
            ) from initial_error


def serialize_research_packet(packet: ResearchPacket) -> str:
    return packet.model_dump_json(exclude_none=True)


def format_research_packet_context(packets: Mapping[str, ResearchPacket]) -> str:
    """Pass typed upstream packets without projecting them back into generic arrays."""
    if not packets:
        return "[]"
    return json.dumps(
        [
            packet.model_dump(mode="json", exclude_none=True)
            for packet in packets.values()
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
