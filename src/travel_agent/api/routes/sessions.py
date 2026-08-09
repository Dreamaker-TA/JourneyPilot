"""
会话压缩 API：将当前会话历史原地压缩为 Anchor Summary。
压缩惠及当前对话——生成的 Anchor 写回同一会话，后续构建上下文时以
「anchor + 压缩点之后的近期消息」组装，不再派生新会话。

「压缩点之后」这句话由一个真实的值执行：``chat_sessions.compaction_boundary_event_order``，
由 ``ChatSessionMemory.save_anchor`` 与摘要在同一个事务里写下，
由 ``get_recent_messages_within_token_budget`` 读。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...builders import get_components
from ...memory.compressor import AnchorSummary, ContextCompressor, build_context_compaction_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["sessions"])


class CompactSessionRequest(BaseModel):
    user_id: str


@router.post("/sessions/{session_id}/compact")
async def compact_session(session_id: str, request_body: CompactSessionRequest):
    """
    手动压缩 API：将当前会话所有历史原地压缩为 Anchor Summary，写回当前会话。

    压缩后的 Anchor 存在同一会话的 anchor_summary 列上（与自动压缩共用同一次
    ``save_anchor``，所以压缩点由同一个事务一起写下），后续任何一轮对话构建上下文时
    都会读取该 Anchor 注入 system prompt，并叠加**压缩点之后**的近期消息——已折叠进
    摘要的早期历史不再逐字重复注入。原会话的消息记录本身不删除，历史在界面上仍可回看。

    Request body:
        user_id: str

    Response: one immutable context_compaction event containing the full
    summary and every explicit planning constraint.
    """
    components = get_components()
    user_id = request_body.user_id

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id 不能为空")

    # 获取全量消息 + 当前会话已有 Anchor（用于增量合并）
    try:
        all_messages = await components.chat_session_memory.get_all_messages_for_compression(
            user_id=user_id,
            session_id=session_id,
        )
        existing_anchor_raw, _ = await components.chat_session_memory.get_anchor(
            user_id=user_id,
            session_id=session_id,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    except Exception as e:
        logger.error(f"手动压缩：加载会话历史失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="加载会话历史失败")

    if not all_messages:
        raise HTTPException(status_code=400, detail="会话中无可压缩的消息")

    # 解析已有 Anchor（有则将新信息合并进去，保持完整性）
    existing_anchor = None
    if existing_anchor_raw:
        try:
            existing_anchor = AnchorSummary.from_dict(existing_anchor_raw)
        except Exception:
            existing_anchor = None

    # 执行压缩
    try:
        compressor = ContextCompressor()
        anchor = await compressor.compress(
            messages=all_messages,
            existing_anchor=existing_anchor,
        )
    except Exception as e:
        logger.error(f"手动压缩：压缩失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="上下文压缩失败，请稍后重试")

    # 原地写回当前会话——与 fast_answer / scope 节点的自动压缩共用 save_anchor：
    # 更新 anchor_summary 列并递增 compression_count，后续构建上下文即读到它。
    try:
        await components.chat_session_memory.save_anchor(
            user_id=user_id,
            session_id=session_id,
            anchor_data=anchor.to_dict(),
        )
    except Exception as e:
        logger.error(f"手动压缩：写回 Anchor 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="上下文整理保存失败，请稍后重试")

    event = build_context_compaction_event(anchor, source="manual")
    try:
        event = await components.chat_session_memory.append_context_compaction_event(
            user_id=user_id,
            session_id=session_id,
            event=event,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    except LookupError:
        raise HTTPException(status_code=404, detail="会话不存在")
    except Exception as e:
        logger.error(f"手动压缩：写入会话事件失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="上下文整理已完成，但记录会话事件失败，请稍后刷新")

    logger.info(
        f"手动压缩完成（原地）: session={session_id}, "
        f"messages={len(all_messages)}, "
        f"tokens {anchor.tokens_before} → {anchor.tokens_after}"
    )

    return event
