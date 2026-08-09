import React from 'react';
import L from 'leaflet';
import { useReducedMotion } from 'motion/react';
import { MapContainer, Marker, Polyline, Popup, TileLayer, useMap } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';
import type { MapProjection, TransportEndpoint } from '../../types/delivery';
import { pointOnTileDatum } from '../../lib/tileDatum';

type ProjectedPlace = MapProjection['content']['places'][number];
type ProjectedRoute = MapProjection['content']['routes'][number];

export interface BundleMapPlace extends ProjectedPlace {
  /** 这个地点被用到的所有天（按行程顺序）；不在任何一天的时间线里时为空。 */
  days: number[];
  sequence: number;
}

interface BundleMapLeafletProps {
  places: BundleMapPlace[];
  routes: ProjectedRoute[];
  selectedEntityId: string | null;
  onSelectEntity: (entityId: string) => void;
  /** 纸面此刻悬停在哪个实体上；地图把对应的针/线点亮。 */
  linkedEntityId?: string | null;
  /** 指针停在针/线上时回报给纸面，让对应那一行亮起来。`null` = 离开。 */
  onLinkEntity?: (entityId: string | null) => void;
}

/**
 * 底图瓦片源 —— 高德 `webrd` 路网瓦片。
 *
 * **不要换回 `{s}.tile.openstreetmap.org`。** 那一组是志愿者运维的生产服务器，它对超额
 * 使用的答复是**一张告示牌图片，用 HTTP 200 发出来**：256×256 的 PNG，画着「403 Access
 * blocked — App is not following the tile usage policy of OpenStreetMap's volunteer-run
 * servers」。实测这台机器的 IP 就在限流线上 —— 串行取 6 张全通，而并发一屏 16 张约有一半
 * 是那张告示牌（去掉 `{s}` 分片、换德国镜像都是 15/16，所以额度按 IP 算、不按主机数），
 * 而 Leaflet 一屏正好并发甩 16~20 张。
 *
 * **「200 + 告示牌」比硬失败坏。** 硬失败时 `tileerror` 会触发、底图状态落到 `degraded`、
 * 提示条说实话；而告示牌是一张**合法 PNG**，Leaflet 记它为 `tileload` 成功，于是状态机报
 * `ready`、提示条不渲染 —— 屏幕上铺满一格一格的「Access blocked」，而界面在宣称它画好了。
 * **客户端补不上这一半**：跨源图片进不了 canvas（tainted），跨源 Resource Timing 的
 * `decodedBodySize` 恒为 0，而按体积判会把合法的空白海洋瓦片（高德实测 4.8 KB）误判成
 * 拒绝。唯一的解法是用不拿 200 撒谎的上游；要在客户端真正看见它，得让瓦片走同源（后端代理）。
 *
 * **高德实测的形态**：正常瓦片 200 + 8-bit PNG；越界坐标与海洋 200 + 1-bit 空白 PNG
 * （正确行为）；参数非法 **404**（真 HTTP 码 → `tileerror` 会触发）；并发 16 张 16/16
 * 全通、不限流。也就是说它用 HTTP 码报错，不用图片撒谎。
 *
 * **代价一：坐标系。** 高德瓦片是 GCJ-02，仓里存的一律 WGS-84 —— 深圳偏 604 m、上海
 * 484 m。所以每个交给 Leaflet 的点都过 `toTileDatum`，见那个文件的开头。
 * **代价二：ToS。** `webrd` 这个 `appmaptile` 端点是未公开文档的，正式做法要走高德 JS API
 * 或授权。本仓按学习 / 演示用途接受这个灰区，这一条**是明知的，不是没看见**。
 *
 * `style=8` 是路网图（无卫星影像、无 POI 拥挤标注），`lang=zh_cn` 让标注是中文。
 */
const TILE_URL =
  'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}';
const TILE_SUBDOMAINS = ['1', '2', '3', '4'];
const TILE_ATTRIBUTION = '&copy; <a href="https://amap.com/">高德地图</a>';

const FALLBACK_COLORS = [
  '#007AFF', '#34C759', '#FF9500', '#FF3B30', '#AF52DE',
  '#5856D6', '#FF2D55', '#00C7BE', '#FFD60A', '#64D2FF',
];

function colorForIndex(index: number): string {
  if (typeof window === 'undefined') return FALLBACK_COLORS[index % FALLBACK_COLORS.length];
  const token = window.getComputedStyle(document.documentElement)
    .getPropertyValue(`--chart-day-${(index % FALLBACK_COLORS.length) + 1}`)
    .trim();
  return token || FALLBACK_COLORS[index % FALLBACK_COLORS.length];
}

/** 一条路线摊成若干条线段。`useRouteLines` 负责让它一帧只算一次。 */
interface RouteLines {
  route: ProjectedRoute;
  segments: Array<[number, number][]>;
}

/**
 * 路线的线段每帧重算会让 react-leaflet 每帧 `setLatLngs`（它按**数组身份**判断
 * `positions` 变没变，见 `react-leaflet/lib/Polyline.js`）。这里算一次，
 * `MapViewport` 与 `<Polyline>` 同读这一份 —— 顺带也就不会出现「视野按一份点算、
 * 线按另一份点画」那种两套值。
 */
function useRouteLines(routes: ProjectedRoute[]): RouteLines[] {
  return React.useMemo(
    () => routes.map((route) => ({ route, segments: routeLines(route) })),
    [routes]
  );
}

/**
 * 坐标进 Leaflet 全部走 `lib/tileDatum.ts::pointOnTileDatum` 这**一个**咽喉点 ——
 * 底图是高德（GCJ-02）而仓里存的是 WGS-84，深圳差 604 m。这里两个别名只是为了让调用点
 * 读起来说的是自己在转什么，实现是同一份（提到 `lib/` 是为了让它可以被单测覆盖）。
 */
const coordinate = (endpoint: TransportEndpoint) => pointOnTileDatum(endpoint);
const placePoint = pointOnTileDatum;

function routeLines(route: ProjectedRoute): Array<[number, number][]> {
  if (route.route_status !== 'ready') return [];
  return route.segments.flatMap((segment) => {
    const from = coordinate(segment.from_endpoint);
    const to = coordinate(segment.to_endpoint);
    return from && to ? [[from, to]] : [];
  });
}

/**
 * 一枚针分成「身份」与「状态」两半。
 *
 * 身份 = 号码 + 当日色 + 实体 id，一枚针一生不变，进 `icon`（`usePlaceIcons` 保证一枚针只
 * 造一次）。状态 = 选中 / 被点亮 / 都不是，由 `applyPinState` 写在**活着的那个元素**上。
 * 点亮是纸面与地图互相照应 —— 鼠标停在哪一行，那枚针就长大并压到最上层；停在针上，那一行
 * 就亮起来；只在精确指针上生效，触屏走点击选中那条路（交互范围限定）。
 *
 * **这两半绝不能烧回同一个 `L.divIcon` 的 html 字符串里**，否则状态一变就整枚重建：
 *
 * - react-leaflet 的 Marker 按**对象身份**决定要不要换图标（`props.icon !== prevProps.icon
 *   → marker.setIcon(...)`，见 `react-leaflet/lib/Marker.js`）。每次渲染新造一个 `divIcon`
 *   就等于每次渲染都换，哪怕状态一个字都没动。
 * - Leaflet 的 `DivIcon.createIcon` 会复用外层那个 `div`，但紧接着就
 *   **`div.innerHTML = options.html`**（`leaflet-src.js::_createIcon`）—— 里面那个
 *   `<span data-bundle-map-marker>` 被整个拆掉重建。
 *
 * 代价有两层。一趟五天行程二十枚针，一次悬停就是二十个节点拆建；而更糟的是下面那条
 * `transition:transform` 根本播不出来 —— 全新的元素没有「上一个值」可以补间，于是那 120ms
 * 长大变成瞬时的：代码里写着有动效，DOM 保证它不发生。
 */
type PinState = 'selected' | 'linked' | 'idle';

/**
 * 三种状态各一份取值，**一处定义**。选中压过被点亮：镜头刚移过去的那一枚，不该被
 * 路过的指针顶掉。
 */
const PIN_STATE_STYLE: Record<PinState, (color: string) => { transform: string; boxShadow: string }> = {
  selected: (color) => ({
    transform: 'scale(1.14)',
    boxShadow: `0 0 0 4px ${color}55,0 4px 14px rgba(0,0,0,.24)`,
  }),
  linked: (color) => ({
    transform: 'scale(1.22)',
    boxShadow: `0 0 0 3px ${color}44,0 4px 12px rgba(0,0,0,.24)`,
  }),
  idle: () => ({ transform: 'scale(1)', boxShadow: '0 3px 10px rgba(0,0,0,.22)' }),
};

function placeIcon(place: BundleMapPlace, color: string): L.DivIcon {
  const idle = PIN_STATE_STYLE.idle(color);
  return L.divIcon({
    className: '',
    // 只动 `transform`（「Animate only transform and opacity」）。
    // `data-pin-color` 是给 `applyPinState` 读的：状态色只在这里算一次，不让它每次
    // 悬停都回头 `getComputedStyle` 一遍根元素。
    html: `<span data-bundle-map-marker="${place.sequence}" data-bundle-map-entity="${place.entity_ref.entity_id}" data-pin-color="${color}" style="width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:${color};color:#fff;font:700 12px/1 -apple-system,BlinkMacSystemFont,sans-serif;border:3px solid #fff;box-shadow:${idle.boxShadow};transform:${idle.transform};transition:transform var(--dur-fast,120ms) var(--ease-standard,ease)">${place.sequence}</span>`,
    iconSize: [30, 30],
    iconAnchor: [15, 15],
    popupAnchor: [0, -17],
  });
}

/** 把状态写到活着的那枚针上。**不重建元素**，所以上面那条 120ms 长大真的会播。 */
function applyPinState(pin: HTMLElement, state: PinState): void {
  const { transform, boxShadow } = PIN_STATE_STYLE[state](pin.dataset.pinColor || FALLBACK_COLORS[0]);
  pin.style.transform = transform;
  pin.style.boxShadow = boxShadow;
  if (state === 'linked') pin.dataset.linked = 'on';
  else delete pin.dataset.linked;
}

interface Pin {
  place: BundleMapPlace;
  position: [number, number];
  icon: L.DivIcon;
}

/**
 * 一枚针的坐标与 icon 都要**跨渲染保持同一个对象**，否则 react-leaflet 每帧都会去
 * `setLatLng` / `setIcon`（它按身份比 props，见 `react-leaflet/lib/Marker.js`），
 * 而 `setIcon` 会把针里那个 span 连根拆掉重建。
 *
 * icon 另走一层缓存，键是「身份」：同一枚针跨天、跨范围切换拿到的还是同一个对象，
 * 号码或当日色真变了才重建。缓存活在这一次挂载里，条目数被 Bundle 的地点数封住。
 */
function usePins(places: BundleMapPlace[]): Pin[] {
  const cache = React.useRef(new Map<string, L.DivIcon>()).current;
  return React.useMemo(() => places.flatMap((place) => {
    const position = placePoint(place);
    if (!position) return [];
    const color = colorForIndex(Math.max((place.days[0] ?? 1) - 1, 0));
    const key = `${place.entity_ref.entity_id}|${place.sequence}|${color}`;
    let icon = cache.get(key);
    if (!icon) {
      icon = placeIcon(place, color);
      cache.set(key, icon);
    }
    return [{ place, position, icon }];
  }), [cache, places]);
}

/**
 * 镜头策略。
 *
 * **选中只可能来自在地图上点针或点路线** —— 纸面那一侧只送 `linkedEntityId`（点亮），从不
 * 送选中。也就是说被点的那一枚**本来就在视野里**，所以这里绝不能一律 `flyTo(那个点, 15)`：
 * 那是把一个已经看得见的东西再飞一遍，顺带把缩放档拽到 15、丢掉全程的上下文、让 Leaflet
 * 重新取一屏瓦片 —— 读起来就是「每点一个地点就重新加载一次」。
 *
 * 三档，按「镜头到底需不需要动」分：
 *
 * | 目标在哪 | 做什么 | 为什么 |
 * |---|---|---|
 * | 已经在视野中间 | **不动** | 针长大 + 弹出框已经说清是哪一枚，动镜头是纯损失 |
 * | 视野外，但缩放已经够近 | 只平移 | 换档会把整棱瓦片换一遍，而这里不需要更多细节 |
 * | 视野外，缩放太粗 | 飞过去并给一个看得清街区的档 | 这时候用户确实需要被带过去 |
 *
 * `IN_VIEW_MARGIN` 把视野往里收一圈再判断：贴着边缘那一枚在屏幕上只露半个，算「看得见」
 * 会让点击看起来没反应。
 */
const IN_VIEW_MARGIN = -0.2;
const CLOSE_ENOUGH_ZOOM = 14;
const FOCUS_ZOOM = 15;

function MapViewport({
  places,
  lines,
  selectedEntityId,
}: Pick<BundleMapLeafletProps, 'places' | 'selectedEntityId'> & { lines: RouteLines[] }) {
  const map = useMap();
  const lastTarget = React.useRef('');
  /* Leaflet 的镜头移动是它自己的 requestAnimationFrame 补间，既不是 CSS 过渡、也不是动效库
     的补间 —— 所以 reduced-motion 那两处集中定义都管不到它，这一支必须自己读设置。
     读法用动效库的 `useReducedMotion()` 而不是自己 `matchMedia`：它返回的是
     `prefersReducedMotion.current`，也就是 `MotionConfig` 判定用的**同一个**来源，
     命令式那一层因此不会和补间那一层各判一次。（这个钩子的 docstring 写着「会随设置变化
     重新渲染」，但实现是 `useState(初值)` 没有 setter —— 它是首帧快照，库里那条 TODO
     就写在下面一行。对这两处够用：镜头移动与轮播都是每次触发时才读。） */
  const reducedMotion = useReducedMotion();

  React.useEffect(() => {
    const selectedPlace = places.find((place) => place.entity_ref.entity_id === selectedEntityId);
    const selectedPoint = selectedPlace ? placePoint(selectedPlace) : null;
    if (selectedPoint) {
      const key = `place:${selectedPlace!.entity_ref.entity_id}`;
      if (lastTarget.current === key) return;
      lastTarget.current = key;
      const target = L.latLng(selectedPoint);
      if (map.getBounds().pad(IN_VIEW_MARGIN).contains(target)) return;
      if (map.getZoom() >= CLOSE_ENOUGH_ZOOM) map.panTo(target, { animate: !reducedMotion });
      else if (reducedMotion) map.setView(target, FOCUS_ZOOM);
      else map.flyTo(target, FOCUS_ZOOM, { duration: 0.35 });
      return;
    }

    const selectedRoute = lines.find((line) => line.route.entity_ref.entity_id === selectedEntityId);
    const selectedPoints = selectedRoute ? selectedRoute.segments.flat() : [];
    if (selectedPoints.length > 0) {
      const key = `route:${selectedRoute!.route.entity_ref.entity_id}`;
      if (lastTarget.current === key) return;
      lastTarget.current = key;
      const bounds = L.latLngBounds(selectedPoints);
      // 整条腿已经完整落在视野里就不动：这一条选中要的是「看见整条」，那件事已经成立。
      if (map.getBounds().pad(IN_VIEW_MARGIN).contains(bounds)) return;
      map.fitBounds(bounds, { padding: [36, 36], animate: !reducedMotion });
      return;
    }

    const allPoints: [number, number][] = [
      ...places.flatMap((place) => { const point = placePoint(place); return point ? [point] : []; }),
      ...lines.flatMap((line) => line.segments.flat()),
    ];
    // 当前范围没东西可画：**保持原样**。地图此刻是被外层收起来的（见 DeliveryWorkspace 的
    // `ItineraryMapRegion`），把镜头挪到别处只会让它回来时是另一个地方。
    if (allPoints.length === 0) return;
    const key = `all:${allPoints.map((point) => point.join(',')).join('|')}`;
    if (lastTarget.current === key) return;
    lastTarget.current = key;
    if (allPoints.length === 1) map.setView(allPoints[0], 13);
    else map.fitBounds(L.latLngBounds(allPoints), { padding: [32, 32], animate: !reducedMotion });
  }, [map, places, lines, selectedEntityId, reducedMotion]);

  React.useEffect(() => {
    map.invalidateSize();
    const frame = window.requestAnimationFrame(() => map.invalidateSize());
    const observer = new ResizeObserver(() => map.invalidateSize());
    observer.observe(map.getContainer());
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [map]);

  return null;
}

/**
 * 底图自己的三个状态。
 *
 * 瓦片拉不到时（离线、上游被挡）**不许**照样画针、照样在左下角印「2 个地点 · 1 条路线」——
 * 那样读者看到的是一张**没有路的地图**，而界面在宣称它画好了。
 * 「No fake precision: live data, degraded connections, and unavailable sources must be
 * labeled honestly」。
 *
 * 一旦成功过就不再回到 `loading`：Leaflet 在平移缩放时保留旧瓦片，屏幕上不会空，
 * 这时候再闪一次「加载中」是噪声。
 */
type BasemapState = 'loading' | 'ready' | 'degraded';

const BASEMAP_NOTICE: Record<BasemapState, string | null> = {
  loading: '底图加载中…',
  ready: null,
  // 针的位置来自 Bundle 的投影，和瓦片是两条来源——底图没画出来不代表坐标不准，
  // 这句话得把这件事说清，否则读者会连针一起不信。
  degraded: '底图暂时没加载出来 · 针的位置仍然准确',
};

export const BundleMapLeaflet: React.FC<BundleMapLeafletProps> = ({
  places,
  routes,
  selectedEntityId,
  onSelectEntity,
  linkedEntityId = null,
  onLinkEntity,
}) => {
  const [basemap, setBasemap] = React.useState<BasemapState>('loading');
  const tileErrors = React.useRef(0);
  const pinRoot = React.useRef<HTMLDivElement>(null);
  /**
   * 拆实例之前先把还在跑的镜头补间停掉。
   *
   * `map.remove()` **不取消**正在跑的补间，而有两条回调会在拆完之后被唤醒、都落到
   * `_move → _getNewPixelOrigin → _getMapPanePos → getPosition(this._mapPane)`，
   * 而 `_mapPane` 那时已经被 `delete` 掉了 —— `Cannot read properties of undefined
   * (reading '_leaflet_pos')`：
   *
   * - `_onZoomTransitionEnd`，挂在一个内部代理元素的 `transitionend` 上（CSS 缩放补间）；
   * - `_flyToFrame` 的动画帧（`flyTo`，我们的镜头策略第三档会用）。
   *
   * **库自己在 `_onZoomTransitionEnd` 里守了 `if (this._mapPane)` 才 removeClass，
   * 紧接着那句 `_move(...)` 没守** —— 是库的漏，不是我们用错。
   *
   * **为什么是包 `remove` 而不是写在某个 effect 的 cleanup 里。** 拆实例的那句
   * `context?.map.remove()` 在 `MapContainer` 自己的 effect cleanup 里
   * （`react-leaflet/lib/MapContainer.js`），任何 effect cleanup 都赶不到它前面 —— 写在
   * 子组件里 `map.stop()` 会落在已经拆掉的实例上、自己再抛一次，写在父组件里一样晚。
   * **卸载顺序不是可以靠推理确定的东西**，所以这里不依赖顺序：谁调 `remove()` 都先停补间。
   *
   * `_animatingZoom` 单独放平 —— CSS 那条补间不由动画帧驱动，而 `_onZoomTransitionEnd`
   * 第一句正是 `if (!this._animatingZoom) return;`。**这是全仓唯一一处碰 Leaflet 私有字段
   * 的地方**，写在这里而不是包一层 try/catch：吞错会把真正的错误一起藏掉，那更糟。
   */
  const guardTeardown = React.useCallback((map: L.Map | null) => {
    if (!map) return;
    const instance = map as unknown as { _animatingZoom: boolean; _teardownGuarded?: true };
    if (instance._teardownGuarded) return;
    instance._teardownGuarded = true;
    const removeMap = map.remove.bind(map);
    map.remove = () => {
      instance._animatingZoom = false;
      map.stop();
      return removeMap();
    };
  }, []);
  // 只有精确指针参与点亮；触屏的 pointerenter 是「按下」的副产物，会把一次点击读成悬停。
  const link = (entityId: string | null) => (event: L.LeafletMouseEvent) => {
    if ((event.originalEvent as PointerEvent | undefined)?.pointerType === 'touch') return;
    onLinkEntity?.(entityId);
  };
  const lines = useRouteLines(routes);
  const pins = usePins(places);

  /**
   * 选中 / 点亮只改活着的那枚针，不重建它。
   *
   * 放在这一级、用一次 `querySelectorAll` 扫完，而不是每枚针各带一个 effect：针的元素是
   * Leaflet 从 html 串造的，React 从不碰它，所以这里是唯一能改到它的地方。effect 的时机
   * 是安全的 —— react-leaflet 在 `useLayoutEffect` 里加/更新图层，那一批一律排在被动
   * effect 之前，所以这里跑的时候新加的针已经在 DOM 上了。
   */
  React.useEffect(() => {
    const root = pinRoot.current;
    if (!root) return;
    root.querySelectorAll<HTMLElement>('[data-bundle-map-entity]').forEach((pin) => {
      const entityId = pin.dataset.bundleMapEntity;
      applyPinState(
        pin,
        entityId === selectedEntityId ? 'selected' : entityId === linkedEntityId ? 'linked' : 'idle'
      );
    });
  }, [pins, selectedEntityId, linkedEntityId]);

  /**
   * 挂载时用的中心点。
   *
   * `MapContainer` 的 `center` 是**不可变** prop（react-leaflet 的合同：首次渲染之后改它
   * 对实例没有影响），视野之后一律交给 `MapViewport`。这里记住第一个算得出来的点，是为了
   * 下面那句早退**只在从没挂过的时候**成立 —— 一旦 Leaflet 起来了就绝不再卸：卸掉等于
   * 下次回来冷启动（重新 init、重新取一屏瓦片、fitBounds 重来、「底图加载中…」再闪一次）。
   * 实测过：切到一个没有可定位地点的那一天再切回来，正是 `mapAdded=1` + 17 个瓦片请求。
   */
  const anchor = React.useRef<[number, number] | null>(null);
  const firstPoint = pins[0]?.position ?? lines.flatMap((line) => line.segments.flat())[0] ?? null;
  if (firstPoint) anchor.current = firstPoint;
  // 从来没有任何东西可画：这时候还没挂过 Leaflet，也就没有实例要保。
  if (!anchor.current) return null;

  return (
    // 状态挂在容器上而不是只挂在提示条上：`ready` 时提示条不渲染，钉要能量出
    // 「已经就绪」和「组件没挂上」的区别。
    <div ref={pinRoot} className="relative h-full w-full" data-testid="bundle-leaflet-map" data-basemap-state={basemap}>
      {BASEMAP_NOTICE[basemap] && (
        <p
          data-testid="bundle-map-basemap-state"
          role={basemap === 'degraded' ? 'status' : undefined}
          className="pointer-events-none absolute left-1/2 top-3 z-[1000] max-w-[92%] -translate-x-1/2 truncate rounded-label bg-panel/90 px-2.5 py-1 text-[11px] font-medium text-ink-secondary shadow-sm backdrop-blur"
        >
          {BASEMAP_NOTICE[basemap]}
        </p>
      )}
    <MapContainer
      ref={guardTeardown}
      center={anchor.current}
      zoom={12}
      zoomControl
      attributionControl
      preferCanvas={false}
      className="h-full w-full"
    >
      {/* **不要加 `updateWhenIdle`。** Leaflet 的出厂默认是 `Browser.mobile` —— 桌面
          `false`（平移过程中就取瓦片）、手机 `true`（等停下来再取），这里就用这个默认。
          写成裸属性等于**在桌面上强制手机行为**：`map.on('moveend', _update)` 而不是
          `'move'`（`leaflet-src.js::onAdd`），于是每一次 `flyTo` / `fitBounds` 的整个过程中
          一张瓦片都不取，屏幕先空一拍、动完才补上 —— 那一拍就是「又加载了一次」的观感。
          它唯一的好处是少打上游，而当前上游实测并发 16/16 不限流，那份客气只剩代价。 */}
      <TileLayer
        url={TILE_URL}
        subdomains={TILE_SUBDOMAINS}
        attribution={TILE_ATTRIBUTION}
        maxZoom={18}
        eventHandlers={{
          // 每一轮取瓦片重新计数：某次平移失败过，不该让后面每一次都永远背着降级。
          loading: () => { tileErrors.current = 0; },
          // Leaflet 把失败的瓦片也算「完成」，所以 `load` 到达时要看这一轮错了几张。
          load: () => setBasemap(tileErrors.current > 0 ? 'degraded' : 'ready'),
          tileerror: () => { tileErrors.current += 1; setBasemap('degraded'); },
        }}
      />
      <MapViewport places={places} lines={lines} selectedEntityId={selectedEntityId} />

      {lines.map(({ route, segments }, routeIndex) => {
        const selected = route.entity_ref.entity_id === selectedEntityId;
        const linked = route.entity_ref.entity_id === linkedEntityId;
        return segments.map((line, segmentIndex) => (
          <Polyline
            key={`${route.entity_ref.entity_id}:${segmentIndex}`}
            positions={line}
            pathOptions={{
              color: colorForIndex(routeIndex),
              weight: selected ? 7 : linked ? 5.5 : 3.5,
              opacity: selected ? 0.95 : linked ? 0.95 : selectedEntityId ? 0.3 : 0.68,
            }}
            eventHandlers={{
              click: () => onSelectEntity(route.entity_ref.entity_id),
              mouseover: link(route.entity_ref.entity_id),
              mouseout: link(null),
            }}
          />
        ));
      })}

      {pins.map(({ place, position, icon }) => {
        const entityId = place.entity_ref.entity_id;
        return (
          <Marker
            key={entityId}
            position={position}
            icon={icon}
            /* 被点亮的针压到最上层，否则它长大之后可能被邻针盖住。
               **选中的那一枚也要抬起来**：`linked` 的语义是「指针正停在它上面」，指针一走就
               掉回原层。只抬 `linked` 的话，在同一个街区里挤着的几枚针中，刚点中、已经放大到
               1.14 的那一枚会被邻针盖住，选中态在屏幕上看不见。
               谁在最上层按「此刻在指谁 > 此刻选了谁」的顺序，不是按视觉优先级。 */
            zIndexOffset={entityId === linkedEntityId ? 1000 : entityId === selectedEntityId ? 800 : 0}
            eventHandlers={{
              click: () => onSelectEntity(entityId),
              mouseover: link(entityId),
              mouseout: link(null),
            }}
            keyboard
            title={place.name}
          >
            <Popup>
              <div className="max-w-52 break-words text-xs">
                <strong>{place.sequence}. {place.name}</strong>
                {place.days.length > 0 && <p className="mt-1">第 {place.days.join('、')} 天</p>}
              </div>
            </Popup>
          </Marker>
        );
      })}
    </MapContainer>
    </div>
  );
};
