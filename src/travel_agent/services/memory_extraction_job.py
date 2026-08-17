"""`memory_extraction` 任务的处理器。

正文从权威会话记录读，不在 payload 里复制一份原文；payload 只带引用与那一轮看到的
画像基线。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..entities.background_job import BackgroundJob, BackgroundJobPermanentError

logger = logging.getLogger(__name__)


def make_memory_extraction_handler(
    *,
    chat_session_memory: Any,
    memory_extractor: Any,
    user_profile_memory: Any,
):
    async def handle(job: BackgroundJob) -> Optional[Dict[str, Any]]:
        payload = job.payload or {}
        user_id = str(payload.get("user_id") or "")
        session_id = str(payload.get("session_id") or "")
        user_message_id = str(payload.get("user_message_id") or "")
        if not (user_id and session_id and user_message_id):
            raise BackgroundJobPermanentError("invalid_payload", "抽取任务缺少来源引用")

        user_message = await chat_session_memory.get_message_content(
            session_id=session_id,
            message_id=user_message_id,
        )
        if user_message is None:
            # 会话或消息已被删除。重试多少次都读不回来。
            raise BackgroundJobPermanentError("source_message_deleted", "来源消息已不存在")

        outcome = await memory_extractor.extract_from_turn(
            user_id=user_id,
            session_id=session_id,
            user_msg=user_message,
            existing_portrait=str(payload.get("portrait_baseline") or ""),
            source_message_id=user_message_id,
        )

        current_revision = await user_profile_memory.get_revision(user_id)
        return {
            "facts": outcome.facts_written,
            "portraits": outcome.portraits_written,
            "rejections": outcome.rejections,
            "profile_revision_at_enqueue": payload.get("profile_revision"),
            "profile_revision_now": current_revision,
        }

    return handle
