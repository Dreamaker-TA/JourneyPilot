"""WGS-84 ⇄ GCJ-02 datum conversion for amap-sourced coordinates.

amap speaks GCJ-02 ("火星坐标") in both directions. The map projection and every
OSM-sourced place identity are WGS-84, and the frontend renders OSM tiles, so an
amap point must be converted before it joins them — otherwise it lands a few
hundred metres off — and a WGS-84 point must be converted before it is handed
back to amap as a query. The offset only exists inside China;
:func:`within_china_coordinate_box` makes both conversions a no-op elsewhere,
which is what amap already returns overseas.

That box has a second reader: choosing which routing provider can answer a
route at all (``services/global_route_search``). It is deliberately the *same*
box and not a second copy of "is this China" — one definition, checked in one
place, is the whole point of that discipline. The box is a rectangle
rather than a border, so it takes in slices of neighbouring countries; for both
readers that is the safe direction to err, because those slices are exactly
where amap has data and the global transit aggregator has none.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Tuple

_GCJ_A = 6378245.0                    # 克拉索夫斯基椭球长半轴
_GCJ_EE = 0.00669342162296594323      # 第一偏心率平方


def within_china_coordinate_box(lng: float, lat: float) -> bool:
    """Whether this point is inside the rectangle where GCJ-02 differs from WGS-84."""
    return 73.66 < lng < 135.05 and 3.86 < lat < 53.55


def _gcj_transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _gcj_transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _gcj_offset(lng: float, lat: float) -> Tuple[float, float]:
    """The GCJ-02 offset at one point, as ``(dlng, dlat)`` in degrees."""
    dlat = _gcj_transform_lat(lng - 105.0, lat - 35.0)
    dlng = _gcj_transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _GCJ_EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_GCJ_A * (1 - _GCJ_EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_GCJ_A / sqrtmagic * math.cos(radlat) * math.pi)
    return dlng, dlat


def wgs84_to_gcj02(lng: float, lat: float) -> Tuple[float, float]:
    """Convert one WGS-84 point to amap's GCJ-02; outside China it is returned as-is."""
    if not within_china_coordinate_box(lng, lat):
        return lng, lat
    dlng, dlat = _gcj_offset(lng, lat)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng: float, lat: float) -> Tuple[float, float]:
    """Convert one amap GCJ-02 point to WGS-84; a point outside China is returned as-is."""
    if not within_china_coordinate_box(lng, lat):
        return lng, lat
    dlng, dlat = _gcj_offset(lng, lat)
    return lng - dlng, lat - dlat


def amap_location_to_wgs84(location: Any) -> Tuple[Optional[float], Optional[float]]:
    """Parse one amap ``"lng,lat"`` string into a WGS-84 ``(latitude, longitude)``."""
    if not isinstance(location, str) or "," not in location:
        return None, None
    lng_text, _, lat_text = location.partition(",")
    try:
        longitude = float(lng_text)
        latitude = float(lat_text)
    except ValueError:
        return None, None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None, None
    longitude, latitude = gcj02_to_wgs84(longitude, latitude)
    return latitude, longitude
