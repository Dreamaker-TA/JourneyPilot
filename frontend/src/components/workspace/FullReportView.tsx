import React from 'react';
import { Download } from 'lucide-react';
import { useApp } from '../../context/AppContext';
import { EvidenceBasisChip } from '../citations/EvidenceBasisChip';
import { api } from '../../lib/api';
import { apiErrorDetail } from '../../lib/apiErrorDetail';
import {
  evidenceBasisForReportBlock,
  formatLocalDate,
  selectDaySummaries,
  selectDayTimeline,
  type DaySummaryVM,
  type DayTimelineNodeVM,
} from '../../lib/itineraryPresentation';
import { splitLabelledFact } from '../../lib/tripDomains';
import { BrandMark } from '../ui/BrandMark';
import type {
  PublicDeliveryBundle,
  PublicStructuredItineraryV2,
} from '../../types/delivery';
import { DayHeader } from './DayHeader';
import { DayTimeline } from './DayTimeline';
import { LabelledFactList, TripFactReadout } from './TripFactReadout';
import { IntentExplanationList } from './IntentExplanationList';
import type { EntityIntentExplanation } from '../../types/delivery';
import { Neatline } from '../ui/Neatline';
import { Postmark } from '../ui/Postmark';

type ReportDocument = NonNullable<PublicDeliveryBundle['report_projection']['document']>;
type ReportDay = ReportDocument['days'][number];
type ReportBlock = ReportDay['blocks'][number];

/**
 * 一条条目的事实行、备注与班次身份 —— 全部由投影层渲染好
 * （`entities/delivery_presentation.py`），这里只负责印。
 *
 * **这里不许自己造字**：按实体种类挑字段拼一行、自己拼班次身份，都会和 PDF 那一份漂开 ——
 * 同一条腿于是在纸上和屏幕上被说成两件事（`coach` 一边「长途巴士」一边「长途汽车」）。
 * 排版仍归各面自己：这里把事实行用 `·` 连成一行，PDF 竖排。
 */
function ReportEntryLines({ block }: { block: ReportBlock }) {
  const { facts, notes, segment_lines: segmentLines } = block.details;
  return (
    <>
      {/* 事实行是**标签/值两级**：标签退到 muted，值留在 ink —— 扫读靠层次，不靠分隔点。
          **不要**压成 `facts.join(' · ')`：同字号、同色、同粗细的一长串灰字
          （`类型: 文化 · 开放: 06:00–17:00 · 预约: 无需预约 · 地址: …`）是全篇最密、最有用
          的内容，也会成为全篇最难扫的一处 —— 读者要找「几点开门」得先读完「类型」。
          仍然是一个流式行内布局（不是 2×2 仪表盘），所以窄屏照旧回流。 */}
      {facts.length > 0 && (
        <p className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1 break-words text-[11px] leading-5">
          {facts.map((fact) => {
            const { label, value } = splitLabelledFact(fact);
            return (
              <span key={fact} className="inline-flex min-w-0 items-baseline gap-1">
                {label && <span className="shrink-0 text-ink-muted">{label}</span>}
                {/* 刻意不加 tabular-nums：值是中英混排而不是纯数字列，而 CJK 字体在
                    等宽数字下会把全角标点也撑成数字宽——第一版就是这样把
                    `06：00–17:00` 印成了 `06 00-17:00`。 */}
                <span className="min-w-0 break-words font-medium text-ink-secondary">{value}</span>
              </span>
            );
          })}
        </p>
      )}
      {notes.map((note) => (
        <p key={note} className="mt-1.5 max-w-[70ch] break-words text-xs leading-5 text-ink-secondary">
          {note}
        </p>
      ))}
      {segmentLines.length > 0 && (
        <ol className="mt-2 space-y-1 border-l border-stroke pl-3" aria-label="班次信息">
          {segmentLines.map((line) => (
            <li key={line} className="break-words text-[11px] leading-5 text-ink-secondary">{line}</li>
          ))}
        </ol>
      )}
    </>
  );
}

function PdfDownloadButton({ bundle }: { bundle: PublicDeliveryBundle }) {
  const { state, dispatch } = useApp();
  const [status, setStatus] = React.useState<'idle' | 'loading' | 'success' | 'error'>('idle');
  const [message, setMessage] = React.useState<string | null>(null);

  const loadCurrent = React.useCallback(async () => {
    const current = await api.getCurrentDeliveryBundle(bundle.manifest.run_id, state.currentSessionId);
    dispatch({ type: 'CONFIRM_DELIVERY_BUNDLE', payload: current });
    dispatch({ type: 'SET_DELIVERABLE_VIEW', payload: 'full_report' });
    return current;
  }, [bundle.manifest.run_id, dispatch, state.currentSessionId]);

  const download = async () => {
    setStatus('loading');
    setMessage(null);
    try {
      const result = await api.exportCurrentTripReportPdf(bundle, state.currentSessionId);
      if (result.bundleId && result.bundleId !== bundle.manifest.bundle_id) await loadCurrent();
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = result.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      setStatus('success');
      setMessage('PDF 已生成。');
    } catch (error) {
      const detail = apiErrorDetail(error);
      if (detail.code === 'report_out_of_date') {
        try {
          await loadCurrent();
          setMessage('行程刚刚更新，已载入最新结果。请再次导出。');
        } catch {
          setMessage('行程刚刚更新，请重新打开当前行程后再导出。');
        }
      } else if (detail.code === 'pdf_temporarily_unavailable') {
        setMessage('PDF 暂时无法生成，请稍后重试。');
      } else {
        setMessage('PDF 生成失败，请检查网络后重试。');
      }
      setStatus('error');
    }
  };

  return (
    <div className="min-w-0">
      <button
        type="button"
        data-testid="download-current-pdf"
        disabled={status === 'loading'}
        onClick={() => void download()}
        className="inline-flex min-h-10 items-center justify-center gap-2 rounded-card bg-accent px-3.5 text-xs font-semibold text-white transition-colors hover:bg-[var(--color-accent-hover)] disabled:cursor-wait disabled:opacity-65"
      >
        <Download size={15} aria-hidden />
        {status === 'loading' ? '正在准备 PDF' : '导出当前 PDF'}
      </button>
      {message && <p role={status === 'error' ? 'alert' : 'status'} className="mt-2 max-w-[36ch] break-words text-[11px] leading-5 text-ink-secondary">{message}</p>}
    </div>
  );
}

/**
 * 报告封面。
 *
 * 三件事撑起交付物的视觉身份 —— 一份行程报告的封面不该弱于空态首页：
 *
 * 1. **刊头**：品牌标记（纸飞机，与落地页同一份素材）+ 等宽刊头行。这一面没有朱红，
 *    §Color 里朱红是「可以出现」不是「必须出现」，不用补。
 * 2. **标题进首屏字号档**：30 / 36px，也就是类型表里「first-screen product
 *    statement」那一挡。**没有**换成 display serif —— 那个字体是 3.9 KB 的 14 字子集
 *    （只够印首页那一句），任意行程标题会掉进 `Songti SC` 回退，而多数机器上没有它。
 * 3. **图廓**：唯一一件借过来的图纸家具，见 index.css `.report-cover`。
 *
 * 日期区间由 `days[].date` 的首末两天算，不另存一份：报告文档里没有 start/end 字段，
 * 而每一天都带自己的日期。全为空（罕见但合同允许）就整行不画，不留空尾巴。
 */
function ReportCover({
  bundle,
  document: report,
  allowPdf,
}: {
  bundle: PublicDeliveryBundle;
  document: ReportDocument;
  allowPdf: boolean;
}) {
  const destinations = report.destinations.map((entry) => entry.display_name.trim()).filter(Boolean);
  const dates = report.days.map((day) => day.date).filter((date): date is string => Boolean(date));
  const firstDate = formatLocalDate(dates[0] ?? null);
  const lastDate = formatLocalDate(dates[dates.length - 1] ?? null);
  const dateRange = firstDate && lastDate
    ? (firstDate === lastDate ? firstDate : `${firstDate} – ${lastDate}`)
    : firstDate ?? lastDate;

  const readouts: Array<{ label: string; value: string }> = [
    ...(destinations.length > 0 ? [{ label: '目的地', value: destinations.join(' · ') }] : []),
    ...(dateRange ? [{ label: '行程期间', value: dateRange }] : []),
    { label: '天数', value: `${report.duration_days} 天` },
    // 费用只有一句，由服务端算（`entities/cost_coverage.py`）。这里、工作台总览、PDF
    // 曾各写一份并且已经漂开过，别再各拼一份。服务端给 null 表示这趟没有任何
    // 价格可报，那就这一格不画。
    ...(report.cost_coverage_statement ? [{ label: '费用', value: report.cost_coverage_statement }] : []),
  ];

  return (
    <header data-testid="report-cover" className="report-cover relative px-5 py-7 sm:px-8 sm:py-9">
      <Neatline />

      <div className="relative z-10 flex min-w-0 flex-wrap items-start justify-between gap-x-6 gap-y-5">
        <div className="min-w-0 flex-1 basis-[22rem]">
          <div className="flex items-center gap-2.5">
            <BrandMark size={20} className="shrink-0" />
            {/* 刊头自己的字距（0.18em），不走 READOUT_LABEL：那是读数标签的声部，这一行是品牌刊头。 */}
            <p className="font-mono text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-muted">
              JOURNEYPILOT · 行程方案
            </p>
          </div>
          <h1 className="mt-5 max-w-[22ch] break-words text-[30px] font-semibold leading-[1.22] tracking-[-0.022em] text-ink sm:text-[36px]">
            {report.title}
          </h1>
          <p className="mt-4 max-w-[58ch] break-words text-[15px] leading-7 text-ink-secondary">{report.overview}</p>
        </div>
        {/* 右列：导出键在上，邮戳在下。邮戳印的是这份方案哪天出的 —— 一份交付物的签发日期
            是读者会找的第一批事实之一，封面上必须有一处说得出它。 */}
        <div className="flex shrink-0 flex-col items-end gap-5">
          {allowPdf && <PdfDownloadButton bundle={bundle} />}
          <Postmark generatedAt={bundle.report_projection.generated_at} />
        </div>
      </div>

      <dl className="relative z-10 mt-7 grid gap-x-6 gap-y-4 border-t border-stroke pt-5 sm:grid-cols-2 lg:grid-cols-4">
        {readouts.map((entry) => <TripFactReadout key={entry.label} {...entry} />)}
      </dl>
    </header>
  );
}

function ReportTimelineSupplement({
  block,
  node,
  renderCitations,
  itinerary,
}: {
  block: ReportBlock;
  node: DayTimelineNodeVM;
  renderCitations: (ids: string[]) => React.ReactNode;
  itinerary: PublicStructuredItineraryV2;
}) {
  const reportSummary = block.summary.trim();
  const nodeSummary = node.summary?.trim() ?? '';
  const summaryIsAdditional = reportSummary && reportSummary !== block.title.trim() && reportSummary !== nodeSummary;
  const intentExplanations = Array.isArray(block.details.intent_explanations)
    ? block.details.intent_explanations.filter(
      (item): item is EntityIntentExplanation => Boolean(
        item
        && typeof item === 'object'
        && typeof (item as EntityIntentExplanation).label === 'string'
        && typeof (item as EntityIntentExplanation).explanation === 'string',
      ),
    )
    : [];
  return (
    <div data-testid={`report-timeline-supplement-${node.key}`} className="min-w-0">
      {summaryIsAdditional && <p className="mt-1 max-w-[68ch] break-words text-sm leading-6 text-ink-secondary">{reportSummary}</p>}
      <ReportEntryLines block={block} />
      <IntentExplanationList items={intentExplanations} />
      {block.entity_kind !== 'custom' && block.citation_ids.length > 0 && renderCitations(block.citation_ids)}
      <EvidenceBasisChip basis={evidenceBasisForReportBlock(block, itinerary)} />
    </div>
  );
}

function ReportDaySection({
  bundle,
  day,
  summary,
  nodes,
  renderCitations,
}: {
  bundle: PublicDeliveryBundle;
  day: ReportDay;
  summary: DaySummaryVM | null;
  nodes: readonly DayTimelineNodeVM[];
  renderCitations: (ids: string[]) => React.ReactNode;
}) {
  // 节点与补充说明来自同一批报告块，按**块自己的位置身份**配对。不能用「实体 id +
  // projection_role」当键：同一天里出现两次的同一实体会撞在一起。
  const blocks = React.useMemo(
    () => new Map(day.blocks.map((block) => [block.details.entry_id, block])),
    [day.blocks],
  );

  if (!summary) return null;

  return (
    <section data-testid={`report-day-${day.day_id}`} className="border-t border-stroke py-7 first:border-t-0 first:pt-6 sm:py-8" aria-labelledby={`delivery-day-report-${day.day_id}`}>
      <DayHeader
        dayId={`report-${day.day_id}`}
        day={summary.day}
        dateLabel={summary.dateLabel}
        weekdayLabel={summary.weekdayLabel}
        destinationLabel={summary.destinationLabel}
        theme={summary.theme}
        weather={summary.weather}
      />
      <DayTimeline
        nodes={nodes}
        renderSupplement={(node) => {
          const block = blocks.get(node.key);
          return block ? (
            <ReportTimelineSupplement
              block={block}
              node={node}
              renderCitations={renderCitations}
              itinerary={bundle.workspace.itinerary}
            />
          ) : null;
        }}
      />
    </section>
  );
}

interface FullReportViewProps {
  bundle: PublicDeliveryBundle;
  renderCitations: (ids: string[]) => React.ReactNode;
  allowPdf?: boolean;
}

export function FullReportView({ bundle, renderCitations, allowPdf = true }: FullReportViewProps) {
  const document = bundle.report_projection.document;
  if (!document) return null;

  const summaries = selectDaySummaries(bundle);
  const summariesByDayId = new Map(summaries.map((summary) => [summary.dayId, summary]));
  // 报告结尾的注释区：本次规划自身「哪些没做到 / 依据来自哪个数据环境」。两句都由
  // 服务端按数据算出来，界面只负责印，PDF 印的是同一批句子。
  const bottomNotes = [
    ...bundle.coverage_disclosure.notes,
    ...(bundle.provider_environment.sandbox_note ? [bundle.provider_environment.sandbox_note] : []),
  ];
  const alternatives = document.selections.flatMap((selection) => selection.options
    .filter((option) => !option.selected)
    .map((option) => ({ ...option, slotType: selection.slot_type })));

  return (
    <article data-testid="full-report" className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-7 sm:py-8">
      <ReportCover bundle={bundle} document={document} allowPdf={allowPdf} />

      {/* 「全程亮点」而不是「全程核心路线」：这一段是服务端从行程实体里推导出来的
          （`entities/trip_highlights.py`），涵盖跨城交通、景点、餐饮与住宿，不只是路线。

          四条**每条自带标签**（`跨城交通：…` / `主要景点：…` / `特色餐饮：…` / `住宿：…`，
          `trip_highlights` 里都是 `f"{label}：{body}"`），所以这里用与停留点事实行同一套
          拆法：`splitLabelledFact` 按全角冒号拆两级，标签走 chart 印刷蓝（chart
          是非交互强调声部），值走 ink。**不要**退回裸 `<ul>` 四条同字号同色的灰句子 ——
          那是全篇信息密度最高的一段，零视觉处理等于没排。

          刻意**不**按标签文字反推域再配域色：那是把界面钉在后端文案上，`_VISIT_LABEL`
          改一个字这里就静默退化。标签是什么字由后端说，这里只负责分两级。

          排法提成了 `LabelledFactList`，工作台总览读同一份 —— 两个面不许各拼一遍。 */}
      {document.highlights.length > 0 && (
        <section data-testid="report-highlights" className="border-b border-stroke py-7" aria-labelledby="report-highlights-heading">
          <h2 id="report-highlights-heading" className="text-base font-semibold text-ink">全程亮点</h2>
          <div className="mt-4">
            <LabelledFactList facts={document.highlights} />
          </div>
        </section>
      )}

      <div>
        {document.days.map((day) => (
          <ReportDaySection
            key={day.day_id}
            bundle={bundle}
            day={day}
            summary={summariesByDayId.get(day.day_id) ?? null}
            nodes={selectDayTimeline(bundle, day.day_id)}
            renderCitations={renderCitations}
          />
        ))}
      </div>

      {alternatives.length > 0 && (
        <section className="border-t border-stroke py-7" aria-labelledby="report-alternatives-heading">
          <h2 id="report-alternatives-heading" className="text-base font-semibold text-ink">其它合格选择</h2>
          <p className="mt-1 text-xs leading-5 text-ink-secondary">主行程只使用当前选择；以下选项来自同一次比较结果。</p>
          <ol className="mt-3 divide-y divide-stroke/60">
            {alternatives.map((option) => (
              <li key={option.option_id} className="min-w-0 py-3 first:pt-0">
                <h3 className="break-words text-sm font-semibold text-ink">{option.name}</h3>
                <p className="mt-1 break-words text-xs leading-5 text-ink-secondary">{option.comparison_facts.join(' · ')}</p>
                <p className="mt-1 break-words text-xs leading-5 text-ink-secondary">{option.selection_reasons.join(' · ')}</p>
                {option.tradeoff && <p className="mt-1.5 break-words text-[11px] leading-5 text-ink-muted">取舍：{option.tradeoff}</p>}
                {option.availability_status === 'needs_confirmation' && <p className="mt-1.5 text-[11px] font-medium text-warning">出发前请确认</p>}
                {option.citation_ids.length > 0 && renderCitations(option.citation_ids)}
              </li>
            ))}
          </ol>
        </section>
      )}

      {document.important_notes.length > 0 && (
        <footer className="border-t border-stroke py-7" aria-labelledby="report-notes-heading">
          <h2 id="report-notes-heading" className="text-base font-semibold text-ink">出发前事项</h2>
          <ul className="mt-2 space-y-1.5 text-sm leading-6 text-ink-secondary">
            {document.important_notes.map((note) => <li key={note} className="break-words">{note}</li>)}
          </ul>
        </footer>
      )}

      {/*
        报告结尾的注释区 —— 「这份方案有哪些地方没做到」。刻意与「出发前事项」分开：
        那些是要用户去办的事，这里是本次规划自身的覆盖情况，也刻意压低音量（muted 小字，
        不是 warning 声部）：它陈述事实，不是告警。
        句子由服务端算好（`entities/coverage_disclosure.py`），域级、一句一域；
        reason_code / provider 名 / worker 名不到这一层，那些在产品面上会被读成
        「供应商的错」或「系统报错」，两种都不是实际发生的事。
      */}
      {bottomNotes.length > 0 && (
        <section
          className="border-t border-stroke py-6"
          aria-labelledby="report-coverage-heading"
          data-testid="report-coverage-disclosure"
        >
          <h2 id="report-coverage-heading" className="text-sm font-semibold text-ink-secondary">本次规划的覆盖情况</h2>
          <ul className="mt-2 space-y-1 text-xs leading-6 text-ink-muted">
            {bottomNotes.map((note) => <li key={note} className="break-words">{note}</li>)}
          </ul>
        </section>
      )}
    </article>
  );
}
