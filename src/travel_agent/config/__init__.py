"""配置层的公开界面。

包内分工：

    models.py         字段、默认值与校验（唯一定义处）
    loader.py         YAML + 环境变量 → Settings，以及每个值的来源
    env.py            JOURNEYPILOT_<段>__<字段> 的一处解析
    providers.py      provider preset 与 capability
    pricing.py        内置定价快照与价格查找
    mcp_defaults.py   内置 MCP server 配置（数据，不是 schema）
    redaction.py      脱敏
    schema_export.py  字段表 / JSON Schema 生成（CI 检查 diff 为空）

调用方一律 ``from ..config import get_settings``，不进包内模块 —— 内部怎么分文件是
这个包自己的事。
"""

from __future__ import annotations

from .loader import (
    ConfigError,
    EffectiveConfig,
    env_variable_reference,
    find_config_yaml,
    get_effective_config,
    get_settings,
    load_effective_config,
    load_yaml,
    migrate_config_data,
    persist_model_config,
    reload_settings,
    resolve_price,
    write_model_section,
)
from .models import (
    MAX_COMPLETION_TOKENS,
    CONFIG_VERSION,
    BackgroundJobsConfig,
    BlockingWorkConfig,
    CheckpointRetentionConfig,
    DatabaseConfig,
    DataSnapshotsConfig,
    EmbeddingConfig,
    FastModelConfig,
    FileLogConfig,
    GeocodingConfig,
    IngestConfig,
    LoggingConfig,
    MaintenanceConfig,
    MCPServerItem,
    ModelPricingItem,
    PrimaryModelConfig,
    ProviderChannelConfig,
    ProviderSnapshotCacheConfig,
    RAGConfig,
    RedisConfig,
    RerankConfig,
    RoutingConfig,
    RunBudgetConfig,
    RunControlConfig,
    RunDeadlineConfig,
    ServerConfig,
    Settings,
    StreamingConfig,
    StrictConfig,
    ToolExposureConfig,
)
from .providers import (
    ProviderCapabilities,
    ProviderPreset,
    available_presets,
    capabilities_for,
    get_preset,
    preset_model_section,
)
from .redaction import redact, redacted_settings

__all__ = [
    "CONFIG_VERSION",
    "MAX_COMPLETION_TOKENS",
    "BackgroundJobsConfig",
    "BlockingWorkConfig",
    "CheckpointRetentionConfig",
    "ConfigError",
    "DataSnapshotsConfig",
    "DatabaseConfig",
    "EffectiveConfig",
    "EmbeddingConfig",
    "FastModelConfig",
    "FileLogConfig",
    "GeocodingConfig",
    "IngestConfig",
    "LoggingConfig",
    "MCPServerItem",
    "MaintenanceConfig",
    "ModelPricingItem",
    "PrimaryModelConfig",
    "ProviderCapabilities",
    "ProviderChannelConfig",
    "ProviderPreset",
    "ProviderSnapshotCacheConfig",
    "RAGConfig",
    "RedisConfig",
    "RerankConfig",
    "RoutingConfig",
    "RunBudgetConfig",
    "RunControlConfig",
    "RunDeadlineConfig",
    "ServerConfig",
    "Settings",
    "StreamingConfig",
    "StrictConfig",
    "ToolExposureConfig",
    "available_presets",
    "capabilities_for",
    "env_variable_reference",
    "find_config_yaml",
    "get_effective_config",
    "get_preset",
    "get_settings",
    "load_effective_config",
    "load_yaml",
    "migrate_config_data",
    "persist_model_config",
    "preset_model_section",
    "redact",
    "redacted_settings",
    "reload_settings",
    "resolve_price",
    "write_model_section",
]
