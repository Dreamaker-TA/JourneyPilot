"""LLM JSON 响应的容错解析工具。"""

import json
import re
from typing import Any, Dict, Optional


def strip_think_blocks(text: str) -> str:
    """清理 <think>...</think> 推理块。"""
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


def _repair_json(raw: str) -> str:
    """移除尾逗号（LLM 常见格式错误）。"""
    return re.sub(r",\s*([}\]])", r"\1", raw)


def _try_load(raw: str, enable_repair: bool) -> Optional[Dict[str, Any]]:
    """单段文本的 json.loads 尝试;开启 repair 时失败后再试一次去尾逗号。"""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    if enable_repair:
        try:
            return json.loads(_repair_json(raw))
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def safe_parse_json(
    text: str,
    *,
    strip_think_tags: bool = False,
    enable_repair: bool = False,
    require_fields: tuple = (),
) -> Optional[Dict[str, Any]]:
    """
    容错解析 LLM 返回的 JSON 文本。

    三级解析：直接 json.loads、Markdown JSON 代码块、最外层对象。
    该工具只用于调用节点的当前模型输出边界，不读取历史行程协议。
    """
    text = (text or "").strip()
    if not text:
        return None

    if strip_think_tags:
        text = strip_think_blocks(text)

    def _validate(parsed: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(parsed, dict):
            return None
        if require_fields and not any(key in parsed for key in require_fields):
            return None
        return parsed

    parsed = _validate(_try_load(text, enable_repair))
    if parsed is not None:
        return parsed

    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if match:
        parsed = _validate(_try_load(match.group(1).strip(), enable_repair))
        if parsed is not None:
            return parsed

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        parsed = _validate(_try_load(match.group(0), enable_repair))
        if parsed is not None:
            return parsed

    return None
