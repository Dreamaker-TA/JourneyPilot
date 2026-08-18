"""
会话历史存储与回放服务 (Infrastructure Layer)
基于 PostgreSQL 两张表：chat_sessions + chat_session_events。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text

from ..entities.session_title import TITLE_MAX_LEN, derive_session_title
from ..infrastructure.database import get_db_session

logger = logging.getLogger(__name__)

# 与 api.schemas.SessionStatus 枚举的 .value 保持对齐;
# 修改时两处同步 (本模块避免反向 import api 层)。
STATUS_ACTIVE = "active"
STATUS_INTERRUPTED = "interrupted"

_PREVIEW_MAX_LEN = 120   # 会话预览截断长度
# 一页最多几个 turn，以及这一页的事件总量上限。两道边界都要有：一个 turn 里可以塞
# 几百个思考步，只限 turn 数的响应仍然可以大到没人能解析。
TURN_PAGE_LIMIT = 30
TURN_PAGE_MAX_EVENTS = 1500
# 标题上限只有一个，判据与派生都在 `entities/session_title.py`；
# 前端重命名输入框的 maxLength 与它对齐（`ConversationList`）。
_TITLE_MAX_LEN = TITLE_MAX_LEN


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_jsonable(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


def _order_to_cursor(event_order: int) -> str:
    """游标 = 这个 turn 第一条事件的 `event_order`。

    不用 offset：新消息插进来会让 offset 分页错位，而 `event_order` 一旦写下就不再变。
    """

    return f"o{int(event_order)}"


def _cursor_to_order(cursor: Optional[str]) -> Optional[int]:
    if not cursor:
        return None
    raw = str(cursor)[1:] if str(cursor).startswith("o") else str(cursor)
    try:
        return int(raw)
    except ValueError:
        return None


def _truncate_preview(text: str, max_len: int = _PREVIEW_MAX_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _normalize_choice_option(value: Any, index: int) -> Dict[str, Any]:
    row = value if isinstance(value, dict) else {}
    label = str(row.get("label") or "").strip()
    option_id = str(row.get("id") or "").strip()
    if not label or not option_id:
        return {}
    option: Dict[str, Any] = {"id": option_id, "label": label}
    description = row.get("description")
    if isinstance(description, str) and description.strip():
        option["description"] = description.strip()
    return option


def _normalize_choice_question(value: Any, index: int) -> Dict[str, Any]:
    row = value if isinstance(value, dict) else {}
    selection_type = row.get("selection_type") or "single"
    if selection_type not in {"single", "multiple"}:
        selection_type = "single"
    question_id = str(row.get("id") or "").strip()
    question = str(row.get("question") or "").strip()
    options = [
        _normalize_choice_option(option, opt_idx)
        for opt_idx, option in enumerate(row.get("options") or [])
    ]
    options = [option for option in options if option]
    memory_informed = row.get("memory_informed")
    memory_basis = row.get("memory_basis")
    normalized: Dict[str, Any] = {
        "id": question_id,
        "question": question,
        "options": options,
        "selection_type": selection_type,
        "memory_informed": memory_informed is True,
        "memory_basis": [
            str(item).strip()[:40]
            for item in (memory_basis if isinstance(memory_basis, list) else [])[:4]
            if str(item).strip()
        ],
    }
    raw_input = row.get("input")
    if isinstance(raw_input, dict) and raw_input.get("kind") == "origin":
        no_value = _normalize_choice_option(raw_input.get("no_value_option") or {}, 0)
        normalized["input"] = {
            "kind": "origin",
            "placeholder": str(
                raw_input.get("placeholder") or "城市、机场或车站，例如：上海、浦东机场"
            ).strip()[:80],
            "no_value_option": {
                "id": "not_decided",
                "label": "暂时没想好出发地",
                "description": str(
                    no_value.get("description") or "先按目的地内行程调研；交通方案稍后按出发地替换。"
                ).strip()[:120],
            },
        }
        normalized["options"] = [normalized["input"]["no_value_option"]]
        normalized["selection_type"] = "single"
    return normalized


def _normalize_context_compaction_event(value: Any) -> Optional[Dict[str, Any]]:
    """Validate the immutable compression snapshot stored in the event log."""
    if not isinstance(value, dict):
        return None

    event_id = str(value.get("event_id") or "").strip()
    source = str(value.get("source") or "").strip()
    occurred_at = str(value.get("occurred_at") or "").strip()
    if not event_id or source not in {"manual", "automatic"} or not occurred_at:
        return None

    def non_negative_int(key: str) -> int:
        try:
            return max(0, int(value.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    constraints = value.get("key_constraints")
    return {
        "event_id": event_id,
        "source": source,
        "occurred_at": occurred_at,
        "messages_compressed": non_negative_int("messages_compressed"),
        "tokens_before": non_negative_int("tokens_before"),
        "tokens_after": non_negative_int("tokens_after"),
        "summary": str(value.get("summary") or ""),
        "key_constraints": [
            str(item).strip()
            for item in (constraints if isinstance(constraints, list) else [])
            if str(item).strip()
        ],
    }


def _project_events_to_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把一段会话事件投影成前端读的消息列表。

    **两遍**：思考步在事件顺序上排在它所属的助手消息**之前**（一轮的写入顺序就是
    message.user → thinking.step* → message.assistant），一遍扫的话它们到达时还没有
    可挂靠的消息，整条思维链会在回看时消失。
    """

    messages: List[Dict[str, Any]] = []
    message_index: Dict[str, int] = {}
    context_report_meta: Dict[str, Dict[str, Any]] = {}
    thinking_rows: List[Dict[str, Any]] = []

    for row in rows:
        event_type = row["event_type"]
        payload = _to_jsonable(row.get("payload"), {})

        if event_type == "context_report":
            mid = payload.get("message_id", "")
            if mid:
                report = {k: v for k, v in payload.items() if k != "message_id"}
                context_report_meta[mid] = report
            continue

        if event_type == "context_compaction":
            compaction_event = _normalize_context_compaction_event(payload)
            if compaction_event:
                messages.append(
                    {
                        "id": compaction_event["event_id"],
                        "role": "system",
                        "content": "",
                        "display_content": "",
                        "timestamp": compaction_event["occurred_at"],
                        "type": "context_compaction",
                        "context_compaction": compaction_event,
                        "thinking_steps": [],
                    }
                )
            continue

        if event_type in ("message.user", "message.assistant"):
            message_id = payload.get("message_id") or ""
            msg = {
                "id": message_id,
                "role": payload.get("role") or ("user" if event_type == "message.user" else "assistant"),
                "content": payload.get("content", ""),
                "display_content": payload.get("display_content", payload.get("content", "")),
                "timestamp": payload.get("timestamp") or _utc_now_iso(),
                "type": payload.get("type", "normal"),
                "task_type": payload.get("task_type"),
                "agent_name": payload.get("agent_name"),
                "step_name": payload.get("step_name"),
                "mode": payload.get("mode"),
                "run_id": payload.get("run_id", ""),
                "citations": payload.get("citations") if isinstance(payload.get("citations"), list) else [],
                "annotations": payload.get("annotations") if isinstance(payload.get("annotations"), list) else [],
                "trip_summary_card": payload.get("trip_summary_card") if isinstance(payload.get("trip_summary_card"), dict) else None,
                "thinking_steps": [],
            }
            if event_type == "message.assistant" and message_id in context_report_meta:
                msg["context_report"] = context_report_meta[message_id]
            message_index[message_id] = len(messages)
            messages.append(msg)
            continue

        if event_type == "thinking.step":
            thinking_rows.append(payload)
            continue

        # 历史会话里可能仍有 clarify.requested / clarify.resolved 事件行：产品面
        # 已删除，没有任何消费者。这里不投影它们——把 type="clarify" 或 choice_data
        # 塞进 messages（无类型 dict，能穿过响应模型）会让前端收到一个类型联合里
        # 已经不存在的形状。两条消息本体照常渲染，只是不再带澄清卡的元数据。
        if event_type in ("clarify.requested", "clarify.resolved"):
            continue

    for payload in thinking_rows:
        idx = message_index.get(payload.get("message_id", ""))
        if idx is None:
            continue
        step_record: Dict[str, Any] = {
            "id": payload.get("step_id", ""),
            "agent_name": payload.get("agent_name", ""),
            "content": payload.get("content", ""),
            "step_name": payload.get("step_name", ""),
            "timestamp": payload.get("timestamp") or _utc_now_iso(),
            "end_time": payload.get("end_time"),
        }
        # 工具调用字段（仅当存在时才写入，避免历史数据污染）
        if payload.get("is_tool_call"):
            step_record["is_tool_call"] = True
            step_record["tool_name"] = payload.get("tool_name", "")
            step_record["tool_call_id"] = payload.get("tool_call_id", "")
            step_record["tool_status"] = payload.get("tool_status", "completed")
            step_record["tool_args"] = payload.get("tool_args", "")
            step_record["tool_result"] = payload.get("tool_result", "")
            step_record["tool_category"] = payload.get("tool_category", "other")
            step_record["from_cache"] = payload.get("from_cache", False)
            step_record["duration_ms"] = payload.get("duration_ms")
        messages[idx].setdefault("thinking_steps", []).append(step_record)

    return messages


class ChatSessionMemory:
    """会话历史核心服务：写事件、读投影、会话回放。"""

    async def ensure_session(
        self,
        session_id: str,
        user_id: str,
        mode: str,
        title_seed: str,
        controlled_trip_identity: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """确保会话存在，不存在则创建；存在时校验 user_id 所有权。

        `controlled_trip_identity` 是**本次请求携带的**旅行身份，只在创建那一次被读到
        （标题此后只由重命名改写）。第一次请求必然没有 `run_id`，所以那一刻请求身份
        与运行身份逐字相同——传谁都一样，因此这里只认一个来源。判据在
        `entities/session_title.derive_session_title`：有目的地就按路线 + 日期命名，
        没有就按问题本身命名。**必传**，没有旅行身份就显式传 None。
        """
        title = derive_session_title(
            user_message=title_seed,
            controlled_trip_identity=controlled_trip_identity,
        )
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT * FROM chat_sessions WHERE session_id = :sid"),
                {"sid": session_id},
            )
            row = result.mappings().first()
            if row:
                row_dict = dict(row)
                if row_dict["user_id"] != user_id:
                    raise PermissionError("会话不属于当前用户")
                return self._row_to_summary(row_dict)

            await session.execute(
                text("""
                    INSERT INTO chat_sessions
                    (session_id, user_id, title, status, mode, last_message_preview,
                     pending_clarify, message_count, created_at, updated_at)
                    VALUES
                    (:sid, :uid, :title, :status, :mode, :preview,
                     NULL, 0, NOW(), NOW())
                """),
                {
                    "sid": session_id,
                    "uid": user_id,
                    "title": title,
                    "status": STATUS_ACTIVE,
                    "mode": mode,
                    "preview": _truncate_preview(title_seed),
                },
            )

            created = {
                "session_id": session_id,
                "user_id": user_id,
                "title": title,
                "status": STATUS_ACTIVE,
                "mode": mode,
                "last_message_preview": _truncate_preview(title_seed),
                "pending_clarify": None,
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
            }
            return self._row_to_summary(created)

    async def list_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT *
                    FROM chat_sessions
                    WHERE user_id = :uid
                    ORDER BY updated_at DESC
                """),
                {"uid": user_id},
            )
            rows = result.mappings().all()
            return [self._row_to_summary(dict(r)) for r in rows]

    async def get_session_summary(
        self, user_id: str, session_id: str
    ) -> Optional[Dict[str, Any]]:
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT * FROM chat_sessions WHERE session_id = :sid"),
                {"sid": session_id},
            )
            row = result.mappings().first()
            if not row:
                return None
            row_dict = dict(row)
            if row_dict["user_id"] != user_id:
                raise PermissionError("会话不属于当前用户")
            return self._row_to_summary(row_dict)

    async def get_recent_messages_within_token_budget(
        self,
        user_id: str,
        session_id: str,
        *,
        token_budget: int,
        model: str = "gpt-4o",
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        """取**压缩点之后**、且不超过 token 预算的会话历史，时间正序返回。

        两道边界，各自守一件事，谁都不许兼职：

        1. **压缩点**（``compaction_boundary_event_order``）：被当前 Anchor 摘要覆盖
           到的最后一条会话事件。摘要已经把那一段说过一遍了，再逐条取回来就是同一段
           历史在 prompt 里印两遍 —— 而摘要是**净加**在 system prompt 上的（预算表里
           那 2,000）。这条边界不存在的时候，压缩是**净亏**的：实测压缩后那次调用
           input 102,121 token，比压缩前的 101,405 还多。
        2. **token 预算**：而不是写死条数。自动压缩的触发判据是「组装后总估算 ≥
           60,000 token」，调用方过去写死取 20 条 —— 实测 30 轮真实会话下 20 条 =
           4,247 token，只有阈值的 7%，压缩层在结构上永远等不到自己的触发条件。
           写死的条数是加在预算层之外的第二道上限，而「这一层能装多少」本来就是
           预算层的职责。

        分页 50 条一取（``ORDER BY event_order DESC``，即由新到旧），碰到压缩点或者
        累计估算越过预算就停 —— 两者都在「由新到旧」这个方向上是提前终止，不会白翻页。
        """

        summary = await self.get_session_summary(user_id, session_id)
        if not summary:
            return []

        boundary = await self.get_compaction_boundary(user_id, session_id)

        from .context_builder import count_tokens

        collected: List[Dict[str, Any]] = []
        accumulated_tokens = 0
        offset = 0
        reached_boundary = False

        while accumulated_tokens < token_budget and not reached_boundary:
            async with get_db_session() as session:
                result = await session.execute(
                    text("""
                        SELECT event_order, event_type, payload
                        FROM chat_session_events
                        WHERE session_id = :sid
                          AND event_type IN ('message.user', 'message.assistant')
                        ORDER BY event_order DESC
                        LIMIT :limit OFFSET :offset
                    """),
                    {"sid": session_id, "limit": page_size, "offset": offset},
                )
                rows = result.mappings().all()

            if not rows:
                break

            for row in rows:
                if int(row["event_order"]) <= boundary:
                    # 这一条及更早的已经折叠进 Anchor 摘要，不再逐字取回。
                    reached_boundary = True
                    break
                payload = _to_jsonable(row.get("payload"), {})
                content = str(payload.get("content") or "")
                if not content:
                    continue
                role = "user" if row["event_type"] == "message.user" else "assistant"
                collected.append({"role": role, "content": content})
                accumulated_tokens += count_tokens(content, model)
                if accumulated_tokens >= token_budget:
                    break

            if len(rows) < page_size:
                break
            offset += page_size

        collected.reverse()
        return collected

    async def get_message_content(
        self, *, session_id: str, message_id: str
    ) -> Optional[str]:
        """按消息 id 读回正文。会话或消息不在了返回 None。

        后台抽取任务只带引用，正文永远从这里读 —— payload 里复制一份原文就会有两份
        会各自漂移的真相。
        """
        if not session_id or not message_id:
            return None
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT payload ->> 'content' AS content
                    FROM chat_session_events
                    WHERE session_id = :sid
                      AND event_type IN ('message.user', 'message.assistant')
                      AND payload ->> 'message_id' = :mid
                    -- 倒序：要读的永远是刚写下的那条，正序会从会话第一条开始逐行
                    -- 解 JSONB 走到末尾。message_id 唯一，两个方向取到的是同一行。
                    ORDER BY event_order DESC
                    LIMIT 1
                """),
                {"sid": session_id, "mid": message_id},
            )
            row = result.mappings().first()
        return str(row["content"]) if row and row["content"] is not None else None

    async def save_turn(
        self,
        *,
        session_id: str,
        user_id: str,
        mode: str,
        user_message: str,
        user_message_id: str,
        assistant_message_id: str,
        assistant_content: str,
        assistant_display_content: str,
        assistant_type: str = "normal",
        task_type: Optional[str] = None,
        agent_name: str = "",
        step_name: str = "",
        thinking_steps: Optional[List[Dict[str, Any]]] = None,
        context_report: Optional[Dict[str, Any]] = None,
        citations: Optional[List[Dict[str, Any]]] = None,
        annotations: Optional[List[Dict[str, Any]]] = None,
        trip_summary_card: Optional[Dict[str, Any]] = None,
        run_id: str = "",
        controlled_trip_identity: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        保存一轮对话。
        事件顺序：message.user -> thinking.step* -> context_report? -> message.assistant，
        全部共用一个 turn_id。上下文整理的快照不在这里写 —— 它由 `CompactionService`
        与摘要、新边界同事务落库，自己成一个 turn。

        `controlled_trip_identity` 只为 `ensure_session` 而来：这条路径也会创建会话
        （安全拦截那条路不走 `load_session_history`），少传一层就会让同一列出现两种
        命名口径。**必传**。
        """
        await self.ensure_session(
            session_id=session_id,
            user_id=user_id,
            mode=mode,
            title_seed=user_message,
            controlled_trip_identity=controlled_trip_identity,
        )

        thinking_steps = thinking_steps or []
        # 一轮对话一个 turn id：用户消息、压缩快照、思考步、助手消息共用它，
        # 分页因此不可能把一轮切成两半。
        turn_id = f"turn_{uuid.uuid4().hex[:16]}"

        events: List[Dict[str, Any]] = []

        events.append(
            {
                "event_type": "message.user",
                "payload": {
                    "message_id": user_message_id,
                    "role": "user",
                    "content": user_message,
                    "display_content": user_message,
                    "timestamp": _utc_now_iso(),
                    "type": "normal",
                    "run_id": run_id,
                },
            }
        )

        for step in thinking_steps:
            events.append(
                {
                    "event_type": "thinking.step",
                    "payload": {
                        "message_id": assistant_message_id,
                        "step_id": step.get("step_id", ""),
                        "agent_name": step.get("agent_name", ""),
                        "step_name": step.get("step_name", ""),
                        "content": step.get("content", ""),
                        "timestamp": step.get("timestamp") or _utc_now_iso(),
                        "end_time": step.get("end_time") or step.get("timestamp") or _utc_now_iso(),
                        # 工具调用字段
                        "is_tool_call": step.get("is_tool_call", False),
                        "tool_name": step.get("tool_name", ""),
                        "tool_call_id": step.get("tool_call_id", ""),
                        "tool_status": step.get("tool_status", ""),
                        "tool_args": step.get("tool_args", ""),
                        "tool_result": step.get("tool_result", ""),
                        "tool_category": step.get("tool_category", ""),
                        "from_cache": step.get("from_cache", False),
                        "duration_ms": step.get("duration_ms"),
                    },
                }
            )

        if context_report:
            events.append(
                {
                    "event_type": "context_report",
                    "payload": {
                        "message_id": assistant_message_id,
                        "run_id": run_id,
                        **context_report,
                    },
                }
            )

        events.append(
            {
                "event_type": "message.assistant",
                "payload": {
                    "message_id": assistant_message_id,
                    "role": "assistant",
                    "content": assistant_content,
                    "display_content": assistant_display_content,
                    "timestamp": _utc_now_iso(),
                    "type": assistant_type,
                    "task_type": task_type,
                    "agent_name": agent_name,
                    "step_name": step_name,
                    "mode": mode,
                    "run_id": run_id,
                    "citations": citations or [],
                    "annotations": annotations or [],
                    "trip_summary_card": trip_summary_card or None,
                },
            }
        )

        async with get_db_session() as session:
            row_result = await session.execute(
                text("SELECT * FROM chat_sessions WHERE session_id = :sid"),
                {"sid": session_id},
            )
            row = row_result.mappings().first()
            if not row:
                raise RuntimeError(f"会话不存在: {session_id}")
            row_dict = dict(row)
            if row_dict["user_id"] != user_id:
                raise PermissionError("会话不属于当前用户")

            max_order_result = await session.execute(
                text("""
                    SELECT COALESCE(MAX(event_order), 0) AS max_order
                    FROM chat_session_events
                    WHERE session_id = :sid
                """),
                {"sid": session_id},
            )
            max_order = int(max_order_result.scalar_one() or 0)

            for idx, event in enumerate(events, start=1):
                await session.execute(
                    text("""
                        INSERT INTO chat_session_events
                        (session_id, event_order, turn_id, event_type, payload, created_at)
                        VALUES
                        (:sid, :order, :turn_id, :etype, CAST(:payload AS jsonb), NOW())
                    """),
                    {
                        "sid": session_id,
                        "order": max_order + idx,
                        "turn_id": turn_id,
                        "etype": event["event_type"],
                        "payload": json.dumps(event["payload"], ensure_ascii=False),
                    },
                )

            if assistant_type == "interrupted":
                status = STATUS_INTERRUPTED
            else:
                status = STATUS_ACTIVE

            preview = (
                assistant_display_content
                or assistant_content
                or user_message
            )
            preview = _truncate_preview(preview or "")

            message_increment = sum(
                1 for event in events if event["event_type"] in ("message.user", "message.assistant")
            )

            await session.execute(
                text("""
                    UPDATE chat_sessions
                    SET status = :status,
                        mode = :mode,
                        last_message_preview = :preview,
                        message_count = message_count + :inc,
                        updated_at = NOW()
                    WHERE session_id = :sid
                """),
                {
                    "sid": session_id,
                    "status": status,
                    "mode": mode,
                    "preview": preview,
                    "inc": message_increment,
                },
            )

        summary = await self.get_session_summary(user_id, session_id)
        if not summary:
            raise RuntimeError("保存后会话不可读取")
        return summary

    async def get_session_detail(
        self, user_id: str, session_id: str
    ) -> Optional[Dict[str, Any]]:
        """会话详情 = **最新一页 turn**，不是全量历史。

        一个用了半年的会话有上万条事件，把它们一次投影出来既慢又没人读得完。
        更早的历史由 `list_turns` 按游标往回取。
        """
        summary = await self.get_session_summary(user_id, session_id)
        if not summary:
            return None
        page = await self.list_turns(user_id, session_id, summary=summary)
        return {
            **summary,
            "messages": [message for turn in page["turns"] for message in turn["messages"]],
            "next_before": page["next_before"],
            "has_more": page["has_more"],
        }

    async def list_turns(
        self,
        user_id: str,
        session_id: str,
        *,
        before_turn: Optional[str] = None,
        limit: int = TURN_PAGE_LIMIT,
        summary: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """按 turn 游标往回翻页，时间正序返回这一页。

        **一个 turn 永远整份返回**。游标是这个 turn 第一条事件的 `event_order`：
        它不随新消息插入而漂移，offset 会。

        ``summary`` 由已经查过它的调用方传进来，省一次重复查询。
        """

        if summary is None:
            summary = await self.get_session_summary(user_id, session_id)
        if not summary:
            return {"turns": [], "next_before": None, "has_more": False}

        page_size = max(1, min(int(limit), TURN_PAGE_LIMIT))
        cursor = _cursor_to_order(before_turn)
        # 先按 (session_id, event_order) 倒序取**有界的一窗**再分组，而不是对整个会话
        # GROUP BY turn_id：后者的代价随会话长度线性增长，于是「按 turn 分页」在一个
        # 上万条事件的会话里和分页之前一样慢。窗口装得下一页的事件预算 + 一个 turn。
        scan_limit = TURN_PAGE_MAX_EVENTS + 1
        async with get_db_session() as session:
            turn_rows = await session.execute(
                text("""
                    WITH windowed AS (
                        SELECT turn_id, event_order
                        FROM chat_session_events
                        WHERE session_id = :sid
                          AND (CAST(:cursor AS INTEGER) IS NULL
                               OR event_order < CAST(:cursor AS INTEGER))
                        ORDER BY event_order DESC
                        LIMIT :scan_limit
                    )
                    SELECT turn_id,
                           min(event_order) AS first_order,
                           count(*)         AS event_count,
                           count(*) OVER ()  AS window_turns
                    FROM windowed
                    GROUP BY turn_id
                    ORDER BY first_order DESC
                    LIMIT :limit
                """),
                {
                    "sid": session_id,
                    "cursor": cursor,
                    "limit": page_size + 1,
                    "scan_limit": scan_limit,
                },
            )
            candidates = [dict(row) for row in turn_rows.mappings().all()]
            # 窗口被填满时，最老的那个 turn 可能只截到一半，它的 event_count 偏小。
            # 丢掉它：窗口外一定还有更多 turn，has_more 本来就成立。
            window_saturated = sum(int(row["event_count"]) for row in candidates) >= scan_limit
            if window_saturated and len(candidates) > 1:
                candidates = candidates[:-1]

            # 页面还有第二道上限：事件总数。一个 turn 里塞了几百个思考步时，
            # 「30 个 turn」可以是一份没人能解析的响应。
            selected: List[Dict[str, Any]] = []
            events_budget = 0
            for candidate in candidates[:page_size]:
                if selected and events_budget + int(candidate["event_count"]) > TURN_PAGE_MAX_EVENTS:
                    break
                selected.append(candidate)
                events_budget += int(candidate["event_count"])
            # 窗口被填满就说明窗口之外还有事件 —— 哪怕这一页把窗口里的 turn 都取了。
            has_more = len(candidates) > len(selected) or window_saturated

            if not selected:
                return {"turns": [], "next_before": None, "has_more": False}

            statement = text("""
                SELECT turn_id, event_order, event_type, payload
                FROM chat_session_events
                WHERE session_id = :sid AND turn_id IN :turn_ids
                ORDER BY event_order ASC
            """).bindparams(bindparam("turn_ids", expanding=True))
            event_rows = await session.execute(
                statement,
                {"sid": session_id, "turn_ids": [str(row["turn_id"]) for row in selected]},
            )
            rows = [dict(row) for row in event_rows.mappings().all()]

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["turn_id"]), []).append(row)

        turns = []
        for candidate in sorted(selected, key=lambda item: int(item["first_order"])):
            turn_id = str(candidate["turn_id"])
            turns.append({
                "turn_id": turn_id,
                "cursor": _order_to_cursor(int(candidate["first_order"])),
                "messages": _project_events_to_messages(grouped.get(turn_id, [])),
            })
        return {
            "turns": turns,
            "next_before": turns[0]["cursor"] if turns and has_more else None,
            "has_more": has_more,
        }


    # -----------------------------------------------------------------------
    # 上下文压缩 (v3)
    # -----------------------------------------------------------------------

    async def get_anchor(
        self, user_id: str, session_id: str
    ) -> tuple:
        """
        读取当前会话的 Anchor Summary 和压缩次数。

        Returns:
            (anchor_data: dict | None, compression_count: int)
        """
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT anchor_summary, compression_count
                    FROM chat_sessions
                    WHERE session_id = :sid AND user_id = :uid
                """),
                {"sid": session_id, "uid": user_id},
            )
            row = result.mappings().first()
        if not row:
            return None, 0
        anchor_raw = row["anchor_summary"]
        compression_count = row["compression_count"] or 0
        if anchor_raw is None:
            return None, compression_count
        anchor_data = _to_jsonable(anchor_raw, None)
        return anchor_data, compression_count

    async def get_compaction_boundary(self, user_id: str, session_id: str) -> int:
        """当前 Anchor 摘要覆盖到的最后一条会话事件的 ``event_order``（没压缩过则 0）。

        **这个数的负责层就是这里，全仓只写这一次。** 它由 :meth:`commit_compaction` 与
        摘要在同一个事务里落库，读它的是 :meth:`get_recent_messages_within_token_budget`
        与 :meth:`read_messages_for_compaction`。
        把它算在调用方（比如让每个节点自己数「摘要压了多少条」）就是这个仓最熟的那个
        形状：同一件事定义在多处，其中一份静默胜出。
        """
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT compaction_boundary_event_order
                    FROM chat_sessions
                    WHERE session_id = :sid AND user_id = :uid
                """),
                {"sid": session_id, "uid": user_id},
            )
            row = result.mappings().first()
        if not row:
            return 0
        return int(row["compaction_boundary_event_order"] or 0)

    async def read_messages_for_compaction(
        self,
        user_id: str,
        session_id: str,
        *,
        token_budget: int,
        max_messages: int,
        model: str = "gpt-4o",
    ) -> tuple[List[Dict[str, Any]], int, int]:
        """取**压缩点之后**、预算之内的消息，返回 `(消息, 起始边界, 实际读到哪一条)`。

        往前读、读到预算为止 —— 压缩不再一次加载全部历史。第三个返回值是**真正进了
        摘要的最后一条**的 `event_order`：把边界推到全部历史的末尾会永久丢掉没进摘要
        的那一段。
        """

        from .context_builder import count_tokens

        boundary = await self.get_compaction_boundary(user_id, session_id)
        collected: List[Dict[str, Any]] = []
        last_included = boundary
        accumulated = 0
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT event_order, event_type, payload
                    FROM chat_session_events
                    WHERE session_id = :sid
                      AND event_order > :boundary
                      AND event_type IN ('message.user', 'message.assistant')
                    ORDER BY event_order ASC
                    LIMIT :limit
                """),
                {"sid": session_id, "boundary": boundary, "limit": max(1, max_messages)},
            )
            rows = result.mappings().all()

        for row in rows:
            payload = _to_jsonable(row.get("payload"), {})
            content = str(payload.get("content") or "")
            if not content:
                continue
            tokens = count_tokens(content, model)
            if collected and accumulated + tokens > token_budget:
                break
            role = "user" if row["event_type"] == "message.user" else "assistant"
            collected.append({"role": role, "content": content})
            accumulated += tokens
            last_included = int(row["event_order"])
        return collected, boundary, last_included

    async def commit_compaction(
        self,
        user_id: str,
        session_id: str,
        *,
        anchor_data: dict,
        expected_boundary: int,
        new_boundary: int,
        event: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """**一个事务**写下摘要、新边界和时间线上的压缩快照。

        三者拆开写就会有一个中间态：只写摘要等于给 prompt 净加 2,000 token 而一条旧
        消息都不减，只写边界等于把没进摘要的历史丢掉。

        `expected_boundary` 是 CAS：边界在读取与提交之间被另一次压缩推走了，这一次就
        整个作废（返回 None），旧摘要原样保留。
        """

        normalized = _normalize_context_compaction_event(event)
        if not normalized:
            raise ValueError("Invalid context compaction event")

        async with get_db_session() as session:
            updated = await session.execute(
                text("""
                    UPDATE chat_sessions
                    SET anchor_summary    = CAST(:anchor AS jsonb),
                        compression_count = compression_count + 1,
                        compaction_boundary_event_order = :new_boundary,
                        updated_at        = NOW()
                    WHERE session_id = :sid AND user_id = :uid
                      AND compaction_boundary_event_order = :expected_boundary
                    RETURNING session_id
                """),
                {
                    "sid": session_id,
                    "uid": user_id,
                    "anchor": json.dumps(anchor_data, ensure_ascii=False),
                    "expected_boundary": expected_boundary,
                    "new_boundary": new_boundary,
                },
            )
            if updated.mappings().first() is None:
                return None
            max_order_result = await session.execute(
                text("""
                    SELECT COALESCE(MAX(event_order), 0) AS max_order
                    FROM chat_session_events
                    WHERE session_id = :sid
                """),
                {"sid": session_id},
            )
            await session.execute(
                text("""
                    INSERT INTO chat_session_events
                    (session_id, event_order, turn_id, event_type, payload, created_at)
                    VALUES
                    (:sid, :order, :turn_id, 'context_compaction',
                     CAST(:payload AS jsonb), NOW())
                """),
                {
                    "sid": session_id,
                    "order": int(max_order_result.scalar_one() or 0) + 1,
                    # 整理发生在两轮之间，自己成一个 turn。
                    "turn_id": f"turn_{uuid.uuid4().hex[:16]}",
                    "payload": json.dumps(normalized, ensure_ascii=False),
                },
            )
        return normalized

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        """删除会话，并取消引用它的未完成后台任务。

        留着那些任务只会让 worker 反复领到一条读不到源消息的活。
        """
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT user_id FROM chat_sessions WHERE session_id = :sid"),
                {"sid": session_id},
            )
            row = result.mappings().first()
            if not row:
                return False
            if row["user_id"] != user_id:
                raise PermissionError("会话不属于当前用户")

            await session.execute(
                text("DELETE FROM chat_sessions WHERE session_id = :sid"),
                {"sid": session_id},
            )

        from ..infrastructure.background_job_store import get_background_job_store

        await get_background_job_store().cancel_for_session(session_id)
        return True

    async def update_session_title(
        self, user_id: str, session_id: str, new_title: str
    ) -> Optional[Dict[str, Any]]:
        """重命名会话标题。会话不存在返回 None；非本人会话抛 PermissionError。

        标题按 _TITLE_MAX_LEN 截断，与自动生成标题共用同一上限；updated_at 顺带刷新，
        使重命名后的会话在列表里回到 updated_at 倒序的顶部（与用户心智一致）。
        """
        title = (new_title or "").strip()[:_TITLE_MAX_LEN]
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT user_id FROM chat_sessions WHERE session_id = :sid"),
                {"sid": session_id},
            )
            row = result.mappings().first()
            if not row:
                return None
            if row["user_id"] != user_id:
                raise PermissionError("会话不属于当前用户")

            await session.execute(
                text("""
                    UPDATE chat_sessions
                    SET title = :title, updated_at = NOW()
                    WHERE session_id = :sid
                """),
                {"sid": session_id, "title": title},
            )
        return await self.get_session_summary(user_id, session_id)

    async def clear_user_anchors(
        self,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> int:
        """Clear compressed session anchors without deleting chat history.

        压缩点跟着摘要一起归零：摘要没了，被它覆盖的那一段历史就必须重新逐条取回，
        否则「忘记摘要」会把那一段对话一并从模型的视野里删掉 —— 而界面上只说了
        清空摘要。这两个值由同一个 UPDATE 写，不许分开。
        """
        if not user_id:
            return 0
        clause = "user_id = :uid AND anchor_summary IS NOT NULL"
        params: Dict[str, Any] = {"uid": user_id}
        if session_id:
            clause += " AND session_id = :sid"
            params["sid"] = session_id
        async with get_db_session() as session:
            result = await session.execute(
                text(f"""
                    UPDATE chat_sessions
                    SET anchor_summary = NULL,
                        compression_count = 0,
                        compaction_boundary_event_order = 0,
                        updated_at = NOW()
                    WHERE {clause}
                    RETURNING session_id
                """),
                params,
            )
            return len(result.fetchall())

    def _row_to_summary(self, row: Dict[str, Any]) -> Dict[str, Any]:
        created_at = row.get("created_at")
        updated_at = row.get("updated_at")
        return {
            "session_id": row.get("session_id", ""),
            "title": row.get("title", "新对话"),
            "status": row.get("status", STATUS_ACTIVE),
            "mode": row.get("mode", "fast"),
            "last_message_preview": row.get("last_message_preview", ""),
            "pending_clarify": _to_jsonable(row.get("pending_clarify"), None),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
            "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or ""),
        }
