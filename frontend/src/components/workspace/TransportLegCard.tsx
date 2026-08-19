import React from 'react';
import { ChevronDown, type LucideIcon } from 'lucide-react';
import { cn } from '../../lib/utils';
import { READOUT_LABEL } from '../../lib/typography';
import { TransportProviderNote } from './TransportProviderNote';
import { IntentExplanationList } from './IntentExplanationList';
import type { PublicTransportLeg, TransportEndpoint, TransportSegment } from '../../types/delivery';
import { BOOKING_LABELS, TRANSPORT_MODE_LABELS } from '../../lib/deliveryLabels';
import {
  TRANSPORT_FAMILY_NOUNS,
  TRANSPORT_MODE_ICONS,
  clockLabel,
  distanceLabel,
  durationLabel,
  legLineChain,
  legReadouts,
  legService,
  transportFamily,
  type Readout,
  type TransportFamily,
} from '../../lib/transportPresentation';

/**
 * 交通卡 —— 一个家族一张小票面。
 *
 * 判据与格式化全在 `lib/transportPresentation.ts`（哪个家族、印哪几条事实、怎么念），
 * 这里只负责把它们排到纸上。四种票面刻意长得不一样，因为它们回答的不是同一个问题：
 *
 *   班次（飞机/高铁/火车/长途巴士/轮渡）  站点代码 + 发到时刻当主读数，车次号进表头
 *   市内公共交通（地铁/公交/电车）        线路链当主读数，换乘次数与票价随后
 *   车程（出租/网约车/自驾）              没有车次也没有站点，时长/里程/费用**就是**内容，
 *                                        所以它们被抬成竖排读数
 *   自行前往（步行/骑行）                 一行说完，连费用都不出现
 *
 * **一张卡只有一条分隔线。** 票面拆成「表头 / 主读数 / 读数条 / 附注」四段、每段各自带
 * 一条 `border-t` 加 `mt-3 pt-2.5` 的话，一条 17 分钟的地铁会被排成两百多像素高的一块
 * 东西，而其中一半是空的横线。分隔线只留读数条上方那一条。
 *
 * **表头是一行，不是竖排两行。** 家族名在上、方式名在下的那种块自己就占 40px，而它印的
 * 两个词加起来七个字。
 *
 * **卡面是 `panel`，不是 `surface`。** 它落在时间线的详情格里，而那一格可能正被点亮
 * （`bg-highlight`）；卡如果也是 surface，就会和井、和点亮底连成一片深米色，卡的边界消失。
 *
 * 卡上**没有色条**：anti-slop 明令禁止用 border-left 色条做强调 —— 层次由整面着色与
 * 字重承担。交通的第二声部蓝只长在真正表意的地方：字形底板、虚线航段、线路芯片、路线箭头。
 */

function MetaLine({ values }: { values: Array<string | null> }) {
  const visible = values.filter((value): value is string => Boolean(value));
  if (visible.length === 0) return null;
  return <p className="break-words text-[11px] leading-5 text-ink-muted">{visible.join(' · ')}</p>;
}

function routeStateText(leg: PublicTransportLeg): string | null {
  if (leg.route_status === 'pending') return '路线正在更新。';
  if (leg.route_status === 'unavailable') return '路线暂时不可用。';
  return null;
}

function SegmentRow({ segment, index }: { segment: TransportSegment; index: number }) {
  const Icon = TRANSPORT_MODE_ICONS[segment.mode];
  return (
    <li className="flex min-w-0 gap-2.5 py-2 first:pt-1 last:pb-1">
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-card bg-surface text-ink-secondary" aria-hidden>
        <Icon size={12} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-3">
          <p className="min-w-0 break-words text-xs font-semibold text-ink">
            {index + 1}. {segment.from_endpoint.name} → {segment.to_endpoint.name}
          </p>
          <TimeWindow departure={segment.departure_at} arrival={segment.arrival_at} className="shrink-0 text-[11px]" />
        </div>
        <MetaLine values={[
          TRANSPORT_MODE_LABELS[segment.mode],
          segment.line_name,
          segment.service_number,
          segment.operator_name,
          durationLabel(segment.duration_minutes),
          distanceLabel(segment.distance_meters),
        ]} />
      </div>
    </li>
  );
}

function SegmentDisclosure({ leg }: { leg: PublicTransportLeg }) {
  const [open, setOpen] = React.useState(false);
  if (leg.route_status !== 'ready' || leg.segments.length <= 1) return null;
  const disclosureId = `transport-segments-${leg.transport_leg_id}`;
  return (
    <>
      <button
        type="button"
        data-testid="transport-segments-toggle"
        aria-expanded={open}
        aria-controls={disclosureId}
        onClick={() => setOpen((current) => !current)}
        className="-ml-2 mt-1 inline-flex min-h-9 items-center gap-1 rounded-card px-2 text-[11px] font-semibold text-ink-secondary transition-colors hover:bg-highlight hover:text-ink"
      >
        {open ? '收起换乘步骤' : `展开 ${leg.segments.length} 段换乘`}
        <ChevronDown size={13} aria-hidden className={cn('transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <ol id={disclosureId} className="divide-y divide-stroke/60" data-testid="transport-segments">
          {leg.segments.map((segment, index) => <SegmentRow key={segment.segment_id} segment={segment} index={index} />)}
        </ol>
      )}
    </>
  );
}

function RouteState({ leg }: { leg: PublicTransportLeg }) {
  const text = routeStateText(leg);
  if (!text) return null;
  return (
    <p role="status" className="mt-2 rounded-card bg-surface px-2.5 py-1.5 text-xs leading-5 text-ink-secondary">
      {text}
    </p>
  );
}

/**
 * 一个时间窗。两端都有才印区间，只有一端就只印那一端 —— 缺的那半**不许用占位符顶替**：
 * `23:30–—` 会被读成「—」是某种到达状态的破折号写法。
 */
function TimeWindow({
  departure,
  arrival,
  className,
}: {
  departure: string | null;
  arrival: string | null;
  className?: string;
}) {
  const from = clockLabel(departure);
  const to = clockLabel(arrival);
  if (!from && !to) return null;
  return (
    <time className={cn('font-mono tabular-nums text-ink-secondary', className)}>
      {from && to ? `${from}–${to}` : (from ?? to)}
    </time>
  );
}

/** `A → B` 一行。箭头走图纸墨蓝：它是路线符号，不是可点的东西。 */
function RouteLine({ leg, className }: { leg: PublicTransportLeg; className?: string }) {
  return (
    <p className={cn('min-w-0 break-words text-[13px] font-semibold text-ink', className)}>
      {leg.from_endpoint.name}
      <span className="mx-1.5 font-normal text-chart" aria-hidden>→</span>
      {leg.to_endpoint.name}
    </p>
  );
}

/**
 * 读数条。两种密度：
 *  - `inline`：标签 + 值同一行，班次 / 线路 / 自行前往用它（这些票面上另有主读数）。
 *  - `stacked`：标签在上、值放大，车程用它——车程没有车次也没有站点，这三条**就是**
 *    它的内容，压成 11px 灰串等于把整张卡的信息量抹掉。
 *
 * 每条读数带 `data-readout`：「哪几条事实印出来了」逐条可识别，不必去匹配一整串用 `·`
 * 拼起来的显示串。
 * 值一律走无衬线：`3 小时 30 分钟`、`4.1 公里` 都是中英混排，等宽数字会把全角字符也撑成
 * 数字宽。
 */
function ReadoutRow({ readouts, layout }: { readouts: Readout[]; layout: 'inline' | 'stacked' }) {
  if (readouts.length === 0) return null;
  const stacked = layout === 'stacked';
  return (
    <dl
      className={cn(
        'mt-2 flex min-w-0 flex-wrap border-t border-stroke/60 pt-2',
        stacked ? 'gap-x-6 gap-y-1.5' : 'items-baseline gap-x-4 gap-y-1',
      )}
    >
      {readouts.map((readout) => (
        <div
          key={readout.key}
          data-readout={readout.key}
          className={cn('min-w-0', stacked ? '' : 'flex items-baseline gap-1.5')}
        >
          <dt className={cn(READOUT_LABEL, 'shrink-0 text-ink-muted')}>
            {readout.label}
          </dt>
          <dd
            className={cn(
              'min-w-0 break-words text-ink',
              stacked ? 'mt-0.5 text-sm font-semibold' : 'text-xs font-medium',
            )}
          >
            {readout.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * 班次身份（车次号 / 线路 / 承运方）—— 票根那一段，排在表头右侧。
 *
 * 它不该独占一条带上边框的横带：那是一条分隔线加二十来像素留白，只为印一个 `MU271`。
 * 三个字段各自成元素，按 `data-service` 取。
 */
function ServiceStub({ leg }: { leg: PublicTransportLeg }) {
  const service = legService(leg);
  if (!service) return null;
  return (
    <p
      data-testid="transport-service"
      className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 font-mono text-[11px] text-ink-secondary"
    >
      {service.serviceNumber && (
        <span data-service="number" className="font-semibold tracking-[0.06em] text-ink">{service.serviceNumber}</span>
      )}
      {service.lineName && <span data-service="line">{service.lineName}</span>}
      {service.operator && <span data-service="operator">{service.operator}</span>}
    </p>
  );
}

/**
 * 票面端点。**站名是主读数，站点代码降为它下面的小字**。
 *
 * 不要照登机牌抄层次：代码 18px 等宽当「车票上最大的那个字」、站名 11px 垫在下面 ——
 * 登机牌上那三个字母**是**旅客要认的东西，而这个产品的读者不是。
 * 一个中文用户在自己的行程上找的是「上海虹桥」「深圳北」，`VNP` / `IOQ` 他既念不出也用不上；
 * 把它印成一屏里最大的字，等于让最大的那个字承载最少的信息。
 *
 * 尺寸要落在类型表里：`text-lg` 是 18px，而类型表里没有 18 这一挡
 * （表是 11/12/13/14/15/16/20/24/30/36）。
 *
 * 站名直接来自数据源，可能是很长的英文全称（Shanghai Pudong International Airport）。
 * 端点名**换行不截断**：截断会把它变成读不出来的 `Shanghai Pudong Interna…`。
 */
function TicketEndpoint({
  endpoint,
  clock,
  align,
}: {
  endpoint: TransportEndpoint;
  clock: string | null;
  align: 'left' | 'right';
}) {
  const code = endpoint.station_code;
  return (
    <div className={cn('min-w-0 flex-1', align === 'right' && 'text-right')}>
      <p className="break-words text-base font-semibold leading-tight text-ink">{endpoint.name}</p>
      {clock && (
        <p className="mt-1 font-mono text-sm tabular-nums text-ink-secondary">{clock}</p>
      )}
      {/* 代码仍然走等宽 + 字距：它是一个代号，念的是字母。 */}
      {code && (
        <p className="mt-0.5 font-mono text-[11px] font-medium leading-4 tracking-[0.06em] text-ink-secondary">
          {code}
        </p>
      )}
    </div>
  );
}

/** 虚线航段 + 方式字形，居中在两个端点之间。登机牌票面同源。 */
function TransitSpan({ Icon }: { Icon: LucideIcon }) {
  return (
    <span className="flex shrink-0 items-center gap-1 self-start pt-1.5 text-chart" aria-hidden>
      <span className="h-px w-3 border-t border-dashed border-chart/50 sm:w-5" />
      <Icon size={13} />
      <span className="h-px w-3 border-t border-dashed border-chart/50 sm:w-5" />
    </span>
  );
}

/** 市内线路链：`步行 › 银座线`。单段线路不出链条——那只是把方式名再说一遍。 */
function LineChain({ leg }: { leg: PublicTransportLeg }) {
  const chain = legLineChain(leg);
  if (chain.length <= 1) return null;
  return (
    <ol data-testid="transport-line-chain" className="mt-2 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1">
      {chain.map((name, index) => (
        <li key={`${name}-${index}`} className="flex min-w-0 items-center gap-1.5">
          {index > 0 && <span className="shrink-0 text-ink-muted" aria-hidden>›</span>}
          <span className="min-w-0 break-words rounded-label border border-chart/25 bg-chart/[0.06] px-1.5 py-0.5 text-[11px] font-medium text-chart">
            {name}
          </span>
        </li>
      ))}
    </ol>
  );
}

/**
 * 票面外壳：一行表头 + 家族自己的内容。
 *
 * 表头左侧是「字形 + 方式名 + 家族名」，右侧留给 `aside`（票根 / 时间窗）、预订角标与
 * 数据环境披露。`aside` 由各家族自己决定放什么：班次放车次号，其余放时间窗。
 */
function CardShell({
  leg,
  family,
  badge,
  aside = null,
  sandboxNote = null,
  compact = false,
  children,
}: {
  leg: PublicTransportLeg;
  family: TransportFamily;
  badge?: React.ReactNode;
  aside?: React.ReactNode;
  sandboxNote?: string | null;
  compact?: boolean;
  children: React.ReactNode;
}) {
  const Icon = TRANSPORT_MODE_ICONS[leg.selected_mode];
  const variant = leg.transport_class === 'long_distance'
    ? 'long-distance'
    : leg.transport_class === 'public_transit' ? 'public-transit' : 'flexible';
  // 沙箱披露挂在长途段上，出不出由数据说：服务端按本次证据算出 sandboxNote，
  // 没有沙箱证据就没有这句话。市内连接段不经供应商班次检索，挂上去只是噪音。
  const providerNote = leg.transport_class === 'long_distance' && Boolean(sandboxNote);
  return (
    <article
      data-testid={`transport-card-${leg.transport_leg_id}`}
      /* 两个属性讲两件事，不是一件事的两种写法：`variant` 是后端给的结构角色
         （`transport_class` + 短驳标志），`family` 是这张卡选用的票面形状。 */
      data-transport-variant={variant}
      data-transport-family={family}
      className={cn('my-2 min-w-0 overflow-hidden rounded-card border border-stroke bg-panel', compact ? 'p-2' : 'p-2.5')}
    >
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <p className="inline-flex min-w-0 items-center gap-2">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-card bg-chart/10 text-chart">
            <Icon size={13} aria-hidden />
          </span>
          <span className="min-w-0 break-words text-[13px] font-semibold text-ink">
            {TRANSPORT_MODE_LABELS[leg.selected_mode]}
          </span>
          <span className={cn(READOUT_LABEL, 'shrink-0 text-ink-muted')}>
            {TRANSPORT_FAMILY_NOUNS[family]}
          </span>
        </p>
        <span className="flex min-w-0 shrink-0 items-center gap-2">
          {aside}
          {badge}
          {providerNote && <TransportProviderNote detail={sandboxNote!} />}
        </span>
      </div>
      {children}
    </article>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return (
    <span className="shrink-0 rounded-label bg-surface px-2 py-0.5 text-[11px] font-medium text-ink-secondary">{children}</span>
  );
}

/**
 * 右上角角标。长途段说预订口径，其余段说方式是否被锁定。
 *
 * `unknown` **不出角标**：「暂无预订信息」是把「我们不知道」印成一条信息，读者会把它
 * 当成一条关于这趟班次的结论。
 */
function LegBadge({ leg }: { leg: PublicTransportLeg }) {
  if (leg.route_status !== 'ready') return null;
  if (leg.transport_class === 'long_distance') {
    if (leg.booking_status === 'unknown') return null;
    return <Badge>{BOOKING_LABELS[leg.booking_status]}</Badge>;
  }
  if (leg.mode_preference.locked_mode) return <Badge>方式已锁定</Badge>;
  return null;
}

interface TransportLegCardProps {
  leg: PublicTransportLeg;
  sourceMarkers?: React.ReactNode;
  /**
   * 沙箱披露句，来自 `bundle.provider_environment.sandbox_note`；`null` 表示不出。
   * **判据不在这里**：卡片自己判「长途 + 航班 ⇒ 沙箱」的话，换 live key 那天那句话就变成
   * 假的，而沙箱的非航班班次又一句都不出。
   */
  sandboxNote?: string | null;
}

/** 附注段：路线状态、换乘步骤、来源标记。它们不是票面主体，排在最后，不各自划线。 */
function SupportingContent({ leg, sourceMarkers }: TransportLegCardProps) {
  return (
    <>
      <RouteState leg={leg} />
      <SegmentDisclosure leg={leg} />
      <IntentExplanationList items={leg.intent_explanations} />
      {sourceMarkers}
    </>
  );
}

/** 班次票面：站点代码与发到时刻是主读数，车次号进表头。 */
function ScheduledTicket({ leg, sourceMarkers, sandboxNote = null }: TransportLegCardProps) {
  const Icon = TRANSPORT_MODE_ICONS[leg.selected_mode];
  const ready = leg.route_status === 'ready';
  return (
    <CardShell
      leg={leg}
      family="scheduled"
      badge={<LegBadge leg={leg} />}
      aside={ready ? <ServiceStub leg={leg} /> : null}
      sandboxNote={sandboxNote}
    >
      {ready && (
        <>
          <div className="mt-2 flex min-w-0 items-start gap-2">
            <TicketEndpoint endpoint={leg.from_endpoint} clock={clockLabel(leg.departure_at)} align="left" />
            <TransitSpan Icon={Icon} />
            <TicketEndpoint endpoint={leg.to_endpoint} clock={clockLabel(leg.arrival_at)} align="right" />
          </div>
          <ReadoutRow readouts={legReadouts(leg)} layout="inline" />
        </>
      )}
      <SupportingContent leg={leg} sourceMarkers={sourceMarkers} />
    </CardShell>
  );
}

/** 市内公共交通票面：线路链是主读数。 */
function TransitCard({ leg, sourceMarkers }: TransportLegCardProps) {
  const ready = leg.route_status === 'ready';
  return (
    <CardShell
      leg={leg}
      family="transit"
      badge={<LegBadge leg={leg} />}
      aside={ready ? <TimeWindow departure={leg.departure_at} arrival={leg.arrival_at} className="text-[11px]" /> : null}
    >
      {ready && (
        <>
          <RouteLine leg={leg} className="mt-2" />
          <LineChain leg={leg} />
          <ReadoutRow readouts={legReadouts(leg)} layout="inline" />
        </>
      )}
      <SupportingContent leg={leg} sourceMarkers={sourceMarkers} />
    </CardShell>
  );
}

/** 车程票面：三条读数被抬成主内容。 */
function RoadCard({ leg, sourceMarkers }: TransportLegCardProps) {
  const ready = leg.route_status === 'ready';
  return (
    <CardShell
      leg={leg}
      family="road"
      badge={<LegBadge leg={leg} />}
      aside={ready ? <TimeWindow departure={leg.departure_at} arrival={leg.arrival_at} className="text-[11px]" /> : null}
    >
      {ready && (
        <>
          <RouteLine leg={leg} className="mt-2" />
          <ReadoutRow readouts={legReadouts(leg)} layout="stacked" />
        </>
      )}
      <SupportingContent leg={leg} sourceMarkers={sourceMarkers} />
    </CardShell>
  );
}

/** 自行前往：一行说完。 */
function SelfCard({ leg, sourceMarkers }: TransportLegCardProps) {
  const ready = leg.route_status === 'ready';
  return (
    <CardShell leg={leg} family="self" compact badge={<LegBadge leg={leg} />}>
      {ready && (
        <>
          <RouteLine leg={leg} className="mt-2" />
          <ReadoutRow readouts={legReadouts(leg)} layout="inline" />
        </>
      )}
      <SupportingContent leg={leg} sourceMarkers={sourceMarkers} />
    </CardShell>
  );
}

/** 方式未知：不冒充任何一种票面，只印拿到的事实。 */
function OtherCard({ leg, sourceMarkers }: TransportLegCardProps) {
  const ready = leg.route_status === 'ready';
  return (
    <CardShell leg={leg} family="other" badge={<LegBadge leg={leg} />}>
      {ready && (
        <>
          <RouteLine leg={leg} className="mt-2" />
          <ReadoutRow readouts={legReadouts(leg)} layout="inline" />
        </>
      )}
      <SupportingContent leg={leg} sourceMarkers={sourceMarkers} />
    </CardShell>
  );
}

/**
 * 短驳：门口几百米的步行，只把两个地点连起来，收成一行。判据唯一定义在后端
 * （`entities/evidence_basis.py::is_micro_transport_leg`），组件读 `is_micro_transport`
 * 这个结论，**不自己按阈值判一遍**。
 */
function MicroConnector({ leg, sourceMarkers }: TransportLegCardProps) {
  const Icon = TRANSPORT_MODE_ICONS[leg.selected_mode];
  const facts = [durationLabel(leg.duration_minutes), distanceLabel(leg.distance_meters)]
    .filter((value): value is string => Boolean(value))
    .join(' · ');
  return (
    <article data-testid={`transport-card-${leg.transport_leg_id}`} data-transport-variant="micro" data-transport-family="self" className="min-w-0 py-1.5">
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 rounded-card border border-dashed border-chart/40 bg-panel px-2.5 py-1.5 text-xs text-ink-secondary">
        <Icon size={13} className="shrink-0 text-chart" aria-hidden />
        <strong className="min-w-0 break-words font-semibold text-ink">{leg.from_endpoint.name} → {leg.to_endpoint.name}</strong>
        {facts && <span className="text-ink-muted">{facts}</span>}
        {sourceMarkers}
      </div>
    </article>
  );
}

export const TransportLegCard: React.FC<TransportLegCardProps> = (props) => {
  if (props.leg.is_micro_transport) return <MicroConnector {...props} />;
  switch (transportFamily(props.leg.selected_mode)) {
    case 'scheduled': return <ScheduledTicket {...props} />;
    case 'transit': return <TransitCard {...props} />;
    case 'road': return <RoadCard {...props} />;
    case 'self': return <SelfCard {...props} />;
    case 'other': return <OtherCard {...props} />;
  }
};
