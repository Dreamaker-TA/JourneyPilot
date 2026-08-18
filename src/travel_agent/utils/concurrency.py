"""按通道分配的并发预算，带排队上界与可观察的积压。

与 `utils/rate_gate.py` 分工明确：闸管**两次请求之间的间隔**，这里管**同时在飞
的条数**。一个上游同时需要两者时两个都要挂上。

通道是产品预算的单位，不是传输层设施：共用一条连接池的上游也要分开配额。

排队**有上界**：等不到位置抛 :class:`ChannelBusy`，由调用方决定这是一次降级还是一个
诚实的失败。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Coroutine, Dict, Optional

logger = logging.getLogger(__name__)


class ChannelBusy(RuntimeError):
    """在排队上限内没等到通道位置。"""

    def __init__(self, channel: str, wait_seconds: float, waiting: int) -> None:
        self.channel = channel
        self.wait_seconds = wait_seconds
        self.waiting = waiting
        super().__init__(
            f"channel {channel} busy: no slot within {wait_seconds:.1f}s "
            f"({waiting} waiting)"
        )


@dataclass
class ChannelGate:
    """一个通道的信号量与它的读数。"""

    name: str
    limit: int
    _semaphore: asyncio.Semaphore = field(init=False)
    #: 创建这个信号量的 Event Loop。换了 loop 就换一个 gate，见 `channel_gate`。
    _loop: Optional[asyncio.AbstractEventLoop] = field(default=None, init=False)
    active: int = field(default=0, init=False)
    waiting: int = field(default=0, init=False)
    peak_waiting: int = field(default=0, init=False)
    admitted: int = field(default=0, init=False)
    busy_rejections: int = field(default=0, init=False)
    total_wait_seconds: float = field(default=0.0, init=False)
    max_wait_seconds: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.limit)

    @contextlib.asynccontextmanager
    async def hold(self, *, wait_seconds: Optional[float]) -> AsyncIterator[None]:
        """占用一个位置，等不到就抛 :class:`ChannelBusy`。

        ``wait_seconds=None`` 表示排队**不由这里定界**，由外层的时间预算定界。模型
        调用走这一档：一次调用能等多久已经由 Run 的 Deadline 回答过了，在这里再写一个
        数就是给同一个问题两个答案。
        """

        self.waiting += 1
        self.peak_waiting = max(self.peak_waiting, self.waiting)
        started = time.monotonic()
        try:
            if wait_seconds is None:
                await self._semaphore.acquire()
            else:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=wait_seconds)
        except asyncio.TimeoutError as exc:
            self.busy_rejections += 1
            raise ChannelBusy(self.name, wait_seconds or 0.0, self.waiting) from exc
        finally:
            waited = time.monotonic() - started
            self.waiting -= 1
            self.total_wait_seconds += waited
            self.max_wait_seconds = max(self.max_wait_seconds, waited)
        self.active += 1
        self.admitted += 1
        try:
            yield
        finally:
            self.active -= 1
            self._semaphore.release()

    def metrics(self) -> Dict[str, float]:
        return {
            "limit": self.limit,
            "active": self.active,
            "waiting": self.waiting,
            "peak_waiting": self.peak_waiting,
            "admitted": self.admitted,
            "busy_rejections": self.busy_rejections,
            "total_wait_seconds": round(self.total_wait_seconds, 3),
            "max_wait_seconds": round(self.max_wait_seconds, 3),
        }


_GATES: Dict[str, ChannelGate] = {}


def channel_gate(name: str, limit: int) -> ChannelGate:
    """这个进程上名为 ``name`` 的通道，首次使用时按 ``limit`` 建立。

    ``limit`` 变了要换一个信号量，否则改配置后旧的上限继续生效。Event Loop 变了
    也要换：一个带等待者的 `asyncio.Semaphore` 跨 loop 使用会把等待者永久挂住，
    而测试每个用例一个 loop。
    """

    try:
        loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    gate = _GATES.get(name)
    if gate is not None and gate.limit == limit and gate._loop in (None, loop):
        if gate._loop is None:
            gate._loop = loop
        return gate
    gate = ChannelGate(name=name, limit=limit)
    gate._loop = loop
    _GATES[name] = gate
    return gate


def channel_metrics() -> Dict[str, Dict[str, float]]:
    """所有已使用通道的读数，供 readiness / doctor 展示积压。"""

    return {name: gate.metrics() for name, gate in sorted(_GATES.items())}


def reset_channels() -> None:
    """丢掉全部通道状态。只供测试在用例之间隔离读数。"""

    _GATES.clear()


class PeriodicTask:
    """一个后台轮询协程的启停。

    这套 start / cancel-and-await-CancelledError 的样板原本在四个类里各写一份，其中
    一份的清理顺序还与另外三份不同 —— 读了三个就以为懂了第四个。加固关闭行为（比如给
    join 加超时，免得一个卡住的轮询挡住 lifespan 收尾）现在只改这一处。
    """

    def __init__(self, name: str, factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self._name = name
        self._factory = factory
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._factory(), name=self._name)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def run_forever(name: str, step: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """反复跑 ``step``，直到被取消。

    一次异常不停掉循环：数据库抖一下之后，取消请求仍然必须能被看见。取消要原样往外抛
    —— 把它并进 ``except Exception`` 会让关闭变成一次静默的重试。
    """

    while True:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("%s 轮询失败：%s", name, exc, exc_info=True)
