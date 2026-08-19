"""
FastAnswer Agent 节点 (Domain Layer)

快速模式：知识问答 + 少量低延迟只读事实工具。
特点：
- 基于 LLM 内置知识 + 轻量 RAG 快速回答
- 对常见高波动事实（如汇率）优先使用已接入的真实数据工具
- 边界自知机制：检测到需要深度调研时，主动提示用户切换模式
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from ...entities.state import TravelAgentState
from ...models.router import get_model_router
from ...guardrails.output_guard import OutputGuard
from ...rag.retriever import HybridRetriever
from ...rag.retrieval_pipeline import retrieve_for_query
from ...rag.retrieval_grader import GradeResult, RetrievalGrader
from ...rag.policy import RAGModePolicy, RAGPolicyInput
from ...rag.summary import build_retrieval_summary
from ...rag.collections import (
    GroundingCorpus,
    grounding_corpus,
    relabel_to_logical_collections,
)
from ...memory.context_builder import ContextBuilder
from ...panels.constraint import (
    format_constraint_pack_for_prompt,
    referenced_context_sections,
)
from ..scope.constraint_normalizer import build_run_constraint_pack
from ..utils import session_history_for_context_builder
from .prompts import DISCOVERY_OBJECTIVE_INSTRUCTION, SYSTEM_PROMPT

if TYPE_CHECKING:
    from ...api.sse_buffer import SSEBuffer

logger = logging.getLogger(__name__)

_NODE_NAME = "fast_answer_agent"

_RAG_TOP_K = 3
_ADVANCED_FAST_MODE = False

_CURRENCY_TOOL_NAME = "latest_exchange_rates"
_CURRENCY_CODES = {
    "AUD",
    "CAD",
    "CHF",
    "CNY",
    "EUR",
    "GBP",
    "HKD",
    "JPY",
    "KRW",
    "SGD",
    "THB",
    "USD",
}
_CURRENCY_ALIASES = {
    "CNY": ("人民币", "rmb", "cny", "cnh"),
    "JPY": ("日元", "日币", "日圓", "日圆", "yen", "jpy"),
    "USD": ("美元", "美金", "美刀", "usd", "dollar", "dollars"),
    "EUR": ("欧元", "eur", "euro", "euros"),
    "GBP": ("英镑", "gbp", "pound", "pounds"),
    "HKD": ("港币", "港元", "hkd"),
    "KRW": ("韩元", "krw"),
    "SGD": ("新币", "新加坡元", "sgd"),
    "THB": ("泰铢", "thb"),
    "AUD": ("澳元", "aud"),
    "CAD": ("加元", "cad"),
    "CHF": ("瑞郎", "瑞士法郎", "chf"),
}
_EXCHANGE_RATE_QUERY_RE = re.compile(
    r"(汇率|兑换|换算|兑|exchange\s*rate|currency|convert|conversion)",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_CN_DATE_RE = re.compile(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日")


def _log_corpus_reach(
    corpus: GroundingCorpus,
    pool: List[Dict[str, Any]],
    injected: List[Dict[str, Any]],
) -> None:
    """按语料出身分两行报「这一问读到了什么」。

    合成一行的话，「知识库有没有贡献」有答案而**「他自己传的东西有没有被读到」没有答案**
    ——后者才是用户能看见、能改、也可能弄错的那一半。这一条与深度规划那两行
     `RAG place funnel [origin=…]` 同源。

    和那两行的一处**故意的不同**：`origin=factory` 这里**零段也印**。funnel 的规矩是
    「注入 0 段就整行不印」，因为那条链上没有提名可丢、一行全零会被读成有；而这里
    零段恰恰是要看的东西 —— 快问快答路径此前根本没查过出厂语料，
    如果零段不印，接线断没断在读数上又是没有答案。

    `origin=user` 沿用原规矩：查过就印（含 0 段），解析不出用户身份则整行没有 ——
    「问过了没拿到」与「没有库可问」必须分得开。
    """

    def _count(docs: List[Dict[str, Any]], user_owned: bool) -> int:
        return sum(
            1
            for doc in docs
            if corpus.is_user_owned(str(doc.get("collection") or "")) is user_owned
        )

    factory_collections = [
        name for name in corpus.logical_collections if not corpus.is_user_owned(name)
    ]
    logger.info(
        "FastAnswer RAG reach [origin=factory]: pool=%d injected=%d | collections=%s",
        _count(pool, False),
        _count(injected, False),
        ",".join(factory_collections),
    )
    logger.info(
        "FastAnswer RAG reach [origin=user]:    pool=%d injected=%d",
        _count(pool, True),
        _count(injected, True),
    )


class FastAnswerRagResult(NamedTuple):
    """快问快答那一次检索的全部产出。"""

    prompt_prefix: str
    summary: Optional[Dict[str, Any]]
    injected_docs: List[Dict[str, Any]]


async def _run_rag(
    retriever: HybridRetriever,
    user_query: str,
    corpus: GroundingCorpus,
    *,
    advanced_fast_mode: bool,
) -> FastAnswerRagResult:
    """快问快答的知识库检索：查哪几个集合、注入哪几段、报什么。

    与 ``destination_researcher._run_rag`` 对称，**而且这份对称是判据要求的**：
    两条执行路径对「知识库在哪」必须给同一个答案。另一条路径此前不查用户上传的
    资料库，而这一半的问题是：快问快答此前只查用户自己那一个集合，出厂语料
    **结构上不可能**被读到。它单独成一个函数，是为了让检查落在真实检索链上，
    而不是落在「清单里有没有这个字符串」上 —— 后者在一条从不执行检索的分支上
    也一样成立。

    这条路径关掉 HyDE / 多路改写 / 精排（保低延迟），但**不**关分级器：
    检索到的文本会直接注进用户看到的答案，分级器是它唯一的相关性防线 ——
    向量臂的下限低于当前 embedder 的噪声地板（无关文本余弦实测 0.44–0.54），
    咬不住任何东西，所以「问汇率也把绍兴夜游文档注进去」。
    """

    try:
        decision = RAGModePolicy().decide(
            RAGPolicyInput(
                query=user_query,
                execution_path="deep_research" if advanced_fast_mode else "fast_answer",
                source_condition="knowledge_base",
                candidate_count=10 if advanced_fast_mode else _RAG_TOP_K,
                latency_budget_ms=1200 if advanced_fast_mode else 350,
            )
        )
        # 送去相关性判断的候选必须**至少覆盖每个被查集合的头一条**，否则截断发生在
        # 判断之前，而这条路径上「谁进 prompt」就由集合顺序决定了：跨集合的 RRF
        # 融合退化成按名次轮流取（同一段正文只属于一个集合，所以任何一段都只被一条
        # 列表排过名，融合分恒等于 1/(60+名次)），候选池因此是一份完美的轮询表 ——
        # 取前 3 就是「前三个探针各自的第 1 名」。用户自己上传的资料库排在探针清单
        # 末位、四个出厂集合在它前面，**它的头一条结构上永远进不了前 3**。
        # 实测那一句「杭州西湖边有哪些本地人常去的小吃店」：被判的三段是无锡小笼 /
        # 上海茶馆 / 无锡夜生活（分级器全判 1 分，判得对），而池子里第 4 条是这个用户
        # 自己写的杭州文档（向量分 0.7406，全池最高）、第 5 条是出厂语料里逐字回答
        # 这个问题的那段杭州小吃清单。
        grading_k = max(_RAG_TOP_K, len(corpus.probe_collections))
        outcome = await retrieve_for_query(
            user_query,
            retriever=retriever,
            collections=corpus.probe_collections,
            top_k=grading_k,
            use_rewrite=decision.use_rewrite,
            use_multi_query=decision.use_multi_query,
            use_hyde=decision.use_hyde,
            use_rerank=decision.use_rerank,
        )
        pool = outcome.pool
        # 物理名不许越过接地边界；翻译只在这里做一次（定义处在 rag.collections）。
        relabel_to_logical_collections((*outcome.docs, *pool), corpus)

        grade_result = None
        if outcome.docs and decision.use_grader:
            grade_result = await RetrievalGrader().grade(query=user_query, docs=outcome.docs)
        # 判过之后才按 prompt 预算截；分级器交出的清单已按分数降序（见那里的注释），
        # 所以这一刀切掉的是分最低的那几段，不是「排在后面的集合」。
        docs = knowledge_docs_cleared_for_injection(outcome.docs, grade_result)[:_RAG_TOP_K]

        summary = build_retrieval_summary(
            user_query,
            pool,
            selected_docs=docs,
            rewritten_queries=outcome.query_variants,
            mode_decision=decision,
            grade_result=grade_result,
            collections=corpus.logical_collections,
        ).to_dict()
        _log_corpus_reach(corpus, pool, docs)

        if not docs:
            return FastAnswerRagResult("", summary, [])
        rag_text = retriever.format_docs_for_prompt(docs)
        logger.info(
            "FastAnswer RAG (Hybrid+Rewrite) 注入 %d 个文档块（候选 %d），advanced=%s，rerank=%s",
            len(docs),
            len(pool),
            advanced_fast_mode,
            decision.use_rerank,
        )
        return FastAnswerRagResult(f"参考知识库信息：\n{rag_text}\n\n", summary, docs)
    except Exception as exc:
        logger.warning("FastAnswer RAG 检索失败: %s", exc)
        # 失败这一支也要报「本轮查的是哪几个集合」：它是「问过了没拿到」与
        # 「压根没问」的唯一区分处，退化成空清单就把两件事画成了同一件。
        summary = build_retrieval_summary(
            user_query,
            [],
            mode_decision={
                "retrieval_mode": "unavailable",
                "enabled_features": [],
                "limitations": ["rag_retrieval_failed"],
            },
            collections=corpus.logical_collections,
        ).to_dict()
        return FastAnswerRagResult("", summary, [])


def knowledge_docs_cleared_for_injection(
    docs: List[Dict[str, Any]],
    grade_result: Optional[GradeResult],
) -> List[Dict[str, Any]]:
    """快速路径唯一的相关性防线：**没判过就不注入**。

    这条路径把检索到的文本直接拼进用户看到的答案，后面没有准入、没有出处校验、
    没有第二个判官。所以它的合同只有一句：一段知识库文本要出现在答案里，必须
    有人判过它跟这个问题相关。

    两种"没判过"都算没判过，一视同仁 fail closed：
    - 压根没跑分级器（``grade_result is None``）；
    - 跑了但没判成（``graded=False``——分级器为深度路径保留了"评分失败就全部
      使用"的降级，那对深度路径是对的，对这里不是）。

    判过之后**逐段说话**：``filtered_docs`` 就是逐段判定相关的那几段，一段都没过
    就是空表。不能只按整批平均分低（``LOW_QUALITY``）丢弃：「四个出厂集合各回
    一段无关的 + 用户自己那段逐字回答问题的」平均分 1.8，于是那段 5 分的正文会跟
    垃圾一起被丢掉，而候选放得越宽越容易触发。
    逐段过滤给的保证更强也更好说：**答案里的每一段知识库文本，都有人判过它相关。**

    向量臂的下限救不了这件事：当前 embedder 的噪声地板实测 0.44–0.54，完全无关
    的文本也能越过任何咬得住的取值——实测一句"100 美元等于多少人民币"把只装了
    绍兴夜游文档的用户库检索了出来，4/4 轮次都注入了它。那种批次里每一段都是 1 分，
    逐段过滤之后一样是空表。
    """

    if not docs:
        return []
    if grade_result is None:
        logger.warning("FastAnswer RAG 无相关性判定结果，本轮不注入知识库内容")
        return []
    if not grade_result.graded:
        logger.warning("FastAnswer RAG 分级器未能评分，本轮不注入知识库内容")
        return []
    cleared = list(grade_result.filtered_docs)
    if not cleared:
        logger.info(
            "FastAnswer RAG 逐段判定后无相关内容（avg=%.2f，档=%s），本轮不注入",
            grade_result.avg_score,
            grade_result.route.value,
        )
    return cleared


def _detect_currency_pair(query: str) -> Optional[tuple[str, str]]:
    """Return (base, target) for common exchange-rate wording, or None."""
    if not query or not _EXCHANGE_RATE_QUERY_RE.search(query):
        return None

    hits: List[tuple[int, str]] = []
    upper_query = query.upper()
    for match in re.finditer(r"\b[A-Z]{3}\b", upper_query):
        code = match.group(0)
        if code in _CURRENCY_CODES:
            hits.append((match.start(), code))

    lowered = query.lower()
    for code, aliases in _CURRENCY_ALIASES.items():
        for alias in aliases:
            start = lowered.find(alias.lower())
            if start >= 0:
                hits.append((start, code))
                break

    ordered: List[str] = []
    for _, code in sorted(hits, key=lambda item: item[0]):
        if code not in ordered:
            ordered.append(code)
    if len(ordered) < 2:
        return None
    return ordered[0], ordered[1]


def _extract_requested_currency_date(query: str) -> Optional[str]:
    """Extract an explicit user date so latest FX cannot answer a dated query."""

    match = _ISO_DATE_RE.search(query) or _CN_DATE_RE.search(query)
    if match is None:
        return None
    try:
        return datetime.date(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        ).isoformat()
    except ValueError:
        return None


def _extract_mcp_text_payload(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), list):
            for item in value["content"]:
                text = _extract_mcp_text_payload(item)
                if text:
                    return text
        if "result" in value:
            return _extract_mcp_text_payload(value["result"])
    if isinstance(value, list):
        for item in value:
            text = _extract_mcp_text_payload(item)
            if text:
                return text
    return ""


def _exchange_payload_from_envelope(envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sanitized = envelope.get("sanitized_result") if isinstance(envelope, dict) else None
    text = _extract_mcp_text_payload(sanitized)
    if not text:
        text = _extract_mcp_text_payload(envelope.get("result") if isinstance(envelope, dict) else None)
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _format_rate(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if value >= 10:
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _build_exchange_realtime_context(
    *,
    query: str,
    base: str,
    target: str,
    envelope: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not envelope:
        return {
            "prompt": (
                "实时汇率工具不可用。用户询问当前汇率时，不要编造具体数字；"
                "只能说明暂时无法读取实时汇率，并建议以银行或支付渠道实时牌价为准。"
            ),
            "evidence": None,
        }

    status = str(envelope.get("status") or "")
    if status != "success":
        error = envelope.get("error") or envelope.get("result_summary") or "unknown error"
        if status in {"not_applicable", "reference_only"}:
            return {
                "prompt": (
                    f"该日期不适用于当前 latest 汇率工具（{error}）。"
                    "不要使用工具失败式措辞，也不要用最新汇率冒充指定日期事实；"
                    "直接说明当前工具只支持最新数据。"
                ),
                "evidence": None,
            }
        return {
            "prompt": (
                f"实时汇率工具调用失败（{error}）。用户询问当前汇率时，不要编造具体数字；"
                "只能说明暂时无法读取实时汇率，并建议以银行或支付渠道实时牌价为准。"
            ),
            "evidence": None,
        }

    data = _exchange_payload_from_envelope(envelope)
    rates = data.get("rates") if isinstance(data, dict) else None
    amount = data.get("amount", 1) if isinstance(data, dict) else 1
    rate_value = None
    if isinstance(rates, dict) and target in rates:
        try:
            rate_value = float(rates[target]) / float(amount or 1)
        except (TypeError, ValueError, ZeroDivisionError):
            rate_value = None
    if rate_value is None:
        return {
            "prompt": (
                "实时汇率工具返回了不可解析结果。用户询问当前汇率时，不要编造具体数字；"
                "只能说明暂时无法读取实时汇率，并建议以银行或支付渠道实时牌价为准。"
            ),
            "evidence": None,
        }

    date = str(data.get("date") or "").strip()
    retrieved_at = str(envelope.get("retrieved_at") or "").strip()
    source = "Frankfurter public exchange-rate API"
    rate_text = f"1 {base} = {_format_rate(rate_value)} {target}"
    evidence_id = f"ev_{envelope.get('audit_id') or uuid.uuid4().hex[:12]}"
    evidence = {
        "evidence_id": evidence_id,
        "source_type": "tool",
        "source_name": source,
        "tool_name": _CURRENCY_TOOL_NAME,
        "title": f"{rate_text} ({date or 'latest'})",
        "snippet": f"{rate_text}; date={date or 'latest'}; query={query}",
        "retrieved_at": retrieved_at,
        "freshness_status": "fresh",
        "authority_score": 0.82,
        "metadata": {
            "tool_audit_id": envelope.get("audit_id"),
            "tool_status": status,
            "untrusted_content": False,
            "trust_level": envelope.get("trust_level"),
        },
    }
    prompt = (
        "实时汇率工具结果（优先级高，请基于它回答）：\n"
        f"- 查询：{base} -> {target}\n"
        f"- 汇率：{rate_text}\n"
        f"- 数据日期：{date or 'latest'}\n"
        f"- 检索时间：{retrieved_at or 'unknown'}\n"
        f"- 来源：{source} via `{_CURRENCY_TOOL_NAME}`\n"
        "回答必须说明来源和日期；不要要求用户切换任何内部模式。"
    )
    return {"prompt": prompt, "evidence": evidence}


async def _build_realtime_tool_context(
    *,
    state: TravelAgentState,
    stream_queue: Optional["SSEBuffer"],
) -> Dict[str, Any]:
    pair = _detect_currency_pair(state.user_query or "")
    if not pair:
        return {"prompt": "", "evidence_records": []}

    base, target = pair
    tool_call_id = f"fast_currency_{uuid.uuid4().hex[:8]}"
    args = {"base_currency": base, "to_currencies": target}
    requested_date = _extract_requested_currency_date(state.user_query or "")
    if requested_date is not None:
        args["requested_date"] = requested_date

    try:
        from ..utils import execute_tool
        from ...builders import get_components
        from ...tools.registry import get_tool_registry
        from ...workflows.run_control import run_ts_ms

        registry = get_tool_registry()
        if not registry.has_tool(_CURRENCY_TOOL_NAME):
            context = _build_exchange_realtime_context(
                query=state.user_query or "",
                base=base,
                target=target,
                envelope=None,
            )
            return {"prompt": context["prompt"], "evidence_records": []}

        if stream_queue is not None:
            await stream_queue.put(("tool_start", _NODE_NAME, {
                "name": _CURRENCY_TOOL_NAME,
                "tool_call_id": tool_call_id,
                "args_summary": f"base_currency={base}, to_currencies={target}",
                "category": "data",
                "ts_ms": run_ts_ms(),
            }))

        components = get_components()
        started = time.perf_counter()
        envelope = await execute_tool(
            _CURRENCY_TOOL_NAME,
            args,
            allowed_tool_names={_CURRENCY_TOOL_NAME},
            max_retries=1,
            run_id=state.run_id,
            node_name=_NODE_NAME,
            tool_audit_store=getattr(components, "tool_audit_store", None),
            activation_source="fast_realtime_preflight",
        )
        context = _build_exchange_realtime_context(
            query=state.user_query or "",
            base=base,
            target=target,
            envelope=envelope,
        )

        if stream_queue is not None:
            evidence = context.get("evidence")
            summary = evidence.get("title") if isinstance(evidence, dict) else str(envelope.get("result_summary") or "实时汇率读取失败")
            status = str(envelope.get("status") or "")
            await stream_queue.put(("tool_done", _NODE_NAME, {
                "name": _CURRENCY_TOOL_NAME,
                "tool_call_id": tool_call_id,
                "summary": summary,
                # ``status``（ToolExecutionStatus）是工具轮次结论的唯一权威：
                # 「该日期不适用于 latest 汇率工具」是能力判定，不是调用失败，
                # 布尔 success 无法表达这个第三态，已从合同中删除。
                "status": status,
                "audit_id": envelope.get("audit_id"),
                "degraded": status == "degraded",
                "category": "data",
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "ts_ms": run_ts_ms(),
            }))

        evidence_records = [context["evidence"]] if isinstance(context.get("evidence"), dict) else []
        return {"prompt": context["prompt"], "evidence_records": evidence_records}
    except Exception as exc:
        logger.warning("FastAnswer 实时汇率工具预检失败: %s", exc)
        context = _build_exchange_realtime_context(
            query=state.user_query or "",
            base=base,
            target=target,
            envelope={"status": "failed", "error": str(exc)},
        )
        return {"prompt": context["prompt"], "evidence_records": []}


async def fast_answer_node(state: TravelAgentState, config: RunnableConfig) -> Dict[str, Any]:
    """
    快速回答 Agent 节点。
    轻量知识问答；仅对已接入的低延迟事实类型调用工具。
    检测到超出能力边界时，用任务语言引导到旅行规划或目的地比较。
    """
    router = get_model_router()

    stream_queue: Optional["SSEBuffer"] = config.get("configurable", {}).get("stream_queue")

    llm = router.get_fast()
    user_query = state.user_query or ""
    current_time = state.current_time or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    preset_suffix = ""
    if state.preset_context:
        from ...preset.injector import PresetInjector
        preset_suffix = PresetInjector.format_for_agent(state.preset_context)

    # Constraint Pack：**这条路径上偏好 / 画像 / 记忆 / preset 的节奏与预算抵达模型的
    # 唯一通道**，而且与深度路径同一个装配函数（``build_run_constraint_pack``）。
    # 快路径此前没有 pack：preset 里的节奏与预算档位没有结构化装配，在这条路上
    # 一个字都到不了模型，那两项只有 ``UserProfile`` 那条平行通道说话
    # —— 同一个用户问一句话和做一份规划，对「快还是慢、省还是贵」拿到两个不同的答案。
    # 代价是每轮多一次 Fast 模型调用（自由文本抽取那一步），这是明确接受的取舍：
    # 不做那一步，两条路径的装配就不同源，而不同源正是这条缺陷的形状。
    constraint_pack = await build_run_constraint_pack(state)
    constraint_text = format_constraint_pack_for_prompt(constraint_pack)

    base_system = SYSTEM_PROMPT.format(current_time=current_time)
    if preset_suffix:
        base_system += preset_suffix
    if constraint_text:
        base_system += "\n\n" + constraint_text

    # 将 LangGraph messages 转为 dict 格式（供 ContextBuilder 使用）。
    # 不在这里设条数上限：裁剪与压缩判断都是预算层的职责。
    raw_history = session_history_for_context_builder(state)

    # 解析 Anchor 摘要（若存在）
    from ...memory.compressor import AnchorSummary
    session_anchor_obj = None
    if state.session_anchor:
        try:
            session_anchor_obj = AnchorSummary.from_dict(state.session_anchor)
        except Exception as e:
            logger.debug(f"FastAnswer: Anchor 解析失败: {e}")

    # 会话轴的预算与压缩检测。**它不再装配任何记忆层，这是刻意的**（与深度路径那侧的
    # ``agents/scope/node.py::_measure_session_size`` 同一个理由）：偏好、画像、手写
    # 记忆、检索记忆现在全部经 Constraint Pack 抵达模型 —— 让这一层也装一份，同一条
    # 手写记忆就会在【本轮统一约束】与【用户明确要求】两节里各印一遍，而那两节的
    # 强制力措辞还不一样。``ContextBuilder`` 上已经没有记忆层入参了。
    ctx_builder = ContextBuilder()
    built_ctx = await ctx_builder.build_context(
        session_id=state.session_id or "",
        system_prompt=base_system,
        recent_messages=raw_history,
        session_anchor=session_anchor_obj,
        session_compressed=state.session_compressed,
    )

    # ── 压缩信号处理 ──────────────────────────────────────────────────────
    # 一次请求最多压一轮（``ContextBudget.max_compaction_rounds_per_request``）：
    # 压完立刻用新 Anchor 重装一次上下文，不再回头看还需不需要压。
    context_compaction: Dict[str, Any] = {"triggered": False}
    if built_ctx.needs_compaction and stream_queue is not None:
        from ...memory.compaction import CompactionBusy, get_compaction_service

        try:
            result = await get_compaction_service().compact(
                user_id=state.user_id or "",
                session_id=state.session_id or "",
                source="automatic",
            )
        except CompactionBusy:
            # 已经有一次在跑（或提交时被抢先）。这是正常跳过，不是失败。
            logger.info("FastAnswer: 会话已有压缩在进行，本轮沿用原上下文")
            result = None
        except Exception as e:
            # 压缩失败就照原上下文继续，不额外发事件：这条路径上没有「压缩进度」这
            # 一类事件了（见 ``chat_stream_handlers`` 里那段说明），而本轮压缩没成，
            # 下面的 ``context_report`` 自然报 triggered=False。
            logger.error(f"FastAnswer: 自动压缩失败，继续使用原始上下文: {e}")
            result = None
        if result is not None:
            anchor = result.anchor
            session_anchor_obj = anchor
            context_compaction = {
                "triggered": True,
                "tokens_before": anchor.tokens_before,
                "tokens_after": anchor.tokens_after,
                "messages_compressed": anchor.messages_compressed,
                "summary_preview": anchor.summary[:200]
                + ("..." if len(anchor.summary) > 200 else ""),
            }
            await stream_queue.put(("context_compaction", _NODE_NAME, result.event))
            built_ctx = await ctx_builder.build_context(
                session_id=state.session_id or "",
                system_prompt=base_system,
                recent_messages=raw_history,
                session_anchor=session_anchor_obj,
                session_compressed=True,
            )

    # context_report：压缩结论出来之后随流下发一次。
    # 列的是 ``referenced_context_sections(pack)`` —— 与 ``format_constraint_pack_for_prompt``
    # 同一个产地（``panels/constraint.py::_constraint_prompt_sections``），所以「屏幕上说
    # 参考了」与「模型真的读到了」在这条路上也不可能各自漂移。深度路径那侧同一份报告由
    # 深度路径由 Request Contract 归一化节点发。
    #
    # 这一轮什么都没参考（没有条目进 prompt，也没压缩）时 ``build_context_report``
    # 返回 None，这个事件就不发 —— 印记的出现本身要能证明数据存在。
    if stream_queue is not None:
        from ...memory.context_builder import build_context_report
        report = build_context_report(
            referenced_sections=referenced_context_sections(constraint_pack),
            compaction=context_compaction,
        )
        if report is not None:
            await stream_queue.put(("context_report", _NODE_NAME, report))

    messages: List[Dict[str, Any]] = [{"role": "system", "content": built_ctx.system_prompt}]
    messages.extend(built_ctx.messages)
    if str((state.route_decision or {}).get("route") or "") == "destination_discovery":
        messages.append({"role": "system", "content": DISCOVERY_OBJECTIVE_INSTRUCTION})

    # 这里**没有**「向 state 回写压缩状态」那一段。它写的 ``needs_compaction`` 全仓
    # 没有读取方，而它的条件读的是**重建之后**的 ``built_ctx``：压缩成功那一支已经用
    # ``session_compressed=True`` 重新装配过，``needs_compaction`` 必然是 False，
    # 于是「成功不写、失败反而可能写」。压缩这件事在这条路径上的出口只有一个 ——
    # 下面已经发过的 ``context_report``（连同持久化用的 ``context_compaction`` 事件），
    # 新 Anchor 本身由 ``CompactionService`` 落库，下一轮由载入器读回来。

    # 轻量 RAG（Hybrid Search + Query Rewriting，单路改写保持低延迟）
    # 设计取舍：fast_answer 关闭 HyDE / 多路改写 / 精排，但**不**关闭分级器。
    # 这条路径把检索到的文本直接注进用户看到的答案，分级器是它唯一的相关性防线：
    # 向量臂的下限低于当前 embedder 的噪声地板（无关文本余弦实测 0.44–0.54），
    # 咬不住任何东西，所以「问汇率也把绍兴夜游文档注进去」。
    advanced_fast_mode = bool(
        config.get("configurable", {}).get("advanced_fast_mode", _ADVANCED_FAST_MODE)
    )
    rag = await _run_rag(
        HybridRetriever(),
        user_query,
        grounding_corpus(),
        advanced_fast_mode=advanced_fast_mode,
    )
    rag_prefix = rag.prompt_prefix
    retrieval_summary = rag.summary
    docs = rag.injected_docs

    realtime_context = await _build_realtime_tool_context(state=state, stream_queue=stream_queue)
    realtime_prompt = str(realtime_context.get("prompt") or "")
    realtime_evidence = [
        item for item in (realtime_context.get("evidence_records") or []) if isinstance(item, dict)
    ]
    if realtime_prompt:
        messages.append({"role": "system", "content": realtime_prompt})

    messages.append({"role": "user", "content": f"{rag_prefix}用户问题：{user_query}"})

    try:
        # 直接流式交付。OutputGuard 只在终态生成来源/标签增强，不以文风审查
        # 阻塞 token；chat_complete 的 final_content 会补上可成功绑定的局部标签。
        if stream_queue is not None:
            response = ""
            async for chunk in llm.astream(messages):
                response += chunk
                await stream_queue.put(("token", _NODE_NAME, chunk))
        else:
            response = await llm.ainvoke(messages)

        logger.info(f"FastAnswer 生成回答，长度: {len(response)}")
        grounding = OutputGuard().check(
            output_text=response,
            retrieved_docs=docs,
            evidence_records=realtime_evidence,
        )
        response = grounding.processed_output
        retrieval_summaries = [retrieval_summary] if retrieval_summary else []
        if realtime_evidence:
            retrieval_summaries.append({
                "query": user_query,
                "mode": "fast_realtime_tool",
                "selected_count": len(realtime_evidence),
                "sources": [
                    {
                        "source_name": ev.get("source_name"),
                        "tool_name": ev.get("tool_name"),
                        "title": ev.get("title"),
                        "retrieved_at": ev.get("retrieved_at"),
                    }
                    for ev in realtime_evidence
                ],
            })
        return {
            # pack 回写进 state：本轮到底装了哪些约束，trace 与快照要看得见同一份
            # 东西，而不是只看得见它的投影。
            "constraint_pack": constraint_pack,
            "constraint_pack_revision": state.constraint_pack_revision + 1,
            "messages": [AIMessage(content=response)],
            "is_completed": True,
            "output_confidence": grounding.confidence.value,
            "final_grounding": grounding.final_grounding,
            "retrieval_summaries": retrieval_summaries,
            "synthesis_mode": "fast",
        }

    except Exception as e:
        logger.error(f"FastAnswer 执行失败: {e}")
        raise
