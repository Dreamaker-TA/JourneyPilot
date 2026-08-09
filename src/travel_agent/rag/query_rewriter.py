"""
RAG Query Rewriter (Application Layer)

将用户口语化查询改写为检索友好的形式，提升 RAG 召回质量。

主要功能：
1. 单路改写：将口语化查询改写为关键词密集的检索 Query
2. 多路改写：生成多个改写变体，合并多路召回结果去重
3. HyDE（Hypothetical Document Embedding）：生成假设性回答文档，
   用其 embedding 作为检索向量，桥接短 query 与长文档的语义鸿沟

Query Rewriting 和 HyDE 是互补的：前者优化关键词层面的匹配，后者优化语义匹配。
HyDE 的思路来自 "Precise Zero-Shot Dense Retrieval without Relevance Labels"，
通过让 LLM 先生成"假设性答案"，使检索向量更接近文档空间（而非问题空间）。
知识库路径串联使用（query → rewrite → hyde_expand → embed → retrieve），
快速路径关闭 HyDE 以控制延迟。

使用 fast model 做改写（成本极低），在 knowledge_agent 和 fast_answer 的 RAG 检索前触发。

**改写与 HyDE 只取决于 query，与检索哪个集合无关**，所以它们由
``rag.retrieval_pipeline.expand_query`` 每条 query 调用一次，绝不放进集合循环里
（旧代码按四个集合循环调用，同一条 query 被改写 4 次、HyDE 4 次，其中三组是纯重复，
且空集合也照付这笔钱）。

示例：
  输入："去东京玩有啥要注意的"
  输出：["东京 旅行 注意事项 签证 交通 文化禁忌 礼仪",
         "东京旅游 入境要求 交通方式 当地习俗",
         "Tokyo travel tips customs transportation visa requirements"]
"""

from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 改写 Prompt
# ---------------------------------------------------------------------------

_SINGLE_REWRITE_PROMPT = """你是一个 RAG 检索专家。你的任务是将用户的口语化旅行问题改写为更适合知识库检索的查询词。

改写原则：
1. 提取核心实体（目的地、时间、类型等）
2. 扩充相关关键词（如签证→入境要求、visa、许可证）
3. 去除口语化词语（"有啥"→"注意事项"）
4. 保持简洁（20-40 字符）
5. 直接输出改写后的查询词，不要解释

用户问题：{query}

改写后的检索查询："""

_MULTI_REWRITE_PROMPT = """你是一个 RAG 检索专家。为提高知识库检索的召回率，请为以下旅行问题生成 3 个不同角度的检索查询。

每个查询应该：
- 聚焦不同的检索角度（如：实用信息、文化背景、交通住宿）
- 使用不同的关键词组合
- 包含中英文关键词（旅行领域很多专有名词有英文形式）

用户问题：{query}

请以 JSON 数组格式输出 3 个检索查询（只输出 JSON 数组）：
["查询1", "查询2", "查询3"]"""

_HYDE_PROMPT = """你是一本旅行百科全书。请根据以下旅行问题，写一段100-200字的权威性说明文字，
就像这道题目的标准答案已经存在于知识库中一样。

要求：
- 使用客观描述性语言（不用"您"、"您应该"等主观语气）
- 包含具体的事实性信息（地名、政策、数据等）
- 覆盖问题的核心维度

旅行问题：{query}

权威说明："""


# ---------------------------------------------------------------------------
# QueryRewriter 类
# ---------------------------------------------------------------------------

class QueryRewriter:
    """
    RAG 查询改写器。

    支持三种模式：
    - single_rewrite(): 单路改写，返回一个优化后的查询
    - multi_rewrite(): 多路改写，返回多个查询变体
    - hyde_expand(): HyDE，生成假设性文档，用其 embedding 代替 query embedding

    设计为非阻塞降级：改写失败时回退到原始查询，不影响检索流程。
    """

    async def single_rewrite(self, query: str) -> str:
        """
        单路改写：将口语化查询改写为检索友好形式。
        失败时返回原始查询（降级）。
        """
        if not query or len(query.strip()) < 3:
            return query

        try:
            from ..models.router import get_model_router
            router = get_model_router()
            llm = router.get_fast()

            prompt = _SINGLE_REWRITE_PROMPT.format(query=query.strip())
            response = await llm.ainvoke([{"role": "user", "content": prompt}])
            rewritten = response.strip()

            if rewritten and len(rewritten) > 3:
                logger.debug(f"QueryRewriter 单路改写: '{query[:30]}' → '{rewritten[:50]}'")
                return rewritten
        except Exception as e:
            logger.debug(f"QueryRewriter 单路改写失败（降级到原始查询）: {e}")

        return query

    async def multi_rewrite(self, query: str, num_variants: int = 3) -> List[str]:
        """
        多路改写：生成多个查询变体，用于多路召回。
        始终包含原始查询，其余为改写变体。
        失败时只返回原始查询（降级）。
        """
        if not query or len(query.strip()) < 3:
            return [query]

        try:
            import json
            from ..models.router import get_model_router
            router = get_model_router()
            llm = router.get_fast()

            prompt = _MULTI_REWRITE_PROMPT.format(query=query.strip())
            response = await llm.ainvoke([{"role": "user", "content": prompt}])
            response = response.strip()

            # 注：此处 LLM 返回 JSON 数组，不适用 safe_parse_json（仅支持 dict）
            if "```" in response:
                import re
                m = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
                if m:
                    response = m.group(1).strip()

            variants = json.loads(response)
            if isinstance(variants, list):
                # 过滤空字符串，确保不超过 num_variants
                valid = [v for v in variants if isinstance(v, str) and v.strip()][:num_variants]
                if valid:
                    # 始终把原始查询加入，去重后返回
                    all_queries = list(dict.fromkeys([query] + valid))
                    logger.info(f"QueryRewriter 多路改写: '{query[:30]}' → {len(all_queries)} 个变体")
                    return all_queries
        except Exception as e:
            logger.debug(f"QueryRewriter 多路改写失败（降级到原始查询）: {e}")

        return [query]

    async def hyde_expand(self, query: str) -> str:
        """
        HyDE（Hypothetical Document Embedding）扩展。

        让 LLM 生成一段"假设性答案文档"，用该文档的 embedding 作为检索向量，
        而非直接用短 query embedding 检索。

        设计思路：
          - 问题空间（query）的 embedding 与文档空间（long-form answers）之间
            存在语义鸿沟，导致相似度偏低
          - 假设性文档在文体和内容上更接近知识库中的真实文档
          - 适合旅行领域中"如何...""什么是..."等知识密集型查询

        局限性：
          - 对开放式或歧义性问题，可能生成不够准确的假设文档
          - 增加约 300-500ms 延迟（一次 LLM 调用）
          - 在 fast_answer 路径禁用，knowledge_agent 路径启用

        失败时返回原始查询（降级）。
        """
        if not query or len(query.strip()) < 3:
            return query

        try:
            from ..models.router import get_model_router
            router = get_model_router()
            llm = router.get_fast()

            prompt = _HYDE_PROMPT.format(query=query.strip())
            hypothetical_doc = await llm.ainvoke([{"role": "user", "content": prompt}])
            hypothetical_doc = hypothetical_doc.strip()

            if hypothetical_doc and len(hypothetical_doc) > 20:
                logger.debug(
                    f"HyDE: '{query[:30]}' → 假设文档 {len(hypothetical_doc)} 字: "
                    f"'{hypothetical_doc[:60]}...'"
                )
                return hypothetical_doc
        except Exception as e:
            logger.debug(f"HyDE expand 失败（降级到原始查询）: {e}")

        return query


__all__ = ["QueryRewriter"]
