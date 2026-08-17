"""内置定价快照与价格查找。

价格是**会过期的外部事实**，所以每条都带 ``effective_from`` 与 ``source_url``，
未命中时只报 token、不编造成本。用户可在 config.yaml 的 `model_pricing` 整体覆盖。

不在这里保存某次部署的调用统计：那属于 benchmark 记录，不属于价格表。
"""

from __future__ import annotations

from typing import List, Optional

from .models import ModelPricingItem


def default_model_pricing() -> List["ModelPricingItem"]:
    """内置定价快照（USD / 1M tokens，国际站口径）。

    分层定价（Qwen）取基础档；价格随时变动，用户可在
    config.yaml 的 `model_pricing` 覆盖。前缀匹配按 pattern 最长优先（见 resolve_price）。
    """
    ds = "https://api-docs.deepseek.com/quick_start/pricing"
    orouter = "https://openrouter.ai/api/v1/models"
    qwen = "https://www.alibabacloud.com/help/en/model-studio/model-pricing"
    mm = "https://platform.minimax.io/docs/guides/pricing-paygo"
    oai = "https://developers.openai.com/api/docs/pricing"
    day = "2026-07-03"
    return [
        # MiniMax（M2.7 有显式缓存写入费；本卡读折扣公式不计 write，仅登记）
        ModelPricingItem(pattern="MiniMax-M2.7", provider="minimax", input_per_1m=0.30,
                         cached_input_per_1m=0.06, cache_write_per_1m=0.375, output_per_1m=1.20,
                         effective_from=day, source_url=mm),
        ModelPricingItem(pattern="MiniMax-M3", provider="minimax", input_per_1m=0.30,
                         cached_input_per_1m=0.06, output_per_1m=1.20,
                         effective_from=day, source_url=mm),
        # DeepSeek（缓存命中价≈未命中 1/50）
        ModelPricingItem(pattern="deepseek-v4-flash", provider="deepseek", input_per_1m=0.14,
                         cached_input_per_1m=0.0028, output_per_1m=0.28,
                         effective_from=day, source_url=ds),
        ModelPricingItem(pattern="deepseek-v4-pro", provider="deepseek", input_per_1m=0.435,
                         cached_input_per_1m=0.003625, output_per_1m=0.87,
                         effective_from=day, source_url=ds),
        # The same two models reached through OpenRouter.  A separate pattern
        # rather than a looser one: the model id carries the `deepseek/` prefix
        # there, so prefix matching never reaches the direct entries above, and
        # the cache-read price genuinely differs (OpenRouter lists 1/5 of the
        # miss price for flash where DeepSeek direct lists 1/50).  Without these
        # rows `resolve_price` returns None and every cost read-out is null.
        ModelPricingItem(pattern="deepseek/deepseek-v4-flash", provider="deepseek",
                         input_per_1m=0.14, cached_input_per_1m=0.028, output_per_1m=0.28,
                         effective_from="2026-07-31", source_url=orouter),
        ModelPricingItem(pattern="deepseek/deepseek-v4-pro", provider="deepseek",
                         input_per_1m=0.435, cached_input_per_1m=0.003625, output_per_1m=0.87,
                         effective_from="2026-07-31", source_url=orouter),
        # Qwen / 阿里云（分层定价取基础档；隐式缓存命中 20%）
        ModelPricingItem(pattern="Qwen-Flash", provider="qwen", input_per_1m=0.05,
                         cached_input_per_1m=0.01, output_per_1m=0.40,
                         effective_from=day, source_url=qwen),
        ModelPricingItem(pattern="Qwen3.7-Plus", provider="qwen", input_per_1m=0.40,
                         cached_input_per_1m=0.08, output_per_1m=1.60,
                         effective_from=day, source_url=qwen),
        # OpenAI 5.4 系
        ModelPricingItem(pattern="gpt-5.4-nano", provider="openai", input_per_1m=0.20,
                         cached_input_per_1m=0.02, output_per_1m=1.25,
                         effective_from=day, source_url=oai),
        ModelPricingItem(pattern="gpt-5.4-mini", provider="openai", input_per_1m=0.75,
                         cached_input_per_1m=0.075, output_per_1m=4.50,
                         effective_from=day, source_url=oai),
        ModelPricingItem(pattern="gpt-5.4", provider="openai", input_per_1m=2.50,
                         cached_input_per_1m=0.25, output_per_1m=15.00,
                         effective_from=day, source_url=oai),
    ]



def resolve_price_in(
    table: List[ModelPricingItem],
    model: Optional[str],
    provider: Optional[str] = None,
) -> Optional[ModelPricingItem]:
    """按 (model, provider) 前缀匹配到一条价格；未命中返回 None（→ 只报 token 不编造成本）。

    - 大小写不敏感的前缀匹配：``model`` 以 ``item.pattern`` 开头即候选。
    - ``item.provider`` 非空时必须与传入 ``provider`` 相等（provider 缺省则不约束）。
    - 多条命中取 **pattern 最长** 者（最具体的档位优先，如 gpt-5.4-mini 胜过 gpt-5.4）。
    """

    if not model:
        return None
    model_lc = model.lower()
    provider_lc = (provider or "").lower()
    best: Optional[ModelPricingItem] = None
    for item in table:
        pattern = (item.pattern or "").lower()
        if not pattern or not model_lc.startswith(pattern):
            continue
        if item.provider and provider_lc and item.provider.lower() != provider_lc:
            continue
        if best is None or len(item.pattern) > len(best.pattern):
            best = item
    return best
