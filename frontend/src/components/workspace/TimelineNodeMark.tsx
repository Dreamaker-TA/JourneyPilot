import { MapPin, type LucideIcon } from 'lucide-react';
import type { DayTimelineNodeVM } from '../../lib/itineraryPresentation';
import { cn } from '../../lib/utils';
import { TRIP_DOMAIN_PRESENTATION, type TripDomain } from '../../lib/tripDomains';
import { TRANSPORT_MODE_ICONS } from '../../lib/transportPresentation';

/**
 * 一条安排的**记号** —— 域身份在纸上的那一枚点。
 *
 * 四个域必须能在余光里分开，而颜色信号**全部由这枚记号承担** —— 四种颜色再复制到小字上只会
 * 更难读。时间轴与总览卡读的是同一份。
 *
 * **不要**改成按 role 给标题文字上色（`movement` → `text-chart`、`arrival`/`departure` →
 * `text-accent`），两条都会坏：
 *
 * 1. role 只有四挡，「地点」一挡同时盖住景点与餐饮 —— 浅草寺和大黑家天麸罗于是同色，而总览
 *    恰好是用来扫读的那一屏。
 * 2. `text-accent` 是唯一的交互蓝（§Color），而节点标题不可点。
 */

/**
 * 这个节点属于哪个域。`custom` 是旅行者自己插的一条安排，不属于四个域里的任何一个，
 * 所以它返回 null 而不是被塞进最近的那个域——把自定义安排印成「景点」是在陈述一件
 * 没发生的事。
 */
export function nodeDomain(node: DayTimelineNodeVM): TripDomain | null {
  return node.entityKind === 'custom' ? null : node.entityKind;
}

/**
 * 字形取自节点背后的实体，不取 role：arrival / departure 由跨夜交通的两半与住宿的
 * 入住 / 退房共用，只看 role 分不出「这是不是航班」——全程高铁的行程会被印上飞机。
 * 交通有具体方式时用方式字形（复用路途卡同一份 TRANSPORT_MODE_ICONS），否则用域字形。
 */
export function nodeIcon(node: DayTimelineNodeVM): LucideIcon {
  if (node.transportMode) return TRANSPORT_MODE_ICONS[node.transportMode];
  const domain = nodeDomain(node);
  return domain ? TRIP_DOMAIN_PRESENTATION[domain].icon : MapPin;
}

/** 自定义安排的记号：中性、最低对比，不冒用任何一个域的印记。 */
const CUSTOM_MARK = { dot: 'bg-ink-muted ring-ink-muted/15', glyph: 'text-panel' } as const;

export function nodeMark(node: DayTimelineNodeVM): { dot: string; glyph: string } {
  const domain = nodeDomain(node);
  return domain ? TRIP_DOMAIN_PRESENTATION[domain] : CUSTOM_MARK;
}

/**
 * 记号本体。`ring` 是那 4px 光环，时间轴靠它在路线线上开一个缺口；总览卡里没有路线线，
 * 所以调用方传 `ringed={false}`——同一枚记号，少一层与背景同色的环。
 */
export function TimelineNodeMark({
  node,
  className,
  ringed = true,
}: {
  node: DayTimelineNodeVM;
  className?: string;
  ringed?: boolean;
}) {
  const mark = nodeMark(node);
  const Icon = nodeIcon(node);
  return (
    <span
      data-timeline-domain={nodeDomain(node) ?? 'custom'}
      className={cn(
        'flex size-4 shrink-0 items-center justify-center rounded-full',
        ringed && 'ring-4',
        mark.dot,
        className,
      )}
      aria-hidden
    >
      <Icon size={10} className={mark.glyph} strokeWidth={2.5} />
    </span>
  );
}
