"""
Preset 注入器 (Domain Layer)
将旅行风格预设的指令和约束转换为可注入到 Agent prompt 的上下文文本。
"""

from __future__ import annotations

from typing import Dict

from ..entities.preset import TravelPreset

# Preset 里**由 Constraint Pack 负责**的两个字段 → pack 的 category。
#
# 这张表是 C 家族那条「三个都叫风格/节奏/预算」的收口处。此前同一个角色有三个来源、
# 强制力各不相同、冲突时没有任何东西负责裁决：
#   * preset 的 ``constraints.pace`` / ``constraints.budget`` 只是 prompt 尾巴上的
#     自由文本，**门不认**；
#   * 用户画像的 ``pace`` / ``typical_budget_level`` 进 Constraint Pack 成为可执行项，
#     **门认**；
#   * TripPlanner 选的 ``identity.style.primary`` 进 ``research_brief.travel_style``。
# 第三个其实是**另一个角色**（这一趟的主题，「历史文化与本地美食」），不参与节奏/预算，
# 所以它留在原处。真正冲突的是前两个，而它们现在只在一层说话：Constraint Pack。
# 于是 pack 既有的单值类仲裁（``_SINGULAR_CATEGORIES`` 含 budget_cap 与
# pace_preference）第一次真的有东西可裁。
PRESET_PACK_CONSTRAINT_CATEGORIES: Dict[str, str] = {
    "pace": "pace_preference",
    "budget": "budget_cap",
}


class PresetInjector:
    """将 Preset 转换为 Agent prompt 上下文"""

    @staticmethod
    def build_context(preset: TravelPreset) -> str:
        """将 Preset 转为可注入到 prompt 的上下文字符串。

        **不包含 pace 与 budget。** 这两项走 ``pack_constraints`` 进 Constraint Pack ——
        在两个地方各说一遍，等于让模型同时读到同一件事的两种强制力（pack 那句门会读，
        这里这句门不读），而冲突时没有任何东西负责裁决。这里只留没有任何 pack category
        认领的东西：描述、指令本体、建议时长、重点关注、输出格式。
        """
        parts = [f"[当前启用预设: {preset.name}]"]
        parts.append(f"预设描述: {preset.description}")
        parts.append(f"\n{preset.instructions}")

        c = preset.constraints
        constraint_parts = []
        if c.duration:
            constraint_parts.append(f"建议时长: {c.duration}")
        if c.focus_areas:
            constraint_parts.append(f"重点关注: {', '.join(c.focus_areas)}")
        if c.output_style:
            constraint_parts.append(f"输出格式偏好: {c.output_style}")

        if constraint_parts:
            parts.append("\n结构化约束:")
            for cp in constraint_parts:
                parts.append(f"- {cp}")

        return "\n".join(parts)

    @staticmethod
    def pack_constraints(preset: TravelPreset) -> Dict[str, str]:
        """Preset 里交给 Constraint Pack 执行的那几项：``category -> 原文``。"""
        constraints = preset.constraints
        mapped: Dict[str, str] = {}
        for field_name, category in PRESET_PACK_CONSTRAINT_CATEGORIES.items():
            value = str(getattr(constraints, field_name, "") or "").strip()
            if value:
                mapped[category] = value
        return mapped

    @staticmethod
    def _wrap_active_preset(prompt: str, preset_context: str) -> str:
        """共享的 Preset 上下文 XML 包装。

        **这个信封只能整段进 prompt。** 调用方不许对返回值做切片：切在中间就切掉了
        ``</active_preset>``，prompt 里留一个开着的标签，而模型对一个没有闭合的标签
        的反应无从预料。长度由 ``entities.preset`` 的字段上限在存入时就管住
        （见那里的 ``PRESET_INSTRUCTIONS_MAX_CHARS``），所以这里不需要、也不许有第二道界。

        （原先还有一个 ``format_for_scope``，给 ``brief_generator`` 用。那个节点自
        第 8 迭代起就不调模型了 —— 简报由受控身份确定性派生 —— 所以那个方法全仓零
        调用方，已删。一个没有调用方的注入格式化器和一句没有判据的合同是同一件事。）
        """
        if not preset_context:
            return ""
        return (
            f"\n<active_preset>\n"
            f"{prompt}\n"
            f"{preset_context}\n"
            f"</active_preset>"
        )

    @staticmethod
    def format_for_planner(preset_context: str) -> str:
        """为 Planner 格式化 Preset 上下文"""
        return PresetInjector._wrap_active_preset(
            "用户启用了风格预设，请据此调整任务分配的侧重点：",
            preset_context,
        )

    @staticmethod
    def format_for_agent(preset_context: str) -> str:
        """为最终输出 Agent（FastAnswer / Synthesizer）格式化"""
        return PresetInjector._wrap_active_preset(
            "请严格按照以下风格预设的指令来组织你的回答：",
            preset_context,
        )
