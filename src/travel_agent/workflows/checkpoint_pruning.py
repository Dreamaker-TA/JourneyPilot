"""Checkpoint retention pruning service."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..config import CheckpointRetentionConfig

logger = logging.getLogger(__name__)


@dataclass
class CheckpointPruneResult:
    scanned: int = 0
    pruned: int = 0
    failed: int = 0
    pruned_run_ids: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)


class CheckpointPruningService:
    """Delete old LangGraph checkpoint threads for expired TripRuns."""

    def __init__(
        self,
        *,
        trip_run_store: Any,
        checkpointer: Any,
        retention: CheckpointRetentionConfig,
    ) -> None:
        self.trip_run_store = trip_run_store
        self.checkpointer = checkpointer
        self.retention = retention

    async def prune_once(self) -> CheckpointPruneResult:
        if self.checkpointer is None:
            raise ValueError("checkpointer is required for checkpoint pruning")

        candidates = await self.trip_run_store.list_checkpoint_prune_candidates(
            completed_days=self.retention.completed_days,
            cancelled_days=self.retention.cancelled_days,
            failed_interrupted_days=self.retention.failed_interrupted_days,
            limit=self.retention.batch_size,
        )
        result = CheckpointPruneResult(scanned=len(candidates))

        for run in candidates:
            try:
                await self.checkpointer.adelete_thread(run.run_id)
                result.pruned += 1
                result.pruned_run_ids.append(run.run_id)
                await self.trip_run_store.append_event(
                    run.run_id,
                    "checkpoint.pruned",
                    {
                        "status": run.status.value,
                        "resume_policy": run.resume_policy.value,
                        "updated_at": run.updated_at,
                    },
                )
            except Exception as exc:
                result.failed += 1
                result.errors[run.run_id] = str(exc)
                logger.warning("checkpoint prune failed for run %s: %s", run.run_id, exc)

        return result

