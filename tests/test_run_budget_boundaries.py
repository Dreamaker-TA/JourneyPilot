"""预算耗尽在两条调用边界上的收口方式，以及封存不变量。"""

from __future__ import annotations

import pytest

from travel_agent.agents.utils import execute_tool
from travel_agent.entities.run_budget import RunBudgetSnapshot
from travel_agent.workflows.run_budget import (
    RunBudgetExhausted,
    ledger_for,
    reset_ledgers,
)
from travel_agent.workflows.run_control import current_run_budget, current_run_id


@pytest.fixture(autouse=True)
def _isolate_ledgers():
    reset_ledgers()
    yield
    reset_ledgers()


def _snapshot(**overrides) -> RunBudgetSnapshot:
    payload = dict(
        max_llm_calls=10,
        max_tool_calls=1,
        max_input_tokens=10_000,
        max_output_tokens=1_000,
        max_cost_usd=1.0,
        max_tool_retries_per_target=0,
    )
    payload.update(overrides)
    return RunBudgetSnapshot(**payload)


async def test_tool_call_over_budget_returns_a_failed_envelope_not_an_exception():
    """把「配额用完了」抛成异常会变成一次 Run 失败。

    它其实是一次可解释的降级：Worker 拿到一个普通的 failed envelope，把这条内容缺口
    交给 Candidate Gate，交付照常收口 —— 与研究窗口关闭那一条走同一条路。
    """

    snapshot = _snapshot(max_tool_calls=1)
    token_run = current_run_id.set("run_tool_budget")
    token_budget = current_run_budget.set(snapshot)
    try:
        ledger = ledger_for("run_tool_budget", snapshot)
        ledger.record_tool_call("web_search")
        envelope = await execute_tool("web_search", {"query": "京都"})
    finally:
        current_run_budget.reset(token_budget)
        current_run_id.reset(token_run)

    assert envelope["status"] == "failed"
    assert envelope["error"] == "run_budget_exhausted.tool_calls"
    assert envelope["metadata"]["run_budget_exhausted"] == "tool_calls"


async def test_a_tool_that_used_up_its_retries_is_not_called_again():
    snapshot = _snapshot(max_tool_calls=50, max_tool_retries_per_target=1)
    token_run = current_run_id.set("run_tool_retries")
    token_budget = current_run_budget.set(snapshot)
    try:
        ledger = ledger_for("run_tool_retries", snapshot)
        ledger.record_tool_retry("web_search")
        envelope = await execute_tool("web_search", {"query": "京都"})
    finally:
        current_run_budget.reset(token_budget)
        current_run_id.reset(token_run)

    assert envelope["status"] == "failed"
    assert envelope["metadata"]["tool_retries_exhausted"] == "web_search"


def _draft(*, authorized_at=None):
    from datetime import date

    from travel_agent.entities.delivery_bundle import (
        MinimumDeliveryDayShell,
        MinimumDeliveryDraft,
    )

    return MinimumDeliveryDraft(
        draft_id="draft_seal_budget",
        content_hash="0" * 64,
        run_id="run_seal_budget",
        planning_generation_id="generation_budget",
        controlled_trip_identity_revision=1,
        constraint_pack_revision=0,
        intent_spec_revision=1,
        intent_spec_hash="1" * 64,
        plan_revision=0,
        policy_version="minimum_delivery.v2",
        day_shells=[
            MinimumDeliveryDayShell(
                day_id="day_1",
                day=1,
                date=date(2026, 9, 1),
                destination_id="place_kyoto",
                lodging_night=False,
            )
        ],
        planning_authorized=authorized_at is not None,
        planning_authorized_at=authorized_at,
    )


def _generation():
    from travel_agent.entities.planning_generation import PlanningGeneration

    return PlanningGeneration(
        generation_id="generation_budget",
        controlled_trip_identity_revision=1,
        intent_spec_revision=1,
        constraint_pack_revision=0,
        plan_revision=0,
        identity_hash="2" * 64,
        intent_hash="1" * 64,
        constraint_hash="3" * 64,
    )


def _intent_spec():
    from travel_agent.entities.intent_spec import IntentSpec

    return IntentSpec(
        intent_spec_id="intent_spec_budget",
        revision=1,
        generation_id="generation_budget",
        content_hash="1" * 64,
        objective_summary="预算边界测试",
    )


def test_budget_and_deadline_must_be_sealed_together():
    """一个封了 Deadline 却没有预算的 Run 说明有条路径只封了一半。"""

    from travel_agent.entities.state import TravelAgentState
    from travel_agent.workflows.minimum_delivery_draft import (
        seal_minimum_delivery_draft,
    )

    class _State:
        minimum_delivery_draft = _draft()
        run_deadline = None
        run_budget = None

    sealed = seal_minimum_delivery_draft(_State())
    with pytest.raises(ValueError, match="sealed together"):
        TravelAgentState(
            session_id="s",
            run_id="run_seal_budget",
            controlled_trip_identity_revision=1,
            intent_spec_revision=1,
            intent_spec=_intent_spec(),
            planning_generation=_generation(),
            minimum_delivery_draft=sealed["minimum_delivery_draft"],
            run_deadline=sealed["run_deadline"],
        )


def test_sealing_a_draft_produces_a_budget_alongside_the_deadline():
    from travel_agent.workflows.minimum_delivery_draft import seal_minimum_delivery_draft

    class _State:
        minimum_delivery_draft = _draft()
        run_deadline = None
        run_budget = None

    update = seal_minimum_delivery_draft(_State())
    assert update["run_deadline"] is not None
    assert update["run_budget"] is not None
    assert update["run_budget"].policy_version == "run_budget.v1"


def test_a_replay_of_a_sealed_draft_never_refills_the_budget():
    """重放已封存的一代不许换来一份新预算 —— 那等于给 Run 续一次费。"""

    from travel_agent.workflows.minimum_delivery_draft import seal_minimum_delivery_draft

    class _State:
        minimum_delivery_draft = _draft()
        run_deadline = None
        run_budget = None

    first = seal_minimum_delivery_draft(_State())

    class _Replayed:
        minimum_delivery_draft = first["minimum_delivery_draft"]
        run_deadline = first["run_deadline"]
        run_budget = first["run_budget"]

    second = seal_minimum_delivery_draft(_Replayed())
    assert second["run_budget"] is first["run_budget"]
    assert second["run_deadline"] is first["run_deadline"]


def test_clearing_a_draft_generation_clears_the_budget_too():
    from travel_agent.workflows.minimum_delivery_draft import (
        _clear_completion_generation,
    )

    cleared = _clear_completion_generation()
    assert cleared["run_deadline"] is None
    assert cleared["run_budget"] is None
    assert "run_budget" in cleared


async def test_the_retry_loop_cannot_outspend_the_tool_call_budget():
    """入口判一次「还剩一次调用」，循环里却能记满 max_retries + 1 次加一次降级。

    判和记是两个可以分别调用的函数时，漏掉判的那一半不会报错 —— 只是让 max_tool_calls
    只精确到 4 倍。这一条钉住「每次尝试都判一次」。
    """

    snapshot = _snapshot(max_tool_calls=2, max_tool_retries_per_target=5)
    token_run = current_run_id.set("run_retry_spend")
    token_budget = current_run_budget.set(snapshot)
    try:
        ledger = ledger_for("run_retry_spend", snapshot)
        ledger.reserve_tool_call("tool.web_search", "web_search")
        ledger.reserve_tool_call("tool.web_search", "web_search")
        # 预算已经用满：第三次预留必须被拒，而不是记进去再说。
        with pytest.raises(RunBudgetExhausted):
            ledger.reserve_tool_call("tool.web_search", "web_search")
    finally:
        current_run_budget.reset(token_budget)
        current_run_id.reset(token_run)

    assert ledger.usage().tool_calls == 2
