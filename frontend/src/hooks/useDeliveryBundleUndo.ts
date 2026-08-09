import React from 'react';
import { useApp } from '../context/AppContext';
import { ApiError, api } from '../lib/api';
import { conflictCurrentBundleId, readCurrentBundleAfterConflict } from '../lib/bundleConflict';
import {
  isPublicDeliveryBundle,
  type PublicDeliveryBundle,
  type WorkspaceV2UndoHead,
  type WorkspaceV2UndoRequest,
} from '../types/delivery';

type UndoStatus = 'loading' | 'ready' | 'saving' | 'failed';

export function useDeliveryBundleUndo(bundle: PublicDeliveryBundle) {
  const { dispatch, currentStateRef } = useApp();
  const [head, setHead] = React.useState<WorkspaceV2UndoHead | null>(null);
  const [status, setStatus] = React.useState<UndoStatus>('loading');
  const [message, setMessage] = React.useState<string | null>(null);
  const pending = React.useRef<WorkspaceV2UndoRequest | null>(null);

  const loadHead = React.useCallback(async () => {
    const current = currentStateRef.current;
    try {
      const next = await api.getWorkspaceV2UndoHead(
        bundle.manifest.run_id,
        current.userId,
        current.currentSessionId
      );
      setHead(next);
      setStatus('ready');
    } catch {
      setHead(null);
      setStatus('failed');
      setMessage('暂时无法读取可撤销的调整。');
    }
  }, [bundle.manifest.run_id, currentStateRef]);

  React.useEffect(() => {
    let active = true;
    setHead(null);
    setStatus('loading');
    setMessage(null);
    pending.current = null;
    const current = currentStateRef.current;
    void api.getWorkspaceV2UndoHead(
      bundle.manifest.run_id,
      current.userId,
      current.currentSessionId
    ).then((next) => {
      if (!active) return;
      setHead(next);
      setStatus('ready');
    }).catch(() => {
      if (!active) return;
      setStatus('failed');
      setMessage('暂时无法读取可撤销的调整。');
    });
    return () => { active = false; };
  }, [bundle.manifest.bundle_id, bundle.manifest.run_id, currentStateRef]);

  const undo = React.useCallback(async () => {
    const current = currentStateRef.current.deliveryBundle;
    if (!current || current.manifest.run_id !== bundle.manifest.run_id || !head?.mutation_id) return;
    const manifest = current.manifest;
    const request = pending.current ?? {
      user_id: currentStateRef.current.userId,
      session_id: currentStateRef.current.currentSessionId,
      undo_id: `undo_${crypto.randomUUID()}`,
      undo_of_mutation_id: head.mutation_id,
      base_bundle_id: manifest.bundle_id,
      base_workspace_revision: manifest.workspace_revision,
      base_fact_data_revision: manifest.fact_data_revision,
      base_weather_data_revision: manifest.weather_data_revision,
    };
    pending.current = request;
    setStatus('saving');
    setMessage('正在撤销…');
    try {
      const result = await api.undoWorkspaceV2Mutation(manifest.run_id, request);
      if (!isPublicDeliveryBundle(result.bundle) || result.bundle.manifest.run_id !== manifest.run_id) {
        throw new Error('undo returned an invalid Delivery Bundle');
      }
      pending.current = null;
      dispatch({ type: 'CONFIRM_DELIVERY_BUNDLE', payload: result.bundle });
      setStatus('ready');
      setMessage('已撤销，可再次撤销来恢复。');
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // Revision conflict and moved undo head both answer with identity only,
        // so the resync is a fresh read of the run's current Bundle.
        if (conflictCurrentBundleId(error)) {
          try {
            dispatch({
              type: 'CONFIRM_DELIVERY_BUNDLE',
              payload: await readCurrentBundleAfterConflict(
                manifest.run_id,
                currentStateRef.current.userId,
                currentStateRef.current.currentSessionId
              ),
            });
          } catch {
            // Keep the conflict message below: it already asks for a retry from
            // the latest result.
          }
        }
        pending.current = null;
        setMessage('行程刚刚有更新，请基于最新结果重试。');
        await loadHead();
        return;
      }
      setStatus('failed');
      setMessage('暂时无法确认是否已撤销，请重试。');
    }
  }, [bundle.manifest.run_id, currentStateRef, dispatch, head?.mutation_id, loadHead]);

  return {
    available: Boolean(head?.available && head.mutation_id),
    label: head?.label ?? '撤销上一步调整',
    status,
    message,
    undo,
    retryLoad: loadHead,
  };
}
