"""
工具注册表 (Infrastructure Layer)
统一管理本地工具和 MCP 工具，提供统一调用接口。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set

from ..entities.tool_gateway import ToolManifest, infer_tool_manifest

logger = logging.getLogger(__name__)

# 压缩目录里单条 brief 的截断长度（名称+一行描述，控制注入 token）。
_BRIEF_MAX_CHARS = 80
# 单次 search_tools 返回的工具上限（官方最佳实践：一组前缀命中，够用即可）。
DEFAULT_SEARCH_LIMIT = 5


def brief_from_description(description: str) -> str:
    """取描述首行并截断，作为压缩目录的一行说明（deferred 曝光注入 prompt 用）。"""
    if not description:
        return ""
    first_line = description.strip().splitlines()[0].strip()
    if len(first_line) > _BRIEF_MAX_CHARS:
        first_line = first_line[: _BRIEF_MAX_CHARS - 1].rstrip() + "…"
    return first_line


def _schema_item_function(item: Dict[str, Any]) -> Dict[str, Any]:
    return (item.get("schema") or {}).get("function") or {}


def compact_catalog_items(items: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """从 schema 项列表（get_tools_as_schemas 形状）构建压缩目录 [{name, brief}]。"""
    catalog: List[Dict[str, str]] = []
    for item in items:
        fn = _schema_item_function(item)
        name = fn.get("name") or ""
        if not name:
            continue
        catalog.append({"name": name, "brief": brief_from_description(fn.get("description") or "")})
    return catalog


def search_tool_items(
    query: str,
    items: Iterable[Dict[str, Any]],
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    exclude: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """在给定 schema 项集合内按名称/描述做包含+前缀匹配（无向量），返回匹配工具项。

    评分：名称前缀命中 > 名称包含 > 描述包含；整串命中额外加权。按分数降序、名称升序稳定
    排序后取前 ``limit`` 条。``exclude`` 中的工具名（已激活的）跳过。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    exclude = exclude or set()
    terms = [t for t in re.split(r"[\s,，、/]+", q) if t]

    scored: List[tuple] = []
    for item in items:
        fn = _schema_item_function(item)
        name = fn.get("name") or ""
        if not name or name in exclude or name == "search_tools":
            continue
        name_l = name.lower()
        desc_l = (fn.get("description") or "").lower()
        score = 0
        for term in terms:
            if name_l.startswith(term):
                score += 3
            elif term in name_l:
                score += 2
            elif term in desc_l:
                score += 1
        if q in name_l:
            score += 3
        elif q in desc_l:
            score += 1
        if score > 0:
            scored.append((score, name, item))

    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored[: max(1, limit)]]


class ToolRegistry:
    """统一工具注册与执行中心"""

    def __init__(self) -> None:
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters_schema: Dict[str, Any],
        executor,  # callable: async fn(**kwargs) -> dict
        source: str = "local",  # "local" | "mcp"
        server_name: Optional[str] = None,
        manifest: Optional[ToolManifest | Dict[str, Any]] = None,
    ) -> None:
        """注册一个工具"""
        resolved_manifest = infer_tool_manifest(
            tool_name=name,
            description=description,
            source=source,
            server_name=server_name,
            manifest=manifest,
        )
        self._tools[name] = {
            "name": name,
            "description": description,
            "parameters_schema": parameters_schema,
            "executor": executor,
            "source": source,
            "server_name": server_name,
            "manifest": resolved_manifest,
        }
        logger.debug(f"工具注册: [{name}] from {source}")

    def has_tool(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._tools

    def get_tool_metadata(self, name: str) -> Dict[str, Any]:
        """返回不含 executor 的工具元数据，供治理层构造 audit envelope。"""
        tool = self._tools.get(name) or {}
        return {
            "name": tool.get("name", name),
            "description": tool.get("description", ""),
            "source": tool.get("source", "unknown"),
            "server_name": tool.get("server_name"),
            "parameters_schema": tool.get("parameters_schema") or {},
            "manifest": (
                tool.get("manifest").model_dump(mode="json")
                if hasattr(tool.get("manifest"), "model_dump")
                else tool.get("manifest")
            ) or infer_tool_manifest(
                tool_name=name,
                description=tool.get("description", ""),
                source=tool.get("source", "unknown"),
                server_name=tool.get("server_name"),
            ).model_dump(mode="json"),
        }

    def get_tools_as_schemas(self) -> List[Dict[str, Any]]:
        """
        返回所有工具的 OpenAI function calling 格式 schema 列表。
        同时返回 executor 以便后续调用。
        """
        result = []
        for tool in self._tools.values():
            result.append({
                "schema": {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters_schema"],
                    },
                },
                "executor": tool["executor"],
                "source": tool["source"],
                "server_name": tool.get("server_name"),
                "manifest": (
                    tool.get("manifest").model_dump(mode="json")
                    if hasattr(tool.get("manifest"), "model_dump")
                    else tool.get("manifest")
                ),
            })
        return result

    def compact_catalog(self, names: Optional[Iterable[str]] = None) -> List[Dict[str, str]]:
        """返回压缩目录 [{name, brief}]（brief=描述首行截断）。

        Tool Search 上下文节省（deferred 曝光）下注入 system prompt 的候选工具清单。``names=None`` 取全部
        已注册工具；否则按给定名称过滤（保持给定顺序，忽略未注册名）。
        """
        if names is None:
            selected = list(self._tools.values())
        else:
            selected = [self._tools[n] for n in names if n in self._tools]
        return [
            {"name": t["name"], "brief": brief_from_description(t.get("description") or "")}
            for t in selected
        ]

    def full_schemas(self, names: Iterable[str]) -> List[Dict[str, Any]]:
        """返回给定名称工具的完整 schema 项（get_tools_as_schemas 形状，保持给定顺序）。

        search_tools 命中后据此激活工具完整定义。
        """
        result: List[Dict[str, Any]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            result.append({
                "schema": {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters_schema"],
                    },
                },
                "executor": tool["executor"],
                "source": tool["source"],
                "server_name": tool.get("server_name"),
                "manifest": (
                    tool.get("manifest").model_dump(mode="json")
                    if hasattr(tool.get("manifest"), "model_dump")
                    else tool.get("manifest")
                ),
            })
        return result

    async def execute(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        """执行工具调用"""
        if tool_name not in self._tools:
            return {"success": False, "error": f"工具不存在: {tool_name}"}
        try:
            executor = self._tools[tool_name]["executor"]
            result = await executor(**kwargs)
            if not isinstance(result, dict):
                result = {"success": True, "result": result}
            return result
        except Exception as e:
            logger.warning(f"工具 [{tool_name}] 执行异常: {e}")
            return {"success": False, "error": str(e)}

    def list_tools(self) -> List[Dict[str, Any]]:
        """列出所有注册的工具（简要信息）"""
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "source": t["source"],
                "server_name": t.get("server_name"),
                "manifest": (
                    t.get("manifest").model_dump(mode="json")
                    if hasattr(t.get("manifest"), "model_dump")
                    else t.get("manifest")
                ),
            }
            for t in self._tools.values()
        ]

    @property
    def count(self) -> int:
        return len(self._tools)


# 全局注册表单例
_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
