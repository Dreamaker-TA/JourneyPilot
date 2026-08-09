"""
旅行风格预设实体模型 (Domain Layer)
用户可选的旅行风格 prompt 模板（区别于 Anthropic Agent Skills）。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

# 一条自写指令最多多少字符 —— **这是这个数在全仓的唯一定义处**。
#
# 它存在的理由是注入侧不该有截断：``PresetInjector`` 把指令包在
# ``<active_preset>…</active_preset>`` 里，而 prompt 组装处曾经对包好的整段做
# ``[:2000]``。九个内置预设包完 405–490 字符，够不到那个数，所以那条截断从来没被
# 触发过；但 ``instructions`` 在实体、请求 schema、前端输入框**三处都没有上限**，
# 用户自写或让模型生成的指令一旦让整段过 2000，切下去的正好是收尾那半行 ——
# prompt 里留一个**没有闭合的 XML 标签**，而模型对一个开着的标签的反应无从预料。
#
# 修法是让过长的指令**根本存不进来**（422），而不是在注入时切一刀：长度是这个字段
# 自己的属性，属于它的定义处；prompt 组装处只负责组装。上限取 4000 —— 内置最长
# 327 字符的八倍多，够写一份很细的风格指令，又远在任何单个 worker prompt 的
# 预算之内。
PRESET_INSTRUCTIONS_MAX_CHARS = 4000
PRESET_NAME_MAX_CHARS = 60
PRESET_DESCRIPTION_MAX_CHARS = 200
# 结构化约束的每个自由文本字段。它们同样进 prompt，同样此前无界。
PRESET_CONSTRAINT_FIELD_MAX_CHARS = 200


class PresetConstraints(BaseModel):
    """Preset 结构化约束"""
    duration: Optional[str] = Field(default=None, max_length=PRESET_CONSTRAINT_FIELD_MAX_CHARS)
    budget: Optional[str] = Field(default=None, max_length=PRESET_CONSTRAINT_FIELD_MAX_CHARS)
    pace: Optional[str] = Field(default=None, max_length=PRESET_CONSTRAINT_FIELD_MAX_CHARS)
    focus_areas: List[str] = Field(default_factory=list)
    output_style: Optional[str] = Field(default=None, max_length=PRESET_CONSTRAINT_FIELD_MAX_CHARS)


class TravelPreset(BaseModel):
    """旅行风格预设"""
    id: str
    user_id: str
    name: str = Field(max_length=PRESET_NAME_MAX_CHARS)
    description: str = Field(max_length=PRESET_DESCRIPTION_MAX_CHARS)
    icon: str = "compass"
    category: str = "custom"
    instructions: str = Field(max_length=PRESET_INSTRUCTIONS_MAX_CHARS)
    constraints: PresetConstraints = Field(default_factory=PresetConstraints)
    is_preset: bool = False  # 是否为系统内置（DB 列名保留以避免迁移）
    usage_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
