import React, { createContext, useContext, useReducer, type ReactNode } from 'react';
import type { Message, ThinkingStep, ChatSession, PlanApprovalGate, TripSummaryCard } from '../types/chat';
import type { ControlledTripIdentity, GuidedIntakeState, RouteDecision, RunCostSummary, TripRunStatus, UsageUpdateEvent } from '../types/api';
import type { PublicDeliveryBundle } from '../types/delivery';
import { readStoredActivePreset, writeStoredActivePreset } from '../lib/activePresetStorage';

/** 运行中成本累加器：由 usage_update 逐次累积，run 结束前作为「跳动」实时值展示。 */
export interface RunCostLive {
  runId: string;
  callCount: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  /** Settled 账本：每个 token 对应台账里落库的一行调用。 */
  totalTokens: number;
  /** 已知价格调用的成本累加（未命中价格的调用不计入）。 */
  totalCostUsd: number;
  /** 是否至少有一次调用报出了成本——false 时只展示 token。 */
  costKnown: boolean;
  estimatedCount: number;
  lastNode: string | null;
  lastAgent: string | null;
}

/**
 * 「我的行程」这一视图已删除（它和侧栏那列对话记录完全重叠）。
 * 一趟旅行的入口只剩侧栏那一条记录 —— `useSessionManager.openSession()` 打开会话时
 * 自己会把该会话最近那个深度调研 run 恢复成 `currentTripRunId` + `tripRunSource:'live'`，
 * 所以被删掉那一屏在 `openSession` 之后补发的那次 `SET_CURRENT_TRIP_RUN` 本来就是第二遍。
 */
export type ActiveView =
  | 'chat'
  | 'knowledge-base'
  | 'presets'
  | 'user-preferences';

const VALID_ACTIVE_VIEWS: ActiveView[] = [
  'chat',
  'knowledge-base',
  'presets',
  'user-preferences',
];

/** `?view=` 值是否是合法视图——URL ↔ 状态双向同步（useViewUrlSync）的守卫。 */
export function isActiveView(value: string | null | undefined): value is ActiveView {
  return value != null && VALID_ACTIVE_VIEWS.includes(value as ActiveView);
}

/** 首次加载时从 `?view=` 恢复侧栏视图，避免与 URL 同步 effect 竞态。 */
function getInitialActiveView(): ActiveView {
  if (typeof window === 'undefined') return 'chat';
  const v = new URLSearchParams(window.location.search).get('view');
  return isActiveView(v) ? v : 'chat';
}

export interface AppState {
  activeView: ActiveView;
  sidebarCollapsed: boolean;
  currentSessionId: string | null;
  /**
   * 会话展示纪元：切换会话（SET_MESSAGES 载入另一段历史）
   * 与新建会话（CLEAR_CHAT）时递增，作为线程容器 crossfade 的 key。
   * 流式中消息只走 ADD/UPDATE，纪元不动——同一会话内消息流入不触发重挂载（key 稳定）。
   * chat_start 的 SET_SESSION_ID（新会话首答中途落 id）不改纪元，故首答不断裂。
   */
  conversationEpoch: number;
  currentTripRunId: string | null;
  /** 后端持久化的当前 TripRun 生命周期；不得从消息或流连接状态反推。 */
  currentTripRunStatus: TripRunStatus | null;
  tripRunSource: 'none' | 'live';
  tripRunRefreshKey: number;
  sessions: ChatSession[];
  currentMessages: Message[];
  thinkingSteps: ThinkingStep[];
  /*
   * 这里此前还有 `traceEvents`（末 80 条）与 `traceSummary`。两个都删了：
   * 工作流 trace 只是**内部可观测**，思维链已经是同一次
   * run 的用户可见汇总，所以后端不再把 `trace_event` / `trace_summary` 投影出来，
   * 这两个字段也就没有生产方 —— 而它们此前也从来没有渲染读者：装进 state、进快照、
   * 被清空，一个像素都没到过。trace 事件仍然逐条落库（`trace.event`）。
   */
  /** 服务端原子持久化并通过 delivery_ready 确认的正式交付事实。 */
  deliveryBundle: PublicDeliveryBundle | null;
  deliveryBundleLoadState: {
    status: 'idle' | 'loading' | 'ready' | 'error';
    message: string | null;
  };
  /** 正式交付的展示形态；纯 UI 状态，不参与 workspace revision。 */
  deliverableView: 'interactive_itinerary' | 'full_report';
  tripSummaryCard: TripSummaryCard | null;
  planApprovalGate: PlanApprovalGate | null;
  pendingGuidedIntake: GuidedIntakeState | null;
  pendingRouteConfirmation: { rawInput: string; decision: RouteDecision } | null;
  lastRouteDecision: RouteDecision | null;
  controlledTripIdentity: ControlledTripIdentity | null;
  /** 运行中成本累加（usage_update 逐次累积）；run 结束由 runCostSummary 落定 */
  runCostLive: RunCostLive | null;
  /** run 级成本汇总（chat_complete / run_cancelled.run_cost_summary） */
  runCostSummary: RunCostSummary | null;
  isStreaming: boolean;
  isSynthesizing: boolean;
  splitViewActive: boolean;
  /** 停靠画布是否展开：有结构化面板时自动打开，可手动收起 / 全屏。 */
  canvasOpen: boolean;
  canvasFullscreen: boolean;
  /**
   * 移动画布可见性：`<lg` 下画布经 bottom sheet 呈现，
   * 开合与桌面 canvasOpen 解耦——面板到达时桌面自动展开，移动端则由用户经画布 pill
   * 或「在画布查看行程」显式唤起，避免流式中途 Sheet 突然盖住对话。
   */
  mobileCanvasOpen: boolean;
  activeDayIndex: number | null;
  /** 当前选中的行程项 id（itinerary ↔ map ↔ risk affected item 双向联动锚点） */
  activeItemId: string | null;
  /** 当前选中的现实地点；可来自已排入 item、待安排卡片或地图 marker。 */
  activePlaceId: string | null;
  inputMode: 'normal' | 'stopped';
  activePresetId: string | null;
  activePresetName: string | null;
  /**
   * 登机牌高亮刷新信号：计划卡里追加辅助信息 / 引导补充被提交时递增。行程登机牌监听此值，
   * 用一次高亮动画「刷一下」并展开票面——补充信息不进对话流，只在这张已确认的票上体现。
   */
  boardingPassFlash: number;
}

function canConfirmDeliveryBundle(
  current: PublicDeliveryBundle | null,
  candidate: PublicDeliveryBundle
): boolean {
  if (!current || current.manifest.run_id !== candidate.manifest.run_id) return true;
  if (current.manifest.bundle_id === candidate.manifest.bundle_id) return true;
  const currentVector = [
    current.manifest.workspace_revision,
    current.manifest.fact_data_revision,
    current.manifest.weather_data_revision,
  ];
  const candidateVector = [
    candidate.manifest.workspace_revision,
    candidate.manifest.fact_data_revision,
    candidate.manifest.weather_data_revision,
  ];
  if (candidateVector.some((revision, index) => revision < currentVector[index])) {
    return false;
  }
  return candidateVector.some((revision, index) => revision > currentVector[index]);
}

export type SessionRuntimeSnapshot = Pick<
  AppState,
  | 'conversationEpoch'
  | 'currentSessionId'
  | 'currentTripRunId'
  | 'currentTripRunStatus'
  | 'tripRunSource'
  | 'tripRunRefreshKey'
  | 'currentMessages'
  | 'thinkingSteps'
  | 'deliveryBundle'
  | 'deliveryBundleLoadState'
  | 'tripSummaryCard'
  | 'planApprovalGate'
  | 'pendingGuidedIntake'
  | 'pendingRouteConfirmation'
  | 'lastRouteDecision'
  | 'controlledTripIdentity'
  | 'runCostLive'
  | 'runCostSummary'
  | 'isStreaming'
  | 'isSynthesizing'
  | 'splitViewActive'
  | 'canvasOpen'
  | 'canvasFullscreen'
  | 'mobileCanvasOpen'
  | 'activeDayIndex'
  | 'activeItemId'
  | 'activePlaceId'
  | 'inputMode'
>;

export interface ActiveStreamHandle {
  token: string;
  sessionId: string | null;
  runId: string | null;
  controller: AbortController | null;
}

export function createSessionRuntimeSnapshot(state: AppState): SessionRuntimeSnapshot {
  return {
    conversationEpoch: state.conversationEpoch,
    currentSessionId: state.currentSessionId,
    currentTripRunId: state.currentTripRunId,
    currentTripRunStatus: state.currentTripRunStatus,
    tripRunSource: state.tripRunSource,
    tripRunRefreshKey: state.tripRunRefreshKey,
    currentMessages: state.currentMessages,
    thinkingSteps: state.thinkingSteps,
    deliveryBundle: state.deliveryBundle,
    deliveryBundleLoadState: state.deliveryBundleLoadState,
    tripSummaryCard: state.tripSummaryCard,
    planApprovalGate: state.planApprovalGate,
    pendingGuidedIntake: state.pendingGuidedIntake,
    pendingRouteConfirmation: state.pendingRouteConfirmation,
    lastRouteDecision: state.lastRouteDecision,
    controlledTripIdentity: state.controlledTripIdentity,
    runCostLive: state.runCostLive,
    runCostSummary: state.runCostSummary,
    isStreaming: state.isStreaming,
    isSynthesizing: state.isSynthesizing,
    splitViewActive: state.splitViewActive,
    canvasOpen: state.canvasOpen,
    canvasFullscreen: state.canvasFullscreen,
    mobileCanvasOpen: state.mobileCanvasOpen,
    activeDayIndex: state.activeDayIndex,
    activeItemId: state.activeItemId,
    activePlaceId: state.activePlaceId,
    inputMode: state.inputMode,
  };
}

export type AppAction =
  | { type: 'SET_ACTIVE_VIEW'; payload: ActiveView }
  | { type: 'TOGGLE_SIDEBAR' }
  | { type: 'SET_SIDEBAR_COLLAPSED'; payload: boolean }
  | { type: 'SET_SESSION_ID'; payload: string | null }
  | { type: 'SET_CURRENT_TRIP_RUN'; payload: { runId: string | null; source?: AppState['tripRunSource']; status?: TripRunStatus | null } }
  | { type: 'SET_TRIP_RUN_STATUS'; payload: TripRunStatus | null }
  | { type: 'CONFIRM_DELIVERY_BUNDLE'; payload: PublicDeliveryBundle }
  | { type: 'SET_DELIVERY_BUNDLE_LOAD_STATE'; payload: AppState['deliveryBundleLoadState'] }
  | { type: 'SET_DELIVERABLE_VIEW'; payload: AppState['deliverableView'] }
  | { type: 'BUMP_TRIP_RUN_REFRESH' }
  | { type: 'SET_SESSIONS'; payload: ChatSession[] }
  | { type: 'ADD_SESSION'; payload: ChatSession }
  | { type: 'REMOVE_SESSION'; payload: string }
  | { type: 'RENAME_SESSION'; payload: { id: string; title: string } }
  | { type: 'SET_MESSAGES'; payload: Message[] }
  | { type: 'RESTORE_SESSION_RUNTIME'; payload: SessionRuntimeSnapshot }
  | { type: 'ADD_MESSAGE'; payload: Message }
  | { type: 'INSERT_MESSAGE_BEFORE_ID'; payload: { message: Message; beforeId: string } }
  | { type: 'UPDATE_LAST_MESSAGE'; payload: Partial<Message> }
  | { type: 'RESOLVE_WAITING_MESSAGES' }
  | { type: 'UPDATE_MESSAGE_BY_ID'; payload: { id: string } & Partial<Message> }
  | { type: 'REMAP_MESSAGE_ID'; payload: { fromId: string; toId: string } }
  | { type: 'SET_THINKING_STEPS'; payload: ThinkingStep[] }
  | { type: 'ADD_THINKING_STEP'; payload: ThinkingStep }
  | { type: 'FINISH_LAST_THINKING_STEP'; payload: { endTime: Date; tsMs?: number } }
  | { type: 'APPEND_THINKING_CONTENT'; payload: { agentName: string; content: string } }
  | { type: 'UPDATE_LAST_TOOL_STEP'; payload: {
      toolCallId?: string;
      agentName?: string;
      toolName?: string;
      toolResult?: string;
      toolStatus?: ThinkingStep['toolStatus'];
      toolCategory?: ThinkingStep['toolCategory'];
      fromCache?: boolean;
      serverDurationMs?: number;
      toolDegraded?: boolean;
      fallbackFrom?: string;
      fallbackTo?: string;
    } }
  | { type: 'SET_TRIP_SUMMARY_CARD'; payload: TripSummaryCard | null }
  | { type: 'SET_PLAN_APPROVAL_GATE'; payload: PlanApprovalGate | null }
  | { type: 'SET_GUIDED_INTAKE'; payload: GuidedIntakeState | null }
  | { type: 'SET_ROUTE_CONFIRMATION'; payload: { rawInput: string; decision: RouteDecision } | null }
  | { type: 'SET_LAST_ROUTE_DECISION'; payload: RouteDecision | null }
  | { type: 'SET_CONTROLLED_TRIP_IDENTITY'; payload: ControlledTripIdentity | null }
  | { type: 'USAGE_UPDATE'; payload: UsageUpdateEvent }
  | { type: 'SET_RUN_COST_SUMMARY'; payload: RunCostSummary | null }
  | { type: 'CLEAR_TRACE' }
  | { type: 'SET_STREAMING'; payload: boolean }
  | { type: 'SET_SPLIT_VIEW'; payload: boolean }
  | { type: 'SET_CANVAS_OPEN'; payload: boolean }
  | { type: 'SET_CANVAS_FULLSCREEN'; payload: boolean }
  | { type: 'SET_MOBILE_CANVAS_OPEN'; payload: boolean }
  | { type: 'SET_ACTIVE_DAY'; payload: number | null }
  | { type: 'SET_ACTIVE_ITEM'; payload: string | null }
  | { type: 'SET_ACTIVE_PLACE'; payload: string | null }
  | { type: 'SET_INPUT_MODE'; payload: 'normal' | 'stopped' }
  | { type: 'MARK_PENDING_DECISIONS_CANCELLED' }
  | { type: 'REMOVE_MESSAGE_BY_ID'; payload: string }
  | { type: 'SET_ACTIVE_PRESET'; payload: { id: string; name: string } | null }
  | { type: 'SET_SYNTHESIZING'; payload: boolean }
  | { type: 'FLASH_BOARDING_PASS' }
  | { type: 'CLEAR_CHAT' };

const initialState: AppState = {
  activeView: getInitialActiveView(),
  sidebarCollapsed: typeof window !== 'undefined' && window.innerWidth < 1024,
  currentSessionId: null,
  conversationEpoch: 0,
  currentTripRunId: null,
  currentTripRunStatus: null,
  tripRunSource: 'none',
  tripRunRefreshKey: 0,
  sessions: [],
  currentMessages: [],
  thinkingSteps: [],
  deliveryBundle: null,
  deliveryBundleLoadState: { status: 'idle', message: null },
  deliverableView: 'interactive_itinerary',
  tripSummaryCard: null,
  planApprovalGate: null,
  pendingGuidedIntake: null,
  pendingRouteConfirmation: null,
  lastRouteDecision: null,
  controlledTripIdentity: null,
  runCostLive: null,
  runCostSummary: null,
  isStreaming: false,
  isSynthesizing: false,
  splitViewActive: false,
  canvasOpen: false,
  canvasFullscreen: false,
  mobileCanvasOpen: false,
  activeDayIndex: null,
  activeItemId: null,
  activePlaceId: null,
  inputMode: 'normal',
  // 旅行风格的选择跨刷新存活（见 lib/activePresetStorage.ts）。初始态直接读存储，
  // 而不是先渲染成「没选」再由某个 effect 补回来：后者会让 chip 闪一下，
  // 而且在补回来之前发出去的消息不带 preset_id。
  ...(() => {
    const stored = readStoredActivePreset();
    return {
      activePresetId: stored?.id ?? null,
      activePresetName: stored?.name ?? null,
    };
  })(),
  boardingPassFlash: 0,
};

export function appReducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case 'SET_ACTIVE_VIEW':
      return {
        ...state,
        activeView: action.payload,
        splitViewActive: action.payload === 'chat' ? state.splitViewActive : false,
      };
    case 'TOGGLE_SIDEBAR':
      return { ...state, sidebarCollapsed: !state.sidebarCollapsed };
    case 'SET_SIDEBAR_COLLAPSED':
      return { ...state, sidebarCollapsed: action.payload };
    case 'SET_SESSION_ID':
      return { ...state, currentSessionId: action.payload };
    case 'SET_CURRENT_TRIP_RUN': {
      const isDifferentRun = action.payload.runId !== state.currentTripRunId;
      return {
        ...state,
        currentTripRunId: action.payload.runId,
        currentTripRunStatus: action.payload.status !== undefined
          ? action.payload.status
          : (isDifferentRun ? null : state.currentTripRunStatus),
        tripRunSource: action.payload.source ?? (action.payload.runId ? state.tripRunSource : 'none'),
        tripSummaryCard: isDifferentRun ? null : state.tripSummaryCard,
        deliveryBundle: isDifferentRun ? null : state.deliveryBundle,
        deliveryBundleLoadState: isDifferentRun
          ? { status: 'idle', message: null }
          : state.deliveryBundleLoadState,
        deliverableView: isDifferentRun ? 'interactive_itinerary' : state.deliverableView,
      };
    }
    case 'SET_TRIP_RUN_STATUS': {
      const status = action.payload;
      const waitingAllowed = status === 'awaiting_input';
      const currentMessages = waitingAllowed
        ? state.currentMessages
        : state.currentMessages.map((message) => (
            message.type === 'waiting_approval'
              ? { ...message, type: 'normal' as const, streamCompleted: true }
              : message
          ));
      return {
        ...state,
        currentTripRunStatus: status,
        currentMessages,
        planApprovalGate: waitingAllowed ? state.planApprovalGate : null,
        inputMode: 'normal',
      };
    }
    case 'CONFIRM_DELIVERY_BUNDLE':
      if (!canConfirmDeliveryBundle(state.deliveryBundle, action.payload)) return state;
      return {
        ...state,
        currentTripRunId: action.payload.manifest.run_id,
        tripRunSource: 'live',
        deliveryBundle: action.payload,
        deliveryBundleLoadState: { status: 'ready', message: null },
        deliverableView: 'interactive_itinerary',
        sidebarCollapsed: true,
        splitViewActive: true,
        canvasOpen: true,
        canvasFullscreen: false,
        mobileCanvasOpen: false,
      };
    case 'SET_DELIVERY_BUNDLE_LOAD_STATE':
      return { ...state, deliveryBundleLoadState: action.payload };
    case 'SET_DELIVERABLE_VIEW':
      return { ...state, deliverableView: action.payload };
    case 'BUMP_TRIP_RUN_REFRESH':
      return { ...state, tripRunRefreshKey: state.tripRunRefreshKey + 1 };
    case 'SET_SESSIONS':
      return { ...state, sessions: action.payload };
    case 'ADD_SESSION':
      return { ...state, sessions: [action.payload, ...state.sessions] };
    case 'REMOVE_SESSION':
      return {
        ...state,
        sessions: state.sessions.filter((s) => s.id !== action.payload),
      };
    case 'RENAME_SESSION':
      return {
        ...state,
        sessions: state.sessions.map((s) =>
          s.id === action.payload.id ? { ...s, title: action.payload.title } : s
        ),
      };
    case 'SET_MESSAGES':
      // 载入另一段会话历史（openSession）= 一次会话切换：递增纪元触发一次 crossfade。
      // 流式追加走 ADD/UPDATE，不经此分支，纪元不动（key 稳定，不重挂载）。
      // 首屏初始化若仍是空会话则保持同一表单实例，避免用户刚输入的地点候选被异步会话
      // 列表刷新重挂载清空。
      if (state.currentMessages.length === 0 && action.payload.length === 0) return state;
      return { ...state, currentMessages: action.payload, tripSummaryCard: null, conversationEpoch: state.conversationEpoch + 1 };
    case 'RESTORE_SESSION_RUNTIME':
      return {
        ...state,
        ...action.payload,
        // 历史/缓存恢复始终从可执行行程进入；展示形态不是会话事实。
        deliverableView: 'interactive_itinerary',
        // 恢复运行中会话草稿也视为一次会话切换，保留当前全局导航/用户/会话列表。
        conversationEpoch: state.conversationEpoch + 1,
      };
    case 'ADD_MESSAGE':
      return {
        ...state,
        currentMessages: [...state.currentMessages, action.payload],
      };
    case 'INSERT_MESSAGE_BEFORE_ID': {
      if (state.currentMessages.some((message) => message.id === action.payload.message.id)) return state;
      const targetIndex = state.currentMessages.findIndex((message) => message.id === action.payload.beforeId);
      if (targetIndex < 0) {
        return { ...state, currentMessages: [...state.currentMessages, action.payload.message] };
      }
      return {
        ...state,
        currentMessages: [
          ...state.currentMessages.slice(0, targetIndex),
          action.payload.message,
          ...state.currentMessages.slice(targetIndex),
        ],
      };
    }
    case 'UPDATE_LAST_MESSAGE': {
      const msgs = [...state.currentMessages];
      if (msgs.length > 0) {
        msgs[msgs.length - 1] = { ...msgs[msgs.length - 1], ...action.payload };
      }
      return { ...state, currentMessages: msgs };
    }
    case 'RESOLVE_WAITING_MESSAGES': {
      // 陈旧等待气泡消解：用户提交 gate 决策、或 run 终态到达时，把 waiting_approval 消息翻回
      // 普通态并撤掉脉冲 chip —— 不留一个悬挂着的「正在等待…」。
      let touched = false;
      const msgs = state.currentMessages.map((m) => {
        if (m.type === 'waiting_approval') {
          touched = true;
          return { ...m, type: 'normal' as const };
        }
        return m;
      });
      return touched ? { ...state, currentMessages: msgs } : state;
    }
    case 'MARK_PENDING_DECISIONS_CANCELLED': {
      const messages = state.currentMessages.map((message) => (
        message.type === 'waiting_approval'
          ? { ...message, type: 'normal' as const, streamCompleted: true }
          : message
      ));
      return {
        ...state,
        currentMessages: messages,
        planApprovalGate: state.planApprovalGate
          ? { ...state.planApprovalGate, status: 'cancelled' }
          : null,
        inputMode: 'normal',
      };
    }
    case 'UPDATE_MESSAGE_BY_ID': {
      const { id, ...updates } = action.payload;
      return {
        ...state,
        currentMessages: state.currentMessages.map((m) =>
          m.id === id ? { ...m, ...updates } : m
        ),
      };
    }
    case 'REMAP_MESSAGE_ID':
      return {
        ...state,
        currentMessages: state.currentMessages.map((m) =>
          m.id === action.payload.fromId ? { ...m, id: action.payload.toId } : m
        ),
      };
    case 'SET_THINKING_STEPS':
      return { ...state, thinkingSteps: action.payload };
    case 'ADD_THINKING_STEP': {
      const newSteps = [...state.thinkingSteps, action.payload];
      const msgs = [...state.currentMessages];
      if (msgs.length > 0) {
        const lastMsg = msgs[msgs.length - 1];
        if (lastMsg.role === 'assistant') {
          msgs[msgs.length - 1] = { ...lastMsg, thinkingSteps: newSteps };
        }
      }
      return {
        ...state,
        thinkingSteps: newSteps,
        currentMessages: msgs,
      };
    }
    case 'FINISH_LAST_THINKING_STEP': {
      if (state.thinkingSteps.length === 0) return state;
      const newSteps = [...state.thinkingSteps];
      const lastIdx = newSteps.length - 1;
      const last = newSteps[lastIdx];
      const { endTime, tsMs: nextTsMs } = action.payload;

      // 服务端定格思考步：非工具步的耗时 = 下一步边界 ts_ms
      // 与本步 ts_ms 之差。即使该步已有 endTime，也允许补齐缺失的 serverDurationMs。
      const serverDurationMs =
        !last.isToolCall && last.serverDurationMs == null && nextTsMs != null && last.tsMs != null && nextTsMs >= last.tsMs
          ? nextTsMs - last.tsMs
          : last.serverDurationMs;
      newSteps[lastIdx] = { ...last, endTime: last.endTime ?? endTime, serverDurationMs };

      const msgs = [...state.currentMessages];
      if (msgs.length > 0) {
        const lastMsg = msgs[msgs.length - 1];
        if (lastMsg.role === 'assistant' && lastMsg.thinkingSteps) {
          msgs[msgs.length - 1] = { ...lastMsg, thinkingSteps: newSteps };
        }
      }

      return {
        ...state,
        thinkingSteps: newSteps,
        currentMessages: msgs,
      };
    }
    case 'APPEND_THINKING_CONTENT': {
      // 向最后一个 thinking step 追加推理文本（ReAct 流式推理）
      if (state.thinkingSteps.length === 0) {
        // 尚无 step，先创建一个
        const newStep: ThinkingStep = {
          id: `step-${Date.now()}`,
          agentName: action.payload.agentName,
          content: action.payload.content,
          stepName: '推理中',
          timestamp: new Date(),
        };
        const newSteps = [newStep];
        const msgs = [...state.currentMessages];
        if (msgs.length > 0) {
          const lastMsg = msgs[msgs.length - 1];
          if (lastMsg.role === 'assistant') {
            msgs[msgs.length - 1] = { ...lastMsg, thinkingSteps: newSteps };
          }
        }
        return { ...state, thinkingSteps: newSteps, currentMessages: msgs };
      }
      const newSteps = [...state.thinkingSteps];
      const lastIdx = newSteps.length - 1;
      const lastStep = newSteps[lastIdx];
      // 只有非工具调用步骤才追加内容
      if (!lastStep.isToolCall) {
        newSteps[lastIdx] = {
          ...lastStep,
          agentName: action.payload.agentName,
          content: lastStep.content + action.payload.content,
          stepName: '推理中',
        };
        const msgs = [...state.currentMessages];
        if (msgs.length > 0) {
          const lastMsg = msgs[msgs.length - 1];
          if (lastMsg.role === 'assistant' && lastMsg.thinkingSteps) {
            msgs[msgs.length - 1] = { ...lastMsg, thinkingSteps: newSteps };
          }
        }
        return { ...state, thinkingSteps: newSteps, currentMessages: msgs };
      }
      return state;
    }
    case 'UPDATE_LAST_TOOL_STEP': {
      // 更新工具调用步骤的状态（tool_result 事件触发）。并行 worker 可能同时调用
      // 同名工具，必须优先按 toolCallId 精确归因，不能只改“最后一步”。
      if (state.thinkingSteps.length === 0) return state;
      const newSteps = [...state.thinkingSteps];
      const lastIdx = (() => {
        if (action.payload.toolCallId) {
          const byId = newSteps.findIndex((step) => step.toolCallId === action.payload.toolCallId || step.id === action.payload.toolCallId);
          if (byId >= 0) return byId;
        }
        for (let i = newSteps.length - 1; i >= 0; i -= 1) {
          const step = newSteps[i];
          if (
            step.isToolCall &&
            step.toolStatus === 'running' &&
            (!action.payload.agentName || step.agentName === action.payload.agentName) &&
            (!action.payload.toolName || step.toolName === action.payload.toolName)
          ) {
            return i;
          }
        }
        return newSteps.length - 1;
      })();
      const lastStep = newSteps[lastIdx];
      if (lastStep.isToolCall) {
        newSteps[lastIdx] = {
          ...lastStep,
          endTime: new Date(),
          ...(action.payload.toolCallId !== undefined && { toolCallId: action.payload.toolCallId }),
          toolStatus: action.payload.toolStatus ?? 'completed',
          toolResult: action.payload.toolResult,
          ...(action.payload.toolCategory !== undefined && { toolCategory: action.payload.toolCategory }),
          ...(action.payload.fromCache !== undefined && { fromCache: action.payload.fromCache }),
          // 服务端实测耗时定格：tool_result.duration_ms 一到即为
          // 该工具步的权威耗时，替换本地估读。
          ...(action.payload.serverDurationMs != null && { serverDurationMs: action.payload.serverDurationMs }),
          ...(action.payload.toolDegraded !== undefined && { toolDegraded: action.payload.toolDegraded }),
          ...(action.payload.fallbackFrom !== undefined && { fallbackFrom: action.payload.fallbackFrom }),
          ...(action.payload.fallbackTo !== undefined && { fallbackTo: action.payload.fallbackTo }),
        };
        const msgs = [...state.currentMessages];
        if (msgs.length > 0) {
          const lastMsg = msgs[msgs.length - 1];
          if (lastMsg.role === 'assistant' && lastMsg.thinkingSteps) {
            msgs[msgs.length - 1] = { ...lastMsg, thinkingSteps: newSteps };
          }
        }
        return { ...state, thinkingSteps: newSteps, currentMessages: msgs };
      }
      return state;
    }
    case 'SET_TRIP_SUMMARY_CARD':
      return { ...state, tripSummaryCard: action.payload };
    case 'SET_PLAN_APPROVAL_GATE':
      return { ...state, planApprovalGate: action.payload };
    case 'USAGE_UPDATE': {
      const ev = action.payload;
      // 换 run 则重置累加器（并发/历史 run 不串账）
      const prev = state.runCostLive && state.runCostLive.runId === ev.run_id ? state.runCostLive : null;
      const inputTokens = ev.input_tokens || 0;
      const outputTokens = ev.output_tokens || 0;
      const next: RunCostLive = {
        runId: ev.run_id,
        callCount: (prev?.callCount ?? 0) + 1,
        totalInputTokens: (prev?.totalInputTokens ?? 0) + inputTokens,
        totalOutputTokens: (prev?.totalOutputTokens ?? 0) + outputTokens,
        totalTokens: (prev?.totalTokens ?? 0) + (ev.total_tokens ?? inputTokens + outputTokens),
        totalCostUsd: (prev?.totalCostUsd ?? 0) + (ev.cost_usd ?? 0),
        costKnown: (prev?.costKnown ?? false) || ev.cost_usd != null,
        estimatedCount: (prev?.estimatedCount ?? 0) + (ev.estimated ? 1 : 0),
        lastNode: ev.node ?? prev?.lastNode ?? null,
        lastAgent: ev.agent ?? prev?.lastAgent ?? null,
      };
      return { ...state, runCostLive: next };
    }
    case 'SET_RUN_COST_SUMMARY': {
      return { ...state, runCostSummary: action.payload };
    }
    case 'CLEAR_TRACE':
      return { ...state, tripSummaryCard: null, planApprovalGate: null, runCostLive: null, runCostSummary: null };
    case 'SET_STREAMING':
      return { ...state, isStreaming: action.payload, isSynthesizing: action.payload ? state.isSynthesizing : false };
    case 'SET_SYNTHESIZING':
      return { ...state, isSynthesizing: action.payload };
    case 'SET_SPLIT_VIEW':
      return { ...state, splitViewActive: action.payload };
    case 'SET_CANVAS_OPEN':
      return { ...state, canvasOpen: action.payload, canvasFullscreen: action.payload ? state.canvasFullscreen : false };
    case 'SET_CANVAS_FULLSCREEN':
      return { ...state, canvasFullscreen: action.payload, canvasOpen: action.payload ? true : state.canvasOpen };
    case 'SET_MOBILE_CANVAS_OPEN':
      return { ...state, mobileCanvasOpen: action.payload };
    case 'SET_ACTIVE_DAY':
      // 切换天时清掉选中项，避免高亮停留在其它天的行程项上
      return { ...state, activeDayIndex: action.payload, activeItemId: null, activePlaceId: null };
    case 'SET_ACTIVE_ITEM':
      return { ...state, activeItemId: action.payload };
    case 'SET_ACTIVE_PLACE':
      return { ...state, activePlaceId: action.payload };
    case 'SET_GUIDED_INTAKE':
      return { ...state, pendingGuidedIntake: action.payload };
    case 'SET_ROUTE_CONFIRMATION':
      return { ...state, pendingRouteConfirmation: action.payload };
    case 'SET_LAST_ROUTE_DECISION':
      return { ...state, lastRouteDecision: action.payload };
    case 'SET_CONTROLLED_TRIP_IDENTITY':
      return { ...state, controlledTripIdentity: action.payload };
    case 'SET_INPUT_MODE':
      return { ...state, inputMode: action.payload };
    case 'REMOVE_MESSAGE_BY_ID':
      return {
        ...state,
        currentMessages: state.currentMessages.filter((m) => m.id !== action.payload),
      };
    case 'SET_ACTIVE_PRESET':
      // 写存储放在 reducer 里，是为了让「选择」这件事只有一个落点：分派一次
      // SET_ACTIVE_PRESET，内存与存储同时到位。摊到各个组件的 effect 里去写，
      // 就又回到了「同一件事两处各写一份」——而这个 action 有四个分派点
      //（下拉里的官方 / 我的两处、chip 上的取消、风格库页删除时的清理）。
      writeStoredActivePreset(action.payload ? { id: action.payload.id, name: action.payload.name } : null);
      if (action.payload) {
        return { ...state, activePresetId: action.payload.id, activePresetName: action.payload.name };
      }
      return { ...state, activePresetId: null, activePresetName: null };
    case 'FLASH_BOARDING_PASS':
      return { ...state, boardingPassFlash: state.boardingPassFlash + 1 };
    case 'CLEAR_CHAT':
      return {
        ...state,
        // 新建会话：同样递增纪元，走一次进入空态的 crossfade。
        conversationEpoch: state.conversationEpoch + 1,
        currentMessages: [],
        thinkingSteps: [],
        deliveryBundle: null,
        deliveryBundleLoadState: { status: 'idle', message: null },
        deliverableView: 'interactive_itinerary',
        tripSummaryCard: null,
        planApprovalGate: null,
        pendingGuidedIntake: null,
        pendingRouteConfirmation: null,
        lastRouteDecision: null,
        controlledTripIdentity: null,
        runCostLive: null,
        runCostSummary: null,
        currentSessionId: null,
        currentTripRunId: null,
        currentTripRunStatus: null,
        tripRunSource: 'none',
        tripRunRefreshKey: state.tripRunRefreshKey + 1,
        splitViewActive: false,
        canvasOpen: false,
        canvasFullscreen: false,
        mobileCanvasOpen: false,
        activeDayIndex: null,
        activeItemId: null,
        activePlaceId: null,
        inputMode: 'normal',
        isStreaming: false,
        isSynthesizing: false,
      };
    default:
      return state;
  }
}

const AppContext = createContext<{
  state: AppState;
  dispatch: React.Dispatch<AppAction>;
  activeStreamAbortRef: React.MutableRefObject<AbortController | null>;
  activeStreamRef: React.MutableRefObject<ActiveStreamHandle | null>;
  currentStateRef: React.MutableRefObject<AppState>;
  sessionRuntimeCacheRef: React.MutableRefObject<Map<string, SessionRuntimeSnapshot>>;
} | null>(null);

export const AppProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [state, dispatch] = useReducer(appReducer, initialState);
  const activeStreamAbortRef = React.useRef<AbortController | null>(null);
  const activeStreamRef = React.useRef<ActiveStreamHandle | null>(null);
  const currentStateRef = React.useRef<AppState>(state);
  const sessionRuntimeCacheRef = React.useRef<Map<string, SessionRuntimeSnapshot>>(new Map());

  React.useEffect(() => {
    currentStateRef.current = state;
  }, [state]);

  React.useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth < 1024) {
        dispatch({ type: 'SET_SIDEBAR_COLLAPSED', payload: true });
      }
    };
    handleResize();
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // memoize provider value，避免每次 render 新建对象导致全树 useApp 重渲染。
  const value = React.useMemo(
    () => ({
      state,
      dispatch,
      activeStreamAbortRef,
      activeStreamRef,
      currentStateRef,
      sessionRuntimeCacheRef,
    }),
    [state]
  );

  return (
    <AppContext.Provider value={value}>
      {children}
    </AppContext.Provider>
  );
};

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useApp must be used within AppProvider');
  return ctx;
}
