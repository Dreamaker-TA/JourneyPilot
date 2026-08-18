import type { GuidedIntakeState, PlanGateDecisionAction, RouteDecision, RunCostSummary, SessionStatus } from './api';
import type { PublicDeliveryBundle, DeliveryRevisionManifest } from './delivery';

export interface TripSummaryFact {
  label: string;
  value: string;
  state: 'confirmed' | 'default' | 'deferred';
}

export interface TripSummaryCard {
  headline: string;
  summary: string;
  facts: TripSummaryFact[];
  priorities: string[];
  currentFocus: string;
  nextMilestone: string | null;
  compactLine: string;
  requiresUserConfirmation: boolean;
}

export interface CitationSource {
  title?: string;
  url?: string;
  sourceName?: string;
  snippet?: string;
  authorityLabel?: string;
  retrievedAt?: string;
}

export interface FinalAnswerCitation {
  citationId: string;
  claimText?: string;
  sources: CitationSource[];
}

export type InformationAnnotationKind = 'time_sensitive' | 'seasonal_reference' | 'safety_reference';

export interface InformationAnnotation {
  annotationId: string;
  kind: InformationAnnotationKind;
  label: string;
  detail: string;
}

/** 这一轮是否整理过较早对话。 */
export interface ContextReportCompaction {
  triggered: boolean;
}

/**
 * 上下文透镜的三段，与后端 prompt 里那三段一一对应。
 * `reference` 是系统推理出来的参考级背景，prompt 里明写着它不是约束。
 */
export type ContextReportSectionKey = 'hard' | 'preference' | 'reference';

export interface ContextReportSection {
  key: ContextReportSectionKey;
  items: string[];
}

/**
 * 用户主动打开“更多信息”时显示的、这一轮真的进了 prompt 的那几段。
 *
 * **分段而不是一串平铺**：平铺那版前端还自己截到 8 条，于是「屏幕上说参考了」与
 * 「模型真的读到了」在条数上各自漂；而且「必须遵守」和「仅供参考」混在一起时，
 * 系统猜出来的东西看上去和用户亲口说的一样硬。
 */
export interface ContextReport {
  sections: ContextReportSection[];
  compaction: ContextReportCompaction;
}

/**
 * 一次不可变的会话压缩快照；面向普通用户的对话时间线事件。
 *
 * 这里**没有** token 计数（`messagesCompressed` / `tokensBefore` / `tokensAfter`）：
 * ⓘ 面不出现 token，所以那三个数在这一屏永远不会被印出来 —— 装进来就是
 * 「算好了没人看」。它们仍然在落库的那份快照里，供可观测使用。
 */
export interface ContextCompactionEvent {
  id: string;
  source: 'manual' | 'automatic';
  occurredAt: string;
  summary: string;
  keyConstraints: string[];
}

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  displayContent: string;
  timestamp: Date;
  /**
   * `guard_blocked` = 输入安全策略拒绝，与 `error`（系统故障）是两件事：
   * 后端实时帧下发 `chat_complete.guard_blocked`，历史回放落库的 assistant_type
   * 就是这个字面量，两条路径共用同一个成员，无需任何转译。
   */
  type?: 'normal' | 'thinking' | 'error' | 'guard_blocked' | 'waiting_approval' | 'interrupted' | 'context_compaction';
  agentType?: string;
  runId?: string;
  duration?: number;
  inputTokens?: number | null;
  outputTokens?: number | null;
  totalTokens?: number | null;
  reasoningTokens?: number | null;
  cachedTokens?: number | null;
  hasUsage?: boolean;
  thinkingSteps?: ThinkingStep[]; // 记录该消息的思考过程
  contextReport?: ContextReport;  // 上下文透镜印记数据（有则渲染 Hallmark，无则不渲染）
  /** 用户可回看的完整压缩摘要与显式约束快照。 */
  contextCompaction?: ContextCompactionEvent;
  /** 由「载入更早的对话」翻回来的历史。setup 投影不切它（见 conversationFlow）。 */
  isEarlierHistory?: boolean;
  /** 正文安全局部锚点与可展示来源；不含 ev_/claim_ 内部 id。 */
  citations?: FinalAnswerCitation[];
  /** 正文局部信息状态；与来源 citation 分离，不承诺每句话都有来源。 */
  annotations?: InformationAnnotation[];
  /** 运行中但正文尚未到达时的用户可读固定状态文案。 */
  pendingStatusText?: string;
  /**
   * 该消息是否已定稿（流式已正常收尾）。作为流式小飞机显隐的第二重保险：一旦定稿即置
   * true，即便全局 isStreaming 因异常路径未复位，飞机也不会残留。
   * chat_complete / error / 中断等一切定稿处均置 true。
   */
  streamCompleted?: boolean;
}

export type ToolCategory = 'internal' | 'search' | 'data' | 'calculation' | 'other';

/**
 * 服务端 `travel_agent.tools.governance.ToolExecutionStatus` 的封闭枚举值，
 * 只由 Tool Gateway 写入，随 `tool_result` 帧原样过桥。
 */
export type ToolExecutionStatus =
  | 'success'
  | 'failed'
  | 'degraded'
  | 'blocked'
  | 'not_applicable'
  | 'reference_only';

export interface ThinkingStep {
  id: string;
  agentName: string;
  content: string;
  stepName: string;
  timestamp: Date;
  endTime?: Date;
  /**
   * 服务端单调钟相对 run 起点的毫秒数。步开始事件
   * （thinking / agent_progress / tool_start）携带；用于「相邻步边界 ts_ms 之差」
   * 定格思考步耗时——两端都是服务端单调钟，不受客户端时钟偏差影响。
   */
  tsMs?: number;
  /**
   * 服务端实测耗时（毫秒），逻辑本体包裹计时的定格值。
   * 工具步 = tool_result.duration_ms；思考步 = 相邻步边界 ts_ms 之差（由 reducer
   * 在下一步边界算好写入）。非空即表示该步已服务端定格，UI 一律用它，绝不用客户端
   * endTime 差值。
   */
  serverDurationMs?: number;
  // 工具调用步骤扩展字段
  isToolCall?: boolean;
  toolName?: string;
  toolCallId?: string;
  /**
   * 工具步的四态展示状态：
   * - `completed` = 主源成功；
   * - `degraded` = 走备用通道但仍拿到可用结果；
   * - `failed` = 执行失败；
   * - `capability_declared` = 服务端在调用前判定该数据源答不了所请求的日期，
   *   只返回参考资料。这是设计内的中性结果，既不是成功也不是失败，
   *   绝不能按错误渲染。判定权威见 `lib/toolDisplay.ts` 的
   *   `CAPABILITY_DECLARATION_STATUSES`。
   */
  toolStatus?: 'running' | 'completed' | 'failed' | 'degraded' | 'capability_declared';
  toolArgs?: string;      // 工具参数摘要，可展开查看
  toolResult?: string;    // 工具结果摘要，可展开查看
  toolCategory?: ToolCategory; // 工具分类，用于差异化 UI 渲染
  fromCache?: boolean;    // 是否来自工具结果缓存
  /** 工具是否经 fallback 降级成功；由 `tool_result.status === 'degraded'` 派生。 */
  toolDegraded?: boolean;
  /** 降级前的主工具名。 */
  fallbackFrom?: string;
  /** 实际执行的 fallback 工具名。 */
  fallbackTo?: string;
  /*
   * 这里此前还有六个 inspect 归因字段（`ttftMs` / `latencyMs` / `model` / `tier` /
   * `cachedInputTokens` / `usageAttribution`），由 `ATTRIBUTE_USAGE_TO_STEP` 从
   * `usage_update` 逐调用写进来。它们服务的是「圆圈 i」归因面 —— 那个面从未建成，
   * `ThinkingChain` 一次都没读过它们，且不打算建：后端六个键停发，reducer 与这六个字段
   * 一并删除。逐调用的这些数仍然落在后端 `run_llm_calls` 里，属内部可观测。
   */
}

export interface ChatSession {
  id: string;
  title: string;
  status: SessionStatus;
  lastMessagePreview: string;
  createdAt: Date;
  updatedAt: Date;
}

export interface SSEEvent {
  type:
    | 'chat_start'
    | 'thinking'
    | 'agent_progress'   // 深度模式 Worker 中间结果（批量，展示在调研过程区）
    | 'agent_thinking'   // Worker ReAct 推理文本（流式 token，追加到当前 thinking step）
    | 'tool_start'       // Worker 工具调用开始
    | 'tool_result'      // Worker 工具调用完成
    | 'chat_chunk'       // 最终输出增量（仅 synthesizer 或 fast_answer 直接回答）
    | 'chat_complete'
    | 'delivery_ready'   // Deep Research 唯一正式交付边界：一次发送完整不可变 Bundle
    | 'run_terminal'     // delivery_ready 之后的持久化终态
    | 'run_cancelled'
    | 'run_failed'       // 运行终态失败（产品流可解释错误）
    | 'approval_gate_raised'
    | 'error'
    | 'usage_update'      // 运行中逐次 LLM 调用计量（台账层落库后下发）
    | 'context_report'    // 六层记忆按 token 预算装配的上下文报告
    | 'context_compaction' // 完整压缩摘要与约束的会话时间线事件
    | 'trip_summary_card' // 由专用 LLM 在稳定工作流边界生成的旅行摘要
    | 'guided_intake'
    | 'route_confirmation'
    | 'synthesis_start';  // Synthesizer 开始 LLM 调用前的过渡信号
  message_id?: string;
  session_id?: string;
  run_id?: string;
  agent_name?: string;
  content?: string;
  show_content?: string;
  step_name?: string;
  message?: string;
  /** chat_complete 专用：这一轮被输入安全策略拒绝（run 终态 failed，但不是系统故障）。 */
  guard_blocked?: boolean;
  run_status?: string;
  gate?: string;
  guided_intake?: GuidedIntakeState;
  route_decision?: RouteDecision;
  // tool_start / tool_result 专用
  tool_name?: string;
  tool_call_id?: string;
  args_summary?: string;
  summary?: string;
  category?: string;   // 工具分类（internal/search/data/calculation/other）
  from_cache?: boolean; // 是否来自缓存
  /**
   * 降级前的主工具名 / 实际执行的 fallback 工具名。
   *
   * 这里**没有** `degraded` 布尔，也没有 `audit_id`：这一步是怎么结束的只由
   * `status` 说一次，`degraded` 只是它的第二种说法；`audit_id` 是内部标识，而检查面
   * 不印 ids。两个都不再下发。
   */
  fallback_from?: string;
  fallback_to?: string;
  // 计时：步边界事件带 ts_ms（服务端单调钟相对 run 起点毫秒），
  // 工具完成事件带 duration_ms（服务端实测耗时）。
  ts_ms?: number;
  duration_ms?: number;
  /**
   * 失败码（`reason_code` / `error_code` / `publish_failure_reason_code`）**不在这个
   * 模型里**。它们是内部归因，没有任何客户端读者，而记录它们的地方是 TripRun
   * 那一行（`transition_status(terminal_reason_code=…)`）—— 运维读的是那里。用户从这一帧
   * 需要的是 `message` 那一句话。
   */
  summary_card?: {
    headline: string;
    summary: string;
    facts: Array<{ label: string; value: string; state: 'confirmed' | 'default' | 'deferred' }>;
    priorities: string[];
    current_focus: string;
    next_milestone: string | null;
    compact_line: string;
    requires_user_confirmation: boolean;
  };
  // delivery_ready / run_terminal：Bundle 与 manifest 必须指向同一原子版本。
  event_seq?: number;
  bundle_id?: string;
  manifest?: DeliveryRevisionManifest;
  bundle?: PublicDeliveryBundle;
  /**
   * 两类事件共用这个字段名（后端就是这么发的）：
   * - `run_terminal`：终态只有 `'completed'`；
   * - `tool_result`：Tool Gateway 写下的 ToolExecutionStatus 值，是这一轮工具结论的**唯一
   *   权威**。**不要**在旁边再下发一个布尔 `success`：它把三值真相（成功 / 失败 / 能力判定）
   *   压成两值，日期能力判定会被前端读成 Provider 失败。四态映射见
   *   `lib/toolDisplay.ts::thinkingStepStatusFromToolResult`。
   */
  status?: 'completed' | ToolExecutionStatus;
  // chat_complete 事件专用：后端剥离 JSON 数据块后的最终正文
  // 用于覆盖流式累积内容，防止 JSON 泄露到用户可见区域
  final_content?: string;
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
    kind: InformationAnnotationKind;
    label: string;
    detail: string;
  }>;
  mode?: string;
  event_id?: string;
  event_type?: string;
  occurred_at?: string;
  key_constraints?: string[];
  node?: string;
  timestamp?: string;
  payload?: Record<string, unknown>;
  // usage_update 事件专用：逐次 LLM 调用计量（audit-safe：只有计数与成本）。
  // 逐调用归因那六个（tier / provider / model / cached_input_tokens /
  // latency_ms / ttft_ms）不在这里 —— 那个归因面明确不建，后端已停发。
  agent?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  cost_usd?: number | null;
  estimated?: boolean;
  // context_report 事件专用：这一轮真的进了 prompt 的那三段（后端载荷是 snake_case）。
  referenced_sections?: Array<{ key?: unknown; items?: unknown }>;
  compaction?: {
    triggered: boolean;
  };
  // chat_complete / run_cancelled 事件专用：run 级成本汇总（终结时一次）
  run_cost_summary?: RunCostSummary | null;
}

export interface PlanGateStep {
  step: number;
  agents: string[];
  /** 各 agent 的任务全文（后端完整下发，不截断）。 */
  tasks?: Record<string, string>;
}

/**
 * 计划批准门决策动作：首轮 approve/edit/supplement，二轮 approve/cancel。
 *
 * 定义在 `types/api.ts`（线上形状那一层，`ChatRequest.gate_decision` 也在那里读它），
 * 这里只是 re-export —— 同一个联合此前在三处各写了一份。
 */
export type { PlanGateDecisionAction } from './api';

/**
 * 后端在计划门上声称「本轮必须遵守」的一条硬约束。
 *
 * 只留下这一屏真的会说出来的两样：识别用的 `constraintId`，和给旅行者读的
 * `summary`（后端 `panels/constraint.py::_public_summary` 已经把它写成人话）。
 * 后端 `_build_plan_gate_payload` 这一帧也不再下 `category` 与
 * `enforcement_scope` —— 那两个是系统词（`budget_cap` / `composition`），照
 * UX Copy 口径不上屏；后端发下来而两端都不显示，正是这条缺陷本身的形状。
 */
export interface PlanGateMustObey {
  constraintId: string;
  summary: string;
}

export interface PlanApprovalGate {
  gate: string;
  runId: string;
  status: 'pending' | 'cancelled';
  revision: number;
  revisionLimit: number;
  revisionLimitReached: boolean;
  steps: PlanGateStep[];
  /** 后端生成的规范化计划全文（markdown）：编辑态预填此文本。 */
  planText: string;
  /** 这一轮可用的决策动作（后端下发）：首轮含 edit/supplement，二轮仅 approve/cancel。 */
  decisionOptions: PlanGateDecisionAction[];
  /**
   * 本轮必须遵守的硬约束（后端 `plan_gate.must_obey`）。用户要在批准之前看见它们。
   *
   * 这里此前**没有**这个字段，而整份 payload 落在一个 `rawPayload: Record<string, unknown>`
   * 里 —— 那个字段全仓除了类型声明和赋值那一行之外没有任何读者。计划门算好了
   * 「本轮必须遵守这几条」，一路投影到前端，然后被丢掉。`rawPayload` 随这次接线删除：
   * 一个没人读的兜底口袋，会让「下发了但没接」看起来像已经接上了。
   */
  mustObey: PlanGateMustObey[];
}
