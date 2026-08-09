"""
Scope 阶段节点 (Domain Layer)

包含两个节点：
1. clarifier_node: Scope 入口——装配记忆上下文、必要时压缩，并守住「受控旅行身份必须
   存在」这条前提
2. brief_generator_node: 从受控旅行身份确定性派生 Research Brief

工作流路由：
  START → clarifier_node → brief_generator_node
  brief_generator_node → destination_geo_resolver → weather_context_builder → planner → ...

两个节点都不再调模型。旅行事实（目的地、日期、同行、风格）由用户在界面上确认后作为
受控身份进入，Scope 阶段只负责把它翻译成下游统一依据，不推断、不追问。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from langchain_core.runnables import RunnableConfig

from ...entities.state import TravelAgentState
from ..utils import session_history_for_context_builder
from .prompts import SCOPE_CONTEXT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_CLARIFIER_NODE = "scope_clarifier"


class ScopeIdentityError(RuntimeError):
    """Scope 阶段拿不到受控旅行身份。

    fail-closed：所有新建路径都必填受控身份（`schemas.py` 的 ChatRequest 与
    `chat.py` 的创建短路），所以到这里还缺身份只有一种成因——库里 identity 为 NULL
    的历史 run 被 chat-stream 续跑。这种 run 过去会掉进 clarifier 的模型追问分支，
    那条分支已经删除；现在明确失败，而不是静默走一条不再存在的旧路。
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(
            f"run {run_id or '<unknown>'} reached scope without a controlled trip identity"
        )


_RESEARCH_BRIEF_FIELDS = {
    "objective",
    "destination",
    "duration_days",
    "budget",
    "travel_style",
    "travel_party",
    "departure_city",
    "departure_city_status",
    "departure_time",
    "constraints",
    "dimensions_to_cover",
}
_RESEARCH_DIMENSIONS = {
    "目的地概览",
    "景点推荐",
    "美食推荐",
    "交通建议",
    "住宿建议",
    "签证信息",
    "文化礼仪",
    "实时天气",
    "行程规划",
    "预算估算",
}


def _validate_research_brief_payload(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("research brief must be an object")
    missing = sorted(_RESEARCH_BRIEF_FIELDS - value.keys())
    if missing:
        raise ValueError(f"research brief missing fields: {', '.join(missing)}")
    if not str(value.get("objective") or "").strip():
        raise ValueError("research brief requires objective")
    if not str(value.get("destination") or "").strip():
        raise ValueError("research brief requires destination")
    duration = value.get("duration_days")
    if duration is not None and (type(duration) is not int or duration <= 0):
        raise ValueError("research brief duration_days must be a positive integer or null")
    if value.get("departure_city_status") not in {"provided", "not_decided"}:
        raise ValueError("research brief has invalid departure_city_status")
    for field in ("budget", "travel_style", "travel_party", "departure_city", "departure_time"):
        if value.get(field) is not None and not isinstance(value.get(field), str):
            raise ValueError(f"research brief {field} must be a string or null")
    for field in ("constraints", "dimensions_to_cover"):
        items = value.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError(f"research brief {field} must be a string list")
    if any(item not in _RESEARCH_DIMENSIONS for item in value["dimensions_to_cover"]):
        raise ValueError("research brief has unsupported dimensions_to_cover")
    return value


async def _measure_session_size(
    state: TravelAgentState,
    system_prompt: str,
) -> Optional[Any]:
    """量本次会话有多大，只为回答「该不该压缩」这一个问题。

    **它不装配任何 prompt，这是刻意的。** 深度路径上模型真正读到的记忆全部经由
    Constraint Pack（``panels/constraint.py::format_constraint_pack_for_prompt``），
    那是唯一一条注入通道。此前这里传的是完整五层（偏好 / 画像 / 跨会话记忆检索），
    ``ContextBuilder`` 于是又做了一次记忆检索、又拼出一份 system prompt ——
    **而那份 prompt 一个字都没进过任何模型调用**：同一个角色两套装配，其中一套
    静默胜出，另一套每轮照付一次库查询与一次 token 计账的钱。

    会话大小只由会话自己的东西决定：历史消息 + 已压缩的 anchor + 基础 system。
    偏好与记忆的 token 开销落在 worker prompt 上，不在这条会话轴上累积，所以
    把它们算进这个阈值反而会让压缩在错误的时刻触发。这里因此不传任何记忆层入参 ——
    也传不了：``ContextBuilder`` 上已经没有那半边了。此前它有一个 ``user_id``
    开关，这里传的是常量空串，于是被它守着的那段代码在两条路径上都到不了。
    """
    from ...memory.compressor import AnchorSummary
    from ...memory.context_builder import ContextBuilder

    # 不设条数上限：裁剪与压缩判断都归预算层。
    raw_history = session_history_for_context_builder(state)

    session_anchor_obj = None
    if state.session_anchor:
        try:
            session_anchor_obj = AnchorSummary.from_dict(state.session_anchor)
        except Exception as e:
            logger.debug("AnchorSummary 解析失败（Scope 上下文）: %s", e)

    return await ContextBuilder().build_context(
        session_id=state.session_id or "",
        system_prompt=system_prompt,
        recent_messages=raw_history,
        session_anchor=session_anchor_obj,
        session_compressed=state.session_compressed,
    )


async def _check_and_handle_compaction_deep(
    state: TravelAgentState,
    stream_queue: Optional[asyncio.Queue],
    system_prompt: str,
) -> dict:
    """
    Deep 模式入口：检测是否需要压缩上下文，返回需要更新到 state 的压缩字段。

    上下文透镜的 ``context_report`` **不在这里发**：这个节点跑在
    ``constraint_normalizer`` 之前，此刻还没有 pack，也就无从知道本轮到底有哪几条
    信息会进 prompt。它现在由 ``constraint_normalizer_node`` 发（见那里）。
    """
    from ...memory.compressor import AnchorSummary, ContextCompressor, build_context_compaction_event

    # 不设条数上限：裁剪与压缩判断都归预算层。
    raw_history = session_history_for_context_builder(state)

    session_anchor_obj = None
    if state.session_anchor:
        try:
            session_anchor_obj = AnchorSummary.from_dict(state.session_anchor)
        except Exception as e:
            logger.debug("AnchorSummary 解析失败（压缩检测）: %s", e)

    # 压缩判断只看会话轴（历史消息 + anchor + 基础 system prompt），偏好与画像的
    # 开销落在 worker 的 prompt 上，不在这条轴上累积 —— 把它们算进阈值会让压缩在
    # 错误的时刻触发。这条轴是唯一的压缩判据。
    built_ctx = await _measure_session_size(state, system_prompt)
    if built_ctx is None:
        return {}

    compaction_updates: dict = {}
    if built_ctx.needs_compaction and stream_queue is not None:
        compressor = ContextCompressor()
        try:
            from ...memory.chat_session import ChatSessionMemory
            anchor = await compressor.compress(
                messages=raw_history,
                existing_anchor=session_anchor_obj,
            )
            chat_session_mem = ChatSessionMemory()
            await chat_session_mem.save_anchor(
                user_id=state.user_id or "",
                session_id=state.session_id or "",
                anchor_data=anchor.to_dict(),
            )
            await stream_queue.put((
                "context_compaction",
                _CLARIFIER_NODE,
                build_context_compaction_event(anchor, source="automatic"),
            ))
            compaction_updates = {
                "session_anchor": anchor.to_dict(),
                "session_compressed": True,
                # 上下文透镜要印「较早的对话已整理」，而发那份报告的是下游的
                # constraint_normalizer（只有它手里才有 pack）。这个布尔是那句话
                # 唯一的传递方式：一个写入点（这里）、一个读取点（那里）。
                "session_compacted_this_turn": True,
            }
        except Exception as e:  # 压缩涉及 DB + LLM 多步异步操作，需 catch-all 保证主流程不中断
            # 失败就照原上下文继续。这条路径上没有「压缩进度」这一类事件了
            # （见 ``api/routes/chat_stream_handlers.py`` 里那段说明）：本轮压缩没成，
            # ``session_compacted_this_turn`` 也就不会被写，透镜自然不印那句话。
            logger.error(f"Scope: Deep 模式自动压缩失败: {e}")

    return compaction_updates



async def clarifier_node(state: TravelAgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """
    Scope 阶段 — 步骤 1：入口与前提守卫

    受控旅行身份带着目的地、日期、同行与风格进来，基础旅行事实无需追问，本节点不调模型。
    它做两件事：
    - Deep 模式入口：量会话大小，必要时压缩会话
    - fail-closed：没有受控身份就失败，不再有「模型追问」这条旁路

    它**不**装配送给模型的记忆上下文：深度路径上那件事归 Constraint Pack，
    见 ``_measure_session_size`` 的说明。
    """
    stream_queue: Optional[asyncio.Queue] = None
    if config is not None:
        stream_queue = config.get("configurable", {}).get("stream_queue")

    # Deep 模式入口：检测并处理上下文压缩
    try:
        compaction_updates = await _check_and_handle_compaction_deep(
            state,
            stream_queue,
            SCOPE_CONTEXT_SYSTEM_PROMPT,
        )
    except Exception as e:
        # 记忆是下游调研的偏好增益，不是 Scope 的可用性前提。
        logger.warning("Clarifier: 会话大小测量失败，降级为当前请求与会话: %s", e)
        compaction_updates = {}

    if not state.controlled_trip_identity:
        # 唯一还能走到这里的是库里 identity 为 NULL 的历史 run 被 chat-stream 续跑。
        # 明确失败，不静默走一条已经删掉的旧分支。
        logger.error("Clarifier: run %s 缺少受控旅行身份，拒绝继续", state.run_id or "<unknown>")
        raise ScopeIdentityError(state.run_id or "")

    logger.info("Clarifier: 受控旅行身份已确认，进入 brief_generator")
    return {"next_agent": "brief_generator", **compaction_updates}


async def brief_generator_node(state: TravelAgentState) -> Dict[str, Any]:
    """
    Scope 阶段 — 步骤 2：Research Brief 生成

    从受控旅行身份确定性派生结构化 JSON 简报，作为后续所有 Agent 工作的统一依据
    （"北极星"）。不调模型：身份里已经有目的地、日期、同行与风格，抽取无从抽取，
    推断只会引入一个可以说谎的环节。

    身份缺失由 `clarifier_node` 的 fail-closed 守卫在上游拦下，走不到这里。
    """
    identity = state.controlled_trip_identity
    if not identity:
        raise ScopeIdentityError(state.run_id or "")

    destinations = identity.get("destinations") or []
    names = [str(item.get("name") or item.get("display_name") or "") for item in destinations]
    origin = identity.get("origin") or {}
    party = identity.get("party") or {}
    style = identity.get("style") or {}
    start_date = str(identity.get("start_date") or "")
    end_date = str(identity.get("end_date") or "")
    try:
        from datetime import date

        duration_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
    except ValueError:
        duration_days = None
    constraints = []
    if party.get("elderly_companions"):
        constraints.append("老人同行")
    if party.get("accessibility_required"):
        constraints.append("需要无障碍")
    brief = {
        "objective": f"为确认路线 {' → '.join(names)} 制定可执行旅行计划",
        "destination": " → ".join(names),
        "duration_days": duration_days,
        "budget": None,
        "travel_style": str(style.get("primary") or "未指定"),
        "travel_party": f"{int(party.get('adults') or 1)} 位成人、{int(party.get('children') or 0)} 位儿童",
        "departure_city": str(origin.get("name") or origin.get("display_name") or ""),
        "departure_city_status": "provided",
        "departure_time": f"{start_date} 至 {end_date}",
        "constraints": constraints,
        "dimensions_to_cover": ["目的地概览", "交通建议", "住宿建议", "行程规划", "预算估算"],
        "controlled_trip_identity": identity,
    }
    _validate_research_brief_payload(brief)
    logger.info("BriefGenerator: 由受控身份派生简报 - destination=%s", brief["destination"])
    return {"research_brief": json.dumps(brief, ensure_ascii=False), "next_agent": None}
