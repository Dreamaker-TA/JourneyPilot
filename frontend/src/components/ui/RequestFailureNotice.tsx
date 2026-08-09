import React from 'react';
import { AlertCircle, RefreshCw, RotateCcw } from 'lucide-react';
import { Button } from './Button';
import { cn } from '../../lib/utils';
import type { RequestFailure } from '../../lib/requestFailureMessage';

/**
 * 一次读写失败在界面上的样子 —— 一处定义，四个屏同读。
 *
 * 四个屏（旅行风格库、资料来源、规划器配置、风格选择器）读的是这一份，**不许
 * 各写一遍**：各写一遍就会漂开 —— 有的无条件印「重试」，有的一个键都不给，有的还用红色
 * error 块，而 §UX Copy 写着「Avoid alarm-heavy red blocks for recoverable product
 * states」。恢复动作由 `describeRequestFailure` 算出来，**判断只有一处**，就是下面的
 * `RecoveryAction`。
 *
 * `data-recovery` 落到 DOM 上是给钉用的：钉要能量出「404 时这一屏没有重试键」，
 * 而不是去数按钮文字。
 */

/**
 * 那一句话之后能按的键。没有可按的（403）时什么都不渲染 —— 给一个按了没用的键
 * 比不给键更不诚实。
 */
export const RecoveryAction: React.FC<{
  failure: RequestFailure;
  onRetry: () => void;
  className?: string;
}> = ({ failure, onRetry, className }) => {
  if (failure.recovery === 'none') return null;
  const reload = failure.recovery === 'reload';
  return (
    <Button
      variant="secondary"
      size="sm"
      data-testid="request-failure-action"
      onClick={reload ? () => window.location.reload() : onRetry}
      className={className}
    >
      {reload ? <RotateCcw size={14} aria-hidden /> : <RefreshCw size={14} aria-hidden />}
      {reload ? '刷新页面' : '重试'}
    </Button>
  );
};

export const RequestFailureNotice: React.FC<{
  /** 「暂时读不到旅行风格」这类一句话标题：说清刚才哪一屏没读到东西。 */
  title: string;
  failure: RequestFailure;
  onRetry: () => void;
  className?: string;
}> = ({ title, failure, onRetry, className }) => (
  <div
    role="alert"
    data-testid="request-failure-notice"
    data-recovery={failure.recovery}
    className={cn('rounded-card border border-warning/30 bg-warning/10 p-4', className)}
  >
    <div className="flex items-start gap-2">
      <AlertCircle size={16} className="mt-0.5 shrink-0 text-warning" aria-hidden />
      <div className="min-w-0">
        <p className="text-sm font-semibold text-ink">{title}</p>
        <p className="mt-1 break-words text-xs leading-relaxed text-ink-secondary">{failure.message}</p>
        <RecoveryAction failure={failure} onRetry={onRetry} className="mt-3" />
      </div>
    </div>
  </div>
);
