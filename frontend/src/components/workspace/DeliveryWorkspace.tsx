import React from 'react';
import { m } from 'motion/react';
import {
  ArrowLeft,
  CalendarDays,
  ChevronDown,
  ChevronUp,
  FileText,
  Map as MapIcon,
  Undo2,
  X,
} from 'lucide-react';
import {
  BundleSourceCluster,
  BundleSourceDetailView,
  type BundleSourceDetail,
} from '../citations/BundleCitationMarker';
import { EvidenceBasisChip } from '../citations/EvidenceBasisChip';
import { useApp } from '../../context/AppContext';
import { useDeliveryBundleMutation } from '../../hooks/useDeliveryBundleMutation';
import { useDeliveryBundleUndo } from '../../hooks/useDeliveryBundleUndo';
import { cn } from '../../lib/utils';
import { duration, easing } from '../../lib/motion';
import { buildConsumerNotices } from '../../lib/consumerNotices';
import { buildWaypointOrder, pinnedMapPlaceIds, type WaypointPlacement } from '../../lib/waypointOrder';
import {
  evidenceBasisForEntity,
  selectCurrentReportDocument,
  selectDaySummaries,
  selectDayTimeline,
  type DaySummaryVM,
} from '../../lib/itineraryPresentation';
import type {
  EvidenceBasis,
  PublicCitationProjection,
  PublicCustomBlock,
  PublicDeliveryBundle,
  PublicDiningStop,
  PublicLodgingStay,
  PublicTransportLeg,
  PublicVisitStop,
} from '../../types/delivery';
import { SelectionSlotCard } from './SelectionSlotCard';
import type { BundleMapPlace } from './BundleMapLeaflet';
import { BundleMapLeafletLazy } from './BundleMapLeafletLazy';
import { TransportLegCard } from './TransportLegCard';
import { FullReportView } from './FullReportView';
import {
  CustomBlockRow,
  DiningStopCard,
  LodgingStayCard,
  VisitStopCard,
} from './TravelEntityCards';
import { WeatherAdjustmentPanel } from './WeatherAdjustmentPanel';
import { AddCustomBlockPanel, ItineraryEntityEditor } from './ItineraryEntityEditor';
import { DayHeader } from './DayHeader';
import { DayTimeline } from './DayTimeline';
import { TripOverview } from './TripOverview';
import { Neatline } from '../ui/Neatline';

interface DeliveryWorkspaceProps {
  bundle: PublicDeliveryBundle;
  variant?: 'docked' | 'sheet';
}

interface SourceContextValue {
  byEntity: Map<string, PublicCitationProjection[]>;
  byId: Map<string, PublicCitationProjection>;
  onOpenDetail: (detail: BundleSourceDetail) => void;
}

const SourceContext = React.createContext<SourceContextValue | null>(null);

function SourceMarkers({ citations }: { citations: PublicCitationProjection[] }) {
  const sources = React.useContext(SourceContext);
  if (!sources || citations.length === 0) return null;
  return (
    <span className="mt-2 inline-flex" data-testid="bundle-source-markers">
      <BundleSourceCluster citations={citations} onOpenDetail={sources.onOpenDetail} />
    </span>
  );
}

/**
 * 一个实体的「依据」槽：有来源就展开来源聚合标记，由规划模型依据公开资料写入
 * 时改为陈述依据口径。两者互斥地占同一个位置，卡片组件不需要知道区别。
 */
function EntityEvidenceMarkers({ entityId, basis }: { entityId: string; basis: EvidenceBasis | null }) {
  const sources = React.useContext(SourceContext);
  return (
    <>
      <SourceMarkers citations={sources?.byEntity.get(entityId) ?? []} />
      <EvidenceBasisChip basis={basis} />
    </>
  );
}

function CitationIdMarkers({ citationIds }: { citationIds: string[] }) {
  const sources = React.useContext(SourceContext);
  const citations = citationIds.flatMap((citationId) => {
    const citation = sources?.byId.get(citationId);
    return citation ? [citation] : [];
  });
  return <SourceMarkers citations={citations} />;
}

type TimelineEntity = PublicVisitStop | PublicDiningStop | PublicLodgingStay | PublicTransportLeg | PublicCustomBlock;
type ProjectionRole = 'full' | 'departure' | 'arrival' | 'check_in' | 'check_out';
type TimelineMutation = React.ComponentProps<typeof ItineraryEntityEditor>['mutation']
  & React.ComponentProps<typeof SelectionSlotCard>['mutation'];

function entityId(entity: TimelineEntity): string {
  if (entity.type === 'lodging_stay') return entity.stay_id;
  if (entity.type === 'transport_leg') return entity.transport_leg_id;
  return entity.item_id;
}

function buildEntityIndex(bundle: PublicDeliveryBundle): Map<string, TimelineEntity> {
  const itinerary = bundle.workspace.itinerary;
  const entities: TimelineEntity[] = [
    ...itinerary.visit_stops,
    ...itinerary.dining_stops,
    ...itinerary.lodging_stays,
    ...itinerary.transport_legs,
    ...itinerary.custom_blocks,
  ];
  const index = new Map<string, TimelineEntity>();
  for (const entity of entities) {
    index.set(entityId(entity), entity);
  }
  return index;
}

function TimelineEntityDetails({
  bundle,
  entity,
  entityId,
  projectionRole = 'full',
  slot,
  mutation,
  readOnly,
}: {
  bundle: PublicDeliveryBundle;
  entity: TimelineEntity;
  entityId: string;
  projectionRole?: ProjectionRole;
  slot: PublicDeliveryBundle['workspace']['selection_slots'][number] | null;
  mutation: TimelineMutation;
  readOnly: boolean;
}) {
  /**
   * 交通是**只读**的：一行就是一张票面，票面下面没有任何操作。
   *
   * 这条腿是编排的产物 —— 班次、路线、时刻、票价都来自供应商证据与门的判定，让用户在纸面上
   * 改方式 / 换候选 / 重绑路线 / 删掉一段，改的是结论而不是输入，而结论一旦被手改，
   * 它旁边那些「来源」「依据」就不再对应它。所以这里**提前 return**：不套 `SelectionSlotCard`
   * （槽位卡的三颗操作全是交通专属），也不挂 `ItineraryEntityEditor`。
   *
   * 提前 return 还让下面那句 `editor` 的类型收窄到剩下四个域 —— 编辑器不再接受交通腿，
   * 这是类型层面的同一句话，不是靠记性。
   */
  if (entity.type === 'transport_leg') {
    return (
      <TransportLegCard
        leg={entity}
        sourceMarkers={<EntityEvidenceMarkers entityId={entityId} basis={evidenceBasisForEntity(entity)} />}
        sandboxNote={bundle.provider_environment.sandbox_note}
      />
    );
  }
  const editor = !readOnly && <ItineraryEntityEditor bundle={bundle} entity={entity} mutation={mutation} />;
  if (entity.type === 'visit_stop') {
    // 与餐饮同构：有槽位就把卡片包进 SelectionSlotCard，备选与切换都由它渲染。
    if (slot && !readOnly) {
      return (
        <>
          <SelectionSlotCard
            bundle={bundle}
            slot={slot}
            mutation={mutation}
            canonicalCard={<VisitStopCard stop={entity} sourceMarkers={<EntityEvidenceMarkers entityId={entityId} basis={evidenceBasisForEntity(entity)} />} />}
          />
          {editor}
        </>
      );
    }
    return (
      <>
        <VisitStopCard stop={entity} sourceMarkers={<EntityEvidenceMarkers entityId={entityId} basis={evidenceBasisForEntity(entity)} />} />
        {editor}
      </>
    );
  }
  if (entity.type === 'dining_stop') {
    if (slot && !readOnly) {
      return (
        <>
          <SelectionSlotCard
            bundle={bundle}
            slot={slot}
            mutation={mutation}
            canonicalCard={<DiningStopCard stop={entity} sourceMarkers={<EntityEvidenceMarkers entityId={entityId} basis={evidenceBasisForEntity(entity)} />} />}
          />
          {editor}
        </>
      );
    }
    return (
      <>
        <DiningStopCard stop={entity} sourceMarkers={<EntityEvidenceMarkers entityId={entityId} basis={evidenceBasisForEntity(entity)} />} />
        {editor}
      </>
    );
  }
  if (entity.type === 'lodging_stay') {
    if (slot && projectionRole === 'check_in' && !readOnly) {
      return (
        <>
          <SelectionSlotCard
            bundle={bundle}
            slot={slot}
            mutation={mutation}
            canonicalCard={<LodgingStayCard stay={entity} sourceMarkers={<EntityEvidenceMarkers entityId={entityId} basis={evidenceBasisForEntity(entity)} />} />}
          />
          {editor}
        </>
      );
    }
    return (
      <>
        <LodgingStayCard stay={entity} sourceMarkers={<EntityEvidenceMarkers entityId={entityId} basis={evidenceBasisForEntity(entity)} />} />
        {editor}
      </>
    );
  }
  return <><CustomBlockRow block={entity} />{editor}</>;
}

type DayPlan = PublicDeliveryBundle['workspace']['itinerary']['day_plans'][number];

/**
 * 行程地图区 —— 竖直构图的顶部常驻地图（上地图、下行程，不回到左右分栏）。
 * 始终可见、约占内容三分之一高，可「压缩成条 / 展开」；随天数切换只聚焦当天的地点与路线，
 * 与下方清单双向联动。当前范围内没有可定位的对象时收为一条细提示，不空占版面。
 */
function ItineraryMapRegion({
  bundle,
  activeDay,
  dayByEntity,
  placeOrder,
  linkedEntityId,
  onLinkEntity,
}: {
  bundle: PublicDeliveryBundle;
  activeDay: number | null;
  dayByEntity: Map<string, number>;
  /** 纸面此刻悬停在哪一行；地图把对应的针点亮。 */
  linkedEntityId: string | null;
  onLinkEntity: (entityId: string | null) => void;
  /** 针号由父级用 `lib/waypointOrder` 算一份，地图与纸面同读。这里**不要**自己再算一遍，
   *  否则时间线上的号和地图上的针对不上，读者没法把「1 号针」落到任何一行。 */
  placeOrder: ReadonlyMap<string, WaypointPlacement>;
}) {
  const [compact, setCompact] = React.useState(false);
  const [selectedEntityId, setSelectedEntityId] = React.useState<string | null>(null);
  const { places, routes } = bundle.map_projection.content;

  const mappedPlaces = React.useMemo<BundleMapPlace[]>(() => places
    .filter((place) => ['visit_stop', 'dining_stop', 'lodging_stay'].includes(place.entity_ref.entity_type))
    .map((place) => {
      const order = placeOrder.get(place.entity_ref.entity_id);
      return { ...place, days: order?.days ?? [], sequence: order?.sequence ?? 1 };
    }), [placeOrder, places]);

  // 全程仍是每个地点一枚针；分天按「这天是否用到它」过滤，跨天的地点在每一天各出现一次。
  const visiblePlaces = React.useMemo(
    () => (activeDay == null ? mappedPlaces : mappedPlaces.filter((place) => place.days.includes(activeDay))),
    [mappedPlaces, activeDay]
  );
  // 地图只呈现「目的地」范围：排除跨城的 long_distance 交通腿（飞机 / 高铁），否则出发地端点会把
  // fitBounds 撑到省级、目的地景点挤成一团（用户核查点）。市内短驳（public_transit / flexible）保留。
  const longDistanceLegIds = React.useMemo(
    () => new Set(
      bundle.workspace.itinerary.transport_legs
        .filter((leg) => leg.transport_class === 'long_distance')
        .map((leg) => leg.transport_leg_id)
    ),
    [bundle.workspace.itinerary.transport_legs]
  );
  const visibleRoutes = React.useMemo(() => {
    const local = routes.filter((route) => (
      route.entity_ref.entity_type === 'transport_leg'
      && !longDistanceLegIds.has(route.entity_ref.entity_id)
    ));
    return activeDay == null ? local : local.filter((route) => dayByEntity.get(route.entity_ref.entity_id) === activeDay);
  }, [routes, activeDay, dayByEntity, longDistanceLegIds]);

  const drawable = React.useCallback((
    somePlaces: BundleMapPlace[],
    someRoutes: typeof routes
  ) => ({
    placeCount: somePlaces.filter((place) => place.latitude != null && place.longitude != null).length,
    routeCount: someRoutes.filter((route) => route.route_status === 'ready'
      && route.segments.some((segment) => segment.from_endpoint.latitude != null && segment.from_endpoint.longitude != null
        && segment.to_endpoint.latitude != null && segment.to_endpoint.longitude != null)).length,
  }), []);
  const scope = drawable(visiblePlaces, visibleRoutes);
  /**
   * 「地图在不在」与「当前范围画什么」是**两个**判断，不许合成一个。
   *
   * 整份 Bundle 有东西可画，地图实例就一直活着；当前范围（比如某一天）没东西可画时它只是
   * **看不见**，版面上仍然只留那条细提示。
   *
   * 合成一个判断的代价：没有可定位地点的那一天会把整块早退成细提示，Leaflet 实例被卸掉，
   * 再切回来就是一次**冷启动** —— 重新 init、重新取一屏瓦片（实测 17 个请求）、fitBounds
   * 重来、「底图加载中…」再闪一次。
   */
  const wholeTrip = drawable(mappedPlaces, routes.filter((route) => (
    route.entity_ref.entity_type === 'transport_leg' && !longDistanceLegIds.has(route.entity_ref.entity_id)
  )));
  const scopeCanDraw = scope.placeCount > 0 || scope.routeCount > 0;
  const tripCanDraw = wholeTrip.placeCount > 0 || wholeTrip.routeCount > 0;
  const scopeLabel = activeDay == null ? '全程' : `第 ${activeDay} 天`;

  React.useEffect(() => { setSelectedEntityId(null); }, [activeDay]);

  // 整趟都没有一个可定位的对象：地图从来没挂过，也就没有实例要保。
  if (!tripCanDraw) {
    return (
      <div data-testid="itinerary-map-region" className="flex flex-none items-center gap-2 border-b border-stroke bg-surface/40 px-4 py-2.5 text-xs text-ink-secondary sm:px-6">
        <MapIcon size={15} className="shrink-0 text-ink-muted" aria-hidden />
        <span className="min-w-0 break-words">{scopeLabel}暂无可在地图上定位的地点或路线。</span>
      </div>
    );
  }

  return (
    <div
      data-testid="itinerary-map-region"
      className={cn(
        // 档位切换是**瞬时**的，**不要**给它加 `transition-[height]`：高度是布局属性，
        // 布局过渡只放行侧栏轨道那一条 width；而地图区在 144↔192px 之间两个方向都在动它，
        // 每一帧都让底图重排 —— 底图本来也不能平滑缩放。
        'relative flex-none overflow-hidden border-b border-stroke bg-surface',
        scopeCanDraw
          ? (compact ? 'h-24' : 'h-60 sm:h-72')
          : 'flex items-center gap-2 bg-surface/40 px-4 py-2.5 text-xs text-ink-secondary sm:px-6'
      )}
    >
      {!scopeCanDraw && (
        <>
          <MapIcon size={15} className="shrink-0 text-ink-muted" aria-hidden />
          <span className="min-w-0 break-words">{scopeLabel}暂无可在地图上定位的地点或路线。</span>
        </>
      )}
      {/**
        * 地图**始终留在 DOM 里**。当前范围没东西可画时用 `invisible` 藏它，而不是把高度收成
        * 0 或把它卸掉：`visibility: hidden` 的元素**保留自己的盒子**，Leaflet 量到的容器尺寸
        * 不变，于是它不会把整屏瓦片 prune 掉；高度收成 0 会（回来时又是一次取瓦片）。
        * 外层的 `overflow-hidden` 负责让这 240px 不影响细提示那一条的版面。
        */}
      <div className={scopeCanDraw ? 'h-full w-full' : 'invisible absolute left-0 top-0 h-60 w-full sm:h-72'}>
        <BundleMapLeafletLazy
          places={visiblePlaces}
          routes={visibleRoutes}
          selectedEntityId={selectedEntityId}
          onSelectEntity={setSelectedEntityId}
          linkedEntityId={linkedEntityId}
          onLinkEntity={onLinkEntity}
        />
      </div>
      {scopeCanDraw && (
        <>
          <div className="pointer-events-none absolute bottom-3 left-3 z-[1000] inline-flex items-center gap-1.5 rounded-label bg-panel/90 px-2.5 py-1 text-[11px] font-medium text-ink-secondary shadow-sm backdrop-blur">
            <MapIcon size={12} aria-hidden />
            {scope.placeCount} 个地点{scope.routeCount ? ` · ${scope.routeCount} 条路线` : ''}
          </div>
          <button
            type="button"
            data-testid="itinerary-map-compact-toggle"
            onClick={() => setCompact((value) => !value)}
            aria-label={compact ? '展开地图' : '压缩地图'}
            /* 视觉尺寸 61×32，触摸上差 12px，命中区由 `index.css` 那条按元素选的规则补。
               它自己是 `absolute`，所以那条规则的 `position: relative`
               必须是 `:where()` 权重 0 —— 一旦它换掉这枚钮的定位，钮会从地图右上角掉下来。 */
            className="absolute right-3 top-3 z-[1000] inline-flex min-h-8 items-center gap-1 rounded-card border border-stroke bg-panel/90 px-2.5 text-[11px] font-semibold text-ink-secondary shadow-sm backdrop-blur transition-colors hover:text-ink"
          >
            {compact ? '展开' : '压缩'}
            {compact ? <ChevronDown size={13} aria-hidden /> : <ChevronUp size={13} aria-hidden />}
          </button>
        </>
      )}
    </div>
  );
}

/** 天数切换（总览 / 第 N 天）：滑动下划线标记当前天。 */
function DayNav({
  days,
  activeDay,
  onSelect,
}: {
  days: DayPlan[];
  activeDay: number | null;
  onSelect: (day: number | null) => void;
}) {
  return (
    <nav
      data-testid="itinerary-day-nav"
      aria-label="按天查看行程"
      className="flex flex-none items-center gap-1 overflow-x-auto border-b border-stroke bg-panel px-3 py-2 sm:px-4"
    >
      <DayTab active={activeDay == null} onClick={() => onSelect(null)}>总览</DayTab>
      {days.map((day) => (
        <DayTab key={day.day_id} active={activeDay === day.day} onClick={() => onSelect(day.day)}>
          第 {day.day} 天
        </DayTab>
      ))}
    </nav>
  );
}

function DayTab({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
       /* 日页签是手机上翻天的主入口，而视觉尺寸只有 **28px 高**（48–65 × 28）。视觉尺寸不动
          —— 44px 契约走透明伪元素（`index.css` 按元素选），桌面上一切照旧。这一枚
          **已经是 `relative`**（下面那条 `layoutId` 下划线要它当参照），正是那条规则的
          `:where()` 权重 0 要保护的形状：热区规则不许换掉它的定位。 */
      className={cn(
        'relative shrink-0 rounded-card px-3 py-1.5 text-xs font-semibold transition-colors',
        active ? 'text-ink' : 'text-ink-secondary hover:text-ink'
      )}
    >
      {children}
      {active && (
        <m.span
          layoutId="itinerary-day-underline"
          className="absolute inset-x-2 -bottom-0.5 h-0.5 rounded-full bg-accent"
          transition={{ duration: duration.base, ease: easing.standard }}
        />
      )}
    </button>
  );
}

/**
 * 交互行程印的是报告投影渲染好的那批行（`entities/delivery_presentation.py`），
 * 所以它和正式报告用同一条「报告是不是当前的」判据。报告落后于行程时这里必须说
 * 「正在同步」：旧的时间轴不是当前行程，把它显示成当前结果就是用省略撒谎。
 */
function InteractiveItinerary({ bundle, readOnly = false }: { bundle: PublicDeliveryBundle; readOnly?: boolean }) {
  const reportDocument = selectCurrentReportDocument(bundle);
  if (!reportDocument) {
    const failed = bundle.report_projection.status === 'failed';
    return (
      <div data-testid="interactive-itinerary" className="mx-auto flex h-full w-full max-w-2xl items-center px-6 py-12">
        <div
          role={failed ? 'alert' : 'status'}
          data-testid={failed ? 'itinerary-unavailable' : 'itinerary-report-syncing'}
          className="w-full border-y border-stroke py-8 text-center"
        >
          <CalendarDays className="mx-auto text-ink-muted" size={24} />
          <h2 className="mt-3 text-base font-semibold text-ink">
            {failed ? '暂时无法加载行程' : '行程正在同步到最新结果'}
          </h2>
          {failed && (
            <p className="mx-auto mt-2 max-w-[52ch] break-words text-sm leading-6 text-ink-secondary">
              请稍后重试。
            </p>
          )}
        </div>
      </div>
    );
  }
  return <ReadyInteractiveItinerary bundle={bundle} readOnly={readOnly} />;
}

function ReadyInteractiveItinerary({
  bundle,
  readOnly = false,
}: {
  bundle: PublicDeliveryBundle;
  readOnly?: boolean;
}) {
  const itinerary = bundle.workspace.itinerary;
  const entities = React.useMemo(() => buildEntityIndex(bundle), [bundle]);
  const mutation = useDeliveryBundleMutation(bundle);
  const [activeDay, setActiveDay] = React.useState<number | null>(null);
  /**
   * 纸面 ↔ 地图互相点亮。状态放在这里，因为这是**同时渲染两者的那一级**——
   * 地图在 `ItineraryMapRegion`、纸面在下面的 `DayTimeline`，它们是兄弟。
   * 与 `selectedEntityId`（地图自己的选中态，会把视野飞过去）是两回事：点亮不动视野。
   */
  const [linkedEntityId, setLinkedEntityId] = React.useState<string | null>(null);
  const [openProposalId, setOpenProposalId] = React.useState<string | null>(null);
  const itineraryScrollRef = React.useRef<HTMLDivElement>(null);
  const summaryCardRefs = React.useRef(new Map<string, HTMLButtonElement>());
  const overviewScrollTop = React.useRef(0);
  const returnFocusDayId = React.useRef<string | null>(null);
  const proposalTriggers = React.useRef(new Map<string, HTMLButtonElement>());
  const activeProposals = React.useMemo(
    () => readOnly ? [] : bundle.workspace.weather_adjustments.filter((item) => item.status === 'pending'),
    [bundle.workspace.weather_adjustments, readOnly]
  );
  React.useEffect(() => {
    if (openProposalId && !activeProposals.some((item) => item.proposal_id === openProposalId)) {
      setOpenProposalId(null);
    }
  }, [activeProposals, openProposalId]);
  const closeProposal = React.useCallback((proposalId: string) => {
    setOpenProposalId(null);
    window.requestAnimationFrame(() => proposalTriggers.current.get(proposalId)?.focus());
  }, []);
  const slotsByEntity = React.useMemo(
    () => new Map(bundle.workspace.selection_slots.map((slot) => [slot.target_entity_id, slot])),
    [bundle]
  );
  const consumerNotices = React.useMemo(
    () => buildConsumerNotices(bundle, { status: 'ready', message: null }),
    [bundle]
  );
  const daySummaries = React.useMemo(
    () => selectDaySummaries(bundle, consumerNotices),
    [bundle, consumerNotices]
  );
  const daySummaryById = React.useMemo(
    () => new Map(daySummaries.map((summary) => [summary.dayId, summary])),
    [daySummaries]
  );
  const timelinesByDayId = React.useMemo(
    () => new Map(itinerary.day_plans.map((day) => [day.day_id, selectDayTimeline(bundle, day.day_id)])),
    [bundle, itinerary.day_plans]
  );
  const dayByEntity = React.useMemo(() => {
    const map = new Map<string, number>();
    for (const day of itinerary.day_plans) {
      for (const entry of day.timeline) map.set(entry.entity_id, day.day);
    }
    return map;
  }, [itinerary.day_plans]);
  // 一份针号，地图与纸面同读。这一屏两样都在，所以它算在两者的共同父级上。
  // 传进去的是地图**真的会插针**的那些地点，和 `ItineraryMapRegion` 里的过滤同一条：
  // 没有针的停留点不发号，否则纸面上的号指向一枚不存在的针。
  const waypointOrder = React.useMemo(
    () => buildWaypointOrder(
      itinerary,
      pinnedMapPlaceIds(bundle.map_projection.content.places),
    ),
    [itinerary, bundle.map_projection.content.places]
  );

  React.useEffect(() => {
    setActiveDay(null);
    returnFocusDayId.current = null;
    overviewScrollTop.current = 0;
  }, [bundle.manifest.run_id]);

  // 换日之后指针大概率已经不在那一行上了，留着点亮就是一枚亮着却没人指的针。
  React.useEffect(() => { setLinkedEntityId(null); }, [activeDay]);

  const registerDayCard = React.useCallback((dayId: string, node: HTMLButtonElement | null) => {
    if (node) summaryCardRefs.current.set(dayId, node);
    else summaryCardRefs.current.delete(dayId);
  }, []);
  const restoreOverview = React.useCallback(() => {
    setActiveDay(null);
  }, []);
  const openDay = React.useCallback((summary: DaySummaryVM) => {
    overviewScrollTop.current = itineraryScrollRef.current?.scrollTop ?? 0;
    returnFocusDayId.current = summary.dayId;
    setActiveDay(summary.day);
    window.requestAnimationFrame(() => itineraryScrollRef.current?.scrollTo({ top: 0 }));
  }, []);
  const selectDay = React.useCallback((day: number | null) => {
    if (day == null) {
      restoreOverview();
      return;
    }
    const summary = daySummaries.find((candidate) => candidate.day === day);
    if (summary) openDay(summary);
  }, [daySummaries, openDay, restoreOverview]);
  React.useEffect(() => {
    if (activeDay != null || !returnFocusDayId.current) return;
    const dayId = returnFocusDayId.current;
    window.requestAnimationFrame(() => {
      itineraryScrollRef.current?.scrollTo({ top: overviewScrollTop.current });
      summaryCardRefs.current.get(dayId)?.focus();
      returnFocusDayId.current = null;
    });
  }, [activeDay]);

  const visibleDays = activeDay == null ? itinerary.day_plans : itinerary.day_plans.filter((day) => day.day === activeDay);

  const renderDay = (day: DayPlan) => {
    const dayProposals = day.date ? activeProposals.filter((proposal) => proposal.date === day.date) : [];
    const summary = daySummaryById.get(day.day_id);
    const timeline = timelinesByDayId.get(day.day_id) ?? [];
    const timelineEntries = new Map(day.timeline.map((entry) => [entry.entry_id, entry]));
    return (
      <section key={day.day_id} className="py-6" aria-labelledby={`delivery-day-${day.day_id}`}>
        <DayHeader
          dayId={day.day_id}
          day={day.day}
          dateLabel={summary?.dateLabel ?? null}
          weekdayLabel={summary?.weekdayLabel ?? null}
          destinationLabel={summary?.destinationLabel ?? null}
          theme={day.theme}
          weather={summary?.weather ?? null}
        />
        {dayProposals.map((proposal) => (
          <React.Fragment key={proposal.proposal_id}>
            {openProposalId !== proposal.proposal_id && (
              <button
                ref={(node) => {
                  if (node) proposalTriggers.current.set(proposal.proposal_id, node);
                  else proposalTriggers.current.delete(proposal.proposal_id);
                }}
                type="button"
                data-testid={`weather-proposal-trigger-${proposal.proposal_id}`}
                aria-expanded={false}
                onClick={() => setOpenProposalId(proposal.proposal_id)}
                className="mt-3 flex w-full min-w-0 items-center justify-between gap-3 rounded-card border border-accent/20 bg-accent-soft/35 px-3 py-2.5 text-left transition-colors hover:border-accent/35 hover:bg-accent-soft/55"
              >
                <span className="min-w-0">
                  <span className="block text-[11px] font-semibold text-accent">天气调整建议</span>
                  <span className="mt-0.5 block break-words text-xs leading-5 text-ink-secondary">{proposal.summary}</span>
                </span>
                <ChevronDown size={16} className="shrink-0 text-accent" aria-hidden />
              </button>
            )}
            {openProposalId === proposal.proposal_id && (
              <WeatherAdjustmentPanel
                bundle={bundle}
                proposal={proposal}
                mutation={mutation}
                onClose={() => closeProposal(proposal.proposal_id)}
              />
            )}
          </React.Fragment>
        ))}
        <DayTimeline
          nodes={timeline}
          waypointOrder={waypointOrder}
          linkedEntityId={linkedEntityId}
          onLinkEntity={setLinkedEntityId}
          staggerEntrance
          renderDetails={(node) => {
            const entry = timelineEntries.get(node.key);
            const entity = entry ? entities.get(entry.entity_id) : null;
            if (!entry || !entity) return null;
            const currentEntityId = entityId(entity);
            return (
              <TimelineEntityDetails
                bundle={bundle}
                entity={entity}
                entityId={currentEntityId}
                projectionRole={entry.projection_role}
                slot={slotsByEntity.get(currentEntityId) ?? null}
                mutation={mutation}
                readOnly={readOnly}
              />
            );
          }}
        />
        {!readOnly && <AddCustomBlockPanel day={day} mutation={mutation} />}
      </section>
    );
  };

  return (
    <div data-testid="interactive-itinerary" className="flex h-full min-h-0 flex-col">
      <ItineraryMapRegion
        bundle={bundle}
        activeDay={activeDay}
        dayByEntity={dayByEntity}
        placeOrder={waypointOrder}
        linkedEntityId={linkedEntityId}
        onLinkEntity={setLinkedEntityId}
      />
      <DayNav days={itinerary.day_plans} activeDay={activeDay} onSelect={selectDay} />
      <div ref={itineraryScrollRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div className="mx-auto w-full max-w-4xl px-4 py-5 sm:px-6 sm:py-7">
          {activeDay == null && (
            <TripOverview
              itinerary={itinerary}
              summaries={daySummaries}
              costCoverageStatement={bundle.report_projection.document?.cost_coverage_statement ?? null}
              importantNotes={bundle.report_projection.document?.important_notes ?? []}
              onOpenDay={openDay}
              registerDayCard={registerDayCard}
            />
          )}

          {activeDay != null && (
            <>
              <button
                type="button"
                data-testid="day-overview-back"
                onClick={restoreOverview}
                className="mb-2 inline-flex items-center gap-1.5 rounded-card px-2 text-sm font-semibold text-accent transition-colors hover:bg-accent-soft hover:text-[var(--color-accent-hover)]"
              >
                <ArrowLeft size={16} aria-hidden />
                返回总览
              </button>
              <div>{visibleDays.map((day) => renderDay(day))}</div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function FullReport({ bundle, readOnly = false }: { bundle: PublicDeliveryBundle; readOnly?: boolean }) {
  const report = bundle.report_projection;
  if (selectCurrentReportDocument(bundle) === null) {
    const failed = report.status === 'failed';
    return (
      <div data-testid="full-report" className="mx-auto flex min-h-full w-full max-w-2xl items-center px-6 py-12">
        <div role={failed ? 'alert' : 'status'} className="w-full border-y border-stroke py-8 text-center">
          <FileText className={cn('mx-auto', failed ? 'text-error' : 'text-ink-muted')} size={24} />
          <h2 className="mt-3 text-base font-semibold text-ink">{failed ? '旅行结果需要重新加载' : '报告正在同步到最新行程'}</h2>
          <p className="mx-auto mt-2 max-w-[52ch] break-words text-sm leading-6 text-ink-secondary">
            {failed ? '交互行程仍可使用；请稍后重新加载。' : '交互行程已经可用。'}
          </p>
        </div>
      </div>
    );
  }

  return <FullReportView bundle={bundle} allowPdf={!readOnly} renderCitations={(ids) => <CitationIdMarkers citationIds={ids} />} />;
}

export const DeliveryWorkspace: React.FC<DeliveryWorkspaceProps> = ({ bundle, variant = 'docked' }) => {
  const { state, dispatch } = useApp();
  const [sourceDetail, setSourceDetail] = React.useState<BundleSourceDetail | null>(null);
  const undo = useDeliveryBundleUndo(bundle);
  const sourceIndexes = React.useMemo(() => {
    const byEntity = new Map<string, PublicCitationProjection[]>();
    const byId = new Map<string, PublicCitationProjection>();
    bundle.source_index.content.citations.forEach((citation) => {
      byId.set(citation.citation_id, citation);
      const current = byEntity.get(citation.entity_ref.entity_id) ?? [];
      current.push(citation);
      byEntity.set(citation.entity_ref.entity_id, current);
    });
    return { byEntity, byId };
  }, [bundle.source_index.content.citations]);
  React.useEffect(() => {
    setSourceDetail(null);
  }, [bundle.manifest.bundle_id]);
  const closeSourceDetail = React.useCallback(() => {
    const returnFocus = sourceDetail?.returnFocus;
    setSourceDetail(null);
    if (returnFocus) window.requestAnimationFrame(() => returnFocus.focus());
  }, [sourceDetail]);
  const sourceContext = React.useMemo<SourceContextValue>(() => ({
    ...sourceIndexes,
    onOpenDetail: setSourceDetail,
  }), [sourceIndexes]);
  const close = () => dispatch({
    type: variant === 'sheet' ? 'SET_MOBILE_CANVAS_OPEN' : 'SET_CANVAS_OPEN',
    payload: false,
  });

  return (
    <SourceContext.Provider value={sourceContext}>
    {/* 正式结果面同样是**图纸**：半径 0 + neatline，见 TripWorkspaceShell 同一处注释。 */}
    <section className="relative flex h-full min-h-0 flex-col overflow-hidden bg-panel shadow-sm" aria-label="旅行正式结果">
      <Neatline />
      {sourceDetail && <BundleSourceDetailView detail={sourceDetail} onClose={closeSourceDetail} />}
      <div className={cn('flex h-full min-h-0 flex-col', sourceDetail && 'hidden')}>
      <div className="flex flex-none items-center justify-between gap-3 border-b border-stroke px-3 py-2 sm:px-4">
        <div role="tablist" aria-label="结果形态" className="flex min-w-0 items-center rounded-card bg-surface p-1">
          <button
            type="button"
            role="tab"
            aria-selected={state.deliverableView === 'interactive_itinerary'}
            data-testid="itinerary-view-tab"
            onClick={() => dispatch({ type: 'SET_DELIVERABLE_VIEW', payload: 'interactive_itinerary' })}
            className={cn('rounded-label px-3 py-1.5 text-xs font-semibold transition-colors', state.deliverableView === 'interactive_itinerary' ? 'bg-panel text-ink shadow-sm' : 'text-ink-secondary hover:text-ink')}
          >
            交互行程
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={state.deliverableView === 'full_report'}
            data-testid="report-view-tab"
            onClick={() => dispatch({ type: 'SET_DELIVERABLE_VIEW', payload: 'full_report' })}
            className={cn('rounded-label px-3 py-1.5 text-xs font-semibold transition-colors', state.deliverableView === 'full_report' ? 'bg-panel text-ink shadow-sm' : 'text-ink-secondary hover:text-ink')}
          >
            完整报告
          </button>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {undo.available && (
            <button
              type="button"
              data-testid="delivery-undo"
              disabled={undo.status === 'saving'}
              onClick={() => void undo.undo()}
              aria-label={undo.status === 'saving' ? '正在撤销行程调整' : undo.label}
              className="inline-flex min-w-0 items-center gap-1.5 rounded-card px-2.5 text-xs font-semibold text-ink-secondary transition-colors hover:bg-surface hover:text-ink disabled:cursor-wait disabled:opacity-55"
            >
              <Undo2 size={15} aria-hidden />
              <span className="hidden max-w-32 truncate sm:inline">
                {undo.status === 'saving' ? '正在撤销' : undo.label}
              </span>
            </button>
          )}
          {/* `h-7 w-7`：44px 命中区必须落在盒子内 —— 没有盒子时宽高就是那枚 17px 字形，
              中心离面板右缘只有 8.5px，命中区会向右越界被面板裁掉。28px 见方与旁边那两枚
              页签同高，所以天头高度不变（32 会把 `py-2` 那一行顶高 4px）。 */}
          <button type="button" onClick={close} aria-label="关闭正式结果" className="flex h-7 w-7 shrink-0 items-center justify-center rounded-card text-ink-muted transition-colors hover:bg-surface hover:text-ink">
            <X size={17} />
          </button>
        </div>
      </div>
      {undo.message && (
        <div
          role="status"
          className={cn(
            'flex flex-none items-center justify-between gap-3 border-b border-stroke bg-surface/45 px-3 py-2 text-xs sm:px-4',
            undo.status === 'failed' ? 'text-error' : 'text-ink-secondary'
          )}
        >
          <span className="min-w-0 break-words">{undo.message}</span>
          {undo.status === 'failed' && !undo.available && (
            <button type="button" onClick={() => void undo.retryLoad()} className="shrink-0 font-semibold text-accent hover:text-[var(--color-accent-hover)]">
              重试
            </button>
          )}
        </div>
      )}
      <div role="tabpanel" className={cn('min-h-0 flex-1', state.deliverableView === 'interactive_itinerary' ? 'overflow-hidden' : 'overflow-y-auto overscroll-contain')}>
        {state.deliverableView === 'interactive_itinerary'
          ? <InteractiveItinerary bundle={bundle} />
          : <FullReport bundle={bundle} />}
      </div>
      </div>
    </section>
    </SourceContext.Provider>
  );
};
