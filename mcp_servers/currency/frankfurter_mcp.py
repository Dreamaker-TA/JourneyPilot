#!/usr/bin/env python3
"""
汇率 MCP（stdio）— 使用 Frankfurter 公共 API，无需 API Key。
替代 npm 包 currency-exchange-mcp（其 dist 在 Node 18+ 下因重复 shebang 无法作为 ESM 加载）。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# The rate publisher has one address in this repo.  The deterministic converter
# (``services/currency_conversion.py``, which turns a Duffel fare quoted in the
# account's currency into the ``*_cny`` the delivery contract requires) reads the
# same publisher, and two spellings of one URL is how a repo ends up asking two
# different services the same question.
from travel_agent.services.currency_conversion import FRANKFURTER_BASE_URL

BASE_URL = FRANKFURTER_BASE_URL

server = Server("frankfurter-currency")


def _format_json(data: Any) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)


def _latest_only_date_error(arguments: dict) -> str | None:
    raw = str(arguments.get("requested_date") or "").strip()
    if not raw:
        return None
    try:
        requested = date.fromisoformat(raw)
    except ValueError:
        return "参数无效：requested_date 必须使用 YYYY-MM-DD。"
    return (
        f"当前汇率工具只支持 Provider 标记日期的 latest，不能把最新汇率声明为 "
        f"{requested.isoformat()} "
        "的日期事实；未向 Frankfurter 发起请求。"
    )


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="convert_currency",
            description=(
                "按最新公开汇率将金额从一种货币换算为另一种（ISO 4217 三位代码，如 USD、CNY、EUR）。"
                "数据来自 Frankfurter，无 API Key。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "number",
                        "description": "要换算的金额（正数）",
                    },
                    "from_currency": {
                        "type": "string",
                        "description": "源货币代码，如 USD",
                    },
                    "to_currency": {
                        "type": "string",
                        "description": "目标货币代码，如 CNY",
                    },
                    "requested_date": {
                        "type": "string",
                        "format": "date",
                        "description": (
                            "可选。当前工具只支持 latest；传入历史或未来日期时会明确拒绝，"
                            "不会用最新汇率冒充指定日期汇率。"
                        ),
                    },
                },
                "required": ["amount", "from_currency", "to_currency"],
            },
        ),
        Tool(
            name="latest_exchange_rates",
            description=(
                "获取指定基准货币对一组目标货币的最新汇率（Frankfurter 支持的币种）。"
                "to_currencies 为逗号分隔或单币种代码，如 \"EUR,GBP,JPY\"。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "base_currency": {
                        "type": "string",
                        "description": "基准货币，如 USD",
                    },
                    "to_currencies": {
                        "type": "string",
                        "description": "目标货币，逗号分隔，如 EUR,GBP 或单个 CNY",
                    },
                    "requested_date": {
                        "type": "string",
                        "format": "date",
                        "description": (
                            "可选。当前工具只支持 latest；传入历史或未来日期时会明确拒绝。"
                        ),
                    },
                },
                "required": ["base_currency", "to_currencies"],
            },
        ),
    ]


async def _get_json(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    r = await client.get(url)
    r.raise_for_status()
    return r.json()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    date_error = _latest_only_date_error(arguments)
    if date_error is not None:
        return [TextContent(type="text", text=date_error)]

    if name == "convert_currency":
        amount = float(arguments.get("amount", 0))
        from_c = str(arguments.get("from_currency", "")).strip().upper()
        to_c = str(arguments.get("to_currency", "")).strip().upper()
        if amount <= 0 or not from_c or not to_c:
            return [TextContent(type="text", text="参数无效：amount 须为正数，货币代码不能为空。")]

        url = f"{BASE_URL}/latest?amount={amount}&from={from_c}&to={to_c}"
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                data = await _get_json(client, url)
        except httpx.HTTPStatusError as e:
            return [TextContent(type="text", text=f"汇率 API 错误: {e.response.status_code} {e.response.text[:500]}")]
        except Exception as e:
            return [TextContent(type="text", text=f"请求失败: {e}")]

        return [TextContent(type="text", text=_format_json(data))]

    if name == "latest_exchange_rates":
        base = str(arguments.get("base_currency", "")).strip().upper()
        to_raw = str(arguments.get("to_currencies", "")).strip().upper()
        if not base or not to_raw:
            return [TextContent(type="text", text="base_currency 与 to_currencies 不能为空。")]

        to_list = [c.strip() for c in to_raw.replace(";", ",").split(",") if c.strip()]
        if not to_list:
            return [TextContent(type="text", text="to_currencies 解析后为空。")]

        to_param = ",".join(to_list)
        url = f"{BASE_URL}/latest?from={base}&to={to_param}"
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                data = await _get_json(client, url)
        except httpx.HTTPStatusError as e:
            return [TextContent(type="text", text=f"汇率 API 错误: {e.response.status_code} {e.response.text[:500]}")]
        except Exception as e:
            return [TextContent(type="text", text=f"请求失败: {e}")]

        return [TextContent(type="text", text=_format_json(data))]

    return [TextContent(type="text", text="未知工具")]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
