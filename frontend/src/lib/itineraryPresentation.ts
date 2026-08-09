import type { ConsumerNotice } from './consumerNotices';
import { weatherIconForLabel, type WeatherIconKind } from './weatherViewModel';
import type {
  EvidenceBasis,
  PublicCustomBlock,
  PublicDeliveryBundle,
  PublicDiningStop,
  PublicLodgingStay,
  PublicReportBlock,
  PublicStructuredItineraryV2,
  PublicTransportLeg,
  PublicVisitStop,
  TransportMode,
  TripReportProjection,
} from '../types/delivery';

export type ItineraryPresentationBundle = Pick<
  PublicDeliveryBundle,
  'manifest' | 'report_projection'
>;

export interface DayWeatherVM {
  dataKind: 'forecast' | 'seasonal_baseline';
  /** 时效角标的两个态与观测时间，投影期判定，界面只印。 */
  dataState: 'current' | 'historical';
  observedAt: string | null;
  icon: WeatherIconKind;
  conditionLabel: string | null;
  temperatureLabel: string | null;
  precipitationProbabilityPct: number | null;
  windSpeedKph: number | null;
}

export interface DayTimelineNodeVM {
  /** 报告块自带的位置身份（`details.entry_id`）。同一实体可以在一天里出现两次
   *  （跨夜交通的两半、住宿的入住与退房），所以键按位置算而不是按实体算。 */
  key: string;
  entityId: string;
  /** 节点背后的实体种类。role 只说这节点在当天路线里处于什么位置（arrival / departure
   *  由跨夜交通与住宿入住退房共用），字形与语义要落到实体本身才判得准。 */
  entityKind: PublicReportBlock['entity_kind'];
  projectionRole: PublicReportBlock['projection_role'];
  /** 交通节点自己的方式；非交通节点为 null。 */
  transportMode: TransportMode | null;
  role: 'place' | 'movement' | 'arrival' | 'departure';
  timeLabel: string | null;
  title: string;
  summary: string | null;
  durationLabel: string | null;
  priceLabel: string | null;
}

export type DaySummaryNodeVM = DayTimelineNodeVM;

export interface DaySummaryVM {
  dayId: string;
  day: number;
  dateLabel: string | null;
  weekdayLabel: string | null;
  destinationLabel: string | null;
  theme: string;
  weather: DayWeatherVM | null;
  nodes: DaySummaryNodeVM[];
  hiddenNodeCount: number;
  notice: ConsumerNotice | null;
}

export type TimelineEntity =
  | PublicVisitStop
  | PublicDiningStop
  | PublicLodgingStay
  | PublicTransportLeg
  | PublicCustomBlock;

type CurrentReportDocument = NonNullable<TripReportProjection['document']>;
type ReportDay = CurrentReportDocument['days'][number];

const MAX_SUMMARY_NODES = 5;


function cleanText(value: string | null | undefined): string | null {
  const text = value?.trim();
  return text ? text : null;
}

function parseDateParts(value: string | null): { year: number; month: number; day: number } | null {
  if (!value) return null;
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return null;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  if (
    parsed.getUTCFullYear() !== year
    || parsed.getUTCMonth() !== month - 1
    || parsed.getUTCDate() !== day
  ) return null;
  return { year, month, day };
}

function dateAtUtcMidnight(value: string | null): Date | null {
  const parts = parseDateParts(value);
  return parts ? new Date(Date.UTC(parts.year, parts.month - 1, parts.day)) : null;
}

/** Formats a canonical YYYY-MM-DD date without applying the viewer's time zone. */
export function formatLocalDate(value: string | null): string | null {
  const date = dateAtUtcMidnight(value);
  if (!date) return null;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'UTC',
    month: 'long',
    day: 'numeric',
  }).format(date);
}

/** Formats a canonical YYYY-MM-DD weekday without changing the trip date. */
export function formatLocalWeekday(value: string | null): string | null {
  const date = dateAtUtcMidnight(value);
  if (!date) return null;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'UTC',
    weekday: 'short',
  }).format(date);
}

/** Temperatures are displayed as whole degrees, matching the rest of the workspace. */
export function formatTemperatureRange(highC: number | null, lowC: number | null): string | null {
  const format = (value: number | null) => (
    value != null && Number.isFinite(value) ? `${Math.round(value)}°` : null
  );
  const high = format(highC);
  const low = format(lowC);
  if (high && low) return `${high} / ${low}`;
  return high ?? low;
}

/**
 * 工作台里一个实体应当陈述的依据口径；`null` 表示不陈述。
 *
 * 只有两种情况不陈述：自定义安排是用户自己写的，短驳只是把两个地点连起来的
 * 连接段——走过去不是对某个地点的断言。其余每一条都必须给出口径，「没有来源」
 * 不能再表现为「什么都不显示」。
 *
 * 「是不是短驳」不在这里算：判据唯一定义在后端，投影把结论打成
 * `is_micro_transport`（见 `types/delivery.ts`）。
 */
export function evidenceBasisForEntity(entity: TimelineEntity): EvidenceBasis | null {
  if (entity.type === 'custom_block') return null;
  if (entity.type === 'transport_leg' && entity.is_micro_transport) return null;
  return entity.evidence_basis;
}

/**
 * 正式报告块的依据口径。报告与工作台读同一份 Bundle、走同一套规则，
 * 因此同一个实体在两个界面上的结论必然一致。
 */
export function evidenceBasisForReportBlock(
  block: PublicReportBlock,
  itinerary: PublicStructuredItineraryV2,
): EvidenceBasis | null {
  if (block.entity_kind === 'custom') return null;
  if (block.entity_kind === 'transport') {
    const leg = itinerary.transport_legs.find((candidate) => (
      candidate.transport_leg_id === block.entity_ref.entity_id
    ));
    if (leg?.is_micro_transport) return null;
  }
  return block.evidence_basis;
}

/**
 * 全程主要长途方式 —— 登机牌票面字形要跟着本次行程真正的长途方式走，高铁行程不能
 * 印飞机。口径：长途段里出现次数最多的方式胜出；次数相同比总时长（更长的那段才是
 * 这趟旅行的主干）；仍相同取行程顺序里最先出现的。纯市内行程没有长途段，返回 `null`。
 */
export function selectPrimaryLongDistanceMode(
  itinerary: PublicStructuredItineraryV2,
): PublicTransportLeg['selected_mode'] | null {
  const tally = new Map<PublicTransportLeg['selected_mode'], { legs: number; minutes: number; order: number }>();
  for (const leg of itinerary.transport_legs) {
    if (leg.transport_class !== 'long_distance') continue;
    const current = tally.get(leg.selected_mode) ?? { legs: 0, minutes: 0, order: tally.size };
    tally.set(leg.selected_mode, {
      legs: current.legs + 1,
      minutes: current.minutes + (leg.duration_minutes ?? 0),
      order: current.order,
    });
  }
  return [...tally.entries()].sort(([, left], [, right]) => (
    right.legs - left.legs || right.minutes - left.minutes || left.order - right.order
  ))[0]?.[0] ?? null;
}

/**
 * 一个时间轴节点 —— 每一行都是投影层渲染好的字符串（`details`，权威在
 * `entities/delivery_presentation.py`）。这里只是把它们摆进视图模型：标题、时刻、
 * 时长、金额都不在前端拼。
 *
 * **不要**改成从行程实体自己算一遍：那样同一条腿会在这里叫「航班」、在路途卡上叫「飞机」、
 * 在 PDF 上又是第三份措辞。
 */
function nodeFromBlock(block: PublicReportBlock): DayTimelineNodeVM {
  const details = block.details;
  return {
    key: details.entry_id,
    entityId: block.entity_ref.entity_id,
    entityKind: block.entity_kind,
    projectionRole: block.projection_role,
    transportMode: details.transport_mode,
    role: details.node_role,
    timeLabel: details.time_label,
    title: details.display_title,
    summary: cleanText(details.node_summary),
    durationLabel: details.duration_label,
    priceLabel: details.price_label,
  };
}

/**
 * 当前报告文档，或 `null`。
 *
 * 「报告是不是当前的」只有这一处判据：三个修订号都要对上 manifest。报告落后于行程时
 * 界面必须说「正在同步」，不能拿旧内容当当前结果印——交互行程与正式报告读的是同一份
 * 投影产物，所以两边不能对同一份 Bundle 得出不同结论。
 */
export function selectCurrentReportDocument(
  bundle: ItineraryPresentationBundle,
): CurrentReportDocument | null {
  const report = bundle.report_projection;
  const manifest = bundle.manifest;
  if (
    report.status !== 'ready'
    || !report.document
    || report.source_workspace_revision !== manifest.workspace_revision
    || report.source_fact_data_revision !== manifest.fact_data_revision
    || report.source_weather_data_revision !== manifest.weather_data_revision
  ) return null;
  return report.document;
}

function weatherForDay(day: ReportDay, document: CurrentReportDocument): DayWeatherVM | null {
  if (!day.date) return null;
  const weather = document.weather.find((candidate) => (
    candidate.destination_id === day.destination_id && candidate.date === day.date
  ));
  if (!weather || weather.data_kind === 'unavailable') return null;
  return {
    dataKind: weather.data_kind,
    dataState: weather.weather_data_state,
    observedAt: weather.observed_at,
    icon: weatherIconForLabel(weather.condition_label),
    conditionLabel: cleanText(weather.condition_label),
    temperatureLabel: formatTemperatureRange(weather.high_c, weather.low_c),
    precipitationProbabilityPct: weather.precipitation_probability_pct,
    windSpeedKph: weather.wind_speed_kph,
  };
}

function noticeForDay(dayId: string, notices: readonly ConsumerNotice[]): ConsumerNotice | null {
  const dayNotices = [...new Map(
    notices
      .filter((notice) => notice.scope === 'day' && notice.dayId === dayId)
      .map((notice) => [notice.key, notice]),
  ).values()];
  return dayNotices.sort((left, right) => {
    const severity = (notice: ConsumerNotice) => notice.severity === 'blocking' ? 0 : 1;
    return severity(left) - severity(right);
  })[0] ?? null;
}

function summaryPriority(node: DayTimelineNodeVM): number {
  if (node.role === 'arrival' || node.role === 'departure') return 0;
  if (node.timeLabel && node.entityKind !== 'custom') return 1;
  if (node.entityKind === 'visit') return 2;
  if (node.entityKind === 'transport') return 3;
  return 4;
}

function summaryNodes(nodes: readonly DayTimelineNodeVM[]): DaySummaryNodeVM[] {
  return nodes
    .map((node, index) => ({ node, index, priority: summaryPriority(node) }))
    .sort((left, right) => left.priority - right.priority || left.index - right.index)
    .slice(0, MAX_SUMMARY_NODES)
    .sort((left, right) => left.index - right.index)
    .map(({ node }) => node);
}

/** Returns the complete, canonical-order route for one day. */
export function selectDayTimeline(
  bundle: ItineraryPresentationBundle,
  dayId: string,
): DayTimelineNodeVM[] {
  const document = selectCurrentReportDocument(bundle);
  const day = document?.days.find((candidate) => candidate.day_id === dayId);
  return day ? day.blocks.map(nodeFromBlock) : [];
}

/**
 * Produces the compact overview projection from the current public Delivery
 * Bundle.  The complete route remains separately selectable by day; no second
 * itinerary state and no re-derivation of the projected lines happens here.
 */
export function selectDaySummaries(
  bundle: ItineraryPresentationBundle,
  notices: readonly ConsumerNotice[] = [],
): DaySummaryVM[] {
  const document = selectCurrentReportDocument(bundle);
  if (!document) return [];
  return document.days.map((day) => {
    const timeline = day.blocks.map(nodeFromBlock);
    const nodes = summaryNodes(timeline);
    return {
      dayId: day.day_id,
      day: day.day,
      dateLabel: formatLocalDate(day.date),
      weekdayLabel: formatLocalWeekday(day.date),
      destinationLabel: cleanText(day.destination_name),
      theme: day.theme,
      weather: weatherForDay(day, document),
      nodes,
      hiddenNodeCount: timeline.length - nodes.length,
      notice: noticeForDay(day.day_id, notices),
    };
  });
}
