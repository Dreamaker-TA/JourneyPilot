"""
Input Guardrail (Application Layer)

单层规则引擎：基于已知 Prompt 注入模式做毫秒级检测。
  - HIGH  → blocked      拒绝处理，返回标准安全回复
  - LOW   → pass_through 正常处理

早期版本曾保留一个 MEDIUM + 二阶段 LLM 分类器的路径，但实际调用方从未消费
`defense_prompt`，维持该分支只会把 LLM 开销与"双层防护"文案的幻象留下；
因此审查后合并为 LOW/HIGH 二元判定。

边界：
- 本门卫是 **正则/启发式**，可被同义改写、拆词、编码绕过；不是语义分类器。
- Deep Research 交付正文 **不** 经 OutputGuard 全文扫描；用户可见叙述来自
  Bundle 投影 + StreamingStripper。签证/医疗等高风险免责声明由报告投影文案承担，
  而非再跑一遍 OutputGuard（避免误杀行程正文）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 风险等级
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    LOW = "low"
    HIGH = "high"


@dataclass
class InputGuardResult:
    risk_level: RiskLevel
    reason: str
    cleaned_message: str
    blocked_reply: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.risk_level == RiskLevel.HIGH


# ---------------------------------------------------------------------------
# 规则引擎：已知 Prompt 注入模式（中英文混合）
# ---------------------------------------------------------------------------

_HIGH_RISK_PATTERNS: List[Tuple[str, str]] = [
    # 高风险：明确指令覆盖
    (r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?|directions?)",
     "检测到指令覆盖尝试（ignore previous instructions）"),
    (r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)",
     "检测到指令忽略尝试（disregard）"),
    (r"forget\s+(all\s+)?(previous|prior|your)\s+(instructions?|training|constraints?)",
     "检测到训练数据篡改尝试"),
    (r"(you are|you're)\s+(now|actually|really)\s+(a|an)\s+\w+",
     "检测到角色替换注入"),
    (r"(act|pretend|roleplay|behave)\s+as\s+(if\s+)?(you\s+(are|were)|a|an)\s+\w+",
     "检测到角色扮演注入"),
    # 系统 prompt 探测
    (r"(print|show|reveal|display|output|repeat|tell me)\s+(your|the)\s+(system\s+prompt|instructions?|training|constraints?|rules?)",
     "检测到系统 prompt 泄露探测"),
    (r"what\s+(are|is)\s+your\s+(system\s+prompt|hidden\s+instructions?|base\s+prompt)",
     "检测到系统 prompt 探测"),
    # 中文变体
    (r"(忘记|忽略|不用管|无视).{0,20}(之前|之前所有|前面|以前).{0,20}(指令|命令|规则|提示|要求)",
     "检测到中文指令覆盖尝试"),
    (r"你(现在|实际上|其实|真正).{0,10}(是|扮演|充当|变成).{0,10}(一个|一名|\w+机器人|\w+助手)",
     "检测到中文角色替换注入"),
    (r"(打印|显示|输出|告诉我|说出).{0,10}(系统提示|system prompt|隐藏指令|你的指令|初始指令)",
     "检测到中文系统 prompt 探测"),
    # 越狱 / 模式切换
    (r"\bDAN\b|\bjailbreak\b|\bDo Anything Now\b",
     "检测到越狱尝试（DAN 模式）"),
    (r"(developer|debug|admin|god)\s+mode",
     "检测到模式切换注入"),
    (r"(override|bypass|circumvent|evade)\s+(the\s+)?(safety|security|filter|restriction|guardrail)",
     "检测到安全绕过尝试"),
    # common paraphrases
    (r"do\s+not\s+follow\s+(your|the|any)\s+(rules?|instructions?|guidelines?)",
     "检测到指令拒绝尝试"),
    (r"(reveal|leak|dump)\s+(the\s+)?(hidden|secret|internal)\s+(prompt|instructions?|policy)",
     "检测到隐藏指令泄露探测"),
    (r"(系统提示词|初始设定|隐藏规则).{0,12}(是什么|告诉我|输出|打印)",
     "检测到中文隐藏设定探测"),
]

_COMPILED_PATTERNS = [
    (re.compile(pattern, re.IGNORECASE | re.UNICODE), reason)
    for pattern, reason in _HIGH_RISK_PATTERNS
]

# 零宽字符（可能隐藏注入指令）
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")

# 最大允许输入长度（字符数）
_MAX_INPUT_LENGTH = 8000

_BLOCKED_REPLY = (
    "抱歉，您的请求包含不允许的内容，无法处理。"
    "如果您有旅行规划相关的问题，欢迎重新提问。"
)


def _detect_high_risk(text: str) -> Tuple[bool, str]:
    """返回 (is_high_risk, reason)；高风险则 True。"""
    if _ZERO_WIDTH_RE.search(text):
        return True, "检测到零宽字符（可能包含隐藏指令）"
    if len(text) > _MAX_INPUT_LENGTH:
        return True, f"输入超过最大允许长度（{len(text)}/{_MAX_INPUT_LENGTH}）"
    for pattern, reason in _COMPILED_PATTERNS:
        if pattern.search(text):
            return True, reason
    return False, ""


# ---------------------------------------------------------------------------
# 主入口：InputGuard
# ---------------------------------------------------------------------------

class InputGuard:
    """输入安全门卫（纯规则）。"""

    async def check(self, user_message: str) -> InputGuardResult:
        """
        检查用户输入的安全性。

        返回 InputGuardResult：
          - is_blocked=True → 拒绝处理，使用 blocked_reply 回复用户
          - 否则            → 正常处理
        """
        if not user_message or not user_message.strip():
            return InputGuardResult(
                risk_level=RiskLevel.LOW,
                reason="空消息",
                cleaned_message=user_message,
            )

        is_high, reason = _detect_high_risk(user_message)
        if is_high:
            logger.warning(f"InputGuard [HIGH]: {reason} | 输入前50字符: {user_message[:50]!r}")
            return InputGuardResult(
                risk_level=RiskLevel.HIGH,
                reason=reason,
                cleaned_message=user_message,
                blocked_reply=_BLOCKED_REPLY,
            )

        return InputGuardResult(
            risk_level=RiskLevel.LOW,
            reason="安全",
            cleaned_message=user_message,
        )
