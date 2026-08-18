"""后台任务的类型合同。最终事实在 `background_jobs`。"""

from __future__ import annotations

import hashlib
import uuid
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from .trip_run import utc_now_iso


def generate_background_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:16]}"


class BackgroundJobType(str, Enum):
    """已经有真实消费者的任务类型。

    `checkpoint_pruning` / `retention_cleanup` 等共享同一套 lease/attempt/backoff 语义，
    但先用一个真实场景把语义跑通，不提前登记没有 handler 的类型。
    """

    MEMORY_EXTRACTION = "memory_extraction"


class BackgroundJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    #: 重试用尽或永久性失败。保留到用户确认，不自动清理。
    DEAD = "dead"
    CANCELLED = "cancelled"


#: 还没有结论的任务。清理绝不碰这三种。
OPEN_BACKGROUND_JOB_STATUSES: set[BackgroundJobStatus] = {
    BackgroundJobStatus.PENDING,
    BackgroundJobStatus.RUNNING,
    BackgroundJobStatus.RETRY_WAIT,
}

#: 指数退避基准（秒）。真实等待时间在此之上叠加抖动。
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (10, 30, 120, 600, 3600)


def retry_delay_seconds(attempts: int, *, jitter_ratio: float = 0.0) -> int:
    """第 `attempts` 次失败后要等多久再试。超出退避表则停在最后一档。"""

    index = max(0, min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1))
    base = RETRY_BACKOFF_SECONDS[index]
    return max(1, int(base * (1.0 + max(0.0, jitter_ratio))))


class BackgroundJobPermanentError(Exception):
    """不可重试的失败：源数据已删除、payload 不合法、业务校验永久不通过。"""

    def __init__(self, error_code: str, message: str = "") -> None:
        super().__init__(message or error_code)
        self.error_code = error_code


class BackgroundJob(BaseModel):
    job_id: str
    job_type: BackgroundJobType
    dedupe_key: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    status: BackgroundJobStatus = BackgroundJobStatus.PENDING
    priority: int = 100
    attempts: int = 0
    max_attempts: int = 5
    available_at: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    last_error_code: Optional[str] = None
    last_error_summary: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    completed_at: Optional[str] = None



def coerce_job_type(value: str | BackgroundJobType) -> BackgroundJobType:
    return value if isinstance(value, BackgroundJobType) else BackgroundJobType(str(value))


def coerce_job_status(value: str | BackgroundJobStatus) -> BackgroundJobStatus:
    return value if isinstance(value, BackgroundJobStatus) else BackgroundJobStatus(str(value))


def memory_extraction_dedupe_key(session_id: str, assistant_message_id: str) -> str:
    """一轮对话一个任务：同一轮重发不会排出第二个抽取。"""

    return f"{session_id}:{assistant_message_id}"


def memory_fact_digest(user_id: str, source_message_id: str, content: str) -> str:
    """同一条来源消息抽出的同一句事实只入库一次。

    重复消费是 at-least-once 的常态（标记 completed 之前崩溃），所以幂等必须落在
    业务写入这一侧，不能指望「job 只会消费一次」。
    """

    material = "\x00".join([user_id, source_message_id, " ".join(content.split())])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
