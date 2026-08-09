"""Agent 与步骤的中文显示名映射（单一数据源）。"""

from __future__ import annotations

import re
from typing import Dict, Final

# ── Agent 显示名 ──────────────────────────────────────────────────────────

AGENT_DISPLAY_NAMES: Final[Dict[str, str]] = {
    # 编排基础设施节点
    "supervisor":               "智能调度",
    "scope_clarifier":          "需求确认",
    "scope_brief_generator":    "需求提炼",
    "constraint_normalizer":    "约束整理",
    "trip_summary_card_brief": "旅行摘要",
    "destination_geo_resolver": "目的地定位",
    "weather_context_builder":  "天气事实",
    "budget_estimate":          "预算估算",
    "delivery_projector":       "交付内容",
    "planner":                  "任务规划",
    "dispatcher":               "任务分发",
    "candidate_gate":           "候选校验",
    "artifact_gate":            "产物校验",
    "delivery_quality_gate":    "交付校验",
    # Worker 节点
    "destination_researcher":   "目的地调研",
    "transport_researcher":     "交通查询",
    "accommodation_researcher": "住宿查询",
    "itinerary_planner":        "行程规划",
    # 最终输出节点
    "fast_answer_agent":        "旅行顾问",
}

# ── 步骤显示名（与 Agent 名有意区分，更偏动作描述）───────────────────────

STEP_DISPLAY_NAMES: Final[Dict[str, str]] = {
    # 阶段级步骤
    "orchestrating":            "编排调度",
    "planning":                 "制定计划",
    "researching":              "调研分析",
    "synthesizing":             "综合总结",
    # 节点级步骤
    "supervisor":               "智能调度",
    "scope_clarifier":          "需求确认",
    "scope_brief_generator":    "需求提炼",
    "constraint_normalizer":    "整理出行约束",
    "trip_summary_card_brief": "整理旅行摘要",
    "destination_geo_resolver": "确认目的地坐标",
    "weather_context_builder":  "获取规划天气",
    "budget_estimate":          "估算整趟预算",
    "delivery_projector":       "生成交付内容",
    "planner":                  "任务规划",
    "dispatcher":               "任务分发",
    "candidate_gate":           "校验候选质量",
    "artifact_gate":            "校验调研产物",
    "delivery_quality_gate":    "校验交付内容",
    "destination_researcher":   "目的地调研",
    "transport_researcher":     "交通方案查询",
    "accommodation_researcher": "住宿信息查询",
    "itinerary_planner":        "行程规划",
    "fast_answer_agent":        "智能解答",
}

# ── 补研轮次后缀正则 ──────────────────────────────────────────────────────

_ROUND_SUFFIX_RE = re.compile(r"^(.+?)_r(\d+)$")


def get_agent_display_name(agent_name: str) -> str:
    """将 Agent 内部名转为中文显示名。

    查找策略：精确匹配 → 去 ``_rN`` 后缀匹配并格式化轮次 → 原样返回。
    """
    label = AGENT_DISPLAY_NAMES.get(agent_name)
    if label is not None:
        return label

    m = _ROUND_SUFFIX_RE.match(agent_name)
    if m:
        base_label = AGENT_DISPLAY_NAMES.get(m.group(1))
        if base_label is not None:
            return f"{base_label}（第{m.group(2)}轮补充）"

    return agent_name


def get_step_display_name(step_name: str) -> str:
    """将步骤/阶段内部名转为中文显示名，未匹配时原样返回。"""
    return STEP_DISPLAY_NAMES.get(step_name, step_name)
