"""LLM usage capture layer（C 域遥测捕获）.

本模块只做**捕获与进程内缓冲**：把每一次 LLM 调用的 token（含缓存/推理细分）、
wall time、TTFT、model、tier 以及 run/node/agent 归因收敛成一条 ``LLMCallRecord``，
投进线程/协程安全的 ``UsageRecorder`` 缓冲区。落库、成本计算、价格表、SSE 暴露都不
在这里。

设计要点：

- **归因**复用 run 控制层的 contextvars（``current_run_id`` / ``current_node`` /
  ``current_agent``），无 run 上下文（run_id 为 None，例如离线 eval）时**静默跳过**，
  以保证既有用例零回归。
- **token 字段命名**向 OTel GenAI 语义约定（development 阶段）对齐：
  ``input_tokens`` / ``output_tokens`` / ``cached_input_tokens`` /
  ``reasoning_output_tokens``（不用 prompt/completion 旧名）。
- **DeepSeek 缓存细分**是顶层 ``prompt_cache_hit_tokens``，LangChain 的标准
  ``usage_metadata`` 映射会丢——需要时从 ``response_metadata["token_usage"]`` 原始
  dict 补读（04 号 §4）。
- **全零 usage 对象**（个别供应商的中间 chunk 回 0 而非 null）不视作真实计数：
  input/output/total 全为 0/None 时判为「缺失」，走 estimated 降级。
- **usage 缺失**（流被中断收不到终结 chunk、供应商不回）→ 字符数/4 粗估并显著标记
  ``estimated=True``，**绝不编造精确数字**。
"""

from __future__ import annotations

import collections
import math
import threading
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple


# 缓冲上限：08 落库方尚未接线时（或落库暂时落后）避免无界增长；超限丢最旧并计数。
DEFAULT_BUFFER_MAX = 10_000


def generate_call_id() -> str:
    return f"llm_{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------- #
# 归因：读取 run 控制层的 contextvars（懒导入，避免 models -> workflows 的硬依赖环）
# --------------------------------------------------------------------------- #

def read_attribution() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """返回 (run_id, node, agent)。任一读取失败时退化为全 None（静默跳过记账）。"""
    try:
        from ..workflows.run_control import current_agent, current_node, current_run_id
    except Exception:  # pragma: no cover - run_control 恒可用，仅作防御
        return None, None, None
    return current_run_id.get(), current_node.get(), current_agent.get()


def infer_provider(base_url: Optional[str], model_name: str = "") -> str:
    """从 base_url / 模型名粗判供应商（08 定价按 provider 前缀匹配时用）。"""
    host = (base_url or "").lower()
    model = (model_name or "").lower()
    for needle, name in (
        ("deepseek", "deepseek"),
        ("minimax", "minimax"),
        ("dashscope", "qwen"),
        ("aliyuncs", "qwen"),
        ("moonshot", "moonshot"),
        ("api.openai.com", "openai"),
    ):
        if needle in host:
            return name
    if "deepseek" in model:
        return "deepseek"
    if "qwen" in model:
        return "qwen"
    if "minimax" in model or model.startswith("abab") or model.startswith("m2"):
        return "minimax"
    return "openai-compat"


# --------------------------------------------------------------------------- #
# 计量记录
# --------------------------------------------------------------------------- #

@dataclass
class LLMCallRecord:
    """一次 LLM 调用的计量记录（04 号 §3 的字段面，成本列留给 08 落库时计算）。"""

    id: str
    run_id: str
    node: Optional[str]
    agent: Optional[str]
    tier: Optional[str]
    provider: Optional[str]
    model_request: str
    method: str  # ainvoke | ainvoke_with_tools | astream | astream_with_tools
    stream: bool
    start_ts: str  # ISO8601（wall clock，供 08 落库）
    model_response: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    reasoning_output_tokens: Optional[int] = None
    estimated: bool = False
    end_ts: Optional[str] = None
    latency_ms: Optional[float] = None
    ttft_ms: Optional[float] = None  # 仅流式方法有意义
    status: str = "ok"  # ok | error
    error_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 缓冲区
# --------------------------------------------------------------------------- #

class UsageRecorder:
    """进程内计量缓冲：捕获方 ``record()``，落库方（08）``drain()`` 取走。

    线程/协程安全：LangGraph 的异步节点共享事件循环，但 Pregel 亦可能在线程池里
    跑同步节点——统一用一把 ``threading.Lock`` 保护，代价可忽略。
    """

    def __init__(self, maxlen: int = DEFAULT_BUFFER_MAX) -> None:
        self._buffer: "collections.deque[LLMCallRecord]" = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._dropped = 0

    def record(self, rec: LLMCallRecord) -> None:
        with self._lock:
            if self._buffer.maxlen is not None and len(self._buffer) >= self._buffer.maxlen:
                self._dropped += 1  # deque 满时 append 会自动挤掉最旧的一条
            self._buffer.append(rec)

    def drain(self) -> List[LLMCallRecord]:
        """取走并清空全部缓冲记录（FIFO 顺序）。"""
        with self._lock:
            items = list(self._buffer)
            self._buffer.clear()
            return items

    def requeue(self, records: List[LLMCallRecord]) -> None:
        """把一批落库失败的记录放回缓冲头部，等待下一次 drain 重试。

        ``record_calls`` 按 id 幂等，重试安全；放回头部保持 FIFO，让最早失败的先被重试。
        缓冲已满时 extendleft 从右端（最新）挤出并计入 dropped——落库失败是罕见路径，
        这里让「已产生但未落库」的旧计量优先于尚在缓冲的新计量。
        """
        if not records:
            return
        with self._lock:
            before = len(self._buffer)
            self._buffer.extendleft(reversed(records))
            overflow = before + len(records) - len(self._buffer)
            if overflow > 0:
                self._dropped += overflow

    def snapshot(self) -> List[LLMCallRecord]:
        """只读快照，不清空（测试/巡检用）。"""
        with self._lock:
            return list(self._buffer)

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._dropped = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)


_recorder_singleton: Optional[UsageRecorder] = None


def get_usage_recorder() -> UsageRecorder:
    """进程级计量缓冲单例（router 捕获与 AppComponents 暴露共用同一实例）。"""
    global _recorder_singleton
    if _recorder_singleton is None:
        _recorder_singleton = UsageRecorder()
    return _recorder_singleton


# --------------------------------------------------------------------------- #
# usage 提取 / 估算 helpers（纯函数，供 router 织入调用）
# --------------------------------------------------------------------------- #

def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _getter(obj: Any):
    """统一 dict / TypedDict / 对象 的取值方式。"""
    if isinstance(obj, dict):
        return obj.get
    return lambda key, default=None: getattr(obj, key, default)


def _deepseek_cache_read(message: Any) -> Optional[int]:
    """从原始 token_usage 补读 DeepSeek 顶层 prompt_cache_hit_tokens（标准映射会丢）。"""
    meta = getattr(message, "response_metadata", None) or {}
    if not isinstance(meta, dict):
        return None
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if not isinstance(token_usage, dict):
        return None
    return _as_int(token_usage.get("prompt_cache_hit_tokens"))


def response_finish_reason(message: Any) -> Optional[str]:
    """供应商的终止原因；``length`` 就是输出撞上 max_tokens 被截断的那个事实。

    截断的 JSON 不可解析，worker 定型只能报「不是精确 JSON 对象」——两端对不上时，
    finish_reason 是唯一能把「模型没写对」和「输出被截断」分开的证据。流式路径读的是
    累加后 chunk 的 ``response_metadata``（终结 chunk 带 finish_reason）。
    """
    meta = getattr(message, "response_metadata", None) or {}
    if not isinstance(meta, dict):
        return None
    reason = meta.get("finish_reason") or meta.get("stop_reason")
    return str(reason) if reason else None


def response_model_name(message: Any) -> Optional[str]:
    meta = getattr(message, "response_metadata", None) or {}
    if isinstance(meta, dict):
        name = meta.get("model_name") or meta.get("model")
        if name:
            return str(name)
    return None


def _is_empty_usage(input_tokens: Optional[int], output_tokens: Optional[int], total: Optional[int]) -> bool:
    """input/output/total 全为 None 或 0 → 视作缺失（全零 usage 对象不当真实计数用）。"""
    return all(v in (None, 0) for v in (input_tokens, output_tokens, total))


def extract_usage(message: Any) -> Optional[Dict[str, Optional[int]]]:
    """从 AIMessage / AIMessageChunk 归一化 token 字段；缺失/全零返回 None（交由估算降级）。"""
    if message is None:
        return None

    input_tokens = output_tokens = total_tokens = None
    cached = reasoning = None

    usage_meta = getattr(message, "usage_metadata", None)
    if usage_meta:
        get = _getter(usage_meta)
        input_tokens = _as_int(get("input_tokens"))
        output_tokens = _as_int(get("output_tokens"))
        total_tokens = _as_int(get("total_tokens"))
        input_details = get("input_token_details") or {}
        output_details = get("output_token_details") or {}
        cached = _as_int(_getter(input_details)("cache_read"))
        reasoning = _as_int(_getter(output_details)("reasoning"))

    # DeepSeek 顶层字段补读（LangChain usage_metadata 只映射 OpenAI 形状）
    if cached is None:
        cached = _deepseek_cache_read(message)

    if _is_empty_usage(input_tokens, output_tokens, total_tokens):
        return None

    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached,
        "reasoning_output_tokens": reasoning,
    }


def estimate_tokens(text: str) -> int:
    """无 tokenizer 时的字符数/4 粗估（显著标记 estimated，绝不当精确值）。"""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))
