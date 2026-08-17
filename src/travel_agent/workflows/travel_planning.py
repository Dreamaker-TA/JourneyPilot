"""
深度调研工作流 (Application Layer) — 仅负责图结构。

v2 三段主链：
  1. scope_clarifier → brief_generator → constraint_normalizer → destination_geo_resolver → weather_context_builder → planner
  2. dispatcher → typed workers → Candidate / Artifact / Delivery Quality Gates
  3. deterministic delivery projector → atomic delivery finalizer → END

Worker 半串行策略（Planner 生成）：
  Step1: destination_researcher
  Step2: transport_researcher + accommodation_researcher (并行)
  Step3: itinerary_planner

天气在 Planner 前构建；正式报告、地图和来源只从通过 Gate 的 v2 facts 投影。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

import langchain
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from ..agents.accommodation_researcher.node import accommodation_researcher_node
from ..agents.destination_researcher.node import destination_researcher_node
from ..agents.itinerary_planner.node import itinerary_planner_node
from ..agents.orchestrator.dispatcher import dispatcher_node, route_after_dispatcher
from ..agents.orchestrator.candidate_gate import candidate_gate_node, route_after_candidate_gate
from ..agents.orchestrator.artifact_gate import artifact_gate_node, route_after_artifact_gate
from ..agents.orchestrator.delivery_quality_gate import (
    delivery_quality_gate_node,
    route_after_delivery_quality_gate,
)
from ..agents.orchestrator.planner import planner_node
from ..entities.trip_run import generate_trip_run_id
from ..infrastructure.cost_ledger_store import get_cost_ledger_store
from ..local_profile import LOCAL_USER_ID
from ..infrastructure.delivery_bundle_store import DeliveryBundleStore
from ..infrastructure.trip_run_store import TripRunStore, get_trip_run_store
from ..infrastructure.tool_audit_store import get_tool_audit_store
from ..models.usage import get_usage_recorder
from ..agents.scope.node import (
    brief_generator_node,
    clarifier_node,
)
from ..agents.scope.constraint_normalizer import constraint_normalizer_node
from ..agents.summary_card.node import trip_summary_card_after_brief_node
from ..agents.transport_researcher.node import transport_researcher_node
from ..config import get_settings
from ..entities.state import TravelAgentState
from ..utils.display_names import get_agent_display_name
from ..utils.message_helpers import build_messages
from .budget_estimate import budget_estimate_node
from .delivery_projection import delivery_projection_node
from .delivery_finalizer import delivery_finalizer_node
from .minimum_delivery_draft import (
    minimum_delivery_draft_builder_node,
    seal_minimum_delivery_draft,
)
from .weather_context import destination_geo_resolver_node, weather_context_builder_node
from .run_control import RunCancelled, run_attribution, with_run_control

logger = logging.getLogger(__name__)


class CheckpointContractError(RuntimeError):
    """A persisted graph state no longer satisfies the current v2 contract."""


def _is_checkpoint_contract_failure(exc: BaseException) -> bool:
    if isinstance(exc, (ValidationError, AttributeError)):
        return True
    nested = getattr(exc, "exceptions", None)
    return bool(nested) and all(_is_checkpoint_contract_failure(item) for item in nested)

# LangChain 1.x 运行时在部分环境里不再暴露 `langchain.debug`，
# 但 langgraph/langchain_core 仍会读取它。这里补一个兼容值，避免
# 工作流执行被依赖版本差异打断。
if not hasattr(langchain, "debug"):
    langchain.debug = False


# ── 节点名称常量 ──────────────────────────────────────────────────────────────
NODE_CLARIFIER = "scope_clarifier"
NODE_BRIEF_GEN = "scope_brief_generator"
NODE_CONSTRAINT_NORMALIZER = "constraint_normalizer"
NODE_MINIMUM_DELIVERY_DRAFT = "minimum_delivery_draft_builder"
NODE_DESTINATION_GEO_RESOLVER = "destination_geo_resolver"
NODE_WEATHER_CONTEXT_BUILDER = "weather_context_builder"
NODE_SUMMARY_CARD_BRIEF = "trip_summary_card_brief"
NODE_PLANNER = "planner"
NODE_PLAN_GATE = "plan_gate"
NODE_DISPATCHER = "dispatcher"
NODE_CANDIDATE_GATE = "candidate_gate"
NODE_DESTINATION = "destination_researcher"
NODE_TRANSPORT = "transport_researcher"
NODE_ACCOMMODATION = "accommodation_researcher"
NODE_ITINERARY = "itinerary_planner"
NODE_ARTIFACT_GATE = "artifact_gate"
NODE_DELIVERY_QUALITY_GATE = "delivery_quality_gate"
NODE_BUDGET_ESTIMATE = "budget_estimate"
NODE_DELIVERY_PROJECTOR = "delivery_projector"
NODE_DELIVERY_FINALIZER = "delivery_finalizer"

# Worker 节点列表（dispatcher fan-out 的目标节点）
WORKER_NODES = [
    NODE_DESTINATION,
    NODE_TRANSPORT,
    NODE_ACCOMMODATION,
    NODE_ITINERARY,
]

# 计划批准门：唯一一次修改（编辑全文 / 补充要求），用满后二轮只接受 approve/cancel。
_MAX_PLAN_GATE_REVISIONS = 1

# 图上每个环都有自己的预算，正常一轮深度规划是几十个 superstep 量级。250 给足余量，
# 同时让任何失控的自转环在秒级失败，而不是耗到墙钟截止。
GRAPH_RECURSION_LIMIT = 250


def _configurable_value(config: Optional[RunnableConfig], key: str) -> Any:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    return configurable.get(key)


def _is_plan_gate_enabled(config: Optional[RunnableConfig]) -> bool:
    request_value = _configurable_value(config, "plan_gate_enabled")
    if request_value is not None:
        return bool(request_value)
    return bool(get_settings().run_control.plan_gate_enabled)


def _assignment_task_text(value: Any) -> str:
    """规范化单个 agent 的任务全文（不截断）。"""
    if isinstance(value, dict):
        text = str(value.get("task") or value.get("summary") or "")
    else:
        text = str(value or "")
    return " ".join(text.split())


def _serialize_plan_text(state: TravelAgentState) -> str:
    """把结构化执行计划序列化为规范化的 markdown 全文（后端单一数据源）。

    按步骤编号 + agent 中文显示名 + 任务全文渲染，供计划卡展示、编辑态预填与
    supplement 的 base_plan_text 混合原文使用。
    """
    assignments = state.agent_assignments or {}
    lines: List[str] = []
    for index, group in enumerate(state.execution_plan or [], start=1):
        agents = [str(agent) for agent in (group if isinstance(group, list) else [group])]
        agent_labels = "、".join(get_agent_display_name(agent) for agent in agents)
        lines.append(f"### 步骤 {index}：{agent_labels}")
        for agent in agents:
            task = _assignment_task_text(assignments.get(agent)) or "（待补充任务描述）"
            lines.append(f"- **{get_agent_display_name(agent)}**：{task}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_plan_gate_payload(state: TravelAgentState) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    assignments = state.agent_assignments or {}
    for index, group in enumerate(state.execution_plan or [], start=1):
        agents = [str(agent) for agent in (group if isinstance(group, list) else [group])]
        steps.append(
            {
                "step": index,
                "agents": agents,
                # 完整下发：任务全文，不截断。
                "tasks": {agent: _assignment_task_text(assignments.get(agent)) for agent in agents},
            }
        )

    revision = int(state.plan_gate_revision_count or 0)
    limit_reached = revision >= _MAX_PLAN_GATE_REVISIONS
    constraint_pack = state.constraint_pack if isinstance(state.constraint_pack, dict) else {}
    must_obey = [
        {
            "constraint_id": str(item.get("constraint_id") or ""),
            "public_summary": str(item.get("public_summary") or ""),
        }
        for item in constraint_pack.get("hard_constraints") or []
        if isinstance(item, dict) and item.get("status") in (None, "active")
    ]
    return {
        # `gate` is this payload's only name for itself.  It used to also
        # carry `"type": "plan_gate"` — a second name with no reader on either
        # side: the durable envelope written in `api/routes/chat.py` supplies its
        # own `type` (`"approval_gate"`), and the client normalizer reads
        # `payload.gate`.  Three names for one thing is how they drift apart.
        "gate": "plan",
        "revision": revision,
        "revision_limit": _MAX_PLAN_GATE_REVISIONS,
        "revision_limit_reached": limit_reached,
        "plan": {
            "steps": steps,
        },
        # 首轮：批准 / 编辑 / 补充 / 取消；用满修改后：仅批准 / 取消。
        "plan_text": _serialize_plan_text(state),
        "must_obey": must_obey,
        "decision_options": ["approve", "cancel"] if limit_reached else ["approve", "edit", "supplement", "cancel"],
    }


async def plan_gate_node(state: TravelAgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """Block after planning and before worker fan-out until the user decides.

    - approve → dispatcher（放行）。
    - cancel → RunCancelled（复用取消收束链；任何轮次均接受）。
    - edit / supplement → 写 plan_revision 载体 + 计数 +1，路由回 planner 重规划；
      仅第一轮（未用满修改额度）接受，且必须带非空 content。
    - 用满修改额度后重规划的第二次门只接受 approve / cancel；收到 edit / supplement
      视为协议违规，校验后重新 interrupt（不做静默兜底）。
    """
    constraint_pack = state.constraint_pack if isinstance(state.constraint_pack, dict) else {}
    pack_meta = constraint_pack.get("pack_meta") if isinstance(constraint_pack.get("pack_meta"), dict) else {}
    if pack_meta.get("hard_constraint_contract_complete") is False:
        unsupported = pack_meta.get("unsupported_hard_constraint_ids") or []
        raise RuntimeError(
            "hard constraint contract is incomplete: " + ", ".join(str(item) for item in unsupported)
        )

    if not _is_plan_gate_enabled(config):
        # A disabled interaction cannot be treated as a user authorization.
        # Legacy non-controlled flows have no Draft and retain their existing
        # behavior; controlled completion-guarantee runs must not dispatch
        # without a real approval/seal.
        if state.minimum_delivery_draft is not None:
            raise RuntimeError("controlled planning requires an enabled approval gate")
        return {"plan_gate_decision": {"action": "skipped", "source": "config"}}

    while True:
        decision = interrupt(_build_plan_gate_payload(state))
        if not isinstance(decision, dict):
            logger.info("plan_gate: non-object decision rejected; waiting for explicit approve/cancel")
            continue

        action = str(decision.get("action") or "").lower()
        revision_count = int(state.plan_gate_revision_count or 0)
        limit_reached = revision_count >= _MAX_PLAN_GATE_REVISIONS

        if action == "cancel":
            raise RunCancelled(state.run_id, NODE_PLAN_GATE)

        if action not in {"approve", "edit", "supplement"}:
            logger.info("plan_gate: unsupported action %s rejected", action)
            continue

        if action in ("edit", "supplement"):
            # 协议违规：用满修改额度后仍提交修改 → 重新 interrupt（不静默放行/降级）。
            if limit_reached:
                logger.info("plan_gate: 修改额度已用满，忽略 %s，重新等待 approve/cancel", action)
                continue
            content = " ".join(str(decision.get("content") or "").split())[:2000]
            # edit/supplement 必须带非空 content（端点已校验，这里是最后防线）。
            if not content:
                logger.info("plan_gate: %s 缺少 content，重新 interrupt", action)
                continue
            revision = {"mode": action, "content": content}
            if action == "supplement":
                revision["base_plan_text"] = _serialize_plan_text(state)
            return {
                "plan_revision": revision,
                "plan_gate_decision": {
                    "action": action,
                    "revision": revision_count + 1,
                },
                "plan_gate_revision_count": revision_count + 1,
                "minimum_delivery_draft": None,
                "run_deadline": None,
                "gate_failure_attributions": {},
                "candidate_research_gaps": [],
                "candidate_gate_attempts": {},
                "candidate_gate_failure_signatures": {},
                "composition_repair_attempts": 0,
                "terminal_attribution": None,
            }

        # approve only. Unknown actions are rejected above and re-interrupt
        # (fail-closed; not treated as approve).
        # 前端在确认时把用户累积的「追加信息 / 补充想法」一并放进 content，此处并入
        # supplemental_requirements（append-only reducer），供 worker 系统提示消费——
        # 不再走独立提交 / 重规划（DESIGN §8）。
        approve_update: Dict[str, Any] = {
            "plan_gate_decision": {
                "action": "approve",
                "revision": revision_count,
            }
        }
        approve_content = str(decision.get("content") or "").strip()[:2000]
        if approve_content:
            approve_update["supplemental_requirements"] = [
                {"category": "user_supplement", "content": approve_content}
            ]
        if state.minimum_delivery_draft is not None:
            approve_update.update(seal_minimum_delivery_draft(state))
        return approve_update


def route_after_plan_gate(state: TravelAgentState) -> str:
    decision = state.plan_gate_decision or {}
    if decision.get("action") in ("edit", "supplement"):
        return NODE_CONSTRAINT_NORMALIZER
    return NODE_DISPATCHER


def route_after_destination(state: TravelAgentState) -> str:
    """destination_researcher 节点后的路由：触发 ask_user 则中断，否则回 dispatcher。"""
    if state.next_agent == "HALT":
        return "HALT"
    return "dispatcher"


def build_travel_workflow() -> StateGraph:
    """
    构建深度调研工作流图（三层架构）。

    v2 主链：Scope/Constraint/Weather → Research Packets → Candidate Gate →
    typed Itinerary Workspace → deterministic Delivery Quality Gate →
    deterministic report/map/source projections → atomic Bundle finalizer → END。
    图上只有 delivery_finalizer 一个出口（destination_researcher 的 ask_user HALT 仍在，
    但那是 Worker 侧的中断，不是 Scope 澄清）；缺口只回到对应 Worker 或 Itinerary
    Planner，最终都汇入投影。
    """
    graph = StateGraph(TravelAgentState)

    # ── 注册所有节点 ──────────────────────────────────────────────────────
    graph.add_node(NODE_CLARIFIER, with_run_control(NODE_CLARIFIER, clarifier_node))
    graph.add_node(NODE_BRIEF_GEN, with_run_control(NODE_BRIEF_GEN, brief_generator_node))
    graph.add_node(
        NODE_CONSTRAINT_NORMALIZER,
        with_run_control(NODE_CONSTRAINT_NORMALIZER, constraint_normalizer_node),
    )
    graph.add_node(
        NODE_MINIMUM_DELIVERY_DRAFT,
        with_run_control(NODE_MINIMUM_DELIVERY_DRAFT, minimum_delivery_draft_builder_node),
    )
    graph.add_node(
        NODE_DESTINATION_GEO_RESOLVER,
        with_run_control(NODE_DESTINATION_GEO_RESOLVER, destination_geo_resolver_node),
    )
    graph.add_node(
        NODE_WEATHER_CONTEXT_BUILDER,
        with_run_control(NODE_WEATHER_CONTEXT_BUILDER, weather_context_builder_node),
    )
    graph.add_node(
        NODE_SUMMARY_CARD_BRIEF,
        with_run_control(NODE_SUMMARY_CARD_BRIEF, trip_summary_card_after_brief_node),
    )
    graph.add_node(NODE_PLANNER, with_run_control(NODE_PLANNER, planner_node))
    graph.add_node(NODE_PLAN_GATE, with_run_control(NODE_PLAN_GATE, plan_gate_node))
    # 所有节点统一 with_run_control（cancel + 运行归因）。
    graph.add_node(NODE_DISPATCHER, with_run_control(NODE_DISPATCHER, dispatcher_node))
    graph.add_node(
        NODE_CANDIDATE_GATE,
        with_run_control(NODE_CANDIDATE_GATE, candidate_gate_node),
    )
    graph.add_node(NODE_ARTIFACT_GATE, with_run_control(NODE_ARTIFACT_GATE, artifact_gate_node))
    graph.add_node(
        NODE_DELIVERY_QUALITY_GATE,
        with_run_control(NODE_DELIVERY_QUALITY_GATE, delivery_quality_gate_node),
    )
    graph.add_node(NODE_DESTINATION, with_run_control(NODE_DESTINATION, destination_researcher_node))
    graph.add_node(NODE_TRANSPORT, with_run_control(NODE_TRANSPORT, transport_researcher_node))
    graph.add_node(NODE_ACCOMMODATION, with_run_control(NODE_ACCOMMODATION, accommodation_researcher_node))
    graph.add_node(NODE_ITINERARY, with_run_control(NODE_ITINERARY, itinerary_planner_node))
    graph.add_node(
        NODE_BUDGET_ESTIMATE,
        with_run_control(NODE_BUDGET_ESTIMATE, budget_estimate_node),
    )
    graph.add_node(
        NODE_DELIVERY_PROJECTOR,
        with_run_control(NODE_DELIVERY_PROJECTOR, delivery_projection_node),
    )
    graph.add_node(
        NODE_DELIVERY_FINALIZER,
        with_run_control(NODE_DELIVERY_FINALIZER, delivery_finalizer_node),
    )

    # ── 固定边 ───────────────────────────────────────────────────────────
    graph.add_edge(START, NODE_CLARIFIER)
    graph.add_edge(NODE_BRIEF_GEN, NODE_CONSTRAINT_NORMALIZER)
    graph.add_edge(NODE_CONSTRAINT_NORMALIZER, NODE_MINIMUM_DELIVERY_DRAFT)
    graph.add_edge(NODE_MINIMUM_DELIVERY_DRAFT, NODE_DESTINATION_GEO_RESOLVER)
    graph.add_edge(NODE_DESTINATION_GEO_RESOLVER, NODE_WEATHER_CONTEXT_BUILDER)
    graph.add_edge(NODE_WEATHER_CONTEXT_BUILDER, NODE_SUMMARY_CARD_BRIEF)
    graph.add_edge(NODE_SUMMARY_CARD_BRIEF, NODE_PLANNER)
    graph.add_edge(NODE_PLANNER, NODE_PLAN_GATE)
    # 预算估算在投影之前：它往 workspace 上写一个数，报告/公共载荷/PDF 都从投影里读。
    # 它自己不会让交付失败——查不到就不写，那一行也就不出现。
    graph.add_edge(NODE_BUDGET_ESTIMATE, NODE_DELIVERY_PROJECTOR)
    graph.add_edge(NODE_DELIVERY_PROJECTOR, NODE_DELIVERY_FINALIZER)
    graph.add_edge(NODE_DELIVERY_FINALIZER, END)

    # destination_researcher 具备 HITL 能力（ask_user 工具）：若触发用户澄清则中断到 END，
    # 否则回 dispatcher 继续；其余 Worker 固定 fan-in 到 dispatcher
    graph.add_conditional_edges(
        NODE_DESTINATION,
        route_after_destination,
        {
            "HALT": END,
            "dispatcher": NODE_DISPATCHER,
        },
    )
    for worker in (NODE_TRANSPORT, NODE_ACCOMMODATION, NODE_ITINERARY):
        graph.add_edge(worker, NODE_DISPATCHER)

    # ── scope_clarifier ───────────────────────────────────────────────────
    # 无条件边：clarifier 不再有 HALT 出口。基础旅行事实由受控身份带入，缺身份即
    # fail-closed 抛错（ScopeIdentityError），不存在「停下来问用户」这条分支。
    graph.add_edge(NODE_CLARIFIER, NODE_BRIEF_GEN)

    # ── 条件边：dispatcher ─────────────────────────────────────────────────
    # dispatcher 完成后先过 typed artifact gate，再进入 deterministic quality gate。
    graph.add_conditional_edges(
        NODE_PLAN_GATE,
        route_after_plan_gate,
        {
            NODE_CONSTRAINT_NORMALIZER: NODE_CONSTRAINT_NORMALIZER,
            NODE_DISPATCHER: NODE_DISPATCHER,
        },
    )

    graph.add_conditional_edges(
        NODE_DISPATCHER,
        route_after_dispatcher,
        {
            "to_check": NODE_ARTIFACT_GATE,
            NODE_CANDIDATE_GATE: NODE_CANDIDATE_GATE,
            NODE_DESTINATION: NODE_DESTINATION,
            NODE_TRANSPORT: NODE_TRANSPORT,
            NODE_ACCOMMODATION: NODE_ACCOMMODATION,
            NODE_ITINERARY: NODE_ITINERARY,
        },
    )

    graph.add_conditional_edges(
        NODE_CANDIDATE_GATE,
        route_after_candidate_gate,
        {
            "passed": NODE_DISPATCHER,
            NODE_DESTINATION: NODE_DESTINATION,
            NODE_TRANSPORT: NODE_TRANSPORT,
            NODE_ACCOMMODATION: NODE_ACCOMMODATION,
            NODE_ITINERARY: NODE_ITINERARY,
            "composition_repair": NODE_ITINERARY,
        },
    )

    graph.add_conditional_edges(
        NODE_ARTIFACT_GATE,
        route_after_artifact_gate,
        {
            "accepted": NODE_DELIVERY_QUALITY_GATE,
            # Typed Artifact Gate hands explicit provider/model content
            # failures to Candidate Gate, which owns its circuit/retry budget.
            NODE_CANDIDATE_GATE: NODE_CANDIDATE_GATE,
            "composition_repair": NODE_ITINERARY,
        },
    )

    graph.add_conditional_edges(
        NODE_DELIVERY_QUALITY_GATE,
        route_after_delivery_quality_gate,
        {
            "passed": NODE_BUDGET_ESTIMATE,
            "composition_repair": NODE_ITINERARY,
        },
    )

    return graph


_compiled_graph = None
_compiled_graphs_with_checkpointer: Dict[int, Any] = {}


def get_travel_graph(checkpointer: Optional[Any] = None):
    """获取编译后的深度调研工作流图（单例）"""
    global _compiled_graph
    if checkpointer is None:
        if _compiled_graph is None:
            workflow = build_travel_workflow()
            _compiled_graph = workflow.compile()
            logger.info("LangGraph 深度调研工作流编译完成（无 checkpointer）")
        return _compiled_graph

    cache_key = id(checkpointer)
    if cache_key not in _compiled_graphs_with_checkpointer:
        workflow = build_travel_workflow()
        _compiled_graphs_with_checkpointer[cache_key] = workflow.compile(
            checkpointer=checkpointer
        )
        logger.info("LangGraph 深度调研工作流编译完成（Postgres checkpointer）")
    return _compiled_graphs_with_checkpointer[cache_key]


class TravelPlanningWorkflow:
    """深度调研工作流封装类。"""

    def __init__(
        self,
        checkpointer: Optional[Any] = None,
        *,
        delivery_bundle_store: Optional[DeliveryBundleStore] = None,
        trip_run_store: Optional[TripRunStore] = None,
    ) -> None:
        self._graph = get_travel_graph(checkpointer=checkpointer)
        self._checkpointer = checkpointer
        self._delivery_bundle_store = delivery_bundle_store or DeliveryBundleStore()
        self._trip_run_store = trip_run_store or get_trip_run_store()

    async def has_checkpoint(self, run_id: str) -> bool:
        """Return whether the compiled graph has persisted state for run_id."""
        available, _checkpoint_id = await self.probe_checkpoint(run_id)
        return available

    async def probe_checkpoint(self, run_id: str) -> tuple[bool, Optional[str]]:
        """Return resumability plus the id of the checkpoint that carries it.

        The id is what a resume records as its safe boundary, so it comes from the
        same read that decided resumability — a second read could answer about a
        different checkpoint.
        """
        if self._checkpointer is None:
            return False, None
        try:
            snapshot = await self._graph.aget_state(
                {"configurable": {"thread_id": run_id}}
            )
        except Exception as exc:
            if not _is_checkpoint_contract_failure(exc):
                raise
            logger.warning(
                "Checkpoint rejected by current v2 contract run_id=%s error=%s",
                run_id,
                exc,
            )
            raise CheckpointContractError(
                "checkpoint does not satisfy the current JourneyPilot v2 contract"
            ) from exc
        if snapshot.values:
            try:
                TravelAgentState.model_validate(snapshot.values)
            except Exception as exc:
                logger.warning(
                    "Checkpoint completion contract rejected run_id=%s error=%s",
                    run_id,
                    exc,
                )
                raise CheckpointContractError(
                    "checkpoint does not satisfy the current JourneyPilot completion contract"
                ) from exc
        available = bool(
            snapshot.values or snapshot.next or snapshot.tasks or snapshot.interrupts
        )
        configurable = (snapshot.config or {}).get("configurable") or {}
        checkpoint_id = configurable.get("checkpoint_id")
        return available, str(checkpoint_id) if checkpoint_id else None

    async def run(
        self,
        user_message: str,
        session_id: str,
        user_id: str = LOCAL_USER_ID,
        selected_mcp_servers: Optional[List[str]] = None,
        current_time: str = "",
        conversation_history: Optional[list] = None,
        session_anchor: Optional[Dict[str, Any]] = None,
        session_compressed: bool = False,
        preset_context: str = "",
        preset_pack_constraints: Optional[Dict[str, str]] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行一轮完整的深度调研对话。"""
        import datetime

        messages = build_messages(conversation_history, user_message)

        initial_state = TravelAgentState(
            messages=messages,
            session_id=session_id,
            user_id=user_id,
            selected_mcp_servers=selected_mcp_servers or [],
            user_query=user_message,
            run_id=run_id or generate_trip_run_id(),
            current_time=current_time or datetime.datetime.now().strftime("%Y-%m-%d %A %H:%M:%S"),
            session_anchor=session_anchor,
            session_compressed=session_compressed,
            preset_context=preset_context,
            preset_pack_constraints=dict(preset_pack_constraints or {}),
        )

        config = {
            "recursion_limit": GRAPH_RECURSION_LIMIT,
            "configurable": {
                "thread_id": initial_state.run_id,
                "plan_gate_enabled": bool(
                    self._checkpointer is not None and get_settings().run_control.plan_gate_enabled
                ),
                "cost_ledger_store": get_cost_ledger_store(),
                "usage_recorder": get_usage_recorder(),
                "tool_audit_store": get_tool_audit_store(),
                "delivery_bundle_store": self._delivery_bundle_store,
                "trip_run_store": self._trip_run_store,
            }
        }
        with run_attribution(initial_state.run_id):
            final_state = await self._graph.ainvoke(
                initial_state, config=config, durability="sync"
            )
        return final_state

    async def astream(
        self,
        user_message: str,
        session_id: str,
        user_id: str = LOCAL_USER_ID,
        selected_mcp_servers: Optional[List[str]] = None,
        current_time: str = "",
        conversation_history: Optional[list] = None,
        stream_queue=None,
        session_anchor: Optional[Dict[str, Any]] = None,
        session_compressed: bool = False,
        preset_context: str = "",
        preset_pack_constraints: Optional[Dict[str, str]] = None,
        run_id: Optional[str] = None,
        resume_from_checkpoint: bool = False,
        resume_payload: Optional[Dict[str, Any]] = None,
        plan_gate_enabled: Optional[bool] = None,
        controlled_trip_identity: Optional[Dict[str, Any]] = None,
        route_decision: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """流式执行深度调研工作流。"""
        import datetime

        effective_run_id = run_id or generate_trip_run_id()
        graph_input: Optional[TravelAgentState | Command]
        if resume_from_checkpoint:
            graph_input = Command(resume=resume_payload) if resume_payload is not None else None
        else:
            messages = build_messages(conversation_history, user_message)
            graph_input = TravelAgentState(
                messages=messages,
                session_id=session_id,
                user_id=user_id,
                selected_mcp_servers=selected_mcp_servers or [],
                user_query=user_message,
                run_id=effective_run_id,
                    current_time=current_time or datetime.datetime.now().strftime("%Y-%m-%d %A %H:%M:%S"),
                session_anchor=session_anchor,
                session_compressed=session_compressed,
                preset_context=preset_context,
                preset_pack_constraints=dict(preset_pack_constraints or {}),
                controlled_trip_identity=controlled_trip_identity or {},
                route_decision=route_decision or {},
            )

        effective_plan_gate_enabled = (
            bool(plan_gate_enabled)
            if plan_gate_enabled is not None
            else bool(getattr(self, "_checkpointer", None) is not None and get_settings().run_control.plan_gate_enabled)
        )
        config = {
            "recursion_limit": GRAPH_RECURSION_LIMIT,
            "configurable": {
                "thread_id": effective_run_id,
                "plan_gate_enabled": effective_plan_gate_enabled,
                "cost_ledger_store": get_cost_ledger_store(),
                "usage_recorder": get_usage_recorder(),
                "tool_audit_store": get_tool_audit_store(),
                "delivery_bundle_store": self._delivery_bundle_store,
                "trip_run_store": self._trip_run_store,
            }
        }
        if stream_queue is not None:
            config["configurable"]["stream_queue"] = stream_queue
        # 双 stream_mode：updates 帧逐节点透传（chat.py 逐节点 token / 进度逻辑不变）；
        # values 帧通过内部 queue 持久化当前全量 state，同时留存末帧供 __final_state__
        # 的 Bundle manifest 与终结事件收口。
        last_values = None
        interrupted = False
        async def lifecycle_sink(payload: Dict[str, Any]) -> None:
            if stream_queue is not None:
                await stream_queue.put(("node_lifecycle", payload))

        with run_attribution(
            effective_run_id,
            lifecycle_sink=lifecycle_sink if stream_queue is not None else None,
        ):
            async for mode, chunk in self._graph.astream(
                graph_input,
                stream_mode=["updates", "values"],
                config=config,
                durability="sync",
            ):
                if mode == "updates":
                    if isinstance(chunk, dict) and "__interrupt__" in chunk:
                        interrupted = True
                    yield chunk
                else:  # "values"
                    if isinstance(chunk, dict) and "__interrupt__" in chunk:
                        filtered = dict(chunk)
                        filtered.pop("__interrupt__", None)
                        last_values = filtered
                        interrupted = True
                    else:
                        last_values = chunk
                    if stream_queue is not None and isinstance(last_values, dict):
                        await stream_queue.put(("state_snapshot", last_values))
        if last_values is not None and not interrupted:
            yield {"__final_state__": last_values}
