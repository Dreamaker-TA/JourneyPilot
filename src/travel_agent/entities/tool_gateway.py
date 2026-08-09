"""Tool Gateway domain models for production-style tool boundaries."""

from __future__ import annotations

import re
import uuid
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from .trip_run import utc_now_iso


def generate_tool_audit_id() -> str:
    return f"tool_{uuid.uuid4().hex[:12]}"


class ToolPermissionClass(str, Enum):
    READ_ONLY = "read_only"
    EXTERNAL_READ = "external_read"
    USER_DATA_READ = "user_data_read"
    WRITE = "write"
    TRANSACTIONAL = "transactional"
    ADMIN = "admin"


class ToolOperationSensitivity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    IRREVERSIBLE = "irreversible"


class ToolGatewayDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    NOT_APPLICABLE = "not_applicable"
    REFERENCE_ONLY = "reference_only"
    # Tool-level quality reduction or alternate-tool path is recorded as DEGRADE.
    DEGRADE = "degrade"
    QUARANTINE_RESULT = "quarantine_result"


class ToolResultTrustPolicy(str, Enum):
    TRUSTED_SUMMARY = "trusted_summary"
    UNTRUSTED_SUMMARY = "untrusted_summary"
    QUARANTINE = "quarantine"


class ToolAuthBoundary(str, Enum):
    NONE = "none"
    SERVER_MANAGED = "server_managed"
    USER_OAUTH_REQUIRED = "user_oauth_required"
    PROVIDER_OAUTH_REQUIRED = "provider_oauth_required"
    FUTURE = "future"


class _StrictBoundaryModel(BaseModel):
    """API/tool boundary DTOs: forbid unknown fields (not frozen)."""

    model_config = ConfigDict(extra="forbid")


class ToolManifest(_StrictBoundaryModel):
    tool_name: str
    source: str = "unknown"
    server_name: Optional[str] = None
    category: str = "other"
    permission_class: ToolPermissionClass = ToolPermissionClass.READ_ONLY
    operation_sensitivity: ToolOperationSensitivity = ToolOperationSensitivity.LOW
    # ``auth_boundary`` is published on every audit row and enforced by nobody:
    # this product only reads, so a boundary is provenance, not a gate.
    auth_boundary: ToolAuthBoundary = ToolAuthBoundary.NONE
    allow_offline_fallback: bool = True
    evidence_allowed: bool = True
    side_effecting: bool = False
    irreversible: bool = False
    untrusted_content_policy: ToolResultTrustPolicy = ToolResultTrustPolicy.UNTRUSTED_SUMMARY
    disabled: bool = False


class ToolGatewayPolicyResult(_StrictBoundaryModel):
    decision: ToolGatewayDecision
    manifest: ToolManifest
    envelope: Optional[Dict[str, Any]] = None
    reason: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ToolAuditRecord(BaseModel):
    audit_id: str = Field(default_factory=generate_tool_audit_id)
    run_id: Optional[str] = None
    tool_name: str
    server_name: Optional[str] = None
    source_type: str = "tool"
    category: str = "other"
    permission_class: str = ToolPermissionClass.READ_ONLY.value
    operation_sensitivity: str = ToolOperationSensitivity.LOW.value
    status: str
    gateway_decision: str = ToolGatewayDecision.ALLOW.value
    args_digest: str
    result_digest: str = ""
    untrusted_content: bool = False
    quarantined: bool = False
    fallback_from: Optional[str] = None
    fallback_to: Optional[str] = None
    degradation_reason: Optional[str] = None
    error: Optional[str] = None
    evidence_allowed: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


_SENSITIVE_OPERATION_RE = re.compile(
    r"(book|booking|reserve|reservation|pay|payment|purchase|submit|passport|visa|cancel|delete|order|ticket[_-]?buy|checkout|refund|证件|护照|签证|支付|付款|预订|订票|取消|退款)",
    re.IGNORECASE,
)
_WRITE_OPERATION_RE = re.compile(
    r"(\b(create|update|write|send|post|upload|modify|change|edit)\b|生成订单|提交|发送|更新|写入)",
    re.IGNORECASE,
)
_SEARCH_RE = re.compile(r"(search|fetch|query|lookup|find|web|duckduckgo|serper|搜索|查询)", re.IGNORECASE)
# 检索/抓取类工具按「工具名 + server」归类。自由文本描述里常含 refund / order / book
# 等示例词（firecrawl_search 的搜索算子说明里有 "refund flights"，firecrawl_scrape 里有
# "order"），据此归类会把纯检索工具归入 payment / booking / identity。
_RETRIEVAL_TOOL_NAME_RE = re.compile(
    r"(search|scrape|crawl|fetch|extract|lookup|geocode|autocomplete|retrieve)",
    re.IGNORECASE,
)


def infer_tool_category(tool_name: str, *, server_name: Optional[str] = None, description: str = "") -> str:
    haystack = f"{tool_name} {server_name or ''} {description}".lower()
    # 工具名（+server）是检索/抓取动词时归入只读检索类：带地图/天气/汇率语义的落 data，
    # 其余落 search。两者都在 infer_tool_manifest 的只读集合内。
    retrieval_by_name = bool(_RETRIEVAL_TOOL_NAME_RE.search(f"{tool_name} {server_name or ''}"))
    if retrieval_by_name:
        if any(token in haystack for token in ("map", "weather", "direction", "train", "flight", "currency")):
            return "data"
        return "search"
    if any(token in haystack for token in ("payment", "pay", "checkout", "refund", "支付", "付款", "退款")):
        return "payment"
    if any(token in haystack for token in ("book", "booking", "reserve", "hotel", "ticket", "订票", "预订")):
        return "booking"
    if any(token in haystack for token in ("passport", "visa", "identity", "id_card", "证件", "护照", "签证")):
        return "identity"
    if any(token in haystack for token in ("cancel", "delete", "取消", "删除")):
        return "cancellation"
    if any(token in haystack for token in ("map", "weather", "direction", "train", "flight", "currency")):
        return "data"
    if _SEARCH_RE.search(haystack):
        return "search"
    if "ask_user" in haystack or "internal" in haystack:
        return "internal"
    return "other"


def infer_tool_manifest(
    *,
    tool_name: str,
    description: str = "",
    source: str = "unknown",
    server_name: Optional[str] = None,
    manifest: Optional[ToolManifest | Dict[str, Any]] = None,
) -> ToolManifest:
    if isinstance(manifest, ToolManifest):
        return manifest
    if isinstance(manifest, dict):
        payload = dict(manifest)
        payload.setdefault("tool_name", tool_name)
        payload.setdefault("source", source)
        payload.setdefault("server_name", server_name)
        return ToolManifest(**payload)

    category = infer_tool_category(tool_name, server_name=server_name, description=description)
    haystack = f"{tool_name} {server_name or ''} {description}"
    # 只读检索类工具（网页搜索/抓取、地图/天气/汇率查询）是 external_read。它们的自然语言
    # 描述里常出现 submit(a query) / order(results) / search 之类词汇，这些词不参与它们的
    # 敏感度判定。预订/支付/证件/取消类（booking/payment/identity/cancellation/other）不在
    # 此集合内，仍按关键词升级为事务型操作。
    is_read_only_retrieval = category in {"search", "data"}
    sensitive = bool(_SENSITIVE_OPERATION_RE.search(haystack)) and not is_read_only_retrieval
    write_like = bool(_WRITE_OPERATION_RE.search(haystack)) and not is_read_only_retrieval
    is_local = source == "local"

    permission = ToolPermissionClass.READ_ONLY
    sensitivity = ToolOperationSensitivity.LOW
    side_effecting = False
    irreversible = False
    auth_boundary = ToolAuthBoundary.NONE

    if sensitive:
        permission = ToolPermissionClass.TRANSACTIONAL
        sensitivity = ToolOperationSensitivity.IRREVERSIBLE
        side_effecting = True
        irreversible = True
        auth_boundary = ToolAuthBoundary.PROVIDER_OAUTH_REQUIRED
    elif write_like:
        permission = ToolPermissionClass.WRITE
        sensitivity = ToolOperationSensitivity.HIGH
        side_effecting = True
    elif source == "mcp":
        permission = ToolPermissionClass.EXTERNAL_READ
        sensitivity = ToolOperationSensitivity.MEDIUM
        auth_boundary = ToolAuthBoundary.SERVER_MANAGED if server_name else ToolAuthBoundary.FUTURE
    elif is_local:
        permission = ToolPermissionClass.READ_ONLY
        sensitivity = ToolOperationSensitivity.LOW

    trust_policy = (
        ToolResultTrustPolicy.TRUSTED_SUMMARY
        if is_local and category in {"internal", "other"}
        else ToolResultTrustPolicy.UNTRUSTED_SUMMARY
    )
    if sensitive:
        trust_policy = ToolResultTrustPolicy.QUARANTINE

    return ToolManifest(
        tool_name=tool_name,
        source=source,
        server_name=server_name,
        category=category,
        permission_class=permission,
        operation_sensitivity=sensitivity,
        auth_boundary=auth_boundary,
        allow_offline_fallback=category in {"search", "data", "booking", "other"},
        evidence_allowed=not sensitive,
        side_effecting=side_effecting,
        irreversible=irreversible,
        untrusted_content_policy=trust_policy,
    )
