"""Tool governance helpers for JourneyPilot ToolExecutionEnvelope v1.

The envelope is the execution-layer contract exposed to agents, trace, source
normalization, and guardrails. Raw tool arguments and raw tool output stay below this layer.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


TOOL_ENVELOPE_SCHEMA_VERSION = "tool_execution_envelope.v1"

_MAX_TEXT = 900
_MAX_SUMMARY = 180
_MAX_LIST_ITEMS = 5
_MAX_SEARCH_RESULTS = 120
_MAX_ROUTE_SEGMENTS = 12
_MAX_DICT_ITEMS = 12
_REDACTED = "[redacted]"
_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|cookie|passport|id[_-]?card|phone|mobile|email|证件|身份证|护照|手机号|邮箱)",
    re.IGNORECASE,
)


class ToolExecutionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"
    REFERENCE_ONLY = "reference_only"


# The two statuses the Tool Gateway writes for a deterministic *capability*
# decision taken before any provider call (``tools/temporal.py``: the requested
# date is outside the provider's documented window, or the provider exposes no
# dated query at all).  Such an envelope is a server-authored statement about
# what the provider can answer — never an execution outcome — so it must not
# consume a retry, must not trigger a fallback, and must not compile into a
# rejected ``external_tool`` SourceRecord.  ``status`` is the carrier of that
# distinction on purpose: it is a closed enum field written only by the
# Gateway, and it is the only such marker that survives
# ``agents.utils.compact_tool_content_for_model`` (whose ``_METADATA_KEEP_KEYS``
# drops ``metadata.policy``) into the transcript downstream compilers read.
CAPABILITY_DECLARATION_STATUSES = frozenset(
    {
        ToolExecutionStatus.NOT_APPLICABLE.value,
        ToolExecutionStatus.REFERENCE_ONLY.value,
    }
)


# Envelope ``metadata`` key stamped by ``agents.utils.execute_tool`` when a
# Provider completed a call and reported zero hits.  Such a round is a
# *successful* Provider answer, so the envelope carries no ``error``, no
# ``degradation_reason`` and no ``fallback_from`` — which leaves nothing
# downstream that can tell "the Provider searched and found nothing" apart from
# "no Provider was ever asked".  This key is that record, and it deliberately
# does not turn the envelope into a failure, a degradation, or a rejected
# SourceRecord.
PROVIDER_RESULT_OUTCOME_METADATA_KEY = "provider_result_outcome"
PROVIDER_RESULT_OUTCOME_EMPTY_SUCCESS = "empty_success"


# ---------------------------------------------------------------------------
# Compiled tool SourceRecord identity
# ---------------------------------------------------------------------------
# Every ``external_tool`` SourceRecord in a Research Packet is minted by the
# server from a successful Tool Gateway envelope, and its id encodes *which
# entity* that envelope was about.  Two layers read that encoding and they must
# never drift apart: ``agents.research_packet_output`` mints the id, and
# ``services.candidate_admission`` re-derives it to ask "is this compiled source
# about the entity this candidate claims to be?".  A duplicated literal or a
# duplicated digest formula in those two places would silently reopen the
# whole-entity hallucination channel the suffix exists to close, so both sites
# call the helpers below instead.
COMPILED_TOOL_SOURCE_ID_PREFIX = "source_tool_success_"

_COMPILED_TOOL_SOURCE_ENTITY_DIGEST_LENGTH = 16


def compiled_tool_source_entity_digest(entity_id: str) -> str:
    """Digest the entity id a compiled tool SourceRecord is about."""

    return hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[
        :_COMPILED_TOOL_SOURCE_ENTITY_DIGEST_LENGTH
    ]


def compiled_tool_source_id(audit_id: str, entity_id: str) -> str:
    """Bind one entity snapshot to one Tool Gateway audit exactly once."""

    return (
        f"{COMPILED_TOOL_SOURCE_ID_PREFIX}{audit_id}"
        f"_{compiled_tool_source_entity_digest(entity_id)}"
    )


def compiled_tool_source_id_is_about(source_record_id: str, entity_id: str) -> bool:
    """Return whether a compiled tool source id was minted for ``entity_id``.

    The audit id component varies per call, so the entity component is matched
    as the id's final segment — which is exactly how
    :func:`compiled_tool_source_id` lays it out.
    """

    return source_record_id.startswith(
        COMPILED_TOOL_SOURCE_ID_PREFIX
    ) and source_record_id.endswith(f"_{compiled_tool_source_entity_digest(entity_id)}")


# Envelope ``metadata`` key the dining quality preflight stamps onto the one
# envelope that proves a branch-level review/quality check really resolved to a
# specific place (``agents.destination_researcher.node``: the amap POI
# substitute for a CN review page, and the generic non-CN review search whose
# result matched the branch's locality tokens).  It is the *only* discriminator
# that separates "a review of this exact branch was retrieved" from "this place
# was looked up on a map", because both compile into an ``external_tool``
# SourceRecord about the same ``place_id``.  ``research_packet_output`` reads it
# to decide which envelopes become quality sources, and that is its only reader:
# a restaurant does not owe a review to be admitted, so whether this key is present
# decides only whether the report *marks* the option 外部评价已核验 — never whether
# the restaurant is delivered at all.
QUALITY_VERIFIED_PLACE_IDS_METADATA_KEY = "quality_verified_place_ids"


class ToolTrustLevel(str, Enum):
    TRUSTED_INTERNAL = "trusted_internal"
    THIRD_PARTY = "third_party"
    UGC = "ugc"
    UNKNOWN = "unknown"


@dataclass
class ToolExecutionEnvelope:
    audit_id: str
    tool_name: str
    server_name: Optional[str]
    category: str
    source_type: str
    trust_level: str
    status: str
    args_digest: str
    sanitized_args_summary: str
    result_summary: str
    untrusted_content: bool
    schema_version: str = TOOL_ENVELOPE_SCHEMA_VERSION
    retrieved_at: Optional[str] = None
    freshness_hint: Optional[Dict[str, Any]] = None
    sanitized_result: Any = None
    fallback_from: Optional[str] = None
    fallback_to: Optional[str] = None
    degradation_reason: Optional[str] = None
    evidence_candidate: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_args_digest(arguments: Dict[str, Any]) -> str:
    """Return a stable digest without exposing raw arguments."""
    payload = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def summarize_args(arguments: Dict[str, Any], *, limit: int = 160) -> str:
    if not arguments:
        return ""
    parts = []
    for key in sorted(arguments.keys()):
        value = _sanitize_value(key, arguments[key], depth=0)
        text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 48:
            text = text[:45].rstrip() + "..."
        parts.append(f"{key}={text}")
    summary = ", ".join(parts)
    return summary[:limit].rstrip()


def sanitize_tool_result(result: Any) -> Any:
    """Return a trace-safe copy of a tool result."""
    return _sanitize_value("", result, depth=0)


def _summarize_structured_collection(value: Any) -> Optional[str]:
    """Human-readable summary for the common structured tool payloads (place
    search / weather / route), so envelope summaries read as "找到 5 个结果：…"
    instead of dumping raw provider JSON.  Returns None when the shape is not
    recognized so the caller falls back to the generic compaction.
    """
    data: Any = value
    # MCP text-content shape: [{"text": "{...json...}"}] — lift and parse.
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and isinstance(data[0].get("text"), str):
            data = data[0]["text"]
        else:
            return None
    if isinstance(data, str):
        stripped = data.strip()
        if not stripped.startswith("{"):
            return None
        try:
            data = json.loads(stripped)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    try:
        results = data.get("results")
        if isinstance(results, list) and results:
            names = [
                str(item.get("name") or item.get("title") or "").strip()
                for item in results
                if isinstance(item, dict)
            ]
            # 去重同名 POI 变体（provider 常返回同名多条），保持出现顺序。
            names = list(dict.fromkeys(name for name in names if name))
            head = "、".join(names[:3])
            if head:
                return f"找到 {len(results)} 个结果：{head}" + ("…" if len(results) > 3 else "")
            return f"找到 {len(results)} 个结果"

        forecasts = data.get("forecasts")
        if isinstance(forecasts, list) and forecasts and isinstance(forecasts[0], dict):
            forecast = forecasts[0]
            city = str(forecast.get("city") or "").strip()
            casts = forecast.get("casts") or []
            if casts and isinstance(casts[0], dict):
                cast = casts[0]
                weather = str(cast.get("dayweather") or "").strip()
                low = str(cast.get("daytemp") or "").strip()
                high = str(cast.get("nighttemp") or "").strip()
                temp = f" {low}~{high}°C" if low and high else ""
                summary = f"{city} {weather}{temp}".strip()
                return summary or None

        routes = data.get("routes")
        if isinstance(routes, list) and routes and isinstance(routes[0], dict):
            route = routes[0]
            distance = route.get("distance")
            duration = route.get("duration")
            parts: list[str] = []
            if str(distance).isdigit():
                parts.append(f"{int(distance) / 1000:.1f}km")
            if str(duration).isdigit():
                parts.append(f"约 {int(duration) // 60} 分钟")
            if parts:
                return "路线：" + "，".join(parts)

        # Web 搜索 / 网页抓取：{web:[...]}、{data:{web:[...]}}，或单条 {url,title}。
        # 这类结果最常在思维链里泄成整坨原始 JSON——收敛成「找到 N 条网页：标题…」。
        web = data.get("web")
        if web is None and isinstance(data.get("data"), dict):
            web = data["data"].get("web")
        if isinstance(web, list) and web:
            titles = [
                str(item.get("title") or item.get("name") or "").strip()
                for item in web
                if isinstance(item, dict)
            ]
            titles = list(dict.fromkeys(title for title in titles if title))
            head = "、".join(titles[:3])
            if head:
                return f"找到 {len(web)} 条网页：{head}" + ("…" if len(web) > 3 else "")
            return f"找到 {len(web)} 条网页"
        # 单条网页文档 {url, title, description}（tavily_extract 等）。
        if isinstance(data.get("url"), str) and data.get("url"):
            title = str(data.get("title") or data.get("description") or "").strip()
            if title:
                return title if len(title) <= _MAX_SUMMARY else title[: _MAX_SUMMARY - 1] + "…"
    except Exception:  # 工具返回结构不可预测，抽取失败即回退通用压缩，绝不影响主流程
        return None
    return None


# The one wording for a tool round that came back with nothing, read by every
# product surface that shows a failed step.
#
# ``result_summary`` is the **human-facing** line of a Tool Envelope; the reason a
# call failed lives in the envelope's own ``error`` field, which is separately in
# ``_ENVELOPE_TOP_KEYS`` and therefore reaches the model unchanged.  Do not conflate
# the two.
#
# In particular, **never sanitize by allowlist** — hiding a handful of known MCP
# protocol shapes (``-32xxx``, "Input validation error") and passing everything else
# through verbatim walks our own English exception text straight onto the screen
# （「失败: global route provider found no executable route」 for an empty route query）.
# An allowlist can only ever cover the messages someone has already seen.  The product
# face states the outcome and nothing else, the same rule the inspect surface follows.
TOOL_FAILURE_SUMMARY = "未取到结果"


def summarize_tool_result(result: Any) -> str:
    if result is None:
        return "无结果"
    if isinstance(result, dict):
        if result.get("type") == "user_input_required":
            return "等待用户输入"
        if result.get("success") is False:
            return TOOL_FAILURE_SUMMARY
        structured = _summarize_structured_collection(result)
        if structured:
            return structured
        inner = result.get("result")
        if inner is None:
            inner = result.get("content", result.get("data", result.get("text")))
        if inner is not None:
            structured_inner = _summarize_structured_collection(inner)
            if structured_inner:
                return structured_inner
            return _compact_summary(inner)
    return _compact_summary(result)


def infer_source_type(source: str) -> str:
    if source == "mcp":
        return "mcp_tool"
    if source == "local":
        return "local_tool"
    return "tool"


def infer_trust_level(*, source: str, server_name: Optional[str], tool_name: str) -> str:
    server = (server_name or "").lower()
    name = (tool_name or "").lower()
    if source == "local":
        return ToolTrustLevel.TRUSTED_INTERNAL.value
    if any(
        token in server or token in name
        for token in ("reddit", "ugc", "social")
    ):
        return ToolTrustLevel.UGC.value
    if source == "mcp" or server:
        return ToolTrustLevel.THIRD_PARTY.value
    return ToolTrustLevel.UNKNOWN.value


def is_untrusted_tool_content(*, source: str, trust_level: str, server_name: Optional[str], tool_name: str) -> bool:
    if trust_level != ToolTrustLevel.TRUSTED_INTERNAL.value:
        return True
    haystack = f"{server_name or ''} {tool_name or ''}".lower()
    return any(
        token in haystack
        for token in ("web", "search", "fetch", "crawl", "scrape")
    )


def build_tool_execution_envelope(
    *,
    tool_name: str,
    arguments: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    status: str = ToolExecutionStatus.SUCCESS.value,
    source: str = "local",
    server_name: Optional[str] = None,
    category: str = "other",
    fallback_from: Optional[str] = None,
    fallback_to: Optional[str] = None,
    degradation_reason: Optional[str] = None,
    audit_id: Optional[str] = None,
    retrieved_at: Optional[str] = None,
    freshness_hint: Optional[Dict[str, Any]] = None,
    activation_source: Optional[str] = None,
) -> Dict[str, Any]:
    trust_level = infer_trust_level(source=source, server_name=server_name, tool_name=tool_name)
    sanitized_result = sanitize_tool_result(result) if result is not None else None
    resolved_retrieved_at = retrieved_at or _extract_retrieved_at(result)
    if status in {ToolExecutionStatus.SUCCESS.value, ToolExecutionStatus.DEGRADED.value} and not resolved_retrieved_at:
        resolved_retrieved_at = utc_now_iso()

    envelope = ToolExecutionEnvelope(
        audit_id=audit_id or f"tool_{uuid.uuid4().hex[:12]}",
        tool_name=tool_name,
        server_name=server_name,
        category=category or "other",
        source_type=infer_source_type(source),
        trust_level=trust_level,
        status=status,
        retrieved_at=resolved_retrieved_at,
        freshness_hint=freshness_hint or _build_freshness_hint(tool_name, result, resolved_retrieved_at),
        args_digest=build_args_digest(arguments),
        sanitized_args_summary=summarize_args(arguments),
        # A failed round says only that it came back with nothing; the reason is
        # carried by ``error`` below, for the model and the audit trail.
        result_summary=(
            summarize_tool_result(result)
            if result is not None
            else (TOOL_FAILURE_SUMMARY if error else "")
        ),
        sanitized_result=sanitized_result,
        untrusted_content=is_untrusted_tool_content(
            source=source,
            trust_level=trust_level,
            server_name=server_name,
            tool_name=tool_name,
        ),
        fallback_from=fallback_from,
        fallback_to=fallback_to,
        degradation_reason=degradation_reason,
        error=(str(error)[:300] if error else None),
    )
    # 标注 schema 激活来源（searched=经 search_tools 按需激活 / preloaded=全量注入
    # 或初始暴露），随 metadata 流入 tool_audit_store，供观测面板区分。
    if activation_source:
        envelope.metadata["activation_source"] = activation_source
    envelope.evidence_candidate = tool_envelope_to_evidence_candidate(envelope)
    return envelope.to_dict()


def tool_envelope_to_evidence_candidate(envelope: ToolExecutionEnvelope | Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = envelope.to_dict() if isinstance(envelope, ToolExecutionEnvelope) else envelope
    if data.get("status") not in {ToolExecutionStatus.SUCCESS.value, ToolExecutionStatus.DEGRADED.value}:
        return None
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if metadata.get("quarantine_result") or metadata.get("evidence_allowed") is False:
        return None
    audit_id = str(data.get("audit_id") or "")
    return {
        "evidence_id": f"ev_{audit_id}" if audit_id and not audit_id.startswith("ev_") else audit_id,
        "source_type": "tool",
        "source_name": data.get("server_name") or data.get("tool_name") or "tool",
        "tool_name": data.get("tool_name") or "",
        "title": data.get("result_summary") or data.get("tool_name") or "",
        "snippet": data.get("result_summary") or "",
        "retrieved_at": data.get("retrieved_at") or "",
        "freshness_status": (data.get("freshness_hint") or {}).get("freshness_status") or "unknown",
        "authority_score": _authority_for_envelope(data),
        "degraded": data.get("status") == ToolExecutionStatus.DEGRADED.value,
        "original_tool": data.get("fallback_from"),
        "metadata": {
            "tool_audit_id": audit_id,
            "tool_status": data.get("status"),
            "degraded": data.get("status") == ToolExecutionStatus.DEGRADED.value,
            "untrusted_content": bool(data.get("untrusted_content")),
            "trust_level": data.get("trust_level"),
            "fallback_from": data.get("fallback_from"),
            "fallback_to": data.get("fallback_to"),
            "quarantine_result": bool(metadata.get("quarantine_result")),
            "result_trust_policy": metadata.get("result_trust_policy"),
            "tool_poisoning_flags": metadata.get("tool_poisoning_flags") or [],
        },
    }


def tool_envelope_to_risk_signal(envelope: ToolExecutionEnvelope | Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = envelope.to_dict() if isinstance(envelope, ToolExecutionEnvelope) else envelope
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if metadata.get("quarantine_result"):
        return {
            "risk_type": "tool",
            "source_signal": "tool_poisoning_quarantined",
            "severity": "medium",
            "audit_id": data.get("audit_id"),
            "tool_name": data.get("tool_name"),
            "fallback_from": data.get("fallback_from"),
            "fallback_to": data.get("fallback_to"),
            "impact": "工具返回包含潜在提示注入或污染内容，已隔离为低信任摘要",
        }
    status = data.get("status")
    if status not in {
        ToolExecutionStatus.DEGRADED.value,
        ToolExecutionStatus.FAILED.value,
        ToolExecutionStatus.BLOCKED.value,
    }:
        return None
    return {
        "risk_type": "tool",
        "source_signal": "tool_fallback" if status == ToolExecutionStatus.DEGRADED.value else f"tool_{status}",
        "severity": "medium" if status == ToolExecutionStatus.DEGRADED.value else "low",
        "audit_id": data.get("audit_id"),
        "tool_name": data.get("tool_name"),
        "fallback_from": data.get("fallback_from"),
        "fallback_to": data.get("fallback_to"),
        "impact": data.get("degradation_reason") or data.get("error") or "工具调用未得到可信结果",
    }


def is_tool_execution_envelope(value: Any) -> bool:
    return isinstance(value, dict) and value.get("schema_version") == TOOL_ENVELOPE_SCHEMA_VERSION


def _sanitize_value(
    key: str,
    value: Any,
    *,
    depth: int,
    max_depth: int = 4,
) -> Any:
    if key == "routes":
        max_depth = max(max_depth, 8)
    if _SENSITIVE_KEY_RE.search(str(key or "")):
        return _REDACTED
    if depth >= max_depth:
        return _compact_summary(value, limit=120)
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for idx, (k, v) in enumerate(value.items()):
            if idx >= _MAX_DICT_ITEMS:
                out["truncated"] = True
                break
            out[str(k)] = _sanitize_value(
                str(k),
                v,
                depth=depth + 1,
                max_depth=max_depth,
            )
        return out
    if isinstance(value, list):
        if key == "segments":
            limit = _MAX_ROUTE_SEGMENTS
        elif key == "results":
            limit = _MAX_SEARCH_RESULTS
        else:
            limit = _MAX_LIST_ITEMS
        out = [
            _sanitize_value(
                key,
                item,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for item in value[:limit]
        ]
        if len(value) > limit:
            out.append({"truncated_count": len(value) - limit})
        return out
    if isinstance(value, str):
        text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", value)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > _MAX_TEXT:
            return text[:_MAX_TEXT].rstrip() + "…"
        return text
    return value


def _compact_summary(value: Any, *, limit: int = _MAX_SUMMARY) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        return f"共 {len(value)} 条结果"
    elif isinstance(value, dict):
        for key in ("summary", "title", "name", "destination", "converted", "rate", "total_cny"):
            if key in value and value[key]:
                text = f"{key}: {value[key]}"
                break
        else:
            text = json.dumps(sanitize_tool_result(value), ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "无结果"


def _extract_retrieved_at(result: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    value = result.get("retrieved_at")
    return str(value).strip() if value else None


def _build_freshness_hint(
    tool_name: str,
    result: Optional[Dict[str, Any]],
    retrieved_at: Optional[str],
) -> Optional[Dict[str, Any]]:
    if retrieved_at is None and not isinstance(result, dict):
        return None
    published_at = ""
    if isinstance(result, dict):
        published_at = str(result.get("published_at") or "")
        existing = result.get("freshness_hint")
        if isinstance(existing, dict):
            hint = dict(existing)
            hint.setdefault("tool_name", tool_name)
            hint.setdefault("retrieved_at", retrieved_at or "")
            return hint
    return {
        "tool_name": tool_name,
        "source_type": "tool",
        "published_at": published_at,
        "retrieved_at": retrieved_at or "",
    }


def _authority_for_envelope(data: Dict[str, Any]) -> float:
    if data.get("status") == ToolExecutionStatus.DEGRADED.value:
        return 0.45
    trust = data.get("trust_level")
    if trust == ToolTrustLevel.TRUSTED_INTERNAL.value:
        return 0.85
    if trust == ToolTrustLevel.THIRD_PARTY.value:
        return 0.65
    if trust == ToolTrustLevel.UGC.value:
        return 0.35
    return 0.5
