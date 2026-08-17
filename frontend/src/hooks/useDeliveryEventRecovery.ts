import React from 'react';
import { useApp } from '../context/AppContext';
import { api } from '../lib/api';
import { isPublicDeliveryBundle } from '../types/delivery';
import type { TripRunEventResponse, TripRunEventWindowResponse } from '../types/api';

const POLL_INTERVAL_MS = 750;
const MAX_RETRY_INTERVAL_MS = 5_000;
const TERMINAL_WITHOUT_DELIVERY = new Set(['failed', 'cancelled']);
const PAUSED_STATUSES = new Set(['awaiting_input', 'paused']);

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function orderedNewEvents(
  events: TripRunEventResponse[],
  afterSequence: number
): TripRunEventResponse[] {
  const bySequence = new Map<number, TripRunEventResponse>();
  for (const event of events) {
    if (event.sequence <= afterSequence || bySequence.has(event.sequence)) continue;
    bySequence.set(event.sequence, event);
  }
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence);
}

function validateWindow(
  window: TripRunEventWindowResponse,
  runId: string,
  requestedAfter: number
): void {
  if (window.run_id !== runId || window.requested_after_sequence !== requestedAfter) {
    throw new Error('TripRun event window identity mismatch');
  }
  if (
    window.latest_sequence < 0
    || window.next_after_sequence < requestedAfter
    || window.next_after_sequence > Math.max(requestedAfter, window.latest_sequence)
  ) {
    throw new Error('TripRun event window sequence mismatch');
  }
  if (window.window_expired && window.events.length > 0) {
    throw new Error('Expired TripRun event window must not replay partial events');
  }
}

/**
 * Durable recovery monitor for the current TripRun.
 *
 * SSE remains the low-latency process channel. This monitor owns recovery:
 * it consumes monotonic persisted event windows, ignores duplicate/out-of-order
 * records, and replaces an expired incremental window with the exact current
 * Delivery Bundle instead of attempting a partial UI merge.
 */
export function useDeliveryEventRecovery(): void {
  const { state, dispatch, currentStateRef } = useApp();
  const cursorByRun = React.useRef(new Map<string, number>());
  const readyRuns = React.useRef(new Set<string>());
  const finalizedRuns = React.useRef(new Set<string>());
  const runId = state.currentTripRunId;
  const sessionId = state.currentSessionId;

  React.useEffect(() => {
    if (!runId) return;
    if (finalizedRuns.current.has(runId)) {
      const current = currentStateRef.current;
      if (current.currentTripRunId === runId && current.deliveryBundle?.manifest.run_id === runId) {
        return;
      }
      finalizedRuns.current.delete(runId);
    }
    let active = true;
    let failures = 0;

    const syncCurrentBundle = async (expectedBundleId: string): Promise<boolean> => {
      const current = currentStateRef.current;
      if (current.currentTripRunId !== runId) return false;
      if (current.deliveryBundle?.manifest.bundle_id === expectedBundleId) return true;
      dispatch({
        type: 'SET_DELIVERY_BUNDLE_LOAD_STATE',
        payload: { status: 'loading', message: '正在恢复这趟旅行的正式结果…' },
      });
      // Product surface is current-only: no by-id history read.
      const bundle = await api.getCurrentDeliveryBundle(runId, sessionId);
      if (
        !isPublicDeliveryBundle(bundle)
        || bundle.manifest.run_id !== runId
        || bundle.manifest.bundle_id !== expectedBundleId
      ) {
        throw new Error('Current Delivery Bundle does not match the durable event window');
      }
      if (!active || currentStateRef.current.currentTripRunId !== runId) return false;
      dispatch({ type: 'CONFIRM_DELIVERY_BUNDLE', payload: bundle });
      return true;
    };

    const monitor = async () => {
      while (active) {
        const requestedAfter = cursorByRun.current.get(runId) ?? 0;
        try {
          const window = await api.getTripRunEventWindow(runId, {
            sessionId,
            afterSequence: requestedAfter,
          });
          if (!active) return;
          validateWindow(window, runId, requestedAfter);

          const events = window.window_expired
            ? []
            : orderedNewEvents(window.events, requestedAfter);
          if (events.some((event) => event.event_type === 'delivery.ready')) {
            readyRuns.current.add(runId);
          }
          if (window.run_status === 'completed') {
            readyRuns.current.add(runId);
          }
          const eventCursor = events.at(-1)?.sequence ?? requestedAfter;
          cursorByRun.current.set(
            runId,
            Math.max(eventCursor, window.next_after_sequence)
          );

          const mayReadFormalDelivery = Boolean(
            window.current_bundle_id
            && window.run_status === 'completed'
          );
          let deliverySynced = false;
          if (mayReadFormalDelivery) {
            deliverySynced = await syncCurrentBundle(window.current_bundle_id!);
          }

          if (failures > 0) {
            failures = 0;
            const load = currentStateRef.current.deliveryBundleLoadState;
            if (load.status === 'error' && load.message) {
              dispatch({
                type: 'SET_DELIVERY_BUNDLE_LOAD_STATE',
                payload: { status: 'idle', message: null },
              });
            }
          } else {
            failures = 0;
          }
          if (window.run_status === 'completed') {
            if (!window.current_bundle_id) {
              throw new Error('Completed TripRun has no current Delivery Bundle');
            }
            if (deliverySynced) {
              const latestAssistant = [...currentStateRef.current.currentMessages]
                .reverse()
                .find((message) => message.role === 'assistant');
              if (
                latestAssistant
                && !latestAssistant.streamCompleted
                && latestAssistant.type !== 'error'
              ) {
                const readyMessage = '旅行方案已准备好，可在右侧查看完整报告与行程。';
                dispatch({
                  type: 'UPDATE_MESSAGE_BY_ID',
                  payload: {
                    id: latestAssistant.id,
                    content: latestAssistant.content.trim() || readyMessage,
                    displayContent: latestAssistant.displayContent.trim() || readyMessage,
                    pendingStatusText: undefined,
                    streamCompleted: true,
                  },
                });
                dispatch({ type: 'SET_SYNTHESIZING', payload: false });
              }
            }
            finalizedRuns.current.add(runId);
            dispatch({ type: 'BUMP_TRIP_RUN_REFRESH' });
            return;
          }
          if (
            TERMINAL_WITHOUT_DELIVERY.has(window.run_status)
            || PAUSED_STATUSES.has(window.run_status)
          ) {
            return;
          }
          await wait(POLL_INTERVAL_MS);
        } catch {
          if (!active || currentStateRef.current.currentTripRunId !== runId) return;
          failures += 1;
          // 不必等 ready 才提示；未 ready 时早期弱提示，ready 后强调结果恢复。
          if (failures >= 2) {
            dispatch({
              type: 'SET_DELIVERY_BUNDLE_LOAD_STATE',
              payload: {
                status: 'error',
                message: readyRuns.current.has(runId)
                  ? '连接中断，正在从已保存的旅行结果恢复。'
                  : '连接不稳定，仍在同步进度…',
              },
            });
          }
          await wait(Math.min(POLL_INTERVAL_MS * 2 ** failures, MAX_RETRY_INTERVAL_MS));
        }
      }
    };

    void monitor();
    return () => { active = false; };
  }, [currentStateRef, dispatch, runId, sessionId]);
}

export const DeliveryEventRecovery: React.FC = () => {
  useDeliveryEventRecovery();
  return null;
};
