import { useCallback } from 'react';
import { useApp } from '../context/AppContext';
import { api } from '../lib/api';
import { stopOutcomeFromError } from '../lib/stopOutcome';
import { isTripRunStatus } from '../types/api';

/**
 * 终止本次运行（协作式取消）：先请求后端 cancel，再断开本地流。
 *
 * 单击立即生效（无二段确认）；若后端不接受取消，UI 明确显示「仅本地断流」，
 * 避免把 awaiting_input 误标成已停止。底部 composer 的停止键与「行程登机牌」
 * 进度区的停止键共用这同一套语义——取消逻辑只此一份，不在两处各写一遍而产生分叉。
 */
export function useStopRun() {
  const { state, dispatch, activeStreamAbortRef } = useApp();

  const stopRun = useCallback(async () => {
    const runId = state.currentTripRunId;
    const hadPendingDecision = state.currentTripRunStatus === 'awaiting_input';
    let cancelAccepted = !runId;
    let cancelMessage = '';
    if (runId) {
      try {
        const result = await api.controlTripRun(runId, {
          action: 'cancel',
          session_id: state.currentSessionId,
        });
        cancelAccepted = result.accepted;
        cancelMessage = result.message || '';
        if (result.accepted && isTripRunStatus(result.status)) {
          dispatch({ type: 'SET_TRIP_RUN_STATUS', payload: result.status });
        }
      } catch (err) {
        // 「来晚了」不是「没停下来」：运行已经结束时服务端会答 409 并带上当前状态。
        const outcome = stopOutcomeFromError(err);
        cancelAccepted = outcome.stopped;
        cancelMessage = outcome.message ?? '';
        if (outcome.status) {
          dispatch({ type: 'SET_TRIP_RUN_STATUS', payload: outcome.status });
        }
      }
    }

    if (activeStreamAbortRef.current) {
      activeStreamAbortRef.current.abort();
      activeStreamAbortRef.current = null;
    }
    // stopRun 负责 dispatch SET_STREAMING=false，useSendMessage 的 finally 会跳过重复 dispatch
    dispatch({ type: 'SET_STREAMING', payload: false });
    if (cancelAccepted && hadPendingDecision) {
      dispatch({ type: 'MARK_PENDING_DECISIONS_CANCELLED' });
    }
    dispatch({ type: 'SET_INPUT_MODE', payload: cancelAccepted ? 'stopped' : 'normal' });

    // 将最后一条 AI 消息标记为已中断
    const msgs = state.currentMessages;
    const lastMsg = msgs[msgs.length - 1];
    if (lastMsg?.role === 'assistant' && !hadPendingDecision) {
      dispatch({
        type: 'UPDATE_MESSAGE_BY_ID',
        payload: cancelAccepted
          ? { id: lastMsg.id, type: 'interrupted', streamCompleted: true }
          : {
              id: lastMsg.id,
              type: 'error',
              content: cancelMessage || '已断开本地流，但后端取消未确认。',
              displayContent: cancelMessage || '已断开本地流，但后端取消未确认。',
              streamCompleted: true,
            },
      });
    }
  }, [
    activeStreamAbortRef,
    state.currentMessages,
    state.currentTripRunId,
    state.currentTripRunStatus,
    state.currentSessionId,
    state.planApprovalGate,
    dispatch,
  ]);

  return { stopRun };
}
