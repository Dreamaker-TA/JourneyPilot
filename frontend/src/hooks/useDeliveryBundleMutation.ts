import React from 'react';
import { useApp } from '../context/AppContext';
import { ApiError, api } from '../lib/api';
import { conflictCurrentBundleId, readCurrentBundleAfterConflict } from '../lib/bundleConflict';
import {
  isPublicDeliveryBundle,
  type PublicDeliveryBundle,
  type WorkspaceV2MutationOperation,
  type WorkspaceV2MutationPreviewResponse,
  type WorkspaceV2MutationRequest,
  type WorkspaceV2MutationResponse,
} from '../types/delivery';

type MutationStatus =
  | 'idle'
  | 'previewing'
  | 'awaiting_confirmation'
  | 'saving'
  | 'confirming'
  | 'conflict'
  | 'failed'
  | 'saved';

interface PendingMutation {
  slotId: string;
  intent: 'selection' | 'weather' | 'itinerary';
  request: WorkspaceV2MutationRequest;
  preview: WorkspaceV2MutationPreviewResponse | null;
}

export interface DeliveryMutationState {
  status: MutationStatus;
  message: string | null;
  pending: PendingMutation | null;
}

function intentMessage(
  intent: PendingMutation['intent'],
  stage: 'saved' | 'saving' | 'confirming' | 'conflict' | 'lookup_failed' | 'apply_failed' | 'previewing' | 'preview_failed'
): string {
  const messages = {
    selection: {
      saved: '选择已更新',
      saving: '正在保存选择…',
      confirming: '正在确认这次选择是否已保存…',
      conflict: '行程刚刚有更新。已保留你的选择，请基于最新结果重试。',
      lookup_failed: '暂时无法确认这次选择，请重试。',
      apply_failed: '这个选项当前无法应用，请刷新后重试。',
      previewing: '正在检查这次选择…',
      preview_failed: '暂时无法检查这个选项，请重试。',
    },
    weather: {
      saved: '天气调整已保存',
      saving: '正在保存天气调整…',
      confirming: '正在确认天气调整是否已保存…',
      conflict: '行程刚刚有更新。请基于最新结果重新检查这项调整。',
      lookup_failed: '暂时无法确认这项调整，请重试。',
      apply_failed: '这项调整当前无法应用，请刷新后重试。',
      previewing: '正在检查天气调整…',
      preview_failed: '暂时无法检查这项调整，请重试。',
    },
    itinerary: {
      saved: '行程调整已保存',
      saving: '正在保存行程调整…',
      confirming: '正在确认这次行程调整是否已保存…',
      conflict: '行程刚刚有更新。已保留你的调整，请基于最新结果重试。',
      lookup_failed: '暂时无法确认这次行程调整，请重试。',
      apply_failed: '这项行程调整当前无法应用，请检查后重试。',
      previewing: '正在检查行程调整…',
      preview_failed: '暂时无法检查这项行程调整，请重试。',
    },
  } as const;
  return messages[intent][stage];
}

function requestFor(
  bundle: PublicDeliveryBundle,
  sessionId: string | null,
  operation: WorkspaceV2MutationOperation,
  mutationId: string
): WorkspaceV2MutationRequest {
  return {
    session_id: sessionId,
    mutation_id: mutationId,
    base_bundle_id: bundle.manifest.bundle_id,
    base_workspace_revision: bundle.manifest.workspace_revision,
    base_fact_data_revision: bundle.manifest.fact_data_revision,
    base_weather_data_revision: bundle.manifest.weather_data_revision,
    operation,
  };
}

export function useDeliveryBundleMutation(bundle: PublicDeliveryBundle) {
  const { dispatch, currentStateRef } = useApp();
  const [state, setState] = React.useState<DeliveryMutationState>({
    status: 'idle',
    message: null,
    pending: null,
  });
  const mounted = React.useRef(true);

  React.useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const acceptBundle = React.useCallback((next: PublicDeliveryBundle, runId: string) => {
    if (!isPublicDeliveryBundle(next) || next.manifest.run_id !== runId) {
      throw new Error('workspace operation returned an invalid public Delivery Bundle');
    }
    dispatch({ type: 'CONFIRM_DELIVERY_BUNDLE', payload: next });
  }, [dispatch]);

  const resyncAfterConflict = React.useCallback(async (error: unknown) => {
    // Only a conflict that names the current Bundle asks for a resync; a reused
    // mutation id keeps the on-screen Bundle as it is.
    if (!conflictCurrentBundleId(error)) return;
    const current = currentStateRef.current;
    try {
      const latest = await readCurrentBundleAfterConflict(
        bundle.manifest.run_id,
        current.currentSessionId
      );
      dispatch({ type: 'CONFIRM_DELIVERY_BUNDLE', payload: latest });
    } catch {
      // The conflict message already tells the user to retry from the latest
      // result; a failed resync must not overwrite it with a different error.
    }
  }, [bundle.manifest.run_id, currentStateRef, dispatch]);

  const accept = React.useCallback((result: WorkspaceV2MutationResponse, pending: PendingMutation) => {
    acceptBundle(result.bundle, bundle.manifest.run_id);
    if (mounted.current) {
      setState({
        status: 'saved',
        message: intentMessage(pending.intent, 'saved'),
        pending: { ...pending, preview: null },
      });
    }
  }, [acceptBundle, bundle.manifest.run_id]);

  const confirmRequest = React.useCallback(async (pending: PendingMutation) => {
    const runId = bundle.manifest.run_id;
    setState({ status: 'saving', message: intentMessage(pending.intent, 'saving'), pending });
    try {
      accept(await api.applyWorkspaceV2Mutation(runId, pending.request), pending);
      return true;
    } catch (error) {
      if (!mounted.current) return false;
      if (error instanceof ApiError && error.status === 409) {
        await resyncAfterConflict(error);
        if (mounted.current) {
          setState({
            status: 'conflict',
            message: intentMessage(pending.intent, 'conflict'),
            pending,
          });
        }
        return false;
      }
      if (!(error instanceof ApiError) || error.status >= 500 || error.status === 429) {
        setState({ status: 'confirming', message: intentMessage(pending.intent, 'confirming'), pending });
        try {
          accept(
            await api.getWorkspaceV2Mutation(
              runId,
              pending.request.mutation_id,
              pending.request.session_id
            ),
            pending
          );
          return true;
        } catch {
          if (mounted.current) {
            setState({ status: 'failed', message: intentMessage(pending.intent, 'lookup_failed'), pending });
          }
          return false;
        }
      }
      setState({ status: 'failed', message: intentMessage(pending.intent, 'apply_failed'), pending });
      return false;
    }
  }, [accept, bundle.manifest.run_id, resyncAfterConflict]);

  const preview = React.useCallback(async (
    operation: WorkspaceV2MutationOperation,
    slotId: string,
    mutationId = `mut_${crypto.randomUUID()}`,
    intent: PendingMutation['intent'] = 'selection'
  ) => {
    const current = currentStateRef.current.deliveryBundle;
    if (!current || current.manifest.run_id !== bundle.manifest.run_id) return false;
    const pending: PendingMutation = {
      slotId,
      intent,
      request: requestFor(
        current,
        currentStateRef.current.currentSessionId,
        operation,
        mutationId
      ),
      preview: null,
    };
    setState({ status: 'previewing', message: intentMessage(intent, 'previewing'), pending });
    try {
      const result = await api.previewWorkspaceV2Mutation(current.manifest.run_id, pending.request);
      if (!mounted.current) return false;
      const next = { ...pending, preview: result };
      if (!result.changed) {
        setState({ status: 'idle', message: null, pending: null });
        return true;
      }
      if (result.requires_confirmation) {
        setState({ status: 'awaiting_confirmation', message: null, pending: next });
        return false;
      }
      return confirmRequest(next);
    } catch (error) {
      if (!mounted.current) return false;
      if (error instanceof ApiError && error.status === 409) {
        await resyncAfterConflict(error);
        if (!mounted.current) return false;
        setState({
          status: 'conflict',
          message: intentMessage(intent, 'conflict'),
          pending,
        });
      } else {
        setState({ status: 'failed', message: intentMessage(intent, 'preview_failed'), pending });
      }
      return false;
    }
  }, [bundle.manifest.run_id, confirmRequest, currentStateRef, resyncAfterConflict]);

  const selectOption = React.useCallback((slotId: string, optionId: string) => preview({
    type: 'select_option',
    selection_slot_id: slotId,
    option_id: optionId,
  }, slotId), [preview]);

  const retry = React.useCallback(() => {
    const pending = state.pending;
    if (!pending) return Promise.resolve(false);
    return preview(pending.request.operation, pending.slotId, pending.request.mutation_id, pending.intent);
  }, [preview, state.pending]);

  return {
    state,
    selectOption,
    previewOperation: (operation: WorkspaceV2MutationOperation, contextId: string) => preview(
      operation,
      contextId,
      `mut_${crypto.randomUUID()}`,
      'weather'
    ),
    previewItineraryOperation: (operation: WorkspaceV2MutationOperation, contextId: string) => preview(
      operation,
      contextId,
      `mut_${crypto.randomUUID()}`,
      'itinerary'
    ),
    confirm: () => state.pending ? confirmRequest(state.pending) : Promise.resolve(false),
    cancel: () => setState({ status: 'idle', message: null, pending: null }),
    retry,
    busy: ['previewing', 'saving', 'confirming'].includes(state.status),
  };
}
