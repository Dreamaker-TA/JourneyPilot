import type { CSSProperties } from 'react';
import { cn } from '../../lib/utils';

/**
 * 图纸边界 —— neatline 细框 + 四角登记刻线。
 *
 * 这是「图纸」这一档形状的**边界表达**。最外层容器半径是 0，
 * 它的边界不靠圆角给出，靠这件家具给出。四个消费者共用这一份：空首页图纸、
 * 报告封面、工作台外壳、正式结果面。
 *
 * 用法：父级必须是 `relative`；这一枚是绝对定位的覆盖层，`pointer-events: none`，
 * 排在内容之下（`z-0`）。
 *
 * `inset` 只在首页图纸上给（20px 天头）。其余三处贴边，用默认的 0。
 */
export function Neatline({
  inset,
  enter = false,
  className,
}: {
  inset?: string;
  /** 一次性入场绘制。只有首页图纸用；常驻边界不画。 */
  enter?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn('neatline', enter && 'neatline-enter', className)}
      style={inset ? ({ '--neatline-inset': inset } as CSSProperties) : undefined}
      aria-hidden
    >
      <span className="neatline-tick neatline-tick-tl" />
      <span className="neatline-tick neatline-tick-tr" />
      <span className="neatline-tick neatline-tick-bl" />
      <span className="neatline-tick neatline-tick-br" />
    </div>
  );
}
