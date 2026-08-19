from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Mapping, Sequence

from ..entities.candidate_intent import CandidateIntentMatch, IntentMatchStatus
from ..entities.delivery_bundle import (
    FactAssertion,
    RecommendationCatalog,
    ResearchCandidate,
)
from ..entities.intent_spec import (
    CategoryIntentValue,
    IntentItem,
    IntentKind,
    IntentSpec,
    IntentTarget,
    VerificationMode,
    canonical_json_hash,
)


INTENT_EVALUATION_POLICY_VERSION = "candidate_intent_evaluation.v1"
INTENT_EVALUATION_PROMPT_VERSION = "candidate_intent_evaluation.prompt.v1"


_KIND_TARGET = {
    "visit": IntentTarget.VISIT,
    "dining": IntentTarget.DINING,
    "lodging": IntentTarget.LODGING,
    "transport": None,
}


def _candidate_target(candidate: ResearchCandidate) -> IntentTarget:
    if candidate.candidate_kind != "transport":
        return _KIND_TARGET[candidate.candidate_kind]  # type: ignore[return-value]
    return (
        IntentTarget.LONG_DISTANCE_TRANSPORT
        if candidate.transport_class == "long_distance"
        else IntentTarget.LOCAL_TRANSPORT
    )


def _terms(intent: IntentItem) -> list[str]:
    if isinstance(intent.value, CategoryIntentValue):
        values = intent.value.categories
    else:
        values = [intent.public_summary]
    terms: list[str] = []
    for value in values:
        normalized = value.casefold().strip()
        normalized = re.sub(
            r"^(?:不要|不去|避开|禁止|排除|avoid\s+|no\s+)",
            "",
            normalized,
        ).strip()
        if normalized:
            terms.append(normalized)
    return terms


def _candidate_evidence(
    catalog: RecommendationCatalog,
    candidate: ResearchCandidate,
) -> tuple[list[FactAssertion], list[str], str]:
    facts = [
        fact
        for packet in catalog.research_packets
        for fact in packet.fact_assertions
        if fact.fact_assertion_id in candidate.fact_assertion_ids
        and fact.status == "verified"
    ]
    source_ids = list(candidate.source_record_ids)
    searchable = json.dumps(
        {
            "candidate": candidate.model_dump(mode="json"),
            "facts": [
                {
                    "field_path": fact.field_path,
                    "asserted_value": fact.asserted_value,
                }
                for fact in facts
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).casefold()
    return facts, source_ids, searchable


def _not_applicable(candidate: ResearchCandidate, intent: IntentItem) -> bool:
    return intent.target not in {_candidate_target(candidate), IntentTarget.TRIP}


def _deterministic_match(
    *,
    candidate: ResearchCandidate,
    intent: IntentItem,
    facts: Sequence[FactAssertion],
    source_ids: Sequence[str],
    searchable: str,
) -> CandidateIntentMatch | None:
    if _not_applicable(candidate, intent):
        return CandidateIntentMatch(
            candidate_id=candidate.candidate_id,
            intent_id=intent.intent_id,
            status=IntentMatchStatus.NOT_APPLICABLE,
            method="deterministic",
            reason_code="intent_target_not_applicable",
        )
    if (
        intent.kind
        not in {
            IntentKind.MUST_INCLUDE,
            IntentKind.MUST_EXCLUDE,
            IntentKind.ATTRIBUTE_PREFERENCE,
            IntentKind.GEOGRAPHIC,
            IntentKind.TIME_WINDOW,
        }
        or intent.verification_mode is VerificationMode.SEMANTIC
    ):
        return None
    matched_terms = [term for term in _terms(intent) if term in searchable]
    if not matched_terms and intent.verification_mode is VerificationMode.MIXED:
        return None
    if not matched_terms:
        return CandidateIntentMatch(
            candidate_id=candidate.candidate_id,
            intent_id=intent.intent_id,
            status=IntentMatchStatus.UNKNOWN,
            method="deterministic",
            reason_code="deterministic_evidence_not_found",
        )
    status = (
        IntentMatchStatus.VIOLATED
        if intent.kind is IntentKind.MUST_EXCLUDE
        else IntentMatchStatus.MATCHED
    )
    return CandidateIntentMatch(
        candidate_id=candidate.candidate_id,
        intent_id=intent.intent_id,
        status=status,
        score=0.0 if status is IntentMatchStatus.VIOLATED else 1.0,
        method="deterministic",
        supporting_fact_assertion_ids=[fact.fact_assertion_id for fact in facts],
        supporting_source_record_ids=list(source_ids),
        reason_code=(
            "excluded_category_present"
            if status is IntentMatchStatus.VIOLATED
            else "verified_candidate_attribute_match"
        ),
        public_reason=(
            f"已验证信息包含：{matched_terms[0]}" if matched_terms else None
        ),
    )


def _cache_key(
    intent: IntentItem,
    candidate: ResearchCandidate,
    facts: Sequence[FactAssertion],
    *,
    model_version: str,
) -> str:
    return canonical_json_hash(
        {
            "intent": intent.model_dump(mode="json"),
            "candidate_id": candidate.candidate_id,
            "candidate_facts": [fact.model_dump(mode="json") for fact in facts],
            "policy_version": INTENT_EVALUATION_POLICY_VERSION,
            "model_version": model_version,
            "prompt_version": INTENT_EVALUATION_PROMPT_VERSION,
        }
    )


async def evaluate_candidate_intents(
    *,
    catalog: RecommendationCatalog,
    intent_spec: IntentSpec,
    llm: Any | None = None,
    cache: Mapping[str, CandidateIntentMatch] | None = None,
    model_version: str = "fast",
) -> tuple[list[CandidateIntentMatch], dict[str, CandidateIntentMatch]]:
    cache_out = dict(cache or {})
    matches: list[CandidateIntentMatch] = []
    semantic_batches: dict[
        str, list[tuple[ResearchCandidate, IntentItem, list[FactAssertion], list[str]]]
    ] = defaultdict(list)

    candidates = catalog.candidate_index()
    source_index = {
        source.source_record_id: source
        for packet in catalog.research_packets
        for source in packet.source_records
    }
    for candidate in candidates.values():
        facts, source_ids, searchable = _candidate_evidence(catalog, candidate)
        for intent in intent_spec.active_items:
            deterministic = _deterministic_match(
                candidate=candidate,
                intent=intent,
                facts=facts,
                source_ids=source_ids,
                searchable=searchable,
            )
            if deterministic is not None:
                matches.append(deterministic)
                continue
            if (
                _not_applicable(candidate, intent)
                or "ranking" not in intent.impact_stages
            ):
                matches.append(
                    CandidateIntentMatch(
                        candidate_id=candidate.candidate_id,
                        intent_id=intent.intent_id,
                        status=IntentMatchStatus.NOT_APPLICABLE,
                        method="deterministic",
                        reason_code="intent_not_ranked_for_candidate",
                    )
                )
                continue
            key = _cache_key(intent, candidate, facts, model_version=model_version)
            if key in cache_out:
                matches.append(cache_out[key])
                continue
            semantic_batches[_candidate_target(candidate).value].append(
                (candidate, intent, facts, source_ids)
            )

    for domain, batch in semantic_batches.items():
        unresolved = {
            (candidate.candidate_id, intent.intent_id): (
                candidate,
                intent,
                facts,
                source_ids,
            )
            for candidate, intent, facts, source_ids in batch
        }
        response_rows: list[dict[str, Any]] = []
        if llm is not None and batch:
            allowed_fact_ids = sorted(
                {
                    fact.fact_assertion_id
                    for _candidate, _intent, facts, _source_ids in batch
                    for fact in facts
                }
            )
            allowed_source_ids = sorted(
                {
                    source_id
                    for _candidate, _intent, _facts, source_ids in batch
                    for source_id in source_ids
                }
            )
            schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "candidate_id": {
                                    "type": "string",
                                    "enum": sorted(
                                        {item[0].candidate_id for item in batch}
                                    ),
                                },
                                "intent_id": {
                                    "type": "string",
                                    "enum": sorted(
                                        {item[1].intent_id for item in batch}
                                    ),
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["matched", "not_matched", "unknown"],
                                },
                                "score": {
                                    "type": ["number", "null"],
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                                "supporting_fact_assertion_ids": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "enum": allowed_fact_ids,
                                    },
                                },
                                "supporting_source_record_ids": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "enum": allowed_source_ids,
                                    },
                                },
                                "reason_code": {"type": "string"},
                                "public_reason": {"type": ["string", "null"]},
                            },
                            "required": [
                                "candidate_id",
                                "intent_id",
                                "status",
                                "score",
                                "supporting_fact_assertion_ids",
                                "supporting_source_record_ids",
                                "reason_code",
                                "public_reason",
                            ],
                        },
                    }
                },
                "required": ["matches"],
            }
            payload = [
                {
                    "candidate_id": candidate.candidate_id,
                    "candidate": candidate.model_dump(mode="json"),
                    "intent_id": intent.intent_id,
                    "intent": intent.public_summary,
                    "facts": [fact.model_dump(mode="json") for fact in facts],
                    "sources": [
                        {
                            "source_record_id": source_id,
                            "title": source_index[source_id].title,
                            "public_excerpt": source_index[source_id].public_excerpt,
                        }
                        for source_id in source_ids
                        if source_id in source_index
                    ],
                }
                for candidate, intent, facts, source_ids in batch
            ]
            try:
                response = await llm.ainvoke(
                    [
                        {
                            "role": "system",
                            "content": "只根据给定事实批量判断候选与意图。没有直接证据必须返回 unknown，不得补写属性。",
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"domain": domain, "evaluations": payload},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "candidate_intent_batch_evaluation",
                            "strict": True,
                            "schema": schema,
                        },
                    },
                    temperature=0,
                )
                content = response.content if hasattr(response, "content") else response
                parsed = json.loads(
                    content if isinstance(content, str) else json.dumps(content)
                )
                if isinstance(parsed, dict) and isinstance(parsed.get("matches"), list):
                    response_rows = [
                        row for row in parsed["matches"] if isinstance(row, dict)
                    ]
            except Exception:
                response_rows = []

        rows_by_key = {
            (str(row.get("candidate_id") or ""), str(row.get("intent_id") or "")): row
            for row in response_rows
        }
        for pair, (candidate, intent, facts, source_ids) in unresolved.items():
            fact_ids = {fact.fact_assertion_id for fact in facts}
            allowed_sources = set(source_ids)
            row = rows_by_key.get(pair)
            status = str((row or {}).get("status") or "unknown")
            if status not in {"matched", "not_matched", "unknown"}:
                status = "unknown"
            supporting_facts = [
                value
                for value in (row or {}).get("supporting_fact_assertion_ids") or []
                if value in fact_ids
            ]
            supporting_sources = [
                value
                for value in (row or {}).get("supporting_source_record_ids") or []
                if value in allowed_sources
            ]
            if status in {"matched", "not_matched"} and not (
                supporting_facts or supporting_sources
            ):
                status = "unknown"
            if status == "matched" and intent.kind is IntentKind.MUST_EXCLUDE:
                status = "violated"
            score = None
            if row is not None and row.get("score") is not None and status != "unknown":
                try:
                    parsed_score = float(row["score"])
                except (TypeError, ValueError):
                    parsed_score = -1.0
                if 0.0 <= parsed_score <= 1.0:
                    score = 0.0 if status == "violated" else parsed_score
            match = CandidateIntentMatch(
                candidate_id=candidate.candidate_id,
                intent_id=intent.intent_id,
                status=IntentMatchStatus(status),
                score=score,
                method="semantic_batch_evaluation",
                supporting_fact_assertion_ids=supporting_facts,
                supporting_source_record_ids=supporting_sources,
                reason_code=str(
                    (row or {}).get("reason_code") or "semantic_evidence_unavailable"
                ),
                public_reason=(
                    str(row.get("public_reason"))[:300]
                    if row is not None and row.get("public_reason")
                    else None
                ),
            )
            matches.append(match)
            cache_out[
                _cache_key(intent, candidate, facts, model_version=model_version)
            ] = match

    matches.sort(key=lambda item: (item.candidate_id, item.intent_id))
    return matches, cache_out
