"""本地后台任务 worker：领取、执行、续租、重试、死信。

不引入外部 broker。数据库是队列，当前进程是唯一的消费者，内存里不留任何「还没做的事」。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, Dict, Optional

from ..entities.background_job import (
    BackgroundJob,
    BackgroundJobPermanentError,
    BackgroundJobStatus,
    BackgroundJobType,
    coerce_job_type,
    retry_delay_seconds,
)
from ..infrastructure.background_job_store import BackgroundJobStore

logger = logging.getLogger(__name__)

JobHandler = Callable[[BackgroundJob], Awaitable[Optional[Dict[str, Any]]]]


class BackgroundJobWorker:
    """轮询 `background_jobs` 并执行任务。"""

    def __init__(
        self,
        store: BackgroundJobStore,
        handlers: Dict[BackgroundJobType, JobHandler],
        *,
        poll_seconds: float = 5.0,
        lease_seconds: int = 60,
        batch_size: int = 1,
        completed_retention_days: int = 30,
    ) -> None:
        self._store = store
        self._handlers = dict(handlers)
        self._poll_seconds = max(0.5, poll_seconds)
        self._lease_seconds = max(5, lease_seconds)
        self._batch_size = max(1, batch_size)
        self._completed_retention_days = max(1, completed_retention_days)
        self._task: Optional[asyncio.Task] = None
        self._wakeup = asyncio.Event()

    def notify(self) -> None:
        """有新任务入队时缩短一次等待。丢了也只影响延迟，不影响正确性。"""

        self._wakeup.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

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

    async def poll_once(self) -> int:
        """领取并执行一批任务，返回执行条数。"""

        jobs = await self._store.claim(lease_seconds=self._lease_seconds, batch=self._batch_size)
        for job in jobs:
            await self._execute(job)
        return len(jobs)

    async def _loop(self) -> None:
        while True:
            try:
                executed = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("后台任务轮询失败: %s", exc, exc_info=True)
                executed = 0
            if executed:
                continue
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), self._poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _execute(self, job: BackgroundJob) -> None:
        handler = self._handlers.get(job.job_type)
        if handler is None:
            await self._store.fail(
                job.job_id,
                error_code="no_handler",
                error_summary=f"没有注册 {job.job_type.value} 的处理器",
                retry_in_seconds=None,
            )
            return

        renewer = asyncio.create_task(self._renew_lease(job.job_id))
        try:
            result = await handler(job)
        except asyncio.CancelledError:
            # 进程正在关闭。租约留给它自己过期，下一次启动重新领取。
            raise
        except BackgroundJobPermanentError as exc:
            await self._store.fail(
                job.job_id,
                error_code=exc.error_code,
                error_summary=str(exc),
                retry_in_seconds=None,
            )
            logger.warning("后台任务永久失败 job=%s code=%s", job.job_id, exc.error_code)
            return
        except Exception as exc:
            delay = retry_delay_seconds(job.attempts, jitter_ratio=random.random() * 0.2)
            status = await self._store.fail(
                job.job_id,
                error_code=exc.__class__.__name__,
                error_summary=str(exc),
                retry_in_seconds=delay,
            )
            if status is BackgroundJobStatus.DEAD:
                logger.warning(
                    "后台任务重试用尽 job=%s type=%s error=%s",
                    job.job_id,
                    job.job_type.value,
                    exc,
                )
            else:
                logger.info(
                    "后台任务将在 %ss 后重试 job=%s attempt=%s error=%s",
                    delay,
                    job.job_id,
                    job.attempts,
                    exc,
                )
            return
        finally:
            renewer.cancel()

        await self._store.complete(job.job_id, result=result)

    async def _renew_lease(self, job_id: str) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._store.renew_lease(job_id, lease_seconds=self._lease_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("后台任务续租失败 job=%s: %s", job_id, exc)

    async def cleanup(self) -> int:
        return await self._store.cleanup_completed(
            retention_days=self._completed_retention_days
        )


def build_job_handlers(components: Any) -> Dict[BackgroundJobType, JobHandler]:
    """任务类型 → 处理器。新增类型时只改这一处。"""

    from .memory_extraction_job import make_memory_extraction_handler

    return {
        coerce_job_type(BackgroundJobType.MEMORY_EXTRACTION): make_memory_extraction_handler(
            chat_session_memory=components.chat_session_memory,
            memory_extractor=components.memory_extractor,
            user_profile_memory=components.user_profile_memory,
        ),
    }
