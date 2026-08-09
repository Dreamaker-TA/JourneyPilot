"""User-facing memory lifecycle APIs."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Query

from ...builders import get_components
from ...memory.memory_store import (
    MemoryStore,
    USER_MANUAL_IMPORTANCE,
    USER_MANUAL_SESSION_ID,
)
from ...utils.user_text import is_blank
from ..schemas import (
    AddMemoryFactRequest,
    AddMemoryFactResponse,
    MemoryDeleteAllOptions,
    MemoryDeleteOptions,
    MemoryDeletionResponse,
    MemoryFactItem,
    MemoryFactListResponse,
    MemoryForgettingAuditListResponse,
    MemoryForgettingAuditResponse,
    MemoryRetentionCleanupRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["memory"])


def _deletion_response(summary) -> MemoryDeletionResponse:
    return MemoryDeletionResponse(
        request_id=summary.request_id,
        user_id=summary.user_id,
        scope=summary.scope.value if hasattr(summary.scope, "value") else str(summary.scope),
        category=summary.category,
        fact_id=summary.fact_id,
        status=summary.status.value if hasattr(summary.status, "value") else str(summary.status),
        affected_facts=summary.affected_facts,
        affected_entities=summary.affected_entities,
        affected_relations=summary.affected_relations,
        affected_profiles=summary.affected_profiles,
        affected_session_anchors=summary.affected_session_anchors,
        boundary=dict(summary.boundary or {}),
        created_at=summary.created_at,
    )


def _fact_item(row: dict) -> MemoryFactItem:
    return MemoryFactItem(
        fact_id=str(row.get("fact_id")),
        content=row.get("content") or "",
        category=row.get("category"),
        importance=int(row.get("importance") or 5),
        source=row.get("source") or "auto",
        created_at=row.get("created_at") or "",
        expires_at=row.get("expires_at") or "",
    )


@router.get("/{user_id}/memory/facts", response_model=MemoryFactListResponse)
async def list_memory_facts(user_id: str):
    """List every long-term memory fact for a user (newest first)."""
    try:
        rows = await MemoryStore().list_facts(user_id)
        facts = [_fact_item(row) for row in rows]
        return MemoryFactListResponse(user_id=user_id, facts=facts, total=len(facts))
    except Exception as e:
        logger.error("读取 memory facts 失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="memory fact list failed")


@router.post("/{user_id}/memory/facts", response_model=AddMemoryFactResponse)
async def add_memory_fact(
    user_id: str,
    request: AddMemoryFactRequest = Body(...),
):
    """
    Add one memory the traveller wants JourneyPilot to always follow.

    手动记忆用固定 session_id 打标，importance 取最高档，
    规划时无条件全量注入（不依赖语义匹配）。

    **幂等**：同一个人把同一句话加两次，库里只留一条，两次拿回同一个 `fact_id`，
    第二次的 `status` 是 `existing`。    这条去重规则只写在这一处，调用方
    不许各自再写一份。
    """
    # 「空白算不算一个值」全仓一条口径（`utils/user_text.py`）：不算。
    if is_blank(request.content or ""):
        raise HTTPException(status_code=422, detail="memory content is required")
    content = (request.content or "").strip()
    category = (request.category or "").strip() or "preference"
    store = MemoryStore()
    try:
        existing = await store.find_manual_fact(user_id, content)
        if existing is not None:
            return AddMemoryFactResponse(
                user_id=user_id,
                status="existing",
                fact=_fact_item(existing),
            )

        fact_id = await store.save_fact(
            user_id=user_id,
            session_id=USER_MANUAL_SESSION_ID,
            content=content,
            category=category,
            importance=USER_MANUAL_IMPORTANCE,
        )
        if fact_id is None:
            raise HTTPException(status_code=500, detail="memory fact save failed")
        # 已经落下真实记忆后才建立画像行；读取和失败写入都不会制造空画像。
        await get_components().user_profile_memory.ensure_profile_for_write(user_id)
        # 身份由写入交回来，按它读回整条 —— 不拿内容去全量列表里猜是哪一条。
        created = await store.get_fact(user_id, fact_id)
        if created is None:
            # 刚写进去、按 id 读不回来，是真故障，不许包装成「成功但没有对象」。
            raise HTTPException(status_code=500, detail="memory fact save failed")
        return AddMemoryFactResponse(
            user_id=user_id,
            status="created",
            fact=_fact_item(created),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("写入 memory fact 失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="memory fact save failed")


@router.delete("/{user_id}/memory/facts/{fact_id}", response_model=MemoryDeletionResponse)
async def delete_memory_fact(
    user_id: str,
    fact_id: str,
    options: MemoryDeleteOptions = Body(default_factory=MemoryDeleteOptions),
):
    """Physically delete one long-term memory fact."""
    components = get_components()
    try:
        summary = await components.memory_lifecycle_store.delete_one_fact(
            user_id=user_id,
            fact_id=fact_id,
            request_id=options.request_id,
            reason=options.reason,
        )
        return _deletion_response(summary)
    except Exception as e:
        logger.error("删除 memory fact 失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="memory fact delete failed")


@router.delete("/{user_id}/memory/categories/{category}", response_model=MemoryDeletionResponse)
async def delete_memory_category(
    user_id: str,
    category: str,
    options: MemoryDeleteOptions = Body(default_factory=MemoryDeleteOptions),
):
    """Physically delete long-term memory facts by category."""
    components = get_components()
    try:
        summary = await components.memory_lifecycle_store.delete_by_category(
            user_id=user_id,
            category=category,
            request_id=options.request_id,
            reason=options.reason,
            clear_auto_portrait=options.clear_auto_portrait,
            clear_graph=options.clear_graph,
            clear_session_anchors=options.clear_session_anchors,
        )
        return _deletion_response(summary)
    except Exception as e:
        logger.error("按 category 删除 memory 失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="memory category delete failed")


@router.delete("/{user_id}/memory", response_model=MemoryDeletionResponse)
async def delete_all_user_memory(
    user_id: str,
    options: MemoryDeleteAllOptions = Body(default_factory=MemoryDeleteAllOptions),
):
    """Physically delete all first-party long-term memory for a user."""
    components = get_components()
    try:
        summary = await components.memory_lifecycle_store.delete_all_user_memory(
            user_id=user_id,
            request_id=options.request_id,
            reason=options.reason,
            clear_auto_portrait=options.clear_auto_portrait,
            clear_graph=options.clear_graph,
            clear_session_anchors=options.clear_session_anchors,
        )
        return _deletion_response(summary)
    except Exception as e:
        logger.error("删除全部 user memory 失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="user memory delete failed")


@router.post("/{user_id}/memory/retention/cleanup", response_model=MemoryDeletionResponse)
async def cleanup_expired_memory(
    user_id: str,
    request: MemoryRetentionCleanupRequest = Body(default_factory=MemoryRetentionCleanupRequest),
):
    """Manually run retention cleanup for expired facts; no scheduler is implied."""
    components = get_components()
    try:
        summary = await components.memory_lifecycle_store.delete_expired(
            user_id=user_id,
            request_id=request.request_id,
            reason=request.reason,
            limit=request.limit,
        )
        return _deletion_response(summary)
    except Exception as e:
        logger.error("清理 expired memory 失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="memory retention cleanup failed")


@router.get("/{user_id}/memory/forgetting-audits", response_model=MemoryForgettingAuditListResponse)
async def list_memory_forgetting_audits(
    user_id: str,
    limit: int = Query(default=50, ge=1, le=200),
):
    """Read audit-safe forgetting records for a user."""
    components = get_components()
    try:
        audits = await components.memory_lifecycle_store.list_audits(user_id, limit=limit)
        responses = [
            MemoryForgettingAuditResponse(**_deletion_response(audit).model_dump())
            for audit in audits
        ]
        return MemoryForgettingAuditListResponse(
            user_id=user_id,
            audits=responses,
            total=len(responses),
        )
    except Exception as e:
        logger.error("读取 forgetting audit 失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="memory forgetting audit read failed")
