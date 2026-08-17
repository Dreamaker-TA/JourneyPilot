"""Provider preset 与 capability。

两件事，分开：

- **preset** 是「帮用户快速填好模型段」的连接模板（`configs/providers/*.yaml`）。
  它不是默认值 —— 把 DeepSeek 的 base_url 写成 `models.py` 的默认，等于让一次私人
  部署成为所有人的默认，而那个人换了 provider 之后没有一处会跟着改。
- **capability** 是「这个上游到底支持什么」。原来它散在 router 里，靠
  ``"api.deepseek.com" == hostname`` 猜；同一个模型搬到代理后面那天判据就不成立，
  而失效是静默的。现在它是 preset 上的一份声明 + 一次按 base_url 的解析，
  认不出的上游走**保守档**，不假装完全兼容。

preset 不是事实保证：每份都带 ``last_verified_at``，过期与否由读的人判断。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

import yaml
from pydantic import Field

from .models import StrictConfig

logger = logging.getLogger(__name__)

PRESET_DIR = Path(__file__).resolve().parents[3] / "configs" / "providers"

#: 关掉思维链的方言。同一个意图三家写法不同，而认不出的那种会被对方忽略，
#: 所以保守档**全都发**（见 `models/router.py` 的 extra_body）。
ReasoningControl = Literal["none", "deepseek", "openrouter", "all_dialects"]

#: 输出上限字段名。langchain-openai 无条件把 max_tokens 改名成
#: max_completion_tokens，而 DeepSeek 只读 max_tokens。
TokenLimitField = Literal["max_tokens", "max_completion_tokens", "both"]


class ProviderCapabilities(StrictConfig):
    """一个上游支持什么。默认值就是**保守档**。"""

    #: 支持 ``response_format={"type":"json_schema"}``（严格结构化输出）。
    supports_json_schema: bool = False
    #: 支持 ``response_format={"type":"json_object"}``。
    supports_json_object: bool = True
    #: 流式响应里带 usage（不带则只能按字符数粗估）。
    supports_stream_usage: bool = True
    reasoning_control: ReasoningControl = "all_dialects"
    token_limit_field: TokenLimitField = "both"


class ProviderModels(StrictConfig):
    primary: str = ""
    fast: str = ""
    embedding: str = ""


class ProviderPreset(StrictConfig):
    """一份连接模板。``id`` 就是 `configs/providers/<id>.yaml` 的文件名。"""

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    #: 这份 preset 上一次被真实调用验证过的日期。preset 会过期，这个字段让它可判。
    last_verified_at: str = ""
    #: base_url 的 hostname 精确匹配到这份 preset 的 capability。为空则只按 base_url。
    hostnames: List[str] = Field(default_factory=list)
    models: ProviderModels = Field(default_factory=ProviderModels)
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)
    notes: str = ""


@dataclass(frozen=True)
class PresetIndex:
    by_id: Dict[str, ProviderPreset]
    by_hostname: Dict[str, ProviderPreset]


@lru_cache(maxsize=1)
def _index() -> PresetIndex:
    by_id: Dict[str, ProviderPreset] = {}
    by_hostname: Dict[str, ProviderPreset] = {}
    if not PRESET_DIR.is_dir():
        logger.warning("provider preset 目录不存在：%s", PRESET_DIR)
        return PresetIndex(by_id={}, by_hostname={})
    for path in sorted(PRESET_DIR.glob("*.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            payload.setdefault("id", path.stem)
            preset = ProviderPreset.model_validate(payload)
        except Exception as exc:
            # 一份坏 preset 不该让整个进程起不来：它影响的是「帮你填配置」这件事。
            logger.error("provider preset 读取失败 [%s]: %s", path.name, exc)
            continue
        by_id[preset.id] = preset
        for authority in [*preset.hostnames, _authority(preset.base_url)]:
            if authority:
                by_hostname[authority] = preset
    return PresetIndex(by_id=by_id, by_hostname=by_hostname)


def _authority(base_url: str) -> str:
    """匹配键：带显式端口时用 ``host:port``，否则只用 host。

    本机上两个不同的推理服务共用 ``127.0.0.1``，只按 host 匹配会让一个本地 vLLM
    拿到 Ollama 的 capability（其中包括「不回 usage」这条，于是费用读数无声退化成
    估算）。带端口的 preset 因此按 host:port 精确匹配；托管上游没有显式端口，
    行为不变。
    """

    try:
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return ""
        return f"{host}:{parsed.port}" if parsed.port else host
    except ValueError:
        return ""


def reload_presets() -> None:
    _index.cache_clear()


def available_presets() -> List[ProviderPreset]:
    return list(_index().by_id.values())


def get_preset(preset_id: str) -> Optional[ProviderPreset]:
    return _index().by_id.get(preset_id)


#: 认不出的 OpenAI-compatible endpoint 拿到的那一份。
CONSERVATIVE_CAPABILITIES = ProviderCapabilities()


def capabilities_for(base_url: str) -> ProviderCapabilities:
    """按 base_url 的 hostname 解析 capability，认不出走保守档。

    保守不是「功能更少」而是「不撒谎」：一个没被验证过支持 json_schema 的上游，
    按 json_object + prompt 里明写 schema 那条路走仍然能拿到正确结果，而反过来
    （假设它支持）拿到的是一次 400。
    """

    index = _index().by_hostname
    parsed_authority = _authority(base_url)
    preset = index.get(parsed_authority)
    if preset is None and ":" in parsed_authority:
        # 带端口没命中就回落到 host：托管上游的 base_url 可能显式写了 :443。
        preset = index.get(parsed_authority.split(":", 1)[0])
    if preset is None:
        return CONSERVATIVE_CAPABILITIES
    return preset.capabilities


def preset_model_section(preset: ProviderPreset) -> Dict[str, Any]:
    """把一份 preset 投影成 config.yaml 的模型段。

    **不含 api_key**：Key 交互输入到 `.env` 或 Secret，写进 config.yaml 就会跟着这份
    文件被提交、被贴进 issue。
    """

    section: Dict[str, Any] = {
        "primary_model": {"base_url": preset.base_url},
        "fast_model": {"base_url": preset.base_url},
    }
    if preset.models.primary:
        section["primary_model"]["model_name"] = preset.models.primary
    if preset.models.fast:
        section["fast_model"]["model_name"] = preset.models.fast
    if preset.models.embedding:
        section["embedding"] = {
            "base_url": preset.base_url,
            "model_name": preset.models.embedding,
        }
    return section
