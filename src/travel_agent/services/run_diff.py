from __future__ import annotations

from typing import Any, Dict, List

from pydantic import Field

from ..entities.contract_base import StrictModel


class RunDiffSection(StrictModel):
    changed: bool
    left_hash: str | None = None
    right_hash: str | None = None
    added_ids: List[str] = Field(default_factory=list)
    removed_ids: List[str] = Field(default_factory=list)


class RunDiffReport(StrictModel):
    sections: Dict[str, RunDiffSection]
    semantic_delta_sensitivity: float = Field(ge=0.0, le=1.0)
    selection_overlap_rate: float = Field(ge=0.0, le=1.0)
    explore_diversity_rate: float = Field(ge=0.0, le=1.0)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _ids(value: Any) -> set[str]:
    return {str(item) for item in value or [] if item}


def _section(
    left_hash: Any,
    right_hash: Any,
    *,
    left_ids: Any = (),
    right_ids: Any = (),
) -> RunDiffSection:
    left = _ids(left_ids)
    right = _ids(right_ids)
    return RunDiffSection(
        changed=left_hash != right_hash or left != right,
        left_hash=str(left_hash) if left_hash else None,
        right_hash=str(right_hash) if right_hash else None,
        added_ids=sorted(right - left),
        removed_ids=sorted(left - right),
    )


def build_run_diff(left_audit: dict, right_audit: dict) -> RunDiffReport:
    left_generation = _mapping(left_audit.get("planning_generation"))
    right_generation = _mapping(right_audit.get("planning_generation"))
    left_research = _mapping(left_audit.get("research"))
    right_research = _mapping(right_audit.get("research"))
    left_candidate = _mapping(left_audit.get("candidate"))
    right_candidate = _mapping(right_audit.get("candidate"))
    left_selection = _mapping(left_audit.get("selection"))
    right_selection = _mapping(right_audit.get("selection"))
    left_composition = _mapping(left_audit.get("composition"))
    right_composition = _mapping(right_audit.get("composition"))
    left_fidelity = _mapping(left_audit.get("intent_fidelity"))
    right_fidelity = _mapping(right_audit.get("intent_fidelity"))
    left_formal = _mapping(left_audit.get("formal_delivery"))
    right_formal = _mapping(right_audit.get("formal_delivery"))
    sections = {
        "input": _section(
            left_generation.get("identity_hash"), right_generation.get("identity_hash")
        ),
        "intent": _section(
            left_generation.get("intent_hash"), right_generation.get("intent_hash")
        ),
        "constraint": _section(
            left_generation.get("constraint_hash"),
            right_generation.get("constraint_hash"),
        ),
        "query": _section(
            left_research.get("query_plan_hash"),
            right_research.get("query_plan_hash"),
            left_ids=left_research.get("executed_query_ids"),
            right_ids=right_research.get("executed_query_ids"),
        ),
        "provider_snapshot": _section(
            None,
            None,
            left_ids=left_research.get("provider_snapshot_hashes"),
            right_ids=right_research.get("provider_snapshot_hashes"),
        ),
        "candidate": _section(
            left_candidate.get("catalog_hash"), right_candidate.get("catalog_hash")
        ),
        "admission": _section(
            None,
            None,
            left_ids=left_candidate.get("admitted_candidate_ids"),
            right_ids=right_candidate.get("admitted_candidate_ids"),
        ),
        "ranking": _section(
            left_candidate.get("ranking_hash"), right_candidate.get("ranking_hash")
        ),
        "selection": _section(
            left_selection.get("selection_plan_hash"),
            right_selection.get("selection_plan_hash"),
            left_ids=left_selection.get("selected_candidate_ids"),
            right_ids=right_selection.get("selected_candidate_ids"),
        ),
        "composition": _section(
            left_composition.get("workspace_hash"),
            right_composition.get("workspace_hash"),
        ),
        "mutation": _section(
            None,
            None,
            left_ids=left_composition.get("mutation_ids"),
            right_ids=right_composition.get("mutation_ids"),
        ),
        "coverage": _section(
            left_fidelity.get("coverage_hash"), right_fidelity.get("coverage_hash")
        ),
        "projection": _section(
            left_formal.get("bundle_id"), right_formal.get("bundle_id")
        ),
    }
    left_selected = _ids(left_selection.get("selected_candidate_ids"))
    right_selected = _ids(right_selection.get("selected_candidate_ids"))
    union = left_selected | right_selected
    overlap = len(left_selected & right_selected) / len(union) if union else 1.0
    intent_changed = sections["intent"].changed
    downstream_changed = any(
        sections[name].changed
        for name in ("query", "ranking", "selection", "composition", "coverage")
    )
    explore = "explore" in {
        left_selection.get("mode"),
        right_selection.get("mode"),
    }
    return RunDiffReport(
        sections=sections,
        semantic_delta_sensitivity=(
            1.0 if intent_changed and downstream_changed else 0.0
        ),
        selection_overlap_rate=round(overlap, 4),
        explore_diversity_rate=round(1.0 - overlap, 4) if explore else 0.0,
    )
