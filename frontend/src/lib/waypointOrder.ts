import type { MapProjection, PublicStructuredItineraryV2 } from '../types/delivery';

/** 一个地点在全程里的落位：它是第几枚针，以及它出现在哪几天。 */
export interface WaypointPlacement {
  sequence: number;
  days: number[];
}

const PHYSICAL_ENTITY_TYPES = ['visit_stop', 'dining_stop', 'lodging_stay'] as const;

export function isPhysicalPlaceEntityType(entityType: string): boolean {
  return (PHYSICAL_ENTITY_TYPES as readonly string[]).includes(entityType);
}

/**
 * 地图**真的会插针**的那些地点：物理地点，而且拿到了坐标。
 *
 * 两个条件必须**一起**判：只按 entity_type 过滤（`DeliveryWorkspace` 那一半）或只按坐标
 * 过滤（`BundleMapLeaflet` 那一半）都不是「有针」。针号必须按同一条判定发，否则一个没有
 * 坐标的停留点会拿到号而地图上没有对应的针 —— 那个号指向一枚不存在的针。
 */
export function pinnedMapPlaceIds(
  places: readonly MapProjection['content']['places'][number][],
): string[] {
  return places
    .filter((place) => (
      isPhysicalPlaceEntityType(place.entity_ref.entity_type)
      && place.latitude != null
      && place.longitude != null
    ))
    .map((place) => place.entity_ref.entity_id);
}

/**
 * 全程的针号，一份。
 *
 * 编号按**全程首次出现**排一次，所以同一个地点在每一天都是同一个号；一个地点可以正当
 * 地属于多天（住宿第 1 天入住、第 4 天退房是同一家酒店），记的是它出现过的所有天，
 * 不是第一天——只记第一天会让第 4 天的地图丢掉当天时间线里看得见的酒店。
 *
 * 计算住在这里，**地图与纸面同读一份**。把它留在地图组件里，纸面就拿不到它：地图印着
 * 「第 3 天 · 2 个地点」和 1 号、6 号两枚针，而时间线上没有任何一行说自己是哪一枚，读者
 * 只能靠猜。合同里「Shrinking never removes route points or itinerary linkage」说的正是
 * 这条联结。两处各算一份是同一个错的另一种形态。
 *
 * `mapPlaceIds` 是地图**真的会插针**的那些地点（`map_projection` 里的物理地点）。
 * 号只发给它们：一个没有坐标、地图上没有针的停留点如果也拿到号，那个号指向一枚不存在
 * 的针，比不印更糟。所以编号在这里就跳过它们，纸面上的号因此和地图上的号逐一对得上。
 * 时间线里没有条目、地图上却有针的地点（投影允许这种形状）排在时间线之后，按传入顺序编号。
 */
export function buildWaypointOrder(
  itinerary: PublicStructuredItineraryV2,
  mapPlaceIds: readonly string[] = [],
): Map<string, WaypointPlacement> {
  const pinned = new Set(mapPlaceIds);
  const order = new Map<string, WaypointPlacement>();
  let sequence = 1;
  for (const day of itinerary.day_plans) {
    for (const entry of day.timeline) {
      if (!isPhysicalPlaceEntityType(entry.entity_type)) continue;
      if (!pinned.has(entry.entity_id)) continue;
      const current = order.get(entry.entity_id);
      if (!current) order.set(entry.entity_id, { days: [day.day], sequence: sequence++ });
      else if (!current.days.includes(day.day)) current.days.push(day.day);
    }
  }
  for (const placeId of mapPlaceIds) {
    if (!order.has(placeId)) order.set(placeId, { days: [], sequence: sequence++ });
  }
  return order;
}

/** 报告块的 `entity_kind` 里，哪几种是地图上有针的地点。 */
const PINNED_ENTITY_KINDS = new Set(['visit', 'dining', 'lodging']);

/**
 * 只有物理停留点有针号。
 *
 * 交通腿不印号：地图上只有地点有针，给一条腿印个号会指向一枚不存在的针。自定义安排同理
 * ——它没有地点身份，也就没有落位。判定按 `entity_kind`（实体是什么）而不是按 role
 * （它在当天路线里处于什么位置）：`arrival` / `departure` 由跨夜交通的两半和住宿的入住
 * 退房共用，只看 role 会把一条跨夜航班的下半段当成地点（同一条读法）。
 */
export function waypointSequenceFor(
  order: ReadonlyMap<string, WaypointPlacement>,
  entityKind: string,
  entityId: string,
): number | null {
  if (!PINNED_ENTITY_KINDS.has(entityKind)) return null;
  return order.get(entityId)?.sequence ?? null;
}
