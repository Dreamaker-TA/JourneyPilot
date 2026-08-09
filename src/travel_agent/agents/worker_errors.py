"""Standardized worker ``last_error`` prefixes for gate classification.

Workers write a single string into ``TravelAgentState.last_error``. Artifact Gate
and Candidate Gate classify with substring markers in
``orchestrator.provider_failure``. Prefixes make schema vs provider outcomes
explicit without a parallel structured error channel.
"""

from __future__ import annotations

from typing import Final

from .orchestrator.provider_failure import (
    _DETERMINISTIC_MARKERS,
    _TRANSIENT_MARKERS,
)
from .research_packet_output import ResearchPacketOutputError

PREFIX_SCHEMA_GATE: Final = "schema_gate:"
PREFIX_PROVIDER_EMPTY: Final = "provider_empty:"
PREFIX_PROVIDER_CAPABILITY: Final = "provider_capability:"
PREFIX_PROVIDER_TRANSIENT: Final = "provider_transient:"
PREFIX_PROVIDER_DETERMINISTIC: Final = "provider_deterministic:"
PREFIX_WORKER_FAILED: Final = "worker_failed:"

_KNOWN_PREFIXES: Final = (
    PREFIX_SCHEMA_GATE,
    PREFIX_PROVIDER_EMPTY,
    PREFIX_PROVIDER_CAPABILITY,
    PREFIX_PROVIDER_TRANSIENT,
    PREFIX_PROVIDER_DETERMINISTIC,
    PREFIX_WORKER_FAILED,
)

_QUERY_MISS_MARKERS = (
    "found no executable route",
    "no results",
    "empty result",
)




def _already_prefixed(text: str) -> bool:
    lowered = text.casefold()
    return any(lowered.startswith(prefix.casefold()) for prefix in _KNOWN_PREFIXES)


def format_worker_last_error(
    exc: BaseException,
    *,
    provider_empty_round: bool = False,
    provider_capability_round: bool = False,
) -> str:
    """Return a classifier-friendly last_error string for research workers.

    ``provider_empty_round`` is the honest empty-success signal computed by
    ``research_packet_output.provider_round_answered_empty``: the Provider
    completed its calls, reported zero hits, and nothing in the round failed.
    The Packet exception that surfaces afterwards is a *symptom* of having
    nothing to serialize, so it must not be attributed to the schema layer —
    ``schema_gate:`` names the wrong failure and classifies as deterministic,
    which closes the domain without the bounded targeted re-research that a
    legitimately empty Provider answer is entitled to.

    ``provider_capability_round`` is the same argument one step earlier in the
    call chain, computed by
    ``research_packet_output.provider_round_capability_declared``: the Gateway
    decided *before* any call that the Provider cannot answer the requested
    date.  It takes precedence over ``provider_empty_round`` because it is the
    more specific statement — the Provider did not merely find nothing, it was
    never asked — and because it is the one the next bounded round must read as
    "switch modality", not "ask the same Provider again".
    """
    text = str(exc).strip() or type(exc).__name__
    if _already_prefixed(text):
        return text

    if provider_capability_round:
        return f"{PREFIX_PROVIDER_CAPABILITY} {text}"

    if provider_empty_round:
        return f"{PREFIX_PROVIDER_EMPTY} {text}"

    if isinstance(exc, ResearchPacketOutputError):
        return f"{PREFIX_SCHEMA_GATE} {text}"

    lowered = text.casefold()

    # Packet/schema language without the typed exception (repair path, etc.).
    if (
        "research packet" in lowered
        or "worker output" in lowered
        or "json object" in lowered
        or "json parse" in lowered
        or ("validation error" in lowered and "research" in lowered)
    ):
        return f"{PREFIX_SCHEMA_GATE} {text}"

    if any(marker in lowered for marker in _DETERMINISTIC_MARKERS):
        return f"{PREFIX_PROVIDER_DETERMINISTIC} {text}"
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return f"{PREFIX_PROVIDER_TRANSIENT} {text}"
    if any(marker in text or marker in lowered for marker in _QUERY_MISS_MARKERS):
        return f"{PREFIX_PROVIDER_EMPTY} {text}"

    return f"{PREFIX_WORKER_FAILED} {text}"
