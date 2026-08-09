"""
Preset 持久化存储层 (Infrastructure Layer)
基于 PostgreSQL 存储系统 seed 与用户自定义旅行风格预设。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ..entities.preset import (
    PRESET_DESCRIPTION_MAX_CHARS,
    PRESET_INSTRUCTIONS_MAX_CHARS,
    PRESET_NAME_MAX_CHARS,
    PresetConstraints,
    TravelPreset,
)
from ..infrastructure.database import get_db_session

logger = logging.getLogger(__name__)


class SystemPresetSeed(BaseModel):
    """Validated, release-managed shape for a system travel preset."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=PRESET_NAME_MAX_CHARS)
    description: str = Field(default="", max_length=PRESET_DESCRIPTION_MAX_CHARS)
    icon: str = "compass"
    category: str = "custom"
    instructions: str = Field(min_length=1, max_length=PRESET_INSTRUCTIONS_MAX_CHARS)
    constraints: PresetConstraints = Field(default_factory=PresetConstraints)


def validated_system_preset_seeds(
    raw_presets: Optional[List[Dict[str, Any]]] = None,
) -> List[SystemPresetSeed]:
    """Validate release data before bootstrap or an explicit publication."""
    if raw_presets is None:
        from .presets import get_presets

        raw_presets = get_presets()

    allowed_constraints = set(PresetConstraints.model_fields)
    seeds: List[SystemPresetSeed] = []
    for raw in raw_presets:
        constraints = raw.get("constraints", {}) if isinstance(raw, dict) else {}
        if not isinstance(constraints, dict):
            raise ValueError("system preset constraints must be an object")
        unsupported = set(constraints) - allowed_constraints
        if unsupported:
            raise ValueError(f"system preset constraints contain unsupported fields: {sorted(unsupported)}")
        seeds.append(SystemPresetSeed.model_validate(raw))

    ids = [seed.id for seed in seeds]
    if len(ids) != len(set(ids)):
        raise ValueError("system preset ids must be unique")
    return seeds


class PresetStore:
    """Preset CRUD 持久化管理"""

    async def list_presets(self, user_id: str) -> List[TravelPreset]:
        """获取用户所有预设（自定义 + 系统内置）"""
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT * FROM travel_presets
                    WHERE user_id = :uid OR is_preset = TRUE
                    ORDER BY is_preset DESC, updated_at DESC
                """),
                {"uid": user_id},
            )
            rows = result.mappings().all()
            return [self._row_to_preset(dict(r)) for r in rows]

    async def get_preset(self, preset_id: str, user_id: Optional[str] = None) -> Optional[TravelPreset]:
        """获取单个预设。传入 user_id 时仅允许读取系统预设或本人自定义预设。"""
        async with get_db_session() as session:
            if user_id:
                result = await session.execute(
                    text("""
                        SELECT * FROM travel_presets
                        WHERE id = :sid AND (user_id = :uid OR is_preset = TRUE)
                    """),
                    {"sid": preset_id, "uid": user_id},
                )
            else:
                result = await session.execute(
                    text("SELECT * FROM travel_presets WHERE id = :sid"),
                    {"sid": preset_id},
                )
            row = result.mappings().first()
            if not row:
                return None
            return self._row_to_preset(dict(row))

    async def create_preset(self, user_id: str, data: Dict[str, Any]) -> TravelPreset:
        """创建新预设"""
        # asyncpg 的 timestamptz 参数只接受 datetime 对象；ISO 字符串留给响应模型。
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        preset_id = data.get("id") or str(uuid.uuid4())

        constraints = data.get("constraints", {})
        if isinstance(constraints, PresetConstraints):
            constraints = constraints.model_dump()

        async with get_db_session() as session:
            await session.execute(
                text("""
                    INSERT INTO travel_presets
                        (id, user_id, name, description, icon, category,
                         instructions, constraints, is_preset, created_at, updated_at)
                    VALUES
                        (:id, :uid, :name, :desc, :icon, :cat,
                         :instr, CAST(:constraints AS jsonb), :preset, :created, :updated)
                """),
                {
                    "id": preset_id,
                    "uid": user_id,
                    "name": data.get("name", ""),
                    "desc": data.get("description", ""),
                    "icon": data.get("icon", "compass"),
                    "cat": data.get("category", "custom"),
                    "instr": data.get("instructions", ""),
                    "constraints": json.dumps(constraints, ensure_ascii=False),
                    "preset": data.get("is_preset", False),
                    "created": now_dt,
                    "updated": now_dt,
                },
            )

        return TravelPreset(
            id=preset_id,
            user_id=user_id,
            name=data.get("name", ""),
            description=data.get("description", ""),
            icon=data.get("icon", "compass"),
            category=data.get("category", "custom"),
            instructions=data.get("instructions", ""),
            constraints=PresetConstraints(**constraints) if isinstance(constraints, dict) else constraints,
            is_preset=data.get("is_preset", False),
            created_at=now,
            updated_at=now,
        )

    async def update_preset(
        self, preset_id: str, user_id: str, data: Dict[str, Any]
    ) -> Optional[TravelPreset]:
        """更新预设（不能修改系统内置预设）"""
        existing = await self.get_preset(preset_id)
        if not existing:
            return None
        if existing.is_preset:
            raise ValueError("不能修改系统内置预设")
        if existing.user_id != user_id:
            raise PermissionError("无权修改此预设")

        now = datetime.now(timezone.utc)
        constraints = data.get("constraints", existing.constraints)
        if isinstance(constraints, PresetConstraints):
            constraints = constraints.model_dump()
        elif not isinstance(constraints, dict):
            constraints = {}

        async with get_db_session() as session:
            await session.execute(
                text("""
                    UPDATE travel_presets SET
                        name = :name,
                        description = :desc,
                        icon = :icon,
                        category = :cat,
                        instructions = :instr,
                        constraints = CAST(:constraints AS jsonb),
                        updated_at = :updated
                    WHERE id = :sid AND user_id = :uid
                """),
                {
                    "sid": preset_id,
                    "uid": user_id,
                    "name": data.get("name", existing.name),
                    "desc": data.get("description", existing.description),
                    "icon": data.get("icon", existing.icon),
                    "cat": data.get("category", existing.category),
                    "instr": data.get("instructions", existing.instructions),
                    "constraints": json.dumps(constraints, ensure_ascii=False),
                    "updated": now,
                },
            )

        return await self.get_preset(preset_id)

    async def delete_preset(self, preset_id: str, user_id: str) -> bool:
        """删除预设（不能删除系统内置预设）"""
        existing = await self.get_preset(preset_id)
        if not existing:
            return False
        if existing.is_preset:
            raise ValueError("不能删除系统内置预设")
        if existing.user_id != user_id:
            raise PermissionError("无权删除此预设")

        async with get_db_session() as session:
            await session.execute(
                text("DELETE FROM travel_presets WHERE id = :sid AND user_id = :uid"),
                {"sid": preset_id, "uid": user_id},
            )
        return True

    async def increment_usage(self, preset_id: str) -> None:
        """使用计数 +1"""
        async with get_db_session() as session:
            await session.execute(
                text("""
                    UPDATE travel_presets
                    SET usage_count = usage_count + 1, updated_at = NOW()
                    WHERE id = :sid
                """),
                {"sid": preset_id},
            )

    async def ensure_presets(self) -> None:
        """Bootstrap missing system presets without changing existing product data."""
        presets = validated_system_preset_seeds()
        now = datetime.now(timezone.utc)
        async with get_db_session() as session:
            for preset in presets:
                payload = preset.model_dump(mode="json", exclude_unset=True)
                await session.execute(
                    text("""
                        INSERT INTO travel_presets
                            (id, user_id, name, description, icon, category,
                             instructions, constraints, is_preset, created_at, updated_at)
                        VALUES
                            (:id, :uid, :name, :desc, :icon, :cat,
                             :instr, CAST(:constraints AS jsonb), TRUE, :ts, :ts)
                        ON CONFLICT (id) DO NOTHING
                    """),
                    {
                        "id": payload["id"],
                        "uid": "__system__",
                        "name": payload["name"],
                        "desc": payload["description"],
                        "icon": payload["icon"],
                        "cat": payload["category"],
                        "instr": payload["instructions"],
                        "constraints": json.dumps(payload["constraints"], ensure_ascii=False),
                        "ts": now,
                    },
                )
        logger.info(f"内置预设检查完成（{len(presets)} 个）")

    async def publish_system_presets(self) -> None:
        """Explicitly publish reviewed system presets to an existing environment."""
        presets = validated_system_preset_seeds()
        now = datetime.now(timezone.utc)
        async with get_db_session() as session:
            for preset in presets:
                payload = preset.model_dump(mode="json", exclude_unset=True)
                await session.execute(
                    text("""
                        INSERT INTO travel_presets
                            (id, user_id, name, description, icon, category,
                             instructions, constraints, is_preset, created_at, updated_at)
                        VALUES
                            (:id, :uid, :name, :desc, :icon, :cat,
                             :instr, CAST(:constraints AS jsonb), TRUE, :ts, :ts)
                        ON CONFLICT (id) DO UPDATE SET
                            user_id = EXCLUDED.user_id,
                            name = EXCLUDED.name,
                            description = EXCLUDED.description,
                            icon = EXCLUDED.icon,
                            category = EXCLUDED.category,
                            instructions = EXCLUDED.instructions,
                            constraints = EXCLUDED.constraints,
                            is_preset = TRUE,
                            updated_at = EXCLUDED.updated_at
                    """),
                    {
                        "id": payload["id"],
                        "uid": "__system__",
                        "name": payload["name"],
                        "desc": payload["description"],
                        "icon": payload["icon"],
                        "cat": payload["category"],
                        "instr": payload["instructions"],
                        "constraints": json.dumps(payload["constraints"], ensure_ascii=False),
                        "ts": now,
                    },
                )
        logger.info(f"系统预设已受控发布（{len(presets)} 个）")

    def _row_to_preset(self, row: Dict[str, Any]) -> TravelPreset:
        constraints_data = row.get("constraints") or {}
        if isinstance(constraints_data, str):
            constraints_data = json.loads(constraints_data)

        return TravelPreset(
            id=str(row["id"]),
            user_id=row.get("user_id", ""),
            name=row.get("name", ""),
            description=row.get("description", ""),
            icon=row.get("icon", "compass"),
            category=row.get("category", "custom"),
            instructions=row.get("instructions", ""),
            constraints=PresetConstraints(**constraints_data),
            is_preset=row.get("is_preset", False),
            usage_count=row.get("usage_count", 0),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )
