"""
Transport Researcher Agent 节点 (Domain Layer)

职责：查询城际及市内交通方案
- 高铁查询（12306-train）/ 航班查询（duffel-flights: search_flights）
- 市内路线（builtin global_route_search：大陆走高德，其余地区走 MOTIS/Transitous，按坐标自动分派）
- 从 destination_researcher 的输出中读取景点分布，规划市内路线

工具白名单：12306-train, duffel-flights, amap-maps
模型：fast（主要是结构化工具调用）
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import time
from copy import deepcopy
from typing import Any, Dict, List, Mapping, Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from ...entities.state import TravelAgentState
from ...models.router import get_model_router
from ..utils import (
    append_recent_history,
    build_tool_context_from_state,
    compact_tool_content_for_model,
    execute_tool,
    exclude_tools,
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
    has_provider_route_selection_option,
    has_required_provider_route_selection_options,
    parse_or_repair_research_packet_output,
    provider_evidence_outcomes,
    provider_round_answered_empty,
    provider_round_capability_declared,
)
from ...entities.provider_evidence import (
    ProviderEvidenceScope,
    ProviderRouteLegScope,
    parse_provider_evidence_assignments,
)
from ...entities.itinerary_composition_v2 import MIN_LOCAL_TRANSFER_MINUTES
from ...services.constraint_applicability import active_hard_constraints, active_hard_constraint_ids
from ..research_packet_prompt import build_research_packet_system_prompt
from ..worker_errors import format_worker_last_error
from ...entities.place_identity import stable_place_id_12306
from ...entities.provider_reference_service import ProviderReferenceService
from ...services.authored_place_resolution import (
    AuthoredPlaceScope,
    AuthoredPlaceToolAccess,
    resolve_station_point,
)
from ...services.destination_scope import destination_points, is_within_destination
from ...services.rail_12306 import city_station_telecodes
from ...tools.registry import get_tool_registry
from ...tools.temporal import (
    TemporalPreflightStatus,
    evaluate_temporal_request,
)
from ...utils.brief_helpers import build_brief_context_for_agent
from .prompts import TASK_TEMPLATE

logger = logging.getLogger(__name__)

_NODE_NAME = "transport_researcher"
# 失败日志里 last_error 的单行上限：供应商偶尔把整个响应体塞进异常消息，
# 分类前缀在开头，截断尾部不影响门的取证。
_LAST_ERROR_LOG_LIMIT = 600
# 交通调研：12306/Duffel 航班/高德·Google 路线 多工具串联，
# 跨城市比价场景需要 5 轮迭代
_MAX_TOOL_ITERATIONS = 5
_EXPLICIT_CROSS_DAY = re.compile(
    r"跨日|跨夜|overnight|red[- ]?eye",
    re.IGNORECASE,
)


def requires_cross_day_long_distance(*values: str) -> bool:
    """Recognize only an explicit user/task contract, never infer a default."""

    return any(_EXPLICIT_CROSS_DAY.search(value or "") for value in values)


def constrain_cross_day_flight_tools(
    available_tools: List[Dict[str, Any]],
    *,
    required: bool,
) -> List[Dict[str, Any]]:
    """Make the explicit cross-day requirement unavoidable in tool calls."""

    if not required:
        return available_tools
    constrained: List[Dict[str, Any]] = []
    for tool in available_tools:
        if (
            tool.get("server_name") != "duffel-flights"
            or tool.get("schema", {}).get("function", {}).get("name")
            != "search_flights"
        ):
            constrained.append(tool)
            continue
        copied = deepcopy(tool)
        parameters = copied["schema"]["function"].setdefault(
            "parameters", {"type": "object"}
        )
        params_schema = parameters.setdefault("properties", {}).setdefault(
            "params", {"type": "object"}
        )
        params_schema.setdefault("properties", {})["require_cross_day"] = {
            "type": "boolean",
            "const": True,
            "description": "用户明确要求真实跨日或跨夜长途路线",
        }
        required_fields = params_schema.setdefault("required", [])
        if "require_cross_day" not in required_fields:
            required_fields.append("require_cross_day")
        constrained.append(copied)
    return constrained


def _containing_destination_id(
    state: TravelAgentState,
    latitude: float,
    longitude: float,
) -> Optional[str]:
    """Which controlled destination this point belongs to, or None.

    A station carries no ``destination_id`` of its own — it is an endpoint on a
    long-distance route, not a researched candidate — so the Day it faces is
    decided the only honest way available: by where it is.
    """
    for place_id, (dest_lat, dest_lon) in sorted(
        destination_points(state.controlled_trip_identity).items()
    ):
        if is_within_destination(latitude, longitude, dest_lat, dest_lon):
            return place_id
    return None


def _routable_connector_endpoints(
    state: TravelAgentState,
) -> List[Dict[str, Any]]:
    """Every located endpoint a connector gap may be routed from or to.

    Two kinds, because a Day's chain has two kinds of node.  Passed Visit/Dining
    candidates are the stops.  The **station** on a long-distance leg is the other
    one, and it was missing here for as long as this projection existed: the
    adjacency between a station and the traveller's first stop was silently
    dropped from the routable set, so the one leg most worth measuring shipped an
    invented duration in every single run.

    A station's point does not depend on which long-distance option was chosen, so
    a slot-bound admitted route is as good a source for it as the chosen one.

    There is a **third** kind of node: the Itinerary Planner may
    author a stop the researchers never proposed, and the server then resolves that
    authored name against a real place provider
    (``services/authored_place_resolution.py``).  Such a stop is located — it has a
    ``resolved_place_id`` and real coordinates — but it is **not** an admitted
    Candidate, so walking ``catalog.candidate_index()`` cannot see it.  Every
    connector gap naming it then reports "no located endpoint" forever, no matter
    how many retries run, and the hop ships an invented duration.
    ``placement_place_id`` is already authored-aware; this projection has to be too.

    The gap this closes had a **false negative baked into how it was watched for**:
    grepping ``no located endpoint for authored:…`` misses the common case.  A
    *successfully resolved* authored place answers with its provider id, so the line
    actually printed is ``no located endpoint for amap:poi:…``; the string
    ``authored:…`` only appears when the place stays *unresolved*, which is the rare
    case.  The projection therefore must locate by resolved place id as well.
    """
    catalog = state.recommendation_catalog
    if catalog is None:
        return []
    passed_ids = {
        admission.candidate_id
        for admission in catalog.admission_results
        if admission.status == "passed" and admission.selection_slot_id is None
    }
    passed_route_ids = {
        admission.candidate_id
        for admission in catalog.admission_results
        if admission.status == "passed"
    }
    sources = {
        source.source_record_id: source
        for packet in catalog.research_packets
        for source in packet.source_records
    }

    def provider_coordinates(candidate: Any) -> tuple[float, float] | None:
        def find(value: Any) -> tuple[float, float] | None:
            if isinstance(value, dict):
                if str(value.get("place_id") or "") == candidate.place_id:
                    latitude = value.get("latitude")
                    longitude = value.get("longitude")
                    if isinstance(latitude, (int, float)) and isinstance(
                        longitude,
                        (int, float),
                    ):
                        return float(latitude), float(longitude)
                for nested in value.values():
                    matched = find(nested)
                    if matched is not None:
                        return matched
            elif isinstance(value, list):
                for nested in value:
                    matched = find(nested)
                    if matched is not None:
                        return matched
            return None

        for source_id in candidate.source_record_ids:
            source = sources.get(source_id)
            if source is None:
                continue
            matched = find(source.snapshot)
            if matched is not None:
                return matched
        return None

    endpoints: List[Dict[str, Any]] = []
    for candidate in catalog.candidate_index().values():
        if (
            candidate.candidate_id in passed_route_ids
            and candidate.candidate_kind == "transport"
            and getattr(candidate, "transport_class", "") == "long_distance"
        ):
            for endpoint in (candidate.from_endpoint, candidate.to_endpoint):
                station_place_id = str(endpoint.place_id or "").strip()
                station_name = str(endpoint.name or "").strip()
                if (
                    not station_place_id
                    or not station_name
                    or endpoint.latitude is None
                    or endpoint.longitude is None
                ):
                    continue
                destination_id = _containing_destination_id(
                    state, endpoint.latitude, endpoint.longitude
                )
                if destination_id is None:
                    continue
                endpoints.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "destination_id": destination_id,
                        "name": station_name,
                        "place_id": station_place_id,
                        "latitude": float(endpoint.latitude),
                        "longitude": float(endpoint.longitude),
                    }
                )
            continue
        if (
            candidate.candidate_id not in passed_ids
            or candidate.candidate_kind not in {"visit", "dining"}
        ):
            continue
        place_id = str(getattr(candidate, "place_id", "") or "").strip()
        coordinates = provider_coordinates(candidate)
        name = str(
            getattr(candidate, "name", None)
            or getattr(candidate, "branch_name", None)
            or ""
        ).strip()
        if (
            not place_id
            or not name
            or coordinates is None
        ):
            continue
        latitude, longitude = coordinates
        endpoints.append(
            {
                "candidate_id": candidate.candidate_id,
                "destination_id": candidate.destination_id,
                "name": name,
                "place_id": place_id,
                "latitude": float(latitude),
                "longitude": float(longitude),
            }
        )
    # The third kind: authored-and-resolved placements on the skeleton (see the
    # docstring).  They carry no candidate id, so they are keyed by their own
    # resolved place id -- which is exactly the id the connector gap names.
    skeleton = getattr(state, "placement_skeleton", None)
    if skeleton is not None:
        seen_place_ids = {item["place_id"] for item in endpoints}
        for day in getattr(skeleton, "days", []) or []:
            for placement in getattr(day, "placements", []) or []:
                if getattr(placement, "candidate_id", None):
                    continue
                authored = getattr(placement, "authored_place", None)
                if authored is None or not getattr(authored, "is_located", False):
                    continue
                place_id = str(authored.resolved_place_id or "").strip()
                name = str(authored.name or "").strip()
                if not place_id or not name or place_id in seen_place_ids:
                    continue
                seen_place_ids.add(place_id)
                endpoints.append(
                    {
                        # No admitted candidate stands behind this stop; the place id
                        # is its identity, and the sort below needs a stable first key.
                        "candidate_id": f"authored:{place_id}",
                        "destination_id": day.destination_id,
                        "name": name,
                        "place_id": place_id,
                        "latitude": float(authored.resolved_latitude),
                        "longitude": float(authored.resolved_longitude),
                    }
                )
    # One long-distance candidate contributes two endpoints, so the candidate id alone
    # does not order this list.
    return sorted(endpoints, key=lambda item: (item["candidate_id"], item["place_id"]))


# ── Deterministic domestic rail (12306) grounding ──────────────────────────
# The intercity long-distance leg is fully determined by the controlled origin
# and destination cities plus the trip start date.  It must not depend on the
# fast model correctly chaining the 12306 station-code -> ticket tools (which it
# routinely fails to do, because Tool Search does not surface the prerequisite
# station-code tool).  We resolve station codes and real trains in code and hand
# the existing provider-route binder a structured route it turns into a grounded
# TransportCandidate, exactly the way global_route_search results are bound.

_HIGH_SPEED_PREFIXES = {"G", "D", "C"}

# A same-day round-trip return should leave a livestayable middle (at least one
# visit + a meal + the transfer buffers), so it should depart a few hours after
# the outbound leg landed rather than the earliest feasible instant.  Applied as a
# soft preference in ``_select_best_train`` (fall back to earliest-usable when no
# such train exists).
MIN_SAME_DAY_RETURN_STAY_MINUTES = 3 * 60

# The tools this deterministic path calls, and therefore the whole allowlist the
# Tool Gateway enforces on it.  Deliberately not the Worker's model-facing tool
# list: this path has no model in the loop, so a model-facing recommendation
# (``recommended_tools`` / ``excluded_tools`` from Candidate Gate) must not be
# able to amputate it.
_DOMESTIC_RAIL_TOOL_NAMES = frozenset({"get-tickets", "get-station-code-of-citys"})

# Locating the station is a different provider answering a different question —
# 12306 names the station, a place provider says where it is — so it gets its own
# gateway allowlist rather than widening the rail one.
_STATION_POINT_TOOL_NAMES = frozenset({"maps_text_search", "maps_search_detail"})

# The same contract for the other deterministic path in this module: the
# placement-adjacency connector gap (``discover_required_local_route``).  It runs
# no model either, so its gateway allowlist is this constant rather than the
# Worker's model-facing ``available_tools``.
_LOCAL_ROUTE_TOOL_NAMES = frozenset({"global_route_search"})

# How many adjacencies one connector round routes.  The local-route domain is
# granted a single targeted research round, so this number *is* the ceiling on
# Provider-measured connectors in a delivered itinerary — which is why it may not
# be 1, as it effectively was while this path read ``connector_gaps[0]``.
#
# It also may not be unbounded, but the reason is narrower than it first looked.
# Each answered route enters the Worker's message list (~1.3 KB per bounded route
# record, measured), and that is the only cost: the Research Packet's route
# candidates are harvested **from the tool envelopes** by
# ``_successful_route_records``, and the model's own output is just a list of
# ``route_id`` strings, so answering more adjacencies does not push against the
# 4,096-token output ceiling that killed the whole transport Worker — it
# only lengthens the prompt.  Twelve adjacencies is ~16 KB of prompt, which covers
# a two-week itinerary's local legs; anything beyond it is logged rather than
# silently missing.
_MAX_CONNECTOR_ROUTE_QUERIES_PER_ROUND = 12

# Every field ``_build_rail_route_envelope`` and the selection policy read out of
# one ``get-tickets`` record.  The provider-side contract is
# ``services/rail_12306.py::build_ticket_record``.
_RAIL_RECORD_TEXT_FIELDS = (
    "train_code",
    "from_name",
    "from_code",
    "to_name",
    "to_code",
    "departure_time",
    "arrival_time",
)


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


def _normalize_cn_city_for_rail(name: str) -> str:
    """把带行政级别后缀的中文城市名收敛成 12306 认得的裸城市名。

    OSM 身份给的是"上海市 / 深圳市 / 北京市"这类全名，而 12306 的
    get-station-code-of-citys 只认"上海 / 深圳 / 北京"，带后缀会返回"未检索到城市"。
    """
    text = (name or "").strip()
    for suffix in ("特别行政区", "市", "省", "地区", "盟"):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


# Major-city station telecodes used when live 12306 station-code lookup fails.
# Prefer high-speed hubs first (虹桥/北/南), then classic stations. Values are
# 12306 telecodes (not OSM ids).
_CN_CITY_STATION_TELECODES: Dict[str, tuple[str, ...]] = {
    "上海": ("AOH", "SHH"),  # 虹桥 (HSR) → 上海站
    "深圳": ("IOQ", "SZQ"),  # 深圳北 (HSR) → 深圳站
    "北京": ("VNP", "BJP"),  # 北京南 (HSR) → 北京站
    "广州": ("IZQ", "GZQ"),  # 广州南 → 广州站
    "杭州": ("HGH", "HZH"),  # 杭州东 → 杭州站
    "南京": ("NJH", "NKH"),
    "成都": ("ICW", "CDW"),  # 成都东 → 成都
    "武汉": ("WHN",),
    "西安": ("XAY",),
    "重庆": ("CUW", "CQW"),  # 重庆西/重庆
    "天津": ("TIP", "TJP"),
    "苏州": ("SZH",),
    "厦门": ("XMS",),
    "长沙": ("CSQ",),
    "郑州": ("ZAF", "ZZF"),
    "青岛": ("QDK",),
    "大连": ("DFT", "DLT"),
    "昆明": ("KMM",),
    "福州": ("FYS", "FZS"),
    "济南": ("JNK",),
    "合肥": ("HFH",),
    "南昌": ("NCG",),
    "石家庄": ("SJP",),
    "哈尔滨": ("HBB",),
    "沈阳": ("SYT",),
    "宁波": ("NGH",),
    "无锡": ("WXH",),
    "东莞": ("RTQ",),
    "佛山": ("FSQ",),
    "珠海": ("ZIQ",),
}


def _builtin_station_telecode(city_name: str) -> Optional[str]:
    """Return the preferred (first) builtin station telecode for a CN city."""
    bare = _normalize_cn_city_for_rail(city_name)
    codes = _CN_CITY_STATION_TELECODES.get(bare) or _CN_CITY_STATION_TELECODES.get(
        city_name.strip()
    )
    return codes[0] if codes else None


def _builtin_station_telecode_candidates(city_name: str) -> List[str]:
    """Return ordered station telecodes to try for get-tickets (HSR hubs first)."""
    bare = _normalize_cn_city_for_rail(city_name)
    codes = _CN_CITY_STATION_TELECODES.get(bare) or _CN_CITY_STATION_TELECODES.get(
        city_name.strip()
    )
    return list(codes) if codes else []


def _parse_station_codes(text: str) -> Dict[str, str]:
    """Parse {city_name: station_code} from get-station-code-of-citys output."""
    codes: Dict[str, str] = {}
    for match in re.finditer(
        r'"([^"]+)"\s*:\s*\{[^{}]*?"station_code"\s*:\s*"([A-Za-z0-9]+)"', text
    ):
        codes[match.group(1)] = match.group(2)
    # Alternate shapes occasionally emitted by MCP text wrappers.
    for match in re.finditer(
        r"([^\s,;:：\"']{2,12})\s*[：:]\s*([A-Za-z]{3})",
        text,
    ):
        city, code = match.group(1).strip(), match.group(2).upper()
        if city and code not in codes.values():
            codes.setdefault(city, code)
    return codes


def _rail_train_records(envelope: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Read the structured ``get-tickets`` rows out of a Tool Gateway envelope.

    The provider answers with a flat ``results`` list built by
    ``services/rail_12306.py::build_left_ticket_payload`` and lifted out of the
    MCP TextContent by ``tools/mcp_manager.py::_normalized_rail_result``; nothing
    here re-parses prose.  Rows the sanitizer added or mangled are not trains and
    are skipped: it appends a ``{"truncated_count": n}`` sentinel past its list
    cap and would drop a 13th key while stamping ``truncated: True``.
    """
    result = envelope.get("sanitized_result")
    if not isinstance(result, Mapping):
        return []
    rows = result.get("results")
    if not isinstance(rows, list):
        return []
    trains: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("truncated") is True:
            continue
        train = {field: str(row.get(field) or "").strip() for field in _RAIL_RECORD_TEXT_FIELDS}
        duration = row.get("duration_minutes")
        price = row.get("min_price_cny")
        if not all(train.values()) or not isinstance(duration, int):
            continue
        train["duration_minutes"] = duration
        train["min_price_cny"] = (
            float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) else None
        )
        trains.append(train)
    return trains


def _rail_station_scope(city_name: str, requested_code: str) -> frozenset[str]:
    """Every station telecode that legitimately represents one trip endpoint.

    12306 expands a query to the whole city station group, so asking
    ``from_station=AOH`` (上海虹桥) also returns trains departing 上海南, 上海松江
    and 金山北.  The city group comes from the committed official station table
    (city is field ``[7]``), so this holds for all 428 cities rather than only
    the high-speed hubs listed in ``_CN_CITY_STATION_TELECODES``.  The exact
    requested station is always in scope: it is the endpoint we asked for.
    """
    return frozenset(
        city_station_telecodes(_normalize_cn_city_for_rail(city_name))
        | {requested_code}
    )


def _filter_rail_trains_by_station_scope(
    trains: List[Dict[str, Any]],
    *,
    from_scope: frozenset[str],
    to_scope: frozenset[str],
) -> List[Dict[str, Any]]:
    """Drop city-group rows whose endpoints are not the trip's endpoints.

    The provider already dropped rows outside the *queried stations'* groups
    (``services/rail_12306.py::build_left_ticket_payload``).  This is the other
    question, and only this layer can ask it: whether a row's endpoints are inside
    the cities of the **controlled trip identity** we are binding a leg for.  The
    requested station is always in scope, so this can never empty an answer that
    contains the pair we asked for.
    """

    return [
        train
        for train in trains
        if str(train.get("from_code") or "") in from_scope
        and str(train.get("to_code") or "") in to_scope
    ]


def _select_best_train(
    trains: List[Dict[str, Any]],
    *,
    requested_from: Optional[str] = None,
    requested_to: Optional[str] = None,
    leg_role: Optional[str] = None,
    depart_after: Optional[str] = None,
    same_day_roundtrip: bool = False,
) -> Optional[Dict[str, Any]]:
    """Select by explicit product policy, never by Provider return order.

    The requested station pair outranks duration.  Without that term the
    duration minimum picks whichever station in the city group happens to sit
    closest to the destination — for 上海虹桥 → 杭州东 that is 金山北, a suburban
    station ~60 km from central Shanghai.  A city sibling is still selected when
    the requested station has no service on the pair (上海 → 兰州 arrives 兰州西),
    because a real leg beats no leg.

    Order: bookable fare > requested station pair > high speed > shortest 历时 >
    earliest departure > train code.  The provider already drops rows with no
    bookable class, so the first term is normally satisfied by every row; it stays
    because it is the policy, not an artefact of who filters where.

    ``leg_role`` in ``{"return", "inter_destination"}`` adds one product rule on
    top: the deterministic "earliest departure" tie-break
    must not hand a departure leg a pre-dawn time that empties its own departure
    day.  A day with no legal local window makes the whole run fail at
    composition (``composition_repair_budget_exhausted`` → ``DeliveryContractViolation``),
    which is far worse than taking a slightly later train.  This applies to any
    leg that *leaves a city the traveller has been staying in* — returning home
    (``return``) or moving between destinations (``inter_destination``).
    **Prefer a departure at or after 08:00 local** (earliest such train wins, keeping the
    rest of the policy); only when every candidate departs before 08:00 do we
    fall back to the ordinary ordering — a real leg still beats no leg.
    """

    if not trains:
        return None

    if depart_after:
        # A same-day round-trip return must leave a livestayable middle, so it
        # should depart a few hours after the inbound leg landed.  This is a
        # *preference*: restrict to the
        # trains that depart at/after this clock when any qualify, otherwise fall
        # back to the ordinary policy (which already keeps a return at/after
        # 08:00), so an early-only date still produces a real leg instead of none.
        after_minutes = _rail_time_to_minutes(depart_after)
        late_trains = [
            train
            for train in trains
            if _rail_time_to_minutes(str(train.get("departure_time") or "")) >= after_minutes
        ]
        if not late_trains:
            logger.info(
                "[transport_researcher] depart_after=%s after_min=%d candidates=%d late=%d -> FALLBACK",
                depart_after,
                after_minutes,
                len(trains),
                len(late_trains),
            )
        if late_trains:
            trains = late_trains

    if same_day_roundtrip and leg_role not in ("return", "inter_destination"):
        # A same-day round trip needs the whole day: pick the earliest feasible
        # departure so the traveller lands as early as possible and the return
        # (a departure leg kept at/after the stay window) still leaves a usable
        # middle.  The ordinary 'best' rule would happily pick a mid-afternoon
        # train and leave the day empty.
        return min(
            trains,
            key=lambda t: _rail_time_to_minutes(str(t.get("departure_time") or "23:59")),
        )

    def is_high_speed(train: Mapping[str, Any]) -> bool:
        return str(train.get("train_code") or "")[:1] in _HIGH_SPEED_PREFIXES

    def endpoint_rank(train: Mapping[str, Any]) -> int:
        matched = 0
        if requested_from and str(train.get("from_code") or "") == requested_from:
            matched += 1
        if requested_to and str(train.get("to_code") or "") == requested_to:
            matched += 1
        return -matched

    def policy_key(train: Mapping[str, Any]) -> tuple:
        return (
            0 if train.get("min_price_cny") is not None else 1,
            endpoint_rank(train),
            0 if is_high_speed(train) else 1,
            int(train.get("duration_minutes") or 10**9),
            _rail_time_to_minutes(str(train.get("departure_time") or "23:59")),
            str(train.get("train_code") or ""),
        )

    if leg_role in {"return", "inter_destination"}:
        # A departure at/after 08:00 leaves a usable window on the day the
        # traveller leaves a city they have been staying in (a return home or
        # an inter-city move inside a multi-destination trip).
        # Prefer the earliest such train under the ordinary policy ordering;
        # only fall back to the ordinary ordering when every departure is
        # pre-dawn.
        day_window_candidates = [
            train
            for train in trains
            if _rail_time_to_minutes(str(train.get("departure_time") or "")) >= 8 * 60
        ]
        if day_window_candidates:
            return min(day_window_candidates, key=policy_key)

    return min(trains, key=policy_key)


def _rail_time_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _clock_plus_buffer(clock: str, buffer_minutes: int) -> str:
    """Return an ``HH:MM`` clock shifted forward by ``buffer_minutes``.

    Used for the same-day round-trip constraint: the return train must
    depart no earlier than the outbound arrival plus the local transfer buffer.
    Wraps past midnight (a rail arrival at 23:40 + 30 min buffer = 00:10 next
    day, which is still a legal "not before" bound).
    """
    try:
        total = _rail_time_to_minutes(clock) + int(buffer_minutes or 0)
    except (TypeError, ValueError):
        return clock
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def _rail_constraint_params(constraint_pack: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for constraint in active_hard_constraints(
        constraint_pack,
        worker_kind=_NODE_NAME,
    ):
        if constraint.get("category") != "transport_constraint":
            continue
        params = constraint.get("params")
        if isinstance(params, Mapping):
            merged.update(params)
    return merged


def _filter_rail_trains(
    trains: List[Dict[str, Any]],
    constraint_params: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Apply canonical transport hard constraints before anchor selection."""

    earliest = str(constraint_params.get("earliest_departure_local") or "")
    latest = str(constraint_params.get("latest_arrival_local") or "")
    avoid_overnight = constraint_params.get("avoid_overnight") is True
    filtered: List[Dict[str, Any]] = []
    for train in trains:
        departure = _rail_time_to_minutes(str(train["departure_time"]))
        arrival = _rail_time_to_minutes(str(train["arrival_time"]))
        if earliest and departure < _rail_time_to_minutes(earliest):
            continue
        if latest and arrival > _rail_time_to_minutes(latest):
            continue
        if avoid_overnight and arrival < departure:
            continue
        filtered.append(train)
    return filtered


def _rail_iso(date_str: str, clock: str) -> str:
    hours, minutes = clock.split(":")
    return f"{date_str}T{int(hours):02d}:{int(minutes):02d}:00+08:00"


def _rail_station_tool_access(tool_context: Dict[str, Any]) -> AuthoredPlaceToolAccess:
    """Bind the station-locating ladder to this run's audited tool gateway.

    Same chokepoint as every other tool call in the run — the allowlist, the
    provider snapshot cache, the research window and the durable
    ``tool_execution_audits`` row.  The allowlist is this step's own two tools
    rather than the Worker's model-facing list, for the reason the whole
    deterministic rail path states: there is no model in this loop, so a
    model-facing tool recommendation must not be able to amputate it.
    """

    async def execute(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await execute_tool(
            tool_name,
            arguments,
            allowed_tool_names=_STATION_POINT_TOOL_NAMES,
            node_name=_NODE_NAME,
            activation_source="rail_station_point",
            allow_fallback=False,
            **tool_context,
        )

    def has_tool(tool_name: str) -> bool:
        return get_tool_registry().has_tool(tool_name)

    return AuthoredPlaceToolAccess(execute=execute, has_tool=has_tool)


async def _locate_rail_station(
    *,
    telecode: str,
    station_name: str,
    city: str,
    city_identity: Mapping[str, Any],
    tool_context: Dict[str, Any],
    point_cache: Dict[str, Optional[tuple[float, float]]],
) -> Optional[tuple[float, float]]:
    """Where one 12306 station is, so the hop to the first stop can be routed.

    Cached per telecode: a round trip touches the same two stations twice and the
    answer cannot differ between the outbound and the return leg.  A station no
    provider could place returns None and is cached as such — the leg still binds
    (a train is a train without a point on the map), and the connector that needed
    it stays authored and says so.
    """
    code = str(telecode or "").strip().upper()
    if not code or not str(station_name or "").strip():
        return None
    if code in point_cache:
        return point_cache[code]
    latitude = city_identity.get("latitude")
    longitude = city_identity.get("longitude")
    scope = AuthoredPlaceScope(
        country_code=str(city_identity.get("country_code") or "").strip().casefold()
        or None,
        destination_place_id=str(city_identity.get("place_id") or "").strip() or None,
        latitude=float(latitude) if isinstance(latitude, (int, float)) else None,
        longitude=float(longitude) if isinstance(longitude, (int, float)) else None,
    )
    try:
        point = await resolve_station_point(
            station_name=str(station_name),
            city=str(city),
            scope=scope,
            tools=_rail_station_tool_access(tool_context),
        )
    except Exception:
        logger.warning(
            "[transport_researcher] station point lookup failed telecode=%s name=%s",
            code,
            station_name,
            exc_info=True,
        )
        point = None
    if point is None:
        logger.warning(
            "[transport_researcher] no provider could place station %s (%s); "
            "its local connector stays authored",
            station_name,
            code,
        )
    point_cache[code] = point
    return point


def _build_rail_route_envelope(
    *,
    train: Mapping[str, Any],
    start_date: str,
    audit_id: str,
    retrieved_at: str,
    from_point: Optional[tuple[float, float]],
    to_point: Optional[tuple[float, float]],
) -> Dict[str, Any]:
    """Wrap one real 12306 train as a structured provider-route envelope.

    The snapshot is authored here (not the truncated Tool Gateway payload) so the
    strict transport verified-value validator finds each asserted field verbatim.
    """
    departure_at = _rail_iso(start_date, train["departure_time"])
    arrival_date = start_date
    if _rail_time_to_minutes(train["arrival_time"]) < _rail_time_to_minutes(
        train["departure_time"]
    ):
        arrival_date = (
            datetime.date.fromisoformat(start_date) + datetime.timedelta(days=1)
        ).isoformat()
    arrival_at = _rail_iso(arrival_date, train["arrival_time"])
    mode = (
        "high_speed_rail"
        if str(train["train_code"])[:1] in _HIGH_SPEED_PREFIXES
        else "train"
    )
    # Stable place_id contract: entities.place_identity (12306:{telecode}).
    # Stations have no OSM id; connector materialization still needs both ends.
    from_place_id = stable_place_id_12306(train["from_code"])
    to_place_id = stable_place_id_12306(train["to_code"])
    if from_place_id is None or to_place_id is None:
        raise ValueError("12306 train missing station telecode for stable place_id")
    from_endpoint = {
        "name": train["from_name"],
        "station_code": train["from_code"],
        "place_id": from_place_id,
    }
    to_endpoint = {
        "name": train["to_name"],
        "station_code": train["to_code"],
        "place_id": to_place_id,
    }
    # The point is what makes this endpoint routable at all; the identity above is
    # still the telecode, and stays it whichever provider found the point.
    if from_point is not None:
        from_endpoint["latitude"], from_endpoint["longitude"] = from_point
    if to_point is not None:
        to_endpoint["latitude"], to_endpoint["longitude"] = to_point
    # The representative fare is the cheapest bookable class, computed provider
    # side as ``min_price_cny`` (``yp_info_new`` is ordered most-expensive-first,
    # so the first class is 商务座 and over-reports the fare ~3.5x).
    cost = train.get("min_price_cny")
    segment: Dict[str, Any] = {
        "segment_id": f"seg_{train['train_code']}",
        "mode": mode,
        "from_endpoint": dict(from_endpoint),
        "to_endpoint": dict(to_endpoint),
        "departure_at": departure_at,
        "arrival_at": arrival_at,
        "duration_minutes": train["duration_minutes"],
        "service_number": train["train_code"],
        "operator_name": "中国铁路",
    }
    route: Dict[str, Any] = {
        "route_id": train["train_code"],
        "transport_class": "long_distance",
        "selected_mode": mode,
        "from_endpoint": dict(from_endpoint),
        "to_endpoint": dict(to_endpoint),
        "departure_at": departure_at,
        "arrival_at": arrival_at,
        "duration_minutes": train["duration_minutes"],
        "segments": [segment],
    }
    if cost is not None:
        segment["cost_cny"] = cost
        route["total_cost_cny"] = cost
    return {
        "tool_name": "domestic_rail_search",
        "server_name": "12306-train",
        "status": "success",
        "audit_id": audit_id,
        "retrieved_at": retrieved_at,
        "metadata": {"evidence_allowed": True},
        "sanitized_result": {"success": True, "provider": "12306", "routes": [route]},
    }


async def _bind_one_rail_leg(
    *,
    state: TravelAgentState,
    messages: List[Dict[str, Any]],
    tool_context: Dict[str, Any],
    authoritative_tool_results: List[Dict[str, Any]],
    reference_services: List[ProviderReferenceService],
    scoped_leg: ProviderRouteLegScope,
    identity_by_place_id: Mapping[str, Mapping[str, Any]],
    station_code_cache: Dict[str, str],
    station_point_cache: Dict[str, Optional[tuple[float, float]]],
    now: Optional[datetime.datetime],
    depart_after: Optional[str] = None,
    same_day_roundtrip: bool = False,
    bound_arrivals: Optional[Dict[str, Optional[str]]] = None,
) -> bool:
    """Ground exactly one assigned long-distance leg from real 12306 data.

    Returns whether this leg produced an evidence-bearing Provider route.  Leg
    readiness is never the round's readiness: the caller judges every assigned
    scope together.

    This path takes no ``available_tools``: it runs no model, so the Worker's
    model-facing tool list has no say in whether it may run.  Availability is the
    registry's answer (was the tool registered by this deployment at all) and the
    gateway allowlist is this path's own ``_DOMESTIC_RAIL_TOOL_NAMES``.
    """
    route_origin = identity_by_place_id.get(scoped_leg.from_place_id)
    route_destination = identity_by_place_id.get(scoped_leg.to_place_id)
    if route_origin is None or route_destination is None:
        logger.warning(
            "[transport_researcher] skip domestic rail: exact route endpoints are uncontrolled"
        )
        return False
    if (
        str(route_origin.get("country_code") or "").lower() != "cn"
        or str(route_destination.get("country_code") or "").lower() != "cn"
    ):
        logger.info("[transport_researcher] skip domestic rail: not a CN↔CN trip")
        return False
    origin_name = str(route_origin.get("name") or "").strip()
    dest_name = str(route_destination.get("name") or "").strip()
    dest_place_id = str(route_destination.get("place_id") or "").strip()
    start_date = scoped_leg.service_date.isoformat()
    if (
        not origin_name
        or not dest_name
        or origin_name == dest_name
        or not start_date
        or not dest_place_id
    ):
        logger.warning(
            "[transport_researcher] skip domestic rail: incomplete identity "
            "origin=%r dest=%r date=%r place_id=%r",
            origin_name,
            dest_name,
            start_date,
            bool(dest_place_id),
        )
        return False
    temporal = evaluate_temporal_request(
        tool_name="get-tickets",
        server_name="12306-train",
        arguments={"date": start_date},
        now=now,
    )
    temporal_envelope: Optional[Dict[str, Any]] = None
    if temporal.status != TemporalPreflightStatus.EXECUTABLE:
        # Execute only the local Gateway preflight for the requested date.  The
        # policy returns before Registry/MCP execution, producing a durable
        # no-retry/no-fallback audit for the unsupported target date.
        temporal_result = await execute_tool(
            "get-tickets",
            {
                "date": start_date,
                "fromStation": "TEMPORAL_PREFLIGHT",
                "toStation": "TEMPORAL_PREFLIGHT",
            },
            allowed_tool_names=_DOMESTIC_RAIL_TOOL_NAMES,
            node_name=_NODE_NAME,
            activation_source="domestic_rail_temporal_preflight",
            allow_fallback=False,
            **tool_context,
        )
        if isinstance(temporal_result, dict):
            temporal_envelope = temporal_result
        if temporal.status == TemporalPreflightStatus.NOT_APPLICABLE:
            if temporal_envelope is not None:
                messages.append(
                    {
                        "role": "tool",
                        "content": compact_tool_content_for_model(
                            temporal_envelope
                        ),
                    }
                )
                authoritative_tool_results.append(temporal_envelope)
            return False
    # Whether this deployment registered the tool at all — the only honest reason
    # a code path with no model in it cannot call 12306.  Reading the Worker's
    # model-facing ``available_tools`` here is what let one real provider failure
    # amputate the whole deterministic path: Candidate Gate put ``get-tickets`` on
    # ``excluded_tools``, the Worker filtered it out of the *model's* list, and
    # this branch then declined to run at all.
    registry = get_tool_registry()
    if not registry.has_tool("get-tickets"):
        logger.warning(
            "[transport_researcher] skip domestic rail: get-tickets is not registered"
        )
        return False
    # 12306 只认不带行政级别后缀的城市名（"上海市"→"上海"、"深圳市"→"深圳"）；
    # OSM 身份给的是带"市/省"的全名，直接传会返回"未检索到城市"。
    query_origin = _normalize_cn_city_for_rail(origin_name)
    query_dest = _normalize_cn_city_for_rail(dest_name)
    rail_constraint_params = _rail_constraint_params(state.constraint_pack)

    async def resolve_station_codes() -> tuple[Optional[str], Optional[str], str]:
        """Return (from_code, to_code, source) with live MCP preferred over table."""
        from_code: Optional[str] = None
        to_code: Optional[str] = None
        source = "none"
        cached_from = station_code_cache.get(query_origin)
        cached_to = station_code_cache.get(query_dest)
        if cached_from and cached_to:
            return cached_from, cached_to, "12306_mcp_cached"
        if registry.has_tool("get-station-code-of-citys"):
            try:
                code_env = await execute_tool(
                    "get-station-code-of-citys",
                    {"citys": f"{query_origin}|{query_dest}"},
                    allowed_tool_names=_DOMESTIC_RAIL_TOOL_NAMES,
                    node_name=_NODE_NAME,
                    activation_source="domestic_rail_station_code",
                    # Deterministic binding must not accept free_web_search
                    # station-code forgeries.
                    allow_fallback=False,
                    **tool_context,
                )
            except Exception:
                logger.warning(
                    "[transport_researcher] 12306 station-code tool raised",
                    exc_info=True,
                )
                code_env = None
            if isinstance(code_env, Mapping) and code_env.get("status") == "success":
                codes = _parse_station_codes(_extract_tool_text(code_env))
                from_code = codes.get(query_origin) or codes.get(origin_name)
                to_code = codes.get(query_dest) or codes.get(dest_name)
                if from_code and to_code:
                    station_code_cache[query_origin] = from_code
                    station_code_cache[query_dest] = to_code
                    return from_code, to_code, "12306_mcp"
                logger.warning(
                    "[transport_researcher] 12306 未返回站码: origin=%r dest=%r keys=%s",
                    query_origin,
                    query_dest,
                    list(codes.keys())[:8],
                )
            else:
                status = code_env.get("status") if isinstance(code_env, Mapping) else None
                logger.warning(
                    "[transport_researcher] 12306 station-code status=%s (no web fallback)",
                    status,
                )
        else:
            logger.warning(
                "[transport_researcher] get-station-code-of-citys not registered; "
                "try builtin table"
            )

        from_code = from_code or _builtin_station_telecode(query_origin)
        to_code = to_code or _builtin_station_telecode(query_dest)
        if from_code and to_code:
            return from_code, to_code, "builtin_table"
        return from_code, to_code, source

    try:
        from_code, to_code, code_source = await resolve_station_codes()
        if not from_code or not to_code:
            logger.warning(
                "[transport_researcher] no station telecodes for %r → %r (source=%s)",
                query_origin,
                query_dest,
                code_source,
            )
            return False
        if code_source == "builtin_table":
            logger.info(
                "[transport_researcher] using builtin station telecodes %s/%s for %s→%s",
                from_code,
                to_code,
                query_origin,
                query_dest,
            )

        # Try the builtin high-speed hub combinations first, then the live MCP pair.
        #
        # **The order matters: builtin hubs first, live MCP pair second.**  The loop below
        # stops at the first pair that yields a selectable train, and live
        # ``get-station-code-of-citys`` answers a bare city name with the *classic* city
        # station (上海 → SHH 上海站, 杭州 → HZH 杭州站).  That pair does return trains, so
        # putting it first means the HSR hubs ``_CN_CITY_STATION_TELECODES`` deliberately
        # lists (上海 → AOH 虹桥, 杭州 → HGH 杭州东) are never reached — the whole table goes
        # dead for exactly the major cities it exists for.  On 上海→杭州 or 虹桥, the classic-station
        # trains out of 上海站 take 2.2x the journey time of the hub trains (G7359 17:51 98min
        # ¥94 out of 上海站 versus G4917 05:44 45min ¥83 out of 虹桥 on the same date).
        # Note this is not a ranking bug to fix downstream: 12306 expands a query to the
        # whole city group, so G4917 *is* in the SHH result set, but ``_select_best_train``
        # ranks the exact requested pair above duration and G7359 is the only exact
        # SHH→HZH match.  Fixing the requested pair is the only place this can be fixed
        # without weakening that ranking rule.
        #
        # Cities absent from the table keep their previous behaviour: the candidate
        # helper returns nothing, so both loops fall back to the live MCP code.
        station_pairs: List[tuple[str, str]] = []
        for left in _builtin_station_telecode_candidates(query_origin) or [from_code]:
            for right in _builtin_station_telecode_candidates(query_dest) or [to_code]:
                pair = (left, right)
                if pair not in station_pairs:
                    station_pairs.append(pair)
        # The live pair still has to be reachable: one endpoint may be a table city
        # while the other is not, so the combinations above need not contain it.
        if (from_code, to_code) not in station_pairs:
            station_pairs.append((from_code, to_code))

        # An out-of-window target date is never sent to 12306.  The optional
        # reference query uses only the exact supported edge date and can never
        # become an exact route for the requested travel date.
        ticket_dates = [
            (
                temporal.reference_date.isoformat()
                if temporal.status == TemporalPreflightStatus.REFERENCE_ONLY
                and temporal.reference_date is not None
                else start_date
            )
        ]

        def compliant_rail_trains(
            env: Mapping[str, Any], pair: tuple[str, str]
        ) -> List[Dict[str, Any]]:
            """Trains that are both constraint-compliant and on the trip's endpoints."""
            return _filter_rail_trains(
                _filter_rail_trains_by_station_scope(
                    _rail_train_records(env),
                    from_scope=_rail_station_scope(query_origin, pair[0]),
                    to_scope=_rail_station_scope(query_dest, pair[1]),
                ),
                rail_constraint_params,
            )

        ticket_env: Any = None
        used_pair: Optional[tuple[str, str]] = None
        used_query_date: Optional[str] = None
        for query_date in ticket_dates:
            for pair in station_pairs:
                env = await execute_tool(
                    "get-tickets",
                    {
                        "date": query_date,
                        "fromStation": pair[0],
                        "toStation": pair[1],
                    },
                    allowed_tool_names=_DOMESTIC_RAIL_TOOL_NAMES,
                    node_name=_NODE_NAME,
                    activation_source="domestic_rail_tickets",
                    allow_fallback=False,
                    **tool_context,
                )
                if not isinstance(env, Mapping) or env.get("status") != "success":
                    logger.warning(
                        "[transport_researcher] get-tickets status=%s from=%s to=%s "
                        "date=%s err=%s",
                        env.get("status") if isinstance(env, Mapping) else None,
                        pair[0],
                        pair[1],
                        query_date,
                        (env.get("error") if isinstance(env, Mapping) else None),
                    )
                    continue
                compliant_trains = compliant_rail_trains(env, pair)
                if (
                    _select_best_train(
                        compliant_trains,
                        requested_from=pair[0],
                        requested_to=pair[1],
                        leg_role=scoped_leg.leg_role,
                        depart_after=depart_after,
                        same_day_roundtrip=same_day_roundtrip,
                    )
                    is None
                ):
                    logger.warning(
                        "[transport_researcher] get-tickets empty trains from=%s to=%s date=%s",
                        pair[0],
                        pair[1],
                        query_date,
                    )
                    continue
                ticket_env = env
                used_pair = pair
                used_query_date = query_date
                break
            if ticket_env is not None:
                break
    except Exception:
        logger.warning(
            "[transport_researcher] deterministic 12306 rail discovery failed",
            exc_info=True,
        )
        return False
    if ticket_env is None or used_pair is None or used_query_date is None:
        if temporal_envelope is not None:
            messages.append(
                {
                    "role": "tool",
                    "content": compact_tool_content_for_model(temporal_envelope),
                }
            )
            authoritative_tool_results.append(temporal_envelope)
        logger.warning(
            "[transport_researcher] all get-tickets attempts failed for %s→%s on %s",
            query_origin,
            query_dest,
            start_date,
        )
        return False
    train = _select_best_train(
        compliant_rail_trains(ticket_env, used_pair),
        requested_from=used_pair[0],
        requested_to=used_pair[1],
        leg_role=scoped_leg.leg_role,
        depart_after=depart_after,
        same_day_roundtrip=same_day_roundtrip,
    )
    audit_id = str(ticket_env.get("audit_id") or "").strip()
    if train is None or not audit_id:
        logger.warning(
            "[transport_researcher] no parseable trains or audit_id for %s→%s",
            used_pair[0],
            used_pair[1],
        )
        return False
    if bound_arrivals is not None:
        bound_arrivals[scoped_leg.leg_role] = str(train.get("arrival_time") or "")
    logger.info(
        "[transport_researcher] bound %s depart_after=%s chosen=%s dep=%s arr=%s",
        scoped_leg.leg_role,
        depart_after,
        str(train.get("train_code") or ""),
        str(train.get("departure_time") or ""),
        str(train.get("arrival_time") or ""),
    )
    retrieved_at = str(
        ticket_env.get("retrieved_at")
        or datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    if temporal.status == TemporalPreflightStatus.REFERENCE_ONLY:
        if temporal_envelope is None:
            raise RuntimeError("rail temporal reference decision omitted its audit envelope")
        temporal_envelope["result_summary"] = temporal.user_message
        temporal_envelope["sanitized_result"] = {
            "success": True,
            "reference_kind": "latest_supported_day",
            "requested_date": start_date,
            "reference_date": used_query_date,
            "reference_train": {
                "train_no": train["train_code"],
                "from_name": train["from_name"],
                "to_name": train["to_name"],
                "departure_time": train["departure_time"],
                "arrival_time": train["arrival_time"],
                "duration_minutes": train["duration_minutes"],
                "lowest_observed_price_cny": train.get("min_price_cny"),
            },
            "claims_not_confirmed_for_requested_date": [
                "train_no",
                "departure_time",
                "arrival_time",
                "price",
                "inventory",
            ],
        }
        temporal_envelope.setdefault("metadata", {})
        temporal_envelope["metadata"].update(
            {
                "reference_tool_audit_id": audit_id,
                "requested_date": start_date,
                "reference_date": used_query_date,
                "reference_kind": "latest_supported_day",
                "evidence_allowed": False,
            }
        )
        messages.append(
            {
                "role": "tool",
                "content": compact_tool_content_for_model(temporal_envelope),
            }
        )
        authoritative_tool_results.append(temporal_envelope)
        # The envelope keeps ``evidence_allowed=False`` and compiles into no
        # source, so this service can never support a fact or a candidate.  The
        # record below is the one channel that reaches the traveller: a sentence
        # naming the real train and the claims the supplier did not carry across
        # dates.  Read entities/provider_reference_service.py before widening it.
        reference_services.append(
            ProviderReferenceService(
                leg_role=scoped_leg.leg_role,
                from_name=train["from_name"],
                to_name=train["to_name"],
                service_number=train["train_code"],
                departure_time=train["departure_time"],
                arrival_time=train["arrival_time"],
                duration_minutes=train.get("duration_minutes"),
                lowest_observed_price_cny=train.get("min_price_cny"),
                requested_date=datetime.date.fromisoformat(start_date),
                reference_date=datetime.date.fromisoformat(used_query_date),
                unconfirmed_claims=list(
                    temporal_envelope["sanitized_result"][
                        "claims_not_confirmed_for_requested_date"
                    ]
                ),
            )
        )
        logger.info(
            "[transport_researcher] retained 12306 reference only: query_date=%s "
            "requested_date=%s train=%s",
            used_query_date,
            start_date,
            train["train_code"],
        )
        return False

    envelope = _build_rail_route_envelope(
        train=train,
        start_date=start_date,
        audit_id=audit_id,
        retrieved_at=retrieved_at,
        from_point=await _locate_rail_station(
            telecode=train["from_code"],
            station_name=train["from_name"],
            city=query_origin,
            city_identity=route_origin,
            tool_context=tool_context,
            point_cache=station_point_cache,
        ),
        to_point=await _locate_rail_station(
            telecode=train["to_code"],
            station_name=train["to_name"],
            city=query_dest,
            city_identity=route_destination,
            tool_context=tool_context,
            point_cache=station_point_cache,
        ),
    )
    messages.append(
        {"role": "tool", "content": compact_tool_content_for_model(envelope)}
    )
    authoritative_tool_results.append(envelope)
    logger.info(
        "[transport_researcher] deterministic 12306 rail bound: %s %s %s->%s %s "
        "(telecode_source=%s pair=%s query_date=%s)",
        scoped_leg.leg_role,
        train["train_code"],
        train["from_name"],
        train["to_name"],
        train["departure_time"],
        code_source,
        used_pair,
        used_query_date,
    )
    return True


async def discover_domestic_rail_route(
    *,
    state: TravelAgentState,
    messages: List[Dict[str, Any]],
    required_transport_classes: List[str] | None,
    connector_gaps: List[Dict[str, Any]] | None,
    tool_context: Dict[str, Any],
    authoritative_tool_results: List[Dict[str, Any]],
    reference_services: List[ProviderReferenceService],
    required_route_scopes: List[ProviderEvidenceScope],
    now: Optional[datetime.datetime] = None,
) -> bool:
    """Deterministically ground every assigned domestic intercity rail leg.

    A usable trip needs one real outbound path and one real return path, so each
    assigned long-distance scope is bound in its own 12306 round with its own
    service date.  Readiness stays the single all-scope judgement.

    No ``available_tools`` parameter on purpose — see :func:`_bind_one_rail_leg`.
    """
    classes = required_transport_classes or []
    # Only the initial intercity round or an explicit long-distance retry; local
    # connector rounds are owned by discover_required_local_route.
    if connector_gaps or (classes and "long_distance" not in classes):
        logger.info(
            "[transport_researcher] skip domestic rail: connector_gaps=%s classes=%s",
            bool(connector_gaps),
            classes,
        )
        return False
    identity = state.controlled_trip_identity or {}
    if not isinstance(identity, Mapping):
        logger.warning("[transport_researcher] skip domestic rail: no controlled_trip_identity")
        return False
    origin = identity.get("origin")
    destinations = identity.get("destinations")
    if not isinstance(origin, Mapping) or not isinstance(destinations, list) or not destinations:
        logger.warning("[transport_researcher] skip domestic rail: missing origin/destinations")
        return False
    scoped_legs = [
        scope.route_leg
        for scope in required_route_scopes
        if scope.transport_class == "long_distance" and scope.route_leg is not None
    ]
    if not scoped_legs:
        logger.warning(
            "[transport_researcher] skip domestic rail: no exact long-distance route scope"
        )
        return False
    identities = [
        item
        for item in [origin, *destinations]
        if isinstance(item, Mapping)
    ]
    identity_by_place_id = {
        str(item.get("place_id") or "").strip(): item
        for item in identities
        if str(item.get("place_id") or "").strip()
    }
    # One round trip queries the same two cities twice; the telecodes are reused
    # so the extra leg costs one get-tickets call, not a second station lookup.
    station_code_cache: Dict[str, str] = {}
    # Same reasoning for the station's point: the two legs of a round trip run
    # through the same two stations, which cannot have moved between them.
    station_point_cache: Dict[str, Optional[tuple[float, float]]] = {}
    bound_leg_roles: List[str] = []
    bound_arrivals: Dict[str, Optional[str]] = {}
    outbound_date = next(
        (
            leg.service_date
            for leg in scoped_legs
            if leg.leg_role == "outbound"
        ),
        None,
    )
    same_day_roundtrip = outbound_date is not None and any(
        leg.leg_role == "return" and leg.service_date == outbound_date
        for leg in scoped_legs
    )
    for scoped_leg in scoped_legs:
        depart_after: Optional[str] = None
        if (
            scoped_leg.leg_role == "return"
            and outbound_date is not None
            and scoped_leg.service_date == outbound_date
        ):
            # Same-day round trip: the return train must depart after the
            # outbound already arrived (plus the local transfer buffer), or the
            # traveller would leave before landing.
            outbound_arrival = bound_arrivals.get("outbound")
            if outbound_arrival:
                # A same-day round trip needs a livestayable middle, not the
                # earliest feasible return (~47 min later leaves no room for a
                # stop).  Prefer a return that departs several hours after the
                # outbound lands, so at least one visit+meal fits with the
                # transfer buffers; fall back to the minimum when no late return
                # exists on that date.
                depart_after = _clock_plus_buffer(
                    outbound_arrival, MIN_SAME_DAY_RETURN_STAY_MINUTES
                )
        if await _bind_one_rail_leg(
            state=state,
            messages=messages,
            tool_context=tool_context,
            authoritative_tool_results=authoritative_tool_results,
            reference_services=reference_services,
            scoped_leg=scoped_leg,
            identity_by_place_id=identity_by_place_id,
            station_code_cache=station_code_cache,
            station_point_cache=station_point_cache,
            now=now,
            depart_after=depart_after,
            same_day_roundtrip=same_day_roundtrip,
            bound_arrivals=bound_arrivals,
        ):
            bound_leg_roles.append(scoped_leg.leg_role)
    ready = has_required_provider_route_selection_options(
        authoritative_tool_messages(authoritative_tool_results),
        required_transport_classes=required_transport_classes,
        required_route_scopes=required_route_scopes,
    )
    logger.info(
        "[transport_researcher] domestic rail legs bound %d/%d (roles=%s ready=%s)",
        len(bound_leg_roles),
        len(scoped_legs),
        bound_leg_roles,
        ready,
    )
    return ready


async def discover_required_local_route(
    *,
    state: TravelAgentState,
    messages: List[Dict[str, Any]],
    required_transport_classes: List[str] | None,
    connector_gaps: List[Dict[str, Any]] | None,
    tool_context: Dict[str, Any],
    authoritative_tool_results: List[Dict[str, Any]],
) -> bool:
    """Query every exact placement-derived adjacency gap, never an arbitrary pair.

    This path takes no ``available_tools``: it runs no model, so the Worker's
    model-facing tool list has no say in whether it may run.  Availability is the
    registry's answer (was the tool registered by this deployment at all) and the
    gateway allowlist is this path's own ``_LOCAL_ROUTE_TOOL_NAMES``.

    **Every gap, not the first one.**  Reading ``connector_gaps[0]`` and dropping the
    rest is the whole coverage story, because the local-route domain is granted a single
    targeted research round: an itinerary with four local adjacencies gets one
    Provider-measured connector and three invented durations, no matter how well the
    binding downstream works.
    """
    required = [
        transport_class
        for transport_class in (required_transport_classes or [])
        if transport_class in {"public_transit", "flexible"}
    ]
    if (
        not required
        or "long_distance" in (required_transport_classes or [])
        or not connector_gaps
    ):
        return False
    # Whether this deployment registered the tool at all — the only honest reason
    # a code path with no model in it cannot route a connector gap.  Reading the
    # Worker's model-facing ``available_tools`` here would let one real provider
    # failure amputate the whole deterministic path: Candidate Gate puts
    # ``global_route_search`` on ``excluded_tools``, the Worker drops it from the
    # *model's* list, and this branch would then decline to run at all.
    if not get_tool_registry().has_tool("global_route_search"):
        logger.warning(
            "[transport_researcher] skip connector gap: "
            "global_route_search is not registered"
        )
        return False
    # Indexed by place id, which is the language both ends of this speak: a gap
    # names its two endpoints that way, and ``connector_candidate_quality_error``
    # decides whether the answered route filled it by comparing the same strings.
    # Keying on the placement key instead is what hid the gap — the station adjacency
    # had no placement-keyed entry, so it was dropped here rather than judged.
    endpoint_index: Dict[str, Dict[str, Any]] = {}
    for endpoint in _routable_connector_endpoints(state):
        endpoint_index.setdefault(endpoint["place_id"], endpoint)
    routable: List[Dict[str, Any]] = []
    unroutable: List[str] = []
    for gap in connector_gaps:
        from_place_id = str(gap.get("from_place_id") or "").strip()
        to_place_id = str(gap.get("to_place_id") or "").strip()
        origin = endpoint_index.get(from_place_id)
        destination = endpoint_index.get(to_place_id)
        departure_time = str(gap.get("departure_time") or "").strip()
        requested_mode = str(gap.get("requested_mode") or "").strip()
        reason = ""
        if origin is None or destination is None:
            reason = "no located endpoint for " + " and ".join(
                place_id
                for place_id, endpoint in (
                    (from_place_id, origin),
                    (to_place_id, destination),
                )
                if endpoint is None
            )
        elif origin["destination_id"] != destination["destination_id"]:
            reason = "endpoints sit in different controlled destinations"
        elif not departure_time:
            reason = "gap carries no departure time"
        elif requested_mode not in {
            "public_transit", "walk", "bike", "drive", "taxi", "ride_hailing"
        }:
            reason = f"requested mode {requested_mode!r} is not routable"
        if reason:
            # A dropped adjacency must leave a record: with nothing but a shrunken
            # ``routable=`` count, an adjacency nobody asked about is indistinguishable
            # from one no Provider could answer.  Both ship as an invented duration.
            unroutable.append(f"{gap.get('gap_id')}: {reason}")
            continue
        routable.append(
            {
                "from_name": origin["name"],
                "from_place_id": origin["place_id"],
                "from_latitude": origin["latitude"],
                "from_longitude": origin["longitude"],
                "to_name": destination["name"],
                "to_place_id": destination["place_id"],
                "to_latitude": destination["latitude"],
                "to_longitude": destination["longitude"],
                "departure_time": departure_time,
                "mode": requested_mode,
            }
        )
    if unroutable:
        logger.warning(
            "[transport_researcher] %d of %d connector gaps are not routable: %s",
            len(unroutable),
            len(connector_gaps),
            "; ".join(unroutable),
        )
    if not routable:
        return False
    queried = routable[:_MAX_CONNECTOR_ROUTE_QUERIES_PER_ROUND]
    if len(routable) > len(queried):
        # A bound that is not announced reads as full coverage in the delivered
        # itinerary, where a dropped adjacency is indistinguishable from one no
        # Provider could answer.
        logger.warning(
            "[transport_researcher] connector round bounded: querying %d of %d "
            "routable adjacencies; the rest stay authored this round",
            len(queried),
            len(routable),
        )
    for request in queried:
        envelope = await execute_tool(
            "global_route_search",
            request,
            allowed_tool_names=_LOCAL_ROUTE_TOOL_NAMES,
            node_name=_NODE_NAME,
            activation_source="placement_adjacency_connector_gap",
            # Deterministic binding must not accept a prose search result standing
            # in for provider route evidence.  ``global_route_search``
            # has no ``_FALLBACK_MAP`` entry today, so this changes nothing now and
            # forbids a future fallback edge from reaching this path.
            allow_fallback=False,
            **tool_context,
        )
        messages.append(
            {
                "role": "tool",
                "content": compact_tool_content_for_model(envelope),
            }
        )
        authoritative_tool_results.append(envelope)
    logger.info(
        "[transport_researcher] connector gaps queried %d/%d (routable=%d)",
        len(queried),
        len(connector_gaps),
        len(routable),
    )
    return has_provider_route_selection_option(
        authoritative_tool_messages(authoritative_tool_results),
        required_transport_classes=required_transport_classes,
    )


def scope_transport_gap_tools(
    available_tools: List[Dict[str, Any]],
    recommended_tools: List[str],
    required_transport_classes: List[str] | None,
    *,
    scoped_retry: bool = False,
) -> List[Dict[str, Any]]:
    """Expose the small recommended tool set directly for any scoped retry."""
    if not (required_transport_classes or scoped_retry) or not recommended_tools:
        return available_tools
    recommended_set = set(recommended_tools)
    # ``global_route_search`` only ever returns a local commuting itinerary, so a
    # long-distance responsibility must keep the Provider tools that can produce
    # intercity evidence.  Letting it take the allowlist alone strands the leg for
    # the model: it would be left with nothing that can answer an intercity
    # question (flights above all).  The deterministic 12306 binder is no longer
    # affected — it does not read this list at all.
    long_distance_scope = "long_distance" in (required_transport_classes or ())
    if not long_distance_scope:
        global_provider = [
            tool
            for tool in available_tools
            if tool.get("schema", {}).get("function", {}).get("name", "")
            == "global_route_search"
            and "global_route_search" in recommended_set
        ]
        if global_provider:
            return global_provider
    scoped = [
        tool
        for tool in available_tools
        if tool.get("schema", {}).get("function", {}).get("name", "")
        in recommended_set
    ]
    return scoped or available_tools


def scope_initial_transport_tools(
    available_tools: List[Dict[str, Any]],
    *,
    require_current_candidate: bool,
    connector_gaps: List[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    """Hide local-route providers until an exact placement gap exists."""
    if require_current_candidate or connector_gaps:
        return available_tools
    return [
        tool
        for tool in available_tools
        if (
            tool.get("schema", {}).get("function", {}).get("name", "")
            != "global_route_search"
            and tool.get("server_name") != "amap-maps"
        )
    ]


# 长途交通 Provider（城际铁路 / 航班）——仅当行程真的含城际腿时才暴露。
_LONG_DISTANCE_TRANSPORT_SERVERS = frozenset({"12306-train", "duffel-flights"})


def has_intercity_leg(controlled_trip_identity: Dict[str, Any] | None) -> Optional[bool]:
    """行程是否含城际腿。

    返回 None 表示无受控行程身份（无法判定，保持既有行为不排除任何 Provider）；
    True/False 表示身份可判定时是否存在两个及以上不同地点。同城/本地行程
    （origin 与全部 destinations 同一地点）→ False，据此不给 ReAct 暴露长途交通
    Provider，避免模型用空参数瞎调 12306/duffel 触发 -32602。
    """
    identity = controlled_trip_identity or {}
    destinations = identity.get("destinations") or []
    if not destinations:
        return None
    places: List[Dict[str, Any]] = []
    origin = identity.get("origin") or {}
    if origin:
        places.append(origin)
    places.extend(dest for dest in destinations if dest)
    place_ids = {
        str(place.get("place_id") or "").strip()
        for place in places
        if str(place.get("place_id") or "").strip()
    }
    if len(place_ids) >= 2:
        return True
    names = {
        str(place.get("name") or "").strip()
        for place in places
        if str(place.get("name") or "").strip()
    }
    if len(names) >= 2:
        return True
    return False


def scope_long_distance_transport_tools(
    available_tools: List[Dict[str, Any]],
    *,
    controlled_trip_identity: Dict[str, Any] | None,
    connector_gaps: List[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    """本地/同城行程（无城际腿）时移除 12306/duffel，避免空参数误调。

    仅在受控行程身份明确判定为本地时才排除；身份不可判定（None）或存在连接缺口
    时保持不变。
    """
    if connector_gaps or has_intercity_leg(controlled_trip_identity) is not False:
        return available_tools
    return [
        tool
        for tool in available_tools
        if tool.get("server_name") not in _LONG_DISTANCE_TRANSPORT_SERVERS
    ]


def build_transport_task_prompt(
    *,
    task_desc: str,
    user_query: str,
    required_transport_classes: List[str] | None,
    connector_gaps: List[Dict[str, Any]] | None,
    require_current_candidate: bool,
    required_route_scopes: List[ProviderEvidenceScope],
) -> str:
    """Keep a scoped Gate retry from falling back to the generic intercity task."""
    if required_transport_classes and "long_distance" in required_transport_classes:
        route_legs = [
            scope.route_leg.model_dump(mode="json")
            for scope in required_route_scopes
            if scope.route_leg is not None
        ]
        return f"""这是 Candidate Gate 发起的精确长途交通 Leg 补研，不是通用交通调研。

当前任务：{task_desc}
权威 required route legs：{json.dumps(route_legs, ensure_ascii=False)}

本轮硬性执行规则：
- 只查询上述 from_place_id → to_place_id、service_date 与 leg_role 对应的责任；不得改方向、改日期或用另一条 Leg 抵消。
- 上文工具消息里若有铁路能力声明说明该 service_date 超出 12306 预售窗（reference_only），本轮必须改用航班 Provider（search_flights）为同一条 Leg 取证：换 Provider 不是换责任，日期与端点一个都不许动。重复同一条铁路查询不会得到新答案。航班同样给不出可核验班次时明确保留缺口。
- cross_day_required=true 时，search_flights 必须传 require_cross_day=true。
- Provider route 的 SourceRecord、facts、provenance 与 scope identity 由系统从完整 Tool Gateway envelope 编译；模型只选择 route_id 和表达取舍，不得自产 evidence。
- 没有匹配当前精确 Leg 的完整 Provider 路线时明确保留缺口，不得退回 start_date 去程或市内路线。
"""
    if required_transport_classes:
        allowed = ", ".join(required_transport_classes)
        return f"""这是 Candidate Gate 发起的市内路线定向补研，不是新一轮通用交通调研。

当前任务：{task_desc}
用户原始需求仅用于理解旅行背景：{user_query}
精确 connector gap：{json.dumps(connector_gaps or [], ensure_ascii=False)}

本轮硬性执行规则：
- 只研究上游 Research Packets 中具体 Visit/Dining 实体之间的真实市内连接，transport_class 只能是 {allowed}。
- from_endpoint / to_endpoint 必须逐字绑定上游候选的真实名称与 place_id；不得改成附近车站、区域或泛化地名。
- 市内路线只调用 global_route_search，并逐字传入上游端点名称、place_id、坐标和带目的地时区的出发时间。它已按端点坐标自动分派 provider（中国大陆及周边走高德，其余地区走 MOTIS/Transitous），你不需要也不能指定 provider；它失败时不要换用别的地图路线工具去回答同一个问题——那类结果不构成本轮可用的路线证据，直接如实失败。
- 禁止查询航班、12306、出发城市到目的地的路线或任何 long_distance 候选；禁止先做城际交通再做市内路线。
- 本轮只收口一个最高质量 current 候选。必须逐字复制 global_route_search.routes[0] 的 route_id、selected_mode、departure_at、arrival_at、duration_minutes 与完整 segments，并分别建立外部 FactAssertion/FieldProvenance；**逐字包括 null**——高德只给时长不给时刻表，所以大陆路线的 departure_at / arrival_at 就是 null，照抄 null，不得用出发时间加时长自己算一个时刻出来。segments 只用一条 field_path=segments 聚合事实，禁止把每个 segment 的叶子字段展开成重复 lineage。整份 Packet 最多 12 条事实与 12 条 provenance。
- 至少一个候选必须由本轮外部结果支持为 freshness_status=current；没有足够事实就明确失败，不得自产 evidence。
"""
    if require_current_candidate:
        return f"""这是 Candidate Gate 发起的候选事实定向补研，不是重跑整份交通研究。

当前任务：{task_desc}
用户原始需求仅用于理解旅行背景：{user_query}

只补当前 gaps 指向的候选或缺失交通类别，优先使用 assignment 推荐工具；不要执行与 gaps 无关的通用“航班、铁路、市内路线”清单。至少一个候选必须由本轮外部结果支持为 freshness_status=current；没有足够事实就明确失败，不得自产 evidence。
"""
    return TASK_TEMPLATE.format(task_desc=task_desc, user_query=user_query)


async def transport_researcher_node(
    state: TravelAgentState, config: RunnableConfig
) -> Dict[str, Any]:
    """交通研究员节点：查询强类型多段交通候选并输出 Research Packet。"""
    router = get_model_router()
    llm = router.get_fast()
    stream_queue: Optional[asyncio.Queue] = config.get("configurable", {}).get("stream_queue")

    current_time = state.current_time or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    user_query = state.user_query or ""
    run_id = state.run_id

    # ── 解析 research_brief ────────────────────────────────────────────────
    research_brief_context = build_brief_context_for_agent("transport_researcher", state.research_brief)

    upstream_packet_context = format_research_packet_context(state.research_packets)

    # ── 任务分配（支持精炼轮次 round suffix）─────────────────────────────
    output_key, assignment = resolve_agent_assignment(
        state.agent_assignments or {}, _NODE_NAME, state.refinement_count
    )
    if isinstance(assignment, dict):
        task_desc = assignment.get("task", user_query)
        recommended_tools = assignment.get("recommended_tools", [])
        excluded_tools = assignment.get("excluded_tools", [])
        required_transport_classes = assignment.get("required_transport_classes")
        connector_gaps = assignment.get("connector_gaps")
        excluded_candidate_ids = assignment.get("excluded_candidate_ids")
        require_current_candidate = bool(assignment.get("require_current_candidate"))
    else:
        task_desc = str(assignment) if assignment else user_query
        recommended_tools = []
        excluded_tools = []
        required_transport_classes = None
        connector_gaps = None
        excluded_candidate_ids = None
        require_current_candidate = False
        output_key = _NODE_NAME
    provider_assignments = parse_provider_evidence_assignments(
        assignment if isinstance(assignment, dict) else {},
        expected_worker=_NODE_NAME,
        expected_run_id=run_id,
        expected_constraint_pack_revision=state.constraint_pack_revision,
    )
    required_route_scopes = [
        item.scope
        for item in provider_assignments
        if item.scope.route_leg is not None
    ]
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
        # Scoped long-distance research is bounded by its required legs, not by a
        # flat supply number — the sentence must say whichever one binds here.
        candidate_limit=packet_candidate_limit(
            _NODE_NAME,
            required_transport_classes=required_transport_classes,
            required_route_scopes=required_route_scopes,
        ),
        upstream_packet_context=upstream_packet_context,
        active_constraint_ids=active_constraint_ids,
    )
    system_content = inject_agent_context(system_content, state, agent_label=_NODE_NAME)

    logger.info(
        "TransportResearcher 上下文注入: anchor=%s, preset=%s, district_ctx_len=%d",
        bool(state.session_anchor),
        bool(state.preset_context),
        len(upstream_packet_context),
    )

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
    append_recent_history(messages, state)
    messages.append(
        {
            "role": "user",
            "content": build_transport_task_prompt(
                task_desc=task_desc,
                user_query=user_query,
                required_transport_classes=required_transport_classes,
                connector_gaps=connector_gaps,
                require_current_candidate=require_current_candidate,
                required_route_scopes=required_route_scopes,
            ),
        }
    )

    # ── 工具准备 ──────────────────────────────────────────────────────────
    # Exact connector and long-distance repairs must be able to use the Gate's
    # policy-approved alternatives even when the initial Planner omitted that
    # MCP server.  Subsequent filtering still enforces the Worker whitelist.
    selected_servers = (
        []
        if require_current_candidate and recommended_tools
        else state.selected_mcp_servers or []
    )
    available_tools = await get_available_tools(selected_servers)
    available_tools = filter_tools_for_agent(available_tools, _NODE_NAME)
    available_tools = exclude_tools(available_tools, excluded_tools)
    available_tools = scope_initial_transport_tools(
        available_tools,
        require_current_candidate=require_current_candidate,
        connector_gaps=connector_gaps,
    )
    available_tools = scope_long_distance_transport_tools(
        available_tools,
        controlled_trip_identity=state.controlled_trip_identity,
        connector_gaps=connector_gaps,
    )
    available_tools = scope_transport_gap_tools(
        available_tools,
        recommended_tools,
        required_transport_classes,
        scoped_retry=require_current_candidate,
    )
    available_tools = prioritize_recommended_tools(available_tools, recommended_tools)
    cross_day_required = bool(required_route_scopes) and all(
        scope.route_leg is not None
        and scope.route_leg.cross_day_required
        for scope in required_route_scopes
    )
    available_tools = constrain_cross_day_flight_tools(
        available_tools,
        required=cross_day_required,
    )

    tool_schemas = [t["schema"] for t in available_tools if "schema" in t]
    tool_cache = dict(state.tool_cache) if state.tool_cache else {}
    tool_context = build_tool_context_from_state(state)
    authoritative_packet_metadata = build_authoritative_research_packet_metadata(
        worker_kind=_NODE_NAME,
        run_id=run_id,
        task_id=output_key,
        constraint_pack_revision=state.constraint_pack_revision,
        fact_data_revision=state.fact_data_revision,
        query_context={
            "task": task_desc,
            "controlled_trip_identity": state.controlled_trip_identity or {},
            "connector_gaps": connector_gaps or [],
            "required_route_legs": [
                scope.route_leg.model_dump(mode="json")
                for scope in required_route_scopes
                if scope.route_leg is not None
            ],
        },
        generated_at=datetime.datetime.now(datetime.timezone.utc),
    )

    # ── deterministic scoped route preflight + ReAct loop ───────────────
    authoritative_tool_results: List[Dict[str, Any]] = []
    # 供应商在预售窗边缘给出的真实班次：只作披露，不进证据链。
    reference_services: List[ProviderReferenceService] = []
    # 定型步骤的墙钟起点；异常发生在定型之前时保持 None，不记一个假的耗时。
    finalize_started_at: Optional[float] = None
    try:
        provider_route_ready = False
        if not connector_gaps and (
            not required_transport_classes
            or "long_distance" in required_transport_classes
        ):
            provider_route_ready = await discover_domestic_rail_route(
                state=state,
                messages=messages,
                required_transport_classes=required_transport_classes,
                connector_gaps=connector_gaps,
                tool_context=tool_context,
                authoritative_tool_results=authoritative_tool_results,
                reference_services=reference_services,
                required_route_scopes=required_route_scopes,
            )
        if (
            not provider_route_ready
            and require_current_candidate
            and required_transport_classes
        ):
            provider_route_ready = await discover_required_local_route(
                state=state,
                messages=messages,
                required_transport_classes=required_transport_classes,
                connector_gaps=connector_gaps,
                tool_context=tool_context,
                authoritative_tool_results=authoritative_tool_results,
            )
        if provider_route_ready:
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
            required_transport_classes=required_transport_classes,
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
            required_route_scopes=required_route_scopes,
        )

        logger.info(
            "TransportResearcher 完成，key=%s candidates=%d facts=%d sources=%d finalize_ms=%.0f",
            output_key, len(packet.candidates), len(packet.fact_assertions), len(packet.source_records),
            (time.perf_counter() - finalize_started_at) * 1000.0,
        )

        return {
            "messages": [AIMessage(content=f"已核验 {len(packet.candidates)} 个交通候选")],
            "research_packets": {output_key: packet},
            "agent_status": {output_key: "completed"},
            "tool_cache": tool_cache,
            "provider_evidence_outcomes": provider_evidence_outcomes(
                authoritative_tool_messages(authoritative_tool_results),
                expected_worker=_NODE_NAME,
                packet=packet,
                assignments=provider_assignments,
            ),
            "provider_reference_services": reference_services,
        }

    except Exception as e:
        if finalize_started_at is not None:
            logger.info(
                "TransportResearcher 交付定型中断，key=%s finalize_ms=%.0f",
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
            "TransportResearcher 执行失败: worker=%s key=%s last_error=%s",
            _NODE_NAME, output_key, last_error[:_LAST_ERROR_LOG_LIMIT],
        )
        result = {
            "messages": [AIMessage(content="交通 Research Packet 生成失败")],
            "last_error": last_error,
            "agent_status": {output_key: "failed"},
        }
        if failure_packet is not None:
            result["research_packets"] = {output_key: failure_packet}
        result["provider_evidence_outcomes"] = outcomes
        # A reference service was observed before this round failed, and the
        # failure does not unobserve it: the disclosure is exactly what a failed
        # round still owes the traveller.
        if reference_services:
            result["provider_reference_services"] = reference_services
        return result


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
