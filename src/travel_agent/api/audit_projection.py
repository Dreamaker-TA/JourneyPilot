"""The inspect/diagnostics projection: labels and counters, never raw I/O.

This is the sibling boundary to ``services/public_delivery.py`` and deliberately
a different one.  The product surface is *constructed* from typed Bundle models,
because its shape is a contract.  Diagnostics are not: the inspect surface
carries open-ended durable payloads (node lifecycle records, run state summaries,
tool audit metadata) whose keys are authored across many call sites and change
with the pipeline.  There is no model to construct from, so this projection
scrubs by key instead — and unlike the deleted product blacklist, scrubbing is
the correct shape here: an unrecognized diagnostic key is far more likely to be
new raw I/O than a new product field, so dropping it fails safe.

What it may expose: provider/cache/gap classifications, deadline controls,
hashes, normalized reason codes, counters.  What it must never become: a bypass
for prompts, messages, tool arguments/results, provider snapshots, HTTP bodies,
or provider error text.
"""

from __future__ import annotations

from typing import Any, Mapping


_AUDIT_RAW_KEYS = frozenset(
    {
        "arguments",
        "argument",
        "args",
        "body",
        "content",
        "context",
        "detail",
        "error",
        "error_message",
        "headers",
        "input",
        "input_summary",
        "last_error",
        "message",
        "messages",
        "metadata",
        "output",
        "output_summary",
        "payload",
        "prompt",
        "prompts",
        "query",
        "request",
        "response",
        "summary",
        "tool_args",
        "tool_args_summary",
        "tool_calls",
        "tool_result",
        "tool_result_summary",
        "args_summary",
    }
)


def _audit_key_is_raw(key: str) -> bool:
    """Return whether a diagnostic mapping key can carry raw user/model data."""

    normalized = key.lower()
    if normalized in _AUDIT_RAW_KEYS:
        return True
    if normalized.startswith("raw_"):
        return True
    return (
        normalized.endswith(("_payload", "_body", "_headers"))
        or "prompt" in normalized
        or "message_content" in normalized
        or "request_body" in normalized
        or "response_body" in normalized
    )


def audit_safe_value(value: Any) -> Any:
    """Project inspect/diagnostics to labels and counters, never raw I/O or prose."""

    if isinstance(value, Mapping):
        return {
            str(key): audit_safe_value(item)
            for key, item in value.items()
            if not _audit_key_is_raw(str(key))
        }
    if isinstance(value, (list, tuple, set)):
        return [audit_safe_value(item) for item in value]
    return value
