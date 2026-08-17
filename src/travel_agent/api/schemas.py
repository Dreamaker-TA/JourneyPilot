"""
API 请求/响应 Schema (Serving Layer)
与领域实体解耦的 DTO 定义。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import MAX_COMPLETION_TOKENS
from ..entities.preset import (
    PRESET_DESCRIPTION_MAX_CHARS,
    PRESET_INSTRUCTIONS_MAX_CHARS,
    PRESET_NAME_MAX_CHARS,
    PresetConstraints,
)
from ..entities.trip_input import (
    ControlledTripIdentity,
    GuidedIntakeState,
    PlaceIdentity,
    RouteDecision,
    RouteName,
)
from ..entities.workspace_v2_mutations import WorkspaceV2Mutation


# ---------------------------------------------------------------------------
# 聊天相关
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: str
    message_id: Optional[str] = None
    type: str = "normal"


class GateDecision(BaseModel):
    # plan_gate：approve / edit / supplement / cancel。
    # content 是唯一的自由文本载体（编辑全文 / 补充要求）。
    action: Literal["approve", "edit", "supplement", "cancel"]
    content: str = ""

    @model_validator(mode="after")
    def _require_content_for_plan_revision(self) -> "GateDecision":
        # 计划批准门的编辑 / 补充必须携带非空 content（协议前移校验，不做静默兜底）。
        if self.action in ("edit", "supplement") and not self.content.strip():
            raise ValueError(f"gate_decision action={self.action!r} 需要非空 content")
        return self


class ChatRequest(BaseModel):
    # 拒收未知字段，而不是静默丢弃。Pydantic v2 的默认 `extra="ignore"` 会让一个
    # 已从服务端删掉的字段仍然被发上来并接受（返回 200，但字段没生效）。
    # 一个被删掉的旋钮如果发上来还是 200，调用方没有任何办法知道它没生效：
    # 「静默接受一个无效字段」和「悄悄支持它」在客户端看来是同一件事。
    model_config = ConfigDict(extra="forbid")

    messages: List[ChatMessage]
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    route: Optional[RouteName] = None
    route_decision: Optional[RouteDecision] = None
    controlled_trip_identity: Optional[ControlledTripIdentity] = None
    guided_intake: Optional[GuidedIntakeState] = None
    selected_mcp_servers: List[str] = Field(default_factory=list)
    plan_gate: Optional[bool] = None
    gate_decision: Optional[GateDecision] = None
    preset_id: Optional[str] = None
    # developer_mode removed (S9 / inspect-surface): single scrubbed product+inspect
    # SSE stream for all clients; UI progressive disclosure replaces dual audience.


# 枚举值须与 memory.chat_session 的 STATUS_* 常量保持一致 (两处同步修改)。
class SessionStatus(str, Enum):
    ACTIVE = "active"
    # 只读历史值：clarify 产品面已删除，没有任何代码再写入这个状态。它留在枚举里是因为
    # chat_sessions 表里仍有历史行带着它——删掉成员会让那些会话在读取时校验失败，比现状
    # （一个永远停在此状态的死会话）更糟。清库不覆盖聊天历史，所以这一条不会自然消失。
    AWAITING_CLARIFY = "awaiting_clarify"
    INTERRUPTED = "interrupted"


class ChatSessionSummary(BaseModel):
    session_id: str
    title: str
    status: SessionStatus
    mode: str
    last_message_preview: str = ""
    created_at: str
    updated_at: str


class ChatSessionDetail(BaseModel):
    session_id: str
    title: str
    status: SessionStatus
    mode: str
    last_message_preview: str = ""
    created_at: str
    updated_at: str
    messages: List[Dict[str, Any]]


class UpdateSessionRequest(BaseModel):
    """会话重命名请求体。空标题在前端即视作取消，不会到达此处。"""

    title: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Trip Run / TripOps 任务生命周期
# ---------------------------------------------------------------------------

class TripRunCreateRequest(BaseModel):
    session_id: str = ""
    route_decision: RouteDecision
    controlled_trip_identity: ControlledTripIdentity
    request_message_id: str = ""
    assistant_message_id: str = ""
    parent_run_id: Optional[str] = None


class DefaultOriginRequest(BaseModel):
    place: PlaceIdentity


class DefaultOriginResponse(BaseModel):
    place: Optional[PlaceIdentity] = None


class PlaceCandidateResponse(BaseModel):
    place: PlaceIdentity
    confidence: float = Field(ge=0, le=1)
    requires_confirmation: bool = True


class PlaceSearchResponse(BaseModel):
    query: str
    role: Literal["origin", "destination", "itinerary_place"]
    candidates: List[PlaceCandidateResponse] = Field(default_factory=list)


class TripRunControlRequest(BaseModel):
    action: Literal["cancel"]
    session_id: Optional[str] = None


class TripRunControlResponse(BaseModel):
    run_id: str
    action: str
    accepted: bool
    status: str
    message: str
    in_process_handle: bool = False


class TripRunSupplementRequest(BaseModel):
    category: Literal["food", "transport", "accommodation", "pace", "must_do", "other"]
    content: str = Field(min_length=1, max_length=2000)
    session_id: Optional[str] = None


class TripRunSupplementResponse(BaseModel):
    run_id: str
    accepted: bool
    category: str
    message: str
    impact_scope: List[str] = Field(default_factory=list)


class TripRunResponse(BaseModel):
    run_id: str
    session_id: str
    mode: str
    status: str
    title: Optional[str] = None
    request_message_id: str = ""
    assistant_message_id: str = ""
    parent_run_id: Optional[str] = None
    current_node: Optional[str] = None
    resume_policy: str = "clarify_only"
    last_error_code: Optional[str] = None
    last_error_message: Optional[str] = None
    attempt: int = 1
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    cancelled_at: Optional[str] = None


class TripRunStateResponse(BaseModel):
    run_id: str
    status: str
    current_node: Optional[str] = None
    completed_nodes: List[str] = Field(default_factory=list)
    latest_state_summary: Dict[str, Any] = Field(default_factory=dict)
    pending_user_choice: Optional[Dict[str, Any]] = None
    trace_event_count: int = 0
    pending_monitor_trigger_count: int = 0
    last_error: Optional[Dict[str, Any]] = None
    updated_at: str


class TripRunEventResponse(BaseModel):
    event_id: Optional[int] = None
    run_id: str
    sequence: int
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class TripRunEventWindowResponse(BaseModel):
    run_id: str
    requested_after_sequence: int
    replay_floor_sequence: int
    latest_sequence: int
    next_after_sequence: int
    window_expired: bool
    run_status: str
    current_bundle_id: Optional[str] = None
    events: List[TripRunEventResponse] = Field(default_factory=list)


class TripRunExecutionResponse(BaseModel):
    """执行归属与恢复判定。`status` 与 TripRun 的业务状态是两件事。"""

    status: str
    last_heartbeat_at: Optional[str] = None
    lease_expires_at: Optional[str] = None
    last_safe_checkpoint_id: Optional[str] = None
    recovery_reason: Optional[str] = None


class TripRunDetailResponse(BaseModel):
    run: TripRunResponse
    controlled_trip_identity: Optional[ControlledTripIdentity] = None
    state: TripRunStateResponse
    execution: Optional[TripRunExecutionResponse] = None
    # 服务端说清这个 run 现在能做什么，客户端不从状态、resume_policy 与恢复判定里自己推。
    available_actions: List[str] = Field(default_factory=list)
    events: List[TripRunEventResponse] = Field(default_factory=list)


class TripRunListResponse(BaseModel):
    runs: List[TripRunResponse] = Field(default_factory=list)
    total: int = Field(
        default=0,
        description=(
            "Number of TripRuns matching the query filters (session_id/status/"
            "mode), ignoring `limit`. May exceed len(runs) when the page "
            "is truncated."
        ),
    )


class PublicBundleManifestResponse(BaseModel):
    """Public subset of Delivery Bundle manifest."""

    model_config = {"extra": "forbid"}

    contract_version: str
    run_id: str
    bundle_id: str
    workspace_revision: int
    fact_data_revision: int
    weather_data_revision: int
    created_at: str


class PublicBundleWorkspaceResponse(BaseModel):
    """Public workspace projection nested under PublicDeliveryBundleResponse."""

    model_config = {"extra": "forbid"}

    contract_version: str
    run_id: str
    workspace_revision: int
    itinerary: Dict[str, Any]
    selection_slots: List[Dict[str, Any]] = Field(default_factory=list)
    weather_proposal_decisions: List[Dict[str, Any]] = Field(default_factory=list)
    weather_adjustments: List[Dict[str, Any]] = Field(default_factory=list)


class PublicCoverageDisclosureResponse(BaseModel):
    """What this Run could not cover, already turned into sentences.

    Locked shape: the structured domain list stays internal, and the wire carries
    only the prose the report and the PDF print verbatim.
    """

    model_config = {"extra": "forbid"}

    notes: List[str] = Field(default_factory=list)


class PublicProviderEnvironmentResponse(BaseModel):
    """Whether any evidence in this plan came from a supplier's test environment."""

    model_config = {"extra": "forbid"}

    sandbox_note: Optional[str] = None


class PublicDeliveryBundleResponse(BaseModel):
    """Consumer-facing projection of the immutable Delivery Bundle.

    Top-level shape is locked. Nested itinerary/report/map bodies remain
    structured dicts projected by ``public_delivery_bundle``; OpenAPI no longer
    treats the entire Bundle as an opaque ``Dict[str, Any]``.
    """

    model_config = {"extra": "forbid"}

    manifest: PublicBundleManifestResponse
    workspace: PublicBundleWorkspaceResponse
    report_projection: Dict[str, Any]
    map_projection: Dict[str, Any]
    source_index: Dict[str, Any]
    coverage_disclosure: PublicCoverageDisclosureResponse
    provider_environment: PublicProviderEnvironmentResponse


class TripRunCompletionDiagnosticsResponse(BaseModel):
    """Developer/Eval-only, audit-safe completion diagnostic summary."""

    run_id: str
    status: str
    completion_audit: Dict[str, Any] = Field(default_factory=dict)


class WorkspaceV2MutationRequest(BaseModel):
    session_id: Optional[str] = None
    mutation_id: str = Field(min_length=1, max_length=200)
    base_bundle_id: str = Field(min_length=1)
    base_workspace_revision: int = Field(ge=0)
    base_fact_data_revision: int = Field(ge=0)
    base_weather_data_revision: int = Field(ge=0)
    operation: WorkspaceV2Mutation


class WorkspaceV2MutationImpactResponse(BaseModel):
    kind: Literal["total_cost"]
    delta_cny: float
    summary: str


class WorkspaceV2MutationPreviewResponse(BaseModel):
    mutation_id: str
    allowed: bool = True
    changed: bool
    requires_confirmation: bool
    impacts: List[WorkspaceV2MutationImpactResponse] = Field(default_factory=list)


class WorkspaceV2MutationResponse(BaseModel):
    mutation_id: str
    changed: bool
    idempotent_replay: bool
    bundle: PublicDeliveryBundleResponse


class WorkspaceV2UndoHeadResponse(BaseModel):
    available: bool
    mutation_id: Optional[str] = None
    label: Optional[str] = None


class WorkspaceV2UndoRequest(BaseModel):
    session_id: Optional[str] = None
    undo_id: str = Field(min_length=1, max_length=200)
    undo_of_mutation_id: str = Field(min_length=1, max_length=200)
    base_bundle_id: str = Field(min_length=1)
    base_workspace_revision: int = Field(ge=0)
    base_fact_data_revision: int = Field(ge=0)
    base_weather_data_revision: int = Field(ge=0)


class WorkspaceV2UndoResponse(BaseModel):
    undo_id: str
    idempotent_replay: bool
    bundle: PublicDeliveryBundleResponse


class TripReportPdfExportRequest(BaseModel):
    session_id: Optional[str] = None
    bundle_id: str = Field(min_length=1)
    workspace_revision: int = Field(ge=0)
    fact_data_revision: int = Field(ge=0)
    weather_data_revision: int = Field(ge=0)


class WeatherBundleRefreshRequest(BaseModel):
    session_id: Optional[str] = None
    refresh_id: str = Field(min_length=1, max_length=200)
    base_bundle_id: str = Field(min_length=1)
    base_workspace_revision: int = Field(ge=0)
    base_fact_data_revision: int = Field(ge=0)
    base_weather_data_revision: int = Field(ge=0)


class WeatherBundleRefreshResponse(BaseModel):
    refresh_id: str
    attempted: bool
    committed: bool
    used_previous_values: bool
    refusal_reason: Optional[str] = Field(
        default=None,
        description=(
            "机器可读的拒绝原因码。仅当本次刷新已尝试但被显式拒绝时出现"
            "（此时 committed=false 且 bundle 为未改变的当前 Bundle）。"
            "取值来自再录取/交付合同词表，例如 candidate_evidence_missing、"
            "weather_snapshot_inconsistent。为 null 表示没有发生拒绝。"
        ),
    )
    bundle: PublicDeliveryBundleResponse


class ToolAuditRecordResponse(BaseModel):
    audit_id: str
    run_id: Optional[str] = None
    tool_name: str
    server_name: Optional[str] = None
    source_type: str = "tool"
    category: str = "other"
    permission_class: str = "read_only"
    operation_sensitivity: str = "low"
    status: str
    gateway_decision: str
    args_digest: str
    result_digest: str = ""
    untrusted_content: bool = False
    quarantined: bool = False
    fallback_from: Optional[str] = None
    fallback_to: Optional[str] = None
    degradation_reason: Optional[str] = None
    error: Optional[str] = None
    evidence_allowed: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ToolAuditListResponse(BaseModel):
    run_id: str
    audits: List[ToolAuditRecordResponse] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# 成本台账 DTO
# ---------------------------------------------------------------------------

class LLMCallCostResponse(BaseModel):
    id: str
    run_id: str
    node: Optional[str] = None
    agent: Optional[str] = None
    tier: Optional[str] = None
    provider: Optional[str] = None
    model_request: str = ""
    model_response: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    reasoning_output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    estimated: bool = False
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None
    ttft_ms: Optional[float] = None
    latency_ms: Optional[float] = None
    status: str = "ok"
    stream: bool = False


class RunCostSummaryResponse(BaseModel):
    run_id: str
    call_count: int = 0
    priced_call_count: int = 0
    unpriced_call_count: int = 0
    estimated_call_count: int = 0
    error_call_count: int = 0
    estimated_ratio: float = 0.0
    cost_coverage_ratio: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_input_tokens: int = 0
    total_reasoning_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: Optional[float] = None
    currency: str = "USD"
    total_latency_ms: float = 0.0
    wall_ms: Optional[float] = None
    by_agent: List[Dict[str, Any]] = Field(default_factory=list)
    by_node: List[Dict[str, Any]] = Field(default_factory=list)
    bottleneck_by_cost: List[Dict[str, Any]] = Field(default_factory=list)
    bottleneck_by_latency: List[Dict[str, Any]] = Field(default_factory=list)
    # Tool Search 上下文节省量：run 终结时随 SSE run_cost_summary 下发实测值
    # {mode, schema_tokens_injected, schema_tokens_full_baseline, tool_context_saving, ...}；
    # DB 台账（REST 事后查询）不持有进程内曝光计量，故此路径为 null。
    tool_context_saving: Optional[Dict[str, Any]] = None


class RunCostResponse(BaseModel):
    run_id: str
    summary: RunCostSummaryResponse
    calls: List[LLMCallCostResponse] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0


# ---------------------------------------------------------------------------
# 系统配置
# ---------------------------------------------------------------------------

class ModelConfigRequest(BaseModel):
    """更新主力模型配置"""
    api_key: str
    model_name: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    # Read from ``config.MAX_COMPLETION_TOKENS`` rather than restated here: this
    # default is written straight into settings and persisted, so a POST that
    # omits the field must not quietly lower a running deployment's ceiling — and
    # a second copy of the number is exactly how that happens.
    max_tokens: int = MAX_COMPLETION_TOKENS
    temperature: float = 0.7
    tier: str = "primary"  # "primary" | "fast" | "vision"


class SystemStatus(BaseModel):
    status: str
    version: str = "2.0"
    tools_count: int = 0
    db_connected: bool = False
    redis_connected: bool = False
    # 记忆抽取管线累计计数（CB-05）：attempted/succeeded/failed/facts_written/portraits_written/last_error。
    # 让 fire-and-forget 抽取链路的健康在演示前可 curl 确认，不必翻日志。
    memory_extraction: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 知识库管理
# ---------------------------------------------------------------------------

class KnowledgeUploadRequest(BaseModel):
    content: str
    source: str = "manual"
    collection: str = "default"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class KnowledgeUploadResponse(BaseModel):
    chunks_indexed: int
    collection: str
    message: str


class KnowledgeSourceDetailResponse(BaseModel):
    source: str
    chunk_count: int
    updated_at: Optional[datetime] = None


class KnowledgeCollectionStatsResponse(BaseModel):
    collection: str
    total: int = 0
    sources: int = 0
    source_details: List[KnowledgeSourceDetailResponse] = Field(default_factory=list)
    oldest: Optional[datetime] = None
    newest: Optional[datetime] = None


class KnowledgeSourceDocumentResponse(BaseModel):
    """一篇资料的正文。

    `chunk_count` 与列表那一行印的是同一个数（都来自段表），所以打开一篇资料不会
    看到和列表不一致的段数。
    """

    collection: str
    source: str
    content: str
    chunk_count: int
    updated_at: Optional[datetime] = None


class KnowledgeSourceUpdateRequest(BaseModel):
    """改写一篇资料的正文。

    只有正文 —— 来源名不在这里改：它是这一篇的身份（`(collection, source)` 唯一），
    改名是另一件事，产品目前没有那个入口，所以这里也不留一个没人拨的旋钮。
    """

    content: str


class KnowledgeDeleteResponse(BaseModel):
    collection: str
    deleted_chunks: int
    message: str


class KnowledgeQueryRequest(BaseModel):
    query: str
    collection: str = "default"
    top_k: int = 5


class KnowledgeQueryResponse(BaseModel):
    results: List[Dict[str, Any]]
    total: int


# ---------------------------------------------------------------------------
# 用户相关
# ---------------------------------------------------------------------------

class UserProfileResponse(BaseModel):
    display_name: str
    preferences: Dict[str, Any]


class UpdatePreferencesRequest(BaseModel):
    preferences: Dict[str, Any]


class MemoryDeleteOptions(BaseModel):
    """删一条 / 删一类记忆时的开关。

    ``extra="forbid"`` 与下面那个是同一道锁，理由见 ``MemoryDeleteAllOptions``。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: Optional[str] = None
    reason: str = ""
    clear_auto_portrait: bool = False
    clear_graph: bool = False
    clear_session_anchors: bool = False


class MemoryDeleteAllOptions(BaseModel):
    """「删除全部记忆」的开关。

    这里的 ``clear_auto_portrait`` 只管系统自己总结出来的那段画像
    （``UserProfile.auto_portrait``）。
    用户手填的六组偏好与常用出发地不在任何一次记忆删除的范围里。

    **``extra="forbid"`` 是这里配套的锁，不是洁癖。** 三个开关的默认值都是 ``True``：
    不禁多余键的话，过期客户端发来的旧字段会被静默丢掉，然后三个默认 True
    生效 —— 一次本该 422 的过期请求变成一次静默全清。那样硬断裂就退化成了兼容层。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: Optional[str] = None
    reason: str = ""
    clear_auto_portrait: bool = True
    clear_graph: bool = True
    clear_session_anchors: bool = True


class MemoryRetentionCleanupRequest(BaseModel):
    request_id: Optional[str] = None
    reason: str = "retention_cleanup"
    limit: int = 1000


class MemoryDeletionResponse(BaseModel):
    request_id: str
    scope: str
    category: Optional[str] = None
    fact_id: Optional[str] = None
    status: str
    affected_facts: int = 0
    affected_entities: int = 0
    affected_relations: int = 0
    affected_profiles: int = 0
    affected_session_anchors: int = 0
    boundary: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class MemoryForgettingAuditResponse(MemoryDeletionResponse):
    pass


class MemoryForgettingAuditListResponse(BaseModel):
    audits: List[MemoryForgettingAuditResponse] = Field(default_factory=list)
    total: int = 0


class MemoryFactItem(BaseModel):
    fact_id: str
    content: str
    category: Optional[str] = None
    importance: int = 5
    # 'manual' = 用户手动添加，'auto' = 系统自动抽取
    source: str = "auto"
    created_at: str = ""
    expires_at: str = ""


class MemoryFactListResponse(BaseModel):
    facts: List[MemoryFactItem] = Field(default_factory=list)
    total: int = 0


class AddMemoryFactRequest(BaseModel):
    content: str
    category: Optional[str] = None


class AddMemoryFactResponse(BaseModel):
    # 'created' = 这次真写了一条；'existing' = 同一句话本来就在，没有重复写入。
    # 界面靠这个字眼解释「点了添加但列表没变长」。
    status: str
    # **必填**：一次成功的添加必然对着一条真事实；它可空时，「回读把异常吞成
    # 空列表」会变成一个 `status="completed"` 而 `fact=None` 的回执。
    fact: MemoryFactItem


# ---------------------------------------------------------------------------
# Preset 相关
# ---------------------------------------------------------------------------

# 三个请求/响应 schema 的长度上限全部引 ``entities.preset`` 的常量，不许写字面量：
# 「一条指令最多多长」是那个字段的属性，只能有一个定义处。抄一份数字进来，
# 就是这个仓那个反复出现的形状 —— 同一个角色两套值，其中一套静默胜出。
class PresetCreateRequest(BaseModel):
    name: str = Field(max_length=PRESET_NAME_MAX_CHARS)
    description: str = Field(max_length=PRESET_DESCRIPTION_MAX_CHARS)
    icon: str = "compass"
    category: str = "custom"
    instructions: str = Field(max_length=PRESET_INSTRUCTIONS_MAX_CHARS)
    constraints: PresetConstraints = Field(default_factory=PresetConstraints)


class PresetUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=PRESET_NAME_MAX_CHARS)
    description: Optional[str] = Field(default=None, max_length=PRESET_DESCRIPTION_MAX_CHARS)
    icon: Optional[str] = None
    category: Optional[str] = None
    instructions: Optional[str] = Field(default=None, max_length=PRESET_INSTRUCTIONS_MAX_CHARS)
    constraints: Optional[PresetConstraints] = None


class PresetResponse(BaseModel):
    id: str
    name: str = Field(max_length=PRESET_NAME_MAX_CHARS)
    description: str = Field(max_length=PRESET_DESCRIPTION_MAX_CHARS)
    icon: str
    category: str
    instructions: str = Field(max_length=PRESET_INSTRUCTIONS_MAX_CHARS)
    constraints: PresetConstraints
    is_preset: bool
    usage_count: int
    created_at: str
    updated_at: str


class GenerateInstructionsRequest(BaseModel):
    description: str
