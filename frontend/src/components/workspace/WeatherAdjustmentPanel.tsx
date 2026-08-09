import React from 'react';
import { AlertTriangle, ShieldCheck, X } from 'lucide-react';
import type { DeliveryMutationState } from '../../hooks/useDeliveryBundleMutation';
import { cn } from '../../lib/utils';
import type {
  PublicDeliveryBundle,
  PublicWeatherAdjustment,
  WorkspaceV2MutationOperation,
} from '../../types/delivery';

interface WeatherAdjustmentPanelProps {
  /**
   * Kept as a public-boundary assertion for callers.  A proposal contains all
   * product data this panel needs; raw impact operations and candidate packets
   * must not be reconstructed in the consumer UI.
   */
  bundle: PublicDeliveryBundle;
  proposal: PublicWeatherAdjustment;
  mutation: {
    state: DeliveryMutationState;
    previewOperation: (operation: WorkspaceV2MutationOperation, contextId: string) => Promise<boolean>;
    confirm: () => Promise<boolean>;
    cancel: () => void;
    retry: () => Promise<boolean>;
    busy: boolean;
  };
  onClose: () => void;
}

export const WeatherAdjustmentPanel: React.FC<WeatherAdjustmentPanelProps> = ({
  proposal,
  mutation,
  onClose,
}) => {
  const isThisProposal = mutation.state.pending?.slotId === proposal.proposal_id;
  const awaiting = isThisProposal && mutation.state.status === 'awaiting_confirmation';
  const message = isThisProposal ? mutation.state.message : null;
  const failed = isThisProposal && ['failed', 'conflict'].includes(mutation.state.status);
  const impactSummary = [
    proposal.time_delta_minutes ? `${Math.abs(proposal.time_delta_minutes)} 分钟时间变化` : null,
    proposal.cost_delta_cny ? `${proposal.cost_delta_cny > 0 ? '增加' : '减少'} ¥${Math.abs(proposal.cost_delta_cny)}` : null,
  ].filter(Boolean).join(' · ');

  return (
    <div data-testid={`weather-proposal-${proposal.proposal_id}`} className="mt-3 overflow-hidden rounded-card border border-accent/25 bg-accent-soft/35">
      <div className="flex items-start justify-between gap-3 px-4 pb-2 pt-4">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 text-xs font-semibold text-accent">
            {proposal.severity === 'high' ? <AlertTriangle size={14} aria-hidden /> : <ShieldCheck size={14} aria-hidden />}
            天气变化后的建议
          </p>
          <h4 className="mt-1 break-words text-sm font-semibold leading-6 text-ink">{proposal.summary}</h4>
          {impactSummary && <p className="mt-1 text-xs text-ink-muted">{impactSummary}</p>}
        </div>
        <button type="button" onClick={onClose} aria-label="收起天气调整" className="-mr-2 -mt-2 flex shrink-0 items-center justify-center rounded-card text-ink-muted hover:bg-panel hover:text-ink">
          <X size={16} aria-hidden />
        </button>
      </div>
      <div className="border-y border-stroke bg-panel/60 px-4 py-3">
        <p className="break-words text-xs leading-5 text-ink-secondary">
          会基于当前天气重新安排受影响的时间与衔接；确认后仍可从行程记录中撤销。
        </p>
      </div>
      <div className="px-4 py-3">
        {message && <p role={failed ? 'alert' : 'status'} className={cn('mb-3 break-words text-xs', failed ? 'text-error' : 'text-ink-secondary')}>{message}</p>}
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          {awaiting ? (
            <>
              <button type="button" disabled={mutation.busy} onClick={mutation.cancel} className="rounded-card px-3 text-xs font-semibold text-ink-secondary hover:bg-panel">返回</button>
              <button type="button" disabled={mutation.busy} onClick={() => void mutation.confirm()} className="rounded-card bg-accent px-4 text-xs font-semibold text-white hover:bg-[var(--color-accent-hover)] disabled:cursor-wait disabled:opacity-60">确认采用</button>
            </>
          ) : failed ? (
            <button type="button" disabled={mutation.busy} onClick={() => void mutation.retry()} className="rounded-card border border-stroke bg-panel px-4 text-xs font-semibold text-ink hover:bg-surface disabled:cursor-wait disabled:opacity-60">基于最新行程重试</button>
          ) : (
            <>
              <button type="button" disabled={mutation.busy} onClick={() => void mutation.previewOperation({ type: 'dismiss_weather_adjustment', proposal_id: proposal.proposal_id }, proposal.proposal_id)} className="rounded-card px-3 text-xs font-semibold text-ink-secondary hover:bg-panel disabled:cursor-wait disabled:opacity-60">暂不调整</button>
              <button type="button" disabled={mutation.busy} onClick={() => void mutation.previewOperation({ type: 'apply_weather_adjustment', proposal_id: proposal.proposal_id }, proposal.proposal_id)} className="rounded-card bg-accent px-4 text-xs font-semibold text-white hover:bg-[var(--color-accent-hover)] disabled:cursor-wait disabled:opacity-60">采用这些调整</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
};
