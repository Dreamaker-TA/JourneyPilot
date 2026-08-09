import React from 'react';
import { AnimatePresence, m } from 'motion/react';
import { useApp } from '../../context/AppContext';
import { useSendMessage } from '../../hooks/useSendMessage';
import { cn } from '../../lib/utils';
import { duration, easing } from '../../lib/motion';
import { projectVisibleMessages } from '../../lib/conversationFlow';
import { InputArea } from './InputArea';
import { MessageBubble } from './MessageBubble';
import { ContextCompactionNotice } from './ContextCompactionNotice';
import { ResearchBoardingPass } from '../workspace/ResearchBoardingPass';
import { TripPlanner } from '../trip/TripPlanner';
import { TripBriefPlanGate } from '../trip/TripBriefPlanGate';
import { DiscoveryToIntake } from '../trip/DiscoveryToIntake';
import { Neatline } from '../ui/Neatline';

/**
 * 空态 hero — Jet-Age Chartroom「Sheet No.1 · 行程图」。图纸家具（经纬网格 / 等高线 /
 * 图廓 / 航点图钉）静默环绕左对齐的 720px 图纸栏，一次性图纸绘制入场。蓝色只有登记条
 * 一个交互声部，朱砂只做航点标记。首屏仍只做一件事——让用户开始（渐进披露第 0 步）。
 */
export const HeroEmptyState: React.FC = () => {
  return (
    /**
     * **图纸是窗口，不是内容**。这一层只画家具（经纬网格 / 等高线 / 图廓），
     * 滚动交给里面那一层。此前家具和内容在**同一个滚动容器**里，同一个错误分三处露出来：
     *
     * - `overflow-y: auto` 让 `overflow-x` 也变成 `auto`（CSS 规定 `visible` 在这种搭配下
     *   计算成 `auto`），而等高线那张 svg 是 `right: -40px` —— 于是这张图纸在**任何**视口
     *   下的 `scrollWidth` 都比 `clientWidth` 宽 40px：底边多一条 4px 的滚动条（那是
     *   `::-webkit-scrollbar` 给的高度），首屏还能被横向拖开 40px。
     * - 内容比窗口高时（1419×700 实测 875 > 668），网格与图廓**跟着内容滚上去**：
     *   滚到底的那一屏下半部分是一块没有网格、图廓横穿正中的空纸。
     * - 家具的 `inset: 0` 量的是滚动容器的 padding box，所以它从来只覆盖第一屏。
     *
     * 三条都是同一个根：**家具被钉在了会动的那个盒子上**。家具留在这一层，内容自己滚。
     *
     * 这一层用 `overflow-clip` 而**不是** `overflow-hidden`：`hidden` 仍然是一个滚动容器
     * （只是不给滚动条），等高线越界的那 40px 照样能被程序或焦点滚出来；`clip` 根本不建立
     * 滚动区，所以「这张图纸自己不滚」是量得出来的（`scrollLeft` 写进去仍然读回 0）。
     *
     * `min-h-0` 是这一档必须写出来的那一半：`clip` 不是滚动容器，于是这枚 flex item 的
     * 自动最小尺寸回到 `auto`（= 内容高），`flex-1` 压不下去 —— 实测 390×844 上图纸长到
     * 1029px，多出来的 185px 被外壳的 `overflow-hidden` 裁掉，而里面那层根本没有可滚的量：
     * **手机上首页的下半截会直接消失**。写上 `min-h-0`，图纸回到窗口高度，滚动重新落在
     * `.hero-scroll` 上。
     *
     * 顶部留白在 `lg` 以下由外壳的 `pt-14` 给（那 56px 是给悬浮的「打开导航」让位的），
     * 所以这一屏自己只在 `lg` 及以上补上 —— 两处相加会把标题推到屏幕三分之一处。
     */
    <div className="hero-sheet relative flex min-h-0 flex-1 flex-col overflow-clip">
      {/* Drafting furniture — non-interactive, behind the sheet content. */}
      <svg className="hero-contours hidden sm:block" width="320" height="320" viewBox="0 0 320 320" fill="none">
        <circle className="hero-contour" cx="320" cy="0" r="90" />
        <circle className="hero-contour" cx="320" cy="0" r="140" />
        <circle className="hero-contour" cx="320" cy="0" r="190" />
        <circle className="hero-contour" cx="320" cy="0" r="240" />
      </svg>
      <Neatline inset="20px" enter className="hidden sm:block" />

      {/* Sheet content — left-aligned 720px column. */}
      <div className="hero-scroll relative z-10 flex min-h-0 flex-1 flex-col items-center overflow-y-auto px-4 pb-8 pt-2 sm:px-8 sm:pb-10 lg:pt-10">
        <div className="w-full max-w-[900px]">
          {/**
           * 产品最大的一句话。
           *
           * **这一句的用字被字体文件锁死。** 展示衬线是 14 字子集
           * `noto-serif-sc-600-trip.woff2`（3.9 KB），它的字符表逐字是
           * `一变句可想成执把旅法的程行，` —— 也就是**这一句**，一个字不多一个字不少。
           *
           * **改这句话之前必须先重做子集字体。** 换掉任何一个字，那个字就会掉到 `Songti SC`
           * （多数机器上连它也没有），产品最大的一句话变成两种字体拼出来的。而仓内没有源字体
           * 也没有子集脚本（`fontTools` 要另装，Noto Serif SC 母版不在仓里）。
           *
           * 判据钉在 `homepage-layout.spec.ts`：这一句的每一个字都必须在子集的字符表里。
           */}
          <h1 className="hero-h1 font-display text-[30px] font-semibold leading-[1.3] text-ink sm:text-[36px]">把一句想法，<br className="hidden sm:block" />变成可执行的<span className="text-chart">旅程</span></h1>
          <div className="mt-6">
            <TripPlanner />
          </div>
        </div>
      </div>
    </div>
  );
};

export const ConversationThread: React.FC = () => {
  const { state, dispatch } = useApp();
  const { sendMessage } = useSendMessage();
  const messages = state.currentMessages;
  // 计划门在等决定时，它自己就是对话投影的边界——同一条 pending 判据也决定
  // 底部 composer 是否渲染（见下方），两处读的是同一件事。
  const planGatePending = Boolean(state.planApprovalGate && state.planApprovalGate.status !== 'cancelled');
  const visibleMessages = React.useMemo(
    () => projectVisibleMessages(messages, { planGatePending }),
    [messages, planGatePending]
  );
  const showResearchBoard = Boolean(state.currentTripRunId && (state.controlledTripIdentity || state.tripSummaryCard));
  const isEmpty = messages.length === 0;
  // 正式交付只认原子 Delivery Bundle，不从聊天消息或旧面板推断可展示状态。
  const showCanvas = state.canvasOpen && state.deliveryBundle !== null;

  // 错误重试（§6.1）：用原文重发上一条用户消息。content 是原始输入（displayContent 仅展示），
  // 重发走原文；流式进行中禁用，避免叠加到正在跑的 run。
  //
  // 读的是投影后的消息，不是全量——重试提供的是「把你还看得见的那句话再发一次」。计划门
  // 等决定期间投影里没有用户消息，因此没有重试键，这是有意的：那一步真正要重试的是门的
  // 决定，而它由计划卡自己的按钮承担（`submit` 在网络失败时刻意保留计划门与本地要求）。
  const lastUserText = React.useMemo(() => {
    for (let i = visibleMessages.length - 1; i >= 0; i -= 1) {
      if (visibleMessages[i].role === 'user') return visibleMessages[i].content;
    }
    return null;
  }, [visibleMessages]);
  const retryLastUserMessage = React.useCallback(() => {
    if (!lastUserText || state.isStreaming) return;
    void sendMessage(lastUserText);
  }, [lastUserText, sendMessage, state.isStreaming]);

  const scrollRef = React.useRef<HTMLDivElement>(null);
  const stickRef = React.useRef(true);
  const lastMessage = visibleMessages[visibleMessages.length - 1];
  const lastLen = lastMessage?.displayContent?.length ?? 0;
  const lastSteps = lastMessage?.thinkingSteps?.length ?? 0;

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };

  React.useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [visibleMessages.length, lastLen, lastSteps, state.isStreaming, state.planApprovalGate]);

  const body = isEmpty ? (
    <div className="flex h-full flex-col">
      <HeroEmptyState />
    </div>
  ) : (
    <div className="flex h-full min-h-0 flex-col">
      {showResearchBoard && (
        <div className="flex-none px-4 pt-4 sm:px-6">
          <ResearchBoardingPass />
        </div>
      )}
      <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-4 py-6 sm:px-6">
        {/* 研究阶段：正文线程与顶部「行程登机牌」共用同一 max-w-5xl 通道且同一水平内边距，
            左缘对齐、宽度一致。无登机牌的快问快答仍用 max-w-3xl 保证阅读行宽。 */}
        <div className={cn('mx-auto flex flex-col gap-6', showResearchBoard ? 'max-w-5xl' : 'max-w-3xl')}>
          {visibleMessages.map((message, index) => {
            if (message.type === 'context_compaction' && message.contextCompaction) {
              return (
                <ContextCompactionNotice
                  key={message.id}
                  event={message.contextCompaction}
                />
              );
            }
            const isLast = index === visibleMessages.length - 1;
            const isGenerating = isLast && state.isStreaming && message.role === 'assistant';
            const canRetry = message.type === 'error' && !!lastUserText;
            return (
              <MessageBubble
                key={message.id}
                message={message}
                isGenerating={isGenerating}
                isLast={isLast}
                onRetry={canRetry ? retryLastUserMessage : undefined}
                retryDisabled={state.isStreaming}
              />
            );
          })}
          {state.planApprovalGate && <TripBriefPlanGate />}
          {state.pendingRouteConfirmation && (
            <section className="rounded-card border border-stroke bg-panel p-4 shadow-sm sm:p-5" aria-labelledby="route-confirmation-title">
              <h2 id="route-confirmation-title" className="text-xl font-semibold text-ink">你希望我先做哪件事？</h2>
              <p className="mt-1 text-sm text-ink-secondary">这句话可能有两种理解。</p>
              <div className="mt-4 flex flex-wrap gap-2">
                {[state.pendingRouteConfirmation.decision.route, ...state.pendingRouteConfirmation.decision.alternatives.map((item) => item.route)]
                  .filter((route, index, all) => all.indexOf(route) === index)
                  .slice(0, 2)
                  .map((route) => {
                    const labels = {
                      trip_planning: '开始规划一趟旅行',
                      destination_discovery: '先推荐并比较目的地',
                      fast_answer: '只回答这个问题',
                      trip_refinement: '调整当前行程',
                    } as const;
                    return <button key={route} type="button" className="min-h-11 rounded-card border border-stroke bg-surface px-4 text-sm font-medium text-ink hover:border-accent hover:text-accent" onClick={() => {
                      const pending = state.pendingRouteConfirmation;
                      if (!pending) return;
                      dispatch({ type: 'SET_ROUTE_CONFIRMATION', payload: null });
                      void sendMessage(pending.rawInput, undefined, {
                        route,
                        routeDecision: { ...pending.decision, route, confidence: 1, requires_trip_draft: route === 'trip_planning', requires_confirmation: false, signals: [...pending.decision.signals, 'user_confirmed_route'] },
                        displayText: labels[route],
                        assistantPendingLabel: route === 'destination_discovery' ? '正在推荐目的地' : route === 'trip_planning' ? '正在整理旅行信息' : route === 'trip_refinement' ? '正在调整行程' : '正在回答问题',
                      });
                    }}>{labels[route]}</button>;
                  })}
              </div>
            </section>
          )}
          {state.lastRouteDecision?.route === 'destination_discovery' && !state.isStreaming && !state.currentTripRunId && !state.pendingGuidedIntake && (
            <DiscoveryToIntake rawInput={lastUserText || ''} />
          )}
          {state.pendingGuidedIntake && (
            <div data-testid="guided-intake" className="rounded-card border border-stroke bg-surface/40 p-4 sm:p-5">
              <p className="font-mono text-[11px] text-chart">正在整理旅行信息</p>
              <h2 className="mt-2 mb-4 text-xl font-semibold text-ink">补齐后再创建这趟旅行</h2>
              {/* Keyed by the raw idea on purpose.  Several of the planner's fields
                  derive their *initial* state from `guidedText` — the natural-text
                  box and the destination pickers — and React reuses the landing
                  page's planner instance at this position, so those initial values
                  were fixed while `guidedText` was still `''`.  The dates and party
                  updated (they are re-read when the configuration reloads) and the
                  destination did not, which is the worst shape: the form looked
                  prefilled and had quietly dropped the one field the traveller
                  actually said.  A key makes a new intake a new instance. */}
              <TripPlanner key={state.pendingGuidedIntake.raw_input} guidedText={state.pendingGuidedIntake.raw_input} initialDestinations={state.pendingGuidedIntake.seed_destinations} compact />
            </div>
          )}
        </div>
      </div>
      {/* 运行进行中（流式调研）不渲染底部 composer：此阶段无法输入，整块输入区（含优化提示词 /
          整理较早对话）随之隐藏，停止改由「行程登机牌」进度区承载；结果出来后再恢复。
          计划审批门期间同样不渲染——确认 / 取消已就位于计划卡正下方。 */}
      {!state.isStreaming && !planGatePending && (
        <div
          className={cn(
            'px-4 py-3 sm:px-6',
            // 有面板：卡片内分层，composer 顶部保留分隔线与底色；
            // 无面板全宽形态：去掉分隔感，输入框直接坐在页面背景上（输入框自身的场样式保留）。
            showCanvas && 'border-t border-stroke bg-surface/50'
          )}
        >
          <div className="mx-auto max-w-3xl">
            {!state.pendingGuidedIntake && !state.pendingRouteConfirmation && <InputArea />}
          </div>
        </div>
      )}
    </div>
  );

  // 会话切换 crossfade：以 conversationEpoch 为 key
  // 走同一 AnimatePresence `mode="wait"` 语法——切换会话 / 新建会话有一次干净的 crossfade。
  // 纪元只在 SET_MESSAGES（载入另一段历史）与 CLEAR_CHAT（新建）时递增；同一会话内消息
  // 流入走 ADD/UPDATE 不改纪元，故流式中 key 稳定、不重挂载。
  return (
    <AnimatePresence mode="wait" initial={false}>
      <m.div
        key={state.conversationEpoch}
        className="flex h-full min-h-0 flex-col"
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0, transition: { duration: duration.base, ease: easing.decelerate } }}
        exit={{ opacity: 0, transition: { duration: duration.fast, ease: easing.accelerate } }}
      >
        {body}
      </m.div>
    </AnimatePresence>
  );
};
