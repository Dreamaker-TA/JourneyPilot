import React from 'react';
import { Check, ChevronDown, ChevronUp, Loader2 } from 'lucide-react';
import { cn } from '../../lib/utils';
import type { DeliveryMutationState } from '../../hooks/useDeliveryBundleMutation';
import type {
  PublicDeliveryBundle,
  SelectionOption,
  SelectionSlot,
  WorkspaceV2MutationOperation,
} from '../../types/delivery';
import { TRIP_DOMAIN_PRESENTATION as SLOT_PRESENTATION } from '../../lib/tripDomains';

interface SelectionSlotCardProps {
  bundle: PublicDeliveryBundle;
  slot: SelectionSlot;
  mutation: {
    state: DeliveryMutationState;
    busy: boolean;
    selectOption: (slotId: string, optionId: string) => Promise<boolean>;
    confirm: () => Promise<boolean>;
    cancel: () => void;
    retry: () => Promise<boolean>;
    previewItineraryOperation: (operation: WorkspaceV2MutationOperation, contextId: string) => Promise<boolean>;
  };
  canonicalCard?: React.ReactNode;
}

type ReportSelection = NonNullable<PublicDeliveryBundle['report_projection']['document']>['selections'][number];

function reportSelectionFor(bundle: PublicDeliveryBundle, slot: SelectionSlot): ReportSelection | null {
  return bundle.report_projection.document?.selections.find(
    (selection) => selection.selection_slot_id === slot.selection_slot_id
  ) ?? null;
}

function optionTitle(slot: SelectionSlot, option: SelectionOption, report: ReportSelection | null): string {
  const named = report?.options.find((item) => item.option_id === option.option_id)?.name;
  if (named) return named;
  return `${SLOT_PRESENTATION[slot.slot_type].plan} ${option.rank}`;
}

function optionFacts(option: SelectionOption, report: ReportSelection | null): string[] {
  const reportOption = report?.options.find((item) => item.option_id === option.option_id);
  return reportOption?.comparison_facts ?? option.comparison_facts;
}

/** workspace 四态中的过程态：禁用选择，避免刷新中 mutation 冲突。 */
function isSlotInFlight(status: SelectionSlot['status']): boolean {
  return status === 'researching' || status === 'refreshing';
}

function SlotStatusBanner({ status }: { status: SelectionSlot['status'] }) {
  if (status === 'researching') {
    return (
      <p
        role="status"
        data-testid="selection-slot-status"
        data-status="researching"
        className="mt-2 flex items-center gap-1.5 rounded-card border border-accent/20 bg-[var(--color-accent-soft)] px-3 py-2 text-xs text-ink-secondary"
      >
        <Loader2 size={12} className="shrink-0 animate-spin text-accent" />
        正在补充可选方案…
      </p>
    );
  }
  if (status === 'refreshing') {
    return (
      <p
        role="status"
        data-testid="selection-slot-status"
        data-status="refreshing"
        className="mt-2 flex items-center gap-1.5 rounded-card border border-warning/25 bg-warning/10 px-3 py-2 text-xs text-ink-secondary"
      >
        <Loader2 size={12} className="shrink-0 animate-spin text-warning" />
        正在更新可选方案…
      </p>
    );
  }
  if (status === 'needs_user_decision') {
    return (
      <p
        role="status"
        data-testid="selection-slot-status"
        data-status="needs_user_decision"
        className="mt-2 rounded-card bg-surface px-3 py-2 text-xs text-ink-secondary"
      >
        请选择一个已通过行程条件检查的方案。
      </p>
    );
  }
  return null;
}

function OptionRow({
  slot,
  option,
  report,
  disabled,
  onSelect,
}: {
  slot: SelectionSlot;
  option: SelectionOption;
  report: ReportSelection | null;
  disabled: boolean;
  onSelect: () => void;
}) {
  const title = optionTitle(slot, option, report);
  const facts = optionFacts(option, report);
  return (
    <article
      data-testid={`selection-option-${option.option_id}`}
      className={cn(
        'min-w-0 rounded-card border px-3 py-3',
        option.selected ? 'border-accent/35 bg-[var(--color-accent-soft)]' : 'border-stroke bg-panel',
        disabled && !option.selected && 'opacity-70'
      )}
    >
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <h4 className="break-words text-sm font-semibold text-ink">{title}</h4>
            {option.recommended && <span className="rounded-label bg-surface px-2 py-0.5 text-[11px] font-semibold text-ink-secondary">首选</span>}
            {option.availability_status === 'needs_confirmation' && <span className="rounded-label border border-stroke px-2 py-0.5 text-[11px] font-semibold text-ink-secondary">需要确认</span>}
          </div>
          {facts.length > 0 && <p className="mt-1 break-words text-[11px] leading-5 text-ink-muted">{facts.join(' · ')}</p>}
        </div>
        <button
          type="button"
          aria-pressed={option.selected}
          disabled={disabled || option.selected}
          onClick={onSelect}
          className={cn(
            'inline-flex shrink-0 items-center justify-center gap-1 rounded-card px-3 py-1.5 text-xs font-semibold transition-colors disabled:cursor-default',
            option.selected ? 'bg-accent text-white' : 'border border-stroke bg-surface text-ink hover:border-accent/35 hover:text-accent disabled:opacity-50'
          )}
        >
          {option.selected && <Check size={13} />}
          {option.selected ? '当前选择' : '选择'}
        </button>
      </div>
      {option.selection_reasons.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs leading-5 text-ink-secondary">
          {option.selection_reasons.slice(0, 3).map((reason) => <li key={reason} className="break-words">· {reason}</li>)}
        </ul>
      )}
      {option.tradeoff && <p className="mt-2 break-words border-t border-stroke pt-2 text-[11px] leading-5 text-ink-muted">取舍：{option.tradeoff}</p>}
    </article>
  );
}

/**
 * 交通独有的两个操作。第三个（换成另一个已准入的候选）就是这张卡本身的选项列表，
 * 不需要单独的按钮。
 *
 * 「重新获取路线」= 对当前这个选项再选一次。服务端只在腿已经是 ready 时把重复选择当成
 * 空操作；腿是 pending 时它会重新绑一条真路线 —— 所以不需要第二套标识，也**不要**再开一条
 * 按 candidate_id 寻址的并行通路（公共边界根本不发布 candidate_id）。
 *
 * 「改用其他方式」= `set_transport_mode`，**不锁定**方式。它会把这条腿打成 pending 并
 * 清掉时长与费用；那条腿的状态由它自己的交通卡（`TransportLegCard` 的 RouteState）
 * 报出来，不在这里重复第二遍。这里刻意不带 `lock_mode`：锁定会让每一个已准入候选都因为方式
 * 不符而被拒，于是那条腿再也补不回来。要锁定有行程编辑器里那个专门的控件。
 * 只对市内腿开放：长途腿服务端直接拒（`long_distance_requires_option`）。
 */
function MutationNotice({ slot, mutation }: Pick<SelectionSlotCardProps, 'slot' | 'mutation'>) {
  const { state } = mutation;
  if (state.pending?.slotId !== slot.selection_slot_id) return null;
  if (state.status === 'awaiting_confirmation') {
    return (
      <div role="alert" data-testid="selection-confirmation" className="mt-3 rounded-card border border-accent/25 bg-[var(--color-accent-soft)] px-3 py-3">
        {state.pending.preview?.impacts.map((impact) => <p key={impact.kind} className="mt-1 break-words text-xs text-ink-secondary">{impact.summary}</p>)}
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" onClick={() => void mutation.confirm()} className="rounded-card bg-accent px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-[var(--color-accent-hover)]">确认调整</button>
          <button type="button" onClick={mutation.cancel} className="rounded-card border border-stroke bg-panel px-3 py-1.5 text-xs font-semibold text-ink transition-colors hover:bg-surface">取消</button>
        </div>
      </div>
    );
  }
  if (state.status === 'conflict' || state.status === 'failed') {
    return (
      <div role="alert" className="mt-3 rounded-card border border-error/25 bg-panel px-3 py-3">
        <p className="break-words text-xs leading-5 text-ink-secondary">{state.message}</p>
        <button type="button" onClick={() => void mutation.retry()} className="mt-2 rounded-card border border-stroke bg-surface px-3 py-1.5 text-xs font-semibold text-ink transition-colors hover:text-accent">重试选择</button>
      </div>
    );
  }
  if (['previewing', 'saving', 'confirming'].includes(state.status)) {
    return <p role="status" className="mt-3 text-xs text-ink-secondary">{state.message}</p>;
  }
  if (state.status === 'saved') {
    return <p role="status" className="mt-3 text-xs font-semibold text-accent">{state.message}</p>;
  }
  return null;
}

export const SelectionSlotCard: React.FC<SelectionSlotCardProps> = ({ bundle, slot, mutation, canonicalCard }) => {
  const hasSelected = slot.options.some((option) => option.selected);
  const [expanded, setExpanded] = React.useState(!hasSelected);
  const report = reportSelectionFor(bundle, slot);
  const selected = slot.options.find((option) => option.selected) ?? null;
  const alternatives = Math.max(0, slot.options.length - (selected ? 1 : 0));
  const Icon = SLOT_PRESENTATION[slot.slot_type].icon;
  const inFlight = isSlotInFlight(slot.status);
  // 过程态与 mutation 忙共用禁用，避免刷新中点选导致 CAS 冲突。
  const selectDisabled = mutation.busy || inFlight;
  const slotLabel = SLOT_PRESENTATION[slot.slot_type].heading;

  // 无 options：过程态用加载文案（非「没有方案」终态）；ready 空才是永久空态。
  if (!slot.options.length) {
    return (
      <section data-testid={`selection-slot-${slot.selection_slot_id}`} className="min-w-0 border-y border-stroke py-4">
        {canonicalCard}
        <div role="status" className="mt-2 rounded-card bg-surface px-3 py-2">
          <p className="text-sm font-semibold text-ink">{slotLabel}</p>
          {inFlight ? (
            <p
              data-testid="selection-slot-status"
              data-status={slot.status}
              className="mt-1 flex items-center gap-1.5 text-xs text-ink-secondary"
            >
              <Loader2 size={12} className="shrink-0 animate-spin text-accent" />
              {slot.status === 'refreshing' ? '正在更新可选方案…' : '正在补充可选方案…'}
            </p>
          ) : (
            <p className="mt-1 text-xs text-ink-secondary">当前没有可供比较的具体方案。</p>
          )}
        </div>
      </section>
    );
  }

  return (
    <section data-testid={`selection-slot-${slot.selection_slot_id}`} className={cn('min-w-0', canonicalCard ? 'py-1' : 'border-t border-stroke/60 py-4 first:border-t-0')}>
      {canonicalCard}
      {/* 换候选是这张卡唯一的操作。**交通槽位到不了这里**：交通是只读的，
          `TimelineEntityDetails` 在包卡之前就把交通行 return 掉了 —— 所以这里
          没有交通专属钮（「重新获取路线」「改用其他方式」），也不会有一个
          永远传不进来的 `slot_type`。 */}
      {canonicalCard && alternatives > 0 && (
        <div className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-1 px-1 pb-1">
          {!inFlight && (
            <button type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)} className="inline-flex shrink-0 items-center gap-1 rounded-card px-2 py-1.5 text-xs font-semibold text-accent transition-colors hover:bg-[var(--color-accent-soft)]">
              {expanded ? '收起选择' : selected ? `查看另外 ${alternatives} 个` : `查看 ${alternatives} 个合格选项`}
              {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </button>
          )}
        </div>
      )}
      {!canonicalCard && (
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-card bg-[var(--color-accent-soft)] text-accent"><Icon size={16} /></span>
          <div className="min-w-0 flex-1">
            <p className="text-[11px] font-semibold tracking-wide text-ink-muted">{SLOT_PRESENTATION[slot.slot_type].plan}</p>
            <h3 data-testid="current-selection-name" className="break-words text-sm font-semibold text-ink">{selected ? optionTitle(slot, selected, report) : '需要选择一个合格方案'}</h3>
            {selected ? <p className="mt-1 break-words text-xs leading-5 text-ink-secondary">{selected.selection_reasons.join(' · ')}</p> : <p className="mt-1 text-xs leading-5 text-ink-secondary">请选择已通过行程条件检查的具体方案。</p>}
          </div>
        </div>
      )}
      <SlotStatusBanner status={slot.status} />
      {(expanded || inFlight) && (
        <div className="mt-3 grid min-w-0 gap-2 sm:grid-cols-2">
          {slot.options.slice(0, 3).map((option) => (
            <OptionRow
              key={option.option_id}
              slot={slot}
              option={option}
              report={report}
              disabled={selectDisabled}
              onSelect={() => void mutation.selectOption(slot.selection_slot_id, option.option_id)}
            />
          ))}
        </div>
      )}
      <MutationNotice slot={slot} mutation={mutation} />
    </section>
  );
};
