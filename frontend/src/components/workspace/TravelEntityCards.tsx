import React from 'react';
import {
  BedDouble,
  Clock3,
  MapPin,
  Sparkles,
  Utensils,
} from 'lucide-react';
import type {
  CustomBlock,
  PublicDiningStop,
  PublicLodgingStay,
  PublicVisitStop,
} from '../../types/delivery';
import { IntentExplanationList } from './IntentExplanationList';

const money = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  maximumFractionDigits: 0,
});

const VISIT_TYPE_LABELS: Record<PublicVisitStop['visit_type'], string> = {
  attraction: '景点',
  experience: '体验',
  culture: '文化',
  shopping: '购物',
  nature: '自然',
  other: '游览',
};

const MEAL_TYPE_LABELS: Record<PublicDiningStop['meal_type'], string> = {
  breakfast: '早餐',
  lunch: '午餐',
  dinner: '晚餐',
  snack: '小吃',
  other: '用餐',
};

function timeLabel(value: string | null): string | null {
  if (!value) return null;
  return value.match(/(?:T|^)(\d{2}:\d{2})/)?.[1] ?? value;
}

function dateLabel(value: string): string {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return match ? `${Number(match[2])}月${Number(match[3])}日` : value;
}

function durationLabel(minutes: number | null): string | null {
  if (minutes == null) return null;
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours} 小时 ${remainder} 分钟` : `${hours} 小时`;
}

function reservationLabel(value: boolean | null): string | null {
  if (value == null) return null;
  return value ? '需要预约' : '无需预约';
}

function lodgingPriceLabel(
  priceKind: PublicLodgingStay['price_kind'],
  scope: 'nightly' | 'total',
  amount: number,
): string {
  const scopeLabel = scope === 'nightly' ? '每晚' : '整段';
  const prefix = priceKind === 'reference_estimate' ? `参考${scopeLabel}约` : scopeLabel;
  return `${prefix} ${money.format(amount)}`;
}

function MetaLine({ values }: { values: Array<string | null> }) {
  const visible = values.filter((value): value is string => Boolean(value));
  if (!visible.length) return null;
  return <p className="break-words text-[11px] leading-5 text-ink-muted">{visible.join(' · ')}</p>;
}

interface EntityCardSupport {
  sourceMarkers?: React.ReactNode;
}

export function VisitStopCard({
  stop,
  sourceMarkers,
}: EntityCardSupport & { stop: PublicVisitStop }) {
  const start = timeLabel(stop.planned_start);
  const end = timeLabel(stop.planned_end);
  return (
    <article data-testid={`visit-stop-card-${stop.item_id}`} data-entity-variant="visit" className="min-w-0 border-t border-stroke/60 py-4 first:border-t-0">
      <div className="flex min-w-0 items-start gap-3">
        <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-card bg-[var(--color-accent-soft)] text-accent" aria-hidden><MapPin size={16} /></span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <div className="min-w-0">
              <p className="text-[11px] font-semibold text-ink-muted">{VISIT_TYPE_LABELS[stop.visit_type]}</p>
              <h4 className="break-words text-sm font-semibold text-ink">{stop.name}</h4>
            </div>
            {start && <time className="shrink-0 text-xs tabular-nums text-ink-secondary">{start}{end ? `–${end}` : ''}</time>}
          </div>
          <div className="mt-1.5 max-w-[70ch]">
            <MetaLine values={[
              durationLabel(stop.duration_minutes),
              stop.opening_window ? `开放 ${stop.opening_window}` : null,
              reservationLabel(stop.reservation_required),
              stop.estimated_cost_cny == null ? null : money.format(stop.estimated_cost_cny),
            ]} />
            {stop.visit_highlights.length > 0 && <p className="mt-1 break-words text-xs leading-5 text-ink-secondary"><strong className="font-semibold text-ink">重点体验：</strong>{stop.visit_highlights.join('、')}</p>}
            <p className="mt-1 break-words text-xs leading-5 text-ink-secondary"><strong className="font-semibold text-ink">安排理由：</strong>{stop.selection_reason}</p>
            <IntentExplanationList items={stop.intent_explanations} />
            {sourceMarkers}
          </div>
        </div>
      </div>
    </article>
  );
}

export function DiningStopCard({
  stop,
  sourceMarkers,
}: EntityCardSupport & { stop: PublicDiningStop }) {
  const start = timeLabel(stop.planned_start);
  const end = timeLabel(stop.planned_end);
  return (
    <article data-testid={`dining-stop-card-${stop.item_id}`} data-entity-variant="dining" className="min-w-0 border-t border-stroke/60 py-4 first:border-t-0">
      <div className="flex min-w-0 items-start gap-3">
        <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-card bg-surface text-ink-secondary" aria-hidden><Utensils size={15} /></span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <div className="min-w-0">
              <p className="text-[11px] font-semibold text-ink-muted">{MEAL_TYPE_LABELS[stop.meal_type]}</p>
              <h4 className="break-words text-sm font-semibold text-ink">{stop.name}</h4>
            </div>
            {start && <time className="shrink-0 text-xs tabular-nums text-ink-secondary">{start}{end ? `–${end}` : ''}</time>}
          </div>
          <div className="mt-1.5 max-w-[70ch]">
            <MetaLine values={[
              stop.average_spend_cny == null ? null : `人均 ${money.format(stop.average_spend_cny)}`,
              stop.cuisine_types.length ? stop.cuisine_types.join(' · ') : null,
              reservationLabel(stop.reservation_required),
              stop.opening_window ? `营业 ${stop.opening_window}` : null,
            ]} />
            {stop.recommended_dishes.length > 0 && <p className="mt-1 break-words text-xs leading-5 text-ink-secondary"><strong className="font-semibold text-ink">推荐菜：</strong>{stop.recommended_dishes.join('、')}</p>}
            <p className="mt-1 break-words text-xs leading-5 text-ink-secondary"><strong className="font-semibold text-ink">选择理由：</strong>{stop.selection_reason}</p>
            <IntentExplanationList items={stop.intent_explanations} />
            {stop.dining_reminders.map((reminder) => (
              <p key={reminder} className="mt-1.5 max-w-[70ch] break-words text-xs leading-5 text-ink-secondary">
                <strong className="font-semibold text-ink">用餐提醒：</strong>{reminder}
              </p>
            ))}
            {sourceMarkers}
          </div>
        </div>
      </div>
    </article>
  );
}

export function LodgingStayCard({
  stay,
  sourceMarkers,
}: EntityCardSupport & { stay: PublicLodgingStay }) {
  return (
    <article id={`lodging-stay-${stay.stay_id}`} data-testid={`lodging-stay-card-${stay.stay_id}`} data-entity-variant="lodging" className="my-3 min-w-0 rounded-card border border-stroke bg-panel p-4 shadow-sm">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <p className="inline-flex items-center gap-1.5 text-xs font-semibold text-accent"><BedDouble size={15} aria-hidden /> 跨夜住宿</p>
        <div className="flex shrink-0 items-center gap-1.5">
          {stay.availability_status === 'needs_confirmation' && <span className="rounded-label border border-stroke px-2 py-0.5 text-[11px] font-semibold text-ink-secondary">需要确认</span>}
          <span className="rounded-label bg-surface px-2 py-1 text-[11px] font-medium text-ink-secondary">{stay.nights} 晚</span>
        </div>
      </div>
      <div className="mt-3 min-w-0">
        <h4 className="break-words text-base font-semibold text-ink">{stay.name}</h4>
        <p className="mt-1 break-words text-xs leading-5 text-ink-secondary">{stay.address}</p>
      </div>
      <div className="mt-3 grid min-w-0 gap-2 rounded-card bg-surface px-3 py-3 sm:grid-cols-2">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold text-ink-muted">入住</p>
          <p className="mt-0.5 break-words text-xs font-semibold text-ink">{dateLabel(stay.check_in_date)}{stay.check_in_time ? ` · ${stay.check_in_time}` : ''}</p>
        </div>
        <div className="min-w-0 sm:text-right">
          <p className="text-[11px] font-semibold text-ink-muted">退房</p>
          <p className="mt-0.5 break-words text-xs font-semibold text-ink">{dateLabel(stay.check_out_date)}{stay.check_out_time ? ` · ${stay.check_out_time}` : ''}</p>
        </div>
      </div>
      <div className="mt-3">
        <MetaLine values={[
          stay.room_type,
          stay.nightly_price_cny == null ? null : lodgingPriceLabel(stay.price_kind, 'nightly', stay.nightly_price_cny),
          stay.total_price_cny == null ? null : lodgingPriceLabel(stay.price_kind, 'total', stay.total_price_cny),
        ]} />
        <p className="mt-1 break-words text-xs leading-5 text-ink-secondary"><strong className="font-semibold text-ink">选择理由：</strong>{stay.selection_reason}</p>
        <IntentExplanationList items={stay.intent_explanations} />
        {sourceMarkers}
      </div>
    </article>
  );
}

export function CustomBlockRow({ block }: { block: CustomBlock }) {
  const start = timeLabel(block.planned_start);
  const end = timeLabel(block.planned_end);
  return (
    <article data-testid={`custom-block-row-${block.item_id}`} data-entity-variant="custom" className="min-w-0 border-t border-dashed border-stroke/60 py-3 first:border-t-0">
      <div className="flex min-w-0 items-start gap-3">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-card bg-surface text-ink-muted" aria-hidden><Sparkles size={13} /></span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <div className="min-w-0">
              <p className="text-[11px] font-semibold text-ink-muted">自定义安排</p>
              <h4 className="break-words text-sm font-semibold text-ink">{block.title}</h4>
            </div>
            {start && <time className="shrink-0 text-xs tabular-nums text-ink-secondary">{start}{end ? `–${end}` : ''}</time>}
          </div>
          {block.note && <p className="mt-1 max-w-[70ch] break-words text-xs leading-5 text-ink-secondary">{block.note}</p>}
          {durationLabel(block.duration_minutes) && (
            <div className="mt-1 inline-flex items-center gap-1 text-[11px] text-ink-muted">
              <Clock3 size={12} aria-hidden />
              <span>{durationLabel(block.duration_minutes)}</span>
            </div>
          )}
        </div>
      </div>
    </article>
  );
}
