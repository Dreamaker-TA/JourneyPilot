"""
统一模型路由层。

用单一入口管理 primary / fast 两档模型，避免散落的实例化逻辑。
未配置真实模型时直接抛出 RuntimeError（不提供占位实现）。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import time
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

from ..config import (
    FastModelConfig,
    PrimaryModelConfig,
    ProviderCapabilities,
    capabilities_for,
    get_settings,
    resolve_price,
)
from ..entities.trip_run import utc_now_iso
from ..utils.concurrency import channel_gate
from ..workflows.run_control import (
    ModelWindowClosed,
    await_model_operation,
    current_budget_ledger,
    current_model_window,
    guard_run_budget,
    observe_current_run_deadline,
    remaining_model_seconds,
)
from .usage import (
    LLMCallRecord,
    UsageRecorder,
    estimate_tokens,
    extract_usage,
    generate_call_id,
    get_usage_recorder,
    infer_provider,
    read_attribution,
    response_finish_reason,
    response_model_name,
)

logger = logging.getLogger(__name__)


def _close_unstarted(awaitable: Any) -> None:
    """关掉一个还没被 await 的协程。已经跑过的对象上是 no-op。"""

    close = getattr(awaitable, "close", None)
    if callable(close):
        try:
            close()
        except RuntimeError:
            pass


@runtime_checkable
class BaseLLM(Protocol):
    """本模块使用的 LLM 协议类型（duck typing，由各实现类自带签名对齐）。"""

    async def ainvoke(self, messages: List[Dict[str, Any]], **kwargs: Any) -> str: ...

    async def ainvoke_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> Dict[str, Any]: ...

    async def astream(
        self, messages: List[Dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[str]: ...

    async def astream_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]: ...

try:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    from langchain_openai import ChatOpenAI
except ImportError:  # pragma: no cover - langchain 未安装时降级（如纯前端开发环境）
    AIMessage = HumanMessage = SystemMessage = ToolMessage = None  # type: ignore[assignment]
    ChatOpenAI = None  # type: ignore[assignment]


class ModelTier(str, Enum):
    PRIMARY = "primary"
    FAST = "fast"


#: 档位默认对应的并发通道。名字与 `ProviderChannelConfig` 的字段一一对应。
_TIER_CHANNELS = {
    ModelTier.PRIMARY: "primary_research_llm",
    ModelTier.FAST: "online_fast_llm",
}

# 一次调用属于哪个通道。默认按档位，但**入库**要走自己的配额：它和在线快问快答用
# 同一个 fast 上游，不分开的话一次上传就能把在线请求排到队尾。
current_llm_channel: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_llm_channel",
    default=None,
)


@contextlib.contextmanager
def llm_channel(name: str) -> Iterator[None]:
    """把这一段里的模型调用记到 ``name`` 通道的配额上。"""

    token = current_llm_channel.set(name)
    try:
        yield
    finally:
        current_llm_channel.reset(token)


def _coerce_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content or "")


def _messages_text(messages: List[Dict[str, Any]]) -> str:
    """拼接入参消息文本，供 usage 缺失时的字符数粗估（仅估算用途）。"""
    parts: List[str] = []
    for msg in messages or []:
        if isinstance(msg, dict):
            parts.append(_coerce_text(msg.get("content", "")))
    return "".join(parts)


def _chunk_text(text: str, chunk_size: int = 48) -> Iterable[str]:
    normalized = text or ""
    if not normalized:
        return []
    return [normalized[i : i + chunk_size] for i in range(0, len(normalized), chunk_size)]


def _normalize_tool_calls(raw_calls: Any) -> List[Dict[str, Any]]:
    tool_calls: List[Dict[str, Any]] = []
    if not isinstance(raw_calls, list):
        return tool_calls

    for item in raw_calls:
        if not isinstance(item, dict):
            continue
        args = item.get("args", item.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, ValueError, TypeError):
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {}
        name = str(item.get("name") or "")
        if not name:
            continue
        tool_calls.append(
            {
                "id": str(item.get("id") or item.get("tool_call_id") or name),
                "name": name,
                "arguments": args,
            }
        )
    return tool_calls


def _to_langchain_messages(messages: List[Dict[str, Any]]) -> List[Any]:
    if SystemMessage is None or HumanMessage is None or AIMessage is None or ToolMessage is None:
        raise RuntimeError("langchain 消息类不可用，无法构建真实模型消息")

    converted: List[Any] = []
    # 合法的 tool 响应必须紧随拥有对应 tool_call_id 的 assistant tool_calls。
    # worker 曾把预取的地点/缺口上下文以 role="tool" 注入；宽松 provider 接受，
    # DeepSeek 则会 400。孤立、错配或已被后续普通消息隔开的 tool 结果降级为 user
    # 上下文，避免伪造 tool 协议关系而不丢失文本。
    pending_tool_call_ids: set[str] = set()
    for msg in messages:
        role = str(msg.get("role") or "user")
        content = msg.get("content", "")

        if role == "system":
            converted.append(SystemMessage(content=content))
            pending_tool_call_ids.clear()
            continue

        if role == "assistant":
            # tool_calls 必须进 AIMessage 的结构化字段：langchain 序列化时才会补全
            # OpenAI wire 格式的 type="function" + function 包裹。塞进 additional_kwargs
            # 会被原样透传、缺 type，触发 provider 400 `messages[N]: missing field type`
            # ——ReAct 迭代 1 起每轮都携带上一轮的 assistant tool_calls，故必现。
            tool_calls = [
                {"name": tc["name"], "args": tc["arguments"], "id": tc["id"], "type": "tool_call"}
                for tc in _normalize_tool_calls(msg.get("tool_calls"))
            ]
            converted.append(AIMessage(content=content, tool_calls=tool_calls))
            pending_tool_call_ids = {str(call["id"]) for call in tool_calls}
            continue

        if role == "tool":
            raw_tool_call_id = msg.get("tool_call_id")
            tool_call_id = str(raw_tool_call_id) if raw_tool_call_id is not None else ""
            if tool_call_id and tool_call_id in pending_tool_call_ids:
                converted.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=tool_call_id,
                    )
                )
                pending_tool_call_ids.remove(tool_call_id)
            else:
                converted.append(HumanMessage(content=f"[工具结果] {content}"))
                # 一条非协议 tool 消息已经把 assistant/tool 相邻关系打断；后续 tool
                # 消息不能再借用更早 assistant 的 tool_calls。
                pending_tool_call_ids.clear()
            continue

        converted.append(HumanMessage(content=content))
        pending_tool_call_ids.clear()
    return converted


def _provider_extra_body(
    capabilities: ProviderCapabilities, *, max_tokens: int
) -> Dict[str, Any]:
    """关思维链 + 把输出上限送到上游真正会读的那个键。

    本项目从不需要思维链，只需要答案：worker 的 ReAct 与结构化输出都不依赖它，而开着
    思维链时 DeepSeek 会 (1) 拒绝 response_format（400 "This response_format type is
    unavailable now"）、(2) 在多轮工具调用里破坏 tool 消息顺序，推理 token 还会整条
    拖慢流水线（实测同一个 deepseek-v4-pro，直连 82 tok/s、经代理 19 tok/s）。

    方言的归属现在**来自 preset 的声明**，不再靠 base_url 猜：

      thinking   —— DeepSeek 直连自己的请求体开关；
      reasoning  —— OpenRouter 的开关，也是被代理的 DeepSeek 唯一真正读的那个
                    （实测 thinking 经代理完全无效，reasoning_tokens 照样 664）；
      max_tokens —— langchain-openai 在 _get_request_payload 里无条件把 max_tokens
                    改名成 max_completion_tokens，而 DeepSeek 只读 max_tokens，
                    两者相乘会让配置的输出上限彻底空转（实测 fast tier 峰值 3932 >
                    配置 2048 且 finish_reason=stop）。openai SDK 把 extra_body 平铺
                    进 body，于是两个键共存，各取所需。

    ``all_dialects`` 是保守档：认不出的上游把每一种都发一遍，认不出的那种会被对方
    忽略（三个 provider 逐一实测过）。少发一种的代价是开关静默失效。
    """

    body: Dict[str, Any] = {}
    control = capabilities.reasoning_control
    if control in ("deepseek", "all_dialects"):
        body["thinking"] = {"type": "disabled"}
    if control in ("openrouter", "all_dialects"):
        # OpenRouter 当前统一方言用 effort="none" 关闭 reasoning。旧的
        # enabled=false 会被部分模型忽略，结构化提取可能把整个 completion 上限都耗在
        # reasoning_tokens，最后没有正文可解析。
        body["reasoning"] = {"effort": "none"}
    if capabilities.token_limit_field in ("max_tokens", "both"):
        body["max_tokens"] = max_tokens
    return body


_JSON_OBJECT_PROMPT_TOKEN = (
    "以 JSON (json) 对象返回结果，不要输出 JSON 之外的任何文本。"
)


def _normalize_response_format(
    kwargs: Dict[str, Any], *, capabilities: ProviderCapabilities
) -> Dict[str, Any]:
    """按上游声明的 capability 决定要不要降级 response format。

    降级的判据从「base_url 长得像不像 api.deepseek.com」换成了一份**声明**
    （`configs/providers/*.yaml`）：前者在同一个模型搬到代理后面那天就不成立，
    而失效是静默的 —— 开关不再命中，结构化输出退回一个形状正确但键名不对的对象。

    认不出的上游走保守档（不声明支持 json_schema），走 json_object + 在 prompt 里
    明写 schema 那条路。它更啰嗦但两边都能到；反过来（假设支持）拿到的是一次 400。
    """
    response_format = kwargs.get("response_format")
    if (
        not capabilities.supports_json_schema
        and isinstance(response_format, dict)
        and response_format.get("type") == "json_schema"
    ):
        return {**kwargs, "response_format": {"type": "json_object"}}
    return kwargs


def _satisfy_json_object_prompt_requirement(
    messages: List[Dict[str, Any]],
    kwargs: Dict[str, Any],
    *,
    dropped_schema: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Restore in the prompt what the ``json_object`` downgrade took away.

    Two things ride on a caller's ``json_schema``: the literal word ``json``
    (DeepSeek rejects ``json_object`` without it) and the response shape itself.
    ``json_object`` enforces neither, so a call site that relied on the schema
    alone to convey its shape gets a well-formed object with the wrong keys.  The
    router states both in the prompt instead.  Callers who already spell out JSON
    and pass no schema are left untouched.
    """
    response_format = kwargs.get("response_format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_object":
        return messages
    instructions: List[str] = []
    if not any("json" in str(message.get("content") or "").lower() for message in messages):
        instructions.append(_JSON_OBJECT_PROMPT_TOKEN)
    if isinstance(dropped_schema, dict) and dropped_schema:
        instructions.append(
            "返回对象必须严格满足此 JSON Schema（键名、必填项、层级一律照此输出）："
            f"{json.dumps(dropped_schema, ensure_ascii=False)}"
        )
    if not instructions:
        return messages
    return [*messages, {"role": "user", "content": "".join(instructions)}]


def _downgraded_json_schema(
    kwargs: Dict[str, Any], *, capabilities: ProviderCapabilities
) -> Optional[Dict[str, Any]]:
    """Return the schema `_normalize_response_format` is about to drop, if any."""
    response_format = kwargs.get("response_format")
    if not (
        not capabilities.supports_json_schema
        and isinstance(response_format, dict)
        and response_format.get("type") == "json_schema"
    ):
        return None
    wrapper = response_format.get("json_schema")
    if not isinstance(wrapper, dict):
        return None
    schema = wrapper.get("schema")
    return schema if isinstance(schema, dict) and schema else None


class OpenAICompatibleLLM(BaseLLM):
    """基于 langchain-openai 的轻量包装。"""

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        base_url: str,
        temperature: float,
        max_tokens: int,
        timeout: int = 60,
        max_retries: int = 2,
        tier: ModelTier,
        usage_recorder: Optional[UsageRecorder] = None,
    ) -> None:
        if ChatOpenAI is None:
            raise RuntimeError("langchain_openai 不可用，无法创建真实模型客户端")

        self.model_name = model_name
        self.tier = tier
        self.base_url = base_url
        self.provider = infer_provider(base_url, model_name)
        # 这个上游支持什么，来自 `configs/providers/*.yaml` 的声明；认不出走保守档。
        self.capabilities = capabilities_for(base_url)
        self._usage_recorder = usage_recorder
        # 预算守卫按「本次最坏输出」估账，而最坏输出就是这个上限。
        self._max_tokens = int(max_tokens)
        # ``request_timeout`` is this client's default per SDK attempt.  A call
        # site needing a wider bound passes ``timeout=`` in its own kwargs; the
        # SDK applies that to the single request instead of the shared client.
        self._client = ChatOpenAI(
            api_key=api_key,
            model=model_name,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            request_timeout=timeout,
            max_retries=max_retries,
            # langchain-openai 仅在默认 OpenAI base_url 下自动开启流式 usage，而本仓
            # 全部模型自定义 base_url，不显式开则流式 usage 永远为空。
            stream_usage=self.capabilities.supports_stream_usage,
            extra_body=_provider_extra_body(self.capabilities, max_tokens=max_tokens),
        )

    # --- usage 捕获织入 ------------------------------------------------ #

    def _recorder(self) -> UsageRecorder:
        # 显式 None 判断：UsageRecorder 定义了 __len__，空缓冲会被判 falsy，不能用 `or`。
        if self._usage_recorder is not None:
            return self._usage_recorder
        return get_usage_recorder()

    # --- 并发通道与预算守卫 --------------------------------------------- #

    def _channel(self):
        name = current_llm_channel.get() or _TIER_CHANNELS[self.tier]
        limit = int(getattr(get_settings().provider_channels, name))
        return channel_gate(f"llm.{name}", limit)

    def _guard_budget(self, operation: str, messages: List[Dict[str, Any]]) -> None:
        """在花掉这次调用之前判预算，按最坏开销估账。

        输入侧按字符数粗估（供应商还没告诉我们真实 token），输出侧按配置上限 ——
        这次调用最多能吐出来的就是那么多。
        """

        guard_run_budget(
            operation,
            llm_calls=1,
            input_tokens=estimate_tokens(_messages_text(messages)),
            output_tokens=self._max_tokens,
        )

    async def _in_channel(self, awaitable: Awaitable[Any], *, operation: str) -> Any:
        """在通道配额内发起一次调用，并受 Run 的时间窗约束。

        排队等在时间窗**里面**：等不到位置和调用本身太慢对一个 Run 是同一件事 ——
        窗口关了。没有窗口的那一档（快问快答、授权之前）由
        `provider_channels.max_queue_wait_seconds` 兜底，否则通道满了这条请求永远不回来。
        """

        gate = self._channel()
        try:
            wait_seconds = remaining_model_seconds(operation)
            if wait_seconds is None:
                wait_seconds = self._queue_wait_seconds()
            async with gate.hold(wait_seconds=wait_seconds):
                return await await_model_operation(awaitable, operation=operation)
        except BaseException:
            # 同步拒绝（窗口已关、已取消、通道满）时调用方构造好的那个协程还没被 await。
            # 不关掉它就是一条 "coroutine was never awaited" 警告。
            _close_unstarted(awaitable)
            raise

    def _queue_wait_seconds(self) -> float:
        return float(get_settings().provider_channels.max_queue_wait_seconds)

    def _start_record(self, method: str, *, stream: bool) -> Optional[LLMCallRecord]:
        """无 run 上下文（离线 eval 等）→ 返回 None，静默跳过记账。"""
        run_id, node, agent = read_attribution()
        if run_id is None:
            return None
        return LLMCallRecord(
            id=generate_call_id(),
            run_id=run_id,
            node=node,
            agent=agent,
            tier=self.tier.value,
            provider=self.provider,
            model_request=self.model_name,
            method=method,
            stream=stream,
            start_ts=utc_now_iso(),
        )

    def _emit(
        self,
        record: Optional[LLMCallRecord],
        monotonic_start: float,
        *,
        status: str = "ok",
        error: Optional[BaseException] = None,
        message: Any = None,
        input_text: str = "",
        output_text: str = "",
        ttft_ms: Optional[float] = None,
    ) -> None:
        if record is None:
            return
        record.end_ts = utc_now_iso()
        record.latency_ms = (time.perf_counter() - monotonic_start) * 1000.0
        record.ttft_ms = ttft_ms
        record.status = status
        if error is not None:
            record.error_type = type(error).__name__
        if message is not None:
            record.model_response = response_model_name(message)

        usage = extract_usage(message) if message is not None else None
        if usage is not None:
            record.input_tokens = usage["input_tokens"]
            record.output_tokens = usage["output_tokens"]
            record.total_tokens = usage["total_tokens"]
            record.cached_input_tokens = usage["cached_input_tokens"]
            record.reasoning_output_tokens = usage["reasoning_output_tokens"]
            record.estimated = False
        elif status == "ok":
            # usage 缺失（流被中断/供应商不回/全零对象）→ 字符数粗估并标记 estimated
            in_est = estimate_tokens(input_text)
            out_est = estimate_tokens(output_text)
            record.input_tokens = in_est
            record.output_tokens = out_est
            record.total_tokens = in_est + out_est
            record.estimated = True
        # status=error 且无 usage：token 列留 null，绝不编数

        # 每次调用一行取证：finish_reason=length 即输出被 max_tokens 截断，配合
        # output_tokens 就能判断 2048 的输出上限是否把 Research Packet 切断。归因直接用
        # record 的字段（run 控制层 contextvars），不另开一套通道；离线 eval 无 run 上下文
        # 时上面已经返回，日志与记账同进同退。
        logger.info(
            "LLM 调用完成 (call_id=%s, tier=%s, model=%s, method=%s, node=%s, agent=%s, "
            "status=%s, finish_reason=%s, output_tokens=%s, estimated=%s, latency_ms=%.0f)",
            record.id,
            record.tier,
            record.model_request,
            record.method,
            record.node,
            record.agent,
            record.status,
            response_finish_reason(message) or "unknown",
            record.output_tokens,
            record.estimated,
            record.latency_ms,
        )

        self._recorder().record(record)
        self._charge_budget(record)

    def _charge_budget(self, record: Optional[LLMCallRecord]) -> None:
        """把这一次调用记进 Run 的预算账本。

        费用用**同一个公式**（`cost_ledger_store.compute_cost_usd`）算：预算和台账
        对同一次调用给两个价钱，就没有一个数字能当上限用。价格表没命中时账本记一次
        未定价调用，而不是记 0 元。

        失败的调用也计数：它一样占了配额、一样可能被无限循环重复。
        """

        ledger = current_budget_ledger()
        if ledger is None or record is None:
            return
        # 延迟 import：台账层要经 `models.usage` 拿 LLMCallRecord，模块级引用会成环。
        from ..infrastructure.cost_ledger_store import compute_cost_usd

        price = resolve_price(record.model_request, record.provider)
        ledger.record_llm_call(
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cost_usd=compute_cost_usd(
                price,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cached_input_tokens=record.cached_input_tokens,
            ),
        )

    async def ainvoke(self, messages: List[Dict[str, Any]], **kwargs: Any) -> str:
        dropped_schema = _downgraded_json_schema(kwargs, capabilities=self.capabilities)
        kwargs = _normalize_response_format(kwargs, capabilities=self.capabilities)
        messages = _satisfy_json_object_prompt_requirement(
            messages, kwargs, dropped_schema=dropped_schema
        )
        self._guard_budget("model.ainvoke", messages)
        record = self._start_record("ainvoke", stream=False)
        started = time.perf_counter()
        try:
            response = await self._in_channel(
                self._client.ainvoke(_to_langchain_messages(messages), **kwargs),
                operation="model.ainvoke",
            )
        except BaseException as exc:  # noqa: BLE001 - 记录后原样抛出
            self._emit(record, started, status="error", error=exc, input_text=_messages_text(messages))
            raise
        text = _coerce_text(response.content).strip()
        self._emit(
            record,
            started,
            message=response,
            input_text=_messages_text(messages),
            output_text=text,
        )
        return text

    async def ainvoke_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        dropped_schema = _downgraded_json_schema(kwargs, capabilities=self.capabilities)
        kwargs = _normalize_response_format(kwargs, capabilities=self.capabilities)
        messages = _satisfy_json_object_prompt_requirement(
            messages, kwargs, dropped_schema=dropped_schema
        )
        self._guard_budget("model.ainvoke_with_tools", messages)
        record = self._start_record("ainvoke_with_tools", stream=False)
        started = time.perf_counter()
        bound = self._client.bind_tools(tools)
        try:
            response = await self._in_channel(
                bound.ainvoke(_to_langchain_messages(messages), **kwargs),
                operation="model.ainvoke_with_tools",
            )
        except BaseException as exc:  # noqa: BLE001
            self._emit(record, started, status="error", error=exc, input_text=_messages_text(messages))
            raise
        result = {
            "content": _coerce_text(response.content).strip(),
            "tool_calls": _normalize_tool_calls(getattr(response, "tool_calls", [])),
        }
        self._emit(
            record,
            started,
            message=response,
            input_text=_messages_text(messages),
            output_text=result["content"],
        )
        return result

    async def astream(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        dropped_schema = _downgraded_json_schema(kwargs, capabilities=self.capabilities)
        kwargs = _normalize_response_format(kwargs, capabilities=self.capabilities)
        messages = _satisfy_json_object_prompt_requirement(
            messages, kwargs, dropped_schema=dropped_schema
        )
        self._guard_budget("model.astream", messages)
        record = self._start_record("astream", stream=True)
        started = time.perf_counter()
        full: Any = None
        ttft_ms: Optional[float] = None
        collected: List[str] = []
        status = "ok"
        error: Optional[BaseException] = None
        try:
            remaining = remaining_model_seconds("model.astream")
            gate = self._channel()
            queue_wait = remaining if remaining is not None else self._queue_wait_seconds()

            async def consume_stream() -> AsyncIterator[str]:
                nonlocal full, ttft_ms
                # 通道位置要占满整条流：一条在读的流一直占着上游的一条连接。
                async with gate.hold(wait_seconds=queue_wait):
                    async for chunk in self._client.astream(
                        _to_langchain_messages(messages), **kwargs
                    ):
                        # 逐 chunk 累加（add_usage 语义正确：全零/无 usage 的中间 chunk 相加不污染）
                        full = chunk if full is None else full + chunk
                        text = _coerce_text(getattr(chunk, "content", ""))
                        if text:
                            if ttft_ms is None:  # TTFT = 首个非空 delta 的时刻
                                ttft_ms = (time.perf_counter() - started) * 1000.0
                            collected.append(text)
                            yield text

            if remaining is None:
                async for text in consume_stream():
                    yield text
            else:
                try:
                    async with asyncio.timeout(remaining):
                        async for text in consume_stream():
                            yield text
                except asyncio.TimeoutError as exc:
                    _deadline, observation = observe_current_run_deadline()
                    if observation is None:  # pragma: no cover - defensive context reset
                        raise
                    raise ModelWindowClosed(
                        "model.astream", observation, current_model_window.get()
                    ) from exc
        except GeneratorExit:
            # 消费方提前停止读取：干净早停，usage 多半缺失 → 走估算，不记为 error
            raise
        except BaseException as exc:  # noqa: BLE001 - 供应商报错 / 流被取消
            status, error = "error", exc
            raise
        finally:
            self._emit(
                record,
                started,
                status=status,
                error=error,
                message=full,
                input_text=_messages_text(messages),
                output_text="".join(collected),
                ttft_ms=ttft_ms,
            )

    async def astream_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        # 为了保证接口稳定，这里优先返回完整 finish 事件；主流程仍能正确执行，不依赖
        # 复杂的 tool chunk 解析。计量随委托的 ainvoke_with_tools 记一条非流式记录即可。
        result = await self.ainvoke_with_tools(messages, tools, **kwargs)
        content = str(result.get("content") or "")
        for piece in _chunk_text(content):
            yield {"type": "text_delta", "content": piece}
        yield {
            "type": "finish",
            "content": content,
            "tool_calls": result.get("tool_calls", []),
        }


class ModelRouter:
    """统一管理 primary / fast 两档模型。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._clients: Dict[ModelTier, BaseLLM] = {}
        self._scope_client: Optional[BaseLLM] = None

    def _effective_fast_config(self) -> FastModelConfig:
        fast = self._settings.fast_model.model_copy(deep=True)
        if not fast.api_key:
            fast.api_key = self._settings.primary_model.api_key
        if not fast.base_url:
            fast.base_url = self._settings.primary_model.base_url
        if not fast.model_name:
            fast.model_name = self._settings.primary_model.model_name
        return fast

    def _build_client(self, tier: ModelTier, *, max_retries: int = 2) -> BaseLLM:
        if tier == ModelTier.PRIMARY:
            config = self._settings.primary_model
        else:
            config = self._effective_fast_config()

        api_key = str(config.api_key or "").strip()
        model_name = str(config.model_name or "").strip()
        base_url = str(config.base_url or "").strip()

        missing = [
            name for name, value in (
                ("api_key", api_key),
                ("model_name", model_name),
                ("base_url", base_url),
            ) if not value
        ]
        if missing:
            raise RuntimeError(
                f"未配置 {tier.value}_model: {', '.join(missing)} 必须齐全（请检查 config.yaml）"
            )

        return OpenAICompatibleLLM(
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            temperature=float(config.temperature),
            max_tokens=int(config.max_tokens),
            timeout=int(getattr(config, "timeout", 60)),
            max_retries=max_retries,
            tier=tier,
            usage_recorder=get_usage_recorder(),
        )

    def _get_or_create(self, tier: ModelTier) -> BaseLLM:
        client = self._clients.get(tier)
        if client is None:
            client = self._build_client(tier)
            self._clients[tier] = client
        return client

    def get_primary(self) -> BaseLLM:
        return self._get_or_create(ModelTier.PRIMARY)

    def get_fast(self) -> BaseLLM:
        return self._get_or_create(ModelTier.FAST)

    def get_scope(self) -> BaseLLM:
        """Scope owns one visible retry, so its transport must perform exactly one request per attempt."""
        if self._scope_client is None:
            self._scope_client = self._build_client(ModelTier.FAST, max_retries=0)
        return self._scope_client

    def update_model(
        self,
        *,
        tier: ModelTier,
        api_key: str,
        model_name: str,
        base_url: str,
        max_tokens: int,
        temperature: float,
    ) -> None:
        target: PrimaryModelConfig | FastModelConfig
        if tier == ModelTier.PRIMARY:
            target = self._settings.primary_model
        else:
            target = self._settings.fast_model

        target.api_key = api_key
        target.model_name = model_name
        target.base_url = base_url
        target.max_tokens = max_tokens
        target.temperature = temperature

        self._clients.pop(tier, None)
        if tier == ModelTier.FAST:
            self._scope_client = None


_router: Optional[ModelRouter] = None


def get_model_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router
