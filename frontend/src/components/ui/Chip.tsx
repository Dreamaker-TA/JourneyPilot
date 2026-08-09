import React from 'react';
import { cn } from '../../lib/utils';

/**
 * 一枚可点的芯片 —— 「从一行里挑」的那一种控件。
 *
 * 两个消费者：我的偏好的六组选项（多选 / 单选），旅行风格库的三枚筛选页签。它们是**同一
 * 副身体语言**（一行里挑，选中态要一眼看见），所以只有一份实现；同一个角色两处各写一遍，
 * 就是「同一个角色两套值」的前一步 —— 暂时还相等，早晚会漂开。
 *
 * ── 未选中态必须有描边 ──────────────────────────────────────────────────────
 * 未选中若是**裸文字**（`text-ink-secondary`，无框无底），一屏 30 枚读起来就会是一整片
 * 灰色的词，读者分不出这里能按。「界面上本身不需要那么多条框」针对的是首屏 900px 列里
 * 十六个填色井套四层，不是「控件不许有边界」：一枚 4px 标签档的描边芯片不是一口井，
 * 「在合适的地方加框」是这条线的方向。
 *
 * ── 选中态是满面淡底 + 描边着色 + 加粗，不是色条 ────────────────────────────────
 * 「No colour bar accents for selection — use full-surface tint + weight」。
 * 三个通道一起说同一件事（底、边、字重）。**必须是三个**：淡底那一档在暖纸上对纸只有
 * 1.15 的对比（见下），单靠它「选中」几乎看不出来 —— 这正是单档 `bg-accent/10` 淡底
 * 读起来弱的原因。描边与字重把它抬起来。
 *
 * ── 淡底是 **10%**，而这一档是天花板不是口味 ────────────────────────────────────
 * `text-accent` 压在 `bg-accent/α` 上，实测（暖纸 #F5F1E4 / 纸白 #FCFBF6）：
 *
 *     α        暖纸 #F5F1E4     纸白 #FCFBF6
 *     0.08         4.64            5.04
 *     0.10         4.51            4.90   ← 天花板
 *     0.12         4.38 ✗          4.75
 *     0.18         4.01 ✗          4.34 ✗
 *
 * 淡底在每张纸上各有一个值：18% 只在那块深墨底板上能过（5.68），在暖纸上只有 4.01。
 * 10% 是这张表里任何一张纸都达标的最大 α —— 它是一档天花板，不是口味。
 */
export const Chip: React.FC<{
  selected?: boolean;
  onClick: () => void;
  /** 可及名字，用于读屏与判据；缺省取 children 的文字。 */
  label?: string;
  disabled?: boolean;
  children: React.ReactNode;
  className?: string;
}> = ({ selected = false, onClick, label, disabled, children, className }) => (
  <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    aria-pressed={selected}
    aria-label={label}
    className={cn(
      // 命中区 36px（`min-h-9`）；粗指针档由 index.css 的触摸契约抬到 44px。
      'inline-flex min-h-9 items-center rounded-label border px-3 text-[13px]',
      'transition-colors duration-fast ease-standard',
      'disabled:pointer-events-none disabled:opacity-50',
      selected
        ? 'border-accent bg-accent/10 font-semibold text-accent'
        : 'border-stroke font-medium text-ink-secondary hover:border-ink-muted hover:text-ink',
      className
    )}
  >
    {children}
  </button>
);
