"""运行控制命令的持久化。

**最终事实在 `trip_run_commands`**：API 接受一次 cancel 或 supplement 就是往这张表写一行，
之后执行器在协作边界读它。发请求的那一刻进程内有没有 handle 不再决定命令存不存在 ——
在此之前它决定，于是「已接受」经常是一句在进程重启后不成立的话。

时间判断交给 PostgreSQL 的 `NOW()`，与执行租约同一个理由：判据只能有一个时钟。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import bindparam, text

from ..entities.trip_run import (
    OPEN_RUN_COMMAND_STATUSES,
    RunCommand,
    RunCommandStatus,
    RunCommandType,
    coerce_run_command_status,
    coerce_run_command_type,
    generate_run_command_id,
    run_command_digest,
)
from .database import get_db_session
from .run_execution_store import EXECUTOR_ID

_OPEN_STATUS_VALUES = tuple(sorted(status.value for status in OPEN_RUN_COMMAND_STATUSES))


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.isoformat()
    text_value = str(value).strip()
    return text_value or None


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_object(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _command_from_row(row: Mapping[str, Any]) -> RunCommand:
    result = row.get("result")
    return RunCommand(
        command_id=row["command_id"],
        run_id=row["run_id"],
        command_type=coerce_run_command_type(row["command_type"]),
        payload=_json_object(row.get("payload")),
        request_digest=row["request_digest"],
        status=coerce_run_command_status(row["status"]),
        claimed_by=row.get("claimed_by"),
        claimed_at=_iso(row.get("claimed_at")),
        consumed_at=_iso(row.get("consumed_at")),
        result=_json_object(result) if isinstance(result, Mapping) else None,
        error_code=row.get("error_code"),
        created_at=_iso(row.get("created_at")) or "",
        updated_at=_iso(row.get("updated_at")) or "",
    )


class RunCommandStore:
    """`trip_run_commands` 的仓储。"""

    async def enqueue(
        self,
        run_id: str,
        command_type: str | RunCommandType,
        payload: Optional[Dict[str, Any]] = None,
    ) -> tuple[RunCommand, bool]:
        """写入一条命令，返回 `(命令, 是否新建)`。

        重发同一个意图拿回同一行：唯一键是 `(run_id, request_digest)`，摘要规则在
        `run_command_digest`。所以「用户连点三次停止」是一条命令、一张回执，而不是三条
        待执行器分别消费的命令。
        """

        kind = coerce_run_command_type(command_type)
        body = dict(payload or {})
        digest = run_command_digest(kind, body)
        async with get_db_session() as session:
            inserted = await session.execute(
                text(
                    """
                    INSERT INTO trip_run_commands
                        (command_id, run_id, command_type, payload, request_digest,
                         status, created_at, updated_at)
                    VALUES
                        (:command_id, :run_id, :command_type, CAST(:payload AS jsonb),
                         :request_digest, :status, NOW(), NOW())
                    ON CONFLICT (run_id, request_digest) DO NOTHING
                    RETURNING *
                    """
                ),
                {
                    "command_id": generate_run_command_id(),
                    "run_id": run_id,
                    "command_type": kind.value,
                    "payload": _dumps(body),
                    "request_digest": digest,
                    "status": RunCommandStatus.PENDING.value,
                },
            )
            row = inserted.mappings().first()
            if row is not None:
                return _command_from_row(dict(row)), True
            existing = await session.execute(
                text(
                    "SELECT * FROM trip_run_commands "
                    "WHERE run_id = :run_id AND request_digest = :request_digest"
                ),
                {"run_id": run_id, "request_digest": digest},
            )
            return _command_from_row(dict(existing.mappings().first())), False

    async def get(self, run_id: str, command_id: str) -> Optional[RunCommand]:
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    "SELECT * FROM trip_run_commands "
                    "WHERE run_id = :run_id AND command_id = :command_id"
                ),
                {"run_id": run_id, "command_id": command_id},
            )
            row = result.mappings().first()
            return _command_from_row(dict(row)) if row else None

    async def claim_pending(self, run_id: str, *, limit: int = 20) -> List[RunCommand]:
        """按创建顺序取走这个 run 的待处理命令。

        `FOR UPDATE SKIP LOCKED`：多实例不是产品目标，但同一进程里两个协程同时轮询同一个
        run 是完全可能的（SSE 与恢复扫描），而同一条命令被消费两次意味着追加要求在提示里
        出现两遍。
        """

        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    WITH claimable AS (
                        SELECT command_id
                        FROM trip_run_commands
                        WHERE run_id = :run_id
                          AND status = :pending
                        ORDER BY created_at ASC, command_id ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT :limit
                    )
                    UPDATE trip_run_commands c
                    SET status = :claimed,
                        claimed_by = :claimed_by,
                        claimed_at = NOW(),
                        updated_at = NOW()
                    FROM claimable
                    WHERE c.command_id = claimable.command_id
                    RETURNING c.*
                    """
                ),
                {
                    "run_id": run_id,
                    "pending": RunCommandStatus.PENDING.value,
                    "claimed": RunCommandStatus.CLAIMED.value,
                    "claimed_by": EXECUTOR_ID,
                    "limit": max(1, min(limit, 200)),
                },
            )
            commands = [_command_from_row(dict(row)) for row in result.mappings().all()]
            commands.sort(key=lambda command: (command.created_at, command.command_id))
            return commands

    async def settle(
        self,
        command_ids: Sequence[str],
        *,
        status: RunCommandStatus,
        result: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> int:
        """给命令一个结论。已经有结论的行不再改动。

        「只改还没收口的行」就是重复消费的防线：执行器在标记之后崩溃，重启后再标一次
        什么也不会发生，而结论本身不会被后来的那一次覆盖成另一个答案。
        """

        ids = [str(value) for value in command_ids if str(value).strip()]
        if not ids:
            return 0
        statement = text(
            """
            UPDATE trip_run_commands
            SET status = :status,
                -- 只有真的被执行过才有 consumed_at。rejected 的时间在 updated_at ——
                -- 给一条被拒绝的命令盖上「消费于」会让回执读起来像是它生效过。
                consumed_at = CASE
                    WHEN :status = 'consumed' THEN NOW() ELSE consumed_at
                END,
                result = CAST(:result AS jsonb),
                error_code = :error_code,
                updated_at = NOW()
            WHERE command_id IN :command_ids
              AND status IN :open_statuses
            RETURNING command_id
            """
        ).bindparams(
            bindparam("command_ids", expanding=True),
            bindparam("open_statuses", expanding=True),
        )
        async with get_db_session() as session:
            settled = await session.execute(
                statement,
                {
                    "command_ids": ids,
                    "open_statuses": list(_OPEN_STATUS_VALUES),
                    "status": coerce_run_command_status(status).value,
                    "result": _dumps(result) if result is not None else None,
                    "error_code": error_code,
                },
            )
            return len(settled.mappings().all())

    async def settle_open_for_run(
        self,
        run_id: str,
        *,
        status: RunCommandStatus,
        command_types: Optional[Iterable[str | RunCommandType]] = None,
        result: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> List[RunCommand]:
        """给这个 run 所有未收口的命令一个结论，返回被改动的行。

        Run 已经终结时必须走一次：一条留在 pending 的追加要求永远不会有人消费，而回执
        接口会一直答「还在等」。
        """

        kinds = (
            [coerce_run_command_type(value).value for value in command_types]
            if command_types is not None
            else [kind.value for kind in RunCommandType]
        )
        statement = text(
            """
            UPDATE trip_run_commands
            SET status = :status,
                -- 只有真的被执行过才有 consumed_at。rejected 的时间在 updated_at ——
                -- 给一条被拒绝的命令盖上「消费于」会让回执读起来像是它生效过。
                consumed_at = CASE
                    WHEN :status = 'consumed' THEN NOW() ELSE consumed_at
                END,
                result = CAST(:result AS jsonb),
                error_code = :error_code,
                updated_at = NOW()
            WHERE run_id = :run_id
              AND status IN :open_statuses
              AND command_type IN :command_types
            RETURNING *
            """
        ).bindparams(
            bindparam("open_statuses", expanding=True),
            bindparam("command_types", expanding=True),
        )
        async with get_db_session() as session:
            settled = await session.execute(
                statement,
                {
                    "run_id": run_id,
                    "open_statuses": list(_OPEN_STATUS_VALUES),
                    "command_types": kinds,
                    "status": coerce_run_command_status(status).value,
                    "result": _dumps(result) if result is not None else None,
                    "error_code": error_code,
                },
            )
            return [_command_from_row(dict(row)) for row in settled.mappings().all()]

    async def list_open(self, run_id: str) -> List[RunCommand]:
        statement = text(
            """
            SELECT * FROM trip_run_commands
            WHERE run_id = :run_id AND status IN :open_statuses
            ORDER BY created_at ASC, command_id ASC
            """
        ).bindparams(bindparam("open_statuses", expanding=True))
        async with get_db_session() as session:
            result = await session.execute(
                statement,
                {"run_id": run_id, "open_statuses": list(_OPEN_STATUS_VALUES)},
            )
            return [_command_from_row(dict(row)) for row in result.mappings().all()]

    async def count_open_by_type(self) -> Dict[str, int]:
        """诊断面：全库还有多少条没有结论的命令，按类型分。"""

        statement = text(
            """
            SELECT command_type, count(*) AS open_count
            FROM trip_run_commands
            WHERE status IN :open_statuses
            GROUP BY command_type
            """
        ).bindparams(bindparam("open_statuses", expanding=True))
        async with get_db_session() as session:
            result = await session.execute(
                statement, {"open_statuses": list(_OPEN_STATUS_VALUES)}
            )
            counts = {kind.value: 0 for kind in RunCommandType}
            for row in result.mappings().all():
                counts[str(row["command_type"])] = int(row["open_count"] or 0)
            return counts


_store: Optional[RunCommandStore] = None


def get_run_command_store() -> RunCommandStore:
    global _store
    if _store is None:
        _store = RunCommandStore()
    return _store
