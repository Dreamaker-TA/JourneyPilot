"""SSE 生产者与消费者之间的有界缓冲。

替代无界 `asyncio.Queue`。事件分三类，各自的边界不同：

- **critical**：状态边界、工具结论、门、终态。绝不丢，队列满时生产者等待。
- **coalescible**：正文 token 与推理文本。相邻的同源片段合并成更大的块，
  字符不丢，但不为每个 Provider token 单独排一个位置。
- **ephemeral**：心跳。不进这条通道。

**入队顺序就是出队顺序**：合并只发生在队尾同源片段之间，所以一个 critical 事件
一旦入队，它前面的正文就已经在它前面了 —— 不会出现「状态先到、正文后到」。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

#: 可合并的文本流。键取 (kind, node)：两个节点的正文不能拼成一段。
COALESCIBLE_KINDS = frozenset({"token", "react_thinking"})

#: 终态与错误。它们不受容量约束 —— 一个被丢掉的终态会让客户端永远等下去。
TERMINAL_KINDS = frozenset({"done", "error"})


@dataclass
class SSEBufferStats:
    """只留水位与计数。当前深度直接读缓冲自己的计数器，不在这里存第二份。"""

    max_critical_size: int = 0
    max_pending_text_chars: int = 0
    coalesced_merges: int = 0
    dropped_text_chars: int = 0
    stalled_total: int = 0


@dataclass
class _Entry:
    kind: str
    node: str
    payload: Any = None
    text: list[str] = field(default_factory=list)
    #: 已累计的字符数。**增量维护**：每个 token 都重算一遍 sum(len(...)) 会让合并
    #: 检查随尾块长度线性增长，而合并块本来就是按 2048 字符攒的。
    chars: int = 0
    coalescible: bool = False

    def to_item(self) -> Tuple:
        if self.coalescible:
            return (self.kind, self.node, "".join(self.text))
        return self.payload


class SSEBuffer:
    """有类别的事件缓冲。`put` / `get` 是这条通道的全部接口。"""

    def __init__(
        self,
        *,
        critical_queue_size: int = 128,
        max_coalesced_chunk_chars: int = 2048,
        max_pending_text_chars: int = 65536,
        stalled_consumer_seconds: float = 30.0,
    ) -> None:
        self._critical_queue_size = max(1, critical_queue_size)
        self._max_chunk_chars = max(1, max_coalesced_chunk_chars)
        self._max_pending_text_chars = max(1, max_pending_text_chars)
        self._stalled_consumer_seconds = max(0.1, stalled_consumer_seconds)
        self._items: deque[_Entry] = deque()
        self._critical_count = 0
        self._pending_text_chars = 0
        self._arrived = asyncio.Event()
        self._space = asyncio.Event()
        self._space.set()
        self.stats = SSEBufferStats()
        self.stalled = False

    async def put(self, item: Tuple) -> None:
        """入队一个 `(kind, ...)` 事件。慢消费者会让这里等待，但不会让它丢东西。"""

        kind = str(item[0])
        if kind in TERMINAL_KINDS:
            self._append(_Entry(kind=kind, node="", payload=item))
            return
        if kind in COALESCIBLE_KINDS:
            await self._put_text(kind, str(item[1]), str(item[2]))
            return
        await self._put_critical(kind, item)

    async def _put_critical(self, kind: str, item: Tuple) -> None:
        if self._critical_count >= self._critical_queue_size:
            await self._await_space()
        # 等不到位置也照样入队：一个被丢掉的 critical 事件会让客户端看不到状态边界。
        # 上限在这里是背压，不是丢弃策略 —— 消费者已经收摊时由它结束传输来收敛。
        self._critical_count += 1
        self._append(_Entry(kind=kind, node="", payload=item))

    async def _put_text(self, kind: str, node: str, chunk: str) -> None:
        if not chunk:
            return
        if self._pending_text_chars + len(chunk) > self._max_pending_text_chars:
            await self._await_space()
            if self.stalled:
                # 传输正在被结束（消费者拿到 stalled 就收摊）。这里丢掉的只是显示用的
                # 增量文本 —— 落库的助手正文来自节点的 state update，不是这些片段。
                self.stats.dropped_text_chars += len(chunk)
                return
        tail = self._items[-1] if self._items else None
        if (
            tail is not None
            and tail.coalescible
            and tail.kind == kind
            and tail.node == node
            and tail.chars + len(chunk) <= self._max_chunk_chars
        ):
            tail.text.append(chunk)
            tail.chars += len(chunk)
            self.stats.coalesced_merges += 1
        else:
            self._items.append(
                _Entry(
                    kind=kind,
                    node=node,
                    text=[chunk],
                    chars=len(chunk),
                    coalescible=True,
                )
            )
        self._pending_text_chars += len(chunk)
        self._after_append()

    def _append(self, entry: _Entry) -> None:
        self._items.append(entry)
        self._after_append()

    def _full(self) -> bool:
        return (
            self._critical_count >= self._critical_queue_size
            or self._pending_text_chars >= self._max_pending_text_chars
        )

    def _after_append(self) -> None:
        self._arrived.set()
        self.stats.max_critical_size = max(self.stats.max_critical_size, self._critical_count)
        self.stats.max_pending_text_chars = max(
            self.stats.max_pending_text_chars, self._pending_text_chars
        )
        if self._full():
            self._space.clear()

    async def _await_space(self) -> None:
        """等消费者腾出位置。等不到就记一次 stall —— 但仍然让生产者继续走。

        这里不抛：浏览器慢不该让 Run 变成 failed。消费者下一次 `get` 会看见
        `stalled`，由它结束传输，之后一切以 durable 状态为准。

        已经 stall 过就不再等：那个标志永不复位（消费者已经不再 `get`，`_space`
        也就永远不会被 set），每个事件再等满一个 30 秒只是把生产者按住。
        """

        if self.stalled:
            return
        try:
            await asyncio.wait_for(self._space.wait(), self._stalled_consumer_seconds)
        except asyncio.TimeoutError:
            self.stalled = True
            self.stats.stalled_total += 1

    async def get(self, timeout: float) -> Tuple:
        """取下一个事件。超时抛 `asyncio.TimeoutError`，由调用方发保活帧。"""

        while True:
            if self._items:
                entry = self._items.popleft()
                if entry.coalescible:
                    self._pending_text_chars -= entry.chars
                elif entry.kind not in TERMINAL_KINDS:
                    self._critical_count -= 1
                if not self._full():
                    self._space.set()
                if not self._items:
                    self._arrived.clear()
                return entry.to_item()
            self._arrived.clear()
            await asyncio.wait_for(self._arrived.wait(), timeout)

    def snapshot(self) -> dict:
        """给日志用的一行读数。"""

        return {
            "critical_size": self._critical_count,
            "pending_text_chars": self._pending_text_chars,
            "max_critical_size": self.stats.max_critical_size,
            "max_pending_text_chars": self.stats.max_pending_text_chars,
            "coalesced_merges": self.stats.coalesced_merges,
            "dropped_text_chars": self.stats.dropped_text_chars,
            "stalled": self.stalled,
        }


def build_sse_buffer(streaming_config: Optional[Any] = None) -> SSEBuffer:
    if streaming_config is None:
        from ..config import get_settings

        streaming_config = get_settings().streaming
    return SSEBuffer(
        critical_queue_size=streaming_config.critical_queue_size,
        max_coalesced_chunk_chars=streaming_config.max_coalesced_chunk_chars,
        max_pending_text_chars=streaming_config.max_pending_text_chars,
        stalled_consumer_seconds=streaming_config.stalled_consumer_seconds,
    )
