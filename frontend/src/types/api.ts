export type PlaceKind = 'city' | 'administrative_area' | 'island' | 'scenic_area' | 'airport' | 'train_station' | 'country' | 'poi' | 'hotel' | 'restaurant';
export type JourneyRoute = 'trip_planning' | 'destination_discovery' | 'fast_answer' | 'trip_refinement';
export type TripSupplementCategory = 'food' | 'transport' | 'accommodation' | 'pace' | 'must_do' | 'other';

/**
 * 计划批准门的决策动作 —— **全前端一处定义**，`api/schemas.py::GateDecision` 那个
 * `Literal` 的对面。
 *
 * 它住在这里（线上形状那一层）而不是 `types/chat.ts`，是因为 `chat.ts` 已经 import
 * `api.ts`；反向再 import 就是一圈依赖。`chat.ts` 原地 re-export 它，既有的引用点不动。
 */
export type PlanGateDecisionAction = 'approve' | 'edit' | 'supplement' | 'cancel';

export interface PlaceIdentity {
  place_id: string;
  provider: 'osm' | 'amap' | 'manual_verified';
  kind: PlaceKind;
  name: string;
  display_name: string;
  country_code: string;
  latitude: number;
  longitude: number;
  admin_path: string[];
}

export interface RouteDecision {
  route: JourneyRoute;
  confidence: number;
  alternatives: Array<{ route: JourneyRoute; confidence: number }>;
  signals: string[];
  requires_trip_draft: boolean;
  requires_confirmation?: boolean;
}

export interface ControlledTripIdentity {
  origin: PlaceIdentity;
  destinations: PlaceIdentity[];
  start_date: string;
  end_date: string;
  party: { adults: number; children: number; elderly_companions: boolean; accessibility_required: boolean };
  // source 只有两个值，与后端 ``entities/trip_input.py::TravelStyle`` 逐字对齐：
  // TripPlanner 是唯一的产出方，它按用户有没有动过风格那一栏写 current 或 suggested。
  style: { primary: string; secondary_interests: string[]; source: 'current' | 'suggested' };
}

export interface PlannerOption {
  id: string;
  label: string;
  inference_keywords: string[];
  is_default: boolean;
}

export interface TripPlannerConfiguration {
  primary_styles: PlannerOption[];
  secondary_interests: PlannerOption[];
  default_adults: number;
  default_children: number;
  default_elderly_companions: boolean;
  default_accessibility_required: boolean;
  max_secondary_interests: number;
  inspiration_rotation_ms: number;
  inspiration_prompts: string[];
}

export interface GuidedIntakeState {
  raw_input: string;
  route_decision: RouteDecision;
  controlled_identity: ControlledTripIdentity | null;
  seed_destinations?: PlaceIdentity[];
  missing_fields: Array<'origin' | 'destinations' | 'dates' | 'party' | 'style' | 'place_confirmation'>;
  ready_to_create: boolean;
}

export interface ChatRequest {
  messages: Array<{
    role: string;
    content: string;
    message_id?: string;
    type?: string;
  }>;
  session_id?: string | null;
  run_id?: string | null;
  user_id?: string;
  route?: JourneyRoute | null;
  route_decision?: RouteDecision | null;
  controlled_trip_identity?: ControlledTripIdentity | null;
  guided_intake?: GuidedIntakeState | null;
  preset_id?: string | null;
  selected_mcp_servers?: string[];
  plan_gate?: boolean | null;
  gate_decision?: {
    // `content` 是唯一自由文本载体（改写后的计划全文 / 补充要求）。
    //
    // 动作表**不在这里第二次写一遍**：它是 `types/chat.ts::PlanGateDecisionAction`，
    // 后端 `api/schemas.py::GateDecision` 那个 `Literal` 的对面。此前这里、
    // `hooks/useSendMessage.ts` 与 `types/chat.ts` 各写了一份同样的四元联合，
    // 三处得靠手工同步 —— 而后端加一个动作时，三份里漏改哪一份都只表现为
    // 「那个动作发不出去」，不报错。
    action: PlanGateDecisionAction;
    content?: string;
  } | null;
}

export interface ChatResponse {
  session_id: string;
  message_id: string;
  content: string;
  task_type?: string | null;
  tokens_used?: number | null;
}

export interface MemoryExtractionStats {
  attempted: number;
  succeeded: number;
  failed: number;
  facts_written: number;
  portraits_written: number;
  last_error: string | null;
}

export interface SystemStatus {
  status: string;
  version: string;
  tools_count: number;
  db_connected: boolean;
  redis_connected: boolean;
  /** 记忆抽取管线累计计数（CB-05）；旧后端可能不含此字段。 */
  memory_extraction?: MemoryExtractionStats;
}

export interface SystemConfig {
  primary_model: ModelConfig;
  fast_model: ModelConfig;
  rag: RagConfig;
  env: string;
}

export interface ModelConfig {
  model_name: string;
  base_url: string;
  api_key_set: boolean;
  max_tokens?: number;
  temperature?: number;
}

export interface RagConfig {
  chunk_size: number;
  top_k: number;
  embedding_model: string;
}

export interface ModelConfigRequest {
  api_key: string;
  model_name: string;
  base_url: string;
  max_tokens: number;
  temperature: number;
  tier: 'primary' | 'fast';
}

export interface ToolInfo {
  name: string;
  description?: string;
  source?: string;
  server_name?: string | null;
  [key: string]: unknown;
}

export interface KnowledgeUploadResponse {
  chunks_indexed: number;
  collection: string;
  message: string;
}

export interface KnowledgeSourceDetail {
  /** 来源名（文件名或手动输入标签） */
  source: string;
  /** 该来源被切分成的资料段数量 */
  chunk_count: number;
  updated_at?: string | null;
}

export interface KnowledgeCollectionStats {
  collection: string;
  /** chunk 总数 */
  total: number;
  /** 来源计数 */
  sources: number;
  /** 每个来源的明细（来源名 + 资料段数量） */
  source_details: KnowledgeSourceDetail[];
  oldest: string | null;
  newest: string | null;
}

/** 一篇资料的正文（打开列表里那一行时读到的东西）。 */
export interface KnowledgeSourceDocument {
  collection: string;
  source: string;
  content: string;
  /** 这一篇当前被切成多少段 —— 与列表那一行印的是同一个数。 */
  chunk_count: number;
  updated_at?: string | null;
}

export interface KnowledgeDeleteResponse {
  collection: string;
  deleted_chunks: number;
  message: string;
}

export interface TripItem {
  tripId: string;
  destination: string;
  durationDays: number;
  date: string;
  travelers?: number;
  rating?: number;
  highlights?: string[];
}

export interface UserProfile {
  user_id: string;
  display_name: string;
  preferences: Record<string, unknown>;
  trip_history_count: number;
  trip_history?: TripItem[];
}

/**
 * 一组偏好的组名、单选/多选、以及它的**全部合法取值**。
 *
 * 逐字来自 `GET /api/users/preference-options`（后端
 * `entities/user.py::TRAVEL_PREFERENCE_GROUPS`）。**前端不许再写一份选项表** ——
 * 此前「我的偏好」自己带着六组各一份中文选项，而后端任意字符串照收，两份漂开之后
 * 库里五个值一枚 chip 都不高亮、多选组永远点不掉。
 */
export interface PreferenceOptionGroup {
  key: string;
  label: string;
  multi: boolean;
  options: string[];
}

/**
 * `awaiting_clarify` 是只读历史值：clarify 产品面已删除，后端没有任何写入方，但
 * chat_sessions 表里仍有历史行带着它（清库不覆盖聊天历史）。保留成员是为了读得出来。
 */
export type SessionStatus = 'active' | 'awaiting_clarify' | 'interrupted';

export interface SessionSummary {
  session_id: string;
  title: string;
  status: SessionStatus;
  mode: 'fast' | 'deep';
  last_message_preview: string;
  created_at: string;
  updated_at: string;
}

export interface SessionDetail extends SessionSummary {
  messages: Array<{
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    display_content: string;
    timestamp: string;
    run_id?: string;
    /**
     * 与实时态 `Message['type']` 同一套词汇。`guard_blocked` = 输入安全策略拒绝：
     * 后端把它原样落库为 assistant_type（`api/routes/chat.py`），回放时逐字带回，
     * 所以这里必须声明得出来，否则契约在说一个后端确实会发的值不可能出现。
     */
    type?: 'normal' | 'thinking' | 'error' | 'guard_blocked' | 'waiting_approval' | 'interrupted' | 'context_compaction';
    agent_name?: string;
    step_name?: string;
    task_type?: string;
    mode?: string;
    thinking_steps?: Array<{
      id: string;
      agent_name: string;
      content: string;
      step_name: string;
      timestamp: string;
      end_time?: string;
      // 工具调用字段
      is_tool_call?: boolean;
      tool_name?: string;
      tool_call_id?: string;
      /** 与实时态同源的展示状态；`capability_declared` = 日期能力判定（中性，非失败）。 */
      tool_status?: 'running' | 'completed' | 'failed' | 'degraded' | 'capability_declared';
      tool_args?: string;
      tool_result?: string;
      tool_category?: string;
      from_cache?: boolean;
      /** 工具步服务端实测耗时（ms）；M1 后端持久化，会话回放时映射为 serverDurationMs。 */
      duration_ms?: number | null;
    }>;
    // 用户可读的「这一轮参考了什么」，随助手消息持久化。分段与 prompt 里那三段一一对应；
    // 换形状之前落库的事件没有 referenced_sections 这个键，按本仓不设兼容层的规矩，
    // 那些老事件不渲染印记（normalizeContextReport 返回 null），而不是被兜成半份报告。
    context_report?: {
      referenced_sections?: Array<{ key?: string; items?: string[] }>;
      compaction?: {
        triggered: boolean;
      };
    };
    context_compaction?: {
      event_id: string;
      source: 'manual' | 'automatic';
      occurred_at: string;
      messages_compressed: number;
      tokens_before: number;
      tokens_after: number;
      summary: string;
      key_constraints: string[];
    };
    citations?: Array<{
      citation_id: string;
      claim_text?: string;
      sources: Array<{
        title?: string;
        url?: string;
        source_name?: string;
        snippet?: string;
        authority_label?: string;
        retrieved_at?: string;
      }>;
    }>;
    annotations?: Array<{
      annotation_id: string;
      kind: 'time_sensitive' | 'seasonal_reference' | 'safety_reference';
      label: string;
      detail: string;
    }>;
    trip_summary_card?: Record<string, unknown>;
  }>;
}

export interface ContextCompactionPayload {
  event_id: string;
  source: 'manual' | 'automatic';
  occurred_at: string;
  messages_compressed: number;
  tokens_before: number;
  tokens_after: number;
  summary: string;
  key_constraints: string[];
}

// ─────────────────────────────────────────────────────────────────────────────
// Phase 8 TripOps live API DTOs
// These mirror backend response_model contracts in src/travel_agent/api/schemas.py.
// These types describe the live REST/SSE boundaries.
// ─────────────────────────────────────────────────────────────────────────────

export type JsonObject = Record<string, unknown>;

export type TripRunMode = 'deep' | 'fast';

export type TripRunStatus =
  | 'created'
  | 'running'
  | 'awaiting_input'
  | 'completed'
  | 'failed'
  | 'interrupted'
  | 'cancel_requested'
  | 'cancelled';

export function isTripRunStatus(value: unknown): value is TripRunStatus {
  return typeof value === 'string' && [
    'created',
    'running',
    'awaiting_input',
    'completed',
    'failed',
    'interrupted',
    'cancel_requested',
    'cancelled',
  ].includes(value);
}

export function isTripRunActive(status: TripRunStatus | null): boolean {
  return status === 'created' || status === 'running';
}

export function isTripRunAwaitingInput(status: TripRunStatus | null): boolean {
  return status === 'awaiting_input';
}

export function isTripRunCancellable(status: TripRunStatus | null): boolean {
  return isTripRunActive(status) || isTripRunAwaitingInput(status);
}

export function isTripRunTerminal(status: TripRunStatus | null): boolean {
  return status === 'completed' || status === 'failed' || status === 'cancelled';
}

export function isTripRunResumable(status: TripRunStatus | null): boolean {
  return status === 'interrupted';
}

// TripRunCreateRequest intentionally omitted: product run creation is
// chat-stream owned. Backend POST /api/trip-runs remains for controlled/script
// callers only.

export interface TripRunControlRequest {
  action: 'cancel';
  user_id?: string | null;
  session_id?: string | null;
}

export interface TripRunControlResponse {
  run_id: string;
  action: string;
  accepted: boolean;
  status: TripRunStatus;
  message: string;
  in_process_handle: boolean;
}

export interface TripRunSupplementResponse {
  run_id: string;
  accepted: boolean;
  category: TripSupplementCategory;
  message: string;
  impact_scope: string[];
}

export interface TripRunResponse {
  run_id: string;
  session_id: string;
  user_id: string;
  mode: string;
  status: TripRunStatus;
  title: string | null;
  request_message_id: string;
  assistant_message_id: string;
  parent_run_id: string | null;
  current_node: string | null;
  resume_policy: string;
  last_error_code: string | null;
  last_error_message: string | null;
  attempt: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  cancelled_at: string | null;
}

export interface TripRunStateResponse {
  run_id: string;
  status: string;
  current_node: string | null;
  completed_nodes: string[];
  latest_state_summary: JsonObject;
  pending_user_choice: JsonObject | null;
  trace_event_count: number;
  pending_monitor_trigger_count: number;
  last_error: JsonObject | null;
  updated_at: string;
}

export interface TripRunEventResponse {
  event_id: number | null;
  run_id: string;
  sequence: number;
  event_type: string;
  payload: JsonObject;
  created_at: string;
}

export interface TripRunEventWindowResponse {
  run_id: string;
  requested_after_sequence: number;
  replay_floor_sequence: number;
  latest_sequence: number;
  next_after_sequence: number;
  window_expired: boolean;
  run_status: string;
  current_bundle_id: string | null;
  events: TripRunEventResponse[];
}

export interface TripRunDetailResponse {
  run: TripRunResponse;
  controlled_trip_identity: ControlledTripIdentity | null;
  state: TripRunStateResponse;
  events: TripRunEventResponse[];
}

export interface TripRunListResponse {
  runs: TripRunResponse[];
  total: number;
}

// ─────────────────────────────────────────────────────────────────────────────
// Cost ledger (台账层/暴露层契约)
// SSE：usage_update（运行中逐次计量）+ chat_complete.run_cost_summary（终结汇总）。
// ─────────────────────────────────────────────────────────────────────────────

/** 运行中逐次 LLM 调用计量事件（audit-safe：只有计数与成本，无内容）。 */
/**
 * 运行中逐次 LLM 调用计量 —— **只有运行中台账用得上的那几个数**。
 *
 * 这里**没有** `tier` / `provider` / `model` / `cached_input_tokens` /
 * `latency_ms` / `ttft_ms`。那六个是**逐调用归因**，服务于一个从未建成、现在明确不建的
 * 「圆圈 i」面；run 级台账（§5）已经把「成本与可观测」这条能力说清了。它们仍然逐条
 * 落在后端 `run_llm_calls` 里供内部可观测，只是不再过桥。
 *
 * `node` / `agent` 留着：运行中台账要说出「此刻在花钱的是哪一步」（`CostLedger`
 * 的 `activeLabel`）。
 */
export interface UsageUpdateEvent {
  type: 'usage_update';
  message_id?: string;
  run_id: string;
  node: string | null;
  agent: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number;
  cost_usd: number | null;
  estimated: boolean;
}

/** run_summary 的 by_agent / by_node 分组行（成本降序）。 */
export interface CostGroupBreakdown {
  agent?: string;
  node?: string;
  call_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  latency_ms: number;
}

/** 瓶颈节点（按 cost 或 latency 维度取 top3）。 */
export interface CostBottleneck {
  node: string;
  cost_usd: number | null;
  latency_ms: number;
  call_count: number;
}

/** Tool Search 上下文节省量（实测；DB/REST 路径为 null）。 */
export interface ToolContextSaving {
  mode: 'full' | 'deferred' | 'mixed';
  worker_assemblies: number;
  deferred_assemblies: number;
  schema_tokens_injected: number;
  schema_tokens_full_baseline: number;
  schema_tokens_saved: number;
  /** 0..1 节省比例（0.68 → 68%）。 */
  tool_context_saving: number;
  tools_exposed_initial: number;
  tools_full_baseline: number;
}

export interface RunCostSummary {
  run_id: string;
  call_count: number;
  priced_call_count: number;
  unpriced_call_count: number;
  estimated_call_count: number;
  error_call_count: number;
  estimated_ratio: number;
  cost_coverage_ratio: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cached_input_tokens: number;
  total_reasoning_output_tokens: number;
  total_tokens: number;
  /** 未命中价格（priced==0）时为 null——只报 token，不编造 0 成本。 */
  total_cost_usd: number | null;
  currency: string;
  total_latency_ms: number;
  wall_ms: number | null;
  by_agent: CostGroupBreakdown[];
  by_node: CostGroupBreakdown[];
  bottleneck_by_cost: CostBottleneck[];
  bottleneck_by_latency: CostBottleneck[];
  tool_context_saving: ToolContextSaving | null;
  /**
   * 本 run 终结时未能写入成本台账的调用数（CB-02）。>0 时事件流发 run.cost_record_failed，
   * 前端如实提示「成本记录失败」。仅 SSE run_cost_summary 携带；REST /cost 汇总不含此字段。
   */
  record_failed?: number;
}

export interface MemoryDeleteOptions {
  request_id?: string | null;
  reason?: string;
  clear_auto_portrait?: boolean;
  clear_graph?: boolean;
  clear_session_anchors?: boolean;
}

/**
 * 「删除全部记忆」的开关。
 *
 * `clear_auto_portrait` 此前叫 `clear_profile`，而它管的从来只有系统自己总结出来的那段
 * 画像；用户手填的六组偏好与常用出发地不在任何一次记忆删除的范围里。后端那两个
 * 模型是 `extra="forbid"` 的 —— **这里发旧键名不会被忽略，会 422**。
 */
export interface MemoryDeleteAllOptions {
  request_id?: string | null;
  reason?: string;
  clear_auto_portrait?: boolean;
  clear_graph?: boolean;
  clear_session_anchors?: boolean;
}

export interface MemoryRetentionCleanupRequest {
  request_id?: string | null;
  reason?: string;
  limit?: number;
}

export interface MemoryDeletionResponse {
  request_id: string;
  user_id: string;
  scope: string;
  category: string | null;
  fact_id: string | null;
  status: string;
  affected_facts: number;
  affected_entities: number;
  affected_relations: number;
  affected_profiles: number;
  affected_session_anchors: number;
  boundary: JsonObject;
  created_at: string;
}

/** 'manual' = 用户手动添加；'auto' = 系统自动抽取 */
export type MemoryFactSource = 'manual' | 'auto';

export interface MemoryFactItem {
  fact_id: string;
  content: string;
  category: string | null;
  importance: number;
  source: MemoryFactSource;
  created_at: string;
  expires_at: string;
}

export interface MemoryFactListResponse {
  user_id: string;
  facts: MemoryFactItem[];
  total: number;
}

export interface AddMemoryFactResponse {
  user_id: string;
  /** `created` = 这次真写了一条；`existing` = 同一句话本来就在，服务端没有重复写入。 */
  status: 'created' | 'existing';
  /** 一次成功的添加必然对着一条真事实，**不可为空**。 */
  fact: MemoryFactItem;
}
