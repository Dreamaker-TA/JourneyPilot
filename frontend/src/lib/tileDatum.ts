/**
 * 底图基准（datum）—— 把一个 WGS-84 点挪到底图供应商用的坐标系上。
 *
 * **为什么这一层存在，以及为什么它属于渲染层而不属于合同。**
 *
 * 仓里存的每一个坐标都是 WGS-84，这是 `src/travel_agent/utils/coordinates.py` 刻意建立
 * 的不变量：OSM 来源的地点身份天生是 WGS-84，高德来源的点在入库前就被转回 WGS-84，
 * 于是「这个地方在哪」全仓只有一种读法。**那条不变量不改，也不该改** —— 一个地点在
 * 地球上的位置是事实，而「某一家瓦片供应商把这个位置画在图片的哪个像素上」是那家供应
 * 商的属性。所以基准转换只发生在交给 Leaflet 画之前的最后一步，转换结果不落库、不进
 * 合同、不回传。
 *
 * 高德的栅格瓦片是 GCJ-02（「火星坐标」）。**深圳实测偏 604 m、上海 484 m** —— 在 z14
 * 以上这是「针插到隔壁街区」的量级，而地图上那句提示写着「针的位置仍然准确」。所以换
 * 底图源和这一层是同一件事的两半，不能只做一半。
 *
 * 算法与 `utils/coordinates.py::wgs84_to_gcj02` **必须逐位一致**，包括那个「中国框」
 * 矩形（框外一律 no-op）。同一段数学在两种语言里各写一份是这个仓最不喜欢的形状，所以
 * 两边共用**同一组数**互相校验：那边验
 * `gcj02_to_wgs84(121.510000, 31.238000) → (121.505604, 31.240045)`，这边就要求正向
 * 转换把 `(121.505604, 31.240045)` 送回 `(121.510000, 31.238000)`，两边一旦漂开即失配。
 */

/** 与 `utils/coordinates.py::within_china_coordinate_box` 同一个矩形，同一组常量。 */
export function withinChinaCoordinateBox(lng: number, lat: number): boolean {
  return lng > 73.66 && lng < 135.05 && lat > 3.86 && lat < 53.55;
}

const GCJ_A = 6378245.0; // 克拉索夫斯基椭球长半轴
const GCJ_EE = 0.00669342162296594323; // 第一偏心率平方

function transformLat(x: number, y: number): number {
  let ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  ret += ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0) / 3.0;
  ret += ((20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin((y / 3.0) * Math.PI)) * 2.0) / 3.0;
  ret += ((160.0 * Math.sin((y / 12.0) * Math.PI) + 320 * Math.sin((y * Math.PI) / 30.0)) * 2.0) / 3.0;
  return ret;
}

function transformLng(x: number, y: number): number {
  let ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  ret += ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0) / 3.0;
  ret += ((20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin((x / 3.0) * Math.PI)) * 2.0) / 3.0;
  ret += ((150.0 * Math.sin((x / 12.0) * Math.PI) + 300.0 * Math.sin((x / 30.0) * Math.PI)) * 2.0) / 3.0;
  return ret;
}

/** 一点上的 GCJ-02 偏移，返回 `[dlng, dlat]`，单位是度。 */
function gcjOffset(lng: number, lat: number): [number, number] {
  let dlat = transformLat(lng - 105.0, lat - 35.0);
  let dlng = transformLng(lng - 105.0, lat - 35.0);
  const radlat = (lat / 180.0) * Math.PI;
  let magic = Math.sin(radlat);
  magic = 1 - GCJ_EE * magic * magic;
  const sqrtmagic = Math.sqrt(magic);
  dlat = (dlat * 180.0) / (((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtmagic)) * Math.PI);
  dlng = (dlng * 180.0) / ((GCJ_A / sqrtmagic) * Math.cos(radlat) * Math.PI);
  return [dlng, dlat];
}

/** WGS-84 → GCJ-02，参数与返回都是 `[lng, lat]`；框外原样返回。 */
export function wgs84ToGcj02(lng: number, lat: number): [number, number] {
  if (!withinChinaCoordinateBox(lng, lat)) return [lng, lat];
  const [dlng, dlat] = gcjOffset(lng, lat);
  return [lng + dlng, lat + dlat];
}

/**
 * 地图上画东西**唯一**该调的那个函数：吃 WGS-84 的 `(纬度, 经度)`，吐 Leaflet 要的
 * `[纬度, 经度]`，已经落在当前底图的基准上。
 *
 * 参数顺序刻意跟 Leaflet 一致（纬度在前），而不是跟 `wgs84ToGcj02` 一致 —— 调用点全在
 * Leaflet 那一侧，让它们不必在每个调用点上翻一次顺序。翻错顺序是这类代码最常见的错，
 * 而且它不报错、只是把针扔到框外从而静默变成 no-op。
 */
export function toTileDatum(latitude: number, longitude: number): [number, number] {
  const [lng, lat] = wgs84ToGcj02(longitude, latitude);
  return [lat, lng];
}

/** 任何带可选经纬度的投影对象（地点、交通端点都是这个形状）。 */
interface MaybeLocated {
  latitude?: number | null;
  longitude?: number | null;
}

/**
 * 把一个投影对象的坐标取成 Leaflet 能用的点，**已经落在底图基准上**；没坐标则 `null`。
 *
 * 这一个函数就是坐标进 Leaflet 的**唯一咽喉点**。坐标会从多处进图（`center`、`MapViewport`
 * 的三条分支、`Polyline`、`Marker`），**绝不要逐处各转一遍**：漏掉任何一处的表现是「大部分
 * 东西对、有一样偏半公里」，屏幕上不报错、控制台也不报错。
 *
 * 提到 `lib/` 里为的是让「它真的过了转换」这件事可以被独立判据验证 —— 留在组件里
 * 就只能靠读代码确认，而这正是需要判据的那种地方（和 `lib/waypointOrder.ts` 同一个理由）。
 */
export function pointOnTileDatum(located: MaybeLocated | null | undefined): [number, number] | null {
  if (!located || located.latitude == null || located.longitude == null) return null;
  return toTileDatum(located.latitude, located.longitude);
}
