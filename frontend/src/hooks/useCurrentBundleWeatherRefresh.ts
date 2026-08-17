import React from 'react';
import { useApp } from '../context/AppContext';
import { ApiError, api } from '../lib/api';
import { apiErrorDetail } from '../lib/apiErrorDetail';
import { conflictCurrentBundleId, readCurrentBundleAfterConflict } from '../lib/bundleConflict';
import { isPublicDeliveryBundle } from '../types/delivery';

const MAX_REFRESH_ATTEMPTS = 3;

function retryableRefreshError(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status === 429 || error.status >= 500;
}

function waitForRetry(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export function useCurrentBundleWeatherRefresh(enabled: boolean) {
  const { state, dispatch, currentStateRef } = useApp();
  // 每个 bundle_id 只机会性刷一次天气。守卫按 bundle_id 记，寿命就**必须和 refresh_id 一样
  // 长** —— 工作区一关就清空它的话，画布收起 / 切到完整方案再切回来，就会带着同一个
  // idempotency key 再打一次请求，服务端只能靠 replay 兜住，两次真撞上还会 409。
  // 真的换了 Bundle 自然是新的 bundle_id，仍然会刷。
  const attemptedBundleIds = React.useRef(new Set<string>());
  const bundle = state.deliveryBundle;

  React.useEffect(() => {
    if (!enabled || !bundle || attemptedBundleIds.current.has(bundle.manifest.bundle_id)) return;
    const { manifest } = bundle;
    attemptedBundleIds.current.add(manifest.bundle_id);
    const refreshId = `weather_${crypto.randomUUID()}`;
    let active = true;

    const request = {
      session_id: state.currentSessionId,
      refresh_id: refreshId,
      base_bundle_id: manifest.bundle_id,
      base_workspace_revision: manifest.workspace_revision,
      base_fact_data_revision: manifest.fact_data_revision,
      base_weather_data_revision: manifest.weather_data_revision,
    };
    const refresh = async () => {
      for (let attempt = 1; attempt <= MAX_REFRESH_ATTEMPTS && active; attempt += 1) {
        try {
          const result = await api.refreshCurrentBundleWeather(manifest.run_id, request);
          if (!active || !isPublicDeliveryBundle(result.bundle)) return;
          attemptedBundleIds.current.add(result.bundle.manifest.bundle_id);
          const current = currentStateRef.current.deliveryBundle;
          if (!current || current.manifest.run_id !== manifest.run_id) return;
          if (result.bundle.manifest.bundle_id !== current.manifest.bundle_id) {
            dispatch({ type: 'CONFIRM_DELIVERY_BUNDLE', payload: result.bundle });
          }
          return;
        } catch (error: unknown) {
          if (!active) return;
          if (error instanceof ApiError && error.status === 409) {
            // Not every 409 is a lost race.  `bundle_contract_superseded` says the
            // stored result was written under a contract that does not describe it —
            // re-reading returns the same refusal, so an automatic resync here would
            // be a retry loop dressed up as recovery.  Discriminate on the code, not
            // on whether a bundle id happens to be present.
            if (apiErrorDetail(error).code === 'bundle_contract_superseded') return;
            // The refresh raced another writer: adopt the run's current Bundle
            // by reading it, since the conflict reports identity only.
            if (conflictCurrentBundleId(error)) {
              try {
                const current = await readCurrentBundleAfterConflict(
                  manifest.run_id,
                  state.currentSessionId
                );
                if (active) dispatch({ type: 'CONFIRM_DELIVERY_BUNDLE', payload: current });
              } catch {
                // An opportunistic on-open refresh must not surface an error:
                // the workspace keeps the Bundle it already has.
              }
            }
            return;
          }
          if (!retryableRefreshError(error) || attempt === MAX_REFRESH_ATTEMPTS) return;
          await waitForRetry(150 * 2 ** (attempt - 1));
        }
      }
    };
    void refresh();

    return () => {
      active = false;
    };
  }, [bundle, currentStateRef, dispatch, enabled, state.currentSessionId]);
}
