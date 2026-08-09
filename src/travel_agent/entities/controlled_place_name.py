"""The one place that decides which anchor field is a destination's public name.

Two layers need this string and they need it for the same reason — it becomes the
subject of a sentence a traveller reads:

* ``entities/itinerary_composition_v2.py`` names each Day with it (「深圳市 · 交通日」).
* ``services/delivery_projection.py`` puts it on the report cover and every
  weather row.

**Neither may read the anchor directly.**  Both would then write down which field to
take, and they drift to **different fields** — one on ``display_name``, one on ``name``
— so a fix that looks complete leaves Day headings still printing
``深圳市, 广东省, 中国 · 交通日``.  This drift survives review because everyone agrees on
the *source* (the anchor) and nobody restates the *field*; "a reader, not a second
source" can be true about the anchor and false about the field at the same time.

So the field choice lives here, once. ``name`` is the place name (``深圳市``);
``display_name`` is a full administrative path (``深圳市, 广东省, 中国``) and is a
whole address where a place name belongs. ``admin_path`` stays on the anchor for
anyone who genuinely needs to disambiguate.
"""

from __future__ import annotations

from typing import Mapping, Optional

CONTROLLED_DESTINATION_ANCHOR_PREFIX = "controlled_trip_identity.destinations."


def controlled_public_place_name(value: Mapping) -> Optional[str]:
    """Return the public place name carried by a controlled-identity anchor value.

    ``None`` when the value carries no usable name; callers decide whether that is
    tolerable (composition falls back to naming a Day by its own places) or fatal
    (the projection refuses to publish).
    """

    name = str(value.get("name") or "").strip()
    return name or None
