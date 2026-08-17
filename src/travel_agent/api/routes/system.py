"""
系统状态与配置 API (Serving Layer)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ...builders import get_components
from ...config import get_settings, persist_model_config
from ...models.router import ModelTier, get_model_router
from ..schemas import ModelConfigRequest, SystemStatus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["system"])

def _public_config_payload(settings: Any) -> Dict[str, Any]:
    """Non-secret config projection for /api/config (no API keys)."""

    primary = getattr(settings, "primary_model", None)
    fast = getattr(settings, "fast_model", None)
    server = getattr(settings, "server", None)
    return {
        "primary_model": {
            "model_name": getattr(primary, "model_name", None),
            "base_url": getattr(primary, "base_url", None),
            "api_key_set": bool(getattr(primary, "api_key", None)),
        },
        "fast_model": {
            "model_name": getattr(fast, "model_name", None),
            "base_url": getattr(fast, "base_url", None),
            "api_key_set": bool(getattr(fast, "api_key", None)),
        },
        "server": {
            "allow_runtime_model_config": bool(
                getattr(server, "allow_runtime_model_config", False)
            ),
        },
        "rag": {
            "chunk_size": settings.rag.chunk_size,
            "top_k": settings.rag.top_k,
            "embedding_model": settings.embedding.model_name,
        },
        "env": settings.env,
    }


async def _probe_database() -> Tuple[bool, str]:
    """只探连接活性。「表在不在、结构对不对」归 `database_schema` 那一项，
    在这里再列一份必需表清单就是同一个合同的第二份定义。
    """

    try:
        from ...infrastructure.database import get_engine

        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


async def _probe_redis() -> Tuple[bool, str]:
    try:
        from ...infrastructure.redis_client import get_redis

        await get_redis().ping()
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def _probe_model_config() -> Tuple[bool, str]:
    try:
        router = get_model_router()
        router.get_primary()
        router.get_fast()
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


@router.get("/status", response_model=SystemStatus)
async def get_status():
    """获取系统状态"""
    components = get_components()

    # 检查数据库连接及业务 schema 可用性
    db_ok, _ = await _probe_database()

    # 检查 Redis 连接
    redis_ok, _ = await _probe_redis()

    # 记忆抽取管线累计计数（CB-05）：让 fire-and-forget 抽取链路的健康可观测。
    from ...memory.extraction_stats import get_memory_extraction_stats

    return SystemStatus(
        status="running",
        tools_count=components.tool_registry.count,
        db_connected=db_ok,
        redis_connected=redis_ok,
        memory_extraction=get_memory_extraction_stats().snapshot(),
    )


# Components reported by /health/ready that must never decide it.  A name list,
# because the reason differs per component and each one has to be argued for:
# ``mcp`` covers optional paid integrations, ``data_snapshots`` covers staleness
# that degrades an answer rather than preventing one.  Anything not named here
# blocks readiness — the default is the strict one.
#
# ``database_schema`` 刻意不在这份名单里 —— 它拦门禁，判据与
# ``db/report.py::GATES_READINESS`` 同源。
_NON_BLOCKING_COMPONENTS = frozenset(
    {"mcp", "data_snapshots", "knowledge_corpus", "run_execution"}
)


async def _probe_run_execution(components: Any) -> Dict[str, Any]:
    """活跃租约、未收口的控制命令与上一次恢复扫描的结论。**报出来，不拦门禁**。

    一个 Run 被判成不可恢复不代表这台服务不能接活；运营者需要看见的是有几个 Run
    在等他点「继续」、有多少条取消/追加要求还没有结论，以及扫描本身有没有出错。
    """

    recovery = components.run_recovery_service
    report = recovery.last_report if recovery is not None else None
    payload: Dict[str, Any] = {
        "ready": True,
        "sweeper_running": recovery is not None,
        "recovered_run_count": len(report.outcomes) if report else 0,
        "resume_available": report.resume_available_count if report else 0,
        "recovery_counts": report.counts if report else {},
        "recovery_failures": list(report.failures) if report else [],
    }
    try:
        payload["active_leases"] = await components.run_execution_store.count_active_leases()
    except Exception as exc:
        payload["active_leases"] = None
        payload["message"] = f"执行租约计数失败: {exc}"
    try:
        payload["pending_commands"] = await components.run_command_store.count_open_by_type()
    except Exception as exc:
        payload["pending_commands"] = None
        payload["command_message"] = f"控制命令计数失败: {exc}"
    return payload


async def _probe_knowledge_corpus() -> Dict[str, Any]:
    """出厂语料在不在库里，逐集合报数，不阻塞就绪。

    不阻塞的理由与上面 `data_snapshots` 同源：空语料降低的是接地质量，不让服务答不出话
    （候选身份全部来自 Provider，知识库只提名）。为它 503 会把一个能服务的部署卡住。
    **报出来才是重点** —— 空库必须有一处能看出来。

    `ready` 的判据是**种子声称有货的集合在库里都非空**，不是「四个集合都非空」：
    `visa_policies` 是记在案的已知缺口，把它算进去
    这一项就会恒为 false，然后被当成正常。「种子声称有货的是哪几个」不在这里判，
    由 `factory_seed.seeded_factory_collections()` 一处回答。

    那份清单同时**就是接地检索的探针清单**，所以这里要把两个方向都报出来，
    否则清单变短是看不见的：

      声明有货、库里为空 → `message` 点名，`ready` 转 false（原有语义）；
      声明没有、库里有货 → `unprobed_with_content` 点名，`ready` 也转 false ——
        那批语料在这台机器上**永远不会被检索到**，而每一处读数都显示一切正常。
    """

    from ...rag import factory_seed

    try:
        counts = await factory_seed.factory_collection_counts()
    except Exception as exc:
        return {"ready": False, "message": f"读不到 knowledge_chunks: {exc}", "collections": {}}

    manifest = factory_seed.read_manifest()
    if manifest is None:
        return {
            "ready": False,
            "message": "未提供本地出厂语料种子（data/corpus/seed/）；公开仓库默认不包含该可选数据",
            "collections": counts,
        }

    probed = factory_seed.seeded_factory_collections()
    empty = sorted(name for name in probed if counts.get(name, 0) == 0)
    unprobed_with_content = sorted(
        name
        for name, total in counts.items()
        if total > 0 and name not in probed
    )

    complaints = []
    if empty:
        complaints.append(f"出厂集合仍然是空的: {', '.join(empty)}")
    if unprobed_with_content:
        complaints.append(
            "这些出厂集合库里有货但种子没声明，接地检索不会查它们: "
            f"{', '.join(unprobed_with_content)}（重新导出种子）"
        )
    return {
        "ready": not complaints,
        "message": "ok" if not complaints else "；".join(complaints),
        "collections": counts,
        "seeded": manifest.collections,
        "probed": list(probed),
        "unprobed_with_content": unprobed_with_content,
    }


def _probe_data_snapshots(settings: Any) -> Dict[str, Any]:
    """Report the age of every committed upstream snapshot, blocking nothing."""

    from ...entities.data_snapshot_freshness import station_snapshot_freshness
    from ...services.rail_12306 import (
        station_snapshot_fingerprint_drift,
        station_snapshot_metadata,
    )

    freshness = station_snapshot_freshness(
        station_snapshot_metadata(),
        now=datetime.now(timezone.utc).date(),
        max_age_days=settings.data_snapshots.station_max_age_days,
    )
    try:
        drift = station_snapshot_fingerprint_drift()
    except OSError as exc:  # unreadable snapshot file
        drift = [f"站表快照不可读：{exc}"]
    return {
        # Always ``True``: this component states facts and never gates readiness.
        # The verdict below is what a reader acts on.
        "ready": True,
        "station_table": {
            "verdict": freshness.verdict,
            "criterion": freshness.criterion,
            "message": freshness.detail,
            "age_days": freshness.age_days,
            "fingerprint_drift": drift,
        },
    }


def _probe_schema_report(components: Any) -> Dict[str, Any]:
    """把启动时那份只读合同校验原样端出来，并让它决定放不放行。

    不在这里重新体检：readiness 每 30 秒被调一次，而结构在运行期不再变化。
    报告缺失与校验不通过一样按不就绪处理 —— 门禁的默认值只能是关着的。
    """

    from ...db.report import GATES_READINESS

    report = getattr(components, "schema_report", None)
    if report is None:
        return {
            "ready": False,
            "gates_readiness": GATES_READINESS,
            "available": False,
            "message": "启动时未执行数据库合同校验（详见启动日志）",
        }
    return {"ready": report.compatible, "available": True, **report.to_dict()}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    """生产就绪探针：核心依赖不可用时返回 503。"""
    settings = get_settings()
    components = get_components()

    db_ok, db_message = await _probe_database()
    redis_ok, redis_message = await _probe_redis()
    model_ok, model_message = _probe_model_config()

    gates_enabled = bool(settings.run_control.plan_gate_enabled)
    checkpointer_ok = bool(components.checkpointer) or not gates_enabled
    checkpointer_message = (
        "ok"
        if checkpointer_ok
        else "run-control gates are enabled but LangGraph checkpointer is unavailable"
    )

    mcp_states = components.mcp_manager.get_server_states()
    mcp_summary = {
        "healthy": sum(1 for item in mcp_states.values() if item.get("healthy")),
        "configured": sum(1 for item in mcp_states.values() if item.get("configured")),
        "total": len(mcp_states),
        "errors": {
            name: item.get("last_error")
            for name, item in mcp_states.items()
            if item.get("enabled") and item.get("configured") and not item.get("healthy")
        },
    }

    snapshot_summary = _probe_data_snapshots(settings)

    components_status = {
        "database": {"ready": db_ok, "message": db_message},
        "redis": {"ready": redis_ok, "message": redis_message},
        "models": {"ready": model_ok, "message": model_message},
        "checkpointer": {"ready": checkpointer_ok, "message": checkpointer_message},
        # MCP servers include optional paid/local integrations. They are reported here
        # but do not block readiness unless a route explicitly needs that tool.
        "mcp": {"ready": True, **mcp_summary},
        # Committed upstream snapshots: an over-age station table degrades station
        # scope, it does not stop the service answering. Reporting it here is the
        # point; blocking on it would hold a working deployment behind a 503 over a
        # file nobody re-downloaded.
        "data_snapshots": snapshot_summary,
        # Factory corpus: reported, never blocking — same call as data_snapshots above.
        # An empty knowledge base makes grounding worse, it does not stop the service
        # answering; 503-ing on it would hold a working deployment behind a seed file.
        "knowledge_corpus": await _probe_knowledge_corpus(),
        # 数据库合同：revision、结构指纹、缺表、可选能力。这一项拦门禁，
        # 不通过时读 `problems` 与 `next_action`。
        "database_schema": _probe_schema_report(components),
        # 执行租约与启动恢复：报出来不拦门禁，理由见 `_probe_run_execution`。
        "run_execution": await _probe_run_execution(components),
    }
    ready = all(
        component["ready"]
        for name, component in components_status.items()
        if name not in _NON_BLOCKING_COMPONENTS
    )
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "components": components_status},
    )


@router.get("/config")
async def get_config() -> Dict[str, Any]:
    """获取当前系统配置（脱敏后）"""
    return _public_config_payload(get_settings())


@router.post("/configure")
async def configure_model(config: ModelConfigRequest):
    """热更新模型配置（无需重启）"""
    try:
        tier_map = {
            "primary": ModelTier.PRIMARY,
            "fast": ModelTier.FAST,
        }
        tier = tier_map.get(config.tier, ModelTier.PRIMARY)
        settings = get_settings()
        if not settings.server.allow_runtime_model_config:
            raise HTTPException(
                status_code=403,
                detail="运行时模型配置接口未启用；请通过后端配置文件或 ALLOW_RUNTIME_MODEL_CONFIG=1 显式开启",
            )
        if tier == ModelTier.PRIMARY:
            m = settings.primary_model
        else:
            m = settings.fast_model
        api_key = config.api_key.strip() if config.api_key else m.api_key
        m.api_key = api_key
        m.model_name = config.model_name
        m.base_url = config.base_url
        m.max_tokens = config.max_tokens
        m.temperature = config.temperature
        router = get_model_router()
        router.update_model(
            tier=tier,
            api_key=api_key,
            model_name=config.model_name,
            base_url=config.base_url,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )
        try:
            persist_model_config(
                config.tier,
                {
                    "api_key": api_key,
                    "model_name": config.model_name,
                    "base_url": config.base_url,
                    "max_tokens": config.max_tokens,
                    "temperature": config.temperature,
                },
            )
        except FileNotFoundError as e:
            raise HTTPException(status_code=500, detail=str(e))
        logger.info(f"模型配置已更新: {config.model_name} ({config.tier})")
        return {"status": "success", "message": f"{config.tier} 模型已更新为 {config.model_name}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tools")
async def get_tools():
    """获取所有可用工具列表"""
    components = get_components()
    return {
        "tools": components.tool_registry.list_tools(),
        "total": components.tool_registry.count,
    }


@router.get("/mcp-servers")
async def get_mcp_servers():
    """获取 MCP 服务器状态"""
    components = get_components()
    states = components.mcp_manager.get_server_states()
    servers = sorted(states.values(), key=lambda item: item["name"])
    return {
        "servers": servers,
        "total": len(servers),
        "active": len([s for s in servers if s["enabled"]]),
        "healthy": len([s for s in servers if s["status"] == "healthy"]),
        "misconfigured": len([s for s in servers if s["status"] == "misconfigured"]),
    }
