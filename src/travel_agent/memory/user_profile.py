"""
长期用户画像记忆 (Infrastructure Layer)
基于 PostgreSQL 持久化存储用户偏好和历史行程。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text

from ..entities.user import TravelPreference, UserProfile
from ..infrastructure.database import get_db_session

logger = logging.getLogger(__name__)


def _bind_ts(value: Optional[str]) -> Optional[datetime]:
    """模型层时间戳是 ISO 字符串；asyncpg 的 timestamptz 参数只接受 datetime。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class UserProfileMemory:
    """用户画像持久化管理"""

    async def get_user_profile(self, user_id: str) -> Optional[UserProfile]:
        """从 PostgreSQL 获取用户画像，不存在则返回 None"""
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT * FROM user_profiles WHERE user_id = :uid"),
                {"uid": user_id},
            )
            row = result.mappings().first()
            if not row:
                return None
            return self._row_to_profile(dict(row))

    async def ensure_profile_for_write(self, user_id: str) -> UserProfile:
        """为真实写入取得画像；只有调用方已经有内容要保存时才允许创建。"""
        profile = await self.get_user_profile(user_id)
        if profile is None:
            profile = UserProfile(
                user_id=user_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            await self.save_user_profile(profile)
        return profile

    async def get_revision(self, user_id: str) -> int:
        """画像版本号。延迟执行的后台任务用它固定「入队时看到的是哪一版画像」。"""
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT revision FROM user_profiles WHERE user_id = :uid"),
                {"uid": user_id},
            )
            return int(result.scalar() or 0)

    async def save_user_profile(self, profile: UserProfile) -> None:
        """保存或更新用户画像（upsert）。每次写入 revision 前进一格。"""
        now = datetime.now(timezone.utc).isoformat()
        profile.updated_at = now
        if not profile.created_at:
            profile.created_at = now

        async with get_db_session() as session:
            await session.execute(
                text("""
                    INSERT INTO user_profiles
                        (user_id, display_name, preferences, auto_portrait, revision,
                         created_at, updated_at)
                    VALUES
                        (:uid, :name, CAST(:prefs AS jsonb), :portrait, 1, :created, :updated)
                    ON CONFLICT (user_id) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        preferences = EXCLUDED.preferences,
                        auto_portrait = EXCLUDED.auto_portrait,
                        revision = user_profiles.revision + 1,
                        updated_at = EXCLUDED.updated_at
                """),
                {
                    "uid": profile.user_id,
                    "name": profile.display_name,
                    "prefs": json.dumps(profile.preferences.model_dump(), ensure_ascii=False),
                    "portrait": profile.auto_portrait,
                    "created": _bind_ts(profile.created_at) or datetime.now(timezone.utc),
                    "updated": _bind_ts(profile.updated_at) or datetime.now(timezone.utc),
                },
            )

    async def update_preferences(self, user_id: str, updates: Dict[str, Any]) -> None:
        """更新用户偏好：**带上来的键整组覆盖，没带上来的键原样保留**。

        所以这**不是**「以 updates 为完整期望状态」：少带一组不会清空那一组。偏好设置页每次保存都发全套键，那条口径前后端
        保持一致，不许在另一端写成第二种说法。

        这个方法的语义就在它身上 —— 没带上来的组逐字不动、带上来的列表组
        是覆盖而不是追加、两个调用点互不踩踏。
        """
        profile = await self.ensure_profile_for_write(user_id)
        current = profile.preferences.model_dump()
        current.update(updates)
        profile.preferences = TravelPreference(**current)
        await self.save_user_profile(profile)

    async def clear_profile_memory(self, user_id: str) -> int:
        """清掉画像里**系统自己学来的**那一份，用户手填的偏好一个字都不动。

        profile 行上住着两样来源完全不同的东西：``preferences``（六组偏好与常用
        出发地，用户在「我的偏好」屏亲手填的，写入方只有 ``api/routes/user.py``
        那两条路由）与 ``auto_portrait``（系统从对话里总结出来的）。**只有后者是
        「记忆」**，也只有后者会被遗忘流程清掉。

        这里没有参数可拨，这是刻意的：不得有开关让一次「删除记忆」把用户手填的
        偏好（尤其常用出发地）带进同一笔删除。「碰不到手填偏好」不是一个调用约定，
        而是一件做不到的事 —— 这类开关不许长出来。
        """
        if not user_id:
            return 0
        profile = await self.get_user_profile(user_id)
        if profile is None:
            return 0
        if not profile.auto_portrait:
            return 0
        profile.auto_portrait = ""
        await self.save_user_profile(profile)
        return 1

    # -----------------------------------------------------------------------
    # 辅助方法
    # -----------------------------------------------------------------------

    def _row_to_profile(self, row: Dict[str, Any]) -> UserProfile:
        prefs_data = row.get("preferences") or {}
        if isinstance(prefs_data, str):
            prefs_data = json.loads(prefs_data)

        return UserProfile(
            user_id=row["user_id"],
            display_name=row.get("display_name") or "",
            preferences=TravelPreference(**prefs_data),
            auto_portrait=row.get("auto_portrait") or "",
            revision=int(row.get("revision") or 0),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )
