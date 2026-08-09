"""
Corrective RAG — 检索质量评估与纠正路由（Step 5）

实现 CRAG（Corrective Retrieval Augmented Generation）模式：
  检索完成后，LLM 对每个文档打相关性分数（1-5），
  根据平均分路由到三条策略：高质量直接使用、中等过滤低分、低质量触发重检。

研究显示约 15-30% 的 RAG 检索结果与查询不相关，这些无关文档会"污染"上下文，
诱导 LLM 产生幻觉。CRAG 在检索和生成之间加入质量门：不相关文档被过滤，
质量太差时触发 query 重写 + 重检（最多 1 次，避免无限循环）。
关键设计取舍：Grading 本身有 LLM 调用开销（约 300-500ms），仅在 knowledge_agent
等深度路径开启，fast_answer 路径不使用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 路由策略枚举
# ---------------------------------------------------------------------------

class GradeRoute(str, Enum):
    """整批检索的**总体**质量档，用来决定要不要重写 + 重检。

    **它不决定哪几段能用** —— 相关性是逐段的性质，由 ``filtered_docs`` 回答。
    此前 ``LOW_QUALITY`` 会把整批丢空，于是「四个出厂集合各回一段不相关的 +
    用户自己那段逐字回答问题的」这种批次（平均分 1.8）被整批丢掉，**分最高的那段
    5 分正文和垃圾一起走**。而候选放得越宽、垃圾越多，平均分越低 —— 一条本来该
    被读到的正文，会因为旁边多了几段无关的而落选。
    """

    HIGH_QUALITY  = "high_quality"   # avg >= 4.0：整批质量好
    MEDIUM        = "medium"         # avg 2.0-4.0：一般
    LOW_QUALITY   = "low_quality"    # avg < 2.0：整批差，值得重写 + 重检


# ---------------------------------------------------------------------------
# 评分结果数据类
# ---------------------------------------------------------------------------

@dataclass
class GradeResult:
    route: GradeRoute
    filtered_docs: List[Dict[str, Any]]
    """逐段判过、判定相关（≥3 分）的那几段，按分数从高到低。

    **不含任何兜底**：一段都没过就是空表。此前 MEDIUM 那一支写着「防止全部过滤」，
    在没有一段合格时把整批原样交出去 —— 那正好是这个字段唯一要拦的东西
    （逐段判过都不相关，却因为一句兜底进了 prompt）。
    """
    avg_score: float
    doc_scores: List[float]
    reasoning: str = ""
    graded: bool = True
    """评分是否真的发生过。

    评分失败时本类降级为「全部使用」，这对深度路径是对的（RAG 文本在那里只是
    worker 的上下文，后面还有准入与出处校验）。但快速路径把这些文本直接注进
    用户看到的答案，且分级器是它唯一的相关性防线——所以调用方必须能分辨
    「判过，判它相关」和「没判成」。名字比 ``reasoning`` 里的一句中文可靠。
    """


# ---------------------------------------------------------------------------
# 评分 Prompt
# ---------------------------------------------------------------------------

_GRADE_PROMPT = """你是一个 RAG 检索质量评估专家。请评估以下每个文档与用户查询的相关性。

用户查询：{query}

文档列表：
{doc_list}

评分标准：
5分 - 文档直接回答了查询中的关键问题，信息高度匹配
4分 - 文档包含与查询相关的有用背景信息
3分 - 文档与查询话题有关，但回答价值有限
2分 - 文档仅有少量相关词汇，主要内容偏离查询
1分 - 文档与查询完全不相关

请输出 JSON 对象（只输出 JSON，不要解释）：
{{
  "scores": [{{"id": 1, "score": 5, "reason": "简短理由"}}, ...],
  "overall_assessment": "整体评估一句话"
}}"""


# ---------------------------------------------------------------------------
# RetrievalGrader
# ---------------------------------------------------------------------------

def _by_score_desc(
    docs: List[Dict[str, Any]],
    doc_scores: List[float],
    *,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    """按评分从高到低取文档（稳定：同分保持原顺序），可顺带滤掉低分的。"""

    kept = [
        (score, index, doc)
        for index, (doc, score) in enumerate(zip(docs, doc_scores))
        if score >= min_score
    ]
    kept.sort(key=lambda item: (-item[0], item[1]))
    return [doc for _, _, doc in kept]


class RetrievalGrader:
    """
    RAG 检索质量评估器（CRAG 核心组件）。

    使用 fast_model 对检索文档批量打分，根据分数执行三路路由：
      HIGH_QUALITY: 全部文档使用，直接进入生成
      MEDIUM:       过滤 score < 3 的文档，用剩余高质量文档生成
      LOW_QUALITY:  触发 query 重写 + 重新检索（caller 负责执行）
    """

    async def grade(
        self,
        query: str,
        docs: List[Dict[str, Any]],
        high_threshold: float = 4.0,
        low_threshold: float = 2.0,
    ) -> GradeResult:
        """
        评估检索文档质量并路由。

        Args:
            query: 原始用户查询
            docs: 检索到的文档列表
            high_threshold: 平均分 >= 此值 → HIGH_QUALITY
            low_threshold: 平均分 < 此值 → LOW_QUALITY

        Returns:
            GradeResult: 包含路由策略、过滤后的文档和评分详情
        """
        if not docs:
            return GradeResult(
                route=GradeRoute.LOW_QUALITY,
                filtered_docs=[],
                avg_score=0.0,
                doc_scores=[],
                reasoning="无检索结果",
            )

        try:
            doc_scores, overall = await self._llm_grade(query, docs)
        except Exception as e:
            logger.warning(f"RetrievalGrader: LLM 评分失败（降级：全部使用）: {e}")
            return GradeResult(
                route=GradeRoute.HIGH_QUALITY,
                filtered_docs=docs,
                avg_score=3.0,
                doc_scores=[3.0] * len(docs),
                reasoning="评分失败，降级全部使用",
                graded=False,
            )

        avg_score = sum(doc_scores) / len(doc_scores) if doc_scores else 0.0

        # 能用哪几段：**逐段判定，与整批的平均分无关**，按分数从高到低交出去
        # （调用方还要按 prompt 预算截一刀，按进来的顺序截等于让一个与相关性无关的
        # 东西决定谁进 prompt；同分保持原顺序，所以不引入第二种排序口径）。
        filtered_docs = _by_score_desc(docs, doc_scores, min_score=3.0)

        # 整批质量档：只回答「值不值得重写 + 重检」，不再决定哪几段能用。
        if avg_score >= high_threshold:
            route = GradeRoute.HIGH_QUALITY
        elif avg_score >= low_threshold:
            route = GradeRoute.MEDIUM
        else:
            route = GradeRoute.LOW_QUALITY

        logger.info(
            f"CRAG Grading: query='{query[:40]}', "
            f"docs={len(docs)}, avg_score={avg_score:.2f}, "
            f"route={route.value}, after_filter={len(filtered_docs)}"
        )

        return GradeResult(
            route=route,
            filtered_docs=filtered_docs,
            avg_score=avg_score,
            doc_scores=doc_scores,
            reasoning=overall,
        )

    async def _llm_grade(
        self,
        query: str,
        docs: List[Dict[str, Any]],
    ) -> Tuple[List[float], str]:
        """调用 LLM 进行批量评分"""
        from ..models.router import get_model_router
        from ..utils.json_helpers import safe_parse_json

        router = get_model_router()
        llm = router.get_fast()

        doc_items = []
        for i, doc in enumerate(docs, 1):
            content = doc.get("original_content") or doc["content"]
            doc_items.append(f"[{i}] {content[:250]}")
        doc_list = "\n\n".join(doc_items)

        prompt = _GRADE_PROMPT.format(query=query, doc_list=doc_list)
        response = await llm.ainvoke([{"role": "user", "content": prompt}])

        data = safe_parse_json(response)
        if not data:
            raise ValueError("RetrievalGrader: LLM 返回无法解析为 JSON")
        scores_data = data.get("scores", [])
        overall = data.get("overall_assessment", "")

        score_map = {item["id"]: float(item["score"]) for item in scores_data}
        doc_scores = [score_map.get(i, 3.0) for i in range(1, len(docs) + 1)]

        return doc_scores, overall
