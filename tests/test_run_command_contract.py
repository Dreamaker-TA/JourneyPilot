"""控制命令的判断规则：摘要口径、搬运与收口、协作边界的消费与去重。

**不需要 PostgreSQL**：这些是规则本身，一个假 store 就够。数据库那半（唯一约束、
`SKIP LOCKED`、结论不可改写）在 `tests/db/test_run_commands.py`。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import pytest

from travel_agent.entities.trip_run import (
    RunCommand,
    RunCommandStatus,
    RunCommandType,
    TripRunStatus,
    generate_run_command_id,
    run_command_digest,
)
from travel_agent.entities.state import TravelAgentState
from travel_agent.services.run_commands import (
    PAST_SUPPLEMENT_STAGE,
    RUN_ENDED_BEFORE_CONSUMPTION,
    RunCommandCoordinator,
)
from travel_agent.workflows.run_control import (
    RunCancelled,
    run_control_registry,
    with_run_control,
)


class FakeCommandStore:
    """内存版命令表。语义与 SQL 那份一致：只有未收口的命令能被取走或改写。"""

    def __init__(self) -> None:
        self.commands: Dict[str, RunCommand] = {}

    async def enqueue(
        self,
        run_id: str,
        command_type: str | RunCommandType,
        payload: Optional[Dict[str, Any]] = None,
    ) -> tuple[RunCommand, bool]:
        body = dict(payload or {})
        digest = run_command_digest(command_type, body)
        for command in self.commands.values():
            if command.run_id == run_id and command.request_digest == digest:
                return command, False
        command = RunCommand(
            command_id=generate_run_command_id(),
            run_id=run_id,
            command_type=command_type,
            payload=body,
            request_digest=digest,
        )
        self.commands[command.command_id] = command
        return command, True

    async def claim_pending(self, run_id: str, *, limit: int = 20) -> List[RunCommand]:
        claimed = []
        for command in list(self.commands.values()):
            if command.run_id != run_id or command.status is not RunCommandStatus.PENDING:
                continue
            command.status = RunCommandStatus.CLAIMED
            claimed.append(command)
        return claimed[:limit]

    async def settle(
        self,
        command_ids: Sequence[str],
        *,
        status: RunCommandStatus,
        result: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> int:
        settled = 0
        for command_id in command_ids:
            command = self.commands.get(command_id)
            if command is None or not command.is_open:
                continue
            command.status = status
            command.result = result
            command.error_code = error_code
            settled += 1
        return settled

    async def settle_open_for_run(
        self,
        run_id: str,
        *,
        status: RunCommandStatus,
        command_types=None,
        result: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> List[RunCommand]:
        kinds = (
            {RunCommandType(str(getattr(value, "value", value))) for value in command_types}
            if command_types is not None
            else set(RunCommandType)
        )
        settled = []
        for command in self.commands.values():
            if command.run_id != run_id or not command.is_open:
                continue
            if command.command_type not in kinds:
                continue
            command.status = status
            command.result = result
            command.error_code = error_code
            settled.append(command)
        return settled


class FakeTripRunStore:
    def __init__(self) -> None:
        self.events: List[tuple[str, str, Dict[str, Any]]] = []

    async def append_event_once(
        self,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
    ) -> None:
        if any(key == idempotency_key for _run, _type, key in self._keys()):
            return
        self.events.append((run_id, event_type, {**payload, "idempotency_key": idempotency_key}))

    def _keys(self):
        return [
            (run_id, event_type, payload.get("idempotency_key"))
            for run_id, event_type, payload in self.events
        ]


@pytest.fixture
def run_id() -> str:
    return "trip_commands"


@pytest.fixture
def wired(run_id):
    """一个已注册的 handle 加一个协调器，测试结束后清空进程内 registry。"""

    handle = run_control_registry.register(run_id)
    store = FakeCommandStore()
    runs = FakeTripRunStore()
    coordinator = RunCommandCoordinator(store, runs, run_id, handle, poll_seconds=0.2)
    try:
        yield handle, store, runs, coordinator
    finally:
        run_control_registry.clear()


def test_cancel_digest_ignores_payload_but_supplement_digest_does_not() -> None:
    """摘要口径就是幂等口径：取消按意图，追加要求按内容。"""

    assert run_command_digest(RunCommandType.CANCEL, {"source": "a"}) == run_command_digest(
        RunCommandType.CANCEL, {"source": "b"}
    )
    same = {"category": "food", "content": "少走路"}
    assert run_command_digest(
        RunCommandType.SUPPLEMENT, same
    ) == run_command_digest(RunCommandType.SUPPLEMENT, dict(same))
    assert run_command_digest(
        RunCommandType.SUPPLEMENT, same
    ) != run_command_digest(
        RunCommandType.SUPPLEMENT, {"category": "pace", "content": "少走路"}
    )


async def test_durable_cancel_reaches_the_boundary_without_any_notification(
    wired, run_id
) -> None:
    """进程内通知丢了也要停下来 —— 这正是命令落库的意义。"""

    handle, store, _runs, coordinator = wired
    await store.enqueue(run_id, RunCommandType.CANCEL, {"source": "control_api"})

    await coordinator.poll_once()

    assert handle.cancel_event.is_set()
    assert handle.stop_reason == "user_cancel"

    async def node(_state: TravelAgentState) -> Dict[str, Any]:  # pragma: no cover
        raise AssertionError("取消已经到达边界，节点不该被执行")

    with pytest.raises(RunCancelled):
        await with_run_control("planner", node)(
            TravelAgentState(run_id=run_id, user_message="x")
        )


async def test_supplement_lands_in_state_and_is_consumed_once(wired, run_id) -> None:
    handle, store, runs, coordinator = wired
    command, _ = await store.enqueue(
        run_id,
        RunCommandType.SUPPLEMENT,
        {"category": "food", "content": "想吃本地早餐"},
    )
    await coordinator.poll_once()

    async def node(state: TravelAgentState) -> Dict[str, Any]:
        # 节点读到的就是这条要求 —— 而它必须**同时**随更新落盘，后续节点才看得见。
        assert [item["content"] for item in state.supplemental_requirements] == [
            "想吃本地早餐"
        ]
        return {"refinement_count": 1}

    update = await with_run_control("planner", node)(
        TravelAgentState(run_id=run_id, user_message="x")
    )

    assert update["supplemental_requirements"] == [
        {"command_id": command.command_id, "category": "food", "content": "想吃本地早餐"}
    ]
    assert store.commands[command.command_id].status is RunCommandStatus.CONSUMED
    assert store.commands[command.command_id].result == {"applied_at_node": "planner"}
    assert [event_type for _run, event_type, _payload in runs.events] == [
        "run.supplement_applied"
    ]
    # 已经生效的要求不会在下一个节点边界再进一次 state。
    assert handle.pending_supplements() == []


async def test_a_failed_node_keeps_the_supplement_for_the_next_boundary(
    wired, run_id
) -> None:
    """节点抛异常时它的更新被丢掉，那条要求就没有生效，不许标成已消费。"""

    handle, store, _runs, coordinator = wired
    command, _ = await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "pace", "content": "慢一点"}
    )
    await coordinator.poll_once()

    async def failing(_state: TravelAgentState) -> Dict[str, Any]:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        await with_run_control("planner", failing)(
            TravelAgentState(run_id=run_id, user_message="x")
        )

    assert store.commands[command.command_id].status is RunCommandStatus.CLAIMED
    assert [item["command_id"] for item in handle.pending_supplements()] == [
        command.command_id
    ]

    update = await with_run_control("dispatcher", lambda state: {})(
        TravelAgentState(run_id=run_id, user_message="x")
    )
    assert update["supplemental_requirements"][0]["content"] == "慢一点"
    assert store.commands[command.command_id].status is RunCommandStatus.CONSUMED


async def test_a_replayed_supplement_does_not_enter_state_twice(wired, run_id) -> None:
    """命令至少消费一次：重复投递同一条要求不该让它在提示里出现两遍。"""

    handle, store, _runs, coordinator = wired
    command, _ = await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "other", "content": "带孩子"}
    )
    await coordinator.poll_once()
    state = TravelAgentState(run_id=run_id, user_message="x")
    first = await with_run_control("planner", lambda s: {})(state)

    # 标记 consumed 之后崩溃、重启再投递一次：state 里已经有这个 command_id。
    handle.add_supplement("other", "带孩子", command_id=command.command_id)
    replayed_state = state.model_copy(
        update={"supplemental_requirements": first["supplemental_requirements"]}
    )
    second = await with_run_control("dispatcher", lambda s: {})(replayed_state)

    assert "supplemental_requirements" not in second


async def test_supplement_after_delivery_is_rejected_not_left_pending(
    wired, run_id
) -> None:
    handle, store, _runs, coordinator = wired
    handle.mark_delivery_ready()
    command, _ = await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "food", "content": "换一家"}
    )

    await coordinator.poll_once()

    settled = store.commands[command.command_id]
    assert settled.status is RunCommandStatus.REJECTED
    assert settled.error_code == PAST_SUPPLEMENT_STAGE
    assert handle.pending_supplements() == []


async def test_cancelled_run_consumes_its_cancel_and_rejects_the_rest(
    wired, run_id
) -> None:
    _handle, store, _runs, coordinator = wired
    cancel, _ = await store.enqueue(run_id, RunCommandType.CANCEL, {})
    supplement, _ = await store.enqueue(
        run_id, RunCommandType.SUPPLEMENT, {"category": "food", "content": "太晚了"}
    )

    await coordinator.settle_terminal(TripRunStatus.CANCELLED)

    assert store.commands[cancel.command_id].status is RunCommandStatus.CONSUMED
    assert store.commands[supplement.command_id].status is RunCommandStatus.REJECTED
    assert store.commands[supplement.command_id].error_code == RUN_ENDED_BEFORE_CONSUMPTION


async def test_a_completed_run_does_not_pretend_the_cancel_was_carried_out(
    wired, run_id
) -> None:
    """交付先提交、取消没赶上：那条取消**没有**被执行，回执要这么说。"""

    _handle, store, _runs, coordinator = wired
    cancel, _ = await store.enqueue(run_id, RunCommandType.CANCEL, {})

    await coordinator.settle_terminal(TripRunStatus.COMPLETED)

    settled = store.commands[cancel.command_id]
    assert settled.status is RunCommandStatus.REJECTED
    assert settled.error_code == RUN_ENDED_BEFORE_CONSUMPTION
    assert settled.result == {"run_status": TripRunStatus.COMPLETED.value}


def test_notification_is_only_a_wake_up(run_id) -> None:
    """registry 里没有 handle 不是拒绝：命令已经落库，通知只是少等一轮轮询。"""

    assert run_control_registry.notify(run_id) is False
    handle = run_control_registry.register(run_id)
    try:
        assert run_control_registry.notify(run_id) is True
        assert handle.wake_event.is_set()
    finally:
        run_control_registry.clear()
