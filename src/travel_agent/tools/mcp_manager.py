"""
MCP 工具管理器 (Infrastructure Layer)
直接使用 mcp SDK 进行服务器探测、工具注册和调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

from typing import Any, AsyncIterator, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

from ..utils.coordinates import amap_location_to_wgs84
from ..utils.rate_gate import rate_gate_for
from .temporal import temporal_schema_contract_errors

logger = logging.getLogger(__name__)

INIT_TIMEOUT_SECONDS = int(os.getenv("MCP_INIT_TIMEOUT_SECONDS", "45"))
CALL_TIMEOUT_SECONDS = int(os.getenv("MCP_CALL_TIMEOUT_SECONDS", "60"))
# Per-server leased session idle TTL. 0 disables idle eviction.
SESSION_IDLE_TTL_SECONDS = float(os.getenv("MCP_SESSION_IDLE_TTL_SECONDS", "60"))
_DUCKDUCKGO_RESULT_TITLE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")

# Stamped onto a tool result whose server declared an ``outputSchema`` that its
# own answer violates.  Present only on such a round, so its absence is the
# normal case rather than an assumption.
PROVIDER_OUTPUT_SCHEMA_VIOLATION_KEY = "provider_output_schema_violation"


class _OutputSchemaTolerantSession(ClientSession):
    """Treat a server's self-contradicting ``outputSchema`` as no schema at all.

    The MCP SDK validates ``structuredContent`` against the tool's declared
    output schema and raises a bare ``RuntimeError`` on mismatch — from inside
    ``call_tool``, discarding the whole ``CallToolResult`` including the text
    content.  Our caller cannot tell that apart from a dead pipe, so it tore the
    lease down and marked the entire server unhealthy: an application-layer
    verdict reported as a transport fault.

    A declaration a server breaks on every call carries no information.  The
    honest reading is the one already applied to the majority of tools that ship
    no ``outputSchema`` whatsoever — take the answer, and say plainly that the
    declaration was unusable (see ``PROVIDER_OUTPUT_SCHEMA_VIOLATION_KEY``).
    Live case: ``@brave/brave-search-mcp-server`` 2.1.0 declares
    ``outputSchema: <zod>.shape``, which drops the ``looseObject`` catchall and
    closes the schema, so Brave's own ``mixed`` field is rejected every time.

    Anything that is not the SDK's validation verdict propagates untouched.
    """

    def __init__(self, *args: Any, server_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._server_name = server_name
        self._output_schema_violation: Optional[str] = None

    async def _validate_tool_result(self, name: str, result: Any) -> None:
        try:
            await super()._validate_tool_result(name, result)
        except RuntimeError as exc:
            message = (str(exc) or exc.__class__.__name__)[:1000]
            self._output_schema_violation = message
            logger.warning(
                "MCP 输出 schema 声明不可用，按无声明处理 | server=%s tool=%s error=%s",
                self._server_name,
                name,
                message,
            )

    def take_output_schema_violation(self) -> Optional[str]:
        """Return the pending violation and clear it (leases outlive one call)."""
        violation = self._output_schema_violation
        self._output_schema_violation = None
        return violation


@dataclass
class _ServerSessionLease:
    """Long-lived MCP session for one server (serialized by per-server lock)."""

    server_name: str
    session: Any
    stack: AsyncExitStack
    last_used_monotonic: float
    create_count: int = 1


def _normalized_duffel_result(server_name: str, tool_name: str, payload: Any) -> Any:
    """Unwrap this repo's JSON TextContent into the canonical route result."""
    if server_name != "duffel-flights" or tool_name != "search_flights":
        return None
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return None
    text = content[0].get("text")
    if not isinstance(text, str):
        return None
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(result, dict)
        or result.get("success") is not True
        or result.get("provider") != "duffel"
        or not isinstance(result.get("routes"), list)
    ):
        return None
    return result


def _normalized_duckduckgo_result(
    server_name: str,
    tool_name: str,
    payload: Any,
) -> Any:
    """Project the repo search MCP text into complete bounded result records."""
    if server_name != "duckduckgo-search" or tool_name != "free_web_search":
        return None
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return None
    text = content[0].get("text")
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    query = ""
    if lines and lines[0].startswith("搜索结果:"):
        query = lines[0].removeprefix("搜索结果:").strip()
    results: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for line in lines[1:]:
        title_match = _DUCKDUCKGO_RESULT_TITLE.match(line)
        if title_match is not None:
            if current is not None:
                results.append(current)
            current = {
                "title": title_match.group(1).strip(),
                "url": "",
                "snippet": "",
            }
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("链接:"):
            current["url"] = stripped.removeprefix("链接:").strip()
        elif stripped.startswith("摘要:"):
            current["snippet"] = stripped.removeprefix("摘要:").strip()
    if current is not None:
        results.append(current)
    return {
        "success": True,
        "provider": "duckduckgo",
        "query": query,
        "results": results,
    }


def _normalized_rail_result(server_name: str, tool_name: str, payload: Any) -> Any:
    """Unwrap the repo 12306 server's JSON TextContent into the ticket payload.

    Without this the payload stays a JSON *string* nested at content[0].text, and
    ``tools/governance.py::_sanitize_value`` collapses it to one 900-char line — a
    389-train answer then reaches the deterministic rail binder as a single train.
    Lifting it here keeps every flat record (the ``results`` key earns the 20-item cap
    instead of the 5-item one) intact for
    ``agents/transport_researcher/node.py::_rail_train_records``.

    Only ``get-tickets`` is structured: ``get-interline-tickets`` is model-facing
    prose and ``get-station-code-of-citys`` is a short JSON string its own parser
    reads, so both keep the raw passthrough.
    """
    if server_name != "12306-train" or tool_name != "get-tickets":
        return None
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return None
    text = content[0].get("text")
    if not isinstance(text, str):
        return None
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(result, dict)
        or result.get("success") is not True
        or result.get("provider") != "12306"
        or not isinstance(result.get("results"), list)
    ):
        return None
    return result


def _amap_text_content(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        return None
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            return item["text"]
    return None


_AMAP_REFUSAL_MARKERS = (" failed:", "cuqps", "over_direction_range", "invalid_user_key")


def _amap_stated_failure(tool_name: str, text: str) -> Optional[Dict[str, Any]]:
    """A refusal amap stated in prose, or ``None`` for any other undecodable text.

    amap words every handler's refusal as ``"<what> failed: <info|infocode>"``.
    Text that says nothing of the kind stays on the generic passthrough it has
    always taken — inventing a failure from an unrecognized shape would be the
    same guess in the other direction.
    """

    lowered = text.casefold()
    if not any(marker in lowered for marker in _AMAP_REFUSAL_MARKERS):
        return None
    return _amap_failure(tool_name, text)


def _amap_failure(tool_name: str, detail: str) -> Optional[Dict[str, Any]]:
    """The failure shape for an amap answer that states a rejection.

    ``status="0"`` is amap's own "this request was refused" and ``infocode``
    carries which refusal it was — a quota allowance above all.  Returning the
    same ``success: False`` shape a protocol error produces is what lets the round
    keep a failure signature instead of a zero-result success: the deterministic
    lodging binder drops both, but only one of them tells Candidate Gate that a
    provider refused and that a retry must not re-use the same exhausted key.
    """

    text = detail.strip()
    if not text:
        return None
    return {
        "success": False,
        "provider": "amap",
        "tool_name": tool_name,
        "error": text[:1000],
    }


def _amap_declared_failure(tool_name: str, parsed: Any) -> Optional[Dict[str, Any]]:
    """Read amap's own refusal out of a decoded response body."""

    if not isinstance(parsed, dict):
        return None
    status = str(parsed.get("status") or "").strip()
    if status and status != "1":
        infocode = str(parsed.get("infocode") or "").strip()
        info = str(parsed.get("info") or "").strip()
        return _amap_failure(
            tool_name,
            f"amap {tool_name} refused: {info or 'unknown'} ({infocode or 'no infocode'})",
        )
    return None


def _normalized_amap_place_result(server_name: str, tool_name: str, payload: Any) -> Any:
    """Flatten amap POI TextContent into a shallow structured record list.

    amap returns its POIs as a big JSON string nested at content[0].text.  Left
    as-is the governance sanitizer compacts that deeply-nested string to ~120
    chars, destroying the POIs.  Mirroring the Duffel normalizer, we lift the
    POIs to a top-level ``results`` list of small dicts so their id/name/address/
    typecode (and, for detail lookups, the point geometry) survive sanitization
    for the deterministic lodging binder and the map projection.
    """
    if server_name != "amap-maps" or tool_name not in {
        "maps_text_search",
        "maps_around_search",
        "maps_search_detail",
    }:
        return None
    text = _amap_text_content(payload)
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # amap states a rejection in prose ("Text Search failed: <infocode>"),
        # which is not JSON.  The pinned server marks those with ``isError``, so
        # ``_mcp_result_error`` has already turned them into a failure by the time
        # this runs — but that guarantee belongs to one provider version, and the
        # generic passthrough below would read the prose as a successful answer.
        # Answer for the shape, not for the version.
        return _amap_stated_failure(tool_name, text)

    # maps_search_detail resolves a single POI id to its full detail, including
    # the ``location`` ("lng,lat") that text/around search omit.  Lift that one
    # record so a bound hotel can carry a map pin.
    if tool_name == "maps_search_detail":
        if not isinstance(parsed, dict):
            return None
        declared = _amap_declared_failure(tool_name, parsed)
        if declared is not None:
            return declared
        record = _amap_record(parsed)
        return {
            "success": True,
            "provider": "amap",
            "tool_name": tool_name,
            "location": parsed.get("location"),
            "results": [record] if record is not None else [],
        }

    declared = _amap_declared_failure(tool_name, parsed)
    if declared is not None:
        return declared
    pois = parsed.get("pois") if isinstance(parsed, dict) else None
    if not isinstance(pois, list):
        return None
    records: List[Dict[str, Any]] = []
    for poi in pois:
        record = _amap_record(poi)
        if record is not None:
            records.append(record)
    # Use the ``results`` key (not ``pois``): ``classify_tool_result`` only treats
    # content/result/results/routes/data/text as substantive, so a ``pois`` key
    # would classify a POI-bearing answer as EMPTY_SUCCESS — the round would be
    # attributed as "the Provider found nothing" while the deterministic lodging
    # binder and the map projection silently lose every POI.  (These three tools
    # have no web-search fallback edge; an empty verdict is a misattribution now,
    # not a substitution.)
    return {
        "success": True,
        "provider": "amap",
        "tool_name": tool_name,
        "results": records,
    }


def _amap_record(poi: Any) -> Optional[Dict[str, Any]]:
    """Project one amap POI dict into a shallow id/name/address/typecode record."""
    if not isinstance(poi, dict):
        return None
    record: Dict[str, Any] = {
        "id": str(poi.get("id") or "").strip(),
        "name": str(poi.get("name") or "").strip(),
        "address": str(poi.get("address") or "").strip(),
        "typecode": str(poi.get("typecode") or "").strip(),
    }
    # amap encodes the point geometry as a GCJ-02 "lng,lat" string; lift it to
    # numeric WGS-84 latitude/longitude so it pins against the same OSM tiles
    # every other place in the bundle does.
    latitude, longitude = amap_location_to_wgs84(poi.get("location"))
    if latitude is not None and longitude is not None:
        record["latitude"], record["longitude"] = latitude, longitude
    if record["id"] and record["name"]:
        return record
    return None


def _mcp_error_message(exc: Exception) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "初始化或调用超时"
    return str(exc) or exc.__class__.__name__


def _mcp_result_error(payload: Any) -> Optional[str]:
    """Return the protocol-level error reported by an MCP CallToolResult."""
    if not isinstance(payload, dict):
        return None
    if not (payload.get("isError") is True or payload.get("is_error") is True):
        return None

    messages: List[str] = []
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                if text:
                    messages.append(text)
    elif isinstance(content, str) and content.strip():
        messages.append(content.strip())
    return "；".join(messages)[:1000] or "MCP 工具返回失败结果"


@dataclass
class MCPServerState:
    name: str
    description: str = ""
    type: str = "stdio"
    enabled: bool = True
    configured: bool = True
    healthy: bool = False
    status: str = "pending"
    tool_count: int = 0
    tools_list: List[str] = field(default_factory=list)
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "enabled": self.enabled,
            "disabled": not self.enabled,
            "configured": self.configured,
            "healthy": self.healthy,
            "status": self.status,
            "tool_count": self.tool_count,
            "tools_list": list(self.tools_list),
            "last_error": self.last_error,
        }


class MCPManager:
    """MCP 服务器管理器。"""

    def __init__(self) -> None:
        self._initialized = False
        self._server_configs: Dict[str, Any] = {}
        self._server_states: Dict[str, MCPServerState] = {}
        # Per-server session lease (single session + lock, not a multi-conn pool).
        self._session_leases: Dict[str, _ServerSessionLease] = {}
        self._server_call_locks: Dict[str, asyncio.Lock] = {}
        self._lease_meta_lock = asyncio.Lock()
        # Survives lease drop so create_count is monotonic per server process lifetime.
        self._session_lease_create_totals: Dict[str, int] = {}

    async def initialize(self, mcp_servers: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """
        初始化 MCP 服务器连接，并将工具注册到 ToolRegistry。

        采用“启动时短连探测、执行时 per-server session lease 复用”的模型：
        - 启动阶段真实调用 initialize/list_tools，得到健康状态与工具清单（短连接）
        - 工具执行阶段按 server 复用 ClientSession（同 server 串行；跨 server 可并行）
        """
        if self._initialized:
            return self.get_server_states()

        from .registry import get_tool_registry

        registry = get_tool_registry()
        await self.close_all_sessions()
        self._server_configs = dict(mcp_servers)
        self._server_states = {}
        async def _initialize_server(server_name: str, server_config: Any) -> int:
            state = self._build_initial_state(server_name, server_config)
            self._server_states[server_name] = state

            if not state.enabled:
                state.status = "disabled"
                return 0

            # 使用 sse_url 时（如百度地图远程接入），AK 通常已嵌入 URL，无需 env
            missing_env = [] if getattr(server_config, "sse_url", None) else self._missing_required_env(server_config)
            if missing_env:
                state.configured = False
                state.status = "misconfigured"
                state.last_error = f"缺少必需环境变量: {', '.join(missing_env)}"
                logger.warning(
                    "MCP 服务器 [%s] 缺少必需环境变量: %s",
                    server_name,
                    ", ".join(missing_env),
                )
                return 0

            if not self._is_transport_configured(server_config):
                state.configured = False
                state.status = "misconfigured"
                state.last_error = "未配置 command 或 sse_url"
                logger.warning(f"MCP 服务器 [{server_name}] 配置不完整")
                return 0

            try:
                tools = await self._list_server_tools(server_name, server_config)
                normalized_tools = [
                    self._normalize_tool_definition(tool) for tool in tools
                ]
                contract_errors = temporal_schema_contract_errors(
                    server_name=server_name,
                    tool_definitions=normalized_tools,
                )
                if contract_errors:
                    raise RuntimeError(
                        "date-sensitive MCP schema contract mismatch: "
                        + "; ".join(contract_errors)
                    )
                tool_names: List[str] = []

                for tool_meta in normalized_tools:
                    tool_name = tool_meta["name"]
                    if not tool_name:
                        continue

                    registry.register(
                        name=tool_name,
                        description=tool_meta["description"],
                        parameters_schema=tool_meta["parameters_schema"],
                        executor=self._make_executor(server_name, tool_name),
                        source="mcp",
                        server_name=server_name,
                    )
                    tool_names.append(tool_name)

                state.healthy = True
                state.status = "healthy"
                state.tool_count = len(tool_names)
                state.tools_list = tool_names
                state.last_error = None
                logger.info(
                    f"MCP 服务器 [{server_name}] 已连接，注册 {len(tool_names)} 个工具"
                )
                return len(tool_names)
            except Exception as exc:
                error = _mcp_error_message(exc)
                state.healthy = False
                state.status = "error"
                state.last_error = error
                logger.error(f"MCP 服务器 [{server_name}] 初始化失败: {error}")
                return 0

        tool_counts = await asyncio.gather(*[
            _initialize_server(server_name, server_config)
            for server_name, server_config in mcp_servers.items()
        ])
        registered_tools = sum(tool_counts)

        self._initialized = True
        logger.info(
            "MCP 初始化完成 | 成功服务器: %s/%s | 工具数: %s",
            len([s for s in self._server_states.values() if s.healthy]),
            len(self._server_states),
            registered_tools,
        )
        return self.get_server_states()

    def get_server_states(self) -> Dict[str, Dict[str, Any]]:
        """返回所有 MCP 服务器的当前状态。"""
        return {name: state.to_dict() for name, state in self._server_states.items()}

    def _build_initial_state(self, server_name: str, server_config: Any) -> MCPServerState:
        server_type = "sse" if getattr(server_config, "sse_url", None) else "stdio"
        return MCPServerState(
            name=server_name,
            description=getattr(server_config, "description", "") or "",
            type=server_type,
            enabled=not getattr(server_config, "disabled", False),
        )

    def _is_transport_configured(self, server_config: Any) -> bool:
        if getattr(server_config, "sse_url", None):
            return True
        return bool(getattr(server_config, "command", None))

    def _missing_required_env(self, server_config: Any) -> List[str]:
        env = getattr(server_config, "env", None) or {}
        missing = []
        for name in getattr(server_config, "required_env", []):
            if not env.get(name):
                missing.append(name)
        return missing

    @asynccontextmanager
    async def _open_session(
        self, server_name: str, server_config: Any
    ) -> AsyncIterator[_OutputSchemaTolerantSession]:
        if getattr(server_config, "sse_url", None):
            async with sse_client(server_config.sse_url) as (read, write):
                async with _OutputSchemaTolerantSession(
                    read, write, server_name=server_name
                ) as session:
                    yield session
            return

        server_params = StdioServerParameters(
            command=server_config.command,
            args=server_config.args or [],
            env={**os.environ, **(server_config.env or {})},
        )
        async with stdio_client(server_params) as (read, write):
            async with _OutputSchemaTolerantSession(
                read, write, server_name=server_name
            ) as session:
                yield session

    async def _server_call_lock(self, server_name: str) -> asyncio.Lock:
        async with self._lease_meta_lock:
            lock = self._server_call_locks.get(server_name)
            if lock is None:
                lock = asyncio.Lock()
                self._server_call_locks[server_name] = lock
            return lock

    def _lease_is_idle_expired(self, lease: _ServerSessionLease) -> bool:
        if SESSION_IDLE_TTL_SECONDS <= 0:
            return False
        return (time.monotonic() - lease.last_used_monotonic) > SESSION_IDLE_TTL_SECONDS

    async def _close_session_lease(self, server_name: str) -> None:
        lease = self._session_leases.pop(server_name, None)
        if lease is None:
            return
        try:
            await lease.stack.aclose()
        except Exception as exc:
            logger.debug(
                "MCP session lease close error | server=%s error=%s",
                server_name,
                exc,
            )

    async def close_all_sessions(self) -> None:
        """Drop all leased MCP sessions (re-init / process teardown)."""
        names = list(self._session_leases.keys())
        for name in names:
            await self._close_session_lease(name)

    def session_lease_create_count(self, server_name: str) -> int:
        """Test/observability helper: how many times a lease was created for server."""
        return int(self._session_lease_create_totals.get(server_name, 0))

    async def _acquire_session_lease(
        self,
        server_name: str,
        server_config: Any,
    ) -> _ServerSessionLease:
        """Return a live per-server session; caller must hold the server call lock."""
        existing = self._session_leases.get(server_name)
        if existing is not None and not self._lease_is_idle_expired(existing):
            existing.last_used_monotonic = time.monotonic()
            return existing

        if existing is not None:
            await self._close_session_lease(server_name)

        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            session = await stack.enter_async_context(
                self._open_session(server_name, server_config)
            )
            await asyncio.wait_for(session.initialize(), timeout=INIT_TIMEOUT_SECONDS)
        except Exception:
            try:
                await stack.aclose()
            except Exception:
                pass
            raise

        create_count = self._session_lease_create_totals.get(server_name, 0) + 1
        self._session_lease_create_totals[server_name] = create_count
        lease = _ServerSessionLease(
            server_name=server_name,
            session=session,
            stack=stack,
            last_used_monotonic=time.monotonic(),
            create_count=create_count,
        )
        self._session_leases[server_name] = lease
        logger.debug(
            "MCP session lease opened | server=%s create_count=%s",
            server_name,
            lease.create_count,
        )
        return lease

    async def _list_server_tools(self, server_name: str, server_config: Any) -> List[Any]:
        # Startup probe keeps a short-lived session (does not share the call lease).
        async def _probe() -> List[Any]:
            async with self._open_session(server_name, server_config) as session:
                await session.initialize()
                response = await session.list_tools()
                tools = getattr(response, "tools", None)
                if tools is None and isinstance(response, dict):
                    tools = response.get("tools", [])
                if tools is None:
                    tools = []
                logger.debug(f"MCP 服务器 [{server_name}] 返回 {len(tools)} 个工具")
                return list(tools)

        return await asyncio.wait_for(_probe(), timeout=INIT_TIMEOUT_SECONDS)

    def _normalize_tool_definition(self, tool: Any) -> Dict[str, Any]:
        if isinstance(tool, dict):
            data = tool
        elif hasattr(tool, "model_dump"):
            data = tool.model_dump()
        else:
            data = {
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", ""),
                "inputSchema": getattr(tool, "inputSchema", None)
                or getattr(tool, "input_schema", None),
            }

        schema = data.get("inputSchema") or data.get("input_schema") or {}
        if not isinstance(schema, dict):
            schema = {}
        if "type" not in schema:
            schema = {"type": "object", **schema}

        return {
            "name": data.get("name", ""),
            "description": data.get("description", "") or "",
            "parameters_schema": schema or {"type": "object", "properties": {}},
        }

    def _make_executor(self, server_name: str, tool_name: str):
        async def executor(**kwargs):
            return await self._execute_tool(server_name, tool_name, kwargs)

        return executor

    async def probe_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Perform one live diagnostics call without changing MCP health state.

        Production tool execution deliberately refreshes the manager's health
        telemetry. Operator preflight needs the same live protocol check but
        must not rewrite even that process-local state merely by observing it.
        """

        return await self._execute_tool(
            server_name,
            tool_name,
            arguments,
            update_server_state=False,
        )

    def _normalize_tool_arguments(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        对特定工具的参数名做防御性规范化，处理 LLM 可能使用 snake_case 而
        MCP Schema 定义为 camelCase 的情况。
        """
        return arguments

    async def _execute_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        update_server_state: bool = True,
    ) -> Dict[str, Any]:
        server_config = self._server_configs.get(server_name)
        if server_config is None:
            return {
                "success": False,
                "error": f"MCP 服务器未配置: {server_name}",
                "server_name": server_name,
                "tool_name": tool_name,
            }

        arguments = self._normalize_tool_arguments(tool_name, arguments)
        call_lock = await self._server_call_lock(server_name)

        async with call_lock:
            # Spacing, inside the serialization that already exists.  A server
            # with no configured interval waits zero, which is what every server
            # but amap does; amap rejects a breached per-second allowance with
            # ``CUQPS_HAS_EXCEEDED_THE_LIMIT`` and that rejection costs the round
            # a whole domain's worth of lodging candidates.
            min_interval = float(getattr(server_config, "min_interval_seconds", 0.0) or 0.0)
            if min_interval > 0:
                await rate_gate_for(f"mcp:{server_name}").acquire(min_interval)
            try:
                async def _call() -> Any:
                    lease = await self._acquire_session_lease(server_name, server_config)
                    # A lease outlives one call; drop anything a previous call left.
                    lease.session.take_output_schema_violation()
                    result = await lease.session.call_tool(tool_name, arguments)
                    lease.last_used_monotonic = time.monotonic()
                    violation = lease.session.take_output_schema_violation()
                    dumped = result.model_dump() if hasattr(result, "model_dump") else result
                    return dumped, violation

                payload, schema_violation = await asyncio.wait_for(
                    _call(), timeout=CALL_TIMEOUT_SECONDS
                )

                protocol_error = _mcp_result_error(payload)
                if protocol_error:
                    # Business/protocol error payload: keep lease (connection still usable).
                    return {
                        "success": False,
                        "server_name": server_name,
                        "tool_name": tool_name,
                        "error": protocol_error,
                        "result": payload,
                    }

                state = self._server_states.get(server_name)
                if state and update_server_state:
                    state.healthy = True
                    state.status = "healthy"
                    state.last_error = None

                normalized = _normalized_duffel_result(server_name, tool_name, payload)
                if normalized is None:
                    normalized = _normalized_duckduckgo_result(
                        server_name,
                        tool_name,
                        payload,
                    )
                if normalized is None:
                    normalized = _normalized_amap_place_result(
                        server_name,
                        tool_name,
                        payload,
                    )
                if normalized is None:
                    normalized = _normalized_rail_result(
                        server_name,
                        tool_name,
                        payload,
                    )
                answer = normalized if normalized is not None else {
                    "success": True,
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "result": payload,
                }
                if schema_violation is not None:
                    answer[PROVIDER_OUTPUT_SCHEMA_VIOLATION_KEY] = schema_violation
                return answer
            except Exception as exc:
                # Transport/timeout/runtime failure: drop lease so the next call rebuilds.
                await self._close_session_lease(server_name)
                error = _mcp_error_message(exc)
                state = self._server_states.get(server_name)
                if state and update_server_state:
                    state.healthy = False
                    state.status = "error"
                    state.last_error = error

                logger.error(
                    "MCP 工具执行失败 | server=%s tool=%s error=%s",
                    server_name,
                    tool_name,
                    error,
                )
                return {
                    "success": False,
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "error": error,
                }


# 全局 MCPManager 单例
_mcp_manager: Optional[MCPManager] = None


def get_mcp_manager() -> MCPManager:
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
    return _mcp_manager
