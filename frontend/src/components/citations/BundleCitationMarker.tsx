import React from 'react';
import { ArrowLeft, BookOpen, ExternalLink } from 'lucide-react';
import type { PublicCitationProjection } from '../../types/delivery';
import { Popover } from '../ui/Popover';

type PublicSource = PublicCitationProjection['sources'][number];

export interface BundleSourceDetail {
  citation: PublicCitationProjection;
  source: PublicSource;
  returnFocus: HTMLButtonElement;
}

interface ClusterProps {
  citations: PublicCitationProjection[];
  onOpenDetail: (detail: BundleSourceDetail) => void;
}

const FIELD_LABELS: Record<string, string> = {
  name: '名称',
  branch_name: '门店',
  property_name: '住宿名称',
  address: '地址',
  opening_window: '营业时间',
  average_spend_cny: '人均消费',
  nightly_price_cny: '每晚价格',
  total_price_cny: '住宿总价',
  departure_at: '出发时间',
  arrival_at: '到达时间',
  duration_minutes: '预计时长',
  distance_meters: '距离',
  total_cost_cny: '交通费用',
  selected_mode: '交通方式',
  precipitation_probability_pct: '降水概率',
  precipitation_mm: '降水量',
  condition_code: '天气状况',
  high_c: '最高温度',
  low_c: '最低温度',
  apparent_high_c: '最高体感温度',
  wind_speed_kph: '风速',
  wind_gust_kph: '阵风',
  visit_highlights: '行程亮点',
  wheelchair_access: '无障碍条件',
};

const SOURCE_KIND_LABELS: Record<PublicSource['source_kind'], string> = {
  external_web: '网页来源',
  external_tool: '数据服务结果',
  rag_chunk: '知识库文档',
};

const FACT_STATUS_LABELS: Partial<Record<PublicCitationProjection['fact_status'], string>> = {
  refreshing: '正在复核',
  stale: '信息已过期',
  conflict: '信息有分歧',
  missing: '缺少依据',
};


/**
 * 认不出的 `field_path` 返回 `null`，调用方跳过那一行——「相关事实」是把「我们不知道这是
 * 什么字段」印成一个字段名（§Anti-Slop「No fake precision」）。
 */
function fieldLabel(path: string): string | null {
  const leaf = path.split('.').at(-1)?.replace(/\[\d+\]/g, '') ?? '';
  return FIELD_LABELS[leaf] ?? null;
}

/** 没有可显示的标量值就返回 `null`：「已核实」不是这个字段的值。 */
function formatValue(value: unknown, unit: string | null, currency: string | null): string | null {
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') {
    return null;
  }
  if (typeof value === 'number' && currency) {
    try {
      return new Intl.NumberFormat('zh-CN', { style: 'currency', currency, maximumFractionDigits: 2 }).format(value);
    } catch {
      return `${value} ${currency}`;
    }
  }
  if (unit === 'percent') return `${value}%`;
  return `${value}${unit ? ` ${unit}` : ''}`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function SourceEntry({
  source,
  citation,
  onOpenDetail,
  triggerRef,
  onNavigate,
}: {
  source: PublicSource;
  citation: PublicCitationProjection;
  onOpenDetail: (detail: BundleSourceDetail) => void;
  triggerRef: React.MutableRefObject<HTMLButtonElement | null>;
  onNavigate: () => void;
}) {
  return (
    <div className="py-3 last:pb-0">
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span className="rounded-label bg-accent-soft px-1.5 py-0.5 font-semibold text-accent">
          {SOURCE_KIND_LABELS[source.source_kind]}
        </span>
        <h4 className="min-w-0 break-words font-semibold text-ink">{source.title}</h4>
      </div>
      {source.public_excerpt && <p className="mt-2 break-words text-ink-secondary">{source.public_excerpt}</p>}
      <p className="mt-2 text-[11px] text-ink-muted">
        取得于 {formatDate(source.retrieved_at)}
        {source.observed_at ? ` · 记录于 ${formatDate(source.observed_at)}` : ''}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2">
        {source.canonical_url && (
          <a
            href={source.canonical_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex min-h-8 items-center gap-1 font-semibold text-accent hover:underline"
          >
            打开原始来源 <ExternalLink size={12} aria-hidden />
          </a>
        )}
        <button
          type="button"
          onClick={() => {
            const trigger = triggerRef.current;
            if (!trigger) return;
            onNavigate();
            onOpenDetail({ citation, source, returnFocus: trigger });
          }}
          className="inline-flex min-h-8 items-center gap-1 font-semibold text-accent hover:underline"
        >
          查看来源摘要 <BookOpen size={12} aria-hidden />
        </button>
      </div>
    </div>
  );
}

type ClusterSource = { source: PublicSource; citation: PublicCitationProjection };

/**
 * 来源聚合标记 —— 默认只说「有几处来源」；支撑字段和去重后的来源列表都留在用户
 * 主动打开的证据层。一个实体的全部引用按 source_record_id 聚合，因而不会把同一
 * 来源重复展示成多枚正文标记。
 *
 * 标签与依据口径标记（`公开资料整理`）同一音量、同一形状：同一个「依据」槽位上，
 * 一条行程要么有来源、要么由规划模型依据公开资料写入，两种情况都必须读得出来，
 * 有来源的那种不该退化成一枚不出声的图标。
 */
export const BundleSourceCluster: React.FC<ClusterProps> = ({ citations, onOpenDetail }) => {
  const triggerRef = React.useRef<HTMLButtonElement | null>(null);

  const { facts, sources, factStatus } = React.useMemo(() => {
    const factList: PublicCitationProjection['supported_values'] = [];
    const seenFact = new Set<string>();
    const sourceMap = new Map<string, ClusterSource>();
    let status: PublicCitationProjection['fact_status'] | null = null;
    for (const citation of citations) {
      for (const value of citation.supported_values) {
        if (seenFact.has(value.label)) continue;
        seenFact.add(value.label);
        factList.push(value);
      }
      for (const source of citation.sources) {
        if (!sourceMap.has(source.source_record_id)) sourceMap.set(source.source_record_id, { source, citation });
      }
      if (!status && FACT_STATUS_LABELS[citation.fact_status]) status = citation.fact_status;
    }
    return { facts: factList, sources: [...sourceMap.values()], factStatus: status };
  }, [citations]);

  const sourceCount = sources.length || citations.length;

  /**
   * 能印出来的事实：字段名认得出、而且有一个可显示的标量值。两者缺一整行不出——
   * 一行「相关事实 / 已核实」是把「我们没有这条信息」印成一条信息。
   */
  const printableFacts = React.useMemo(
    () => facts.flatMap((item) => {
      const printableLabel = fieldLabel(item.label);
      const printableValue = formatValue(item.value, item.unit, item.currency);
      if (printableLabel === null || printableValue === null) return [];
      return [{ ...item, printableLabel, printableValue }];
    }),
    [facts]
  );

  if (citations.length === 0) return null;

  return (
    <Popover
      portal
      testId="bundle-source-popover"
      className="max-h-[min(30rem,calc(100vh-1.5rem))] overflow-y-auto p-4"
      trigger={({ ref, open, toggle }) => (
        // 11px 文字要过 AA 4.5:1，所以字色用 accent-hover(#1B5FC2) —— surface 上 4.96:1、
        // accent-soft 上 5.32:1，两态都达标。text-chart(#3E6FB0) 压在 bg-surface(#EEE8D8) 上
        // 只有 4.18:1，accent 更是掉到 3.78:1。因此 hover **只换底色、不换字色**。
        <button
          ref={(node) => {
            // 同时把节点交给原语的 triggerRef 与本地的 triggerRef（后者供来源详情返焦用）。
            if (typeof ref === 'function') {
              ref(node);
            } else if (ref) {
              (ref as { current: HTMLButtonElement | null }).current = node;
            }
            triggerRef.current = node;
          }}
          type="button"
          data-testid="bundle-source-cluster"
          onClick={toggle}
          className="inline-flex max-w-full items-center gap-1 rounded-card bg-surface px-1.5 py-0.5 text-[11px] font-medium leading-5 text-accent-hover transition-colors hover:bg-accent-soft"
          aria-label={`查看 ${sourceCount} 处来源`}
          aria-expanded={open}
          aria-haspopup="true"
        >
          <BookOpen size={11} className="shrink-0" aria-hidden />
          <span className="min-w-0 break-words">来源 · {sourceCount}</span>
        </button>
      )}
    >
      {(close) => (
        <div className="text-left text-xs font-normal leading-relaxed text-ink-secondary">
          <div className="mb-1 flex items-center justify-between gap-2 border-b border-stroke/60 pb-2">
            <p className="font-semibold text-ink">来源</p>
            {factStatus && FACT_STATUS_LABELS[factStatus] ? (
              <span className="shrink-0 rounded-label bg-error/10 px-1.5 py-0.5 font-semibold text-error">{FACT_STATUS_LABELS[factStatus]}</span>
            ) : printableFacts.length > 0 ? (
              <p className="shrink-0 text-[11px] text-ink-muted">支持 {printableFacts.length} 项已核实事实</p>
            ) : null}
          </div>
          {/* 认不出字段名、或没有可显示标量值的事实**整行不出**：一行「相关事实 / 已核实」
              是把「我们没有这条信息」印成一条信息。过滤在渲染之前做，所以计数与
              表格永远一致。 */}
          {printableFacts.length > 0 && (
            <dl className="space-y-1.5 border-b border-stroke pb-3">
              {printableFacts.map((item, index) => (
                <div key={index} className="flex min-w-0 items-baseline justify-between gap-3">
                  <dt className="break-words text-ink-muted">{item.printableLabel}</dt>
                  <dd className="min-w-0 break-words text-right font-medium text-ink">{item.printableValue}</dd>
                </div>
              ))}
            </dl>
          )}
          <div className="divide-y divide-stroke/60">
            {sources.map(({ source, citation }) => (
              <SourceEntry
                key={source.source_record_id || citation.citation_id}
                source={source}
                citation={citation}
                onOpenDetail={onOpenDetail}
                triggerRef={triggerRef}
                onNavigate={close}
              />
            ))}
          </div>
        </div>
      )}
    </Popover>
  );
};

export const BundleSourceDetailView: React.FC<{
  detail: BundleSourceDetail;
  onClose: () => void;
}> = ({ detail, onClose }) => {
  const backRef = React.useRef<HTMLButtonElement>(null);
  const titleId = React.useId();
  const { source } = detail;

  React.useEffect(() => {
    backRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <section data-testid="bundle-source-detail" aria-labelledby={titleId} className="flex h-full min-h-0 flex-col bg-panel">
      <header className="flex flex-none items-center gap-3 border-b border-stroke px-4 py-3">
        <button
          ref={backRef}
          type="button"
          onClick={onClose}
          className="inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-card px-2 text-xs font-semibold text-ink-secondary transition-colors hover:bg-surface hover:text-ink"
        >
          <ArrowLeft size={15} aria-hidden /> 返回行程
        </button>
        <p className="min-w-0 truncate text-xs font-medium text-ink-muted">来源详情</p>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-6 sm:px-7">
        <article className="mx-auto w-full max-w-3xl">
          <p className="text-xs font-semibold text-accent">{SOURCE_KIND_LABELS[source.source_kind]}</p>
          <h2 id={titleId} className="mt-2 break-words text-xl font-semibold tracking-tight text-ink">{source.title}</h2>
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
            <span>取得于 {formatDate(source.retrieved_at)}</span>
            {source.observed_at && <span>记录于 {formatDate(source.observed_at)}</span>}
          </div>
          {source.public_excerpt && (
            <div className="mt-5 border-y border-stroke py-5">
              <h3 className="text-sm font-semibold text-ink">本次引用摘要</h3>
              <p className="mt-3 max-w-[72ch] whitespace-pre-wrap break-words text-sm leading-7 text-ink-secondary">
                {source.public_excerpt}
              </p>
            </div>
          )}
          {source.canonical_url && (
            <a
              href={source.canonical_url}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-5 inline-flex min-h-10 items-center gap-1.5 rounded-card px-2 font-semibold text-accent hover:bg-accent-soft"
            >
              打开原始文档 <ExternalLink size={14} aria-hidden />
            </a>
          )}
        </article>
      </div>
    </section>
  );
};
