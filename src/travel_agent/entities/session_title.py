"""The single authority for what a conversation is called in the sidebar.

**A title is not the first 20 characters of whatever the traveller typed.**  For a
free-text question that would be fine — the question *is* the subject.  For a trip
planned through the structured planner it is not: that request is generated from a
form, so every one of them opens with the same clause, and the sidebar fills with rows
that are **byte-identical after truncation**:

    帮我规划 2026-08-05 到 20...
    帮我规划 2026-08-06 到 20...
    帮我规划 2026-08-06 到 20...

A list whose rows cannot be told apart is not a list; the traveller has to open each
one to find out which trip it is.

The rule is one rule with two subjects: **a session is named after what it is
about.**  When the request carries a confirmed trip identity the subject is that
trip, so the title is its route and its dates.  When it does not, the subject is
the question, so the title is the question's opening.  This is not a
"field missing → old behaviour" fallback: a visa question has no route to print,
and printing one would be inventing a trip the traveller never asked for.

Derived once, when the session row is created.  Renames overwrite it and are
never re-derived — a traveller's own name for a trip outranks ours.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

# One cap for the column, shared by derived titles and renames.  A route title
# for two destinations is the longest thing this module produces:
#     上海市 → 东京都/東京都、京都市 · 8/5–8/8   →  31 characters
# so 32 is the smallest cap that never truncates one.  The rename input mirrors
# it (`ConversationList`), because two caps on one column is how they drift.
TITLE_MAX_LEN = 32

FALLBACK_TITLE = "新对话"

# Beyond two named destinations a route line stops being scannable, which is the
# entire point of the title.  Three or more get counted instead of listed.
_MAX_NAMED_DESTINATIONS = 2

_ROUTE_ARROW = " → "
_DESTINATION_JOIN = "、"
# En dash, matching the date ranges the report cover and PDF already print.
_DATE_RANGE_DASH = "–"


def _place_name(place: Any) -> Optional[str]:
    """The provider's own name for a place, or nothing.

    Deliberately **not** normalised.  ``东京都/東京都`` is the name OSM answers with, and
    a client-side rewrite (简繁归一 included) would be a second authority for a
    user-visible string that the itinerary, the map and the report all print from the
    provider.
    """

    if not isinstance(place, Mapping):
        return None
    name = str(place.get("name") or "").strip()
    return name or None


def _destination_names(destinations: Any) -> list[str]:
    if not isinstance(destinations, Sequence) or isinstance(destinations, (str, bytes)):
        return []
    names: list[str] = []
    for entry in destinations:
        name = _place_name(entry)
        if name and name not in names:
            names.append(name)
    return names


def _destinations_label(names: Sequence[str]) -> Optional[str]:
    if not names:
        return None
    if len(names) <= _MAX_NAMED_DESTINATIONS:
        return _DESTINATION_JOIN.join(names)
    return f"{names[0]} 等 {len(names)} 地"


def _short_date(value: Any) -> Optional[str]:
    """``2026-08-05`` → ``8/5``.

    Month and day only: the year is the same on both ends of every trip this
    product plans (≤14 days), so printing it twice spends a third of the title
    on a constant.
    """

    text = str(value or "").strip()
    parts = text.split("-")
    if len(parts) != 3:
        return None
    try:
        month, day = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{month}/{day}"


def _date_range_label(identity: Mapping[str, Any]) -> Optional[str]:
    start = _short_date(identity.get("start_date"))
    end = _short_date(identity.get("end_date"))
    if start and end:
        return start if start == end else f"{start}{_DATE_RANGE_DASH}{end}"
    return start or end


def trip_shaped_title(identity: Optional[Mapping[str, Any]]) -> Optional[str]:
    """``上海市 → 东京都/東京都 · 8/5–8/8``, or nothing when there is no trip.

    Nothing is returned unless a **destination** is known: an origin and a date
    range with no destination names no trip, and a bare date range names any
    trip at all — which is the defect this module exists to remove.
    """

    if not isinstance(identity, Mapping) or not identity:
        return None

    destinations = _destinations_label(_destination_names(identity.get("destinations")))
    if not destinations:
        return None

    origin = _place_name(identity.get("origin"))
    route = f"{origin}{_ROUTE_ARROW}{destinations}" if origin else destinations
    dates = _date_range_label(identity)
    return f"{route} · {dates}" if dates else route


def _question_shaped_title(user_message: str) -> str:
    text = (user_message or "").strip()
    if not text:
        return FALLBACK_TITLE
    if len(text) <= TITLE_MAX_LEN:
        return text
    return text[:TITLE_MAX_LEN] + "..."


def derive_session_title(
    *,
    user_message: str,
    controlled_trip_identity: Optional[Mapping[str, Any]] = None,
) -> str:
    """Name a session after its subject — the trip if there is one, else the ask."""

    shaped = trip_shaped_title(controlled_trip_identity)
    if shaped:
        # A route title is built from four short fields and cannot outgrow the
        # cap; asserting it here would be dead code, so it is simply trimmed the
        # same way a rename is.
        return shaped[:TITLE_MAX_LEN]
    return _question_shaped_title(user_message)
