#!/usr/bin/env python3
"""JourneyPilot-owned Duffel v2 MCP server."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from travel_agent.services.duffel_flight_search import search_duffel_flights


server = Server("journeypilot-duffel-v2")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_flights",
            description=(
                "通过 Duffel v2 查询真实一程航班方案。origin/destination 必须是精确 IATA 机场或城市代码；"
                "返回可无损绑定为 long_distance TransportCandidate 的完整航段、时刻、承运人与班次。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "params": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "type": {"const": "one_way", "type": "string"},
                            "origin": {"type": "string", "pattern": "^[A-Za-z]{3}$"},
                            "destination": {"type": "string", "pattern": "^[A-Za-z]{3}$"},
                            "departure_date": {"type": "string", "format": "date"},
                            "adults": {"type": "integer", "minimum": 1, "maximum": 9},
                            "cabin_class": {
                                "type": "string",
                                "enum": ["first", "business", "premium_economy", "economy"],
                            },
                            "max_connections": {"type": "integer", "minimum": 0, "maximum": 4},
                            "require_cross_day": {
                                "type": "boolean",
                                "description": (
                                    "仅当用户明确要求跨日或跨夜长途交通时设为 true；"
                                    "此时无真实跨日 Provider 路线则明确失败。"
                                ),
                            },
                        },
                        "required": [
                            "type",
                            "origin",
                            "destination",
                            "departure_date",
                            "adults",
                            "cabin_class",
                        ],
                    }
                },
                "required": ["params"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "search_flights":
        raise ValueError(f"unknown tool: {name}")
    result = await search_duffel_flights(arguments.get("params") or {})
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
