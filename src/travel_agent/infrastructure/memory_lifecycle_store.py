"""Memory lifecycle deletion orchestration and audit-safe persistence."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from ..entities.memory_lifecycle import (
    ForgettingAuditRecord,
    ForgettingAuditStatus,
    MemoryDeleteScope,
    MemoryDeletionSummary,
    default_forgetting_boundary,
    generate_memory_forgetting_request_id,
    sanitize_forgetting_metadata,
)
from ..entities.trip_run import utc_now_iso
from ..memory.chat_session import ChatSessionMemory
from ..memory.memory_graph import MemoryGraph
from ..memory.memory_store import MemoryStore
from ..memory.user_profile import UserProfileMemory
from .database import get_db_session
from .row_values import iso_or_empty as _iso, json_dumps as _json_dumps




def _json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value




def _record_from_row(row: Dict[str, Any]) -> ForgettingAuditRecord:
    return ForgettingAuditRecord(
        request_id=row["request_id"],
        user_id=row.get("user_id") or "",
        scope=MemoryDeleteScope(row.get("scope") or MemoryDeleteScope.ALL_USER.value),
        category=row.get("category"),
        fact_id=row.get("fact_id"),
        status=ForgettingAuditStatus(row.get("status") or ForgettingAuditStatus.COMPLETED.value),
        affected_facts=int(row.get("affected_facts") or 0),
        affected_entities=int(row.get("affected_entities") or 0),
        affected_relations=int(row.get("affected_relations") or 0),
        affected_profiles=int(row.get("affected_profiles") or 0),
        affected_session_anchors=int(row.get("affected_session_anchors") or 0),
        boundary=dict(_json_loads(row.get("boundary"), {})),
        metadata=dict(_json_loads(row.get("metadata"), {})),
        created_at=_iso(row.get("created_at")) or utc_now_iso(),
    )


class MemoryLifecycleStore:
    """PostgreSQL-backed memory lifecycle service."""

    def __init__(
        self,
        *,
        memory_store: Optional[MemoryStore] = None,
        memory_graph: Optional[MemoryGraph] = None,
        user_profile_memory: Optional[UserProfileMemory] = None,
        chat_session_memory: Optional[ChatSessionMemory] = None,
    ) -> None:
        self.memory_store = memory_store or MemoryStore()
        self.memory_graph = memory_graph or MemoryGraph()
        self.user_profile_memory = user_profile_memory or UserProfileMemory()
        self.chat_session_memory = chat_session_memory or ChatSessionMemory()

    async def delete_one_fact(
        self,
        *,
        user_id: str,
        fact_id: str,
        request_id: Optional[str] = None,
        reason: str = "",
    ) -> MemoryDeletionSummary:
        summary = MemoryDeletionSummary(
            request_id=request_id or generate_memory_forgetting_request_id(),
            user_id=user_id,
            scope=MemoryDeleteScope.FACT,
            fact_id=str(fact_id),
            affected_facts=await self.memory_store.delete_fact(user_id, fact_id),
            boundary=default_forgetting_boundary(),
            metadata=sanitize_forgetting_metadata({"reason": reason, "fact_id": str(fact_id)}),
        )
        await self.record_audit(summary)
        return summary

    async def _clear_system_learned_memory(
        self,
        user_id: str,
        *,
        clear_auto_portrait: bool,
        clear_graph: bool,
        clear_session_anchors: bool,
    ) -> Dict[str, int]:
        """清掉**系统自己学来的**那几样，用户手填的偏好一律不动。

        「一次删除只能动它宣称的那几样」这条规则**只在这里写一次**，两个删除入口
        （按类别、全部）共用它。

        六组偏好与常用出发地（``default_origin``）长在同一个 ``TravelPreference``
        上，而那是用户在「我的偏好」屏亲手填的，唯一的写入方是
        ``api/routes/user.py`` 那两条路由 —— 遗忘流程碰不得。这里只需要决定
        「要不要清画像」。
        """
        counts = {"entities": 0, "relations": 0, "profiles": 0, "anchors": 0}
        if clear_graph:
            graph_counts = await self.memory_graph.delete_user_graph(user_id)
            counts["entities"] = int(graph_counts.get("entities", 0))
            counts["relations"] = int(graph_counts.get("relations", 0))
        if clear_auto_portrait:
            counts["profiles"] = await self.user_profile_memory.clear_profile_memory(
                user_id
            )
        if clear_session_anchors:
            counts["anchors"] = await self.chat_session_memory.clear_user_anchors(user_id)
        return counts

    async def delete_by_category(
        self,
        *,
        user_id: str,
        category: str,
        request_id: Optional[str] = None,
        reason: str = "",
        clear_auto_portrait: bool = False,
        clear_graph: bool = False,
        clear_session_anchors: bool = False,
    ) -> MemoryDeletionSummary:
        affected_facts = await self.memory_store.delete_by_category(user_id, category)
        cleared = await self._clear_system_learned_memory(
            user_id,
            clear_auto_portrait=clear_auto_portrait,
            clear_graph=clear_graph,
            clear_session_anchors=clear_session_anchors,
        )

        summary = MemoryDeletionSummary(
            request_id=request_id or generate_memory_forgetting_request_id(),
            user_id=user_id,
            scope=MemoryDeleteScope.CATEGORY,
            category=category,
            affected_facts=affected_facts,
            affected_entities=cleared["entities"],
            affected_relations=cleared["relations"],
            affected_profiles=cleared["profiles"],
            affected_session_anchors=cleared["anchors"],
            boundary=default_forgetting_boundary(
                auto_portrait_cleared=clear_auto_portrait,
                graph_cleared=clear_graph,
                session_anchor_cleared=clear_session_anchors,
            ),
            metadata=sanitize_forgetting_metadata({
                "reason": reason,
                "category": category,
                "clear_auto_portrait": clear_auto_portrait,
                "clear_graph": clear_graph,
                "clear_session_anchors": clear_session_anchors,
            }),
        )
        await self.record_audit(summary)
        return summary

    async def delete_all_user_memory(
        self,
        *,
        user_id: str,
        request_id: Optional[str] = None,
        reason: str = "",
        clear_auto_portrait: bool = True,
        clear_graph: bool = True,
        clear_session_anchors: bool = True,
    ) -> MemoryDeletionSummary:
        affected_facts = await self.memory_store.delete_all_user_facts(user_id)
        cleared = await self._clear_system_learned_memory(
            user_id,
            clear_auto_portrait=clear_auto_portrait,
            clear_graph=clear_graph,
            clear_session_anchors=clear_session_anchors,
        )

        summary = MemoryDeletionSummary(
            request_id=request_id or generate_memory_forgetting_request_id(),
            user_id=user_id,
            scope=MemoryDeleteScope.ALL_USER,
            affected_facts=affected_facts,
            affected_entities=cleared["entities"],
            affected_relations=cleared["relations"],
            affected_profiles=cleared["profiles"],
            affected_session_anchors=cleared["anchors"],
            boundary=default_forgetting_boundary(
                auto_portrait_cleared=clear_auto_portrait,
                graph_cleared=clear_graph,
                session_anchor_cleared=clear_session_anchors,
            ),
            metadata=sanitize_forgetting_metadata({
                "reason": reason,
                "clear_auto_portrait": clear_auto_portrait,
                "clear_graph": clear_graph,
                "clear_session_anchors": clear_session_anchors,
            }),
        )
        await self.record_audit(summary)
        return summary

    async def delete_expired(
        self,
        *,
        user_id: Optional[str] = None,
        request_id: Optional[str] = None,
        reason: str = "retention_cleanup",
        limit: int = 1000,
    ) -> MemoryDeletionSummary:
        affected_facts = await self.memory_store.delete_expired_facts(user_id, limit=limit)
        summary = MemoryDeletionSummary(
            request_id=request_id or generate_memory_forgetting_request_id(),
            user_id=user_id or "*",
            scope=MemoryDeleteScope.EXPIRED,
            affected_facts=affected_facts,
            boundary=default_forgetting_boundary(),
            metadata=sanitize_forgetting_metadata({"reason": reason, "limit": limit}),
        )
        await self.record_audit(summary)
        return summary

    async def record_audit(self, summary: MemoryDeletionSummary) -> ForgettingAuditRecord:
        record = ForgettingAuditRecord(**summary.model_dump())
        record.metadata = sanitize_forgetting_metadata(record.metadata)
        async with get_db_session() as session:
            await session.execute(
                text("""
                    INSERT INTO memory_forgetting_audits
                        (request_id, user_id, scope, category, fact_id, status,
                         affected_facts, affected_entities, affected_relations,
                         affected_profiles, affected_session_anchors, boundary,
                         metadata, created_at)
                    VALUES
                        (:request_id, :user_id, :scope, :category, :fact_id, :status,
                         :affected_facts, :affected_entities, :affected_relations,
                         :affected_profiles, :affected_session_anchors,
                         CAST(:boundary AS jsonb), CAST(:metadata AS jsonb), NOW())
                    ON CONFLICT (request_id) DO NOTHING
                """),
                {
                    "request_id": record.request_id,
                    "user_id": record.user_id,
                    "scope": record.scope.value,
                    "category": record.category,
                    "fact_id": record.fact_id,
                    "status": record.status.value,
                    "affected_facts": record.affected_facts,
                    "affected_entities": record.affected_entities,
                    "affected_relations": record.affected_relations,
                    "affected_profiles": record.affected_profiles,
                    "affected_session_anchors": record.affected_session_anchors,
                    "boundary": _json_dumps(record.boundary),
                    "metadata": _json_dumps(record.metadata),
                },
            )
        return record

    async def list_audits(self, user_id: str, *, limit: int = 50) -> List[ForgettingAuditRecord]:
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT *
                    FROM memory_forgetting_audits
                    WHERE user_id = :user_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                """),
                {"user_id": user_id, "limit": max(1, min(limit, 200))},
            )
            return [_record_from_row(dict(row)) for row in result.mappings().all()]


class InMemoryMemoryLifecycleStore(MemoryLifecycleStore):
    """In-memory implementation with the same public async contract."""

    def __init__(
        self,
        *,
        memory_store: Optional[MemoryStore] = None,
        memory_graph: Optional[Any] = None,
        user_profile_memory: Optional[Any] = None,
        chat_session_memory: Optional[Any] = None,
    ) -> None:
        super().__init__(
            memory_store=memory_store,
            memory_graph=memory_graph,
            user_profile_memory=user_profile_memory,
            chat_session_memory=chat_session_memory,
        )
        self.records: Dict[str, ForgettingAuditRecord] = {}

    async def record_audit(self, summary: MemoryDeletionSummary) -> ForgettingAuditRecord:
        record = ForgettingAuditRecord(**summary.model_dump())
        record.metadata = sanitize_forgetting_metadata(record.metadata)
        self.records.setdefault(record.request_id, record)
        return self.records[record.request_id]

    async def list_audits(self, user_id: str, *, limit: int = 50) -> List[ForgettingAuditRecord]:
        records = [record for record in self.records.values() if record.user_id == user_id]
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records[: max(1, min(limit, 200))]


_memory_lifecycle_store_singleton: Optional[MemoryLifecycleStore] = None


def get_memory_lifecycle_store() -> MemoryLifecycleStore:
    global _memory_lifecycle_store_singleton
    if _memory_lifecycle_store_singleton is None:
        _memory_lifecycle_store_singleton = MemoryLifecycleStore()
    return _memory_lifecycle_store_singleton
