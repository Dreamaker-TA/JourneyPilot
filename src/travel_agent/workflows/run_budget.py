"""Run 预算的记账与守卫。

分工：`entities/run_budget.py` 是纯判据（上限、消耗、还剩多少、哪一维用完了），这里
是它在一个进程里的账本与调用边界。

**账本是进程内的，快照不是。** 快照随 checkpoint 走；消耗量的最终事实在
``run_llm_calls`` 台账里，所以一个 Run 在本进程第一次被记账之前要先从台账把已花的量
读回来（:func:`seed_run_budget`）—— 否则重启后继续跑的 Run 会拿到一份满额预算，而
「重启不许悄悄多花钱」正是 P1 的那条 ADR。

工具调用不落台账（它们进 `tool_audit`，那里没有 token/费用），所以工具计数只在进程内
成立。这是有意的取舍：工具调用的成本上限是「别无限循环」，而无限循环发生在一个进程里。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..config import get_settings
from ..entities.run_budget import (
    RUN_BUDGET_POLICY_VERSION,
    BudgetDimension,
    RunBudgetSnapshot,
    RunBudgetUsage,
    exhausted_dimension,
    remaining_budget,
)

logger = logging.getLogger(__name__)


class RunBudgetExhausted(RuntimeError):
    """一次新调用会越过预算的某一维。

    和 `ModelWindowClosed` 同一类：**用户没有取消**，所以调用方必须走确定性收口，
    不能把它记成 CANCELLED。区别在 reason code —— 时间用完和钱用完不是同一件事。
    """

    def __init__(
        self,
        operation: str,
        dimension: BudgetDimension,
        snapshot: RunBudgetSnapshot,
        usage: RunBudgetUsage,
    ) -> None:
        self.operation = operation
        self.dimension = dimension
        self.snapshot = snapshot
        self.usage = usage
        super().__init__(
            f"run budget exhausted on {dimension} before {operation} "
            f"(spent={usage.spent(dimension)}, limit={snapshot.limit(dimension)})"
        )

    @property
    def reason_code(self) -> str:
        return f"run_budget_exhausted.{self.dimension}"


def build_run_budget_snapshot(
    *, policy_version: str = RUN_BUDGET_POLICY_VERSION
) -> RunBudgetSnapshot:
    """按当前配置封存一份上限。只在 Run 被授权时调用一次。"""

    config = get_settings().run_budget
    return RunBudgetSnapshot(
        policy_version=policy_version,
        max_llm_calls=config.max_llm_calls,
        max_tool_calls=config.max_tool_calls,
        max_input_tokens=config.max_input_tokens,
        max_output_tokens=config.max_output_tokens,
        max_cost_usd=config.max_cost_usd,
        max_tool_retries_per_target=config.max_tool_retries_per_target,
    )


@dataclass
class RunBudgetLedger:
    """一个 Run 在本进程的消耗账本。"""

    run_id: str
    snapshot: RunBudgetSnapshot
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    #: 有没有出现过价格表没命中的调用。见 `RunBudgetUsage.cost_complete`。
    unpriced_calls: int = 0
    #: 每个工具在这个 Run 上重试了多少轮。
    tool_attempts: Dict[str, int] = field(default_factory=dict)
    seeded: bool = False

    def usage(self) -> RunBudgetUsage:
        return RunBudgetUsage(
            llm_calls=self.llm_calls,
            tool_calls=self.tool_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=round(self.cost_usd, 8),
            cost_complete=self.unpriced_calls == 0,
        )

    def guard(
        self,
        operation: str,
        *,
        llm_calls: int = 0,
        tool_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        usage = self.usage()
        dimension = exhausted_dimension(
            self.snapshot,
            usage,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if dimension is not None:
            raise RunBudgetExhausted(operation, dimension, self.snapshot, usage)

    def record_llm_call(
        self,
        *,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        cost_usd: Optional[float],
    ) -> None:
        self.llm_calls += 1
        self.input_tokens += max(0, int(input_tokens or 0))
        self.output_tokens += max(0, int(output_tokens or 0))
        if cost_usd is None:
            self.unpriced_calls += 1
        else:
            self.cost_usd += float(cost_usd)

    def record_tool_call(self, tool_name: str) -> None:
        self.tool_calls += 1
        self.tool_attempts[tool_name] = self.tool_attempts.get(tool_name, 0) + 1

    def tool_retries_exhausted(self, tool_name: str) -> bool:
        """这个工具在这个 Run 上是否已经用完了它的重试轮数。"""

        allowed = self.snapshot.max_tool_retries_per_target + 1
        return self.tool_attempts.get(tool_name, 0) >= allowed

    def report(self) -> Dict[str, Any]:
        """给 SSE / REST / doctor 的那一份读数。"""

        usage = self.usage()
        return {
            "policy_version": self.snapshot.policy_version,
            "limits": self.snapshot.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
            "remaining": remaining_budget(self.snapshot, usage),
        }


_LEDGERS: Dict[str, RunBudgetLedger] = {}


def ledger_for(run_id: str, snapshot: RunBudgetSnapshot) -> RunBudgetLedger:
    """这个 Run 在本进程的账本，首次使用时按快照建立。

    快照换了（编辑行程后重新授权）就换一本账：新的一代 Draft 有自己的预算，旧的消耗
    量属于上一代。
    """

    existing = _LEDGERS.get(run_id)
    if existing is not None and existing.snapshot == snapshot:
        return existing
    ledger = RunBudgetLedger(run_id=run_id, snapshot=snapshot)
    _LEDGERS[run_id] = ledger
    return ledger


def peek_ledger(run_id: Optional[str]) -> Optional[RunBudgetLedger]:
    if not run_id:
        return None
    return _LEDGERS.get(run_id)


def release_ledger(run_id: Optional[str]) -> None:
    if run_id:
        _LEDGERS.pop(run_id, None)


def reset_ledgers() -> None:
    """只供测试在用例之间隔离账本。"""

    _LEDGERS.clear()


async def seed_run_budget(
    run_id: str,
    snapshot: RunBudgetSnapshot,
    *,
    cost_ledger_store: Any,
) -> RunBudgetLedger:
    """把台账里已花的 token/费用读回本进程的账本。

    只做一次（``seeded``）。读不到台账时**不清零**也不阻断：账本从 0 起算意味着一个
    恢复的 Run 可能多花一轮，而拒绝启动意味着它一点都跑不了 —— 前者是可以看见的偏差，
    后者是一次不必要的中断。
    """

    ledger = ledger_for(run_id, snapshot)
    if ledger.seeded:
        return ledger
    ledger.seeded = True
    try:
        summary = await cost_ledger_store.run_summary(run_id)
    except Exception as exc:
        logger.warning("预算基线读取失败，账本从 0 起算 | run=%s error=%s", run_id, exc)
        return ledger
    totals = summary or {}
    ledger.llm_calls = int(totals.get("call_count") or 0)
    ledger.input_tokens = int(totals.get("total_input_tokens") or 0)
    ledger.output_tokens = int(totals.get("total_output_tokens") or 0)
    ledger.cost_usd = float(totals.get("total_cost_usd") or 0.0)
    ledger.unpriced_calls = max(0, int(totals.get("unpriced_call_count") or 0))
    if ledger.llm_calls:
        logger.info(
            "预算基线已载入 | run=%s calls=%d in=%d out=%d cost=%.6f",
            run_id,
            ledger.llm_calls,
            ledger.input_tokens,
            ledger.output_tokens,
            ledger.cost_usd,
        )
    return ledger
