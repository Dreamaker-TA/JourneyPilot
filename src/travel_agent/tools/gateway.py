"""Production-style Tool Gateway policy boundary."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from ..entities.tool_gateway import (
    ToolGatewayDecision,
    ToolGatewayPolicyResult,
    ToolManifest,
    ToolResultTrustPolicy,
)
from ..infrastructure.tool_audit_store import ToolAuditStore
from .governance import ToolExecutionStatus, build_tool_execution_envelope
from .registry import ToolRegistry
from .temporal import TemporalPreflightStatus, evaluate_temporal_request

logger = logging.getLogger(__name__)


_POISONING_PATTERNS = [
    (re.compile(r"ignore\s+(all\s+)?(previous|prior|system|developer)\s+(instructions?|prompts?|rules?)", re.I), "instruction_override"),
    (re.compile(r"(reveal|print|show|exfiltrate|send)\s+(the\s+)?(system\s+prompt|developer\s+message|api[_ -]?key|token|cookie|secret)", re.I), "exfiltration_instruction"),
    (re.compile(r"\b(system|developer|assistant|tool)\s*:\s*(ignore|override|reveal|execute)", re.I), "forged_role_instruction"),
    (re.compile(r"\b(thought|observation|action)\s*:\s*(ignore|override|call|execute)", re.I), "forged_reasoning_step"),
    (re.compile(r"(忽略|无视|覆盖).{0,20}(系统|开发者|之前).{0,20}(指令|规则|提示)", re.I), "cn_instruction_override"),
    (re.compile(r"(泄露|发送|输出|显示).{0,20}(系统提示|密钥|令牌|Cookie|证件|护照|银行卡)", re.I), "cn_exfiltration_instruction"),
]
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")


def _manifest_from_metadata(tool_name: str, metadata: Dict[str, Any]) -> ToolManifest:
    raw = metadata.get("manifest") if isinstance(metadata, dict) else None
    if isinstance(raw, ToolManifest):
        return raw
    if isinstance(raw, dict):
        payload = dict(raw)
        payload.setdefault("tool_name", tool_name)
        return ToolManifest(**payload)
    from ..entities.tool_gateway import infer_tool_manifest

    return infer_tool_manifest(
        tool_name=tool_name,
        description=str(metadata.get("description") or ""),
        source=str(metadata.get("source") or "unknown"),
        server_name=metadata.get("server_name"),
    )


def _scan_tool_result_for_poisoning(envelope: Dict[str, Any]) -> list[str]:
    chunks: list[str] = []
    for key in ("result_summary", "sanitized_result"):
        value = envelope.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            chunks.append(value)
        else:
            try:
                chunks.append(json.dumps(value, ensure_ascii=False, default=str)[:3000])
            except Exception:
                chunks.append(str(value)[:3000])
    text = "\n".join(chunks)
    if not text:
        return []
    flags: list[str] = []
    if _ZERO_WIDTH_RE.search(text):
        flags.append("hidden_zero_width")
    for pattern, flag in _POISONING_PATTERNS:
        if pattern.search(text):
            flags.append(flag)
    seen: set[str] = set()
    return [flag for flag in flags if not (flag in seen or seen.add(flag))]


class ToolGateway:
    """Policy gateway around tool execution.

    The gateway is intentionally in-process. It establishes the contract for
    manifest enforcement, temporal preflight, audit persistence, and untrusted
    result isolation without claiming a full external gateway.

    It does **not** gate a tool behind human approval.  That surface existed here
    for two rounds and never fired once: no registered tool ever classified as
    approval-required, nothing consumed the verdict, and no screen could answer
    it.  This product searches and reads; it does not book, pay, or handle
    identity documents, so the honest shape is no gate at all.  A future booking
    tool has to decide its own guard here, deliberately.
    """

    async def before_call(
        self,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        registry: ToolRegistry,
        allowed_tool_names: Optional[Set[str]] = None,
        run_id: Optional[str] = None,
        node_name: Optional[str] = None,
        audit_store: Optional[ToolAuditStore] = None,
    ) -> ToolGatewayPolicyResult:
        metadata = registry.get_tool_metadata(tool_name)
        manifest = _manifest_from_metadata(tool_name, metadata)

        if allowed_tool_names is not None and tool_name not in allowed_tool_names:
            return await self._blocked(
                manifest=manifest,
                arguments=arguments,
                reason=f"工具 {tool_name} 未在当前会话中启用",
                decision=ToolGatewayDecision.BLOCK,
                run_id=run_id,
                audit_store=audit_store,
                metadata={"node_name": node_name, "policy": "agent_allowlist"},
            )

        if manifest.disabled:
            return await self._blocked(
                manifest=manifest,
                arguments=arguments,
                reason=f"工具 {tool_name} 已被 manifest 禁用",
                decision=ToolGatewayDecision.BLOCK,
                run_id=run_id,
                audit_store=audit_store,
                metadata={"node_name": node_name, "policy": "manifest_disabled"},
            )

        temporal = evaluate_temporal_request(
            tool_name=tool_name,
            server_name=manifest.server_name,
            arguments=arguments,
        )
        if temporal.status != TemporalPreflightStatus.EXECUTABLE:
            return await self._temporal_not_executable(
                manifest=manifest,
                arguments=arguments,
                temporal=temporal,
                run_id=run_id,
                node_name=node_name,
                audit_store=audit_store,
            )

        return ToolGatewayPolicyResult(
            decision=ToolGatewayDecision.ALLOW,
            manifest=manifest,
            metadata={"node_name": node_name},
        )

    async def _temporal_not_executable(
        self,
        *,
        manifest: ToolManifest,
        arguments: Dict[str, Any],
        temporal: Any,
        run_id: Optional[str],
        node_name: Optional[str],
        audit_store: Optional[ToolAuditStore],
    ) -> ToolGatewayPolicyResult:
        reference_only = (
            temporal.status == TemporalPreflightStatus.REFERENCE_ONLY
        )
        decision = (
            ToolGatewayDecision.REFERENCE_ONLY
            if reference_only
            else ToolGatewayDecision.NOT_APPLICABLE
        )
        status = (
            ToolExecutionStatus.REFERENCE_ONLY.value
            if reference_only
            else ToolExecutionStatus.NOT_APPLICABLE.value
        )
        # A date-capability decision is deliberately *not* an ``error``: the
        # envelope's ``error`` field is the execution-failure carrier that
        # ``research_packet_output._failed_tool_sources`` compiles into a
        # rejected source and Candidate Gate reads as a provider failure.  The
        # capability text stays fully visible on ``result_summary`` (below), in
        # ``metadata.temporal``, in the durable audit record, and in the
        # ``reason`` returned to the caller.
        envelope = build_tool_execution_envelope(
            tool_name=manifest.tool_name,
            arguments=arguments,
            status=status,
            source=manifest.source,
            server_name=manifest.server_name,
            category=manifest.category,
        )
        temporal_metadata = temporal.model_dump(mode="json", exclude_none=True)
        envelope["result_summary"] = temporal.user_message
        envelope["metadata"] = {
            "node_name": node_name,
            "policy": "temporal_capability",
            "temporal": temporal_metadata,
            "gateway_decision": decision.value,
            "permission_class": manifest.permission_class.value,
            "operation_sensitivity": manifest.operation_sensitivity.value,
            "evidence_allowed": False,
            "retry_allowed": False,
            "fallback_allowed": False,
        }
        await self._record_audit_safe(
            envelope,
            manifest=manifest,
            run_id=run_id,
            gateway_decision=decision,
            audit_store=audit_store,
        )
        return ToolGatewayPolicyResult(
            decision=decision,
            manifest=manifest,
            envelope=envelope,
            reason=temporal.user_message,
            metadata={"temporal": temporal_metadata},
        )

    async def after_call(
        self,
        envelope: Dict[str, Any],
        *,
        manifest: ToolManifest,
        run_id: Optional[str] = None,
        audit_store: Optional[ToolAuditStore] = None,
        gateway_decision: ToolGatewayDecision = ToolGatewayDecision.ALLOW,
        post_policy_hook: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        metadata = dict(envelope.get("metadata") or {})
        flags = _scan_tool_result_for_poisoning(envelope)
        trust_policy = manifest.untrusted_content_policy.value
        quarantined = bool(flags) or trust_policy == ToolResultTrustPolicy.QUARANTINE.value
        result_usable = envelope.get("status") in {
            ToolExecutionStatus.SUCCESS.value,
            ToolExecutionStatus.DEGRADED.value,
        }
        evidence_allowed = manifest.evidence_allowed and not quarantined and result_usable

        if manifest.untrusted_content_policy != ToolResultTrustPolicy.TRUSTED_SUMMARY:
            envelope["untrusted_content"] = True

        if flags:
            gateway_decision = ToolGatewayDecision.QUARANTINE_RESULT
        if quarantined:
            metadata["quarantine_result"] = True
            envelope["evidence_candidate"] = None

        metadata.update(
            {
                "gateway_decision": gateway_decision.value,
                "permission_class": manifest.permission_class.value,
                "operation_sensitivity": manifest.operation_sensitivity.value,
                "auth_boundary": manifest.auth_boundary.value,
                "result_trust_policy": trust_policy,
                "tool_poisoning_flags": flags,
                "evidence_allowed": evidence_allowed,
                "side_effecting": manifest.side_effecting,
                "irreversible": manifest.irreversible,
            }
        )
        envelope["metadata"] = metadata

        if not evidence_allowed:
            envelope["evidence_candidate"] = None
        elif isinstance(envelope.get("evidence_candidate"), dict):
            ev_meta = envelope["evidence_candidate"].setdefault("metadata", {})
            ev_meta["untrusted_content"] = bool(envelope.get("untrusted_content"))
            ev_meta["result_trust_policy"] = trust_policy
            ev_meta["quarantine_result"] = quarantined
            ev_meta["tool_poisoning_flags"] = flags

        # This hook is intentionally after all evidence/quarantine decisions but
        # before audit persistence.  Provider Snapshot Cache uses it to store
        # only an admitted fact snapshot and to make the live/cache marker part
        # of the immutable audit record.  A hook failure never blocks delivery.
        if post_policy_hook is not None:
            try:
                await post_policy_hook(envelope)
            except Exception as exc:  # pragma: no cover - defensive cache boundary
                logger.warning("ToolGateway post-policy hook failed safely: %s", exc)

        await self._record_audit_safe(
            envelope,
            manifest=manifest,
            run_id=run_id,
            gateway_decision=gateway_decision,
            audit_store=audit_store,
        )
        return envelope

    async def _blocked(
        self,
        *,
        manifest: ToolManifest,
        arguments: Dict[str, Any],
        reason: str,
        decision: ToolGatewayDecision,
        run_id: Optional[str],
        audit_store: Optional[ToolAuditStore],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ToolGatewayPolicyResult:
        envelope = build_tool_execution_envelope(
            tool_name=manifest.tool_name,
            arguments=arguments,
            status=ToolExecutionStatus.BLOCKED.value,
            error=reason,
            source=manifest.source,
            server_name=manifest.server_name,
            category=manifest.category,
        )
        envelope["metadata"] = {
            **(metadata or {}),
            "gateway_decision": decision.value,
            "permission_class": manifest.permission_class.value,
            "operation_sensitivity": manifest.operation_sensitivity.value,
            "evidence_allowed": False,
        }
        await self._record_audit_safe(
            envelope,
            manifest=manifest,
            run_id=run_id,
            gateway_decision=decision,
            audit_store=audit_store,
        )
        return ToolGatewayPolicyResult(decision=decision, manifest=manifest, envelope=envelope, reason=reason)

    async def _record_audit_safe(
        self,
        envelope: Dict[str, Any],
        *,
        manifest: ToolManifest,
        run_id: Optional[str],
        gateway_decision: ToolGatewayDecision,
        audit_store: Optional[ToolAuditStore],
    ) -> None:
        if audit_store is None:
            return
        try:
            await audit_store.record_envelope(
                envelope,
                manifest=manifest,
                run_id=run_id,
                gateway_decision=gateway_decision,
            )
        except Exception as exc:
            logger.warning("Tool audit persistence failed | tool=%s audit=%s error=%s", manifest.tool_name, envelope.get("audit_id"), exc)


_gateway_singleton: Optional[ToolGateway] = None


def get_tool_gateway() -> ToolGateway:
    global _gateway_singleton
    if _gateway_singleton is None:
        _gateway_singleton = ToolGateway()
    return _gateway_singleton
