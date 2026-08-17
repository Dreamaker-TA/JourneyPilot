"""
Preset API 路由 (Serving Layer)
用户自定义旅行风格预设的 CRUD 和 AI 辅助生成。
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, HTTPException

from ...local_profile import LOCAL_USER_ID
from ...preset.store import PresetStore
from ..schemas import (
    GenerateInstructionsRequest,
    PresetCreateRequest,
    PresetResponse,
    PresetUpdateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/presets", tags=["presets"])

_store = PresetStore()


def _preset_to_response(preset) -> PresetResponse:
    return PresetResponse(
        id=preset.id,
        name=preset.name,
        description=preset.description,
        icon=preset.icon,
        category=preset.category,
        instructions=preset.instructions,
        constraints=preset.constraints,
        is_preset=preset.is_preset,
        usage_count=preset.usage_count,
        created_at=preset.created_at or "",
        updated_at=preset.updated_at or "",
    )


@router.get("", response_model=List[PresetResponse])
async def list_presets():
    """获取用户所有预设（含系统内置）"""
    try:
        presets = await _store.list_presets(LOCAL_USER_ID)
        return [_preset_to_response(p) for p in presets]
    except Exception as e:
        logger.error(f"获取预设列表失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{preset_id}", response_model=PresetResponse)
async def get_preset(preset_id: str):
    """获取单个预设"""
    preset = await _store.get_preset(preset_id, user_id=LOCAL_USER_ID)
    if not preset:
        raise HTTPException(status_code=404, detail="预设不存在")
    return _preset_to_response(preset)


@router.post("", response_model=PresetResponse)
async def create_preset(request: PresetCreateRequest):
    """创建新预设"""
    try:
        preset = await _store.create_preset(
            user_id=LOCAL_USER_ID,
            data=request.model_dump(),
        )
        return _preset_to_response(preset)
    except Exception as e:
        logger.error(f"创建预设失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{preset_id}", response_model=PresetResponse)
async def update_preset(preset_id: str, request: PresetUpdateRequest):
    """更新预设"""
    try:
        data = {k: v for k, v in request.model_dump().items() if v is not None}
        preset = await _store.update_preset(preset_id, LOCAL_USER_ID, data)
        if not preset:
            raise HTTPException(status_code=404, detail="预设不存在")
        return _preset_to_response(preset)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新预设失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{preset_id}")
async def delete_preset(preset_id: str):
    """删除预设"""
    try:
        deleted = await _store.delete_preset(preset_id, LOCAL_USER_ID)
        if not deleted:
            raise HTTPException(status_code=404, detail="预设不存在")
        return {"status": "ok", "message": "预设已删除"}
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除预设失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/generate-instructions")
async def generate_instructions(request: GenerateInstructionsRequest):
    """AI 辅助生成预设指令"""
    if len(request.description.strip()) < 5:
        raise HTTPException(status_code=400, detail="描述太短，请提供更详细的信息")

    system_prompt = """你是旅行风格预设创建助手。用户会描述他们想要的旅行风格或场景，你需要生成一份结构化的预设指令。

输出 JSON 格式：
{
  "name": "预设名称（简洁，2-6个字）",
  "description": "一句话描述（20字以内）",
  "instructions": "详细的行为指令（参考示例格式，包含 5-8 条具体原则）",
  "constraints": {
    "duration": "建议时长（如 3-5天）或 null",
    "budget": "预算档位（经济/中等/奢华）或 null",
    "pace": "行程节奏（紧凑/悠闲/弹性）或 null",
    "focus_areas": ["关注领域1", "关注领域2"],
    "output_style": "详细日程表"
  },
  "icon": "lucide图标名（如 compass, map, utensils, mountain, baby, crown, piggy-bank, camera, palette, music）",
  "category": "custom"
}

	指令格式示例：
	"你正在以「亲子慢游」模式为用户规划旅行。请遵循以下原则：
	1. 优先选择步行距离短、排队压力低、适合儿童休息的活动。
	2. 每天保留明确的午休或机动时间，避免连续高强度移动。
	3. 对门票预约、儿童年龄限制和安全注意事项给出可核验提醒。"

	只输出 JSON，不要其他文字。"""

    try:
        from ...builders import get_components
        components = get_components()
        fast_llm = components.model_router.get_fast()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": request.description},
        ]

        raw = await fast_llm.ainvoke(messages)
        result_text = raw.strip() if isinstance(raw, str) else (getattr(raw, "content", None) or "").strip()

        from ...utils.json_helpers import safe_parse_json

        result = safe_parse_json(result_text)
        if result is None:
            raise ValueError("无法从 LLM 响应中提取有效 JSON")

        return {"success": True, "data": result}

    except Exception as e:
        logger.error(f"AI 生成预设指令失败: {e}", exc_info=True)
        return {
            "success": False,
            "data": None,
            "error_message": "生成失败，请稍后重试或手动编写指令",
        }
