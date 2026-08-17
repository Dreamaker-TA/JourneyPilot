import React from 'react';
import { AnimatePresence, m } from 'motion/react';
import { CircleAlert, CheckCircle2, ChevronDown, Loader2, Play, Route, Square } from 'lucide-react';
import { useApp } from '../../context/AppContext';
import { useSendMessage } from '../../hooks/useSendMessage';
import { useStopRun } from '../../hooks/useStopRun';
import { BrandMark } from '../ui/BrandMark';
import { TRANSPORT_MODE_ICONS } from '../../lib/transportPresentation';
import { selectPrimaryLongDistanceMode } from '../../lib/itineraryPresentation';
import { derivePlanningStageSnapshot } from '../../lib/travelProgress';
import { cn } from '../../lib/utils';
import { READOUT_LABEL } from '../../lib/typography';
import { duration, easing } from '../../lib/motion';
import { isTripRunActive, isTripRunAwaitingInput } from '../../types/api';

const STAGES = ['理解需求', '制定计划', '并行调研', '核对事实', '生成行程'];

/** 续跑请求的正文。断点续跑不读会话历史，这句话只是这次请求的可读标记。 */
const RESUME_TEXT = '继续上次被中断的规划';

function days(start: string, end: string): number {
  if (!start || !end) return 0;
  return Math.floor((new Date(`${end}T00:00:00`).getTime() - new Date(`${start}T00:00:00`).getTime()) / 86_400_000) + 1;
}

/**
 * 语义行程摘要卡 —— 做成一张真实登机牌：确定的旅行边界（出发 ✈ 抵达、日期、时长、
 * 同行、风格）印在票面，撕裂线下方的票根显示当前调研进度。信息都是已确认的，所以
 * 这张卡上唯一的交互就是展开 / 收起（DESIGN §Motion「restrained axis」）。计划确认、
 * 追加要求等待办动作不属于这里——它们作为独立卡片留在会话流中。
 */
export const ResearchBoardingPass: React.FC = () => {
  const { state } = useApp();
  const { stopRun } = useStopRun();
  const { sendMessage } = useSendMessage();
  const [expanded, setExpanded] = React.useState(true);
  const [flashing, setFlashing] = React.useState(false);
  const [resuming, setResuming] = React.useState(false);
  /* 这里**不写** `useReducedMotion()` 分支：reduce 由根节点的 `MotionConfig
     reducedMotion="user"` 统一承担（见 `motion/MotionProviders.tsx`）。`"user"` 关掉的正是
     位置类键（transform / width / height / top-left-right-bottom），也就是下面这两处动画里的
     `y` 与 `height`；opacity 仍然过渡，那是 reduce 下允许留下的那一半。每个组件各写一份手写
     分支，结果一定是一处对、其余全漏。 */
  const identity = state.controlledTripIdentity;
  const card = state.tripSummaryCard;
  const runId = state.currentTripRunId;
  const gateActive = Boolean(state.planApprovalGate);

  React.useEffect(() => setExpanded(true), [runId]);

  // 计划审批门（DESIGN §5/§8）：方案送审时票面自动收起（等待批准期间不喧宾夺主），
  // 用户确认后门解除 → 票面重新展开，进入「可展开的新形式」。
  const prevGateRef = React.useRef(gateActive);
  React.useEffect(() => {
    if (gateActive && !prevGateRef.current) setExpanded(false);
    else if (!gateActive && prevGateRef.current) setExpanded(true);
    prevGateRef.current = gateActive;
  }, [gateActive]);

  // 追加辅助信息 / 引导补充提交时（§8）：补充不进对话流，只在这张已确认的票上体现——
  // 高亮「刷一下」并展开票面，让用户看到补充已被理解并并入行程简报。
  React.useEffect(() => {
    if (state.boardingPassFlash === 0) return;
    setExpanded(true);
    setFlashing(true);
    const timer = window.setTimeout(() => setFlashing(false), 1400);
    return () => window.clearTimeout(timer);
  }, [state.boardingPassFlash]);
  const lastAssistant = [...state.currentMessages].reverse().find((message) => message.role === 'assistant');
  const answerStarted = Boolean(lastAssistant?.content?.trim());
  const snapshot = derivePlanningStageSnapshot({ steps: state.thinkingSteps, isStreaming: state.isStreaming, isSynthesizing: state.isSynthesizing, answerStarted });
  const runStatus = state.currentTripRunStatus;
  const recovery = state.currentTripRunRecovery;
  // 程序被关掉造成的中断与用户自己停下来是两件事，票面要说清是哪一件。
  // 「能不能继续」只认服务端的判定（`available_actions`），不在这里第二次推导。
  const interruptedByShutdown = runStatus === 'interrupted' && recovery !== null;
  const canResume = runStatus === 'interrupted' && recovery?.resumable === true;
  // 本次 run 自己的 Bundle：跨 run 残留的那份既不代表这张票已完成，也不该决定票面字形。
  const bundle = state.deliveryBundle?.manifest.run_id === runId ? state.deliveryBundle : null;
  const completed = runStatus === 'completed' && bundle !== null;
  const active = isTripRunActive(runStatus);
  const awaiting = isTripRunAwaitingInput(runStatus);
  const statusMessage = completed
    ? '旅行方案已生成'
    : runStatus === 'completed'
      ? '方案已生成，结果暂时无法加载'
      : runStatus === 'failed'
        ? '本次规划未完成'
        : runStatus === 'cancelled'
          ? '本次规划已取消'
          : runStatus === 'interrupted'
            ? (canResume
                ? '上次运行被程序关闭中断，可继续'
                : interruptedByShutdown
                  ? '上次运行被中断，无法继续'
                  : '运行已中断，可恢复')
            : runStatus === 'cancel_requested'
              ? '正在停止'
              : null;
  const activeIndex = completed
    ? STAGES.length
    : Math.min(STAGES.length - 1, snapshot.stages.filter((stage) => stage.status === 'done').length);

  const resume = async () => {
    if (!runId || resuming || state.isStreaming) return;
    setResuming(true);
    try {
      await sendMessage(RESUME_TEXT, undefined, {
        resumeRunId: runId,
        route: 'trip_refinement',
        assistantPendingLabel: '正在从最近检查点继续',
      });
    } finally {
      // 失败时保留「继续」入口：同一次点击可以再试，而不是把按钮永久转成 loading。
      setResuming(false);
    }
  };

  if (!runId || !identity) return null;

  const arrival = identity.destinations.map((item) => item.name).join(' → ');
  const durationDays = days(identity.start_date, identity.end_date);
  const party = `${identity.party.adults} 成人${identity.party.children ? ` · ${identity.party.children} 儿童` : ''}`;
  const style = identity.style.primary;
  // 票面字形印这趟行程真正的长途方式：高铁行程画列车，航班行程才画飞机。结果出来前
  // （或纯市内行程）没有长途段可读，用中性的路线字形，不预设一种交通方式。
  const primaryMode = bundle ? selectPrimaryLongDistanceMode(bundle.workspace.itinerary) : null;
  const RouteGlyph = primaryMode ? TRANSPORT_MODE_ICONS[primaryMode] : Route;
  // 展开态票根可承载较长的当前聚焦文案；收起态只留一个简短阶段名，保持登记条紧凑。
  const stageShort = statusMessage || (awaiting ? '等待确认' : `${STAGES[Math.min(activeIndex, STAGES.length - 1)]}中`);
  const stageLabel = statusMessage || (awaiting
    ? (state.planApprovalGate ? '等待确认调研计划' : '等待补充信息')
    : (card?.currentFocus?.trim() || stageShort));
  const statusTone = completed
    ? 'text-success'
    : runStatus === 'failed'
      ? 'text-error'
      : 'text-ink-secondary';

  return (
    <m.section
      aria-label="旅行简报"
      data-flashing={flashing ? 'true' : undefined}
      className={cn(
        /* 过渡里**没有** box-shadow（motion 规矩是「never animate `box-shadow`」），而
           时长也只能取 token 表那四挡（120/200/320/480）。闪一下本就该是被看见的那种瞬时，
           所以外圈直接落位、只让边色走 base 过渡。 */
        /* 闪一下的外圈走 `--flash-ring`，阴影走 `--shadow-lg` —— 两个都是登记过的 token，
           **不要**在这里手写等价值。写成 `var(--focus-ring-color, …)` 那类带兜底的引用最坏：
           变量没定义时它静默取兜底色（Tailwind 默认 indigo，不是本产品的 accent），没有任何
           报错；手抄一份 `0 10px 30px` 顶替 `--shadow-lg` 的 `0 12px 32px` 也一样 —— 差 2px
           谁也看不出来，但那就是同一个角色的第二个值。 */
        'research-ticket mx-auto w-full max-w-5xl overflow-hidden rounded-card border bg-panel text-ink shadow-lg transition-[border-color] duration-base ease-standard',
        flashing ? 'border-accent shadow-[var(--flash-ring),var(--shadow-lg)]' : 'border-stroke'
      )}
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: duration.base, ease: easing.decelerate }}
    >
      {/* Ticket header — the one shared, non-jumping row across both states. */}
      <header className="flex items-center gap-3 px-4 py-3 sm:px-5">
        <BrandMark size={20} className="shrink-0" />
        <div className="min-w-0 flex-1">
          <p className={cn(READOUT_LABEL, 'text-ink-muted')}>行程登机牌</p>
          <h2 className="mt-0.5 truncate text-sm font-semibold text-ink">
            <span className="text-chart">{identity.origin.name}</span> → {arrival}
            <span className="ml-2 font-normal text-ink-secondary">{durationDays} 天 {durationDays - 1} 晚</span>
          </h2>
        </div>
        {!expanded && (
          <span className="hidden shrink-0 items-center gap-1.5 rounded-label bg-surface px-2.5 py-1 font-mono text-[11px] text-ink-secondary sm:inline-flex">
            {completed ? (
              <CheckCircle2 size={12} className="text-success" />
            ) : runStatus === 'failed' ? (
              <CircleAlert size={12} className="text-error" />
            ) : active || runStatus === 'cancel_requested' || state.isStreaming ? (
              <Loader2 size={12} className="animate-spin text-accent" />
            ) : (
              <Square size={12} className="text-ink-muted" />
            )}
            {stageShort}
          </span>
        )}
        {/* 运行进行中的唯一取消入口（§5/§8）：底部 composer 在流式期间隐藏，停止移到这张
            始终可见的进度票上；展开 / 收起两态都够得着。 */}
        {/* 停止：运行中唯一的取消入口，做成常驻的破坏性红钮——与相邻的中性「收起」ghost 钮
            在静止态就明显区分，避免误触。 */}
        {/* 继续：程序被关闭造成的中断是可恢复的，但恢复必须是用户按下的这一次点击 ——
            重启不该在后台悄悄继续花模型的钱。所以这里只有入口，没有自动重试。 */}
        {canResume && !state.isStreaming && (
          <button
            type="button"
            data-testid="boarding-pass-resume"
            onClick={() => void resume()}
            disabled={resuming}
            aria-label="从最近检查点继续这次规划"
            className="inline-flex min-h-10 shrink-0 items-center gap-1.5 rounded-card border border-accent/40 bg-accent/10 px-3 text-xs font-semibold text-accent transition-colors hover:border-accent/60 hover:bg-accent/[0.16] disabled:opacity-60"
          >
            {resuming ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
            继续
          </button>
        )}
        {state.isStreaming && (
          <button
            type="button"
            data-testid="boarding-pass-stop"
            onClick={() => void stopRun()}
            aria-label="停止本次规划"
            className="inline-flex min-h-10 shrink-0 items-center gap-1.5 rounded-card border border-error/35 bg-error/10 px-3 text-xs font-semibold text-error transition-colors hover:border-error/55 hover:bg-error/[0.16]"
          >
            <Square size={12} />
            停止
          </button>
        )}
        <button
          type="button"
          aria-expanded={expanded}
          aria-label={expanded ? '收起旅行简报' : '展开旅行简报'}
          onClick={() => setExpanded((value) => !value)}
          className="inline-flex min-h-10 shrink-0 items-center gap-1 rounded-card border border-stroke px-2.5 text-xs text-ink-secondary hover:border-accent/40 hover:text-ink"
        >
          {expanded ? '收起' : '展开'}
          <ChevronDown size={14} className={cn('transition-transform', expanded && 'rotate-180')} />
        </button>
      </header>

      <AnimatePresence initial={false}>
        {expanded && (
          <m.div
            /* 展开/收起：**入场与退场各一条规格**。token 表第一句就是「Enter is long, exit
               is short」—— 进场 `slow + decelerate`，退场 `base + accelerate`。一条规格
               （例如 `base + standard`）同时服务两个方向，等于两个方向都不对。
               高度用 `height: auto`（一张票展开就是它长高，没有等价的 transform），这一条与
               侧栏轨道的 width、`ConfirmAction` 的 width 一起在 §Motion 里明码登记为获准的
               布局动画，不是漏网的。 */
            initial={{ height: 0, opacity: 0 }}
            animate={{
              height: 'auto',
              opacity: 1,
              transition: { duration: duration.slow, ease: easing.decelerate },
            }}
            exit={{
              height: 0,
              opacity: 0,
              transition: { duration: duration.base, ease: easing.accelerate },
            }}
            className="overflow-hidden border-t border-stroke/60"
          >
            {/* Ticket face — the confirmed trip printed as a boarding pass. */}
            <div className="px-4 pb-5 pt-4 sm:px-6">
              <div className="flex items-center gap-3 sm:gap-6">
                <div className="min-w-0 flex-1">
                  <p className={cn(READOUT_LABEL, 'text-ink-muted')}>出发 · FROM</p>
                  {/* 同上：多目的地的 TO（`杭州 → 苏州 → 南京`）截断会丢掉后面的城市，折行。 */}
                  <p className="mt-1 break-words text-xl font-semibold text-ink">{identity.origin.name}</p>
                </div>
                <div className="flex shrink-0 flex-col items-center text-chart" aria-hidden>
                  <RouteGlyph size={18} />
                  <span className="mt-1.5 block h-px w-12 border-t border-dashed border-stroke sm:w-20" />
                </div>
                <div className="min-w-0 flex-1 text-right">
                  <p className={cn(READOUT_LABEL, 'text-ink-muted')}>抵达 · TO</p>
                  <p className="mt-1 break-words text-xl font-semibold text-ink">{arrival}</p>
                </div>
              </div>

              <dl className="mt-5 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
                <TicketField label="日期 · DATE" value={`${identity.start_date} → ${identity.end_date}`} />
                <TicketField label="同行 · PARTY" value={party} />
                <TicketField label="风格 · STYLE" value={style} />
              </dl>

              {(identity.party.elderly_companions || identity.party.accessibility_required || identity.style.secondary_interests.length > 0 || (card?.priorities?.length ?? 0) > 0) && (
                <div className="mt-4 flex flex-wrap gap-1.5">
                  {identity.style.secondary_interests.map((item) => <Tag key={item}>{item}</Tag>)}
                  {card?.priorities?.map((item) => <Tag key={item}>{item}</Tag>)}
                  {identity.party.elderly_companions && <Tag>老人同行</Tag>}
                  {identity.party.accessibility_required && <Tag>需要无障碍</Tag>}
                </div>
              )}
            </div>

            {/* Perforation — the ticket tears here; the stub below carries progress. */}
            <div className="ticket-perf mx-4 sm:mx-6" />

            {/* Ticket stub — live research progress on the confirmed trip. */}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2 bg-surface/40 px-4 py-3 sm:px-6">
              <span className={cn(READOUT_LABEL, 'shrink-0 text-ink-muted')}>当前进度</span>
              {statusMessage ? (
                <p className={cn('inline-flex items-center gap-1.5 text-sm font-medium', statusTone)}>
                  {completed ? <CheckCircle2 size={14} /> : runStatus === 'failed' ? <CircleAlert size={14} /> : <Square size={12} />}
                  {statusMessage}
                </p>
              ) : (
                <>
                  <span className="text-sm font-medium text-ink">{stageLabel}</span>
                  <span className="ml-auto flex items-center gap-1" aria-label={`阶段 ${Math.min(activeIndex + 1, STAGES.length)} / ${STAGES.length}`}>
                    {STAGES.map((label, index) => {
                      const status = index < activeIndex ? 'done' : index === activeIndex && active ? 'active' : 'pending';
                      /* 五枚进度点：**槽位是固定的 24px，动的是里面那条的 `scaleX`**。
                         **不要**改成动 `width`（12px → 24px）：width 是布局属性，§Motion 只
                         放行侧栏轨道那一条，而且整组宽度会 76 → 88px，右边四个兄弟每一帧都
                         被推一次。`scaleX` 走合成器、不推邻居。 */
                      return (
                        <span key={label} className="flex h-1.5 w-6 items-center justify-start">
                          <span
                            className={cn(
                              'h-1.5 w-6 origin-left rounded-full',
                              'transition-[transform,background-color] duration-base ease-standard',
                              status === 'active' ? 'scale-x-100 bg-accent' : 'scale-x-50',
                              status === 'done' && 'bg-success',
                              status === 'pending' && 'bg-stroke'
                            )}
                          />
                        </span>
                      );
                    })}
                  </span>
                </>
              )}
            </div>

          </m.div>
        )}
      </AnimatePresence>
    </m.section>
  );
};

/* 票面字段是这趟行程的事实印刷件：窄栏里宁可折行也不能截断——`2026-08-05 → 2026-08-08`
   被裁成 `2026-08-05 → 20…` 等于把返程日期藏了。栏宽由 dl 的网格决定，折行只往下长。 */
const TicketField: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="min-w-0">
    <dt className={cn(READOUT_LABEL, 'text-ink-muted')}>{label}</dt>
    <dd className="mt-0.5 break-words font-mono text-sm font-medium tabular-nums text-ink">{value}</dd>
  </div>
);

const Tag: React.FC<React.PropsWithChildren> = ({ children }) => (
  <span className="rounded-label bg-surface px-2.5 py-1 text-xs text-ink-secondary">{children}</span>
);
