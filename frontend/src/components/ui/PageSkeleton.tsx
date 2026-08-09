import React from 'react';
import { cn } from '../../lib/utils';
import { PAGE_COLUMN, PAGE_GUTTER } from './PageShell';
import { Skeleton } from './Skeleton';

/**
 * 懒加载视图的通用页面骨架（懒加载分包）。
 *
 * **它必须和 `PageShell` 读同一根列、同一套内边距**（`PAGE_COLUMN` / `PAGE_GUTTER`）。
 * 在这里另写一个宽度（`max-w-[1400px]` 那类），每一次懒加载 chunk 落地那一刻内容列都会
 * 横跳一次 —— 实测能跳 176px，宽视口上到 316px。§4.2 那段「骨架与真实内容共用同一次入场，
 * 所以不会双闪」说的是**时间**上不闪；空间上跳不跳，取决于这里读的是不是同一根列。
 *
 * 形状取自 `PageShell` 的加载态：标题条 + 一条搜索线 + 四行刻线行。刻意**不含任何
 * 页面真实标题文案**（避免 e2e 的 `main h1/h2` 选择器误命中骨架），自身也不做入场
 * 动效——入场过渡由 `ViewRouter` 的 `AnimatePresence` 容器统一提供。
 *
 * 唯一动态是 `Skeleton` 原语自带的 `animate-pulse`（呼吸），表明「正在加载」而非
 * 「空态」。
 */
export const PageSkeleton: React.FC = () => (
  <div data-testid="page-skeleton" className="flex h-full flex-col overflow-hidden">
    <div className={cn('flex-shrink-0 pb-4 pt-6', PAGE_GUTTER)}>
      <div className={cn(PAGE_COLUMN, 'pl-14 lg:pl-0')}>
        <Skeleton radius="label" tone="surface" className="h-6 w-40" />
        <Skeleton radius="label" tone="surface" className="mt-2 h-3 w-56" />
        {/* 搜索那一条线：骨架也是一条线，不是一口井。 */}
        <div className="mt-4 h-11 border-b border-stroke" />
      </div>
    </div>

    <div className={cn('flex-1 overflow-hidden pb-6', PAGE_GUTTER)}>
      <div className={cn(PAGE_COLUMN, 'divide-y divide-stroke/60')}>
        {[0, 1, 2, 3].map((row) => (
          <div key={row} className="flex items-center justify-between gap-4 py-4">
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <Skeleton
                radius="label"
                tone="surface"
                className={row % 2 ? 'h-4 w-1/3' : 'h-4 w-1/2'}
              />
              <Skeleton radius="label" tone="surface" className="h-3 w-2/5" />
            </div>
            <Skeleton radius="label" tone="surface" className="h-3 w-20 flex-shrink-0" />
          </div>
        ))}
      </div>
    </div>
  </div>
);
