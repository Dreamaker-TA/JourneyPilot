"""Typed Itinerary Planner: composition decisions only, no generic activities."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import unicodedata
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError

from ...entities.delivery_bundle import (
    DiningCandidate,
    LodgingCandidate,
    RecommendationCatalog,
    ResearchDomain,
    TransportCandidate,
    TripWorkspaceV2,
    UserInputAnchor,
    VisitCandidate,
)
from ...entities.candidate_selection import CandidateSelectionPlan
from ...entities.composition_mutation import CompositionMutation
from ...entities.composition_rules import CompositionRule
from ...entities.composition_rules import COMPOSITION_PROMPT_VERSION
from ...entities.intent_coverage import IntentContractSnapshot
from ...entities.itinerary_composition_v2 import (
    AUTHORED_ROUTE_CLASS_BY_MODE,
    MIN_LOCAL_TRANSFER_MINUTES,
    MIN_PHYSICAL_STAY_MINUTES,
    AuthoredPlaceBase,
    AuthoredRoute,
    ItineraryCompositionDraft,
    ItineraryCompositionError,
    LocalConnectorGap,
    drop_authored_places,
    drop_placements_the_traveller_has_already_left,
    _passed_candidates,
    authored_place_key,
    authored_places,
    connector_mode_requests_from_constraint_pack,
    extract_local_connector_gaps,
    map_authored_places,
    materialize_skeleton_connectors,
    materialize_trip_workspace,
    unfilled_connector_gaps,
    validate_placement_skeleton,
    validate_itinerary_transport_topology,
)
from ...services.authored_place_resolution import (
    AuthoredPlaceScope,
    AuthoredPlaceToolAccess,
    authored_place_scopes,
    resolve_authored_place,
)
from ...services.geo_dispersion import itinerary_dispersion
from ...services.candidate_selection import (
    catalog_for_candidate_selection,
    selected_candidate_capabilities,
)
from ...services.composition_rule_compiler import compile_composition_rules
from ...services.composition_backfill import (
    assert_never_violate_rules,
    build_legal_open_slots,
    order_backfill_candidates,
)
from ...services.composition_mutations import (
    diff_composition_mutations,
    mark_mutations_revalidated,
)
from ...entities.state import TravelAgentState, bounded_repair_context
from ...models.router import get_model_router
from ...models.strict_json_schema import as_strict_schema
from ...tools.registry import get_tool_registry
from ...utils.brief_helpers import build_assignment_context
from ...workflows.run_control import ModelWindowClosed, await_model_operation
from ...workflows.run_deadline import observe_run_deadline
from ...entities.provider_evidence import (
    build_required_long_distance_legs,
    explicit_cross_day_return_required,
    provider_evidence_scope_id,
)
from ..utils import (
    append_recent_history,
    build_tool_context_from_state,
    execute_tool,
    inject_agent_context,
    resolve_agent_assignment,
)

logger = logging.getLogger(__name__)

_NODE_NAME = "itinerary_planner"


_AUTHORED_PLACE_DEFS = ("AuthoredVisitPlace", "AuthoredDiningPlace")
_SERVER_OWNED_PLACE_FIELDS = (
    "resolved_place_id",
    "resolved_address",
    "resolved_latitude",
    "resolved_longitude",
)


def selected_ids_by_kind(
    catalog: RecommendationCatalog,
    *,
    skeleton_only: bool = False,
) -> Dict[str, list[str]]:
    """List selected candidate ids per domain, ordered by descending fit."""
    candidate_index = catalog.candidate_index()
    fit_by_id: Dict[str, float] = {}
    for result in catalog.admission_results:
        if result.status != "passed" or result.candidate_id not in candidate_index:
            continue
        scores = result.fit_scores
        fit = min(scores.budget_fit, scores.weather_fit, scores.constraint_fit)
        fit_by_id[result.candidate_id] = min(fit_by_id.get(result.candidate_id, 1.0), fit)
    return {
        kind: sorted(
            (
                candidate_id
                for candidate_id in fit_by_id
                if candidate_index[candidate_id].candidate_kind == kind
                and not (
                    skeleton_only
                    and kind == "transport"
                    and isinstance(candidate_index[candidate_id], TransportCandidate)
                    and candidate_index[candidate_id].transport_class != "long_distance"
                )
            ),
            key=lambda candidate_id: (-fit_by_id[candidate_id], candidate_id),
        )
        for kind in ("visit", "dining", "transport", "lodging")
    }


def controlled_day_dates(state: TravelAgentState) -> List[date]:
    """Exactly which days the composition has to fill, read off the controlled window.

    The count and the dates are one fact, so they are read in one place.  Empty
    when the run has no window to read — then there is nothing to check a
    composition against, which is different from a composition being right.
    """
    identity = state.controlled_trip_identity or {}
    start = end = None
    if isinstance(identity, dict):
        try:
            start = date.fromisoformat(str(identity.get("start_date") or "")[:10])
            end = date.fromisoformat(str(identity.get("end_date") or "")[:10])
        except ValueError:
            start = end = None
    if (start is None or end is None or end < start) and state.weather_context is not None:
        start = state.weather_context.trip_start_date
        end = state.weather_context.trip_end_date
    if start is None or end is None or end < start:
        return []
    return [start + timedelta(days=index) for index in range((end - start).days + 1)]


def itinerary_day_count(state: TravelAgentState) -> int:
    """How many days the composition has to fill, read off the controlled window."""
    return len(controlled_day_dates(state))


def _verify_day_window(
    draft: ItineraryCompositionDraft,
    state: TravelAgentState,
) -> ItineraryCompositionDraft:
    """The composition covers the controlled window exactly, or it is not this trip.

    Nothing else checked this: ``validate_placement_skeleton`` verifies the Day
    dates are *contiguous* — starting from whatever the model wrote as day one —
    and ``ItineraryCompositionDraft`` verifies ``duration_days == len(days)``.  So
    a four-day trip drafted as five days only ever surfaces when the model also
    mis-filled ``duration_days``, and a trip drafted starting on the wrong date
    surfaces not at all.  Both would ship.  Found while root-causing a run where
    one such draft cost the Run a repair round with the useless message
    「composition duration must equal day count」 — this one says the dates.
    """
    expected = controlled_day_dates(state)
    if not expected:
        return draft
    actual = [day.date for day in draft.days]
    if actual != expected:
        raise ValueError(
            "itinerary must cover the controlled window exactly: "
            f"{len(expected)} 天 {expected[0].isoformat()}..{expected[-1].isoformat()}，"
            f"当前 {len(actual)} 天 "
            f"{actual[0].isoformat() if actual else '(空)'}"
            f"..{actual[-1].isoformat() if actual else '(空)'}"
        )
    return draft


def authoring_domains(
    catalog: RecommendationCatalog,
    *,
    day_count: int,
    skeleton_only: bool = False,
) -> set[str]:
    """Domains whose selected candidates cannot cover the itinerary on their own.

    An empty catalog is the extreme case of under-coverage, not the only one:
    every visit/dining entry is unique across the whole itinerary, so one selected
    dining option cannot fill a four-day trip no matter how it is placed.  Inviting
    authoring never displaces a selected candidate — the composition contract
    still requires the selected ones to be referenced — it only lets the planner
    write the entries the catalog cannot supply.
    """
    ids_by_kind = selected_ids_by_kind(catalog, skeleton_only=skeleton_only)
    domains = {
        kind
        for kind in ("visit", "dining")
        if len(ids_by_kind[kind]) < max(day_count, 1)
    }
    if not skeleton_only and not ids_by_kind["transport"]:
        domains.add("transport")
    return domains


def _composition_response_schema(
    catalog: RecommendationCatalog | None = None,
    *,
    skeleton_only: bool = False,
) -> Dict[str, Any]:
    """Bind each placement branch to the selected candidates of the same kind.

    A domain with an empty catalog drops its ``candidate_id`` branch and opens
    the authoring branch instead, which requires a name, an address, and a city.
    """
    schema = ItineraryCompositionDraft.model_json_schema()
    definitions = schema.get("$defs", {})

    def remove_branch(value: Any, ref: str, mapping_key: str) -> None:
        if isinstance(value, dict):
            for key in ("oneOf", "anyOf", "allOf"):
                branches = value.get(key)
                if isinstance(branches, list):
                    value[key] = [
                        branch
                        for branch in branches
                        if not (isinstance(branch, dict) and branch.get("$ref") == ref)
                    ]
            discriminator = value.get("discriminator")
            if isinstance(discriminator, dict) and isinstance(
                discriminator.get("mapping"), dict
            ):
                discriminator["mapping"].pop(mapping_key, None)
            for child in value.values():
                remove_branch(child, ref, mapping_key)
        elif isinstance(value, list):
            for child in value:
                remove_branch(child, ref, mapping_key)

    # The place-provider resolution step owns every ``resolved_*`` field; it is
    # never model input.
    for def_name in _AUTHORED_PLACE_DEFS + ("AuthoredRoute",):
        authored_schema = definitions.get(def_name)
        if not isinstance(authored_schema, dict):
            continue
        properties = authored_schema.get("properties", {})
        for field_name in _SERVER_OWNED_PLACE_FIELDS:
            properties.pop(field_name, None)
        authored_schema["required"] = list(properties)

    ids_by_kind = (
        selected_ids_by_kind(catalog, skeleton_only=skeleton_only)
        if catalog is not None
        else None
    )
    for model_name, candidate_kind in (
        ("VisitPlacement", "visit"),
        ("DiningPlacement", "dining"),
        ("TransportPlacement", "transport"),
    ):
        placement_schema = definitions.get(model_name)
        if not isinstance(placement_schema, dict):
            continue
        properties = placement_schema.get("properties", {})
        if candidate_kind == "transport" and skeleton_only:
            # The skeleton carries no local connectors at all.
            properties.pop("authored_route", None)
        if ids_by_kind is not None:
            selected = ids_by_kind[candidate_kind]
            may_author = "authored_place" in properties or "authored_route" in properties
            if selected:
                properties["candidate_id"] = {
                    "anyOf": [
                        {"type": "string", "enum": selected},
                        {"type": "null"},
                    ]
                }
            elif may_author:
                properties["candidate_id"] = {"type": "null"}
            else:
                remove_branch(
                    schema, f"#/$defs/{model_name}", candidate_kind
                )
                continue
        placement_schema["required"] = list(properties)

    if ids_by_kind is not None:
        lodging_ids = ids_by_kind["lodging"]
        schema["properties"]["lodging_candidate_ids"] = (
            {"type": "array", "items": {"type": "string", "enum": lodging_ids}}
            if lodging_ids
            else {"type": "array", "items": {"type": "string"}, "maxItems": 0}
        )
    # The same schema goes on the wire and into the prompt, so legalize once
    # here: a strict provider rejects the discriminated union outright, and a
    # lenient one reads ``anyOf`` exactly as it read ``oneOf``.  Placement kinds
    # stay selectable either way — every branch pins ``placement_kind`` to its
    # own ``const`` — and the authoritative check is still the Pydantic union in
    # ``_parse_exact_llm_composition``.
    return as_strict_schema(schema)


def _placement_capabilities(
    catalog: RecommendationCatalog,
    *,
    skeleton_only: bool = False,
    selection_plan: CandidateSelectionPlan | None = None,
) -> list[dict[str, Any]]:
    """Expose only typed placement facts needed to compose a legal topology."""
    if selection_plan is not None:
        selected, _alternatives = selected_candidate_capabilities(
            catalog=catalog,
            selection_plan=selection_plan,
        )
        return [
            capability.model_dump(mode="json", exclude_none=True)
            for capability in selected
            if not (
                skeleton_only
                and capability.candidate_kind == "transport"
                and capability.schedule_capabilities.get("transport_class")
                != "long_distance"
            )
        ]
    candidate_index = catalog.candidate_index()
    fit_by_id: dict[str, Any] = {}
    for result in catalog.admission_results:
        if result.status != "passed" or result.selection_slot_id is not None:
            continue
        fit_by_id[result.candidate_id] = result.fit_scores
    capabilities: list[dict[str, Any]] = []
    for candidate_id in sorted(fit_by_id):
        candidate = candidate_index[candidate_id]
        if (
            skeleton_only
            and isinstance(candidate, TransportCandidate)
            and candidate.transport_class != "long_distance"
        ):
            continue
        scores = fit_by_id[candidate_id]
        item: dict[str, Any] = {
            "candidate_id": candidate_id,
            "candidate_kind": candidate.candidate_kind,
            "destination_id": candidate.destination_id,
            "budget_fit": round(scores.budget_fit, 2),
            "weather_fit": round(scores.weather_fit, 2),
            "constraint_fit": round(scores.constraint_fit, 2),
        }
        if isinstance(candidate, VisitCandidate):
            item.update(name=candidate.name, place_id=candidate.place_id)
        elif isinstance(candidate, DiningCandidate):
            item.update(
                name=candidate.branch_name,
                place_id=candidate.place_id,
                meal_types=candidate.meal_types,
            )
        elif isinstance(candidate, TransportCandidate):
            item.update(
                transport_class=candidate.transport_class,
                from_place_id=candidate.from_endpoint.place_id,
                to_place_id=candidate.to_endpoint.place_id,
                departure_date=(
                    candidate.departure_at.date().isoformat()
                    if candidate.departure_at is not None
                    else None
                ),
                arrival_date=(
                    candidate.arrival_at.date().isoformat()
                    if candidate.arrival_at is not None
                    else None
                ),
            )
        elif isinstance(candidate, LodgingCandidate):
            item.update(
                name=candidate.property_name,
                check_in_date=candidate.check_in_date.isoformat(),
                check_out_date=candidate.check_out_date.isoformat(),
            )
        capabilities.append(item)
    return capabilities


# A Pydantic failure lists every violated field; the leading entries name the
# shape of the mistake and the tail repeats it once per placement.
_COMPOSITION_FAILURE_VALIDATION_ERRORS = 3

# How many blocking delivery-quality gaps a repair prompt restates.
_COMPOSITION_REPAIR_GAP_LIMIT = 8


# What an output-ceiling truncation looks like coming back through the SDK.  The
# provider does not raise a typed error for it: the completion simply stops at the
# ceiling and the parser fails on the half-written JSON.
_OUTPUT_TRUNCATION_MARKERS = ("length limit was reached", "finish_reason=length")


def _is_output_truncation(exc: BaseException) -> bool:
    """Whether this composition failed because the completion hit its ceiling."""
    text = str(exc)
    return any(marker in text for marker in _OUTPUT_TRUNCATION_MARKERS)


def _composition_failure_detail(exc: BaseException) -> str:
    """The part of a composition failure worth replaying to the model.

    A repair round's *only* new input is this string, so it has to be something
    the model can act on.  Every other failure here already is — a day that ends
    after the traveller leaves, a dining candidate placed at a meal it does not
    serve — but an output-ceiling truncation arrives as the SDK's own token
    accounting (``Could not parse response content as the length limit was
    reached - CompletionUsage(completion_tokens=8192, prompt_tokens=81110)``).
    Replayed verbatim that tells the model **nothing about what to change**, so
    the round is spent restating a number and the next attempt is the same length.
    Measured: ``trip_de0e806baee64ddc`` spent two of its four repair rounds this
    way and then died with the budget exhausted.

    Retrying is not useless here — completion length varies run to run (78
    measured completions span 67..7103 tokens) — it is only useless while the
    instruction is missing.  So this names the one thing that fixes it, and
    deliberately names what must **not** shrink: dropping days or required
    placements to fit is a wrong itinerary, which is worse than a failed one.
    """
    if _is_output_truncation(exc):
        return (
            "OutputTruncated: 上一轮 composition 输出超出单次补全长度上限被截断，"
            "因此不是一份完整 JSON。请输出更紧凑的同一份行程：压缩 theme、"
            "selection_reason 等自由文本字段的长度，不要重复叙述。"
            "**天数、每天的必需落位与所有 candidate_id 一个都不许减** —— "
            "为了变短而删掉行程内容会产出一份错的行程。"
        )
    if isinstance(exc, ValidationError):
        detail = "；".join(
            "{path}: {message}".format(
                path=".".join(str(part) for part in error.get("loc", ())) or "(root)",
                message=str(error.get("msg", "")),
            )
            for error in exc.errors()[:_COMPOSITION_FAILURE_VALIDATION_ERRORS]
        )
    else:
        detail = str(exc)
    return f"{type(exc).__name__}: {detail}"


def _composition_repair_section(state: TravelAgentState) -> str:
    """The repair round's only new input: why the previous attempt failed.

    Empty whenever nothing failed yet, so a first composition prompt is
    unchanged.  Each writer owns its own channel: the planner's own failures,
    the Candidate Gate's verdict on the previous skeleton, and the blocking
    delivery-quality gaps all arrive separately and are restated together here.
    """
    lines: List[str] = []
    if state.composition_failure_context:
        lines.append(f"- 组合失败原因：{state.composition_failure_context}")
    if state.placement_skeleton_failure_context:
        lines.append(
            "- 候选门对上一轮 skeleton 的裁定："
            f"{state.placement_skeleton_failure_context}"
        )
    blocking = [gap for gap in state.delivery_quality_gaps if gap.blocking]
    for gap in blocking[:_COMPOSITION_REPAIR_GAP_LIMIT]:
        detail = f"{gap.gate} / {gap.reason}（{gap.field_path}）"
        locator = gap.entity_id or gap.candidate_id
        if locator:
            detail += f"，涉及 {locator}"
        lines.append(f"- 质量门阻塞项：{detail}")
    fidelity_gaps = [gap for gap in state.intent_fidelity_gaps if gap.blocking]
    for gap in fidelity_gaps[:_COMPOSITION_REPAIR_GAP_LIMIT]:
        missing_days = gap.repair_context.get("missing_days") or []
        detail = f"{gap.reason}（intent={gap.intent_id}）"
        if missing_days:
            detail += f"，缺失 Day {missing_days}"
        lines.append(f"- 意图验收阻塞项：{detail}")
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"""

<previous_attempt_failure>
上一轮组合失败的确切原因如下，这一轮必须修正它：
{body}
只针对上面点出的问题做最小改动：其余已经合法的候选选择、日期、顺序与当地时间保持原样，不要连带改写。
</previous_attempt_failure>"""


def _required_leg_scope_ids(
    required_legs: Optional[List[Any]],
    run_id: str,
    constraint_pack_revision: int,
) -> Dict[str, str]:
    """Map each required long-distance leg to its authoritative Provider scope id.

    The transport researcher builds one candidate per required leg and tags it
    with ``provider_evidence_scope_id`` (a deterministic hash of the leg's
    from/to/service_date/leg_role).  The placement skeleton must verify *each*
    leg is placed by that id — the date-only view lets a same-day outbound
    satisfy the return's service date and the return never reaches the itinerary
    (the same-day two-long-distance-leg case).
    """
    if not required_legs:
        return {}
    return {
        f"{leg.leg_role}@{leg.service_date.isoformat()}": provider_evidence_scope_id(
            run_id=run_id,
            constraint_pack_revision=constraint_pack_revision,
            worker_kind="transport_researcher",
            research_domain=ResearchDomain.LONG_DISTANCE_TRANSPORT,
            candidate_kind=None,
            transport_class="long_distance",
            target_identity=None,
            route_leg=leg,
        )
        for leg in required_legs
    }


def _composition_schema_json(
    state: TravelAgentState, *, skeleton_only: bool
) -> str:
    """The response schema exactly as the prompt carries it."""
    return json.dumps(
        _composition_response_schema(
            state.recommendation_catalog,
            skeleton_only=skeleton_only,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _composition_catalog_json(state: TravelAgentState) -> str:
    """Selected candidate facts exactly as the prompt carries them."""
    if state.candidate_selection_plan is None:
        raise ValueError("itinerary composition requires a Candidate Selection Plan")
    candidate_index = state.recommendation_catalog.candidate_index()
    candidate_ids = state.candidate_selection_plan.composition_candidate_ids()
    return json.dumps(
        {
            "generation_id": state.recommendation_catalog.generation_id,
            "intent_spec_revision": state.recommendation_catalog.intent_spec_revision,
            "candidates": [
                candidate_index[candidate_id].model_dump(mode="json", exclude_none=True)
                for candidate_id in sorted(candidate_ids)
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _composition_capabilities_json(
    state: TravelAgentState, *, skeleton_only: bool
) -> str:
    """Placement Capabilities exactly as the prompt carries them."""
    return json.dumps(
        _placement_capabilities(
            state.recommendation_catalog,
            skeleton_only=skeleton_only,
            selection_plan=state.candidate_selection_plan,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _alternative_capabilities_json(state: TravelAgentState) -> str:
    if state.candidate_selection_plan is None:
        raise ValueError("itinerary composition requires a Candidate Selection Plan")
    _selected, alternatives = selected_candidate_capabilities(
        catalog=state.recommendation_catalog,
        selection_plan=state.candidate_selection_plan,
    )
    return json.dumps(
        [item.model_dump(mode="json", exclude_none=True) for item in alternatives],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _composition_rules(state: TravelAgentState) -> list[CompositionRule]:
    if state.intent_spec is None:
        raise ValueError("itinerary composition requires an IntentSpec")
    return compile_composition_rules(state.intent_spec)


def _composition_rules_json(state: TravelAgentState) -> str:
    return json.dumps(
        [rule.model_dump(mode="json") for rule in _composition_rules(state)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _intent_contract_json(state: TravelAgentState) -> str:
    if state.intent_spec is None:
        raise ValueError("itinerary composition requires an IntentSpec")
    return json.dumps(
        {
            "schema_version": state.intent_spec.schema_version,
            "intent_spec_id": state.intent_spec.intent_spec_id,
            "revision": state.intent_spec.revision,
            "generation_id": state.intent_spec.generation_id,
            "content_hash": state.intent_spec.content_hash,
            "objective_summary": state.intent_spec.objective_summary,
            "active_items": [
                item.model_dump(
                    mode="json",
                    exclude={"source_text", "source_span_start", "source_span_end"},
                )
                for item in state.intent_spec.active_items
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _minimum_delivery_json(state: TravelAgentState) -> str:
    draft = state.minimum_delivery_draft
    if draft is None:
        raise ValueError("itinerary composition requires a Minimum Delivery Draft")
    return json.dumps(
        {
            "generation_id": draft.planning_generation_id,
            "intent_spec_revision": draft.intent_spec_revision,
            "constraint_pack_revision": draft.constraint_pack_revision,
            "day_shells": [item.model_dump(mode="json") for item in draft.day_shells],
            "preserved_hard_intent_ids": draft.preserved_hard_intent_ids,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _previous_mutations_json(state: TravelAgentState) -> str:
    return json.dumps(
        [
            mutation.model_dump(mode="json")
            for mutation in state.composition_mutations[-20:]
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _composition_prompt(
    state: TravelAgentState,
    task_desc: str,
    *,
    skeleton_only: bool = False,
    required_candidate_kinds: Optional[set[str]] = None,
    required_long_distance_legs: Optional[list] = None,
) -> str:
    """Assemble the composition prompt.

    There is no ``Weather Context`` line in ``<context>`` any more.  This
    prompt used to render ``WeatherContext.model_dump_json()`` there *and* then
    get ``format_weather_context_for_planning``'s block appended by
    ``inject_agent_context`` — the same day records twice, 36% of the prompt
    measured, paid on every call and every repair round.  The injected block is
    the survivor, so the hard contract points at 【规划前天气事实】 by name.  The
    line it used to point at, "已有 Weather Impact", named a field that is empty
    in all 200 measured runs — ``WeatherContext.impacts`` is never populated; the
    impacts live in ``state.weather_impacts`` and reach the composer as
    ``weather_fit`` on Placement Capabilities.
    """
    if state.recommendation_catalog is None:
        raise ValueError("itinerary composition requires an admitted Recommendation Catalog")
    schema = _composition_schema_json(state, skeleton_only=skeleton_only)
    catalog = _composition_catalog_json(state)
    capabilities = _composition_capabilities_json(state, skeleton_only=skeleton_only)
    alternatives = _alternative_capabilities_json(state)
    rules = _composition_rules_json(state)
    intent_contract = _intent_contract_json(state)
    minimum_delivery = _minimum_delivery_json(state)
    previous_mutations = _previous_mutations_json(state)
    _, assignment = resolve_agent_assignment(
        state.agent_assignments or {}, _NODE_NAME
    )
    brief = build_assignment_context(
        assignment=assignment,
        brief=state.research_brief,
        intent_spec=state.intent_spec,
        constraint_pack=state.constraint_pack,
    )
    transport_contract = (
        "- 这是 placement skeleton 阶段：禁止引用任何 public_transit 或 flexible candidate，"
        "只确定 Visit/Dining 的 Day、顺序、当地时间以及日期匹配的 long_distance 主方案。"
        "相邻实体之间暂时不插入路线，后续只从这个顺序确定性提取 connector gaps。"
        if skeleton_only
        else "- 同一 Day 内相邻两个 Visit/Dining placement 之间必须插入一个 Transport placement："
        "这一对端点上只要有已准入的 public_transit / flexible 候选，就**必须**引用那个候选——"
        "它是 Provider 实测的线路、时长与票价，你写的是估计值，估计值不得顶替实测值。"
        "只有这一对端点上没有任何已选主方案时，才用 authored_route 写出出行方式与门到门分钟数"
        "（端点由服务器按前后两个停留点自动填写）。候选只给时长不给时刻表是正常的——"
        "中国大陆的公交地铁按时长作答，时长本身就是这段路的事实。"
        "authored_route 的端点来自它前后紧邻的两个停留点，所以它只能作为这一段相邻关系里"
        "的唯一一条路线：同一段相邻关系内不得再放第二条 Transport placement，也不得把 "
        "authored_route 放在 Day 开头、Day 结尾或 long_distance 旁边这类没有前后两个停留点的位置。"
        "long_distance 只能作为远距离锚点，不能冒充市内连接，也不得输出没有连接段的路线。"
    )
    repair_section = _composition_repair_section(state)
    authoring_kinds = authoring_domains(
        state.recommendation_catalog,
        day_count=itinerary_day_count(state),
        skeleton_only=skeleton_only,
    )
    authoring_contract = ""
    if authoring_kinds:
        kind_labels = "、".join(sorted(authoring_kinds))
        authoring_contract = (
            f"\n- 下列领域的已选主方案不足以覆盖整份行程，缺口由你直接撰写具名条目补足：{kind_labels}。"
            "这些领域里已选的主方案必须先全部用上，撰写条目只用来补足剩下的天数与时段，"
            "不得用撰写条目替换任何一个已选主方案。"
            "撰写条目必须是真实存在、可被地图检索到的地点，"
            "并同时给出 local_name、name、address（街道级地址）、city（所在城市），"
            "以及一句 selection_reason 说明为什么放在这一天这个时段。"
            "local_name 写这家店/这个景点门口招牌上的当地语言原名，逐字照写、不加译名不加拼音；"
            "name 写给旅行者看的常用名称。"
            "地点库是按当地语言原名收录的，local_name 写对与否直接决定这个条目能不能落地，"
            "所以字形必须与当地实际书写一致：中国大陆写简体，不写繁体或异体；"
            "日本写日文汉字与假名，按招牌上的假名写法，不改写成中文汉字也不换用别的假名拼法；"
            "韩国写韩文，泰国写泰文。"
            "服务器会逐个核对坐标，查不到的地点会被退回要求你换一个；"
            "所以只写你确信真实存在的地点，不要写泛指区域、连锁品牌统称或臆造门店。"
            "价格、营业时间、评分这类实时字段一律留空，不要编造。"
        )
    required_kinds_contract = ""
    if required_candidate_kinds:
        kind_labels = "、".join(sorted(required_candidate_kinds))
        required_kinds_contract = (
            f"\n- 整份行程必须为下列每一种物理类型各放置至少一个已选主方案：{kind_labels}。"
            "“每个 Day 至少含一个 Visit/Dining”指的是“非空”，绝不等于可以整份省略某一类型；"
            "只要 required 类型含 dining，就必须至少出现一个 DiningCandidate（用户明确要求美食），"
            "同理 visit 也必须至少出现一个。宁可减少同日停留点，也不得整类缺席。"
        )
    return f"""<role>
你是 JourneyPilot 的行程组合器。你做已选主方案的日期、顺序、当地时间与停留时长决策；已选主方案不足以覆盖行程的领域由你直接撰写具名条目补足。
</role>

<hard_contract>
- 只输出一个可直接 json.loads 的 ItineraryCompositionDraft JSON 对象；禁止 Markdown、解释、旧通用信封或 activities[]。
- 引用候选时只能引用 CandidateSelectionPlan 选入主方案、且由 JSON Schema 对当前 placement_kind 开放的 candidate_id：visit 只能选 VisitCandidate，dining 只能选 DiningCandidate，transport 只能选 TransportCandidate，lodging_candidate_ids 只能选 LodgingCandidate；禁止跨类型引用，也禁止复制或改写 candidate 的名称、价格、来源、交通 segments、营业事实或天气事实。
- Composition Rules 是本轮可执行意图合同。never_violate 规则不得违反；repair_then_deviate 规则无法落实时留给 Fidelity Gate 形成明确偏差；nonblocking_preference 只参与选择和排程，不得伪装成硬事实。
- Placement Capabilities 给出每个候选的 budget_fit / weather_fit / constraint_fit（0–1）。同一领域内优先选择分值高的候选；weather_fit 低的户外项排到天气更好的一天，budget_fit 低的项让位给分值更高的同类。
- 主方案直接适应【规划前天气事实】里的逐日 data_kind、降水概率与风力：forecast 日的恶劣条件必须反映在排序、时间或交通选择上。{authoring_contract}
- 每个 Dining placement 必须选择具体门店并填写餐次；每个需要住宿的入住区间必须在 lodging_candidate_ids 选择具体 property。
- 同一 Day 内每个条目（candidate 或撰写地点）只出现一次；同一个具体 Visit/Dining 在整份行程中也只能出现一次；同一个入住区间在 lodging_candidate_ids 也只选一家 property。候选不够时撰写新的具名地点，不得跨日重复门店或景点凑数。
- 每个 Day 的 placements 必须非空。除已准入 long_distance 构成的真实 travel-only day 外，每个 Day 必须至少包含一个在整份行程中唯一的 Visit/Dining；先给每个 Day 分配一个唯一条目，再考虑给某天增加第二个停留点。不得把多个实体挤在前几天后再跨日复制其中一个填满剩余天。
- **带长途锚点的 Day 允许同时安排 Visit/Dining**，条件是这些停留点必须给出 planned_start 与 planned_end，并且**全部落在锚点的同一侧**。长途锚点不是一段「忙碌时间」，它把旅行者从一座城市搬到另一座：出发前他在出发城市，抵达后他才在这个 Day 的目的地。所以只有一侧可能安排本地停留，另一侧他根本不在这座城市——
  - **抵达日**（锚点把旅行者送到这个 Day 的目的地，含跨夜锚点落在其 arrival 日期的那一天）：锚点必须写成这个 Day 的**第一个** placement，所有停留点排在它后面，且每个 planned_start 都不早于锚点的 arrival_at。
  - **离开日**（锚点把旅行者带离这个 Day 的目的地，含跨夜锚点落在其 departure 日期的那一天）：锚点必须写成这个 Day 的**最后一个** placement，所有停留点排在它前面，且每个 planned_end 都不晚于锚点的 departure_at。
  - placements 的书写顺序就是服务器读取锚点方向的依据，写反了会被当成反方向的锚点，并据此向反方向的端点索取一段市内接驳（例如把抵达高铁写在停留点之后，服务器就会去要一段「深圳的餐厅 → 上海虹桥」的市内路线）。顺序与方向必须一致。
  **锚点与停留点之间同样要留出 {MIN_LOCAL_TRANSFER_MINUTES} 分钟通行窗口**：抵达后的第一站，planned_start 减去锚点 arrival_at 不得小于该窗口；出发前的最后一站，锚点 departure_at 减去 planned_end 不得小于该窗口——从站台到景点、从餐厅回车站都是真实路程，需要这段时间才能排出接驳。当整份行程的每一天都被长途锚点占据（最典型的是两天往返）时，**必须**用这个办法把 required kinds 安排进去——此时不存在没有锚点的空闲 Day，把所有天都做成 travel-only 会导致 required kinds 无处安放、整份组合被拒。
- Catalog 中存在已准入 long_distance 时，每一段**必需移动**（出发、逐对 inter_destination、返程）各选择一个主方案即可；"每个 service window 只选一个主方案" 指的是**同一段必需移动的同方向备选互斥**，不禁止同一天既到达又离开（例如 1 天同日往返，或末日既要交接又要返程）。若是后者那种同一天同时欠「到达腿」与「离开腿」的日子：**必须把到达腿放这一天第一个 placement、离开腿放这一天最后一个 placement**，所有 Visit/Dining 停留在两腿**之间**（planned_start 不早于到达腿 arrival_at、planned_end 不晚于离开腿 departure_at，且都留足 {MIN_LOCAL_TRANSFER_MINUTES} 分钟通行窗口），不得只放一段而漏另一段。长途锚点只能放在 Placement Capabilities 给出的 departure_date 或 arrival_date 对应 Day；行程还有不带锚点的 Day 时它可以单独构成 travel-only Day。禁止把同日备选航班分配到其它日期凑行程，也禁止为了同日追加停留点而把端点不一致的市内路线硬接到长距离锚点。
{transport_contract}{required_kinds_contract}
- 不生成第二份晴雨行程；局部 Plan B 由后续 typed contingency 合同处理。
- Visit/Dining 必须同时输出 planned_start 与 planned_end 两个键：有排期时两者都是带 IANA 对应 UTC offset、且落在 DayComposition.date 当地日期内的 ISO-8601 datetime；不排具体时间时两者都为 null，禁止只输出一端。
- 同一 Day 内已排期的 Visit/Dining 必须按时间先后书写，且相邻两站之间至少留出 {MIN_LOCAL_TRANSFER_MINUTES} 分钟通行窗口：后一站的 planned_start 减去前一站的 planned_end 不得小于该窗口。禁止把两站首尾相接或时间重叠——两站之间的真实通行需要这段时间才能安排。
</hard_contract>

<context>
组合提示版本：{COMPOSITION_PROMPT_VERSION}
任务：{task_desc}
Intent Contract Snapshot：{intent_contract}
Composition Rules：{rules}
Minimum Delivery Day Shell：{minimum_delivery}
Previous Composition Mutations：{previous_mutations}
{brief}
Selected Candidate Facts：{catalog}
Selected Candidate Capabilities：{capabilities}
Alternative Candidate Capabilities（只供修复路由判断，不得直接放入本轮行程）：{alternatives}
</context>{repair_section}

<json_schema>{schema}</json_schema>"""


def composition_prompt_segments(
    state: TravelAgentState,
    task_desc: str,
    *,
    skeleton_only: bool = False,
    required_candidate_kinds: Optional[set[str]] = None,
    required_long_distance_legs: Optional[list] = None,
) -> Dict[str, int]:
    """What a composition prompt is made of, in characters, piece by piece.

    This call carries 66k–80k prompt tokens and nothing says what they are.
    Deciding a candidate-supply number off a guess is how
    ``RESEARCH_PACKET_CANDIDATE_LIMITS`` got its current values in the first
    place, so the supply decision needs a measurement instead.

    It measures **the prompt the node sends**, not a second reconstruction of
    it: it assembles through the same two calls the node makes and then locates
    every named piece verbatim inside that one string.  A named piece that is
    non-empty and *not* found raises — which is the only thing that keeps this
    from silently drifting into measuring something else once a segment name and
    the prompt disagree.  Whatever the named pieces do not cover is reported as
    ``static_frame`` (the role, the hard contract, the tags), so the parts always
    add up to ``total`` exactly.

    Nothing on the request path calls it, so it costs a live run nothing.
    """
    body = _composition_prompt(
        state,
        task_desc,
        skeleton_only=skeleton_only,
        required_candidate_kinds=required_candidate_kinds,
        required_long_distance_legs=required_long_distance_legs,
    )
    prompt = inject_agent_context(body, state, agent_label=_NODE_NAME)

    pieces: List[tuple[str, str]] = [
        ("catalog", _composition_catalog_json(state)),
        ("capabilities", _composition_capabilities_json(state, skeleton_only=skeleton_only)),
        ("alternatives", _alternative_capabilities_json(state)),
        ("composition_rules", _composition_rules_json(state)),
        ("intent_contract", _intent_contract_json(state)),
        ("minimum_delivery", _minimum_delivery_json(state)),
        ("previous_mutations", _previous_mutations_json(state)),
        ("schema", _composition_schema_json(state, skeleton_only=skeleton_only)),
        (
            "brief",
            build_assignment_context(
                assignment=resolve_agent_assignment(
                    state.agent_assignments or {},
                    _NODE_NAME,
                )[1],
                brief=state.research_brief,
                intent_spec=state.intent_spec,
                constraint_pack=state.constraint_pack,
            ),
        ),
        ("repair_section", _composition_repair_section(state)),
        ("task_desc", task_desc),
    ]
    pieces.extend(_agent_context_pieces(state))

    # Longest first, so a short piece that also occurs inside a long one takes a
    # span of its own instead of stealing the long one's.
    claimed: List[tuple[int, int]] = []
    segments: Dict[str, int] = {}
    for name, text in sorted(pieces, key=lambda item: -len(item[1])):
        if not text:
            segments[name] = 0
            continue
        offset = 0
        while True:
            start = prompt.find(text, offset)
            if start < 0:
                raise ValueError(
                    f"composition prompt segment {name!r} is not in the prompt it "
                    "claims to measure"
                )
            end = start + len(text)
            if not any(start < taken_end and end > taken_start for taken_start, taken_end in claimed):
                break
            offset = start + 1
        claimed.append((start, end))
        segments[name] = len(text)
    segments["static_frame"] = len(prompt) - sum(segments.values())
    segments["total"] = len(prompt)
    return segments


def _agent_context_pieces(state: TravelAgentState) -> List[tuple[str, str]]:
    """The named pieces ``inject_agent_context`` appends to this prompt.

    Each one is produced by its own single formatter; this asks those formatters
    for the same strings rather than re-deriving them, so the segment table
    cannot disagree with what was injected.
    """
    from ...panels.constraint import format_constraint_pack_for_prompt
    from ...preset.injector import PresetInjector
    from ...memory.compressor import AnchorSummary
    from ...workflows.weather_context import format_weather_context_for_planning

    anchor = (
        AnchorSummary.from_dict(state.session_anchor).format_for_prompt()
        if getattr(state, "session_anchor", None)
        else ""
    )
    preset = (
        PresetInjector.format_for_agent(state.preset_context)
        if getattr(state, "preset_context", None)
        else ""
    )
    constraints = (
        format_constraint_pack_for_prompt(state.constraint_pack)
        if state.constraint_pack
        else ""
    )
    weather_prose = (
        format_weather_context_for_planning(state)
        if state.weather_context is not None
        else ""
    )
    return [
        ("session_anchor", anchor),
        ("preset", preset),
        ("constraint_pack", constraints),
        # This is the prompt's **only** weather; the composition
        # prompt no longer renders a second copy of the same day records.
        ("weather_planning_prose", weather_prose),
    ]


# An authored place must resolve to a real map location before it can enter the
# itinerary.  When the provider has no match the planner is asked for a
# different named place once, and the composition fails after that. The
# resolution ladder itself is what raises the hit rate; a second model round
# costs more wall clock than it recovers entries.
_AUTHORED_PLACE_REPLACEMENT_ROUNDS = 1

# One entry's whole resolution ladder. A place every provider knows answers in a
# few seconds; a place none of them knows costs a full-bbox scan per alias, and
# the composition window has other entries to place.
_AUTHORED_ENTRY_BUDGET_SECONDS = 20.0

# The tools the authored-place ladder's CN rung calls, and therefore the whole
# allowlist the gateway enforces on this deterministic path.
_AUTHORED_PLACE_TOOL_NAMES = frozenset({"maps_text_search", "maps_search_detail"})


class _AuthoredEntryBudgetSpent(RuntimeError):
    """One entry's resolution ladder ran its full slice and found nothing.

    Distinct from :class:`ModelWindowClosed`: the run still has window left, so
    this entry is disproved while the ones after it keep their own slice.  A
    bare ``asyncio.TimeoutError`` cannot carry that difference — the window
    guard turns any timeout inside the awaitable into a closed window.
    """


# Separators an authored name can gain or lose between attempts without naming
# a different place: spacing, the katakana middle dot, hyphen variants.
_NAME_SEPARATORS = str.maketrans({ch: "" for ch in " \t　・·‧•‐-–—_/"})


def _name_key(value: str) -> str:
    """Match key for one authored name, tolerant of how it was typed.

    NFKC folds the full-width and half-width forms of the same characters
    together; dropping separators and case covers the rest of the respellings
    that still ask for the place the ladder already searched for.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return text.translate(_NAME_SEPARATORS).casefold()


def _names_disproved(name: str, local_name: str, disproved: set[str]) -> bool:
    """Whether either name of an entry is one this run already disproved."""
    if not disproved:
        return False
    keys = {_name_key(value) for value in (name, local_name) if str(value or "").strip()}
    return bool(keys & {_name_key(value) for value in disproved})


def _place_names(place: AuthoredPlaceBase) -> set[str]:
    return {
        str(value)
        for value in (place.name, place.local_name)
        if str(value or "").strip()
    }


def _composition_window_closed(state: TravelAgentState) -> bool:
    if state.run_deadline is None:
        return False
    _observed, observation = observe_run_deadline(state.run_deadline)
    return observation.composition_closed


def _authored_place_tool_access(state: TravelAgentState) -> AuthoredPlaceToolAccess:
    """Bind the authored-place ladder to this run's audited tool gateway.

    The ladder resolves an authored entry's identity against a live provider, and
    that provider answer is what makes an authored identity more trustworthy than
    a candidate's own claim. So its calls travel the same chokepoint as every
    other tool call in the run — ``execute_tool``, which applies the gateway
    allowlist, the provider snapshot cache and the research-window boundary, and
    persists the durable ``tool_execution_audits`` row.

    The allowlist is the ladder's own two tools rather than this node's
    model-facing tool policy: that policy denies the composer's LLM every tool,
    and this path has no model in the loop. ``allow_fallback=False`` for the same
    reason a deterministic binder sets it — a web-search substitute must never
    stand in for a provider identity.
    """
    tool_context = build_tool_context_from_state(state)

    async def execute(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await execute_tool(
            tool_name,
            arguments,
            allowed_tool_names=_AUTHORED_PLACE_TOOL_NAMES,
            node_name=_NODE_NAME,
            activation_source="authored_place_resolution",
            allow_fallback=False,
            **tool_context,
        )

    def has_tool(tool_name: str) -> bool:
        # Whether this deployment registered the tool at all. The gateway decides
        # whether this path may call it; the registry is the only thing that knows
        # whether it exists to be called.
        return get_tool_registry().has_tool(tool_name)

    return AuthoredPlaceToolAccess(execute=execute, has_tool=has_tool)


async def _locate_authored_places(
    composition: ItineraryCompositionDraft,
    scopes: Dict[str, AuthoredPlaceScope],
    unlocatable: set[str],
    tools: AuthoredPlaceToolAccess,
) -> tuple[ItineraryCompositionDraft, list[AuthoredPlaceBase]]:
    """Fill the resolved identity of every authored place a provider knows.

    ``unlocatable`` carries the names earlier attempts disproved and grows with
    the ones this pass disproves. Only a ladder that actually ran counts: an
    entry the composition window cut short was never searched for, so it stays
    open for the next attempt.
    """
    pending = [ref for ref in authored_places(composition) if not ref.place.is_located]
    if not pending:
        return composition, []
    located: Dict[str, AuthoredPlaceBase] = {}
    unresolved: List[AuthoredPlaceBase] = []
    for index, ref in enumerate(pending):
        place = ref.place
        key = authored_place_key(place)
        if key in located:
            continue
        if _names_disproved(place.name, place.local_name, unlocatable):
            logger.info(
                "authored place already disproved this run name=%s", place.name
            )
            unresolved.append(place)
            continue
        try:
            # The ladder walks several provider phrasings per entry, and an entry
            # no provider knows pays for every one of them. Each entry gets a
            # slice rather than the rest of the composition window, so one
            # unknown place cannot spend the budget the other entries need.
            async def _resolve_within_budget(
                place: AuthoredPlaceBase = place,
                kind: str = ref.kind,
                scope: AuthoredPlaceScope = scopes.get(
                    ref.destination_id, AuthoredPlaceScope()
                ),
            ) -> Optional[Dict[str, Any]]:
                try:
                    return await asyncio.wait_for(
                        resolve_authored_place(
                            name=place.name,
                            local_name=place.local_name,
                            city=place.city,
                            address=place.address,
                            kind=kind,
                            scope=scope,
                            tools=tools,
                        ),
                        timeout=_AUTHORED_ENTRY_BUDGET_SECONDS,
                    )
                except asyncio.TimeoutError as spent:
                    raise _AuthoredEntryBudgetSpent(place.name) from spent

            resolved = await await_model_operation(
                _resolve_within_budget(),
                operation="place.resolve_authored",
            )
        except _AuthoredEntryBudgetSpent:
            # The ladder had its full slice and produced nothing; a place every
            # provider knows answers in a few seconds.
            logger.warning(
                "authored place resolution budget spent name=%s", place.name
            )
            unlocatable |= _place_names(place)
            unresolved.append(place)
            continue
        except ModelWindowClosed as closed:
            # The window, not this entry, is what ran out; the rest go unresolved
            # so the itinerary still ships the entries that did resolve.
            logger.warning(
                "%s window closed at elapsed=%.1fs with %s authored entries unresolved",
                closed.window,
                closed.observation.elapsed_seconds,
                len(pending) - index,
            )
            unresolved.extend(item.place for item in pending[index:])
            break
        if resolved is None:
            unlocatable |= _place_names(place)
            unresolved.append(place)
            continue
        located[key] = place.model_copy(
            update={
                "resolved_place_id": resolved["place_id"],
                "resolved_address": resolved["address"],
                "resolved_latitude": resolved["latitude"],
                "resolved_longitude": resolved["longitude"],
            }
        )
    if located:
        composition = map_authored_places(
            composition,
            lambda place: located.get(authored_place_key(place), place),
        )
    return composition, unresolved


def _replacement_place_schema(original_names: List[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "replacements": {
                "type": "array",
                "minItems": len(original_names),
                "maxItems": len(original_names),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "original_name": {"type": "string", "enum": original_names},
                        "name": {"type": "string", "minLength": 1},
                        "local_name": {"type": "string", "minLength": 1},
                        "address": {"type": "string", "minLength": 1},
                        "city": {"type": "string", "minLength": 1},
                        "selection_reason": {"type": "string", "minLength": 1},
                    },
                    "required": [
                        "original_name",
                        "name",
                        "local_name",
                        "address",
                        "city",
                        "selection_reason",
                    ],
                },
            }
        },
        "required": ["replacements"],
    }


async def _request_replacement_places(
    llm: Any,
    unresolved: List[AuthoredPlaceBase],
    unlocatable: set[str],
) -> Dict[str, Dict[str, str]]:
    """Ask for one different named place per entry the map could not find.

    A replacement that names something already disproved is discarded here, so
    a round that only respells the dead entry costs no resolution budget.
    """
    originals = [
        {
            "name": place.name,
            "local_name": place.local_name,
            "city": place.city,
            "address": place.address,
        }
        for place in unresolved
    ]
    original_names = [place.name for place in unresolved]
    # Names an earlier composition attempt already disproved. Naming them keeps
    # the round from spending itself rediscovering the same dead entries.
    pending_keys = {
        _name_key(value) for place in unresolved for value in _place_names(place)
    }
    already_disproved = sorted(
        value for value in unlocatable if _name_key(value) not in pending_keys
    )
    exclusion = (
        f"\n本次运行已确认检索不到、禁止再次使用的名称："
        f"{json.dumps(already_disproved, ensure_ascii=False)}"
        if already_disproved
        else ""
    )
    response = await llm.ainvoke(
        [
            {
                "role": "system",
                "content": (
                    "你是 JourneyPilot 的行程组合器。下列地点在地点库中检索不到，"
                    "为每一个换一个同城、同类型、真实存在且能在地图上检索到的地点。"
                    "换的必须是另一个地点：改写法、换假名拼法、换繁简字形、加减分店后缀"
                    "都算同一个地点，一律不接受。"
                    "只返回替换项：local_name 逐字写当地语言的招牌原名（地点库按它收录，"
                    "字形与当地实际书写一致：中国大陆写简体，日本按招牌上的假名写法），"
                    "name 写旅行者读的常用名称，address 写街道级地址，city 保持同城，"
                    "selection_reason 一句话说明适配理由。不要解释，不要返回完整行程。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"检索不到的地点：{json.dumps(originals, ensure_ascii=False)}"
                    f"{exclusion}"
                ),
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "authored_place_replacements",
                "strict": True,
                "schema": _replacement_place_schema(original_names),
            },
        },
        temperature=0,
    )
    content = response.content if hasattr(response, "content") else response
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    payload = json.loads(content)
    items = payload.get("replacements") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("place replacement must return one replacements list")
    by_original: Dict[str, Dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        original = str(item.get("original_name") or "")
        name = str(item.get("name") or "")
        if not original or not name:
            continue
        local_name = str(item.get("local_name") or name)
        if _names_disproved(name, local_name, unlocatable):
            logger.info(
                "discarding replacement that renames a disproved place name=%s",
                name,
            )
            continue
        by_original[original] = {
            "name": name,
            "local_name": local_name,
            "address": str(item.get("address") or ""),
            "city": str(item.get("city") or ""),
            "selection_reason": str(item.get("selection_reason") or ""),
        }
    return by_original


async def locate_authored_composition(
    composition: ItineraryCompositionDraft,
    llm: Any,
    state: TravelAgentState,
    unlocatable: set[str],
) -> ItineraryCompositionDraft:
    """Return a composition whose every authored entry has a map location.

    ``unlocatable`` is the run's growing record of names no provider knows; the
    caller carries it into the state update so a composition repair attempt
    starts from what this one proved.
    """
    scopes = authored_place_scopes(state.controlled_trip_identity)
    tools = _authored_place_tool_access(state)
    composition, unresolved = await _locate_authored_places(
        composition, scopes, unlocatable, tools
    )
    for _round in range(_AUTHORED_PLACE_REPLACEMENT_ROUNDS):
        if not unresolved:
            break
        if _composition_window_closed(state):
            # Past the boundary a replacement round would spend the model call
            # that composition repair still needs.
            break
        replacements = await _request_replacement_places(llm, unresolved, unlocatable)
        by_key = {
            authored_place_key(place): replacements[place.name]
            for place in unresolved
            if place.name in replacements
        }
        if not by_key:
            break
        composition = map_authored_places(
            composition,
            lambda place: (
                place.model_copy(update=by_key[authored_place_key(place)])
                if authored_place_key(place) in by_key
                else place
            ),
        )
        composition, unresolved = await _locate_authored_places(
            composition, scopes, unlocatable, tools
        )
    if unresolved:
        # The ladder searched the local-language signage name and the provider
        # still has no such place. Deliver the rest of the itinerary without it
        # rather than losing the whole composition to one unplaceable entry.
        unresolved_keys = {authored_place_key(place) for place in unresolved}
        logger.warning(
            "dropping unlocatable authored entries: %s",
            "、".join(
                f"{place.name} (local_name={place.local_name!r}, city={place.city!r})"
                for place in unresolved
            ),
        )
        composition = drop_authored_places(
            composition,
            lambda place: authored_place_key(place) in unresolved_keys,
        )
    return composition


def _authored_connector_schema(gap_ids: List[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "connectors": {
                "type": "array",
                "minItems": len(gap_ids),
                "maxItems": len(gap_ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "gap_id": {"type": "string", "enum": gap_ids},
                        "mode": {
                            "type": "string",
                            "enum": sorted(AUTHORED_ROUTE_CLASS_BY_MODE),
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 360,
                        },
                        "selection_reason": {"type": "string", "minLength": 1},
                    },
                    "required": [
                        "gap_id",
                        "mode",
                        "duration_minutes",
                        "selection_reason",
                    ],
                },
            }
        },
        "required": ["connectors"],
    }


async def _author_connector_routes(
    llm: Any,
    gaps: List[LocalConnectorGap],
) -> Dict[str, AuthoredRoute]:
    """Write one door-to-door connector per adjacency with no Provider route."""
    gap_ids = [gap.gap_id for gap in gaps]
    described = [
        {
            "gap_id": gap.gap_id,
            "day_date": gap.day_date.isoformat(),
            "from_place_id": gap.from_place_id,
            "to_place_id": gap.to_place_id,
            "leave_after": gap.departure_time.isoformat(),
            "arrive_before": gap.latest_arrival_time.isoformat(),
            "requested_modes": gap.requested_flexible_modes,
        }
        for gap in gaps
    ]
    response = await llm.ainvoke(
        [
            {
                "role": "system",
                "content": (
                    "你是 JourneyPilot 的行程组合器。为下列相邻停留点之间各写一段市内交通："
                    "选一个出行方式，并给出门到门分钟数与一句选择理由。"
                    "分钟数必须落在给定的出发与到达时间窗内。"
                    "端点由服务器按前后两个停留点填写，你不要写地点、线路号或票价。"
                ),
            },
            {
                "role": "user",
                "content": f"待补的相邻段：{json.dumps(described, ensure_ascii=False)}",
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "authored_local_connectors",
                "strict": True,
                "schema": _authored_connector_schema(gap_ids),
            },
        },
        temperature=0,
    )
    content = response.content if hasattr(response, "content") else response
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    payload = json.loads(content)
    items = payload.get("connectors") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("connector authoring must return one connectors list")
    routes: Dict[str, AuthoredRoute] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        gap_id = str(item.get("gap_id") or "")
        if gap_id not in gap_ids or gap_id in routes:
            continue
        routes[gap_id] = AuthoredRoute(
            mode=item.get("mode"),
            duration_minutes=int(item.get("duration_minutes") or 0),
            selection_reason=str(item.get("selection_reason") or ""),
        )
    missing = [gap_id for gap_id in gap_ids if gap_id not in routes]
    if missing:
        raise ValueError(f"connector authoring skipped adjacencies: {missing}")
    return routes


def parse_itinerary_composition(raw: str) -> ItineraryCompositionDraft:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("itinerary composition must be one exact JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("itinerary composition must be one JSON object")
    _normalize_duplicate_placements(payload)
    _normalize_empty_placements(payload)
    _normalize_time_structure(payload)
    _normalize_placement_chain(payload)
    try:
        return ItineraryCompositionDraft.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"itinerary composition failed schema gate: {exc}") from exc


def _parse_exact_llm_composition(
    raw: str,
    state: TravelAgentState,
) -> ItineraryCompositionDraft:
    """The one funnel every model composition arrives through.

    Both the day-window check and the deterministic normalizers live on
    this path precisely because it is the only one: a second entry point is how a
    composition rule ends up enforced on the skeleton pass and not on the
    materialize pass.

    The departure-overlap drop is here rather than beside the payload normalizers
    for the reason its own docstring gives: ``departure_at`` lives on the admitted
    candidate, so the rule needs the catalog.  ``validate_placement_skeleton`` has
    five call sites and this funnel has one, which is why the normalizer attaches
    here and not in front of each validation.
    """
    draft = _verify_day_window(parse_itinerary_composition(raw), state)
    catalog = state.recommendation_catalog
    if catalog is None:
        return draft
    return drop_placements_the_traveller_has_already_left(draft, catalog)


def _payload_placement_key(placement: Dict[str, Any]) -> Optional[str]:
    """Mirror ``placement_identity`` on a raw, not-yet-validated placement dict."""
    candidate_id = placement.get("candidate_id")
    if isinstance(candidate_id, str) and candidate_id:
        return f"candidate:{candidate_id}"
    authored = placement.get("authored_place")
    if isinstance(authored, dict):
        name = str(authored.get("name") or "")
        city = str(authored.get("city") or "")
        if name:
            return f"authored:{city}:{name}"
    return None


def _normalize_duplicate_placements(payload: dict[str, Any]) -> None:
    """Keep the first physical/day placement when strict JSON repeats a candidate.

    JSON Schema cannot express uniqueness across placement objects or day arrays.
    The product contract already defines the deterministic resolution: preserve
    itinerary order and omit later repetitions rather than inventing a substitute.
    Everything else remains untouched for Pydantic and materialization gates.
    """
    days = payload.get("days")
    if not isinstance(days, list):
        return

    seen_physical: set[str] = set()
    for day in days:
        if not isinstance(day, dict):
            continue
        placements = day.get("placements")
        if not isinstance(placements, list):
            continue
        seen_in_day: set[str] = set()
        retained: list[Any] = []
        for placement in placements:
            if not isinstance(placement, dict):
                retained.append(placement)
                continue
            key = _payload_placement_key(placement)
            placement_kind = placement.get("placement_kind")
            if key is None:
                retained.append(placement)
                continue
            if key in seen_in_day:
                continue
            if placement_kind in {"visit", "dining"} and key in seen_physical:
                continue
            retained.append(placement)
            seen_in_day.add(key)
            if placement_kind in {"visit", "dining"}:
                seen_physical.add(key)
        day["placements"] = retained


# The authored half of each placement kind's exactly-one-origin rule.  There are
# three placement kinds and no others (``CompositionPlacement`` is the union of
# ``VisitPlacement``/``DiningPlacement``/``TransportPlacement``), and
# ``entities.itinerary_composition_v2._validate_placement_origin`` is the *same*
# rule for all three — so the deterministic half of it has to be the same for all
# three too.
_AUTHORED_PLACEMENT_FIELD_BY_KIND = {
    "visit": "authored_place",
    "dining": "authored_place",
    "transport": "authored_route",
}


def _normalize_empty_placements(payload: dict[str, Any]) -> None:
    """Drop any placement that names neither a candidate nor an authored entry.

    Every placement kind requires exactly one origin: an admitted ``candidate_id``
    **or** its authored entry (``authored_place`` for visit/dining,
    ``authored_route`` for transport).  A placement carrying neither holds no
    itinerary information at all — no name, no address, no coordinates, nothing to
    schedule, nothing to map, nothing to price.  Failing the whole draft over it
    discards an otherwise complete itinerary and spends a composition repair round:
    measured on ``trip_9f4b78e6fceb4d5b`` (上海→杭州→苏州), where round one drafted
    five days for a four-day trip and round two came back with two of these empty
    placements — two *different* model slips, one repair, ``run_failed`` with
    「旅行方案暂时无法生成」.

    **This used to cover ``transport`` only** (``_normalize_empty_transport_placements``),
    but an empty ``visit`` or ``dining`` entry is exactly as informationless and was
    still failing the whole draft.  One rule, one implementation, every kind it
    applies to.

    Only the both-absent case is deterministic.  A placement naming *both* an
    admitted candidate and an authored entry is genuinely ambiguous — which one is
    the stop? — so that one still fails the gate.
    """
    days = payload.get("days")
    if not isinstance(days, list):
        return
    for day in days:
        if not isinstance(day, dict):
            continue
        placements = day.get("placements")
        if not isinstance(placements, list):
            continue
        day["placements"] = [
            placement
            for placement in placements
            if not _placement_names_nothing(placement)
        ]


def _placement_names_nothing(placement: Any) -> bool:
    """Whether this placement carries neither of the two origins it may have."""
    if not isinstance(placement, dict):
        return False
    authored_field = _AUTHORED_PLACEMENT_FIELD_BY_KIND.get(
        str(placement.get("placement_kind") or "")
    )
    if authored_field is None:
        return False
    return (
        not placement.get("candidate_id")
        and placement.get(authored_field) is None
    )


def _normalize_time_structure(payload: dict[str, Any]) -> None:
    """Force the fixed four-block day scaffold; the model must not author it.

    ``DayComposition.time_structure`` must equal the four canonical blocks and is
    pure display scaffolding — no placement references it.  Models (even primary)
    routinely emit a truncated subset covering only the blocks they filled, which
    the strict validator rejects, discarding an otherwise complete, well-placed
    itinerary over scaffold formatting.  Overwrite it deterministically instead.
    """
    days = payload.get("days")
    if not isinstance(days, list):
        return
    for day in days:
        if isinstance(day, dict):
            day["time_structure"] = ["morning", "lunch", "afternoon", "evening"]


def _scheduled_window(
    placement: Dict[str, Any],
) -> Optional[tuple[datetime, datetime]]:
    """Read one placement's authored local window, or None when it has no schedule."""
    raw_start = placement.get("planned_start")
    raw_end = placement.get("planned_end")
    if not isinstance(raw_start, str) or not isinstance(raw_end, str):
        return None
    try:
        start = datetime.fromisoformat(raw_start)
        end = datetime.fromisoformat(raw_end)
    except ValueError:
        return None
    if start.utcoffset() is None or end.utcoffset() is None or end <= start:
        return None
    return start, end


def _normalize_placement_chain(payload: dict[str, Any]) -> None:
    """Order each day's scheduled stops and open a real transfer window between them.

    ``DayComposition`` constrains one placement's own window, while the connector
    derivation reads the day as a chain: every adjacent pair becomes a gap a
    provider route has to fit inside.  Models routinely write stops back to back,
    or out of chronological order, both of which produce a gap no route can fill.
    Rebuild the chain deterministically instead of discarding a well-placed
    itinerary: sort by start, push each later stop past the previous one's
    transfer window, compress a stay that no longer fits before local midnight,
    and drop the trailing stop when even the shortest stay does not fit.

    Unscheduled stops keep their authored slots and are left untouched.
    """
    days = payload.get("days")
    if not isinstance(days, list):
        return
    transfer = timedelta(minutes=MIN_LOCAL_TRANSFER_MINUTES)
    minimum_stay = timedelta(minutes=MIN_PHYSICAL_STAY_MINUTES)

    for day in days:
        if not isinstance(day, dict):
            continue
        placements = day.get("placements")
        raw_date = day.get("date")
        if not isinstance(placements, list) or not isinstance(raw_date, str):
            continue
        try:
            day_date = date.fromisoformat(raw_date)
        except ValueError:
            continue

        scheduled: List[tuple[int, Dict[str, Any], datetime, datetime]] = []
        for index, placement in enumerate(placements):
            if not isinstance(placement, dict):
                continue
            if placement.get("placement_kind") not in {"visit", "dining"}:
                continue
            window = _scheduled_window(placement)
            if window is None:
                continue
            scheduled.append((index, placement, window[0], window[1]))
        if len(scheduled) < 2:
            continue

        slots = [entry[0] for entry in scheduled]
        scheduled.sort(key=lambda entry: (entry[2], entry[0]))

        retained: List[Dict[str, Any]] = []
        previous_end: Optional[datetime] = None
        for _index, placement, start, end in scheduled:
            duration = end - start
            if previous_end is not None and start - previous_end < transfer:
                start = previous_end + transfer
                end = start + duration
            day_end = datetime.combine(
                day_date, time(23, 59), tzinfo=start.tzinfo
            )
            if end > day_end:
                end = day_end
                if end - start < minimum_stay:
                    # The day cannot hold this stop after the chain was opened
                    # up; it loses the stop rather than shipping an unfillable
                    # connector.  Later stops start no earlier, so they go too.
                    continue
            placement["planned_start"] = start.isoformat()
            placement["planned_end"] = end.isoformat()
            placement["duration_minutes"] = max(
                1, round((end - start).total_seconds() / 60)
            )
            retained.append(placement)
            previous_end = end

        # Reinsert the rebuilt chain into the slots it came from, keeping every
        # other placement where the composition put it.  A dropped stop vacates
        # a trailing slot, so the surviving chain fills the leading ones in order.
        filled_slots = set(slots[: len(retained)])
        merged: List[Any] = []
        chain = iter(retained)
        for index, placement in enumerate(placements):
            if index in filled_slots:
                merged.append(next(chain))
            elif index not in slots:
                merged.append(placement)
        day["placements"] = merged


# Which meal a backfilled Day prefers when the restaurant serves several. Lunch
# first keeps every backfill that was already valid byte-identical; the rest of
# the order only decides what a restaurant that cannot do lunch gets. Covering
# the whole ``DiningPlacement.meal_type`` domain is what makes ``_backfill_meal``
# total — pinned by ``test_the_backfill_meal_preference_covers_the_whole_domain``.
_BACKFILL_MEAL_PREFERENCE = ("lunch", "dinner", "breakfast", "snack", "other")


def _backfill_meal(candidate: Any) -> str:
    """A meal this restaurant actually serves.

    Inventing one is what killed a whole run: a backfilled 「lunch」 for a
    dinner-only branch cleared the skeleton gate and only blew up a phase later
    inside ``materialize_trip_workspace``, where the repair round re-authors
    local connectors and can never reach a meal type. ``meal_types`` is
    non-empty by contract, so a candidate with none of the known meals is a
    contract breach and says so rather than getting a quiet default.
    """
    for meal in _BACKFILL_MEAL_PREFERENCE:
        if meal in candidate.meal_types:
            return meal
    raise ItineraryCompositionError(
        f"admitted DiningCandidate {candidate.candidate_id} serves no known meal"
    )


def _mk_skeleton_placement(kind: str, candidate_id: str, candidate: Any) -> Dict[str, Any]:
    if kind == "transport":
        return {"placement_kind": "transport", "candidate_id": candidate_id}
    if kind == "dining":
        return {
            "placement_kind": "dining",
            "candidate_id": candidate_id,
            "duration_minutes": 60,
            "meal_type": _backfill_meal(candidate),
        }
    return {
        "placement_kind": "visit",
        "candidate_id": candidate_id,
        "duration_minutes": 90,
    }


def _selected_long_distance_anchors(
    catalog: RecommendationCatalog,
) -> List[TransportCandidate]:
    """The admitted long-distance legs the itinerary owes, one per service window.

    A round trip admits an outbound and a return leg and the traveller needs both.
    The exact-topology contract only forbids two *competing* options inside one
    provider service window, so windows are what deduplicates here; within a
    window the best-fitting admitted option wins.
    """
    candidate_index = catalog.candidate_index()
    selected: Dict[tuple[str, date, date], TransportCandidate] = {}
    for candidate_id in selected_ids_by_kind(catalog, skeleton_only=True)["transport"]:
        candidate = candidate_index[candidate_id]
        if (
            not isinstance(candidate, TransportCandidate)
            or candidate.transport_class != "long_distance"
            or candidate.departure_at is None
            or candidate.arrival_at is None
        ):
            continue
        window = (
            candidate.destination_id,
            candidate.departure_at.date(),
            candidate.arrival_at.date(),
        )
        selected.setdefault(window, candidate)
    return sorted(
        selected.values(),
        key=lambda candidate: (candidate.departure_at, candidate.candidate_id),
    )


def _anchor_service_window(
    candidate: TransportCandidate,
) -> tuple[str, date, date]:
    """The provider service window an admitted long-distance leg occupies."""
    return (
        candidate.destination_id,
        candidate.departure_at.date(),
        candidate.arrival_at.date(),
    )


def _assign_anchor_days(
    anchors: List[TransportCandidate],
    days: List[Any],
    *,
    occupied: frozenset[int] = frozenset(),
) -> Dict[int, TransportCandidate]:
    """Bind each selected anchor to the one Day its provider schedule allows.

    A Day only accepts an anchor whose provider service date and destination both
    match it, which is exactly what the skeleton Gate checks; an anchor with no
    such Day is left unplaced rather than forced onto a Day that would fail. Two
    anchors never share a Day, so a same-day turnaround keeps only the first;
    ``occupied`` additionally withholds the Days that already carry an anchor.

    A cross-night leg matches two Days.  When it does, the Day the composition
    left free wins over the one it filled with stops: an anchor now only displaces
    the stops that run into its own service window, but preferring the free Day
    still costs the itinerary the fewest of the composition's own decisions.
    """
    assigned: Dict[int, TransportCandidate] = {}
    for anchor in anchors:
        service_dates = [anchor.arrival_at.date(), anchor.departure_at.date()]
        eligible = [
            day
            for service_date in service_dates
            for day in days
            if isinstance(day, dict)
            and id(day) not in assigned
            and id(day) not in occupied
            and str(day.get("date") or "")[:10] == service_date.isoformat()
            and day.get("destination_id") == anchor.destination_id
        ]
        chosen = next(
            (day for day in eligible if not (day.get("placements") or [])),
            None,
        )
        if chosen is None:
            chosen = next(iter(eligible), None)
        if chosen is not None:
            assigned[id(chosen)] = anchor
    return assigned


def _drafted_entry_key(placement: Dict[str, Any]) -> Optional[str]:
    """``placement_identity`` of a raw drafted placement, before it is parsed."""
    authored = placement.get("authored_place")
    if isinstance(authored, dict):
        city = authored.get("city")
        name = authored.get("name")
        if not city or not name:
            return None
        return f"authored:{city}:{name}"
    candidate_id = placement.get("candidate_id")
    return f"candidate:{candidate_id}" if candidate_id else None


def _stop_clears_anchor_window(
    stop: Dict[str, Any],
    anchor: Optional[Any],
) -> bool:
    """Whether a drafted local stop can share its Day with this anchor.

    Read off the drafted dict rather than a parsed placement, because this pass
    runs before parsing.  A stop with no times cannot be shown to clear the window,
    and on a Day carrying an anchor that is enough to drop it — the alternative is
    letting it through to a Gate that will reject the whole composition.
    """

    if anchor is None or not isinstance(anchor, TransportCandidate):
        return True
    if anchor.departure_at is None or anchor.arrival_at is None:
        return True
    window = _scheduled_window(stop)
    if window is None:
        return False
    start, end = window
    if start < anchor.arrival_at and end > anchor.departure_at:
        return False
    # A stop that ends after the traveller departs but before the next leg is
    # still illegal; so is a stop too close to the departure.  The departure
    # agent must physically clear the station, and the composition's connector
    # gate requires a full transfer window before a following long-distance leg
    # (`MIN_LOCAL_TRANSFER_MINUTES`) — under-cutting it gets the *whole* day
    # rejected at composition (observed with multi-destination: a last-day
    # `visit end 10:00 -> leg depart 10:24` left only 24min and looped to
    # DeliveryContractViolation).  Product decision: a marginally-too-late local
    # stop is dropped rather than the departure day being lost.
    if end <= anchor.departure_at and (
        anchor.departure_at - end < timedelta(minutes=MIN_LOCAL_TRANSFER_MINUTES)
    ):
        return False
    return True


def _ordered_mixed_day(
    placements: List[Dict[str, Any]],
    anchor_candidate: Optional[Any],
) -> List[Dict[str, Any]]:
    """Put a mixed Day's entries in clock order, anchor included.

    A Day's placement order *is* its chain: the connector between two adjacent
    entries is derived from that order, so an arriving 08:00 leg listed after an
    afternoon stop reads as "see the temple, then board this morning's train" and
    the derived gap runs backwards.  The deterministic passes append the anchor at
    the end, which is right for a departure and wrong for an arrival, and nothing
    downstream can tell the difference — it only sees the order.

    Ordering is by the clock each entry already carries: the anchor by its provider
    departure, a stop by its authored start.  When any of them has no time the Day
    is left exactly as the composition wrote it — guessing an order is the kind of
    silent rewrite this pass exists to avoid, and an untimed stop cannot share a
    Day with an anchor anyway.
    """

    if anchor_candidate is None or getattr(anchor_candidate, "departure_at", None) is None:
        return placements
    keyed: List[tuple[datetime, int, Dict[str, Any]]] = []
    for index, placement in enumerate(placements):
        if placement.get("placement_kind") == "transport":
            keyed.append((anchor_candidate.departure_at, index, placement))
            continue
        window = _scheduled_window(placement)
        if window is None:
            return placements
        keyed.append((window[0], index, placement))
    keyed.sort(key=lambda item: (item[0], item[1]))
    return [placement for _start, _index, placement in keyed]


def _prune_illegal_drafted_placements(
    days: List[Any],
    passed: Dict[str, Any],
    anchor_ids: set[str],
) -> None:
    """Drop the drafted placements the skeleton Gate would reject, keep the rest.

    The composition owns its own placement decisions; the deterministic pass is
    only allowed to remove a placement the hard contract cannot carry: an entry
    placed twice, a candidate belonging to another destination or another domain, a
    local connector (the skeleton has none), a long-distance leg on a Day its
    provider does not serve, and a second leg inside one Day or one service window.
    Everything else survives verbatim, times included.

    A Day carrying both a long-distance leg and local stops is a shape the contract
    carries, so the leg **stays where the composition put it**.  Releasing it here for
    :func:`_place_long_distance_anchors` to re-bind to a free Day is fatal once every Day
    has an anchor: a two-day return trip has no free Day, so the leg is dropped and the
    composition fails for missing required kinds.

    What still cannot stand is a stop that runs into the leg's own service window,
    and there the leg wins: its times come from the provider, the stop's are the
    composition's own choice, so the stop is the one that goes.  A stop sharing the
    Day with no times at all goes for the same reason — on a Day half spent
    travelling, "sometime today" is not placeable.
    """
    seen: set[str] = set()
    served_windows: set[tuple[str, date, date]] = set()
    for day in days:
        if not isinstance(day, dict):
            continue
        drafted = day.get("placements")
        if not isinstance(drafted, list):
            day["placements"] = []
            continue
        kept: List[Dict[str, Any]] = []
        day_date = str(day.get("date") or "")[:10]
        anchors: list = []
        for placement in drafted:
            if not isinstance(placement, dict):
                continue
            kind = placement.get("placement_kind")
            key = _drafted_entry_key(placement)
            if key is None or key in seen:
                continue
            if isinstance(placement.get("authored_place"), dict):
                # The skeleton carries no local connectors, authored or otherwise.
                if kind == "transport":
                    continue
                seen.add(key)
                kept.append(placement)
                continue
            candidate = passed.get(str(placement.get("candidate_id") or ""))
            if candidate is None or candidate.destination_id != day.get(
                "destination_id"
            ):
                continue
            if kind == "visit":
                if not isinstance(candidate, VisitCandidate):
                    continue
            elif kind == "dining":
                if not isinstance(candidate, DiningCandidate):
                    continue
            elif kind == "transport":
                if (
                    not isinstance(candidate, TransportCandidate)
                    or candidate.candidate_id not in anchor_ids
                    or len(anchors) >= 2
                ):
                    continue
                window = _anchor_service_window(candidate)
                if window in served_windows or day_date not in {
                    candidate.departure_at.date().isoformat(),
                    candidate.arrival_at.date().isoformat(),
                }:
                    continue
                anchors.append((key, window, placement))
                continue
            else:
                continue
            seen.add(key)
            kept.append(placement)
        if anchors:
            for key, window, placement in anchors:
                seen.add(key)
                served_windows.add(window)
            anchor_candidates = [
                passed.get(str(a[2].get("candidate_id") or "")) for a in anchors
            ]
            if len(anchors) == 1:
                cleared = [
                    stop
                    for stop in kept
                    if _stop_clears_anchor_window(stop, anchor_candidates[0])
                ]
                if len(cleared) != len(kept):
                    logger.info(
                        "Composition prune: anchor day %s dropped %d local stop(s) that "
                        "did not clear the leg window | kinds=%s untimed=%d",
                        day.get("day_id") or day_date,
                        len(kept) - len(cleared),
                        sorted(
                            {
                                str(stop.get("placement_kind"))
                                for stop in kept
                                if stop not in cleared
                            }
                        ),
                        sum(
                            1
                            for stop in kept
                            if stop not in cleared and _scheduled_window(stop) is None
                        ),
                    )
                kept = cleared
                kept.append(anchors[0][2])
                kept = _ordered_mixed_day(kept, anchor_candidates[0])
            else:
                # Same-day two-long-distance-leg day: the arriving leg must
                # be first, the departing leg last, and every local stop sits
                # between them — after the arrival and before the departure.  A
                # stop that clears only one side is not placeable.
                cleared = [
                    stop
                    for stop in kept
                    if _stop_clears_anchor_window(stop, anchor_candidates[0])
                    and _stop_clears_anchor_window(stop, anchor_candidates[1])
                ]
                if len(cleared) != len(kept):
                    logger.info(
                        "Composition prune: two-anchor day %s dropped %d local "
                        "stop(s) that did not clear the arrive→depart corridor "
                        "| kinds=%s untimed=%d",
                        day.get("day_id") or day_date,
                        len(kept) - len(cleared),
                        sorted(
                            {
                                str(stop.get("placement_kind"))
                                for stop in kept
                                if stop not in cleared
                            }
                        ),
                        sum(
                            1
                            for stop in kept
                            if stop not in cleared and _scheduled_window(stop) is None
                        ),
                    )
                kept = [anchors[0][2]] + cleared + [anchors[1][2]]
        if len(kept) != len([p for p in drafted if isinstance(p, dict)]):
            logger.info(
                "Composition prune: day %s drafted %d → kept %d",
                day.get("day_id") or day_date,
                len([p for p in drafted if isinstance(p, dict)]),
                len(kept),
            )
        day["placements"] = kept


def _backfill_skeleton_placements(
    payload: Dict[str, Any],
    catalog: RecommendationCatalog,
    required_candidate_kinds: set,
    rules: List[CompositionRule],
    selection_plan: CandidateSelectionPlan,
) -> None:
    """Complete the composition's placement skeleton without rewriting it.

    Even at temperature=0 the primary model intermittently drops a required dining
    candidate or a long-distance anchor, looping the composition to the deadline
    with no Bundle.  What it does place, though, is its own orchestration decision —
    which stop belongs to which day, in what order, at what local time, and which
    authored entry fills a gap the catalog cannot — and none of that can be
    reproduced deterministically.  So this pass only ever *adds*:

    * every drafted placement survives verbatim unless the hard contract cannot
      carry it (see :func:`_prune_illegal_drafted_placements`);
    * each admitted long-distance anchor whose service window is not already served
      is appended to the Day its provider schedule allows;
    * admitted visit/dining candidates the composition left unplaced fill the Days
      it left empty;
    * an enforced kind that is still absent is placed anyway, and stays absent — a
      gate failure — when the catalog has nothing to place.

    A composition that placed nothing at all needs no separate branch: every Day is
    then empty, so the same empty-Day fill builds the whole skeleton on its day
    scaffold (dates and destinations), one candidate per Day.  That is the safety
    net, not the normal path.

    Admission, not enforcement, is what earns a candidate its place: a domain that
    passed Candidate Gate is offered a slot even when the domain is not one of the
    ``required_candidate_kinds``.  Enforcement stays exactly what it was — the
    kinds whose absence is a gate failure.
    """
    days = payload.get("days")
    if not isinstance(days, list) or not days:
        return
    passed = _passed_candidates(catalog)
    lodgings = [cid for cid, c in passed.items() if isinstance(c, LodgingCandidate)]
    anchor_ids = {
        candidate_id
        for candidate_id, candidate in passed.items()
        if isinstance(candidate, TransportCandidate)
        and candidate.transport_class == "long_distance"
        and candidate.departure_at is not None
        and candidate.arrival_at is not None
    }
    def _shape() -> str:
        return " | ".join(
            f"{day.get('day_id')}:"
            + ",".join(
                f"{p.get('placement_kind')}"
                + ("" if _scheduled_window(p) else "(untimed)")
                for p in (day.get("placements") or [])
                if isinstance(p, dict)
            )
            for day in days
            if isinstance(day, dict)
        )

    logger.info("Composition drafted: %s", _shape())
    _prune_illegal_drafted_placements(days, passed, anchor_ids)
    _place_long_distance_anchors(days, passed, catalog)
    selected, _alternatives = selected_candidate_capabilities(
        catalog=catalog,
        selection_plan=selection_plan,
    )
    _fill_remaining_placements(
        days,
        passed,
        catalog,
        required_candidate_kinds,
        rules=rules,
        capabilities=selected,
    )
    logger.info("Composition after backfill: %s", _shape())
    if lodgings and not payload.get("lodging_candidate_ids"):
        # One bed per city the traveller sleeps in, not "the first admitted one".
        # ``lodgings[0]`` shipped a single property for a whole multi-destination
        # trip (batch 4 measured a Hangzhou hostel covering the Suzhou nights of a
        # 上海→杭州→苏州 run), and the composition layer then had nothing better to
        # place.  Which nights belong to which city is already written on the Day
        # scaffold, so read it from there instead of inventing a second answer;
        # a city with no admitted property simply gets none, and the uncovered
        # night is reported by ``delivery_quality_gate``'s ``missing_lodging_night``.
        day_destinations: list[str] = []
        for day in days:
            if not isinstance(day, dict):
                continue
            destination_id = str(day.get("destination_id") or "").strip()
            if destination_id and destination_id not in day_destinations:
                day_destinations.append(destination_id)
        chosen: list[str] = []
        for destination_id in day_destinations:
            for candidate_id in lodgings:
                if passed[candidate_id].destination_id == destination_id:
                    chosen.append(candidate_id)
                    break
        payload["lodging_candidate_ids"] = chosen or [lodgings[0]]


def _log_composition_dispersion(workspace: TripWorkspaceV2) -> None:
    """Print how spread out each Day is: local connector minutes, and stop span.

    Measurement only.  Nothing here filters a candidate, moves a stop or fails a
    Day — "how far is too far" is a threshold nobody can pick before the
    distribution exists, and this line is what makes every run contribute to it.

    It belongs to the ``Composition …`` family above (``grep "Composition "``
    collects all of them) but cannot be *emitted* up there: the placement
    skeleton carries no local connectors at all — they are materialized one pass
    later — so half the measurement does not exist yet.  Which stop sits on
    which Day, which is what the span measures, is settled by the backfill above
    and left untouched by connector materialization, so the span printed here is
    the span of the composition those two lines describe.

    Coordinates come from the two layers that own them, the same pair the map
    the traveller sees is drawn from: an authored entry carries its resolved
    point on the stop, a candidate-backed stop carries verified coordinate facts
    in its lineage.  ``pts=`` reports how many of the Day's stops resolved, so a
    span computed over fewer points than the Day has cannot read as a full one.
    """
    fact_index = {
        fact.fact_assertion_id: fact
        for packet in workspace.recommendation_catalog.research_packets
        for fact in packet.fact_assertions
    }
    measured = []
    for day_id, dispersion in itinerary_dispersion(workspace.itinerary, fact_index):
        legs = f"{dispersion.local_leg_minutes}min/{dispersion.local_leg_count}legs"
        if dispersion.untimed_local_leg_count:
            legs += f"(+{dispersion.untimed_local_leg_count} untimed)"
        if dispersion.span_km is None:
            span = "span=n/a"
        else:
            pair = dispersion.farthest_pair or ("", "")
            span = f"span={dispersion.span_km:.2f}km[{pair[0]}↔{pair[1]}]"
        measured.append(
            f"{day_id}:local={legs} {span} "
            f"pts={dispersion.located_point_count}/{dispersion.point_count}"
        )
    logger.info("Composition dispersion: %s", " | ".join(measured))


def _restore_full_catalog(
    workspace: TripWorkspaceV2,
    full_catalog: RecommendationCatalog | None,
) -> TripWorkspaceV2:
    if full_catalog is None:
        return workspace
    admissions = {
        (result.candidate_id, result.selection_slot_id): result
        for result in full_catalog.admission_results
    }
    admissions.update(
        {
            (result.candidate_id, result.selection_slot_id): result
            for result in workspace.recommendation_catalog.admission_results
            if result.selection_slot_id is not None
        }
    )
    restored_catalog = full_catalog.model_copy(
        update={"admission_results": list(admissions.values())}
    )
    return workspace.model_copy(
        update={"recommendation_catalog": restored_catalog}
    )


def _anchor_day_ids(days: List[Any], passed: Dict[str, Any]) -> set[int]:
    """The Days that carry a long-distance anchor and are therefore travel-only."""
    return {
        id(day)
        for day in days
        if isinstance(day, dict)
        and any(
            isinstance(p, dict)
            and p.get("placement_kind") == "transport"
            and isinstance(
                passed.get(str(p.get("candidate_id") or "")), TransportCandidate
            )
            for p in (day.get("placements") or [])
        )
    }


def _place_long_distance_anchors(
    days: List[Any],
    passed: Dict[str, Any],
    catalog: RecommendationCatalog,
) -> None:
    """Append each unserved admitted long-distance leg to the Day it serves.

    Both legs of a round trip have to reach the traveller, so every service window
    the composition left unserved gets its leg here.

    **Append, never replace.**  Writing ``day["placements"] = [leg]`` discards whatever
    the composition put on that Day, which the topology contract explicitly allows it to
    hold — it would silently undo the loosening :func:`_prune_illegal_drafted_placements`
    grants one function above.  Nor do the discarded stops come back: they do *not* return
    to the unplaced pool for :func:`_fill_remaining_placements`, because that pass only
    fills *empty* Days and the Day just handed a leg is not empty.  On a two-day return
    trip, where both Days carry an anchor, the stops are simply lost — and the skeleton
    Gate then reports ``['dining', 'visit']`` missing.

    What the leg does displace is a stop that runs into its own service window, and
    only that: the leg's times come from the provider, the stop's are the
    composition's own choice, so the stop is the one that goes.  A stop with no
    times at all goes for the same reason — on a Day half spent travelling,
    "sometime today" is not placeable.  That is exactly
    :func:`_stop_clears_anchor_window`, the same rule the pruning pass applies, so
    the two passes cannot disagree about what a mixed Day may hold.
    """
    anchors = _selected_long_distance_anchors(catalog)
    if not anchors:
        return
    served_windows: set[tuple[str, date, date]] = set()
    for day in days:
        if not isinstance(day, dict):
            continue
        for placement in day.get("placements") or []:
            if not isinstance(placement, dict):
                continue
            candidate = passed.get(str(placement.get("candidate_id") or ""))
            if (
                isinstance(candidate, TransportCandidate)
                and candidate.transport_class == "long_distance"
                and candidate.departure_at is not None
                and candidate.arrival_at is not None
            ):
                served_windows.add(_anchor_service_window(candidate))
    pending = [
        anchor
        for anchor in anchors
        if _anchor_service_window(anchor) not in served_windows
    ]
    if not pending:
        return
    occupied = frozenset(_anchor_day_ids(days, passed))
    for day_key, anchor in _assign_anchor_days(
        pending, days, occupied=occupied
    ).items():
        for day in days:
            if isinstance(day, dict) and id(day) == day_key:
                existing = [
                    stop
                    for stop in (day.get("placements") or [])
                    if isinstance(stop, dict)
                    and _stop_clears_anchor_window(stop, anchor)
                ]
                day["placements"] = _ordered_mixed_day(
                    existing
                    + [_mk_skeleton_placement("transport", anchor.candidate_id, anchor)],
                    anchor,
                )
                break


def _fill_remaining_placements(
    days: List[Any],
    passed: Dict[str, Any],
    catalog: RecommendationCatalog,
    required_candidate_kinds: set,
    *,
    rules: List[CompositionRule],
    capabilities: list[Any],
) -> None:
    """Place the still-unplaced selected candidates on the Days that have room.

    Room means an empty Day, and the exact-connector contract is what makes it mean
    that: a deterministically placed entry carries no local time, and two adjacent
    stops without times have no connector window, which
    :func:`extract_local_connector_gaps` rejects outright.  So an entry can only be
    added where it becomes the Day's single stop; deciding times for a Day the
    composition already scheduled is the composition's job, not this pass's.

    Each empty Day takes one destination-matched candidate, preferring an uncovered
    enforced kind, then an uncovered selected one, so no domain is starved before
    another gets a second entry.  This is also the whole safety net for a draft that
    placed nothing: every Day is then empty, so the same pass rebuilds the skeleton
    end to end.  An enforced kind still absent afterwards is placed regardless, and
    stays absent — a gate failure — when the catalog has nothing left to place.
    """
    placed_ids = {
        str(p.get("candidate_id"))
        for day in days
        if isinstance(day, dict)
        for p in (day.get("placements") or [])
        if isinstance(p, dict) and p.get("candidate_id")
    }
    selected = [
        capability
        for capability in capabilities
        if capability.candidate_kind in {"visit", "dining"}
        and capability.candidate_id in passed
    ]
    covered: set = {
        p.get("placement_kind")
        for day in days
        if isinstance(day, dict)
        for p in (day.get("placements") or [])
        if isinstance(p, dict)
    } & {"visit", "dining"}
    required = set(required_candidate_kinds or set())
    # Admission is the entitlement to a slot; enforcement only decides whose
    # absence fails the run.  Both physical domains that cleared Candidate Gate
    # are therefore offered a day before any domain gets a second one.
    admitted_kinds = {item.candidate_kind for item in selected}
    anchor_day_ids = _anchor_day_ids(days, passed)
    fill_days = [
        day
        for day in days
        if isinstance(day, dict) and id(day) not in anchor_day_ids
    ]

    def take(day: Dict[str, Any], wants: tuple[str, ...]) -> Optional[str]:
        slots = build_legal_open_slots({"days": [day]}, rules)
        if not slots or slots[0].remaining_capacity <= 0:
            return None
        slot = slots[0]
        avail = order_backfill_candidates(
            [item for item in selected if item.candidate_id not in placed_ids],
            slot,
        )
        for want in wants:
            candidate = next(
                (item for item in avail if item.candidate_kind == want),
                None,
            )
            if candidate is not None:
                kind = candidate.candidate_kind
                cid = candidate.candidate_id
                day.setdefault("placements", []).append(
                    _mk_skeleton_placement(kind, cid, passed[cid])
                )
                placed_ids.add(cid)
                covered.add(kind)
                return kind
        return None

    def priorities() -> tuple[str, ...]:
        return (
            *sorted(required - covered),
            *sorted(admitted_kinds - required - covered),
            *sorted(admitted_kinds),
        )

    # Pass 1: one destination-matched candidate for every Day still empty.
    for day in fill_days:
        if not (day.get("placements") or []):
            take(day, priorities())

    # Pass 2: guarantee every enforced kind appears somewhere.  An empty Day is
    # where it can go without inventing a connector window; only when none is left
    # does it join the least crowded Day, which is what the previous pass did too.
    for want in sorted(required - covered):
        for day in sorted(fill_days, key=lambda d: len(d.get("placements") or [])):
            if take(day, (want,)) is not None:
                break

    # Pass 3: a Day the composition left empty that the pool cannot fill is still a
    # contract breach — every Day carries at least one entry.  The minimal repair is
    # to move one entry off the fullest Day of the same destination instead of
    # inventing or duplicating one.  Its local schedule belonged to the Day it left,
    # so the move carries the entry and not the clock.
    for day in fill_days:
        if day.get("placements") or []:
            continue
        donor = max(
            (
                other
                for other in fill_days
                if other is not day
                and other.get("destination_id") == day.get("destination_id")
                and len(other.get("placements") or []) > 1
            ),
            key=lambda other: len(other["placements"]),
            default=None,
        )
        if donor is None:
            continue
        moved = donor["placements"].pop()
        moved.pop("planned_start", None)
        moved.pop("planned_end", None)
        day["placements"] = [moved]


def _apply_skeleton_backfill(
    content: str,
    catalog: RecommendationCatalog,
    required_candidate_kinds: set,
    *,
    rules: List[CompositionRule],
    selection_plan: CandidateSelectionPlan,
    generation_id: str,
) -> tuple[str, list[CompositionMutation]]:
    """Parse, deterministically backfill required skeleton placements, re-serialize."""
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content, []
    if not isinstance(payload, dict):
        return content, []
    before = copy.deepcopy(payload)
    _backfill_skeleton_placements(
        payload,
        catalog,
        required_candidate_kinds,
        rules,
        selection_plan,
    )
    selected, _alternatives = selected_candidate_capabilities(
        catalog=catalog,
        selection_plan=selection_plan,
    )
    intent_ids = {
        capability.candidate_id: capability.matched_intent_ids
        for capability in selected
    }
    mutations = diff_composition_mutations(
        before=before,
        after=payload,
        generation_id=generation_id,
        reason_code="slot_aware_skeleton_closeout",
        created_by="slot_backfill",
        intent_ids_by_entity=intent_ids,
        rule_ids=[rule.rule_id for rule in rules],
    )
    return json.dumps(payload, ensure_ascii=False), mutations


def _anchor_move_response_schema(
    *,
    candidate_ids: list[str],
    from_day_ids: list[str],
    to_day_ids: list[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "moves": {
                "type": "array",
                "minItems": len(to_day_ids),
                "maxItems": len(to_day_ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "candidate_id": {"type": "string", "enum": candidate_ids},
                        "from_day_id": {"type": "string", "enum": from_day_ids},
                        "to_day_id": {"type": "string", "enum": to_day_ids},
                    },
                    "required": ["candidate_id", "from_day_id", "to_day_id"],
                },
            }
        },
        "required": ["moves"],
    }


def _apply_anchor_moves(
    payload: dict[str, Any],
    *,
    moves: list[dict[str, Any]],
    anchorless_day_ids: list[str],
    catalog: RecommendationCatalog,
) -> dict[str, Any]:
    repaired = copy.deepcopy(payload)
    _normalize_duplicate_placements(repaired)
    days = {
        str(day.get("day_id") or ""): day
        for day in repaired.get("days", [])
        if isinstance(day, dict)
    }
    target_ids = [str(move.get("to_day_id") or "") for move in moves]
    candidate_ids = [str(move.get("candidate_id") or "") for move in moves]
    if sorted(target_ids) != sorted(anchorless_day_ids) or len(target_ids) != len(
        set(target_ids)
    ):
        raise ValueError("anchor repair must cover every anchorless day exactly once")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("anchor repair cannot move one candidate more than once")

    candidate_index = catalog.candidate_index()
    for move in moves:
        candidate_id = str(move.get("candidate_id") or "")
        from_day_id = str(move.get("from_day_id") or "")
        to_day_id = str(move.get("to_day_id") or "")
        source = days.get(from_day_id)
        target = days.get(to_day_id)
        candidate = candidate_index.get(candidate_id)
        if source is None or target is None or source is target:
            raise ValueError("anchor repair references an invalid day move")
        if not isinstance(candidate, (VisitCandidate, DiningCandidate)):
            raise ValueError("anchor repair may only move Visit/Dining candidates")
        if candidate.destination_id != str(target.get("destination_id") or ""):
            raise ValueError("anchor repair candidate belongs to another destination")
        source_placements = source.get("placements")
        target_placements = target.get("placements")
        if not isinstance(source_placements, list) or not isinstance(
            target_placements, list
        ):
            raise ValueError("anchor repair requires typed day placements")
        matching = [
            placement
            for placement in source_placements
            if isinstance(placement, dict)
            and placement.get("candidate_id") == candidate_id
            and placement.get("placement_kind") in {"visit", "dining"}
        ]
        remaining_physical = [
            placement
            for placement in source_placements
            if isinstance(placement, dict)
            and placement.get("placement_kind") in {"visit", "dining"}
            and placement.get("candidate_id") != candidate_id
        ]
        if len(matching) != 1 or not remaining_physical:
            raise ValueError("anchor repair must move from a day with spare physical anchors")
        if any(
            isinstance(placement, dict)
            and placement.get("placement_kind") in {"visit", "dining"}
            for placement in target_placements
        ):
            raise ValueError("anchor repair target already has a physical anchor")

        moved = copy.deepcopy(matching[0])
        moved_index = next(
            index
            for index, placement in enumerate(source_placements)
            if placement is matching[0]
        )
        physical_indexes = [
            index
            for index, placement in enumerate(source_placements)
            if isinstance(placement, dict)
            and placement.get("placement_kind") in {"visit", "dining"}
        ]
        if moved_index == physical_indexes[-1]:
            previous_physical_index = physical_indexes[-2]
            retained_source_placements = source_placements[
                : previous_physical_index + 1
            ]
        elif moved_index == physical_indexes[0]:
            next_physical_index = physical_indexes[1]
            retained_source_placements = source_placements[next_physical_index:]
        else:
            raise ValueError(
                "anchor repair can only move a boundary physical candidate"
            )
        target_date = date.fromisoformat(str(target.get("date") or ""))
        for field_name in ("planned_start", "planned_end"):
            raw_value = moved.get(field_name)
            if raw_value is None:
                continue
            parsed = datetime.fromisoformat(str(raw_value))
            moved[field_name] = parsed.replace(
                year=target_date.year,
                month=target_date.month,
                day=target_date.day,
            ).isoformat()
        source["placements"] = retained_source_placements
        # An anchorless day cannot legally retain local transport on its own.
        # Those edge routes were composed around the duplicate that was just
        # removed, so keep no dangling endpoint when placing the moved anchor.
        target_placements.clear()
        target_placements.append(moved)
    return repaired


def _anchor_move_scope(
    payload: dict[str, Any],
    catalog: RecommendationCatalog,
) -> tuple[dict[str, Any], list[str], list[str], list[str], list[dict[str, Any]]]:
    """Describe an exact move-only repair for physical-anchor coverage.

    The model may choose which already-admitted boundary stop moves, but the
    server derives every legal source/target/candidate option from the returned
    placement payload.  No place, route, date, or Provider field is invented.
    """

    normalized = copy.deepcopy(payload)
    _normalize_duplicate_placements(normalized)
    candidate_index = catalog.candidate_index()
    physical_occurrences: dict[str, list[str]] = {}
    first_physical_day: dict[str, str] = {}
    physical_anchor_counts: dict[str, int] = {}
    boundary_physical_candidate_ids: set[str] = set()
    long_distance_days: set[str] = set()

    for day in normalized.get("days", []):
        if not isinstance(day, dict):
            continue
        day_id = str(day.get("day_id") or "")
        placements = day.get("placements")
        if not isinstance(placements, list):
            continue
        day_physical_ids = [
            str(placement.get("candidate_id") or "")
            for placement in placements
            if isinstance(placement, dict)
            and placement.get("placement_kind") in {"visit", "dining"}
            and placement.get("candidate_id")
        ]
        if day_physical_ids:
            boundary_physical_candidate_ids.update(
                (day_physical_ids[0], day_physical_ids[-1])
            )
        for candidate_id in day_physical_ids:
            physical_occurrences.setdefault(candidate_id, []).append(day_id)
            if candidate_id not in first_physical_day:
                first_physical_day[candidate_id] = day_id
                physical_anchor_counts[day_id] = (
                    physical_anchor_counts.get(day_id, 0) + 1
                )
        for placement in placements:
            if not isinstance(placement, dict):
                continue
            # An authored stop anchors its day just as a candidate one does; it
            # is simply not movable, so it never joins the occurrence index.
            if placement.get("placement_kind") in {"visit", "dining"} and isinstance(
                placement.get("authored_place"), dict
            ):
                physical_anchor_counts[day_id] = (
                    physical_anchor_counts.get(day_id, 0) + 1
                )
            candidate = candidate_index.get(str(placement.get("candidate_id") or ""))
            if (
                isinstance(candidate, TransportCandidate)
                and candidate.transport_class == "long_distance"
            ):
                long_distance_days.add(day_id)

    day_ids = [
        str(day.get("day_id") or "")
        for day in normalized.get("days", [])
        if isinstance(day, dict)
    ]
    anchorless_day_ids = [
        day_id
        for day_id in day_ids
        if not physical_anchor_counts.get(day_id)
        and day_id not in long_distance_days
    ]
    overfilled_day_ids = [
        day_id
        for day_id in day_ids
        if physical_anchor_counts.get(day_id, 0) > 1
    ]
    movable_candidate_ids = sorted(
        candidate_id
        for candidate_id, owner_day_id in first_physical_day.items()
        if owner_day_id in overfilled_day_ids
        and candidate_id in boundary_physical_candidate_ids
        and len(physical_occurrences.get(candidate_id, [])) == 1
    )
    eligible_candidates = [
        {
            "candidate_id": candidate_id,
            "candidate_kind": candidate_index[candidate_id].candidate_kind,
            "current_day_ids": physical_occurrences.get(candidate_id, []),
        }
        for candidate_id in movable_candidate_ids
        if candidate_id in candidate_index
    ]
    return (
        normalized,
        anchorless_day_ids,
        overfilled_day_ids,
        movable_candidate_ids,
        eligible_candidates,
    )


def _draft_user_input_anchors(state: TravelAgentState) -> List[UserInputAnchor]:
    """Carry the Draft's controlled anchors into the composed Workspace.

    The report projection resolves every itinerary destination id to its public
    name through these anchors, and the cost summary reads the budget cap from
    them.  Both are authored once in the minimum delivery draft.
    """
    draft = state.minimum_delivery_draft
    if draft is None:
        return []
    return list(draft.user_input_anchors)


def _drop_day_boundary_authored_connectors(
    composition: ItineraryCompositionDraft,
) -> ItineraryCompositionDraft:
    """Remove authored connectors that sit outside any stop-to-stop adjacency.

    An authored route carries no endpoints of its own — the server fills them
    from the two stops around it.  A Day opens and closes at the traveller's
    lodging, which is not a composition placement, so a leading or trailing
    authored connector has only one adjacent stop and no derivable route.  It is
    dropped rather than failing the Day: no candidate, stop, time, or source fact
    changes.  Candidate-backed routes keep their provider endpoints and stay.
    """
    updated_days = []
    changed = False
    for day in composition.days:
        physical_indexes = [
            index
            for index, placement in enumerate(day.placements)
            if placement.placement_kind in {"visit", "dining"}
        ]
        if not physical_indexes:
            updated_days.append(day)
            continue
        first, last = physical_indexes[0], physical_indexes[-1]
        retained = [
            placement
            for index, placement in enumerate(day.placements)
            if not (
                (index < first or index > last)
                and placement.placement_kind == "transport"
                and placement.authored_route is not None
            )
        ]
        if len(retained) != len(day.placements):
            changed = True
            logger.info(
                "ItineraryPlanner dropped %d day-boundary authored connector(s) day=%s",
                len(day.placements) - len(retained),
                day.day_id,
            )
            updated_days.append(day.model_copy(update={"placements": retained}))
            continue
        updated_days.append(day)
    if not changed:
        return composition
    return composition.model_copy(update={"days": updated_days})


def _isolate_incompatible_long_distance_days(
    composition: ItineraryCompositionDraft,
    catalog: RecommendationCatalog,
) -> ItineraryCompositionDraft:
    """Keep an admitted long-distance anchor without rewriting incompatible endpoints.

    A model can place a flight and an unrelated city connector before a physical
    stop because airport identities from separate providers do not share a stable
    place id.  The long-distance leg is still a valid fixed plan anchor.  When the
    mixed Day fails the exact topology gate, deterministically retain the already
    selected long-distance leg as a travel-only Day and omit the incompatible
    optional placements.  No candidate, endpoint, segment, time, or source fact is
    changed.

    **Materialized compositions only — never the placement skeleton.**  A skeleton
    carries no local connectors by contract, so on a mixed Day
    ``validate_itinerary_transport_topology`` always reports "transport chain origin does
    not match the preceding stop" (the leg departs from a station and the stop before it
    does not), and this pass would answer by discarding the stops.  Three-day trips hide
    it — their Days are either anchor-only or stops-only, so no probe sees a mixed Day —
    while a two-day return trip has an anchor on both Days and loses every stop it has.
    The stop-to-platform hop is a connector gap that
    ``entities.itinerary_composition_v2.day_connector_adjacencies`` emits, so by the time
    this pass runs on a materialized composition the connector is there to judge.
    """
    candidate_index = catalog.candidate_index()
    updated_days = []
    changed = False
    for day in composition.days:
        long_distance_placements = [
            placement
            for placement in day.placements
            if placement.placement_kind == "transport"
            and isinstance(
                candidate_index.get(placement.candidate_id),
                TransportCandidate,
            )
            and candidate_index[placement.candidate_id].transport_class
            == "long_distance"
        ]
        has_additional_placements = len(day.placements) > len(
            long_distance_placements
        )
        if not long_distance_placements or not has_additional_placements:
            updated_days.append(day)
            continue

        day_probe = ItineraryCompositionDraft(
            itinerary_id=composition.itinerary_id,
            title=composition.title,
            duration_days=1,
            days=[day.model_copy(update={"day": 1})],
            lodging_candidate_ids=[],
        )
        try:
            validate_itinerary_transport_topology(day_probe, catalog)
        except ValueError:
            isolated_day = day.model_copy(
                update={"placements": [long_distance_placements[0]]}
            )
            updated_days.append(isolated_day)
            changed = True
        else:
            updated_days.append(day)
    if not changed:
        return composition
    return composition.model_copy(update={"days": updated_days})


def _composition_phase(state: TravelAgentState) -> str:
    """Which of the two composition passes this entry owes, read off the state.

    The passes hand over through the state they produce, not through a message
    one gate has to remember to write: no skeleton yet means the first pass, a
    skeleton with nothing materialized means the second, and a workspace already
    in hand means this is a repair and the whole composition is rewritten.
    """
    if state.trip_workspace_v2 is not None:
        return "recompose"
    if state.placement_skeleton is None:
        return "placement_skeleton"
    return "materialize_connectors"


async def itinerary_planner_node(
    state: TravelAgentState, config: RunnableConfig
) -> Dict[str, Any]:
    full_catalog = state.recommendation_catalog
    if full_catalog is not None:
        if state.candidate_selection_plan is None:
            raise ValueError("itinerary composition requires a Candidate Selection Plan")
        state = state.model_copy(
            update={
                "recommendation_catalog": catalog_for_candidate_selection(
                    full_catalog,
                    state.candidate_selection_plan,
                )
            }
        )
    output_key, assignment = resolve_agent_assignment(
        state.agent_assignments or {}, _NODE_NAME
    )
    task_desc = str(assignment["objective"])
    required_candidate_kinds = {
        str(kind)
        for kind in assignment.get("required_candidate_kinds", [])
        if str(kind) in {"visit", "dining"}
    }
    phase = _composition_phase(state)
    unlocatable = {key for key in state.unlocatable_authored_places if key}
    # Names the failing stage for the repair round. Workspace materialization
    # runs outside the in-node repair, so it is recorded apart from the
    # composition pass that produced the draft it rejected.
    stage = phase
    # Multi-destination handover legs are authoritative: the skeleton
    # must keep a travel day for every required long-distance leg (outbound,
    # each inter_destination move, return), pinned to the deterministic
    # per-city handover dates decided in the first wave.
    required_long_distance_legs = build_required_long_distance_legs(
        state.controlled_trip_identity or {},
        cross_day_return_required=explicit_cross_day_return_required(
            state.user_query or ""
        ),
    )
    composition_rules = _composition_rules(state)
    selected_capabilities, _alternative_capabilities = selected_candidate_capabilities(
        catalog=state.recommendation_catalog,
        selection_plan=state.candidate_selection_plan,
    )
    new_mutations: list[CompositionMutation] = []

    try:
        deadline_update: Dict[str, Any] = {}
        if state.run_deadline is not None:
            observed_deadline, observation = observe_run_deadline(state.run_deadline)
            deadline_update["run_deadline"] = observed_deadline
            if observation.phase == "expired":
                return {
                    **deadline_update,
                    "agent_status": {output_key: "failed"},
                    "last_error": "delivery deadline elapsed before itinerary composition",
                    "composition_failure_context": bounded_repair_context(
                        stage,
                        "delivery deadline elapsed before itinerary composition",
                    ),
                }
        if phase == "placement_skeleton":
            if state.recommendation_catalog is None:
                raise ValueError(
                    "placement skeleton requires an admitted Recommendation Catalog"
                )
            system_content = inject_agent_context(
                _composition_prompt(
                    state,
                    task_desc,
                    skeleton_only=True,
                    required_candidate_kinds=required_candidate_kinds,
                ),
                state,
                agent_label=_NODE_NAME,
            )
            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": system_content}
            ]
            append_recent_history(messages, state, limit=4)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "只返回不含市内交通的 ItineraryCompositionDraft JSON skeleton。"
                    ),
                }
            )
            llm = get_model_router().get_primary()
            response_kwargs = {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "itinerary_placement_skeleton",
                        "strict": True,
                        "schema": _composition_response_schema(
                            state.recommendation_catalog,
                            skeleton_only=True,
                        ),
                    },
                },
                "temperature": 0,
            }
            response = await llm.ainvoke(messages, **response_kwargs)
            content = response.content if hasattr(response, "content") else response
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            content, backfill_mutations = _apply_skeleton_backfill(
                content,
                state.recommendation_catalog,
                required_candidate_kinds,
                rules=composition_rules,
                selection_plan=state.candidate_selection_plan,
                generation_id=state.intent_spec.generation_id,
            )
            new_mutations.extend(backfill_mutations)
            try:
                skeleton = _parse_exact_llm_composition(content, state)
                skeleton = await locate_authored_composition(
                    skeleton, llm, state, unlocatable
                )
                validate_placement_skeleton(
                    skeleton,
                    state.recommendation_catalog,
                    required_candidate_kinds=required_candidate_kinds,
                    required_long_distance_legs=required_long_distance_legs,
                    required_leg_scope_ids=_required_leg_scope_ids(
                        required_long_distance_legs,
                        state.run_id,
                        state.constraint_pack_revision,
                    ),
                )
            except ValueError as initial_error:
                try:
                    payload = json.loads(content)
                except (TypeError, json.JSONDecodeError):
                    raise initial_error
                if not isinstance(payload, dict):
                    raise initial_error
                (
                    normalized_payload,
                    anchorless_day_ids,
                    overfilled_day_ids,
                    movable_candidate_ids,
                    eligible_candidates,
                ) = _anchor_move_scope(payload, state.recommendation_catalog)
                if (
                    anchorless_day_ids
                    and overfilled_day_ids
                    and len(movable_candidate_ids) >= len(anchorless_day_ids)
                ):
                    move_schema = _anchor_move_response_schema(
                        candidate_ids=movable_candidate_ids,
                        from_day_ids=overfilled_day_ids,
                        to_day_ids=anchorless_day_ids,
                    )
                    move_messages = [
                        messages[0],
                        {
                            "role": "user",
                            "content": (
                                "placement skeleton 中存在无物理锚点的旅行日。"
                                "你只选择把哪些已准入且尚未重复的 Visit/Dining 从拥有多个物理锚点的 Day 移到空缺 Day；"
                                "每个目标 Day 恰好一个 move，每个 candidate 只能移动一次。"
                                "不得返回完整行程，不得复制候选，不得新增交通或改写现实字段。"
                                f"可移动候选：{json.dumps(eligible_candidates, ensure_ascii=False)}；"
                                f"无锚点 Day：{json.dumps(anchorless_day_ids, ensure_ascii=False)}；"
                                f"来源 Day：{json.dumps(overfilled_day_ids, ensure_ascii=False)}；"
                                f"允许移动的 candidate_id：{json.dumps(movable_candidate_ids, ensure_ascii=False)}。"
                            ),
                        },
                    ]
                    moved = await llm.ainvoke(
                        move_messages,
                        response_format={
                            "type": "json_schema",
                            "json_schema": {
                                "name": "itinerary_skeleton_anchor_move_decision",
                                "strict": True,
                                "schema": move_schema,
                            },
                        },
                        temperature=0,
                    )
                    moved_content = moved.content if hasattr(moved, "content") else moved
                    if not isinstance(moved_content, str):
                        moved_content = json.dumps(moved_content, ensure_ascii=False)
                    decision = json.loads(moved_content)
                    if not isinstance(decision, dict) or not isinstance(
                        decision.get("moves"), list
                    ):
                        raise ValueError("skeleton anchor repair must return one moves object")
                    repaired_payload = _apply_anchor_moves(
                        normalized_payload,
                        moves=decision["moves"],
                        anchorless_day_ids=anchorless_day_ids,
                        catalog=state.recommendation_catalog,
                    )
                    new_mutations.extend(
                        diff_composition_mutations(
                            before=normalized_payload,
                            after=repaired_payload,
                            generation_id=state.intent_spec.generation_id,
                            reason_code="anchor_coverage_repair",
                            created_by="composition_repair",
                            intent_ids_by_entity={
                                item.candidate_id: item.matched_intent_ids
                                for item in selected_capabilities
                            },
                            rule_ids=[rule.rule_id for rule in composition_rules],
                        )
                    )
                    skeleton = _parse_exact_llm_composition(
                        json.dumps(repaired_payload, ensure_ascii=False),
                        state,
                    )
                else:
                    repair_messages = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "上一个 exact JSON 未通过 placement skeleton Gate。"
                                "只修正 candidate 选择、Day、顺序和当地时间；"
                                "不得新增事实，不得加入 public_transit/flexible，"
                                f"错误：{initial_error}"
                            ),
                        },
                    ]
                    repaired = await llm.ainvoke(repair_messages, **response_kwargs)
                    repaired_content = (
                        repaired.content if hasattr(repaired, "content") else repaired
                    )
                    if not isinstance(repaired_content, str):
                        repaired_content = json.dumps(
                            repaired_content,
                            ensure_ascii=False,
                        )
                    skeleton = _parse_exact_llm_composition(repaired_content, state)
                skeleton = await locate_authored_composition(
                    skeleton, llm, state, unlocatable
                )
                validate_placement_skeleton(
                    skeleton,
                    state.recommendation_catalog,
                    required_candidate_kinds=required_candidate_kinds,
                    required_long_distance_legs=required_long_distance_legs,
                    required_leg_scope_ids=_required_leg_scope_ids(
                        required_long_distance_legs,
                        state.run_id,
                        state.constraint_pack_revision,
                    ),
                )
            assert_never_violate_rules(
                skeleton.model_dump(mode="json"),
                composition_rules,
                selected_capabilities,
            )
            flexible_requests, required_flexible_pairs = (
                connector_mode_requests_from_constraint_pack(
                    skeleton,
                    state.constraint_pack,
                )
            )
            gaps = extract_local_connector_gaps(
                skeleton,
                state.recommendation_catalog,
                weather_data_revision=state.recommendation_catalog.weather_data_revision,
                flexible_mode_requests=flexible_requests,
                required_flexible_mode_pairs=required_flexible_pairs,
                required_candidate_kinds=required_candidate_kinds,
            )
            # The skeleton is half a composition: staying non-terminal is what
            # brings the Dispatcher back here for the materialization pass.
            return {
                "messages": [AIMessage(content="行程 placement skeleton 已完成")],
                "agent_status": {output_key: "pending"},
                "placement_skeleton": skeleton,
                "local_connector_gaps": gaps,
                "candidate_gate_status": "needs_research",
                "candidate_gate_route": "candidate_gate",
                "unlocatable_authored_places": sorted(unlocatable),
                "composition_failure_context": None,
                "composition_mutations": mark_mutations_revalidated(new_mutations),
            }

        if phase == "materialize_connectors":
            if (
                state.candidate_gate_status != "passed"
                or state.recommendation_catalog is None
                or state.placement_skeleton is None
            ):
                raise ValueError(
                    "connector materialization requires a passed Gate and placement skeleton"
                )
            flexible_requests, required_flexible_pairs = (
                connector_mode_requests_from_constraint_pack(
                    state.placement_skeleton,
                    state.constraint_pack,
                )
            )
            gaps = extract_local_connector_gaps(
                state.placement_skeleton,
                state.recommendation_catalog,
                weather_data_revision=state.recommendation_catalog.weather_data_revision,
                flexible_mode_requests=flexible_requests,
                required_flexible_mode_pairs=required_flexible_pairs,
                required_candidate_kinds=required_candidate_kinds,
            )
            unfilled = unfilled_connector_gaps(state.recommendation_catalog, gaps)
            authored_routes = (
                await _author_connector_routes(
                    get_model_router().get_primary(),
                    unfilled,
                )
                if unfilled
                else {}
            )
            composition = materialize_skeleton_connectors(
                state.placement_skeleton,
                state.recommendation_catalog,
                gaps,
                authored_routes,
            )
            new_mutations.extend(
                diff_composition_mutations(
                    before=state.placement_skeleton.model_dump(mode="json"),
                    after=composition.model_dump(mode="json"),
                    generation_id=state.intent_spec.generation_id,
                    reason_code="connector_materialization",
                    created_by="composition_repair",
                    intent_ids_by_entity={
                        item.candidate_id: item.matched_intent_ids
                        for item in selected_capabilities
                    },
                    rule_ids=[rule.rule_id for rule in composition_rules],
                )
            )
            assert_never_violate_rules(
                composition.model_dump(mode="json"),
                composition_rules,
                selected_capabilities,
            )
            stage = f"{phase}/workspace_materialization"
            workspace = materialize_trip_workspace(
                run_id=state.run_id,
                workspace_revision=0,
                composition=composition,
                catalog=state.recommendation_catalog,
                intent_contract_snapshot=IntentContractSnapshot.from_intent_spec(
                    state.intent_spec
                ),
                candidate_selection_plan=state.candidate_selection_plan,
                composition_mutations=[
                    *state.composition_mutations,
                    *mark_mutations_revalidated(new_mutations),
                ],
                user_input_anchors=_draft_user_input_anchors(state),
                reference_services=list(state.provider_reference_services),
            )
            workspace = _restore_full_catalog(workspace, full_catalog)
            _log_composition_dispersion(workspace)
            return {
                "messages": [AIMessage(content="精确相邻路线已物化为正式行程")],
                "agent_status": {output_key: "completed"},
                "trip_workspace_v2": workspace,
                "recommendation_catalog": workspace.recommendation_catalog,
                "local_connector_gaps": gaps,
                "unlocatable_authored_places": sorted(unlocatable),
                "composition_failure_context": None,
                "composition_mutations": mark_mutations_revalidated(new_mutations),
            }

        if state.candidate_gate_status != "passed" or state.recommendation_catalog is None:
            raise ValueError("candidate gate must pass before itinerary composition")
        system_content = inject_agent_context(
            _composition_prompt(
                state,
                task_desc,
                required_candidate_kinds=required_candidate_kinds,
            ),
            state,
            agent_label=_NODE_NAME,
        )
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
        append_recent_history(messages, state, limit=4)
        messages.append(
            {
                "role": "user",
                "content": "只返回符合 ItineraryCompositionDraft 的 JSON 对象。",
            }
        )
        llm = get_model_router().get_primary()
        response_schema = _composition_response_schema(state.recommendation_catalog)
        response_kwargs = {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "itinerary_composition_draft",
                    "strict": True,
                    "schema": response_schema,
                },
            },
            "temperature": 0,
        }
        response = await llm.ainvoke(messages, **response_kwargs)
        content = response.content if hasattr(response, "content") else response
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        try:
            composition = _parse_exact_llm_composition(content, state)
            composition = await locate_authored_composition(
                composition, llm, state, unlocatable
            )
            before_postprocessing = composition.model_dump(mode="json")
            composition = _drop_day_boundary_authored_connectors(composition)
            composition = _isolate_incompatible_long_distance_days(
                composition,
                state.recommendation_catalog,
            )
            new_mutations.extend(
                diff_composition_mutations(
                    before=before_postprocessing,
                    after=composition.model_dump(mode="json"),
                    generation_id=state.intent_spec.generation_id,
                    reason_code="deterministic_topology_repair",
                    created_by="deterministic_pruner",
                    intent_ids_by_entity={
                        item.candidate_id: item.matched_intent_ids
                        for item in selected_capabilities
                    },
                    rule_ids=[rule.rule_id for rule in composition_rules],
                )
            )
            validate_itinerary_transport_topology(
                composition, state.recommendation_catalog
            )
        except ValueError as initial_error:
            # Non-JSON/Markdown is never a repair input. A syntactically exact
            # object may receive one bounded decision-only repair, then must pass
            # the same Pydantic and deterministic materialization gates.
            try:
                repair_input = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                raise initial_error
            if not isinstance(repair_input, dict):
                raise initial_error
            duplicate_placements = []
            physical_occurrences: dict[str, list[str]] = {}
            first_physical_day: dict[str, str] = {}
            physical_anchor_counts: dict[str, int] = {}
            long_distance_days: set[str] = set()
            boundary_physical_candidate_ids: set[str] = set()
            candidate_index = state.recommendation_catalog.candidate_index()
            for day in repair_input.get("days", []):
                if not isinstance(day, dict):
                    continue
                day_id = str(day.get("day_id") or "")
                placement_ids = [
                    str(placement.get("candidate_id") or "")
                    for placement in day.get("placements", [])
                    if isinstance(placement, dict)
                ]
                duplicate_placements.extend(
                    {
                        "day_id": str(day.get("day_id") or ""),
                        "candidate_id": candidate_id,
                    }
                    for candidate_id in sorted(set(placement_ids))
                    if candidate_id and placement_ids.count(candidate_id) > 1
                )
                day_physical_ids = [
                    str(placement.get("candidate_id") or "")
                    for placement in day.get("placements", [])
                    if isinstance(placement, dict)
                    and placement.get("placement_kind") in {"visit", "dining"}
                    and placement.get("candidate_id")
                ]
                if day_physical_ids:
                    boundary_physical_candidate_ids.update(
                        (day_physical_ids[0], day_physical_ids[-1])
                    )
                for placement in day.get("placements", []):
                    if not isinstance(placement, dict):
                        continue
                    candidate_id = str(placement.get("candidate_id") or "")
                    placement_kind = placement.get("placement_kind")
                    if placement_kind in {"visit", "dining"} and candidate_id:
                        physical_occurrences.setdefault(candidate_id, []).append(
                            day_id
                        )
                        if candidate_id not in first_physical_day:
                            first_physical_day[candidate_id] = day_id
                            physical_anchor_counts[day_id] = (
                                physical_anchor_counts.get(day_id, 0) + 1
                            )
                    elif placement_kind in {"visit", "dining"} and isinstance(
                        placement.get("authored_place"), dict
                    ):
                        # Anchors its day, but stays where the planner wrote it.
                        physical_anchor_counts[day_id] = (
                            physical_anchor_counts.get(day_id, 0) + 1
                        )
                    candidate = candidate_index.get(candidate_id)
                    if (
                        isinstance(candidate, TransportCandidate)
                        and candidate.transport_class == "long_distance"
                    ):
                        long_distance_days.add(day_id)
            repeated_physical = [
                {"candidate_id": candidate_id, "day_ids": day_ids}
                for candidate_id, day_ids in sorted(physical_occurrences.items())
                if len(day_ids) > 1
            ]
            day_ids = [
                str(day.get("day_id") or "")
                for day in repair_input.get("days", [])
                if isinstance(day, dict)
            ]
            anchorless_day_ids = [
                day_id
                for day_id in day_ids
                if not physical_anchor_counts.get(day_id)
                and day_id not in long_distance_days
            ]
            overfilled_day_ids = [
                day_id
                for day_id in day_ids
                if physical_anchor_counts.get(day_id, 0) > 1
            ]
            passed_ids = {
                admission.candidate_id
                for admission in state.recommendation_catalog.admission_results
                if admission.status == "passed"
            }
            eligible_candidates = [
                {
                    "candidate_id": candidate_id,
                    "candidate_kind": candidate_index[candidate_id].candidate_kind,
                    "current_day_ids": physical_occurrences.get(candidate_id, []),
                }
                for candidate_id in sorted(passed_ids)
            ]
            placement_capabilities = _placement_capabilities(
                state.recommendation_catalog,
                selection_plan=state.candidate_selection_plan,
            )
            movable_candidate_ids = sorted(
                candidate_id
                for candidate_id, owner_day_id in first_physical_day.items()
                if owner_day_id in overfilled_day_ids
                and candidate_id in boundary_physical_candidate_ids
                and len(physical_occurrences.get(candidate_id, [])) == 1
            )
            repair_messages = [
                *messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "上一个 JSON 对象未通过 ItineraryCompositionDraft 语义校验。"
                        "只做一次行程决策修复：不得新增或改写 Catalog 现实事实；"
                        "只能重新组合已选主方案的日期、顺序和时间。"
                        "同一 Day 内 placement candidate_id 必须唯一，同一 Visit/Dining candidate 在整份行程中也只能出现一次，"
                        "duration_days 必须等于 days 数量，"
                        "day 必须从 1 连续递增，时间必须落在各自当地日期内。"
                        f"当前同 Day 重复项：{json.dumps(duplicate_placements, ensure_ascii=False)}。"
                        f"当前跨日重复 Visit/Dining：{json.dumps(repeated_physical, ensure_ascii=False)}。"
                        f"去重后无合法日锚点的 Day：{json.dumps(anchorless_day_ids, ensure_ascii=False)}。"
                        f"当前拥有多个唯一物理锚点的 Day：{json.dumps(overfilled_day_ids, ensure_ascii=False)}。"
                        "对每个同 Day 重复 id 只保留一次；其余位置只能换成下列类型匹配的候选，"
                        "跨日重复 Visit/Dining 也只保留一次；没有合格替代时直接省略，不要为了填满餐次或交通而重复。"
                        "若去重后某 Day 无合法日锚点，必须把一个当前只出现在拥有多个唯一物理锚点 Day 的 Visit/Dining 移动到该 Day；"
                        "移动意味着从原 Day 删除并只在目标 Day 保留一次，禁止复制已经用于其它 Day 的实体。"
                        "相邻 Visit/Dining 之间必须使用 endpoint place_id 连续且首尾匹配的交通链，"
                        "并至少包含一个 public_transit 或 flexible connector；long_distance 不能单独替代。"
                        "移动实体后只保留仍与相邻实体 place_id 匹配的交通；无法连接时删除悬空或旧起终点路线，不得改写 endpoint。"
                        f"已选主方案：{json.dumps(eligible_candidates, ensure_ascii=False)}。"
                        f"候选放置能力：{json.dumps(placement_capabilities, ensure_ascii=False)}。"
                        f"校验错误：{initial_error}"
                    ),
                },
            ]
            try:
                if (
                    anchorless_day_ids
                    and overfilled_day_ids
                    and len(movable_candidate_ids) >= len(anchorless_day_ids)
                ):
                    move_schema = _anchor_move_response_schema(
                        candidate_ids=movable_candidate_ids,
                        from_day_ids=overfilled_day_ids,
                        to_day_ids=anchorless_day_ids,
                    )
                    move_messages = [
                        messages[0],
                        {
                            "role": "user",
                            "content": (
                                "上一个行程在确定性去重后出现无物理锚点的旅行日。"
                                "你只选择把哪些尚未重复的 Visit/Dining 从拥有多个物理锚点的 Day 移动到无锚点 Day；"
                                "每个目标 Day 恰好一个 move，每个 candidate 只能移动一次。"
                                "不得返回完整行程、不得复制候选、不得改写任何现实字段。"
                                f"可移动候选及当前位置：{json.dumps(eligible_candidates, ensure_ascii=False)}。"
                                f"无锚点 Day：{json.dumps(anchorless_day_ids, ensure_ascii=False)}；"
                                f"来源 Day：{json.dumps(overfilled_day_ids, ensure_ascii=False)}；"
                                f"允许移动的 candidate_id：{json.dumps(movable_candidate_ids, ensure_ascii=False)}。"
                            ),
                        },
                    ]
                    move_kwargs = {
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "itinerary_anchor_move_decision",
                                "strict": True,
                                "schema": move_schema,
                            },
                        },
                        "temperature": 0,
                    }
                    decision_response = await llm.ainvoke(
                        move_messages, **move_kwargs
                    )
                    decision_content = (
                        decision_response.content
                        if hasattr(decision_response, "content")
                        else decision_response
                    )
                    if not isinstance(decision_content, str):
                        decision_content = json.dumps(
                            decision_content, ensure_ascii=False
                        )
                    decision = json.loads(decision_content)
                    if not isinstance(decision, dict) or not isinstance(
                        decision.get("moves"), list
                    ):
                        raise ValueError("anchor repair must return one moves object")
                    repaired_payload = _apply_anchor_moves(
                        repair_input,
                        moves=decision["moves"],
                        anchorless_day_ids=anchorless_day_ids,
                        catalog=state.recommendation_catalog,
                    )
                    new_mutations.extend(
                        diff_composition_mutations(
                            before=repair_input,
                            after=repaired_payload,
                            generation_id=state.intent_spec.generation_id,
                            reason_code="anchor_coverage_repair",
                            created_by="composition_repair",
                            intent_ids_by_entity={
                                item.candidate_id: item.matched_intent_ids
                                for item in selected_capabilities
                            },
                            rule_ids=[rule.rule_id for rule in composition_rules],
                        )
                    )
                    composition = _parse_exact_llm_composition(
                        json.dumps(repaired_payload, ensure_ascii=False),
                        state,
                    )
                else:
                    repaired = await llm.ainvoke(repair_messages, **response_kwargs)
                    repaired_content = (
                        repaired.content if hasattr(repaired, "content") else repaired
                    )
                    if not isinstance(repaired_content, str):
                        repaired_content = json.dumps(
                            repaired_content, ensure_ascii=False
                        )
                    composition = _parse_exact_llm_composition(repaired_content, state)
                composition = await locate_authored_composition(
                    composition, llm, state, unlocatable
                )
                before_postprocessing = composition.model_dump(mode="json")
                composition = _drop_day_boundary_authored_connectors(composition)
                composition = _isolate_incompatible_long_distance_days(
                    composition,
                    state.recommendation_catalog,
                )
                new_mutations.extend(
                    diff_composition_mutations(
                        before=before_postprocessing,
                        after=composition.model_dump(mode="json"),
                        generation_id=state.intent_spec.generation_id,
                        reason_code="deterministic_topology_repair",
                        created_by="deterministic_pruner",
                        intent_ids_by_entity={
                            item.candidate_id: item.matched_intent_ids
                            for item in selected_capabilities
                        },
                        rule_ids=[rule.rule_id for rule in composition_rules],
                    )
                )
                validate_itinerary_transport_topology(
                    composition, state.recommendation_catalog
                )
            except Exception as repair_error:
                raise ValueError(
                    f"itinerary composition semantic repair failed: {repair_error}"
                ) from initial_error
        assert_never_violate_rules(
            composition.model_dump(mode="json"),
            composition_rules,
            selected_capabilities,
        )
        stage = f"{phase}/workspace_materialization"
        workspace = materialize_trip_workspace(
            run_id=state.run_id,
            workspace_revision=0,
            composition=composition,
            catalog=state.recommendation_catalog,
            intent_contract_snapshot=IntentContractSnapshot.from_intent_spec(
                state.intent_spec
            ),
            candidate_selection_plan=state.candidate_selection_plan,
            composition_mutations=[
                *state.composition_mutations,
                *mark_mutations_revalidated(new_mutations),
            ],
            user_input_anchors=_draft_user_input_anchors(state),
            reference_services=list(state.provider_reference_services),
        )
        workspace = _restore_full_catalog(workspace, full_catalog)
        _log_composition_dispersion(workspace)
        logger.info(
            "ItineraryPlanner v2 complete key=%s days=%d entities=%d slots=%d",
            output_key,
            workspace.itinerary.duration_days,
            sum(
                len(items)
                for items in (
                    workspace.itinerary.visit_stops,
                    workspace.itinerary.dining_stops,
                    workspace.itinerary.lodging_stays,
                    workspace.itinerary.transport_legs,
                )
            ),
            len(workspace.selection_slots),
        )
        return {
            "messages": [AIMessage(content="可执行行程组合已完成")],
            "agent_status": {output_key: "completed"},
            "trip_workspace_v2": workspace,
            "recommendation_catalog": workspace.recommendation_catalog,
            "unlocatable_authored_places": sorted(unlocatable),
            "composition_failure_context": None,
            "composition_mutations": mark_mutations_revalidated(new_mutations),
        }
    except Exception as exc:
        # 带上 traceback：这是整个深度规划的终止点，只留一句话没法定位是哪一道校验。
        logger.error("ItineraryPlanner v2 failed: %s", exc, exc_info=True)
        return {
            "messages": [AIMessage(content="行程组合未通过结构化校验")],
            "last_error": str(exc),
            "agent_status": {output_key: "failed"},
            # What this attempt disproved, and why it failed, are the only two
            # things a composition repair round can start from that it did not
            # already have. ``last_error`` is a shared channel every worker
            # writes, so the reason travels on the planner's own field.
            "unlocatable_authored_places": sorted(unlocatable),
            "composition_failure_context": bounded_repair_context(
                stage, _composition_failure_detail(exc)
            ),
        }
