import { useCallback } from 'react';
import { appReducer, createSessionRuntimeSnapshot, useApp, type AppAction, type AppState } from '../context/AppContext';
import { api } from '../lib/api';
import { streamChat } from '../lib/sse';
import { generateId } from '../lib/utils';
import { normalizeContextReport } from '../components/hallmark/normalize';
import { normalizeContextCompactionEvent } from '../lib/contextCompaction';
import { normalizeTripSummaryCard } from '../lib/tripSummaryCard';
import { normalizePlanApprovalGate } from '../lib/planApprovalGate';
import { thinkingStepStatusFromToolResult } from '../lib/toolDisplay';
import { useSessionManager } from './useSessionManager';
import type { FinalAnswerCitation, InformationAnnotation, Message, SSEEvent, ThinkingStep } from '../types/chat';
import { isTripRunStatus, type ChatRequest, type ControlledTripIdentity, type JourneyRoute, type PlanGateDecisionAction, type RouteDecision, type UsageUpdateEvent } from '../types/api';
import { isPublicDeliveryBundle, type PublicDeliveryBundle } from '../types/delivery';

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(',')}]`;
  }
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(record[key])}`
    )).join(',')}}`;
  }
  return JSON.stringify(value);
}

/**
 * 提取核心发送/流式消息逻辑，供 InputArea 与对话线程共享使用。
 * 返回 sendMessage(text, signal?, options?) 函数。
 */
export function useSendMessage() {
  const { state, dispatch, activeStreamAbortRef, activeStreamRef, currentStateRef, sessionRuntimeCacheRef } = useApp();
  const { refreshSessions, setLastSession } = useSessionManager();

  const sendMessage = useCallback(
    async (
      text: string,
      signal?: AbortSignal,
      options?: {
        displayText?: string;
        assistantPendingLabel?: string;
        resumeRunId?: string | null;
        route?: JourneyRoute;
        routeDecision?: RouteDecision;
        controlledTripIdentity?: ControlledTripIdentity;
        // 动作表读 `types/api.ts::PlanGateDecisionAction`，不在这里第三次写一遍。
        gateDecision?: {
          action: PlanGateDecisionAction;
          content?: string;
        };
      }
    ) => {
      if (!text.trim() || state.isStreaming) return false;
      const ownedController = signal ? null : new AbortController();
      const effectiveSignal = signal ?? ownedController!.signal;
      if (ownedController) activeStreamAbortRef.current = ownedController;
      const streamToken = generateId();
      const startSessionId = state.currentSessionId || null;
      const startEpoch = state.conversationEpoch;
      const initialRunId = options?.resumeRunId || null;
      activeStreamRef.current = {
        token: streamToken,
        sessionId: startSessionId,
        runId: initialRunId,
        controller: ownedController,
      };

      const userMessage: Message = {
        id: generateId(),
        role: 'user',
        content: text,
        displayContent: options?.displayText || text,
        timestamp: new Date(),
      };

      dispatch({ type: 'ADD_MESSAGE', payload: userMessage });
      if (options?.controlledTripIdentity) {
        dispatch({ type: 'SET_CONTROLLED_TRIP_IDENTITY', payload: options.controlledTripIdentity });
      }
      // 用户回应 gate 决策 → 立刻消解悬挂中的等待气泡（不等下一帧 SSE）。
      if (options?.gateDecision) {
        dispatch({ type: 'RESOLVE_WAITING_MESSAGES' });
      }
      dispatch({ type: 'SET_STREAMING', payload: true });
      dispatch({ type: 'SET_THINKING_STEPS', payload: [] });
      dispatch({ type: 'CLEAR_TRACE' });

      const request: ChatRequest = {
        messages: [{
          role: userMessage.role,
          content: userMessage.content,
          message_id: userMessage.id,
          type: userMessage.type || 'normal',
        }],
        session_id: state.currentSessionId,
        run_id: initialRunId,
        route: options?.route ?? (initialRunId ? 'trip_refinement' : null),
        route_decision: options?.routeDecision ?? null,
        ...(options?.controlledTripIdentity
          ? { controlled_trip_identity: options.controlledTripIdentity }
          : {}),
        preset_id: state.activePresetId,
      };
      if (options?.gateDecision) {
        request.gate_decision = options.gateDecision;
      }
      const assistantTempId = generateId();
      const assistantMessage: Message = {
        id: assistantTempId,
        role: 'assistant',
        content: '',
        displayContent: '',
        timestamp: new Date(),
        pendingStatusText: options?.assistantPendingLabel,
      };
      dispatch({ type: 'ADD_MESSAGE', payload: assistantMessage });
      let assistantMessageId = assistantTempId;

      let hasReceivedChunk = false;
      // 首个真实思考/工具步到达后清掉入场态文案（如「正在开始并行调研」），否则
      // ThinkingChain 让 pendingStatusText 绝对优先，标题会一直冻结、看着像卡住。
      let pendingStatusCleared = !options?.assistantPendingLabel;
      let confirmedDeliveryBundle: PublicDeliveryBundle | null = null;
      let deliveryReadyEvent: SSEEvent | null = null;
      let deliveryTerminalEvent: SSEEvent | null = null;
      let deliveryFinalized = false;
      let deliveryProtocolError: string | null = null;
      let deliveryStreamInterrupted = false;
      let activeSessionId = state.currentSessionId || null;
      // 本次流式调用归属的 run（chat_start 落定）；in-flight 账本按此 runId 记账防串账。
      let activeRunId = initialRunId;
      let frameId: number | null = null;
      const pendingThinking: Array<{ agentName: string; content: string }> = [];
      const pendingUsage: UsageUpdateEvent[] = [];
      // In-flight 估算：本次流式输出累计字符数，随 chat_chunk
      // 的同一 rAF 批处理下发，不新增高频 setState。usage_update 到达时由 reducer 归零校准。
      let assistantUpdateQueued = false;
      let requestFailed = false;
      let intakeOnly = false;
      const routeStatus = (route?: JourneyRoute) => ({
        destination_discovery: '正在推荐目的地',
        trip_planning: '正在整理旅行信息',
        fast_answer: '正在回答问题',
        trip_refinement: '正在调整行程',
      }[route || 'fast_answer']);

      const isCurrentStream = () => activeStreamRef.current?.token === streamToken;
      const isStreamVisible = () => {
        if (!isCurrentStream()) return false;
        const current = currentStateRef.current;
        if (activeSessionId) {
          return current.currentSessionId === activeSessionId || (!startSessionId && current.conversationEpoch === startEpoch);
        }
        if (startSessionId) return current.currentSessionId === startSessionId;
        return current.conversationEpoch === startEpoch;
      };
      const reduceHiddenRuntime = (action: AppAction) => {
        const sessionId = activeSessionId || startSessionId;
        if (!sessionId) return;
        const cached = sessionRuntimeCacheRef.current.get(sessionId);
        if (!cached) return;
        const baseState: AppState = {
          ...currentStateRef.current,
          ...cached,
        };
        const nextState = appReducer(baseState, action);
        sessionRuntimeCacheRef.current.set(sessionId, createSessionRuntimeSnapshot(nextState));
      };
      const scopedDispatch = (action: AppAction) => {
        if (isStreamVisible()) {
          dispatch(action);
        } else {
          reduceHiddenRuntime(action);
        }
      };
      const updateActiveStreamHandle = (updates: Partial<{ sessionId: string | null; runId: string | null }>) => {
        if (!isCurrentStream()) return;
        activeStreamRef.current = {
          token: streamToken,
          sessionId: updates.sessionId !== undefined ? updates.sessionId : activeStreamRef.current?.sessionId ?? activeSessionId,
          runId: updates.runId !== undefined ? updates.runId : activeStreamRef.current?.runId ?? activeRunId,
          controller: activeStreamRef.current?.controller ?? ownedController,
        };
      };

      const flushFrame = () => {
        frameId = null;
        if (pendingThinking.length > 0) {
          const chunks = pendingThinking.splice(0);
          for (const chunk of chunks) {
            scopedDispatch({ type: 'APPEND_THINKING_CONTENT', payload: chunk });
          }
        }
        if (pendingUsage.length > 0) {
          const usageEvents = pendingUsage.splice(0);
          for (const usage of usageEvents) {
            // 只落运行中台账。此前这里还有一条 `ATTRIBUTE_USAGE_TO_STEP`，把
            // latency / ttft / model / tier 按 node/agent 归到思维链步上 ——
            // 那是一条从未建成的归因面的入口，写进 state 之后
            // 没有任何一处渲染它：六个字段后端已停发，这条 dispatch 与它的 reducer 一并删除。
            scopedDispatch({ type: 'USAGE_UPDATE', payload: usage });
          }
        }
        if (assistantUpdateQueued) {
          assistantUpdateQueued = false;
          scopedDispatch({
            type: 'UPDATE_LAST_MESSAGE',
            payload: {
              content: assistantMessage.content,
              displayContent: assistantMessage.displayContent,
            },
          });
        }
      };

      const scheduleFrame = () => {
        if (typeof window === 'undefined') {
          flushFrame();
          return;
        }
        if (frameId == null) {
          frameId = window.requestAnimationFrame(flushFrame);
        }
      };

      const flushFrameNow = () => {
        if (frameId != null && typeof window !== 'undefined') {
          window.cancelAnimationFrame(frameId);
        }
        flushFrame();
      };

      const failDeliveryProtocol = (message: string): never => {
        deliveryProtocolError = message;
        throw new Error(message);
      };

      const deliverySequence = (event: SSEEvent, label: string): number => {
        if (!Number.isSafeInteger(event.event_seq) || (event.event_seq ?? 0) <= 0) {
          return failDeliveryProtocol(`${label} 缺少有效的持久化事件序号，请重新加载本次行程。`);
        }
        return event.event_seq!;
      };

      const finalizeDeliveryIfComplete = () => {
        if (deliveryFinalized || !deliveryReadyEvent || !deliveryTerminalEvent) return;
        const readySequence = deliverySequence(deliveryReadyEvent, '旅行交付事件');
        const terminalSequence = deliverySequence(deliveryTerminalEvent, '旅行终态事件');
        if (
          terminalSequence <= readySequence
          || deliveryTerminalEvent.status !== 'completed'
          || deliveryTerminalEvent.run_id !== deliveryReadyEvent.run_id
          || deliveryTerminalEvent.bundle_id !== deliveryReadyEvent.bundle_id
        ) {
          failDeliveryProtocol('行程已更新，请重新加载本次行程。');
        }
        flushFrameNow();
        scopedDispatch({
          type: 'FINISH_LAST_THINKING_STEP',
          payload: { endTime: new Date(), tsMs: deliveryTerminalEvent.ts_ms },
        });
        if (!assistantMessage.content.trim()) {
          const readyMessage = '旅行方案已准备好，可在右侧查看完整报告与行程。';
          assistantMessage.content = readyMessage;
          assistantMessage.displayContent = readyMessage;
        }
        scopedDispatch({
          type: 'UPDATE_LAST_MESSAGE',
          payload: {
            content: assistantMessage.content,
            displayContent: assistantMessage.displayContent,
            streamCompleted: true,
          },
        });
        if (deliveryTerminalEvent.run_cost_summary !== undefined) {
          scopedDispatch({ type: 'SET_RUN_COST_SUMMARY', payload: deliveryTerminalEvent.run_cost_summary });
        }
        scopedDispatch({ type: 'SET_TRIP_RUN_STATUS', payload: 'completed' });
        scopedDispatch({ type: 'SET_PLAN_APPROVAL_GATE', payload: null });
        scopedDispatch({ type: 'RESOLVE_WAITING_MESSAGES' });
        scopedDispatch({ type: 'SET_SYNTHESIZING', payload: false });
        deliveryFinalized = true;
      };

      const acceptDeliveryReady = (event: SSEEvent) => {
        const sequence = deliverySequence(event, '旅行交付事件');
        const bundle = event.bundle;
        if (!isPublicDeliveryBundle(bundle)) {
          return failDeliveryProtocol('行程已更新，请重新加载本次行程。');
        }
        if (
          !event.manifest
          || event.bundle_id !== bundle.manifest.bundle_id
          || event.run_id !== bundle.manifest.run_id
          || event.manifest.bundle_id !== bundle.manifest.bundle_id
          || canonicalJson(event.manifest) !== canonicalJson(bundle.manifest)
        ) {
          failDeliveryProtocol('行程已更新，请重新加载本次行程。');
        }
        if (deliveryReadyEvent) {
          const existingSequence = deliverySequence(deliveryReadyEvent, '旅行交付事件');
          if (
            existingSequence !== sequence
            || deliveryReadyEvent.run_id !== event.run_id
            || deliveryReadyEvent.bundle_id !== event.bundle_id
            || canonicalJson(deliveryReadyEvent.bundle) !== canonicalJson(bundle)
          ) {
            failDeliveryProtocol('收到互相冲突的旅行交付事件，请重新加载本次行程。');
          }
          return;
        }
        deliveryReadyEvent = event;
        confirmedDeliveryBundle = bundle;
        flushFrameNow();
        scopedDispatch({ type: 'CONFIRM_DELIVERY_BUNDLE', payload: bundle });
        finalizeDeliveryIfComplete();
      };

      const acceptDeliveryTerminal = (event: SSEEvent) => {
        const sequence = deliverySequence(event, '旅行终态事件');
        if (event.status !== 'completed' || !event.bundle_id || !event.run_id) {
          failDeliveryProtocol('旅行交付未进入完整终态，请重新加载本次行程。');
        }
        if (deliveryTerminalEvent) {
          const existingSequence = deliverySequence(deliveryTerminalEvent, '旅行终态事件');
          if (
            existingSequence !== sequence
            || deliveryTerminalEvent.run_id !== event.run_id
            || deliveryTerminalEvent.bundle_id !== event.bundle_id
          ) {
            failDeliveryProtocol('收到互相冲突的旅行终态事件，请重新加载本次行程。');
          }
          return;
        }
        deliveryTerminalEvent = event;
        finalizeDeliveryIfComplete();
      };

      const recoverDeliveryTerminal = async () => {
        if (!deliveryReadyEvent || deliveryFinalized || !activeRunId) return;
        const readySequence = deliverySequence(deliveryReadyEvent, '旅行交付事件');
        let window = await api.getTripRunEventWindow(activeRunId, {
          sessionId: activeSessionId,
          afterSequence: readySequence,
        });
        if (window.window_expired) {
          window = await api.getTripRunEventWindow(activeRunId, {
            sessionId: activeSessionId,
            afterSequence: Math.max(0, window.replay_floor_sequence - 1),
          });
        }
        if (window.run_status !== 'completed') {
          throw new Error('旅行交付终态尚未持久化');
        }
        const matching = window.events.filter((event) => (
          event.event_type === 'run.terminal'
          && event.payload.status === 'completed'
          && event.payload.bundle_id === deliveryReadyEvent?.bundle_id
        ));
        if (matching.length !== 1) {
          throw new Error('旅行交付终态事件缺失或重复');
        }
        const terminal = matching[0];
        acceptDeliveryTerminal({
          type: 'run_terminal',
          run_id: terminal.run_id,
          event_id: terminal.event_id == null ? undefined : String(terminal.event_id),
          event_seq: terminal.sequence,
          bundle_id: String(terminal.payload.bundle_id),
          status: 'completed',
        });
      };

      try {
        await streamChat(
          request,
          (event) => {
            switch (event.type) {
              case 'guided_intake':
                if (event.guided_intake) {
                  intakeOnly = true;
                  scopedDispatch({ type: 'SET_LAST_ROUTE_DECISION', payload: event.guided_intake.route_decision });
                  scopedDispatch({ type: 'SET_ROUTE_CONFIRMATION', payload: null });
                  scopedDispatch({ type: 'SET_GUIDED_INTAKE', payload: event.guided_intake });
                  scopedDispatch({ type: 'REMOVE_MESSAGE_BY_ID', payload: assistantMessageId });
                }
                break;

              case 'route_confirmation':
                intakeOnly = true;
                if (event.route_decision) {
                  scopedDispatch({ type: 'SET_LAST_ROUTE_DECISION', payload: event.route_decision });
                  scopedDispatch({ type: 'SET_ROUTE_CONFIRMATION', payload: { rawInput: text, decision: event.route_decision } });
                }
                scopedDispatch({ type: 'REMOVE_MESSAGE_BY_ID', payload: assistantMessageId });
                break;

              case 'chat_start':
                if (event.route_decision) {
                  scopedDispatch({ type: 'SET_LAST_ROUTE_DECISION', payload: event.route_decision });
                  scopedDispatch({ type: 'SET_ROUTE_CONFIRMATION', payload: null });
                  scopedDispatch({ type: 'UPDATE_MESSAGE_BY_ID', payload: { id: assistantMessageId, pendingStatusText: routeStatus(event.route_decision.route) } });
                }
                if (event.session_id) {
                  activeSessionId = event.session_id;
                  updateActiveStreamHandle({ sessionId: event.session_id });
                  scopedDispatch({ type: 'SET_SESSION_ID', payload: event.session_id });
                  if (isStreamVisible()) setLastSession(event.session_id);
                }
                if (event.run_id) {
                  activeRunId = event.run_id;
                  updateActiveStreamHandle({ runId: event.run_id });
                  if (event.route_decision?.route === 'trip_planning' || event.route_decision?.route === 'trip_refinement') {
                    scopedDispatch({
                      type: 'SET_CURRENT_TRIP_RUN',
                      payload: {
                        runId: event.run_id,
                        source: 'live',
                        status: 'running',
                      },
                    });
                  }
                }
                if (event.message_id && event.message_id !== assistantMessageId) {
                  scopedDispatch({
                    type: 'REMAP_MESSAGE_ID',
                    payload: { fromId: assistantMessageId, toId: event.message_id },
                  });
                  assistantMessageId = event.message_id;
                  assistantMessage.id = event.message_id;
                }
                break;

              case 'thinking': {
                if (!pendingStatusCleared) {
                  pendingStatusCleared = true;
                  scopedDispatch({ type: 'UPDATE_MESSAGE_BY_ID', payload: { id: assistantMessageId, pendingStatusText: undefined } });
                }
                const now = new Date();
                // 服务端定格前一步：本步边界的 ts_ms 供 reducer 与
                // 上一步 ts_ms 求差，定格上一思考步的 serverDurationMs。
                scopedDispatch({ type: 'FINISH_LAST_THINKING_STEP', payload: { endTime: now, tsMs: event.ts_ms } });

                const step: ThinkingStep = {
                  id: generateId(),
                  agentName: event.agent_name || '',
                  content: event.content || '',
                  stepName: event.step_name || '',
                  timestamp: now,
                  tsMs: event.ts_ms,
                };
                scopedDispatch({ type: 'ADD_THINKING_STEP', payload: step });
                break;
              }

              case 'agent_progress': {
                if (!pendingStatusCleared) {
                  pendingStatusCleared = true;
                  scopedDispatch({ type: 'UPDATE_MESSAGE_BY_ID', payload: { id: assistantMessageId, pendingStatusText: undefined } });
                }
                // 深度模式 Worker 中间结果（批量）→ 添加到调研步骤展示区，不追加到主消息
                const now = new Date();
                scopedDispatch({ type: 'FINISH_LAST_THINKING_STEP', payload: { endTime: now, tsMs: event.ts_ms } });
                const progressStep: ThinkingStep = {
                  id: generateId(),
                  agentName: event.agent_name || '',
                  content: event.content || '',
                  stepName: event.step_name || '',
                  timestamp: now,
                  endTime: now,
                  tsMs: event.ts_ms,
                };
                scopedDispatch({ type: 'ADD_THINKING_STEP', payload: progressStep });
                break;
              }

              case 'agent_thinking': {
                // Worker ReAct 推理文本流式 token → 追加到当前 thinking step
                pendingThinking.push({
                  agentName: event.agent_name || '',
                  content: event.content || '',
                });
                scheduleFrame();
                break;
              }

              case 'tool_start': {
                if (!pendingStatusCleared) {
                  pendingStatusCleared = true;
                  scopedDispatch({ type: 'UPDATE_MESSAGE_BY_ID', payload: { id: assistantMessageId, pendingStatusText: undefined } });
                }
                // 工具调用开始 → 结束当前推理步骤，添加工具调用步骤
                const now = new Date();
                scopedDispatch({ type: 'FINISH_LAST_THINKING_STEP', payload: { endTime: now, tsMs: event.ts_ms } });
                const toolStep: ThinkingStep = {
                  id: event.tool_call_id || generateId(),
                  agentName: event.agent_name || '',
                  content: event.args_summary || '',
                  stepName: event.tool_name ? `调用 ${event.tool_name}` : '调用工具',
                  timestamp: now,
                  tsMs: event.ts_ms,
                  isToolCall: true,
                  toolName: event.tool_name,
                  toolCallId: event.tool_call_id,
                  toolStatus: 'running',
                  toolArgs: event.args_summary,
                  toolCategory: (event.category as ThinkingStep['toolCategory']) || 'other',
                  fromCache: event.from_cache || false,
                };
                scopedDispatch({ type: 'ADD_THINKING_STEP', payload: toolStep });
                break;
              }

              case 'tool_result': {
                // 工具调用完成 → 更新工具步骤。
                // 四态：failed | degraded（降级可用）| completed（主源成功）
                // | capability_declared（服务端判定该数据源答不了这个日期，只给参考资料）。
                // 唯一权威是帧上的 ToolExecutionStatus，映射见
                // lib/toolDisplay.ts::thinkingStepStatusFromToolResult。
                const toolStatus = thinkingStepStatusFromToolResult(event.status);
                const isDegraded = toolStatus === 'degraded';
                scopedDispatch({
                  type: 'UPDATE_LAST_TOOL_STEP',
                  payload: {
                    toolCallId: event.tool_call_id,
                    agentName: event.agent_name,
                    toolName: event.tool_name,
                    toolResult: event.summary,
                    toolStatus,
                    toolCategory: (event.category as ThinkingStep['toolCategory']) || 'other',
                    fromCache: event.from_cache || false,
                    // 服务端实测工具耗时定格。
                    serverDurationMs: event.duration_ms,
                    toolDegraded: isDegraded,
                    fallbackFrom: event.fallback_from,
                    fallbackTo: event.fallback_to,
                  },
                });
                break;
              }

              case 'synthesis_start': {
                scopedDispatch({ type: 'SET_SYNTHESIZING', payload: true });
                break;
              }

              case 'context_report': {
                // 上下文透镜：build_context 成功后随流下发一次，
                // 挂到当前助手消息（低频离散事件，直接派发，不进 rAF 批处理）。
                const report = normalizeContextReport(event);
                if (report) {
                  scopedDispatch({ type: 'UPDATE_LAST_MESSAGE', payload: { contextReport: report } });
                }
                break;
              }

              case 'context_compaction': {
                const compaction = normalizeContextCompactionEvent(event);
                if (!compaction) break;
                // The assistant placeholder already exists while the workflow
                // prepares its context. Insert the event before that placeholder
                // so later streaming UPDATE_LAST_MESSAGE calls still target the
                // assistant and the restored order is user → event → answer.
                scopedDispatch({
                  type: 'INSERT_MESSAGE_BEFORE_ID',
                  payload: {
                    beforeId: assistantMessageId,
                    message: {
                      id: compaction.id,
                      role: 'system',
                      content: '',
                      displayContent: '',
                      timestamp: new Date(compaction.occurredAt),
                      type: 'context_compaction',
                      contextCompaction: compaction,
                    },
                  },
                });
                break;
              }

              case 'trip_summary_card': {
                const card = normalizeTripSummaryCard(event);
                if (card) scopedDispatch({ type: 'SET_TRIP_SUMMARY_CARD', payload: card });
                break;
              }

              case 'usage_update': {
                // 逐次 LLM 调用计量：累积到运行中成本块（audit-safe：只有计数与成本）
                if (!event.run_id) break;
                const inputTokens = event.input_tokens ?? null;
                const outputTokens = event.output_tokens ?? null;
                const usage: UsageUpdateEvent = {
                  type: 'usage_update',
                  run_id: event.run_id,
                  message_id: event.message_id,
                  node: event.node ?? null,
                  agent: event.agent ?? null,
                  input_tokens: inputTokens,
                  output_tokens: outputTokens,
                  total_tokens: event.total_tokens ?? (inputTokens ?? 0) + (outputTokens ?? 0),
                  cost_usd: event.cost_usd ?? null,
                  estimated: !!event.estimated,
                };
                pendingUsage.push(usage);
                scheduleFrame();
                break;
              }

              case 'approval_gate_raised': {
                flushFrameNow();
                scopedDispatch({ type: 'FINISH_LAST_THINKING_STEP', payload: { endTime: new Date() } });
                const gate = normalizePlanApprovalGate(event);
                if (gate) {
                  scopedDispatch({ type: 'SET_PLAN_APPROVAL_GATE', payload: gate });
                  // run 的生命周期状态由后端说，这里**读**它而不是再写一遍字面量。
                  // 此前这行硬编码 `'awaiting_input'`，而同一帧上 `run_status` 已经写着
                  // 它 —— 一件事两个 owner，后端改了口径这里不会跟。
                  if (isTripRunStatus(event.run_status)) {
                    scopedDispatch({ type: 'SET_TRIP_RUN_STATUS', payload: event.run_status });
                  }
                }
                // 计划门的说明文字由计划卡（TripBriefPlanGate）独家承载，助手侧**只标类型、
                // 不写正文**。在这里写正文（「可在计划卡中追加信息…然后点确认」那类）会在取消
                // 路径上露出来：run 进终态后 reducer 把 waiting_approval 翻回 normal，气泡开始
                // 渲染 content，用户读到一句指向已经消失的按钮的指示。
                scopedDispatch({
                  type: 'UPDATE_LAST_MESSAGE',
                  payload: { type: 'waiting_approval' },
                });
                scopedDispatch({ type: 'SET_SYNTHESIZING', payload: false });
                break;
              }

              case 'chat_chunk': {
                if (!hasReceivedChunk) {
                  hasReceivedChunk = true;
                  scopedDispatch({ type: 'SET_SYNTHESIZING', payload: false });
                  scopedDispatch({ type: 'FINISH_LAST_THINKING_STEP', payload: { endTime: new Date(), tsMs: event.ts_ms } });
                }

                const content = event.show_content || event.content || '';
                assistantMessage.content += content;
                assistantMessage.displayContent += content;
                // 这里此前还从帧上抄 `agent_name` / `step_name` / `task_type` 进消息 ——
                // 三个键**投影层从来没放它们出门**，而消息上那三个字段全仓也没有渲染读者
                // （屏幕上的 agent 名与步名来自 `thinkingSteps[]`，那是 thinking / tool
                // 帧的事）。既没有生产也没有消费，两头一起删。
                assistantUpdateQueued = true;
                // In-flight 估算随流式字符累加：只统计可见正文字符数，
                // usage_update 到达时归零校准。走同一 rAF 批处理，不新增高频 setState。
                scheduleFrame();
                break;
              }

              case 'delivery_ready': {
                acceptDeliveryReady(event);
                break;
              }

              case 'run_terminal': {
                acceptDeliveryTerminal(event);
                break;
              }

              case 'chat_complete': {
                if (intakeOnly) break;
                flushFrameNow();
                scopedDispatch({ type: 'FINISH_LAST_THINKING_STEP', payload: { endTime: new Date(), tsMs: event.ts_ms } });
                const citations: FinalAnswerCitation[] = (event.citations || []).map((citation) => ({
                  citationId: citation.citation_id,
                  claimText: citation.claim_text,
                  sources: (citation.sources || []).map((source) => ({
                    title: source.title,
                    url: source.url,
                    sourceName: source.source_name,
                    snippet: source.snippet,
                    authorityLabel: source.authority_label,
                    retrievedAt: source.retrieved_at,
                  })),
                })).filter((citation) => citation.citationId && citation.sources.length > 0);
                const annotations: InformationAnnotation[] = (event.annotations || []).map((annotation) => ({
                  annotationId: annotation.annotation_id,
                  kind: annotation.kind,
                  label: annotation.label,
                  detail: annotation.detail,
                })).filter((annotation) => annotation.annotationId && annotation.label);
                assistantMessage.citations = citations;
                assistantMessage.annotations = annotations;
                // final_content：后端剥离 JSON 数据块后的最终正文，覆盖流式累积内容
                // 解决 fast_answer 流式输出中 JSON 尾块无法被 StreamingStripper 实时过滤的问题
                if (event.final_content && typeof event.final_content === 'string') {
                  assistantMessage.content = event.final_content;
                  assistantMessage.displayContent = event.final_content;
                  scopedDispatch({
                    type: 'UPDATE_LAST_MESSAGE',
                    payload: {
                      content: event.final_content,
                      displayContent: event.final_content,
                      citations,
                      annotations,
                      streamCompleted: true,
                    },
                  });
                } else {
                  // 无 final_content 覆盖也需定稿：正常收尾即置终态标记，收口流式小飞机。
                  scopedDispatch({
                    type: 'UPDATE_LAST_MESSAGE',
                    payload: { citations, annotations, streamCompleted: true },
                  });
                }
                if (event.run_id) {
                  scopedDispatch({
                    type: 'SET_CURRENT_TRIP_RUN',
                    payload: {
                      runId: event.run_id || state.currentTripRunId,
                      source: 'live',
                    },
                  });
                }
                if (event.run_cost_summary !== undefined) {
                  scopedDispatch({ type: 'SET_RUN_COST_SUMMARY', payload: event.run_cost_summary });
                }
                if (event.run_status !== 'awaiting_input') {
                  scopedDispatch({ type: 'SET_PLAN_APPROVAL_GATE', payload: null });
                  // 终态收口：run 已不等待输入 → 消解任何残留的等待气泡。
                  scopedDispatch({ type: 'RESOLVE_WAITING_MESSAGES' });
                }
                if (isTripRunStatus(event.run_status)) {
                  scopedDispatch({ type: 'SET_TRIP_RUN_STATUS', payload: event.run_status });
                }
                // 输入安全策略拒绝：run 终态是 failed，但这不是系统故障。标成
                // guard_blocked 后由告警卡承载（不给重试），而不是当成普通正文气泡。
                if (event.guard_blocked) {
                  scopedDispatch({
                    type: 'UPDATE_LAST_MESSAGE',
                    payload: {
                      type: 'guard_blocked',
                      streamCompleted: true,
                      pendingStatusText: undefined,
                    },
                  });
                }
                break;
              }

              case 'run_cancelled': {
                flushFrameNow();
                scopedDispatch({ type: 'FINISH_LAST_THINKING_STEP', payload: { endTime: new Date() } });
                const hadPendingDecision = Boolean(
                  currentStateRef.current.planApprovalGate
                  || currentStateRef.current.currentMessages.some(
                    (message) => message.type === 'waiting_approval'
                  )
                );
                scopedDispatch({ type: 'MARK_PENDING_DECISIONS_CANCELLED' });
                const finalContent =
                  typeof event.final_content === 'string' && event.final_content.trim()
                    ? event.final_content
                    : '已取消当前规划。已产生的阶段性内容会保留在本次 Trip Run 记录中。';
                assistantMessage.content = finalContent;
                assistantMessage.displayContent = finalContent;
                if (hadPendingDecision) {
                  scopedDispatch({
                    type: 'ADD_MESSAGE',
                    payload: {
                      id: `${assistantMessageId}-cancelled`,
                      role: 'assistant',
                      content: finalContent,
                      displayContent: finalContent,
                      timestamp: new Date(),
                      type: 'interrupted',
                      streamCompleted: true,
                    },
                  });
                } else {
                  scopedDispatch({
                    type: 'UPDATE_LAST_MESSAGE',
                    payload: {
                      content: finalContent,
                      displayContent: finalContent,
                      type: 'interrupted',
                      streamCompleted: true,
                    },
                  });
                }
                if (event.run_cost_summary !== undefined) {
                  scopedDispatch({ type: 'SET_RUN_COST_SUMMARY', payload: event.run_cost_summary });
                }
                scopedDispatch({ type: 'SET_TRIP_RUN_STATUS', payload: 'cancelled' });
                scopedDispatch({ type: 'SET_SYNTHESIZING', payload: false });
                scopedDispatch({ type: 'SET_INPUT_MODE', payload: 'normal' });
                scopedDispatch({ type: 'SET_STREAMING', payload: false });
                break;
              }

              case 'run_failed': {
                // 产品流终态失败必须可见，对齐 error 收敛。
                flushFrameNow();
                scopedDispatch({ type: 'FINISH_LAST_THINKING_STEP', payload: { endTime: new Date() } });
                const failMessage =
                  (typeof event.message === 'string' && event.message.trim())
                    ? event.message
                    : '旅行方案暂时无法生成，请稍后重试。';
                assistantMessage.content = failMessage;
                assistantMessage.displayContent = failMessage;
                scopedDispatch({
                  type: 'UPDATE_LAST_MESSAGE',
                  payload: {
                    content: failMessage,
                    displayContent: failMessage,
                    type: 'error',
                    streamCompleted: true,
                    pendingStatusText: undefined,
                  },
                });
                scopedDispatch({ type: 'SET_PLAN_APPROVAL_GATE', payload: null });
                scopedDispatch({ type: 'RESOLVE_WAITING_MESSAGES' });
                scopedDispatch({ type: 'SET_TRIP_RUN_STATUS', payload: 'failed' });
                scopedDispatch({ type: 'SET_SYNTHESIZING', payload: false });
                scopedDispatch({ type: 'SET_INPUT_MODE', payload: 'normal' });
                scopedDispatch({ type: 'SET_STREAMING', payload: false });
                scopedDispatch({ type: 'BUMP_TRIP_RUN_REFRESH' });
                break;
              }

              case 'error':
                flushFrameNow();
                if (!hasReceivedChunk) {
                  scopedDispatch({ type: 'FINISH_LAST_THINKING_STEP', payload: { endTime: new Date() } });
                }
                scopedDispatch({
                  type: 'UPDATE_LAST_MESSAGE',
                  payload: {
                    content: event.message || '发生错误',
                    displayContent: event.message || '发生错误',
                    type: 'error',
                    streamCompleted: true,
                  },
                });
                scopedDispatch({ type: 'SET_SYNTHESIZING', payload: false });
                scopedDispatch({ type: 'SET_STREAMING', payload: false });
                break;
            }
          },
          (error) => {
            if (confirmedDeliveryBundle && !deliveryProtocolError) {
              deliveryStreamInterrupted = true;
              return;
            }
            requestFailed = true;
            scopedDispatch({
              type: 'UPDATE_LAST_MESSAGE',
              payload: {
                content: `请求失败: ${error.message}`,
                displayContent: `请求失败: ${error.message}`,
                type: 'error',
                streamCompleted: true,
              },
            });
          },
          effectiveSignal
        );
        if ((deliveryStreamInterrupted || deliveryReadyEvent) && !deliveryFinalized && !deliveryProtocolError) {
          try {
            await recoverDeliveryTerminal();
          } catch {
            // Durable recovery monitor keeps polling this Run. Do not turn an
            // already confirmed immutable Bundle into an assistant error.
          }
        }
      } finally {
        flushFrameNow();
        // 若用户主动停止（signal 已 abort），handleStop 已经 dispatch 过 SET_STREAMING=false
        // 此处跳过，避免与 handleStop 产生竞态
        if (!effectiveSignal.aborted) {
          scopedDispatch({ type: 'SET_STREAMING', payload: false });
        }
        const completedWhileVisible = isStreamVisible();
        if (ownedController && activeStreamAbortRef.current === ownedController) {
          activeStreamAbortRef.current = null;
        }
        if (activeStreamRef.current?.token === streamToken) {
          activeStreamRef.current = null;
        }
        try {
          await refreshSessions();
        } catch {
          // ignore refresh failures
        }
        if (activeSessionId && completedWhileVisible) {
          setLastSession(activeSessionId);
        }
      }

      return !requestFailed && !effectiveSignal.aborted;
    },
    [
      activeStreamAbortRef,
      activeStreamRef,
      currentStateRef,
      dispatch,
      refreshSessions,
      sessionRuntimeCacheRef,
      setLastSession,
      state,
    ]
  );

  return { sendMessage };
}
