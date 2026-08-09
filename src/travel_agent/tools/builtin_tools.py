"""
内置工具（精简版）

保留受控本地边界工具：
  - Scope 阶段 clarifier_node：对整体请求的初判澄清
  - destination_researcher：遇到目的地重大歧义时主动澄清（通过 ReAct 工具调用触发）
  - destination_researcher：通过 Nominatim 查询全球稳定地点身份与国家边界
其它 Worker（transport/accommodation/itinerary）保持禁用 ask_user。

其他内置工具（预算计算、汇率查询、旅行须知）已移除：
  - 汇率查询：通过 MCP 搜索工具覆盖
  - 旅行须知：通过 RAG 知识库覆盖
  - 预算计算：通过 LLM 内置知识覆盖
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..services.global_route_search import global_route_search
from ..services.nominatim_place_search import global_place_search
from .registry import get_tool_registry

logger = logging.getLogger(__name__)


async def ask_user_for_input(
    question: str,
    options: List[Dict[str, Any]],
    selection_type: str = "single",
    allow_free_input: bool = True,
) -> Dict[str, Any]:
    """
    特殊工具：触发用户交互，暂停当前 Agent 推理等待用户输入。
    返回含 type='user_input_required' 的标记 dict。
    """
    return {
        "type": "user_input_required",
        "question": question,
        "options": options,
        "selection_type": selection_type,
        "allow_free_input": allow_free_input,
    }


ASK_USER_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "向用户提出的问题，简明扼要说明需要什么信息",
        },
        "options": {
            "type": "array",
            "description": "供用户选择的选项列表，2到6个",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "选项唯一标识，如 'a', 'b'"},
                    "label": {"type": "string", "description": "选项显示文本"},
                    "description": {"type": "string", "description": "选项补充说明（可选）"},
                },
                "required": ["id", "label"],
            },
            "minItems": 2,
            "maxItems": 6,
        },
        "selection_type": {
            "type": "string",
            "enum": ["single", "multiple"],
            "description": "选择类型：single=单选，multiple=多选",
            "default": "single",
        },
        "allow_free_input": {
            "type": "boolean",
            "description": "是否允许用户手动输入自定义答案",
            "default": True,
        },
    },
    "required": ["question", "options"],
}

GLOBAL_PLACE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
            "description": "具体景点、餐饮门店或住宿 property 名称，建议同时包含城市/行政区。",
        },
        "country_code": {
            "type": "string",
            "pattern": "^[A-Za-z]{2}$",
            "description": "受控目的地的 ISO 3166-1 alpha-2 国家码，例如日本 jp。",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "default": 5,
        },
        "destination_place_id": {
            "type": "string",
            "pattern": "^osm:relation:[1-9][0-9]*$",
            "description": "可选的受控 OSM 目的地 relation；具体餐馆或景点文本搜索未命中时用于区域内身份解析。",
        },
        "destination_latitude": {
            "type": "number",
            "minimum": -90,
            "maximum": 90,
            "description": "受控目的地 Provider 纬度；与经度一起限制区域内的具体餐馆/景点身份查询。",
        },
        "destination_longitude": {
            "type": "number",
            "minimum": -180,
            "maximum": 180,
            "description": "受控目的地 Provider 经度；与纬度一起限制区域内的具体餐馆/景点身份查询。",
        },
        "candidate_kind": {
            "type": "string",
            "enum": ["dining", "visit"],
            "description": "定向补研具体门店身份时传所在域：餐饮传 dining，景点传 visit。",
        },
        "aliases": {
            "type": "array",
            "items": {"type": "string", "minLength": 2, "maxLength": 120},
            "maxItems": 3,
            "description": "同一外部发现结果明确给出的门店别名；不得由模型新造实体。",
        },
    },
    "required": ["query", "country_code"],
}

GLOBAL_ROUTE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "from_name": {"type": "string", "minLength": 1},
        "from_place_id": {"type": "string", "minLength": 1},
        "from_latitude": {"type": "number", "minimum": -90, "maximum": 90},
        "from_longitude": {"type": "number", "minimum": -180, "maximum": 180},
        "to_name": {"type": "string", "minLength": 1},
        "to_place_id": {"type": "string", "minLength": 1},
        "to_latitude": {"type": "number", "minimum": -90, "maximum": 90},
        "to_longitude": {"type": "number", "minimum": -180, "maximum": 180},
        "departure_time": {
            "type": "string",
            "format": "date-time",
            "description": "带目的地时区偏移的 ISO 8601 出发时间。",
        },
        "mode": {
            "type": "string",
            "enum": ["public_transit", "walk", "drive", "bike"],
        },
    },
    "required": [
        "from_name",
        "from_place_id",
        "from_latitude",
        "from_longitude",
        "to_name",
        "to_place_id",
        "to_latitude",
        "to_longitude",
        "departure_time",
        "mode",
    ],
}


# ---------------------------------------------------------------------------
# search_tools 元工具（Tool Search 按需工具曝光）
# ---------------------------------------------------------------------------
# search_tools 是 ReAct 循环**就地拦截**的元工具（不走 ToolRegistry 执行）：deferred
# 曝光下 worker 初始只带这一个工具 + 压缩目录，模型用它按 query 检索并激活所需工具的完整
# schema。检索范围恒为「该 agent 白名单」（循环持有的 available_tools），因此治理边界不放松。
# 之所以不注册进 ToolRegistry：搜索范围要按 run 内实际可用工具（白名单 ∩ selected_mcp_servers）
# 界定，只有循环持有这个上下文；registry executor 拿不到，硬编一份等于复制治理逻辑。
SEARCH_TOOLS_NAME = "search_tools"

SEARCH_TOOLS_DESCRIPTION = (
    "在你被授权的工具集中按关键词检索并激活工具。未加载完整定义的工具需先用本工具找到、"
    "激活后才能调用。支持中文（地图/航班/酒店/汇率/景点）或工具名前缀（amap_/duffel_）检索；"
    "同前缀的一组工具一次即可命中，激活后当轮与后续轮次持续可用，无需重复检索。"
)

SEARCH_TOOLS_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEARCH_TOOLS_NAME,
        "description": SEARCH_TOOLS_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词，如「地图」「航班」「amap」「hotel」。",
                }
            },
            "required": ["query"],
        },
    },
}


def build_search_tools_item() -> Dict[str, Any]:
    """构造 search_tools 的曝光工具项（schema 项形状，供 deferred 曝光注入）。"""
    return {
        "schema": SEARCH_TOOLS_SCHEMA,
        "executor": None,  # 循环就地拦截，不经 registry 执行
        "source": "builtin",
        "server_name": None,
        "manifest": None,
    }


def register_builtin_tools() -> None:
    """Register controlled HITL and global place identity tools."""
    registry = get_tool_registry()

    registry.register(
        name="ask_user",
        description=(
            "当缺少完成任务所需的关键信息时，向用户提问并给出2到6个备选选项。"
            "用户可以选择预设选项或手动输入自定义答案。"
            "调用此工具后，当前推理将暂停等待用户响应。"
            "使用范围：Scope clarifier 与 destination_researcher（仅在重大歧义时），"
            "其它 Worker 不允许调用。"
        ),
        parameters_schema=ASK_USER_SCHEMA,
        executor=ask_user_for_input,
        source="builtin",
    )

    registry.register(
        name="global_place_search",
        description=(
            "使用全球 OpenStreetMap Nominatim/Overpass 搜索具体地点。返回 provider 原始稳定 place_id、"
            "provider_place_type、provider_country_code、完整地址和坐标；country_code 必须来自"
            "受控目的地，适合海外景点、具体餐饮门店与住宿 property 身份核验。"
        ),
        parameters_schema=GLOBAL_PLACE_SEARCH_SCHEMA,
        executor=global_place_search,
        source="builtin",
        server_name="nominatim",
        manifest={
            "category": "search",
            "permission_class": "external_read",
            "operation_sensitivity": "medium",
            "auth_boundary": "none",
            # 地点身份不接受替身：与 fallback._FALLBACK_MAP 里「没有 global_place_search
            # 条目」是同一条约定的两半，缺一半就是一句无人执行的声明。
            "allow_offline_fallback": False,
            "evidence_allowed": True,
            "side_effecting": False,
            "irreversible": False,
            "untrusted_content_policy": "untrusted_summary",
        },
    )

    registry.register(
        name="global_route_search",
        description=(
            "查询一个具体 from→to 路线。中国大陆及周边由高德路径规划作答，其余地区由全球 "
            "MOTIS/Transitous 作答，按端点坐标自动选择，调用方不需要也不能指定 provider。"
            "public_transit 返回真实公交/地铁/铁路换乘，walk/drive/bike 返回完整直达路线；"
            "端点必须逐字使用上游地点名称、稳定 place_id 和坐标，departure_time 必须带目的地时区。"
            "返回 routes[0] 可直接无损复制进 TransportCandidate，并附完整有界 provider_response。"
        ),
        parameters_schema=GLOBAL_ROUTE_SEARCH_SCHEMA,
        executor=global_route_search,
        source="builtin",
        # The tool has two upstreams behind it, so the server name is the tool's own
        # dispatch layer rather than either supplier: naming one of them here would
        # attribute every mainland route to a provider that never saw the query.
        server_name="route_search",
        manifest={
            "category": "data",
            "permission_class": "external_read",
            "operation_sensitivity": "medium",
            "auth_boundary": "none",
            "allow_offline_fallback": False,
            "evidence_allowed": True,
            "side_effecting": False,
            "irreversible": False,
            "untrusted_content_policy": "untrusted_summary",
        },
    )

    logger.info("已注册 3 个内置工具（ask_user, global_place_search, global_route_search）")
