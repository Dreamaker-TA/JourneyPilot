"""
Output Guardrail (Application Layer)

输出侧安全校验，包含三个核心机制：

1. 来源追踪（GroundingChecker）
   - 把正文里的 [[fact:N]] 机器标记转换为可点击角标与 citation 列表
   - citation 只由本轮 verified_claims 命中的 evidence 生成，无对应 evidence 的标记被摘除
   - 拒绝旧引用形状（[来源: xxx]、裸 ev_/claim_ 内部标识）

2. 置信度评分（ConfidenceScorer）
   - 基于数据来源质量评估输出置信度
   - 工具实时数据 > RAG 召回 > LLM 知识 > 无支撑

3. 局部信息状态（InformationAnnotations）
   - 对签证、价格、班次、天气等时效内容自动附加局部标签
   - 普通建议与稳定知识不打标签，不在回答末尾追加统一免责声明

调用面：
- **Fast Answer** 主路径调用本门卫。
- **Deep Research** 不把整份 Delivery Bundle 正文再跑 OutputGuard；交付以
  确定性投影为准，高风险免责由报告模板/投影文案承担，避免误杀行程结构。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 置信度等级
# ---------------------------------------------------------------------------

class ConfidenceLevel(str, Enum):
    HIGH = "high"      # 基于工具实时数据或高分 RAG 命中
    MEDIUM = "medium"  # 基于 LLM 知识但有部分 RAG 佐证
    LOW = "low"        # 纯 LLM 推理，无外部数据支撑


@dataclass
class GroundingResult:
    """来源追踪与置信度评估结果"""
    confidence: ConfidenceLevel
    safety_disclaimer: str = ""          # 高风险话题免责声明
    processed_output: str = ""           # 最终处理后的输出文本（加了免责声明等）
    confidence_reasons: List[str] = field(default_factory=list)
    citations: List[Dict[str, Any]] = field(default_factory=list)
    annotations: List[Dict[str, Any]] = field(default_factory=list)
    final_grounding: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 局部信息状态
# ---------------------------------------------------------------------------

_TIME_SENSITIVE_RE = re.compile(
    r"(?:签证|visa|入境|出入境|immigration|许可证|permit|护照|申请表|"
    r"照片|银行流水|资产证明|在职证明|收入证明|办理材料|政策|"
    r"价格|票价|费用|汇率|班次|航班|飞行时长|飞行时间|交通时间|车程|时刻表|"
    r"营业时间|开放时间|预约|限额|有效期)",
    re.IGNORECASE,
)
_SEASONAL_REFERENCE_RE = re.compile(
    r"(?:天气|气温|温度|摄氏|°\s*[CF]|降雨|降雪|台风|雨季|旱季|花期|红叶|秋叶|银杏季|祭典|节庆)",
    re.IGNORECASE,
)
_SAFETY_REFERENCE_RE = re.compile(
    r"(?:过敏|过敏原|allergen|医疗|健康|安全|疫苗|防疫)",
    re.IGNORECASE,
)
_ANNOTATION_KINDS: Dict[str, Dict[str, str]] = {
    "time_sensitive": {
        "label": "时效信息",
        "detail": "政策、价格、班次或办理要求可能随时间和适用范围变化。",
    },
    "seasonal_reference": {
        "label": "季节参考",
        "detail": "这是季节性概况，不是具体出行日期的实时预报。",
    },
    "safety_reference": {
        "label": "安全参考",
        "detail": "涉及健康或安全，请结合个人情况与具体服务方信息核对。",
    },
}


def _annotate_information_blocks(text: str) -> tuple[str, List[Dict[str, Any]]]:
    """按 Markdown 段落添加稀疏局部状态；不判断或改写正文内容。"""
    blocks = re.split(r"(\n\s*\n)", text)
    annotations: List[Dict[str, Any]] = []
    rendered: List[str] = []
    emitted_kinds: set[str] = set()

    for block in blocks:
        if not block or re.fullmatch(r"\n\s*\n", block):
            rendered.append(block)
            continue
        if "<!--" in block:
            rendered.append(block)
            continue
        if _MACHINE_CITATION_RE.search(block):
            rendered.append(block)
            continue

        kind = ""
        if _TIME_SENSITIVE_RE.search(block):
            kind = "time_sensitive"
        elif _SEASONAL_REFERENCE_RE.search(block):
            kind = "seasonal_reference"
        elif _SAFETY_REFERENCE_RE.search(block):
            kind = "safety_reference"
        if not kind or kind in emitted_kinds:
            rendered.append(block)
            continue

        emitted_kinds.add(kind)
        annotation_id = f"annotation_{len(annotations) + 1}"
        annotation = {
            "annotation_id": annotation_id,
            "kind": kind,
            **_ANNOTATION_KINDS[kind],
        }
        annotations.append(annotation)

        lines = block.splitlines()
        target_index = next((idx for idx, line in enumerate(lines) if line.strip()), None)
        if target_index is None:
            rendered.append(block)
            continue
        if lines[target_index].lstrip().startswith("|"):
            # 不向 GFM 表格表头增加额外单元格；把状态作为紧邻表格的独立行。
            rendered.append(f"[[annotation:{annotation_id}]]\n\n{block}")
            continue
        lines[target_index] = f"{lines[target_index]} [[annotation:{annotation_id}]]"
        rendered.append("\n".join(lines))

    return "".join(rendered), annotations


def _best_effort_information_annotations(text: str) -> tuple[str, List[Dict[str, Any]]]:
    """标签是可失败的展示增强；任何异常都必须退化为未改动正文。"""
    try:
        return _annotate_information_blocks(text)
    except Exception:
        logger.exception("OutputGuard information annotation failed; delivering plain answer")
        return text, []


# ---------------------------------------------------------------------------
# 来源标注解析
# ---------------------------------------------------------------------------

# 支持的来源标注格式：
#   [来源: xxx]  [Source: xxx]  [数据来源: xxx]  [Ref: xxx]
_SOURCE_ANNOTATION_RE = re.compile(
    r"\[(?:来源|source|数据来源|ref|引用)\s*[:：]\s*([^\]]+)\]",
    re.IGNORECASE | re.UNICODE,
)

_RETIRED_FACT_MARKER_RE = re.compile(r"\[\[\s*fact\s*[:：]\s*(\d+)\s*\]\]", re.IGNORECASE)
_MACHINE_CITATION_RE = re.compile(r"\[\[\s*cite\s*[:：]\s*([a-z0-9_-]+)\s*\]\]", re.IGNORECASE)
_MACHINE_ANNOTATION_RE = re.compile(
    r"\[\[\s*annotation\s*[:：]\s*([a-z0-9_-]+)\s*\]\]",
    re.IGNORECASE,
)
_RETIRED_INTERNAL_REFERENCE_RE = re.compile(r"\b(?:ev|claim)_[a-z0-9][a-z0-9_-]*\b", re.IGNORECASE)
_USER_PLACEHOLDER_SENTENCE_RE = re.compile(
    r"[^。！？\n]*(?:"
    r"待确认|待核实|"
    r"(?:具体)?(?:价格|费用|时间|时长|日期|地点)(?:未知|不详|待定)"
    r")[。！？]?",
    re.IGNORECASE,
)
_USER_QUERY_HANDOFF_RE = re.compile(
    r"建议[^。！？\n]{0,160}(?:自行|通过[^。！？\n]{0,60})?查询[^。！？\n]*[。！？]?",
    re.IGNORECASE,
)

def _public_source(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """将已验证外部来源投影为可展示来源；裸内部 id 不构成用户来源。"""
    title = str(ev.get("title") or "").strip()
    source_name = str(ev.get("source_name") or ev.get("tool_name") or "").strip()
    url = str(ev.get("url") or "").strip()
    snippet = str(ev.get("snippet") or "").strip()
    if not (title or source_name or url or snippet):
        return None
    if title and _RETIRED_INTERNAL_REFERENCE_RE.fullmatch(title):
        title = ""
    if source_name and _RETIRED_INTERNAL_REFERENCE_RE.fullmatch(source_name):
        source_name = ""
    if not (title or source_name or url):
        return None
    return {
        "title": title,
        "url": url,
        "source_name": source_name,
        "snippet": snippet[:900],
        "authority_label": str(ev.get("source_authority_label") or ev.get("authority_label") or "").strip(),
        "retrieved_at": ev.get("retrieved_at"),
    }


def _clean_user_markdown(text: str) -> str:
    """最终防线：移除未解析机器标记和内部 id，并收紧清洗后的空白。"""
    cleaned = _SOURCE_ANNOTATION_RE.sub("", text)
    cleaned = _RETIRED_FACT_MARKER_RE.sub("", cleaned)
    cleaned = _MACHINE_ANNOTATION_RE.sub("", cleaned)
    cleaned = _RETIRED_INTERNAL_REFERENCE_RE.sub("", cleaned)
    cleaned = _USER_PLACEHOLDER_SENTENCE_RE.sub("", cleaned)
    cleaned = _USER_QUERY_HANDOFF_RE.sub("", cleaned)
    cleaned = cleaned.replace("据了解", "")
    cleaned = re.sub(r"[ \t]+([，。；：、,.!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _build_final_grounding(
    output_text: str,
    *,
    verified_claims: Dict[str, Dict[str, Any]],
    evidence_records: List[Dict[str, Any]],
) -> tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """将当前 fact marker 转换为 cite contract，并拒绝旧引用形状。"""
    evidence_by_id = {
        str(ev.get("evidence_id")): ev
        for ev in evidence_records
        if isinstance(ev, dict) and ev.get("evidence_id")
    }
    ordered_claims = [
        (str(claim_id), claim)
        for claim_id, claim in sorted((verified_claims or {}).items())
        if isinstance(claim, dict)
    ]
    citations: List[Dict[str, Any]] = []
    citation_by_key: Dict[str, str] = {}
    contract_errors: List[str] = []
    if _SOURCE_ANNOTATION_RE.search(output_text):
        contract_errors.append("legacy_source_marker")
    if _RETIRED_INTERNAL_REFERENCE_RE.search(output_text):
        contract_errors.append("internal_evidence_or_claim_id")

    def ensure_citation(
        claim_id: str,
        claim: Optional[Dict[str, Any]],
        evidence_ids: List[str],
    ) -> str:
        public_sources = []
        usable_ids = []
        for evidence_id in evidence_ids:
            ev = evidence_by_id.get(str(evidence_id))
            if not ev:
                continue
            public = _public_source(ev)
            if public:
                usable_ids.append(str(evidence_id))
                public_sources.append(public)
        if not public_sources:
            return ""

        dedupe_key = claim_id or "|".join(usable_ids)
        existing = citation_by_key.get(dedupe_key)
        if existing:
            return f"[[cite:{existing}]]"

        citation_id = f"cite_{len(citations) + 1}"
        citation_by_key[dedupe_key] = citation_id
        citations.append({
            "citation_id": citation_id,
            "claim_id": claim_id,
            "claim_text": str((claim or {}).get("claim_text") or "").strip(),
            "evidence_ids": usable_ids,
            "sources": public_sources,
        })
        return f"[[cite:{citation_id}]]"

    def replace_fact(match: re.Match[str]) -> str:
        fact_index = int(match.group(1)) - 1
        if fact_index < 0 or fact_index >= len(ordered_claims):
            if "unknown_fact_marker" not in contract_errors:
                contract_errors.append("unknown_fact_marker")
            return ""
        claim_id, claim = ordered_claims[fact_index]
        evidence_ids = [str(item) for item in (claim.get("evidence_ids") or []) if str(item)]
        return ensure_citation(claim_id, claim, evidence_ids)

    cleaned = _RETIRED_FACT_MARKER_RE.sub(replace_fact, output_text)

    cleaned = _clean_user_markdown(cleaned)
    cleaned, annotations = _best_effort_information_annotations(cleaned)

    claim_refs_used = [citation["claim_id"] for citation in citations if citation.get("claim_id")]
    citation_map = {
        citation["citation_id"]: {
            "citation_status": "grounded",
            "claim_ref": citation.get("claim_id") or None,
            "evidence_refs": citation.get("evidence_ids") or [],
            "risk_refs": [],
        }
        for citation in citations
    }
    final_grounding = {
        "answer_markdown": cleaned,
        "citations": citations,
        "claim_refs_used": claim_refs_used,
        "citation_map": citation_map,
        "risk_refs_shown": [],
        "contract_errors": contract_errors,
        "annotations": annotations,
    }
    if contract_errors:
        logger.warning("OutputGuard rejected output contract violations: %s", contract_errors)
    return cleaned, citations, final_grounding


# ---------------------------------------------------------------------------
# 主类：OutputGuard
# ---------------------------------------------------------------------------

class OutputGuard:
    """
    输出安全校验器。
    评估 LLM 输出的可信度，并追踪来源与局部信息状态。

    本门卫不承担文风审查，不按关键词删除旅行内容。来源和标签都是正向增强：
    只有真实绑定存在时才展示，增强失败时仍交付原始正文。
    """

    def check(
        self,
        output_text: str,
        retrieved_docs: Optional[List[Dict[str, Any]]] = None,
        evidence_records: Optional[List[Dict[str, Any]]] = None,
        tool_audit_events: Optional[List[Dict[str, Any]]] = None,
        risk_records: Optional[List[Dict[str, Any]]] = None,
        verified_claims: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> GroundingResult:
        """
        校验输出文本的可信度。

        Args:
            output_text: fast_answer 的原始输出
            retrieved_docs: RAG 检索结果列表

        Returns:
            GroundingResult，包含置信度评估和处理后的输出文本
        """
        evidence_records = evidence_records or []
        tool_audit_events = tool_audit_events or []
        risk_records = risk_records or []
        verified_claims = verified_claims or {}
        has_rag_data = bool(retrieved_docs)

        confidence, confidence_reasons = self._score_confidence(
            has_rag_data=has_rag_data,
            evidence_records=evidence_records,
            tool_audit_events=tool_audit_events,
            risk_records=risk_records,
        )

        cleaned_output, citations, final_grounding = _build_final_grounding(
            output_text,
            verified_claims=verified_claims,
            evidence_records=evidence_records,
        )

        # 置信度与来源缺口只进入结构化结果和日志。annotation 是可失败增强，
        # 不追加统一免责声明，也不因缺少来源改写或阻断正文。
        processed_output = cleaned_output
        final_grounding["answer_markdown"] = processed_output

        return GroundingResult(
            confidence=confidence,
            safety_disclaimer="",
            processed_output=processed_output,
            confidence_reasons=confidence_reasons,
            citations=citations,
            annotations=list(final_grounding.get("annotations") or []),
            final_grounding=final_grounding,
        )

    def _score_confidence(
        self,
        *,
        has_rag_data: bool,
        evidence_records: List[Dict[str, Any]],
        tool_audit_events: List[Dict[str, Any]],
        risk_records: List[Dict[str, Any]],
    ) -> tuple[ConfidenceLevel, List[str]]:
        reasons: List[str] = []

        def _meta(ev: Dict[str, Any]) -> Dict[str, Any]:
            return ev.get("metadata") if isinstance(ev.get("metadata"), dict) else {}

        has_fresh_verified_tool = any(
            isinstance(ev, dict)
            and ev.get("source_type") == "tool"
            and (ev.get("freshness_status") in {"fresh", "unknown"} or not ev.get("freshness_status"))
            and not _meta(ev).get("untrusted_content")
            for ev in evidence_records
        )
        has_any_evidence = bool(evidence_records)
        stale = any(isinstance(ev, dict) and ev.get("freshness_status") == "stale" for ev in evidence_records)
        untrusted = any(isinstance(ev, dict) and _meta(ev).get("untrusted_content") for ev in evidence_records) or any(
            isinstance(ev, dict) and ev.get("untrusted_content") for ev in tool_audit_events
        )
        quarantined = any(
            isinstance(ev, dict) and (_meta(ev).get("quarantine_result") or _meta(ev).get("quarantined"))
            for ev in evidence_records
        ) or any(
            isinstance(ev, dict) and (ev.get("quarantined") or ev.get("gateway_decision") == "quarantine_result")
            for ev in tool_audit_events
        )
        unsupported_or_conflict = any(
            isinstance(r, dict)
            and str(r.get("risk_type") or "") in {"evidence", "conflict"}
            and str(r.get("status") or "open") == "open"
            for r in risk_records
        )
        freshness_risk = any(isinstance(r, dict) and str(r.get("risk_type") or "") == "freshness" for r in risk_records)

        if stale or freshness_risk:
            reasons.append("stale_evidence")
        if untrusted:
            reasons.append("untrusted_tool_content")
        if quarantined:
            reasons.append("tool_result_quarantined")
        if unsupported_or_conflict:
            reasons.append("unsupported_or_conflict")

        if has_fresh_verified_tool and not reasons:
            return ConfidenceLevel.HIGH, ["fresh_verified_tool_evidence"]
        if has_any_evidence or has_rag_data:
            if "tool_result_quarantined" in reasons or "unsupported_or_conflict" in reasons:
                return ConfidenceLevel.LOW, reasons
            return ConfidenceLevel.MEDIUM, reasons or ["external_grounding"]
        return ConfidenceLevel.LOW, reasons or ["no_external_grounding"]

# ---------------------------------------------------------------------------
# Synthesizer Prompt 增强：注入来源追踪指令
# ---------------------------------------------------------------------------

SOURCE_TRACKING_INSTRUCTION = """

已验证结论会以「事实 1」「事实 2」这类本轮局部编号提供。正文使用某条可核验事实时，
紧跟一个机器标记，例如 [[fact:1]]；同一句使用多个来源支持的同一事实时仍只标一次。
不要输出 [来源: xxx]、URL、evidence id、claim id 或任何 ev_/claim_ 内部标识；系统会把机器标记转换为可点击角标。
对于价格、距离、营业时间、班次、酒店、天气、签证/入境等可能变化的信息，只在上方数据中存在可追溯依据时写入具体值；否则直接省略，不要生成统一的“建议确认”或“来源不足”提醒。

行程的顺序、节奏和区域组合属于规划建议，可以自然表达而无需逐条引用。不得使用自己的训练知识补写可能过时的具体事实。"""
