"""
文档分块策略 (Application Layer)
将长文档分割为适合嵌入的文本块。

支持三种策略（通过 config.rag.chunker_type 选择）：
  text        — 固定滑动窗口（默认兼容模式）
  semantic    — 基于 embedding 余弦相似度检测语义边界（异步）
  contextual  — semantic + LLM 上下文前缀注入，参考 Anthropic Contextual Retrieval（异步）
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from ..config import get_settings
from ..models.router import llm_channel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模块级辅助：句子边界切分（带 char_span）
# ---------------------------------------------------------------------------

_SENTENCE_BOUNDARY = re.compile(r'(?<=[。！？!?；\n])\s*')


# ---------------------------------------------------------------------------
# 模块级辅助：逐项调模型的分块要有界
# ---------------------------------------------------------------------------


def _exceeded_model_chunking_budget(text: str) -> Optional[int]:
    """这篇正文超界了吗？超了就交出**做出这个判断的那个数**，没超交出 None。

    ``semantic`` 与 ``contextual`` 两档分块的**模型调用条数由文档大小决定**（逐句
    一次 embedding、逐段一次 LLM），所以「一次入库最多花多少」这个界必须在
    **花掉第一次调用之前**判得出来 —— 它的单位因此是字符，不是段数。规则写在这里
    一次，两个会调模型的分块器都问它；数在 ``config.RAGConfig.model_chunking_max_chars``。

    交出那个数而不是一个 bool，是为了让日志印的就是**执行时用的那一份** ——
    调用方自己再读一次配置来印，那一行随时可以和实际生效的数漂开。

    超界不是失败，是**降档**：按固定窗口分块入库，一次模型都不调。段会差一点，
    而替代方案是一个用户的一次上传把全站的模型调用排到队尾，曾让服务不可达
    7 分半并需重启。
    """

    limit = int(get_settings().rag.model_chunking_max_chars)
    return limit if len(text) > limit else None


def _split_sentences_with_spans(text: str) -> List[tuple[str, int, int]]:
    """
    按中英文句子边界切分，返回 [(sentence_text, char_start, char_end)]。
    char_start / char_end 是原文级字符偏移（strip 后的真实范围）。
    """
    result: List[tuple[str, int, int]] = []
    last = 0
    for m in _SENTENCE_BOUNDARY.finditer(text):
        end = m.start()
        if last < end:
            segment = text[last:end]
            stripped = segment.strip()
            if stripped:
                lstrip_off = len(segment) - len(segment.lstrip())
                rstrip_off = len(segment) - len(segment.rstrip())
                result.append(
                    (stripped, last + lstrip_off, end - rstrip_off)
                )
        last = m.end()
    if last < len(text):
        segment = text[last:]
        stripped = segment.strip()
        if stripped:
            lstrip_off = len(segment) - len(segment.lstrip())
            rstrip_off = len(segment) - len(segment.rstrip())
            result.append(
                (stripped, last + lstrip_off, len(text) - rstrip_off)
            )
    return result


# ---------------------------------------------------------------------------
# TextChunker — 固定窗口分块器
# ---------------------------------------------------------------------------

class TextChunker:
    """
    基于滑动窗口的文本分块器。
    支持按句子边界分块（避免截断句子）。
    同步接口，可直接在非 async 环境使用。
    """

    def __init__(
        self,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
    ) -> None:
        settings = get_settings()
        self.chunk_size = chunk_size or settings.rag.chunk_size
        self.chunk_overlap = chunk_overlap or settings.rag.chunk_overlap

    def split(
        self,
        text: str,
        source: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        将文本分割为 chunks。
        返回：[{"content": str, "source": str, "metadata": dict, "chunk_index": int}]
        metadata 内必带 char_start / char_end —— chunk 核心句子在原文中的字符偏移（不含 overlap）
        """
        if not text or not text.strip():
            return []

        meta = metadata or {}
        spans = _split_sentences_with_spans(text)
        chunks = self._merge_spans(spans)

        result = []
        for i, (content, char_start, char_end) in enumerate(chunks):
            if content.strip():
                result.append({
                    "content": content.strip(),
                    "source": source,
                    "metadata": {
                        **meta,
                        "chunk_index": i,
                        "char_start": char_start,
                        "char_end": char_end,
                    },
                    "chunk_index": i,
                })
        return result

    def _merge_spans(
        self, spans: List[tuple[str, int, int]]
    ) -> List[tuple[str, int, int]]:
        """
        将带 char_span 的句子合并为 chunk。
        返回 [(content, char_start, char_end), ...]；char_span 取构成该 chunk 的核心句子范围
        （overlap 字符附在 content 头部但不计入 char_span，避免重叠区域被多 chunk 重复声明）
        """
        chunks: List[tuple[str, int, int]] = []
        current_text = ""
        current_start: Optional[int] = None
        current_end: Optional[int] = None
        overlap_buffer = ""

        for sent_text, s_start, s_end in spans:
            # 当前句子太长单独作为 chunk
            if len(sent_text) > self.chunk_size:
                if current_text:
                    chunks.append((current_text, current_start, current_end))
                    current_text = ""
                    current_start = current_end = None
                step = max(1, self.chunk_size - self.chunk_overlap)
                for off in range(0, len(sent_text), step):
                    sub = sent_text[off : off + self.chunk_size]
                    chunks.append((sub, s_start + off, s_start + off + len(sub)))
                continue

            if len(current_text) + len(sent_text) + 1 <= self.chunk_size:
                if not current_text:
                    current_text = sent_text
                    current_start = s_start
                else:
                    current_text = (current_text + " " + sent_text).strip()
                current_end = s_end
            else:
                if current_text:
                    chunks.append((current_text, current_start, current_end))
                overlap_buffer = (
                    current_text[-self.chunk_overlap :]
                    if len(current_text) > self.chunk_overlap
                    else current_text
                )
                current_text = (overlap_buffer + " " + sent_text).strip() if overlap_buffer else sent_text
                current_start = s_start  # 新 chunk 的核心句子从这里开始
                current_end = s_end

        if current_text:
            chunks.append((current_text, current_start, current_end))

        return chunks


# ---------------------------------------------------------------------------
# SemanticChunker — 语义感知分块器（Step 1）
# ---------------------------------------------------------------------------

class SemanticChunker:
    """
    基于 embedding 余弦相似度的语义分块器。

    设计思路：
    1. 先按句子边界切分（同 TextChunker）
    2. 批量 embed 所有句子（单次 API 调用，高效）
    3. 计算相邻句子的余弦相似度
    4. 在相似度骤降处（< semantic_split_threshold）切分，标记语义边界
    5. 按 chunk_size 约束合并邻近句子

    固定长度分块经常在话题中间截断，导致 chunk 语义不完整。
    语义分块通过 embedding 相似度检测语义转换点，保证 chunk 内语义连贯。
    """

    def __init__(
        self,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
        split_threshold: Optional[float] = None,
        embedder=None,
    ) -> None:
        settings = get_settings()
        self.chunk_size = chunk_size or settings.rag.chunk_size
        self.chunk_overlap = chunk_overlap or settings.rag.chunk_overlap
        self.split_threshold = split_threshold or settings.rag.semantic_split_threshold
        self._embedder = embedder

    def _get_embedder(self):
        if self._embedder is None:
            from ..models.embedder import get_embedder
            self._embedder = get_embedder()
        return self._embedder

    async def split(
        self,
        text: str,
        source: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        异步语义分块。
        返回：[{"content": str, "source": str, "metadata": dict, "chunk_index": int}]
        metadata 内必带 char_start / char_end —— chunk 核心句子在原文中的字符偏移。
        """
        if not text or not text.strip():
            return []

        # 逐句 embedding 的条数就是句子数，所以先问那道界。
        over_budget = _exceeded_model_chunking_budget(text)
        if over_budget is not None:
            logger.warning(
                "SemanticChunker: [%s] 正文 %d 字符超过逐句 embedding 的上界 %d，"
                "本篇按固定窗口分块（零模型调用）",
                source[:40],
                len(text),
                over_budget,
            )
            return TextChunker(self.chunk_size, self.chunk_overlap).split(
                text, source, metadata
            )

        meta = metadata or {}
        spans = _split_sentences_with_spans(text)

        if not spans:
            return []

        # 单句直接返回（无需 embed）
        if len(spans) == 1:
            sent_text, s_start, s_end = spans[0]
            return [{
                "content": sent_text,
                "source": source,
                "metadata": {
                    **meta,
                    "chunk_index": 0,
                    "char_start": s_start,
                    "char_end": s_end,
                },
                "chunk_index": 0,
            }]

        # 批量 embed 所有句子（单次 API 调用，高效）
        sentences = [s for s, _, _ in spans]
        try:
            embedder = self._get_embedder()
            embeddings = await embedder.embed_batch(sentences)
            groups = self._semantic_group_with_spans(spans, embeddings)
        except Exception as e:
            logger.warning(f"SemanticChunker: embedding 失败，降级为 TextChunker: {e}")
            text_chunker = TextChunker(self.chunk_size, self.chunk_overlap)
            return text_chunker.split(text, source, metadata)

        merged_chunks = self._merge_groups_with_overlap_spans(groups)

        result = []
        for i, (content, char_start, char_end) in enumerate(merged_chunks):
            if content.strip():
                result.append({
                    "content": content.strip(),
                    "source": source,
                    "metadata": {
                        **meta,
                        "chunk_index": i,
                        "char_start": char_start,
                        "char_end": char_end,
                    },
                    "chunk_index": i,
                })
        return result

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """计算两个向量的余弦相似度"""
        import math
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _semantic_group_with_spans(
        self,
        spans: List[tuple[str, int, int]],
        embeddings: List[List[float]],
    ) -> List[List[tuple[str, int, int]]]:
        """
        根据相邻句子 embedding 相似度，将句子（带 span）分组为语义段落。
        相似度 < split_threshold 时视为语义边界，切分为新组。
        """
        groups: List[List[tuple[str, int, int]]] = [[spans[0]]]

        for i in range(1, len(spans)):
            sim = self._cosine_similarity(embeddings[i - 1], embeddings[i])
            current_group_text = " ".join(s for s, _, _ in groups[-1])

            if sim < self.split_threshold or len(current_group_text) + len(spans[i][0]) > self.chunk_size:
                groups.append([spans[i]])
            else:
                groups[-1].append(spans[i])

        return groups

    def _merge_groups_with_overlap_spans(
        self, groups: List[List[tuple[str, int, int]]]
    ) -> List[tuple[str, int, int]]:
        """
        将分组合并为带重叠的 chunk 文本，同时计算 chunk 的 char_span。
        char_span 仅覆盖组内核心句子（不含 overlap 字符），避免重叠区域被多 chunk 重复声明。
        """
        chunks: List[tuple[str, int, int]] = []
        for i, group in enumerate(groups):
            chunk_text = " ".join(s for s, _, _ in group).strip()
            char_start = group[0][1]
            char_end = group[-1][2]

            if i > 0 and self.chunk_overlap > 0:
                prev_text = " ".join(s for s, _, _ in groups[i - 1])
                overlap = (
                    prev_text[-self.chunk_overlap :]
                    if len(prev_text) > self.chunk_overlap
                    else prev_text
                )
                chunk_text = (overlap + " " + chunk_text).strip()

            chunks.append((chunk_text, char_start, char_end))
        return chunks


# ---------------------------------------------------------------------------
# ContextualChunker — Anthropic Contextual Retrieval（Step 2）
# ---------------------------------------------------------------------------

_CONTEXTUAL_PROMPT = """你是一个文档理解专家。请用1-2句简洁中文说明以下段落在原文中所属的主题和位置，帮助检索系统更好地找到这段内容。

原文标题/来源：{title}
原文开头（前300字）：{doc_beginning}

当前段落：
{chunk}

请直接输出简要说明（30-80字，不要重复段落原文，不要解释你在做什么）："""


def _chunk_without_prefix(chunk: Dict[str, Any]) -> Dict[str, Any]:
    """ContextualChunker 没能（或不该）为这一段调模型时，它交出的形状。

    ``original_content`` 照样写上：这个分块器的返回形状**只有一种**，缺了这个键就
    等于让下游去分辨「这一段有没有前缀」，而那正是「字段缺失则走旧逻辑」。
    """

    plain = dict(chunk)
    plain["original_content"] = chunk["content"]
    return plain


class ContextualChunker:
    """
    Contextual Retrieval 分块器（参考 Anthropic 2024.09 研究）。

    在 SemanticChunker（或 TextChunker）之上包装 LLM 上下文前缀注入层：
    - content（用于 embedding + lexical full-text）= 上下文前缀 + 原始内容
    - original_content（用于 UI 展示）= 原始内容

    传统 RAG 分块后，"其超过385万居民使其成为欧盟人口最多的城市"这样的 chunk
    完全失去了关于"哪座城市"的上下文，导致检索失败。Contextual Chunking 通过
    LLM 为每个 chunk 注入文档级上下文说明，Anthropic 实测：Contextual
    Embedding 单独减少检索失败 35%；配合词法全文检索进一步提升关键词命中；再加
    Rerank 形成二阶段检索。本项目走的是"Embedding + lexical full-text + Rerank"三
    项组合路径（见 retriever.py 的 hybrid + reranker 流水线）。
    关键取舍：embedding 和 lexical full-text 都使用带前缀的 content，original_content 仅
    供展示；成本控制上复用已有 fast_model 而非专用模型。

    **这一步的模型调用条数由文档大小决定，所以它自己带界**，三道：

    1. 规模 —— 正文超过 ``RAGConfig.model_chunking_max_chars`` 就一次模型都不调
       （见 ``_exceeded_model_chunking_budget``）；
    2. 并发 —— 走 ``ingest_contextual_llm`` 通道
       （``ProviderChannelConfig``，`models/router.llm_channel`）。这一档共用的是
       **全站 fast 档那一个 httpx 连接池**（``langchain_openai`` 按
       (base_url, timeout) lru_cache 一个 AsyncClient），所以「一次入库排出上万条
       并发请求」的后果不是这次入库慢，是**所有人的模型调用排到队尾**；
    3. 失败 —— 本篇累计失败到 ``RAGConfig.contextual_failure_threshold`` 条就熔断，
       其余段直接留原文。上游在限流时继续发等于替它放大。

    **重试与退避不在这里**：那是 transport 的事（openai SDK 指数退避 +
    ``max_retries``），而 transport 管不了「一次上传发几千条请求」。在这里再加一层
    退避就是同一件事的第二份账。
    """

    def __init__(self, base_chunker=None, llm=None) -> None:
        self._base_chunker = base_chunker  # 延迟初始化
        self._llm = llm

    def _get_base_chunker(self):
        if self._base_chunker is None:
            self._base_chunker = SemanticChunker()
        return self._base_chunker

    def _get_llm(self):
        if self._llm is None:
            from ..models.router import get_model_router
            router = get_model_router()
            self._llm = router.get_fast()  # fast model：低延迟低成本
        return self._llm

    async def split(
        self,
        text: str,
        source: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        异步分块 + LLM 上下文注入。
        返回：[{"content": str, "original_content": str, "source": str, "metadata": dict, "chunk_index": int}]
        """
        if not text or not text.strip():
            return []

        # 规模那道界：超界的正文一次模型都不调 —— 连基础分块也不走语义档，
        # 因为语义档自己就是逐句 embedding。
        settings = get_settings().rag
        over_budget = _exceeded_model_chunking_budget(text)
        if over_budget is not None:
            logger.warning(
                "ContextualChunker: [%s] 正文 %d 字符超过逐段调模型的上界 %d，"
                "本篇按固定窗口分块入库（零模型调用）",
                source[:40],
                len(text),
                over_budget,
            )
            plain = TextChunker().split(text, source, metadata)
            return [_chunk_without_prefix(c) for c in plain]

        # Step 1: 获取基础 chunks（语义分块或固定窗口）
        base_chunker = self._get_base_chunker()
        if hasattr(base_chunker, 'split') and asyncio.iscoroutinefunction(base_chunker.split):
            base_chunks = await base_chunker.split(text, source, metadata)
        else:
            base_chunks = base_chunker.split(text, source, metadata)

        if not base_chunks:
            return []

        # Step 2: 逐段调 LLM 生成上下文前缀 —— 有并发上限、有失败熔断。
        title = source or (metadata or {}).get("title", "本文档")
        doc_beginning = text[:300].strip()
        llm = self._get_llm()

        failure_threshold = int(settings.contextual_failure_threshold)
        failures = 0
        tripped = False

        async def _add_context(chunk: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal failures, tripped
            # 熔断后进来的段直接留原文，**不排队**：排在通道后面等一个已经决定不发的
            # 调用，等于把熔断变成「慢一点的重试风暴」。
            if tripped:
                return _chunk_without_prefix(chunk)
            original_content = chunk["content"]
            try:
                prompt = _CONTEXTUAL_PROMPT.format(
                    title=title,
                    doc_beginning=doc_beginning,
                    chunk=original_content,
                )
                # 并发上限由通道持有：`llm.ainvoke` 自己会占一个位置。
                context_prefix = await llm.ainvoke([{"role": "user", "content": prompt}])
                # 剥离 thinking 模型（MiniMax-M2.7 / Qwen3 等）输出的 <think>...</think>
                from ..utils.json_helpers import strip_think_blocks
                context_prefix = strip_think_blocks(context_prefix).strip()

                new_chunk = dict(chunk)
                new_chunk["original_content"] = original_content
                if context_prefix:
                    new_chunk["content"] = f"{context_prefix}\n\n{original_content}"
                return new_chunk
            except Exception as e:
                failures += 1
                logger.debug(
                    f"ContextualChunker: LLM 上下文生成失败（降级保留原始内容）: {e}"
                )
                if failures >= failure_threshold and not tripped:
                    tripped = True
                    logger.warning(
                        "ContextualChunker: [%s] 上下文前缀调用累计失败 %d 条，"
                        "已熔断，本篇其余段留原文入库",
                        source[:40],
                        failures,
                    )
                return _chunk_without_prefix(chunk)

        # 分批派发而不是一次 gather 全部：熔断标志只在**进入** `_add_context` 时读一次，
        # 而一次性派发会让后面所有段都已经排在通道队列里 —— 熔断之后它们照样发出去，
        # `contextual_failure_threshold` 于是只是一句日志。批大小取通道配额，通道本来
        # 就会把它们排成这个宽度，所以吞吐不变。
        wave = max(1, int(get_settings().provider_channels.ingest_contextual_llm))
        results: List[Dict[str, Any]] = []
        with llm_channel("ingest_contextual_llm"):
            for start in range(0, len(base_chunks), wave):
                batch = base_chunks[start : start + wave]
                results.extend(await asyncio.gather(*[_add_context(c) for c in batch]))
        prefixed = sum(
            1 for c in results if c["content"] != c.get("original_content")
        )
        # 这一行**报实数**：一篇 100% 失败的资料在日志里不能和一篇全部成功的
        # 长得一模一样。
        logger.info(
            "ContextualChunker: %s → %d 个 chunk，%d 段带上下文前缀（失败 %d 段%s）",
            source[:40],
            len(results),
            prefixed,
            failures,
            "，已熔断" if tripped else "",
        )
        return list(results)


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def get_chunker(chunker_type: Optional[str] = None):
    """
    根据配置获取分块器实例。
    chunker_type: "text" | "semantic" | "contextual"（None 时读 config）
    """
    settings = get_settings()
    ctype = chunker_type or settings.rag.chunker_type

    if ctype == "semantic":
        return SemanticChunker()
    elif ctype == "contextual":
        return ContextualChunker(base_chunker=SemanticChunker())
    else:
        return TextChunker()
