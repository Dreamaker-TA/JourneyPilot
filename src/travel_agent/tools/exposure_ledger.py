"""Tool-schema exposure metering（Tool Search 上下文节省量化）.

按 run 累计 worker agent 每次组装工具列表时**实际注入**的 schema token 与**全量注入基线**，
据此报出 ``run_cost_summary.tool_context_saving = 1 - injected/full``。

- **口径与 usage 捕获层一致**：无 tokenizer 时用字符数/4 粗估（``estimate_tokens``），schema 序列化
  成 JSON 后估。deferred 曝光下 injected = search_tools schema + 压缩目录文本；全量基线 =
  该 agent 白名单全部工具的 full schema。full 模式下两者相等，saving=0。
- **进程内、按 run 聚合**：与 ``run_control_registry`` 同类的瞬态进程状态，run 终结时由
  chat 层 ``clear(run_id)`` 回收；无 run 上下文（run_id 为空，如离线单测直调循环）静默跳过。
- **线程/协程安全**：LangGraph 并发 worker superstep 可能并行组装，统一一把锁保护。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from ..models.usage import estimate_tokens


def estimate_text_tokens(text: str) -> int:
    """文本 token 粗估（len/4，同 07 口径）。"""
    return estimate_tokens(text or "")


def estimate_schema_tokens(items: Iterable[Dict[str, Any]]) -> int:
    """一组 schema 项（get_tools_as_schemas 形状）序列化为 JSON 后的 token 粗估之和。"""
    total = 0
    for item in items:
        schema = item.get("schema") if isinstance(item, dict) else None
        if schema is None:
            continue
        total += estimate_tokens(json.dumps(schema, ensure_ascii=False))
    return total


@dataclass
class _RunExposure:
    worker_assemblies: int = 0
    deferred_assemblies: int = 0
    injected_tokens: int = 0
    full_tokens: int = 0
    exposed_tool_count: int = 0     # deferred 初始暴露给模型的工具数（含 search_tools）
    full_tool_count: int = 0        # 全量注入本会暴露的工具数
    agents: List[str] = field(default_factory=list)


class ToolExposureLedger:
    """进程内、按 run 聚合的工具曝光计量。"""

    def __init__(self) -> None:
        self._runs: Dict[str, _RunExposure] = {}
        self._lock = threading.Lock()

    def record(
        self,
        run_id: Optional[str],
        *,
        agent: Optional[str],
        deferred: bool,
        injected_tokens: int,
        full_tokens: int,
        exposed_tool_count: int,
        full_tool_count: int,
    ) -> None:
        """记一次 worker 工具组装的曝光计量。run_id 为空时静默跳过（离线/无 run 上下文）。"""
        if not run_id:
            return
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                entry = _RunExposure()
                self._runs[run_id] = entry
            entry.worker_assemblies += 1
            if deferred:
                entry.deferred_assemblies += 1
            entry.injected_tokens += max(0, injected_tokens)
            entry.full_tokens += max(0, full_tokens)
            entry.exposed_tool_count += max(0, exposed_tool_count)
            entry.full_tool_count += max(0, full_tool_count)
            if agent:
                entry.agents.append(agent)

    def summary(self, run_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """返回 run 级曝光汇总；无记录返回 None（无 worker 走过按需曝光路径，如纯 Fast Answer）。"""
        if not run_id:
            return None
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None or entry.worker_assemblies == 0:
                return None
            injected = entry.injected_tokens
            full = entry.full_tokens
            if entry.deferred_assemblies == 0:
                mode = "full"
            elif entry.deferred_assemblies == entry.worker_assemblies:
                mode = "deferred"
            else:
                mode = "mixed"
            saving = round(1 - injected / full, 4) if full > 0 else 0.0
            return {
                "mode": mode,
                "worker_assemblies": entry.worker_assemblies,
                "deferred_assemblies": entry.deferred_assemblies,
                "schema_tokens_injected": injected,
                "schema_tokens_full_baseline": full,
                "schema_tokens_saved": max(0, full - injected),
                "tool_context_saving": saving,
                "tools_exposed_initial": entry.exposed_tool_count,
                "tools_full_baseline": entry.full_tool_count,
            }

    def clear(self, run_id: Optional[str]) -> None:
        if not run_id:
            return
        with self._lock:
            self._runs.pop(run_id, None)

    def reset(self) -> None:
        """测试隔离：清空全部 run 记录。"""
        with self._lock:
            self._runs.clear()


_ledger_singleton: Optional[ToolExposureLedger] = None


def get_tool_exposure_ledger() -> ToolExposureLedger:
    """进程级工具曝光计量单例（worker 组装写入，chat 终结层读取并回收）。"""
    global _ledger_singleton
    if _ledger_singleton is None:
        _ledger_singleton = ToolExposureLedger()
    return _ledger_singleton
