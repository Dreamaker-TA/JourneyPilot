"""
工具降级策略 (Application Layer)

当主工具重试耗尽后，系统自动降级到备用工具。
降级是系统级决策（非 LLM 发起），但仍必须通过当前 Agent 的工具白名单和 ToolGateway。
备用工具既要在全局 ToolRegistry 中注册，也要在当前 agent allowlist 中可见。

降级结果标记 degraded=True + original_tool=原工具名，供思维链的工具检查面与
tool audit 记录展示实际执行路径。

降级映射（按工具函数名）——**只有网页检索/抓取层有降级边**：
  tavily / brave / firecrawl: 网页检索/抓取层 → free_web_search（共 5 条边）

检索工具降级到检索工具是同类替换：两边产出的都是「网页文本 + URL」，
消费路径完全一致，所以付费额度耗尽或供应商故障时降级是净收益。
其余所有工具没有降级边，理由分两类（见 P16-D2）：

1. **散文产不出结构化身份**（F3.2 的推广）：路线、站点代码、航班、地点身份
   都是 Provider 的结构化记录，``free_web_search`` 只返回散文。替身既绑不成
   typed candidate，又会在证据闭包里留下一条 degradation 记录把闭包标成
   FAILED——纯损失。因此 ``maps_direction_driving`` / ``maps_direction_walking``
   / ``maps_direction_transit_integrated`` / ``get-station-code-of-citys`` /
   ``search_flights`` 一律硬失败，不降级。
2. **死键**：表里写一个本仓库没注册过的名字，``registry.has_tool`` 会在 gateway 就拦住，
   那条降级边永远跑不到。12306 的真名是 ``get-tickets`` / ``get-interline-tickets`` /
   ``get-station-code-of-citys``（三者由仓库内
   ``mcp_servers/rail/rail_12306_mcp.py::list_tools`` 注册，前两者的日期能力由
   ``tools/temporal.py::_CAPABILITIES`` 钉住）；duffel 只暴露 ``search_flights``
   （``mcp_servers/flights/duffel_mcp.py:27``）。

**新增降级边必须先拿到 Provider 注册面证据**（包名@版本 + 注册那一行），
否则就是又一个死键：映射表只收录确实暴露了对应工具的注册面。

注：geocode / weather 工具（maps_geo / maps_weather 等）由 Worker 的
deny_tools 排除（见 agents/utils.py: _AGENT_TOOL_POLICY），且
交付投影不调用工具；需要刷新坐标的 provider adapter 不经过本降级路径，
因此不在映射中。

**地点身份类工具一律没有降级边**，无论 Provider 是 Nominatim 还是高德：

- ``global_place_search``（Nominatim 全球地点身份）：网页散文永远产不出 Provider
  地点身份，``research_packet_output._successful_place_records`` 只认
  ``tool_name == "global_place_search"`` 且 ``status == "success"`` 的信封。
- ``maps_text_search`` / ``maps_around_search`` / ``maps_search_detail``（高德 POI）：
  三条消费路径全部封死——确定性住宿预绑定丢弃 ``status != "success"``，坐标补全对
  payload 直接 ``json.loads`` 会在散文上抛异常，ReAct 路径只会把替身变成 rejected
  来源。``maps_search_detail`` 更彻底：唯一参数 ``id`` 是非语义键，降级查询恒为常量
  「地点 景点」。

拿 free_web_search 顶替地点身份是纯损失——它还会在证据闭包里留下一条
degradation 记录，把整条闭包标成 FAILED。地点 Provider 失败就如实失败。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 降级映射表
# 所有条目当前均降级到 free_web_search（DuckDuckGo），由参数适配器取出原调用
# 的查询文本，拼成一句自然语言查询（见 _fallback_query_text）。
#
# 每个键都必须是上游 Provider 真实注册的工具名。下面 5 条边的注册面证据通过
# 解包 config.py 实际启动的 npm 包核实。config.py 里所有 npx
# server 都用 ``npx -y <pkg>@<version>`` 钉死版本，因此下面的版本号就是配置
# 真正请求的版本，而不是某天 latest dist-tag 的解析结果。12306 与 duffel 不在
# 那张表里：两者都是仓库内 stdio server（``mcp_servers/rail/`` 与
# ``mcp_servers/flights/``），注册面直接读源码即可：
#
#   tavily-mcp@0.2.21                       build/index.js:105  name: "tavily_search"
#   tavily-mcp@0.2.21                       build/index.js:358  name: "tavily_research"
#     （两者都在 ListToolsRequestSchema handler 内，:906-922 再次列出）
#   @brave/brave-search-mcp-server@2.1.0    dist/tools/web/index.js:4
#                                           export const name = 'brave_web_search'
#   firecrawl-mcp@3.23.0                    dist/index.js:1891
#                                           server.addTool({ name: "firecrawl_scrape"
#   firecrawl-mcp@3.23.0                    dist/index.js:1977
#                                           server.addTool({ name: "firecrawl_search"
# ---------------------------------------------------------------------------
_FALLBACK_MAP: Dict[str, List[Tuple[str, str]]] = {
    # 通用联网检索：付费额度或供应商故障时仍使用真实公共搜索，禁止静态结果兜底。
    "tavily_search": [("free_web_search", "destination_search")],
    "tavily_research": [("free_web_search", "destination_search")],
    # 网页检索/抓取层：Brave / Firecrawl（destination_researcher 的检索骨干）。
    # 供应商 key 缺失、额度耗尽或抓取失败时降级到零 Key 的 DuckDuckGo，绝不硬失败。
    "brave_web_search": [("free_web_search", "destination_search")],
    "firecrawl_search": [("free_web_search", "destination_search")],
    "firecrawl_scrape": [("free_web_search", "destination_search")],
}

# F3.3：降级查询是一句给公共搜索引擎的自然语言，不是原参数的转写。
# 主查询文本键优先，其余语义文本参数（city / date / origin / fromStation…）按
# 声明顺序补在后面。
_QUERY_TEXT_KEYS: Tuple[str, ...] = (
    "query",
    "keywords",
    "q",
    "search_query",
    "text",
    "name",
)

# 执行旋钮与 provider 内部标识：这些值在网页检索里是噪声，会把查询推离目标
# （实测 `limit=5 max_results=5 include_raw_content=True
# destination_latitude=34.6937 osm:relation:358674` 把酒店查询推到 YouTube 视频
# 和另一座城市的同名酒店）。一律不进入查询正文。
#
# 这张表按「键名」而不是「哪个工具还在映射里」收口，删条目要按键名给证据：
# 降级跑在**已经失败的那次调用参数**上，而 ToolGateway 不拿 schema 校验入参
# （见 tools/gateway.py），所以模型编造出来的、任何 schema 都没声明过的键也会
# 原样流到这里。「存活边的工具都没声明这个键」因此不足以证明它不会出现。
_NON_SEMANTIC_KEYS = frozenset(
    {
        "limit",
        "max_results",
        "offset",
        "page",
        "page_size",
        "radius",
        "include_raw_content",
        "include_answer",
        "include_images",
        "search_depth",
        "extract_depth",
        "topic",
        "format",
        "timeout",
        "id",
        "poi_id",
        "place_id",
        "destination_place_id",
        "osm_ids",
        "location",
        "lat",
        "lng",
        "latitude",
        "longitude",
        "destination_latitude",
        "destination_longitude",
        "country_code",
        "candidate_kind",
        "aliases",
    }
)


def _fallback_query_text(original_args: Dict) -> str:
    """Build the free-text query from the actual search text, nothing else.

    Only string arguments contribute, and only semantic ones: numbers and
    booleans are execution knobs, never part of what the user is searching for.
    """
    fragments: List[str] = []
    for key in _QUERY_TEXT_KEYS:
        value = original_args.get(key)
        if isinstance(value, str) and value.strip():
            fragments.append(value.strip())
    for key, value in original_args.items():
        name = str(key)
        if name in _QUERY_TEXT_KEYS or name.lower() in _NON_SEMANTIC_KEYS:
            continue
        if isinstance(value, str) and value.strip():
            fragments.append(value.strip())
    # 同一段文本可能同时出现在 query 与其它键上，去重但保序。
    return " ".join(dict.fromkeys(fragments))


# 需要降级时的参数转换
def _adapt_args_for_fallback(
    original_tool: str,
    fallback_tool: str,
    adapter_key: str,
    original_args: Dict,
) -> Dict:
    """
    将主工具的参数转换为备用工具的参数格式。
    对于 free_web_search，取原调用的查询文本 + 适配类型关键词组成一句查询；
    执行旋钮与 provider 内部标识不进入查询（F3.3）。
    """
    if fallback_tool == "free_web_search":
        query = _fallback_query_text(original_args)

        # 按适配类型追加语义关键词，与 _FALLBACK_MAP 条目一一对应。
        # 表已收口为检索层专用，所以只剩 destination_search 一个后缀；
        # route/train/station/flight 四个后缀随各自的边一起删除（P16-D2）。
        if adapter_key == "destination_search":
            query += " 旅游攻略 景点"

        return {"query": query.strip()}

    return original_args


def get_fallback_tool(tool_name: str) -> Optional[Tuple[str, str]]:
    """
    获取工具的备用工具信息。

    Returns:
        (fallback_tool_name, adapter_key) 或 None
    """
    fallbacks = _FALLBACK_MAP.get(tool_name)
    if not fallbacks:
        return None
    return fallbacks[0]  # 当前取第一个备用工具


def build_fallback_args(
    original_tool: str,
    fallback_tool: str,
    adapter_key: str,
    original_args: Dict,
) -> Dict:
    """构建备用工具的参数。"""
    return _adapt_args_for_fallback(original_tool, fallback_tool, adapter_key, original_args)
