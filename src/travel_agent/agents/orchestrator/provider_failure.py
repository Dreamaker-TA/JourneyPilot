"""Normalized provider-failure classification shared by orchestration gates.

The classifier deliberately works on error *categories*, never raw provider
payloads.  Candidate Gate owns the retry/circuit decision; Artifact Gate only
uses the same classification to decide whether an absent typed artifact is a
provider/model-content outcome or a true internal-contract failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProviderFailureCategory = Literal["deterministic", "transient", "incomplete"]


@dataclass(frozen=True)
class ProviderFailureClassification:
    category: ProviderFailureCategory
    reason_code: str


# A query that reached the Provider and came back with nothing.  The
# authoritative signal is the ``provider_empty:`` prefix below; this free-text
# marker only covers Provider payloads that phrase the miss in their own words.
_QUERY_MISS_MARKERS = (
    "found no executable route",
)

# The one definition of these two vocabularies; ``agents.worker_errors`` imports
# them rather than keeping a second copy.  A classification that depended on
# which layer saw the string first is not a classification.
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "temporarily",
    "rate limit",
    "rate-limit",
    # amap answers a breached query-per-second allowance with
    # ``CUQPS_HAS_EXCEEDED_THE_LIMIT`` — no space, so "rate limit" never matched
    # it.  It is transient by construction: the allowance is per second, and the
    # rate gate is what keeps a Run from tripping it again.  Reading it as
    # deterministic would close the whole lodging domain on one second's worth of
    # contention (candidate_gate closes a domain on a deterministic signal).
    "cuqps",
    "exceeded_the_limit",
    "429",
    "502",
    "503",
    "504",
    "service unavailable",
    "provider unavailable",
    "upstream",
)

_DETERMINISTIC_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "authentication",
    "auth failed",
    "permission denied",
    "invalid parameter",
    "invalid params",
    "bad request",
    "400",
    "unsupported",
    "not supported",
    "invalid output schema",
    "response schema",
    "provider contract",
)

# These phrases identify an external/model failure without treating a local
# contract message such as ``tool assignment missing`` as a provider outcome.
# The generic nouns "provider", "model", and "tool" are deliberately not
# signals by themselves.
#
# Research workers write standardized prefixes via
# ``agents.worker_errors.format_worker_last_error``:
# ``schema_gate:``, ``provider_empty:``, ``provider_capability:``,
# ``provider_transient:``, ``provider_deterministic:``.  Artifact Gate already
# treats a present Research Packet + failed status as content (not missing
# packet); prefixes still matter when the packet is absent.
_EXPLICIT_EXTERNAL_FAILURE_MARKERS = (
    "schema_gate:",
    "provider_empty:",
    "provider_capability:",
    "provider_transient:",
    "provider_deterministic:",
    "provider error",
    "provider failed",
    "provider failure",
    "model error",
    "model failed",
    "model failure",
    "tool error",
    "tool call failed",
    "tool execution failed",
    "error executing tool",
)


def classify_provider_failure(value: object) -> ProviderFailureClassification:
    """Classify an external/provider failure without treating it as evidence."""
    raw = str(value or "")
    text = raw.casefold()
    # Prefer standardized worker prefixes before free-text markers.
    # A ``schema_gate:`` prefix marks the packet-collection layer, not the
    # transiency of the failure: a collection call that timed out or hit a
    # rate limit does not repeat verbatim, so the transient markers inside the
    # text decide the category.
    if text.startswith("schema_gate:"):
        if any(marker in text for marker in _TRANSIENT_MARKERS):
            return ProviderFailureClassification(
                category="transient",
                reason_code="provider_transient_failure",
            )
        return ProviderFailureClassification(
            category="deterministic",
            reason_code="provider_deterministic_failure",
        )
    if text.startswith("provider_deterministic:"):
        return ProviderFailureClassification(
            category="deterministic",
            reason_code="provider_deterministic_failure",
        )
    if text.startswith("provider_transient:"):
        return ProviderFailureClassification(
            category="transient",
            reason_code="provider_transient_failure",
        )
    if text.startswith("provider_empty:"):
        return ProviderFailureClassification(
            category="incomplete",
            reason_code="provider_empty_result",
        )
    # The Gateway decided before any call that this Provider cannot answer the
    # requested date.  Nothing failed and nothing repeats verbatim on a
    # *different* Provider, so this is an incomplete round, not a deterministic
    # contract failure: the domain keeps its one bounded targeted re-research,
    # which is precisely the round that switches modality.
    if text.startswith("provider_capability:"):
        return ProviderFailureClassification(
            category="incomplete",
            reason_code="provider_capability_declined",
        )
    if any(marker in text for marker in _DETERMINISTIC_MARKERS):
        return ProviderFailureClassification(
            category="deterministic",
            reason_code="provider_deterministic_failure",
        )
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return ProviderFailureClassification(
            category="transient",
            reason_code="provider_transient_failure",
        )
    if any(marker in text for marker in _QUERY_MISS_MARKERS):
        return ProviderFailureClassification(
            category="incomplete",
            reason_code="provider_empty_result",
        )
    return ProviderFailureClassification(
        category="incomplete",
        reason_code="provider_incomplete_result",
    )


def is_provider_or_model_failure(value: object) -> bool:
    """Return whether a worker failure carries an explicit external/model signal.

    A blank or purely local malformed-state message remains Delivery Integrity;
    it must not be quietly reclassified as a content gap.
    """
    text = str(value or "").casefold()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            *_DETERMINISTIC_MARKERS,
            *_TRANSIENT_MARKERS,
            *_QUERY_MISS_MARKERS,
            *_EXPLICIT_EXTERNAL_FAILURE_MARKERS,
        )
    )
