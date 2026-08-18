"""把同步工作赶出 Event Loop 的唯一入口。

PDF 渲染、文档解析、本地 ONNX 推理、大 JSON canonical hash 都是纯 CPU 的同步调用。

**取消的限度**（ADR-0006）：线程里的 Python/C 代码不能被安全强杀，``asyncio.wait_for``
超时只停止*等待*，线程会继续跑到自己结束。所以

- 可信的内部数据（ReportLab 读一份 immutable Bundle）走线程；
- 不可信的用户上传走子进程（`rag/sources/document_parse.py`），超时杀进程树；
- 每一路都必须先有输入上限，超时只是最后一道，不是唯一一道。
"""

from __future__ import annotations

import contextlib
import functools
import logging
from typing import Any, AsyncIterator, Callable, Dict, Optional, TypeVar

import anyio.to_thread

from ..config import get_settings
from ..utils.concurrency import ChannelBusy, channel_gate, channel_metrics

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 通道名 → `BlockingWorkConfig` 上的字段名。多一个通道就在这里和配置里各加一行，
#: 不允许调用方传一个未声明的名字进来 —— 那种通道没有上限。
BLOCKING_CHANNELS = {
    "pdf_export": "pdf_export",
    "document_parse": "document_parse",
    "local_embedding": "local_embedding",
}


class BlockingWorkBusy(RuntimeError):
    """通道排满且在排队上限内没等到位置。"""

    def __init__(self, channel: str, wait_seconds: float) -> None:
        self.channel = channel
        self.wait_seconds = wait_seconds
        super().__init__(f"blocking work channel {channel} busy for {wait_seconds:.1f}s")


def _gate(channel: str):
    field = BLOCKING_CHANNELS.get(channel)
    if field is None:
        raise ValueError(f"unknown blocking work channel: {channel}")
    config = get_settings().blocking_work
    return channel_gate(f"blocking.{channel}", int(getattr(config, field)))


@contextlib.asynccontextmanager
async def blocking_channel(
    channel: str, *, wait_seconds: Optional[float] = None
) -> AsyncIterator[None]:
    """占用一个通道位置。

    子进程那一路（文档解析）需要这个而不是 :func:`run_blocking`：它的工作不在线程
    里，但**配额是同一个** —— 同时跑几份解析这件事只能有一个上限。
    """

    gate = _gate(channel)
    budget = (
        float(wait_seconds)
        if wait_seconds is not None
        else float(get_settings().blocking_work.queue_wait_seconds)
    )
    try:
        async with gate.hold(wait_seconds=budget):
            yield
    except ChannelBusy as exc:
        logger.warning(
            "同步工作通道排满 | channel=%s waited=%.1fs waiting=%d",
            channel,
            budget,
            exc.waiting,
        )
        raise BlockingWorkBusy(channel, budget) from exc


async def run_in_thread(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """在线程里跑一次同步调用，**不占通道**。

    ``anyio.to_thread.run_sync`` 而不是 ``asyncio.to_thread``：它把当前的
    contextvars 带进线程，run 归因与 deadline 观察在同步代码里仍然成立。

    调用方必须已经持有一个通道（`blocking_channel`）。不持有就用
    :func:`run_blocking`；直接用这个等于给自己开一条没有上限的路。
    """

    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


async def run_blocking(
    channel: str,
    fn: Callable[..., T],
    /,
    *args: Any,
    wait_seconds: Optional[float] = None,
    **kwargs: Any,
) -> T:
    """占一个通道位置，在受限线程里跑一次同步调用。"""

    async with blocking_channel(channel, wait_seconds=wait_seconds):
        return await run_in_thread(fn, *args, **kwargs)


def blocking_work_metrics() -> Dict[str, Dict[str, float]]:
    """只取 blocking 通道的读数（其余通道由 `channel_metrics` 一并给出）。"""

    return {
        name.removeprefix("blocking."): metrics
        for name, metrics in channel_metrics().items()
        if name.startswith("blocking.")
    }
