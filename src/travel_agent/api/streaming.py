"""
流式 token 过滤器：实时剔除 HTML 注释数据块、思考块和纯文本推理块。
从 LLM 输出中过滤掉不应出现在用户可见正文中的内容。
"""

from __future__ import annotations

import re


class StreamingStripper:
    """
    流式 token 过滤器：实时剔除以下三类块，防止其出现在用户可见的正文中：
    - <!-- DESTINATION_JSON ... -->   快速模式目的地 JSON
    - <think>...</think>              模型推理/思考内容（Qwen3、QwQ 等模型会输出）
    - "Thinking Process" 等开头的纯文本推理块（部分 Qwen3 API 不输出 <think> 标签）
    """
    # 两种结构化数据注释块的 open 标签共享 -->  闭合符
    _OPEN_COMMENTS = ("<!-- DESTINATION_JSON",)
    _CLOSE_COMMENT = "-->"
    _OPEN_THINK = "<think>"
    _CLOSE_THINK = "</think>"
    # 纯文本推理块的开头关键词（大小写不敏感匹配时需正规化）
    _THINKING_MARKERS = (
        "Thinking Process",
        "thinking process",
        "Let me analyze",
        "Let me think",
        "思考过程",
    )
    # 引用在流结束后由 OutputGuard 映射为结构化角标；流式阶段整段缓冲并隐藏，
    # 避免用户短暂看到 [[fact:1]] 或 [来源：ev_...]。
    _INTERNAL_MARKER_PREFIXES = (
        "[[fact:",
        "[[fact：",
        "[来源:",
        "[来源：",
        "[source:",
        "[source：",
        "[数据来源:",
        "[数据来源：",
        "[ref:",
        "[ref：",
        "[引用:",
        "[引用：",
    )
    # 纯文本推理结束的判定：连续空行（\n\n）后紧跟以下任一内容即视为正文开始：
    #   [^\x00-\x7F] — 非 ASCII 字符（即中文/日文等）
    #   \s*#{1,3}\s   — Markdown 标题（# / ## / ###）
    #   \s*[-*]\s     — Markdown 列表项（- / *）
    #   [A-Z]         — 英文句首大写（纯英文正文，避免吞到 EOS）
    _THINKING_END_RE = re.compile(
        r'\n\n(?=[^\x00-\x7F#\-\*]|\s*#{1,3}\s|\s*[-*]\s|[A-Z])',
    )
    # Hard cap on pure-text think buffer so English prose without a blank line
    # cannot be held until stream end and then dropped by flush().
    _TEXT_THINK_MAX_CHARS = 4000
    # 需要缓冲的最大前缀长度（用于检测跨 chunk 的标签开头）
    _HOLD = max(
        max(len(tag) for tag in _OPEN_COMMENTS),
        len("<think>"),
        len("Thinking Process"),
        max(len(tag) for tag in _INTERNAL_MARKER_PREFIXES),
    )

    def __init__(self) -> None:
        self._buf = ""
        self._in_comment_block = False
        self._in_think_block = False
        self._in_text_think_block = False   # 纯文本推理块状态
        self._comment_buf = ""
        self._think_buf = ""
        self._text_think_buf = ""
        self._in_internal_marker = False
        self._internal_marker_buf = ""
        self._internal_marker_close = "]"

    def feed(self, chunk: str) -> str:
        """返回可立即显示的文本（两类注释块、think 块、纯文本推理块均被过滤）。"""
        if self._in_internal_marker:
            self._internal_marker_buf += chunk
            close_pos = self._internal_marker_buf.find(self._internal_marker_close)
            if close_pos != -1:
                remainder = self._internal_marker_buf[close_pos + len(self._internal_marker_close):]
                self._internal_marker_buf = ""
                self._in_internal_marker = False
                self._buf = ""
                return self._process(remainder)
            return ""
        if self._in_comment_block:
            self._comment_buf += chunk
            close_pos = self._comment_buf.find(self._CLOSE_COMMENT)
            if close_pos != -1:
                remainder = self._comment_buf[close_pos + len(self._CLOSE_COMMENT):]
                self._comment_buf = ""
                self._in_comment_block = False
                self._buf = ""
                return self._process(remainder)
            return ""
        if self._in_think_block:
            self._think_buf += chunk
            close_pos = self._think_buf.find(self._CLOSE_THINK)
            if close_pos != -1:
                remainder = self._think_buf[close_pos + len(self._CLOSE_THINK):]
                self._think_buf = ""
                self._in_think_block = False
                self._buf = ""
                return self._process(remainder)
            return ""
        if self._in_text_think_block:
            self._text_think_buf += chunk
            # 检测正文开始：连续空行后出现中文、Markdown 标题/列表、或英文大写句首
            m = self._THINKING_END_RE.search(self._text_think_buf)
            if m:
                remainder = self._text_think_buf[m.start():]
                self._text_think_buf = ""
                self._in_text_think_block = False
                self._buf = ""
                return self._process(remainder)
            # Force end after max buffer — treat remainder as body
            if len(self._text_think_buf) >= self._TEXT_THINK_MAX_CHARS:
                overflow = self._text_think_buf[self._TEXT_THINK_MAX_CHARS :]
                # Prefer to surface text after the last blank line as body.
                split_at = self._text_think_buf.rfind("\n\n")
                if split_at != -1 and split_at > 0:
                    remainder = self._text_think_buf[split_at:]
                else:
                    remainder = overflow or self._text_think_buf[-200:]
                self._text_think_buf = ""
                self._in_text_think_block = False
                self._buf = ""
                return self._process(remainder)
            return ""
        return self._process(chunk)

    def _find_text_thinking_marker(self, buf: str):
        """在 buf 中查找最早出现的纯文本推理块标记，返回 (pos, marker) 或 (-1, '')。"""
        earliest_pos = -1
        earliest_marker = ""
        for marker in self._THINKING_MARKERS:
            pos = buf.find(marker)
            if pos != -1 and (earliest_pos == -1 or pos < earliest_pos):
                # 仅在行首（前面是空白或文本开头）时触发，避免误伤正文中的句子
                prefix = buf[:pos]
                if not prefix or prefix[-1] in ("\n", "\r", " ", "\t") or pos == 0:
                    earliest_pos = pos
                    earliest_marker = marker
        return earliest_pos, earliest_marker

    def _find_comment_open(self, buf: str):
        """在 buf 中查找最早出现的注释块 open 标签，返回 (pos, tag) 或 (-1, '')。"""
        earliest_pos = -1
        earliest_tag = ""
        for tag in self._OPEN_COMMENTS:
            pos = buf.find(tag)
            if pos != -1 and (earliest_pos == -1 or pos < earliest_pos):
                earliest_pos = pos
                earliest_tag = tag
        return earliest_pos, earliest_tag

    def _find_internal_marker(self, buf: str):
        """查找最早的机器引用或 legacy 来源标记，英文前缀忽略大小写。"""
        lower = buf.lower()
        earliest_pos = -1
        earliest_tag = ""
        for tag in self._INTERNAL_MARKER_PREFIXES:
            pos = lower.find(tag.lower())
            if pos != -1 and (earliest_pos == -1 or pos < earliest_pos):
                earliest_pos = pos
                earliest_tag = tag
        return earliest_pos, earliest_tag

    def _process(self, text: str) -> str:
        self._buf += text

        # 找到最近出现的特殊块开头
        comment_pos, comment_tag = self._find_comment_open(self._buf)
        think_pos = self._buf.find(self._OPEN_THINK)
        text_think_pos, text_think_marker = self._find_text_thinking_marker(self._buf)
        marker_pos, marker_tag = self._find_internal_marker(self._buf)

        # 选择最早出现的块
        earliest_pos: int = -1
        earliest_type: str = ""
        if comment_pos != -1:
            earliest_pos, earliest_type = comment_pos, "comment"
        if think_pos != -1 and (earliest_pos == -1 or think_pos < earliest_pos):
            earliest_pos, earliest_type = think_pos, "think"
        if text_think_pos != -1 and (earliest_pos == -1 or text_think_pos < earliest_pos):
            earliest_pos, earliest_type = text_think_pos, "text_think"
        if marker_pos != -1 and (earliest_pos == -1 or marker_pos < earliest_pos):
            earliest_pos, earliest_type = marker_pos, "internal_marker"

        if earliest_type == "internal_marker":
            safe = self._buf[:earliest_pos]
            marker_start = self._buf[earliest_pos:earliest_pos + len(marker_tag)]
            self._internal_marker_close = "]]" if marker_start.startswith("[[") else "]"
            self._in_internal_marker = True
            self._internal_marker_buf = self._buf[earliest_pos + len(marker_tag):]
            self._buf = ""
            close_pos = self._internal_marker_buf.find(self._internal_marker_close)
            if close_pos != -1:
                remainder = self._internal_marker_buf[close_pos + len(self._internal_marker_close):]
                self._internal_marker_buf = ""
                self._in_internal_marker = False
                return safe + self._process(remainder)
            return safe

        if earliest_type == "think":
            safe = self._buf[:earliest_pos]
            self._in_think_block = True
            self._think_buf = self._buf[earliest_pos + len(self._OPEN_THINK):]
            self._buf = ""
            close_pos = self._think_buf.find(self._CLOSE_THINK)
            if close_pos != -1:
                remainder = self._think_buf[close_pos + len(self._CLOSE_THINK):]
                self._think_buf = ""
                self._in_think_block = False
                return safe + self._process(remainder)
            return safe

        if earliest_type == "comment":
            safe = self._buf[:earliest_pos]
            self._in_comment_block = True
            self._comment_buf = self._buf[earliest_pos + len(comment_tag):]
            self._buf = ""
            close_pos = self._comment_buf.find(self._CLOSE_COMMENT)
            if close_pos != -1:
                remainder = self._comment_buf[close_pos + len(self._CLOSE_COMMENT):]
                self._comment_buf = ""
                self._in_comment_block = False
                return safe + self._process(remainder)
            return safe

        if earliest_type == "text_think":
            safe = self._buf[:earliest_pos]
            self._in_text_think_block = True
            self._text_think_buf = self._buf[earliest_pos + len(text_think_marker):]
            self._buf = ""
            # 检查本 chunk 中是否已经包含结束标志
            m = self._THINKING_END_RE.search(self._text_think_buf)
            if m:
                remainder = self._text_think_buf[m.start():]
                self._text_think_buf = ""
                self._in_text_think_block = False
                return safe + self._process(remainder)
            return safe

        # 未命中任何块，检查尾部是否可能是某个标签的前缀，需要暂时保留
        all_patterns = (
            list(self._OPEN_COMMENTS)
            + [self._OPEN_THINK]
            + list(self._THINKING_MARKERS)
            + list(self._INTERNAL_MARKER_PREFIXES)
        )
        max_hold = min(self._HOLD, len(self._buf))
        best_hold = 0
        for pattern in all_patterns:
            for prefix_len in range(min(len(pattern), max_hold), 0, -1):
                if pattern.lower().startswith(self._buf[-prefix_len:].lower()):
                    best_hold = max(best_hold, prefix_len)
                    break
        if best_hold > 0:
            safe = self._buf[:-best_hold]
            self._buf = self._buf[-best_hold:]
            return safe
        safe = self._buf
        self._buf = ""
        return safe

    def flush(self) -> str:
        """流结束时调用，返回剩余可显示文本。"""
        if (
            self._in_comment_block
            or self._in_think_block
            or self._in_text_think_block
            or self._in_internal_marker
        ):
            return ""
        result = self._buf
        self._buf = ""
        return result


def strip_non_display_blocks(content: str) -> str:
    """Apply the streaming visibility contract to persisted/non-stream content."""

    stripper = StreamingStripper()
    return stripper.feed(content) + stripper.flush()
