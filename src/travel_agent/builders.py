"""
依赖注入工厂 (Application Layer)
根据配置环境构建各层组件，是整个架构的连接器。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Settings, get_settings
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
from .services.weather_context_builder import WeatherContextBuilder
from .workflows.fast_answer import FastAnswerWorkflow
from .workflows.travel_planning import TravelPlanningWorkflow

logger = logging.getLogger(__name__)


@dataclass
class AppComponents:
    """全量组件容器，应用启动时构建一次"""
    # P21-01: explicit schema-bootstrap signal — 生产没有任何 InMemory 兜底，
    # init_db 失败等于「全绿启动、第一条请求 500」，不能靠沉默推断库是好的。
    db_available: bool = False
    db_init_error: Optional[str] = None

    # P0-A：只读 schema 报告（migration revision、结构指纹、缺表、可选能力）。
    # **只读** —— API 进程不再是「谁改 Schema」这个问题的答案之一（ADR-P0-03）。
    # 这一阶段它不参与 readiness 门禁（理由见 db/report.py 的 docstring），
    # 但它是那道门禁将来站的位置，也是现在运营者能看到的唯一一份结构真相。
    schema_report: Optional[Any] = None

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

        # 初始化数据库
        db_available = False
        db_init_error: Optional[str] = None
        try:
            from .infrastructure.database import init_db
            await init_db()
            db_available = True
            logger.info("数据库初始化完成")
        except Exception as e:
            db_init_error = f"{type(e).__name__}: {e}"
            # P21-01: 不是「部分功能不可用」——所有 store 只有 Postgres 实现，
            # 这里失败即首条业务请求直接 500。进程仍继续启动，只为让 readiness
            # 探针把故障报出去（/api/health/ready → 503），供运营者定位。
            logger.error(
                "数据库初始化失败（无内存兜底，业务请求将直接失败；"
                "readiness 探针会返回 503）: %s",
                e,
            )

        # 只读 schema 报告。放在 init_db 之后：这一阶段 init_db 仍然是建表的人，
        # 报告的职责是**把它建出来的东西如实说出来**（版本号、指纹、缺表、可选能力），
        # 而不是替它决定能不能启动。
        schema_report = None
        if db_available:
            try:
                from .db.report import build_schema_report
                from .infrastructure.database import get_engine

                schema_report = await build_schema_report(
                    get_engine(), embedding_dimensions=self.settings.embedding.dimensions
                )
                schema_report.log_summary()
            except Exception as e:
                # 报告自己崩了不该拦启动 —— 它是观察手段，不是运行依赖。
                # 但要留一条 ERROR：静默失败等于运营者以为「没消息就是结构没问题」。
                logger.error("只读 schema 报告生成失败（不影响启动，但结构状态未知）: %s", e)

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
        return AppComponents(
            db_available=db_available,
            db_init_error=db_init_error,
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
            travel_workflow=TravelPlanningWorkflow(
                checkpointer=checkpointer,
                delivery_bundle_store=delivery_bundle_store,
                trip_run_store=trip_run_store,
                ),
            fast_workflow=FastAnswerWorkflow(),
            preset_store=PresetStore(),
            product_configuration_store=ProductConfigurationStore(),
            trip_run_store=trip_run_store,
            delivery_bundle_store=delivery_bundle_store,
            weather_context_builder=weather_context_builder,
            tool_audit_store=get_tool_audit_store(),
            memory_lifecycle_store=get_memory_lifecycle_store(),
        )

    async def teardown(self) -> None:
        """清理资源"""
        components = _components
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
