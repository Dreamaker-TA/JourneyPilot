"""
用户配置 API (Serving Layer)
管理用户画像和偏好。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from ...builders import get_components
from ...entities.user import (
    TRAVEL_PREFERENCE_GROUPS,
    PreferenceOptionGroup,
    TravelPreference,
)
from ..schemas import (
    ChatSessionDetail,
    ChatSessionSummary,
    DefaultOriginRequest,
    DefaultOriginResponse,
    UpdatePreferencesRequest,
    UpdateSessionRequest,
    UserProfileResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/preference-options", response_model=list[PreferenceOptionGroup])
async def get_preference_options() -> list[PreferenceOptionGroup]:
    """六组偏好各有哪些可选项 —— 界面画 chip 用的那张表。

    **这条路由存在的理由就是「那张表只许有一份」。** 表放在后端，因为取值原文要进模型
    （``panels/constraint.py``）。

    **不走数据库、也不进 ``product_configurations``**：这不是可以被运营改的产品配置，
    是 ``TravelPreference`` 自己的合同 —— 它的值决定模型读到什么。让它经过一张表只会
    给一件恒定的东西加一个 503（``/api/product/trip-planner`` 那条是 DB 支撑的，
    别把两者混成一副语法）。

    路径上没有 ``{user_id}``：这张表与人无关，而那一屏在画像 404 的时候也要画得出 chip。
    """

    return list(TRAVEL_PREFERENCE_GROUPS)


@router.get("/{user_id}/default-origin", response_model=DefaultOriginResponse)
async def get_default_origin(user_id: str):
    components = get_components()
    profile = await components.user_profile_memory.get_user_profile(user_id)
    return DefaultOriginResponse(
        user_id=user_id,
        place=profile.preferences.default_origin if profile else None,
    )


@router.put("/{user_id}/default-origin", response_model=DefaultOriginResponse)
async def set_default_origin(user_id: str, request: DefaultOriginRequest):
    from ...entities.trip_input import ORIGIN_PLACE_KINDS

    if request.place.kind not in ORIGIN_PLACE_KINDS:
        raise HTTPException(status_code=422, detail="常用出发地只支持城市、机场或火车站")
    components = get_components()
    await components.user_profile_memory.update_preferences(
        user_id, {"default_origin": request.place.model_dump(mode="json")}
    )
    return DefaultOriginResponse(user_id=user_id, place=request.place)


@router.get("/{user_id}/profile", response_model=UserProfileResponse)
async def get_user_profile(user_id: str):
    """获取用户画像"""
    components = get_components()
    try:
        profile = await components.user_profile_memory.get_user_profile(user_id)
        if profile is None:
            return UserProfileResponse(
                user_id=user_id,
                display_name="",
                preferences=TravelPreference().model_dump(),
            )
        return UserProfileResponse(
            user_id=profile.user_id,
            display_name=profile.display_name,
            preferences=profile.preferences.model_dump(),
        )
    except Exception as e:
        logger.error(f"获取用户画像失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _preference_rejection(error: ValidationError) -> str:
    """把 `TravelPreference` 的拒绝理由投影成一句客户端读得懂的话。

    只取校验器自己写的那句（Pydantic 会给它加一个 ``Value error, `` 前缀），
    **不带**类型名、字段路径与 ``errors.pydantic.dev`` 链接 —— 那些是内部实现细节，
    不该整段发给客户端。
    """

    messages = [
        str(err.get("msg", "")).removeprefix("Value error, ").strip()
        for err in error.errors()
    ]
    unique = list(dict.fromkeys(m for m in messages if m))
    return "；".join(unique) or "偏好取值不合法"


@router.patch("/{user_id}/preferences")
async def update_preferences(user_id: str, request: UpdatePreferencesRequest):
    """更新用户偏好（设置页显式编辑：带上来的每一组都按覆盖处理，取消勾选才能真正移除）"""
    components = get_components()
    unknown = sorted(set(request.preferences) - set(TravelPreference.model_fields))
    if unknown:
        # 客户端发来一个后端不认的偏好组，是**请求的问题**，不是服务端故障：
        # 这里显式 422，而不是让 `TravelPreference(extra="forbid")` 抛 `ValidationError`
        # 包成 500 并把 Pydantic 的原始报文（含 errors.pydantic.dev 链接）整段发给客户端 ——
        # 一个错误的字段名读起来像服务挂了。
        raise HTTPException(
            status_code=422,
            detail=f"不认识这几组偏好：{'、'.join(unknown)}",
        )
    try:
        await components.user_profile_memory.update_preferences(
            user_id, request.preferences
        )
        return {"status": "success", "message": "用户偏好已更新"}
    except HTTPException:
        raise
    except ValidationError as e:
        # 取值不在选项表里、或者是一串空白：同样是**请求的问题**。
        # 判定在 `TravelPreference` 那一处（读写共用），这里只负责把它投影成一个
        # 请求错误 —— 不是 500，也不是把 Pydantic 的原始报文发给客户端。
        raise HTTPException(status_code=422, detail=_preference_rejection(e))
    except Exception as e:
        logger.error(f"更新用户偏好失败: {e}")
        raise HTTPException(status_code=500, detail="保存偏好失败，请稍后重试")


@router.get("/{user_id}/sessions", response_model=list[ChatSessionSummary])
async def list_user_sessions(user_id: str):
    """获取用户会话摘要列表（按 updated_at 倒序）。"""
    components = get_components()
    try:
        return await components.chat_session_memory.list_sessions(user_id)
    except Exception as e:
        logger.error(f"获取会话列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{user_id}/sessions/{session_id}", response_model=ChatSessionDetail)
async def get_user_session_detail(user_id: str, session_id: str):
    """获取单个会话详情（含消息回放与澄清状态）。"""
    components = get_components()
    try:
        detail = await components.chat_session_memory.get_session_detail(user_id, session_id)
        if not detail:
            raise HTTPException(status_code=404, detail="会话不存在")
        return detail
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取会话详情失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{user_id}/sessions/{session_id}", response_model=ChatSessionSummary)
async def update_user_session(user_id: str, session_id: str, request: UpdateSessionRequest):
    """重命名会话标题，返回更新后的摘要。"""
    components = get_components()
    try:
        updated = await components.chat_session_memory.update_session_title(
            user_id, session_id, request.title
        )
        if not updated:
            raise HTTPException(status_code=404, detail="会话不存在")
        return updated
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"重命名会话失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{user_id}/sessions/{session_id}")
async def delete_user_session(user_id: str, session_id: str):
    """删除指定会话（硬删除，级联事件）。"""
    components = get_components()
    try:
        deleted = await components.chat_session_memory.delete_session(user_id, session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"status": "success", "message": f"会话 [{session_id}] 已删除"}
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
