"""
Dispatcher 节点 (Domain Layer)

纯代码逻辑节点，无 LLM 调用。
职责：
  1. 检查当前步骤的 agent 组是否全部完成
  2. 推进到下一步骤或路由到 v2 Candidate / Artifact / Delivery Quality Gates
  3. 支持并行 fan-out（通过 LangGraph Send API）

所有 Worker Agent 完成后回到 dispatcher，dispatcher 决定下一步。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set, Union

from langchain_core.runnables import RunnableConfig

from ...entities.state import TravelAgentState
from ...workflows.run_deadline import observe_run_deadline
from ..utils import strip_round_suffix

logger = logging.getLogger(__name__)

_NODE_NAME = "dispatcher"

# Worker 完成状态（partial/failed 视为本轮结束，质量问题由确定性 v2 Gates 处理）
_DONE_STATUSES = {"completed", "partial", "failed"}

# route_after_dispatcher 合法 next_agent 集合。未知值不得静默落入 to_check。
_KNOWN_NEXT_AGENTS: Set[str] = {
    "to_check",
    "candidate_gate",
    "dispatch_group",
}


def _composition_step(plan: Optional[list], from_step: int) -> Optional[int]:
    """Index of the plan's itinerary composition group at or after ``from_step``."""
    if not plan:
        return None
    for index in range(max(0, from_step), len(plan)):
        if any(strip_round_suffix(agent) == "itinerary_planner" for agent in plan[index]):
            return index
    return None


def _pending_agents_in_group(state: TravelAgentState, group: list[str]) -> list[str]:
    """Return plan-group agent keys that are not yet in a terminal worker status."""
    return [
        agent
        for agent in group
        if state.agent_status.get(agent) not in _DONE_STATUSES
    ]


async def dispatcher_node(
    state: TravelAgentState,
    config: Optional[RunnableConfig] = None,
) -> Dict[str, Any]:
    """
    Dispatcher 节点：推进执行计划，检查 worker 完成状态。

    路由逻辑：
    - 当前步骤未全部完成 → 继续分发（返回 dispatch_group 供 route_after_dispatcher 处理）
    - 当前步骤全部完成且有下一步 → 推进到下一步
    - 所有步骤完成 → 路由到 typed artifact / delivery quality 检查
    """
    plan = state.execution_plan
    step = state.current_plan_step

    # The Dispatcher is the only fan-out boundary.  It therefore owns the
    # hard promise that no new research Worker starts after minute six.
    # Candidate Gate still owns scoped content classification.
    deadline = state.run_deadline
    if deadline is not None:
        observed_deadline, observation = observe_run_deadline(deadline)
        deadline_update: Dict[str, Any] = {"run_deadline": observed_deadline}
        if observation.research_closed:
            composition_step = _composition_step(plan, step)
            owes_composition = (
                not observation.composition_closed
                and state.trip_workspace_v2 is None
                and composition_step is not None
            )
            if owes_composition:
                # Research is over but the itinerary is what the run exists to
                # deliver, and its own window is still open.  Skip the remaining
                # research groups and advance straight to composition rather
                # than reaching projection with nothing to project.
                logger.info(
                    "Dispatcher: 研究窗口关闭，跳至行程组合 step=%s", composition_step
                )
                return {
                    **deadline_update,
                    "current_plan_step": composition_step,
                    "next_agent": (
                        "candidate_gate"
                        if state.candidate_gate_status != "passed"
                        else "dispatch_group"
                    ),
                }
            # The wall clock closes the research surface, not the delivery.
            # Whatever the run established goes straight to the typed artifact
            # and quality gates, then to projection.
            return {
                **deadline_update,
                "next_agent": "to_check",
            }

    if not plan or step >= len(plan):
        logger.info("Dispatcher: 计划已完成，路由到校验闭环")
        return {"next_agent": "to_check"}

    current_group = plan[step]

    # Itinerary Planner may only run after the typed candidate/weather gate has
    # accepted the current Research Packet cohort. Gap repair returns here and
    # is re-evaluated before any itinerary composition can start.
    if any(strip_round_suffix(agent) == "itinerary_planner" for agent in current_group):
        if state.candidate_gate_status != "passed":
            return {"next_agent": "candidate_gate"}

    # 检查当前组是否全部完成（支持带 round suffix 的 agent 名）
    group_complete = all(
        state.agent_status.get(agent) in _DONE_STATUSES
        for agent in current_group
    )

    if not group_complete:
        # 当前组还有 agent 未完成，分发当前组（route 层只 Send pending sibling）
        logger.info(f"Dispatcher: 分发当前组 step={step}, group={current_group}")
        return {"next_agent": "dispatch_group"}

    # 当前组完成，推进到下一步
    next_step = step + 1
    if next_step < len(plan):
        next_group = plan[next_step]
        logger.info(f"Dispatcher: 推进到 step={next_step}, group={next_group}")
        return {
            "current_plan_step": next_step,
            "next_agent": (
                "candidate_gate"
                if any(strip_round_suffix(agent) == "itinerary_planner" for agent in next_group)
                else "dispatch_group"
            ),
        }

    # 所有研究步骤都必须经过同一个 Candidate Gate。完整行程计划会在
    # itinerary_planner 前触发；窄计划没有 itinerary_planner，因此在最后一个
    # Worker 完成后触发。否则 destination-only 等合法计划会绕过 scoped research，
    # 直接把空 Recommendation Catalog 交给 artifact gate。
    if state.candidate_gate_status != "passed":
        logger.info("Dispatcher: 研究步骤完成，路由到 Candidate Gate")
        return {"next_agent": "candidate_gate"}

    logger.info("Dispatcher: Candidate Gate 已通过，路由到校验闭环")
    return {"next_agent": "to_check"}


def route_after_dispatcher(state: TravelAgentState) -> Union[str, list]:
    """
    LangGraph 条件边：根据 dispatcher 决策返回下一节点。
    支持并行 fan-out（返回 Send 列表）。

    Worker 节点映射（round-suffixed → base node name）：
      destination_researcher_r2 → destination_researcher
      transport_researcher_r2   → transport_researcher
      itinerary_planner         → itinerary_planner（无后缀）

    Unknown ``next_agent`` values converge on the typed artifact gate rather
    than opening a new model path. Parallel groups only re-Send agents that are
    not yet in a terminal status.
    """
    next_agent = state.next_agent or "to_check"

    if next_agent not in _KNOWN_NEXT_AGENTS:
        logger.error(
            "Dispatcher: unknown next_agent=%r; converging on the artifact gate",
            next_agent,
        )
        return "to_check"

    if next_agent == "to_check":
        return "to_check"

    if next_agent == "candidate_gate":
        return "candidate_gate"

    if next_agent == "dispatch_group":
        plan = state.execution_plan
        step = state.current_plan_step

        if plan and step < len(plan):
            current_group = plan[step]
            pending = _pending_agents_in_group(state, current_group)
            if not pending:
                # All siblings already terminal; caller should have advanced.
                # Fail closed rather than re-fanning completed workers.
                logger.warning(
                    "Dispatcher: dispatch_group with no pending agents step=%s group=%s",
                    step,
                    current_group,
                )
                return "to_check"

            # 将带 round suffix 的 agent 名映射到实际节点名（base name）
            resolved_group = [strip_round_suffix(a) for a in pending]

            if len(resolved_group) > 1:
                from langgraph.types import Send
                return [Send(node, state) for node in resolved_group]
            return resolved_group[0]

        logger.error(
            "Dispatcher: dispatch_group without a valid plan step; "
            "converging on the artifact gate"
        )
        return "to_check"

    logger.error(
        "Dispatcher: unhandled next_agent=%r; converging on the artifact gate",
        next_agent,
    )
    return "to_check"
