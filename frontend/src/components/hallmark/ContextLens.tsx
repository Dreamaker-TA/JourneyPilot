import React, { useId, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { cn } from '../../lib/utils';
import type { ContextReport, ContextReportSection, ContextReportSectionKey } from '../../types/chat';

/** 用户主动打开“更多信息”后看到的本次规划依据，不暴露记忆实现。 */

/**
 * 段标题按 prompt 里那三段来。**prompt 的措辞归后端、屏幕的
 * 措辞归这里**，各写一次 —— 后端往 SSE 里塞标题、前端再显示，就是同一句话两个产地。
 */
const SECTION_LABEL: Record<ContextReportSectionKey, string> = {
  hard: '硬约束',
  preference: '偏好',
  reference: '参考级背景',
};

// 超过这个条数就折叠 —— 这是**显示折叠，不是截断**：一条都没少，只是先不占满一屏。
const COLLAPSED_ITEM_LIMIT = 6;

/** 折叠态下按段依次取，取满就停；展开态原样返回。 */
function visibleSections(sections: ContextReportSection[], budget: number): ContextReportSection[] {
  const out: ContextReportSection[] = [];
  let remaining = budget;
  for (const section of sections) {
    if (remaining <= 0) break;
    const items = section.items.slice(0, remaining);
    remaining -= items.length;
    out.push({ ...section, items });
  }
  return out;
}

export const ContextLens: React.FC<{ report: ContextReport }> = ({ report }) => {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();

  const total = report.sections.reduce((count, section) => count + section.items.length, 0);
  const foldable = total > COLLAPSED_ITEM_LIMIT;
  const collapsed = foldable && !expanded;
  const shown = collapsed ? visibleSections(report.sections, COLLAPSED_ITEM_LIMIT) : report.sections;

  return (
    <div data-testid="context-lens" className="flex max-w-[320px] flex-col gap-3 p-4">
      {/* 标题之外**不写**「以下偏好已用于本次规划」：紧下方就是那几段条目，中间那句
          等于把标题和列表各复述一次。
           也**没有**空集那一句（「本次回答主要依据当前对话中的旅行需求。」）：
           没有真东西可说的那一轮**根本不发报告**（`memory/context_builder.py::build_context_report`
           返回 None），这个浮窗打不开，那句话没有存在的余地。 */}
      <span className="text-[13px] font-semibold text-ink">本次参考的信息</span>
      <div id={listId} className="flex flex-col gap-3">
        {shown.map((section) => (
          <div key={section.key} className="flex flex-col gap-1.5">
            <div className="flex items-baseline gap-1.5">
              <span className="text-[11px] font-semibold text-ink">{SECTION_LABEL[section.key]}</span>
              {/* 参考级那一段在 prompt 里就明写着自己不是约束；界面不说这一句，
                  系统猜出来的东西看上去会和用户亲口说的一样硬。 */}
              {section.key === 'reference' && (
                <span className="text-[11px] text-ink-muted">参考，不是要求</span>
              )}
            </div>
            <ul className="flex flex-col gap-1.5">
              {section.items.map((item) => (
                <li
                  key={`${section.key}:${item}`}
                  className="rounded-card bg-surface px-3 py-2 text-xs leading-relaxed text-ink-secondary"
                >
                  {item}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      {foldable && (
        <button
          type="button"
          data-testid="context-lens-toggle"
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((current) => !current)}
          className="-ml-2 inline-flex min-h-9 items-center gap-1 self-start rounded-card px-2 text-[11px] font-semibold text-ink-secondary transition-colors hover:bg-highlight hover:text-ink"
        >
          {expanded ? '收起' : `展开全部 ${total} 条`}
          <ChevronDown size={13} aria-hidden className={cn('transition-transform', expanded && 'rotate-180')} />
        </button>
      )}
      {report.compaction.triggered && (
        <p className="rounded-card bg-surface px-3 py-2 text-[11px] leading-relaxed text-ink-secondary">
          较早的对话已整理，旅行重点和已明确的规划约束会继续沿用。
        </p>
      )}
    </div>
  );
};
