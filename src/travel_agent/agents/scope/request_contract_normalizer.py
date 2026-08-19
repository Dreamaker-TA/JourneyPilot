"""Scope node that creates the request contract in one normalization pass."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig

from ...entities.state import TravelAgentState
from ...memory.context_builder import build_context_report
from ...models.router import get_model_router
from ...panels.constraint import (
    constraint_free_text_sources,
    referenced_context_sections,
)
from ...services.intent_normalization import (
    INTENT_NORMALIZATION_PROMPT_VERSION,
    normalize_clauses,
    split_source_clauses,
)
from ...services.intent_revision import (
    REQUEST_CONTRACT_POLICY_VERSION,
    build_request_contract_revision,
    constraint_pack_semantic_hash,
)
from .constraint_normalizer import build_run_constraint_pack, load_constraint_sources


_NODE_NAME = "request_contract_normalizer"


def _source_kind(state: TravelAgentState, source: str, source_id: str) -> str:
    amendment = state.plan_gate_amendment
    if amendment is not None and source_id == amendment.command_id:
        return "plan_gate_amendment"
    if any(source_id == item.command_id for item in state.pending_intent_amendments):
        return "run_supplement"
    return {
        "current_query": "current_request",
        "session_anchor": "trip_context",
        "memory_fact": "saved_preference",
    }.get(source, "trip_context")


def _constraint_drafts_by_source(
    clauses: List[Any], normalized: Any
) -> Dict[str, List[Dict[str, Any]]]:
    source_by_clause = {item.clause_id: item.source_ref_id for item in clauses}
    result: Dict[str, List[Dict[str, Any]]] = {}
    for clause in normalized.clauses:
        source_ref_id = source_by_clause[clause.clause_id]
        for constraint in clause.constraints:
            result.setdefault(source_ref_id, []).append(
                {
                    "category": constraint.category,
                    "value": constraint.value,
                    "params": constraint.params.compact() or None,
                }
            )
    return result


async def request_contract_normalizer_node(
    state: TravelAgentState,
    config: Optional[RunnableConfig] = None,
) -> Dict[str, Any]:
    loaded = await load_constraint_sources(state)
    free_items = constraint_free_text_sources(state, loaded.memory_facts)
    sources = [
        (
            str(item["id"]),
            _source_kind(
                state, str(item.get("source") or ""), str(item["id"])
            ),
            str(item.get("text") or ""),
        )
        for item in free_items
        if str(item.get("text") or "").strip()
    ]
    clauses = split_source_clauses(sources)
    normalized = await normalize_clauses(
        clauses=clauses,
        controlled_identity=state.controlled_trip_identity,
        llm=get_model_router().get_fast(),
    )
    constraint_pack = await build_run_constraint_pack(
        state,
        loaded=loaded,
        precomputed_free_text_constraints=_constraint_drafts_by_source(
            clauses, normalized
        ),
    )
    constraint_hash = constraint_pack_semantic_hash(constraint_pack)
    same_constraint_contract = bool(
        state.planning_generation is not None
        and state.planning_generation.constraint_hash == constraint_hash
    )
    constraint_revision = (
        state.constraint_pack_revision
        if same_constraint_contract
        else state.constraint_pack_revision + 1
    )
    amendment_ids = {
        item.command_id for item in state.pending_intent_amendments
    }
    if state.plan_gate_amendment is not None:
        amendment_ids.add(state.plan_gate_amendment.command_id)
    normalized_by_clause = {item.clause_id: item for item in normalized.clauses}
    amendment_clauses = [
        item for item in clauses if item.source_ref_id in amendment_ids
    ]
    research_contract_changed = any(
        normalized_by_clause[item.clause_id].constraints
        or any(
            stage in {"research", "admission"}
            for intent in normalized_by_clause[item.clause_id].intents
            for stage in intent.impact_stages
        )
        for item in amendment_clauses
    )
    preserve_generation_id = bool(
        state.request_contract is not None
        and amendment_clauses
        and same_constraint_contract
        and not research_contract_changed
    )
    request_contract, generation = build_request_contract_revision(
        run_id=state.run_id,
        identity=state.controlled_trip_identity,
        identity_revision=state.controlled_trip_identity_revision,
        constraint_pack=constraint_pack,
        constraint_pack_revision=constraint_revision,
        clauses=clauses,
        normalized=normalized,
        previous=state.request_contract,
        plan_revision=state.plan_gate_revision_count,
        preserve_generation_id=preserve_generation_id,
    )

    stream_queue = (config or {}).get("configurable", {}).get("stream_queue")
    if stream_queue is not None:
        report = build_context_report(
            referenced_sections=referenced_context_sections(constraint_pack),
            compaction={"triggered": bool(state.session_compacted_this_turn)},
        )
        if report is not None:
            await stream_queue.put(("context_report", _NODE_NAME, report))

    applied_amendment_ids = [
        item.command_id for item in state.pending_intent_amendments
    ]
    if state.plan_gate_amendment is not None:
        applied_amendment_ids.append(state.plan_gate_amendment.command_id)
    return {
        "request_contract": request_contract,
        "intent_spec": request_contract.intent_spec,
        "intent_spec_revision": request_contract.intent_spec.revision,
        "constraint_pack": constraint_pack,
        "constraint_pack_revision": constraint_revision,
        "planning_generation": generation,
        "plan_gate_amendment": None,
        "pending_intent_amendments": [],
        "applied_intent_amendment_ids": applied_amendment_ids,
        "intent_amendment_route": None,
        "intent_amendment_resume_node": None,
        "prompt_versions": {
            "request_contract_normalization": INTENT_NORMALIZATION_PROMPT_VERSION,
        },
        "policy_versions": {
            "request_contract_revision": REQUEST_CONTRACT_POLICY_VERSION,
        },
    }
