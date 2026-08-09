"""Research Brief 上下文构建工具 — 将 JSON 格式的 brief 按 Agent 需要的字段子集格式化为可读文本。"""

from typing import Dict, Optional, Sequence, Tuple

from .json_helpers import safe_parse_json

# ── 字段配置 ────────────────────────────────────────────────────────────
# key → (中文标签, 后缀)
_FIELD_LABELS: Dict[str, Tuple[str, str]] = {
    "objective": ("用户目标", ""),
    "destination": ("目的地", ""),
    "duration_days": ("计划天数", "天"),
    "travel_style": ("旅行风格", ""),
    "travel_party": ("出行人员", ""),
    "dimensions_to_cover": ("需覆盖维度", ""),
    "departure_city": ("出发城市", ""),
    "departure_time": ("出行时间", ""),
    "budget": ("预算", ""),
}

# 需要跳过的占位值
_SKIP_VALUES: Dict[str, set] = {
    "destination": {"", "未指定"},
}

# 值为列表的字段，格式化时 join
_LIST_FIELDS: set = {"dimensions_to_cover"}

# ── Agent 字段预设 ──────────────────────────────────────────────────────
# 每个 Agent 看到的 brief 字段子集及顺序（保持与各 Agent 原始实现一致）
BRIEF_FIELD_PRESETS: Dict[str, Tuple[str, ...]] = {
    "destination_researcher": (
        "objective", "destination", "duration_days",
        "travel_style", "travel_party", "dimensions_to_cover",
    ),
    "transport_researcher": (
        "objective", "destination", "departure_city",
        "departure_time", "duration_days", "budget",
    ),
    "accommodation_researcher": (
        "objective", "destination", "duration_days",
        "budget", "departure_time", "travel_party",
    ),
    "itinerary_planner": (
        "objective", "destination", "duration_days",
        "departure_city", "departure_time", "budget",
        "travel_style", "travel_party",
    ),
}


def build_brief_context(
    research_brief_str: Optional[str], *, fields: Sequence[str]
) -> str:
    """按字段列表将 research_brief JSON 格式化为上下文文本。"""
    if not research_brief_str:
        return ""

    brief = safe_parse_json(research_brief_str)
    if brief is None:
        return research_brief_str

    parts: list[str] = []
    for key in fields:
        value = brief.get(key)
        if not value:
            continue
        if key in _SKIP_VALUES and str(value) in _SKIP_VALUES[key]:
            continue
        label, suffix = _FIELD_LABELS.get(key, (key, ""))
        if key in _LIST_FIELDS and isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        parts.append(f"{label}：{value}{suffix}")

    return "\n".join(parts)


def build_brief_context_for_agent(
    agent_name: str, research_brief_str: Optional[str]
) -> str:
    """按 Agent 预设字段生成 brief 上下文文本。"""
    fields = BRIEF_FIELD_PRESETS.get(agent_name, ())
    if not fields:
        return ""
    return build_brief_context(research_brief_str, fields=fields)
