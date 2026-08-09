import React from 'react';
import { cn } from '../../lib/utils';
import blankTabs from '../../assets/marks/blank-tabs.png';
import emptySleeve from '../../assets/marks/empty-sleeve.png';

/**
 * 空态的图纸标记。
 *
 * 空态各借**一件**图纸家具，而不是挂一枚通用 lucide 图标（`Map` / `FileText` /
 * `Sparkles` 那类）—— 一个本该说「这一屏还什么都没有」的位置上顶着通用图标，什么也没说。
 * 词汇出自 §Design Direction 的参考混：「1960s aeronautical charts / Field Notes: grid +
 * contour + neatline + waypoint pins」。
 *
 * 这里原来有第三枚 `chart-fragment`，挂在「我的行程」的真空态上。**随那一屏
 * 一起删掉了遮罩、位图与这条登记**：留着它就是一件没有任何一屏会挂的家具，而这个组件的
 * 全部约束（一屏一枚、由调用点显式写出）都建立在「每一枚都指名道姓属于哪一屏」上。
 *
 * 三条合同约束，缺一条这个组件就不该存在：
 *
 * 1. §Color「Chart furniture … belongs to the homepage empty state; other views may adopt
 *    **a single element deliberately, never by default**」——所以是一屏一枚、只在空态、
 *    由调用点显式写出，不做成任何默认装饰。§Anti-Slop 同一条的反面（「never migrates into
 *    conversation, panels, or settings **by default**」）说的是同一件事。
 * 2. §Color 的耳语档：lines ≤ .25。着色走 token 的 `bg-*`，默认 `ink-secondary/25`。
 * 3. §Anti-Slop「No repeated icon-card soup」——所以不给「随便传张图」的口子：
 *    `mark` 是两个字面量的联合类型，加一枚必须先改这里、先回答它属于哪一屏。
 *
 * **为什么是 alpha 遮罩而不是直接贴图**：颜色必须跟着 token 走。一张自带白底的位图压在
 * `#f5f1e4` 的制图纸上会露出一块白方块，深色主题下更是反的。遮罩由 `background-color`
 * 上色，换 token、换主题都不用重新出图。
 *
 * **为什么是位图而不是 SVG**：这几张是文生图产出的线稿，仓里没有矢量化工具（potrace /
 * inkscape / imagemagick 都没有）。位图只在这一个尺寸档（96–112px）用，所以够用。
 * 分类字形**不能**这么办 —— 产品在 14/18px 渲染它们，位图细线在那个尺寸下发灰糊掉，
 * 所以那一套是手写 SVG 路径（`preset/presetIcons.tsx`）。**尺寸档决定载体**，不是「这批
 * 是图就都当图用」。
 *
 * 报告封面那枚邮戳（`ui/Postmark.tsx`）走同一条烘焙管线、同一个耳语档，但**不是**这里的
 * 一员：这几枚不承载信息，它承载一个日期，两件事在 §Color 里是分开记账的。
 */

const MARK_SRC = {
  'empty-sleeve': emptySleeve,
  'blank-tabs': blankTabs,
} as const;

export type ChartMarkName = keyof typeof MARK_SRC;

interface ChartMarkProps {
  mark: ChartMarkName;
  /** 边长（px）。三处空态用 104–112；位图按 2x 存，别超过 112，超过就会看出是位图。 */
  size?: number;
  /** 只允许覆盖着色（`bg-*`）与外边距；形状与遮罩参数不对外开放。 */
  className?: string;
}

export const ChartMark: React.FC<ChartMarkProps> = ({ mark, size = 104, className }) => {
  const src = MARK_SRC[mark];
  return (
    <span
      aria-hidden
      // 取证钩子：判据要能量到「这一屏挂的是哪一枚」以及它的着色不透明度。
      data-chart-mark={mark}
      style={{
        width: size,
        height: size,
        maskImage: `url(${src})`,
        WebkitMaskImage: `url(${src})`,
        maskSize: 'contain',
        WebkitMaskSize: 'contain',
        maskRepeat: 'no-repeat',
        WebkitMaskRepeat: 'no-repeat',
        maskPosition: 'center',
        WebkitMaskPosition: 'center',
      }}
      className={cn('block shrink-0 bg-ink-secondary/25', className)}
    />
  );
};
