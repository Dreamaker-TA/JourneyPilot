import React from 'react';
import { Info } from 'lucide-react';
import { cn } from '../../lib/utils';
import { Popover, type PopoverPlacement } from './Popover';
import { Tooltip } from './Tooltip';

/**
 * 检查面入口（Inspect Surface）：圆圈 i，点开看 audit-safe 细节。
 * 展开态仅为当次 local state，不写 localStorage、不记「本会话已展开」。
 */
export interface InspectHintProps {
  /** 浮层内容；无内容时不渲染触发器。 */
  children: React.ReactNode;
  /** 悬停微释义。 */
  label?: string;
  placement?: PopoverPlacement;
  /** 面板额外 class。 */
  panelClassName?: string;
  /** 触发器额外 class。 */
  className?: string;
  /** 测试 id。 */
  testId?: string;
  /** 紧凑尺寸（工具行内嵌）。 */
  size?: 'sm' | 'md';
}

export const InspectHint: React.FC<InspectHintProps> = ({
  children,
  label = '运行检查',
  placement = 'bottom-end',
  panelClassName,
  className,
  testId = 'inspect-hint',
  size = 'md',
}) => {
  if (children == null || children === false) return null;

  const dim = size === 'sm' ? 'h-5 w-5' : 'h-6 w-6';
  const icon = size === 'sm' ? 11 : 13;

  return (
    <Popover
      placement={placement}
      className={cn('max-w-[min(360px,calc(100vw-24px))]', panelClassName)}
      trigger={({ ref, open, toggle }) => (
        <Tooltip content={label} position="left" disabled={open}>
          <button
            ref={ref}
            type="button"
            data-testid={testId}
            aria-label={label}
            aria-expanded={open}
            onClick={(e) => {
              e.stopPropagation();
              toggle();
            }}
            className={cn(
              'inline-flex shrink-0 items-center justify-center rounded-card border border-stroke bg-surface text-ink-muted transition-[color,border-color,background-color] duration-fast ease-standard',
              'hover:border-accent/35 hover:bg-accent-soft hover:text-accent',
              open && 'border-accent/40 bg-accent-soft text-accent',
              dim,
              className
            )}
          >
            <Info size={icon} strokeWidth={2.25} />
          </button>
        </Tooltip>
      )}
    >
      <div data-testid={`${testId}-panel`} className="p-3 text-left">
        {children}
      </div>
    </Popover>
  );
};

/** 检查面段落标题。 */
export const InspectSectionTitle: React.FC<{ children: React.ReactNode; className?: string }> = ({
  children,
  className,
}) => (
  <p className={cn('text-[11px] font-semibold tracking-wide text-ink-secondary', className)}>{children}</p>
);

/** 检查面键值行。 */
export const InspectRow: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <div className="flex items-start justify-between gap-3 text-[11px] leading-relaxed">
    <span className="shrink-0 text-ink-muted">{label}</span>
    <span className="min-w-0 break-words text-right font-mono text-ink-secondary">{value}</span>
  </div>
);
