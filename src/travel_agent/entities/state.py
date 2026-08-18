"""
LangGraph 状态定义 (Domain Layer)
TravelAgentState 是整个工作流中唯一可变的共享状态。
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, model_validator

from ..local_profile import LOCAL_USER_ID

from .delivery_bundle import (
    FactAssertion,
    FactStoreSnapshot,
    DeliveryBundle,
    DeliveryFailureRecord,
    FieldProvenance,
    MapProjection,
    CandidateResearchGap,
    DeliveryQualityGap,
    RecommendationQualityState,
    RecommendationCatalog,
    ResearchPacket,
    SourceRecord,
    SourceIndexProjection,
    TripReportProjection,
    WeatherImpact,
    TripWorkspaceV2,
    WeatherContextSnapshot,
    GateFailureAttribution,
    MinimumDeliveryDraft,
    RunDeadlineSnapshot,
    TerminalAttribution,
)
from .run_budget import RunBudgetSnapshot
from .weather_planning import DestinationGeoPoint
from .itinerary_composition_v2 import ItineraryCompositionDraft, LocalConnectorGap
from .provider_evidence import (
    ProviderEvidenceOutcome,
    merge_provider_evidence_outcomes,
)
from .provider_reference_service import ProviderReferenceService

def _merge_dicts(a: Dict, b: Dict) -> Dict:
    """LangGraph fan-in reducer for dict fields.

    Semantics: **last-writer-wins** via ``{**a, **b}`` (right side overwrites
    same keys). Fan-in order is not a business "later completion" clock, so
    concurrent writers must **not** share keys.

    Contract for worker-owned maps (``research_packets``, ``agent_status``,
    ``artifact_status``, …): each parallel branch writes **only its agent
    key** (or a round-suffixed key owned by that branch). Same-key dual write
    is undefined and must not appear in production paths; tests document
    last-wins under both merge orders.
    """
    return {**a, **b}


def _merge_lists(a: List[Any], b: List[Any]) -> List[Any]:
    """Append-only list reducer for lightweight bridge summaries."""
    return list(a or []) + list(b or [])


def _merge_supplements(a: List[Dict[str, str]], b: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """按 ``command_id`` 去重的追加要求。

    并行 ``Send`` 扇出里每个 worker 拿到的是同一份入参 state，于是都判定这条要求还没并入，
    都把它加进自己返回的更新里。纯 append 的 reducer 会把它存下 N 份，此后每个下游提示里
    那句「本次运行追加要求」就出现 N 遍。
    """

    merged: List[Dict[str, str]] = []
    seen: set[str] = set()
    for item in list(a or []) + list(b or []):
        command_id = str(item.get("command_id") or "").strip() if isinstance(item, dict) else ""
        if command_id:
            if command_id in seen:
                continue
            seen.add(command_id)
        merged.append(item)
    return merged


def _merge_reference_services(
    a: List["ProviderReferenceService"], b: List["ProviderReferenceService"]
) -> List["ProviderReferenceService"]:
    """Keep one reference service per responsibility, latest round wins.

    A later targeted round re-answers the same leg, so appending would leave two
    sentences about one leg on the plan.  The key is the responsibility the
    reference stands in for — leg role and the date the traveller asked about —
    not the service number, which is exactly the value a second round may
    change.
    """
    merged: Dict[Any, "ProviderReferenceService"] = {}
    for item in [*(a or []), *(b or [])]:
        merged[(item.leg_role, item.requested_date)] = item
    return list(merged.values())


def _or_bool(a: bool, b: bool) -> bool:
    """布尔 OR 聚合：任一并行分支为 True 即为 True。"""
    return bool(a or b)


def _prefer_non_empty_str(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """
    字符串聚合：优先保留最新的非空字符串。
    用于并行分支同时写入错误信息时避免并发冲突。
    """
    return b or a


def _prefer_pending_choice(
    a: Optional[Dict[str, Any]],
    b: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    澄清选项聚合：
    - 仅一侧有值：返回该值
    - 双侧都有值：保留先到达的值（避免覆盖造成前端抖动）
    """
    if a and b:
        return a
    return b or a


def _prefer_latest_dict(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Prefer the latest non-empty control payload."""
    return b or a


def _take_latest_allow_clear(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Last-write-wins including an explicit ``None`` clear.

    plan_revision 只被两个单节点串行写入：plan_gate 写入修改载体（dict），planner
    消费后写回 None 清空。非 fan-out 字段，故 last-write-wins 安全，且允许 None 覆盖
    （prefer-latest 的 ``b or a`` 无法清空）。
    """
    return b


def _take_latest_value_allow_clear(_a: Any, b: Any) -> Any:
    """Whole-generation replacement reducer, including ``None`` and ``{}``.

    Completion-control values all belong to one Draft generation.  A merge
    would let an edit retain a deadline/budget from an obsolete generation, so
    these fields deliberately use single-writer replacement semantics.
    """
    return b


def _merge_run_deadline_allow_clear(
    a: Optional[RunDeadlineSnapshot],
    b: Optional[RunDeadlineSnapshot],
) -> Optional[RunDeadlineSnapshot]:
    """Merge same-generation parallel observations without rolling time back."""
    if b is None or a is None or a.draft_id != b.draft_id:
        return b
    return b.model_copy(
        update={
            "checkpointed_elapsed_seconds": max(
                a.checkpointed_elapsed_seconds,
                b.checkpointed_elapsed_seconds,
            ),
            "last_observed_at": max(a.last_observed_at, b.last_observed_at),
        }
    )


def _merge_gate_failure_attributions_allow_clear(
    a: Dict[str, GateFailureAttribution],
    b: Dict[str, GateFailureAttribution],
) -> Dict[str, GateFailureAttribution]:
    """Keep independent gap/domain attribution records without cross-over.

    Gate nodes may update different domains concurrently. Records are keyed by
    their stable attribution id, so a newer observation of the same classified
    failure replaces only that record. An explicit empty mapping clears the
    previous Draft generation during a plan edit.
    """
    if not b:
        return {}
    return {**(a or {}), **b}


# Both composition repair-context fields below hold one line of this shape, and
# both ride the same repair prompt section, so one bound governs them: enough of
# the failure to name it, never enough to displace the catalog it sits next to.
REPAIR_CONTEXT_CHAR_LIMIT = 600


def bounded_repair_context(stage: str, detail: str) -> str:
    """One bounded line stating which composition stage failed and why."""
    text = f"{stage} 阶段失败 — {detail}".strip()
    if len(text) <= REPAIR_CONTEXT_CHAR_LIMIT:
        return text
    return text[: REPAIR_CONTEXT_CHAR_LIMIT - 1] + "…"


class TaskType(str, Enum):
    """任务类型标记（由 Planner 设置）"""
    QUICK_QA = "quick_qa"
    TRAVEL_PLANNING = "travel_planning"
    DESTINATION_INFO = "destination_info"
    TRANSPORT_QUERY = "transport_query"
    ACCOMMODATION_QUERY = "accommodation_query"


class MessageItem(BaseModel):
    """单条对话消息"""
    role: str
    content: str
    message_id: str = ""
    type: str = "normal"
    step_name: str = ""
    agent_name: str = ""



class TravelAgentState(BaseModel):
    """
    全局工作流状态（LangGraph StateGraph 使用）。

    字段分组：
    - 基础会话：messages, session_id, user_id, user_query
    - 研究主键：run_id
    - Scope 阶段：research_brief（澄清后生成的结构化简报）
    - 基础设施：前置 destination_geo / weather_context
    - 编排：execution_plan, current_plan_step, agent_assignments, next_agent
    - Worker 输出：agent_status 与强类型 Research Packet / Workspace
    - 用户交互：pending_user_choice
    - 系统：current_time, selected_mcp_servers, tool_cache 等
    """

    # 消息历史
    messages: Annotated[List[BaseMessage], add_messages] = Field(default_factory=list)

    # 基础会话
    session_id: str = ""
    user_id: str = LOCAL_USER_ID
    user_query: str = ""
    run_id: str = ""
    # P1-A 当前唯一基础旅行身份。Scope/Brief/Planner 不再从 raw user_query
    # 重复猜测这些字段。
    controlled_trip_identity: Dict[str, Any] = Field(default_factory=dict)
    controlled_trip_identity_revision: int = Field(default=0, ge=0)
    route_decision: Dict[str, Any] = Field(default_factory=dict)
    # ── Scope 阶段 ──────────────────────────────────────────────────────────
    # research_brief: Scope 阶段输出的结构化研究简报（JSON 字符串）
    # 格式: {"objective": "...", "destination": "...", "duration_days": N,
    #        "budget": "...", "travel_style": "...", "constraints": [...],
    #        "dimensions_to_cover": [...]}
    research_brief: Optional[str] = None
    # trip_summary_card: 稳定边界从结构化 brief 确定性投影的消费者卡片。
    # 不调用额外 LLM，不承载实时进度或原始推理。
    trip_summary_card: Dict[str, Any] = Field(default_factory=dict)
    # Scope 后一次性生成的 PersonalConstraintPack；Planner、Worker 与 Bundle 投影
    # 共用，禁止在下游重新调用模型解释同一组约束。
    constraint_pack: Dict[str, Any] = Field(default_factory=dict)
    constraint_pack_revision: int = Field(default=0, ge=0)
    fact_data_revision: int = Field(default=0, ge=0)
    # ── Durable completion foundation ─────────────────────────────────────
    # Built before provider/model work.  These are intentionally whole-value
    # reducers so an edit/identity change can atomically replace or clear the
    # old Draft generation.
    minimum_delivery_draft: Annotated[
        Optional[MinimumDeliveryDraft], _take_latest_value_allow_clear
    ] = None
    run_deadline: Annotated[
        Optional[RunDeadlineSnapshot], _merge_run_deadline_allow_clear
    ] = None
    # 与 run_deadline 同一代：Draft 被授权时封存，被清除时一起清。整值替换 —— 上限
    # 不会在 fan-in 里「合并」出一个新的上限。
    run_budget: Annotated[
        Optional[RunBudgetSnapshot], _take_latest_value_allow_clear
    ] = None
    gate_failure_attributions: Annotated[
        Dict[str, GateFailureAttribution], _merge_gate_failure_attributions_allow_clear
    ] = Field(default_factory=dict)
    terminal_attribution: Annotated[
        Optional[TerminalAttribution], _take_latest_value_allow_clear
    ] = None

    # ── 基础设施 ────────────────────────────────────────────────────────────
    # 规划前基础事实：由 controlled trip identity 与正式 Weather Provider 构建。
    destination_geo: List[DestinationGeoPoint] = Field(default_factory=list)
    weather_context: Optional[WeatherContextSnapshot] = None
    weather_source_records: List[SourceRecord] = Field(default_factory=list)
    weather_fact_assertions: List[FactAssertion] = Field(default_factory=list)
    weather_field_provenance: List[FieldProvenance] = Field(default_factory=list)
    # ── 编排字段 ────────────────────────────────────────────────────────────
    task_type: Optional[TaskType] = None
    next_agent: Optional[str] = None
    execution_plan: List[List[str]] = Field(default_factory=list)
    current_plan_step: int = 0

    # agent_assignments: planner_node 输出的结构化任务分配
    # 格式: {"agent_name": {"task": "...", "recommended_tools": [...]}}
    agent_assignments: Dict[str, Any] = Field(default_factory=dict)

    # ── Worker 输出 ─────────────────────────────────────────────────────────
    agent_status: Annotated[Dict[str, str], _merge_dicts] = Field(default_factory=dict)
    # artifact gate separates scheduling completion from current-contract acceptance.
    artifact_status: Annotated[Dict[str, str], _merge_dicts] = Field(default_factory=dict)
    artifact_gate_route: Optional[str] = None
    # v2 Research Worker 的唯一业务产物。key 与 execution plan worker key 对齐，
    # 补研轮次使用后缀，不存在通用文本信封的兼容读取。
    research_packets: Annotated[Dict[str, ResearchPacket], _merge_dicts] = Field(
        default_factory=dict
    )
    provider_evidence_outcomes: Annotated[
        Dict[str, ProviderEvidenceOutcome],
        merge_provider_evidence_outcomes,
    ] = Field(default_factory=dict)
    recommendation_catalog: Optional[RecommendationCatalog] = None
    weather_impacts: Annotated[Dict[str, WeatherImpact], _merge_dicts] = Field(
        default_factory=dict
    )
    candidate_research_gaps: List[CandidateResearchGap] = Field(default_factory=list)
    # Real provider services observed off-date, carried for disclosure only.  They
    # hold no evidence standing anywhere: see entities/provider_reference_service.py.
    provider_reference_services: Annotated[
        List[ProviderReferenceService], _merge_reference_services
    ] = Field(default_factory=list)
    candidate_gate_status: Optional[
        Literal[
            "passed",
            "needs_research",
            "composition_repair",
        ]
    ] = None
    candidate_gate_route: Optional[str] = None
    # Whole-field replace (no reducer). Single writer: candidate_gate.
    candidate_gate_attempts: Dict[str, int] = Field(default_factory=dict)
    candidate_gate_failure_signatures: Dict[str, List[str]] = Field(default_factory=dict)
    # Single writer: gates that emit composition_repair. Cleared on plan edit.
    composition_repair_attempts: int = 0
    # Normalized authored place names whose whole resolution ladder ran and
    # found nothing. Single writer: itinerary_planner. A later composition
    # attempt rejects them outright instead of buying the same answer again.
    unlocatable_authored_places: List[str] = Field(default_factory=list)
    # Why the previous composition attempt failed, in the words of the failure
    # itself (stage, exception type, bounded message). Single writer:
    # itinerary_planner — every terminal branch writes it, failures with the
    # reason and successes with None, so a repair round reads exactly one round
    # back and no context ever accumulates across two rounds. Gates keep their
    # own gap channels; none of them writes here.
    composition_failure_context: Optional[str] = None
    # The gate's verdict on the skeleton the previous composition round produced
    # successfully: it validated, yet no connector could be extracted from it.
    # Single writer: candidate_gate — the invalid verdict carries the bounded
    # reason and an extractable skeleton clears it, so no verdict outlives the
    # skeleton it judged. The planner's own failures ride
    # ``composition_failure_context``; neither channel writes the other.
    placement_skeleton_failure_context: Optional[str] = None
    placement_skeleton: Optional[ItineraryCompositionDraft] = None
    local_connector_gaps: List[LocalConnectorGap] = Field(default_factory=list)
    trip_workspace_v2: Optional[TripWorkspaceV2] = None
    recommendation_quality: Optional[RecommendationQualityState] = None
    delivery_quality_gaps: List[DeliveryQualityGap] = Field(default_factory=list)
    delivery_quality_route: Optional[str] = None
    fact_store_snapshot: Optional[FactStoreSnapshot] = None
    report_projection: Optional[TripReportProjection] = None
    map_projection: Optional[MapProjection] = None
    source_index_projection: Optional[SourceIndexProjection] = None
    delivery_bundle: Optional[DeliveryBundle] = None
    delivery_failure: Optional[DeliveryFailureRecord] = None
    delivery_persisted: bool = False
    # ── RAG ────────────────────────────────────────────────────────────────
    retrieved_docs: List[Dict[str, Any]] = Field(default_factory=list)
    retrieval_summaries: Annotated[List[Dict[str, Any]], _merge_lists] = Field(default_factory=list)

    # ── 行程规划 ────────────────────────────────────────────────────────────
    # Fast Answer 正文与外部来源的结构化绑定。Deep 交付只读 Bundle source projection。
    final_grounding: Annotated[Dict[str, Any], _merge_dicts] = Field(default_factory=dict)

    # ── 用户交互 ────────────────────────────────────────────────────────────
    pending_user_choice: Annotated[Optional[Dict[str, Any]], _prefer_pending_choice] = None
    plan_gate_decision: Annotated[Optional[Dict[str, Any]], _prefer_latest_dict] = None
    plan_gate_revision_count: int = 0
    # 计划批准门修改载体：plan_gate_node 写入 {"mode": "edit"|"supplement",
    # "content": ..., "base_plan_text": ...}，planner 消费后清空（置 None）。prefer-latest
    # reducer 与 plan_gate_decision 一致——门是单节点写入，不参与 fan-out 合并。
    plan_revision: Annotated[Optional[Dict[str, Any]], _take_latest_allow_clear] = None
    # 运行中追加的辅助规划要求；由 run control 在下一个节点边界注入，基础旅行身份不在此字段内。
    supplemental_requirements: Annotated[List[Dict[str, str]], _merge_supplements] = Field(default_factory=list)

    # ── 用户上下文 ──────────────────────────────────────────────────────────
    # 这里**没有** ``user_profile_summary``：用户偏好与系统画像抵达模型的通道只有一条，
    # 就是 Constraint Pack（``panels/constraint.py::_map_manual_profile`` 与
    # ``_auto_portrait_block``），快慢两条路径共用。此前那个字段由路由无分支塞进两条路，
    # 而只有快路径读它、读完拆成两半再拼进 prompt —— 同一份画像因此在快路径上是散文、
    # 在深度路径上是可仲裁的 pack item，冲突时谁赢取决于走的是哪条路。
    selected_mcp_servers: List[str] = Field(default_factory=list)

    # ── Preset 上下文 ─────────────────────────────────────────────────────
    # preset_context 是进 prompt 的那段散文（指令 / 重点关注 / 输出格式）。
    # preset_pack_constraints 是同一个 preset 里**归 Constraint Pack 执行**的那几项
    # （``category -> 原文``，当前是 pace_preference 与 budget_cap）。分成两个字段是
    # 因为它们归两层负责：散文给模型读，那两项要进 pack 参与单值类仲裁、被门读到。
    # 在一个字符串里同时表达两者，正是「三个风格互不相认」的起点。
    preset_context: str = ""
    preset_pack_constraints: Dict[str, str] = Field(default_factory=dict)

    # ── 上下文压缩 (v3) ──────────────────────────────────────────────────────
    # session_anchor: 会话压缩后的 Anchor 摘要（dict 格式，对应 AnchorSummary.to_dict()）
    session_anchor: Optional[Dict[str, Any]] = None
    # session_compressed: 当前会话是否已经完成过自动压缩
    session_compressed: bool = False
    # 这里**没有** ``needs_compaction``：那是 ``BuiltContext`` 上的一个**装配期信号**，
    # 由 ``ContextBuilder`` 在同一次调用里算出、由两个节点在同一个函数体内读掉，
    # 生命周期从头到尾不跨节点。它此前在 state 上也留了一份，两条路径压缩完各往里写
    # 一次 ``False``，而全仓没有任何读取方 —— 一个写反了都没人发现的字段。
    # 「本轮压缩过没有」这件事有它自己的出口：快路径是随流下发的 ``context_report``，
    # 深度路径是下面那个 ``session_compacted_this_turn``（单写入点 / 单读取点）。
    # session_compacted_this_turn: 本轮**刚刚**压缩过（区别于 session_compressed 的
    # 「这个会话历史上压缩过」）。唯一的写入点是 scope_clarifier 压缩成功那一支，
    # 唯一的读取点是 constraint_normalizer —— 它要据此在上下文透镜里印那句
    # 「较早的对话已整理」，而透镜的报告只能由它来发（只有它手里有 pack）。
    session_compacted_this_turn: Annotated[bool, _or_bool] = False

    # ── 系统上下文 ──────────────────────────────────────────────────────────
    current_time: str = ""

    # ── 工具缓存 ────────────────────────────────────────────────────────────
    tool_cache: Annotated[Dict[str, Any], _merge_dicts] = Field(default_factory=dict)

    # ── Synthesizer 模式 ────────────────────────────────────────────────────
    synthesis_mode: Optional[str] = None  # "deep" | "fast" | None

    # ── 循环控制 ────────────────────────────────────────────────────────────
    # Legacy worker round suffix support; the v2 graph uses scoped Candidate Gate retries.
    refinement_count: int = 0
    is_completed: Annotated[bool, _or_bool] = False

    # ── 错误追踪 ────────────────────────────────────────────────────────────
    last_error: Annotated[Optional[str], _prefer_non_empty_str] = None

    @model_validator(mode="after")
    def validate_completion_generation(self) -> "TravelAgentState":
        """Reject checkpoint state that mixes Draft generations.

        This validation is intentionally at the shared-state boundary: a
        malformed or older checkpoint must not resume with a fresh eight-minute
        budget attached to an unrelated Draft.
        """
        draft = self.minimum_delivery_draft
        dependent_values = (
            self.run_deadline,
            self.run_budget,
            self.terminal_attribution,
        )
        if draft is None:
            if any(value is not None for value in dependent_values):
                raise ValueError("completion state requires a minimum delivery draft")
            return self
        draft_generation_matches = (
            draft.controlled_trip_identity_revision == self.controlled_trip_identity_revision
            and draft.constraint_pack_revision == self.constraint_pack_revision
            and draft.plan_revision == self.plan_gate_revision_count
        )
        # An unsealed seed may be visible for the single graph transition
        # between a constraint/identity revision and the deterministic Draft
        # builder that replaces it.  It cannot carry any dependent state while
        # stale.  A sealed generation is always fail-closed on a mismatch.
        if not draft_generation_matches:
            if draft.planning_authorized or any(
                value is not None for value in dependent_values
            ):
                raise ValueError("completion state belongs to an obsolete minimum delivery draft")
            return self
        if self.run_deadline is not None:
            if not draft.planning_authorized:
                raise ValueError("run deadline requires a sealed minimum delivery draft")
            if self.run_deadline.draft_id != draft.draft_id:
                raise ValueError("run deadline belongs to another minimum delivery draft")
            if self.run_deadline.planning_authorized_at != draft.planning_authorized_at:
                raise ValueError("run deadline authorization time differs from minimum delivery draft")
        # 预算与 Deadline 同进同出：一个封了预算却没有 Deadline 的 Run 说明有一条
        # 路径只封了一半，而那条路径上的另一半不受任何上限约束。
        if (self.run_budget is None) != (self.run_deadline is None):
            raise ValueError("run budget and run deadline must be sealed together")
        if self.terminal_attribution is not None and self.terminal_attribution.draft_id != draft.draft_id:
            raise ValueError("terminal attribution belongs to another minimum delivery draft")
        for attribution in self.gate_failure_attributions.values():
            if attribution.draft_id != draft.draft_id:
                raise ValueError("gate failure attribution belongs to another minimum delivery draft")
        return self

    model_config = {"arbitrary_types_allowed": True}
