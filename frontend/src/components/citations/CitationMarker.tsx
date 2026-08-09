import React from 'react';
import { BookOpen, ExternalLink } from 'lucide-react';
import type { FinalAnswerCitation } from '../../types/chat';
import { cn } from '../../lib/utils';
import { Popover } from '../ui/Popover';

interface CitationMarkerProps {
  citation: FinalAnswerCitation;
  testId?: string;
}

/**
 * 行内正文引用标记 —— 点开浮层看这处claim的支撑来源。
 *
 * 浮层是锚定浮层 → 收编进 `ui/Popover`：portal 逃出正文容器的 overflow，
 * Escape 关闭 + 返焦触发器，外点关闭。不再手写 `role="dialog"`（弹层角色只在
 * `ui/Modal` 一处定义，业务组件一行 role 都不写）。
 *
 * 文字字号：面板里 11–12px 的正文要过 WCAG 4.5:1。面板底色是 panel，所以正文用
 * `text-ink-secondary`（对 panel 达标）；来源标题用 `text-ink`。断言在 e2e 里按
 * `citation-popover` / `信息` 找，见 `ix-m2-flows` 与 `workspace-layout`。
 */
export const CitationMarker: React.FC<CitationMarkerProps> = ({ citation, testId }) => {
  const sourceCount = citation.sources.length;

  return (
    <Popover
      portal
      testId="citation-popover"
      className="max-h-[min(26rem,calc(100vh-1.5rem))] overflow-y-auto p-3.5"
      trigger={({ ref, open, toggle }) => (
        <button
          ref={ref}
          type="button"
          data-testid={testId || 'citation-marker'}
          onClick={toggle}
          className="mx-0.5 inline-flex h-5 w-5 translate-y-[-0.12em] items-center justify-center rounded-full text-chart transition-colors duration-fast ease-standard hover:bg-accent-soft hover:text-accent"
          aria-label={`查看 ${sourceCount} 处来源`}
          aria-expanded={open}
          aria-haspopup="true"
        >
          <BookOpen size={12} aria-hidden="true" />
        </button>
      )}
    >
      {() => (
        <div className="text-left text-xs font-normal leading-relaxed text-ink-secondary">
          {citation.claimText && (
            <p className="mb-2.5 font-medium leading-relaxed text-ink">{citation.claimText}</p>
          )}
          <div className="space-y-3">
            {citation.sources.map((source, sourceIndex) => (
              <div
                key={`${source.url || source.title || source.sourceName || 'source'}-${sourceIndex}`}
                className={cn(sourceIndex > 0 && 'border-t border-stroke/60 pt-3')}
              >
                <div className="flex flex-wrap items-center gap-1.5">
                  {source.authorityLabel && (
                    <span className="rounded-label bg-[var(--color-accent-soft)] px-1.5 py-0.5 font-semibold text-accent">
                      {source.authorityLabel}
                    </span>
                  )}
                  <span className="font-semibold text-ink">
                    {source.title || source.sourceName || `参考来源 ${sourceIndex + 1}`}
                  </span>
                </div>
                {source.snippet && <p className="mt-1.5 text-ink-secondary">{source.snippet}</p>}
                {source.url && (
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-2 inline-flex items-center gap-1 font-semibold text-accent hover:underline"
                  >
                    查看原始来源
                    <ExternalLink size={11} />
                  </a>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </Popover>
  );
};
