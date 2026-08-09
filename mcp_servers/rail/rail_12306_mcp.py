#!/usr/bin/env python3
"""JourneyPilot-owned 12306 MCP server (replaces the dead ``12306-mcp`` package).

``12306-mcp@0.3.9`` is the newest published release and it hardcodes
``/otn/leftTicket/query``, a path 12306 retired: every ``get-tickets`` call
returns HTTP 404, so the pin cannot move and a domestic itinerary loses every
intercity rail leg.  This server keeps the three tool names and argument shapes
the repo already couples to, and reads the live query path out of
``/otn/leftTicket/init`` on every call.

All decode logic lives in ``travel_agent.services.rail_12306`` so it is unit
testable without a subprocess.  ``get-tickets`` answers with a JSON payload
(structured, code-consumed); ``get-interline-tickets`` and
``get-station-code-of-citys`` answer with the text/JSON string their only
consumers read.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from travel_agent.services.rail_12306 import (
    get_interline_tickets_text,
    get_station_code_of_citys_text,
    get_tickets_payload,
)


server = Server("journeypilot-12306-rail")

_STATION_ARG_DESCRIPTION = (
    "出发地/到达地的中文车站名（如 \"上海虹桥\"）或 12306 `station_code` 电报码"
    "（如 \"AOH\"，可通过 `get-station-code-of-citys` 查询）"
)
_DATE_ARG_DESCRIPTION = (
    "查询日期，格式为 \"yyyy-MM-dd\"。12306 常规余票窗口为今天起 15 天内。"
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get-tickets",
            description=(
                "查询 12306 真实余票信息：返回 JSON，results 为可购票车次列表，每条含 "
                "train_code、出发/到达车站名与电报码、departure_time/arrival_time、"
                "duration_minutes、min_price_cny（最低可购席别票价）、"
                "second_class_price_cny 与 fare_summary（各席别票价与余票状态）。"
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "date": {
                        "type": "string",
                        "format": "date",
                        "description": _DATE_ARG_DESCRIPTION,
                    },
                    "fromStation": {
                        "type": "string",
                        "description": _STATION_ARG_DESCRIPTION,
                    },
                    "toStation": {
                        "type": "string",
                        "description": _STATION_ARG_DESCRIPTION,
                    },
                },
                "required": ["date", "fromStation", "toStation"],
            },
        ),
        Tool(
            name="get-interline-tickets",
            description=(
                "查询 12306 真实中转换乘余票信息：返回每条换乘方案的总历时、"
                "换乘车站与等待时间，以及各段车次的时刻与票价。"
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "date": {
                        "type": "string",
                        "format": "date",
                        "description": _DATE_ARG_DESCRIPTION,
                    },
                    "fromStation": {
                        "type": "string",
                        "description": _STATION_ARG_DESCRIPTION,
                    },
                    "toStation": {
                        "type": "string",
                        "description": _STATION_ARG_DESCRIPTION,
                    },
                    "middleStation": {
                        "type": "string",
                        "description": "可选：指定中转车站的中文名或 `station_code`。",
                    },
                },
                "required": ["date", "fromStation", "toStation"],
            },
        ),
        Tool(
            name="get-station-code-of-citys",
            description="通过中文城市名查询代表该城市的 12306 `station_code`。",
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "citys": {
                        "type": "string",
                        "description": (
                            "要查询的城市，比如 \"上海\"。查询多个城市时用 | 分隔，"
                            "比如 \"上海|杭州\"。"
                        ),
                    }
                },
                "required": ["citys"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "get-tickets":
        # JSON, not prose: the repo binds a real train from these rows in code, and
        # the governance sanitizer flattens + truncates any long string to a single
        # 900-char line.  ``tools/mcp_manager.py::_normalized_rail_result`` unwraps
        # this text back into the payload.
        text = json.dumps(
            await get_tickets_payload(
                str(arguments["date"]),
                str(arguments["fromStation"]),
                str(arguments["toStation"]),
            ),
            ensure_ascii=False,
        )
    elif name == "get-interline-tickets":
        text = await get_interline_tickets_text(
            str(arguments["date"]),
            str(arguments["fromStation"]),
            str(arguments["toStation"]),
            str(arguments.get("middleStation") or ""),
        )
    elif name == "get-station-code-of-citys":
        text = get_station_code_of_citys_text(str(arguments["citys"]))
    else:
        raise ValueError(f"unknown tool: {name}")
    return [TextContent(type="text", text=text)]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
