"""上游通道配额：入库不许把在线请求排到队尾。"""

from __future__ import annotations

import asyncio

import pytest

from travel_agent.config import get_settings
from travel_agent.models.router import current_llm_channel, llm_channel
from travel_agent.utils.concurrency import (
    ChannelBusy,
    channel_gate,
    channel_metrics,
    reset_channels,
)


@pytest.fixture(autouse=True)
def _isolate_channels():
    reset_channels()
    yield
    reset_channels()


async def test_a_gate_caps_concurrency_and_reports_its_backlog():
    gate = channel_gate("test.online", 2)
    peak = 0

    async def _one() -> None:
        nonlocal peak
        async with gate.hold(wait_seconds=1.0):
            peak = max(peak, gate.active)
            await asyncio.sleep(0.02)

    await asyncio.gather(*[_one() for _ in range(6)])
    assert peak == 2
    assert channel_metrics()["test.online"]["admitted"] == 6


async def test_waiting_past_the_bound_is_a_busy_signal_not_a_hang():
    gate = channel_gate("test.bounded", 1)

    async def _hold() -> None:
        async with gate.hold(wait_seconds=1.0):
            await asyncio.sleep(0.3)

    holder = asyncio.create_task(_hold())
    await asyncio.sleep(0.02)
    with pytest.raises(ChannelBusy):
        async with gate.hold(wait_seconds=0.03):
            pass
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder


async def test_ingest_and_online_calls_do_not_share_a_quota():
    """两条路走同一个 fast 上游，但配额必须分开。

    这一条断言的是**通道身份**：入库的调用记在 ingest_contextual_llm 上，同时在线
    请求仍然记在 online_fast_llm 上。共用一个配额时，一次上传排出的上千条调用会让
    在线请求全部排在它们后面（实测服务不可达 7 分半）。
    """

    channels = get_settings().provider_channels
    online = channel_gate("llm.online_fast_llm", channels.online_fast_llm)
    ingest = channel_gate("llm.ingest_contextual_llm", channels.ingest_contextual_llm)

    async def _ingest_burst() -> None:
        with llm_channel("ingest_contextual_llm"):
            assert current_llm_channel.get() == "ingest_contextual_llm"
            async with ingest.hold(wait_seconds=1.0):
                await asyncio.sleep(0.05)

    async def _online_call() -> None:
        assert current_llm_channel.get() is None  # 默认按档位，不是入库那一档
        async with online.hold(wait_seconds=0.1):
            await asyncio.sleep(0.01)

    # 入库把自己那一档占满，在线请求照样进得去。
    burst = [asyncio.create_task(_ingest_burst()) for _ in range(20)]
    await asyncio.sleep(0.01)
    await _online_call()
    await asyncio.gather(*burst)
    assert ingest.busy_rejections == 0
    assert online.busy_rejections == 0


async def test_the_channel_override_is_scoped_to_its_block():
    with llm_channel("ingest_contextual_llm"):
        assert current_llm_channel.get() == "ingest_contextual_llm"
    assert current_llm_channel.get() is None


async def test_changing_the_limit_replaces_the_gate():
    """改配置之后旧的上限不许继续生效。"""

    first = channel_gate("test.resized", 1)
    second = channel_gate("test.resized", 4)
    assert second is not first
    assert second.limit == 4
