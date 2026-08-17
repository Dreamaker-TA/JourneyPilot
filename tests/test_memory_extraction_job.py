"""memory_extraction 任务处理器的合同：正文从会话记录读，源没了就是永久失败。"""

from __future__ import annotations

import pytest

from travel_agent.entities.background_job import (
    BackgroundJob,
    BackgroundJobPermanentError,
    BackgroundJobType,
)
from travel_agent.memory.memory_extractor import MemoryExtractionOutcome
from travel_agent.services.memory_extraction_job import make_memory_extraction_handler


class _Sessions:
    def __init__(self, messages: dict[str, str]) -> None:
        self._messages = messages

    async def get_message_content(self, *, session_id: str, message_id: str):
        return self._messages.get(message_id)


class _Extractor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def extract_from_turn(self, **kwargs):
        self.calls.append(kwargs)
        return MemoryExtractionOutcome(facts_written=2, portraits_written=1)


class _Profiles:
    async def get_revision(self, user_id: str) -> int:
        return 7


def _job(**payload) -> BackgroundJob:
    base = {
        "user_id": "local",
        "session_id": "s-1",
        "user_message_id": "m-1",
        "assistant_message_id": "a-1",
        "profile_revision": 3,
        "portrait_baseline": "喜欢慢节奏",
    }
    base.update(payload)
    return BackgroundJob(
        job_id="job_1",
        job_type=BackgroundJobType.MEMORY_EXTRACTION,
        dedupe_key="s-1:a-1",
        payload=base,
    )


async def test_handler_reads_the_message_from_the_session_record():
    extractor = _Extractor()
    handle = make_memory_extraction_handler(
        chat_session_memory=_Sessions({"m-1": "我吃素，不吃海鲜"}),
        memory_extractor=extractor,
        user_profile_memory=_Profiles(),
    )

    result = await handle(_job())

    assert extractor.calls[0]["user_msg"] == "我吃素，不吃海鲜"
    # 画像基线是入队那一刻固定下来的，不是执行时的最新画像。
    assert extractor.calls[0]["existing_portrait"] == "喜欢慢节奏"
    assert extractor.calls[0]["source_message_id"] == "m-1"
    assert result["facts"] == 2 and result["portraits"] == 1
    assert result["profile_revision_at_enqueue"] == 3
    assert result["profile_revision_now"] == 7


async def test_a_deleted_source_message_is_not_retryable():
    handle = make_memory_extraction_handler(
        chat_session_memory=_Sessions({}),
        memory_extractor=_Extractor(),
        user_profile_memory=_Profiles(),
    )

    with pytest.raises(BackgroundJobPermanentError) as excinfo:
        await handle(_job())
    assert excinfo.value.error_code == "source_message_deleted"


async def test_a_payload_without_references_is_not_retryable():
    handle = make_memory_extraction_handler(
        chat_session_memory=_Sessions({"m-1": "x"}),
        memory_extractor=_Extractor(),
        user_profile_memory=_Profiles(),
    )

    with pytest.raises(BackgroundJobPermanentError) as excinfo:
        await handle(_job(user_message_id=""))
    assert excinfo.value.error_code == "invalid_payload"
