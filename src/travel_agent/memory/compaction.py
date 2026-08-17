"""上下文压缩的唯一入口。手动整理与自动压缩走同一条路。

增量：只读压缩点之后、预算之内的消息，边界只推到**真正进了摘要**的那一条。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional
from weakref import WeakValueDictionary

from .chat_session import ChatSessionMemory
from .compressor import AnchorSummary, ContextCompressor, build_context_compaction_event
from .context_builder import ContextBudget

logger = logging.getLogger(__name__)

#: 每个会话一把锁。产品是单进程的（不支持多 API worker），所以「同一会话一次只能有一次
#: 压缩」在这里成立；跨进程那一半由提交时的边界 CAS 兜住。
_session_locks: "WeakValueDictionary[str, asyncio.Lock]" = WeakValueDictionary()


def _lock_for(session_id: str) -> asyncio.Lock:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    return lock


@dataclass(frozen=True)
class CompactionResult:
    anchor: AnchorSummary
    event: Dict[str, Any]
    messages_selected: int
    last_included_event_order: int


class CompactionService:
    """读预算内的历史 → 生成增量摘要 → 与新边界、时间线快照同事务落库。"""

    def __init__(
        self,
        *,
        chat_session_memory: Optional[ChatSessionMemory] = None,
        compressor: Optional[ContextCompressor] = None,
        budget: Optional[ContextBudget] = None,
    ) -> None:
        self._sessions = chat_session_memory or ChatSessionMemory()
        self._compressor = compressor or ContextCompressor()
        self._budget = budget or ContextBudget()

    async def compact(
        self,
        *,
        user_id: str,
        session_id: str,
        source: str,
    ) -> Optional[CompactionResult]:
        """压缩一次。没有可压缩的消息、或提交时边界已被别人推走，返回 None。

        失败一律抛出：**不推进边界、不覆盖旧摘要**。压缩失败只是本轮少一份摘要，
        不该变成整个 Run 失败。
        """

        lock = _lock_for(session_id)
        if lock.locked():
            # 同一会话已经有一次压缩在跑。第二个请求继续用旧摘要，不排队再调一次模型。
            logger.info("会话已有压缩在进行，本次跳过 session=%s", session_id)
            return None

        async with lock:
            messages, expected_boundary, last_included = (
                await self._sessions.read_messages_for_compaction(
                    user_id,
                    session_id,
                    token_budget=self._budget.compaction_input_tokens,
                    max_messages=self._budget.max_messages_per_compaction,
                )
            )
            if not messages:
                return None

            existing_anchor = await self._load_anchor(user_id, session_id)
            anchor = await self._compressor.compress(
                messages=messages,
                existing_anchor=existing_anchor,
            )
            event = build_context_compaction_event(anchor, source=source)
            committed = await self._sessions.commit_compaction(
                user_id,
                session_id,
                anchor_data=anchor.to_dict(),
                expected_boundary=expected_boundary,
                new_boundary=last_included,
                event=event,
            )
            if committed is None:
                logger.info("压缩提交时边界已被推走，本次作废 session=%s", session_id)
                return None

            logger.info(
                "上下文压缩完成 session=%s source=%s messages=%s 边界 %s→%s tokens %s→%s",
                session_id,
                source,
                len(messages),
                expected_boundary,
                last_included,
                anchor.tokens_before,
                anchor.tokens_after,
            )
            return CompactionResult(
                anchor=anchor,
                event=committed,
                messages_selected=len(messages),
                last_included_event_order=last_included,
            )

    async def _load_anchor(self, user_id: str, session_id: str) -> Optional[AnchorSummary]:
        raw, _count = await self._sessions.get_anchor(user_id, session_id)
        if not raw:
            return None
        try:
            return AnchorSummary.from_dict(raw)
        except Exception:
            # 旧摘要读不出来就当没有：这一轮生成一份全新的，绝不因此中断压缩。
            return None


_service: Optional[CompactionService] = None


def get_compaction_service() -> CompactionService:
    global _service
    if _service is None:
        _service = CompactionService()
    return _service
