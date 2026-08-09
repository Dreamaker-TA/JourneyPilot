import React from 'react';
import type { ContextReport } from '../../types/chat';
import type { CostLedgerView } from '../../lib/costLedger';
import { costLedgerBlurb } from '../../lib/costLedger';
import { ContextLens } from './ContextLens';
import { CostLedger } from './CostLedger';

/**
 * 能力族群注册表。
 *
 * 一个 glyph 键映射到：几何字形符号、读屏用的名字、悬停微释义一句话、点击浮窗组件。
 * 注册表模式保证后续能力接入只需「一条 SSE 契约 + 一个浮窗组件」，符号与交互不再重新设计
 * —— `cost ¤` 依赖 `usage_update` 与 `chat_complete.run_cost_summary` 两条契约，
 * 缺的只是一个浮窗。`verify ◇` 仍是预留。
 */

export type HallmarkGlyph = 'context' | 'cost';

export interface HallmarkEntry<Data> {
  /** 12px 几何字形（非 emoji、非彩色 icon）。 */
  symbol: string;
  /**
   * 读屏软件念出来的名字。
   *
   * **必须有**：这枚钮只有一个字形、没有可见文字，所以它的可及名只能由 `aria-label` 给。
   * 从 `<span>{symbol}</span>` 捡来的可见文字形式上算「有名字」，实际上什么也没说。
   * 名字**不**从 `ui/Tooltip` 的 `content` 来（理由在那个文件的头注释）。
   */
  ariaLabel: string;
  /** 悬停微释义（一句话，走 ui/Tooltip）。可依数据取措辞。 */
  blurb: (data: Data) => string;
  /** 点击原地展开的浮窗正文。 */
  render: (data: Data) => React.ReactNode;
}

export const HALLMARK_REGISTRY: {
  context: HallmarkEntry<ContextReport>;
  cost: HallmarkEntry<CostLedgerView>;
} = {
  context: {
    symbol: '◈',
    ariaLabel: '本次参考的信息',
    // 悬停这一句与浮窗标题「本次参考的信息」说的是同一件事。印记只在真有东西可列（或
    // 这一轮刚整理过较早对话）时出现，所以这句话对每一次出现都成立。
    blurb: () => '这轮回答参考了已知的旅行信息',
    render: (data) => <ContextLens report={data} />,
  },
  cost: {
    symbol: '¤',
    ariaLabel: '本轮模型用量',
    // 运行中与终结后措辞不同，因为它们说的不是同一件事：一个还在涨，一个已经定格。
    // 两句都由 `lib/costLedger.ts` 那一处给 —— 悬停语与浮窗读的是同一份视图。
    blurb: costLedgerBlurb,
    render: (data) => <CostLedger view={data} />,
  },
};
