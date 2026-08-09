"""
Planner 节点 (Domain Layer)

职责：根据 Research Brief 生成结构化执行计划。
仅做规划，不做分发和审查。
使用 primary model 保证规划质量。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Set

from ...entities.delivery_bundle import MinimumDeliveryDraft
from ...entities.state import TaskType, TravelAgentState
from ...entities.provider_evidence import (
    build_provider_evidence_assignments,
    build_required_long_distance_legs,
    dump_provider_evidence_assignments,
    explicit_cross_day_return_required,
    scope_attempt_numbers,
)
from ...models.router import get_model_router
from ...panels.constraint import format_constraint_pack_for_prompt
from ...tools.registry import get_tool_registry
from ...utils.json_helpers import safe_parse_json
from .prompts import PLANNER_SYSTEM_PROMPT, PLANNER_USER_TEMPLATE

logger = logging.getLogger(__name__)

_NODE_NAME = "planner"

VALID_AGENTS: Set[str] = {
    "destination_researcher",
    "transport_researcher",
    "accommodation_researcher",
    "itinerary_planner",
}



def _build_available_tools_summary(selected_mcp_servers: List[str]) -> str:
    """格式化当前可用工具列表（供 Planner 参考）。"""
    try:
        registry = get_tool_registry()
        tools = registry.list_tools()
        lines = []
        for t in tools:
            name = t.get("name", "")
            if name in ("ask_user",):
                continue
            source = t.get("source", "local")
            server = t.get("server_name", "")
            if source == "mcp" and selected_mcp_servers and server not in selected_mcp_servers:
                continue
            desc = t.get("description", "")[:60]
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines) if lines else "（无可用工具）"
    except Exception as e:
        logger.warning(f"获取工具列表失败: {e}")
        return "（工具列表暂时不可用）"


def _validate_plan(plan: Any) -> List[List[str]]:
    """校验并修正执行计划格式。"""
    if not isinstance(plan, list) or not plan:
        return [["destination_researcher"]]
    validated: List[List[str]] = []
    seen: Set[str] = set()
    for group in plan:
        if isinstance(group, str):
            group = [group]
        if not isinstance(group, list):
            continue
        valid = []
        for agent in group:
            if agent not in VALID_AGENTS or agent in seen:
                continue
            valid.append(agent)
            seen.add(agent)
        if valid:
            validated.append(valid)
    return validated or [["destination_researcher"]]


_DEFAULT_TASKS = {
    "destination_researcher": "收集目的地的景点、美食、文化和实用信息",
    "transport_researcher": "查询城际及市内交通方案",
    "accommodation_researcher": "推荐住宿区域与酒店，给出预算估算",
    "itinerary_planner": "整合研究员数据，编排完整按天行程",
}


def _normalize_plan_dependencies(plan: List[List[str]]) -> List[List[str]]:
    """
    Apply a small deterministic dependency validator to planner output.

    The LLM may emit valid worker names in an invalid order, for example placing
    itinerary_planner before transport/accommodation. Dispatcher executes groups
    exactly as given, so dependency order must be corrected before state update.
    """
    if not plan:
        return [["destination_researcher"]]

    flat: List[str] = []
    for group in plan:
        for agent in group:
            if agent in VALID_AGENTS and agent not in flat:
                flat.append(agent)

    if not flat:
        return [["destination_researcher"]]

    normalized: List[List[str]] = []
    if "destination_researcher" in flat:
        normalized.append(["destination_researcher"])

    middle = [agent for agent in ("transport_researcher", "accommodation_researcher") if agent in flat]
    if middle:
        normalized.append(middle)

    if "itinerary_planner" in flat:
        normalized.append(["itinerary_planner"])

    # If this was a narrow non-itinerary task, preserve remaining valid groups after
    # dependency correction instead of forcing a full travel-planning chain.
    normalized_agents = {agent for group in normalized for agent in group}
    remaining = [
        [agent for agent in group if agent in flat and agent not in normalized_agents]
        for group in plan
    ]
    for group in remaining:
        if group:
            normalized.append(group)

    return normalized or [["destination_researcher"]]


def _validate_assignments(assignments: Any, plan: List[List[str]]) -> Dict[str, Any]:
    """确保 agent_assignments 覆盖 plan 中所有 agent，并规范化格式。"""
    if not isinstance(assignments, dict):
        assignments = {}
    all_agents = {a for group in plan for a in group}
    validated: Dict[str, Any] = {}
    for agent in all_agents:
        raw = assignments.get(agent, {})
        default_task = _DEFAULT_TASKS.get(agent, "完成用户请求")
        if isinstance(raw, str):
            validated[agent] = {"task": raw or default_task, "recommended_tools": []}
        elif isinstance(raw, dict):
            validated[agent] = {
                "task": raw.get("task", default_task),
                "recommended_tools": raw.get("recommended_tools", []),
            }
        else:
            validated[agent] = {"task": default_task, "recommended_tools": []}
    return validated


def _infer_task_type(plan: List[List[str]]) -> TaskType:
    """根据 plan 中的 agent 组合推断任务类型。"""
    all_agents = {a for group in plan for a in group}
    if "itinerary_planner" in all_agents:
        return TaskType.TRAVEL_PLANNING
    if "transport_researcher" in all_agents and "accommodation_researcher" not in all_agents:
        return TaskType.TRANSPORT_QUERY
    if "accommodation_researcher" in all_agents and "transport_researcher" not in all_agents:
        return TaskType.ACCOMMODATION_QUERY
    if "destination_researcher" in all_agents:
        return TaskType.DESTINATION_INFO
    return TaskType.QUICK_QA


def _attach_initial_provider_evidence_scopes(
    assignments: Dict[str, Any],
    state: TravelAgentState,
) -> Dict[str, Any]:
    """Attach server-owned attempt-zero scopes to every research assignment."""

    scoped = dict(assignments)
    long_distance_legs = build_required_long_distance_legs(
        state.controlled_trip_identity or {},
        cross_day_return_required=explicit_cross_day_return_required(
            state.user_query or ""
        ),
    )
    for worker in (
        "destination_researcher",
        "transport_researcher",
        "accommodation_researcher",
    ):
        if worker not in scoped:
            continue
        if worker == "transport_researcher" and not long_distance_legs:
            scoped[worker] = {
                **scoped[worker],
                "provider_evidence_assignments": [],
            }
            continue
        scoped[worker] = {
            **scoped[worker],
            "provider_evidence_assignments": dump_provider_evidence_assignments(
                build_provider_evidence_assignments(
                    run_id=state.run_id,
                    constraint_pack_revision=state.constraint_pack_revision,
                    worker_kind=worker,
                    controlled_trip_identity=(
                        state.controlled_trip_identity or {}
                    ),
                    prior_scope_attempts=scope_attempt_numbers(
                        state.provider_evidence_outcomes
                    ),
                    long_distance_legs=(
                        long_distance_legs
                        if worker == "transport_researcher"
                        else None
                    ),
                )
            ),
        }
    return scoped


def _needs_itinerary_planner(research_brief_str: str) -> bool:
    """
    判断 research_brief 是否明确要求按天行程规划。
    触发条件（满足任意一项）：
      - duration_days 字段 > 1
      - dimensions_to_cover 中包含"行程"相关词
    """
    if not research_brief_str:
        return False
    try:
        brief = json.loads(research_brief_str)
    except (json.JSONDecodeError, ValueError):
        return False

    duration = brief.get("duration_days")
    if isinstance(duration, (int, float)) and duration > 1:
        return True

    dims = brief.get("dimensions_to_cover", [])
    itinerary_keywords = ("行程", "日程", "行程规划", "每日安排", "itinerary", "day plan")
    for dim in dims:
        if any(kw in str(dim) for kw in itinerary_keywords):
            return True

    return False


def _ensure_itinerary_planner(plan: List[List[str]]) -> List[List[str]]:
    """
    确保 plan 遵循半串行策略，且末尾有 itinerary_planner。
    强制结构：[["destination_researcher"], ["transport_researcher", "accommodation_researcher"], ["itinerary_planner"]]
    若 plan 中已有正确结构则不改变。
    """
    all_agents = {a for group in plan for a in group}
    # 若已包含 itinerary_planner，说明 Planner 生成正确，直接返回
    if "itinerary_planner" in all_agents:
        return plan
    # 否则注入完整半串行结构
    return [
        ["destination_researcher"],
        ["transport_researcher", "accommodation_researcher"],
        ["itinerary_planner"],
    ]


def _ensure_accommodation_researcher(
    plan: List[List[str]],
    draft: Optional[MinimumDeliveryDraft],
) -> List[List[str]]:
    """确保有过夜的行程一定派出 accommodation_researcher。

    住宿需求的唯一信号是 Minimum Delivery Draft 的 ``lodging_night``；此处不
    重算天数。补入后走一遍依赖归一化，让住宿与其他 researcher 并行、并落在
    itinerary_planner 之前。
    """
    if draft is None:
        return plan
    if not any(shell.lodging_night for shell in draft.day_shells):
        return plan
    if any("accommodation_researcher" in group for group in plan):
        return plan
    return _normalize_plan_dependencies([*plan, ["accommodation_researcher"]])


def _format_planner_context(state: TravelAgentState) -> str:
    """Format the shared constraints, preset and plan revision for Planner."""
    parts: List[str] = []
    constraint_text = format_constraint_pack_for_prompt(state.constraint_pack)
    if constraint_text:
        parts.append(constraint_text)
    if state.preset_context:
        from ...preset.injector import PresetInjector
        # 整段进，不切片 —— 与 ``agents/utils.py::inject_agent_context`` 同一条规则，
        # 理由写在 ``PresetInjector._wrap_active_preset``。
        parts.append(PresetInjector.format_for_planner(state.preset_context))
    if state.weather_context is not None:
        from ...workflows.weather_context import format_weather_context_for_planning

        parts.append(format_weather_context_for_planning(state))

    revision = state.plan_revision if isinstance(state.plan_revision, dict) else None
    if revision:
        mode = str(revision.get("mode") or "")
        content = str(revision.get("content") or "").strip()
        if mode == "edit" and content:
            parts.append(
                "\n用户已直接编辑调研计划全文，以下文本是前期分析的最终结果、具有最高优先级，"
                f"严格按照它生成结构化执行计划与任务分配：\n{content}"
            )
        elif mode == "supplement" and content:
            base_plan_text = str(revision.get("base_plan_text") or "").strip()
            parts.append(
                f"\n原计划全文：\n{base_plan_text}\n\n用户补充要求（必须并入）：\n{content}\n"
                "将补充要求与原计划混合为最终计划；未被补充影响的部分保持稳定。"
            )
    return "\n".join(part for part in parts if part)


async def planner_node(state: TravelAgentState) -> Dict[str, Any]:
    """
    Planner 节点：根据 research_brief、约束与规划前 Weather Context 生成执行计划。

    输出：
    - execution_plan: 二维数组（外层=步骤，内层=并行 agent 组）
    - agent_assignments: 结构化任务分配（含 recommended_tools）
    - current_plan_step: 重置为 0
    """
    router = get_model_router()
    llm = router.get_primary()
    plan_revision = state.plan_revision if isinstance(state.plan_revision, dict) else None
    has_authoritative_edit = bool(plan_revision and plan_revision.get("mode") == "edit")

    research_brief = state.research_brief or f'{{"objective": "{state.user_query}"}}'
    tools_summary = _build_available_tools_summary(state.selected_mcp_servers or [])

    messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": PLANNER_USER_TEMPLATE.format(
                research_brief=research_brief,
                available_tools_summary=tools_summary,
                preset_context=_format_planner_context(state),
            ),
        },
    ]

    try:
        response = await llm.ainvoke(messages)
        decision = safe_parse_json(response)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Network/SDK wrappers (httpx, OpenAI, etc.) are not always RuntimeError.
        # Degrade to a legal default plan rather than failing the whole run.
        logger.warning("Planner LLM 调用失败，降级为 destination_researcher: %s", e)
        decision = None

    if not decision:
        logger.warning("Planner: 无法解析响应，使用默认计划")
        plan = [["destination_researcher"]]
        assignments = {"destination_researcher": {"task": state.user_query, "recommended_tools": []}}
    else:
        plan = _validate_plan(decision.get("execution_plan", []))
        normalized_plan = _normalize_plan_dependencies(plan)
        if normalized_plan != plan:
            logger.info("Planner validator reordered execution_plan: %s -> %s", plan, normalized_plan)
            plan = normalized_plan
        assignments = _validate_assignments(decision.get("agent_assignments", {}), plan)

    # 保底规则：当 research_brief 明确包含多日行程需求时，确保
    # itinerary_planner 出现在执行计划中，并强制半串行结构。
    # 用户在计划门提交的完整 edit 是本轮最终计划，不能再被旧 brief
    # 恢复为编辑前的住宿、交通和行程链路；supplement 仍保留原计划语义。
    research_brief_str = state.research_brief or ""
    if _needs_itinerary_planner(research_brief_str) and not has_authoritative_edit:
        all_planned = {a for group in plan for a in group}
        if "itinerary_planner" not in all_planned:
            plan = _ensure_itinerary_planner(plan)
            plan = _normalize_plan_dependencies(plan)
            assignments = _validate_assignments(assignments, plan)
            logger.info("Planner 保底规则触发: 强制半串行结构（含 itinerary_planner）")

    # 保底规则：Draft 里存在过夜时，住宿调研不是模型的自由裁量项。
    if not has_authoritative_edit:
        with_lodging = _ensure_accommodation_researcher(
            plan, state.minimum_delivery_draft
        )
        if with_lodging != plan:
            plan = with_lodging
            assignments = _validate_assignments(assignments, plan)
            logger.info("Planner 保底规则触发: 补入 accommodation_researcher（Draft 含过夜）")

    task_type = _infer_task_type(plan)
    assignments = _attach_initial_provider_evidence_scopes(assignments, state)

    logger.info("Planner: plan=%s, task_type=%s", plan, task_type)

    return {
        "execution_plan": plan,
        "current_plan_step": 0,
        "agent_assignments": assignments,
        "task_type": task_type,
        # 计划批准门修改载体已被本轮重规划消费，清空避免下一轮门重复注入。
        "plan_revision": None,
    }
