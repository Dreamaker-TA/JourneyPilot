"""Run 资源预算：调用前守卫、快照不受热更新影响、耗尽后的收口方式。"""

from __future__ import annotations

import pytest

from travel_agent.config import get_settings
from travel_agent.entities.run_budget import (
    RunBudgetSnapshot,
    RunBudgetUsage,
    exhausted_dimension,
    remaining_budget,
)
from travel_agent.workflows.run_budget import (
    RunBudgetExhausted,
    build_run_budget_snapshot,
    ledger_for,
    peek_ledger,
    release_ledger,
    reset_ledgers,
    seed_run_budget,
)
from travel_agent.workflows.run_control import (
    current_run_budget,
    current_run_id,
    guard_run_budget,
)


@pytest.fixture(autouse=True)
def _isolate_ledgers():
    reset_ledgers()
    yield
    reset_ledgers()


@pytest.fixture
def budget_config():
    config = get_settings().run_budget
    original = config.model_dump()
    yield config
    for key, value in original.items():
        setattr(config, key, value)


def _snapshot(**overrides) -> RunBudgetSnapshot:
    payload = dict(
        max_llm_calls=10,
        max_tool_calls=10,
        max_input_tokens=10_000,
        max_output_tokens=1_000,
        max_cost_usd=1.0,
        max_tool_retries_per_target=1,
    )
    payload.update(overrides)
    return RunBudgetSnapshot(**payload)


# --- 纯判据 -------------------------------------------------------------- #


def test_worst_case_estimate_blocks_the_call_that_would_overspend():
    """判据看的是「这次最坏花多少」，不是「已经花了多少」。

    只在调用后记账拦不住超支：第 11 次调用之后再发现越过 10 次上限，那 11 次已经
    花掉了。
    """

    snapshot = _snapshot(max_llm_calls=10)
    usage = RunBudgetUsage(llm_calls=10)
    assert exhausted_dimension(snapshot, usage) is None  # 刚好用满，没有再发
    assert exhausted_dimension(snapshot, usage, llm_calls=1) == "llm_calls"


def test_output_token_ceiling_is_counted_before_the_call():
    snapshot = _snapshot(max_output_tokens=1_000)
    usage = RunBudgetUsage(output_tokens=900)
    assert exhausted_dimension(snapshot, usage, output_tokens=200) == "output_tokens"


def test_an_unpriced_call_never_lets_cost_reject_a_call():
    """价格表没命中时费用低报，而一个低报的读数不许当上限用。"""

    snapshot = _snapshot(max_cost_usd=0.5)
    incomplete = RunBudgetUsage(cost_usd=9.0, cost_complete=False)
    assert exhausted_dimension(snapshot, incomplete) is None
    complete = RunBudgetUsage(cost_usd=9.0, cost_complete=True)
    assert exhausted_dimension(snapshot, complete) == "cost_usd"


def test_remaining_never_goes_negative():
    snapshot = _snapshot(max_llm_calls=3)
    remaining = remaining_budget(snapshot, RunBudgetUsage(llm_calls=9))
    assert remaining["llm_calls"] == 0.0


# --- 账本 ---------------------------------------------------------------- #


def test_a_failed_call_still_consumes_a_call_slot():
    """失败的调用一样占配额：它一样能被无限循环重复。"""

    ledger = ledger_for("run_a", _snapshot(max_llm_calls=2))
    ledger.record_llm_call(input_tokens=None, output_tokens=None, cost_usd=None)
    assert ledger.usage().llm_calls == 1
    assert ledger.usage().cost_complete is False


def test_a_new_snapshot_starts_a_new_ledger():
    """编辑行程后重新授权是新的一代 Draft，旧的消耗属于上一代。"""

    first = ledger_for("run_b", _snapshot(max_llm_calls=5))
    first.record_llm_call(input_tokens=10, output_tokens=10, cost_usd=0.1)
    second = ledger_for("run_b", _snapshot(max_llm_calls=9))
    assert second is not first
    assert second.usage().llm_calls == 0


def test_per_tool_retries_are_bounded():
    ledger = ledger_for("run_c", _snapshot(max_tool_retries_per_target=1))
    assert ledger.tool_retries_exhausted("search") is False
    ledger.record_tool_call("search")
    assert ledger.tool_retries_exhausted("search") is False  # 1 次首发 + 1 次重试
    ledger.record_tool_call("search")
    assert ledger.tool_retries_exhausted("search") is True
    assert ledger.tool_retries_exhausted("other") is False


def test_report_states_limits_usage_and_remaining_together():
    ledger = ledger_for("run_d", _snapshot(max_llm_calls=4))
    ledger.record_llm_call(input_tokens=100, output_tokens=20, cost_usd=0.01)
    report = ledger.report()
    assert report["limits"]["max_llm_calls"] == 4
    assert report["usage"]["llm_calls"] == 1
    assert report["remaining"]["llm_calls"] == 3


# --- 快照 ---------------------------------------------------------------- #


def test_a_sealed_snapshot_ignores_later_config_changes(budget_config):
    budget_config.max_llm_calls = 7
    sealed = build_run_budget_snapshot()
    assert sealed.max_llm_calls == 7
    budget_config.max_llm_calls = 999
    ledger = ledger_for("run_e", sealed)
    assert ledger.snapshot.max_llm_calls == 7


# --- 调用边界 ------------------------------------------------------------ #


def test_guard_is_a_no_op_without_a_sealed_budget():
    """快问快答与授权之前的阶段不封预算，不该凭空造一份来判它们。"""

    token = current_run_id.set("run_f")
    try:
        guard_run_budget("model.ainvoke", llm_calls=1)
    finally:
        current_run_id.reset(token)


def test_guard_raises_with_the_dimension_that_ran_out():
    snapshot = _snapshot(max_tool_calls=1)
    token_run = current_run_id.set("run_g")
    token_budget = current_run_budget.set(snapshot)
    try:
        guard_run_budget("tool.search", tool_calls=1)
        ledger_for("run_g", snapshot).record_tool_call("search")
        with pytest.raises(RunBudgetExhausted) as exc:
            guard_run_budget("tool.search", tool_calls=1)
    finally:
        current_run_budget.reset(token_budget)
        current_run_id.reset(token_run)
    assert exc.value.dimension == "tool_calls"
    assert exc.value.reason_code == "run_budget_exhausted.tool_calls"


# --- 重启基线 ------------------------------------------------------------ #


class _FakeCostLedger:
    def __init__(self, summary) -> None:
        self._summary = summary
        self.calls = 0

    async def run_summary(self, run_id: str):
        self.calls += 1
        if isinstance(self._summary, Exception):
            raise self._summary
        return self._summary


async def test_a_resumed_run_reads_its_spend_back_from_the_ledger():
    """重启后不许拿到一份满额预算 —— 那正是「重启不悄悄多花钱」要防的事。"""

    store = _FakeCostLedger(
        {
            "call_count": 42,
            "priced_call_count": 42,
            "unpriced_call_count": 0,
            "total_input_tokens": 5_000,
            "total_output_tokens": 900,
            "total_cost_usd": 0.42,
        }
    )
    snapshot = _snapshot()
    ledger = await seed_run_budget("run_h", snapshot, cost_ledger_store=store)
    assert ledger.usage().llm_calls == 42
    assert ledger.usage().input_tokens == 5_000
    assert ledger.usage().cost_usd == pytest.approx(0.42)

    # 只读一次：每个节点边界都查一遍台账是一次没有理由的重复查询。
    await seed_run_budget("run_h", snapshot, cost_ledger_store=store)
    assert store.calls == 1


async def test_an_unreadable_ledger_starts_at_zero_instead_of_blocking():
    store = _FakeCostLedger(RuntimeError("db down"))
    ledger = await seed_run_budget("run_i", _snapshot(), cost_ledger_store=store)
    assert ledger.usage().llm_calls == 0
    assert ledger.seeded is True


async def test_unpriced_calls_survive_the_baseline_read():
    store = _FakeCostLedger(
        {"call_count": 5, "unpriced_call_count": 2, "total_cost_usd": None}
    )
    ledger = await seed_run_budget("run_j", _snapshot(), cost_ledger_store=store)
    assert ledger.usage().cost_complete is False


def test_release_drops_the_process_local_ledger():
    ledger_for("run_k", _snapshot())
    assert peek_ledger("run_k") is not None
    release_ledger("run_k")
    assert peek_ledger("run_k") is None
