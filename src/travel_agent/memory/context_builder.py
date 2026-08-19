"""
Context Builder v3 (Application Layer)

**这一层只管会话轴**：把「基础 system prompt + 压缩后的 Anchor 摘要 + 当前会话消息」
按 token 预算装配起来，并回答唯一那个它有资格回答的问题 —— 这个会话是不是该压缩了。

  1. [中高优先] Anchor 摘要   (session_anchor)  → 压缩后的历史会话摘要
  2. [低优先]   当前会话消息  (Working Memory)  → Token 预算裁剪

**记忆层不在这里**。用户声明的偏好、系统推理的画像、手写记忆、语义检索记忆，抵达模型
的通道只有一条：Constraint Pack（``agents/scope/request_contract_normalizer.py``
统一解析、``agents/scope/constraint_normalizer.py::build_run_constraint_pack`` 装配、``panels/constraint.py::
format_constraint_pack_for_prompt`` 投影），快慢两条路径共用同一份装配、同一套仲裁。

这一层曾经也装记忆，而且装得很全：手写记忆按条数上限取回、编号成表、被截时出声，
语义检索记忆去重后注入 —— 后来两条执行路径的 ``user_id`` 都被改成常量空串
（否则同一条手写记忆会在【本轮统一约束】与【用户明确要求】两节里各印一遍，而那两节
的强制力措辞还不一样）。**于是那半边当场变成死码，却还被几枚全绿的钉守着** ——
钉守的是一条产品到不了的路，而真正在跑的那条路上「截断必须出声」根本不成立。
后来先把出声搬进 pack 层（``panels/constraint.py::_manual_memory_omitted_note``），
再把这半边连同它的两个预算池一起删掉。留一个「传空串就不生效」的开关，等于把死码
写成看起来还能用的样子。

Token 预算（保守策略，基于 128k context 模型）：
  System Prompt:         ~600  tokens（基础 prompt，固定）
  Anchor 摘要:           ~2000 tokens（压缩后历史）
  响应预留:              ~8000 tokens（保守策略，原 4000 翻倍）
  当前会话消息:            剩余空间（messages_budget 扣减上述全部层）

压缩检测阈值：总预估 token ≥ context_limit * 50% 时触发。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN_ESTIMATE = 2.5
_TOKEN_ESTIMATE_MODEL = "gpt-4o"     # 会话轴记账用的那把尺，全层一把
_MIN_MESSAGES_BUDGET_TOKENS = 2_000   # 消息层最小保留 token 数（兜底）
_TRIM_SAFETY_RATIO = 0.9             # 裁剪时预留 10% 安全余量


def count_tokens(text: str, model: str = "gpt-4o") -> int:
    """精确计算 token 数，失败时降级为字符估算。

    公开的原因：会话历史层要按 token 预算而不是按写死的条数取消息，
    取消息的那一步必须用**同一把尺**估算，否则两侧口径不一致。
    """
    # tiktoken may download the o200k encoding for gpt-4o on first use. Context
    # budgeting must stay offline-safe in tests and degraded runtime paths.
    if model in {"gpt-4o", "gpt-4o-mini"}:
        return max(1, int(len(text) / _CHARS_PER_TOKEN_ESTIMATE))
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model)
        return len(enc.encode(text))
    except Exception:
        return max(1, int(len(text) / _CHARS_PER_TOKEN_ESTIMATE))


# ---------------------------------------------------------------------------
# Token 预算配置
# ---------------------------------------------------------------------------

@dataclass
class ContextBudget:
    """Context Window 预算配置（v3）"""
    total_context_limit: int = 120_000
    response_reserve: int = 8_000           # 保守策略：原 4000 翻倍
    system_prompt_budget: int = 600
    anchor_summary_budget: int = 2_000      # Anchor 摘要（v3 新增）
    # 手写记忆进 prompt 的条数上限（**全仓唯一定义处**）。
    # 它的消费方**不在本文件**：Request Contract Normalizer 取数时用它，
    # 出声那一句由 ``panels/constraint.py`` 印。留在这张预算表上是因为它问的是同一个
    # 问题 ——「用户的东西有多少能挤进模型的上下文」；散到调用点或存储层去，正是它
    # 曾经有三份、其中两份静默胜出的老路。
    manual_memory_facts_limit: int = 20
    compaction_threshold: float = 0.50      # 总预估超过 50% 时触发压缩
    # 一次压缩最多读多少 token / 多少条消息。**压缩是增量的**：读到预算为止，边界只推到
    # 真正进了摘要的那一条。这两个数与触发阈值分开 —— 触发说的是「该压了」，
    # 这两个说的是「这一次压多少」。
    compaction_input_tokens: int = 50_000
    max_messages_per_compaction: int = 500
    # 一次请求最多做几轮自动压缩。为了压缩而连续调好几次模型不划算。
    max_compaction_rounds_per_request: int = 1

    @property
    def messages_budget(self) -> int:
        """静态消息预算：扣减会话轴上的全部固定层。

        手写记忆与语义检索记忆那两个池子已经删除：它们的 token 落在 worker /
        fast 的 prompt 上（Constraint Pack 那条通道），不在会话轴上累积 —— 继续从这里
        扣，等于让压缩在错误的时刻触发，还让人以为这一层仍然装着记忆。
        """
        return (
            self.total_context_limit
            - self.response_reserve
            - self.system_prompt_budget
            - self.anchor_summary_budget
        )

    @property
    def compaction_trigger_tokens(self) -> int:
        """触发压缩的 token 阈值。"""
        return int(self.total_context_limit * self.compaction_threshold)


# ---------------------------------------------------------------------------
# 输出结构
# ---------------------------------------------------------------------------

@dataclass
class BuiltContext:
    """ContextBuilder.build_context() 的输出（v3）

    这里**没有** ``memory_attribution``：那份逐条记忆归因（retrieved / retained /
    injected / trimmed 四态、各层 token 账、trimmed_layers）曾经算得很全，
    写进 ``state.memory_context_attribution``，然后**全仓没有任何读取者** ——
    现状表自己就是这么记的。它唯一可能的呈现面是 ⓘ 检查面，
    而那一面明令不许出现 token / 归因粒度这类东西，所以它没有、也不该有出口。
    「装了没人看」与「没装」之间只能择一，这里选了后者：整层连同 ``memory/attribution.py``
    一起删除，而不是再加一个写入点让两条路径都往一个死字段里写。

    也**没有** ``referenced_sections``：上下文透镜列的是「本轮有哪些信息进了
    prompt」，而这一层装的只有会话历史与它自己的摘要 —— 那不是「参考的信息」。
    两条路径的透镜条目都由 ``panels/constraint.py::referenced_context_sections(pack)``
    给出，与 ``format_constraint_pack_for_prompt`` 同一个产地。它此前在这里也算了
    一份（手写记忆那几条），而两条路径后来都不再喂 ``user_id``，那份永远是空的。

    也**没有** ``compaction_warning``：「已压缩过仍超阈值」这件事此前算了一个
    布尔，两条路径各读一次，各发一个 ``compaction.warning`` SSE —— 而那个事件不在
    ``sse_projection`` 的任何一张白名单里，出门就被丢掉，前端也没有它的 case。
    「装了没人看」与「没装」之间只能择一，这里同样选后者：布尔连同两处读取点一起删，
    服务端那句 ``logger.warning`` 留着（它有真的读者：看日志的人）。
    """
    system_prompt: str
    messages: List[Dict[str, Any]]
    token_usage: Dict[str, int] = field(default_factory=dict)
    # 需要触发自动压缩（首次）。**装配期信号**：同一次 build_context 里算出，
    # 由调用它的那个节点在同一个函数体内读掉，不进 state。
    needs_compaction: bool = False


_LENS_SECTION_KEYS = ("hard", "preference", "reference")


def build_context_report(
    *,
    referenced_sections: List[Dict[str, Any]],
    compaction: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """组装用户可读的「本次参考的信息」载荷，不投影上下文实现细节。

    只定义**线上形状**，不决定内容从哪来。两条执行路径今天给的是同一份东西：
    Constraint Pack 实际印进 prompt 的条目（``panels.constraint.referenced_context_sections``）。
    此前这个函数只认 ``BuiltContext``，于是 deep 路径报的是一份被丢弃的装配 ——
    屏幕上列着几条「本次参考的信息」，而它们没有进过任何 prompt。

    **载荷是分段的，不是一串平铺的偏好。** 平铺那版还带着一个属于自己的条数上限
    （后端 8、前端再 ``slice(0, 8)`` 一刀），于是「屏幕上说参考了」与「模型真的读到了」
    在条数上各自漂。段的键只有三个（``hard`` / ``preference`` / ``reference``），
    与 prompt 里那三段一一对应；显示用的中文标题归前端，不在这条线上再写一份。

    **没有真东西可说就返回 ``None``，不返回一份空报告**。这是「这一轮值不值得
    立一枚印记」的**唯一判断处**：拿到 ``None`` 的调用方不发事件，前端那侧只有一句
    ``if (!data) return null``，不再自己判一次空。此前这个函数从不返回空，两条路径又都
    无条件下发，于是零偏好、未压缩的一轮照样得到一枚印记 —— 而印记合同
    写的是「印记的出现本身就是数据存在的证明」，且「为空则整个组件不渲染」。
    悬停语说「这轮回答已参考相关偏好和历史信息」，点开却说「本次回答主要依据当前对话中
    的旅行需求」：同一枚印记两句互相打脸的话，正是恒亮换来的。

    「这轮刚把较早的对话整理过」也算真东西：那份 Anchor 摘要进了 system prompt，
    透镜里那句「较早的对话已整理」是它唯一的出口。
    """

    sections = []
    for section in referenced_sections:
        key = str(section.get("key") or "")
        if key not in _LENS_SECTION_KEYS:
            raise ValueError(f"上下文透镜段名不在合同里: {key!r}")
        items = [str(item) for item in section.get("items") or []]
        if items:
            sections.append({"key": key, "items": items})
    triggered = bool((compaction or {}).get("triggered"))
    if not sections and not triggered:
        return None
    return {
        "referenced_sections": sections,
        "compaction": {"triggered": triggered},
    }


# ---------------------------------------------------------------------------
# ContextBuilder v3
# ---------------------------------------------------------------------------

class ContextBuilder:
    """
    会话轴的 Context 预算管理器（v3）。

    v3 相较 v2 的变更：
    - response_reserve 从 4000 → 8000（保守策略）
    - 新增 Anchor 摘要层（压缩后历史注入）
    - 新增压缩检测逻辑：组装前估算总 token，判断是否需要压缩
    - BuiltContext 新增 needs_compaction 字段

    **它不是记忆的消费方，也没有开关能让它变成记忆的消费方。** 偏好、画像、手写记忆、
    检索记忆全部经 Constraint Pack 抵达模型（见模块 docstring）。这里此前有一个
    ``user_id`` 参数当那个开关，而两条执行路径都传常量空串 —— 一个永远为假的开关和
    它守着的那半边代码一起，是「装了没人看」最难发现的一种：从签名上看它还活着。

    使用方式：
        builder = ContextBuilder()
        ctx = await builder.build_context(
            session_id=session_id,
            system_prompt=base_prompt,
            recent_messages=history,
            session_anchor=anchor,          # AnchorSummary 对象（可选）
            session_compressed=False,       # 会话是否已经压缩过
        )
        if ctx.needs_compaction:
            # 触发压缩流程
            ...
    """

    def __init__(self, budget: Optional[ContextBudget] = None) -> None:
        self._budget = budget or ContextBudget()

    async def build_context(
        self,
        session_id: str,
        system_prompt: str,
        recent_messages: List[Dict[str, Any]],
        # v3 新增参数
        session_anchor: Optional[Any] = None,   # AnchorSummary 对象
        session_compressed: bool = False,
    ) -> BuiltContext:
        """
        构建优化后的会话轴上下文（v3）：
        1. 压缩检测：组装前估算总 token，判断是否需要压缩
        2. 按预算裁剪 Anchor 摘要
        3. 组装 system_prompt（基础 prompt + Anchor 摘要）
        4. 裁剪 recent_messages

        Returns:
            BuiltContext — 包含 system_prompt, messages, 以及压缩信号字段
        """
        budget = self._budget
        model = _TOKEN_ESTIMATE_MODEL

        # Anchor 摘要处理（v3 新增）
        anchor_text = ""
        if session_anchor is not None:
            try:
                anchor_text = session_anchor.format_for_prompt()
                anchor_text = self._trim_text(anchor_text, budget.anchor_summary_budget, model)
            except Exception as e:
                logger.debug(f"ContextBuilder: Anchor 格式化失败: {e}")

        # ── 计算各层 token 数 ──────────────────────────────────────────────
        system_tokens = count_tokens(system_prompt, model)
        anchor_tokens = count_tokens(anchor_text, model)

        # ── 压缩检测（v3 核心逻辑）────────────────────────────────────────
        # 估算全量消息 token（用于判断是否超阈值）
        all_messages_text = " ".join(
            str(m.get("content", "")) for m in recent_messages
        )
        all_messages_tokens = count_tokens(all_messages_text, model)

        estimated_total = (
            system_tokens
            + anchor_tokens
            + all_messages_tokens
            + budget.response_reserve
        )

        needs_compaction = False

        if estimated_total >= budget.compaction_trigger_tokens:
            if not session_compressed:
                # 首次超阈值：标记需要压缩，返回信号
                needs_compaction = True
                logger.info(
                    f"ContextBuilder v3: 触发压缩信号 "
                    f"估算 {estimated_total} tokens ≥ 阈值 {budget.compaction_trigger_tokens} tokens"
                )
                # 仍然正常组装上下文（让当前请求先响应，压缩由外层处理）
            else:
                # 已压缩过但再次超阈值：只落日志。这里此前还算一个 compaction_warning
                # 布尔，两条路径各读一次去发 compaction.warning SSE —— 那个事件出不了
                # 投影层，前端也没有 case，随层一起删了。
                logger.warning(
                    f"ContextBuilder v3: 再次超阈值警告 "
                    f"估算 {estimated_total} tokens, 已压缩过，建议开新会话"
                )

        # ── 计算消息可用空间 ──────────────────────────────────────────────
        used_so_far = system_tokens + anchor_tokens
        available_for_messages = max(
            budget.total_context_limit - budget.response_reserve - used_so_far,
            _MIN_MESSAGES_BUDGET_TOKENS,
        )

        trimmed_messages = self._trim_messages(recent_messages, available_for_messages, model)

        # ── 组装 system prompt（含优先级标注）────────────────────────────
        system_parts = [system_prompt]

        if anchor_text:
            system_parts.append(
                "\n\n【历史对话摘要 - 本次会话背景】\n"
                "以下是本次会话较早期对话的压缩摘要，请在此基础上继续协助用户：\n"
                f"{anchor_text}"
            )

        final_system = "".join(system_parts)

        total_used = count_tokens(final_system, model) + sum(
            count_tokens(str(m.get("content", "")), model) for m in trimmed_messages
        )

        logger.debug(
            f"ContextBuilder v3: anchor={anchor_tokens}t, "
            f"msgs={len(trimmed_messages)}条, "
            f"total≈{total_used}t / {budget.total_context_limit}t "
            f"(needs_compaction={needs_compaction})"
        )

        token_usage = {
            "system": system_tokens,
            "anchor_summary": anchor_tokens,
            "messages": sum(
                count_tokens(str(m.get("content", "")), model) for m in trimmed_messages
            ),
            "total_estimated": total_used,
            "all_messages_estimated": all_messages_tokens,
            "budget": budget.total_context_limit,
            "threshold": budget.compaction_trigger_tokens,
        }

        return BuiltContext(
            system_prompt=final_system,
            messages=trimmed_messages,
            token_usage=token_usage,
            needs_compaction=needs_compaction,
        )

    # -----------------------------------------------------------------------
    # 内部辅助
    # -----------------------------------------------------------------------

    def _trim_text(self, text: str, max_tokens: int, model: str) -> str:
        """将文本裁剪到不超过 max_tokens。"""
        if not text:
            return ""
        tokens = count_tokens(text, model)
        if tokens <= max_tokens:
            return text
        ratio = max_tokens / tokens
        char_limit = int(len(text) * ratio * _TRIM_SAFETY_RATIO)
        return text[:char_limit] + "..."

    def _trim_messages(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        model: str,
    ) -> List[Dict[str, Any]]:
        """从后向前保留消息（优先保留最近的），直到达到 token 上限。"""
        if not messages:
            return []

        selected = []
        used_tokens = 0

        for msg in reversed(messages):
            content = str(msg.get("content", ""))
            tokens = count_tokens(content, model)
            if used_tokens + tokens > max_tokens:
                break
            selected.append(msg)
            used_tokens += tokens

        selected.reverse()

        if len(selected) < len(messages):
            logger.info(
                f"ContextBuilder v3: 消息裁剪 {len(messages)} → {len(selected)} 条 "
                f"({used_tokens}/{max_tokens} tokens)"
            )

        return selected
