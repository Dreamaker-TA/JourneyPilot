import {
  Bike,
  BusFront,
  CarFront,
  Footprints,
  Plane,
  Ship,
  TrainFront,
  TramFront,
  type LucideIcon,
} from 'lucide-react';
import type { PublicTransportLeg, TransportMode, TransportSegment } from '../types/delivery';
import { TRANSPORT_MODE_LABELS } from './deliveryLabels';

/**
 * 一条交通腿在**纸面上是什么形状** —— 交通卡的唯一判据表。
 *
 * **不要让所有交通腿共用同一张卡**（`路途 · X` 表头 + `A → B` 一行 + 一条 `·` 拼起来的
 * 11px 灰色元信息串那种）：飞机、地铁、网约车、步行会逐像素同构，而它们要回答的根本不是
 * 同一个问题：
 *
 *   - 飞机 / 高铁 / 火车 / 长途巴士 / 轮渡：**一个你必须赶上的班次**。要紧的是车次号、
 *     站点代码、发到时刻。
 *   - 地铁 / 公交 / 有轨电车：**一条你乘的线路**。要紧的是线路名、换乘次数、票价；
 *     时刻是近似的。
 *   - 出租 / 网约车 / 自驾：**一段车程**。没有车次也没有站点，要紧的是时长、里程、费用。
 *   - 步行 / 骑行：**你自己走过去**。只有时长与里程，连费用都不该出现。
 *
 * 于是形状按**方式家族**分，而不是按 `transport_class` 分（那三挡讲的是这条腿在编排
 * 里的结构角色，长途航班和长途轮渡同挡，但票面完全不同）。
 *
 * 这个模块是纯函数：卡片印哪几条事实由 `legReadouts` 决定，因此「只显示有的信息」
 * 是一条能被单元测试钉住的合同，而不是 JSX 里的一串 `&&`。
 */
export type TransportFamily = 'scheduled' | 'transit' | 'road' | 'self' | 'other';

export const TRANSPORT_FAMILY_BY_MODE: Record<TransportMode, TransportFamily> = {
  flight: 'scheduled',
  high_speed_rail: 'scheduled',
  train: 'scheduled',
  coach: 'scheduled',
  ferry: 'scheduled',
  metro: 'transit',
  bus: 'transit',
  tram: 'transit',
  taxi: 'road',
  ride_hailing: 'road',
  drive: 'road',
  bike: 'self',
  walk: 'self',
  // 方式本身就是「不知道是什么」，所以不许它冒充任何一种票面：走通用形状，
  // 只印拿到的事实。
  other: 'other',
};

/** 交通方式字形的单一出处：路途卡、分段行、时间线节点与登机牌票面共用同一份映射。 */
export const TRANSPORT_MODE_ICONS: Record<TransportMode, LucideIcon> = {
  flight: Plane,
  high_speed_rail: TrainFront,
  train: TrainFront,
  coach: BusFront,
  ferry: Ship,
  metro: TrainFront,
  bus: BusFront,
  tram: TramFront,
  taxi: CarFront,
  ride_hailing: CarFront,
  drive: CarFront,
  bike: Bike,
  walk: Footprints,
  other: BusFront,
};

/** 家族名 —— 表头那个词。`路途` 在每张卡上都一样，等于没说。 */
export const TRANSPORT_FAMILY_NOUNS: Record<TransportFamily, string> = {
  scheduled: '班次',
  transit: '市内公共交通',
  road: '车程',
  self: '自行前往',
  other: '交通安排',
};

export function transportFamily(mode: TransportMode): TransportFamily {
  return TRANSPORT_FAMILY_BY_MODE[mode];
}

export function durationLabel(minutes: number | null): string | null {
  if (minutes == null) return null;
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours} 小时 ${remainder} 分钟` : `${hours} 小时`;
}

export function distanceLabel(meters: number | null): string | null {
  if (meters == null) return null;
  if (meters < 1000) return `${meters} 米`;
  return `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(meters / 1000)} 公里`;
}

export function moneyLabel(value: number | null): string | null {
  if (value == null) return null;
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    maximumFractionDigits: 0,
  }).format(value);
}

/** `2026-10-03T23:30:00+08:00` → `23:30`。取 Bundle 存的本地时刻，不做时区换算。 */
export function clockLabel(value: string | null): string | null {
  if (!value) return null;
  return value.match(/T(\d{2}:\d{2})/)?.[1] ?? value;
}

export interface Readout {
  key: string;
  label: string;
  value: string;
}

/**
 * 这张卡该印哪几条事实。
 *
 * 两条硬规则：
 *
 *  1. **路线没就绪就一条都不印。** 旧时长、旧费用、旧换乘在路线更新中或不可用时全部撤下。
 *     这条规则写在这里而不是组件里，所以它钉得住。
 *  2. **拿不到的字段不出现，也不用占位符替代。** 不许用 `—` 顶替缺失的时刻、给步行腿印
 *     `¥0`、给 `booking_status: unknown` 印「暂无预订信息」—— 三者都是把「没有这条信息」
 *     印成一条信息。
 *
 * 每个家族只印它这张票面上真正是内容的字段：换乘次数对班次与线路是真维度，对一段
 * 网约车车程是噪音；费用对步行不存在。
 */
export function legReadouts(leg: PublicTransportLeg): Readout[] {
  if (leg.route_status !== 'ready') return [];
  const family = transportFamily(leg.selected_mode);
  const readouts: Readout[] = [];

  const duration = durationLabel(leg.duration_minutes);
  if (duration) readouts.push({ key: 'duration', label: '时长', value: duration });

  if (family === 'road' || family === 'self' || family === 'other') {
    const distance = distanceLabel(leg.distance_meters);
    if (distance) readouts.push({ key: 'distance', label: '里程', value: distance });
  }

  if (family === 'scheduled' || family === 'transit') {
    readouts.push({
      key: 'transfer',
      label: '换乘',
      value: leg.transfer_count > 0 ? `${leg.transfer_count} 次换乘` : '直达',
    });
  }

  if (family !== 'self') {
    const cost = moneyLabel(leg.total_cost_cny);
    if (cost) readouts.push({ key: 'cost', label: '费用', value: cost });
  }

  return readouts;
}

/** 班次身份：车次号 / 线路名 / 承运方，来自第一条自报身份的分段。 */
export interface LegService {
  serviceNumber: string | null;
  lineName: string | null;
  operator: string | null;
}

export function legService(leg: PublicTransportLeg): LegService | null {
  if (leg.route_status !== 'ready') return null;
  const segment = leg.segments.find((entry) => entry.service_number || entry.line_name);
  if (!segment) return null;
  return {
    serviceNumber: segment.service_number,
    lineName: segment.line_name,
    operator: segment.operator_name,
  };
}

/**
 * 市内线路链：`步行 › 银座线 › …`。
 *
 * 没有线路名的分段用它的方式名（步行段没有线路，但它是链条上真实的一环，抹掉会让
 * 「1 次换乘」对不上眼前的链条）。
 */
export function legLineChain(leg: PublicTransportLeg): string[] {
  if (leg.route_status !== 'ready') return [];
  return leg.segments.map((segment: TransportSegment) => (
    segment.line_name || segment.service_number || TRANSPORT_MODE_LABELS[segment.mode]
  ));
}
