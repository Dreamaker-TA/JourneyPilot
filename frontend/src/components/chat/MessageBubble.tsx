import React from 'react';
import {
  CircleAlert,
  Check,
  Copy,
  PanelRightOpen,
  RefreshCw,
  ShieldBan,
  ShieldQuestion,
  Square,
} from 'lucide-react';
import { cn } from '../../lib/utils';
import { getChatErrorGuidance } from '../../lib/chatErrorGuidance';
import { useApp } from '../../context/AppContext';
import { useIsMobileViewport } from '../../hooks/useMediaQuery';
import type { Message } from '../../types/chat';
import { Markdown, prepareCitationCopyText } from './Markdown';
import { ThinkingChain } from './ThinkingChain';
import { Hallmark } from '../hallmark/Hallmark';
import { InspectHint, InspectSectionTitle } from '../ui/InspectHint';
import { buildCostLedgerView } from '../../lib/costLedger';

const SHELL_ANSWER = 'rounded-card rounded-bl-label px-4 py-3 max-w-full break-words bg-panel border border-stroke/60 shadow-sm';

interface MessageBubbleProps {
  message: Message;
  isGenerating?: boolean;
  isLast?: boolean;
  onRetry?: () => void;
  /** 流式进行中禁用「重新发送」——避免重发叠加到正在跑的 run。 */
  retryDisabled?: boolean;
}

export const MessageBubble: React.FC<MessageBubbleProps> = ({ message, isGenerating, isLast, onRetry, retryDisabled }) => {
  const { state, dispatch } = useApp();
  const isMobile = useIsMobileViewport();
  const [copied, setCopied] = React.useState(false);
  const isUser = message.role === 'user';
  const isError = message.type === 'error';
  // 安全策略拒绝复用同一张告警卡的结构，但不是故障：不给恢复动作、不走琥珀色。
  const isGuardBlocked = message.type === 'guard_blocked';
  const isAlertCard = isError || isGuardBlocked;
  const isWaitingApproval = message.type === 'waiting_approval';
  const isWaiting = isWaitingApproval && state.currentTripRunStatus === 'awaiting_input';
  const isInterrupted = message.type === 'interrupted';

  const steps = message.thinkingSteps || [];
  const hasDisplay = Boolean(message.displayContent?.trim());
  const hasThinking = steps.length > 0 || (!!isGenerating && !hasDisplay);
  // 流式小飞机显隐双保险：全局 isGenerating 之外，一旦消息定稿（streamCompleted）
  // 或已中断即收口——异常路径未复位 isStreaming 时飞机也不会残留。
  const streaming = !!isGenerating && !message.streamCompleted && message.type !== 'interrupted';
  const regionId = `thinking-${message.id}`;
  const hasPlanCanvas = state.deliveryBundle !== null;
  const errorGuidance = React.useMemo(
    () => getChatErrorGuidance(
      message.displayContent || message.content || '',
      isGuardBlocked ? 'guard_blocked' : 'fault'
    ),
    [message.content, message.displayContent, isGuardBlocked]
  );

  const handleErrorAction = () => {
    switch (errorGuidance.action) {
      case 'retry':
        onRetry?.();
        break;
      case 'reload':
        window.location.reload();
        break;
      case 'edit':
        document.querySelector<HTMLTextAreaElement>('[data-testid="brief-input"]')?.focus();
        break;
    }
  };

  const showErrorAction = errorGuidance.action !== null
    && (errorGuidance.action !== 'retry' || Boolean(onRetry));

  /**
   * 本条消息所属那个 run 的成本台账 —— **这一处认领，两个消费方共用**。
   *
   * 「哪份台账属于这条消息」只判一次（`run_id === currentTripRunId` 且是最后一条助手
   * 消息）；两个消费方共用这一处判定，避免「耗时按这条规则认领、成本按那条规则认领」的两套账。
   */
  const runSummary = React.useMemo(
    () =>
      !isUser && isLast && state.runCostSummary && state.runCostSummary.run_id === state.currentTripRunId
        ? state.runCostSummary
        : null,
    [isUser, isLast, state.runCostSummary, state.currentTripRunId]
  );
  const runCostLive = React.useMemo(
    () =>
      !isUser && isLast && state.runCostLive && state.runCostLive.runId === state.currentTripRunId
        ? state.runCostLive
        : null,
    [isUser, isLast, state.runCostLive, state.currentTripRunId]
  );

  // 服务端收口的总耗时（run_cost_summary.wall_ms）——定格后思维链总耗时优先展示。
  // 生成中不给：那一段由思维链自己的本地 tick 负责。
  const serverTotalMs = isGenerating ? null : runSummary?.wall_ms ?? null;

  /**
   * 成本台账印记的数据（`cost ¤`）。终结汇总在场即整块接管，
   * 否则用运行中累加器；一次调用都没落账就是 `null`，那时印记不渲染。
   */
  const costLedger = React.useMemo(
    () => buildCostLedgerView(runSummary, runCostLive),
    [runSummary, runCostLive]
  );

  const rawErrorText = (message.content || message.displayContent || '').trim();

  const handleCopy = async () => {
    await navigator.clipboard.writeText(prepareCitationCopyText(message.content, message.citations, message.annotations));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // 计划审批门的等待态由计划卡（TripBriefPlanGate）**独家**承载：助手侧不出「等待批准方案」
  // 气泡（planApprovalGate 存在即计划门；风险门不设此状态，保持原气泡）。这一条拦的是重复的
  // 等待指示，不是文案。**删掉这行**会让脉冲气泡回到计划卡旁边，两处同时说「在等你」。
  if (isWaitingApproval && state.planApprovalGate) return null;

  return (
    <div className={cn('flex w-full', isUser ? 'flex-row-reverse animate-slide-in-right' : 'flex-row animate-slide-up')}>
      <div className={cn('flex min-w-0 flex-col', isUser ? 'items-end max-w-[85%]' : 'items-start gap-2', !isUser && (hasThinking ? 'w-full max-w-full' : 'max-w-[85%]'))}>
        {isUser && (
          /* data-user-bubble：判据钉的是「用户那句话另占了一个气泡」这件事本身，不是
             蓝底白字这套 class——按 class 钉会被任意一个同色按钮蒙对。 */
          <div data-user-bubble className="rounded-card rounded-br-label bg-accent px-4 py-3 text-white">
            <p className="whitespace-pre-wrap text-sm leading-relaxed">{message.displayContent}</p>
          </div>
        )}

        {isWaiting && (
          <div
            aria-live="polite"
            className="flex w-full min-w-0 overflow-hidden rounded-card border border-accent/15 bg-[var(--color-accent-soft)] shadow-sm"
          >
            <div className="flex w-full items-center gap-2 px-4 py-3">
              <ShieldQuestion size={14} className="flex-shrink-0 text-accent" />
              <span className="flex-1 text-sm font-medium leading-relaxed text-ink">等待批准方案</span>
              <span className="flex items-center gap-[5px]">
                <span className="waiting-dot inline-block h-[5px] w-[5px] rounded-full bg-accent" />
                <span className="waiting-dot inline-block h-[5px] w-[5px] rounded-full bg-accent" />
                <span className="waiting-dot inline-block h-[5px] w-[5px] rounded-full bg-accent" />
              </span>
            </div>
          </div>
        )}

        {/* 失败不抹掉思维链：这条 run 已经看了几分钟的推理是唯一的过程证据，错误卡只接在它下面。 */}
        {!isUser && !isWaiting && hasThinking && (
          <ThinkingChain
            steps={steps}
            isGenerating={!!isGenerating}
            displayContent={message.displayContent}
            isSynthesizing={state.isSynthesizing}
            pendingStatusText={message.pendingStatusText}
            variant={isInterrupted ? 'interrupted' : 'default'}
            serverTotalMs={serverTotalMs}
            startedAt={message.timestamp}
            regionId={regionId}
          />
        )}

        {/* 琥珀色失败卡占正文位：与正常回答同一个槽位（思维链之后），推理在前、结论在后。
            安全策略拒绝共用这张卡的结构，但换成中性色与盾牌——它是一次策略决定，不是崩溃。 */}
        {isAlertCard && (
          <div
            role="alert"
            data-testid={isGuardBlocked ? 'guard-blocked-card' : undefined}
            className={cn(
              'max-w-xl rounded-card px-4 py-4 shadow-sm sm:px-5',
              isGuardBlocked
                ? 'border border-stroke bg-panel'
                : 'border border-warning/20 bg-[color-mix(in_srgb,var(--color-warning)_6%,var(--color-panel))]'
            )}
          >
            <div className="flex items-start gap-3">
              <div
                className={cn(
                  'mt-0.5 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full',
                  isGuardBlocked ? 'bg-surface text-ink-secondary' : 'bg-warning/10 text-warning'
                )}
              >
                {isGuardBlocked ? <ShieldBan size={16} /> : <CircleAlert size={16} />}
              </div>
              <div className="flex min-w-0 flex-1 flex-col">
                <p className="text-sm font-semibold leading-snug text-ink">{errorGuidance.title}</p>
                {/* 服务端那句固定拒绝话术原样露出 —— 它是这一轮唯一的正式答复。 */}
                {isGuardBlocked && rawErrorText && (
                  <p className="mt-1.5 max-w-[56ch] text-sm leading-relaxed text-ink">{rawErrorText}</p>
                )}
                <p className="mt-1.5 max-w-[56ch] text-sm leading-relaxed text-ink-secondary">
                  {errorGuidance.description}
                </p>

                {/* 策略拒绝没有恢复动作，也没有可排查的技术详情：整行不渲染。 */}
                {isError && (
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    {showErrorAction && (
                      <button
                        type="button"
                        data-testid={errorGuidance.action === 'retry' ? 'retry-send' : 'error-primary-action'}
                        data-error-action={errorGuidance.action || undefined}
                        onClick={handleErrorAction}
                        disabled={errorGuidance.action === 'retry' && retryDisabled}
                        className="inline-flex items-center gap-1.5 self-start rounded-card border border-stroke bg-panel px-3.5 py-2 text-xs font-semibold text-ink shadow-sm transition-[border-color,background-color,color,transform] duration-fast ease-standard hover:border-accent/25 hover:bg-accent-soft hover:text-accent active:translate-y-px disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:border-stroke disabled:hover:bg-panel disabled:hover:text-ink"
                      >
                        <RefreshCw size={13} />
                        {errorGuidance.actionLabel}
                      </button>
                    )}
                    {rawErrorText && (
                      <InspectHint label="技术详情" testId="error-inspect" placement="bottom-start">
                        <InspectSectionTitle>错误技术详情</InspectSectionTitle>
                        <p className="mt-2 max-h-40 overflow-y-auto break-words font-mono text-[11px] leading-relaxed text-ink-secondary">
                          {rawErrorText}
                        </p>
                      </InspectHint>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {!isUser && !isAlertCard && !isWaiting && hasDisplay && (
          /* data-assistant-answer：判据要能问「这段话在**主对话**里出现了没有」。
             推理正文只许出现在思维链的展开面上，不许漏进这块答案卡 —— 没有一个
             稳定的选择器，那条判据只能去数全页文本，而全页包含思维链本身。 */
          <div data-assistant-answer className={cn(SHELL_ANSWER, 'relative')}>
            {(message.contextReport || costLedger) && (
              // 印记列：正文卡片右上角，与首行文字视觉对齐——像文献脚注，也像
              // 器物角落的錾印。两枚各自按「有没有数据」出现，没有数据的那一枚不占位
              // （印记的出现本身就是数据存在的证明）。
              <div className="absolute right-3 top-2.5 z-10 flex items-center gap-0.5">
                <Hallmark glyph="context" data={message.contextReport} />
                <Hallmark glyph="cost" data={costLedger} />
              </div>
            )}
            <Markdown content={message.displayContent} citations={message.citations} annotations={message.annotations} streaming={streaming} />
            {isInterrupted && (
              <div className="mt-2 flex items-center gap-1.5 border-t border-stroke pt-2">
                <Square size={10} className="text-ink-muted" />
                <span className="text-[11px] text-ink-muted">已停止生成</span>
              </div>
            )}
          </div>
        )}

        {!isUser && !isAlertCard && !isWaiting && message.content && (
          <div className="mt-1.5 flex items-center gap-3 px-1">
            <button
              type="button"
              data-testid="copy-message"
              onClick={handleCopy}
              className="flex items-center gap-1 text-[11px] text-ink-muted transition-colors hover:text-ink-secondary"
            >
              {copied ? <Check size={12} className="text-success" /> : <Copy size={12} />}
              <span>{copied ? '已复制' : '复制'}</span>
            </button>
            {isLast && hasPlanCanvas && (
              <button
                type="button"
                data-testid="open-canvas"
                onClick={() => {
                  // `<lg` 画布经 bottom sheet 呈现；桌面停靠行为不变。
                  if (isMobile) {
                    dispatch({ type: 'SET_MOBILE_CANVAS_OPEN', payload: true });
                  } else {
                    dispatch({ type: 'SET_CANVAS_OPEN', payload: true });
                  }
                }}
                className="ml-auto flex items-center gap-1 rounded-card bg-[var(--color-accent-soft)] px-2.5 py-1 text-[11px] font-medium text-accent transition-colors hover:bg-accent/10"
              >
                <PanelRightOpen size={12} />
                <span>在画布查看行程</span>
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
