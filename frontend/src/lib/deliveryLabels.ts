import type { PublicTransportLeg, TransportMode } from '../types/delivery';

/**
 * 交付面上用户可见的标签，全前端只有这一份。
 *
 * **不许在别处再写一份交通方式的中文名。** 各写一份必然漂开 —— 同一条腿在路途卡上叫
 * 「飞机」、在当日摘要节点上叫「航班」，`other` 一边是「其它交通」一边是「交通安排」，
 * 用户看到的是同一趟行程在两个界面上被叫成两个名字。
 *
 * 判据（哪条腿算什么）在后端；这里只负责把枚举值翻成一个词。报告与 PDF 印的是后端渲染好的
 * 字符串，所以这张表只剩编辑通道在用。
 */
export const TRANSPORT_MODE_LABELS: Record<TransportMode, string> = {
  flight: '飞机',
  high_speed_rail: '高铁',
  train: '火车',
  coach: '长途巴士',
  ferry: '轮渡',
  metro: '地铁',
  bus: '公交',
  tram: '有轨电车',
  taxi: '出租车',
  ride_hailing: '网约车',
  drive: '自驾',
  bike: '骑行',
  walk: '步行',
  other: '其它交通',
};

/**
 * 预订口径。一份计划不是订单：`booking_status` 是编排模型写的、没有任何校验，
 * 所以 `booked` 说的是「你需要自己确认」，而不是宣称有一份 JourneyPilot 从未替
 * 用户下过的预订。
 *
 * 这两张表逐字等于后端权威（`entities/delivery_presentation.py`）。交付面的行由后端渲染好，
 * 这里只服务另一条通道：路途卡与方式锁定按钮读的是行程实体本身。
 */
export const BOOKING_LABELS: Record<PublicTransportLeg['booking_status'], string> = {
  not_required: '无需预订',
  recommended: '建议提前预订',
  required: '需要提前预订',
  booked: '需自行确认',
  unknown: '暂无预订信息',
};

export function bookingLabel(value: string | null): string | null {
  if (!value) return null;
  return BOOKING_LABELS[value as PublicTransportLeg['booking_status']] ?? null;
}


/**
 * 天气时效角标的两个态。逐字等于后端权威（`entities/delivery_presentation.py`）——
 * PDF 上打印的是同一句。
 *
 * 只有两个态，各一个词：要么这份读数是什么时候测的，要么它根本不是这一天的读数。
 * 刻意不加防御性文案——「这次没刷新成功」和「读数更旧」是同一件事，说两遍会让读者
 * 把其中一句当成报错。
 */
export const WEATHER_OBSERVED_SUFFIX = '观测';
export const WEATHER_HISTORICAL_LABEL = '历史天气数据';

/** 角标点开后显示的那一行。时间戳按 Bundle 存的本地时刻原样读，不做时区换算。 */
export function weatherFreshnessText(
  dataState: 'current' | 'historical',
  observedAt: string | null,
): string | null {
  if (dataState === 'historical') return WEATHER_HISTORICAL_LABEL;
  const match = observedAt?.match(/^\d{4}-(\d{2})-(\d{2})T(\d{2}:\d{2})/);
  if (!match) return null;
  return `${match[1]}-${match[2]} ${match[3]} ${WEATHER_OBSERVED_SUFFIX}`;
}

/*
 * 这里曾有一张 `TRANSPORT_MODE_GROUPS`（每个 transport_class 允许的交通方式）。它的两个
 * 消费方是槽位卡的「改用其他方式」与行程编辑器的「排除某种方式」，**两个都删掉了**
 * —— 交通是只读的。一张零消费方的表和一个过滤空集的过滤器是同一个形状：那几行看起来像是
 * 在允许什么。所以表一起删，需要它的那天从后端的 `transport_class` 定义处重新取。
 */
