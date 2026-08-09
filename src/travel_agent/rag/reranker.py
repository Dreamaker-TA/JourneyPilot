"""
Reranking 精排模块（Step 4: 二阶段精排）

架构：策略模式
  CrossEncoderReranker — BGE-Reranker-v2-m3（需要 pip install sentence-transformers）
                         本地 ONNX-like 推理，零成本高精度
  LLMReranker          — 复用现有 LLM 做相关性评分，开箱即用

初检（Hybrid Search）top_k=20 保证召回，精排（Reranker）top_k=5 保证精度。
策略模式允许在 cross_encoder 和 llm 之间通过配置切换，无需改代码。
关键取舍：cross-encoder 对 20 个文档约需 200ms（CPU），我们在 fast_answer
路径关闭 reranking，knowledge_agent 路径开启，根据延迟 SLO 动态适配。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class Reranker(ABC):
    """Reranker 抽象基类（策略接口）"""

    @abstractmethod
    async def rerank(
        self,
        query: str,
        docs: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """
        对候选文档精排。
        Args:
            query: 用户查询
            docs: 候选文档列表（每项含 content, score 等字段）
            top_k: 精排后返回数量
        Returns:
            精排后的文档列表，按相关性降序，每项新增 rerank_score 字段
        """
        ...


# ---------------------------------------------------------------------------
# CrossEncoderReranker — BGE-Reranker-v2-m3（本地推理）
# ---------------------------------------------------------------------------

class CrossEncoderReranker(Reranker):
    """
    基于 sentence-transformers CrossEncoder 的精排器。
    使用 BGE-Reranker-v2-m3，首次调用时自动从 HuggingFace 下载（约 1.1GB）。

    需要安装：pip install sentence-transformers
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3") -> None:
        self._model_name = model_name
        self._model = None  # 延迟加载

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
                logger.info(f"加载 CrossEncoder Reranker: {self._model_name}（首次加载可能需要下载模型）")
                self._model = CrossEncoder(self._model_name)
                logger.info("CrossEncoder Reranker 加载完成")
            except ImportError as e:
                raise ImportError(
                    "CrossEncoderReranker 需要安装 sentence-transformers：\n"
                    "  pip install sentence-transformers\n"
                    "或在 config.yaml 中将 rerank.provider 设置为 'llm'"
                ) from e
        return self._model

    def _predict_sync(self, model, pairs: List[tuple]) -> List[float]:
        """在线程中同步执行，避免阻塞 event loop"""
        scores = model.predict(pairs)
        return [float(s) for s in scores]

    async def rerank(
        self,
        query: str,
        docs: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        if not docs:
            return docs

        try:
            model = self._get_model()
            pairs = [(query, doc.get("original_content") or doc["content"]) for doc in docs]

            # 在线程池中运行（CPU 密集型，避免阻塞 event loop）
            loop = asyncio.get_event_loop()
            scores = await loop.run_in_executor(None, self._predict_sync, model, pairs)

            scored_docs = []
            for doc, score in zip(docs, scores):
                new_doc = dict(doc)
                new_doc["rerank_score"] = score
                scored_docs.append(new_doc)

            sorted_docs = sorted(scored_docs, key=lambda x: x["rerank_score"], reverse=True)
            logger.info(
                f"CrossEncoder Reranking: {len(docs)} → {min(top_k, len(docs))} 个文档，"
                f"top score: {sorted_docs[0]['rerank_score']:.4f}"
            )
            return sorted_docs[:top_k]

        except ImportError:
            logger.warning("sentence-transformers 未安装，降级到 LLM Reranker")
            fallback = LLMReranker()
            return await fallback.rerank(query, docs, top_k)
        except Exception as e:
            logger.warning(f"CrossEncoder reranking 失败，使用原始排序: {e}")
            return docs[:top_k]


# ---------------------------------------------------------------------------
# LLMReranker — LLM 相关性评分（开箱即用）
# ---------------------------------------------------------------------------

_LLM_RERANK_PROMPT = """你是一个文档相关性评估专家。请评估以下每个文档与用户查询的相关性，给出1-5分（5分最相关）。

用户查询：{query}

文档列表：
{doc_list}

评分标准：
5分 - 文档直接回答了查询，信息高度匹配
4分 - 文档包含查询相关的有用信息
3分 - 文档与查询有一定关联，但不够直接
2分 - 文档仅有轻微关联
1分 - 文档与查询无关

请输出 JSON 数组（只输出数组，不要解释）：
[{{"id": 1, "score": 5}}, {{"id": 2, "score": 3}}, ...]"""


class LLMReranker(Reranker):
    """
    基于 LLM 的相关性评分精排器（fallback / 默认方案）。
    复用现有 fast_model 做批量评分，单次 LLM 调用处理所有候选文档。
    """

    async def rerank(
        self,
        query: str,
        docs: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        if not docs:
            return docs

        try:
            from ..models.router import get_model_router
            router = get_model_router()
            llm = router.get_fast()

            # 构建文档列表（使用 original_content 优先，避免 LLM 看到上下文前缀）
            doc_items = []
            for i, doc in enumerate(docs, 1):
                content = doc.get("original_content") or doc["content"]
                doc_items.append(f"[{i}] {content[:300]}")
            doc_list = "\n".join(doc_items)

            prompt = _LLM_RERANK_PROMPT.format(query=query, doc_list=doc_list)

            response = await llm.ainvoke([{"role": "user", "content": prompt}])
            response = response.strip()

            # 注：此处 LLM 返回 JSON 数组，不适用 safe_parse_json（仅支持 dict）
            import json
            import re
            if "```" in response:
                m = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
                if m:
                    response = m.group(1).strip()

            scores_data = json.loads(response)
            score_map = {item["id"]: float(item["score"]) for item in scores_data}

            scored_docs = []
            for i, doc in enumerate(docs, 1):
                new_doc = dict(doc)
                new_doc["rerank_score"] = score_map.get(i, 3.0)
                scored_docs.append(new_doc)

            sorted_docs = sorted(scored_docs, key=lambda x: x["rerank_score"], reverse=True)
            logger.info(
                f"LLM Reranking: {len(docs)} → {min(top_k, len(docs))} 个文档，"
                f"top score: {sorted_docs[0]['rerank_score']:.1f}"
            )
            return sorted_docs[:top_k]

        except Exception as e:
            logger.warning(f"LLM reranking 失败，使用原始排序: {e}")
            return docs[:top_k]


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

_reranker_instance: Optional[Reranker] = None


def get_reranker() -> Optional[Reranker]:
    """根据配置获取 Reranker 实例（单例）"""
    global _reranker_instance
    if _reranker_instance is not None:
        return _reranker_instance

    from ..config import get_settings
    settings = get_settings()
    cfg = settings.rerank

    if not cfg.enabled:
        return None

    if cfg.provider == "cross_encoder":
        _reranker_instance = CrossEncoderReranker(model_name=cfg.model_name)
    elif cfg.provider == "llm":
        _reranker_instance = LLMReranker()
    else:
        logger.warning(f"未知 reranker provider '{cfg.provider}'，使用 LLM Reranker")
        _reranker_instance = LLMReranker()

    logger.info(f"Reranker 初始化: provider={cfg.provider}, enabled={cfg.enabled}")
    return _reranker_instance
