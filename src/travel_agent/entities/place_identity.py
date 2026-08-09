"""Stable place_id synthesis for core geography identity.

JourneyPilot connectors, Research Packets, and composition materialization
require a non-empty stable ``place_id`` on physical places and transport
endpoints. IDs are **provider-native strings**, not a global gazetteer:

| Namespace | Form | Source of truth |
|---|---|---|
| 12306 | ``12306:{station_telecode}`` | China railway station telecode |
| amap POI | ``amap:poi:{poi_id}`` | Amap maps_text_search / detail POI id |
| OSM | ``osm:{osm_type}:{osm_id}`` | Nominatim / OpenStreetMap object |

Contract:
- Same provider key → same place_id across runs (determinism).
- Downstream only requires a non-empty stable string; OSM resolvability is
  **not** required (12306 stations have no OSM id by design).
- Missing provider keys → **fail-closed** (return None / reject candidate).
  Do **not** invent ``unstable:…`` or model-authored ids.
- Do not add namespaces unless a real provider integration lands.
"""

from __future__ import annotations

import re
from typing import Optional

_OSM_TYPE = re.compile(r"^(node|way|relation)$")


def stable_place_id_12306(station_telecode: str) -> Optional[str]:
    """Return ``12306:{telecode}`` or None when the telecode is empty.

    Telecodes are usually three Latin letters (e.g. BJP), but providers may
    emit other non-empty station codes; only emptiness fails closed.
    """
    code = str(station_telecode or "").strip().upper()
    if not code:
        return None
    return f"12306:{code}"


def stable_place_id_amap_poi(poi_id: str) -> Optional[str]:
    """Return ``amap:poi:{poi_id}`` or None when poi_id is empty."""
    poi = str(poi_id or "").strip()
    if not poi:
        return None
    if poi.startswith("amap:poi:"):
        return poi
    return f"amap:poi:{poi}"


def stable_place_id_osm(osm_type: str, osm_id: str | int) -> Optional[str]:
    """Return ``osm:{type}:{id}`` or None when type/id are invalid."""
    kind = str(osm_type or "").strip().lower()
    raw_id = str(osm_id or "").strip()
    if not _OSM_TYPE.fullmatch(kind):
        return None
    if not raw_id.isdigit() or raw_id == "0":
        return None
    return f"osm:{kind}:{raw_id}"
