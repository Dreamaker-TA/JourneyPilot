"""Memory-extraction telemetry (CB-05 · 记忆抽取管线可观测性).

记忆抽取是 fire-and-forget 的非关键路径：每轮对话结束后异步触发一次 fast-model 抽取，
把 facts / portrait 写入 memory_facts 与知识图谱。链路断掉时用户只会觉得「它不记得我」，
运维却无从归因——本模块给这条静默管线一个进程级计数，配合升级后的日志分级，让抽取的
成败在 ``/api/status`` 与 INFO 日志里可见。

- **进程内累计**：自进程启动起的累计计数（同 Prometheus counter 语义），``/api/status``
  读快照即可确认链路健康，不做 per-run 生命周期管理。
- **线程/协程安全**：并发 run 的 fire-and-forget 抽取可能同时结算，统一一把锁保护。
- **口径**：``attempted`` 为真正调用过抽取的轮次（已过滤空消息与匿名用户）；
  ``succeeded`` 为 LLM 返回并处理完成（可能本轮无值可写）；``failed`` 为 LLM/解析/写入
  任一环节抛错。``facts_written`` / ``portraits_written`` 为实际入库计数。
- ``rejected_all`` 与它们都不同，是本模块存在的理由本身：**模型交出了候选，准入把它们
  全部拒掉**。这不是「这一轮没什么可记的」（纯寒暄就该是那样，并且照样算 succeeded），
  而是「有东西要记，但一条都没进去」。两者在 ``succeeded`` 里长得一模一样，而只有后者
  意味着链路坏了——实测过一次 11 轮全拒、``succeeded`` 报 11/11 的情形，运维从任何读数
  上都看不出异常。``last_rejection`` 带上准入给出的拒绝理由计数，让「为什么全拒」不必
  去翻 DEBUG 日志。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class _ExtractionStats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    rejected_all: int = 0
    facts_written: int = 0
    portraits_written: int = 0
    last_error: Optional[str] = None
    last_rejection: Optional[str] = None


class MemoryExtractionStats:
    """进程级记忆抽取计量（抽取器结算点写入，/api/status 读取）。"""

    def __init__(self) -> None:
        self._stats = _ExtractionStats()
        self._lock = threading.Lock()

    def record_attempt(self) -> None:
        with self._lock:
            self._stats.attempted += 1

    def record_success(self, *, facts: int, portraits: int) -> None:
        with self._lock:
            self._stats.succeeded += 1
            self._stats.facts_written += max(0, facts)
            self._stats.portraits_written += max(0, portraits)

    def record_failure(self, error: str) -> None:
        with self._lock:
            self._stats.failed += 1
            self._stats.last_error = error

    def record_rejected_all(self, reasons: str) -> None:
        """模型交出了候选，准入一条都没放过——记在自己的名下，不混进 succeeded。"""
        with self._lock:
            self._stats.rejected_all += 1
            self._stats.last_rejection = reasons

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            s = self._stats
            return {
                "attempted": s.attempted,
                "succeeded": s.succeeded,
                "failed": s.failed,
                "rejected_all": s.rejected_all,
                "facts_written": s.facts_written,
                "portraits_written": s.portraits_written,
                "last_error": s.last_error,
                "last_rejection": s.last_rejection,
            }

    def reset(self) -> None:
        """测试隔离：清空累计计数。"""
        with self._lock:
            self._stats = _ExtractionStats()


_stats_singleton: Optional[MemoryExtractionStats] = None


def get_memory_extraction_stats() -> MemoryExtractionStats:
    """进程级记忆抽取计量单例（抽取器写入，系统状态读取）。"""
    global _stats_singleton
    if _stats_singleton is None:
        _stats_singleton = MemoryExtractionStats()
    return _stats_singleton


def reset_memory_extraction_stats() -> None:
    global _stats_singleton
    _stats_singleton = None
