"""
依赖注入工厂 (Application Layer)
根据配置环境构建各层组件，是整个架构的连接器。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Settings, get_settings
from .db.report import SchemaReport, verify_database_contract
from .infrastructure.cost_ledger_store import CostLedgerStore, get_cost_ledger_store
from .infrastructure.delivery_bundle_store import DeliveryBundleStore
from .infrastructure.memory_lifecycle_store import (
    MemoryLifecycleStore,
    get_memory_lifecycle_store,
)
from .infrastructure.provider_snapshot_cache import (
    ProviderSnapshotCache,
    get_provider_snapshot_cache,
)
from .infrastructure.run_execution_store import (
    RunExecutionStore,
    get_run_execution_store,
)
from .infrastructure.trip_run_store import TripRunStore, get_trip_run_store
from .infrastructure.tool_audit_store import ToolAuditStore, get_tool_audit_store
from .infrastructure.weather_provider import default_weather_providers
from .memory.chat_session import ChatSessionMemory
from .memory.memory_extractor import MemoryExtractor
from .memory.user_profile import UserProfileMemory
from .models.router import ModelRouter, get_model_router
from .models.usage import UsageRecorder, get_usage_recorder
from .preset.store import PresetStore
from .preset.product_config import ProductConfigurationStore
from .rag.indexer import KnowledgeIndexer
from .rag.retriever import HybridRetriever
from .tools.mcp_manager import MCPManager, get_mcp_manager
from .tools.registry import ToolRegistry, get_tool_registry
from .services.run_lease import release_all_leases
from .services.run_recovery import RunRecoveryService
from .services.weather_context_builder import WeatherContextBuilder
from .workflows.fast_answer import FastAnswerWorkflow
from .workflows.travel_planning import TravelPlanningWorkflow

logger = logging.getLogger(__name__)


@dataclass
class AppComponents:
    """全量组件容器，应用启动时构建一次"""
    # 数据库合同的只读校验结果。所有 store 只有 Postgres 实现，没有内存兜底，
    # 所以这一项决定 readiness 放不放行；`None`（校验没跑成）同样按不就绪处理。
    schema_report: Optional[SchemaReport] = None

    # Models
    model_router: ModelRouter = field(default_factory=get_model_router)

    # C 域遥测：LLM usage 捕获缓冲（router 捕获写入，落库方 drain 消费）
    usage_recorder: UsageRecorder = field(default_factory=get_usage_recorder)

    # C 域台账：drain 出的 usage 记录算成本后落库（run_llm_calls），SSE/REST 暴露
    cost_ledger_store: CostLedgerStore = field(default_factory=get_cost_ledger_store)

    # Memory — 基础
    chat_session_memory: ChatSessionMemory = field(default_factory=ChatSessionMemory)
    user_profile_memory: UserProfileMemory = field(default_factory=UserProfileMemory)

    # Memory — 新记忆系统 (memory_graph / memory_store 由 memory_extractor / context_builder
    # 内部按需本地构造，不走 AppComponents 注入)
    memory_extractor: MemoryExtractor = field(default_factory=MemoryExtractor)

    # RAG
    knowledge_indexer: KnowledgeIndexer = field(default_factory=KnowledgeIndexer)
    vector_retriever: HybridRetriever = field(default_factory=HybridRetriever)

    # Tools
    tool_registry: ToolRegistry = field(default_factory=get_tool_registry)
    mcp_manager: MCPManager = field(default_factory=get_mcp_manager)
    provider_snapshot_cache: ProviderSnapshotCache = field(
        default_factory=get_provider_snapshot_cache
    )

    # Workflows
    checkpointer: Optional[Any] = None
    _checkpointer_pool: Optional[Any] = None
    # Explicit durability signal — do not infer "has resume" from silence.
    checkpointer_available: bool = False
    checkpointer_init_error: Optional[str] = None
    travel_workflow: TravelPlanningWorkflow = field(
        default_factory=TravelPlanningWorkflow
    )
    fast_workflow: FastAnswerWorkflow = field(
        default_factory=FastAnswerWorkflow
    )

    # Preset
    preset_store: PresetStore = field(default_factory=PresetStore)
    product_configuration_store: ProductConfigurationStore = field(default_factory=ProductConfigurationStore)

    # TripOps durable run lifecycle
    trip_run_store: TripRunStore = field(default_factory=get_trip_run_store)

    # 执行归属：谁在跑这个 run，以及重启后的恢复判定。
    run_execution_store: RunExecutionStore = field(default_factory=get_run_execution_store)
    run_recovery_service: Optional[RunRecoveryService] = None

    # JourneyPilot v2 immutable delivery snapshots + current-bundle CAS.
    delivery_bundle_store: DeliveryBundleStore = field(default_factory=DeliveryBundleStore)
    weather_context_builder: WeatherContextBuilder = field(
        default_factory=lambda: WeatherContextBuilder(default_weather_providers())
    )
    # Optional wall-clock override for weather refresh (tests inject a fixed NOW).
    # Production leaves this None → WeatherBundleRefreshService uses datetime.now(UTC).
    weather_refresh_clock: Optional[Any] = None


    # TripOps production tool gateway audit state
    tool_audit_store: ToolAuditStore = field(default_factory=get_tool_audit_store)

    # Memory lifecycle / physical forgetting audit state
    memory_lifecycle_store: MemoryLifecycleStore = field(
        default_factory=get_memory_lifecycle_store
    )


class AppBuilder:
    """
    应用组件构建器。
    在不同环境（dev / test / prod）下注入不同实现。
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    async def build(self) -> AppComponents:
        """构建并初始化所有组件"""
        logger.info(f"构建应用组件（环境: {self.settings.env}）")

        # 只读合同校验。结构由 `journeypilot migrate` 在启动 API 之前建好；
        # 校验不通过不抛异常，进程仍要启动到 readiness 可读。
        from .infrastructure.database import get_engine

        schema_report = await verify_database_contract(
            get_engine(), embedding_dimensions=self.settings.embedding.dimensions
        )
        schema_report.log_summary()

        checkpointer = None
        checkpointer_pool = None
        checkpointer_available = False
        checkpointer_init_error: Optional[str] = None
        require_checkpointer = bool(
            getattr(self.settings.checkpoint_retention, "require_on_startup", False)
        )
        try:
            from .infrastructure.checkpointer import build_checkpointer

            checkpointer, checkpointer_pool = await build_checkpointer(self.settings)
            checkpointer_available = checkpointer is not None
        except Exception as e:
            checkpointer_init_error = f"{type(e).__name__}: {e}"
            if require_checkpointer:
                logger.error(
                    "LangGraph checkpointer 初始化失败且 require_on_startup=true，拒绝启动: %s",
                    e,
                )
                raise RuntimeError(
                    "checkpointer required but initialization failed: "
                    f"{checkpointer_init_error}"
                ) from e
            # Soft degrade: graph runs without durable interrupt/resume.
            logger.error(
                "LangGraph checkpointer 初始化失败（将使用无持久图执行；"
                "plan_gate/crash-resume 不可用）: %s",
                e,
            )

        # 注册内置工具
        try:
            from .tools.builtin_tools import register_builtin_tools
            register_builtin_tools()
            logger.info("内置工具注册完成")
        except Exception as e:
            logger.warning(f"内置工具注册失败: {e}")

        # 初始化 MCP 工具
        mcp_manager = get_mcp_manager()
        try:
            await mcp_manager.initialize(self.settings.mcp_servers)
        except Exception as e:
            logger.warning(f"MCP 工具初始化失败（部分工具不可用）: {e}")

        trip_run_store = get_trip_run_store()
        delivery_bundle_store = DeliveryBundleStore()
        weather_context_builder = WeatherContextBuilder(default_weather_providers())
        travel_workflow = TravelPlanningWorkflow(
            checkpointer=checkpointer,
            delivery_bundle_store=delivery_bundle_store,
            trip_run_store=trip_run_store,
        )
        run_execution_store = get_run_execution_store()
        run_recovery_service = RunRecoveryService(
            trip_run_store=trip_run_store,
            execution_store=run_execution_store,
            # 没有 checkpointer 时不传探针：那不是「探测失败」，而是「这个部署没有可恢复的
            # 断点」，恢复判定要按 non_resumable 说出来。
            checkpoint_probe=travel_workflow.has_checkpoint if checkpointer_available else None,
            sweep_seconds=self.settings.run_control.recovery_sweep_seconds,
        )
        return AppComponents(
            schema_report=schema_report,
            model_router=get_model_router(),
            usage_recorder=get_usage_recorder(),
            cost_ledger_store=get_cost_ledger_store(),
            chat_session_memory=ChatSessionMemory(),
            user_profile_memory=UserProfileMemory(),
            memory_extractor=MemoryExtractor(),
            knowledge_indexer=KnowledgeIndexer(),
            vector_retriever=HybridRetriever(),
            tool_registry=get_tool_registry(),
            mcp_manager=mcp_manager,
            provider_snapshot_cache=get_provider_snapshot_cache(),
            checkpointer=checkpointer,
            _checkpointer_pool=checkpointer_pool,
            checkpointer_available=checkpointer_available,
            checkpointer_init_error=checkpointer_init_error,
            travel_workflow=travel_workflow,
            fast_workflow=FastAnswerWorkflow(),
            preset_store=PresetStore(),
            product_configuration_store=ProductConfigurationStore(),
            trip_run_store=trip_run_store,
            run_execution_store=run_execution_store,
            run_recovery_service=run_recovery_service,
            delivery_bundle_store=delivery_bundle_store,
            weather_context_builder=weather_context_builder,
            tool_audit_store=get_tool_audit_store(),
            memory_lifecycle_store=get_memory_lifecycle_store(),
        )

    async def teardown(self) -> None:
        """清理资源。

        顺序不是随意的：租约与孤儿扫描都要写数据库，所以它们必须排在连接池关闭之前，
        否则「交还租约」会变成一条关不掉的连接错误，而下一次启动要白等一个租约周期。
        """
        components = _components
        if components and components.run_recovery_service is not None:
            await components.run_recovery_service.stop()
        try:
            released = await release_all_leases()
            if released:
                logger.info("已交还 %d 个执行租约", released)
        except Exception as e:
            logger.warning(f"执行租约交还失败: {e}")

        if components and components._checkpointer_pool is not None:
            try:
                await components._checkpointer_pool.close()
            except Exception as e:
                logger.warning(f"LangGraph checkpointer 连接池关闭失败: {e}")

        try:
            from .infrastructure.database import close_db
            await close_db()
        except Exception as e:
            logger.warning(f"数据库关闭失败: {e}")

        try:
            from .infrastructure.redis_client import close_redis
            await close_redis()
        except Exception as e:
            logger.warning(f"Redis 关闭失败: {e}")


# 全局组件容器（FastAPI lifespan 期间设置）
_components: Optional[AppComponents] = None


def get_components() -> AppComponents:
    """获取全局组件容器（需要先调用 AppBuilder.build()）"""
    if _components is None:
        raise RuntimeError(
            "AppComponents 尚未初始化，请确保在 FastAPI 启动事件中调用 AppBuilder.build()"
        )
    return _components


def set_components(components: AppComponents) -> None:
    """设置全局组件容器（在 FastAPI lifespan 中调用）"""
    global _components
    _components = components
