import { useCallback, useRef } from 'react';
import { createSessionRuntimeSnapshot, useApp, type SessionRuntimeSnapshot } from '../context/AppContext';
import { api } from '../lib/api';
import { generateId } from '../lib/utils';
import { normalizeContextReport } from '../components/hallmark/normalize';
import { normalizeContextCompactionEvent } from '../lib/contextCompaction';
import { normalizeTripSummaryCard } from '../lib/tripSummaryCard';
import { normalizePlanApprovalGate } from '../lib/planApprovalGate';
import { hasResearchStarted, projectVisibleMessages } from '../lib/conversationFlow';
import type { ChatSession, FinalAnswerCitation, InformationAnnotation, Message, ThinkingStep} from '../types/chat';
import type { ControlledTripIdentity, SessionDetail, SessionSummary, TripRunStatus } from '../types/api';
import { tripRunRecoveryFromDetail } from '../types/api';
import { isPublicDeliveryBundle } from '../types/delivery';
import { stripAssistantControlBlocks } from '../lib/visibleContent';

const LAST_SESSION_KEY = 'sta_last_session_id';

function mapSummary(raw: SessionSummary): ChatSession {
  return {
    id: raw.session_id,
    title: raw.title,
    status: raw.status,
    lastMessagePreview: raw.last_message_preview || '',
    createdAt: new Date(raw.created_at),
    updatedAt: new Date(raw.updated_at),
  };
}

function mapThinkingSteps(rawSteps: SessionDetail['messages'][number]['thinking_steps']): ThinkingStep[] {
  if (!rawSteps) return [];
  return rawSteps.map((step) => ({
    id: step.id || generateId(),
    agentName: step.agent_name || '',
    content: step.content || '',
    stepName: step.step_name || '',
    timestamp: new Date(step.timestamp),
    endTime: step.end_time ? new Date(step.end_time) : undefined,
    // 服务端定格耗时：工具步 M1 持久化了 duration_ms，回放时映射为
    // serverDurationMs——刷新后历史步仍显示服务端实测耗时，而非客户端 endTime 差值。
    serverDurationMs: step.duration_ms ?? undefined,
    // 工具调用字段
    isToolCall: step.is_tool_call,
    toolName: step.tool_name,
    toolCallId: step.tool_call_id,
    toolStatus: step.tool_status,
    toolArgs: step.tool_args,
    toolResult: step.tool_result,
    toolCategory: step.tool_category as ThinkingStep['toolCategory'],
    fromCache: step.from_cache,
  }));
}

function mapCitations(raw: SessionDetail['messages'][number]['citations']): FinalAnswerCitation[] {
  return (raw || []).map((citation) => ({
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
}

function mapAnnotations(raw: SessionDetail['messages'][number]['annotations']): InformationAnnotation[] {
  return (raw || []).map((annotation) => ({
    annotationId: annotation.annotation_id,
    kind: annotation.kind,
    label: annotation.label,
    detail: annotation.detail,
  })).filter((annotation) => annotation.annotationId && annotation.label);
}

function mapMessage(raw: SessionDetail['messages'][number]): Message {
  const persistedContent = raw.content || '';
  const persistedDisplayContent = raw.display_content || persistedContent;
  const content = raw.role === 'assistant'
    ? stripAssistantControlBlocks(persistedContent)
    : persistedContent;
  const displayContent = raw.role === 'assistant'
    ? stripAssistantControlBlocks(persistedDisplayContent)
    : persistedDisplayContent;

  return {
    id: raw.id || generateId(),
    role: raw.role,
    content,
    displayContent,
    timestamp: new Date(raw.timestamp),
    type: raw.type,
    runId: raw.run_id,
    // `agent_name` / `step_name` / `task_type` 不再映射：消息上那三个字段全仓没有渲染
    // 读者（屏幕上的 agent 名与步名来自 `thinkingSteps[]`），实时那一半也已经删掉 ——
    // 只有回放这一半接着写，就成了「同一个字段一头有值一头没有」。
    thinkingSteps: mapThinkingSteps(raw.thinking_steps),
    // 会话历史加载：把持久化的 context_report 规范化挂回助手消息，刷新后印记仍在（§4.4）。
    contextReport: normalizeContextReport(raw.context_report) || undefined,
    contextCompaction: normalizeContextCompactionEvent(raw.context_compaction) || undefined,
    citations: mapCitations(raw.citations),
    annotations: mapAnnotations(raw.annotations),
  };
}

function projectMessagesForRunLifecycle(messages: Message[], status: TripRunStatus | null): Message[] {
  if (status === 'awaiting_input') return messages;
  return messages.map((message) => (
    message.type === 'waiting_approval'
      ? { ...message, type: 'normal' as const, streamCompleted: true }
      : message
  ));
}

function latestAssistantThinkingSteps(messages: Message[]): ThinkingStep[] {
  const latestWithSteps = [...messages]
    .reverse()
    .find((m) => m.role === 'assistant' && (m.thinkingSteps?.length ?? 0) > 0);
  return latestWithSteps?.thinkingSteps || [];
}

export function useSessionManager() {
  const { state, dispatch, activeStreamRef, currentStateRef, sessionRuntimeCacheRef } = useApp();
  const openSessionSeqRef = useRef(0);

  const cacheCurrentRuntimeIfStreaming = useCallback(
    (nextSessionId: string) => {
      const current = currentStateRef.current;
      const activeStream = activeStreamRef.current;
      const currentSessionId = current.currentSessionId;
      if (!currentSessionId || currentSessionId === nextSessionId) return;
      if (activeStream?.sessionId !== currentSessionId) return;
      sessionRuntimeCacheRef.current.set(currentSessionId, createSessionRuntimeSnapshot(current));
    },
    [activeStreamRef, currentStateRef, sessionRuntimeCacheRef]
  );

  const setLastSession = useCallback((sessionId: string | null) => {
    try {
      if (!sessionId) {
        localStorage.removeItem(LAST_SESSION_KEY);
      } else {
        localStorage.setItem(LAST_SESSION_KEY, sessionId);
      }
    } catch {
      // ignore storage errors
    }
  }, []);

  const refreshSessions = useCallback(async (): Promise<ChatSession[]> => {
    const rawSessions = await api.listSessions();
    const sessions = rawSessions.map(mapSummary);
    dispatch({ type: 'SET_SESSIONS', payload: sessions });
    return sessions;
  }, [dispatch]);

  const openSession = useCallback(
    async (sessionId: string) => {
      const seq = openSessionSeqRef.current + 1;
      openSessionSeqRef.current = seq;

      const currentBeforeOpen = currentStateRef.current;
      if (currentBeforeOpen.currentSessionId === sessionId && activeStreamRef.current?.sessionId === sessionId) {
        setLastSession(sessionId);
        return;
      }

      cacheCurrentRuntimeIfStreaming(sessionId);

      const cachedRuntime = sessionRuntimeCacheRef.current.get(sessionId);
      const activeStream = activeStreamRef.current;
      if (cachedRuntime && activeStream?.sessionId === sessionId) {
        currentStateRef.current = {
          ...currentStateRef.current,
          ...cachedRuntime,
          conversationEpoch: currentStateRef.current.conversationEpoch + 1,
        };
        dispatch({ type: 'RESTORE_SESSION_RUNTIME', payload: cachedRuntime });
        setLastSession(sessionId);
        return;
      }

      const [detail, runListResult] = await Promise.all([
        api.getSessionDetail(sessionId),
        api.listTripRuns({ sessionId, mode: 'deep', limit: 1 }).catch(() => null),
      ]);
      if (seq !== openSessionSeqRef.current) return;
      cacheCurrentRuntimeIfStreaming(sessionId);

      // fast_answer / destination_discovery 可以有内部执行记录，但不是用户的规划 TripRun。
      const latestRun = runListResult?.runs[0] || null;
      const [latestRunDetail, currentBundleResult] = latestRun
        ? await Promise.all([
            api.getTripRunDetail(latestRun.run_id).catch(() => null),
            api.getCurrentDeliveryBundle(latestRun.run_id, sessionId)
              .then((bundle) => (
                isPublicDeliveryBundle(bundle) && bundle.manifest.run_id === latestRun.run_id
                  ? { bundle, failed: false }
                  : { bundle: null, failed: true }
              ))
              .catch(() => ({ bundle: null, failed: true })),
          ])
        : [null, { bundle: null, failed: false }];
      if (seq !== openSessionSeqRef.current) return;
      const pendingRunChoice = latestRunDetail?.state.pending_user_choice;
      const currentTripRunStatus = latestRunDetail?.run.status ?? null;
      const hasCancelledChoice = pendingRunChoice?.read_only === true
        && pendingRunChoice?.terminal_status === 'cancelled';

      let messages = detail.messages.map(mapMessage);
      let restoredPlanGate = null;
      if (pendingRunChoice && latestRun && currentTripRunStatus === 'awaiting_input') {
        const choiceType = String(pendingRunChoice?.type || '');
        if (choiceType === 'approval_gate' && pendingRunChoice?.gate === 'plan') {
          const payload = pendingRunChoice.payload && typeof pendingRunChoice.payload === 'object'
            ? pendingRunChoice.payload as Record<string, unknown>
            : {};
          restoredPlanGate = normalizePlanApprovalGate({
            run_id: latestRun.run_id,
            gate: 'plan',
            payload,
          }, hasCancelledChoice ? 'cancelled' : 'pending');
        }
      }
      const latestSummaryCard = [...detail.messages]
        .reverse()
        .map((message) => normalizeTripSummaryCard(message.trip_summary_card))
        .find((card) => card !== null) || null;
      const researchStarted = hasResearchStarted(messages);
      const restoredStepMessages = researchStarted ? projectVisibleMessages(messages) : messages;
      const restoredIdentity = latestRunDetail?.controlled_trip_identity ?? null;
      const deliveryBundle = currentBundleResult.bundle;
      const deliveryBundleLoadState: SessionRuntimeSnapshot['deliveryBundleLoadState'] = deliveryBundle
        ? { status: 'ready', message: null }
        : currentTripRunStatus === 'completed' && currentBundleResult.failed
          ? { status: 'error', message: '暂时无法加载这趟旅行的正式结果，请稍后重试。' }
          : { status: 'idle', message: null };
      const restoredRuntime: SessionRuntimeSnapshot = {
        conversationEpoch: currentStateRef.current.conversationEpoch,
        currentSessionId: sessionId,
        currentTripRunId: latestRun?.run_id ?? null,
        currentTripRunStatus,
        currentTripRunRecovery: tripRunRecoveryFromDetail(latestRunDetail),
        tripRunSource: latestRun ? 'live' : 'none',
        tripRunRefreshKey: currentStateRef.current.tripRunRefreshKey,
        currentMessages: projectMessagesForRunLifecycle(messages, currentTripRunStatus),
        thinkingSteps: latestAssistantThinkingSteps(restoredStepMessages),
        deliveryBundle,
        deliveryBundleLoadState,
        tripSummaryCard: latestSummaryCard,
        planApprovalGate: restoredPlanGate,
        pendingGuidedIntake: null,
        pendingRouteConfirmation: null,
        lastRouteDecision: null,
        controlledTripIdentity: restoredIdentity as ControlledTripIdentity | null,
        runCostLive: null,
        runCostSummary: null,
        isStreaming: false,
        isSynthesizing: false,
        splitViewActive: deliveryBundle !== null,
        canvasOpen: deliveryBundle !== null,
        canvasFullscreen: false,
        mobileCanvasOpen: false,
        activeDayIndex: null,
        activeItemId: null,
        activePlaceId: null,
        inputMode: 'normal',
      };
      dispatch({ type: 'RESTORE_SESSION_RUNTIME', payload: restoredRuntime });

      setLastSession(sessionId);
    },
    [activeStreamRef, cacheCurrentRuntimeIfStreaming, currentStateRef, dispatch, sessionRuntimeCacheRef, setLastSession]
  );

  const initializeSessions = useCallback(async () => {
    try {
      const sessions = await refreshSessions();
      if (sessions.length === 0) {
        // 首次进入本来就是空会话。不要用一次迟到的初始化结果重挂载首页表单，
        // 否则用户已经键入的地点或自然语言会被清空。
        return;
      }
      if (state.activeView !== 'chat') {
        return;
      }

      // 启动只恢复「用户显式停在那儿」的那一段会话，也就是 LAST_SESSION_KEY 记着的
      // 那一条；记录不在、或它指向的会话已被删除，就停在新建行程那一屏。
      //
      // **不要**加 `targetId ??= sessions[0].id` 那种兜底（取 updated_at 最新的一条）：它会让
      // 「新建行程」这个动作无法兑现 —— 侧栏的新建会 setLastSession(null) 把记录清掉，而下一次
      // 启动的兜底又按「最近改过的」把同一段捡回来，用户明确表达过的「我不要停在那儿」被一条
      // 默认值覆盖，半途中断的那一段就成了永久的开机首屏。
      // 恢复的依据只有一处，就是那条记录本身。
      let targetId: string | null = null;
      try {
        const stored = localStorage.getItem(LAST_SESSION_KEY);
        if (stored && sessions.some((s) => s.id === stored)) {
          targetId = stored;
        }
      } catch {
        // ignore storage errors
      }
      if (!targetId) {
        return;
      }

      await openSession(targetId);
    } catch {
      // 初始化失败时保留当前空会话。CLEAR_CHAT 会递增 conversationEpoch 并重挂载
      // 首页规划器，从而清空用户已经输入或选中的出发地。
    }
  }, [openSession, refreshSessions, state.activeView]);

  const deleteSession = useCallback(
    async (sessionId: string) => {
      await api.deleteSession(sessionId);
      dispatch({ type: 'REMOVE_SESSION', payload: sessionId });

      if (state.currentSessionId === sessionId) {
        const sessions = await refreshSessions();
        if (sessions.length > 0) {
          await openSession(sessions[0].id);
        } else {
          dispatch({ type: 'CLEAR_CHAT' });
          setLastSession(null);
        }
      }
    },
    [dispatch, openSession, refreshSessions, setLastSession, state.currentSessionId]
  );

  /**
   * 会话重命名：乐观更新——先落新标题，成功后用后端权威标题收口（截断可能改字），
   * 失败回滚到原标题并抛出错误交给调用方提示。空标题在 UI 层即当作取消，不会走到这里。
   */
  const renameSession = useCallback(
    async (sessionId: string, nextTitle: string) => {
      const trimmed = nextTitle.trim();
      if (!trimmed) return;

      const previousTitle = state.sessions.find((s) => s.id === sessionId)?.title ?? '';
      if (trimmed === previousTitle) return;

      dispatch({ type: 'RENAME_SESSION', payload: { id: sessionId, title: trimmed } });
      try {
        const updated = await api.renameSession(sessionId, trimmed);
        // 后端权威标题收口（可能因 20 字上限被截断）。
        dispatch({ type: 'RENAME_SESSION', payload: { id: sessionId, title: updated.title } });
      } catch (err) {
        dispatch({ type: 'RENAME_SESSION', payload: { id: sessionId, title: previousTitle } });
        throw err instanceof Error ? err : new Error(String(err));
      }
    },
    [dispatch, state.sessions]
  );

  return {
    refreshSessions,
    openSession,
    initializeSessions,
    deleteSession,
    renameSession,
    setLastSession,
  };
}
