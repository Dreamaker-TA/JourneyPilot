import React from 'react';
import { AnimatePresence, m } from 'motion/react';
import { cn } from '../../lib/utils';
import { duration, easing, transitions } from '../../lib/motion';

/**
 * Per-digit rolling odometer (§5.2).
 *
 * Each numeric position is a vertical column carrying 0–9 stacked; the column is
 * driven by an `m.*` `y` transform to the target digit's slot, on
 * `transitions.numberRoll` (~450ms, decelerate). A carry naturally cascades:
 * higher digits roll at the same time because their target slot changes too.
 *
 * Re-targeting, not queueing: because the target `y` is derived from the current
 * `value` prop, a new value mid-roll simply re-targets the same animated `y`.
 * The motion library re-runs the tween from the live position, so high-frequency
 * updates (rAF-batched stream chunks) collapse into one continuous roll — they
 * never stack up (§5.2 "重定向不堆积").
 *
 * `tabular-nums` (global) keeps every digit the same width, so a rolling column
 * causes zero layout shift. When the number gains a digit the new column fades
 * in from the right.
 *
 * Non-digit characters (thousands separators, the compact "K"/"M" suffix, a "≈"
 * prefix, currency symbols) are rendered statically between the rolling columns.
 */

export interface RollingNumberProps {
  /** The raw numeric value to display. */
  value: number;
  /**
   * Formats the value into the display string (e.g. `formatTokensCompact`,
   * `toLocaleString`). Only characters 0–9 roll; everything else is static.
   */
  format: (value: number) => string;
  /** Optional static prefix, e.g. "≈" for estimated values (§5.1). */
  prefix?: string;
  className?: string;
  /** Digit column height in em (line box). Default 1.1. */
  digitHeightEm?: number;
  /** Optional stable selector for the readout root (e2e). */
  testId?: string;
}

/** Height of one digit cell, in em — the roll distance per digit step. */
const DEFAULT_DIGIT_HEIGHT = 1.1;

/**
 * A single rolling digit column. Holds 0–9 stacked vertically and translates to
 * the target slot. Because `animate.y` is derived from `digit`, a changed digit
 * re-targets the live transform (no queue).
 */
const DigitColumn: React.FC<{ digit: number; heightEm: number }> = ({
  digit,
  heightEm,
}) => {
  const targetY = `-${digit * heightEm}em`;
  return (
    <span
      className="relative inline-block tabular-nums"
      style={{
        height: `${heightEm}em`,
        lineHeight: `${heightEm}em`,
        width: '1ch',
        // 裁剪用 clip-path，**不要换成 `overflow: hidden`**。
        //
        // CSS 2.1 §10.8.1：inline-block 的基线是它最后一个流内行盒的基线，**除非它没有
        // 流内行盒、或者 `overflow` 的计算值不是 `visible` —— 那时基线是它的下外边距边。**
        // 这个窗口两条都会踩：内容只有绝对定位的数字列，不产生流内行盒。基线一旦变成窗口
        // 底边，浏览器就把窗口底边对到周围文字的基线上，整列数字浮成上标。
        //
        // `clip-path` 一样裁掉溢出，但不参与基线计算；配合下面那根 strut，这个窗口就有了
        // 一条**真的文字基线**，与数字格 0 的基线逐像素重合（同一个 line-height、同一个
        // 顶边）。也不要改成「按字体度量减去下伸部」来补偿：等宽栈是系统栈，每台机器的
        // ascent/descent 都不一样，那种写法必然在别的机器上错。
        clipPath: 'inset(0)',
      }}
    >
      {/* 基线 strut：流内、不可见、宽度正好 1ch（tabular 数字），只为给这个窗口一条基线。
          `visibility: hidden` 仍然生成行盒，`display: none` 不会 —— 这里必须是前者。 */}
      <span aria-hidden className="invisible">0</span>
      <m.span
        // 十个数字都在 DOM 里（只有目标那个透过裁剪露出来），读屏会把它们连成
        // 「0123456789」念一遍。视觉列整体让读屏跳过，值由根节点的 sr-only 文本负责。
        aria-hidden
        className="absolute left-0 top-0 flex flex-col"
        style={{ willChange: 'transform' }}
        animate={{ y: targetY }}
        transition={transitions.numberRoll()}
      >
        {Array.from({ length: 10 }, (_, n) => (
          <span
            key={n}
            // `block` + 居中对齐，**不是 flex 居中**：strut 与每一格必须用同一种排版方式，
            // 基线才会落在同一处。flex 居中是把字形按盒子中线摆，那与基线差一个半下伸部。
            className="block text-center tabular-nums"
            style={{ height: `${heightEm}em`, lineHeight: `${heightEm}em` }}
          >
            {n}
          </span>
        ))}
      </m.span>
    </span>
  );
};

/**
 * A static (non-rolling) character — separator, suffix, prefix, currency symbol.
 *
 * 就是一段普通的行内文字，**不要给它套定高盒子**（`inline-flex items-center` 之类）：
 * 那是把周围的字也改成按中线摆，让它们跟着数字一起偏离外部文字的基线。整个读数按基线
 * 对齐，数字窗口经由 strut 已经有真基线，这里本来就在同一条基线上。
 */
const StaticChar: React.FC<{ char: string }> = ({ char }) => <span>{char}</span>;

/** One display cell — a rolling digit, or a static character (separator/suffix). */
interface Cell {
  key: string;
  char: string;
  isDigit: boolean;
}

/**
 * Build stable per-cell keys anchored to the DECIMAL POINT (or right edge) so a
 * digit keeps the same key as the number grows — that lets the same column keep
 * rolling instead of remounting, and lets new leading columns fade in.
 */
function buildCells(text: string): Cell[] {
  const chars = text.split('');
  // Anchor index: the units digit. Prefer the char before a decimal separator;
  // otherwise the last digit in the string; otherwise the string end.
  const dotIdx = text.indexOf('.');
  let anchor = dotIdx >= 0 ? dotIdx - 1 : -1;
  if (anchor < 0) {
    for (let i = chars.length - 1; i >= 0; i--) {
      if (chars[i] >= '0' && chars[i] <= '9') {
        anchor = i;
        break;
      }
    }
  }
  if (anchor < 0) anchor = chars.length - 1;

  return chars.map((char, i) => {
    const isDigit = char >= '0' && char <= '9';
    // Position relative to the anchor keeps a digit's key stable across growth.
    const offset = i - anchor;
    return {
      key: isDigit ? `d${offset}` : `s${offset}:${char}`,
      char,
      isDigit,
    };
  });
}

export const RollingNumber: React.FC<RollingNumberProps> = ({
  value,
  format,
  prefix,
  className,
  digitHeightEm = DEFAULT_DIGIT_HEIGHT,
  testId,
}) => {
  const text = format(value);
  const cells = buildCells(text);

  return (
    <span
      data-testid={testId}
      // Test hook: the display string as one attribute. Each digit column stacks
      // 0–9 in the DOM (only the target one shows through the `clip-path` window),
      // so text-content assertions can't read the shown value — this can.
      data-value={prefix ? `${prefix}${text}` : text}
      // `items-baseline`，不是 `items-center`：这个读数要和它左右的普通文字坐在同一条
      // 基线上，而 flex 容器的 `align-items` 决定的正是这件事。根节点自己作为 inline-flex
      // 暴露给外部的基线取自第一个 flex item —— 经过 DigitColumn 的 strut，那就是数字的基线。
      className={cn('inline-flex items-baseline tabular-nums', className)}
    >
      {/* 读屏只念这一份；上面十格数字列整体 aria-hidden。 */}
      <span className="sr-only">{prefix ? `${prefix}${text}` : text}</span>
      {prefix ? <StaticChar char={prefix} /> : null}
      <AnimatePresence initial={false}>
        {cells.map((cell) =>
          cell.isDigit ? (
            <m.span
              key={cell.key}
              className="inline-block"
              initial={{ opacity: 0, width: 0 }}
              animate={{ opacity: 1, width: '1ch' }}
              exit={{ opacity: 0, width: 0 }}
              transition={transitions.enter(duration.base)}
            >
              <DigitColumn digit={Number(cell.char)} heightEm={digitHeightEm} />
            </m.span>
          ) : (
            <m.span
              key={cell.key}
              className="inline-block"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: duration.fast, ease: easing.decelerate }}
            >
              <StaticChar char={cell.char} />
            </m.span>
          )
        )}
      </AnimatePresence>
    </span>
  );
};
