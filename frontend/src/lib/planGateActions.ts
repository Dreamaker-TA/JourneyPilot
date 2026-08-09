import type { PlanApprovalGate, PlanGateDecisionAction } from '../types/chat';
import type { TripSupplementCategory } from '../types/api';

/**
 * 计划门那一屏「有哪几个决定可按」与「用户写的要求怎么变成 content」——**一处定义**。
 *
 * ## 它修的是什么
 *
 * 后端 `workflows/travel_planning.py::_build_plan_gate_payload` 逐轮算出
 * `decision_options`（首轮 `approve/edit/supplement/cancel`，用满修改额度后
 * `approve/cancel`），`api/sse_projection.py::public_plan_gate_payload` 原样下发，
 * `planApprovalGate.ts` 也归一成了 `decisionOptions` —— 然后**没有任何东西读它**。
 * 屏幕上的按钮集由组件里的 `revisionLimitReached` 独立决定，而界面从来只发得出
 * `approve` 与 `cancel`。
 *
 * 后果是一串连锁：`edit` / `supplement` 没有入口 → `plan_gate_revision_count` 恒 0
 * → `revision_limit_reached` 恒 False → 那条「用满额度只剩批准/取消」的分支在产品里
 * **不可达**，钉它的判据在过滤空集。也就是说 `decision_options` 在广告一个客户端发不出的
 * 动作，而按钮集有第二个 owner —— 两处各写一份会漂开。
 *
 * ## 现在的 owner 划分（一件事一个 owner）
 *
 * - **哪几个控件在场：`decisionOptions`，只此一处。** `planGateEntries` 是它到控件的
 *   唯一映射，组件不许再自己判一次。后端广告什么，屏幕上就有什么；反之，屏幕上没有的
 *   动作后端也不许广告：广告出来的每个动作都真的被 `plan_gate_node` 接受，
 *   不会被重新 interrupt。
 * - **额度还剩多少、为什么少了两个键：`revision` / `revisionLimit` /
 *   `revisionLimitReached`。** 它们**不**决定按钮集，只负责把按钮集的理由说出来 ——
 *   第二轮的卡片如果只是默默少两个键，用户读不出「修改额度用完了」这件事。
 * - **用户写的要求怎么变成一段 content：`composeRequirementContent`，只此一处。**
 *   `approve` 与 `supplement` 走同一段文本：前者把它并进 worker 系统提示，后者把它送回
 *   planner 重规划。两处各拼一遍，就会出现「补充想法在批准时算、在重规划时不算」这种
 *   一个数两套值。
 */

/** 单条追加要求的最大字数。硬限制：任一条超出就提交不了（输入不截断）。 */
export const SUPPLEMENT_MAX = 50;

/**
 * 追加信息的分类表。**标签只在这里写一次** —— 下拉里显示的那份和拼进 content 的那份
 * 是同一张表，否则用户选「饮食口味」而模型收到的是 `food`。
 */
export const SUPPLEMENT_CATEGORIES: Array<{ value: TripSupplementCategory; label: string }> = [
  { value: 'food', label: '饮食口味' },
  { value: 'transport', label: '交通与租车' },
  { value: 'accommodation', label: '住宿倾向' },
  { value: 'pace', label: '每日节奏' },
  { value: 'must_do', label: '必去体验' },
  { value: 'other', label: '其他要求' },
];

export function supplementCategoryLabel(value: TripSupplementCategory | ''): string {
  return SUPPLEMENT_CATEGORIES.find((item) => item.value === value)?.label || '其他要求';
}

export interface RequirementRow {
  category: TripSupplementCategory | '';
  content: string;
}

/** 任一条超出 `SUPPLEMENT_MAX` —— 提交被硬拦，理由由行下那条 `role="alert"` 说出来。 */
export function requirementRowsOverLimit(rows: RequirementRow[]): boolean {
  return rows.some((row) => row.content.trim().length > SUPPLEMENT_MAX);
}

/**
 * 把卡上累积的「追加信息」行与「补充想法」拼成 `gate_decision.content`。
 *
 * 空行整条不要：一条只有分类没有内容的要求送到模型那里，是让它去满足一句空话。
 */
export function composeRequirementContent(rows: RequirementRow[], guide: string): string {
  const lines = rows
    .filter((row) => row.content.trim())
    .map((row) => `[${supplementCategoryLabel(row.category)}] ${row.content.trim()}`);
  const guideText = guide.trim();
  if (guideText) lines.push(`[补充想法] ${guideText}`);
  return lines.join('\n');
}

export type PlanGateEntries = Record<PlanGateDecisionAction, boolean>;

/**
 * 这一屏该出现哪几个控件 —— 完全由 `decisionOptions` 决定。
 *
 * 唯一的例外是 `cancelled`：取消之后卡片留着供回看（`conversationFlow.ts` 那条投影规则
 * 依赖它），但一个已经结束的决定不该再有任何可按的东西。
 */
export function planGateEntries(
  gate: Pick<PlanApprovalGate, 'decisionOptions' | 'status'>,
): PlanGateEntries {
  const live = gate.status !== 'cancelled';
  const has = (action: PlanGateDecisionAction) => live && gate.decisionOptions.includes(action);
  return {
    approve: has('approve'),
    edit: has('edit'),
    supplement: has('supplement'),
    cancel: has('cancel'),
  };
}

/** 这一轮还能让 planner 重规划几次。 */
export function planGateRevisionsLeft(
  gate: Pick<PlanApprovalGate, 'revision' | 'revisionLimit'>,
): number {
  return Math.max(0, gate.revisionLimit - gate.revision);
}

/**
 * 修改额度那一行要说的话；没有话说时是 `null`（不印一行「剩余 0 次」的废话）。
 *
 * 两种情况才有话说：
 * - 额度已用满 —— 必须说，否则第二轮的卡片只是默默少了两个键。
 * - 还能改，而且这一轮真的广告了修改入口 —— 说清还剩几次，用户才知道这一次值不值得花。
 */
export function planGateBudgetCopy(
  gate: Pick<PlanApprovalGate, 'decisionOptions' | 'status' | 'revision' | 'revisionLimit' | 'revisionLimitReached'>,
): string | null {
  if (gate.status === 'cancelled') return null;
  const entries = planGateEntries(gate);
  if (gate.revisionLimitReached) {
    return '这份计划已经按你的要求改过了，本次规划的修改额度到此为止 —— 请确认它，或取消本次运行。';
  }
  if (!entries.edit && !entries.supplement) return null;
  const left = planGateRevisionsLeft(gate);
  if (left <= 0) return null;
  return `你还可以让我按你的要求重新规划 ${left} 次；直接确认则立即开始调研。`;
}
