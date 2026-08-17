"""YAML + 环境变量 → `Settings`，以及「这个值是从哪来的」。

加载顺序固定：

    找到 config.yaml → 按 config_version 迁移结构 → 严格校验 → 环境变量覆盖
    → 生成 effective config（脱敏）

严格校验在环境变量之前：一个拼错的 YAML 字段要在它有机会被环境变量掩盖之前报出来。

错误信息必须能直接照着改：字段路径、当前值、合法范围。**不许**把 api_key 写进任何
一条错误或日志（`redaction.py`）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from .env import EnvOverrideError, apply_env_overrides, known_env_variables
from .mcp_defaults import REMOVED_MCP_SERVERS
from .models import CONFIG_VERSION, MCPServerItem, ModelPricingItem, Settings
from .pricing import resolve_price_in
from .redaction import redact

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """配置读不成一份合法的 `Settings`。"""


# --------------------------------------------------------------------------- #
# 文件定位
# --------------------------------------------------------------------------- #

def find_config_yaml() -> Optional[Path]:
    """按 ``CONFIG_PATH`` → 向上查找 → 当前目录 定位 config.yaml。

    ``CONFIG_PATH`` 不带 ``JOURNEYPILOT_`` 前缀：它回答的不是「哪个字段是多少」，
    而是「去哪读那份文件」—— 在配置被加载之前就得知道，所以它不能是配置项。
    """

    env_path = os.getenv("CONFIG_PATH")
    if env_path:
        candidate = Path(env_path)
        if candidate.exists():
            return candidate
        raise ConfigError(f"CONFIG_PATH 指向的文件不存在：{env_path}")

    current = Path(__file__).resolve().parent
    for _ in range(10):
        candidate = current / "config.yaml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent

    candidate = Path("config.yaml")
    return candidate if candidate.exists() else None


def load_yaml(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


# --------------------------------------------------------------------------- #
# 结构迁移
# --------------------------------------------------------------------------- #

def migrate_config_data(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """把任意受支持版本的 config.yaml 结构迁到当前版本，返回 (数据, 迁移说明)。

    没有 ``config_version`` 的文件按 1 处理：这个字段是版本 2 引入的，所以「缺失」
    本身就是版本 1 的标记。
    """

    payload = dict(data)
    version = int(payload.pop("config_version", 1) or 1)
    notes: List[str] = []
    if version > CONFIG_VERSION:
        raise ConfigError(
            f"config_version={version} 比这个版本的 JourneyPilot 认识的 "
            f"{CONFIG_VERSION} 更新；升级程序或改回旧配置。"
        )
    if version < 2:
        notes.extend(_migrate_v1_to_v2(payload))
    return payload, notes


def _migrate_v1_to_v2(payload: Dict[str, Any]) -> List[str]:
    notes: List[str] = []

    if "model" in payload:
        # v1 把主力模型段叫 `model`，与「哪个模型」这个更细的问题重名。
        payload.setdefault("primary_model", payload["model"])
        payload.pop("model")
        notes.append("model → primary_model")

    rag = payload.get("rag")
    if isinstance(rag, dict) and "contextual_max_concurrency" in rag:
        # 并发归 provider_channels 一处：入库与在线请求共用上游，配额必须分开声明。
        rag.pop("contextual_max_concurrency")
        notes.append(
            "rag.contextual_max_concurrency 已移除"
            "（并发归 provider_channels.ingest_contextual_llm）"
        )

    servers = (payload.get("mcp") or {}).get("servers")
    if isinstance(servers, dict):
        removed = sorted(name for name in servers if name in REMOVED_MCP_SERVERS)
        for name in removed:
            servers.pop(name)
        if removed:
            notes.append(f"移除已停用的 MCP server：{', '.join(removed)}")

    return notes


# --------------------------------------------------------------------------- #
# 构造
# --------------------------------------------------------------------------- #

def _describe_validation_error(exc: ValidationError, path: Optional[Path]) -> str:
    """把 Pydantic 的报错变成一条能照着改的话：字段路径、当前值、合法范围。"""

    location = f"（{path}）" if path else ""
    lines = [f"config.yaml 校验未通过{location}："]
    for error in exc.errors():
        dotted = ".".join(str(part) for part in error["loc"]) or "<root>"
        given = error.get("input")
        if any(marker in dotted.lower() for marker in ("api_key", "password", "secret", "token")):
            given = "<redacted>"
        lines.append(f"  {dotted}: {error['msg']}（当前值 {given!r}）")
    lines.append("  字段清单：docs/configuration.md（journeypilot config docs 生成）")
    return "\n".join(lines)


def build_settings(data: Dict[str, Any], *, path: Optional[Path] = None) -> Settings:
    """严格校验一份已迁移的 YAML 数据。

    ``mcp.servers`` 与 `Settings` 其余部分不同：它是**合并**语义而不是替换 ——
    用户通常只在 YAML 里填 env，command/args/required_env 要保留内置默认，否则
    健康检查会因为 ``required_env=[]`` 漏报配置缺失。
    """

    payload = dict(data)
    mcp_section = payload.pop("mcp", None) or {}
    try:
        settings = Settings.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError(_describe_validation_error(exc, path)) from exc

    servers = mcp_section.get("servers") if isinstance(mcp_section, dict) else None
    if isinstance(servers, dict):
        for name, override in servers.items():
            if not isinstance(override, dict):
                raise ConfigError(f"mcp.servers.{name}: 应当是一组字段，实际 {override!r}")
            try:
                if name in settings.mcp_servers:
                    merged = settings.mcp_servers[name].model_dump()
                    merged.update({k: v for k, v in override.items() if v is not None})
                    settings.mcp_servers[name] = MCPServerItem.model_validate(merged)
                else:
                    settings.mcp_servers[name] = MCPServerItem.model_validate(override)
            except ValidationError as exc:
                raise ConfigError(_describe_validation_error(exc, path)) from exc
    return settings


def _apply_mcp_native_env(settings: Settings) -> None:
    """MCP provider 的原生 Key 名按原名读，传给子进程。不走 JOURNEYPILOT_ 前缀。"""

    for server in settings.mcp_servers.values():
        merged = dict(server.env or {})
        for key in server.required_env:
            value = os.getenv(key)
            if value:
                merged[key] = value
        server.env = merged or None


# --------------------------------------------------------------------------- #
# Effective config
# --------------------------------------------------------------------------- #

@dataclass
class EffectiveConfig:
    """当前生效的配置，以及每个值的来源。

    这一份直接回答「我改了 YAML 为什么没生效」：来源里写着 environment 的字段，
    改 YAML 不会有任何效果。
    """

    settings: Settings
    config_path: Optional[Path]
    migration_notes: List[str] = field(default_factory=list)
    #: 字段路径 → "config default" | "config.yaml" | "environment (VAR)"
    sources: Dict[str, str] = field(default_factory=dict)

    def source_of(self, dotted: str) -> str:
        return self.sources.get(dotted, "config default")

    def redacted(self) -> Dict[str, Any]:
        return redact(self.settings.model_dump(mode="json"))

    def report_lines(self) -> List[str]:
        """``journeypilot config show --effective`` 打的那些行。"""

        lines: List[str] = []
        for dotted, value in sorted(_flatten(self.redacted()).items()):
            lines.append(f"{dotted} = {value}    source={self.source_of(dotted)}")
        return lines


def _flatten(payload: Any, prefix: str = "") -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    if isinstance(payload, dict):
        for name, item in payload.items():
            flat.update(_flatten(item, f"{prefix}.{name}" if prefix else str(name)))
    elif isinstance(payload, list):
        flat[prefix] = f"[{len(payload)} 项]"
    else:
        flat[prefix] = payload
    return flat


def _yaml_sources(data: Dict[str, Any]) -> Dict[str, str]:
    return {dotted: "config.yaml" for dotted in _flatten(data)}


def load_effective_config() -> EffectiveConfig:
    """完整走一遍加载顺序，带来源。"""

    path = find_config_yaml()
    raw = load_yaml(path) if path else {}
    migrated, notes = migrate_config_data(raw)
    settings = build_settings(migrated, path=path)
    sources = _yaml_sources(migrated)
    try:
        applied = apply_env_overrides(settings)
    except EnvOverrideError as exc:
        # 环境变量的问题与 YAML 的问题对调用方是同一件事：配置读不成一份合法的
        # Settings。收敛成同一个异常，CLI 与启动路径才只需要处理一种。
        raise ConfigError(str(exc)) from exc
    for dotted, variable in applied:
        sources[dotted] = f"environment ({variable})"
    _apply_mcp_native_env(settings)
    return EffectiveConfig(
        settings=settings,
        config_path=path,
        migration_notes=notes,
        sources=sources,
    )


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #

_effective: Optional[EffectiveConfig] = None


def get_effective_config() -> EffectiveConfig:
    global _effective
    if _effective is None:
        _effective = load_effective_config()
        if _effective.migration_notes:
            logger.warning(
                "config.yaml 按 config_version 迁移了结构（文件本身未改写）：%s",
                "；".join(_effective.migration_notes),
            )
    return _effective


def get_settings() -> Settings:
    """全局配置单例。"""

    return get_effective_config().settings


def reload_settings() -> Settings:
    global _effective
    _effective = None
    return get_settings()


def resolve_price(
    model: Optional[str],
    provider: Optional[str] = None,
    *,
    pricing: Optional[List[ModelPricingItem]] = None,
) -> Optional[ModelPricingItem]:
    """按 (model, provider) 查一条价格；未命中返回 None（→ 只报 token 不编造成本）。"""

    table = pricing if pricing is not None else get_settings().model_pricing
    return resolve_price_in(table, model, provider)


def persist_model_config(tier: str, model_data: Dict[str, Any]) -> None:
    """把模型段写回 config.yaml。

    写入方**只有 CLI 与这一个函数**。写回时补上 ``config_version``：一份被程序改过
    却没有版本号的文件，下一次加载会被当成 v1 再迁移一遍。
    """

    path = find_config_yaml()
    if not path:
        raise FileNotFoundError("config.yaml 未找到，无法持久化配置")
    data = load_yaml(path)
    data["config_version"] = CONFIG_VERSION
    key = "primary_model" if tier == "primary" else "fast_model"
    section = data.get(key)
    if not isinstance(section, dict):
        section = {}
    section.update(model_data)
    data[key] = section
    with open(path, "w", encoding="utf-8") as handle:
        yaml.dump(data, handle, allow_unicode=True, default_flow_style=False, sort_keys=False)


def write_model_section(section: Dict[str, Any]) -> Path:
    """把一份 provider preset 的模型段写回 config.yaml，返回被写的路径。"""

    path = find_config_yaml()
    if not path:
        raise FileNotFoundError("config.yaml 未找到，先从 config.example.yaml 复制一份")
    data = load_yaml(path)
    data["config_version"] = CONFIG_VERSION
    for name, values in section.items():
        existing = data.get(name)
        if isinstance(existing, dict):
            existing.update(values)
            data[name] = existing
        else:
            data[name] = values
    with open(path, "w", encoding="utf-8") as handle:
        yaml.dump(data, handle, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return path


def env_variable_reference() -> Dict[str, str]:
    """所有合法环境变量名 → 它覆盖的字段路径。文档与 CLI 共用这一份。"""

    return {name: ".".join(path) for name, path in known_env_variables(Settings).items()}
