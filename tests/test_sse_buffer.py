"""SSEBuffer：顺序、合并、不可丢、慢客户端下的内存边界。"""

from __future__ import annotations

import asyncio

import pytest

from travel_agent.api.sse_buffer import SSEBuffer


def _buffer(**kwargs) -> SSEBuffer:
    defaults = dict(
        critical_queue_size=4,
        max_coalesced_chunk_chars=8,
        max_pending_text_chars=32,
        stalled_consumer_seconds=0.05,
    )
    defaults.update(kwargs)
    return SSEBuffer(**defaults)


async def _drain(buffer: SSEBuffer) -> list:
    items = []
    while True:
        try:
            items.append(await buffer.get(0.01))
        except asyncio.TimeoutError:
            return items


async def test_adjacent_tokens_merge_without_losing_characters():
    buffer = _buffer()
    for chunk in ("你好", "，世", "界"):
        await buffer.put(("token", "fast_answer_agent", chunk))

    items = await _drain(buffer)
    assert items == [("token", "fast_answer_agent", "你好，世界")]


async def test_tokens_from_two_nodes_never_merge():
    buffer = _buffer()
    await buffer.put(("token", "a", "一"))
    await buffer.put(("token", "b", "二"))
    await buffer.put(("token", "a", "三"))

    assert await _drain(buffer) == [
        ("token", "a", "一"),
        ("token", "b", "二"),
        ("token", "a", "三"),
    ]


async def test_a_critical_event_never_jumps_ahead_of_earlier_text():
    """状态先到、正文后到会让前端把文字挂在错的消息上。"""

    buffer = _buffer()
    await buffer.put(("token", "a", "前半"))
    await buffer.put(("state", "a", {"done": True}))
    await buffer.put(("token", "a", "后半"))

    assert await _drain(buffer) == [
        ("token", "a", "前半"),
        ("state", "a", {"done": True}),
        ("token", "a", "后半"),
    ]


async def test_a_long_stream_is_chunked_not_dropped():
    buffer = _buffer(max_coalesced_chunk_chars=4, max_pending_text_chars=4096)
    for _ in range(10):
        await buffer.put(("token", "a", "ab"))

    items = await _drain(buffer)
    assert "".join(item[2] for item in items) == "ab" * 10
    assert all(len(item[2]) <= 4 for item in items)


async def test_a_full_critical_queue_makes_the_producer_wait():
    buffer = _buffer(critical_queue_size=2, stalled_consumer_seconds=5)
    await buffer.put(("state", "a", 1))
    await buffer.put(("state", "a", 2))

    blocked = asyncio.create_task(buffer.put(("state", "a", 3)))
    await asyncio.sleep(0.01)
    assert not blocked.done()
    assert buffer.stalled is False

    assert await buffer.get(1) == ("state", "a", 1)
    await asyncio.wait_for(blocked, 1)
    assert buffer.stalled is False


async def test_a_consumer_that_never_reads_is_reported_stalled():
    """浏览器停读不该让 Run 变成 failed —— 记一次 stall，由消费者收摊。"""

    buffer = _buffer(critical_queue_size=1)
    await buffer.put(("state", "a", 1))
    await buffer.put(("state", "a", 2))

    assert buffer.stalled is True
    assert buffer.stats.stalled_total == 1
    # 关键事件仍然在缓冲里，一条都没丢。
    assert await _drain(buffer) == [("state", "a", 1), ("state", "a", 2)]


async def test_terminal_events_ignore_the_capacity_bound():
    """丢一个终态等于让客户端永远等下去。"""

    buffer = _buffer(critical_queue_size=1, stalled_consumer_seconds=0.01)
    await buffer.put(("state", "a", 1))
    await buffer.put(("done",))

    items = await _drain(buffer)
    assert items[-1] == ("done",)


async def test_text_over_budget_is_dropped_only_after_a_stall():
    buffer = _buffer(max_coalesced_chunk_chars=4, max_pending_text_chars=8)
    for _ in range(6):
        await buffer.put(("token", "a", "abcd"))

    assert buffer.stalled is True
    assert buffer.stats.dropped_text_chars > 0
    # 缓冲本身仍在预算内 —— 慢客户端不会让内存无限增长。
    assert buffer.stats.max_pending_text_chars <= 12


async def test_get_times_out_without_touching_the_buffer():
    buffer = _buffer()
    await buffer.put(("state", "a", 1))

    with pytest.raises(asyncio.TimeoutError):
        # 先取走唯一一条，再等一次超时。
        assert await buffer.get(0.01) == ("state", "a", 1)
        await buffer.get(0.01)

    await buffer.put(("state", "a", 2))
    assert await buffer.get(0.01) == ("state", "a", 2)


async def test_a_slow_client_keeps_every_character_and_every_critical_event():
    """每 20ms 读一次的客户端：正文完整、关键事件齐全、顺序不变。"""

    buffer = _buffer(
        critical_queue_size=8, max_coalesced_chunk_chars=16, max_pending_text_chars=256,
        stalled_consumer_seconds=5,
    )
    produced_text = "".join(f"{index:03d}" for index in range(200))

    async def produce() -> None:
        for index in range(200):
            await buffer.put(("token", "a", f"{index:03d}"))
            if index % 50 == 0:
                await buffer.put(("tool_done", "a", {"i": index}))
        await buffer.put(("done",))

    received: list = []
    producer = asyncio.create_task(produce())
    while True:
        item = await buffer.get(2)
        received.append(item)
        if item[0] == "done":
            break
        await asyncio.sleep(0.002)
    await producer

    assert buffer.stalled is False
    assert "".join(item[2] for item in received if item[0] == "token") == produced_text
    assert sum(1 for item in received if item[0] == "tool_done") == 4
