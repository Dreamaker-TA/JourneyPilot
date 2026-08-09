import type {
  PlanApprovalGate,
  PlanGateDecisionAction,
  PlanGateMustObey,
  PlanGateStep,
} from '../types/chat';

/**
 * 后端可能在 `decision_options` 里广告的全部动作。
 *
 * 导出是因为这里定义的每一个动作都要有对应的界面控件 ——
 * 此前 `edit` 被广告了十几轮而界面从来发不出它 ——
 * 一个只在一头存在的动作，两头各自都看不出问题。
 */
export const PLAN_GATE_ACTIONS: PlanGateDecisionAction[] = ['approve', 'edit', 'supplement', 'cancel'];

/**
 * 「本轮必须遵守」的归一。
 *
 * 只取 `constraint_id` 与 `public_summary`：那是这一屏会说出来的两样。缺任何一样的
 * 条目**整条不要** —— 一行没有内容的「必须遵守」比不显示更糟，它在屏幕上画出一条
 * 用户读不懂也没法照办的硬约束。后端已不再下发 `category` /
 * `enforcement_scope`。
 */
function normalizeMustObey(raw: unknown): PlanGateMustObey[] {
  if (!Array.isArray(raw)) return [];
  const rows: PlanGateMustObey[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue;
    const row = entry as Record<string, unknown>;
    const constraintId = String(row.constraint_id || '').trim();
    const summary = String(row.public_summary || '').trim();
    if (!constraintId || !summary) continue;
    rows.push({ constraintId, summary });
  }
  return rows;
}

export function normalizePlanApprovalGate(
  event: { run_id?: string; gate?: string; payload?: Record<string, unknown> },
  status: PlanApprovalGate['status'] = 'pending',
): PlanApprovalGate | null {
  const payload = event.payload || {};
  const plan = payload.plan && typeof payload.plan === 'object' ? payload.plan as Record<string, unknown> : {};
  const rawSteps = Array.isArray(plan.steps) ? plan.steps : [];
  const steps: PlanGateStep[] = [];
  for (const item of rawSteps) {
    if (!item || typeof item !== 'object') continue;
    const row = item as Record<string, unknown>;
    const agents = Array.isArray(row.agents) ? row.agents.map(String) : [];
    const tasks = row.tasks && typeof row.tasks === 'object'
      ? Object.fromEntries(Object.entries(row.tasks as Record<string, unknown>).map(([key, value]) => [key, String(value || '')]))
      : {};
    steps.push({ step: Number(row.step) || 0, agents, tasks });
  }
  if (!event.run_id || steps.length === 0) return null;
  const rawOptions = Array.isArray(payload.decision_options) ? payload.decision_options.map(String) : [];
  const decisionOptions = rawOptions.filter((option): option is PlanGateDecisionAction =>
    (PLAN_GATE_ACTIONS as string[]).includes(option)
  );
  if (decisionOptions.length === 0) return null;
  const revisionLimitReached = Boolean(payload.revision_limit_reached);
  return {
    gate: event.gate || String(payload.gate || 'plan'),
    runId: event.run_id,
    status,
    revision: Number(payload.revision) || 0,
    revisionLimit: Number(payload.revision_limit) || 1,
    revisionLimitReached,
    steps,
    planText: String(payload.plan_text || ''),
    decisionOptions,
    mustObey: normalizeMustObey(payload.must_obey),
  };
}
