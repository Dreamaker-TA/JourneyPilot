import { CalendarClock, PenLine } from 'lucide-react';
import type { EvidenceBasis } from '../../types/delivery';

export const PUBLIC_REFERENCE_LABEL = '公开资料整理';
export const PUBLIC_REFERENCE_HINT = '由规划模型依据公开资料写入，未附来源链接';
export const REFERENCE_SERVICE_LABEL = '参考班次';
export const REFERENCE_SERVICE_HINT = '来自供应商的真实班次，但未对你的出行日期确认';

/**
 * 依据口径标记 —— 与来源聚合标记共用同一个位置（实体卡的 sourceMarkers 槽、
 * 正式报告的实体块尾部）。一条行程要么由已录取候选写入、可以展开来源，要么由
 * 规划模型依据公开资料写入；后者过去什么都不显示，用户无法区分「没有来源」和
 * 「来源没显示在这里」。这里把后一种情况说出来。
 *
 * 第三种是参考班次：供应商返回的真实车次/航班，但预售窗口没覆盖到出行日期，
 * 所以车次和时刻是真的，「那天还这样」没被确认。它既不是有来源的断言，也不是
 * 模型自己写的，必须有自己的口径——否则只能在两种谎话里挑一个。
 *
 * 刻意保持低音量：muted 文本 + surface 底色，不用 warning / error 声部，
 * 它陈述依据口径，不是告警，也不参与任何全程比例或评分。
 *
 * 文案与后端 `entities/evidence_basis.py` 逐字一致；那里是唯一的判据来源。
 */
const STATED: Partial<Record<EvidenceBasis, { label: string; hint: string; icon: typeof PenLine }>> = {
  public_reference: {
    label: PUBLIC_REFERENCE_LABEL,
    hint: PUBLIC_REFERENCE_HINT,
    icon: PenLine,
  },
  reference_service: {
    label: REFERENCE_SERVICE_LABEL,
    hint: REFERENCE_SERVICE_HINT,
    icon: CalendarClock,
  },
};

export function EvidenceBasisChip({ basis }: { basis: EvidenceBasis | null }) {
  // `cited_source` 已经带着来源标记，再说一遍只是噪音。
  const stated = basis ? STATED[basis] : undefined;
  if (!basis || !stated) return null;
  const Icon = stated.icon;
  return (
    <span
      role="note"
      data-testid="entity-evidence-basis"
      data-evidence-basis={basis}
      title={stated.hint}
      aria-label={stated.hint}
      className="mt-2 inline-flex max-w-full items-center gap-1 rounded-label bg-surface px-1.5 py-0.5 text-[11px] font-medium leading-5 text-ink-muted"
    >
      <Icon size={11} className="shrink-0" aria-hidden />
      <span className="min-w-0 break-words">{stated.label}</span>
    </span>
  );
}
