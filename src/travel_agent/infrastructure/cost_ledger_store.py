"""Cost ledger persistence（台账层 / 暴露层）.

把捕获层缓冲的每条 ``LLMCallRecord`` 落成 Postgres 台账行 ``run_llm_calls``，
写入时按价格表**快照计算** ``cost_usd``（LangSmith 语义：价格表后改不追溯重算已落库行），
再经 SSE（运行中）与 REST（历史）暴露 run 级成本汇总。

设计要点：

- **快照计算**：cost 在 ``record_calls`` 写入时算好并存库；``run_summary`` 只读库里的
  cost 聚合，绝不重算——因此改价格表不影响历史行。
- **未命中价格 → cost_usd=None**：只报 token，不编造成本（``resolve_price`` 返回 None）。
- **读折扣型公式**：``(input-cached)×p_in + cached×p_cached + output×p_out``；reasoning
  已含在 output_tokens 里，不另算（05 号 §2）。
- **聚合走查询期**：表小无需预聚合；``run_summary`` 拉全量行后用纯函数 ``summarize_calls``
  聚合，SQL 与 InMemory 两实现共用同一聚合逻辑，保证形状一致、可单测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from ..config import ModelPricingItem, resolve_price
from ..models.usage import LLMCallRecord
from .database import get_db_session

logger = logging.getLogger(__name__)


class CostLedgerConflict(Exception):
    """Same call id already stored with different content."""


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# 台账行 + 成本公式
# --------------------------------------------------------------------------- #

@dataclass
class LLMCostCall:
    """一条成本台账行（run_llm_calls）：捕获层字段 + 写入时快照 cost_usd。"""

    id: str
    run_id: str
    node: Optional[str]
    agent: Optional[str]
    tier: Optional[str]
    provider: Optional[str]
    model_request: str
    model_response: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cached_input_tokens: Optional[int]
    reasoning_output_tokens: Optional[int]
    cost_usd: Optional[float]
    estimated: bool
    start_ts: Optional[str]
    end_ts: Optional[str]
    ttft_ms: Optional[float]
    latency_ms: Optional[float]
    status: str
    stream: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "node": self.node,
            "agent": self.agent,
            "tier": self.tier,
            "provider": self.provider,
            "model_request": self.model_request,
            "model_response": self.model_response,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "cost_usd": self.cost_usd,
            "estimated": self.estimated,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "ttft_ms": self.ttft_ms,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "stream": self.stream,
        }


def compute_cost_usd(
    price: Optional[ModelPricingItem],
    *,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cached_input_tokens: Optional[int],
) -> Optional[float]:
    """读折扣型成本公式；未命中价格或无 token → None（不编造成本）。"""
    if price is None:
        return None
    if input_tokens is None and output_tokens is None:
        return None  # error 记录 token 留 null → 成本也 null
    inp = max(0, input_tokens or 0)
    out = max(0, output_tokens or 0)
    cached = max(0, cached_input_tokens or 0)
    cached = min(cached, inp)  # 缓存命中不可能超过总输入
    p_in = price.input_per_1m / 1_000_000.0
    p_cached = (
        price.cached_input_per_1m if price.cached_input_per_1m is not None else price.input_per_1m
    ) / 1_000_000.0
    p_out = price.output_per_1m / 1_000_000.0
    cost = (inp - cached) * p_in + cached * p_cached + out * p_out
    return round(cost, 8)


def build_ledger_call(
    record: LLMCallRecord,
    *,
    pricing: Optional[List[ModelPricingItem]] = None,
) -> LLMCostCall:
    """捕获层 ``LLMCallRecord`` → 台账行，写入时快照计算 cost_usd。"""
    price = resolve_price(record.model_request, record.provider, pricing=pricing)
    cost = compute_cost_usd(
        price,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cached_input_tokens=record.cached_input_tokens,
    )
    return LLMCostCall(
        id=record.id,
        run_id=record.run_id,
        node=record.node,
        agent=record.agent,
        tier=record.tier,
        provider=record.provider,
        model_request=record.model_request,
        model_response=record.model_response,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cached_input_tokens=record.cached_input_tokens,
        reasoning_output_tokens=record.reasoning_output_tokens,
        cost_usd=cost,
        estimated=bool(record.estimated),
        start_ts=record.start_ts,
        end_ts=record.end_ts,
        ttft_ms=record.ttft_ms,
        latency_ms=record.latency_ms,
        status=record.status or "ok",
        stream=bool(record.stream),
    )


# --------------------------------------------------------------------------- #
# run 级聚合（纯函数，SQL 与 InMemory 共用）
# --------------------------------------------------------------------------- #

def _round_cost(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 8)


def _group_aggregate(calls: List[LLMCostCall], key_attr: str, label: str) -> List[Dict[str, Any]]:
    """按 node / agent 分组聚合，成本降序返回。"""
    buckets: Dict[str, Dict[str, Any]] = {}
    for call in calls:
        key = getattr(call, key_attr) or "unknown"
        bucket = buckets.setdefault(
            key,
            {
                label: key,
                "call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": None,
                "latency_ms": 0.0,
            },
        )
        bucket["call_count"] += 1
        bucket["input_tokens"] += call.input_tokens or 0
        bucket["output_tokens"] += call.output_tokens or 0
        bucket["total_tokens"] += (call.input_tokens or 0) + (call.output_tokens or 0)
        bucket["latency_ms"] += call.latency_ms or 0.0
        if call.cost_usd is not None:
            bucket["cost_usd"] = (bucket["cost_usd"] or 0.0) + call.cost_usd
    rows = list(buckets.values())
    for row in rows:
        row["cost_usd"] = _round_cost(row["cost_usd"])
        row["latency_ms"] = round(row["latency_ms"], 3)
    rows.sort(key=lambda r: ((r["cost_usd"] or 0.0), r["total_tokens"]), reverse=True)
    return rows


def summarize_calls(run_id: str, calls: List[LLMCostCall]) -> Dict[str, Any]:
    """聚合出 run 级成本摘要：总量、按 agent 分解、瓶颈节点 top3、estimated 占比。"""
    total_input = total_output = total_cached = total_reasoning = 0
    total_cost = 0.0
    priced = estimated = errors = 0
    total_latency = 0.0
    starts: List[datetime] = []
    ends: List[datetime] = []

    for call in calls:
        total_input += call.input_tokens or 0
        total_output += call.output_tokens or 0
        total_cached += call.cached_input_tokens or 0
        total_reasoning += call.reasoning_output_tokens or 0
        total_latency += call.latency_ms or 0.0
        if call.cost_usd is not None:
            total_cost += call.cost_usd
            priced += 1
        if call.estimated:
            estimated += 1
        if call.status == "error":
            errors += 1
        st = _parse_ts(call.start_ts)
        en = _parse_ts(call.end_ts)
        if st is not None:
            starts.append(st)
        if en is not None:
            ends.append(en)

    call_count = len(calls)
    wall_ms: Optional[float] = None
    if starts and ends:
        span = (max(ends) - min(starts)).total_seconds() * 1000.0
        wall_ms = round(span, 3) if span >= 0 else None

    by_node = _group_aggregate(calls, "node", "node")
    by_agent = _group_aggregate(calls, "agent", "agent")

    bottleneck_by_cost = [
        {
            "node": row["node"],
            "cost_usd": row["cost_usd"],
            "latency_ms": row["latency_ms"],
            "call_count": row["call_count"],
        }
        for row in by_node[:3]
    ]
    bottleneck_by_latency = [
        {
            "node": row["node"],
            "latency_ms": row["latency_ms"],
            "cost_usd": row["cost_usd"],
            "call_count": row["call_count"],
        }
        for row in sorted(by_node, key=lambda r: r["latency_ms"], reverse=True)[:3]
    ]

    return {
        "run_id": run_id,
        "call_count": call_count,
        "priced_call_count": priced,
        "unpriced_call_count": call_count - priced,
        "estimated_call_count": estimated,
        "error_call_count": errors,
        "estimated_ratio": round(estimated / call_count, 4) if call_count else 0.0,
        "cost_coverage_ratio": round(priced / call_count, 4) if call_count else 0.0,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cached_input_tokens": total_cached,
        "total_reasoning_output_tokens": total_reasoning,
        "total_tokens": total_input + total_output,
        # priced_call_count==0 时不编造 0 成本，报 None（只报 token）。
        "total_cost_usd": _round_cost(total_cost) if priced else None,
        "currency": "USD",
        "total_latency_ms": round(total_latency, 3),
        "wall_ms": wall_ms,
        "by_agent": by_agent,
        "by_node": by_node,
        "bottleneck_by_cost": bottleneck_by_cost,
        "bottleneck_by_latency": bottleneck_by_latency,
        # Tool Search 上下文节省量：DB 台账不持有进程内曝光计量，基值 null；由 chat 终结层
        # 用 ToolExposureLedger.summary(run_id) 回填后随 run_cost_summary 下发。
        "tool_context_saving": None,
    }


def cost_event_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    """终态 run.cost_recorded 事件的 audit-safe 摘要（只计数无内容）。"""
    top = summary.get("bottleneck_by_cost") or []
    return {
        "call_count": summary.get("call_count", 0),
        "total_tokens": summary.get("total_tokens", 0),
        "total_cost_usd": summary.get("total_cost_usd"),
        "currency": summary.get("currency", "USD"),
        "estimated_ratio": summary.get("estimated_ratio", 0.0),
        "cost_coverage_ratio": summary.get("cost_coverage_ratio", 0.0),
        "bottleneck_node": top[0]["node"] if top else None,
        "bottleneck_cost_usd": top[0]["cost_usd"] if top else None,
    }


# --------------------------------------------------------------------------- #
# 台账存储：SQL + InMemory 双实现
# --------------------------------------------------------------------------- #

def _call_from_row(row: Dict[str, Any]) -> LLMCostCall:
    return LLMCostCall(
        id=row["id"],
        run_id=row["run_id"],
        node=row.get("node"),
        agent=row.get("agent"),
        tier=row.get("tier"),
        provider=row.get("provider"),
        model_request=row.get("model_request") or "",
        model_response=row.get("model_response"),
        input_tokens=row.get("input_tokens"),
        output_tokens=row.get("output_tokens"),
        cached_input_tokens=row.get("cached_input_tokens"),
        reasoning_output_tokens=row.get("reasoning_output_tokens"),
        cost_usd=row.get("cost_usd"),
        estimated=bool(row.get("estimated")),
        start_ts=_iso(row.get("start_ts")),
        end_ts=_iso(row.get("end_ts")),
        ttft_ms=row.get("ttft_ms"),
        latency_ms=row.get("latency_ms"),
        status=row.get("status") or "ok",
        stream=bool(row.get("stream")),
    )


def _norm_cost(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _ledger_identity(call: LLMCostCall) -> Tuple[Any, ...]:
    """Fields that must match for same-id idempotent replay."""
    return (
        call.run_id,
        call.node,
        call.agent,
        call.provider,
        call.model_request,
        call.model_response,
        call.input_tokens,
        call.output_tokens,
        call.cached_input_tokens,
        call.reasoning_output_tokens,
        _norm_cost(call.cost_usd),
        call.status,
    )


def _assert_ledger_idempotent(existing: LLMCostCall, incoming: LLMCostCall) -> None:
    if _ledger_identity(existing) != _ledger_identity(incoming):
        logger.error(
            "cost ledger id conflict: id=%s existing_run=%s incoming_run=%s",
            incoming.id,
            existing.run_id,
            incoming.run_id,
        )
        raise CostLedgerConflict(
            f"LLM call id {incoming.id!r} already stored with different content"
        )


class CostLedgerStore:
    """PostgreSQL-backed cost ledger repository."""

    async def record_calls(
        self,
        batch: List[LLMCallRecord],
        *,
        pricing: Optional[List[ModelPricingItem]] = None,
    ) -> List[LLMCostCall]:
        """把捕获层 drain 出的记录算好 cost 后批量落库；同 id 内容一致则幂等跳过。"""
        ledger = [build_ledger_call(rec, pricing=pricing) for rec in batch if rec and rec.run_id]
        if not ledger:
            return []
        async with get_db_session() as session:
            for call in ledger:
                await session.execute(
                    text(
                        """
                        INSERT INTO run_llm_calls
                            (id, run_id, node, agent, tier, provider, model_request,
                             model_response, input_tokens, output_tokens, cached_input_tokens,
                             reasoning_output_tokens, cost_usd, estimated, start_ts, end_ts,
                             ttft_ms, latency_ms, status, stream, created_at)
                        VALUES
                            (:id, :run_id, :node, :agent, :tier, :provider, :model_request,
                             :model_response, :input_tokens, :output_tokens, :cached_input_tokens,
                             :reasoning_output_tokens, :cost_usd, :estimated,
                             CAST(:start_ts AS timestamptz), CAST(:end_ts AS timestamptz),
                             :ttft_ms, :latency_ms, :status, :stream, NOW())
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": call.id,
                        "run_id": call.run_id,
                        "node": call.node,
                        "agent": call.agent,
                        "tier": call.tier,
                        "provider": call.provider,
                        "model_request": call.model_request,
                        "model_response": call.model_response,
                        "input_tokens": call.input_tokens,
                        "output_tokens": call.output_tokens,
                        "cached_input_tokens": call.cached_input_tokens,
                        "reasoning_output_tokens": call.reasoning_output_tokens,
                        "cost_usd": call.cost_usd,
                        "estimated": call.estimated,
                        # asyncpg 对 timestamptz 参数只接受 datetime 对象；ISO 字符串
                        # 会在预编译阶段抛 DataError（CAST 也救不回来）。
                        "start_ts": _parse_ts(call.start_ts),
                        "end_ts": _parse_ts(call.end_ts),
                        "ttft_ms": call.ttft_ms,
                        "latency_ms": call.latency_ms,
                        "status": call.status,
                        "stream": call.stream,
                    },
                )
                existing_row = await session.execute(
                    text("SELECT * FROM run_llm_calls WHERE id = :id"),
                    {"id": call.id},
                )
                row = existing_row.mappings().first()
                if row is not None:
                    _assert_ledger_idempotent(_call_from_row(dict(row)), call)
        return ledger

    async def list_calls(
        self,
        run_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[LLMCostCall]:
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT *
                    FROM run_llm_calls
                    WHERE run_id = :run_id
                    ORDER BY start_ts ASC, id ASC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"run_id": run_id, "limit": max(1, min(limit, 500)), "offset": max(0, offset)},
            )
            return [_call_from_row(dict(row)) for row in result.mappings().all()]

    async def run_summary(self, run_id: str) -> Dict[str, Any]:
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT *
                    FROM run_llm_calls
                    WHERE run_id = :run_id
                    ORDER BY start_ts ASC, id ASC
                    """
                ),
                {"run_id": run_id},
            )
            calls = [_call_from_row(dict(row)) for row in result.mappings().all()]
        return summarize_calls(run_id, calls)

    async def count_calls(self, run_id: str) -> int:
        async with get_db_session() as session:
            result = await session.execute(
                text("SELECT COUNT(*) AS count FROM run_llm_calls WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            return int(result.mappings().first()["count"])


class InMemoryCostLedgerStore(CostLedgerStore):
    """In-memory cost ledger with the same async contract."""

    def __init__(self) -> None:
        self.calls: List[LLMCostCall] = []
        self._ids: set[str] = set()

    async def record_calls(
        self,
        batch: List[LLMCallRecord],
        *,
        pricing: Optional[List[ModelPricingItem]] = None,
    ) -> List[LLMCostCall]:
        ledger = [build_ledger_call(rec, pricing=pricing) for rec in batch if rec and rec.run_id]
        inserted: List[LLMCostCall] = []
        for call in ledger:
            if call.id in self._ids:
                existing = next(c for c in self.calls if c.id == call.id)
                _assert_ledger_idempotent(existing, call)
                continue  # 幂等：同 id 同内容不重复落
            self._ids.add(call.id)
            self.calls.append(call)
            inserted.append(call)
        return inserted

    def _run_calls(self, run_id: str) -> List[LLMCostCall]:
        rows = [call for call in self.calls if call.run_id == run_id]
        rows.sort(key=lambda c: (c.start_ts or "", c.id))
        return rows

    async def list_calls(
        self,
        run_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[LLMCostCall]:
        rows = self._run_calls(run_id)
        start = max(0, offset)
        return rows[start : start + max(1, min(limit, 500))]

    async def run_summary(self, run_id: str) -> Dict[str, Any]:
        return summarize_calls(run_id, self._run_calls(run_id))

    async def count_calls(self, run_id: str) -> int:
        return len(self._run_calls(run_id))


_cost_ledger_store_singleton: Optional[CostLedgerStore] = None


def get_cost_ledger_store() -> CostLedgerStore:
    global _cost_ledger_store_singleton
    if _cost_ledger_store_singleton is None:
        _cost_ledger_store_singleton = CostLedgerStore()
    return _cost_ledger_store_singleton
