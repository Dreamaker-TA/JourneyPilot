"""
Destination Researcher Agent 节点 (Domain Layer)

职责：收集目的地的深度知识信息
- 景点推荐、美食、文化礼仪、实用信息、区域分布
- RAG 多集合检索 + Tavily/Brave 检索 + Firecrawl 抓取 + 公开网页资料
- 输出强类型 Research Packet

工具白名单：tavily-search, brave-search, firecrawl, duckduckgo-search, fetch
模型：primary（知识整合质量优先）
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from ...entities.state import TravelAgentState
from ...models.router import get_model_router
from ...rag.retriever import HybridRetriever
from ...rag.retrieval_pipeline import retrieve_for_query
from ...rag.collections import (
    GroundingCorpus,
    grounding_corpus,
    relabel_to_logical_collections,
)
from ...rag.place_mentions import (
    chunk_texts_by_collection,
    measure_place_funnel,
)
from ...rag.source_records import rag_chunk_source_id, rag_chunk_source_records
from ...rag.retrieval_grader import RetrievalGrader, GradeRoute
from ...rag.policy import RAGModePolicy, RAGPolicyInput
from ...rag.summary import build_retrieval_summary
from ...entities.place_identity import stable_place_id_amap_poi
from ...utils.coordinates import amap_location_to_wgs84
from ...services.nominatim_place_search import (
    NominatimPlaceSearchError,
    lookup_typed_addresses_bilingual,
)
from ...services.product_requirements import (
    destinations_are_cn_only,
    discovery_physical_candidate_kinds,
    required_physical_candidate_kinds,
)
from ..utils import (
    append_recent_history,
    build_tool_context_from_state,
    compact_tool_content_for_model,
    exclude_tools,
    filter_tools_for_agent,
    get_available_tools,
    inject_agent_context,
    prioritize_recommended_tools,
    resolve_agent_assignment,
    resolve_scoped_research_output_key,
    execute_tool,
    streaming_react_loop,
)
from ..research_packet_output import (
    packet_candidate_limit,
    authoritative_tool_messages,
    authoritative_retry_source_records,
    build_authoritative_research_packet_metadata,
    build_failure_only_research_packet,
    has_required_provider_place_selection,
    observed_place_nominations,
    parse_or_repair_research_packet_output,
    provider_evidence_outcomes,
    provider_round_answered_empty,
    provider_round_capability_declared,
)
from ...tools.governance import QUALITY_VERIFIED_PLACE_IDS_METADATA_KEY
from ...entities.provider_evidence import parse_provider_evidence_assignments
from ...services.candidate_admission import provider_place_type_matches_candidate_kind
from ...services.delivery_projection import candidate_place_id
from ...services.destination_scope import annotate_destination_distance
from ...services.state_invalidation import generation_packet_key
from ...services.constraint_applicability import active_hard_constraints, active_hard_constraint_ids
from ..research_packet_prompt import build_research_packet_system_prompt
from ..worker_errors import format_worker_last_error
from ...utils.brief_helpers import build_assignment_context
from .prompts import (
    DINING_OPTIONS_TEMPLATE,
    KNOWLEDGE_BASE_NOMINATION,
    NO_DINING_OPTIONS,
    TASK_TEMPLATE,
)

if TYPE_CHECKING:
    from ...api.sse_buffer import SSEBuffer

logger = logging.getLogger(__name__)

_NODE_NAME = "destination_researcher"
# 失败日志里 last_error 的单行上限：供应商偶尔把整个响应体塞进异常消息，
# 分类前缀在开头，截断尾部不影响门的取证。
_LAST_ERROR_LOG_LIMIT = 600
# 查哪几个集合不写在这里：出厂四个 + 这个用户自己上传的那一个，由
# ``rag.collections.grounding_corpus`` 一处给出。此前这里有一份裸的出厂集合名清单，
# 而 ``scripts/corpus_place_census.py`` 又抄了一份、靠一句「keep in step with」维持 ——
# 那是这个仓的老形状：同一张表两处各写一份。
# 交给分级器评判的候选条数（分级器再往下过滤）。
_RAG_CANDIDATE_TOP_K = 8
# 注入 prompt 的知识库正文预算（字符）。每段占多少不在这里估：那是
# ``HybridRetriever.format_docs_for_prompt`` 说了算的，见 ``_format_rag_context``。
_RAG_CONTEXT_CHAR_BUDGET = 5000
# 块间分隔符，与 ``format_docs_for_prompt`` 的 ``"\n\n---\n\n"`` 同一个东西。
_RAG_CONTEXT_BLOCK_SEPARATOR = "\n\n---\n\n"
# 目的地调研：RAG 主导，工具调用较少，3 轮即可覆盖大部分场景
_MAX_TOOL_ITERATIONS = 3
_SCOPED_DISCOVERY_TOOLS = (
    "free_web_search",
    "tavily_search",
    "brave_web_search",
    "firecrawl_search",
)
_SEARCH_RESULT_TITLE = re.compile(r"^\s*\d{1,2}\.\s+(.+?)\s*$")
_INLINE_SEARCH_RESULT = re.compile(
    r"(?:^|\s)\d{1,2}\.\s+(.{2,180}?)(?=\s+(?:链接|link)\s*:)",
    re.IGNORECASE,
)
# Dining 质量交叉核验用到的两个正则 / 两组 addressdetails 类型键。
# ``_QUALITY_COMPACT`` 只保留数字、ASCII 小写字母、日文假名与 CJK：其余脚本（泰文、
# 西里尔、天城文…）会被压成空串，所以任何参与 ``in`` 判定的值都必须先过一遍
# ``_compact_quality_text`` 并丢掉空结果。
_QUALITY_COMPACT = re.compile(r"[^0-9a-z぀-ヿ㐀-鿿]")
# CJK/假名连写 2 字起（地名本身就短），字母类脚本 5 字起（"rue"、"ward"、"via" 这类
# 通用词长度不够，落不进 token 集）。CJK 分支在前，避免长 CJK 串被字母分支吞掉。
_QUALITY_LOCALITY_TOKEN = re.compile(
    r"[぀-ヿ㐀-鿿]{2,}|[^\W\d_]{5,}",
    re.UNICODE,
)
# 由细到粗排列，因为顺序会外溢：token 列表的前两个直接进质量查询串，越细的地名越
# 能把同城同品牌的两家分店分开。
_QUALITY_LOCALITY_SUB_CITY_KEYS = (
    "road",
    "pedestrian",
    "neighbourhood",
    "quarter",
    "hamlet",
    "suburb",
    "borough",
    "city_district",
)
_QUALITY_LOCALITY_BROAD_KEYS = (
    "city",
    "town",
    "village",
    "municipality",
    "county",
    "state",
    "province",
    "region",
    "country",
)


def scope_destination_gap_tools(
    available_tools: List[Dict[str, Any]],
    recommended_tools: List[str],
    *,
    scoped_retry: bool = False,
) -> List[Dict[str, Any]]:
    """Expose the small identity-research set directly for a scoped retry."""
    if not scoped_retry or not recommended_tools:
        return available_tools
    recommended_set = set(recommended_tools)
    scoped = [
        tool
        for tool in available_tools
        if tool.get("schema", {}).get("function", {}).get("name", "")
        in recommended_set
    ]
    global_identity = [
        tool
        for tool in scoped
        if tool.get("schema", {}).get("function", {}).get("name", "")
        == "global_place_search"
    ]
    if global_identity:
        scoped_by_name = {
            tool.get("schema", {}).get("function", {}).get("name", ""): tool
            for tool in scoped
        }
        discovery = next(
            (scoped_by_name[name] for name in _SCOPED_DISCOVERY_TOOLS if name in scoped_by_name),
            None,
        )
        return [*global_identity, *([discovery] if discovery is not None else [])]
    return scoped or available_tools


def build_destination_boundaries(
    controlled_trip_identity: Dict[str, Any] | None,
) -> List[Dict[str, Any]]:
    """Project controlled destinations into the boundary shape workers query with.

    ``name`` is the short city name, never the Nominatim full address string.
    ``PlaceIdentity`` carries both — ``name`` ("大阪市") and ``display_name``
    ("大阪市, 大阪府, 日本"), each a required non-empty field
    (entities/trip_input.py:54-55) — and every consumer downstream wants the
    short one: the place-provider queries below, amap's ``city=`` parameter, and
    the city suffix stripped off discovered restaurant names.  Feeding the full
    string into a provider query is what made non-CN Visit discovery return
    nothing; see the probe table on the Visit binding.
    """
    return [
        {
            "destination_id": str(item.get("place_id") or ""),
            "name": str(item.get("name") or ""),
            "country_code": str(item.get("country_code") or "").casefold(),
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
        }
        for item in (controlled_trip_identity or {}).get("destinations", [])
        if isinstance(item, dict)
    ]


def collect_offered_dining_place_ids(
    authoritative_tool_results: List[Dict[str, Any]],
) -> List[str]:
    """Every restaurant the server put on this round's table.

    The task prompt names these ids so the model knows which restaurants it may
    turn into ``DiningCandidate`` records — and which it must not invent.  It is
    deliberately *every* dining place the Provider returned, not only the ones a
    branch-level review could be found for: whether an option gets
    marked 外部评价已核验 is decided downstream from the envelopes, never by the
    model, and a restaurant nobody reviewed is still a restaurant.

    The discriminator is ``provider_place_type_matches_candidate_kind`` — the
    same one ``research_packet_output`` uses to build the eligible option list —
    so the two surfaces the model sees cannot disagree about what is on offer.
    """
    place_ids: List[str] = []
    for envelope in authoritative_tool_results:
        if not isinstance(envelope, dict):
            continue
        payload = envelope.get("sanitized_result")
        results = payload.get("results") if isinstance(payload, dict) else None
        for item in results or []:
            if not isinstance(item, dict):
                continue
            place_id = item.get("place_id")
            provider_type = item.get("provider_place_type")
            if not isinstance(place_id, str) or not place_id:
                continue
            if place_id in place_ids or not isinstance(provider_type, str):
                continue
            if provider_place_type_matches_candidate_kind(provider_type, "dining"):
                place_ids.append(place_id)
    return place_ids


def build_destination_task_prompt(
    *,
    task_desc: str,
    user_query: str,
    rag_context_section: str,
    require_current_candidate: bool,
    offered_dining_place_ids: List[str],
    destination_boundaries: List[Dict[str, Any]] | None = None,
) -> str:
    """Keep an identity repair focused on provider-backed place records."""
    boundaries = json.dumps(destination_boundaries or [], ensure_ascii=False)
    if require_current_candidate:
        return f"""这是 Candidate Gate 发起的目的地候选身份定向补研，不是重新生成通用景点和餐饮清单。

当前任务：{task_desc}
用户原始需求仅用于理解旅行背景：{user_query}
受控目的地边界：{boundaries}

本轮硬性执行规则：
- 只补当前 gaps 指向的 Visit/Dining 候选身份字段。若 gap 只给出领域而没有具体实体名，先用唯一开放的网页发现工具找到 1 至 3 个与用户偏好匹配的真实门店/地点名称，再分别用 global_place_search 解析稳定身份；禁止拿“东京餐厅”等类别词直接撞地点 Provider 后就结束本轮。
- 海外或全球地点优先调用 global_place_search，候选身份必须来自其结果，并逐字使用受控目的地的 country_code；maps_text_search 只可保留国家码与目的地一致的结果。网页发现结果只能提供待解析的实体名，不能直接支持 place_id、Provider type/country 或正式 Candidate。
- place_id、provider_place_type 与 provider_country_code 必须逐字来自本轮外部工具原始结果，并由对应 FactAssertion 和 SourceRecord.snapshot 支持；禁止拼接、归一化、猜测或输出 ChIJ placeholder 等自产标识。
- DiningCandidate 必须是具体餐馆、咖啡馆、酒吧或摊位门店；市场、商圈、美食街、区域和餐饮类别不能冒充门店候选。
- 网页搜索可用于交叉核验名称、地址和营业信息，但网页只出现名称或地址时不能作为 place_id 证据。
- 动态保留 1 至 6 个身份完整且质量足够的候选；宁可减少候选，也不得用 unknown、未检索、占位值或自产 evidence 凑数。
- 没有任何满足合同的候选就明确失败，不得放宽硬约束。
"""
    # 已核验餐饮选项段只进初始轮。Candidate Gate 的定向补研轮由 gate 决定域，dining
    # 只在国内（enforced）成为 gap，那一轮必须保留上面「没有满足合同的候选就明确失败」
    # 的硬合同；把「缺少餐饮不算失败」的软措辞塞进去会直接削弱国内 dining 闸门。
    dining_section = DINING_OPTIONS_TEMPLATE.format(
        place_ids="、".join(offered_dining_place_ids) or NO_DINING_OPTIONS,
    )
    # 提名那段紧贴着 chunk 正文，而且**只在真的印了 chunk 的那一轮出现**：一轮没检索到
    # 东西却还教模型「去 chunk 里挖地名」，是在给它一个不存在的抽屉，只会换来幻觉出的
    # 名字。这个「有正文才有指令」的绑定写在一处，所以两者不可能各走各的。
    knowledge_base_section = (
        f"{rag_context_section}\n\n{KNOWLEDGE_BASE_NOMINATION}"
        if rag_context_section.strip()
        else rag_context_section
    )
    return (
        TASK_TEMPLATE.format(
            task_desc=task_desc,
            user_query=user_query,
            rag_context_section=knowledge_base_section,
        )
        + f"\n受控目的地边界：{boundaries}。所有地点必须保留 Provider 原始国家码并与对应 destination_id 一致。"
        + f"\n{dining_section}"
    )


def _web_search_entries(value: Any) -> List[tuple[str, str]]:
    """Extract result titles/snippets from structured or MCP text search output."""
    entries: List[tuple[str, str]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            title = item.get("title")
            if isinstance(title, str):
                snippet = item.get("snippet") or item.get("body") or ""
                entries.append((title, snippet if isinstance(snippet, str) else ""))
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, list):
            for nested in item:
                visit(nested)
            return
        if not isinstance(item, str):
            return
        lines = item.splitlines()
        for index, line in enumerate(lines):
            match = _SEARCH_RESULT_TITLE.match(line)
            if match is None:
                continue
            snippet = ""
            for following in lines[index + 1 : index + 4]:
                stripped = following.strip()
                if stripped.startswith("摘要:"):
                    snippet = stripped.removeprefix("摘要:").strip()
                    break
            entries.append((match.group(1), snippet))
        for match in _INLINE_SEARCH_RESULT.finditer(item):
            entries.append((match.group(1), ""))

    visit(value)
    return list(dict.fromkeys(entries))



def _compact_quality_text(value: Any) -> str:
    """Strip everything the two-sided name comparison must not depend on.

    Punctuation, spacing, case and script decorations differ freely between an
    OSM ``name`` tag and a review page's title; what survives is the identity
    core.  Only the scripts this pattern keeps survive at all, which is why every
    caller must drop values that compact to nothing instead of comparing them:
    ``"" in anything`` is True, and that made the Thai alias ``ย่งเซ่งหลี`` match
    every result on earth.
    """
    return _QUALITY_COMPACT.sub("", str(value).casefold())


def dining_quality_locality_tokens(
    typed_addresses: List[Dict[str, Any]],
    entity_names: List[str] | None = None,
) -> List[str]:
    """Pick branch-distinguishing locality tokens out of typed provider addresses.

    Input is what ``lookup_typed_addresses_bilingual`` returns for one place: the
    same address in its local script and in English.  Selection is by
    ``addressdetails`` *type key*, never by value:

    - kept — ``road`` / ``pedestrian`` / ``suburb`` / ``quarter`` /
      ``neighbourhood`` / ``city_district`` / ``borough`` / ``hamlet``: the fields
      that separate two branches of one brand inside one city, which is this
      function's whole job.
    - dropped — city and above, plus postcode and ISO codes: they hold for every
      branch in town, so they can only manufacture agreement.  This replaces the
      old hardcoded {japan, tokyo, 日本, 東京都, …} blocklist, which knew one
      country's city names and let every other city's name through.
    - dropped as well — any token that also occurs in a city-and-above field of
      the same lookup, so "Paris" cannot re-enter through ``city_district``.

    Both languages contribute because the match target is an open web page: a
    Paris review page spells the street "Rue Saint-Dominique", never the zh-CN
    "圣多米尼克路" the stored identity carries.  That mismatch is why the locality
    arm scored zero outside Japan — a zero-LLM probe, ``"<店名>" <城市>
    reviews`` judged by this very function:

      店 / 城市                        旧 zh-CN 串 token   类型化双语 token
      Mon Square / 巴黎                     不匹配              匹配
      La Terrazza Caffarelli / 罗马          匹配               匹配
      Young Seng Lee / 曼谷                 不匹配              不匹配

    Bangkok stays honestly unverifiable: its review pages name a neighbourhood
    ("Chinatown / Yaowarat") that no OSM address field carries.
    """
    compact_names = {
        compact
        for name in (entity_names or [])
        if (compact := _compact_quality_text(name))
    }
    broad: set[str] = set()
    for address in typed_addresses:
        if not isinstance(address, dict):
            continue
        for key in _QUALITY_LOCALITY_BROAD_KEYS:
            value = address.get(key)
            if isinstance(value, str) and value.strip():
                broad.update(
                    token.casefold()
                    for token in _QUALITY_LOCALITY_TOKEN.findall(value)
                )
    tokens: List[str] = []
    for address in typed_addresses:
        if not isinstance(address, dict):
            continue
        for key in _QUALITY_LOCALITY_SUB_CITY_KEYS:
            value = address.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            if _compact_quality_text(value) in compact_names:
                continue
            for raw_token in _QUALITY_LOCALITY_TOKEN.findall(value):
                token = raw_token.casefold()
                if not _compact_quality_text(token):
                    continue
                if token in broad or token in tokens:
                    continue
                tokens.append(token)
                # Japanese review sites commonly omit the administrative suffix
                # used by Nominatim (for example, ``新宿`` vs ``新宿区``).  Keep
                # both spellings, while still requiring a branch-local token.
                without_admin_suffix = re.sub(
                    r"(?:都|道|府|県|市|区|町|村)$",
                    "",
                    token,
                )
                if (
                    len(without_admin_suffix) >= 2
                    and without_admin_suffix not in broad
                    and without_admin_suffix not in tokens
                ):
                    tokens.append(without_admin_suffix)
    return tokens[:8]


async def resolve_dining_locality_tokens(
    places: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Map each candidate place_id to its locality tokens, or to nothing.

    Two provider calls for the whole batch, whatever the number of places (see
    ``lookup_typed_addresses_bilingual``).  A provider failure yields an empty map
    instead of falling back to the stored zh-CN address string: that path is
    measured not to match foreign review pages, so reusing it would be a silent
    fallback manufacturing unverifiable quality bindings.  No tokens means this
    branch cannot be cross-checked, and an unverifiable Dining option is not
    delivered at all.
    """
    place_ids = [
        place_id
        for place in places
        if isinstance(place, dict) and (place_id := str(place.get("place_id") or ""))
    ]
    if not place_ids:
        return {}
    try:
        typed_addresses = await lookup_typed_addresses_bilingual(place_ids)
    except NominatimPlaceSearchError as exc:
        logger.warning(
            "[destination_researcher] dining locality lookup unavailable for %d place(s): %s",
            len(place_ids),
            exc,
        )
        return {}
    tokens: Dict[str, List[str]] = {}
    for place in places:
        place_id = str(place.get("place_id") or "")
        if not place_id:
            continue
        names = [place.get("name"), *(place.get("aliases") or [])]
        tokens[place_id] = dining_quality_locality_tokens(
            typed_addresses.get(place_id) or [],
            [str(name) for name in names if isinstance(name, str)],
        )
    return tokens


def dining_quality_source_matches_place(
    place: Dict[str, Any],
    quality_result: Any,
    *,
    locality_tokens: List[str],
) -> bool:
    """Require one external result to match both entity identity and branch locality.

    ``locality_tokens`` comes from ``resolve_dining_locality_tokens``; an empty
    list means the branch could not be pinned down, and the answer is False.
    """
    names = [place.get("name"), *(place.get("aliases") or [])]
    compact_names = {
        compact
        for name in names
        if isinstance(name, str)
        and name.strip()
        and (compact := _compact_quality_text(name))
    }
    if not compact_names or not locality_tokens:
        return False
    for title, snippet in _web_search_entries(quality_result):
        entry = f"{title} {snippet}".casefold()
        compact_entry = _compact_quality_text(entry)
        if not any(name in compact_entry for name in compact_names):
            continue
        if not any(locality in entry for locality in locality_tokens):
            continue
        if "permanently closed" in entry or title.strip().casefold().startswith("[closed]"):
            continue
        return True
    return False


def dining_intent_in_request(
    task_desc: str,
    user_query: str,
    controlled_trip_identity: Dict[str, Any],
) -> bool:
    """Did this round's request text or interests ask about food at all?

    Pure intent, no promise: it answers "should the worker look", which is why
    the discovery resolver below reads it without any country gate.
    """
    style = controlled_trip_identity.get("style")
    interests = style.get("secondary_interests") if isinstance(style, dict) else []
    normalized_interests = {
        str(interest).strip().casefold()
        for interest in (interests or [])
        if str(interest).strip()
    }
    if normalized_interests.intersection({"food", "dining", "ramen", "美食", "餐饮", "拉面"}):
        return True
    request_text = f"{task_desc} {user_query}".casefold()
    return any(
        token in request_text
        for token in ("restaurant", "dining", "ramen", "美食", "餐馆", "餐厅", "拉面", "拉麵")
    )


def dining_is_required_by_request(
    task_desc: str,
    user_query: str,
    controlled_trip_identity: Dict[str, Any],
) -> bool:
    """The second promise source, under the same country gate as the product one.

    Free-text intent can promise dining exactly where a style selection can
    (``services/product_requirements.py::required_physical_candidate_kinds``):
    CN-only destinations.  Abroad the same text still drives discovery, but it
    cannot put dining into the enforced set — a promise nothing can close would
    only spend repair budget and fail the run.
    """
    if not dining_intent_in_request(task_desc, user_query, controlled_trip_identity):
        return False
    return destinations_are_cn_only(controlled_trip_identity)


def resolve_required_candidate_kinds(
    assignment_required_kinds: List[str] | None,
    *,
    task_desc: str,
    user_query: str,
    controlled_trip_identity: Dict[str, Any],
) -> List[str] | None:
    """Return the *enforced* domains: what this packet must contain to be valid.

    This set is the parse-time contract
    (``parse_or_repair_research_packet_output(required_candidate_kinds=...)``): a
    packet missing one of these kinds is rejected.  It is therefore the promise
    set, not the search wishlist — see ``resolve_discovery_candidate_kinds`` for
    the superset the deterministic preflight actually goes looking for.

    The two promise sources are **unioned, not tried in order**.  Visit is an
    unconditional promise, so the product set is never empty; an ordered check would
    therefore make the free-text source dead code, and a trip whose style names no
    food but whose request asks for it would quietly stop promising dining.
    """
    if assignment_required_kinds:
        return list(dict.fromkeys(assignment_required_kinds))
    required = required_physical_candidate_kinds(controlled_trip_identity)
    if dining_is_required_by_request(
        task_desc,
        user_query,
        controlled_trip_identity,
    ):
        required = required | {"dining"}
    return sorted(required) or None


def resolve_discovery_candidate_kinds(
    assignment_required_kinds: List[str] | None,
    *,
    task_desc: str,
    user_query: str,
    controlled_trip_identity: Dict[str, Any],
) -> List[str] | None:
    """Return the domains the deterministic preflight *tries* to ground.

    Superset of the enforced set, and now a superset by construction rather than
    by coincidence: ``discovery_physical_candidate_kinds`` is itself the union of
    the promise set and the style's interest, so no domain can be enforced without
    also being searched for.  The one member it adds over the promise set is
    non-CN dining — delivering a restaurant nobody promised costs one identity
    query, one batched address lookup pair and at most three review queries, and
    failing to find one costs nothing downstream, because no gap, no assignment
    and no parse rule references it.

    Enforced and discovery coincide on a Candidate-Gate-driven round: the
    assignment kinds are already country-filtered by the gate, and a Visit-only
    repair must not re-open dining just because the trip likes food.
    """
    if assignment_required_kinds:
        return list(dict.fromkeys(assignment_required_kinds))
    discovery = discovery_physical_candidate_kinds(controlled_trip_identity)
    if dining_intent_in_request(task_desc, user_query, controlled_trip_identity):
        discovery = discovery | {"dining"}
    return sorted(discovery) or None


# ---------------------------------------------------------------------------
# 高德 POI 确定性餐饮落地（deterministic amap dining grounding）
#
# CN-only 行程的美食兴趣是唯一仍然把 dining 提升为 required_candidate_kinds 的情形
# （services/product_requirements.py::required_physical_candidate_kinds），因为国内
# 这条 provider seam 能确定性闭环：这里镜像 accommodation 的国内酒店预检，用高德 POI
# 搜索投影为 global_place_search 形信封，并用高德 POI 自身核验绑定
# quality_verified_place_ids，让候选挣到 external_quality_match（报告里的「外部评价已
# 核验」），不依赖任何外部点评页。非 CN 走下面的 OSM + 公开点评页那条路径——那条核不上
# 也不影响餐厅交付，只是不带标记。
# ---------------------------------------------------------------------------

# 高德「餐饮服务」POI typecode（050xxx）
_AMAP_DINING_TYPECODE_PREFIXES = ("050",)
_AMAP_DINING_PER_CITY = 5

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


def _extract_amap_tool_text(envelope: Dict[str, Any] | Any) -> str:
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

    if isinstance(envelope, dict):
        walk(envelope.get("sanitized_result"))
        if not parts:
            summary = envelope.get("result_summary")
            if isinstance(summary, str):
                parts.append(summary)
    return "\n".join(parts)


def _is_amap_dining_typecode(typecode: str) -> bool:
    for code in str(typecode or "").split("|"):
        code = code.strip()
        if any(code.startswith(prefix) for prefix in _AMAP_DINING_TYPECODE_PREFIXES):
            return True
    return False


def _amap_dining_type_label(typecode: str) -> str:
    """provider_place_type must carry dining admission markers (餐饮服务/餐厅)."""
    first = ""
    for code in str(typecode or "").split("|"):
        code = code.strip()
        if code:
            first = code
            break
    if first.startswith("0505"):
        kind = "咖啡厅"
    elif first.startswith("0506"):
        kind = "茶艺馆"
    elif first.startswith("0503"):
        kind = "快餐厅"
    elif first.startswith("0502"):
        kind = "外国餐厅"
    elif first.startswith("0501"):
        kind = "中餐厅"
    else:
        kind = "餐饮服务"
    return f"餐饮服务·{kind}（amap typecode {typecode}）"


def _dining_place_record(
    poi_id: str,
    name: str,
    address: str,
    typecode: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    poi_id, name, address, typecode = (
        poi_id.strip(),
        name.strip(),
        address.strip(),
        typecode.strip(),
    )
    if not poi_id or not name or not address or not typecode:
        return None
    if not _is_amap_dining_typecode(typecode):
        return None
    place_id = stable_place_id_amap_poi(poi_id)
    if place_id is None:
        return None
    record: Dict[str, Any] = {
        "place_id": place_id,
        "provider": "amap",
        "provider_place_type": _amap_dining_type_label(typecode),
        "provider_country_code": "cn",
        "name": name,
        "address": address,
    }
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        record["latitude"] = float(latitude)
        record["longitude"] = float(longitude)
    return record


def _restaurants_from_pois(pois: Any) -> List[Dict[str, Any]]:
    restaurants: List[Dict[str, Any]] = []
    for poi in pois or []:
        if not isinstance(poi, dict):
            continue
        latitude = poi.get("latitude")
        longitude = poi.get("longitude")
        if latitude is None or longitude is None:
            latitude, longitude = amap_location_to_wgs84(poi.get("location"))
        record = _dining_place_record(
            str(poi.get("id") or ""),
            str(poi.get("name") or ""),
            str(poi.get("address") or ""),
            str(poi.get("typecode") or ""),
            latitude if isinstance(latitude, (int, float)) else None,
            longitude if isinstance(longitude, (int, float)) else None,
        )
        if record is not None:
            restaurants.append(record)
        if len(restaurants) >= _AMAP_DINING_PER_CITY:
            break
    return restaurants


def _parse_amap_restaurants(text: str) -> List[Dict[str, Any]]:
    restaurants: List[Dict[str, Any]] = []
    source = text or ""
    for match in _AMAP_POI_RE.finditer(source):
        object_start = source.rfind("{", 0, match.start())
        object_end = source.find("}", match.end())
        object_span = source[
            object_start if object_start != -1 else match.start() :
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
        record = _dining_place_record(
            match.group("id"),
            match.group("name"),
            match.group("address"),
            match.group("typecode"),
            latitude,
            longitude,
        )
        if record is not None:
            restaurants.append(record)
        if len(restaurants) >= _AMAP_DINING_PER_CITY:
            break
    return restaurants


def _build_amap_dining_identity_envelope(
    *,
    results: List[Dict[str, Any]],
    audit_id: str,
    retrieved_at: str,
) -> Dict[str, Any]:
    return {
        "tool_name": "global_place_search",
        "server_name": "amap-maps",
        "status": "success",
        "audit_id": audit_id,
        "retrieved_at": retrieved_at,
        "metadata": {"evidence_allowed": True},
        "sanitized_result": {
            "success": True,
            "provider": "amap",
            "results": results,
        },
    }


def _build_amap_dining_quality_envelope(
    *,
    place: Dict[str, Any],
    parent_audit_id: str,
    index: int,
    retrieved_at: str,
) -> Dict[str, Any]:
    """Bind external_quality_match from amap POI identity (CN substitute for a review page)."""
    place_id = str(place.get("place_id") or "")
    name = str(place.get("name") or "")
    address = str(place.get("address") or "")
    # Distinct audit_id so identity + quality source_record_ids do not collide.
    quality_audit_id = f"{parent_audit_id}_dining_q{index}"
    locality = address.split(",")[0].strip() if address else name
    return {
        "tool_name": "maps_text_search",
        "server_name": "amap-maps",
        "status": "success",
        "audit_id": quality_audit_id,
        "retrieved_at": retrieved_at,
        "metadata": {
            "evidence_allowed": True,
            QUALITY_VERIFIED_PLACE_IDS_METADATA_KEY: [place_id],
        },
        "sanitized_result": {
            "content": [
                {
                    "title": f"{name} - {locality}/餐厅 | 高德地图",
                    "snippet": f"{name} {address} 餐饮服务 POI 核验",
                    "url": f"amap://poi/{place_id.removeprefix('amap:poi:')}",
                }
            ]
        },
    }


async def _enrich_amap_dining_coordinates(
    restaurants: List[Dict[str, Any]],
    *,
    available_tools: List[Dict[str, Any]],
    tool_context: Dict[str, Any],
) -> None:
    available_names = {
        tool.get("schema", {}).get("function", {}).get("name", "")
        for tool in available_tools
    }
    if "maps_search_detail" not in available_names:
        return
    for restaurant in restaurants:
        if restaurant.get("latitude") is not None and restaurant.get("longitude") is not None:
            continue
        place_id = str(restaurant.get("place_id") or "")
        poi_id = place_id.removeprefix("amap:poi:")
        if not poi_id or poi_id == place_id:
            continue
        try:
            detail = await execute_tool(
                "maps_search_detail",
                {"id": poi_id},
                available_tools=available_tools,
                node_name=_NODE_NAME,
                activation_source="amap_dining_detail",
                **tool_context,
            )
        except Exception:
            logger.warning(
                "[destination_researcher] amap dining detail failed for %s",
                poi_id,
                exc_info=True,
            )
            continue
        if not isinstance(detail, dict):
            continue
        location: Any = None
        sanitized = detail.get("sanitized_result")
        if isinstance(sanitized, dict):
            location = sanitized.get("location")
        if location is None:
            text = _extract_amap_tool_text(detail)
            if text:
                try:
                    location = json.loads(text).get("location")
                except (json.JSONDecodeError, AttributeError):
                    location = None
        latitude, longitude = amap_location_to_wgs84(location)
        if latitude is not None and longitude is not None:
            restaurant["latitude"] = latitude
            restaurant["longitude"] = longitude


def _cn_only_destination_boundaries(
    destination_boundaries: List[Dict[str, Any]],
) -> bool:
    if not destination_boundaries:
        return False
    return all(
        str(boundary.get("country_code") or "").casefold() == "cn"
        for boundary in destination_boundaries
    )


async def discover_amap_dining(
    *,
    messages: List[Dict[str, Any]],
    available_tools: List[Dict[str, Any]],
    destination_boundaries: List[Dict[str, Any]],
    tool_context: Dict[str, Any],
    authoritative_tool_results: List[Dict[str, Any]],
) -> bool:
    """Deterministically ground CN dining from real amap POI data.

    Returns True when at least one quality-bound dining option is available.
    """
    if not _cn_only_destination_boundaries(destination_boundaries):
        return False
    available_names = {
        tool.get("schema", {}).get("function", {}).get("name", "")
        for tool in available_tools
    }
    if "maps_text_search" not in available_names:
        logger.info(
            "[destination_researcher] amap dining skip: maps_text_search absent"
        )
        return False

    injected_any = False
    for boundary in destination_boundaries:
        # 边界 name 已是短名（"深圳市"），amap 的 city= 直接吃它。
        city_hint = str(boundary.get("name") or "").strip()
        if not city_hint:
            continue
        try:
            env = await execute_tool(
                "maps_text_search",
                {"keywords": f"{city_hint}餐厅", "city": city_hint},
                available_tools=available_tools,
                node_name=_NODE_NAME,
                activation_source="amap_dining_search",
                **tool_context,
            )
        except Exception:
            logger.warning(
                "[destination_researcher] amap dining discovery failed for %s",
                city_hint,
                exc_info=True,
            )
            continue
        if not isinstance(env, dict) or env.get("status") != "success":
            logger.info(
                "[destination_researcher] amap dining search for %s status=%s",
                city_hint,
                env.get("status") if isinstance(env, dict) else type(env).__name__,
            )
            continue
        sanitized = env.get("sanitized_result")
        records = sanitized.get("results") if isinstance(sanitized, dict) else None
        if isinstance(records, list):
            restaurants = _restaurants_from_pois(records)
        else:
            restaurants = _parse_amap_restaurants(_extract_amap_tool_text(env))
        await _enrich_amap_dining_coordinates(
            restaurants,
            available_tools=available_tools,
            tool_context=tool_context,
        )
        # Same reason as the amap lodging binder: amap's ``city=`` is a prefecture,
        # these records enter the same selection enum a ``global_place_search``
        # answer does, and that enum drops an out-of-destination option only by
        # reading the record's own ``destination_distance_km``.
        annotate_destination_distance(
            restaurants,
            destination_latitude=boundary.get("latitude"),
            destination_longitude=boundary.get("longitude"),
        )
        audit_id = str(env.get("audit_id") or "").strip()
        logger.info(
            "[destination_researcher] amap dining for %s: %d POIs, audit_id=%s",
            city_hint,
            len(restaurants),
            bool(audit_id),
        )
        if not restaurants or not audit_id:
            continue
        retrieved_at = str(
            env.get("retrieved_at")
            or datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
        for index, place in enumerate(restaurants):
            quality_envelope = _build_amap_dining_quality_envelope(
                place=place,
                parent_audit_id=audit_id,
                index=index,
                retrieved_at=retrieved_at,
            )
            messages.append(
                {
                    "role": "tool",
                    "content": compact_tool_content_for_model(quality_envelope),
                }
            )
            authoritative_tool_results.append(quality_envelope)
        identity_envelope = _build_amap_dining_identity_envelope(
            results=restaurants,
            audit_id=audit_id,
            retrieved_at=retrieved_at,
        )
        messages.append(
            {
                "role": "tool",
                "content": compact_tool_content_for_model(identity_envelope),
            }
        )
        authoritative_tool_results.append(identity_envelope)
        injected_any = True
        logger.info(
            "[destination_researcher] deterministic amap dining bound: %d for %s (e.g. %s)",
            len(restaurants),
            city_hint,
            restaurants[0]["name"],
        )

    if not injected_any:
        return False
    return has_required_provider_place_selection(
        authoritative_tool_messages(authoritative_tool_results),
        expected_worker=_NODE_NAME,
        required_candidate_kinds=["dining"],
    )


async def discover_and_resolve_required_places(
    *,
    messages: List[Dict[str, Any]],
    available_tools: List[Dict[str, Any]],
    required_candidate_kinds: List[str] | None,
    destination_boundaries: List[Dict[str, Any]],
    tool_context: Dict[str, Any],
    authoritative_tool_results: List[Dict[str, Any]],
) -> None:
    """Deterministically close discovery -> stable Provider identity for scoped gaps."""
    required_kinds = list(dict.fromkeys(required_candidate_kinds or []))
    if not required_kinds or has_required_provider_place_selection(
        authoritative_tool_messages(authoritative_tool_results),
        expected_worker=_NODE_NAME,
        required_candidate_kinds=required_kinds,
    ):
        return
    available_names = {
        tool.get("schema", {}).get("function", {}).get("name", "")
        for tool in available_tools
    }
    if "global_place_search" not in available_names:
        return
    discovery_name = next(
        (name for name in _SCOPED_DISCOVERY_TOOLS if name in available_names),
        None,
    )
    country_codes = {
        boundary.get("country_code", "").casefold()
        for boundary in destination_boundaries
        if boundary.get("country_code")
    }
    country_code = next(iter(country_codes)) if len(country_codes) == 1 else None
    # ── Visit 确定性预绑定的查询公式（零 LLM 实测选出）──────────────────────
    #
    # 受控目的地边界的 name 是 /api/places 落库的短名（PlaceIdentity.name，
    # accept-language=zh-CN,zh,en），不是 Nominatim 全地址串。逗号式
    # "<类别>, <地点>" 会被 Nominatim 当成结构化地址逐段匹配，地点一旦是全串就几乎
    # 全灭；"<类别> in <地点>" 是 special-phrase 语法，先解析地点再在其范围内找类别。
    # 六城零 LLM 探针（limit=10，按国家码过滤，只数通过 visit 准入、非行政区划、
    # 有真实名字的具体地点）：
    #
    #   公式                          大阪 巴黎 曼谷 纽约 京都 罗马  合计
    #   'museum, <display_name>' 旧式   10    1    0    0    0    0    11
    #   'museum, <短名>'                10    1   10    3   10    1    35
    #   'museum in <短名>'   ← 采用     10   10   10   10   10   10    60
    #   'attraction in <短名>'          10   10   10   10   10   10    60
    #
    # 两个 in-式并列满额，选 museum 不是因为条数而是因为逐条可用性：museum 100%
    # 返回 tourism;museum，条条是有地址、能开门进去的场馆；attraction 返回的
    # tourism;attraction 在 OSM 里是杂物抽屉，实测混进纪念铭牌（Site of the Beach
    # Pneumatic Subway）、集合点指示牌（阪急BIGMAN）和错标（京都的 "Location de
    # voiture" 租车行）。Visit 准入是排除法（services/candidate_admission.py:142-145），
    # 这些杂物会照样过闸落进用户行程，而这条确定性路径没有模型在回路里挡它。
    #
    # 复测过生产原样短名（PlaceIdentity.name 并不总等于查询词）：'大阪市' 10/10、
    # '京都市' 10/10、'纽约;紐約' 10/9——多脚本名也无需归一化。
    if "visit" in required_kinds and country_code and len(destination_boundaries) == 1:
        boundary = destination_boundaries[0]
        visit_arguments: Dict[str, Any] = {
            "query": f"museum in {boundary.get('name', '')}",
            "country_code": country_code,
            "limit": 10,
            "candidate_kind": "visit",
            "aliases": [],
        }
        destination_id = boundary.get("destination_id")
        if destination_id:
            visit_arguments["destination_place_id"] = destination_id
        latitude = boundary.get("latitude")
        longitude = boundary.get("longitude")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            visit_arguments["destination_latitude"] = latitude
            visit_arguments["destination_longitude"] = longitude
        visit_envelope = await execute_tool(
            "global_place_search",
            visit_arguments,
            available_tools=available_tools,
            node_name=_NODE_NAME,
            activation_source="candidate_gate_deterministic_visit_identity",
            **tool_context,
        )
        messages.append(
            {
                "role": "tool",
                "content": compact_tool_content_for_model(visit_envelope),
            }
        )
        authoritative_tool_results.append(visit_envelope)
        # 这条确定性路径没有模型在回路里，日志是它唯一的可归因面：绑定了几个、还是
        # 一个都没绑上、以及没绑上时是 provider 失败还是区域内查不到。
        visit_result = visit_envelope.get("sanitized_result")
        visit_places = (
            visit_result.get("results") if isinstance(visit_result, dict) else None
        ) or []
        destination_name = boundary.get("name", "")
        if visit_envelope.get("status") != "success":
            logger.warning(
                "[destination_researcher] deterministic nominatim visit unbound for %s: %s",
                destination_name,
                visit_envelope.get("result_summary") or visit_envelope.get("status"),
            )
        elif visit_places:
            logger.info(
                "[destination_researcher] deterministic nominatim visit bound: %d places for %s (e.g. %s)",
                len(visit_places),
                destination_name,
                next(
                    (
                        place.get("name")
                        for place in visit_places
                        if isinstance(place, dict) and place.get("name")
                    ),
                    "",
                ),
            )
        else:
            fallback_failure = (
                visit_result.get("identity_fallback_failure")
                if isinstance(visit_result, dict)
                else None
            )
            logger.info(
                "[destination_researcher] deterministic nominatim visit bound 0 places for %s: query=%r fallback=%s",
                destination_name,
                visit_arguments["query"],
                (fallback_failure or {}).get("reason") or "none",
            )

    # A Visit-only repair must not inherit the dining discovery path merely
    # because the overall trip has a food preference. Candidate Gate owns the
    # requested domains for this scoped round.
    if "dining" not in required_kinds:
        return

    # CN: prefer amap POI grounding (mirrors lodging).
    if await discover_amap_dining(
        messages=messages,
        available_tools=available_tools,
        destination_boundaries=destination_boundaries,
        tool_context=tool_context,
        authoritative_tool_results=authoritative_tool_results,
    ):
        return

    if discovery_name is None:
        return

    # ── Dining 确定性预绑定的查询公式（零 LLM 实测选出）────────────────────────
    #
    # 与 Visit 同一条公式，同一个理由：``"<类别> in <地点>"`` 是 Nominatim 的
    # special-phrase 语法，先解析地点再在其范围内找类别；逗号式会被当成结构化地址
    # 逐段匹配。``restaurant`` 是 Nominatim 自带的 special phrase。六城零 LLM 探针
    # （生产同路径 search_nominatim_raw + normalize_nominatim_place +
    # is_concrete_dining_place，limit=10，按国家码过滤，用生产原样短名）：
    #
    #   公式                          大阪市 巴黎 曼谷 纽约;紐約 京都市 罗马  合计
    #   'restaurant, <短名>' 逗号式       1    0    0     1      0    0    1/60
    #   'restaurant in <短名>'  ← 采用   10   10   10    10     10   10   60/60
    #
    # 60 条全部返回 amenity;restaurant，具体性过滤（``_GENERIC_DINING_NAMES`` /
    # ``_DINING_COLLECTION_TOKENS``）一条没拦。旧公式写死 ``ramen``，只有日本市场
    # 撞得上；它连巴黎/罗马/曼谷的 0 命中都不是慢，而是查不到。
    if country_code and len(destination_boundaries) == 1:
        boundary = destination_boundaries[0]
        identity_arguments: Dict[str, Any] = {
            "query": f"restaurant in {boundary.get('name', '')}",
            "country_code": country_code,
            "limit": 10,
            "candidate_kind": "dining",
            "aliases": [],
        }
        destination_id = boundary.get("destination_id")
        if destination_id:
            identity_arguments["destination_place_id"] = destination_id
        latitude = boundary.get("latitude")
        longitude = boundary.get("longitude")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            identity_arguments["destination_latitude"] = latitude
            identity_arguments["destination_longitude"] = longitude
        identity_envelope = await execute_tool(
            "global_place_search",
            identity_arguments,
            available_tools=available_tools,
            node_name=_NODE_NAME,
            activation_source="candidate_gate_deterministic_identity_discovery",
            **tool_context,
        )
        identity_payload = identity_envelope.get("sanitized_result") or {}
        identity_results = (
            identity_payload.get("results")
            if isinstance(identity_payload, dict)
            else None
        )
        selected = [
            item
            for item in (identity_results or [])[:3]
            if isinstance(item, dict) and item.get("name") and item.get("place_id")
        ]
        # 一次批量 /lookup 取全部候选的类型化双语地址（两次 provider 调用，速率门
        # 由 nominatim_place_search 自带），再逐条发质量查询。
        locality_tokens_by_place = await resolve_dining_locality_tokens(selected)
        verified_quality_envelopes: List[Dict[str, Any]] = []
        for item in selected:
            place_id = str(item["place_id"])
            locality_tokens = locality_tokens_by_place.get(place_id) or []
            if not locality_tokens:
                # 没有分店级 token，质量结果无法归到这一家门店；不发这条查询。
                logger.info(
                    "[destination_researcher] dining quality unverifiable for %s (%s): no locality token",
                    item.get("name"),
                    place_id,
                )
                continue
            aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
            quality_name = next(
                (
                    alias
                    for alias in aliases
                    if isinstance(alias, str) and re.search(r"[A-Za-z]", alias)
                ),
                str(item["name"]),
            )
            quality_envelope = await execute_tool(
                discovery_name,
                {
                    "query": f'"{quality_name}" {" ".join(locality_tokens[:2])} reviews',
                    "max_results": 10,
                },
                available_tools=available_tools,
                node_name=_NODE_NAME,
                activation_source="candidate_gate_deterministic_quality",
                **tool_context,
            )
            if quality_envelope.get("status") == "success" and dining_quality_source_matches_place(
                item,
                quality_envelope.get("sanitized_result") or {},
                locality_tokens=locality_tokens,
            ):
                bound_quality = dict(quality_envelope)
                quality_metadata = dict(bound_quality.get("metadata") or {})
                quality_metadata[QUALITY_VERIFIED_PLACE_IDS_METADATA_KEY] = [place_id]
                bound_quality["metadata"] = quality_metadata
                verified_quality_envelopes.append(bound_quality)
        if selected:
            # 质量核验是加分项，不是准入条件。核上了，这家餐厅在报告里带「外部评价
            # 已核验」；核不上，照常给出、不带标记。身份信封无论如何都注入——这些
            # 餐厅是 Provider 真返回的具体门店，丢掉它们只会让那一天没有吃饭的地方，
            # 并不会让行程更真实。旧写法「一家都核不上就一条都不注入」对整片非拉丁
            # 非 CJK 文字圈是永久关闭：``dining_quality_locality_tokens`` 只保留
            # ASCII/假名/汉字，泰文、西里尔、天城文被压成空串，分店级 token 天生取
            # 不到，于是曼谷、莫斯科这类目的地一家餐厅都出不来。
            logger.info(
                "[destination_researcher] dining options injected for %s: "
                "%d identities, %d quality-verified",
                boundary.get("name", ""),
                len(selected),
                len(verified_quality_envelopes),
            )
            for envelope in verified_quality_envelopes:
                messages.append(
                    {
                        "role": "tool",
                        # Compact but identity-preserving.
                        "content": compact_tool_content_for_model(envelope),
                    }
                )
                authoritative_tool_results.append(envelope)
            messages.append(
                {
                    "role": "tool",
                    # The identity envelope is what makes a dining candidate
                    # admissible at all; compact keeps place_id / metadata /
                    # audited envelope fields for parse.
                    "content": compact_tool_content_for_model(identity_envelope),
                }
            )
            authoritative_tool_results.append(identity_envelope)


async def destination_researcher_node(
    state: TravelAgentState, config: RunnableConfig
) -> Dict[str, Any]:
    """目的地研究员节点：收集具体 Visit/Dining 候选并输出 Research Packet。"""
    router = get_model_router()
    # Research workers run on the fast tier: they gather grounded facts via tools
    # rather than doing long-form reasoning, and the fast model avoids the slow
    # reasoning-model timeouts that starved provider grounding.  Orchestration
    # (planner/synthesizer) keeps the primary tier.
    llm = router.get_fast()
    retriever = HybridRetriever()
    stream_queue: Optional["SSEBuffer"] = config.get("configurable", {}).get("stream_queue")

    current_time = state.current_time or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    user_query = state.user_query or ""
    run_id = state.run_id

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
    recommended_tools = assignment.get("recommended_tools", [])
    excluded_tools = assignment.get("excluded_tools", [])
    excluded_candidate_ids = assignment.get("excluded_candidate_ids")
    require_current_candidate = bool(assignment.get("require_current_candidate"))
    required_candidate_kinds = assignment.get("required_candidate_kinds")
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

    # ── RAG 检索（多集合 + CRAG）─────────────────────────────────────────
    # 定向身份补研轮**不检索**。``build_destination_task_prompt`` 在这条分支上提前返回、
    # 从不打印知识库段落 —— 那一轮的合同就是「只补 Provider 身份字段，不新增描述性断言」。
    # 文本进不了 prompt，检索、精排、分级那几次调用就是白花；而 ``injected_rag_sources``
    # 若照旧交给解析器，接地守卫「模型只能引用它被展示过的那一段」这个前提就恰好在什么都
    # 没展示的那一轮失效。这个决定只写在一处。
    corpus = grounding_corpus()
    if require_current_candidate:
        rag_docs: List[Dict[str, Any]] = []
        retrieval_summary = None
        rag_context_section, injected_rag_sources = "", {}
    else:
        rag_docs, retrieval_summary = await _run_rag(
            retriever, task_desc or user_query, corpus
        )
        rag_context_section, injected_rag_sources = _format_rag_context(retriever, rag_docs)

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
        active_constraint_ids=active_constraint_ids,
    )
    system_content = inject_agent_context(system_content, state, agent_label=_NODE_NAME)

    # 这一行只报 ``inject_agent_context`` **真的追加过**的那几段。画像摘要字段的 ``len()``
    # 不是注入是否成功的证据：那个变量在本文件里唯一的用途就是被 ``len()`` 一次，
    # 画像从来没有进过这个 prompt，而日志里那个正数每轮都在，看着像注入成功了。
    # **一句量死变量的日志比没有日志更糟**：画像现在经 Constraint Pack 的
    # 【参考级背景】一节进 prompt，所以这里报的是 pack 有没有内容。
    logger.info(
        "DestinationResearcher 上下文注入: constraint_pack=%s, anchor=%s, preset=%s, knowledge=%s",
        bool(state.constraint_pack),
        bool(state.session_anchor),
        bool(state.preset_context),
        "user+factory",
    )

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
    append_recent_history(messages, state)
    destination_boundaries = build_destination_boundaries(state.controlled_trip_identity)
    # 任务提示词要逐字列出预检核验通过的餐饮 place_id，所以它在预检跑完之后才拼装
    # （见下面 try 块）；最终消息顺序保持 system -> history -> user 任务 -> 预检信封。

    # ── 工具准备（白名单过滤）──────────────────────────────────────────────
    # Candidate Gate owns scoped repair tool selection.  The initial Planner's
    # MCP subset must not make a recommended, policy-allowed independent source
    # disappear during a hard-gate retry.
    selected_servers = (
        []
        if require_current_candidate and recommended_tools
        else state.selected_mcp_servers or []
    )
    available_tools = await get_available_tools(selected_servers)
    available_tools = filter_tools_for_agent(available_tools, _NODE_NAME)
    available_tools = exclude_tools(available_tools, excluded_tools)
    available_tools = scope_destination_gap_tools(
        available_tools,
        recommended_tools,
        scoped_retry=require_current_candidate,
    )
    available_tools = prioritize_recommended_tools(available_tools, recommended_tools)

    tool_schemas = [t["schema"] for t in available_tools if "schema" in t]
    tool_cache = dict(state.tool_cache) if state.tool_cache else {}
    tool_context = build_tool_context_from_state(state)
    if state.planning_generation is None:
        raise ValueError("destination research requires a planning generation")
    generation_id = state.planning_generation.generation_id
    packet_state_key = generation_packet_key(output_key, generation_id)
    authoritative_packet_metadata = build_authoritative_research_packet_metadata(
        worker_kind=_NODE_NAME,
        run_id=run_id,
        generation_id=generation_id,
        task_id=output_key,
        constraint_pack_revision=state.constraint_pack_revision,
        fact_data_revision=state.fact_data_revision,
        query_context={
            "objective": task_desc,
            "controlled_trip_identity": state.controlled_trip_identity or {},
        },
        generated_at=datetime.datetime.now(datetime.timezone.utc),
    )
    # 两个集合，两份合同：discovery 决定这一轮去找什么，enforced 决定这一轮的
    # packet 必须包含什么。非 CN dining 只在前者里——找到就交付，找不到静默缺席。
    effective_required_candidate_kinds = resolve_required_candidate_kinds(
        required_candidate_kinds,
        task_desc=str(task_desc or ""),
        user_query=user_query,
        controlled_trip_identity=state.controlled_trip_identity or {},
    )
    discovery_candidate_kinds = resolve_discovery_candidate_kinds(
        required_candidate_kinds,
        task_desc=str(task_desc or ""),
        user_query=user_query,
        controlled_trip_identity=state.controlled_trip_identity or {},
    )

    # ── scoped identity preflight + ReAct loop ───────────────────────────
    authoritative_tool_results: List[Dict[str, Any]] = []
    # 定型步骤的墙钟起点；异常发生在定型之前时保持 None，不记一个假的耗时。
    finalize_started_at: Optional[float] = None
    try:
        # 预检信封先落在自己的列表里，任务提示词拼好后再接到 messages 后面。
        preflight_messages: List[Dict[str, Any]] = []
        if discovery_candidate_kinds:
            await discover_and_resolve_required_places(
                messages=preflight_messages,
                available_tools=available_tools,
                required_candidate_kinds=discovery_candidate_kinds,
                destination_boundaries=destination_boundaries,
                tool_context=tool_context,
                authoritative_tool_results=authoritative_tool_results,
            )
        messages.append({
            "role": "user",
            "content": build_destination_task_prompt(
                task_desc=task_desc,
                user_query=user_query,
                rag_context_section=rag_context_section,
                require_current_candidate=require_current_candidate,
                offered_dining_place_ids=collect_offered_dining_place_ids(
                    authoritative_tool_results
                ),
                destination_boundaries=destination_boundaries,
            ),
        })
        messages.extend(preflight_messages)
        # 判据用 discovery 集：它问的是「这一轮预检要绑的身份都绑上了吗」。scoped 补研轮
        # （唯一消费者，见下面的 require_current_candidate）两集恒等，因为 assignment 一到
        # 就短路。
        provider_identity_ready = bool(discovery_candidate_kinds) and (
            has_required_provider_place_selection(
                authoritative_tool_messages(authoritative_tool_results),
                expected_worker=_NODE_NAME,
                required_candidate_kinds=discovery_candidate_kinds,
            )
        )
        if require_current_candidate and provider_identity_ready:
            raw_response = ""
            pending_choice = None
        else:
            (
                raw_response,
                _tool_summaries,
                pending_choice,
                react_tool_results,
            ) = await streaming_react_loop(
                llm=llm,
                messages=messages,
                tool_schemas=tool_schemas,
                available_tools=available_tools,
                stream_queue=stream_queue,
                node_name=_NODE_NAME,
                max_iterations=_MAX_TOOL_ITERATIONS,
                can_ask_user=True,
                tool_cache=tool_cache,
                tool_context=tool_context,
            )
            authoritative_tool_results.extend(react_tool_results)

        # HITL: 若 LLM 触发 ask_user，立即中断并等待用户回答
        if pending_choice:
            logger.info(
                "DestinationResearcher 触发 ask_user: %s",
                (pending_choice.get("question") or "")[:60],
            )
            return {
                "messages": [AIMessage(content=pending_choice.get("question") or "需要用户澄清目的地信息")],
                "pending_user_choice": {
                    "questions": [
                        {
                            "question": pending_choice.get("question", ""),
                            "options": pending_choice.get("options", []),
                            "selection_type": pending_choice.get("selection_type", "single"),
                        }
                    ],
                    "round": 1,
                    "allow_free_input": pending_choice.get("allow_free_input", True),
                },
                # HITL mid-research is not a completed packet delivery.
                "agent_status": {output_key: "partial"},
                "next_agent": "HALT",
            }

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
            required_candidate_kinds=effective_required_candidate_kinds,
            authoritative_packet_metadata=authoritative_packet_metadata,
            authoritative_source_records=authoritative_retry_source_records(
                state.recommendation_catalog,
                expected_worker=_NODE_NAME,
                constraint_pack_revision=state.constraint_pack_revision,
                fact_data_revision=state.fact_data_revision,
            )
            if require_current_candidate
            else (),
            injected_rag_sources=injected_rag_sources,
        )

        logger.info(
            "DestinationResearcher 完成，key=%s candidates=%d facts=%d sources=%d finalize_ms=%.0f",
            output_key, len(packet.candidates), len(packet.fact_assertions), len(packet.source_records),
            (time.perf_counter() - finalize_started_at) * 1000.0,
        )
        _log_rag_place_funnel(
            injected_rag_sources,
            authoritative_tool_results=authoritative_tool_results,
            candidates=packet.candidates,
            corpus=corpus,
        )

        result: Dict[str, Any] = {
            "messages": [AIMessage(content=f"已核验 {len(packet.candidates)} 个目的地候选")],
            "research_packets": {packet_state_key: packet},
            "agent_status": {output_key: "completed"},
            "retrieved_docs": rag_docs,
            "retrieval_summaries": [retrieval_summary] if retrieval_summary else [],
            "tool_cache": tool_cache,
            "provider_evidence_outcomes": provider_evidence_outcomes(
                authoritative_tool_messages(authoritative_tool_results),
                expected_worker=_NODE_NAME,
                packet=packet,
                assignments=provider_assignments,
            ),
        }
        return result

    except Exception as e:
        if finalize_started_at is not None:
            logger.info(
                "DestinationResearcher 交付定型中断，key=%s finalize_ms=%.0f",
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
            "DestinationResearcher 执行失败: worker=%s key=%s last_error=%s",
            _NODE_NAME, output_key, last_error[:_LAST_ERROR_LOG_LIMIT],
        )
        result = {
            "messages": [AIMessage(content="目的地 Research Packet 生成失败")],
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



async def _run_rag(
    retriever: HybridRetriever, query: str, corpus: GroundingCorpus
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """多集合 RAG 检索 + CRAG 质量过滤。

    检索本体在 ``rag.retrieval_pipeline``：一条 query 扩写一次、跨
    (集合 × 变体) 检索、融合成一个候选池、精排一次。本函数只负责 CRAG 那一层
    ——分级、以及首轮低质量时的一次纠正性重检。

    ``corpus`` 决定查哪几个集合，**用户自己上传的资料库就在里面**，和出厂语料一起
    进同一个候选池（「平等竞争」）。这条路径此前只查四个裸的
    出厂集合名，于是资料库页上传的东西在规划里结构上不可能被读到——而快问快答那条
    路一直在读它，所以缺陷的形状是「两条执行路径对同一个问题给了两套答案」。
    """
    try:
        policy = RAGModePolicy()
        decision = policy.decide(
            RAGPolicyInput(
                query=query,
                execution_path="deep_research",
                source_condition="knowledge_base",
                candidate_count=8,
                latency_budget_ms=1500,
            )
        )
        outcome = await retrieve_for_query(
            query,
            retriever=retriever,
            collections=corpus.probe_collections,
            top_k=_RAG_CANDIDATE_TOP_K,
            use_rewrite=decision.use_rewrite,
            use_multi_query=decision.use_multi_query,
            use_hyde=decision.use_hyde,
            use_rerank=decision.use_rerank,
        )
        relabel_to_logical_collections((*outcome.docs, *outcome.pool), corpus)
        candidates = outcome.docs

        grade_result = None
        filtered_docs = candidates
        grader = RetrievalGrader()
        if decision.use_grader and candidates:
            grade_result = await grader.grade(query=query, docs=candidates)
            filtered_docs = grade_result.filtered_docs
            logger.info(
                "DestinationResearcher RAG 首轮: query=%s, candidates=%d, filtered=%d, route=%s, avg=%.2f",
                query[:60],
                len(candidates),
                len(grade_result.filtered_docs),
                grade_result.route.value,
                grade_result.avg_score,
            )

        if grade_result and grade_result.route == GradeRoute.LOW_QUALITY and candidates:
            # 纠正性重检：首轮整批被判为无关，所以它**被替换**而不是并进来。
            # 把判过是垃圾的一批留在候选池里，只会稀释重检拿回来的东西，而且
            # 两批各自的精排顺序本来就没法互相比较。
            logger.info("DestinationResearcher RAG 首轮低质量，触发重检")
            retry = await retrieve_for_query(
                query,
                retriever=retriever,
                collections=corpus.probe_collections,
                top_k=_RAG_CANDIDATE_TOP_K,
                use_rewrite=True,
                use_multi_query=True,
                use_hyde=decision.use_hyde,
                use_rerank=decision.use_rerank,
            )
            relabel_to_logical_collections((*retry.docs, *retry.pool), corpus)
            outcome = retry
            candidates = retry.docs
            grade_result = await grader.grade(query=query, docs=candidates)
            filtered_docs = grade_result.filtered_docs
            logger.info(
                "DestinationResearcher RAG 重检: candidates=%d, filtered=%d, route=%s, avg=%.2f",
                len(candidates),
                len(grade_result.filtered_docs),
                grade_result.route.value,
                grade_result.avg_score,
            )

        summary = build_retrieval_summary(
            query,
            outcome.pool,
            selected_docs=filtered_docs,
            rewritten_queries=outcome.query_variants,
            mode_decision=decision,
            grade_result=grade_result,
            collections=corpus.logical_collections,
        ).to_dict()
        return filtered_docs, summary
    except Exception as e:
        logger.warning(f"DestinationResearcher RAG 检索失败: {e}")
        summary = build_retrieval_summary(
            query,
            [],
            mode_decision={
                "retrieval_mode": "unavailable",
                "enabled_features": [],
                "limitations": ["rag_retrieval_failed"],
            },
            collections=corpus.logical_collections,
        ).to_dict()
        return [], summary


def _log_rag_place_funnel(
    injected_rag_sources: Dict[str, Dict[str, Any]],
    *,
    authoritative_tool_results: List[Dict[str, Any]],
    candidates: Sequence[Any],
    corpus: GroundingCorpus,
) -> None:
    """Print how far this round's knowledge-base nominations got, per corpus.

    The knowledge base cannot admit a place — ``place_id`` is pinned to a
    Provider result by the packet schema, and that stays true. What it can do is name
    one that the worker then resolves, the same path a place the user asked for by name
    takes in. This line is the number that describes that path — without it, the path
    can produce nothing indefinitely and nobody notices.

    **Two rows, not one.** The factory corpus and the user's own uploaded library
    compete in the same retrieval, so a pooled row cannot say which of them was
    read. ``origin=user`` is printed whenever the user's library was *queried*,
    including when it contributed zero chunks — "asked and got nothing back" and
    "never asked" are the two states this measurement most needs to keep apart,
    and only the second one means the wiring is broken. ``origin=factory``
    keeps the existing rule of staying silent when it injected nothing.

    **Measurement only.** Nothing here filters, rejects or reorders anything, and
    the counts are read after the packet is already built.
    """

    by_collection = chunk_texts_by_collection(injected_rag_sources)
    user_texts = tuple(
        text
        for collection, texts in by_collection.items()
        if corpus.is_user_owned(collection)
        for text in texts
    )
    factory_texts = tuple(
        text
        for collection, texts in by_collection.items()
        if not corpus.is_user_owned(collection)
        for text in texts
    )
    rows: List[tuple[str, tuple[str, ...]]] = []
    if factory_texts:
        rows.append(("factory", factory_texts))
    rows.append(("user", user_texts))

    observed = observed_place_nominations(
        authoritative_tool_messages(authoritative_tool_results),
        expected_worker=_NODE_NAME,
    )
    admitted_place_ids = [
        place_id
        for candidate in candidates
        if (place_id := candidate_place_id(candidate))
    ]
    for origin, texts in rows:
        funnel = measure_place_funnel(
            injected_chunk_texts=texts,
            lookups=observed.lookups,
            selectable_place_ids=observed.selectable_place_ids,
            admitted_place_ids=admitted_place_ids,
        )
        logger.info("RAG place funnel [origin=%s]: %s", origin, funnel.as_log_line())


def _format_rag_context(
    retriever: HybridRetriever, docs: List[Dict[str, Any]]
) -> tuple[str, Dict[str, Dict[str, Any]]]:
    """注入知识库上下文，并交出**这一轮模型真的读到了哪几段**。

    返回 (注入文本, 引用标识 → 服务端 SourceRecord)。第二个返回值就是这条证据
    通道的抄本：模型只能引用它读到过的那几段，服务端据此接地并写记录。

    **截断只能整条地发生。** 不许先把全部拼好再 ``[:2000]``：最后一段会被拦腰截断，而服务端
    手里是完整快照，模型引用的和它读到的就不是同一段文字了。装不下的那一段不打印、也不进抄本。

     **一段占多少字符，由渲染它的那个函数说了算 —— 直接量，不许估。** 手抄一个开销常量
     （比如按 ``正文长度 + 160`` 估，而 ``format_docs_for_prompt`` 实测只花约 107：标注头 100 +
     块间分隔 7）会每段虚报约 53 字符，累起来足以把一段**真的装得下**的正文挡在 prompt 外 ——
     语料中位数 264 字符时，记账认为只装得下 4 段，而实际 5 段才 1913 字符，还在预算里；被丢掉
     的那一段已经吃过精排与分级两次模型判断。更要紧的是那种常量是**派生量的手抄本**：格式化
     那边一改标注格式，它就静默失准，不会有任何东西把它暴露出来。
    """

    if not docs:
        return "", {}

    records = rag_chunk_source_records(docs)
    selected: List[Dict[str, Any]] = []
    used = 0
    for doc in docs:
        source_id = rag_chunk_source_id(doc)
        if source_id not in records:
            continue
        # 带上引用标识再渲染：标识本身也占预算，它是这段正文能被引用的前提。
        block = retriever.format_docs_for_prompt([{**doc, "source_record_id": source_id}])
        cost = len(block) + (len(_RAG_CONTEXT_BLOCK_SEPARATOR) if selected else 0)
        if used + cost > _RAG_CONTEXT_CHAR_BUDGET:
            break
        doc["source_record_id"] = source_id
        selected.append(doc)
        used += cost

    if not selected:
        return "", {}

    rag_text = retriever.format_docs_for_prompt(selected)
    injected = {
        doc["source_record_id"]: records[doc["source_record_id"]] for doc in selected
    }
    return f"\n\n参考知识库：\n{rag_text}", injected
