"""Turn 分页与增量压缩：一个 turn 永远整份返回、游标不漂移、边界只推到进了摘要的那一条。"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from travel_agent.memory.chat_session import ChatSessionMemory
from travel_agent.memory.compaction import CompactionService
from travel_agent.memory.compressor import AnchorSummary
from travel_agent.infrastructure.database import get_db_session

pytestmark = pytest.mark.postgres

_USER = "local"


class _FakeCompressor:
    """不调模型的压缩器：摘要就是「压了几条」，判据落在边界与事务上。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def compress(self, messages, existing_anchor=None, model="gpt-4o"):
        self.calls.append(list(messages))
        return AnchorSummary(
            compressed_at="2026-08-18T00:00:00+00:00",
            messages_compressed=len(messages),
            tokens_before=100 * len(messages),
            tokens_after=10,
            key_constraints=["不吃香菜"],
            summary=f"合并了 {len(messages)} 条",
        )


async def _write_turns(
    memory: ChatSessionMemory, session_id: str, count: int, *, steps: int = 2, start: int = 0
):
    for index in range(start, start + count):
        await memory.save_turn(
            session_id=session_id,
            user_id=_USER,
            mode="fast",
            user_message=f"问题 {index}",
            user_message_id=f"u-{index}",
            assistant_message_id=f"a-{index}",
            assistant_content=f"回答 {index}",
            assistant_display_content=f"回答 {index}",
            thinking_steps=[
                {"step_id": f"s-{index}-{n}", "content": f"想 {n}", "agent_name": "x", "step_name": "y"}
                for n in range(steps)
            ],
            controlled_trip_identity=None,
        )


async def test_a_turn_is_always_returned_whole(migrated_async_database):
    memory = ChatSessionMemory()
    await _write_turns(memory, "s-1", 3, steps=3)

    page = await memory.list_turns(_USER, "s-1", limit=2)

    assert len(page["turns"]) == 2
    for turn in page["turns"]:
        roles = [message["role"] for message in turn["messages"]]
        assert roles == ["user", "assistant"]
        # 思考步跟着它的助手消息走，不会被切到另一页。
        assert len(turn["messages"][1]["thinking_steps"]) == 3
    assert page["has_more"] is True
    assert page["next_before"] == page["turns"][0]["cursor"]


async def test_paging_backwards_covers_every_turn_exactly_once(migrated_async_database):
    memory = ChatSessionMemory()
    await _write_turns(memory, "s-1", 7)

    seen: list[str] = []
    cursor = None
    while True:
        page = await memory.list_turns(_USER, "s-1", before_turn=cursor, limit=3)
        seen = [turn["turn_id"] for turn in page["turns"]] + seen
        if not page["has_more"]:
            break
        cursor = page["next_before"]

    assert len(seen) == 7
    assert len(set(seen)) == 7


async def test_a_new_turn_does_not_shift_an_open_cursor(migrated_async_database):
    """游标是 event_order，不是 offset —— 翻页途中来了新消息不会让某一轮被跳过。"""

    memory = ChatSessionMemory()
    await _write_turns(memory, "s-1", 4)
    first_page = await memory.list_turns(_USER, "s-1", limit=2)
    cursor = first_page["next_before"]

    await _write_turns(memory, "s-1", 1)

    older = await memory.list_turns(_USER, "s-1", before_turn=cursor, limit=2)
    assert [turn["turn_id"] for turn in older["turns"]] != [
        turn["turn_id"] for turn in first_page["turns"]
    ]
    assert all(
        turn["cursor"] < cursor for turn in older["turns"]
    ) or all(int(turn["cursor"][1:]) < int(cursor[1:]) for turn in older["turns"])


async def test_session_detail_returns_only_the_latest_page(migrated_async_database):
    memory = ChatSessionMemory()
    await _write_turns(memory, "s-1", 40)

    detail = await memory.get_session_detail(_USER, "s-1")

    assert detail is not None
    assert detail["has_more"] is True
    assert detail["next_before"] is not None
    # 30 个 turn × 2 条消息。
    assert len(detail["messages"]) == 60


async def test_ten_thousand_events_still_page_in_one_screen(migrated_async_database):
    """1,000 轮 / 约 10,000 事件：最新一页只读它自己那几十条。"""

    memory = ChatSessionMemory()
    await _write_turns(memory, "s-big", 1000, steps=8)

    async with get_db_session() as session:
        total = await session.execute(
            text("SELECT count(*) FROM chat_session_events WHERE session_id = 's-big'")
        )
    assert total.scalar() >= 10_000

    page = await memory.list_turns(_USER, "s-big", limit=30)
    assert len(page["turns"]) == 30
    assert page["has_more"] is True
    # 最新的那个 turn 要在页尾：翻页取的是**最近**一页，不是最早一页。
    assert page["turns"][-1]["messages"], "最新一页不该是空的"


async def test_paging_backwards_walks_the_whole_session(migrated_async_database):
    """有界窗口不能漏 turn：一路往回翻要能走完全部 40 轮。

    分组从「整个会话 GROUP BY」改成「倒序一窗再分组」之后，窗口边界上那个可能被截断
    的 turn 是唯一的风险点。
    """

    memory = ChatSessionMemory()
    await _write_turns(memory, "s-walk", 40, steps=2)

    seen: list[str] = []
    cursor = None
    for _ in range(20):
        page = await memory.list_turns(_USER, "s-walk", before_turn=cursor, limit=10)
        seen.extend(turn["turn_id"] for turn in page["turns"])
        if not page["has_more"]:
            break
        cursor = page["next_before"]

    assert len(seen) == 40, f"漏了 turn：只走到 {len(seen)} 个"
    assert len(set(seen)) == 40, "同一个 turn 被返回了两次"


async def test_a_page_stops_at_the_event_budget(migrated_async_database):
    """一个 turn 里塞几百个思考步时，页面按事件总量收口，不按 turn 数硬凑。"""

    memory = ChatSessionMemory()
    await _write_turns(memory, "s-1", 6, steps=400)

    page = await memory.list_turns(_USER, "s-1", limit=30)
    assert 0 < len(page["turns"]) < 6
    assert page["has_more"] is True


async def test_compaction_only_reads_the_budget_and_moves_the_exact_boundary(
    migrated_async_database,
):
    memory = ChatSessionMemory()
    compressor = _FakeCompressor()
    from travel_agent.memory.context_builder import ContextBudget

    budget = ContextBudget()
    budget.max_messages_per_compaction = 4
    service = CompactionService(
        chat_session_memory=memory, compressor=compressor, budget=budget
    )
    await _write_turns(memory, "s-1", 5, steps=0)

    result = await service.compact(user_id=_USER, session_id="s-1", source="manual")

    assert result is not None
    # 只读了预算内的 4 条，不是全部 10 条。
    assert result.messages_selected == 4
    assert len(compressor.calls[0]) == 4
    boundary = await memory.get_compaction_boundary(_USER, "s-1")
    # 边界停在真正进了摘要的最后一条，不是历史末尾。
    assert boundary == result.last_included_event_order
    assert boundary < await _max_event_order("s-1")


async def test_the_compaction_snapshot_lands_in_the_same_transaction(migrated_async_database):
    memory = ChatSessionMemory()
    service = CompactionService(chat_session_memory=memory, compressor=_FakeCompressor())
    await _write_turns(memory, "s-1", 2, steps=0)

    result = await service.compact(user_id=_USER, session_id="s-1", source="manual")

    assert result is not None
    async with get_db_session() as session:
        rows = await session.execute(
            text(
                "SELECT payload ->> 'event_id' AS event_id FROM chat_session_events "
                "WHERE session_id = 's-1' AND event_type = 'context_compaction'"
            )
        )
    assert [row["event_id"] for row in rows.mappings().all()] == [result.event["event_id"]]
    # 快照自己成一个 turn，翻页时不会挂在别人那一轮里。
    page = await memory.list_turns(_USER, "s-1", limit=10)
    compaction_turns = [
        turn for turn in page["turns"]
        if any(message["type"] == "context_compaction" for message in turn["messages"])
    ]
    assert len(compaction_turns) == 1
    assert len(compaction_turns[0]["messages"]) == 1


async def test_a_second_compaction_is_incremental(migrated_async_database):
    memory = ChatSessionMemory()
    compressor = _FakeCompressor()
    service = CompactionService(chat_session_memory=memory, compressor=compressor)
    await _write_turns(memory, "s-1", 2, steps=0)
    await service.compact(user_id=_USER, session_id="s-1", source="manual")

    await _write_turns(memory, "s-1", 1, steps=0, start=2)
    second = await service.compact(user_id=_USER, session_id="s-1", source="automatic")

    assert second is not None
    # 第二次只看到新增的那一轮：已经折叠进摘要的历史不再逐条重读。
    assert [message["content"] for message in compressor.calls[1]] == ["问题 2", "回答 2"]


async def test_nothing_to_compact_returns_none(migrated_async_database):
    memory = ChatSessionMemory()
    service = CompactionService(chat_session_memory=memory, compressor=_FakeCompressor())
    await memory.ensure_session(
        session_id="s-empty", user_id=_USER, mode="fast", title_seed="", controlled_trip_identity=None
    )

    assert await service.compact(user_id=_USER, session_id="s-empty", source="manual") is None


async def test_a_failed_compaction_leaves_the_anchor_and_boundary_alone(
    migrated_async_database,
):
    memory = ChatSessionMemory()

    class _Broken:
        async def compress(self, messages, existing_anchor=None, model="gpt-4o"):
            raise RuntimeError("provider down")

    service = CompactionService(chat_session_memory=memory, compressor=_Broken())
    await _write_turns(memory, "s-1", 2, steps=0)

    with pytest.raises(RuntimeError):
        await service.compact(user_id=_USER, session_id="s-1", source="manual")

    assert await memory.get_compaction_boundary(_USER, "s-1") == 0
    anchor, count = await memory.get_anchor(_USER, "s-1")
    assert anchor is None and count == 0


async def _max_event_order(session_id: str) -> int:
    async with get_db_session() as session:
        result = await session.execute(
            text("SELECT COALESCE(MAX(event_order), 0) FROM chat_session_events WHERE session_id = :sid"),
            {"sid": session_id},
        )
    return int(result.scalar() or 0)


async def test_a_raced_commit_is_busy_not_nothing_to_compact(migrated_async_database):
    """提交时边界被抢先，答案不能是「会话中无可整理的消息」。

    那个会话有几千条没压缩的消息，而路由照着这句话回 400，用户没有理由再试一次。
    """

    from travel_agent.memory.compaction import CompactionBusy

    memory = ChatSessionMemory()
    service = CompactionService(chat_session_memory=memory, compressor=_FakeCompressor())
    await _write_turns(memory, "s-race", 2, steps=0)

    async def _lose_the_cas(*args, **kwargs):
        return None

    memory.commit_compaction = _lose_the_cas

    with pytest.raises(CompactionBusy):
        await service.compact(user_id=_USER, session_id="s-race", source="manual")
