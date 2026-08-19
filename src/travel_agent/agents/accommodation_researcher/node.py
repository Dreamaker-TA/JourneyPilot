"""
Accommodation Researcher Agent 节点 (Domain Layer)

职责：查询住宿选项与预算
- 住宿调研：Tavily/Brave 检索（价格/可订性/点评）+ 汇率换算
- 汇率换算（Frankfurter）
- 从 destination_researcher 读取区域分布以推荐最优住宿区域

工具白名单：tavily-search, brave-search, currency-exchange-mcp
模型：fast（主要是结构化工具调用）
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from ...entities.state import TravelAgentState
from ...entities.research_domain import ResearchDomain
from ...entities.research_query_plan import ResearchQuery, ResearchQueryKind
from ...models.router import get_model_router
from ..utils import (
    append_recent_history,
    build_tool_context_from_state,
    compact_tool_content_for_model,
    exclude_tools,
    execute_tool,
    filter_tools_for_agent,
    get_available_tools,
    inject_agent_context,
    prioritize_recommended_tools,
    resolve_agent_assignment,
    resolve_scoped_research_output_key,
    streaming_react_loop,
)
from ..research_packet_output import (
    packet_candidate_limit,
    authoritative_tool_messages,
    authoritative_retry_source_records,
    build_authoritative_research_packet_metadata,
    build_failure_only_research_packet,
    format_research_packet_context,
    has_required_provider_place_selection,
    parse_or_repair_research_packet_output,
    provider_evidence_outcomes,
    provider_round_answered_empty,
    provider_round_capability_declared,
)
from ...entities.provider_evidence import parse_provider_evidence_assignments
from ...services.candidate_admission import provider_place_type_matches_candidate_kind
from ...services.constraint_applicability import active_hard_constraints, active_hard_constraint_ids
from ...services.destination_scope import annotate_destination_distance
from ...services.state_invalidation import generation_packet_key
from ...services.research_query_planner import queries_by_ids
from ...services.fallback_query_policy import (
    FallbackQueryPolicy,
    runtime_fallback_capacity,
)
from ...services.nominatim_place_search import is_concrete_lodging_place
from ..research_packet_prompt import build_research_packet_system_prompt
from ..worker_errors import format_worker_last_error
from ...entities.place_identity import stable_place_id_amap_poi
from ...utils.coordinates import amap_location_to_wgs84
from ...utils.brief_helpers import build_assignment_context
from .prompts import TASK_TEMPLATE

if TYPE_CHECKING:
    from ...api.sse_buffer import SSEBuffer

logger = logging.getLogger(__name__)

_NODE_NAME = "accommodation_researcher"
# 失败日志里 last_error 的单行上限：供应商偶尔把整个响应体塞进异常消息，
# 分类前缀在开头，截断尾部不影响门的取证。
_LAST_ERROR_LOG_LIMIT = 600
# 住宿调研：多源网页检索协同，4 轮平衡覆盖与延迟
_MAX_TOOL_ITERATIONS = 4


def scope_accommodation_gap_tools(
    available_tools: List[Dict[str, Any]],
    recommended_tools: List[str],
    *,
    scoped_retry: bool,
) -> List[Dict[str, Any]]:
    """Keep a scoped lodging retry on the Gate-selected search providers.

    The Gate's recommendation list is the only thing that decides *which* providers
    a targeted round may use; this function only intersects it with the policy set.
    It used to decide as well, by returning ``global_place_search`` alone whenever
    that tool was recommended — which made every targeted lodging round
    Nominatim-only, while :func:`discover_deterministic_hotels` right below routes a
    CN destination to amap and to nothing else ("国内酒店在 OpenStreetMap 覆盖极差",
    and "Neither provider is ever retried through the other").  So a CN targeted
    lodging round had no lodging provider at all.  Two tables answering "which
    provider covers this destination" is one table too many.
    """
    if not scoped_retry or not recommended_tools:
        return available_tools
    recommended_set = set(recommended_tools)
    scoped = [
        tool
        for tool in available_tools
        if tool.get("schema", {}).get("function", {}).get("name", "")
        in recommended_set
    ]
    return scoped or available_tools


# ---------------------------------------------------------------------------
# 高德 POI 确定性住宿落地（deterministic amap lodging grounding）
#
# 国内酒店在 OpenStreetMap 覆盖极差，模型驱动的 global_place_search 常常对国内城市
# 落地 0 家具体酒店。这里镜像 transport 的 12306 预检：在代码里调用高德 POI 搜索，
# 把结果投影成一份 global_place_search 形状的信封注入消息，交给既有的 place-selection
# 修复路径把它物化成已落地的 LodgingCandidate —— 完全不依赖模型输出。
# ---------------------------------------------------------------------------

# 高德「住宿服务」POI typecode（1001xx 宾馆酒店 / 1002xx 旅馆招待所 / 100000 住宿服务）
_AMAP_LODGING_TYPECODE_PREFIXES = ("1001", "1002")
_AMAP_LODGING_TYPECODE_EXACT = {"100000"}
_AMAP_HOTELS_PER_CITY = 5


def _extract_tool_text(envelope: Mapping[str, Any]) -> str:
    """Collect any text payload from an MCP tool envelope, defensively."""
    parts: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if node.get("type") == "text" and isinstance(text, str):
                parts.append(text)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(envelope.get("sanitized_result"))
    if not parts:
        summary = envelope.get("result_summary")
        if isinstance(summary, str):
            parts.append(summary)
    return "\n".join(parts)


def _is_amap_lodging_typecode(typecode: str) -> bool:
    for code in str(typecode or "").split("|"):
        code = code.strip()
        if code in _AMAP_LODGING_TYPECODE_EXACT or code.startswith(
            _AMAP_LODGING_TYPECODE_PREFIXES
        ):
            return True
    return False


def _amap_lodging_type_label(typecode: str) -> str:
    """Project an amap 住宿 typecode into a label carrying a lodging marker.

    Candidate admission classifies lodging by the '酒店/旅馆' marker in
    provider_place_type, not by amap's numeric typecode, so we keep a readable
    marker while preserving the raw code for audit.
    """
    first = ""
    for code in str(typecode or "").split("|"):
        code = code.strip()
        if code:
            first = code
            break
    if first.startswith("1002"):
        label = "旅馆招待所"
    elif first.startswith("1001"):
        label = "宾馆酒店"
    else:
        label = "住宿服务·酒店"
    return f"{label}（amap typecode {typecode}）"


# Extract each POI's id/name/address/typecode tuple in order. Uses [^{}] so a
# match never crosses a POI's nested `photos` object or the next POI boundary;
# this survives the gateway's 900-char string cap (which breaks a strict
# json.loads), exactly as the rail parser regexes its truncated get-tickets text.
_AMAP_POI_RE = re.compile(
    r'"id"\s*:\s*"(?P<id>[^"]+)"'
    r'[^{}]*?"name"\s*:\s*"(?P<name>[^"]+)"'
    r'[^{}]*?"address"\s*:\s*"(?P<address>[^"]*)"'
    r'[^{}]*?"typecode"\s*:\s*"(?P<typecode>[^"]+)"',
    re.DOTALL,
)
_AMAP_LOCATION_RE = re.compile(
    r'"location"\s*:\s*"(?P<lng>-?\d+(?:\.\d+)?)\s*,\s*(?P<lat>-?\d+(?:\.\d+)?)"'
)


def _lodging_place_record(
    poi_id: str,
    name: str,
    address: str,
    typecode: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Project one amap POI into a global_place_search-shaped lodging record."""
    poi_id, name, address, typecode = (
        poi_id.strip(),
        name.strip(),
        address.strip(),
        typecode.strip(),
    )
    if not poi_id or not name or not address or not typecode:
        return None
    if not _is_amap_lodging_typecode(typecode):
        return None
    place_id = stable_place_id_amap_poi(poi_id)
    if place_id is None:
        return None
    record: Dict[str, Any] = {
        "place_id": place_id,
        "provider": "amap",
        "provider_place_type": _amap_lodging_type_label(typecode),
        "provider_country_code": "cn",
        "name": name,
        "address": address,
    }
    # The point geometry is optional: a record without it still grounds a real
    # lodging identity, it just cannot contribute a map pin.
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        record["latitude"] = float(latitude)
        record["longitude"] = float(longitude)
    return record


def _hotels_from_pois(pois: Any) -> List[Dict[str, Any]]:
    """Project the mcp_manager-normalized amap ``pois`` list into place records."""
    hotels: List[Dict[str, Any]] = []
    for poi in pois or []:
        if not isinstance(poi, dict):
            continue
        latitude = poi.get("latitude")
        longitude = poi.get("longitude")
        if latitude is None or longitude is None:
            latitude, longitude = amap_location_to_wgs84(poi.get("location"))
        record = _lodging_place_record(
            str(poi.get("id") or ""),
            str(poi.get("name") or ""),
            str(poi.get("address") or ""),
            str(poi.get("typecode") or ""),
            latitude if isinstance(latitude, (int, float)) else None,
            longitude if isinstance(longitude, (int, float)) else None,
        )
        if record is not None:
            hotels.append(record)
        if len(hotels) >= _AMAP_HOTELS_PER_CITY:
            break
    return hotels


def _parse_amap_hotels(text: str) -> List[Dict[str, Any]]:
    """Regex fallback: recover lodging POIs from raw amap text (truncation-safe)."""
    hotels: List[Dict[str, Any]] = []
    source = text or ""
    for match in _AMAP_POI_RE.finditer(source):
        # The identity fields and the "location" field can appear in any order
        # within a POI object, so search the whole enclosing object rather than
        # only the identity span.  Truncation-safe: fall back to a bounded window
        # when the object's closing brace was cut off.
        object_start = source.rfind("{", 0, match.start())
        object_end = source.find("}", match.end())
        object_span = source[
            object_start if object_start != -1 else match.start():
            object_end if object_end != -1 else match.end() + 200
        ]
        location_match = _AMAP_LOCATION_RE.search(object_span)
        latitude, longitude = (
            amap_location_to_wgs84(
                f"{location_match.group('lng')},{location_match.group('lat')}"
            )
            if location_match is not None
            else (None, None)
        )
        record = _lodging_place_record(
            match.group("id"),
            match.group("name"),
            match.group("address"),
            match.group("typecode"),
            latitude,
            longitude,
        )
        if record is not None:
            hotels.append(record)
        if len(hotels) >= _AMAP_HOTELS_PER_CITY:
            break
    return hotels


def _build_place_search_envelope(
    *,
    results: List[Dict[str, Any]],
    provider: str,
    server_name: str,
    audit_id: str,
    retrieved_at: str,
) -> Dict[str, Any]:
    """Wrap deterministically bound lodging places as a Provider place envelope.

    Reuses the recognized global_place_search tool_name so the existing
    place-selection repair binds these records identically no matter which
    provider (amap for CN, Nominatim elsewhere) supplied them.  ``audit_id`` and
    ``retrieved_at`` are always carried over from the real Gateway result, so the
    compiled SourceRecord points at an auditable provider call.
    """
    return {
        "tool_name": "global_place_search",
        "server_name": server_name,
        "status": "success",
        "audit_id": audit_id,
        "retrieved_at": retrieved_at,
        "metadata": {"evidence_allowed": True},
        "sanitized_result": {
            "success": True,
            "provider": provider,
            "results": results,
        },
    }


async def _enrich_amap_hotel_coordinates(
    hotels: List[Dict[str, Any]],
    *,
    available_tools: List[Dict[str, Any]],
    tool_context: Dict[str, Any],
) -> None:
    """Resolve each amap lodging POI's point geometry via maps_search_detail.

    amap ``maps_text_search`` returns no coordinates, so a bound hotel would land
    on the map without a pin.  ``maps_search_detail`` maps a POI id to its
    ``location`` ("lng,lat").  Best-effort and in place: a hotel whose detail
    lookup fails simply stays pin-less rather than blocking the deterministic
    lodging binding.
    """
    for hotel in hotels:
        if hotel.get("latitude") is not None and hotel.get("longitude") is not None:
            continue
        place_id = str(hotel.get("place_id") or "")
        poi_id = place_id.removeprefix("amap:poi:")
        if not poi_id or poi_id == place_id:
            continue
        try:
            detail = await execute_tool(
                "maps_search_detail",
                {"id": poi_id},
                available_tools=available_tools,
                node_name=_NODE_NAME,
                activation_source="amap_hotel_detail",
                **tool_context,
            )
        except Exception:
            logger.warning(
                "[accommodation_researcher] amap detail lookup failed for %s",
                poi_id,
                exc_info=True,
            )
            continue
        if not isinstance(detail, Mapping):
            continue
        location: Any = None
        sanitized = detail.get("sanitized_result")
        if isinstance(sanitized, Mapping):
            location = sanitized.get("location")
        if location is None:
            text = _extract_tool_text(detail)
            if text:
                try:
                    location = json.loads(text).get("location")
                except (json.JSONDecodeError, AttributeError):
                    location = None
        latitude, longitude = amap_location_to_wgs84(location)
        if latitude is not None and longitude is not None:
            hotel["latitude"] = latitude
            hotel["longitude"] = longitude


_NOMINATIM_PLACE_SEARCH_LIMIT = 10
_NOMINATIM_HOTELS_PER_CITY = 5


def _nominatim_hotels_from_results(results: List[Any]) -> List[Dict[str, Any]]:
    """Keep the concrete lodging-typed Provider records, in Provider order.

    Provider relevance order is preserved verbatim and truncated from the front,
    so the bound set is a pure function of the payload — no set or dict iteration
    decides which hotels a run gets.
    """
    hotels: List[Dict[str, Any]] = []
    bound_place_ids: List[str] = []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        record = dict(item)
        place_id = str(record.get("place_id") or "").strip()
        if not place_id or place_id in bound_place_ids:
            continue
        # Every identity field the place binder reads must be present verbatim;
        # a record missing one of them cannot ground a LodgingCandidate.
        if not all(
            isinstance(record.get(field), str) and record[field].strip()
            for field in (
                "provider",
                "provider_place_type",
                "provider_country_code",
                "name",
                "address",
            )
        ):
            continue
        if not provider_place_type_matches_candidate_kind(
            record["provider_place_type"],
            "lodging",
        ):
            continue
        if not is_concrete_lodging_place(record):
            continue
        hotels.append(record)
        bound_place_ids.append(place_id)
        if len(hotels) >= _NOMINATIM_HOTELS_PER_CITY:
            break
    return hotels


async def _bind_amap_lodging(
    dest: Mapping[str, Any],
    *,
    messages: List[Dict[str, Any]],
    available_tools: List[Dict[str, Any]],
    tool_context: Dict[str, Any],
    authoritative_tool_results: List[Dict[str, Any]],
    query_text: str,
) -> bool:
    """Bind one CN destination's lodging identity from real amap POI data.

    Takes the whole controlled destination, not just its name, because the records
    this injects go into the same selection enum a ``global_place_search`` answer
    does — and that enum drops an out-of-destination option by reading the
    record's own ``destination_distance_km``.  amap's ``city=`` is a *prefecture*:
    ``苏州市酒店`` legitimately answers with a hotel in 张家港, 80 km out.  Without
    the annotation that hotel stays selectable, gets picked, and admission then
    rejects it — and admission can only reject, it cannot re-pick, so the lodging
    domain goes to zero.
    """
    dest_name = str(dest.get("name") or "").strip()
    try:
        env = await execute_tool(
            "maps_text_search",
            {"keywords": query_text, "city": dest_name},
            available_tools=available_tools,
            node_name=_NODE_NAME,
            activation_source="amap_hotel_search",
            **tool_context,
        )
    except Exception:
        logger.warning(
            "[accommodation_researcher] amap hotel discovery failed for %s",
            dest_name,
            exc_info=True,
        )
        return False
    if not isinstance(env, Mapping) or env.get("status") != "success":
        logger.info(
            "[accommodation_researcher] amap search for %s returned status=%s error=%s",
            dest_name,
            (env.get("status") if isinstance(env, Mapping) else type(env).__name__),
            (env.get("error") if isinstance(env, Mapping) else None),
        )
        # Keep the refusal on the round's transcript.  This binder answers its
        # caller with a bare bool, so dropping the envelope here was the whole
        # difference between "a provider refused us" and "this city has no
        # hotels": the packet compiler reads failures off this list, and without
        # one the Gate gets no signature, re-runs the same exhausted key, and
        # attributes the empty lodging domain to the city.
        if isinstance(env, Mapping):
            authoritative_tool_results.append(dict(env))
        return False
    # Prefer the mcp_manager-normalized structured records (survive sanitization);
    # fall back to regexing the raw text if the normalizer did not run.
    sanitized = env.get("sanitized_result")
    records = sanitized.get("results") if isinstance(sanitized, Mapping) else None
    if isinstance(records, list):
        hotels = _hotels_from_pois(records)
    else:
        hotels = _parse_amap_hotels(_extract_tool_text(env))
    # amap text search carries no geometry; resolve each bound hotel's point
    # location so the map projection can pin it.
    await _enrich_amap_hotel_coordinates(
        hotels,
        available_tools=available_tools,
        tool_context=tool_context,
    )
    # After the coordinates are on the records and before they reach the enum:
    # this is the only moment that holds both the place and the destination it was
    # searched for.
    annotate_destination_distance(
        hotels,
        destination_latitude=dest.get("latitude"),
        destination_longitude=dest.get("longitude"),
    )
    audit_id = str(env.get("audit_id") or "").strip()
    logger.info(
        "[accommodation_researcher] amap search for %s: %d lodging POIs, audit_id=%s",
        dest_name, len(hotels), bool(audit_id),
    )
    if not hotels or not audit_id:
        return False
    retrieved_at = str(
        env.get("retrieved_at")
        or datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    envelope = _build_place_search_envelope(
        results=hotels,
        provider="amap",
        server_name="amap-maps",
        audit_id=audit_id,
        retrieved_at=retrieved_at,
    )
    messages.append(
        {"role": "tool", "content": compact_tool_content_for_model(envelope)}
    )
    authoritative_tool_results.append(envelope)
    logger.info(
        "[accommodation_researcher] deterministic amap lodging bound: %d hotels for %s (e.g. %s)",
        len(hotels),
        dest_name,
        hotels[0]["name"],
    )
    return True


async def _bind_nominatim_lodging(
    dest: Mapping[str, Any],
    *,
    messages: List[Dict[str, Any]],
    available_tools: List[Dict[str, Any]],
    tool_context: Dict[str, Any],
    authoritative_tool_results: List[Dict[str, Any]],
    query_text: str,
) -> bool:
    """Bind one non-CN destination's lodging identity from real Nominatim data.

    The destination's own point goes into the call rather than being applied to the
    answer afterwards: the executor is the single writer of
    ``destination_distance_km``, and the selection enum drops an out-of-destination
    option by reading exactly that key.  ``hotel in <city>`` names an
    administrative area, and an administrative area is not a location — omitting
    the point left every hotel this path bound exempt from the destination-scope
    rule.
    """
    dest_name = str(dest.get("name") or "").strip()
    country_code = str(dest.get("country_code") or "").strip().lower()
    if not dest_name or len(country_code) != 2:
        logger.info(
            "[accommodation_researcher] nominatim skip: incomplete destination identity %s",
            dest,
        )
        return False
    try:
        env = await execute_tool(
            "global_place_search",
            {
                "query": query_text,
                "country_code": country_code,
                "limit": _NOMINATIM_PLACE_SEARCH_LIMIT,
                "destination_latitude": dest.get("latitude"),
                "destination_longitude": dest.get("longitude"),
            },
            available_tools=available_tools,
            node_name=_NODE_NAME,
            activation_source="nominatim_hotel_search",
            # A degraded free_web_search envelope carries no Provider place
            # identity, so it must never stand in for this binding — that
            # substitution is exactly how the non-CN lodging domain used to
            # collapse to zero candidates.
            allow_fallback=False,
            **tool_context,
        )
    except Exception:
        logger.warning(
            "[accommodation_researcher] nominatim hotel discovery failed for %s",
            dest_name,
            exc_info=True,
        )
        return False
    if not isinstance(env, Mapping) or env.get("status") != "success":
        logger.info(
            "[accommodation_researcher] nominatim search for %s returned status=%s",
            dest_name, (env.get("status") if isinstance(env, Mapping) else type(env).__name__),
        )
        return False
    sanitized = env.get("sanitized_result")
    records = sanitized.get("results") if isinstance(sanitized, Mapping) else None
    hotels = _nominatim_hotels_from_results(records if isinstance(records, list) else [])
    audit_id = str(env.get("audit_id") or "").strip()
    logger.info(
        "[accommodation_researcher] nominatim search for %s (%s): %d lodging places, audit_id=%s",
        dest_name, country_code, len(hotels), bool(audit_id),
    )
    if not hotels or not audit_id:
        return False
    retrieved_at = str(
        env.get("retrieved_at")
        or datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    envelope = _build_place_search_envelope(
        results=hotels,
        provider="nominatim",
        server_name="nominatim",
        audit_id=audit_id,
        retrieved_at=retrieved_at,
    )
    messages.append(
        {"role": "tool", "content": compact_tool_content_for_model(envelope)}
    )
    authoritative_tool_results.append(envelope)
    logger.info(
        "[accommodation_researcher] deterministic nominatim lodging bound: %d hotels for %s (e.g. %s)",
        len(hotels),
        dest_name,
        hotels[0]["name"],
    )
    return True


async def discover_deterministic_hotels(
    *,
    state: TravelAgentState,
    messages: List[Dict[str, Any]],
    available_tools: List[Dict[str, Any]],
    tool_context: Dict[str, Any],
    authoritative_tool_results: List[Dict[str, Any]],
    planned_queries: List[ResearchQuery],
    executed_query_ids: List[str],
) -> bool:
    """Ground lodging identity for every controlled destination from real Provider data.

    Provider routing is a per-destination either/or, never a fallback chain: a CN
    destination binds from amap POI data (OpenStreetMap barely covers domestic
    hotels), every other destination binds from Nominatim (which carries no usable
    CN lodging coverage).  Neither provider is ever retried through the other.

    Mixed-country trips therefore bind each leg through the provider that actually
    covers it, and every leg's outcome is named in the summary log line, so a leg
    whose provider came back empty is visible instead of silent.  One selectable
    lodging option is enough to skip the ReAct loop: the placement skeleton needs a
    grounded property somewhere before it can place any night, and the remaining
    legs stay a scoped Candidate Gate content gap rather than a whole-domain zero.
    """
    identity = state.controlled_trip_identity or {}
    if not isinstance(identity, Mapping):
        logger.info(
            "[accommodation_researcher] deterministic lodging skip: identity not a mapping (%s)",
            type(identity).__name__,
        )
        return False
    destinations = identity.get("destinations")
    if not isinstance(destinations, list) or not destinations:
        logger.info("[accommodation_researcher] deterministic lodging skip: no destinations")
        return False
    available_names = {
        tool.get("schema", {}).get("function", {}).get("name", "")
        for tool in available_tools
    }
    bound: List[str] = []
    unbound: List[str] = []
    fallback_policy = FallbackQueryPolicy()
    for dest in destinations:
        if not isinstance(dest, Mapping):
            continue
        dest_name = str(dest.get("name") or "").strip()
        if not dest_name:
            continue
        destination_queries = [
            query
            for query in planned_queries
            if query.destination_id == str(dest.get("place_id") or "")
        ]
        primary_queries = [
            query
            for query in destination_queries
            if query.query_kind
            in {
                ResearchQueryKind.INTENT_PRIMARY,
                ResearchQueryKind.STRUCTURAL,
                ResearchQueryKind.TARGETED_REPAIR,
            }
        ]
        fallback_queries = [
            query
            for query in destination_queries
            if query.query_kind is ResearchQueryKind.GENERIC_FALLBACK
        ]
        injected = False
        provider = (
            "amap"
            if str(dest.get("country_code") or "").lower() == "cn"
            else "nominatim"
        )
        for query in [*primary_queries, *fallback_queries]:
            if query.query_kind is ResearchQueryKind.GENERIC_FALLBACK:
                research_window_open, run_budget_available = runtime_fallback_capacity()
                if not fallback_policy.is_allowed(
                    query,
                    executed_query_ids=set(executed_query_ids),
                    admitted_candidate_count=0,
                    required_candidate_count=1,
                    research_window_open=research_window_open,
                    run_budget_available=run_budget_available,
                ):
                    continue
            if provider == "amap" and "maps_text_search" in available_names:
                executed_query_ids.append(query.query_id)
                injected = await _bind_amap_lodging(
                    dest,
                    messages=messages,
                    available_tools=available_tools,
                    tool_context=tool_context,
                    authoritative_tool_results=authoritative_tool_results,
                    query_text=query.query_text,
                )
            elif provider == "nominatim" and "global_place_search" in available_names:
                executed_query_ids.append(query.query_id)
                injected = await _bind_nominatim_lodging(
                    dest,
                    messages=messages,
                    available_tools=available_tools,
                    tool_context=tool_context,
                    authoritative_tool_results=authoritative_tool_results,
                    query_text=query.query_text,
                )
            if injected:
                break
        (bound if injected else unbound).append(f"{dest_name}/{provider}")
    logger.info(
        "[accommodation_researcher] deterministic lodging binding: bound=%s unbound=%s",
        bound or "-",
        unbound or "-",
    )
    if not bound:
        return False
    return has_required_provider_place_selection(
        authoritative_tool_messages(authoritative_tool_results),
        expected_worker=_NODE_NAME,
        required_candidate_kinds=["lodging"],
    )


def build_accommodation_task_prompt(
    *,
    task_desc: str,
    user_query: str,
    require_current_candidate: bool,
) -> str:
    """Prevent an identity repair from restarting generic area and budget research."""
    if require_current_candidate:
        return f"""这是 Candidate Gate 发起的住宿身份定向补研，不是新一轮住宿区域或预算推荐。

当前任务：{task_desc}
用户原始需求仅用于理解旅行背景：{user_query}

本轮硬性执行规则：
- 只补当前 gaps 指向的具体酒店身份字段；先读取上游 Research Packet 中对应 candidate_id、property_name 和缺失字段。
- 优先调用 global_place_search，并使用受控目的地 ISO 国家码；只接受 Provider 原始类别为 hotel/hostel/guest_house 等真实住宿实体的结果。
- 每个保留的 LodgingCandidate 必须有外部事实逐字支持 property_name、place_id、provider_place_type、provider_country_code 与完整街道地址 address；typed 字段、FactAssertion.asserted_value 与 FieldProvenance 必须一致。
- 只返回酒店标题、区域名或官网链接而没有地址的 place-search 结果不足以准入；继续使用推荐网页搜索查到包含真实地址的官方页或可靠外部页。
- 价格、房型或实时余房查不到时使用 null / needs_confirmation，不得因此把已核验的酒店名称和地址写成 unknown。普通网页或搜索摘要支持的价格只能设为 price_kind=reference_estimate；只有按本次日期和入住条件取得的实时 quote 才可设为 live_quote。reference_estimate 必须有同 entity_id 的价格 FactAssertion 支持，供用户作为“约”预算参考，绝不写成当前报价或可订。
- 动态保留 1 至 3 个满足上述合同的高质量酒店；没有任何完整身份候选就明确失败，不得自产地址、place_id、Provider 类别/国家或 evidence。
"""
    return TASK_TEMPLATE.format(task_desc=task_desc, user_query=user_query)


async def accommodation_researcher_node(
    state: TravelAgentState, config: RunnableConfig
) -> Dict[str, Any]:
    """住宿研究员节点：查询具体 property 并输出 Research Packet。"""
    router = get_model_router()
    llm = router.get_fast()
    stream_queue: Optional["SSEBuffer"] = config.get("configurable", {}).get("stream_queue")

    current_time = state.current_time or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    user_query = state.user_query or ""
    run_id = state.run_id

    upstream_packet_context = format_research_packet_context(state.research_packets)

    # ── 任务分配（支持精炼轮次 round suffix）─────────────────────────────
    output_key, assignment = resolve_agent_assignment(
        state.agent_assignments or {}, _NODE_NAME, state.refinement_count
    )
    research_brief_context = build_assignment_context(
        assignment=assignment,
        brief=state.research_brief,
        intent_spec=state.intent_spec,
        constraint_pack=state.constraint_pack,
    )
    task_desc = assignment["objective"]
    if state.research_query_plan is None:
        raise ValueError("accommodation research requires a Research Query Plan")
    planned_queries = [
        query
        for query in queries_by_ids(
            state.research_query_plan,
            assignment.get("research_query_ids") or [],
        )
        if query.domain is ResearchDomain.LODGING
    ]
    task_desc = (
        f"{task_desc}；研究查询："
        + "；".join(query.query_text for query in planned_queries)
    )[:1000]
    recommended_tools = assignment.get("recommended_tools", [])
    excluded_tools = assignment.get("excluded_tools", [])
    excluded_candidate_ids = assignment.get("excluded_candidate_ids")
    require_current_candidate = bool(assignment.get("require_current_candidate"))
    provider_assignments = parse_provider_evidence_assignments(
        assignment,
        expected_worker=_NODE_NAME,
        expected_run_id=run_id,
        expected_constraint_pack_revision=state.constraint_pack_revision,
    )
    output_key = resolve_scoped_research_output_key(
        state.research_packets,
        _NODE_NAME,
        output_key,
        scoped_retry=require_current_candidate,
    )

    # ── 构建 messages ─────────────────────────────────────────────────────
    active_constraint_ids = active_hard_constraint_ids(
        state.constraint_pack,
        worker_kind=_NODE_NAME,
    )
    active_constraints = active_hard_constraints(
        state.constraint_pack,
        worker_kind=_NODE_NAME,
    )
    system_content = build_research_packet_system_prompt(
        worker_kind=_NODE_NAME,
        run_id=run_id,
        task_id=output_key,
        constraint_pack_revision=state.constraint_pack_revision,
        fact_data_revision=state.fact_data_revision,
        current_time=current_time,
        research_brief_context=research_brief_context,
        candidate_limit=packet_candidate_limit(_NODE_NAME),
        upstream_packet_context=upstream_packet_context,
        active_constraint_ids=active_constraint_ids,
    )
    system_content = inject_agent_context(system_content, state, agent_label=_NODE_NAME)

    logger.info(
        "AccommodationResearcher 上下文注入: anchor=%s, preset=%s, district_ctx_len=%d",
        bool(state.session_anchor),
        bool(state.preset_context),
        len(upstream_packet_context),
    )

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
    append_recent_history(messages, state)
    messages.append({
        "role": "user",
        "content": build_accommodation_task_prompt(
            task_desc=task_desc,
            user_query=user_query,
            require_current_candidate=require_current_candidate,
        ),
    })

    # ── 工具准备 ──────────────────────────────────────────────────────────
    # A Candidate Gate repair uses its own bounded recommendation set within
    # this Worker's policy, independent of the initial Planner MCP subset.
    selected_servers = (
        []
        if require_current_candidate and recommended_tools
        else state.selected_mcp_servers or []
    )
    available_tools = await get_available_tools(selected_servers)
    available_tools = filter_tools_for_agent(available_tools, _NODE_NAME)
    available_tools = exclude_tools(available_tools, excluded_tools)
    available_tools = prioritize_recommended_tools(available_tools, recommended_tools)
    available_tools = scope_accommodation_gap_tools(
        available_tools,
        recommended_tools,
        scoped_retry=require_current_candidate,
    )

    tool_schemas = [t["schema"] for t in available_tools if "schema" in t]
    tool_cache = dict(state.tool_cache) if state.tool_cache else {}
    tool_context = build_tool_context_from_state(state)
    if state.planning_generation is None:
        raise ValueError("accommodation research requires a planning generation")
    generation_id = state.planning_generation.generation_id
    executed_query_ids: List[str] = []
    packet_state_key = generation_packet_key(output_key, generation_id)
    authoritative_packet_metadata = build_authoritative_research_packet_metadata(
        worker_kind=_NODE_NAME,
        run_id=run_id,
        generation_id=generation_id,
        intent_spec_revision=state.intent_spec_revision,
        research_query_plan_id=state.research_query_plan.query_plan_id,
        executed_queries=[],
        task_id=output_key,
        constraint_pack_revision=state.constraint_pack_revision,
        fact_data_revision=state.fact_data_revision,
        query_context={
            "objective": task_desc,
            "controlled_trip_identity": state.controlled_trip_identity or {},
            "research_round": state.refinement_count,
        },
        generated_at=datetime.datetime.now(datetime.timezone.utc),
    )

    # ── ReAct 循环 ────────────────────────────────────────────────────────
    authoritative_tool_results: List[Dict[str, Any]] = []
    # 定型步骤的墙钟起点；异常发生在定型之前时保持 None，不记一个假的耗时。
    finalize_started_at: Optional[float] = None
    try:
        # 确定性住宿落地（CN 走高德，其余走 Nominatim）：命中则跳过 ReAct 循环，
        # 交由 place-selection 修复物化候选。
        deterministic_places_ready = await discover_deterministic_hotels(
            state=state,
            messages=messages,
            available_tools=available_tools,
            tool_context=tool_context,
            authoritative_tool_results=authoritative_tool_results,
            planned_queries=planned_queries,
            executed_query_ids=executed_query_ids,
        )
        authoritative_packet_metadata["executed_query_ids"] = list(
            dict.fromkeys(executed_query_ids)
        )
        authoritative_packet_metadata["query_context"]["query_lineage"] = [
            query.model_dump(mode="json")
            for query in planned_queries
            if query.query_id in set(executed_query_ids)
        ]
        if deterministic_places_ready:
            raw_response = ""
        else:
            (
                raw_response,
                _tool_summaries,
                _pending,
                react_tool_results,
            ) = await streaming_react_loop(
                llm=llm,
                messages=messages,
                tool_schemas=tool_schemas,
                available_tools=available_tools,
                stream_queue=stream_queue,
                node_name=_NODE_NAME,
                max_iterations=_MAX_TOOL_ITERATIONS,
                can_ask_user=False,
                tool_cache=tool_cache,
                tool_context=tool_context,
            )
            authoritative_tool_results.extend(react_tool_results)

        finalize_started_at = time.perf_counter()
        packet = await parse_or_repair_research_packet_output(
            raw_response or "",
            expected_worker=_NODE_NAME,
            expected_run_id=run_id,
            llm=llm,
            context_messages=messages,
            authoritative_tool_results=authoritative_tool_results,
            excluded_candidate_ids=excluded_candidate_ids,
            require_current_candidate=require_current_candidate,
            expected_active_constraint_ids=active_constraint_ids,
            expected_active_constraints=active_constraints,
            authoritative_packet_metadata=authoritative_packet_metadata,
            authoritative_source_records=authoritative_retry_source_records(
                state.recommendation_catalog,
                expected_worker=_NODE_NAME,
                constraint_pack_revision=state.constraint_pack_revision,
                fact_data_revision=state.fact_data_revision,
            )
            if require_current_candidate
            else (),
        )

        logger.info(
            "AccommodationResearcher 完成，key=%s candidates=%d facts=%d sources=%d finalize_ms=%.0f",
            output_key, len(packet.candidates), len(packet.fact_assertions), len(packet.source_records),
            (time.perf_counter() - finalize_started_at) * 1000.0,
        )

        return {
            "messages": [AIMessage(content=f"已核验 {len(packet.candidates)} 个住宿候选")],
            "research_packets": {packet_state_key: packet},
            "agent_status": {output_key: "completed"},
            "tool_cache": tool_cache,
            "provider_evidence_outcomes": provider_evidence_outcomes(
                authoritative_tool_messages(authoritative_tool_results),
                expected_worker=_NODE_NAME,
                packet=packet,
                assignments=provider_assignments,
            ),
        }

    except Exception as e:
        if finalize_started_at is not None:
            logger.info(
                "AccommodationResearcher 交付定型中断，key=%s finalize_ms=%.0f",
                output_key, (time.perf_counter() - finalize_started_at) * 1000.0,
            )
        evidence_messages = authoritative_tool_messages(authoritative_tool_results)
        failure_packet = build_failure_only_research_packet(
            authoritative_packet_metadata=authoritative_packet_metadata,
            context_messages=evidence_messages,
        )
        outcomes = provider_evidence_outcomes(
            evidence_messages,
            expected_worker=_NODE_NAME,
            packet=failure_packet,
            assignments=provider_assignments,
        )
        # 写进 last_error 的是分类结论（provider_empty: / provider_capability: /
        # schema_gate: / provider_transient: / provider_deterministic:），门就是按它决定该域能否
        # 拿到补研预算；日志记原始异常会把这个判据丢掉。零命中成功应答不留失败
        # source，last_error 是唯一能带出诚实成因的通道。
        last_error = format_worker_last_error(
            e,
            provider_empty_round=provider_round_answered_empty(
                evidence_messages, outcomes=outcomes
            ),
            provider_capability_round=provider_round_capability_declared(
                evidence_messages, outcomes=outcomes
            ),
        )
        logger.error(
            "AccommodationResearcher 执行失败: worker=%s key=%s last_error=%s",
            _NODE_NAME, output_key, last_error[:_LAST_ERROR_LOG_LIMIT],
        )
        result = {
            "messages": [AIMessage(content="住宿 Research Packet 生成失败")],
            "last_error": last_error,
            "agent_status": {output_key: "failed"},
        }
        if failure_packet is not None:
            result["research_packets"] = {packet_state_key: failure_packet}
        result["provider_evidence_outcomes"] = outcomes
        return result


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
