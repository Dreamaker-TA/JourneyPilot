"""
Agent 节点通用工具函数。

对外主要接口:
- get_available_tools / filter_tools_for_agent: Agent 工具策略过滤
- streaming_react_loop: 所有 Worker 共用的流式 ReAct 循环
- inject_agent_context: 系统提示词注入 Anchor、Preset 与统一 Constraint Pack
- resolve_agent_assignment: Worker 精炼轮次任务查找
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Set, Tuple, TypedDict

from ..config import get_settings
from ..tools.builtin_tools import SEARCH_TOOLS_NAME, build_search_tools_item
from ..tools.exposure_ledger import (
    estimate_schema_tokens,
    estimate_text_tokens,
    get_tool_exposure_ledger,
)
from ..tools.governance import (
    CAPABILITY_DECLARATION_STATUSES,
    PROVIDER_RESULT_OUTCOME_EMPTY_SUCCESS,
    PROVIDER_RESULT_OUTCOME_METADATA_KEY,
    QUALITY_VERIFIED_PLACE_IDS_METADATA_KEY,
    TOOL_FAILURE_SUMMARY,
    ToolExecutionStatus,
    build_tool_execution_envelope,
    is_tool_execution_envelope,
)
from ..entities.tool_gateway import ToolGatewayDecision
from ..tools.gateway import get_tool_gateway
from ..tools.registry import (
    compact_catalog_items,
    get_tool_registry,
    search_tool_items,
)
from ..workflows.run_budget import RunBudgetExhausted
from ..workflows.run_control import (
    ModelWindowClosed,
    await_model_operation,
    check_cancel_requested,
    current_budget_ledger,
    guard_run_budget,
    remaining_model_seconds,
    run_ts_ms,
)

if TYPE_CHECKING:
    from ..api.sse_buffer import SSEBuffer

logger = logging.getLogger(__name__)


# ``degraded`` 走了备用通道但仍然拿回了可用结果，所以它和 ``success`` 一样进 ReAct
# 断路器的分子。能力判定（``CAPABILITY_DECLARATION_STATUSES``）既不进分子也不进分母。
_SUCCEEDED_TOOL_STATUSES = frozenset({
    ToolExecutionStatus.SUCCESS.value,
    ToolExecutionStatus.DEGRADED.value,
})


# ---------------------------------------------------------------------------
# 共享上下文注入 (供各 Worker 节点避免重复实现 anchor / preset 注入)
# ---------------------------------------------------------------------------

def inject_agent_context(system_content: str, state: Any, agent_label: str = "") -> str:
    """向 system prompt 追加 session anchor、preset 与统一约束。

    被三个 researcher (destination / transport / accommodation) 与
    itinerary_planner 共享。延迟 import 避免与 memory / preset 模块循环依赖。
    """
    if getattr(state, "session_anchor", None):
        try:
            from ..memory.compressor import AnchorSummary
            anchor_text = AnchorSummary.from_dict(state.session_anchor).format_for_prompt()
            if anchor_text:
                system_content += f"\n\n【历史对话摘要】\n{anchor_text}"
        except Exception as e:
            logger.debug(f"{agent_label or 'agent'} AnchorSummary 注入失败: {e}")

    if getattr(state, "preset_context", None):
        try:
            from ..preset.injector import PresetInjector
            # 整段进，不切片：``format_for_agent`` 交回的是一个
            # ``<active_preset>…</active_preset>`` 信封，切在中间就是留一个开着的标签。
            # 长度在存入时就由 ``entities.preset`` 的字段上限管住了。
            preset_text = PresetInjector.format_for_agent(state.preset_context)
            if preset_text:
                system_content += "\n\n" + preset_text
        except Exception as e:
            logger.debug(f"{agent_label or 'agent'} Preset 上下文注入失败: {e}")

    constraint_pack = getattr(state, "constraint_pack", None)
    if constraint_pack:
        from ..panels.constraint import format_constraint_pack_for_prompt

        constraint_text = format_constraint_pack_for_prompt(constraint_pack)
        if constraint_text:
            system_content += "\n\n" + constraint_text

    if getattr(state, "weather_context", None) is not None:
        from ..workflows.weather_context import format_weather_context_for_planning

        weather_text = format_weather_context_for_planning(state)
        if weather_text:
            system_content += "\n\n" + weather_text

    supplements = getattr(state, "supplemental_requirements", None) or []
    if supplements:
        lines = [f"- [{item.get('category', 'other')}] {item.get('content', '')}" for item in supplements if item.get("content")]
        if lines:
            system_content += "\n\n【本次运行追加要求】\n" + "\n".join(lines)

    return system_content


def append_recent_history(
    messages: List[Dict[str, Any]],
    state: Any,
    limit: int = 8,
) -> None:
    """将 state.messages 末 limit 条转为 role/content dict, 就地追加到 messages 列表。

    用于 Worker 节点向 LLM messages 注入最近历史 (langchain Message → OpenAI dict)。
    """
    recent = (getattr(state, "messages", None) or [])[-limit:]
    for msg in recent:
        if hasattr(msg, "type"):
            role = "user" if msg.type == "human" else "assistant"
            content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
            if content:
                messages.append({"role": role, "content": content})


def session_history_for_context_builder(state: Any) -> List[Dict[str, Any]]:
    """Every loaded session message, converted for :class:`ContextBuilder`.

    Deliberately uncapped, and that is the whole point.  ``ContextBuilder`` trims
    the message layer to ``messages_budget`` before it reaches the prompt, and it
    is also the layer that decides whether the assembled context crossed the
    compaction threshold — a judgement it can only make on the history that was
    actually loaded.  "How much fits" belongs to the budget layer; a second cap
    written at the call site is what makes auto-compaction unreachable — the loader
    stops at the 60,000-token trigger, this conversion then throws all but the last
    20 messages away, and the judgement runs on 1,027 tokens, 1.7% of the threshold
    it is comparing against.

    Contrast :func:`append_recent_history`, whose ``limit`` is correct: that path
    injects turns straight into a worker prompt, with no budget layer behind it
    to do the trimming.
    """

    history: List[Dict[str, Any]] = []
    for msg in getattr(state, "messages", None) or []:
        if hasattr(msg, "type"):
            role = "user" if msg.type == "human" else "assistant"
            history.append({"role": role, "content": msg.content or ""})
    return history


def prioritize_recommended_tools(
    available: List[Dict[str, Any]],
    recommended: List[str],
) -> List[Dict[str, Any]]:
    """将 recommended 中的工具名排在前面, 其余保持原序返回。

    工具项结构: ``{"schema": {"function": {"name": ...}}, ...}``。
    用于 Worker 节点在 tool_schemas 顺序上体现 planner 推荐倾向 (LLM 倾向于调首个)。
    """
    if not recommended:
        return available
    rec_set = set(recommended)

    def _name(item: Dict[str, Any]) -> str:
        return item.get("schema", {}).get("function", {}).get("name", "")

    ranked = [t for t in available if _name(t) in rec_set]
    rest = [t for t in available if _name(t) not in rec_set]
    return ranked + rest


def exclude_tools(
    available: List[Dict[str, Any]],
    excluded: List[str],
) -> List[Dict[str, Any]]:
    """Remove tools that produced a durable failure in an earlier gap attempt."""
    if not excluded:
        return available
    excluded_set = set(excluded)
    return [
        tool
        for tool in available
        if tool.get("schema", {}).get("function", {}).get("name", "") not in excluded_set
    ]


def build_tool_context_from_state(state: Any) -> Dict[str, Any]:
    """Build optional ToolGateway context from workflow state and app stores.

    The Run is the only identity a tool call needs.  ``user_id`` / ``session_id``
    used to ride along for an approval queue that no longer exists; every key in
    here is splatted straight into ``execute_tool``, so a key nobody consumes is
    a ``TypeError`` waiting for the next signature change.
    """
    context: Dict[str, Any] = {
        "run_id": getattr(state, "run_id", None),
    }
    try:
        from ..builders import get_components

        components = get_components()
        context["tool_audit_store"] = getattr(components, "tool_audit_store", None)
        context["provider_snapshot_cache"] = getattr(
            components,
            "provider_snapshot_cache",
            None,
        )
        context["trip_run_store"] = getattr(components, "trip_run_store", None)
    except Exception:
        pass
    return context



# ---------------------------------------------------------------------------
# 轮次命名约定
# ---------------------------------------------------------------------------

def strip_round_suffix(agent_name: str) -> str:
    """去除 Agent 名称的轮次后缀，返回基础名称。

    命名约定：``{base_name}_r{N}`` 表示第 N 轮调度。
    首轮执行无后缀；第一次补充为 _r2；第二次为 _r3。

    示例：
        >>> strip_round_suffix("destination_researcher_r2")
        'destination_researcher'
        >>> strip_round_suffix("itinerary_planner")
        'itinerary_planner'
    """
    return re.sub(r"_r\d+$", "", agent_name)


def make_round_name(base_name: str, round_num: int) -> str:
    """生成带轮次后缀的 Agent 名称。

    命名约定：``{base_name}_r{round_num}``。
    round_num 从 2 开始（首轮无后缀，第一次补充为 _r2）。

    示例：
        >>> make_round_name("destination_researcher", 2)
        'destination_researcher_r2'
    """
    return f"{base_name}_r{round_num}"


# ---------------------------------------------------------------------------
# 工具获取与过滤
# ---------------------------------------------------------------------------

class _AgentToolPolicy(TypedDict, total=False):
    """Agent 工具访问策略。未在策略中的 agent 默认拒绝所有 MCP 工具，保留本地工具。"""
    servers: Set[str]       # 允许的 MCP server_name 集合
    deny_tools: Set[str]    # 要拒绝的工具名集合（含本地工具），"*" 表示拒绝所有
    extra_tools: Set[str]   # 额外允许的工具名集合

_AGENT_TOOL_POLICY: Dict[str, _AgentToolPolicy] = {
    "destination_researcher": {
        # 检索层（tavily/brave/firecrawl/duckduckgo/fetch）。
        "servers": {
            "tavily-search",
            "brave-search",
            "firecrawl",
            "duckduckgo-search",
            "fetch",
        },
        # destination_researcher 是唯一被允许调用 ask_user 的 Worker：
        # 当用户目的地存在重大歧义（同名城市、日期不明、地点指向不确定）时主动澄清。
        # 地图/天气工具不在其 server 白名单中，无需额外 deny。
        "extra_tools": {
            "free_web_search",
            "global_place_search",
            "maps_text_search",
            "maps_around_search",
            # CN dining amap text search returns no geometry; detail resolves pins.
            "maps_search_detail",
        },
    },
    "transport_researcher": {
        # 火车（12306）+ 航班（Duffel）+ 国内地图（高德）
        "servers": {"12306-train", "duffel-flights", "amap-maps"},
        # 地图投影只消费 typed entity/place identity；天气由前置正式 Provider 获取。
        "deny_tools": {"maps_geo", "maps_geocode", "maps_weather", "ask_user"},
        "extra_tools": {"free_web_search", "global_route_search"},
    },
    "accommodation_researcher": {
        # 住宿：Nominatim 核验海外 property identity；Tavily/Brave 检索价格与可订性；汇率（Frankfurter）；
        # 国内酒店在 OpenStreetMap 覆盖极差，用高德 POI（maps_text_search/maps_around_search）落地具体酒店身份。
        "servers": {"tavily-search", "brave-search", "currency-exchange-mcp"},
        "deny_tools": {"ask_user"},
        "extra_tools": {
            "free_web_search",
            "global_place_search",
            "maps_text_search",
            "maps_around_search",
            # amap text search returns no geometry; maps_search_detail resolves a
            # POI id to its point location so lodging can carry a map pin.
            "maps_search_detail",
        },
    },
    "itinerary_planner": {
        "servers": set(),
        "deny_tools": {"*"},  # 拒绝所有工具（纯 LLM 规划）
    },
}

_SCOPED_NON_MCP_TOOLS = {"global_place_search", "global_route_search"}


async def get_available_tools(selected_mcp_servers: List[str]) -> List[Dict[str, Any]]:
    """
    从工具注册表获取可用工具列表，按 selected_mcp_servers 过滤 MCP 工具。
    本地工具（ask_user 等）始终包含。
    """
    try:
        registry = get_tool_registry()
        tools = registry.get_tools_as_schemas()
        if not selected_mcp_servers:
            return tools

        selected = set(selected_mcp_servers)
        filtered: List[Dict[str, Any]] = []
        for tool in tools:
            if tool.get("source") != "mcp":
                filtered.append(tool)
            elif tool.get("server_name") in selected:
                filtered.append(tool)
        return filtered
    except Exception as e:
        logger.warning(f"获取工具列表失败: {e}")
        return []


def resolve_agent_assignment(
    agent_assignments: Dict[str, Any],
    base_name: str,
    refinement_count: int,
) -> Tuple[str, Dict[str, Any]]:
    """
    为 Worker 节点查找正确的任务分配。

    精炼/补研轮次中，Candidate Gate 写入的 assignment key 带有 round suffix
    （如 destination_researcher_r2）。Worker 节点用 base_name 注册，需要找到
    当前轮次对应的 assignment。

    返回 (output_key, assignment_dict)：
    - output_key: 存储输出时使用的 key（base_name 或 base_name_rN）
    - assignment_dict: 任务配置 {"task": ..., "recommended_tools": [...]}
    """
    # 优先查找当前精炼轮次对应的后缀 key。
    # 语义约定：首轮无后缀；第一次补充为 _r2；第二次补充为 _r3。
    # 因此 refinement_count=1 -> _r2，refinement_count=2 -> _r3。
    if refinement_count > 0:
        round_key = make_round_name(base_name, refinement_count + 1)
        if round_key in agent_assignments:
            return round_key, agent_assignments[round_key]

    # 查找任意轮次的 round-suffixed key（按数字轮次倒序，取最新）
    matching_keys_with_round = []
    for key in agent_assignments:
        m = re.match(rf"^{re.escape(base_name)}_r(\d+)$", key)
        if m:
            matching_keys_with_round.append((int(m.group(1)), key))

    if matching_keys_with_round:
        matching_keys_with_round.sort(key=lambda x: x[0], reverse=True)
        latest_key = matching_keys_with_round[0][1]
        return latest_key, agent_assignments[latest_key]

    # 使用 base_name
    assignment = agent_assignments.get(base_name, {})
    return base_name, assignment


def resolve_scoped_research_output_key(
    research_packets: Dict[str, Any],
    base_name: str,
    resolved_key: str,
    *,
    scoped_retry: bool,
) -> str:
    """Give every Candidate Gate retry an immutable worker-round key."""
    if not scoped_retry:
        return resolved_key
    resolved_match = re.fullmatch(rf"{re.escape(base_name)}_r(\d+)", resolved_key)
    if resolved_match and resolved_key not in research_packets:
        return resolved_key
    rounds: List[int] = []
    for key in research_packets:
        if key == base_name:
            rounds.append(1)
            continue
        match = re.fullmatch(rf"{re.escape(base_name)}_r(\d+)", key)
        if match:
            rounds.append(int(match.group(1)))
    return make_round_name(base_name, max(rounds, default=1) + 1)


def filter_tools_for_agent(
    tools: List[Dict[str, Any]],
    agent_name: str,
) -> List[Dict[str, Any]]:
    """
    按 Agent 工具策略（_AGENT_TOOL_POLICY）过滤工具。

    策略字段：
    - servers:     允许的 MCP server_name 集合
    - deny_tools:  按工具名拒绝的集合（含本地工具），"*" 表示拒绝所有
    - extra_tools: 额外允许的工具名集合（可选）

    过滤逻辑：
    1. deny_tools 包含 "*" → 返回空列表
    2. deny_tools 中列出的工具名被移除（同时适用于 MCP 和本地工具）
    3. MCP 工具：只允许 servers 白名单或 extra_tools 中指定的工具通过
    4. 本地工具（source != "mcp"）：保留（除非在 deny_tools 中）
    5. 未在策略中的 agent：只保留本地工具
    """
    policy = _AGENT_TOOL_POLICY.get(agent_name)

    if policy is None:
        # 未知 agent：只保留本地工具
        return [
            t
            for t in tools
            if t.get("source") != "mcp"
            and t.get("schema", {}).get("function", {}).get("name", "")
            not in _SCOPED_NON_MCP_TOOLS
        ]

    deny_tools = policy.get("deny_tools", set())

    # "*" 表示拒绝所有工具
    if "*" in deny_tools:
        return []

    allowed_servers = policy.get("servers", set())
    extra_tools = policy.get("extra_tools", set())

    result = []
    for t in tools:
        tool_name = t.get("schema", {}).get("function", {}).get("name", "")

        # deny_tools 过滤（同时适用于 MCP 和本地工具）
        if tool_name in deny_tools:
            continue

        if t.get("source") != "mcp":
            # 本地工具保留（除非已被 deny_tools 拦截）
            if tool_name in _SCOPED_NON_MCP_TOOLS and tool_name not in extra_tools:
                continue
            result.append(t)
        elif t.get("server_name") in allowed_servers:
            result.append(t)
        elif tool_name in extra_tools:
            result.append(t)

    logger.debug(
        "filter_tools_for_agent(%s): %d -> %d tools [%s]",
        agent_name,
        len(tools),
        len(result),
        ", ".join(
            t.get("schema", {}).get("function", {}).get("name", "?")
            for t in result
        ),
    )
    return result


# ---------------------------------------------------------------------------
# Tool Search 按需工具曝光
# ---------------------------------------------------------------------------

# worker agent 集合（= filter_tools_for_agent 白名单键）——worker_only 判定用。
_WORKER_AGENTS: Set[str] = set(_AGENT_TOOL_POLICY.keys())

_CATALOG_HINT = (
    "\n\n【可用工具（按需激活）】\n"
    "你当前只加载了 search_tools 一个元工具，其余工具的完整定义尚未载入。需要用工具时先调用 "
    "search_tools(query=\"关键词\") 检索并激活——支持中文（地图/航班/酒店/汇率/景点）或工具名前缀"
    "（amap_/duffel_）检索，同前缀的一组工具一次即可命中。激活后该工具当轮与后续轮次持续可用，"
    "无需重复检索；若无需任何工具可直接作答。\n"
    "候选工具（名称 — 说明）：\n"
)


@dataclass
class ToolExposurePlan:
    """一次 worker 工具组装的曝光计划（apply_tool_exposure 产出）。"""

    deferred: bool
    agent: str
    tool_schemas: List[Dict[str, Any]]   # 初始暴露给模型的 schema（deferred=仅 search_tools）
    catalog_prompt: str                  # deferred 注入 system prompt 的能力提示 + 压缩目录
    injected_tokens: int
    full_tokens: int
    exposed_tool_count: int
    full_tool_count: int


def _format_catalog_prompt(catalog: List[Dict[str, str]]) -> str:
    lines = [
        f"- {c['name']}" + (f" — {c['brief']}" if c.get("brief") else "")
        for c in catalog
    ]
    return _CATALOG_HINT + "\n".join(lines)


def apply_tool_exposure(
    available_tools: List[Dict[str, Any]],
    agent_name: str,
) -> ToolExposurePlan:
    """按 tool_exposure 配置决定 worker 的工具曝光方式（deferred 压缩目录 / full 全量注入）。

    ``available_tools`` 是 ``filter_tools_for_agent`` 产出的**白名单**（= search_tools 的
    搜索边界，治理不放松）。deferred 时初始只暴露 search_tools + 把压缩目录注入 system prompt；
    full（或阈值以下 / 非 worker / 回退）时原样暴露全部 schema。两条路径都算出 injected/full
    的 schema token，供 ``tool_context_saving`` 量化。
    """
    base_agent = strip_round_suffix(agent_name)
    cfg = get_settings().tool_exposure

    full_schemas = [t["schema"] for t in available_tools if "schema" in t]
    full_tokens = estimate_schema_tokens(available_tools)
    full_count = len(full_schemas)

    is_worker = base_agent in _WORKER_AGENTS
    should_defer = (
        cfg.mode == "deferred"
        and (is_worker if cfg.worker_only else True)
        and full_count >= cfg.min_tools_threshold
    )

    if not should_defer:
        return ToolExposurePlan(
            deferred=False,
            agent=base_agent,
            tool_schemas=full_schemas,
            catalog_prompt="",
            injected_tokens=full_tokens,
            full_tokens=full_tokens,
            exposed_tool_count=full_count,
            full_tool_count=full_count,
        )

    search_item = build_search_tools_item()
    catalog = compact_catalog_items(available_tools)
    catalog_prompt = _format_catalog_prompt(catalog)
    injected_tokens = estimate_schema_tokens([search_item]) + estimate_text_tokens(catalog_prompt)
    return ToolExposurePlan(
        deferred=True,
        agent=base_agent,
        tool_schemas=[search_item["schema"]],
        catalog_prompt=catalog_prompt,
        injected_tokens=injected_tokens,
        full_tokens=full_tokens,
        exposed_tool_count=1,
        full_tool_count=full_count,
    )


async def execute_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    available_tools: Optional[List[Dict[str, Any]]] = None,
    allowed_tool_names: Optional[Set[str]] = None,
    max_retries: int = 2,
    run_id: Optional[str] = None,
    node_name: Optional[str] = None,
    tool_audit_store: Any = None,
    tool_gateway: Any = None,
    activation_source: Optional[str] = None,
    provider_snapshot_cache: Any = None,
    trip_run_store: Any = None,
    allow_fallback: bool = True,
) -> Dict[str, Any]:
    """
    执行工具调用，通过 ToolRegistry 路由到本地工具或 MCP 工具。

    增强特性：
    - 指数退避重试（可重试错误：网络超时、连接失败）
    - 降级策略：主工具失败后尝试备用工具（通过 fallback.py 映射，且不绕过 agent allowlist）
    - ``allow_fallback=False``：确定性绑定路径禁止 free_web_search 等降级冒充 Provider 证据
    - 结果校验：空结果或格式异常时记录 warning
    """
    if allowed_tool_names is None and available_tools is not None:
        allowed_tool_names = {
            t["schema"]["function"]["name"]
            for t in available_tools
            if "schema" in t and "function" in t["schema"]
        }

    registry = get_tool_registry()
    main_meta = registry.get_tool_metadata(tool_name)
    # The 6-minute boundary applies before every new ToolGateway / Provider
    # path, including a retry or fallback.  Return a normal failed envelope so
    # the Worker can hand the scoped content gap to Candidate Gate rather than
    # accidentally turn a deadline policy into user cancellation.
    try:
        remaining_model_seconds(f"tool.{tool_name}")
    except ModelWindowClosed as exc:
        envelope = build_tool_execution_envelope(
            tool_name=tool_name,
            arguments=arguments,
            status=ToolExecutionStatus.FAILED.value,
            error=str(exc),
            source=str(main_meta.get("source") or "unknown"),
            server_name=main_meta.get("server_name"),
            category=str(main_meta.get("category") or "other"),
            activation_source=activation_source,
        )
        envelope["metadata"]["research_window_closed"] = True
        return envelope
    # 预算和 Deadline 在这里是同一类边界，收口方式也一样：返回一个普通的 failed
    # envelope，让 Worker 把这条内容缺口交给 Candidate Gate。把「钱用完了」抛成异常
    # 会变成一次 Run 失败，而它其实是一次可解释的降级。
    try:
        guard_run_budget(f"tool.{tool_name}", tool_calls=1)
    except RunBudgetExhausted as exc:
        envelope = build_tool_execution_envelope(
            tool_name=tool_name,
            arguments=arguments,
            status=ToolExecutionStatus.FAILED.value,
            error=exc.reason_code,
            source=str(main_meta.get("source") or "unknown"),
            server_name=main_meta.get("server_name"),
            category=str(main_meta.get("category") or "other"),
            activation_source=activation_source,
        )
        envelope["metadata"]["run_budget_exhausted"] = exc.dimension
        return envelope
    ledger = current_budget_ledger()
    if ledger is not None and ledger.tool_retries_exhausted(tool_name):
        envelope = build_tool_execution_envelope(
            tool_name=tool_name,
            arguments=arguments,
            status=ToolExecutionStatus.FAILED.value,
            error="tool_retries_exhausted",
            source=str(main_meta.get("source") or "unknown"),
            server_name=main_meta.get("server_name"),
            category=str(main_meta.get("category") or "other"),
            activation_source=activation_source,
        )
        envelope["metadata"]["tool_retries_exhausted"] = tool_name
        return envelope
    gateway = tool_gateway or get_tool_gateway()
    before = await gateway.before_call(
        tool_name=tool_name,
        arguments=arguments,
        registry=registry,
        allowed_tool_names=allowed_tool_names,
        run_id=run_id,
        node_name=node_name,
        audit_store=tool_audit_store,
    )
    if before.envelope is not None:
        if before.decision in {
            ToolGatewayDecision.NOT_APPLICABLE,
            ToolGatewayDecision.REFERENCE_ONLY,
        }:
            logger.info(
                "工具 [%s] 日期能力判定为 %s: %s",
                tool_name,
                before.decision.value,
                before.reason,
            )
        elif before.decision != ToolGatewayDecision.ALLOW:
            logger.warning("工具 [%s] 被 ToolGateway 阻断: %s", tool_name, before.reason)
        return before.envelope
    main_manifest = before.manifest

    # Provider snapshots deliberately live outside ``state.tool_cache``.  A
    # cache hit still travels through this Gateway and receives a new audit id
    # for the current Run; it is never an old ToolExecutionEnvelope replay.
    snapshot_scope = None
    snapshot_cache = provider_snapshot_cache
    try:
        from ..infrastructure.provider_snapshot_cache import (
            get_provider_snapshot_cache,
            provider_snapshot_scope_for_tool,
        )

        snapshot_scope = provider_snapshot_scope_for_tool(tool_name, arguments)
        if snapshot_scope is not None:
            snapshot_cache = snapshot_cache or get_provider_snapshot_cache()
            lookup = await snapshot_cache.lookup(snapshot_scope)
            if lookup.outcome == "hit" and lookup.record is not None:
                cached_result = lookup.record.snapshot
                envelope = build_tool_execution_envelope(
                    tool_name=tool_name,
                    arguments=arguments,
                    result=cached_result,
                    status=ToolExecutionStatus.SUCCESS.value,
                    source=str(main_meta.get("source") or "unknown"),
                    server_name=main_meta.get("server_name"),
                    category=main_manifest.category,
                    retrieved_at=str(cached_result.get("retrieved_at") or "") or None,
                    freshness_hint=cached_result.get("freshness_hint"),
                    activation_source=activation_source,
                )
                # The cached payload was already sanitized before persistence.
                # Preserve its byte-equivalent logical shape so SourceRecord's
                # retained content hash cannot drift through a second sanitizer.
                envelope["sanitized_result"] = cached_result
                envelope["metadata"]["snapshot_cache"] = lookup.record.trace_metadata(
                    origin="provider_snapshot_cache",
                    outcome="hit",
                )
                return await gateway.after_call(
                    envelope,
                    manifest=main_manifest,
                    run_id=run_id,
                    audit_store=tool_audit_store,
                )
    except Exception as exc:
        # Redis/cache policy is an optimization only.  Scope errors and a
        # disconnected cache must fall through to the real Provider call.
        logger.info("provider snapshot cache unavailable; using live tool call: %s", exc)
        snapshot_scope = None
        snapshot_cache = None

    async def persist_provider_snapshot(envelope: Dict[str, Any]) -> None:
        if snapshot_scope is None or snapshot_cache is None:
            return
        from ..infrastructure.provider_snapshot_cache import build_provider_snapshot_record

        record = build_provider_snapshot_record(scope=snapshot_scope, envelope=envelope)
        if record is None:
            return
        stored = await snapshot_cache.write(record)
        metadata = dict(envelope.get("metadata") or {})
        metadata["snapshot_cache"] = record.trace_metadata(
            origin="live",
            outcome="stored" if stored else "unavailable",
        )
        envelope["metadata"] = metadata

    async def record_retry_failure(error: str, attempt: int) -> None:
        envelope = build_tool_execution_envelope(
            tool_name=tool_name,
            arguments=arguments,
            status=ToolExecutionStatus.FAILED.value,
            error=error,
            source=str(main_meta.get("source") or "unknown"),
            server_name=main_meta.get("server_name"),
            category=main_manifest.category,
            activation_source=activation_source,
        )
        envelope["metadata"].update({
            "retry_attempt": attempt + 1,
            "retry_scheduled": True,
            "max_attempts": max_retries + 1,
        })
        await gateway.after_call(
            envelope,
            manifest=main_manifest,
            run_id=run_id,
            audit_store=tool_audit_store,
        )

    # 指数退避重试
    last_error: Optional[str] = None
    research_window_closed = False
    budget_exhausted: Optional[str] = None
    for attempt in range(max_retries + 1):
        try:
            remaining_model_seconds(f"tool.{tool_name}.attempt_{attempt + 1}")
            # 每一次尝试都判一次、记一次：重试也花配额。只在入口判一次而在循环里记
            # max_retries + 1 次，等于让 max_tool_calls 只精确到 4 倍。
            # 重试另记一笔：那一档的上限是「同一个工具在这个 Run 上重试多少轮」，
            # 把首发也算进去等于调够几次就把这个工具永久关掉。
            if ledger is not None:
                ledger.reserve_tool_call(f"tool.{tool_name}", tool_name)
                if attempt > 0:
                    ledger.record_tool_retry(tool_name)
            result = await await_model_operation(
                registry.execute(tool_name, **arguments),
                operation=f"tool.{tool_name}.attempt_{attempt + 1}",
            )
            # 结果校验：失败 / 成功但无命中 / 成功且有内容
            outcome, outcome_reason = classify_tool_result(tool_name, result)
            if outcome is not ToolResultOutcome.FAILED:
                if outcome is ToolResultOutcome.EMPTY_SUCCESS:
                    # provider 已经回答"没有"。既不重试也不换工具，直接把这条
                    # 成功应答交给模型自行改写查询。
                    logger.info(
                        "工具 [%s] provider 成功应答但零命中，不重试不降级: %s",
                        tool_name,
                        outcome_reason,
                    )
                result = _attach_freshness_metadata(tool_name, result)
                envelope = build_tool_execution_envelope(
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                    status=ToolExecutionStatus.SUCCESS.value,
                    source=str(main_meta.get("source") or "unknown"),
                    server_name=main_meta.get("server_name"),
                    category=main_manifest.category,
                    retrieved_at=result.get("retrieved_at"),
                    freshness_hint=result.get("freshness_hint"),
                    activation_source=activation_source,
                )
                if outcome is ToolResultOutcome.EMPTY_SUCCESS:
                    # 唯一记录"provider 问过且回答没有"的通道。成功信封没有
                    # error / degradation_reason，下游否则无法把它与"根本没问过
                    # provider"区分开，归因就只能落到 schema_gate。
                    envelope["metadata"][PROVIDER_RESULT_OUTCOME_METADATA_KEY] = (
                        ToolResultOutcome.EMPTY_SUCCESS.value
                    )
                if attempt > 0:
                    envelope["metadata"].update({
                        "retry_count": attempt,
                        "recovered_after_retry": True,
                    })
                return await gateway.after_call(
                    envelope,
                    manifest=main_manifest,
                    run_id=run_id,
                    audit_store=tool_audit_store,
                    post_policy_hook=persist_provider_snapshot,
                )
            else:
                last_error = outcome_reason
                # 成因必须在 INFO 可见：否则后面的"降级到 X"没有任何成因行。
                logger.info(
                    "工具 [%s] 调用失败（attempt %d/%d）: %s",
                    tool_name,
                    attempt + 1,
                    max_retries + 1,
                    last_error,
                )
                is_retryable = _is_retryable_tool_error(last_error)
                if is_retryable and attempt < max_retries:
                    await record_retry_failure(last_error, attempt)
                    await await_model_operation(
                        asyncio.sleep(1.0 * (2 ** attempt)),
                        operation=f"tool.{tool_name}.retry_backoff",
                    )
                    continue
                break
        except RunBudgetExhausted as exc:
            # 与窗口关闭同一类边界：返回一个可解释的降级，不抛成 Run 失败。
            last_error = str(exc)
            budget_exhausted = exc.dimension
            break
        except ModelWindowClosed as exc:
            last_error = str(exc)
            research_window_closed = True
            break
        except (TimeoutError, ConnectionError, RuntimeError, OSError, ValueError) as e:
            err_str = str(e).lower()
            is_retryable = _is_retryable_tool_error(err_str)
            last_error = str(e)
            if is_retryable and attempt < max_retries:
                wait = 1.0 * (2 ** attempt)
                logger.warning(f"工具 [{tool_name}] 可重试错误（attempt {attempt + 1}），{wait:.0f}s 后重试: {e}")
                await record_retry_failure(last_error, attempt)
                try:
                    await await_model_operation(
                        asyncio.sleep(wait),
                        operation=f"tool.{tool_name}.retry_backoff",
                    )
                except ModelWindowClosed as closed:
                    last_error = str(closed)
                    research_window_closed = True
                    break
                continue
            elif attempt == max_retries:
                logger.error(f"工具 [{tool_name}] 执行失败（已重试 {max_retries} 次）: {e}")
            else:
                logger.error(f"工具 [{tool_name}] 不可重试错误: {e}")
                break

    # ── 降级策略 ─────────────────────────────────────────────────────
    # 重试耗尽后尝试备用工具（详见 tools/fallback.py: _FALLBACK_MAP）。
    # 降级是系统级决策（非 LLM 发起），但仍必须通过 agent 级 allowed_tool_names
    # 与 ToolGateway；否则记录 fallback boundary reason 后返回 failed envelope。
    blocked_fallback_to: Optional[str] = None
    try:
        from ..tools.fallback import get_fallback_tool, build_fallback_args
        fallback_info = get_fallback_tool(tool_name)
        if (
            not research_window_closed
            # 预算用完之后不再降级：降级也是一次真实的 Provider 调用。
            and budget_exhausted is None
            and allow_fallback
            and fallback_info
            and main_manifest.allow_offline_fallback
        ):
            fallback_name, adapter_key = fallback_info
            if registry.has_tool(fallback_name):
                if allowed_tool_names is not None and fallback_name not in allowed_tool_names:
                    blocked_fallback_to = fallback_name
                    boundary_reason = (
                        f"主工具失败后可降级到 {fallback_name}，但该备用工具不在当前 agent allowlist 中"
                    )
                    logger.warning(
                        "工具 [%s] 降级被 agent allowlist 阻止: fallback=%s node=%s",
                        tool_name,
                        fallback_name,
                        node_name,
                    )
                    last_error = f"{last_error or '主工具失败'}；{boundary_reason}"
                    raise PermissionError(boundary_reason)
                fallback_args = build_fallback_args(tool_name, fallback_name, adapter_key, arguments)
                logger.info(
                    "工具 [%s] 降级到 [%s] | cause=%s args=%s",
                    tool_name,
                    fallback_name,
                    last_error or "主工具失败",
                    fallback_args,
                )
                try:
                    fallback_before = await gateway.before_call(
                        tool_name=fallback_name,
                        arguments=fallback_args,
                        registry=registry,
                        allowed_tool_names=allowed_tool_names,
                        run_id=run_id,
                        node_name=node_name,
                        audit_store=tool_audit_store,
                    )
                    if fallback_before.envelope is not None:
                        return fallback_before.envelope
                    # 降级也是一次真实的 Provider 调用，记在备用工具自己的名下。
                    if ledger is not None:
                        ledger.reserve_tool_call(f"tool.{fallback_name}", fallback_name)
                    fallback_result = await await_model_operation(
                        registry.execute(fallback_name, **fallback_args),
                        operation=f"tool.{fallback_name}.fallback",
                    )
                    fallback_outcome, fallback_reason = classify_tool_result(
                        fallback_name, fallback_result
                    )
                    if fallback_outcome is not ToolResultOutcome.FAILED:
                        if fallback_outcome is ToolResultOutcome.EMPTY_SUCCESS:
                            logger.info(
                                "备用工具 [%s] provider 成功应答但零命中: %s",
                                fallback_name,
                                fallback_reason,
                            )
                        fallback_result = _attach_freshness_metadata(fallback_name, fallback_result)
                        fallback_meta = registry.get_tool_metadata(fallback_name)
                        envelope = build_tool_execution_envelope(
                            tool_name=fallback_name,
                            arguments=fallback_args,
                            result=fallback_result,
                            status=ToolExecutionStatus.DEGRADED.value,
                            source=str(fallback_meta.get("source") or "unknown"),
                            server_name=fallback_meta.get("server_name"),
                            category=fallback_before.manifest.category,
                            fallback_from=tool_name,
                            fallback_to=fallback_name,
                            degradation_reason=last_error or "主工具失败后降级",
                            retrieved_at=fallback_result.get("retrieved_at"),
                            freshness_hint=fallback_result.get("freshness_hint"),
                            activation_source=activation_source,
                        )
                        if fallback_outcome is ToolResultOutcome.EMPTY_SUCCESS:
                            envelope["metadata"][
                                PROVIDER_RESULT_OUTCOME_METADATA_KEY
                            ] = ToolResultOutcome.EMPTY_SUCCESS.value
                        return await gateway.after_call(
                            envelope,
                            manifest=fallback_before.manifest,
                            run_id=run_id,
                            audit_store=tool_audit_store,
                            gateway_decision=ToolGatewayDecision.DEGRADE,
                        )
                    last_error = (
                        f"{last_error or '主工具失败'}；备用工具 {fallback_name} 失败: {fallback_reason}"
                    )
                    logger.info(
                        "备用工具 [%s] 也失败: %s", fallback_name, fallback_reason
                    )
                except ModelWindowClosed as closed:
                    research_window_closed = True
                    last_error = str(closed)
                except Exception as fe:
                    fallback_error = str(fe)
                    logger.warning(f"备用工具 [{fallback_name}] 也失败: {fallback_error}")
                    last_error = f"{last_error or '主工具失败'}；备用工具失败: {fallback_error}"
            else:
                logger.warning(f"备用工具 [{fallback_name}] 未在注册表中，跳过降级")
                last_error = f"{last_error or '主工具失败'}；备用工具 {fallback_name} 未注册"
    except PermissionError:
        pass
    except Exception as e:
        # 降级策略是辅助路径：fallback.py 导入或映射查找失败不应中断主流程
        logger.debug(f"降级策略查找失败: {e}")

    envelope = build_tool_execution_envelope(
        tool_name=tool_name,
        arguments=arguments,
        status=ToolExecutionStatus.FAILED.value,
        error=last_error or "未知错误",
        source=str(main_meta.get("source") or "unknown"),
        server_name=main_meta.get("server_name"),
        category=main_manifest.category,
        fallback_to=blocked_fallback_to,
        degradation_reason=last_error,
        activation_source=activation_source,
    )
    if budget_exhausted is not None:
        envelope["metadata"]["run_budget_exhausted"] = budget_exhausted
    if research_window_closed:
        envelope["metadata"]["research_window_closed"] = True
    return await gateway.after_call(
        envelope,
        manifest=main_manifest,
        run_id=run_id,
        audit_store=tool_audit_store,
    )


class ToolResultOutcome(str, Enum):
    """工具单次调用的三态结果。

    ``EMPTY_SUCCESS`` 是关键区分：provider 完成了这次调用并明确回答"没有命中"
    （``success=True`` + 空结果集）。这是一个**成功的 provider 应答**，不是工具
    失败，因此既不重试也不降级到别的工具——换一个工具去回答同一个问题属于静默
    兜底。模型会在 ReAct 循环里看到这条空结果，自行改写查询或换实体。
    """

    FAILED = "failed"
    EMPTY_SUCCESS = PROVIDER_RESULT_OUTCOME_EMPTY_SUCCESS
    CONTENT = "content"


def classify_tool_result(
    tool_name: str, result: Any
) -> Tuple[ToolResultOutcome, str]:
    """把一次工具返回归类为三态，并给出一句可直接进日志的原因。

    reason 只在 ``FAILED`` / ``EMPTY_SUCCESS`` 时有内容；调用方必须把它记到
    INFO，否则"降级到 X"这类行就没有任何可归因的成因。
    """
    if not isinstance(result, dict) or not result:
        return ToolResultOutcome.FAILED, f"{tool_name} 未返回结构化结果"
    if result.get("type") == "user_input_required":
        return ToolResultOutcome.CONTENT, ""
    if not result.get("success", True):
        return ToolResultOutcome.FAILED, str(result.get("error") or f"{tool_name} 报告失败")
    payload = (
        result.get("content")
        or result.get("result")
        or result.get("results")
        or result.get("routes")
        or result.get("data")
        or result.get("text")
    )
    if _contains_tool_error_text(payload):
        return ToolResultOutcome.FAILED, f"{tool_name} 在成功标记下返回错误文本"
    if not payload:
        return (
            ToolResultOutcome.EMPTY_SUCCESS,
            f"{tool_name} 成功应答但结果集为空（keys={sorted(result.keys())}）",
        )
    return ToolResultOutcome.CONTENT, ""


def _is_retryable_tool_error(error: str) -> bool:
    lowered = str(error or "").lower()
    non_retryable = (
        "usage limit",
        "quota",
        "配额",
        "额度",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "authentication",
        "http 400",
        "400 bad request",
        "http 401",
        "http 403",
    )
    if any(marker in lowered for marker in non_retryable):
        return False
    return any(
        marker in lowered
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "network",
            "refused",
            "reset",
            "temporarily",
            "rate limit",
            "http 429",
            "http 502",
            "http 503",
            "http 504",
            "超时",
            "连接失败",
            "网络错误",
            "暂时不可用",
        )
    )


def _contains_tool_error_text(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        return lowered.startswith(("搜索出错", "search error", "error:", "duckduckgo search failed"))
    if isinstance(value, dict):
        return any(_contains_tool_error_text(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_tool_error_text(v) for v in value)
    return False


def _find_published_at_hint(result: Dict[str, Any]) -> str:
    candidates: List[Any] = []
    candidates.append(result.get("published_at"))
    nested = result.get("result")
    if isinstance(nested, dict):
        candidates.append(nested.get("published_at"))
        meta = nested.get("metadata")
        if isinstance(meta, dict):
            candidates.append(meta.get("published_at"))
    data = result.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("published_at"))
        meta = data.get("metadata")
        if isinstance(meta, dict):
            candidates.append(meta.get("published_at"))
    meta = result.get("metadata")
    if isinstance(meta, dict):
        candidates.append(meta.get("published_at"))

    for v in candidates:
        if v is None:
            continue
        text = str(v).strip()
        if text:
            return text
    return ""


def _attach_freshness_metadata(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {"success": False, "error": f"{tool_name} 返回格式异常"}
    if not result.get("success", True):
        return result

    retrieved_at = str(result.get("retrieved_at") or "").strip()
    if not retrieved_at:
        retrieved_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result["retrieved_at"] = retrieved_at

    published_at = _find_published_at_hint(result)
    if published_at and not str(result.get("published_at") or "").strip():
        result["published_at"] = published_at

    # 作为 evidence 提取时的兜底提示字段，尽量不覆盖工具原生返回
    if "source_type" not in result:
        result["source_type"] = "tool"
    result["freshness_hint"] = {
        "tool_name": tool_name,
        "source_type": str(result.get("source_type") or "tool"),
        "published_at": str(result.get("published_at") or ""),
        "retrieved_at": retrieved_at,
    }
    return result


# ---------------------------------------------------------------------------
# 工具结果格式化（UI 展示）
# ---------------------------------------------------------------------------

def _summarize_args(arguments: Dict[str, Any]) -> str:
    """将工具参数压缩为单行可读字符串。"""
    if not arguments:
        return ""
    parts = []
    for k, v in arguments.items():
        v_str = str(v)
        if len(v_str) > 60:
            v_str = v_str[:57] + "..."
        parts.append(f"{k}={v_str}")
    return ", ".join(parts)


def _summarize_result(result: Dict[str, Any]) -> str:
    """通用工具返回值压缩摘要。"""
    if not result:
        return "无结果"
    if result.get("type") == "user_input_required":
        return "等待用户输入"
    if not result.get("success", True):
        # One wording, owned by ``tools/governance``: the summary is the line a
        # traveller reads, and the reason lives in the envelope's ``error``.
        # Never format the raw exception text here — that bypasses even the
        # sanitizing the envelope path does.
        return TOOL_FAILURE_SUMMARY

    inner = result.get("result") or result.get("content") or result.get("data")
    if inner is None:
        return str(result)[:120].replace("\n", " ")
    if isinstance(inner, str):
        return inner.strip()[:120].replace("\n", " ")
    if isinstance(inner, dict):
        for key in ("total_cny", "converted", "rate", "destination"):
            if key in inner:
                return f"{key}: {inner[key]}"
        return str(inner)[:120]
    if isinstance(inner, list):
        return f"共 {len(inner)} 条结果"
    return str(inner)[:120]


# 工具分类（用于 UI 展示优先级）
TOOL_CATEGORIES: Dict[str, str] = {
    "global_place_search": "search",
    # 地图（高德 amap-maps）
    "maps_geo": "internal",
    "maps_text_search": "search",
    "maps_around_search": "search",
    "maps_weather": "data",
    "maps_direction_driving": "data",
    "maps_direction_walking": "data",
    "maps_direction_transit_integrated": "data",
    # 火车（12306-train）
    "get-station-code-of-citys": "internal",
    "get-tickets": "data",
    "get-interline-tickets": "data",
    # 航班（Duffel duffel-flights）
    "search_flights": "data",
    # 检索
    "free_web_search": "search",
}


def _extract_geocode_summary(result: Dict[str, Any]) -> str:
    try:
        content_str = result.get("content", "")
        if isinstance(content_str, list):
            content_str = content_str[0].get("text", "") if content_str else ""
        if isinstance(content_str, str) and content_str.startswith("{"):
            data = json.loads(content_str)
            # 高德 maps_geo: {"results": [{"location": "lng,lat", ...}]}
            results = data.get("results")
            if isinstance(results, list) and results:
                loc_str = results[0].get("location")
                if isinstance(loc_str, str) and "," in loc_str:
                    lng_s, lat_s = loc_str.split(",", 1)
                    addr = results[0].get("formatted_address") or results[0].get("level") or ""
                    prefix = f"已定位 {addr}" if addr else "已定位坐标"
                    return f"{prefix}（{float(lat_s):.4f}°N, {float(lng_s):.4f}°E）"
            loc = data.get("location", {})
            lat = loc.get("lat", "")
            lng = loc.get("lng", "")
            address = data.get("formatted_address") or data.get("level") or ""
            if lat and lng:
                if address:
                    return f"已定位 {address}（{float(lat):.4f}°N, {float(lng):.4f}°E）"
                return f"已定位坐标（{float(lat):.4f}°N, {float(lng):.4f}°E）"
    except Exception:
        pass
    return "地理编码完成"


def _extract_weather_summary(result: Dict[str, Any]) -> str:
    try:
        content_str = result.get("content", "")
        if isinstance(content_str, list):
            content_str = content_str[0].get("text", "") if content_str else ""
        if isinstance(content_str, str) and content_str.startswith("{"):
            data = json.loads(content_str)
            forecasts = data.get("forecasts", [{}])
            if forecasts:
                f = forecasts[0]
                city = f.get("city", "")
                casts = f.get("casts", [{}])
                if casts:
                    today = casts[0]
                    weather = today.get("dayweather", "")
                    high = today.get("nighttemp", "")
                    low = today.get("daytemp", "")
                    parts = [city, weather]
                    if high and low:
                        parts.append(f"{low}~{high}°C")
                    return " ".join(p for p in parts if p)
    except Exception:
        pass
    return "天气查询完成"


def _extract_search_summary(result: Dict[str, Any]) -> str:
    try:
        content_str = result.get("content", "")
        if isinstance(content_str, list):
            content_str = content_str[0].get("text", "") if content_str else ""
        if isinstance(content_str, str) and content_str.startswith("{"):
            data = json.loads(content_str)
            results = data.get("results", [])
            if results:
                names = [r.get("name", "") for r in results[:3] if r.get("name")]
                suffix = f"等 {len(results)} 个地点"
                if names:
                    return f"找到 {suffix}：{', '.join(names)}"
                return f"找到 {suffix}"
    except Exception:
        pass
    return "搜索完成"


def _extract_web_search_summary(result: Dict[str, Any]) -> str:
    try:
        content_str = result.get("content", "")
        if isinstance(content_str, list):
            items = [c.get("text", "") for c in content_str if isinstance(c, dict)]
            content_str = " ".join(items)
        if isinstance(content_str, str):
            lines = [line.strip() for line in content_str.split("\n") if line.strip()]
            if lines:
                preview = lines[0][:60]
                return f"搜索到 {len(lines)} 条结果" + (f"：{preview}…" if preview else "")
    except Exception:
        pass
    return "网络搜索完成"


def _extract_direction_summary(result: Dict[str, Any]) -> str:
    try:
        content_str = result.get("content", "")
        if isinstance(content_str, list):
            content_str = content_str[0].get("text", "") if content_str else ""
        if isinstance(content_str, str) and content_str.startswith("{"):
            data = json.loads(content_str)
            routes = data.get("routes", [{}])
            if routes:
                r = routes[0]
                distance = r.get("distance", "")
                duration = r.get("duration", "")
                parts = []
                if distance:
                    dist_km = int(distance) / 1000 if str(distance).isdigit() else distance
                    parts.append(f"{dist_km:.1f}km" if isinstance(dist_km, float) else str(dist_km))
                if duration:
                    mins = int(duration) // 60 if str(duration).isdigit() else duration
                    parts.append(f"约 {mins} 分钟" if isinstance(mins, int) else str(mins))
                if parts:
                    return "路线：" + "，".join(parts)
    except Exception:
        pass
    return "路线规划完成"


def _extract_station_summary(result: Dict[str, Any]) -> str:
    try:
        content_str = result.get("content", "")
        if isinstance(content_str, list):
            content_str = content_str[0].get("text", "") if content_str else ""
        if isinstance(content_str, str) and content_str.startswith("{"):
            data = json.loads(content_str)
            stations = [v.get("station_name", k) for k, v in data.items() if isinstance(v, dict)]
            if stations:
                return f"已获取车站代码：{', '.join(stations)}"
    except Exception:
        pass
    return "车站代码查询完成"


_TOOL_SEMANTIC_SUMMARIZERS: Dict[str, Any] = {
    "global_place_search": _extract_search_summary,
    "maps_geo": _extract_geocode_summary,
    "maps_geocode": _extract_geocode_summary,
    "maps_weather": _extract_weather_summary,
    "maps_text_search": _extract_search_summary,
    "maps_around_search": _extract_search_summary,
    "maps_search_places": _extract_search_summary,
    "maps_direction_driving": _extract_direction_summary,
    "maps_direction_walking": _extract_direction_summary,
    "maps_direction_transit_integrated": _extract_direction_summary,
    "maps_directions": _extract_direction_summary,
    "free_web_search": _extract_web_search_summary,
    "get-station-code-of-citys": _extract_station_summary,
}


def _summarize_tool_result(tool_name: str, result: Dict[str, Any]) -> str:
    """按工具类型生成语义化摘要，用于 UI 展示。"""
    if not result:
        return "无结果"
    if is_tool_execution_envelope(result):
        return str(result.get("result_summary") or "无结果")
    if result.get("type") == "user_input_required":
        return "等待用户输入"
    if not result.get("success", True):
        # One wording, owned by ``tools/governance``: the summary is the line a
        # traveller reads, and the reason lives in the envelope's ``error``.
        # Never format the raw exception text here — that bypasses even the
        # sanitizing the envelope path does.
        return TOOL_FAILURE_SUMMARY

    summarizer = _TOOL_SEMANTIC_SUMMARIZERS.get(tool_name)
    if summarizer:
        try:
            summary = summarizer(result)
            if summary:
                return summary
        except Exception as e:  # 工具返回格式不可预测，摘要失败不影响主流程
            logger.debug(f"工具摘要器 [{tool_name}] 异常: {e}")
    return _summarize_result(result)


# ---------------------------------------------------------------------------
# model-facing tool content budget
# ---------------------------------------------------------------------------
# Tool role messages are dual-use: (1) next-turn LLM context, (2) Research
# Packet parse transcript (place/route identity + failed sources). Compact
# structure, never plain UI-only summary strings.

_TOOL_CONTENT_CHAR_LIMIT = 6000
_PLACE_RESULT_CAP = 12
_ROUTE_RESULT_CAP = 8
_GENERIC_LIST_CAP = 16
_STRING_FIELD_CAP = 400

_PLACE_IDENTITY_KEYS = (
    "place_id",
    "name",
    "address",
    "provider_place_type",
    "provider_country_code",
    "provider",
    "latitude",
    "longitude",
    "aliases",
    "station_code",
)
_ROUTE_IDENTITY_KEYS = (
    "route_id",
    "mode",
    # Required by has_provider_route_selection_option / TransportCandidate binding.
    "transport_class",
    "selected_mode",
    "from_endpoint",
    "to_endpoint",
    "from_place_id",
    "to_place_id",
    "departure_at",
    "arrival_at",
    "duration_minutes",
    "distance_meters",
    "cost",
    "segments",
    "provider",
    "train_no",
    "prices",
)
_ENVELOPE_TOP_KEYS = (
    "schema_version",
    "audit_id",
    "tool_name",
    "server_name",
    "category",
    "source_type",
    "trust_level",
    "status",
    "args_digest",
    "sanitized_args_summary",
    "result_summary",
    "untrusted_content",
    "retrieved_at",
    "freshness_hint",
    "fallback_from",
    "fallback_to",
    "degradation_reason",
    "error",
    "evidence_candidate",
)
_METADATA_KEEP_KEYS = (
    "evidence_allowed",
    "quarantine_result",
    QUALITY_VERIFIED_PLACE_IDS_METADATA_KEY,
    "snapshot_cache",
    "provider_snapshot",
    "place_ids",
    "route_ids",
)


def _clip_str(value: str, limit: int = _STRING_FIELD_CAP) -> str:
    text = value if len(value) <= limit else value[: limit - 1].rstrip() + "…"
    return text


def _compact_mapping_fields(
    item: Mapping[str, Any],
    keys: tuple[str, ...],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in keys:
        if key not in item:
            continue
        value = item[key]
        if isinstance(value, str):
            out[key] = _clip_str(value)
        elif isinstance(value, dict):
            # Nested endpoint-like objects: keep identity keys only.
            nested = {
                nk: (_clip_str(nv) if isinstance(nv, str) else nv)
                for nk, nv in value.items()
                if nk in _PLACE_IDENTITY_KEYS or nk in {"name", "place_id", "latitude", "longitude", "station_code"}
            }
            out[key] = nested
        elif isinstance(value, list):
            out[key] = value[:_GENERIC_LIST_CAP]
        else:
            out[key] = value
    return out


def _compact_sanitized_result(result: Any) -> Any:
    if not isinstance(result, dict):
        if isinstance(result, str):
            return _clip_str(result, 800)
        if isinstance(result, list):
            return [
                _compact_sanitized_result(item) for item in result[:_GENERIC_LIST_CAP]
            ]
        return result

    compact: Dict[str, Any] = {}
    for key, value in result.items():
        if key == "results" and isinstance(value, list):
            places: List[Any] = []
            for item in value[:_PLACE_RESULT_CAP]:
                if isinstance(item, dict):
                    places.append(_compact_mapping_fields(item, _PLACE_IDENTITY_KEYS))
                else:
                    places.append(item)
            compact[key] = places
        elif key == "routes" and isinstance(value, list):
            routes: List[Any] = []
            for item in value[:_ROUTE_RESULT_CAP]:
                if isinstance(item, dict):
                    routes.append(_compact_mapping_fields(item, _ROUTE_IDENTITY_KEYS))
                else:
                    routes.append(item)
            compact[key] = routes
        elif key in {
            "success",
            "error",
            "message",
            "type",
            "question",
            "options",
            "selection_type",
            "allow_free_input",
            "provider",
        }:
            compact[key] = value
        elif isinstance(value, str):
            compact[key] = _clip_str(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            compact[key] = value
        elif isinstance(value, list):
            # Keep id lists and short scalar lists (quality_verified_place_ids, etc.).
            if value and all(isinstance(item, (str, int, float, bool)) or item is None for item in value):
                compact[key] = value[: max(_GENERIC_LIST_CAP, _PLACE_RESULT_CAP)]
            else:
                compact[key] = [
                    _compact_sanitized_result(item) for item in value[:_GENERIC_LIST_CAP]
                ]
        elif isinstance(value, dict):
            # Recurse so nested sanitized_result / place wrappers keep results[].
            compact[key] = _compact_sanitized_result(value)
    return compact


def _looks_like_tool_envelope(value: Mapping[str, Any]) -> bool:
    """True for full ToolExecutionEnvelope or worker-injected partial envelopes."""
    if is_tool_execution_envelope(value):
        return True
    return (
        "sanitized_result" in value
        and ("tool_name" in value or "audit_id" in value)
        and "status" in value
    )


def _compact_envelope_dict(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {}
    for key in _ENVELOPE_TOP_KEYS:
        if key in envelope:
            value = envelope[key]
            compact[key] = _clip_str(value) if isinstance(value, str) else value
    # Partial worker-injected envelopes may omit schema_version but still carry
    # tool_name / audit_id / status (already copied when present in TOP_KEYS).
    for key in ("tool_name", "audit_id", "status"):
        if key in envelope and key not in compact:
            compact[key] = envelope[key]
    metadata = envelope.get("metadata")
    if isinstance(metadata, dict):
        meta_out: Dict[str, Any] = {}
        for key in _METADATA_KEEP_KEYS:
            if key in metadata:
                meta_out[key] = metadata[key]
        # Preserve quality place id lists even under alternate keys.
        for key, value in metadata.items():
            if (key.endswith("_place_ids") or key.endswith("_ids")) and key not in meta_out:
                meta_out[key] = value
            if key in {"evidence_allowed", "quarantine_result"} and key not in meta_out:
                meta_out[key] = value
        if meta_out:
            compact["metadata"] = meta_out
    if "sanitized_result" in envelope:
        compact["sanitized_result"] = _compact_sanitized_result(
            envelope.get("sanitized_result")
        )
    return compact


def compact_tool_content_for_model(tool_result: Any) -> str:
    """Serialize a tool result for LLM / packet-transcript messages.

    Preserves ToolExecutionEnvelope shape and place/route identity fields so
    ``research_packet_output`` can still bind provider evidence, while capping
    payload size so multi-round ReAct does not fill the context window.

    Always returns parseable JSON (never mid-JSON string chops).
    """
    if tool_result is None:
        return "null"
    if isinstance(tool_result, dict) and _looks_like_tool_envelope(tool_result):
        compact: Any = _compact_envelope_dict(tool_result)
    elif isinstance(tool_result, dict):
        compact = _compact_sanitized_result(tool_result)
    else:
        compact = tool_result

    text = json.dumps(compact, ensure_ascii=False, default=str)
    if len(text) <= _TOOL_CONTENT_CHAR_LIMIT:
        return text

    # Hard ceiling: drop bulky nested result first, keep audit/status/summary.
    if isinstance(compact, dict):
        reduced = dict(compact)
        if "sanitized_result" in reduced:
            summary = reduced.get("result_summary") or "result truncated for model budget"
            # Preserve place/route identity lists from the compact form when
            # present so parse can still bind endpoints after size pressure.
            prior = reduced.get("sanitized_result")
            identity_only: Dict[str, Any] = {
                "truncated": True,
                "note": _clip_str(str(summary), 240),
            }
            if isinstance(prior, dict):
                if isinstance(prior.get("results"), list):
                    identity_only["results"] = prior["results"][:_PLACE_RESULT_CAP]
                    identity_only["success"] = prior.get("success", True)
                if isinstance(prior.get("routes"), list):
                    identity_only["routes"] = prior["routes"][:_ROUTE_RESULT_CAP]
                    identity_only["success"] = prior.get("success", True)
            reduced["sanitized_result"] = identity_only
            text = json.dumps(reduced, ensure_ascii=False, default=str)
            if len(text) <= _TOOL_CONTENT_CHAR_LIMIT:
                return text
            # Still too large: keep only top place/route id strings.
            ids: List[str] = []
            for item in identity_only.get("results") or []:
                if isinstance(item, dict) and item.get("place_id"):
                    ids.append(str(item["place_id"]))
            for item in identity_only.get("routes") or []:
                if isinstance(item, dict) and item.get("route_id"):
                    ids.append(str(item["route_id"]))
            reduced["sanitized_result"] = {
                "truncated": True,
                "success": True,
                "identity_ids": ids[:32],
                "note": _clip_str(str(summary), 160),
            }
            text = json.dumps(reduced, ensure_ascii=False, default=str)
            if len(text) <= _TOOL_CONTENT_CHAR_LIMIT:
                return text
        reduced.pop("evidence_candidate", None)
        reduced.pop("freshness_hint", None)
        text = json.dumps(reduced, ensure_ascii=False, default=str)
        if len(text) <= _TOOL_CONTENT_CHAR_LIMIT:
            return text
        minimal = {
            "audit_id": reduced.get("audit_id"),
            "tool_name": reduced.get("tool_name"),
            "status": reduced.get("status"),
            "result_summary": _clip_str(str(reduced.get("result_summary") or "truncated"), 200),
            "error": reduced.get("error"),
            "degradation_reason": reduced.get("degradation_reason"),
            "fallback_from": reduced.get("fallback_from"),
            "fallback_to": reduced.get("fallback_to"),
            "metadata": reduced.get("metadata"),
            "truncated": True,
        }
        return json.dumps(minimal, ensure_ascii=False, default=str)

    # Non-dict oversized payloads: wrap as a small valid JSON object.
    return json.dumps(
        {"truncated": True, "note": _clip_str(text, 400)},
        ensure_ascii=False,
    )


def _normalize_tool_arguments(arguments: Dict[str, Any]) -> str:
    """Return a stable JSON representation for idempotent tool replay keys."""
    return json.dumps(
        arguments or {},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _make_cache_key(tool_name: str, arguments: Dict[str, Any]) -> str:
    """生成工具调用缓存键：工具名 + 规范化参数 hash。"""
    normalized_args = _normalize_tool_arguments(arguments)
    digest = hashlib.sha256(normalized_args.encode("utf-8")).hexdigest()[:24]
    return f"{tool_name}:{digest}"


_NO_CACHE_TOOLS: Set[str] = {
    "ask_user",
    # These use Redis-backed ProviderSnapshotCache at the Gateway boundary.
    # Replaying their old workflow-local Envelope would bypass TTL/provenance
    # and would incorrectly reuse a previous Run's audit id.
    "global_place_search",
    "global_route_search",
}


def _cacheable_tool_result(tool_name: str, result: Dict[str, Any]) -> bool:
    if tool_name in _NO_CACHE_TOOLS or not isinstance(result, dict):
        return False
    if result.get("status") not in {"success", "degraded"}:
        return False
    sanitized_result = result.get("sanitized_result") if is_tool_execution_envelope(result) else result
    return not (
        isinstance(sanitized_result, dict)
        and sanitized_result.get("type") == "user_input_required"
    )


# ---------------------------------------------------------------------------
# 流式 ReAct 循环（供所有 Worker Agent 共享）
# ---------------------------------------------------------------------------


async def streaming_react_loop(
    llm: Any,
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    available_tools: List[Dict[str, Any]],
    stream_queue: Optional["SSEBuffer"],
    node_name: str,
    max_iterations: int = 5,
    can_ask_user: bool = False,
    tool_cache: Optional[Dict[str, Any]] = None,
    tool_context: Optional[Dict[str, Any]] = None,
) -> Tuple[
    str,
    List[str],
    Optional[Dict[str, Any]],
    List[Dict[str, Any]],
]:
    """
    通用流式 ReAct 循环。

    每轮迭代：
    1. 调用 llm.astream_with_tools，实时推送 react_thinking 事件
    2. 若检测到 tool_calls：推送 tool_start → 执行（优先命中缓存）→ 推送 tool_done
    3. 将工具结果注入 messages，进入下一轮

    返回：(final_content, tool_results_summary, pending_choice,
    authoritative_tool_results)。最后一项保留完整 Tool Gateway envelope，
    不受 model-facing message budget 影响。
    """
    tool_results_summary: List[str] = []
    authoritative_tool_results: List[Dict[str, Any]] = []
    last_text_content = ""
    tool_context = tool_context or {}

    # ── Tool Search 按需工具曝光 ────────────────────────────────────────────
    # deferred 时初始只暴露 search_tools 元工具 + 把压缩目录注入 system prompt；模型经
    # search_tools 按需激活的工具进入 activated_tools，当轮并入 tool_schemas 且延续后轮。
    # available_tools（白名单）保持不变——既是执行 allowlist，也是 search_tools 的检索边界。
    exposure_plan = apply_tool_exposure(available_tools, node_name)
    tool_schemas = list(exposure_plan.tool_schemas)
    activated_tools: Set[str] = set()
    if exposure_plan.deferred:
        if messages and messages[0].get("role") == "system":
            messages[0] = {
                **messages[0],
                "content": messages[0]["content"] + exposure_plan.catalog_prompt,
            }
        else:
            messages.insert(0, {"role": "system", "content": exposure_plan.catalog_prompt.lstrip()})
    get_tool_exposure_ledger().record(
        tool_context.get("run_id"),
        agent=exposure_plan.agent,
        deferred=exposure_plan.deferred,
        injected_tokens=exposure_plan.injected_tokens,
        full_tokens=exposure_plan.full_tokens,
        exposed_tool_count=exposure_plan.exposed_tool_count,
        full_tool_count=exposure_plan.full_tool_count,
    )
    # deferred 首轮通常耗在 search_tools 这一跳上，补 1 轮预算让「实际调研」轮数与 full 对齐，
    # 避免按需曝光牺牲研究深度（仍有限，不会无界搜索）。
    effective_max_iterations = max_iterations + (1 if exposure_plan.deferred else 0)

    for iteration in range(effective_max_iterations):
        check_cancel_requested(node_name)
        try:
            # Do not start another open-ended model/tool round after minute
            # six.  The caller will return its typed partial packet and the
            # graph moves into Candidate Gate's deterministic closeout path.
            remaining_model_seconds(f"react.{node_name}.iteration_{iteration + 1}")
        except ModelWindowClosed as exc:
            logger.info("[%s] research window closed: %s", node_name, exc)
            return last_text_content, tool_results_summary, None, authoritative_tool_results
        content = ""
        tool_calls: List[Dict[str, Any]] = []

        try:
            async for event in llm.astream_with_tools(messages, tool_schemas):
                if event["type"] == "text_delta":
                    content += event["content"]
                    if stream_queue is not None:
                        await stream_queue.put(("react_thinking", node_name, event["content"]))
                elif event["type"] == "reasoning_delta":
                    if stream_queue is not None:
                        await stream_queue.put(("react_thinking", node_name, event["content"]))
                elif event["type"] == "finish":
                    finish_content = event.get("content")
                    if finish_content:
                        content = finish_content
                    tool_calls = event.get("tool_calls", [])
        except Exception as e:
            logger.error(f"[{node_name}] 流式 LLM 调用失败 (迭代 {iteration}): {e}")
            return content or "", tool_results_summary, None, authoritative_tool_results

        if content.strip():
            last_text_content = content

        if not tool_calls:
            return content, tool_results_summary, None, authoritative_tool_results

        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })

        pending_choice: Optional[Dict[str, Any]] = None
        fail_count = 0
        success_count = 0
        total_count = len(tool_calls)
        # 服务端在调用前作出的日期能力判定（``reference_only`` / ``not_applicable``）：
        # Provider 一次都没被调用，所以它既不是成功也不是失败，**不能进断路器的分子或
        # 分母** —— 否则「唯一一个工具撞上能力判定」会被判成这一轮全部失败，模型收到的是
        # 「工具不可用，别编」，而不是它真正需要的「这个数据源答不了这个日期，换个模态」。
        capability_tool_names: List[str] = []

        for tc in tool_calls:
            tc_name = tc.get("name", "")
            tc_args = tc.get("arguments", {})
            tc_id = tc.get("id", "") or f"tool_{uuid.uuid4().hex}"

            try:
                remaining_model_seconds(f"react.{node_name}.tool.{tc_name}")
            except ModelWindowClosed as exc:
                logger.info("[%s] suppressing tool after research cutoff: %s", node_name, exc)
                return (
                    content or last_text_content,
                    tool_results_summary,
                    None,
                    authoritative_tool_results,
                )

            # ── search_tools 元工具：就地激活白名单内工具的完整 schema ─────────────
            # 不经 registry/gateway 执行——检索范围恒为 available_tools（该 agent 白名单），
            # 命中的完整 schema 并入 tool_schemas 供当轮及后续轮次调用（activated_tools）。
            if tc_name == SEARCH_TOOLS_NAME:
                query = tc_args.get("query", "") if isinstance(tc_args, dict) else ""
                search_started = time.perf_counter()
                if stream_queue is not None:
                    await stream_queue.put(("tool_start", node_name, {
                        "name": tc_name,
                        "tool_call_id": tc_id,
                        "args_summary": _summarize_args(tc_args),
                        "category": "internal",
                        "ts_ms": run_ts_ms(),
                    }))
                matches = search_tool_items(query, available_tools, exclude=activated_tools)
                newly: List[str] = []
                for m in matches:
                    m_name = m.get("schema", {}).get("function", {}).get("name", "")
                    if not m_name or m_name in activated_tools:
                        continue
                    activated_tools.add(m_name)
                    tool_schemas.append(m["schema"])
                    newly.append(m_name)
                search_result = {
                    "success": True,
                    "activated": newly,
                    "catalog": compact_catalog_items(matches),
                    "note": (
                        f"已激活 {len(newly)} 个工具，现在可直接调用。"
                        if newly
                        else "未匹配到新工具，请更换关键词，或直接基于已有信息作答。"
                    ),
                }
                logger.info("[%s] search_tools(query=%r) 激活: %s", node_name, query, newly)
                if stream_queue is not None:
                    await stream_queue.put(("tool_done", node_name, {
                        "name": tc_name,
                        "tool_call_id": tc_id,
                        "summary": f"激活工具：{', '.join(newly)}" if newly else "未匹配到工具",
                        "status": ToolExecutionStatus.SUCCESS.value,
                        "category": "internal",
                        "duration_ms": round((time.perf_counter() - search_started) * 1000.0, 3),
                        "ts_ms": run_ts_ms(),
                    }))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(search_result, ensure_ascii=False),
                })
                continue

            cache_key = _make_cache_key(tc_name, tc_args)
            cache_hit = (
                tool_cache is not None
                and tc_name not in _NO_CACHE_TOOLS
                and cache_key in tool_cache
            )

            category = TOOL_CATEGORIES.get(tc_name, "other")

            tool_started = time.perf_counter()
            if cache_hit:
                tool_result = tool_cache[cache_key]
                logger.info(f"[{node_name}] 缓存命中: {tc_name}")
                if stream_queue is not None:
                    await stream_queue.put(("tool_start", node_name, {
                        "name": tc_name,
                        "tool_call_id": tc_id,
                        "args_summary": _summarize_args(tc_args),
                        "category": category,
                        "from_cache": True,
                        "ts_ms": run_ts_ms(),
                    }))
            else:
                if stream_queue is not None:
                    await stream_queue.put(("tool_start", node_name, {
                        "name": tc_name,
                        "tool_call_id": tc_id,
                        "args_summary": _summarize_args(tc_args),
                        "category": category,
                        "ts_ms": run_ts_ms(),
                    }))
                tool_result = await execute_tool(
                    tc_name,
                    tc_args,
                    available_tools,
                    run_id=tool_context.get("run_id"),
                    node_name=node_name,
                    tool_audit_store=tool_context.get("tool_audit_store"),
                    tool_gateway=tool_context.get("tool_gateway"),
                    activation_source="searched" if tc_name in activated_tools else "preloaded",
                    provider_snapshot_cache=tool_context.get("provider_snapshot_cache"),
                    trip_run_store=tool_context.get("trip_run_store"),
                )
                if tool_cache is not None and _cacheable_tool_result(tc_name, tool_result):
                    tool_cache[cache_key] = tool_result
            if isinstance(tool_result, dict):
                authoritative_tool_results.append(tool_result)

            # Cooperative cancel after each tool result; next LLM
            # round will also check at loop head.
            check_cancel_requested(node_name)

            # 处理 ask_user
            sanitized_result = tool_result.get("sanitized_result") if is_tool_execution_envelope(tool_result) else tool_result
            if isinstance(sanitized_result, dict) and sanitized_result.get("type") == "user_input_required":
                if can_ask_user:
                    logger.info(f"[{node_name}] ask_user: {sanitized_result.get('question', '')}")
                    pending_choice = {
                        "question": sanitized_result.get("question", ""),
                        "options": sanitized_result.get("options", []),
                        "selection_type": sanitized_result.get("selection_type", "single"),
                        "allow_free_input": sanitized_result.get("allow_free_input", True),
                    }
                else:
                    logger.info(f"[{node_name}] 无 ask_user 权限，忽略")
                if stream_queue is not None:
                    await stream_queue.put(("tool_done", node_name, {
                        "name": tc_name,
                        "tool_call_id": tc_id,
                        "summary": "等待用户输入" if can_ask_user else "（已忽略提问）",
                        "status": ToolExecutionStatus.SUCCESS.value,
                        "duration_ms": round((time.perf_counter() - tool_started) * 1000.0, 3),
                        "ts_ms": run_ts_ms(),
                    }))
                break

            tool_status = str(tool_result.get("status") or "")
            tool_succeeded = tool_status in _SUCCEEDED_TOOL_STATUSES
            tool_capability_declared = tool_status in CAPABILITY_DECLARATION_STATUSES
            if tool_capability_declared:
                capability_tool_names.append(tc_name)
            elif tool_succeeded:
                success_count += 1
            else:
                fail_count += 1

            summary = _summarize_tool_result(tc_name, tool_result)
            model_tool_content = compact_tool_content_for_model(tool_result)
            tool_results_summary.append(f"[{tc_name}]: {summary[:400]}")

            snapshot_cache_metadata = (
                tool_result.get("metadata", {}).get("snapshot_cache", {})
                if isinstance(tool_result.get("metadata"), dict)
                else {}
            )
            provider_snapshot_hit = (
                isinstance(snapshot_cache_metadata, dict)
                and snapshot_cache_metadata.get("origin") == "provider_snapshot_cache"
            )
            if stream_queue is not None:
                await stream_queue.put(("tool_done", node_name, {
                    "name": tc_name,
                    "tool_call_id": tc_id,
                    "summary": summary,
                    # ``status`` 是工具轮次结论的**唯一权威**。不许在旁边再下发一个布尔
                    # ``success``：它把上面的三值真相（成功 / 失败 / 能力判定）压回两值，
                    # 于是 Tool Gateway 明确写下的「能力判定刻意不是 error」这条不变量，
                    # 在 SSE 上被谎报成 Provider 失败。
                    "status": tool_status,
                    "audit_id": tool_result.get("audit_id"),
                    "degraded": tool_status == "degraded",
                    "fallback_from": tool_result.get("fallback_from"),
                    "fallback_to": tool_result.get("fallback_to"),
                    "category": category,
                    "from_cache": cache_hit or provider_snapshot_hit,
                    "duration_ms": round((time.perf_counter() - tool_started) * 1000.0, 3),
                    "ts_ms": run_ts_ms(),
                }))
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": model_tool_content,
            })

        if pending_choice is not None:
            return (
                content,
                tool_results_summary,
                pending_choice,
                authoritative_tool_results,
            )

        # 断路器只看真正执行过的工具：能力判定不进分子也不进分母。
        executed_count = total_count - len(capability_tool_names)
        notices: List[str] = []
        if fail_count > 0 and fail_count == executed_count:
            logger.warning(f"[{node_name}] 本轮 {executed_count} 个工具全部失败，注入断路提示")
            notices.append(
                "注意：本轮所有工具调用均失败，无法获取实时数据。"
                "请用一到两句话简要告知用户工具暂不可用，不要基于推测生成详细内容。"
            )
        if capability_tool_names and success_count == 0:
            # 这不是失败，是该数据源对该日期没有可查数据。模型需要的是换模态，
            # 不是停手，也不是把同一个工具再调一遍。
            names = "、".join(dict.fromkeys(capability_tool_names))
            logger.info(f"[{node_name}] 本轮命中日期能力判定：{names}，提示改用其它模态")
            notices.append(
                f"说明：{names} 未被调用，服务端已判定该数据源对你请求的日期没有可查数据"
                "（能力边界，不是调用失败，重试也不会有结果）。"
                f"请不要再调用 {names} 或用同一日期重试，"
                "改用其它交通模态或工具（例如航班、长途客运、通用路线检索、网页检索）"
                "去覆盖同一段行程；确实无任何可用工具时，如实说明该日期暂无法核实。"
            )
        if notices:
            messages.append({"role": "user", "content": "\n".join(notices)})

    return (
        content or last_text_content,
        tool_results_summary,
        None,
        authoritative_tool_results,
    )
