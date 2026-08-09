import React from 'react';
import { m } from 'motion/react';
import { ChevronDown } from 'lucide-react';
import type { DayTimelineNodeVM } from '../../lib/itineraryPresentation';
import { cn } from '../../lib/utils';
import { TRIP_DOMAIN_PRESENTATION } from '../../lib/tripDomains';
import { waypointSequenceFor, type WaypointPlacement } from '../../lib/waypointOrder';
import { staggerContainer, staggerItem } from '../../lib/motion';
import { READOUT_LABEL } from '../../lib/typography';
import { TimelineNodeMark, nodeDomain } from './TimelineNodeMark';

/**
 * 类型标签：域名词在前，跨夜/入住这类**位置**语义在后。
 *
 * 域名词是主语，role 只在它真的补充了信息时作后缀：一条跨夜航班的两半是「交通 · 抵达」与
 * 「交通 · 出发」，一段住宿的两端是「住宿 · 入住」与「住宿 · 退房」。
 *
 * **不要只印 role**（`地点安排` / `移动安排`）：role 的「地点」一挡同时盖住景点与餐饮，
 * 那是一天里最该被区分的两种安排。
 */
const CUSTOM_ROLE_LABELS: Record<DayTimelineNodeVM['role'], string> = {
  place: '自定义安排',
  movement: '自定义移动',
  arrival: '抵达',
  departure: '离开',
};

const LODGING_EDGE_LABELS: Partial<Record<DayTimelineNodeVM['role'], string>> = {
  arrival: '入住',
  departure: '退房',
};

const TRANSPORT_EDGE_LABELS: Partial<Record<DayTimelineNodeVM['role'], string>> = {
  arrival: '抵达',
  departure: '出发',
};

/**
 * 位置后缀，或 `undefined`。跨夜交通的两半与住宿的两端各自只有这一个后缀能说出
 * 「这是哪一半」—— 交通行把票面卡直接印在行上之后，它是那张卡说不出、因此必须留在行上
 * 的唯一一件事（卡两半同一张，印的是整条腿）。
 */
function nodeEdgeLabel(node: DayTimelineNodeVM): string | undefined {
  const domain = nodeDomain(node);
  if (domain === 'lodging') return LODGING_EDGE_LABELS[node.role];
  if (domain === 'transport') return TRANSPORT_EDGE_LABELS[node.role];
  return undefined;
}

function nodeTypeLabel(node: DayTimelineNodeVM): string {
  const domain = nodeDomain(node);
  if (!domain) return CUSTOM_ROLE_LABELS[node.role];
  const noun = TRIP_DOMAIN_PRESENTATION[domain].noun;
  const edge = nodeEdgeLabel(node);
  return edge ? `${noun} · ${edge}` : noun;
}

export interface DayTimelineProps {
  nodes: readonly DayTimelineNodeVM[];
  renderDetails?: (node: DayTimelineNodeVM) => React.ReactNode;
  renderSupplement?: (node: DayTimelineNodeVM) => React.ReactNode;
  /**
   * 全程针号，来自 `lib/waypointOrder`。**只有屏幕上同时有地图时才传**：这个号存在的唯一
   * 理由是指向一枚针，没有针的地方（完整报告——「a report is read, not plotted」）印它就是
   * 噪音。
   */
  waypointOrder?: ReadonlyMap<string, WaypointPlacement>;
  /**
   * 这一份时间线是不是「刚换到的这一天」。
   *
   * 交互行程里换日是一次**状态变化**——整条路线换成另一条——而合同里约束轴的规矩是
   * 「Every meaningful state change gets a transition」，列表编排就是 `stagger.step`
   * 那两个 token 的用途。完整报告不传：那是一份**印出来的文档**，一次性把每一行都动
   * 一遍就是装饰。
   */
  staggerEntrance?: boolean;
  /**
   * 纸面 ↔ 地图互相点亮。地图此刻点亮的是哪个实体；这一行是它就亮起来。
   * **只有屏幕上同时有地图时才传**，理由与 `waypointOrder` 同一条：印出来的报告没有针
   * 可以点亮，那里传它就是噪音。
   */
  linkedEntityId?: string | null;
  /** 指针停在某一行上时回报出去，让地图把那枚针点亮。`null` = 离开。 */
  onLinkEntity?: (entityId: string | null) => void;
}

/**
 * 针号记号 —— 纸面与地图的联结。
 *
 * 声部是 `chart`（印刷墨）而不是 `accent`：chart 的用途逐字写着
 * 「grid, routes, **waypoint codes**, title keyword」，而 accent 是唯一的可交互蓝，这枚
 * 号不可点。形态是**圆形轮廓 + 等宽数字**，不是填色块也不是任何一条边上的色条
 * （anti-slop：任何一条边都算，一根薄条转 90° 仍是同一根条子）。
 */
const WaypointMark: React.FC<{ sequence: number }> = ({ sequence }) => (
  <span
    data-testid={`day-timeline-waypoint-${sequence}`}
    data-waypoint={sequence}
    className="inline-flex size-[17px] shrink-0 items-center justify-center rounded-full border border-chart/55 font-mono text-[11px] font-semibold tabular-nums leading-none text-chart"
  >
    {sequence}
  </span>
);

/**
 * Canonical-order route presentation for one day.  The route itself stays
 * scannable; source, explanation, selection, and editing surfaces mount only
 * after the traveller opens the corresponding node — **except transport, whose
 * ticket card is the row** (see `inlineCard` below: a leg has no summary line of
 * its own to fold away, so folding it only meant stating the leg twice).
 *
 * The rail geometry is one set of numbers at every viewport, because the widest
 * time label (`HH:MM–HH:MM`) has to stay on one line at every viewport: a
 * 5.5rem time column, a 2.25rem gutter, and the marker at -1.75rem.  The marker
 * carries a 4px ring, so its outer edge lands 2rem left of the content column —
 * 0.25rem clear of the time column, which therefore keeps its full 5.5rem.  The
 * connector line at -1.3125rem sits exactly on the marker's centre.
 */
export function DayTimeline({
  nodes,
  renderDetails,
  renderSupplement,
  waypointOrder,
  staggerEntrance = false,
  linkedEntityId = null,
  onLinkEntity,
}: DayTimelineProps) {
  const [openNodeKey, setOpenNodeKey] = React.useState<string | null>(null);

  if (nodes.length === 0) return null;

  // 逐项延迟由 `staggerContainer(count)` 按项数收敛，整段封顶 480ms（`stagger.maxTotal`），
  // 所以一天排十几项也不会拖成一段动画表演。
  const orchestration = staggerEntrance
    ? { variants: staggerContainer(nodes.length), initial: 'hidden' as const, animate: 'visible' as const }
    : {};

  return (
    <m.ol
      data-testid="day-timeline"
      data-stagger-entrance={staggerEntrance ? 'on' : 'off'}
      aria-label="当天时间路线"
      className="min-w-0"
      {...orchestration}
    >
      {nodes.map((node, index) => {
        const details = renderDetails?.(node);
        const supplement = renderSupplement?.(node);
        /**
         * 交通行的详情**就是**那张票面卡，所以它直接印在行上，不藏在钮后面。
         *
         * 折起来的代价是这条腿在屏幕上被说了两遍：行上一句「高铁 · 上海虹桥 → 深圳北
         * 6 小时 35 分钟 ¥877」，钮后面一张印着同一批事实的卡 —— 而那张卡本来就是这批
         * 事实的定义处（`TransportLegCard`：方式名、家族名、端点、时刻、时长、换乘、
         * 费用、来源）。行上那一句于是既是复述又是**更差的**复述：它没有站点代码、
         * 没有换乘、没有来源，还得先点一下才能看到真正的那一份。
         *
         * 所以交通行只留卡说不出的东西：左边那列时刻（整条时间轴的对齐轴）、记号、
         * 轨道线，以及跨夜腿的「抵达 / 出发」后缀。**标题、时长、费用不在行上重印。**
         */
        const inlineCard = Boolean(details) && nodeDomain(node) === 'transport';
        const open = openNodeKey === node.key;
        const hasContinuation = index < nodes.length - 1;
        const detailId = `day-timeline-detail-${node.key}`;
        const waypoint = waypointOrder
          ? waypointSequenceFor(waypointOrder, node.entityKind, node.entityId)
          : null;
        // 自定义安排没有 entityId，也就没有针可以点亮——它不参与这条联动。
        const linkable = Boolean(onLinkEntity && node.entityId);
        const linked = Boolean(node.entityId) && node.entityId === linkedEntityId;

        return (
          <m.li
            key={node.key}
            data-testid={`day-timeline-node-${node.key}`}
            data-timeline-role={node.role}
            data-linked={linked ? 'on' : undefined}
            className="grid min-w-0 grid-cols-[5.5rem_minmax(0,1fr)] gap-x-9"
            variants={staggerEntrance ? staggerItem : undefined}
            // 触屏的 pointerenter 是「按下」的副产物，会把一次点按读成悬停；只让鼠标进这条路。
            onPointerEnter={linkable ? (event) => {
              if (event.pointerType === 'mouse') onLinkEntity!(node.entityId!);
            } : undefined}
            onPointerLeave={linkable ? (event) => {
              if (event.pointerType === 'mouse') onLinkEntity!(null);
            } : undefined}
          >
            <div className="pt-0.5 text-right">
              {node.timeLabel && <time className="block break-words text-xs font-semibold tabular-nums text-ink-secondary">{node.timeLabel}</time>}
            </div>
            {/* 行距由 `hasContinuation` 给，**不是** `last:pb-0`：这个 div 永远是 `li` 的
                最后一个子元素（时刻列 + 内容列，就两个），所以 `last:` 每一行都命中，
                `pb-6` 一次都没生效过 —— 而点亮底色的下边界是照着那 24px 算的，于是它
                每一行都比内容矮 16px，横穿「查看详情」那颗钮。轨道线的 `bottom-0` 落在
                这段留白的底边，所以竖线仍然接到下一枚记号。 */}
            <div className={cn('relative min-w-0', hasContinuation && 'pb-6')}>
              {hasContinuation && <span className="absolute -left-[1.3125rem] top-5 bottom-0 w-px bg-stroke" aria-hidden />}
              <TimelineNodeMark node={node} className="absolute -left-[1.75rem] top-0.5" />
              {/* 交通行不带这 2px：它的第一个元素是票面卡，卡自己的 `my-2` 已经把记号与
                  卡的上边框对齐了，再加一层就把卡按下去。 */}
              <div className={cn('relative min-w-0', node.role === 'movement' && !inlineCard ? 'pt-0.5' : '')}>
                {/* 点亮只加一层暖底，不动几何——一行长高会把它下面所有行推走，那不是「指出这一枚」。
                    底色是这一格内容的 `inset`，横向外扩 8px、纵向外扩 4px：它跟着内容自己长，
                    展开详情、缺时刻、最后一行都不用另算一套数。 */}
                <div
                  aria-hidden
                  data-testid={`day-timeline-linked-ground-${node.key}`}
                  className={cn(
                    'pointer-events-none absolute -inset-x-2 -inset-y-1 rounded-card bg-highlight',
                    'transition-opacity duration-fast ease-standard',
                    linked ? 'opacity-100' : 'opacity-0'
                  )}
                />
                <div className="relative min-w-0">
                  {inlineCard ? (
                    /* 交通行的读数行整条不出：类型名、时长、费用都在卡面上。剩下的那一个
                       后缀（抵达 / 出发）只在跨夜腿的两半上才有值，整段腿什么都不印。 */
                    nodeEdgeLabel(node) && (
                      <p className={cn(READOUT_LABEL, 'text-ink-muted')}>{nodeTypeLabel(node)}</p>
                    )
                  ) : (
                    <>
                      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                        {/* 针号排在读数行最前：它回答的是「地图上那枚几号针是哪一行」，
                            在类型与时长之前。 */}
                        {waypoint !== null && <WaypointMark sequence={waypoint} />}
                        {/* 类型标签用等宽 + 字距做成「读数」声部而不是散文：颜色信号全部由
                            左边那枚记号承担，四种颜色再复制到小字上只会更难读。 */}
                        <p className={cn(READOUT_LABEL, 'text-ink-muted')}>{nodeTypeLabel(node)}</p>
                        {/* 时长与费用刻意**不加** `tabular-nums`：`2 小时 30 分钟`、`人均 ¥160`
                            都是中英混排的「标签 · 值」读数，而 §Typography 写明等宽数字只给纯数字列。
                            纯数字的那一列是左边那条 `timeLabel`（`09:00–11:30`），它带着。 */}
                        {node.durationLabel && <span className="text-xs text-ink-secondary">{node.durationLabel}</span>}
                        {node.priceLabel && <span className="text-xs text-ink-secondary">{node.priceLabel}</span>}
                      </div>
                      <h3 className={cn('mt-0.5 break-words font-semibold text-ink', node.role === 'movement' ? 'text-sm' : 'text-base')}>
                        {node.title}
                      </h3>
                      {node.summary && <p className="mt-1 max-w-[68ch] break-words text-sm leading-6 text-ink-secondary">{node.summary}</p>}
                    </>
                  )}
                  {supplement && <div className="min-w-0">{supplement}</div>}

                  {inlineCard && <div data-testid={detailId} className="min-w-0">{details}</div>}
                  {details && !inlineCard && (
                    <>
                      <button
                        type="button"
                        data-testid={`day-timeline-details-${node.key}`}
                        aria-expanded={open}
                        aria-controls={detailId}
                        onClick={() => setOpenNodeKey((current) => current === node.key ? null : node.key)}
                        className="mt-1.5 inline-flex min-h-9 items-center gap-1 rounded-card px-2 text-xs font-semibold text-ink-secondary transition-colors hover:bg-highlight hover:text-ink"
                      >
                        {open ? '收起详情' : '查看详情'}
                        <ChevronDown size={14} aria-hidden className={cn('transition-transform', open && 'rotate-180')} />
                      </button>
                      {open && (
                        <div id={detailId} data-testid={detailId} className="mt-2 min-w-0 border-t border-stroke/60 pt-2.5">
                          {details}
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>
            </div>
          </m.li>
        );
      })}
    </m.ol>
  );
}
