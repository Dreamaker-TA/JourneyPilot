"""
上下文压缩器 (Application Layer)

将对话历史压缩为结构化 Anchor Summary，供 ContextBuilder v3 注入。

设计原则：
- 只压缩 user + assistant 消息对，不压缩 system prompt / 画像 / memory_facts
- 工具调用原始结果不保留（结论已体现在 assistant 输出中）
- 使用 Fast 模型执行压缩，降低延迟和成本
- 混合 Anchor 结构：结构化字段保留关键约束，自然语言摘要覆盖其余内容
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..utils.json_helpers import safe_parse_json

logger = logging.getLogger(__name__)

# 字符/token 估算比率（中文约 2.5 字符 ≈ 1 token）
_CHARS_PER_TOKEN_ESTIMATE = 2.5

# 压缩提示词（核心）
_COMPRESS_SYSTEM_PROMPT = """\
你是一个对话摘要专家，专门为旅行规划助手场景压缩对话历史。

你的任务是将对话历史压缩为一个结构化摘要，供后续对话继续使用。

要求：
1. 提取**用户本人明确说出**的所有约束和限制条件（如预算、人员构成、特殊需求、不能接受/必须遵守的事项、硬约束），放入 key_constraints 数组。**只从用户（user 角色）写下的内容里提取**，助手（assistant 角色）自己建议的话、推荐的措辞、推测的偏好**一律不算作**用户的约束，不得写进 key_constraints。**早先几条消息里的用户约束也要保留，不得因为被后来的内容盖过而丢弃**。每条约束独立成一个字符串，尽可能用用户的原话措辞（如「不吃香菜」「每天住西湖边」），不要改写稀释。
2. 用自然语言摘要覆盖以下内容（summary 字段）：
   - 用户的核心旅行意图（去哪里、几天、什么时候）
   - 双方已达成的重要决策（如选定的目的地、住宿类型、行程框架）
   - 助手已提供的关键信息结论（无需保留工具调用原始数据，只保留结论）
   - **用户明确点名的专属信息（人物、地点、具体偏好等）必须在摘要里保留**（如「朋友老周推荐去良渚博物院」），不得丢失
   - 当前对话尚未解决的待讨论问题
3. 摘要应简洁但不遗漏任何对后续规划有影响的信息；**在有疑问时以完整保留用户约束与用户亲口事实为准，宁可多写一行也不要丢**
4. 不需要保留工具调用的原始数据或中间推理过程，只保留最终结论
5. 如果已有之前的压缩摘要（existing_anchor），请将新信息合并进去，保持完整性

仅输出 JSON 格式，不要有任何额外文字：
{
  "key_constraints": ["用户约束1", "用户约束2", ...],
  "summary": "自然语言摘要..."
}
"""

_COMPRESS_USER_TEMPLATE = """\
请将以下对话历史压缩为结构化摘要。

{existing_anchor_section}对话历史（{message_count} 条消息）：
{conversation_text}
"""


@dataclass
class AnchorSummary:
    """对话历史的压缩锚点摘要。"""
    compressed_at: str
    messages_compressed: int
    tokens_before: int
    tokens_after: int
    key_constraints: List[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "compressed_at": self.compressed_at,
            "messages_compressed": self.messages_compressed,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "key_constraints": self.key_constraints,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnchorSummary":
        return cls(
            compressed_at=data.get("compressed_at", ""),
            messages_compressed=data.get("messages_compressed", 0),
            tokens_before=data.get("tokens_before", 0),
            tokens_after=data.get("tokens_after", 0),
            key_constraints=data.get("key_constraints", []),
            summary=data.get("summary", ""),
        )

    def format_for_prompt(self) -> str:
        """将 Anchor 格式化为可注入 system prompt 的文本块。"""
        parts = []
        if self.summary:
            parts.append(f"对话摘要：\n{self.summary}")
        if self.key_constraints:
            constraints_text = "\n".join(f"- {c}" for c in self.key_constraints)
            parts.append(f"用户明确约束（必须严格遵守）：\n{constraints_text}")
        return "\n\n".join(parts)

    def estimate_tokens(self) -> int:
        """粗估 Anchor 的 token 数量（用于预算计算）。"""
        text = self.format_for_prompt()
        return max(1, int(len(text) / _CHARS_PER_TOKEN_ESTIMATE))


def build_context_compaction_event(
    anchor: AnchorSummary,
    *,
    source: str,
) -> Dict[str, Any]:
    """Project a saved Anchor into the immutable, user-visible timeline event.

    The event deliberately snapshots the full summary and every explicit
    constraint.  Later compactions may replace the session's current anchor,
    but must never rewrite a past conversation event.
    """
    if source not in {"manual", "automatic"}:
        raise ValueError(f"Unsupported compaction event source: {source}")

    return {
        "event_id": f"ctxcmp_{uuid4().hex}",
        "source": source,
        "occurred_at": anchor.compressed_at or datetime.now(timezone.utc).isoformat(),
        "messages_compressed": max(0, int(anchor.messages_compressed)),
        "tokens_before": max(0, int(anchor.tokens_before)),
        "tokens_after": max(0, int(anchor.tokens_after)),
        "summary": str(anchor.summary or ""),
        "key_constraints": [
            str(constraint).strip()
            for constraint in (anchor.key_constraints or [])
            if str(constraint).strip()
        ],
    }




def _count_tokens(text: str, model: str = "gpt-4o") -> int:
    """精确计算 token 数，失败时降级为字符估算。"""
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model)
        return len(enc.encode(text))
    except Exception:
        return max(1, int(len(text) / _CHARS_PER_TOKEN_ESTIMATE))


def _format_messages_for_compression(
    messages: List[Dict[str, Any]],
) -> str:
    """将消息列表格式化为压缩提示词中的对话文本。"""
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = str(msg.get("content", "")).strip()
        if not content:
            continue
        role_label = "用户" if role == "user" else "助手"
        parts.append(f"{role_label}：{content}")
    return "\n\n".join(parts)


# 用户的「硬约束」动词/短语标记：命中这些词的 user 消息片段才被视为用户自己确定的约束。
# 只做兜底（在 LLM 提取的 key_constraints 之外再补），不替换 LLM 的摘要判断。
_HARD_CONSTRAINT_MARKERS = (
    "必须",
    "一定要",
    "绝不能",
    "不能",
    "不可以",
    "不允许",
    "不要",
    "千万别",
    "不吃",
    "吃不了",
    "禁止",
    "硬性要求",
    "硬约束",
    "每晚",
    "只去",
    "只吃",
    "只住",
)


def _scan_user_hard_constraints(messages: List[Dict[str, Any]]) -> List[str]:
    """自底向上兜底：从**用户**写下的消息里，扫描显式硬约束片段。

    只读 role=user 的内容（绝不用助手的话当作用户约束）。把命中了硬约束标记词
    （必须/不能/不要/不吃/硬约束/每晚…）的片段收进来，作为 LLM 提取结果之外的第二道
    保真：即使模型压缩时漏了早先消息里的约束，这里也能补回。每条尽量保留用户原话。
    """
    found: List[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        # 按常见断句符切出候选片段（保留一个片段内的多个约束子句）
        fragments = re.split(r"[。！？！\n]", content)
        for frag in fragments:
            frag = frag.strip(" \t*【】#-·,，")
            if not frag:
                continue
            if any(marker in frag for marker in _HARD_CONSTRAINT_MARKERS):
                if frag not in found:
                    found.append(frag)
    return found


def _merge_user_constraints(base: List[str], additional: List[str]) -> List[str]:
    """合并保真约束：去重（子串/相等都算），base 优先，追加补漏的。"""
    merged: List[str] = []
    def _dup(text: str) -> bool:
        t = text.strip()
        return any(t == m or t in m or m in t for m in merged if m)
    for item in [*base, *additional]:
        s = item.strip()
        if not s or _dup(s):
            continue
        merged.append(s)
    return merged


class ContextCompressor:
    """
    上下文压缩器。

    使用 Fast LLM 将对话历史压缩为 AnchorSummary，
    压缩后的 Anchor 由 ContextBuilder v3 注入 system prompt。
    """

    async def compress(
        self,
        messages: List[Dict[str, Any]],
        existing_anchor: Optional[AnchorSummary] = None,
        model: str = "gpt-4o",
    ) -> AnchorSummary:
        """
        调用 Fast LLM 将消息列表压缩为 AnchorSummary。

        Args:
            messages: user + assistant 消息列表（不含工具结果）
            existing_anchor: 如果会话已有之前的压缩摘要，传入以便合并
            model: 用于 token 计数的模型名称

        Returns:
            AnchorSummary 压缩摘要
        """
        from ..models.router import get_model_router

        if not messages:
            return AnchorSummary(
                compressed_at=datetime.now(timezone.utc).isoformat(),
                messages_compressed=0,
                tokens_before=0,
                tokens_after=0,
            )

        # 计算压缩前 token 数
        conversation_text = _format_messages_for_compression(messages)
        tokens_before = _count_tokens(conversation_text, model)

        # 构建压缩提示词
        existing_anchor_section = ""
        if existing_anchor:
            existing_anchor_section = (
                f"已有的历史压缩摘要（请将新信息合并进去）：\n"
                f"{existing_anchor.format_for_prompt()}\n\n"
            )

        user_content = _COMPRESS_USER_TEMPLATE.format(
            existing_anchor_section=existing_anchor_section,
            message_count=len(messages),
            conversation_text=conversation_text,
        )

        compress_messages = [
            {"role": "system", "content": _COMPRESS_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        # 调用 Fast LLM
        router = get_model_router()
        llm = router.get_fast()

        try:
            response_text = await llm.ainvoke(compress_messages)
            parsed = safe_parse_json(response_text)
        except Exception as e:
            logger.error(f"ContextCompressor: LLM 调用失败: {e}")
            parsed = None

        if not parsed:
            logger.warning("ContextCompressor: 无法解析 LLM 响应，生成降级摘要")
            summary_text = conversation_text[:800] + ("..." if len(conversation_text) > 800 else "")
            parsed = {
                "key_constraints": [],
                "summary": f"（摘要生成失败，原始对话片段）{summary_text}",
            }

        key_constraints = parsed.get("key_constraints", [])
        if not isinstance(key_constraints, list):
            key_constraints = []

        # 兜底保真：LLM 提取的 key_constraints 之外，再补上从用户消息里扫到的显式硬约束，
        # 避免压缩时把用户早先的硬约束丢掉。
        key_constraints = _merge_user_constraints(
            key_constraints,
            _scan_user_hard_constraints(messages),
        )

        summary = parsed.get("summary", "")
        if not isinstance(summary, str):
            summary = str(summary)

        anchor = AnchorSummary(
            compressed_at=datetime.now(timezone.utc).isoformat(),
            messages_compressed=len(messages),
            tokens_before=tokens_before,
            tokens_after=_count_tokens(summary + " ".join(key_constraints), model),
            key_constraints=key_constraints,
            summary=summary,
        )

        logger.info(
            f"ContextCompressor: 压缩完成 {len(messages)} 条消息, "
            f"token {tokens_before} → {anchor.tokens_after} "
            f"({anchor.tokens_after / max(1, tokens_before) * 100:.0f}%)"
        )
        return anchor
