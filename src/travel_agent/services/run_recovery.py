"""启动恢复：进程死掉之后，把「看起来还在跑」的 Run 说清楚。

两条硬规则：

- **不永久 running**：没有活着的执行器的 running Run 必须收敛到一个诚实的状态；
- **不自动续跑**：恢复不重新调用模型。用户重启程序不该在后台继续产生费用，
  继续必须是一次显式的点击（ADR-P1-02）。

durable completion 永远赢过执行记录：Bundle 已经原子完成的 Run 只清租约，不重跑。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from ..entities.trip_run import (
    RunRecoveryStatus,
    TripRunMode,
    TripRunResumePolicy,
    TripRunStatus,
)
from ..infrastructure.run_execution_store import (
    RunExecutionStore,
    RunRecoveryCandidate,
)

logger = logging.getLogger(__name__)

#: 探测这个 run 的 checkpoint 是否存在且能被当前合同读懂。
CheckpointProbe = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class RunRecoveryOutcome:
    run_id: str
    previous_status: str
    resolved_status: str
    recovery_status: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "previous_status": self.previous_status,
            "resolved_status": self.resolved_status,
            "recovery_status": self.recovery_status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RunRecoveryReport:
    outcomes: tuple[RunRecoveryOutcome, ...] = ()
    failures: tuple[str, ...] = ()

    @property
    def resume_available_count(self) -> int:
        return sum(
            1
            for outcome in self.outcomes
            if outcome.recovery_status == RunRecoveryStatus.RESUME_AVAILABLE.value
        )

    @property
    def counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.recovery_status] = counts.get(outcome.recovery_status, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recovered_run_count": len(self.outcomes),
            "counts": self.counts,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "failures": list(self.failures),
        }

    def log_summary(self) -> None:
        if not self.outcomes and not self.failures:
            logger.info("启动恢复：没有孤儿 Run")
            return
        if self.outcomes:
            logger.warning(
                "启动恢复：%d 个 Run 失去执行器 | %s",
                len(self.outcomes),
                "；".join(
                    f"{outcome.run_id} {outcome.previous_status}→{outcome.resolved_status}"
                    f"（{outcome.recovery_status}/{outcome.reason}）"
                    for outcome in self.outcomes
                ),
            )
        for failure in self.failures:
            logger.error("启动恢复失败：%s", failure)


def _has_durable_bundle(completion_audit: Dict[str, Any]) -> bool:
    delivery = completion_audit.get("formal_delivery")
    if not isinstance(delivery, dict):
        return False
    return bool(delivery.get("has_bundle")) and bool(delivery.get("bundle_id"))


class RunRecoveryService:
    """启动扫一次，之后按周期复扫。

    只在启动扫一次是不够的：上一个进程死的那一刻租约可能还剩几十秒，那批 Run 会被
    第一次扫描正确地跳过（它们的租约还没过期），然后永远没人再看它们一眼。
    """

    def __init__(
        self,
        *,
        trip_run_store: Any,
        execution_store: RunExecutionStore,
        checkpoint_probe: Optional[CheckpointProbe] = None,
        sweep_seconds: int = 60,
    ) -> None:
        self._trip_run_store = trip_run_store
        self._execution_store = execution_store
        self._checkpoint_probe = checkpoint_probe
        self._sweep_seconds = max(5, sweep_seconds)
        self._task: Optional[asyncio.Task] = None
        self.last_report = RunRecoveryReport()

    async def sweep(self) -> RunRecoveryReport:
        candidates = await self._execution_store.list_recovery_candidates()
        outcomes: list[RunRecoveryOutcome] = []
        failures: list[str] = []
        for candidate in candidates:
            try:
                outcome = await self._recover(candidate)
            except Exception as exc:
                logger.error(
                    "恢复判定失败 run_id=%s error=%s", candidate.run_id, exc, exc_info=True
                )
                failures.append(f"{candidate.run_id}: {type(exc).__name__}: {exc}")
                continue
            if outcome is not None:
                outcomes.append(outcome)
        report = RunRecoveryReport(outcomes=tuple(outcomes), failures=tuple(failures))
        self.last_report = report
        return report

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._sweep_loop())

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

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_seconds)
            try:
                report = await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("孤儿扫描失败: %s", exc, exc_info=True)
                continue
            if report.outcomes or report.failures:
                report.log_summary()

    async def _recover(self, candidate: RunRecoveryCandidate) -> Optional[RunRecoveryOutcome]:
        if candidate.status == TripRunStatus.CANCEL_REQUESTED:
            return await self._converge_cancelled(candidate)
        if candidate.status == TripRunStatus.RUNNING:
            return await self._converge_running(candidate)
        return await self._settle_inactive(candidate)

    async def _converge_cancelled(
        self, candidate: RunRecoveryCandidate
    ) -> RunRecoveryOutcome:
        """取消请求在重启恢复时完成。durable completion 优先，所以先看还能不能转。"""

        reason = "cancel_requested_completed_during_recovery"
        await self._execution_store.record_recovery(
            candidate.run_id,
            recovery_status=RunRecoveryStatus.RELEASED,
            recovery_reason=reason,
        )
        try:
            await self._trip_run_store.transition_status(
                candidate.run_id,
                TripRunStatus.CANCELLED,
                current_node=candidate.current_node,
                event_type="run.cancelled",
                payload={"reason": reason},
            )
        except ValueError:
            # 状态在这两次写之间又动了（例如 Bundle 原子完成先提交）。durable 记录赢。
            return RunRecoveryOutcome(
                run_id=candidate.run_id,
                previous_status=candidate.status.value,
                resolved_status="unchanged",
                recovery_status=RunRecoveryStatus.RELEASED.value,
                reason="durable_status_moved_during_recovery",
            )
        return RunRecoveryOutcome(
            run_id=candidate.run_id,
            previous_status=candidate.status.value,
            resolved_status=TripRunStatus.CANCELLED.value,
            recovery_status=RunRecoveryStatus.RELEASED.value,
            reason=reason,
        )

    async def _converge_running(
        self, candidate: RunRecoveryCandidate
    ) -> RunRecoveryOutcome:
        resumable, reason = await self._resume_verdict(candidate)
        recovery_status = (
            RunRecoveryStatus.RESUME_AVAILABLE if resumable else RunRecoveryStatus.NON_RESUMABLE
        )
        await self._execution_store.record_recovery(
            candidate.run_id,
            recovery_status=recovery_status,
            recovery_reason=reason,
        )
        try:
            await self._trip_run_store.transition_status(
                candidate.run_id,
                TripRunStatus.INTERRUPTED,
                current_node=candidate.current_node,
                event_type="run.interrupted",
                payload={"reason": reason, "recovery_status": recovery_status.value},
            )
        except ValueError:
            return RunRecoveryOutcome(
                run_id=candidate.run_id,
                previous_status=candidate.status.value,
                resolved_status="unchanged",
                recovery_status=recovery_status.value,
                reason="durable_status_moved_during_recovery",
            )
        return RunRecoveryOutcome(
            run_id=candidate.run_id,
            previous_status=candidate.status.value,
            resolved_status=TripRunStatus.INTERRUPTED.value,
            recovery_status=recovery_status.value,
            reason=reason,
        )

    async def _resume_verdict(self, candidate: RunRecoveryCandidate) -> tuple[bool, str]:
        if candidate.resume_policy != TripRunResumePolicy.CHECKPOINT.value:
            return False, "run_has_no_checkpoint_resume_policy"
        if self._checkpoint_probe is None:
            return False, "checkpointer_unavailable"
        try:
            available = await self._checkpoint_probe(candidate.run_id)
        except Exception as exc:
            # 当前合同读不懂旧 checkpoint：明确不可恢复，而不是让它在工作流中途炸开。
            logger.warning(
                "checkpoint 合同校验拒绝恢复 run_id=%s error=%s", candidate.run_id, exc
            )
            return False, "checkpoint_contract_mismatch"
        if not available:
            return False, "process_restarted_without_checkpoint"
        return True, "process_restarted"

    async def _settle_inactive(
        self, candidate: RunRecoveryCandidate
    ) -> Optional[RunRecoveryOutcome]:
        """已经收口的 Run 只剩残留租约。矛盾的 durable 事实不猜，只标诊断。"""

        if (
            candidate.status == TripRunStatus.COMPLETED
            and candidate.mode == TripRunMode.DEEP.value
            and not _has_durable_bundle(candidate.completion_audit)
        ):
            reason = "completed_without_durable_bundle"
            await self._execution_store.record_recovery(
                candidate.run_id,
                recovery_status=RunRecoveryStatus.RECOVERY_CONTRACT_FAILURE,
                recovery_reason=reason,
            )
            return RunRecoveryOutcome(
                run_id=candidate.run_id,
                previous_status=candidate.status.value,
                resolved_status="unchanged",
                recovery_status=RunRecoveryStatus.RECOVERY_CONTRACT_FAILURE.value,
                reason=reason,
            )
        released = await self._execution_store.release(
            candidate.run_id,
            recovery_status=RunRecoveryStatus.RELEASED,
            recovery_reason="executor_gone_on_inactive_run",
        )
        if not released:
            return None
        return RunRecoveryOutcome(
            run_id=candidate.run_id,
            previous_status=candidate.status.value,
            resolved_status="unchanged",
            recovery_status=RunRecoveryStatus.RELEASED.value,
            reason="executor_gone_on_inactive_run",
        )
