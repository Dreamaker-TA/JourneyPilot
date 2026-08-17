"""Rate-limited, cached global place search shared by API intake and research."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Literal, Optional

import httpx

from ..config import get_settings
from ..entities.place_identity import stable_place_id_osm
from ..infrastructure.redis_client import get_redis
from ..utils.rate_gate import RateGate, rate_gate_for
from .destination_scope import annotate_destination_distance
from .candidate_admission import (
    is_administrative_provider_type,
    provider_place_type_matches_candidate_kind,
)

logger = logging.getLogger(__name__)

_COUNTRY_CODE = re.compile(r"^[a-z]{2}$")
_OSM_RELATION_PLACE_ID = re.compile(r"^osm:relation:(?P<relation_id>[1-9]\d*)$")
_DINING_PLACE_TYPES = {
    "amenity;restaurant",
    "amenity;cafe",
    "amenity;fast_food",
    "amenity;bar",
    "amenity;pub",
    "amenity;food_court",
}
_GENERIC_DINING_NAMES = {
    "ramen",
    "restaurant",
    "cafe",
    "bar",
    "pub",
    "ラーメン",
    "拉面",
    "拉麵",
    "餐厅",
    "餐廳",
}
_DINING_COLLECTION_TOKENS = (
    "food court",
    "kokugikan",
    "ramen street",
    "market",
    "国技館",
    "横丁",
    "市場",
    "市场",
    "美食街",
)
# OSM lodging records whose ``name`` tag is only the category word. Nominatim
# happily returns them for a category-level query and they carry no bookable
# property identity.
_GENERIC_LODGING_NAMES = {
    "hotel",
    "hôtel",
    "hostel",
    "motel",
    "inn",
    "resort",
    "apartment",
    "apartments",
    "guest house",
    "guesthouse",
    "guest-house",
    "b&b",
    "bed and breakfast",
    "pension",
    "ryokan",
    "ホテル",
    "旅館",
    "ゲストハウス",
    "民宿",
    "酒店",
    "飯店",
    "饭店",
    "宾馆",
    "賓館",
    "旅馆",
    "招待所",
}

# ── rung2 单 alias × 多 predicate 的用时探针（零 LLM）──────────────────────
#
# 六城生产短名，先用 search_nominatim_raw 取受控 relation 与中心点，再按 rung2 的新
# 形态调 search_overpass_place_ids([单个 alias], kind=...)：每次调用前抹掉速率闸等待，
# 只记 wall-clock 秒 / 命中数。命中名取该城 'museum in <短名>' 返回的真实场馆名；
# 未命中名取一个不存在的字符串（最坏情况：全 bbox 把本域每条 predicate 都扫空）。
#
#   城市        visit 真名(5 predicate)  visit 不存在名        dining 不存在名(1)
#   大阪市       72.5s ERR               39.0s ERR            36.8s ERR
#   巴黎         49.7s ERR               50.8s ERR            37.0s 0 命中
#   曼谷         51.1s ERR               54.5s ERR            28.4s ERR
#   纽约;紐約    74.0s ERR               33.8s ERR            23.2s 0 命中
#   京都市       54.3s ERR               51.7s ERR            35.5s 0 命中
#   罗马         50.7s ERR               52.3s ERR            30.1s ERR
#
# ERR = overpass-api.de 当日过载，连续 504，_send_with_retry 用尽 3 次尝试后抛
# provider unavailable；「0 命中」= 查询真的跑完了，区域内没有这个名字。当日拿不到
# 一次成功的 visit 查询，所以命中路径的耗时这一轮没有测到——能确定的是下界：连只有
# 1 条 predicate 的 dining 每条别名都要 23-37s，5 条 predicate 的 visit 一次都没在
# 重试窗口内回话。别名上界 4 条，不设时间片时一次工具调用最坏能花掉 140-300s，而
# Candidate Gate 每域只有 1 次定向补研、调研阶段整体只有 6 分钟。
#
# Tag filters per candidate kind. Each entry is one Overpass tag predicate; a
# kind with several predicates emits one statement per predicate, and every
# statement scans the whole destination bbox on its own.  Visit carries five of
# them against Dining's one, which is what sets the identity-fallback budget
# below.
_OVERPASS_TAG_FILTERS: Dict[str, tuple[str, ...]] = {
    "dining": (
        '["amenity"~"^(restaurant|cafe|fast_food|bar|pub|food_court)$"]',
    ),
    "visit": (
        '["tourism"~"^(attraction|museum|gallery|artwork|viewpoint|theme_park|zoo|aquarium)$"]',
        '["historic"]',
        '["leisure"~"^(park|garden|nature_reserve|water_park)$"]',
        '["amenity"~"^(place_of_worship|theatre|arts_centre|marketplace)$"]',
        '["shop"~"^(mall|department_store)$"]',
    ),
}


PlaceProviderFailureCode = Literal[
    # provider 没干活：HTTP 失败、重试耗尽，或它自己在响应体里说没跑完。
    "provider_unavailable",
    # 答了，但响应体不是 JSON——通常是对端或中间代理在返错误页。
    "provider_unparseable",
    # 是合法 JSON，但形状不对——对端契约变了。
    "provider_invalid_payload",
]


class NominatimPlaceSearchError(RuntimeError):
    """The global place provider could not return a valid response.

    ``code`` 是必填的。三种失败在运维上要区别对待，而路由会把 message 丢掉、换成一句
    本地化字符串 —— 只靠英文 message 区分，区别就在边界上没了。
    判据放在异常上，**不靠 message 匹配**：靠 message 认失败类型是另一条隐式合同。
    """

    def __init__(self, message: str, *, code: PlaceProviderFailureCode) -> None:
        super().__init__(message)
        self.code = code




# Two upstreams, two gates: Nominatim and Overpass answer separately and each
# publishes its own courtesy interval.
_RATE_GATE = rate_gate_for("nominatim")
_OVERPASS_RATE_GATE = rate_gate_for("overpass")

# Public OSM providers shed load (5xx / read timeouts) under the many place
# lookups one research round fires. A single such blip must not collapse the
# whole place tool to the web-search fallback — that fallback cannot supply the
# branch-level identity Dining admission strictly requires, so one blip would
# otherwise fail the entire run at Candidate Gate. Retry the transient classes a
# bounded number of times, spacing each attempt through the same per-provider
# rate gate; genuine bad requests and invalid payloads still surface immediately.
#
# Two attempts, not three. ``/search`` is the synchronous first hop of a session:
# with the 3.4s search timeout the whole ladder is bounded by
# 1.1 rate-gate + 2 × 3.4 ≈ 7.9s, against 32.4s for the old
# 3 × 10.0 + 0.8 + 1.6 shape (measured 33.6-34.1s during a public-instance
# brownout). The third attempt only ever paid off when the
# provider was already failing two in a row, which is exactly the brownout it
# cannot recover from.
#
# There is no separate backoff sleep, because the rate gate already is one. Every
# attempt goes through ``rate_gate.acquire(min_interval_seconds)``, so two
# attempts are spaced by at least ``min_interval_seconds`` (1.1s) whatever the
# first one cost: a fast 5xx waits out the remaining interval, and an attempt
# that burned its whole timeout waited far longer than the interval already. The
# old fixed 0.8/1.6 ladder was therefore double-counting for a fast failure and
# pure added latency for a slow one.
_PROVIDER_MAX_ATTEMPTS = 2
# 429 is deliberately absent. A 429 is the provider telling us we are already
# over its 1 req/s policy; re-firing 0.8s later is both *below* our own
# ``min_interval_seconds`` floor (1.1s) and more pressure at the exact moment we
# were told to back off. Honouring ``Retry-After`` instead would mean parking a
# synchronous intake request for whatever delay the server names (commonly ≥60s
# and frequently absent from Nominatim's 429), which re-creates the tens-of-
# seconds hang this ladder exists to remove. So a 429 fails immediately and
# audibly; the process-global rate gate, not a retry, is what keeps us under the
# policy in the first place.
_TRANSIENT_STATUS = frozenset({500, 502, 503, 504})

# Half-size, in kilometres, of the box a controlled destination's own point puts
# around a place query.  It answers "where is this destination, actually" for a
# query whose text does not say — ``restaurant in <destination>`` and
# ``museum in <destination>`` name a category and an administrative area, and an
# administrative area is not a location: 东京都 reaches ~50km west of the city
# every traveller means by it, so Nominatim's own relevance order handed back the
# same three restaurants on one street in 小金井市, 23km out, on 8 runs out of 8.
#
# 15km is read off the measured distribution rather than off this one query: over
# 48 domestic day-samples the widest healthy Day spans 14.81km,
# so a 15km half-size box is the smallest one that still comfortably holds a whole
# day's worth of a destination.  The outcome does not balance on the exact value —
# 10 / 12 / 15 / 20km all return the same central answers for the Tokyo query, and
# only at 25km does the 23km cluster fall back inside the box.  That plateau is
# the reason this is a box and not a threshold: nothing is accepted or rejected by
# it.  It is *not* ``authored_place_resolution._MAX_DESTINATION_DISTANCE_KM``
# (25km), which is a different quantity — that one rejects a resolved place for
# belonging to another city, this one tells the provider where to look first.
_DESTINATION_FOCUS_HALF_SIZE_KM = 15.0
# One degree of latitude on the sphere the haversine formula assumes.
_KM_PER_DEGREE_LATITUDE = 2 * math.pi * 6371.0088 / 360


def _is_coordinate(value: Any) -> bool:
    """Whether a value is a usable coordinate. ``True`` is an ``int``, not a point."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def destination_focus_viewbox(latitude: float, longitude: float) -> str:
    """The Nominatim ``viewbox`` a destination's own point defines.

    A *preference*, never a filter: the caller must not pair it with
    ``bounded=1``.  Soft is the whole point — a query that names a place outside
    the box still resolves to that place (日光東照宮 120km north and 箱根神社 80km
    south-west return byte-identical answers with and without it), while a query
    that names no place stops being answered from the far edge of a prefecture.
    Making it a filter would trade the defect for a worse one: a named stop the
    traveller asked for by name would become unresolvable.

    The box is square in kilometres, not in degrees, so it does not stretch
    east-west as the destination moves away from the equator.
    """

    latitude_span = _DESTINATION_FOCUS_HALF_SIZE_KM / _KM_PER_DEGREE_LATITUDE
    # Meridians converge; at the poles the box would be the whole world, so the
    # longitude half-width is bounded by a half-turn rather than left to blow up.
    parallel_km = _KM_PER_DEGREE_LATITUDE * math.cos(math.radians(latitude))
    longitude_span = (
        180.0
        if parallel_km <= 0
        else min(180.0, _DESTINATION_FOCUS_HALF_SIZE_KM / parallel_km)
    )
    return (
        f"{longitude - longitude_span:.6f},{latitude - latitude_span:.6f},"
        f"{longitude + longitude_span:.6f},{latitude + latitude_span:.6f}"
    )


def _is_transient_provider_error(exc: httpx.HTTPError) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS
    # Timeouts, resets and other transport failures are retryable by nature.
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


async def _send_with_retry(
    send,
    *,
    rate_gate: RateGate,
    min_interval_seconds: float,
    unavailable_message: str,
) -> httpx.Response:
    """Run one rate-gated request, retrying only transient provider failures.

    The rate gate is the only spacing between attempts; see
    ``_PROVIDER_MAX_ATTEMPTS`` for why there is no second backoff on top of it.
    """
    last_exc: Optional[httpx.HTTPError] = None
    for attempt in range(_PROVIDER_MAX_ATTEMPTS):
        try:
            await rate_gate.acquire(min_interval_seconds)
            response = await send()
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            last_exc = exc
            if _is_transient_provider_error(exc) and attempt + 1 < _PROVIDER_MAX_ATTEMPTS:
                continue
            raise NominatimPlaceSearchError(
                unavailable_message, code="provider_unavailable"
            ) from exc
    raise NominatimPlaceSearchError(
        unavailable_message, code="provider_unavailable"
    ) from last_exc


# ── Raw ``/search`` reuse ─────────────────────────────────────────────────────
#
# Why this is **not** ``infrastructure.provider_snapshot_cache``:
#
# ``ProviderSnapshotCache`` is the evidence vehicle. Everything it stores is
# declared ``evidence_eligible``, is minted only from a post-Gateway envelope
# whose ``metadata.evidence_allowed`` is True, must be a non-empty ``dict``
# carrying ``success``/``results`` (``_provider_snapshot_has_content``), is keyed
# by a ``tool_name`` from a closed ``_SUPPORTED_TOOLS`` set, and exists so a
# Candidate can cite it through ``to_provenance()``. A raw ``/search`` body is a
# bare JSON *list* that never went through a tool, a Gateway or an allowlist.
# Putting it there would mean fabricating a tool-shaped envelope and asserting
# evidence eligibility no Gateway ever granted — bending exactly the governance
# that keeps a cached geocode from being read as a fresh provider observation.
#
# So: a separate, narrower cache that stores the provider bytes **with the
# instant the provider actually produced them**. Reuse never fabricates
# freshness — ``search_nominatim_raw`` hands its caller that true
# ``observed_at``, and ``global_place_search`` publishes it, so
# ``build_provider_snapshot_record`` derives ``provider_valid_until`` from the
# real observation and the SourceRecord provenance inherits it unchanged.
_SEARCH_CACHE_SCHEMA_VERSION = "nominatim.search.raw.v1"


@dataclass(frozen=True)
class NominatimSearchObservation:
    """One ``/search`` answer plus when the provider actually produced it.

    ``observed_at`` is the provider observation instant, not the moment this
    process read it: on a reuse it is the timestamp of the original live call.
    Callers that turn this into evidence must publish *this* value, never
    ``now`` — that difference is the whole reason the raw body is reusable.
    """

    items: list[Dict[str, Any]]
    observed_at: datetime
    origin: Literal["live", "cache"]


def _search_cache_digest(base_url: str, params: Dict[str, Any]) -> str:
    """The full identity of one provider request: endpoint plus every parameter.

    ``params`` already carries the normalized query, the country filter, the
    limit and the accept-language, so hashing it whole means a cache entry can
    only ever serve a byte-identical request.
    """
    payload = json.dumps(
        {
            "schema_version": _SEARCH_CACHE_SCHEMA_VERSION,
            "base_url": base_url.rstrip("/"),
            "params": {str(key): params[key] for key in sorted(params)},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class NominatimSearchCache:
    """Redis reuse of raw ``/search`` bodies. Redis loss is a miss, never a fault.

    Only non-empty successful answers are stored. An empty answer is a statement
    about one query phrasing at one instant — the authored-place ladder walks
    seven phrasings and intake sees a half-typed word — and pinning "no such
    place" for a week would mask every newly indexed OSM object. A provider
    failure is not stored for the same reason, and more bluntly: a brownout must
    not become the answer.
    """

    def __init__(self, *, redis: Any = None) -> None:
        self._redis = redis

    def _client(self) -> Any:
        return self._redis if self._redis is not None else get_redis()

    def _key(self, digest: str) -> str:
        return f"{get_settings().geocoding.search_cache_key_prefix}:{digest}"

    def _timeout(self) -> float:
        return max(0.01, get_settings().geocoding.search_cache_redis_timeout_seconds)

    async def get(self, digest: str) -> Optional[NominatimSearchObservation]:
        try:
            raw = await asyncio.wait_for(
                self._client().get(self._key(digest)),
                timeout=self._timeout(),
            )
        except Exception as exc:
            logger.info("nominatim search cache read unavailable: %s", exc)
            return None
        if raw is None:
            return None
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            if payload.get("schema_version") != _SEARCH_CACHE_SCHEMA_VERSION:
                return None
            items = payload["items"]
            observed_at = datetime.fromisoformat(payload["observed_at"])
        except Exception:
            return None
        if (
            not isinstance(items, list)
            or not items
            or any(not isinstance(item, dict) for item in items)
            or observed_at.tzinfo is None
        ):
            return None
        return NominatimSearchObservation(
            items=[dict(item) for item in items],
            observed_at=observed_at,
            origin="cache",
        )

    async def set(self, digest: str, observation: NominatimSearchObservation) -> None:
        if not observation.items:
            return
        settings = get_settings().geocoding
        # The entry may not outlive the observation it carries: a reused body is
        # allowed to be up to the TTL old, never older.
        expires_at = observation.observed_at + timedelta(
            seconds=settings.search_cache_ttl_seconds
        )
        ttl_seconds = int((expires_at - datetime.now(timezone.utc)).total_seconds())
        if ttl_seconds <= 0:
            return
        payload = json.dumps(
            {
                "schema_version": _SEARCH_CACHE_SCHEMA_VERSION,
                "observed_at": observation.observed_at.isoformat(),
                "items": observation.items,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            await asyncio.wait_for(
                self._client().set(self._key(digest), payload, ex=ttl_seconds),
                timeout=self._timeout(),
            )
        except Exception as exc:
            logger.info("nominatim search cache write unavailable: %s", exc)


_SEARCH_CACHE = NominatimSearchCache()


def normalize_country_code(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip().casefold() or None
    if normalized is not None and not _COUNTRY_CODE.fullmatch(normalized):
        raise ValueError("country_code must be a two-letter ISO 3166-1 code")
    return normalized


_DISPLAY_NAME_PREFERENCE = ("name:zh", "name:zh-Hans")


def _preferred_display_name(namedetails: Dict[str, Any], provider_name: str) -> str:
    """Pick the name to show a Chinese-reading traveller, from what the provider has."""

    for key in _DISPLAY_NAME_PREFERENCE:
        candidate = str(namedetails.get(key) or "").strip()
        if candidate:
            return candidate
    return provider_name


def normalize_nominatim_place(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Project one provider item without inventing identity or category fields."""
    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    country_code = str(address.get("country_code") or "").strip().casefold()
    osm_type = str(item.get("osm_type") or "").strip().casefold()
    osm_id = str(item.get("osm_id") or "").strip()
    display_name = str(item.get("display_name") or "").strip()
    namedetails = item.get("namedetails") if isinstance(item.get("namedetails"), dict) else {}
    provider_name = str(
        item.get("name") or namedetails.get("name") or display_name.split(",", 1)[0]
    ).strip()
    # Prefer the place's Chinese name where the provider has one.  Nominatim's
    # ``name`` is the local sign, so an itinerary came out mixing 「明治神宮」 with
    # 「银座」 — same plan, two languages, for no reason a reader could see.  The zh
    # aliases were already being fetched (``_NAME_DETAIL_KEYS``) and used only for
    # matching, never for display.
    #
    # Provider-supplied only.  No hardcoded translation table: a hand-maintained
    # name map is a second source of truth that goes stale silently, which is the
    # shape of口子 9 and口子 26.  A place with no zh name keeps its local one — which
    # is also the name on the sign the traveller will be standing in front of.
    name = _preferred_display_name(namedetails, provider_name)
    if (
        not _COUNTRY_CODE.fullmatch(country_code)
        or osm_type not in {"node", "way", "relation"}
        or not osm_id
        or not display_name
        or not name
    ):
        return None
    provider_types = list(
        dict.fromkeys(
            value
            for value in (
                str(item.get("category") or item.get("class") or "").strip(),
                str(item.get("type") or "").strip(),
                str(item.get("addresstype") or "").strip(),
            )
            if value
        )
    )
    if not provider_types:
        return None
    try:
        latitude = float(item["lat"])
        longitude = float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    aliases = list(
        dict.fromkeys(
            alias
            for alias in (
                # The local-language name the provider indexes this place under.
                # It has to stay reachable: it is what matching recalls on, and it
                # is the name written on the sign.
                provider_name,
                *(
                    str(namedetails.get(key) or "").strip()
                    for key in (
                        "name:en",
                        "name:ja",
                        "name:zh",
                        "name:ja-Hira",
                        "official_name",
                        "brand",
                        "name",
                    )
                ),
            )
            if alias and alias.casefold() != name.casefold()
        )
    )
    place_id = stable_place_id_osm(osm_type, osm_id)
    if place_id is None:
        return None
    place = {
        "place_id": place_id,
        "provider": "nominatim",
        "provider_place_type": ";".join(provider_types),
        "provider_country_code": country_code,
        "name": name,
        "address": display_name,
        "latitude": latitude,
        "longitude": longitude,
    }
    if aliases:
        place["aliases"] = aliases[:6]
    return place


def is_concrete_dining_place(place: Dict[str, Any]) -> bool:
    """Reject category labels and multi-vendor collections as Dining entities."""
    name = " ".join(str(place.get("name") or "").split())
    lowered = name.casefold()
    return bool(
        place.get("provider_place_type") in _DINING_PLACE_TYPES
        and place.get("provider_place_type") != "amenity;food_court"
        and lowered not in _GENERIC_DINING_NAMES
        and not any(token in lowered for token in _DINING_COLLECTION_TOKENS)
    )


def is_concrete_lodging_place(place: Dict[str, Any]) -> bool:
    """Reject category labels and nameless OSM records as Lodging entities.

    The lodging sibling of ``is_concrete_dining_place``.  The provider-domain
    boundary stays the single admission authority
    (``provider_place_type_matches_candidate_kind``); this adds the concreteness
    dimension a bound property identity needs.  An OSM lodging record with no
    ``name`` tag normalizes its name from the first ``display_name`` component —
    a house number such as "16" — which is not a property anyone can book.
    """
    if not provider_place_type_matches_candidate_kind(
        str(place.get("provider_place_type") or ""),
        "lodging",
    ):
        return False
    name = " ".join(str(place.get("name") or "").split())
    if not any(character.isalpha() for character in name):
        return False
    return name.casefold() not in _GENERIC_LODGING_NAMES


def is_concrete_visit_place(place: Dict[str, Any]) -> bool:
    """Reject areas and nameless records as Visit entities.

    Visit 的具体性判据与 Visit 准入同哲学：排除法。这个域装得下博物馆、寺庙、公园、
    观景台、商场和 amap 的中文类别，写 allowlist 会把没列到的真实场馆、以及测试里
    ``attraction`` / ``商业街`` 一类裸类别全判假；能确定的只有反面——一条 provider
    类型说自己是行政区划或边界时，返回的是一片区域，不是一个能安排进某一天的停留点。
    "museum in <短名>" 这类区域内查询正是靠这条把目的地自身的边界记录挡在外面。
    """
    if not provider_place_type_matches_candidate_kind(
        str(place.get("provider_place_type") or ""),
        "visit",
    ):
        return False
    if is_administrative_provider_type(str(place.get("provider_place_type") or "")):
        return False
    name = " ".join(str(place.get("name") or "").split())
    return any(character.isalpha() for character in name)


# 每个域的具体性判据。传进来的 candidate_kind 只有这两个域会带具体性要求，其余
# kind 不做后过滤。
_CONCRETE_PLACE_FILTERS: Dict[str, Callable[[Dict[str, Any]], bool]] = {
    "dining": is_concrete_dining_place,
    "visit": is_concrete_visit_place,
}


async def search_nominatim_raw(
    query: str,
    *,
    country_code: Optional[str] = None,
    limit: int = 5,
    focus_latitude: Optional[float] = None,
    focus_longitude: Optional[float] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> NominatimSearchObservation:
    """Return the complete bounded provider response for one global place query.

    Every caller of the ``/search`` endpoint goes through here — the intake
    route, the authored-place ladder and the ``global_place_search`` tool — so
    this is where reuse belongs. The answer carries its own ``observed_at``: a
    reused body reports the instant the provider produced it, and the caller is
    responsible for publishing that instead of ``now``.

    ``focus_latitude`` / ``focus_longitude`` are the controlled destination's own
    provider point, when the caller has one.  They become a soft
    :func:`destination_focus_viewbox`, which is why they belong here and not at
    each call site: the box is derived from the point exactly once, and the two
    callers that hold a destination point (the ``global_place_search`` tool and
    the authored-place ladder) cannot end up focusing differently.  The intake
    route is the caller that legitimately has none — it is the request that
    *resolves* the destination, so there is no destination to focus on yet.

    Half a point is not a point: a lone latitude focuses nothing rather than
    silently focusing on the prime meridian.
    """
    normalized_query = " ".join((query or "").split())
    if not normalized_query:
        raise ValueError("query is required")
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    normalized_country = normalize_country_code(country_code)
    settings = get_settings().geocoding
    params: Dict[str, Any] = {
        "q": normalized_query,
        "format": "jsonv2",
        "addressdetails": 1,
        "namedetails": 1,
        "limit": limit,
        "accept-language": "zh-CN,zh,en",
    }
    if normalized_country is not None:
        params["countrycodes"] = normalized_country
    if _is_coordinate(focus_latitude) and _is_coordinate(focus_longitude):
        params["viewbox"] = destination_focus_viewbox(
            float(focus_latitude), float(focus_longitude)
        )
    base_url = settings.nominatim_base_url.rstrip("/")
    digest = _search_cache_digest(base_url, params)
    # Ahead of the rate gate on purpose: a reuse owes the provider nothing, so it
    # must not queue behind the 1.1s spacing the live path is bound by.
    cached = await _SEARCH_CACHE.get(digest)
    if cached is not None:
        return cached
    owns_client = client is None
    resolved_client = client or httpx.AsyncClient(
        timeout=settings.search_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
    )
    try:
        response = await _send_with_retry(
            lambda: resolved_client.get(
                f"{base_url}/search",
                params=params,
            ),
            rate_gate=_RATE_GATE,
            min_interval_seconds=settings.min_interval_seconds,
            unavailable_message="global place provider unavailable",
        )
        raw = response.json()
    except ValueError as exc:
        raise NominatimPlaceSearchError(
            "global place provider returned a non-JSON body",
            code="provider_unparseable",
        ) from exc
    finally:
        if owns_client:
            await resolved_client.aclose()
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise NominatimPlaceSearchError(
            "global place provider returned an invalid payload",
            code="provider_invalid_payload",
        )
    observation = NominatimSearchObservation(
        items=[dict(item) for item in raw],
        observed_at=datetime.now(timezone.utc),
        origin="live",
    )
    await _SEARCH_CACHE.set(digest, observation)
    return observation


def _bounded_aliases(query: str, aliases: Optional[list[str]]) -> list[str]:
    values = [query, *(aliases or [])]
    normalized = list(
        dict.fromkeys(
            " ".join(value.split())
            for value in values
            if isinstance(value, str) and 2 <= len(" ".join(value.split())) <= 120
        )
    )
    if not normalized:
        raise ValueError("at least one concrete entity name is required")
    return normalized[:4]


async def search_overpass_place_ids(
    entity_names: list[str],
    *,
    kind: str,
    destination_place_id: str,
    destination_latitude: Optional[float] = None,
    destination_longitude: Optional[float] = None,
    limit: int,
    client: Optional[httpx.AsyncClient] = None,
) -> list[str]:
    """Resolve named POIs of one kind inside one controlled OSM relation.

    Nominatim's text index matches a place's primary name; Overpass matches the
    localized and alternate name tags too, which is what a place written in
    another script than its OSM name needs.
    """
    tag_filter = _OVERPASS_TAG_FILTERS.get(kind)
    if tag_filter is None:
        raise ValueError(f"unsupported overpass place kind: {kind}")
    match = _OSM_RELATION_PLACE_ID.fullmatch(destination_place_id.strip())
    if match is None:
        # 一个不是受控 OSM relation 的目的地 id 是调用方传错了，不是「区域内没有这个
        # 地点」。返回空列表会把两者压成同一个答案。
        raise ValueError(
            f"destination_place_id is not a controlled osm relation: {destination_place_id!r}"
        )
    # A signage name is written with spaces the OSM tag may not carry, so each
    # alias matches across optional whitespace rather than requiring it verbatim.
    regex = "|".join(
        r"\s*".join(re.escape(part) for part in name.split())
        for name in entity_names
        if name.strip()
    )
    regex_literal = json.dumps(regex, ensure_ascii=False)
    if (
        isinstance(destination_latitude, (int, float))
        and not isinstance(destination_latitude, bool)
        and isinstance(destination_longitude, (int, float))
        and not isinstance(destination_longitude, bool)
        and -90 <= destination_latitude <= 90
        and -180 <= destination_longitude <= 180
    ):
        south = max(-90.0, float(destination_latitude) - 0.75)
        north = min(90.0, float(destination_latitude) + 0.75)
        west = max(-180.0, float(destination_longitude) - 0.75)
        east = min(180.0, float(destination_longitude) + 0.75)
        spatial_filter = f"({south},{west},{north},{east})"
        area_prefix = ""
    else:
        spatial_filter = "(area.searchArea)"
        area_prefix = (
            f"rel({match.group('relation_id')});map_to_area->.searchArea;"
        )
    statements = "".join(
        f"nwr{spatial_filter}{tag}"
        f'[~"^(name|name:en|name:ja|name:zh|alt_name|official_name|brand)$"~{regex_literal},i];'
        for tag in tag_filter
    )
    query = (
        "[out:json][timeout:25];"
        f"{area_prefix}"
        f"({statements});out center tags {limit};"
    )
    settings = get_settings().geocoding
    owns_client = client is None
    resolved_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(max(30.0, settings.timeout_seconds)),
        headers={"User-Agent": settings.user_agent},
    )
    try:
        response = await _send_with_retry(
            lambda: resolved_client.post(
                os.getenv(
                    "OVERPASS_API_URL",
                    "https://overpass-api.de/api/interpreter",
                ),
                data={"data": query},
            ),
            rate_gate=_OVERPASS_RATE_GATE,
            min_interval_seconds=settings.min_interval_seconds,
            unavailable_message="global place identity provider unavailable",
        )
        payload = response.json()
    except ValueError as exc:
        raise NominatimPlaceSearchError(
            "global place identity provider returned a non-JSON body",
            code="provider_unparseable",
        ) from exc
    finally:
        if owns_client:
            await resolved_client.aclose()
    elements = payload.get("elements") if isinstance(payload, dict) else None
    remark = payload.get("remark") if isinstance(payload, dict) else None
    if isinstance(remark, str) and remark.strip():
        raise NominatimPlaceSearchError(
            f"global place identity provider did not complete: {remark.strip()}",
            code="provider_unavailable",
        )
    if not isinstance(elements, list):
        raise NominatimPlaceSearchError(
            "global place identity provider returned invalid data",
            code="provider_invalid_payload",
        )
    prefixes = {"node": "N", "way": "W", "relation": "R"}
    osm_ids: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        prefix = prefixes.get(str(element.get("type") or ""))
        osm_id = element.get("id")
        if prefix is None or not isinstance(osm_id, int) or isinstance(osm_id, bool):
            continue
        stable_id = f"{prefix}{osm_id}"
        if stable_id not in osm_ids:
            osm_ids.append(stable_id)
        if len(osm_ids) == limit:
            break
    return osm_ids


async def lookup_nominatim_osm_ids(
    osm_ids: list[str],
    *,
    accept_language: Optional[str] = "zh-CN,zh,en",
    client: Optional[httpx.AsyncClient] = None,
) -> list[Dict[str, Any]]:
    """Fetch complete address/country/type facts for exact OSM identities.

    ``accept_language`` is the provider's own parameter name: the default is the
    identity posture the rest of the pipeline stores places under, ``None`` omits
    the parameter so the provider answers in each place's local script.
    """
    if not osm_ids:
        return []
    settings = get_settings().geocoding
    owns_client = client is None
    resolved_client = client or httpx.AsyncClient(
        timeout=settings.timeout_seconds,
        headers={"User-Agent": settings.user_agent},
    )
    params: Dict[str, Any] = {
        "osm_ids": ",".join(osm_ids),
        "format": "jsonv2",
        "addressdetails": 1,
        "namedetails": 1,
    }
    if accept_language:
        params["accept-language"] = accept_language
    try:
        response = await _send_with_retry(
            lambda: resolved_client.get(
                f"{settings.nominatim_base_url.rstrip('/')}/lookup",
                params=params,
            ),
            rate_gate=_RATE_GATE,
            min_interval_seconds=settings.min_interval_seconds,
            unavailable_message="global place identity lookup unavailable",
        )
        payload = response.json()
    except ValueError as exc:
        raise NominatimPlaceSearchError(
            "global place identity lookup returned a non-JSON body",
            code="provider_unparseable",
        ) from exc
    finally:
        if owns_client:
            await resolved_client.aclose()
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise NominatimPlaceSearchError(
            "global place identity lookup returned invalid data",
            code="provider_invalid_payload",
        )
    return [dict(item) for item in payload]


_OSM_PLACE_ID = re.compile(r"^osm:(?P<osm_type>node|way|relation):(?P<osm_id>[1-9]\d*)$")
_OSM_LOOKUP_PREFIXES = {"node": "N", "way": "W", "relation": "R"}


def osm_lookup_id(place_id: str) -> Optional[str]:
    """Convert a stable ``osm:{type}:{id}`` place_id into a ``/lookup`` token.

    The inverse of ``stable_place_id_osm``; non-OSM namespaces (12306, amap POI)
    have no OSM object and fail closed with None.
    """
    match = _OSM_PLACE_ID.fullmatch(str(place_id or "").strip())
    if match is None:
        return None
    return f"{_OSM_LOOKUP_PREFIXES[match.group('osm_type')]}{match.group('osm_id')}"


async def lookup_typed_addresses_bilingual(
    place_ids: list[str],
) -> Dict[str, list[Dict[str, Any]]]:
    """Return each place's typed ``address`` dicts in local script and in English.

    Two batched provider calls total, whatever the number of places: one without
    ``accept-language`` (each place answers in its own script) and one pinned to
    English.  Callers need both because a locality name only helps cross-checking
    an external page when it is spelled the way that page spells it — a Paris
    review page writes "Rue Saint-Dominique", never the zh-CN "圣多米尼克路" the
    stored identity carries.

    Provider failures propagate as ``NominatimPlaceSearchError``: an empty answer
    and an unavailable provider are different facts, and the caller decides.
    """
    lookup_ids: Dict[str, str] = {}
    for place_id in place_ids:
        token = osm_lookup_id(place_id)
        if token is not None and token not in lookup_ids:
            lookup_ids[token] = str(place_id)
    if not lookup_ids:
        return {}
    typed: Dict[str, list[Dict[str, Any]]] = {
        place_id: [] for place_id in lookup_ids.values()
    }
    tokens = list(lookup_ids)
    for accept_language in (None, "en"):
        for item in await lookup_nominatim_osm_ids(
            tokens,
            accept_language=accept_language,
        ):
            prefix = _OSM_LOOKUP_PREFIXES.get(
                str(item.get("osm_type") or "").strip().casefold()
            )
            address = item.get("address")
            if prefix is None or not isinstance(address, dict):
                continue
            place_id = lookup_ids.get(f"{prefix}{item.get('osm_id')}")
            if place_id is None:
                continue
            typed[place_id].append(dict(address))
    return typed


# 一次工具调用里整条身份 fallback 的时间片。每条别名查询都是一次全 bbox 扫描，Visit
# 还要乘上 5 条 tag predicate，六城探针（见 ``_OVERPASS_TAG_FILTERS`` 上方的表）实测
# 单条别名 23-74s。20.0 与 B 梯子的按条目口径对齐
# （itinerary_planner/node.py::_AUTHORED_ENTRY_BUDGET_SECONDS）：健康实例上够一次成功
# 解析跑完，实例过载时把损失钉在一个时间片里，而不是让 4 条别名 × 3 次重试拖走整轮
# 调研。时间片先于重试阶梯用尽是预期结果，此时归因是 budget_spent 而不是 provider
# unavailable——两者都写进 identity_fallback_failure，读日志能分开。
_IDENTITY_FALLBACK_BUDGET_SECONDS = 20.0


async def _resolve_identity_inside_relation(
    entity_names: list[str],
    *,
    candidate_kind: str,
    country_code: str,
    destination_place_id: str,
    destination_latitude: Optional[float],
    destination_longitude: Optional[float],
    limit: int,
    is_concrete: Callable[[Dict[str, Any]], bool],
) -> list[Dict[str, Any]]:
    """按别名逐条解析受控 relation 内的具体实体身份，命中即停。

    一条别名一次查询：把整个别名集拼成一条 alternation、再乘上本域的多条 tag
    predicate，会超出 Overpass 解释器自己的 timeout，回来的是「provider 不可用」而
    不是「区域内没有」。Provider 连重试都用尽时结束整条 fallback——那是 provider 的
    判决，不是这条别名的判决，换个别名只会再烧一遍同一个时间片。
    """
    for alias in entity_names:
        osm_ids = await search_overpass_place_ids(
            [alias],
            kind=candidate_kind,
            destination_place_id=destination_place_id,
            destination_latitude=destination_latitude,
            destination_longitude=destination_longitude,
            limit=limit,
        )
        lookup = await lookup_nominatim_osm_ids(osm_ids)
        resolved = [
            place
            for item in lookup
            if (place := normalize_nominatim_place(item)) is not None
            and place["provider_country_code"] == country_code
            and is_concrete(place)
        ]
        if resolved:
            return resolved
    return []


async def global_place_search(
    query: str,
    country_code: str,
    limit: int = 5,
    destination_place_id: Optional[str] = None,
    destination_latitude: Optional[float] = None,
    destination_longitude: Optional[float] = None,
    candidate_kind: Optional[str] = None,
    aliases: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """Tool executor returning only identities inside the requested country.

    ``destination_latitude`` / ``destination_longitude`` must reach the primary text
    search, not just the Overpass rung below: the text search is the rung that actually
    answers, and without them it runs with no idea where the destination is.  Every
    caller already supplies them.
    """
    normalized_country = normalize_country_code(country_code)
    if normalized_country is None:
        raise ValueError("country_code is required")
    observation = await search_nominatim_raw(
        query,
        country_code=normalized_country,
        limit=limit,
        focus_latitude=destination_latitude,
        focus_longitude=destination_longitude,
    )
    results = [
        place
        for item in observation.items
        if (place := normalize_nominatim_place(item)) is not None
        and place["provider_country_code"] == normalized_country
    ]
    is_concrete = _CONCRETE_PLACE_FILTERS.get(candidate_kind or "")
    if is_concrete is not None:
        results = [place for place in results if is_concrete(place)]
    annotate_destination_distance(
        results,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
    )
    resolution_method = "nominatim_text_search"
    identity_fallback_failure: Optional[Dict[str, str]] = None
    if not results and is_concrete is not None and isinstance(destination_place_id, str):
        if _OSM_RELATION_PLACE_ID.fullmatch(destination_place_id.strip()) is None:
            # 区域内身份解析只在受控 OSM relation 里成立。调用方给了别的 provider 的
            # id 时，把它归因写出来，而不是安静地当成「区域内没有这个地点」。
            message = (
                "destination_place_id is not a controlled osm relation: "
                f"{destination_place_id!r}"
            )
            logger.warning("global place identity fallback skipped: %s", message)
            identity_fallback_failure = {
                "provider": "overpass",
                "reason": "destination_place_id_not_controlled_relation",
                "message": message,
            }
        else:
            entity_names = _bounded_aliases(query, aliases)
            try:
                results = await asyncio.wait_for(
                    _resolve_identity_inside_relation(
                        entity_names,
                        candidate_kind=candidate_kind,
                        country_code=normalized_country,
                        destination_place_id=destination_place_id,
                        destination_latitude=destination_latitude,
                        destination_longitude=destination_longitude,
                        limit=limit,
                        is_concrete=is_concrete,
                    ),
                    timeout=_IDENTITY_FALLBACK_BUDGET_SECONDS,
                )
                resolution_method = "overpass_name_then_nominatim_identity"
            except asyncio.TimeoutError:
                # 时间片用完的是这条 fallback，不是整个工具：主 Nominatim 请求已经
                # 成功，抛出去会把一个可归因的空结果变成整轮故障。
                logger.warning(
                    "global place identity fallback budget spent after %.1fs: kind=%s query=%r",
                    _IDENTITY_FALLBACK_BUDGET_SECONDS,
                    candidate_kind,
                    " ".join(query.split()),
                )
                identity_fallback_failure = {
                    "provider": "overpass",
                    "reason": "identity_fallback_budget_spent",
                    "message": (
                        "identity fallback exceeded "
                        f"{_IDENTITY_FALLBACK_BUDGET_SECONDS}s"
                    ),
                }
            except NominatimPlaceSearchError as exc:
                # The primary Nominatim request completed. Keep the optional identity
                # fallback failure explicit without marking the entire place tool dead;
                # a later bounded query may still resolve through Nominatim directly.
                logger.warning(
                    "global place identity fallback unavailable: kind=%s query=%r: %s",
                    candidate_kind,
                    " ".join(query.split()),
                    exc,
                )
                identity_fallback_failure = {
                    "provider": "overpass",
                    "reason": "provider_unavailable",
                    "message": str(exc),
                }
    response = {
        "success": True,
        "provider": "nominatim",
        "resolution_method": resolution_method,
        "query": " ".join(query.split()),
        "requested_country_code": normalized_country,
        "destination_place_id": destination_place_id,
        "results": results,
    }
    if identity_fallback_failure is not None:
        response["identity_fallback_failure"] = identity_fallback_failure
    # ``observed_at`` is the provider's, ``retrieved_at`` is this Run's. They
    # differ exactly when the text-search body came out of the raw ``/search``
    # cache, and that difference is what stops a reused geocode from claiming a
    # fresh observation: ``build_provider_snapshot_record`` derives
    # ``provider_valid_until`` from ``observed_at``, and the SourceRecord
    # provenance inherits it, so a reused body's validity window shrinks with its
    # real age instead of resetting.
    response["observed_at"] = observation.observed_at.isoformat()
    response["retrieved_at"] = datetime.now(timezone.utc).isoformat()
    return response
