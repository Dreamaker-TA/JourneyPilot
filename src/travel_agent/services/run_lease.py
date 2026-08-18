"""执行租约的持有者：claim 一次，心跳续租，退出时交还。

租约回答的是「这个 running Run 现在由谁在跑」。它与 SSE 客户端是否在线无关，也不代表
Provider 健康 —— 只代表执行器这个进程还活着并且还能写数据库。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from ..entities.trip_run import RunRecoveryStatus
from ..infrastructure.run_execution_store import RunExecutionStore

logger = logging.getLogger(__name__)

LeaseLostCallback = Callable[[str], Awaitable[None] | None]


class RunLeaseKeeper:
    """一个 Run 的租约。claim 成功后才允许开始执行。"""

    def __init__(
        self,
        store: RunExecutionStore,
        run_id: str,
        *,
        lease_seconds: int,
        heartbeat_seconds: int,
        failure_threshold: int,
        on_lease_lost: Optional[LeaseLostCallback] = None,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._failure_threshold = failure_threshold
        self._on_lease_lost = on_lease_lost
        self._lease_token: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._lost = False

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def held(self) -> bool:
        return self._lease_token is not None and not self._lost

    @property
    def lost(self) -> bool:
        return self._lost

    async def claim(self, *, last_safe_checkpoint_id: Optional[str] = None) -> bool:
        execution = await self._store.claim(
            self._run_id,
            lease_seconds=self._lease_seconds,
            last_safe_checkpoint_id=last_safe_checkpoint_id,
        )
        if execution is None or not execution.lease_token:
            return False
        self._lease_token = execution.lease_token
        self._lost = False
        return True

    def start(self) -> None:
        """开始心跳。claim 之前调用是编程错误。"""

        if self._lease_token is None:
            raise RuntimeError(f"lease not claimed for run {self._run_id}")
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._heartbeat_loop())
        _active_keepers[self._run_id] = self

    async def mark_safe_checkpoint(self, checkpoint_id: str) -> None:
        if not checkpoint_id or self._lease_token is None or self._lost:
            return
        try:
            await self._store.mark_safe_checkpoint(
                self._run_id,
                lease_token=self._lease_token,
                checkpoint_id=checkpoint_id,
            )
        except Exception as exc:
            # 安全边界记录是诊断，不是执行前提：写不进去不该打断这次运行。
            logger.warning(
                "安全 checkpoint 记录失败 run_id=%s error=%s", self._run_id, exc
            )

    async def release(
        self,
        *,
        recovery_status: RunRecoveryStatus = RunRecoveryStatus.RELEASED,
        reason: Optional[str] = None,
    ) -> None:
        _drop_active_keeper(self)
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        token = self._lease_token
        self._lease_token = None
        if token is None:
            return
        try:
            await self._store.release(
                self._run_id,
                lease_token=token,
                recovery_status=recovery_status,
                recovery_reason=reason,
            )
        except Exception as exc:
            # 租约会自己过期，恢复扫描接得住。这里只需要它变成一条日志而不是一次崩溃。
            logger.warning("执行租约释放失败 run_id=%s error=%s", self._run_id, exc)

    async def _heartbeat_loop(self) -> None:
        failures = 0
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            token = self._lease_token
            if token is None:
                return
            try:
                renewed = await self._store.heartbeat(
                    self._run_id,
                    lease_token=token,
                    lease_seconds=self._lease_seconds,
                    recovery_status=RunRecoveryStatus.RUNNING,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                logger.warning(
                    "执行租约心跳失败 run_id=%s 连续第 %d 次 error=%s",
                    self._run_id,
                    failures,
                    exc,
                )
                if failures < self._failure_threshold:
                    continue
                await self._lose_lease("heartbeat_write_failed")
                return
            if not renewed:
                # 行还在，但 token 不是我们的：另一个执行器已经接管，立即停手，
                # 不必等阈值 —— 这不是「写不进去」，这是「已经不是我们的 run 了」。
                await self._lose_lease("lease_taken_over")
                return
            failures = 0

    async def _lose_lease(self, reason: str) -> None:
        self._lost = True
        logger.error(
            "执行租约失效 run_id=%s reason=%s，停止发起新的外部调用",
            self._run_id,
            reason,
        )
        _drop_active_keeper(self)
        if self._on_lease_lost is None:
            return
        try:
            result = self._on_lease_lost(reason)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.error("租约失效回调失败 run_id=%s error=%s", self._run_id, exc)


#: 本进程正在持有租约的 run。关闭时要把它们交还，否则下一次启动要等租约自然过期。
_active_keepers: dict[str, RunLeaseKeeper] = {}


def _drop_active_keeper(keeper: "RunLeaseKeeper") -> None:
    """只在这一格还是自己的时候摘掉。

    盲删按 run_id 摘，摘掉的会是接管者；关闭时 `release_all_leases` 就看不见它，
    它的租约留着一个未来的过期时间，下一个进程要白等一个租约周期。
    """

    if _active_keepers.get(keeper._run_id) is keeper:
        _active_keepers.pop(keeper._run_id, None)


async def release_all_leases(*, reason: str = "process_shutdown") -> int:
    """进程关闭时交还所有租约。

    状态收敛不在这里做：SSE 协程各自的退出路径拥有那件事。这里只保证下一次启动的
    census 立刻就能看见「没有执行器」，而不是先干等一个租约周期。
    """

    keepers = list(_active_keepers.values())
    for keeper in keepers:
        await keeper.release(
            recovery_status=RunRecoveryStatus.SHUTDOWN_REQUESTED,
            reason=reason,
        )
    return len(keepers)
