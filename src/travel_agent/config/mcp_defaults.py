"""仓库内置的 MCP server 默认配置。

从 schema 里分出来只有一个理由：这是一份**数据**（十几个 server 的命令行、包版本与
它们各自的 Key 名），不是一段结构声明。放在 `models.py` 里会让「这个字段合法范围是
什么」和「Tavily 的包版本是多少」挤在同一屏。

MCP provider 的原生环境变量名（``TAVILY_API_KEY`` 等）**保持原名**：它们要被原样
传给子进程，改成 ``JOURNEYPILOT_*`` 前缀反而会让子进程读不到。仓库自己的配置项走
统一前缀，见 `env.py`。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict

from .models import MCPServerItem

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _first_env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return ""


def _env_mapping(target_name: str, *aliases: str) -> Dict[str, str]:
    value = _first_env_value(target_name, *aliases)
    return {target_name: value} if value else {}


def _node_mcp_fields(package: str, bin_name: str) -> Dict[str, object]:
    """Node stdio server 的启动方式：本地装好的 bin 优先，否则回落到 ``npx -y``。

    镜像构建期 ``npm ci`` 到 ``<repo>/node_modules``，运行层不带 npm（npx 不存在）。
    """
    local_bin = _REPO_ROOT / "node_modules" / ".bin" / bin_name
    if local_bin.exists():
        return {"command": str(local_bin), "args": []}
    return {"command": "npx", "args": ["-y", package]}


def default_mcp_servers() -> Dict[str, "MCPServerItem"]:
    """
    提供仓库内置的 MCP 默认配置。

    设计原则（真实可用 + 官方可溯源）：
    - 检索层用 agent 原生、带引用的搜索（Tavily/Brave）+ 深度抓取（Firecrawl），
      DuckDuckGo/fetch 作为零 Key 降级兜底。
    - 数据源一律走官方 API（Duffel 航班、高德/百度地图、Open-Meteo），不用浏览器爬虫，
      也不接远程第三方聚合服务。酒店/住宿不依赖专用点评 MCP，统一由
      Tavily/Brave/Firecrawl/fetch 检索覆盖。
    - 地图做高德 + 百度双覆盖；UGC 由公开网页检索补充。

    凡带 required_env 的 server，未配置对应 Key 时会被 MCPManager 自动跳过
    （见 tools/mcp_manager.py::_missing_required_env），不影响其他 server 启动，
    因此可以「配好一个用一个」逐步开通。
    """
    return {
        # ── 检索层：agent 原生搜索 + 深度抓取 ──────────────────────────────
        "tavily-search": MCPServerItem(
            **_node_mcp_fields("tavily-mcp@0.2.21", "tavily-mcp"),
            env=_env_mapping("TAVILY_API_KEY"),
            description="Tavily agent 原生检索（搜索/提取/爬取，返回带引用结果，https://tavily.com）",
            required_env=["TAVILY_API_KEY"],
        ),
        "brave-search": MCPServerItem(
            **_node_mcp_fields("@brave/brave-search-mcp-server@2.1.0", "brave-search-mcp-server"),
            env=_env_mapping("BRAVE_API_KEY"),
            description="Brave 独立索引网络搜索（备份检索源，https://brave.com/search/api）",
            required_env=["BRAVE_API_KEY"],
        ),
        "firecrawl": MCPServerItem(
            **_node_mcp_fields("firecrawl-mcp@3.23.0", "firecrawl-mcp"),
            env=_env_mapping("FIRECRAWL_API_KEY"),
            description="Firecrawl 深度网页抓取与正文提取（反爬强，https://firecrawl.dev）",
            required_env=["FIRECRAWL_API_KEY"],
        ),
        "duckduckgo-search": MCPServerItem(
            command=sys.executable,
            args=[
                str(_REPO_ROOT / "mcp_servers" / "search" / "free_search.py")
            ],
            description="DuckDuckGo 免费网络搜索（零 Key，作为降级兜底 free_web_search 的 backbone）",
        ),
        "fetch": MCPServerItem(
            command=sys.executable,
            args=["-m", "mcp_server_fetch"],
            description="通用 HTTP 获取服务（零 Key 轻量抓取）",
        ),
        # ── 地图层：高德 + 百度双覆盖 ──────────────────────────────────────
        "baidu-maps": MCPServerItem(
            **_node_mcp_fields("@baidumap/mcp-server-baidu-map@1.0.5", "mcp-server-baidu-map"),
            env=_env_mapping("BAIDU_MAP_API_KEY"),
            description="百度地图（国内）：地理编码、POI、路线、天气、路况（https://lbsyun.baidu.com）",
            required_env=["BAIDU_MAP_API_KEY"],
        ),
        "amap-maps": MCPServerItem(
            **_node_mcp_fields("@amap/amap-maps-mcp-server@0.0.8", "mcp-amap"),
            env=_env_mapping("AMAP_MAPS_API_KEY"),
            description="高德地图（国内）：地理编码 maps_geo、天气 maps_weather、路线、POI（https://lbs.amap.com）",
            required_env=["AMAP_MAPS_API_KEY"],
        ),
        # ── 航班：仓库内 Duffel v2 MCP（全球，取代已停用的 Amadeus）────────
        # 不再使用 flights-mcp 0.1.0：它固定发送已停用的 Duffel-Version:v1，所有请求均 400。
        # 本地 server 只开放强类型 search_flights，并将 Provider 响应规范化为 long_distance route。
        # 酒店不走这里：Duffel Stays 无干净免费 MCP，住宿改由检索与抓取工具覆盖。
        "duffel-flights": MCPServerItem(
            command=sys.executable,
            args=[
                str(
                    _REPO_ROOT
                    / "mcp_servers"
                    / "flights"
                    / "duffel_mcp.py"
                )
            ],
            env=_env_mapping("DUFFEL_API_KEY_LIVE"),
            description="Duffel v2 航班搜索（全球官方 API，https://duffel.com）",
            required_env=["DUFFEL_API_KEY_LIVE"],
        ),
        # ── 火车：仓库内 12306 MCP（国内，零 Key）──────────────────────────
        # 不再使用 12306-mcp 0.3.9（已是最新发布版）：它把 12306 早已下线的
        # /otn/leftTicket/query 写死在代码里，get-tickets 恒定 404，交付出的国内
        # 行程一条城际铁路腿都没有。12306 会轮换该路径并把当前值发布在
        # /otn/leftTicket/init 的 HTML 里（CLeftTicketUrl），只有仓库内客户端能
        # 跟着轮换走；官方站点表快照见 mcp_servers/rail/station_name.js。
        "12306-train": MCPServerItem(
            command=sys.executable,
            args=[
                str(
                    _REPO_ROOT
                    / "mcp_servers"
                    / "rail"
                    / "rail_12306_mcp.py"
                )
            ],
            description="12306 火车票与车次查询服务（仓库内实现，零 Key，国内）",
        ),
        # ── 天气：独立一等源（全球，零 Key）────────────────────────────────
        # 注：mcp-openweathermap 的 fastmcp 版本存在 completion 能力声明 bug，
        # 与本项目 MCP 客户端不兼容（启动即崩），故改用零 Key 的 Open-Meteo。
        "open-meteo": MCPServerItem(
            **_node_mcp_fields("open-meteo-mcp@0.1.0", "weather-mcp"),
            description="Open-Meteo MCP：当前天气、固定 7 日预报、历史天气与空气质量（零 Key，https://open-meteo.com）",
        ),
        # ── 汇率（全球，零 Key）────────────────────────────────────────────
        "currency-exchange-mcp": MCPServerItem(
            command=sys.executable,
            args=[
                str(
                    _REPO_ROOT
                    / "mcp_servers"
                    / "currency"
                    / "frankfurter_mcp.py"
                )
            ],
            description="实时汇率换算（内置 Frankfurter API，无需 API Key，不依赖 Node）",
        ),
    }

