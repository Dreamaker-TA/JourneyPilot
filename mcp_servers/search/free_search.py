#!/usr/bin/env python3
"""
免费搜索 MCP 服务器 - 使用 duckduckgo-search 库（无需 API Key）
"""

import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("free-search")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """列出可用工具"""
    return [
        Tool(
            name="free_web_search",
            description="免费网络搜索 - 使用 DuckDuckGo 进行实时搜索，无需 API Key",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "返回结果数量，默认 5",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        )
    ]


def _do_search(query: str, max_results: int = 5) -> str:
    """同步执行搜索（在线程池中运行，避免阻塞）"""
    try:
        # duckduckgo_search 已更名为 ddgs，旧包后端失效（返回 0 条结果）
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(
                    {
                        "title": r.get("title", ""),
                        "link": r.get("href", ""),
                        "snippet": r.get("body", ""),
                    }
                )
    except Exception as e:
        raise RuntimeError(f"DuckDuckGo search failed: {e}") from e

    output = f"搜索结果: {query}\n\n"
    for i, r in enumerate(results, 1):
        output += f"{i}. {r['title']}\n"
        output += f"   链接: {r['link']}\n"
        if r["snippet"]:
            output += f"   摘要: {r['snippet']}\n"
        output += "\n"
    return output


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """执行搜索"""
    if name == "free_web_search":
        query = arguments.get("query", "")
        max_results = min(int(arguments.get("max_results", 5)), 20)

        loop = asyncio.get_event_loop()
        output = await loop.run_in_executor(
            None, _do_search, query, max_results
        )
        return [TextContent(type="text", text=output)]

    raise ValueError(f"未知工具: {name}")

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
