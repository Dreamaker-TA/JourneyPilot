"""The single authority for a Day's title.

The theme is **derived from what the Day actually holds** — never free text a model
writes.  Model-written prose in a field with no guidance behind it gets carried forward
untouched by every later ``model_copy`` and is never checked against the Day it names:
a Day holding no canal entry ends up titled 「运河遗韵与返程」, and an empty title
reaches the workspace card, the report and the PDF unremarked.

Derivation is plainer than prose a model would write, and that is the trade: it can
always be reconciled against the Day's placements, which is the property a delivered
plan needs.

The derivation runs once, at materialization, and the report copies the result.
Deriving it a second time at projection would be the same defect this module
exists to remove: two authorities for one user-visible string, free to disagree
about the same Day.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .delivery_bundle import DiningStop, TransportLeg, VisitStop


_MAX_LEAD_LENGTH = 24

_FALLBACK_LEAD = "本日行程"

# Suffixes are chosen by what the Day is mostly made of, in priority order.  Each
# is a claim about the Day that its own entities support.
_TRANSFER_ONLY = "交通日"
_TRANSFER_AND_VISITS = "转场与游览"
_VISIT_HEAVY = "景点为主"
_VISITS_AND_DINING = "游览与美食"
_SINGLE_VISIT = "慢逛一日"
_DINING_ONLY = "美食为主"
_OPEN = "自由安排"

_VISIT_HEAVY_THRESHOLD = 3


def _lead(
    *,
    destination_name: Optional[str],
    visits: Sequence[VisitStop],
    dining: Sequence[DiningStop],
) -> str:
    """Name the Day by the places it is actually built around."""

    names: list[str] = []
    for stop in visits:
        name = stop.name.strip()
        if name and name not in names:
            names.append(name)
        if len(names) == 2:
            break
    if not names:
        for stop in dining:
            name = stop.name.strip()
            if name:
                names.append(name)
                break
    lead = "与".join(names)
    if not lead:
        lead = (destination_name or "").strip() or _FALLBACK_LEAD
    if len(lead) > _MAX_LEAD_LENGTH:
        # Prefer one whole name over two truncated ones.
        lead = names[0][:_MAX_LEAD_LENGTH] if names else lead[:_MAX_LEAD_LENGTH]
    return lead


def _suffix(
    *,
    visit_count: int,
    dining_count: int,
    long_distance_count: int,
) -> str:
    if long_distance_count and not visit_count and not dining_count:
        return _TRANSFER_ONLY
    if long_distance_count:
        return _TRANSFER_AND_VISITS
    if visit_count >= _VISIT_HEAVY_THRESHOLD:
        return _VISIT_HEAVY
    if visit_count and dining_count:
        return _VISITS_AND_DINING
    if visit_count:
        return _SINGLE_VISIT
    if dining_count:
        return _DINING_ONLY
    return _OPEN


def derive_day_theme(
    *,
    destination_name: Optional[str],
    visits: Sequence[VisitStop],
    dining: Sequence[DiningStop],
    long_distance_legs: Sequence[TransportLeg],
) -> str:
    """Title one Day from the entities it holds.

    ``destination_name`` is the controlled public name of the Day's destination
    when the caller has the identity anchors to resolve it; it is only used when
    the Day names no place of its own, which happens on a pure transfer Day.
    """

    lead = _lead(destination_name=destination_name, visits=visits, dining=dining)
    suffix = _suffix(
        visit_count=len(visits),
        dining_count=len(dining),
        long_distance_count=len(long_distance_legs),
    )
    return f"{lead} · {suffix}"
