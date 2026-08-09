export const DELIVERY_BUNDLE_CONTRACT_VERSION = 'journeypilot.delivery_bundle.v7' as const;
export const TRIP_WORKSPACE_CONTRACT_VERSION = 'journeypilot.trip_workspace.v7' as const;
export const FACT_SNAPSHOT_CONTRACT_VERSION = 'journeypilot.fact_store_snapshot.v4' as const;
export const WEATHER_SNAPSHOT_CONTRACT_VERSION = 'journeypilot.weather_context_snapshot.v2' as const;
export const RESEARCH_PACKET_CONTRACT_VERSION = 'journeypilot.research_packet.v4' as const;
export const RECOMMENDATION_CATALOG_CONTRACT_VERSION = 'journeypilot.recommendation_catalog.v5' as const;

export type EntityType =
  | 'visit_stop'
  | 'dining_stop'
  | 'lodging_stay'
  | 'transport_leg'
  | 'transport_segment'
  | 'custom_block'
  | 'weather_day';

export interface EntityRef {
  entity_type: EntityType;
  entity_id: string;
}

export interface EntityLineage {
  /**
   * `authored_entity` marks an entry the planner wrote; it has no candidate, fact, or source.
   * `reference_entity` is a real service a supplier returned for a date outside its
   * booking window: it names its packet and candidate but holds no fact or source ids,
   * because none of its claims were confirmed for the traveller's date.
   */
  lineage_kind: 'candidate_entity' | 'authored_entity' | 'reference_entity';
  research_packet_id: string | null;
  candidate_id: string | null;
  selection_slot_id: string | null;
  fact_assertion_ids: string[];
  source_record_ids: string[];
  planning_decision_ids: string[];
  weather_impact_ids: string[];
  personalization_influence_ids: string[];
}

export interface UserInputAnchor {
  anchor_id: string;
  field_path: string;
  value: unknown;
  input_kind: 'controlled_identity' | 'hard_constraint' | 'preference' | 'fixed_transport' | 'planning_authorization';
  constraint_id: string | null;
}

export type TransportMode =
  | 'flight'
  | 'high_speed_rail'
  | 'train'
  | 'coach'
  | 'ferry'
  | 'metro'
  | 'bus'
  | 'tram'
  | 'taxi'
  | 'ride_hailing'
  | 'drive'
  | 'bike'
  | 'walk'
  | 'other';

export interface TransportEndpoint {
  name: string;
  place_id: string | null;
  station_code: string | null;
  latitude: number | null;
  longitude: number | null;
}

export interface TransportSegment {
  segment_id: string;
  mode: TransportMode;
  from_endpoint: TransportEndpoint;
  to_endpoint: TransportEndpoint;
  departure_at: string | null;
  arrival_at: string | null;
  duration_minutes: number | null;
  distance_meters: number | null;
  operator_name: string | null;
  service_number: string | null;
  line_name: string | null;
  cost_cny: number | null;
}

export interface TransportLeg {
  type: 'transport_leg';
  transport_leg_id: string;
  transport_class: 'long_distance' | 'public_transit' | 'flexible';
  selected_mode: TransportMode;
  from_endpoint: TransportEndpoint;
  to_endpoint: TransportEndpoint;
  departure_at: string | null;
  arrival_at: string | null;
  duration_minutes: number | null;
  distance_meters: number | null;
  total_cost_cny: number | null;
  transfer_count: number;
  segments: TransportSegment[];
  booking_status: 'not_required' | 'recommended' | 'required' | 'booked' | 'unknown';
  route_status: 'pending' | 'ready' | 'unavailable';
  mode_preference: { locked_mode: TransportMode | null; excluded_modes: TransportMode[] };
  lineage: EntityLineage;
}

interface ScheduledStopBase {
  item_id: string;
  day_id: string;
  place_id: string;
  name: string;
  address: string | null;
  latitude: number | null;
  longitude: number | null;
  planned_start: string | null;
  planned_end: string | null;
  duration_minutes: number;
  estimated_cost_cny: number | null;
  selection_reason: string;
  lineage: EntityLineage;
}

export interface VisitStop extends ScheduledStopBase {
  type: 'visit_stop';
  visit_type: 'attraction' | 'experience' | 'culture' | 'shopping' | 'nature' | 'other';
  opening_window: string | null;
  reservation_required: boolean | null;
  visit_highlights: string[];
}

export interface DiningStop extends ScheduledStopBase {
  type: 'dining_stop';
  meal_type: 'breakfast' | 'lunch' | 'dinner' | 'snack' | 'other';
  cuisine_types: string[];
  average_spend_cny: number | null;
  recommended_dishes: string[];
  reservation_required: boolean | null;
  opening_window: string | null;
  dining_reminders: string[];
}

export type LodgingPriceKind = 'reference_estimate' | 'live_quote';
export type LodgingAvailabilityStatus = 'confirmed' | 'needs_confirmation' | 'unavailable';

export interface LodgingStay {
  type: 'lodging_stay';
  stay_id: string;
  place_id: string;
  name: string;
  check_in_date: string;
  check_out_date: string;
  check_in_time: string | null;
  check_out_time: string | null;
  nights: number;
  room_type: string | null;
  nightly_price_cny: number | null;
  total_price_cny: number | null;
  price_kind: LodgingPriceKind;
  availability_status: LodgingAvailabilityStatus;
  address: string;
  selection_reason: string;
  lineage: EntityLineage;
}

export interface CustomBlock {
  type: 'custom_block';
  item_id: string;
  day_id: string;
  title: string;
  note: string | null;
  planned_start: string | null;
  planned_end: string | null;
  duration_minutes: number | null;
}

export interface DayPlanV2 {
  day_id: string;
  day: number;
  date: string | null;
  destination_id: string | null;
  theme: string;
  timeline: Array<{
    entry_id: string;
    entity_type: EntityType;
    entity_id: string;
    projection_role: 'full' | 'departure' | 'arrival' | 'check_in' | 'check_out';
  }>;
  estimated_cost_cny: number | null;
}

export interface StructuredItineraryV2 {
  itinerary_id: string;
  title: string;
  destination_ids: string[];
  duration_days: number;
  day_plans: DayPlanV2[];
  visit_stops: VisitStop[];
  dining_stops: DiningStop[];
  lodging_stays: LodgingStay[];
  transport_legs: TransportLeg[];
  custom_blocks: CustomBlock[];
  cost_summary: CostCoverageSummary;
  highlights: string[];
  important_notes: string[];
}

/**
 * How much backing an entry has, stated to the traveller in one word.
 *
 * `cited_source` 由已录取的调研候选写入，可以展开来源与事实；
 * `public_reference` 由规划模型依据公开资料写入，本次没有附来源链接；
 * `reference_service` 是供应商返回的真实班次，但预售窗口没覆盖出行日期，
 * 因此车次与时刻是真的、「那天还这样」未被确认。
 */
export type EvidenceBasis = 'cited_source' | 'public_reference' | 'reference_service';

/**
 * Consumer-safe itinerary records.  The persisted counterparts above retain
 * lineage for replay and audit — packet / candidate / fact / decision ids stay
 * internal and never cross this boundary.  Two derived product fields do cross
 * it.  `evidence_basis` is not lineage, it is the single answer to
 * "这条是查来的还是写出来的", so an entry without sources reads as a stated basis
 * instead of an unexplained absence; custom blocks are the traveller's own
 * arrangements and carry no basis at all.  `is_micro_transport` is the other —
 * see below.
 *
 * These are written out rather than derived with `Omit<…, 'lineage'>`.  Deriving the
 * type only describes the payload if the server really produces it field by field;
 * reaching the public payload by recursively deleting keys whose *names* match a
 * blacklist leaves the runtime payload missing fields the `Omit<>` type still
 * promises (and possibly carrying ones it denies), with type-check vouching for the
 * blacklist instead of checking it.  Each field is named on both sides —
 * `services/public_delivery.py` constructs exactly this list — so a rename or an
 * addition has to appear in both diffs or one of them fails.
 */
export interface PublicVisitStop {
  type: 'visit_stop';
  item_id: string;
  day_id: string;
  place_id: string;
  name: string;
  address: string | null;
  latitude: number | null;
  longitude: number | null;
  planned_start: string | null;
  planned_end: string | null;
  duration_minutes: number;
  estimated_cost_cny: number | null;
  selection_reason: string;
  visit_type: VisitStop['visit_type'];
  opening_window: string | null;
  reservation_required: boolean | null;
  visit_highlights: string[];
  evidence_basis: EvidenceBasis;
}

export interface PublicDiningStop {
  type: 'dining_stop';
  item_id: string;
  day_id: string;
  place_id: string;
  name: string;
  address: string | null;
  latitude: number | null;
  longitude: number | null;
  planned_start: string | null;
  planned_end: string | null;
  duration_minutes: number;
  estimated_cost_cny: number | null;
  selection_reason: string;
  meal_type: DiningStop['meal_type'];
  cuisine_types: string[];
  average_spend_cny: number | null;
  recommended_dishes: string[];
  reservation_required: boolean | null;
  opening_window: string | null;
  dining_reminders: string[];
  evidence_basis: EvidenceBasis;
}

export interface PublicLodgingStay {
  type: 'lodging_stay';
  stay_id: string;
  place_id: string;
  name: string;
  check_in_date: string;
  check_out_date: string;
  check_in_time: string | null;
  check_out_time: string | null;
  nights: number;
  room_type: string | null;
  nightly_price_cny: number | null;
  total_price_cny: number | null;
  price_kind: LodgingPriceKind;
  availability_status: LodgingAvailabilityStatus;
  address: string;
  selection_reason: string;
  evidence_basis: EvidenceBasis;
}

export interface PublicTransportLeg {
  type: 'transport_leg';
  transport_leg_id: string;
  transport_class: TransportLeg['transport_class'];
  selected_mode: TransportMode;
  from_endpoint: TransportEndpoint;
  to_endpoint: TransportEndpoint;
  departure_at: string | null;
  arrival_at: string | null;
  duration_minutes: number | null;
  distance_meters: number | null;
  total_cost_cny: number | null;
  transfer_count: number;
  segments: TransportSegment[];
  booking_status: TransportLeg['booking_status'];
  route_status: TransportLeg['route_status'];
  mode_preference: { locked_mode: TransportMode | null; excluded_modes: TransportMode[] };
  evidence_basis: EvidenceBasis;
  /**
   * 这条腿是不是短驳——门口几百米的步行，只把两个地点连起来，因此卡片收成一行、
   * 界面上也不陈述依据。地铁腿永不折叠成短驳：它的线路与票价是有依据的断言。
   *
   * 判据是对 Bundle 字段的领域判断，**唯一定义在后端**
   * （`entities/evidence_basis.py::is_micro_transport_leg`），投影时把结论打在这里。
   * 前端**不许照抄一遍阈值**：第二份判据一旦与后端漂移，同一条腿在工作台、报告与 PDF 上
   * 就会得到不同结论。
   */
  is_micro_transport: boolean;
}

/** A traveller's own arrangement: it has no lineage, so nothing to strip. */
export interface PublicCustomBlock {
  type: 'custom_block';
  item_id: string;
  day_id: string;
  title: string;
  note: string | null;
  planned_start: string | null;
  planned_end: string | null;
  duration_minutes: number | null;
}

export interface PublicStructuredItineraryV2 {
  itinerary_id: string;
  title: string;
  destination_ids: string[];
  duration_days: number;
  day_plans: DayPlanV2[];
  visit_stops: PublicVisitStop[];
  dining_stops: PublicDiningStop[];
  lodging_stays: PublicLodgingStay[];
  transport_legs: PublicTransportLeg[];
  custom_blocks: PublicCustomBlock[];
  cost_summary: CostCoverageSummary;
  highlights: string[];
  important_notes: string[];
}

export interface SelectionOption {
  option_id: string;
  rank: number;
  selected: boolean;
  recommended: boolean;
  selection_reasons: string[];
  tradeoff: string | null;
  comparison_facts: string[];
  availability_status: 'confirmed' | 'needs_confirmation';
}

/** 一个槽位能提供备选的四个域：住宿、餐饮、景点、交通。 */
export type SelectionSlotType = 'lodging' | 'dining' | 'visit' | 'transport';

export interface SelectionSlot {
  selection_slot_id: string;
  slot_type: SelectionSlotType;
  target_entity_id: string;
  context: Record<string, unknown>;
  options: SelectionOption[];
  status: 'researching' | 'ready' | 'refreshing' | 'needs_user_decision';
}

export interface PersonalizationInfluence {
  influence_id: string;
  target_ref: EntityRef | { selection_slot_id: string };
  constraint_id: string;
  effect: 'candidate_filter' | 'option_ranking' | 'selection_reason';
  source_kind: 'current_request' | 'saved_preference' | 'trip_context';
  display_text: string;
}

export interface SourceRecord {
  source_record_id: string;
  source_kind: 'external_web' | 'external_tool' | 'rag_chunk';
  title: string;
  provider_name: string;
  canonical_url: string | null;
  public_excerpt: string;
  published_at: string | null;
  retrieved_at: string;
  observed_at: string | null;
  effective_from: string | null;
  effective_to: string | null;
  provider_valid_until: string | null;
  content_hash: string;
  snapshot: Record<string, unknown>;
  lifecycle_status: 'active' | 'superseded' | 'withdrawn' | 'rejected';
  tool_audit_id: string | null;
  cache_provenance: {
    origin: 'live' | 'provider_snapshot_cache';
    provider_name: string;
    tool_name: string;
    cache_key_digest: string;
    content_hash: string;
    observed_at: string;
    retrieved_at: string;
    provider_valid_until: string | null;
    cache_valid_until: string;
    provider_contract_version: string;
    payload_schema_version: string;
  } | null;
}

export interface FactAssertion {
  fact_assertion_id: string;
  entity_ref: EntityRef;
  field_path: string;
  asserted_value: unknown;
  unit: string | null;
  currency: string | null;
  criticality: 'execution_critical' | 'decision_critical' | 'auxiliary';
  status: 'verified' | 'refreshing' | 'stale' | 'conflict' | 'missing' | 'superseded';
  observed_at: string | null;
  effective_from: string | null;
  effective_to: string | null;
  expires_at: string | null;
  source_links: Array<{
    source_record_id: string;
    relation: 'supports' | 'qualifies' | 'contradicts';
    source_locator: string;
  }>;
  supersedes_assertion_ids: string[];
}

export interface FieldProvenance {
  origin: 'external_fact' | 'planning_decision' | 'deterministic_computation' | 'user_input';
  entity_ref: EntityRef;
  field_path: string;
  reference_ids: string[];
}

export interface FactStoreSnapshot {
  contract_version: typeof FACT_SNAPSHOT_CONTRACT_VERSION;
  fact_data_revision: number;
  source_records: SourceRecord[];
  fact_assertions: FactAssertion[];
  field_provenance: FieldProvenance[];
}

interface CandidateBase {
  candidate_id: string;
  research_packet_id: string;
  destination_id: string;
  fact_assertion_ids: string[];
  source_record_ids: string[];
  field_paths: string[];
  active_constraint_ids: string[];
  constraint_evaluations: Array<{
    constraint_id: string;
    status: 'passed' | 'failed' | 'unknown';
    fact_assertion_ids: string[];
    reason_code: string | null;
  }>;
  weather_sensitivity: {
    exposure: 'indoor' | 'outdoor' | 'mixed';
    rain_sensitivity: 'none' | 'low' | 'high';
    heat_sensitivity: 'none' | 'low' | 'high';
    cold_sensitivity: 'none' | 'low' | 'high';
    wind_sensitivity: 'none' | 'low' | 'high';
    requires_clear_visibility: boolean;
  };
  selection_reasons: string[];
  tradeoff: string;
  planning_decision_ids: string[];
  weather_impact_ids: string[];
  personalization_influence_ids: string[];
  freshness_status: 'current' | 'refreshing' | 'stale';
  observed_at: string | null;
  expires_at: string | null;
}

export type ResearchCandidate = CandidateBase & (
  | { candidate_kind: 'visit'; place_id: string; name: string; address: string; visit_type: VisitStop['visit_type']; recommended_duration_minutes: number; estimated_cost_cny: number | null; opening_window: string | null; reservation_required: boolean | null; highlights: string[] }
  | { candidate_kind: 'dining'; place_id: string; branch_name: string; address: string; meal_types: DiningStop['meal_type'][]; cuisine_types: string[]; average_spend_cny: number | null; recommended_dishes: string[]; opening_window: string | null; reservation_required: boolean | null; availability_status: 'confirmed' | 'needs_confirmation' | 'unavailable' }
  | { candidate_kind: 'lodging'; place_id: string; property_name: string; address: string; check_in_date: string; check_out_date: string; nights: number; room_type: string | null; nightly_price_cny: number | null; total_price_cny: number | null; price_kind: LodgingPriceKind; facilities: string[]; anchor_travel_minutes: Record<string, number>; availability_status: LodgingAvailabilityStatus }
  | { candidate_kind: 'transport'; route_id: string; transport_class: TransportLeg['transport_class']; selected_mode: TransportMode; from_endpoint: TransportEndpoint; to_endpoint: TransportEndpoint; departure_at: string | null; arrival_at: string | null; duration_minutes: number; distance_meters: number | null; total_cost_cny: number | null; segments: TransportSegment[]; booking_status: TransportLeg['booking_status'] }
);

export interface ResearchPacket {
  contract_version: typeof RESEARCH_PACKET_CONTRACT_VERSION;
  research_packet_id: string;
  run_id: string;
  task_id: string;
  worker_kind: 'destination_researcher' | 'accommodation_researcher' | 'transport_researcher';
  constraint_pack_revision: number;
  fact_data_revision: number;
  query_context: Record<string, unknown>;
  candidates: ResearchCandidate[];
  source_records: SourceRecord[];
  fact_assertions: FactAssertion[];
  field_provenance: FieldProvenance[];
  generated_at: string;
}

export interface RecommendationCatalog {
  contract_version: typeof RECOMMENDATION_CATALOG_CONTRACT_VERSION;
  fact_data_revision: number;
  weather_data_revision: number;
  research_packets: ResearchPacket[];
  admission_results: Array<{
    candidate_id: string;
    selection_slot_id: string | null;
    status: 'passed' | 'insufficient_for_admission';
    checked_constraint_ids: string[];
    missing_field_paths: string[];
    fit_scores: {
      budget_fit: number;
      weather_fit: number;
      constraint_fit: number;
    };
    evaluated_fact_revision: number;
    evaluated_weather_revision: number;
    weather_impact_ids: string[];
  }>;
}

export interface TripWorkspaceV2 {
  contract_version: typeof TRIP_WORKSPACE_CONTRACT_VERSION;
  run_id: string;
  workspace_revision: number;
  itinerary: StructuredItineraryV2;
  recommendation_catalog: RecommendationCatalog;
  user_input_anchors: UserInputAnchor[];
  selection_slots: SelectionSlot[];
  personalization_influences: PersonalizationInfluence[];
  weather_proposal_decisions: Array<{
    proposal_id: string;
    decision: 'applied' | 'dismissed';
  }>;
}

export type WeatherAdjustmentOperation =
  | { type: 'reschedule_item'; item_id: string; expected_planned_start: string | null; expected_planned_end: string | null; planned_start: string; planned_end: string }
  | { type: 'select_option'; selection_slot_id: string; expected_option_id: string | null; option_id: string }
  | { type: 'replace_visit_candidate'; item_id: string; expected_candidate_id: string; candidate_id: string }
  | { type: 'set_transport_mode'; transport_leg_id: string; expected_mode: TransportMode; selected_mode: TransportMode }
  | { type: 'add_buffer'; target_entity_id: string; day_id: string; block_id: string; duration_minutes: number };

export interface WeatherAdjustmentProposal {
  proposal_id: string;
  date: string;
  base_workspace_revision: number;
  base_weather_data_revision: number;
  severity: 'medium' | 'high';
  summary: string;
  weather_impact_ids: string[];
  fact_assertion_ids: string[];
  operations: WeatherAdjustmentOperation[];
  cost_delta_cny: number | null;
  time_delta_minutes: number | null;
}

export interface WeatherContextSnapshot {
  contract_version: typeof WEATHER_SNAPSHOT_CONTRACT_VERSION;
  weather_data_revision: number;
  trip_start_date: string;
  trip_end_date: string;
  days: Array<{
    destination_id: string;
    date: string;
    timezone: string | null;
    latitude: number;
    longitude: number;
    data_kind: 'forecast' | 'seasonal_baseline' | 'unavailable';
    condition_code: number | null;
    condition_label: string | null;
    high_c: number | null;
    low_c: number | null;
    apparent_high_c: number | null;
    precipitation_probability_pct: number | null;
    precipitation_mm: number | null;
    wind_speed_kph: number | null;
    wind_gust_kph: number | null;
    hourly_windows: Array<{ start_at: string; end_at: string; precipitation_probability_pct: number | null; apparent_temperature_c: number | null; wind_speed_kph: number | null }>;
    alert_ids: string[];
    fact_assertion_ids: string[];
  }>;
  coverage: Array<{
    destination_id: string;
    start_date: string;
    end_date: string;
    status: 'complete' | 'partial' | 'unavailable';
    available_dates: string[];
    unavailable_dates: string[];
  }>;
  impacts: Array<{
    weather_impact_id: string;
    date: string;
    target_ref: EntityRef | { selection_slot_id: string } | { transport_leg_id: string };
    condition_type: 'rain' | 'heat' | 'cold' | 'wind' | 'thunderstorm' | 'snow' | 'visibility';
    severity: 'low' | 'medium' | 'high';
    action: 'keep' | 'move_time' | 'rerank' | 'replace' | 'change_transport' | 'add_buffer' | 'require_plan_b';
    fact_assertion_ids: string[];
    affected_constraint_ids: string[];
    data_kind: 'forecast' | 'seasonal_baseline';
    trigger_code: string;
  }>;
  adjustment_proposals: WeatherAdjustmentProposal[];
  retrieved_at: string;
}

export interface PublicCitationProjection {
  citation_id: string;
  entity_ref: EntityRef;
  field_paths: string[];
  fact_status: 'verified' | 'refreshing' | 'stale' | 'conflict' | 'missing';
  supported_values: Array<{ label: string; value: unknown; unit: string | null; currency: string | null }>;
  sources: Array<{
    source_record_id: string;
    source_kind: 'external_web' | 'external_tool' | 'rag_chunk';
    title: string;
    public_excerpt: string;
    canonical_url: string | null;
    retrieved_at: string;
    observed_at: string | null;
  }>;
}

/**
 * 一条行程条目要显示的每一行，服务端渲染好一次（`entities/delivery_presentation.py`）。
 *
 * 浏览器与 PDF 都**只负责印这些字符串**，排版（竖排还是用 `·` 连起来）各自决定，字一个都不
 * 在消费端造。各面自己拼一遍必然漂开 —— `coach` 在浏览器上是「长途巴士」在纸上是「长途
 * 汽车」，一条 `booked` 的腿在浏览器上说「已预订」在 PDF 上说「需自行确认」。
 *
 * 编辑与 mutation 仍然直接读行程实体：那是另一条通道，渲染表单控件而不是交付文案。
 */
export interface PublicBlockPresentation {
  /** 这一行在当天路线里的位置身份。同一实体可以在一天里出现两次（跨夜交通的两半、
   *  住宿的入住与退房），所以键必须按位置而不是按实体。 */
  entry_id: string;
  display_title: string;
  node_summary: string | null;
  node_role: 'place' | 'movement' | 'arrival' | 'departure';
  transport_mode: TransportMode | null;
  time_label: string | null;
  duration_label: string | null;
  price_label: string | null;
  facts: string[];
  notes: string[];
  segment_lines: string[];
}

/** 实体原始字段 + 投影层渲染好的行。渲染面只读后者。 */
export type PublicReportBlockDetails = PublicBlockPresentation & { [key: string]: unknown };

interface PublicReportBlockBase {
  entity_ref: EntityRef;
  day_id: string | null;
  projection_role: 'full' | 'departure' | 'arrival' | 'check_in' | 'check_out';
  title: string;
  summary: string;
  details: PublicReportBlockDetails;
  citation_ids: string[];
}

/** 正式报告里的行程实体块；与工作台同一实体共享同一个 evidence_basis。 */
export interface PublicReportEntityBlock extends PublicReportBlockBase {
  entity_kind: 'visit' | 'dining' | 'lodging' | 'transport';
  evidence_basis: EvidenceBasis;
}

/** 自定义安排来自用户自己，既没有来源也没有依据口径。 */
export interface PublicReportCustomBlock extends PublicReportBlockBase {
  entity_kind: 'custom';
}

export type PublicReportBlock = PublicReportEntityBlock | PublicReportCustomBlock;

export interface TripReportProjection {
  source_workspace_revision: number;
  source_fact_data_revision: number;
  source_weather_data_revision: number;
  status: 'pending' | 'building' | 'ready' | 'stale' | 'failed';
  document: {
    title: string;
    overview: string;
    destinations: Array<{ destination_id: string; display_name: string }>;
    duration_days: number;
    cost_summary: CostCoverageSummary;
    /**
     * 费用那一句话，服务端算一次。三个界面各自从 cost_summary 拼过一份，
     * 而且已经漂开了，所以判据与文案都收在
     * `entities/cost_coverage.py`，这里只负责印。
     *
     * `null` = 这趟一个价都没查到，服务端明确表示无话可说，三个面都不画这一行。
     * 不要在这里回退成「费用待确认」——那正是被去掉的话术。
     */
    cost_coverage_statement: string | null;
    days: Array<{ day_id: string; day: number; date: string | null; destination_id: string; destination_name: string; theme: string; blocks: PublicReportBlock[] }>;
    selections: Array<{ selection_slot_id: string; slot_type: SelectionSlotType; context: Record<string, unknown>; status: 'ready' | 'needs_user_decision'; options: Array<{ option_id: string; name: string; rank: number; selected: boolean; recommended: boolean; selection_reasons: string[]; tradeoff: string | null; comparison_facts: string[]; availability_status: 'confirmed' | 'needs_confirmation'; citation_ids: string[] }> }>;
    /**
     * `observed_at` / `weather_data_state` 是时效角标的数据。少了它们，一个连着几天刷新失败
     * 的 run 和一分钟前刷过的 run 长得一模一样。二态由投影期判定
     * （`services/delivery_projection.py`），三个渲染面都不各自推导。
     */
    weather: Array<{ destination_id: string; destination_name: string; date: string; data_kind: 'forecast' | 'seasonal_baseline' | 'unavailable'; observed_at: string | null; weather_data_state: 'current' | 'historical'; condition_label: string | null; high_c: number | null; low_c: number | null; precipitation_probability_pct: number | null; wind_speed_kph: number | null; citation_ids: string[] }>;
    highlights: string[];
    important_notes: string[];
  } | null;
  citations: PublicCitationProjection[];
  generated_at: string | null;
}

export interface CostCoverageSummary {
  known_subtotal_cny: number | null;
  estimated_total_cny: number | null;
  priced_component_count: number;
  budget_relevant_component_count: number;
  coverage: 'none' | 'partial' | 'complete';
  budget_cap_cny: number | null;
  budget_status: 'unknown' | 'within_cap' | 'over_cap';
  /**
   * 整趟预算的估算值，由服务端一次 fast tier 调用给出。
   * 和上面两个总额**不是一回事**：那两个是供应商真报过的价钱加出来的，
   * 这个是对没人报价那部分的推算。渲染时必须标明是估算，不要和已知费用相加。
   */
  llm_estimated_total_cny: number | null;
}

export interface MapProjection {
  source_workspace_revision: number;
  content: {
    places: Array<{ entity_ref: EntityRef; name: string; place_id: string; latitude: number | null; longitude: number | null; citation_ids: string[] }>;
    routes: Array<{ entity_ref: EntityRef; transport_class: TransportLeg['transport_class']; selected_mode: TransportMode; route_status: TransportLeg['route_status']; from_endpoint: TransportEndpoint; to_endpoint: TransportEndpoint; segments: TransportSegment[]; citation_ids: string[] }>;
  };
}

export interface DeliveryRevisionManifest {
  contract_version: typeof DELIVERY_BUNDLE_CONTRACT_VERSION;
  run_id: string;
  bundle_id: string;
  workspace_revision: number;
  fact_data_revision: number;
  weather_data_revision: number;
  created_at: string;
}

/**
 * The only Bundle shape admitted to ordinary product surfaces.
 *
 * Persisted facts, provider/cache provenance, research packets, closeout
 * controls, and raw weather ledgers deliberately never cross this boundary.
 */
export interface PublicDeliveryBundle {
  manifest: DeliveryRevisionManifest;
  workspace: PublicTripWorkspace;
  report_projection: TripReportProjection;
  map_projection: MapProjection;
  source_index: { source_fact_data_revision: number; content: { citations: PublicCitationProjection[] } };
  /**
   * 本次规划没做到的部分，已经是句子。结构化的域枚举留在服务端：域名之外的任何东西
   * （reason_code、provider 名、worker 名）在产品面上都会被读成别的意思。
   */
  coverage_disclosure: { notes: string[] };
  /**
   * 沙箱披露的那一句，服务端按证据算出来（`entities/provider_environment.py`）。
   * 界面**不许自己判断**「长途 + 航班 ⇒ 沙箱」：那个判据在换 live key 那天会在已导出的 PDF 上
   * 留一句不可逆的假话，而对沙箱的非航班班次又一句都不出。
   * `null` 表示本次所有供应商响应都来自生产环境。
   */
  provider_environment: { sandbox_note: string | null };
}

export interface PublicWeatherAdjustment {
  proposal_id: string;
  date: string;
  severity: 'medium' | 'high';
  summary: string;
  cost_delta_cny: number | null;
  time_delta_minutes: number | null;
  status: 'pending' | 'applied' | 'dismissed';
}

export interface PublicTripWorkspace {
  contract_version: typeof TRIP_WORKSPACE_CONTRACT_VERSION;
  run_id: string;
  workspace_revision: number;
  itinerary: PublicStructuredItineraryV2;
  selection_slots: SelectionSlot[];
  weather_proposal_decisions: Array<{
    proposal_id: string;
    decision: 'applied' | 'dismissed';
  }>;
  weather_adjustments: PublicWeatherAdjustment[];
}

export type WorkspaceV2MutationOperation =
  | { type: 'select_option'; selection_slot_id: string; option_id: string }
  | { type: 'move_timeline_item'; item_id: string; to_day_id: string; before_entry_id?: string | null }
  | { type: 'update_stop_schedule'; item_id: string; planned_start?: string | null; planned_end?: string | null; duration_minutes?: number | null }
  | { type: 'create_custom_block'; block: CustomBlock; before_entry_id?: string | null }
  | { type: 'update_custom_block'; item_id: string; title?: string; note?: string | null; planned_start?: string | null; planned_end?: string | null; duration_minutes?: number | null }
  | { type: 'delete_custom_block'; item_id: string }
  | { type: 'delete_transport_leg'; transport_leg_id: string }
  | { type: 'set_transport_mode'; transport_leg_id: string; selected_mode: TransportMode; lock_mode?: boolean }
  | { type: 'update_transport_mode_preference'; transport_leg_id: string; locked_mode?: TransportMode | null; excluded_modes?: TransportMode[] | null }
  | { type: 'delete_lodging_stay'; stay_id: string }
  | { type: 'apply_weather_adjustment'; proposal_id: string }
  | { type: 'dismiss_weather_adjustment'; proposal_id: string };

export interface WeatherBundleRefreshRequest {
  user_id: string;
  session_id: string | null;
  refresh_id: string;
  base_bundle_id: string;
  base_workspace_revision: number;
  base_fact_data_revision: number;
  base_weather_data_revision: number;
}

export interface WeatherBundleRefreshResponse {
  refresh_id: string;
  attempted: boolean;
  committed: boolean;
  used_previous_values: boolean;
  /**
   * Machine-readable refusal code, present only when the refresh was attempted and
   * explicitly refused: `committed` is then false and `bundle` is the unchanged current
   * Bundle. Null when nothing was refused.
   */
  refusal_reason: string | null;
  bundle: PublicDeliveryBundle;
}

export interface WorkspaceV2MutationRequest {
  user_id: string;
  session_id: string | null;
  mutation_id: string;
  base_bundle_id: string;
  base_workspace_revision: number;
  base_fact_data_revision: number;
  base_weather_data_revision: number;
  operation: WorkspaceV2MutationOperation;
}

export interface WorkspaceV2MutationPreviewResponse {
  mutation_id: string;
  allowed: boolean;
  changed: boolean;
  requires_confirmation: boolean;
  impacts: Array<{ kind: 'total_cost'; delta_cny: number; summary: string }>;
}

export interface WorkspaceV2MutationResponse {
  mutation_id: string;
  changed: boolean;
  idempotent_replay: boolean;
  bundle: PublicDeliveryBundle;
}

export interface WorkspaceV2UndoHead {
  available: boolean;
  mutation_id: string | null;
  label: string | null;
}

export interface WorkspaceV2UndoRequest {
  user_id: string;
  session_id: string | null;
  undo_id: string;
  undo_of_mutation_id: string;
  base_bundle_id: string;
  base_workspace_revision: number;
  base_fact_data_revision: number;
  base_weather_data_revision: number;
}

export interface WorkspaceV2UndoResponse {
  undo_id: string;
  idempotent_replay: boolean;
  bundle: PublicDeliveryBundle;
}

/** Minimal runtime guard for the atomic boundary; deep validation remains server-owned. */
export function isPublicDeliveryBundle(value: unknown): value is PublicDeliveryBundle {
  if (!value || typeof value !== 'object') return false;
  const bundle = value as Partial<PublicDeliveryBundle>;
  const manifest = bundle.manifest;
  const workspace = bundle.workspace;
  return Boolean(
    manifest
      && manifest.contract_version === DELIVERY_BUNDLE_CONTRACT_VERSION
      && typeof manifest.run_id === 'string'
      && typeof manifest.bundle_id === 'string'
      && workspace?.contract_version === TRIP_WORKSPACE_CONTRACT_VERSION
      && workspace.run_id === manifest.run_id
      && workspace.workspace_revision === manifest.workspace_revision
      && Array.isArray(workspace.itinerary?.day_plans)
      && Array.isArray(workspace.selection_slots)
      && Array.isArray(workspace.weather_proposal_decisions)
      && Array.isArray(workspace.weather_adjustments)
      && bundle.report_projection
      && bundle.map_projection
      && bundle.source_index
  );
}
