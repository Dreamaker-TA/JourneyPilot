import React from 'react';
import { m } from 'motion/react';
import { Check, ChevronDown, ListPlus, Loader2, PencilLine, Plus, RefreshCw, Square, X } from 'lucide-react';
import { useApp } from '../../context/AppContext';
import { useSendMessage } from '../../hooks/useSendMessage';
import type { PlanGateDecisionAction, TripSupplementCategory } from '../../types/api';
import { Button } from '../ui/Button';
import { ConfirmAction } from '../ui/ConfirmAction';
import { Modal } from '../ui/Modal';
import { cn, generateId } from '../../lib/utils';
import { emphasisEnter } from '../../lib/motion';
import { RESEARCH_START_TEXT } from '../../lib/conversationFlow';
import {
  SUPPLEMENT_CATEGORIES,
  SUPPLEMENT_MAX,
  composeRequirementContent,
  planGateBudgetCopy,
  planGateEntries,
  requirementRowsOverLimit,
} from '../../lib/planGateActions';

const AGENT_LABELS: Record<string, string> = {
  destination_researcher: '目的地与体验',
  transport_researcher: '交通可行性',
  accommodation_researcher: '住宿选择',
  itinerary_planner: '每日路线编排',
};

interface SupplementRow {
  id: string;
  category: TripSupplementCategory | '';
  content: string;
}

/**
 * 计划门：一张计划卡承载这一轮所有的决定。
 *
 * ## 四个决定，一个 owner
 *
 * 屏幕上有哪几个控件**只由后端的 `decisionOptions` 决定**（映射写在
 * `lib/planGateActions.ts::planGateEntries`，组件不再自己判一次）：
 *
 * - `approve`「确认调研计划并继续」—— 卡上累积的要求随 `content` 并入本次运行，
 *   后端注入 worker 系统提示，不重规划。
 * - `supplement`「按这些要求重新规划」—— 同一段 `content` 送回 planner，与原计划混合成
 *   新计划（planner 侧的 `base_plan_text` 由后端从 state 重算，不读这一帧）。
 * - `edit`「改写计划全文」—— 开 `ui/Modal`，大 textarea 预填后端下发的 `planText`，提交后
 *   那段文本就是本轮最终计划（planner 侧 `has_authoritative_edit` 会让两条保底规则
 *   让位于用户的权威，所以这个入口比 `supplement` 重，措辞也要重）。
 *   **它是一枚弹层而不是卡内的编辑态**：卡内切态会把「改写计划全文」那枚触发器连同
 *   整行按钮一起卸载，而 `hooks/useOverlayDismiss` 归还焦点的对象正是「打开它之前焦点
 *   在哪」—— 触发器一卸载，那个元素就从文档里消失，Esc 之后焦点静默落到 `body`。
 *   `ui/Modal` 自己已经调了那个 hook（Esc / 焦点归还 / 遮罩 / `role="dialog"` 全在它身上），
 *   所以这个文件一行 Esc 都不写。
 * - `cancel`「取消规划」—— 两步 `ui/ConfirmAction`，走 `RunCancelled` 收束链。
 *
 * `revision` / `revisionLimit` / `revisionLimitReached` **不**决定按钮集，只负责把按钮集的
 * 理由说出来（`planGateBudgetCopy`）。按钮集只能由后端的 `decisionOptions` 决定：界面里
 * 发不出 `edit` 时，额度若当第二个 owner 就会永远用不掉、那条分支不可达，而
 * `decisionOptions` 会广告一个客户端发不出的 `edit`。
 */
export const TripBriefPlanGate: React.FC = () => {
  const { state, dispatch } = useApp();
  const { sendMessage } = useSendMessage();
  const gate = state.planApprovalGate;

  const [openSupplement, setOpenSupplement] = React.useState(false);
  const [rows, setRows] = React.useState<SupplementRow[]>([]);
  const [guide, setGuide] = React.useState('');
  const [editing, setEditing] = React.useState(false);
  const [planDraft, setPlanDraft] = React.useState('');
  const [submitting, setSubmitting] = React.useState<PlanGateDecisionAction | null>(null);
  const guideRef = React.useRef<HTMLTextAreaElement>(null);

  // 引导输入随内容自动加高：与主 composer 同一套 scrollHeight 策略，封顶 160px。
  React.useEffect(() => {
    const el = guideRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [guide]);

  // 新一帧的门到达（`revision` 变了）＝ 上一轮的本地草稿已经被后端吸收进新计划：
  // 留着它会在下一次提交时把同一批要求再送一遍。编辑态也一并退出——它预填的
  // `planText` 已经是上一版计划了。
  const revisionKey = gate ? `${gate.runId}#${gate.revision}` : null;
  const seenRevisionRef = React.useRef<string | null>(revisionKey);
  React.useEffect(() => {
    if (revisionKey === null || seenRevisionRef.current === revisionKey) return;
    seenRevisionRef.current = revisionKey;
    setRows([]);
    setGuide('');
    setOpenSupplement(false);
    setEditing(false);
    setPlanDraft('');
  }, [revisionKey]);

  if (!gate) return null;
  const cancelled = gate.status === 'cancelled';
  const runId = state.currentTripRunId;
  const entries = planGateEntries(gate);
  const budgetCopy = planGateBudgetCopy(gate);
  // 要求收集区只在这一轮真的能用上它时出现：`approve` 会把它并入 worker 提示，
  // `supplement` 会拿它重规划。两个都没有就没有收集的意义。
  const showRefine = entries.approve || entries.supplement;
  const overLimit = requirementRowsOverLimit(rows);
  const requirementContent = composeRequirementContent(rows, guide);
  const controlsBlocked = state.isStreaming || submitting !== null || !runId;

  const addRow = () =>
    setRows((prev) => [...prev, { id: generateId(), category: '', content: '' }]);
  const updateRow = (id: string, patch: Partial<SupplementRow>) =>
    setRows((prev) => prev.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  const removeRow = (id: string) =>
    setRows((prev) => prev.filter((row) => row.id !== id));

  const toggleSupplement = () =>
    setOpenSupplement((open) => {
      const next = !open;
      if (next && rows.length === 0) addRow();
      return next;
    });

  const openEditor = () => {
    setPlanDraft(gate.planText);
    setEditing(true);
  };

  /**
   * 提交一个决定。`content` 由调用点给出，不在这里第二次拼装
   * （唯一的拼装处是 `composeRequirementContent`）。
   *
   * 后端要求 `run_id` 在**顶层**、`gate_decision` 只装 `{action, content}`：把 `run_id`
   * 放进 `gate_decision` 会 400「gate_decision 需要提供 run_id」。`resumeRunId` 走的正是
   * 顶层那条路（`useSendMessage` 里 `request.run_id = initialRunId`）。
   */
  const submit = async (action: PlanGateDecisionAction, content = '') => {
    if (!runId || submitting || state.isStreaming) return;
    // edit / supplement 的非空 content 是后端 `GateDecision` 的校验项，不是建议。
    if ((action === 'edit' || action === 'supplement') && !content.trim()) return;
    if (action === 'approve' && overLimit) return;
    setSubmitting(action);

    // 确认即清场：以 RESEARCH_START_TEXT 作为消息正文，使其成为对话投影边界——之前的 setup
    // 对话（行程摘要 / 计划已生成 / 思维链）在 live 与会话恢复后都一致隐藏。重规划的两个
    // 决定不设这条边界：门会再次抬起，而 `projectVisibleMessages` 在门 pending 期间自己拿
    // 最后一条用户消息当边界，所以这句话同样不会在线程里另占一个气泡。
    const messageText = {
      approve: RESEARCH_START_TEXT,
      supplement: '按这些要求重新规划',
      edit: '按我改写的计划重新规划',
      cancel: '取消本次规划',
    }[action];
    const pendingLabel = {
      approve: '正在开始并行调研',
      supplement: '正在按你的要求重新规划',
      edit: '正在按你改写的计划重新规划',
      cancel: undefined,
    }[action];

    try {
      const sent = await sendMessage(messageText, undefined, {
        resumeRunId: runId,
        route: 'trip_refinement',
        gateDecision: { action, content },
        assistantPendingLabel: pendingLabel,
      });
      if (sent && action === 'approve' && content) dispatch({ type: 'FLASH_BOARDING_PASS' });
      if (sent && action === 'edit') setEditing(false);
    } finally {
      // 网络失败时保留计划门和本地要求，允许用户以同一输入重试。
      setSubmitting(null);
    }
  };

  return (
    <m.section
      data-testid="trip-brief-plan-gate"
      aria-labelledby="trip-brief-plan-gate-title"
      className="rounded-card border border-stroke bg-panel p-4 shadow-sm sm:p-5"
      /* 表现轴的第二个瞬间：**审批门出现**，与「行程第一次显形」共用同一个 `emphasisEnter`。
         **不要**把 spring 的数（stiffness / damping）手抄在行内：那既绕过 `--dur-emphasis`，
         也让 token 表在这一处失去权威 —— 改一次 token 改不到这里。 */
      variants={emphasisEnter}
      initial="hidden"
      animate="visible"
    >
      <h2 id="trip-brief-plan-gate-title" className="text-xl font-semibold text-ink">{cancelled ? '已取消时的调研计划' : '调研计划已准备好'}</h2>

      {/*
        任务列表 — 一层容器 + 发丝分隔，去掉逐条边框，保持清爽。

        **一个域在一步里只写一次名字。** `AGENT_LABELS[agent]` 只印在**任务行行首那枚芯片**上，
        它回答「这段话是谁的任务」，在多域步骤里是必需的。**表头不许再排一排同样的芯片**：
        两处都印，一个域的步骤就把同一个词并排印两遍（「目的地与体验　目的地与体验 调研
        深圳…」），两个域的步骤印四遍。
        没有任务正文的步骤仍然要那枚芯片 —— 那是它唯一能说出域的地方。
      */}
      <div className="mt-4 divide-y divide-stroke/60 overflow-hidden rounded-card border border-stroke bg-surface/30">
        {gate.steps.map((step) => {
          const tasked = step.agents.filter((agent) => step.tasks?.[agent]?.trim());
          const named = tasked.length > 0 ? tasked : step.agents;
          return (
            <div key={step.step} data-plan-step={step.step} className="px-3 py-2.5">
              <span className="block font-mono text-[11px] uppercase tracking-wide text-ink-muted">任务 {step.step}</span>
              <ul className="mt-1.5 space-y-2">
                {named.map((agent) => (
                  <li key={agent} className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-[13px] leading-relaxed text-ink-secondary">
                    <span data-plan-agent={agent} className="shrink-0 rounded-label bg-accent-soft px-2 py-0.5 text-xs text-accent">
                      {AGENT_LABELS[agent] || '旅行调研'}
                    </span>
                    {step.tasks?.[agent]?.trim() && (
                      <span className="min-w-0 flex-1 basis-full break-words sm:basis-auto">{step.tasks[agent].trim()}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </div>

      {/*
        本轮必须遵守。计划门算好了这几条硬约束、`sse_projection` 也逐字段下发，用户在这一屏
        按下「确认」时能看到系统声称本轮必须遵守什么。

        只印后端已经写成 人话的 `public_summary`：后端这一帧也不再下发 `category`
        （`budget_cap`）与 `enforcement_scope`（`composition`）—— 系统词不上屏。
        位置在任务列表**之后、确认键之前**：它是批准这份计划的前提，不是计划的一部分。
      */}
      {gate.mustObey.length > 0 && (
        <section className="mt-4 rounded-card border border-stroke bg-surface/30 px-3 py-2.5">
          <h3 className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">本轮必须遵守</h3>
          <ul className="mt-1.5 space-y-1.5">
            {gate.mustObey.map((item) => (
              <li
                key={item.constraintId}
                data-must-obey={item.constraintId}
                className="flex items-baseline gap-2 text-[13px] leading-relaxed text-ink-secondary"
              >
                <span aria-hidden className="mt-[0.35em] h-1 w-1 shrink-0 rounded-full bg-accent" />
                <span className="min-w-0 flex-1 break-words">{item.summary}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {showRefine && (
        <div className="mt-4 space-y-3">
          {/* 补充想法（引导）— 常驻、自动加高、无发送键。 */}
          <div className="rounded-card border border-stroke bg-panel px-3 py-2 focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/15">
            <textarea
              ref={guideRef}
              value={guide}
              onChange={(event) => setGuide(event.target.value)}
              disabled={controlsBlocked}
              rows={1}
              placeholder="补充你的想法：侧重点、临时约束或任何希望调研时考虑的事…"
              aria-label="补充想法"
              className="block max-h-40 min-h-[24px] w-full resize-none bg-transparent text-sm leading-relaxed text-ink placeholder:text-ink-secondary disabled:opacity-50"
            />
          </div>

          {/* 追加信息 — 折叠展开一组本地要求行，每行可分类、可单独删除。 */}
          <div>
            <button
              type="button"
              aria-expanded={openSupplement}
              disabled={controlsBlocked}
              onClick={toggleSupplement}
              className={cn(
                'inline-flex h-9 items-center gap-1.5 rounded-card border px-3 text-sm font-medium transition-colors disabled:opacity-50',
                openSupplement ? 'border-accent/40 bg-accent-soft text-accent' : 'border-stroke text-ink-secondary hover:border-accent/40 hover:text-ink'
              )}
            >
              <ListPlus size={14} />
              追加信息
              {rows.some((row) => row.content.trim()) && (
                <span className="rounded-label bg-accent/15 px-1.5 text-[11px] tabular-nums text-accent">
                  {rows.filter((row) => row.content.trim()).length}
                </span>
              )}
              <ChevronDown size={13} className={cn('transition-transform', openSupplement && 'rotate-180')} />
            </button>

            <div className={cn('grid transition-[grid-template-rows,opacity] duration-slow ease-standard', openSupplement ? 'mt-2 grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0')}>
              <div className="overflow-hidden">
                <div className="space-y-2">
                  {rows.map((row) => {
                    const over = row.content.trim().length > SUPPLEMENT_MAX;
                    return (
                      <div key={row.id}>
                        <div className="flex items-center gap-2">
                          <select
                            aria-label="追加信息分类"
                            value={row.category}
                            onChange={(event) => updateRow(row.id, { category: event.target.value as TripSupplementCategory | '' })}
                            disabled={controlsBlocked}
                            className="h-9 shrink-0 rounded-card border border-stroke bg-panel px-2 text-sm text-ink disabled:opacity-50"
                          >
                            <option value="">分类</option>
                            {SUPPLEMENT_CATEGORIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                          </select>
                          <input
                            aria-label="追加信息内容"
                            value={row.content}
                            onChange={(event) => updateRow(row.id, { content: event.target.value })}
                            disabled={controlsBlocked}
                            placeholder="写下本次规划需要考虑的一条要求"
                            className={cn(
                              'h-9 min-w-0 flex-1 rounded-card border bg-panel px-3 text-sm text-ink disabled:opacity-50',
                              over ? 'border-warning/60 focus:border-warning' : 'border-stroke'
                            )}
                          />
                          <button
                            type="button"
                            aria-label="删除这条"
                            disabled={controlsBlocked}
                            onClick={() => removeRow(row.id)}
                            className="grid h-9 w-9 shrink-0 place-items-center rounded-card text-ink-muted transition-colors hover:bg-surface hover:text-danger disabled:opacity-40"
                          >
                            <X size={15} />
                          </button>
                        </div>
                        {/*
                          这条提示是**拦截理由**，不是装饰：它出现时确认按钮已经 disabled，
                          所以走仓内既有的拦截样式 —— error 色 + role="alert"（与
                          ItineraryEntityEditor 的冲突卡同一套），并报出当前字数，让用户知道
                          还要删几个字。压成 11px 的 text-warning 等于没有：用户只会看到按钮
                          不响应，读不到为什么。
                        */}
                        {over && (
                          <p role="alert" data-testid="supplement-over-limit" className="mt-1.5 flex items-center gap-1 rounded-card border border-error/25 bg-panel px-2 py-1 text-xs font-medium leading-5 text-error">
                            <span className="tabular-nums">{row.content.trim().length}/{SUPPLEMENT_MAX}</span>
                            字 · 超出后无法提交，请删减这一条
                          </p>
                        )}
                      </div>
                    );
                  })}
                  <button
                    type="button"
                    disabled={controlsBlocked}
                    onClick={addRow}
                    className="inline-flex h-9 items-center gap-1.5 rounded-card px-2 text-xs font-semibold text-ink-muted transition-colors hover:bg-surface hover:text-ink disabled:opacity-40"
                  >
                    <Plus size={14} /> 再加一条
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/*
        修改额度那一行。它**不**决定下面有哪几个键（那是 `decisionOptions` 的活），它只
        说出理由：第二轮的卡片如果只是默默少了两个键，用户读不出「修改额度用完了」。
        没话说时整行不印（`planGateBudgetCopy` 返回 null）。
      */}
      {budgetCopy && (
        <p data-testid="plan-gate-revision-budget" className="mt-4 text-[13px] leading-relaxed text-ink-muted">
          {budgetCopy}
        </p>
      )}

      {/*
        决定行 —— 每一枚键的在场与否**只由 `decisionOptions` 决定**（`planGateEntries`）。
        后端广告了什么，这里就有什么；这一行不许再自己判一次修改额度。
        确认键更大、主导；两个重规划入口是次级；取消走两步 ConfirmAction。
      */}
      {(entries.approve || entries.edit || entries.supplement || entries.cancel) && (
        <div className="mt-5 flex flex-wrap items-center gap-3">
          {entries.approve && (
            <Button
              type="button"
              size="lg"
              className="flex-1 basis-full sm:basis-auto"
              disabled={Boolean(submitting) || state.isStreaming || overLimit}
              onClick={() => void submit('approve', requirementContent)}
            >
              {submitting === 'approve' ? <Loader2 size={16} className="animate-spin" /> : <Check size={16} />}
              确认调研计划并继续
            </Button>
          )}
          {entries.supplement && (
            <Button
              type="button"
              variant="secondary"
              data-testid="plan-gate-supplement"
              disabled={Boolean(submitting) || state.isStreaming || overLimit || !requirementContent.trim()}
              onClick={() => void submit('supplement', requirementContent)}
            >
              {submitting === 'supplement' ? <Loader2 size={15} className="animate-spin" /> : <RefreshCw size={15} />}
              按这些要求重新规划
            </Button>
          )}
          {entries.edit && (
            <Button
              type="button"
              variant="secondary"
              data-testid="plan-gate-edit"
              disabled={Boolean(submitting) || state.isStreaming}
              onClick={openEditor}
            >
              <PencilLine size={15} />
              改写计划全文
            </Button>
          )}
          {entries.cancel && (
            <ConfirmAction
              tone="error"
              confirmLabel="取消运行"
              confirmPending={submitting === 'cancel'}
              disabled={Boolean(submitting) || state.isStreaming}
              onConfirm={() => void submit('cancel')}
            >
              <Square size={13} />取消规划
            </ConfirmAction>
          )}
        </div>
      )}

      {/*
        「按这些要求重新规划」在没有要求时是 disabled 的 —— 拦截理由必须说出来，否则用户
        只看到一枚按不动的键。这条与超字数那条同一套语法（`role="alert"` + error 色）。
      */}
      {entries.supplement && !requirementContent.trim() && !overLimit && (
        <p role="alert" data-testid="plan-gate-supplement-empty" className="mt-2 text-xs leading-5 text-ink-muted">
          想让我重新规划，先在上面写下至少一条要求或一句补充想法。
        </p>
      )}

      {/*
        改写计划全文（`decisionOptions` 含 `edit` 才有入口开它）。

        `ui/Modal` 承担 Esc / 遮罩 / 焦点归还 / `role="dialog"`，这里一行都不重复写。
        `planText` 是后端 `_serialize_plan_text` 的产物 —— **这一屏是它唯一的读者**，
        也是它留在 payload 里的唯一理由。

        那句警告不是客套：提交后 planner 侧 `has_authoritative_edit` 会让两条保底规则
        （多日行程补 itinerary_planner、有过夜补 accommodation_researcher）让位于用户
        写下的文本。一个静默生效的权威等于一个陷阱，所以它必须写在提交键上面。
      */}
      <Modal open={editing} onClose={() => setEditing(false)} title="改写这份调研计划" maxWidth="max-w-2xl">
        <p className="text-[13px] leading-relaxed text-ink-secondary">
          你写下的这段文本会成为本轮的最终计划 —— 系统不再按原计划替你补任务。改完提交，我按它重新分配调研。
        </p>
        {/*
          **这里不许写 `autoFocus`。** `useOverlayDismiss` 记的「打开它之前焦点在哪」是在
          `useEffect` 里读 `document.activeElement`，而 React 处理子元素的 `autoFocus` 在
          commit 阶段、**早于**这个 effect —— 于是它记下的 opener 是这枚 textarea 自己。
          弹层一关，那个元素随之从文档里消失，`focus()` 无声失效，焦点落回 `body`。
          焦点由 `ui/Modal` 的 `panelRef` 放进面板，Tab 第一站就是这枚 textarea。
        */}
        <textarea
          aria-label="调研计划全文"
          data-testid="plan-gate-plan-text"
          value={planDraft}
          onChange={(event) => setPlanDraft(event.target.value)}
          disabled={controlsBlocked}
          className="mt-3 block h-72 w-full resize-none rounded-card border border-stroke bg-surface/30 px-3 py-2 font-mono text-[13px] leading-relaxed text-ink focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/15 disabled:opacity-50"
        />
        {!planDraft.trim() && (
          <p role="alert" data-testid="plan-gate-editor-empty" className="mt-2 rounded-card border border-error/25 bg-panel px-2 py-1 text-xs font-medium leading-5 text-error">
            计划全文不能为空 —— 空白的计划提交不了，请写下你要的调研安排。
          </p>
        )}
        <div className="mt-4 flex items-center gap-2">
          <Button
            type="button"
            data-testid="plan-gate-edit-submit"
            disabled={controlsBlocked || !planDraft.trim()}
            onClick={() => void submit('edit', planDraft)}
          >
            {submitting === 'edit' ? <Loader2 size={16} className="animate-spin" /> : <RefreshCw size={15} />}
            按此计划重新规划
          </Button>
          <Button type="button" variant="ghost" disabled={Boolean(submitting)} onClick={() => setEditing(false)}>
            返回
          </Button>
        </div>
      </Modal>
    </m.section>
  );
};
