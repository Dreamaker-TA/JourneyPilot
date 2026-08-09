import React, { useEffect, useRef, useState } from 'react';
import { m } from 'motion/react';
import {
  Calculator,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Database,
  Info,
  Loader2,
  MapPin,
  RotateCcw,
  Search,
  Wrench,
  XCircle,
} from 'lucide-react';
import { cn } from '../../lib/utils';
import { formatDuration } from '../../lib/format';
import { duration as motionDuration, easing } from '../../lib/motion';
import { formatAgentLabel } from '../../lib/travelProgress';
import {
  countToolSources,
  describeToolGroup,
  describeToolStep,
  toolSourceLabel,
  toolStatusLabel,
} from '../../lib/toolDisplay';

import { RollingNumber } from '../ui/RollingNumber';
import { InspectHint, InspectRow, InspectSectionTitle } from '../ui/InspectHint';
import type { ThinkingStep, ToolCategory } from '../../types/chat';
import { shouldShowLiveDuration } from '../../lib/thinkingChain';

/**
 * 这一步有没有值得给读者看的东西。
 *
 * 判据必须与 :func:`ToolStepInspectPanel` **实际会渲染的行**一致。`auditId` / `ttftMs` /
 * `latencyMs` / `model` / `tier` / `toolArgs` 都不在那个面上，所以也不能算进这里：算进来会让
 * 一个只有开发者标识的步骤长出一个 ⓘ，点开是空的。
 */
function stepHasInspectDetail(step: ThinkingStep): boolean {
  return Boolean(
    step.toolDegraded
    || step.fallbackFrom
    || step.fallbackTo
    || (step.toolStatus === 'degraded')
    || (step.toolStatus === 'capability_declared')
  );
}

function groupHasInspectDetail(steps: ThinkingStep[]): boolean {
  return steps.some(stepHasInspectDetail);
}

/**
 * 检查面入口的名字，逐条说出它开的是哪一行、里面有什么。
 *
 * 每个入口**各有各的名字**（既是 tooltip 也是读屏名）。共用一句「工具检查」的话，它们在右侧
 * 排成一列一模一样的圆圈 i，看不出哪一枚对应哪一行，也看不出点开会得到什么。
 */
function inspectLabel(step: ThinkingStep): string {
  const { subject } = describeToolStep(step);
  const what = step.toolStatus === 'capability_declared' ? '这个数据源覆盖到哪里' : '这一步走了哪条通道';
  return subject ? `${what}（${subject}）` : what;
}

/**
 * 一次查询的详情（点 ⓘ 才见；默认时间线不铺满）。
 *
 * **这里只放读者能读懂、并且据此能做点什么的东西。** 移走的是纯开发者标识：原始工具名、
 * `audit_id`、`TTFT`、毫秒延迟、原始模型 id（`deepseek/deepseek-v4-flash-0731`）、
 * 档位（primary/fast）、归因粒度（步级/agent 级）、以及等宽印出来的原始参数串。它们对旅行者
 * 既不可读也不可操作，而 normal mode 口径本来就写着隐藏 ids ——
 * 这个面是它们最后的出口，`developerMode` 那道门从来没有被建起来过。
 *
 * 留下的都是人话：这一步成不成、数据源覆盖不覆盖你选的日期、有没有走备用通道、查的是什么。
 * 耗时不在这里重复——每一行右侧本来就有它。
 *
 * `showResult` 决定要不要再印一遍「这一步拿到了」。从某一行的 ⓘ 点开时不印：那一行的正文
 * 就是这个结果，两处一字不差，用户点开只会看到刚读过的那句话。从组的 ⓘ 点开时印：
 * 组收起着，兄弟步骤的结果并不在屏幕上，这时它是新信息。
 */
function ToolStepInspectPanel({ step, showResult = false }: { step: ThinkingStep; showResult?: boolean }) {
  const display = describeToolStep(step);
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <InspectSectionTitle>{display.categoryLabel}</InspectSectionTitle>
      {display.subject && <InspectRow label="查询" value={display.subject} />}
      {step.toolStatus && <InspectRow label="状态" value={toolStatusLabel(step.toolStatus)} />}
      {(step.toolDegraded || step.toolStatus === 'degraded') && (
        <InspectRow
          label="备用通道"
          value={
            step.fallbackFrom && step.fallbackTo
              ? `${toolSourceLabel(step.fallbackFrom)} 未返回结果，改用${toolSourceLabel(step.fallbackTo)}`
              : '主数据源未返回结果'
          }
        />
      )}
      {showResult && step.toolResult && (
        <div className="mt-1 border-t border-stroke/60 pt-2">
          <p className="text-[11px] font-semibold text-ink-muted">这一步拿到了</p>
          <p className="mt-1 max-h-28 overflow-y-auto break-words text-[11px] leading-relaxed text-ink-secondary">
            {step.toolResult}
          </p>
        </div>
      )}
    </div>
  );
}

function ToolGroupInspectPanel({ steps }: { steps: ThinkingStep[] }) {
  const degraded = steps.filter((s) => s.toolStatus === 'degraded' || s.toolDegraded);
  const capability = steps.filter((s) => s.toolStatus === 'capability_declared');
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <InspectSectionTitle>本组工具检查 · {steps.length} 次</InspectSectionTitle>
      {degraded.length > 0 && (
        <p className="text-[11px] text-warning">
          {degraded.length} 次经备用通道完成
        </p>
      )}
      {capability.length > 0 && (
        // 中性语域：这不是失败统计，而是「数据源的日期覆盖范围」这一事实。
        <p className="text-[11px] text-ink-secondary">
          {capability.length} 次因数据源未覆盖该日期，只取到参考资料
        </p>
      )}
      <ul className="max-h-56 space-y-2 overflow-y-auto">
        {steps.map((step) => (
          <li key={step.id} className="rounded-card border border-stroke bg-surface/80 px-2 py-1.5">
            <ToolStepInspectPanel step={step} showResult />
          </li>
        ))}
      </ul>
    </div>
  );
}

function ToolStatusIcon({
  isRunning,
  isFailed,
  isCapability = false,
  size = 11,
  className,
}: {
  isRunning: boolean;
  isFailed: boolean;
  /** 日期能力判定：中性图标，绝不用错误图标或错误色。 */
  isCapability?: boolean;
  size?: number;
  className?: string;
}) {
  if (isRunning) {
    return <Loader2 size={size} className={cn('thinking-chain-inline-status animate-[spin_1.5s_linear_infinite] flex-shrink-0', className)} />;
  }
  if (isFailed) {
    return <XCircle size={size} className={cn('thinking-chain-inline-status flex-shrink-0', className)} />;
  }
  if (isCapability) {
    return <Info size={size} className={cn('thinking-chain-inline-status flex-shrink-0', className)} />;
  }
  return <CheckCircle2 size={size} className={cn('thinking-chain-inline-status flex-shrink-0', className)} />;
}

/** 思维链外壳：中性细轨 + 轻 surface（时间轴感，非 accent 侧签）。
 *  w-full：始终撑满气泡宽度（与「行程登机牌」同宽），不随展开/收起在 flex items-start
 *  下按内容宽度伸缩——否则收起时缩到标题宽、展开时又变宽，视觉抖动。 */
const SHELL_THINKING_RAIL = cn(
  'thinking-chain-shell relative group w-full max-w-full break-words text-ink'
);

const TIMELINE_DOT_SHADOW = '0 0 0 3px var(--thinking-surface, var(--color-panel)), 0 5px 13px color-mix(in srgb, var(--color-accent) 12%, transparent)';

/** 内部 agent 名 → 用户语言标签（不暴露 destination_researcher 之类）。 */
function agentDisplay(agentName: string): string {
  return formatAgentLabel(agentName || '').label;
}

/**
 * 定格瞬间的 120ms 微脉冲：数值 scale 1→1.06→1，--dur-fast。
 * 当 `freezeKey` 从 undefined 变为具体服务端值那一刻触发一次——「已定格」的物理反馈。
 */
const FreezePulse: React.FC<{ freezeKey?: number; className?: string; children: React.ReactNode }> = ({
  freezeKey,
  className,
  children,
}) => {
  if (freezeKey == null) {
    return <span className={className}>{children}</span>;
  }
  return (
    <m.span
      key={freezeKey}
      className={className}
      initial={{ scale: 1 }}
      animate={{ scale: [1, 1.06, 1] }}
      transition={{ duration: motionDuration.fast, ease: easing.standard, times: [0, 0.5, 1] }}
    >
      {children}
    </m.span>
  );
};

function dateMs(value: Date | string | undefined): number | null {
  if (!value) return null;
  const ms = value instanceof Date ? value.getTime() : new Date(value).getTime();
  return Number.isFinite(ms) ? ms : null;
}

function fallbackClientDurationMs(step: ThinkingStep): number | null {
  const start = dateMs(step.timestamp);
  const end = dateMs(step.endTime);
  if (start == null || end == null || end < start) return null;
  return end - start;
}

function frozenStepDurationMs(step: ThinkingStep): number | null {
  return step.serverDurationMs ?? fallbackClientDurationMs(step);
}

function stepBoundaryMs(step: ThinkingStep): number | null {
  if (step.tsMs != null) {
    return step.tsMs + (step.serverDurationMs ?? 0);
  }
  const end = dateMs(step.endTime);
  if (end != null) return end;
  return dateMs(step.timestamp);
}

function clientTotalSpanMs(steps: ThinkingStep[]): number | null {
  if (steps.length === 0) return null;
  const start = dateMs(steps[0].timestamp);
  if (start == null) return null;
  let end = start;
  for (const step of steps) {
    const boundary = dateMs(step.endTime) ?? dateMs(step.timestamp);
    if (boundary != null) end = Math.max(end, boundary);
  }
  return end >= start ? end - start : null;
}

function settledTotalSpanMs(steps: ThinkingStep[]): number | null {
  if (steps.length === 0) return null;
  const firstTs = steps[0].tsMs;
  if (firstTs != null) {
    let lastBoundary: number | null = null;
    for (const step of steps) {
      const boundary = stepBoundaryMs(step);
      if (boundary != null) lastBoundary = lastBoundary == null ? boundary : Math.max(lastBoundary, boundary);
    }
    if (lastBoundary != null && lastBoundary > firstTs) return lastBoundary - firstTs;
  }
  return clientTotalSpanMs(steps);
}

/**
 * 单步耗时。
 * - 运行中：保留 100ms 本地 tick，只作「正在发生」的活感估读；
 * - 定格：优先用服务端 `serverDurationMs`；缺少服务端边界时用 endTime 兜底，避免已完成步骤继续跳秒。
 */
const StepDuration: React.FC<{ step: ThinkingStep; isGenerating?: boolean }> = ({ step, isGenerating }) => {
  const [nowMs, setNowMs] = useState(() => Date.now());
  const frozenMs = frozenStepDurationMs(step);
  const frozen = frozenMs != null;
  const running = !!isGenerating && !frozen;

  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(() => setNowMs(Date.now()), 100);
    return () => clearInterval(timer);
  }, [running]);

  if (frozen) {
    // 服务端值优先；缺少服务端边界时，用本地 endTime 兜底，避免已完成步骤继续跳秒。
    return (
      <FreezePulse
        freezeKey={Math.round(frozenMs as number)}
        className="thinking-chain-duration ml-auto font-mono text-[11px] text-accent"
      >
        {formatDuration(frozenMs as number)}
      </FreezePulse>
    );
  }
  if (!running) return null;
  // 运行中本地估读（服务端定格前的活感）。
  const start = dateMs(step.timestamp) ?? nowMs;
  const elapsed = Math.max(0, nowMs - start);
  return <span className="thinking-chain-duration ml-auto font-mono text-[11px] text-accent">{formatDuration(elapsed)}</span>;
};

/**
 * 总耗时。
 * - 运行中：本地 tick，从首步开始估读；
 * - 定格：优先服务端收口值 `serverTotalMs`；无则退化到步边界，最后用本地 endTime 兜底。
 */
const TotalDuration: React.FC<{
  steps: ThinkingStep[];
  isGenerating?: boolean;
  serverTotalMs?: number | null;
  startedAt?: Date | string;
}> = ({ steps, isGenerating, serverTotalMs, startedAt }) => {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!isGenerating) return undefined;
    const timer = setInterval(() => setNowMs(Date.now()), 100);
    return () => clearInterval(timer);
  }, [isGenerating]);

  if (isGenerating) {
    const startMs = dateMs(startedAt) ?? (steps.length > 0 ? dateMs(steps[0].timestamp) : null);
    if (startMs == null) return null;
    return <span>{formatDuration(Math.max(0, nowMs - startMs))}</span>;
  }

  if (steps.length === 0) return null;

  // 定格：服务端收口值优先。
  if (serverTotalMs != null) {
    return (
      <FreezePulse freezeKey={Math.round(serverTotalMs)}>{formatDuration(serverTotalMs)}</FreezePulse>
    );
  }
  const fallbackMs = settledTotalSpanMs(steps);
  if (fallbackMs != null) {
    return <FreezePulse freezeKey={Math.round(fallbackMs)}>{formatDuration(fallbackMs)}</FreezePulse>;
  }
  return null;
};

/**
 * 折叠头括号读数是否有内容可显：镜像 TotalDuration 的渲染条件 + 是否问过数据源。
 * 恢复的历史会话步没有服务端时钟数据（tsMs/serverDurationMs/serverTotalMs 全缺）且一处
 * 数据源都没问过时，整个括号不渲染——不出现「思考过程 · N 步（）」的空括号。
 */
function hasHeaderReadout(
  steps: ThinkingStep[],
  isGenerating: boolean,
  serverTotalMs: number | null | undefined,
  startedAt?: Date | string,
): boolean {
  if (countToolSources(steps) > 0) return true;
  if (isGenerating) return steps.length > 0 || dateMs(startedAt) != null;
  if (steps.length === 0) return false;
  if (serverTotalMs != null) return true;
  return settledTotalSpanMs(steps) != null;
}

/**
 * 折叠头读数：**问过几处数据源**，RollingNumber 逐位滚动。
 *
 * **不印 token 总量与美元成本**：旅行者不关心 token，那两个数是这条链上最纯粹的开发者读数。
 * 「你去查了几处资料」回答同一个「它到底干了多少活」的问题，而读者能读懂。
 *
 * 数的口径与逐行显示同源（``countToolSources`` 按 DisplayKind 去重），所以头里的数和展开后
 * 能数出来的类别数永远一致 —— 一个读者会去数的数字必须数得对。
 */
const SourceReadout: React.FC<{ count: number }> = ({ count }) => (
  <>
    {' · '}
    <RollingNumber
      value={count}
      format={(value) => String(value)}
      className="thinking-chain-metric font-mono text-[11px]"
      testId="source-readout"
    />
    {' 处资料'}
  </>
);

function getCategoryIcon(category: ToolCategory | undefined, size = 11) {
  switch (category) {
    case 'internal':
      return <MapPin size={size} className="thinking-chain-category-icon text-ink-muted flex-shrink-0" />;
    case 'search':
      return <Search size={size} className="thinking-chain-category-icon text-accent flex-shrink-0" />;
    case 'data':
      return <Database size={size} className="thinking-chain-category-icon text-success flex-shrink-0" />;
    case 'calculation':
      return <Calculator size={size} className="thinking-chain-category-icon text-warning flex-shrink-0" />;
    default:
      return <Wrench size={size} className="thinking-chain-category-icon text-accent flex-shrink-0" />;
  }
}

interface StepGroup {
  key: string;
  steps: ThinkingStep[];
  isToolCall: boolean;
}

function groupThinkingSteps(steps: ThinkingStep[]): StepGroup[] {
  const groups: StepGroup[] = [];
  for (const step of steps) {
    const last = groups[groups.length - 1];
    if (
      last &&
      last.isToolCall &&
      step.isToolCall &&
      last.steps[0].agentName === step.agentName &&
      last.steps[0].toolName &&
      last.steps[0].toolName === step.toolName
    ) {
      last.steps.push(step);
    } else {
      groups.push({ key: step.id, steps: [step], isToolCall: !!step.isToolCall });
    }
  }
  return groups;
}

const MergedToolGroupItem: React.FC<{
  group: StepGroup;
  isLastGroup: boolean;
  isGenerating?: boolean;
}> = ({ group, isLastGroup, isGenerating }) => {
  const [expanded, setExpanded] = useState(false);

  if (group.steps.length === 1) {
    return <ThinkingStepItem step={group.steps[0]} isLast={isLastGroup} isGenerating={isGenerating} />;
  }

  const firstStep = group.steps[0];
  const lastStep = group.steps[group.steps.length - 1];
  const isRunning = lastStep.toolStatus === 'running';
  // Earlier provider failures and successful fallback attempts are execution
  // facts, not traveller failures. The terminal step alone determines the
  // default consumer state; InspectHint retains every underlying detail.
  const isFailed = lastStep.toolStatus === 'failed' && !isRunning;
  // 第四态：终态是服务端的日期能力判定 —— 中性，不是失败，也不折叠进「已完成」。
  const isCapability = lastStep.toolStatus === 'capability_declared' && !isRunning;
  const isConsumerCompleted = !isRunning && !isFailed && !isCapability;
  const category = firstStep.toolCategory || 'other';
  const groupDisplay = describeToolGroup(group.steps);
  const latestDisplay = describeToolStep(lastStep);
  const isInternal = category === 'internal';
  const isActiveGroup = isLastGroup && isGenerating;

  if (isInternal) {
    return (
      <div
        className={cn(
          'thinking-chain-action thinking-chain-action--internal relative animate-fade-in',
          isRunning && 'is-active',
          isFailed && 'is-failed',
          isCapability && 'is-capability',
          isConsumerCompleted && 'is-complete'
        )}
      >
        <div
          className="thinking-chain-marker absolute"
          style={{ boxShadow: TIMELINE_DOT_SHADOW }}
        />
        <div className="flex items-center gap-1.5 text-[11px] text-ink-muted">
          {isRunning ? (
            <Loader2 size={9} className="thinking-chain-inline-status animate-[spin_1.5s_linear_infinite] flex-shrink-0" />
          ) : isFailed ? (
            <XCircle size={9} className="thinking-chain-inline-status text-error flex-shrink-0" />
          ) : isCapability ? (
            <Info size={9} className="thinking-chain-inline-status text-ink-muted flex-shrink-0" />
          ) : (
            <CheckCircle2 size={9} className="thinking-chain-inline-status text-ink-muted flex-shrink-0" />
          )}
          {getCategoryIcon(category, 9)}
          <span key={lastStep.id} className="min-w-0 flex-1 truncate tool-content-animate">
            {latestDisplay.actionText}
          </span>
          <StepDuration step={lastStep} isGenerating={isActiveGroup && isRunning} />
        </div>
      </div>
    );
  }

  const statusColor = isRunning
    ? 'text-warning'
    : isFailed
      ? 'text-error'
      // 能力判定是中性事实：用次级墨色，绝不用 error/warning 色。
      : isCapability
        ? 'text-ink-secondary'
        : 'text-success';
  // 产品面：列出每次检索词/摘要；参数/归因细节走检查面 i。
  const queryItems = group.steps
    .map((step) => ({ step, display: describeToolStep(step) }))
    .filter((item) => Boolean(item.step.toolResult || item.display.subject));
  const canExpand = queryItems.length > 0;
  const collapsedContent = isRunning
    ? (latestDisplay.subject || '')
    : (lastStep.toolResult || '');

  return (
    <div
      className={cn(
        'thinking-chain-action thinking-chain-action--tool relative animate-fade-in',
        isRunning && 'is-active',
        isFailed && 'is-failed',
        isCapability && 'is-capability',
        isConsumerCompleted && 'is-complete'
      )}
    >
      <div
        className="thinking-chain-marker absolute"
        style={{ boxShadow: TIMELINE_DOT_SHADOW }}
      />
      <div className="flex items-center gap-1.5 text-[13px] mb-1 flex-wrap">
        <ToolStatusIcon
          isRunning={isRunning}
          isFailed={isFailed}
          isCapability={isCapability}
          className={statusColor}
        />
        {getCategoryIcon(category)}
        <span className="thinking-chain-action-title font-semibold text-accent">{groupDisplay.categoryLabel}</span>
        <span className="min-w-0 flex-1 truncate text-ink-secondary">· {groupDisplay.actionText}</span>
        <StepDuration step={lastStep} isGenerating={isActiveGroup && isRunning} />
        {groupHasInspectDetail(group.steps) && (
          <InspectHint
            label={`这一组里有几步走了别的通道（${groupDisplay.categoryLabel} ${group.steps.length} 次）`}
            size="sm"
            testId="tool-group-inspect"
            placement="bottom-end"
          >
            <ToolGroupInspectPanel steps={group.steps} />
          </InspectHint>
        )}
        {canExpand && (
          <button
            type="button"
            onClick={() => setExpanded(!expanded)}
            className="ml-auto flex items-center gap-0.5 text-accent hover:text-accent transition-colors cursor-pointer"
          >
            {expanded ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
            <span className="text-[11px]">{expanded ? '收起' : '展开'}</span>
          </button>
        )}
      </div>

      {!expanded && collapsedContent && (
        <div key={lastStep.id} className="text-[13px] text-ink-secondary leading-relaxed break-words pl-1 tool-content-animate">
          {collapsedContent}
        </div>
      )}

      {expanded && (
        <ul className="mt-1 space-y-1 pl-1">
          {queryItems.map(({ step, display }) => {
            const stepRunning = step.toolStatus === 'running';
            const stepFailed = step.toolStatus === 'failed' && !isConsumerCompleted;
            const stepCapability = step.toolStatus === 'capability_declared';
            const content = stepRunning
              ? (display.subject || display.actionText)
              : step.toolStatus === 'failed' && isConsumerCompleted
                ? (display.subject || groupDisplay.actionText)
                : (step.toolResult || display.subject);
            return (
              <li key={step.id} className="flex items-start gap-1.5 text-[13px] leading-relaxed text-ink-secondary">
                <ToolStatusIcon
                  isRunning={stepRunning}
                  isFailed={stepFailed}
                  isCapability={stepCapability}
                  size={10}
                  className={cn(
                    'mt-0.5',
                    stepRunning
                      ? 'text-warning'
                      : stepFailed
                        ? 'text-error'
                        : stepCapability
                          ? 'text-ink-secondary'
                          : 'text-success'
                  )}
                />
                <span className="min-w-0 flex-1 break-words">
                  {content}
                </span>
                {stepHasInspectDetail(step) && (
                  <InspectHint label={inspectLabel(step)} size="sm" testId={`tool-step-inspect-${step.id}`} placement="bottom-end">
                    <ToolStepInspectPanel step={step} />
                  </InspectHint>
                )}
                <StepDuration
                  step={step}
                  isGenerating={shouldShowLiveDuration(step, Boolean(isActiveGroup))}
                />
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
};

/**
 * 一个思考步的**推理正文**。
 *
 * 推理正文在流上（`thinking` / `agent_thinking` / `agent_progress` 三条 kind 的
 * `content`，落到 `ThinkingStep.content`），这一层是它唯一的渲染处。
 *
 * **它只出现在这里，不进主对话。** 那条边界是结构性的，不是靠这里克制：
 * `useSendMessage` 的三个 case 一次都没有碰过 `assistantMessage.content` /
 * `displayContent`，正文的唯一入口是 `chat_chunk`（而深度模式连 `chat_chunk` 都不发）。
 * 「主对话里隐藏思考」这条既有策略照旧成立。
 *
 * 排版照既有声部，不新造：
 * - 与工具步那一行的结果预览**同一档**（13px / `text-ink-secondary` / `leading-relaxed`），
 *   因为它们在信息层级上是同一件事——某一步说了什么。正文不许大过它上面的步骤标题。
 * - 长文本**自己封顶滚动**（`max-h-28`，与 `ToolStepInspectPanel` 里那段结果正文同一个数）。
 *   外层时间线已有 `thinking-scroll-max`，但一段几千字的推理会把其余步骤整个挤出视野；
 *   一个步骤占多高，由它自己那一格负责。
 * - 流式时跟着自己的底走，用的是本文件已有的那一句 `scrollTop = scrollHeight`——
 *   容器高度固定，所以「流式中不跳版」成立。
 * - 入场动画复用 `tool-content-animate`（合同允许 entry 的 opacity/transform）。
 */
const ReasoningBody: React.FC<{ step: ThinkingStep; isActiveStep: boolean }> = ({
  step,
  isActiveStep,
}) => {
  const bodyRef = useRef<HTMLDivElement>(null);
  const text = (step.content || '').trim();

  useEffect(() => {
    if (!isActiveStep) return;
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [text, isActiveStep]);

  if (!text) return null;
  return (
    <div
      ref={bodyRef}
      data-reasoning-body={step.id}
      className="thinking-scroll mt-1 max-h-28 overflow-y-auto whitespace-pre-wrap break-words pl-1 text-[13px] leading-relaxed text-ink-secondary tool-content-animate"
    >
      {text}
    </div>
  );
};

const ThinkingStepItem: React.FC<{
  step: ThinkingStep;
  isLast: boolean;
  isGenerating?: boolean;
}> = ({ step, isLast, isGenerating }) => {
  const isActiveStep = isLast && isGenerating;

  if (step.isToolCall) {
    const isRunning = step.toolStatus === 'running';
    const isFailed = step.toolStatus === 'failed';
    // 第四态：服务端的日期能力判定 —— 中性结果，既不是失败也不是成功。
    const isCapability = step.toolStatus === 'capability_declared';
    const category = step.toolCategory || 'other';
    const display = describeToolStep(step);
    const isInternal = category === 'internal';

    if (isInternal) {
      return (
        <div
          className={cn(
            'thinking-chain-action thinking-chain-action--internal relative animate-fade-in',
            isRunning && 'is-active',
            isFailed && 'is-failed',
            isCapability && 'is-capability',
            !isRunning && !isFailed && !isCapability && 'is-complete'
          )}
        >
          <div
            className="thinking-chain-marker absolute"
            style={{ boxShadow: TIMELINE_DOT_SHADOW }}
          />
          <div className="flex items-center gap-1.5 text-[11px] text-ink-muted">
            {isRunning ? (
              <Loader2 size={9} className="thinking-chain-inline-status animate-[spin_1.5s_linear_infinite] flex-shrink-0" />
            ) : isFailed ? (
              <XCircle size={9} className="thinking-chain-inline-status text-error flex-shrink-0" />
            ) : isCapability ? (
              <Info size={9} className="thinking-chain-inline-status text-ink-muted flex-shrink-0" />
            ) : (
              <CheckCircle2 size={9} className="thinking-chain-inline-status text-ink-muted flex-shrink-0" />
            )}
            {getCategoryIcon(category, 9)}
            <span className="min-w-0 flex-1 truncate">{display.actionText}</span>
            {step.fromCache && <RotateCcw size={9} className="flex-shrink-0 text-ink-muted" />}
            <StepDuration step={step} isGenerating={isActiveStep && isRunning} />
          </div>
        </div>
      );
    }

    const statusColor = isRunning
      ? 'text-warning'
      : isFailed
        ? 'text-error'
        // 能力判定是中性事实：用次级墨色，绝不用 error/warning 色。
        : isCapability
          ? 'text-ink-secondary'
          : 'text-success';
    // 产品面：进行中显示检索目标，完成/失败/能力判定显示结果摘要
    // （能力判定的摘要就是服务端写的能力说明）。参数/归因 → 检查面 i。
    const preview = isRunning
      ? (display.subject || '')
      : (step.toolResult || display.subject || '');

    return (
      <div
        className={cn(
          'thinking-chain-action thinking-chain-action--tool relative animate-fade-in',
          isRunning && 'is-active',
          isFailed && 'is-failed',
          isCapability && 'is-capability',
          !isRunning && !isFailed && !isCapability && 'is-complete'
        )}
      >
        <div
          className="thinking-chain-marker absolute"
          style={{ boxShadow: TIMELINE_DOT_SHADOW }}
        />
        <div className="flex items-center gap-1.5 text-[13px] mb-1 flex-wrap">
          <ToolStatusIcon
            isRunning={isRunning}
            isFailed={isFailed}
            isCapability={isCapability}
            className={statusColor}
          />
          {getCategoryIcon(category)}
          <span className="thinking-chain-action-title font-semibold text-accent">{display.categoryLabel}</span>
          <span className="min-w-0 flex-1 truncate text-ink-secondary">· {display.actionText}</span>
          {step.fromCache && (
            <span className="text-[11px] text-ink-muted bg-ink/5 rounded-label px-1">
              缓存
            </span>
          )}
          <StepDuration step={step} isGenerating={isActiveStep && isRunning} />
          {stepHasInspectDetail(step) && (
            <InspectHint label={inspectLabel(step)} size="sm" testId={`tool-step-inspect-${step.id}`} placement="bottom-end">
              <ToolStepInspectPanel step={step} />
            </InspectHint>
          )}
        </div>
        {preview && (
          <div className="text-[13px] text-ink-secondary leading-relaxed break-words pl-1 tool-content-animate">
            {preview}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className={cn('thinking-chain-action thinking-chain-action--agent relative animate-fade-in', isActiveStep ? 'is-active' : 'is-complete')}>
      <div className="thinking-chain-marker absolute" style={{ boxShadow: TIMELINE_DOT_SHADOW }} />
      {/* 大步骤（一个 agent 的一个阶段）= 面内小节标题这一档，14/600 —— 必须**大于**它下面
          每一次工具调用的正文（13px）。压到 11px 会把层级整个倒过来：最不重要的一行（一次
          查询的结果摘要）成了屏幕上最大的字。 */}
      <div className="flex items-center gap-1.5 text-[14px] leading-[1.5]">
        {isActiveStep ? (
          <Loader2 size={11} className="thinking-chain-inline-status text-accent animate-[spin_2s_linear_infinite] flex-shrink-0" />
        ) : (
          <CheckCircle2 size={11} className="thinking-chain-inline-status text-accent flex-shrink-0" />
        )}
        <span className="thinking-chain-action-title font-semibold text-accent">{agentDisplay(step.agentName)}</span>
        {step.stepName && step.stepName !== '推理中' && <span className="text-ink-secondary">· {step.stepName}</span>}
        <StepDuration step={step} isGenerating={isActiveStep} />
      </div>
      <ReasoningBody step={step} isActiveStep={Boolean(isActiveStep)} />
    </div>
  );
};

const SynthesizingIndicator: React.FC = () => (
  <div className="flex items-center gap-2.5 py-1">
    <div className="flex items-center gap-1">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="block w-1.5 h-1.5 rounded-full bg-accent"
          // 环境循环走 `--dur-loop` 与 `--ease-standard`（循环用 ease-in-out 家族，
          // 而 standard 就在那一族里）。**不要**写字面时长（`1.2s ease-in-out` 那类）：
          // 每处各写一个值，一屏上的环境循环就会散在 900ms–2.6s 之间。
          style={{ animation: `synthesizing-dot var(--dur-loop) var(--ease-standard) ${i * 0.2}s infinite` }}
        />
      ))}
    </div>
    <span className="text-xs text-ink-secondary font-medium">正在生成回答</span>
  </div>
);

interface ThinkingChainProps {
  steps: ThinkingStep[];
  isGenerating: boolean;
  displayContent?: string;
  isSynthesizing: boolean;
  pendingStatusText?: string;
  variant?: 'default' | 'interrupted';
  /** 服务端收口的总耗时（run_cost_summary.wall_ms，ms）；定格后优先展示。 */
  serverTotalMs?: number | null;
  /** 助手消息创建时间：首个 SSE 思考步到达前，也用它启动「正在理解需求」计时。 */
  startedAt?: Date | string;
  regionId: string;
}

/**
 * 可展开的思维链：每步显示 agent(用户语言) + 工具 + 每步耗时，折叠头显示步数、总耗时、
 * 以及运行级 token/成本。运行中默认展开，正文到达 / synthesizing 时自动收起。
 */
export const ThinkingChain: React.FC<ThinkingChainProps> = ({
  steps,
  isGenerating,
  displayContent,
  isSynthesizing,
  pendingStatusText,
  variant = 'default',
  serverTotalMs,
  startedAt,
  regionId,
}) => {
  const [expanded, setExpanded] = useState(false);
  const [hasClipTop, setHasClipTop] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const isInterrupted = variant === 'interrupted';
  const hasDisplay = Boolean(displayContent?.trim());
  const thinkingLive = isGenerating && !hasDisplay && !isSynthesizing;
  const isTransient = steps.length === 0;
  const latestStep = steps[steps.length - 1];
  const sourceCount = React.useMemo(() => countToolSources(steps), [steps]);

  useEffect(() => {
    if (isGenerating && !hasDisplay && !isSynthesizing) setExpanded(true);
  }, [isGenerating, hasDisplay, isSynthesizing]);
  useEffect(() => {
    if (isSynthesizing) setExpanded(false);
  }, [isSynthesizing]);
  useEffect(() => {
    if (hasDisplay) setExpanded(false);
  }, [hasDisplay]);
  // 顶部渐隐状态机：scrollTop > 4 表示上方有被裁剪的历史条目，此时才加 has-clip-top；
  // 条目 ≤3 条无滚动时 scrollTop 恒为 0，容器完全无 mask（最新条目始终清晰完整）。
  const syncClipTop = React.useCallback(() => {
    const el = scrollRef.current;
    if (el) setHasClipTop(el.scrollTop > 4);
  }, []);
  useEffect(() => {
    const el = scrollRef.current;
    if (el && expanded) {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
        syncClipTop();
      });
    }
  }, [steps.length, expanded, syncClipTop]);

  const summaryLabel = (() => {
    if (thinkingLive) {
      if (pendingStatusText) {
        return (
          <>
            <span className="text-shimmer">{pendingStatusText}</span>
            {hasHeaderReadout(steps, true, serverTotalMs, startedAt) && (
              <span className="inline-flex shrink-0 items-center font-mono text-[11px] text-ink-muted">
                （<TotalDuration steps={steps} isGenerating={thinkingLive} serverTotalMs={serverTotalMs} startedAt={startedAt} />
                {sourceCount > 0 ? <SourceReadout count={sourceCount} /> : null}）
              </span>
            )}
          </>
        );
      }
      if (steps.length === 0) {
        return (
          <>
            <span className="text-shimmer">正在理解需求</span>
            {hasHeaderReadout(steps, true, serverTotalMs, startedAt) && (
              <span className="inline-flex shrink-0 items-center font-mono text-[11px] text-ink-muted">
                （<TotalDuration steps={steps} isGenerating={thinkingLive} serverTotalMs={serverTotalMs} startedAt={startedAt} />
                {sourceCount > 0 ? <SourceReadout count={sourceCount} /> : null}）
              </span>
            )}
          </>
        );
      }
      return (
        <>
          <span className="text-shimmer">
            {latestStep?.stepName || '正在理解需求'}
          </span>
          {hasHeaderReadout(steps, true, serverTotalMs, startedAt) && (
            <span className="inline-flex items-center text-ink-muted font-mono text-[11px] shrink-0">
              （<TotalDuration steps={steps} isGenerating={thinkingLive} serverTotalMs={serverTotalMs} startedAt={startedAt} />
              {sourceCount > 0 ? <SourceReadout count={sourceCount} /> : null}）
            </span>
          )}
        </>
      );
    }
    if (steps.length === 0) return <span className="text-ink-secondary">正在理解需求</span>;
    return (
      <>
        <span className={cn('inline-flex items-center text-ink-secondary', isInterrupted && 'text-ink-secondary')}>
          思考过程 · {steps.length} 步
          {hasHeaderReadout(steps, false, serverTotalMs, startedAt) && (
            <>
              （<TotalDuration steps={steps} isGenerating={false} serverTotalMs={serverTotalMs} startedAt={startedAt} />
              {sourceCount > 0 ? <SourceReadout count={sourceCount} /> : null}）
            </>
          )}
        </span>
        {isInterrupted && <span className="text-[11px] text-warning font-medium shrink-0">· 已中断</span>}
      </>
    );
  })();

  const summaryRowClass = cn('thinking-chain-summary-row inline-flex items-center gap-1.5 w-full max-w-full text-left text-[13px] font-medium text-ink-secondary');

  return (
    <div
      className={cn(
        SHELL_THINKING_RAIL,
        thinkingLive && 'is-live',
        isTransient && 'is-transient',
        expanded && 'is-expanded',
        // `opacity-[0.92]` 走了：它把整条链的每一个字都乘了一次，包括已经在第三档的
        // 那些，而「已中断」这件事本来就由轨道的琥珀色和标题右边那枚「· 已中断」说清楚了。
        isInterrupted && 'is-interrupted'
      )}
    >
      <div className="flex flex-col gap-1">
        {/* 运行中：思维链是这一阶段页面的主体，去掉「收起整条思维链」的顶部
            chevron——此刻没有可收起进的目标，时间轴常驻展开。settled / interrupted 后才恢复
            可折叠的「思考过程 · N 步」头，让历史推理可以收起。 */}
        {steps.length === 0 || thinkingLive ? (
          <div className={cn(summaryRowClass, 'py-0.5')}>
            <span className="thinking-chain-summary-content">{summaryLabel}</span>
          </div>
        ) : (
          <button
            type="button"
            data-testid="thinking-toggle"
            onClick={() => setExpanded(!expanded)}
            className={cn(
              summaryRowClass,
              'border-0 bg-transparent p-0 cursor-pointer hover:text-ink-secondary',
              'rounded-card'
            )}
          >
            <span className="thinking-chain-summary-content">{summaryLabel}</span>
            <ChevronDown
              size={14}
              className={cn('text-ink-muted thinking-chain-chevron flex-shrink-0', expanded && 'rotate-180')}
            />
          </button>
        )}

        {steps.length > 0 && (
          <div id={regionId} className={cn('thinking-chain-toggle', (expanded || thinkingLive) && 'expanded')}>
            <div className="thinking-chain-content">
              <div className="thinking-chain-rail relative mt-1 ml-3 thinking-timeline">
                <div
                  ref={scrollRef}
                  onScroll={syncClipTop}
                  className={cn(
                    'thinking-chain-actions pl-5 py-1 thinking-scroll-max overflow-y-auto thinking-scroll',
                    hasClipTop && 'has-clip-top'
                  )}
                >
                  {groupThinkingSteps(steps).map((group, gi, arr) => (
                    <MergedToolGroupItem key={group.key} group={group} isLastGroup={gi === arr.length - 1} isGenerating={thinkingLive} />
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {isGenerating && !hasDisplay && (isSynthesizing ? <SynthesizingIndicator /> : <span className="thinking-chain-flight streaming-glyph" />)}
    </div>
  );
};
