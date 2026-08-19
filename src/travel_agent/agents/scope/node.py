"""Scope entry guard and session-compaction boundary."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from langchain_core.runnables import RunnableConfig

from ...entities.state import TravelAgentState
from ..utils import session_history_for_context_builder
from .prompts import SCOPE_CONTEXT_SYSTEM_PROMPT

if TYPE_CHECKING:
    from ...api.sse_buffer import SSEBuffer

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
    stream_queue: Optional["SSEBuffer"],
    system_prompt: str,
) -> dict:
    """
    Deep 模式入口：检测是否需要压缩上下文，返回需要更新到 state 的压缩字段。

    上下文透镜的 ``context_report`` **不在这里发**：这个节点跑在
    Request Contract 归一化之前，此刻还没有 pack，也就无从知道本轮到底有哪几条
    信息会进 prompt。该报告由后续的归一化节点发送。
    """
    # 压缩判断只看会话轴（历史消息 + anchor + 基础 system prompt），偏好与画像的
    # 开销落在 worker 的 prompt 上，不在这条轴上累积 —— 把它们算进阈值会让压缩在
    # 错误的时刻触发。这条轴是唯一的压缩判据。
    built_ctx = await _measure_session_size(state, system_prompt)
    if built_ctx is None:
        return {}

    compaction_updates: dict = {}
    if built_ctx.needs_compaction and stream_queue is not None:
        from ...memory.compaction import CompactionBusy, get_compaction_service

        try:
            result = await get_compaction_service().compact(
                user_id=state.user_id or "",
                session_id=state.session_id or "",
                source="automatic",
            )
        except CompactionBusy:
            # 已经有一次在跑（或提交时被抢先）。这是正常跳过，不是失败。
            logger.info("Scope: 会话已有压缩在进行，本轮沿用原上下文")
            result = None
        except Exception as e:  # 压缩涉及 DB + LLM 多步异步操作，需 catch-all 保证主流程不中断
            # 失败就照原上下文继续。这条路径上没有「压缩进度」这一类事件了
            # （见 ``api/routes/chat_stream_handlers.py`` 里那段说明）：本轮压缩没成，
            # ``session_compacted_this_turn`` 也就不会被写，透镜自然不印那句话。
            logger.error(f"Scope: Deep 模式自动压缩失败: {e}")
            result = None
        if result is not None:
            await stream_queue.put(("context_compaction", _CLARIFIER_NODE, result.event))
            compaction_updates = {
                "session_anchor": result.anchor.to_dict(),
                "session_compressed": True,
                # 上下文透镜要印「较早的对话已整理」，而发那份报告的是下游的
                # request_contract_normalizer（只有它手里才有 pack）。这个布尔是那句话
                # 唯一的传递方式：一个写入点（这里）、一个读取点（那里）。
                "session_compacted_this_turn": True,
            }

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
    stream_queue: Optional["SSEBuffer"] = None
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

    logger.info("Clarifier: 受控旅行身份已确认，进入请求合同归一化")
    return {"next_agent": "request_contract_normalizer", **compaction_updates}
