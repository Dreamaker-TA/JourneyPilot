"""会话相关 API：按 turn 分页回看历史，以及手动整理上下文。

分页按 **turn** 走，不按原始事件：一个 turn 里有用户消息、思考步、上下文报告与助手
消息，按事件游标切页会把它们切到两页，前端拼不回来。

手动整理与自动压缩共用 `CompactionService` —— 摘要、新边界与时间线快照由它在同一个
事务里写下。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from ...builders import get_components
from ...local_profile import LOCAL_USER_ID
from ...memory.chat_session import TURN_PAGE_LIMIT
from ...memory.compaction import CompactionBusy, get_compaction_service
from ..schemas import SessionTurnPage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["sessions"])


@router.get("/sessions/{session_id}/turns", response_model=SessionTurnPage)
async def list_session_turns(
    session_id: str,
    before_turn: str | None = Query(default=None),
    limit: int = Query(default=TURN_PAGE_LIMIT, ge=1, le=TURN_PAGE_LIMIT),
):
    """按 turn 游标往回取一页历史。不带游标就是最新一页。"""

    components = get_components()
    try:
        return await components.chat_session_memory.list_turns(
            LOCAL_USER_ID,
            session_id,
            before_turn=before_turn,
            limit=limit,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    except CompactionBusy:
        raise HTTPException(status_code=409, detail="上下文正在整理中，稍后再试")
    except Exception as e:
        logger.error(f"读取会话分页失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="读取会话历史失败")


@router.post("/sessions/{session_id}/compact")
async def compact_session(session_id: str):
    """手动整理：把压缩点之后、预算之内的历史折叠进 Anchor Summary。

    返回时间线上那一枚不可变的 `context_compaction` 快照。
    """

    try:
        result = await get_compaction_service().compact(
            user_id=LOCAL_USER_ID,
            session_id=session_id,
            source="manual",
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    except Exception as e:
        logger.error(f"手动压缩失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="上下文整理失败，请稍后重试")

    if result is None:
        raise HTTPException(status_code=400, detail="会话中无可整理的消息")
    return result.event
