"""Pure JourneyPilot v2 delivery projections.

Every function in this module is deterministic: it only reads the immutable
workspace/fact/weather inputs supplied by the caller and performs no provider,
LLM, database, clock, or global-state access.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from ..entities.delivery_bundle import (
    SELECTION_SLOT_ENTITY_TYPES,
    DiningCandidate,
    DiningStop,
    CustomBlock,
    EntityRef,
    EntityType,
    FactAssertion,
    FactStoreSnapshot,
    LodgingCandidate,
    LodgingStay,
    MapPlaceProjection,
    MapProjection,
    MapProjectionContent,
    MapRouteProjection,
    PublicCitationProjection,
    PublicSourceSummary,
    PublicSupportedValue,
    ReportDaySection,
    ReportDestination,
    ReportEntityBlock,
    ReportSelectionOption,
    ReportSelectionSection,
    ReportWeatherDay,
    SourceIndexDocument,
    SourceIndexProjection,
    SourceRecord,
    StructuredItineraryV2,
    TransportCandidate,
    TransportLeg,
    TripReportDocument,
    TripReportProjection,
    TripWorkspaceV2,
    VisitCandidate,
    VisitStop,
    WeatherContextSnapshot,
    WeatherDayContext,
)
from ..entities.controlled_place_name import (
    CONTROLLED_DESTINATION_ANCHOR_PREFIX,
    controlled_public_place_name,
)
from ..entities.cost_coverage import cost_coverage_statement
from ..entities.delivery_presentation import block_presentation
from ..entities.provider_evidence import (
    build_required_long_distance_legs,
    missing_long_distance_leg_roles,
)
from ..tools.temporal import RAIL_LIVE_INVENTORY_DAYS
from .geo_dispersion import entity_coordinates


class DeliveryProjectionError(ValueError):
    """Raised when immutable v2 facts cannot produce a truthful projection."""


_INTERNAL_SELECTION_MARKERS = (
    "eligible_place_options",
    "candidate_id",
    "place_id",
    "provider_",
    "selection_slot",
    "fact_assertion",
    "source_record",
    "research_packet",
)

_PUBLIC_COMPARISON_LABELS = {
    "branch_name": "具体餐厅",
    "property_name": "具体住宿",
    "place_id": "实体身份已核验",
    "provider_place_type": "实体类型已核验",
    "provider_country_code": "所在国家或地区已核验",
    "address": "地址",
    "external_quality_match": "外部评价已核验",
    "average_spend_cny": "人均消费",
    "nightly_price_cny": "每晚价格",
    "total_price_cny": "总价",
    "facilities": "设施",
    "opening_window": "营业时间",
    "reservation_required": "预约要求",
}

_BOOKING_CONFIRMATION_TITLE_MARKERS = (
    "预订确认",
    "预订确认单",
    "确认单",
    "booking confirmation",
    "reservation confirmation",
)


def _public_selection_text(value: str) -> str:
    """Keep model planning rationale while hiding orchestration vocabulary."""

    lowered = value.lower()
    if any(marker in lowered for marker in _INTERNAL_SELECTION_MARKERS):
        return "已核验为真实具体实体，并满足本次行程的硬约束"
    return value


def _public_comparison_facts(field_paths: Sequence[str]) -> list[str]:
    labels: list[str] = []
    for field_path in field_paths:
        label = _PUBLIC_COMPARISON_LABELS.get(field_path)
        if label and label not in labels:
            labels.append(label)
    return labels or ["实体与来源已核验"]


# 结构化搜索载荷放条目的键。``results`` 是本仓自有工具的形状，``content`` 是 MCP
# 工具结果的标准形状——只认前者时，后者整族漏过提取，原样把一坨 JSON 印给用户，
# 而藏在里面的 url 一条都到不了卡片上。
_PAYLOAD_RESULT_KEYS = ("results", "content")


def _payload_public_excerpt(payload: object) -> tuple[str | None, str | None]:
    """Turn a structured search payload into one readable excerpt and URL."""

    if not isinstance(payload, dict):
        return None, None
    usable: list[dict] = []
    for key in _PAYLOAD_RESULT_KEYS:
        entries = payload.get(key)
        if isinstance(entries, list):
            usable = [item for item in entries if isinstance(item, dict)]
            if usable:
                break
    if not usable:
        return None, None
    first = usable[0]
    title = str(first.get("title") or "").strip()
    snippet = str(first.get("snippet") or "").strip()
    excerpt = "。".join(part for part in (title, snippet) if part)
    if len(excerpt) > 360:
        excerpt = f"{excerpt[:357].rstrip()}…"
    url = str(first.get("url") or "").strip() or None
    return excerpt or f"已核对 {len(usable)} 条外部结果。", url


def _json_public_excerpt(value: str) -> tuple[str | None, str | None]:
    if not value.lstrip().startswith(("{", "[")):
        return None, None
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None, None
    return _payload_public_excerpt(payload)


# 载荷里可以直接读给人看的字段，按优先级。名字类在前、说明类在后，拼出来才像句话。
_READABLE_FIELDS = (
    "title",
    "name",
    "display_name",
    "label",
    "snippet",
    "description",
    "summary",
    "address",
    "formatted_address",
)


def _readable_bits(entry: Mapping[str, Any]) -> list[str]:
    """从一条记录里挑出能读的几段，保持 ``_READABLE_FIELDS`` 的顺序。"""

    bits: list[str] = []
    for field in _READABLE_FIELDS:
        value = entry.get(field)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text and text not in bits:
                bits.append(text)
    return bits


def _rendered_payload_excerpt(payload: object) -> str | None:
    """把任意结构化载荷渲染成人话——**卡片上永远不许出现原始 JSON**。

    `_payload_public_excerpt` 只认 ``results`` / ``content`` 两个已知键；键名一换，
    整坨 JSON 就会原样印到读者眼前（实测 32 个 Bundle 里有 83 条这样的摘要）。
    这里必须兜底：**读者不该为我们的可观测性买单**。

    所以这里做的是**渲染**而不是「换一句套话」：任意形状都尽力读出名字与说明。
    这跟「静默 fallback」不是一回事——新形状不会被藏起来，它照样以可读文本出现，
    只是不再以 JSON 的样子出现；两个已知形状仍由各自的钉守着。
    """

    if isinstance(payload, list):
        entries = [item for item in payload if isinstance(item, Mapping)]
    elif isinstance(payload, Mapping):
        nested = [
            value
            for value in payload.values()
            if isinstance(value, list) and any(isinstance(i, Mapping) for i in value)
        ]
        if nested:
            entries = [item for item in nested[0] if isinstance(item, Mapping)]
        else:
            entries = [payload]
    else:
        return None

    for entry in entries:
        bits = _readable_bits(entry)
        if bits:
            excerpt = "。".join(bits)
            if len(excerpt) > 360:
                excerpt = f"{excerpt[:357].rstrip()}…"
            return excerpt

    # 一条可读字段都没有：只说清这是什么，不把内部结构倒给读者。
    return f"已核对 {len(entries)} 条外部结果。" if entries else None


# 非 http 的供应商链接 → 网页地址。卡片上的链接必须**点得开**：实测 5 条链接里
# 3 条是 ``amap://poi/…`` 应用深链，在浏览器里是死的。
_APP_LINK_WEB_FORMS = {
    "amap": "https://ditu.amap.com/detail/{ident}",
}


def _web_openable_url(url: str | None) -> str | None:
    """只交出读者点得开的链接：http(s) 原样，已知应用深链转网页，其余不给。

    不给，而不是原样给——一个点不动的链接比没有链接更让人困惑。
    """

    if not url:
        return None
    text = str(url).strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        return text
    scheme, _, rest = text.partition("://")
    template = _APP_LINK_WEB_FORMS.get(scheme.lower())
    if not template:
        return None
    ident = rest.rsplit("/", 1)[-1].strip()
    return template.format(ident=ident) if ident else None


def _public_source_presentation(source: SourceRecord) -> tuple[str, str, str | None]:
    """Project audit-rich SourceRecord fields into ordinary-user source copy."""

    title = source.title
    excerpt = source.public_excerpt.strip()
    canonical_url = source.canonical_url
    lowered = title.lower()

    if " branch review match: " in lowered:
        entity_name = title.split(":", 1)[-1].strip()
        title = f"餐饮评价核验：{entity_name}"
    elif " place record: " in lowered:
        entity_name = title.split(":", 1)[-1].strip()
        title = f"地点资料：{entity_name}"
    elif " route: " in lowered:
        route_label = excerpt.split(" · ", 1)[0].strip()
        title = f"交通路线：{route_label}" if route_label else "交通路线"
    elif source.source_record_id.startswith("weather-source-"):
        title = "天气参考"

    snapshot_result = source.snapshot.get("sanitized_result")
    structured_excerpt, result_url = (
        _payload_public_excerpt(snapshot_result)
        if excerpt.lstrip().startswith(("{", "["))
        else (None, None)
    )
    if not structured_excerpt:
        structured_excerpt, result_url = _json_public_excerpt(excerpt)
    if structured_excerpt:
        excerpt = structured_excerpt
        canonical_url = canonical_url or result_url
    if excerpt.lstrip().startswith(("{", "[")):
        # 两个已知形状都没认出来。渲染成人话，绝不把 JSON 交给读者。
        try:
            payload = json.loads(excerpt)
        except (TypeError, json.JSONDecodeError):
            payload = source.snapshot.get("sanitized_result")
        rendered = _rendered_payload_excerpt(payload)
        excerpt = rendered or "已核对外部结果。"
    return title, excerpt, _web_openable_url(canonical_url)


def build_fact_snapshot(
    workspace: TripWorkspaceV2,
    *,
    weather_sources: Sequence[SourceRecord] = (),
    weather_facts: Sequence[FactAssertion] = (),
    weather_provenance: Sequence = (),
) -> FactStoreSnapshot:
    """Merge packet and weather facts without accepting conflicting identities."""

    sources: dict[str, SourceRecord] = {}
    facts: dict[str, FactAssertion] = {}
    provenance: dict[tuple[str, str, str], object] = {}

    def add_unique(index: dict, key: str, value: object, kind: str) -> None:
        current = index.get(key)
        if current is not None and current != value:
            raise DeliveryProjectionError(f"conflicting {kind} identity: {key}")
        index[key] = value

    for packet in workspace.recommendation_catalog.research_packets:
        for source in packet.source_records:
            add_unique(sources, source.source_record_id, source, "source record")
        for fact in packet.fact_assertions:
            add_unique(facts, fact.fact_assertion_id, fact, "fact assertion")
        for item in packet.field_provenance:
            provenance[(item.entity_ref.entity_id, item.field_path, item.origin)] = item
    for source in weather_sources:
        add_unique(sources, source.source_record_id, source, "weather source record")
    for fact in weather_facts:
        add_unique(facts, fact.fact_assertion_id, fact, "weather fact assertion")
    for item in weather_provenance:
        provenance[(item.entity_ref.entity_id, item.field_path, item.origin)] = item

    return FactStoreSnapshot(
        fact_data_revision=workspace.recommendation_catalog.fact_data_revision,
        source_records=list(sources.values()),
        fact_assertions=list(facts.values()),
        field_provenance=list(provenance.values()),
    )


def _public_source(source: SourceRecord) -> PublicSourceSummary:
    chunk_content = None
    chunk_locator = None
    if source.source_kind == "rag_chunk":
        chunk_content = source.snapshot.get("chunk_content") or source.snapshot.get(
            "content"
        )
        if not isinstance(chunk_content, str) or not chunk_content.strip():
            raise DeliveryProjectionError(
                f"RAG source {source.source_record_id} is missing the complete chunk"
            )
        locator_parts = [
            source.snapshot.get("document_id"),
            source.snapshot.get("document_revision"),
            source.snapshot.get("chunk_id"),
            source.snapshot.get("section") or source.snapshot.get("page"),
        ]
        chunk_locator = " / ".join(
            str(part) for part in locator_parts if part is not None
        )
    title, public_excerpt, canonical_url = _public_source_presentation(source)
    return PublicSourceSummary(
        source_record_id=source.source_record_id,
        source_kind=source.source_kind,
        title=title,
        provider_name=source.provider_name,
        public_excerpt=public_excerpt,
        canonical_url=canonical_url,
        retrieved_at=source.retrieved_at,
        observed_at=source.observed_at,
        content_hash=source.content_hash,
        rag_chunk_content=chunk_content,
        rag_document_locator=chunk_locator or None,
    )


def _citation_id(entity_ref: EntityRef, fact: FactAssertion) -> str:
    payload = f"{entity_ref.entity_type.value}:{entity_ref.entity_id}:{fact.fact_assertion_id}"
    return f"cite_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def _entity_lineage_targets(
    workspace: TripWorkspaceV2,
) -> list[tuple[EntityRef, Sequence[str]]]:
    itinerary = workspace.itinerary
    targets: list[tuple[EntityRef, Sequence[str]]] = []
    targets.extend(
        (
            EntityRef(entity_type=EntityType.VISIT_STOP, entity_id=item.item_id),
            item.lineage.fact_assertion_ids,
        )
        for item in itinerary.visit_stops
    )
    targets.extend(
        (
            EntityRef(entity_type=EntityType.DINING_STOP, entity_id=item.item_id),
            item.lineage.fact_assertion_ids,
        )
        for item in itinerary.dining_stops
    )
    targets.extend(
        (
            EntityRef(entity_type=EntityType.LODGING_STAY, entity_id=item.stay_id),
            item.lineage.fact_assertion_ids,
        )
        for item in itinerary.lodging_stays
    )
    targets.extend(
        (
            EntityRef(
                entity_type=EntityType.TRANSPORT_LEG, entity_id=item.transport_leg_id
            ),
            item.lineage.fact_assertion_ids if item.route_status == "ready" else [],
        )
        for item in itinerary.transport_legs
    )
    for slot in workspace.selection_slots:
        entity_type = SELECTION_SLOT_ENTITY_TYPES[slot.slot_type]
        for option in slot.options:
            # The selected option is already projected through the canonical
            # itinerary entity above.  Alternative option facts must keep their
            # candidate identity; otherwise their names, addresses, and prices
            # are falsely attributed to the selected stop/stay.
            if option.option_id == slot.selected_option_id:
                continue
            targets.append(
                (
                    EntityRef(entity_type=entity_type, entity_id=option.candidate_id),
                    option.fact_assertion_ids,
                )
            )
    return targets


def project_public_citations(
    workspace: TripWorkspaceV2,
    facts: FactStoreSnapshot,
    weather: WeatherContextSnapshot,
) -> list[PublicCitationProjection]:
    fact_index = {item.fact_assertion_id: item for item in facts.fact_assertions}
    source_index = {item.source_record_id: item for item in facts.source_records}
    targets = _entity_lineage_targets(workspace)
    for day in weather.days:
        targets.append(
            (
                EntityRef(
                    entity_type=EntityType.WEATHER_DAY,
                    entity_id=f"weather:{day.destination_id}:{day.date.isoformat()}",
                ),
                day.fact_assertion_ids,
            )
        )

    projections: dict[str, PublicCitationProjection] = {}
    for target_ref, assertion_ids in targets:
        for assertion_id in assertion_ids:
            fact = fact_index.get(assertion_id)
            if fact is None:
                raise DeliveryProjectionError(
                    f"projection fact is missing: {assertion_id}"
                )
            source_ids = [
                link.source_record_id
                for link in fact.source_links
                if link.relation in {"supports", "qualifies"}
            ]
            if not source_ids:
                raise DeliveryProjectionError(
                    f"projection fact has no public source: {assertion_id}"
                )
            missing = [
                source_id for source_id in source_ids if source_id not in source_index
            ]
            if missing:
                raise DeliveryProjectionError(
                    f"projection source is missing: {missing[0]}"
                )
            citation_id = _citation_id(target_ref, fact)
            projections[citation_id] = PublicCitationProjection(
                citation_id=citation_id,
                entity_ref=target_ref,
                field_paths=[fact.field_path],
                fact_status=fact.status if fact.status != "superseded" else "stale",
                supported_values=[
                    PublicSupportedValue(
                        label=fact.field_path,
                        value=fact.asserted_value,
                        unit=fact.unit,
                        currency=fact.currency,
                    )
                ],
                sources=[
                    _public_source(source_index[source_id])
                    for source_id in dict.fromkeys(source_ids)
                ],
                fact_assertion_ids=[assertion_id],
            )
    return sorted(projections.values(), key=lambda item: item.citation_id)


def _citation_lookup(
    citations: Iterable[PublicCitationProjection],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    by_fact: dict[str, str] = {}
    by_entity: dict[str, list[str]] = {}
    for citation in citations:
        for fact_id in citation.fact_assertion_ids:
            by_fact[f"{citation.entity_ref.entity_id}:{fact_id}"] = citation.citation_id
        by_entity.setdefault(citation.entity_ref.entity_id, []).append(
            citation.citation_id
        )
    return by_fact, by_entity


def _trip_level_requirements(workspace: TripWorkspaceV2) -> list[str]:
    categories = {
        "budget_cap",
        "elderly_mobility",
        "health_condition",
        "pace_preference",
    }
    requirements: list[str] = []
    for anchor in workspace.user_input_anchors:
        if anchor.input_kind != "hard_constraint" or not isinstance(
            anchor.value, Mapping
        ):
            continue
        if str(anchor.value.get("category") or "") not in categories:
            continue
        if anchor.public_summary and anchor.public_summary not in requirements:
            requirements.append(anchor.public_summary)
    return requirements


_LONG_DISTANCE_GAP_NOTES = {
    ("outbound", "return"): (
        "本方案尚未给出前往目的地与返回出发地的长途交通路径；"
        "出行前请自行确认往返的可用班次与票务。"
    ),
    ("outbound",): (
        "本方案尚未给出前往目的地的长途交通路径；出行前请自行确认去程的可用班次与票务。"
    ),
    ("return",): (
        "本方案尚未给出返回出发地的长途交通路径；出行前请自行确认返程的可用班次与票务。"
    ),
}

_UNBOUND_LONG_DISTANCE_NOTE = (
    "出行日期距今较远，长途交通尚未确定到具体班次；"
    "方案中的长途路径只作可行性参考，出发前请按实际出行日期重新查询班次、时刻与票价。"
)


def _controlled_trip_identity(
    workspace: TripWorkspaceV2,
) -> Mapping[str, object] | None:
    """The Draft-authored trip identity every composed Workspace carries."""

    for anchor in workspace.user_input_anchors:
        if (
            anchor.input_kind == "controlled_identity"
            and anchor.field_path == "controlled_trip_identity"
            and isinstance(anchor.value, Mapping)
        ):
            return anchor.value
    return None


def _long_distance_legs_by_date(
    itinerary: StructuredItineraryV2,
) -> dict[date, list[TransportLeg]]:
    """Index the delivered long-distance legs by the Day they are projected on.

    ``StructuredItineraryV2.validate_references`` already forces every
    long-distance leg onto the planned Day(s) carrying its service dates, so
    the Day timeline is the server-anchored place to read a leg's service date
    from — no leg reaches a reader outside its own Day.
    """

    legs = {
        leg.transport_leg_id: leg
        for leg in itinerary.transport_legs
        if leg.transport_class == "long_distance"
    }
    by_date: dict[date, list[TransportLeg]] = {}
    for day in itinerary.day_plans:
        for ref in day.timeline:
            if ref.entity_type != EntityType.TRANSPORT_LEG:
                continue
            leg = legs.get(ref.entity_id)
            if leg is not None:
                by_date.setdefault(day.date, []).append(leg)
    return by_date


def _long_distance_path_notes(
    workspace: TripWorkspaceV2, generated_at: datetime
) -> list[str]:
    """Tell the reader when the round trip has no usable path, or an unbound one.

    ``build_required_long_distance_legs`` is the same authority the Planner and
    the Candidate Gate use to decide what the Run owed the traveller, so the
    report never invents a responsibility the Run was not given.  Only the
    ``leg_role``/``service_date`` of each required leg is read here, which is
    why ``cross_day_return_required`` — a research-scope detail with no bearing
    on which legs exist — is not consulted.  ``missing_long_distance_leg_roles``
    is the single judgement of what went undelivered, shared verbatim with the
    TripRun completion audit so the two never disagree about one Workspace.
    """

    identity = _controlled_trip_identity(workspace)
    if identity is None:
        return []
    required = build_required_long_distance_legs(
        identity, cross_day_return_required=False
    )
    itinerary = workspace.itinerary
    delivered = _long_distance_legs_by_date(itinerary)
    missing = missing_long_distance_leg_roles(
        required,
        day_dates={day.date for day in itinerary.day_plans},
        dates_with_long_distance=set(delivered),
    )
    if missing is None:
        return []

    unbound = False
    for leg in required:
        on_date = delivered.get(leg.service_date, [])
        if on_date and all(
            item.lineage.lineage_kind != "candidate_entity" for item in on_date
        ):
            # Candidate lineage is the only binding the server validates: the
            # Workspace checks it against an admitted candidate and a subset of
            # that candidate's verified facts and sources.  booking_status,
            # route_status and departure_at are values the composing agent
            # writes freely, so none of them proves a real service was found.
            #
            # Read as "not candidate-backed", not as "authored": ``lineage_kind``
            # is three-valued, and a ``reference_entity`` — a real service whose
            # claims were never confirmed for this date — is no more bound than an
            # authored one.  Naming the one bound state is what keeps a fourth
            # state from being silently counted as evidence.
            unbound = True

    notes: list[str] = []
    gap_note = _LONG_DISTANCE_GAP_NOTES.get(tuple(missing))
    if gap_note is not None:
        notes.append(gap_note)
    lead_days = (itinerary.day_plans[0].date - generated_at.date()).days
    if unbound and lead_days >= RAIL_LIVE_INVENTORY_DAYS:
        notes.append(_UNBOUND_LONG_DISTANCE_NOTE)
    return notes


def _report_destinations(workspace: TripWorkspaceV2) -> list[ReportDestination]:
    """Resolve every internal destination id to its controlled public name.

    The public name is the place's ``name``, **not** its ``display_name``.
    A provider ``display_name`` is a full administrative path — ``深圳市, 广东省, 中国``
    — and this value is the subject of a sentence on three product surfaces: the
    report cover's destination readout, every day heading, and each weather row.
    Printed there it reads ``深圳市, 广东省, 中国 · 交通日``: a whole address where a
    place name belongs.

    ``admin_path`` stays on the anchor for anyone who genuinely needs to
    disambiguate, so nothing is lost by not carrying the joined string here.
    """

    names: dict[str, str] = {}
    for anchor in workspace.user_input_anchors:
        if (
            anchor.input_kind != "controlled_identity"
            or not anchor.field_path.startswith(CONTROLLED_DESTINATION_ANCHOR_PREFIX)
            or not isinstance(anchor.value, Mapping)
        ):
            continue
        destination_id = str(anchor.value.get("place_id") or "").strip()
        public_name = controlled_public_place_name(anchor.value)
        if not destination_id or not public_name:
            raise DeliveryProjectionError(
                "controlled destination anchor requires place_id and name"
            )
        existing = names.get(destination_id)
        if existing is not None and existing != public_name:
            raise DeliveryProjectionError(
                f"conflicting public destination identity: {destination_id}"
            )
        names[destination_id] = public_name

    missing = [
        item for item in workspace.itinerary.destination_ids if item not in names
    ]
    if missing:
        raise DeliveryProjectionError(
            f"report destinations are missing controlled public identities: {missing}"
        )
    return [
        ReportDestination(destination_id=item, display_name=names[item])
        for item in workspace.itinerary.destination_ids
    ]


def _destination_name(
    destination_names: Mapping[str, str], destination_id: str | None
) -> str:
    if destination_id is None or destination_id not in destination_names:
        raise DeliveryProjectionError(
            f"public report references an unknown destination: {destination_id}"
        )
    return destination_names[destination_id]


def _weather_observed_at(
    day: WeatherDayContext,
    fact_index: Mapping[str, FactAssertion],
    snapshot_retrieved_at: datetime,
) -> datetime | None:
    """When this Day's weather was actually observed.

    The per-field assertions carry the provider's own retrieval time, which is the
    closest thing to an observation timestamp the pipeline has.  A Day whose
    assertions are not in this snapshot falls back to the snapshot's retrieval,
    which is still a truthful "obtained no later than".  A Day with no weather has
    no observation, and says so rather than borrowing one.
    """

    if day.data_kind == "unavailable":
        return None
    observations = [
        fact.observed_at
        for assertion_id in day.fact_assertion_ids
        if (fact := fact_index.get(assertion_id)) is not None
        and fact.observed_at is not None
    ]
    return max(observations) if observations else snapshot_retrieved_at


def _weather_data_state(
    day: WeatherDayContext, historical_days: frozenset[tuple[str, date]]
) -> str:
    """Whether the Day shows current weather or historical data, decided once.

    Three ways a Day is historical, and they are all the same statement to a
    reader — "what you are looking at is not from this refresh".  A seasonal
    baseline is month-level climatology, not a forecast for the traveller's date.
    A Day whose provider came back unavailable keeps the previous snapshot's
    values.  And a Day that still carries an impact this refresh did not
    re-evaluate is showing a forecast alongside a carried-forward history.

    The last two are known only to the refresh that produced the snapshot, so it
    passes them in for the length of one call; nothing about them is stored, and
    every refresh re-derives them from set arithmetic.  The decision is made here,
    once, not by each of the three renderers — otherwise the workspace badge, the
    report and the PDF could reach different verdicts about one Day.
    """

    if day.data_kind == "seasonal_baseline":
        return "historical"
    return (
        "historical" if (day.destination_id, day.date) in historical_days else "current"
    )


def _public_report_title(title: str) -> str:
    """Keep a formal delivery document from implying a reservation record."""

    if any(
        marker in title.casefold() for marker in _BOOKING_CONFIRMATION_TITLE_MARKERS
    ):
        return "旅行计划"
    return title


def _entity_block(
    entity: object,
    citation_ids: Sequence[str],
    entry_id: str,
    projection_role: str,
) -> ReportEntityBlock:
    if isinstance(entity, VisitStop):
        ref = EntityRef(entity_type=EntityType.VISIT_STOP, entity_id=entity.item_id)
        kind = "visit"
        summary = entity.selection_reason
        day_id = entity.day_id
    elif isinstance(entity, DiningStop):
        ref = EntityRef(entity_type=EntityType.DINING_STOP, entity_id=entity.item_id)
        kind = "dining"
        summary = entity.selection_reason
        day_id = entity.day_id
    elif isinstance(entity, LodgingStay):
        ref = EntityRef(entity_type=EntityType.LODGING_STAY, entity_id=entity.stay_id)
        kind = "lodging"
        summary = entity.selection_reason
        day_id = None
    elif isinstance(entity, TransportLeg):
        ref = EntityRef(
            entity_type=EntityType.TRANSPORT_LEG, entity_id=entity.transport_leg_id
        )
        kind = "transport"
        summary = f"{entity.from_endpoint.name} → {entity.to_endpoint.name}"
        day_id = None
    elif isinstance(entity, CustomBlock):
        ref = EntityRef(entity_type=EntityType.CUSTOM_BLOCK, entity_id=entity.item_id)
        kind = "custom"
        summary = entity.note or entity.title
        day_id = entity.day_id
    else:
        raise DeliveryProjectionError(
            f"unsupported report entity: {type(entity).__name__}"
        )
    payload = entity.model_dump(mode="json", exclude={"lineage", "type"})
    if "intent_explanations" in payload:
        payload["intent_explanations"] = [
            {
                "label": item.label,
                "explanation": item.explanation,
                "evidence_basis": item.evidence_basis,
            }
            for item in getattr(entity, "intent_explanations", [])
        ]
    for field in ("item_id", "stay_id", "transport_leg_id", "day_id", "name"):
        payload.pop(field, None)
    if isinstance(entity, LodgingStay) and projection_role == "check_out":
        summary = f"从 {entity.name} 办理退房"
        payload = {key: payload[key] for key in ("check_out_date", "check_out_time")}
    elif isinstance(entity, LodgingStay) and projection_role == "check_in":
        summary = f"入住 {entity.name}，覆盖 {entity.nights} 晚"
    elif isinstance(entity, TransportLeg) and projection_role == "departure":
        summary = f"从 {entity.from_endpoint.name} 出发"
    elif isinstance(entity, TransportLeg) and projection_role == "arrival":
        summary = f"抵达 {entity.to_endpoint.name}"
        payload = {key: payload[key] for key in ("selected_mode", "arrival_at")}
    # The rendered lines this entry shows, authored once (``delivery_presentation``)
    # and printed unchanged by the report, the workspace timeline and the PDF.  The
    # raw entity fields stay alongside them for the audit and mutation surfaces;
    # no renderer reads them any more.
    rendered = block_presentation(
        entity, entry_id=entry_id, projection_role=projection_role
    )
    payload.update(rendered)
    lineage = getattr(entity, "lineage", None)
    return ReportEntityBlock(
        entity_ref=ref,
        day_id=day_id,
        projection_role=projection_role,
        title=rendered["display_title"],
        entity_kind=kind,
        summary=summary,
        details=payload,
        citation_ids=list(citation_ids),
        weather_impact_ids=(
            getattr(lineage, "weather_impact_ids", []) if lineage is not None else []
        ),
        personalization_influence_ids=(
            getattr(lineage, "personalization_influence_ids", [])
            if lineage is not None
            else []
        ),
    )


def candidate_display_name(candidate: object) -> str:
    """The one name a Candidate is called by, whatever its domain calls the field.

    Public alongside :func:`candidate_place_id`: three domains spell this field three
    ways, so a copy of the mapping anywhere else is a derived quantity that goes stale
    the moment a fourth domain lands.
    """
    if isinstance(candidate, VisitCandidate):
        return candidate.name
    if isinstance(candidate, DiningCandidate):
        return candidate.branch_name
    if isinstance(candidate, LodgingCandidate):
        return candidate.property_name
    if isinstance(candidate, TransportCandidate):
        return f"{candidate.from_endpoint.name} → {candidate.to_endpoint.name}"
    raise DeliveryProjectionError(
        "selection option references an unsupported candidate"
    )


def candidate_place_id(candidate: object) -> str:
    """The Provider identity a Candidate is pinned to, or "" where there is none.

    Three of the four domains are a place and carry ``place_id`` under that exact
    name; transport is a route between two endpoints and has no single place, so
    it answers "" rather than being coerced into one. Callers keyed on identity
    (the knowledge-base nomination funnel) drop the empty answer, which is the
    right outcome: a route was never a place a chunk could have nominated.
    """
    if isinstance(candidate, (VisitCandidate, DiningCandidate, LodgingCandidate)):
        return candidate.place_id
    if isinstance(candidate, TransportCandidate):
        return ""
    raise DeliveryProjectionError(
        "selection option references an unsupported candidate"
    )


def project_report(
    workspace: TripWorkspaceV2,
    facts: FactStoreSnapshot,
    weather: WeatherContextSnapshot,
    *,
    generated_at: datetime,
    historical_weather_days: frozenset[tuple[str, date]] = frozenset(),
) -> TripReportProjection:
    citations = project_public_citations(workspace, facts, weather)
    citation_by_fact, citations_by_entity = _citation_lookup(citations)
    fact_index = {item.fact_assertion_id: item for item in facts.fact_assertions}
    itinerary = workspace.itinerary
    destinations = _report_destinations(workspace)
    destination_names = {
        item.destination_id: item.display_name for item in destinations
    }
    entities: dict[tuple[EntityType, str], object] = {}
    for item in itinerary.visit_stops:
        entities[(EntityType.VISIT_STOP, item.item_id)] = item
    for item in itinerary.dining_stops:
        entities[(EntityType.DINING_STOP, item.item_id)] = item
    for item in itinerary.lodging_stays:
        entities[(EntityType.LODGING_STAY, item.stay_id)] = item
    for item in itinerary.transport_legs:
        entities[(EntityType.TRANSPORT_LEG, item.transport_leg_id)] = item
    for item in itinerary.custom_blocks:
        entities[(EntityType.CUSTOM_BLOCK, item.item_id)] = item
    days: list[ReportDaySection] = []
    for day in itinerary.day_plans:
        blocks: list[ReportEntityBlock] = []
        for ref in day.timeline:
            entity = entities.get((ref.entity_type, ref.entity_id))
            if entity is None:
                raise DeliveryProjectionError(
                    f"report timeline reference is missing: {ref.entity_id}"
                )
            blocks.append(
                _entity_block(
                    entity,
                    citations_by_entity.get(ref.entity_id, []),
                    ref.entry_id,
                    ref.projection_role,
                )
            )
        days.append(
            ReportDaySection(
                day_id=day.day_id,
                day=day.day,
                date=day.date,
                destination_id=day.destination_id,
                destination_name=_destination_name(
                    destination_names, day.destination_id
                ),
                theme=day.theme,
                blocks=blocks,
            )
        )

    candidates = workspace.recommendation_catalog.candidate_index()
    selections: list[ReportSelectionSection] = []
    for slot in workspace.selection_slots:
        options: list[ReportSelectionOption] = []
        for option in slot.options:
            citation_entity_id = (
                slot.target_entity_id
                if option.option_id == slot.selected_option_id
                else option.candidate_id
            )
            option_citations = [
                citation_by_fact[f"{citation_entity_id}:{fact_id}"]
                for fact_id in option.fact_assertion_ids
            ]
            options.append(
                ReportSelectionOption(
                    option_id=option.option_id,
                    candidate_id=option.candidate_id,
                    name=candidate_display_name(candidates[option.candidate_id]),
                    rank=option.rank,
                    selected=option.option_id == slot.selected_option_id,
                    recommended=option.option_id == slot.recommended_option_id,
                    selection_reasons=[
                        _public_selection_text(reason)
                        for reason in option.selection_reasons
                    ],
                    tradeoff=(
                        _public_selection_text(option.tradeoff)
                        if option.tradeoff
                        else None
                    ),
                    comparison_facts=_public_comparison_facts(option.comparison_facts),
                    availability_status=option.availability_status,
                    citation_ids=option_citations,
                )
            )
        selections.append(
            ReportSelectionSection(
                selection_slot_id=slot.selection_slot_id,
                slot_type=slot.slot_type,
                context=slot.context,
                status="needs_user_decision"
                if slot.status == "needs_user_decision"
                else "ready",
                options=options,
            )
        )

    weather_days = [
        ReportWeatherDay(
            destination_id=day.destination_id,
            destination_name=_destination_name(destination_names, day.destination_id),
            date=day.date,
            data_kind=day.data_kind,
            observed_at=_weather_observed_at(day, fact_index, weather.retrieved_at),
            weather_data_state=_weather_data_state(day, historical_weather_days),
            condition_label=day.condition_label,
            high_c=day.high_c,
            low_c=day.low_c,
            precipitation_probability_pct=day.precipitation_probability_pct,
            wind_speed_kph=day.wind_speed_kph,
            citation_ids=[
                citation_by_fact[
                    f"weather:{day.destination_id}:{day.date.isoformat()}:{fact_id}"
                ]
                for fact_id in day.fact_assertion_ids
            ],
        )
        for day in weather.days
    ]
    start = itinerary.day_plans[0].date
    end = itinerary.day_plans[-1].date
    date_text = (
        f"{start.isoformat()} 至 {end.isoformat()}"
        if start and end
        else f"{itinerary.duration_days} 天"
    )
    destination_text = "、".join(item.display_name for item in destinations)
    document = TripReportDocument(
        title=_public_report_title(
            f"{destination_text} {itinerary.duration_days} 日旅行计划"
        ),
        overview=f"{date_text}，前往{destination_text}的可执行旅行方案。",
        destinations=destinations,
        duration_days=itinerary.duration_days,
        cost_summary=itinerary.cost_summary,
        cost_coverage_statement=cost_coverage_statement(itinerary.cost_summary),
        days=days,
        selections=selections,
        weather=weather_days,
        highlights=itinerary.highlights,
        important_notes=list(
            dict.fromkeys(
                [
                    *_trip_level_requirements(workspace),
                    *_long_distance_path_notes(workspace, generated_at),
                    *itinerary.important_notes,
                ]
            )
        ),
    )
    return TripReportProjection(
        source_workspace_revision=workspace.workspace_revision,
        source_fact_data_revision=facts.fact_data_revision,
        source_weather_data_revision=weather.weather_data_revision,
        status="ready",
        document=document,
        citations=citations,
        generated_at=generated_at,
    )


def project_map(
    workspace: TripWorkspaceV2,
    facts: FactStoreSnapshot,
    citations: Sequence[PublicCitationProjection],
) -> MapProjection:
    _by_fact, by_entity = _citation_lookup(citations)
    fact_index = {fact.fact_assertion_id: fact for fact in facts.fact_assertions}
    itinerary = workspace.itinerary
    places: list[MapPlaceProjection] = []
    for entity, entity_type, entity_id, name, place_id in [
        *[
            (item, EntityType.VISIT_STOP, item.item_id, item.name, item.place_id)
            for item in itinerary.visit_stops
        ],
        *[
            (item, EntityType.DINING_STOP, item.item_id, item.name, item.place_id)
            for item in itinerary.dining_stops
        ],
        *[
            (item, EntityType.LODGING_STAY, item.stay_id, item.name, item.place_id)
            for item in itinerary.lodging_stays
        ],
    ]:
        latitude, longitude = entity_coordinates(entity, fact_index)
        places.append(
            MapPlaceProjection(
                entity_ref=EntityRef(entity_type=entity_type, entity_id=entity_id),
                name=name,
                place_id=place_id,
                latitude=latitude,
                longitude=longitude,
                citation_ids=by_entity.get(entity_id, []),
            )
        )
    routes = [
        MapRouteProjection(
            entity_ref=EntityRef(
                entity_type=EntityType.TRANSPORT_LEG, entity_id=leg.transport_leg_id
            ),
            transport_class=leg.transport_class,
            selected_mode=leg.selected_mode,
            route_status=leg.route_status,
            from_endpoint=leg.from_endpoint,
            to_endpoint=leg.to_endpoint,
            segments=leg.segments if leg.route_status == "ready" else [],
            citation_ids=by_entity.get(leg.transport_leg_id, []),
        )
        for leg in itinerary.transport_legs
        if leg.route_status == "ready"
    ]
    return MapProjection(
        source_workspace_revision=workspace.workspace_revision,
        content=MapProjectionContent(places=places, routes=routes),
    )


def project_source_index(
    facts: FactStoreSnapshot,
    citations: Sequence[PublicCitationProjection],
) -> SourceIndexProjection:
    return SourceIndexProjection(
        source_fact_data_revision=facts.fact_data_revision,
        content=SourceIndexDocument(citations=list(citations)),
    )


def build_delivery_projections(
    workspace: TripWorkspaceV2,
    facts: FactStoreSnapshot,
    weather: WeatherContextSnapshot,
    *,
    generated_at: datetime,
    historical_weather_days: frozenset[tuple[str, date]] = frozenset(),
) -> tuple[TripReportProjection, MapProjection, SourceIndexProjection]:
    report = project_report(
        workspace,
        facts,
        weather,
        generated_at=generated_at,
        historical_weather_days=historical_weather_days,
    )
    map_projection = project_map(workspace, facts, report.citations)
    source_index = project_source_index(facts, report.citations)
    return report, map_projection, source_index
