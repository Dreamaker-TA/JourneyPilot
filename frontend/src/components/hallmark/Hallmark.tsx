import React, { useState } from 'react';
import { Popover } from '../ui/Popover';
import { Tooltip } from '../ui/Tooltip';
import { HALLMARK_REGISTRY } from './registry';
import type { ContextReport } from '../../types/chat';
import type { CostLedgerView } from '../../lib/costLedger';

/**
 * 工艺印记。
 *
 * 一枚印记 = 符号 + 微释义 + 数据浮窗，由本组件统一渲染，行为查注册表：
 *
 * - 符号：12px 几何字形（非 emoji、非彩色 icon），静息 ink-muted + opacity 0.45，
 *   hover/展开态 accent，过渡走 duration.fast；点击热区 24×24。
 * - Rest → Hover（300ms delay 微释义，走 ui/Tooltip）→ Click（M2 ui/Popover 原地展开，
 *   transformOrigin 取印记角位）。浮窗打开时 Tooltip 抑制。
 * - data 为空则整个组件不渲染——印记的出现本身就是数据存在的证明。
 *   **「有没有真东西可说」不在这里判**：那一句写在数据的产地 ——
 *   `context` 由后端 `memory/context_builder.py::build_context_report` 判（没东西可说就返回
 *   None，两条路径于是不发 `context_report`）；
 *   `cost` 由 `lib/costLedger.ts::buildCostLedgerView` 判（一次调用都没落账就返回 null）。
 *   这里这句 `if (!data)` 只是「没有数据就没有印记」，不是第二处空判。
 *
 * 定位由宿主负责（右上角 absolute）；本组件只渲染触发器与浮层。
 */

/**
 * glyph 与 data 成对，**在类型上**绑住：
 * 接第三枚印记时新加一个分支，编译器会逼着调用点传对它那一种数据。
 */
type HallmarkProps =
  | { glyph: 'context'; data: ContextReport | null | undefined }
  | { glyph: 'cost'; data: CostLedgerView | null | undefined };

const HallmarkTrigger: React.FC<{
  symbol: string;
  label: string;
  // `triggerRef`（非保留字 `ref`）：函数组件不 forwardRef，用 `ref` 命名会被 React 拦截，
  // 导致 Popover 的 triggerRef 无法附着到按钮、定位测量拿不到锚点矩形。
  triggerRef: React.Ref<HTMLButtonElement>;
  open: boolean;
  toggle: () => void;
  testId: string;
}> = ({ symbol, label, triggerRef, open, toggle, testId }) => {
  const [hovered, setHovered] = useState(false);
  // 三态：静息 ink-muted@0.45 → hover/展开态 accent@1，opacity 与着色同时
  // 走 duration.fast。用受控状态声明式驱动，避免命令式 DOM 改写与 transition 抢占。
  const lit = open || hovered;

  return (
    <button
      ref={triggerRef}
      type="button"
      data-testid={testId}
      // 只有字形、没有可见文字 —— 名字只能写在控件自己身上。
      aria-label={label}
      aria-expanded={open}
      onClick={toggle}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      // 24×24 视觉盒；12px 字形居中。coarse pointer 下命中区由 `index.css` 那条按元素选的
      // 规则补到 44×44 —— 这里不写类名，热区不再是调用点的可选项。
      className="flex h-6 w-6 items-center justify-center leading-none transition-[color,opacity] duration-fast ease-standard"
      style={{
        fontSize: '12px',
        color: lit ? 'var(--color-accent)' : 'var(--color-ink-muted)',
        opacity: lit ? 1 : 0.45,
      }}
    >
      {/* 字形自己 aria-hidden：名字由 aria-label 给，读屏不该再念一遍「◈」。 */}
      <span aria-hidden>{symbol}</span>
    </button>
  );
};

export const Hallmark: React.FC<HallmarkProps> = (props) => {
  if (!props.data) return null;

  // `glyph` 与 `data` 的配对由 props 的联合类型保证；这里按 glyph 各取自己那一格的
  // 注册项与渲染函数，不做 `as` 断言 —— 断言会让「换了 glyph 忘了换 data」静默通过。
  const entry = props.glyph === 'context' ? HALLMARK_REGISTRY.context : HALLMARK_REGISTRY.cost;
  const { symbol, ariaLabel } = entry;
  const { blurb, body } =
    props.glyph === 'context'
      ? {
          blurb: HALLMARK_REGISTRY.context.blurb(props.data),
          body: HALLMARK_REGISTRY.context.render(props.data),
        }
      : {
          blurb: HALLMARK_REGISTRY.cost.blurb(props.data),
          body: HALLMARK_REGISTRY.cost.render(props.data),
        };

  return (
    <Popover
      placement="bottom-end"
      trigger={({ ref, open, toggle }) => (
        // Tooltip 抑制：浮窗打开时不显示微释义，避免与浮窗叠加。
        <Tooltip content={blurb} position="left" disabled={open}>
          <HallmarkTrigger
            symbol={symbol}
            label={ariaLabel}
            triggerRef={ref}
            open={open}
            toggle={toggle}
            testId={`hallmark-${props.glyph}`}
          />
        </Tooltip>
      )}
    >
      {body}
    </Popover>
  );
};
