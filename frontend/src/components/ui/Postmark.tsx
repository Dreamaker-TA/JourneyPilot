import React from 'react';
import { cn } from '../../lib/utils';
import postmarkRing from '../../assets/marks/postmark.png';

/**
 * 报告封面上的邮戳 —— 一枚**说得出话**的图纸家具。
 *
 * 圈是 alpha 遮罩（与 `ui/ChartMark.tsx` 同一条烘焙管线、同一个耳语档），圆心印这份方案
 * 是哪天出的。**这一枚和 ChartMark 那几枚不是一类东西**，所以不做成它的又一个变体：
 * 那几枚是空态上的装饰性记号、不承载信息、`mark` 是一个封闭的字面量联合；这一枚存在的
 * 唯一理由是它承载一个事实。合同 §Color 因此把「结构性家具」（`<Neatline />`，图纸层
 * 每个面按定义都有）与「采纳性家具」（一个面自己挑的那一件）分开，**并要求后者必须
 * 承载信息** —— 一枚不说话的邮戳只是装饰。
 *
 * **没有日期就整枚不画。** 投影层的 `generated_at` 可以是 `null`（这份报告还没生成完），
 * 那时画一个空圈就等于把「没有这条信息」印成一条信息，是这个仓反复修掉的那个形状。
 *
 * 两个声部：圈走耳语档（`bg-ink-secondary/25`，§Color lines ≤ .25），日期走读数档
 * （`text-ink-muted`）。**信息该看得见，装饰该退后**，两者不能是同一个浓淡。
 */

/**
 * 遮罩按 2x 存（192px），所以显示别超过 96 —— 超过就会看出是位图。
 *
 * **尺寸是被字倒推出来的，而字号只能从合同的类型表里挑。** 这一条踩了两次才站稳：
 *
 * - 第一版 96px 圈配 **9px** 字，字两端贴着内圈；
 * - 第二版把圈放大到 112px 去迁就 **10px** 字 —— 看着是修好了，其实还是同一个错，
 *   因为 **10 不在 §Typography 的类型表里**（表是 11/12/13/14/15/16/20/24/30/36），
 *   一上屏就会抓住它。
 *
 * 正着推一遍：字取表里最小的 **11px** → `2026.07.16` 单行实测 **71px** → 单行要 126px 的
 * 圈，对这张封面太大 → **日期排两行**（`2026` / `07.16`，最宽一行约 36px）→ 96px 的圈
 * 净空 62px，两边各余 13px。两行本来也正是邮戳日戳的排法。
 */
const RING_PX = 96;

/** 圆心那块净空实测占全宽的 64.6%（遮罩中线上的墨隙 34..158 / 192）。 */
const CLEAR_RATIO = 0.646;

function stampDate(iso: string): { year: string; day: string; full: string } | null {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  const p = (n: number) => String(n).padStart(2, '0');
  const year = String(at.getFullYear());
  const day = `${p(at.getMonth() + 1)}.${p(at.getDate())}`;
  return { year, day, full: `${year}.${day}` };
}

export const Postmark: React.FC<{ generatedAt: string | null; className?: string }> = ({
  generatedAt,
  className,
}) => {
  const stamped = generatedAt ? stampDate(generatedAt) : null;
  if (!stamped) return null;

  return (
    <div
      // 取证钩子：判据要能量到「封面盖了戳」以及戳上印的是哪一天。
      data-postmark={stamped.full}
      className={cn('relative shrink-0', className)}
      style={{ width: RING_PX, height: RING_PX }}
    >
      <span
        aria-hidden
        style={{
          width: RING_PX,
          height: RING_PX,
          maskImage: `url(${postmarkRing})`,
          WebkitMaskImage: `url(${postmarkRing})`,
          maskSize: 'contain',
          WebkitMaskSize: 'contain',
          maskRepeat: 'no-repeat',
          WebkitMaskRepeat: 'no-repeat',
          maskPosition: 'center',
          WebkitMaskPosition: 'center',
        }}
        className="block bg-ink-secondary/25"
      />
      <span
        // 日期比圈**重**一档：圈是装饰、走耳语，日期是这枚戳存在的理由、走读数。
        // 两者同一个浓淡的话，读者会把它整枚当成背景纹样略过。
        className="absolute inset-0 flex flex-col items-center justify-center gap-0.5 text-center font-mono text-[11px] leading-none tabular-nums tracking-[0.04em] text-ink-secondary"
        // 只在净空里排字，不压到刻线上。
        style={{ padding: `0 ${Math.round((RING_PX * (1 - CLEAR_RATIO)) / 2)}px` }}
      >
        <span>{stamped.year}</span>
        <span>{stamped.day}</span>
      </span>
    </div>
  );
};
