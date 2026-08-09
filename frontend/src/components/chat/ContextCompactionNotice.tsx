import React, { useId, useState } from 'react';
import { Archive, Check, ChevronDown } from 'lucide-react';
import type { ContextCompactionEvent } from '../../types/chat';
import { cn } from '../../lib/utils';

export const ContextCompactionNotice: React.FC<{
  event: ContextCompactionEvent;
}> = ({ event }) => {
  const [expanded, setExpanded] = useState(false);
  const detailsId = useId();
  const constraints = event.keyConstraints;
  const summary = event.summary.trim();

  return (
    <section
      data-testid="context-compaction-event"
      className="border-y border-stroke bg-panel/65"
      aria-label="较早对话已整理"
    >
      <div className="flex w-full items-center gap-2 px-4 py-3 sm:px-5">
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={detailsId}
          onClick={() => setExpanded((value) => !value)}
          className="group flex min-w-0 flex-1 items-center gap-3 text-left transition-colors duration-base ease-standard hover:bg-transparent"
        >
          <span className="flex h-7 w-7 flex-none items-center justify-center rounded-card border border-accent/18 bg-accent-soft text-accent">
            <Archive size={14} aria-hidden="true" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-[13px] font-semibold text-ink">已整理较早对话</span>
            <span className="mt-0.5 block text-xs leading-relaxed text-ink-secondary">
              {constraints.length > 0
                ? `已保留 ${constraints.length} 项规划约束，后续规划会继续沿用。`
                : '后续规划会继续沿用已整理的旅行重点。'}
            </span>
          </span>
          <span className="flex flex-none items-center gap-1 text-xs font-medium text-accent">
            <span>{expanded ? '收起' : '查看摘要'}</span>
            <ChevronDown
              size={15}
              aria-hidden="true"
              className={cn('transition-transform duration-base ease-standard', expanded && 'rotate-180')}
            />
          </span>
        </button>
      </div>

      {expanded && (
        <div id={detailsId} className="border-t border-stroke px-4 pb-4 pt-3 sm:px-5">
          <div className="grid gap-4">
            <div>
              <h3 className="text-xs font-semibold text-ink">对话摘要</h3>
              <p className="mt-1.5 whitespace-pre-wrap text-sm leading-6 text-ink-secondary">
                {summary || '这次整理未生成可展示的摘要。'}
              </p>
            </div>

            <div>
              <h3 className="text-xs font-semibold text-ink">你明确提出的规划约束</h3>
              {constraints.length > 0 ? (
                <ul className="mt-2 grid gap-1.5 sm:gap-x-5 sm:grid-cols-2" aria-label="完整规划约束">
                  {constraints.map((constraint, index) => (
                    <li key={`${constraint}-${index}`} className="flex min-w-0 items-start gap-2 text-sm leading-6 text-ink-secondary">
                      <Check size={14} className="mt-1 flex-none text-success" aria-hidden="true" />
                      <span>{constraint}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-1.5 text-sm leading-6 text-ink-secondary">未提取到明确的规划约束。</p>
              )}
            </div>

          </div>
        </div>
      )}
    </section>
  );
};
