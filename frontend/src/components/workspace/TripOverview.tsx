import React from 'react';
import type { DaySummaryVM } from '../../lib/itineraryPresentation';
import type { PublicFulfillmentSummary, PublicStructuredItineraryV2 } from '../../types/delivery';
import { DaySummaryCard } from './DaySummaryCard';
import { LabelledFactList, TripFactReadout } from './TripFactReadout';

interface TripOverviewProps {
  itinerary: PublicStructuredItineraryV2;
  summaries: readonly DaySummaryVM[];
  /**
   * 费用那一句，来自 `report_projection.document.cost_coverage_statement`。**只印服务端给的
   * 那份，不许自己从 cost_summary 再拼一遍** —— 拼出来的那句必然和报告 / PDF 漂开。
   * `null` = 没有任何价格可报，下面的 metadata 会把它过滤掉，这一段就不出现。
   */
  costCoverageStatement: string | null;
  /**
   * 出发前事项。过去只在完整报告里出现，工作台一句都不出——「长途交通没查到」这种
   * 缺口，最需要看到的人恰好停在工作台上。
   */
  importantNotes: readonly string[];
  fulfillmentSummary: PublicFulfillmentSummary;
  onOpenDay: (day: DaySummaryVM) => void;
  registerDayCard: (dayId: string, node: HTMLButtonElement | null) => void;
}

function dateRangeLabel(summaries: readonly DaySummaryVM[]): string | null {
  const dates = summaries.map((summary) => summary.dateLabel).filter((value): value is string => Boolean(value));
  if (dates.length === 0) return null;
  return dates.length === 1 ? dates[0] : `${dates[0]} – ${dates[dates.length - 1]}`;
}

export const TripOverview: React.FC<TripOverviewProps> = ({
  itinerary,
  summaries,
  costCoverageStatement,
  importantNotes,
  fulfillmentSummary,
  onOpenDay,
  registerDayCard,
}) => {
  const destinations = [...new Set(summaries.map((summary) => summary.destinationLabel).filter((value): value is string => Boolean(value)))];
  /*
   * 四个硬事实排成读数格，与报告封面同一份实现。**不要**压成
   * `[dateRange, destinations.join(' · '), '4 天', costStatement].join(' · ')`：目的地列表和
   * 字段会用同一个分隔符，读者分不出哪个 `·` 是列表内的、哪个是字段间的。
   */
  const readouts: Array<{ label: string; value: string }> = [
    ...(destinations.length > 0 ? [{ label: '目的地', value: destinations.join(' · ') }] : []),
    ...(dateRangeLabel(summaries) ? [{ label: '行程期间', value: dateRangeLabel(summaries) as string }] : []),
    { label: '天数', value: `${itinerary.duration_days} 天` },
    // 费用那一句由服务端算一份（`entities/cost_coverage.py`），报告封面 / PDF / 这里读的是
    // 同一个字段。
    ...(costCoverageStatement ? [{ label: '费用', value: costCoverageStatement }] : []),
  ];
  // 四条亮点**全印**：截成两条又不说还有两条，四个域就缺了两个。
  const highlights = itinerary.highlights.filter((highlight) => Boolean(highlight.trim()));

  return (
    <section data-testid="trip-overview" aria-labelledby="trip-overview-title" className="mx-auto w-full max-w-3xl py-1 sm:py-2">
      <header className="border-b border-stroke pb-5 sm:pb-6">
        <h2 id="trip-overview-title" className="break-words text-2xl font-semibold tracking-tight text-ink">{itinerary.title}</h2>
        <dl className="mt-4 grid gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-4">
          {readouts.map((entry) => <TripFactReadout key={entry.label} {...entry} />)}
        </dl>
        {highlights.length > 0 && (
          <div className="mt-5 border-t border-stroke/60 pt-4">
            <LabelledFactList facts={highlights} testId="overview-highlights" />
          </div>
        )}
      </header>

      {(fulfillmentSummary.fulfilled.length > 0 || fulfillmentSummary.deviations.length > 0) && (
        <section
          className="mt-5 border-b border-stroke pb-5 sm:mt-6 sm:pb-6"
          aria-labelledby="requirement-fulfillment-heading"
          data-testid="requirement-fulfillment"
        >
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 id="requirement-fulfillment-heading" className="text-sm font-semibold text-ink">你的要求</h3>
            <p className="text-xs text-ink-tertiary">
              已满足 {fulfillmentSummary.fulfilled.length} 项
              {fulfillmentSummary.deviations.length > 0 && ` · 待留意 ${fulfillmentSummary.deviations.length} 项`}
            </p>
          </div>
          <ul className="mt-3 space-y-2">
            {[...fulfillmentSummary.deviations, ...fulfillmentSummary.fulfilled].map((item) => {
              const statusLabel = {
                satisfied: '已满足',
                partially_satisfied: '部分满足',
                unsatisfied: '未满足',
                unverifiable: '无法验证',
              }[item.status];
              return (
                <li key={item.requirement_id} className="grid gap-1 border-l-2 border-stroke pl-3 sm:grid-cols-[5rem_1fr] sm:gap-3">
                  <span className={`text-xs font-semibold ${item.status === 'satisfied' ? 'text-success' : 'text-warning'}`}>
                    {statusLabel}
                  </span>
                  <div className="min-w-0">
                    <p className="break-words text-sm font-medium text-ink">{item.summary}</p>
                    {item.status !== 'satisfied' && (
                      <p className="mt-0.5 break-words text-xs leading-5 text-ink-secondary">{item.explanation}</p>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <ol className="mt-5 space-y-3 sm:mt-6 sm:space-y-4" aria-label="每日行程摘要">
        {summaries.map((day) => (
          <li key={day.dayId}>
            <DaySummaryCard
              day={day}
              onOpen={() => onOpenDay(day)}
              cardRef={(node) => registerDayCard(day.dayId, node)}
            />
          </li>
        ))}
      </ol>

      {/*
        出发前事项 —— 与完整报告同一份 `important_notes`，不是这里重写一遍。里面最要紧
        的一条是长途交通缺口披露：过去它只在报告里出现，而停在工作台上的用户看到的是
        一份「行程里就是没有返程」的方案，没有任何一句说这件事。
      */}
      {importantNotes.length > 0 && (
        <section
          className="mt-6 border-t border-stroke pt-5"
          aria-labelledby="overview-notes-heading"
          data-testid="overview-important-notes"
        >
          <h3 id="overview-notes-heading" className="text-sm font-semibold text-ink">出发前事项</h3>
          <ul className="mt-2 space-y-1.5 text-sm leading-6 text-ink-secondary">
            {importantNotes.map((note) => <li key={note} className="break-words">{note}</li>)}
          </ul>
        </section>
      )}
    </section>
  );
};
