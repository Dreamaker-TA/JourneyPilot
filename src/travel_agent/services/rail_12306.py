"""JourneyPilot-owned 12306 remaining-ticket client (Domain Service).

Why this lives in the repo instead of behind ``npx 12306-mcp``:
``12306-mcp@0.3.9`` is the newest published release and it hardcodes
``${API_BASE}/otn/leftTicket/query``.  12306 retired that path, so every
``get-tickets`` call returns HTTP 404 deterministically and a delivered domestic
itinerary ends up with zero intercity rail legs.  12306 rotates the path and
publishes the live one in the HTML of ``/otn/leftTicket/init`` as
``CLeftTicketUrl = 'leftTicket/queryG'``; only a client that reads that page can
stay alive across a rotation.  The pin cannot move forward, so the server moved
in-repo.

Upstream contract (empirically verified):

``GET /otn/leftTicket/init``
    200, sets ``JSESSIONID`` / ``BIGipServerotn`` / ``route``; body carries
    ``CLeftTicketUrl``.  Cookies are mandatory — without them the query 302s.
``GET /otn/{discovered_path}?leftTicketDTO.train_date=…&purpose_codes=ADULT``
    200 → ``{"data": {"result": [pipe-delimited rows], "map": {telecode: 中文名}}}``
    302 → empty body, ``Location`` → ``error.html``: date outside the live
          inventory window, or a rejected session.  Never an empty result.
    404 → the discovered path went stale mid-flight: rediscover once and retry.

``data.map`` is the only source of 中文 station display names.

Two output contracts, one per tool, because they have different consumers:

``get-tickets`` → :func:`build_left_ticket_payload`
    A structured payload whose ``results`` list holds **flat records of scalars**,
    consumed in code by
    ``agents/transport_researcher/node.py::_rail_train_records``.  Prose is not an
    option here: ``tools/governance.py::_sanitize_value`` collapses every string
    to one line and then truncates it at 900 chars, which reduced the previous
    text contract (49 588 chars / 389 trains for one real query) to a single
    train — so the whole in-repo selection policy never once ran.  Every shape
    constraint of the payload is dictated by that sanitizer and is spelled out on
    :func:`build_left_ticket_payload` and :func:`build_ticket_record`.
``get-interline-tickets`` → :func:`format_interline_text`
    Model-facing text.  No code parses it, so it stays prose (nested legs are
    rendered by :func:`format_tickets_text`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import httpx

API_BASE = "https://kyfw.12306.cn"
LEFT_TICKET_INIT_URL = f"{API_BASE}/otn/leftTicket/init"
LCQUERY_INIT_URL = f"{API_BASE}/otn/lcQuery/init"

# 12306 refuses non-browser clients on these endpoints.
_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,zh-TW;q=0.8,zh-HK;q=0.7,en-US;q=0.6,en;q=0.5"
_BROWSER_HEADERS = {
    "User-Agent": _DESKTOP_USER_AGENT,
    "Accept-Language": _ACCEPT_LANGUAGE,
}

_LEFT_TICKET_PATH_RE = re.compile(r"CLeftTicketUrl = '(.+?)'")
_LC_SEARCH_PATH_RE = re.compile(r"lc_search_url = '(.+?)'")
_TELECODE_RE = re.compile(r"^[A-Z]+$")
_DIGITS_RE = re.compile(r"^\d+$")

NO_TRAIN_MESSAGE = "没有查询到相关车次信息"
CITY_NOT_FOUND_MESSAGE = "未检索到城市。"

DEFAULT_TIMEOUT_SECONDS = 20.0

STATION_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3] / "mcp_servers" / "rail" / "station_name.js"
)
# Machine-readable provenance for the snapshot beside it: where it came from, when,
# and which fingerprint upstream gave.  The facts were prose in the README, which
# means no check could read them.
STATION_SNAPSHOT_META_PATH = STATION_SNAPSHOT_PATH.with_name("station_name.meta.json")


class Rail12306Error(RuntimeError):
    """Any failure that must not be reported as 'no trains found'."""


class StationNotFoundError(Rail12306Error):
    """A station name or telecode is absent from the committed snapshot."""


class QueryRefusedError(Rail12306Error):
    """12306 refused the request: redirect, stale path, or unusable body."""


# ---------------------------------------------------------------------------
# Station snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RailStation:
    telecode: str
    name: str
    pinyin: str
    pinyin_abbr: str
    short_abbr: str
    city: str


def parse_station_snapshot(raw: str) -> dict[str, RailStation]:
    """Parse the official ``station_name.js`` body into ``{telecode: station}``."""

    body_match = re.search(r"='(.*)'", raw, re.S)
    if body_match is None:
        raise Rail12306Error("12306 station snapshot is not a station_names assignment")
    stations: dict[str, RailStation] = {}
    for record in body_match.group(1).split("@"):
        if not record:
            continue
        fields = record.split("|")
        if len(fields) < 8:
            continue
        telecode = fields[2].strip()
        name = fields[1].strip()
        if not telecode or not name:
            continue
        stations[telecode] = RailStation(
            telecode=telecode,
            name=name,
            pinyin=fields[3].strip(),
            pinyin_abbr=fields[0].strip(),
            short_abbr=fields[4].strip(),
            city=fields[7].strip(),
        )
    if not stations:
        raise Rail12306Error("12306 station snapshot decoded to zero stations")
    return stations


@lru_cache(maxsize=1)
def station_table() -> Mapping[str, RailStation]:
    """``{telecode: RailStation}`` from the committed offline snapshot."""

    return parse_station_snapshot(STATION_SNAPSHOT_PATH.read_text("utf-8"))


@lru_cache(maxsize=1)
def _stations_by_name() -> Mapping[str, RailStation]:
    return {station.name: station for station in station_table().values()}


@lru_cache(maxsize=1)
def _stations_by_city() -> Mapping[str, tuple[RailStation, ...]]:
    grouped: dict[str, list[RailStation]] = {}
    for station in station_table().values():
        grouped.setdefault(station.city, []).append(station)
    return {
        city: tuple(sorted(members, key=lambda item: item.telecode))
        for city, members in grouped.items()
    }


def city_station_telecodes(city: str) -> frozenset[str]:
    """Every station telecode 12306 files under ``city`` (empty if unknown)."""

    return frozenset(
        station.telecode for station in _stations_by_city().get(city.strip(), ())
    )


def station_group_telecodes(telecode: str) -> frozenset[str]:
    """Every telecode 12306 answers with when one station is queried.

    A remaining-ticket query is expanded by 12306 to the whole city station
    group: asking ``from_station=AOH`` (上海虹桥) also returns rows departing
    上海南, 上海松江 and 金山北.  The grouping is the provider's own — city is
    field ``[7]`` of the official station table — so reading it back is fact
    work, not a repo judgement.  The queried station is always in the group even
    when the snapshot does not know it.
    """

    station = station_table().get((telecode or "").strip())
    if station is None:
        return frozenset({telecode})
    return frozenset(city_station_telecodes(station.city) | {telecode})


def city_station_code(city: str) -> Optional[str]:
    """The station representing a city: the one whose name equals the city name."""

    for station in _stations_by_city().get(city.strip(), ()):
        if station.name == station.city:
            return station.telecode
    return None


def resolve_station_code(value: str) -> Optional[str]:
    """Accept a telecode or a 中文 station name (with or without a trailing 站)."""

    text = (value or "").strip()
    if not text:
        return None
    if _TELECODE_RE.match(text) and text in station_table():
        return text
    if text.endswith("站") and len(text) > 1:
        text = text[:-1]
    station = _stations_by_name().get(text)
    return station.telecode if station else None


def _require_station_code(value: str, *, role: str) -> str:
    code = resolve_station_code(value)
    if code is None:
        raise StationNotFoundError(f"{role} 不是有效的车站名或 station_code: {value!r}")
    return code


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def extract_left_ticket_path(html: str) -> str:
    """Read the live remaining-ticket path 12306 publishes in the init page."""

    match = _LEFT_TICKET_PATH_RE.search(html or "")
    if match is None:
        raise QueryRefusedError(
            "12306 /otn/leftTicket/init did not publish CLeftTicketUrl"
        )
    return match.group(1).strip()


def extract_lc_query_path(html: str) -> str:
    """Read the live interline path 12306 publishes in the lcQuery init page."""

    match = _LC_SEARCH_PATH_RE.search(html or "")
    if match is None:
        raise QueryRefusedError(
            "12306 /otn/lcQuery/init did not publish lc_search_url"
        )
    return match.group(1).strip()


# ---------------------------------------------------------------------------
# Row decode
# ---------------------------------------------------------------------------

# Verified indices into the 58-field pipe-delimited ``data.result`` row.
_IDX_TRAIN_NO = 2
_IDX_STATION_TRAIN_CODE = 3
_IDX_FROM_TELECODE = 6
_IDX_TO_TELECODE = 7
_IDX_START_TIME = 8
_IDX_ARRIVE_TIME = 9
_IDX_LISHI = 10
_IDX_CAN_WEB_BUY = 11
_IDX_START_TRAIN_DATE = 13
_IDX_YP_INFO_NEW = 39
_ROW_FIELD_COUNT = 40  # highest index we read, plus one

# ``{seat_type_code: (显示名, inventory field short name)}`` — reverse-engineered
# from the 12306 web client and unchanged across releases.
_SEAT_TYPES: Mapping[str, tuple[str, str]] = {
    "9": ("商务座", "swz"),
    "P": ("特等座", "tz"),
    "M": ("一等座", "zy"),
    "D": ("优选一等座", "zy"),
    "O": ("二等座", "ze"),
    "S": ("二等包座", "ze"),
    "6": ("高级软卧", "gr"),
    "A": ("高级动卧", "gr"),
    "4": ("软卧", "rw"),
    "I": ("一等卧", "rw"),
    "F": ("动卧", "rw"),
    "3": ("硬卧", "yw"),
    "J": ("二等卧", "yw"),
    "2": ("软座", "rz"),
    "1": ("硬座", "yz"),
    "W": ("无座", "wz"),
    "H": ("其他", "qt"),
}

# ``{short name: row index}`` for the ``{short}_num`` inventory fields.
_SEAT_COUNT_INDEX: Mapping[str, int] = {
    "gr": 21,
    "qt": 22,
    "rw": 23,
    "rz": 24,
    "tz": 25,
    "wz": 26,
    "yw": 28,
    "yz": 29,
    "ze": 30,
    "zy": 31,
    "swz": 32,
}

_PRICE_CHUNK_LENGTH = 10
# 12306 stores 无座 as an inventory count of 3000+ rather than a seat code.
_NO_SEAT_COUNT_MARKER = 3000


@dataclass(frozen=True)
class SeatPrice:
    seat_name: str
    short: str
    price: float
    inventory: str


@dataclass(frozen=True)
class RailTicket:
    train_no: str
    train_code: str
    from_name: str
    from_telecode: str
    to_name: str
    to_telecode: str
    start_time: str
    arrive_time: str
    lishi: str
    start_train_date: str
    can_web_buy: str
    prices: tuple[SeatPrice, ...]


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def decode_seat_prices(
    yp_info: str, seat_inventory: Mapping[str, str]
) -> list[SeatPrice]:
    """Decode ``yp_info_new`` into per-class fare + inventory, upstream order."""

    prices: list[SeatPrice] = []
    blob = yp_info or ""
    for offset in range(0, len(blob) - _PRICE_CHUNK_LENGTH + 1, _PRICE_CHUNK_LENGTH):
        chunk = blob[offset : offset + _PRICE_CHUNK_LENGTH]
        if _safe_int(chunk[6:10]) >= _NO_SEAT_COUNT_MARKER:
            seat_code = "W"
        elif chunk[0] not in _SEAT_TYPES:
            seat_code = "H"
        else:
            seat_code = chunk[0]
        seat_name, short = _SEAT_TYPES[seat_code]
        prices.append(
            SeatPrice(
                seat_name=seat_name,
                short=short,
                price=_safe_int(chunk[1:6]) / 10,
                inventory=str(seat_inventory.get(short, "")),
            )
        )
    return prices


def parse_left_ticket_rows(
    rows: Sequence[str], station_names: Mapping[str, str]
) -> list[RailTicket]:
    """Turn ``data.result`` rows into tickets, naming stations from ``data.map``."""

    tickets: list[RailTicket] = []
    for row in rows:
        fields = str(row).split("|")
        if len(fields) < _ROW_FIELD_COUNT:
            continue
        from_telecode = fields[_IDX_FROM_TELECODE]
        to_telecode = fields[_IDX_TO_TELECODE]
        inventory = {
            short: fields[index] for short, index in _SEAT_COUNT_INDEX.items()
        }
        tickets.append(
            RailTicket(
                train_no=fields[_IDX_TRAIN_NO],
                train_code=fields[_IDX_STATION_TRAIN_CODE],
                from_name=station_names.get(from_telecode, from_telecode),
                from_telecode=from_telecode,
                to_name=station_names.get(to_telecode, to_telecode),
                to_telecode=to_telecode,
                start_time=fields[_IDX_START_TIME],
                arrive_time=fields[_IDX_ARRIVE_TIME],
                # 历时 is provider-authored; recomputing it invents a fact.
                lishi=fields[_IDX_LISHI],
                start_train_date=fields[_IDX_START_TRAIN_DATE],
                can_web_buy=fields[_IDX_CAN_WEB_BUY],
                prices=tuple(
                    decode_seat_prices(fields[_IDX_YP_INFO_NEW], inventory)
                ),
            )
        )
    return tickets


def format_ticket_status(inventory: str) -> str:
    """Render one 12306 inventory field as the availability phrase."""

    value = (inventory or "").strip()
    if _DIGITS_RE.match(value):
        count = int(value)
        return "无票" if count == 0 else f"剩余{count}张票"
    if value in {"有", "充足"}:
        return "有票"
    if value in {"无", "--", ""}:
        return "无票"
    if value == "候补":
        return "无票需候补"
    return f"{value}票"


def _format_price(price: float) -> str:
    return str(int(price)) if float(price).is_integer() else str(price)


def _format_ticket_block(ticket: RailTicket) -> str:
    lines = [
        f"{ticket.train_code} "
        f"{ticket.from_name}(telecode:{ticket.from_telecode}) -> "
        f"{ticket.to_name}(telecode:{ticket.to_telecode}) "
        f"{ticket.start_time} -> {ticket.arrive_time} 历时：{ticket.lishi}"
    ]
    for seat in ticket.prices:
        lines.append(
            f"- {seat.seat_name}: {format_ticket_status(seat.inventory)} "
            f"{_format_price(seat.price)}元"
        )
    return "\n".join(lines)


def format_tickets_text(tickets: Sequence[RailTicket]) -> str:
    """Render legs as text for ``get-interline-tickets`` nesting only.

    ``get-tickets`` returns :func:`build_left_ticket_payload` instead: text is
    unusable for a code consumer under the governance sanitizer.  Interline text
    is read by the model alone, so it stays prose.
    """

    if not tickets:
        return NO_TRAIN_MESSAGE
    blocks = "\n".join(_format_ticket_block(ticket) for ticket in tickets)
    return "车次|出发站 -> 到达站|出发时间 -> 到达时间|历时\n" + blocks + "\n"


# ---------------------------------------------------------------------------
# get-tickets structured contract
# ---------------------------------------------------------------------------
# Every constraint below is imposed by ``tools/governance.py::_sanitize_value``,
# which every tool result passes through before any consumer sees it.  The
# sanitizer is deliberately not being relaxed for us, so the payload is shaped to
# survive it losslessly:
#
# * the list key must be literally ``results``: that key gets the 20-item cap
#   (``_MAX_SEARCH_RESULTS``) while any other key is capped at 5, and
#   ``agents/utils.py::classify_tool_result`` only counts content/result/results/
#   routes/data/text as substantive — a ``trains`` key would report a
#   train-bearing answer as EMPTY_SUCCESS, i.e. "12306 found nothing".
# * a record must be a flat dict of scalars: records sit at sanitize depth 2 and
#   their fields at depth 3, so anything nested inside a record lands at
#   ``depth >= max_depth`` and is replaced by a 120-char summary.
# * a record must stay under 12 keys (``_MAX_DICT_ITEMS``): the 13th key is
#   dropped silently and the record is stamped ``truncated: True``.
#
# Hence: no per-train price list.  The fare table is reduced here to flat scalar
# fields, and the selection policy stays in the agent layer
# (``transport_researcher/node.py::_select_best_train``).

# The sanitizer keeps the first items of a ``results`` list and replaces the
# rest with a ``{\"truncated_count\": n}`` sentinel, so emitting more than this
# only buys a sentinel.  12306 returns up to ~390 rows for a busy pair; the
# earlier cap of 20 truncated to the first (earliest) rows, which silently hid
# every evening train — a same-day round trip whose return had to depart late
# found \"no train after the outbound\" and fell back to a return before the
# outbound.  A real busy route spans the whole day, so keep enough rows
# to cover morning through last train.
MAX_TICKET_RECORDS = 120

_MAX_FARE_SUMMARY_CHARS = 120
_SECOND_CLASS_SEAT_NAME = "二等座"
_CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}$")
_LISHI_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def seat_is_bookable(inventory: str) -> bool:
    """Whether one 12306 inventory field means this class can be bought now.

    ``候补`` (waitlist) is not bookable: the traveller cannot hold that seat, and
    binding a leg on it would assert an itinerary nobody can buy.
    """

    value = (inventory or "").strip()
    if _DIGITS_RE.match(value):
        return int(value) > 0
    return value in {"有", "充足"}


def lishi_to_minutes(lishi: str) -> Optional[int]:
    """``00:45`` → ``45``.  ``None`` when 12306 sent an unreadable 历时."""

    match = _LISHI_RE.match((lishi or "").strip())
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _fare_summary(ticket: RailTicket) -> str:
    """One short scalar line covering every class, for the report surface."""

    text = " / ".join(
        f"{seat.seat_name} {format_ticket_status(seat.inventory)} "
        f"{_format_price(seat.price)}元"
        for seat in ticket.prices
    )
    if len(text) > _MAX_FARE_SUMMARY_CHARS:
        return text[: _MAX_FARE_SUMMARY_CHARS - 1].rstrip() + "…"
    return text


def build_ticket_record(ticket: RailTicket) -> Optional[dict[str, Any]]:
    """One bookable train as a flat 11-key record of scalars.

    ``None`` when the train carries no bookable class, or when 12306 sent a
    timetable this repo cannot turn into a leg (unreadable clock or 历时).  A row
    we cannot place on a timeline is not evidence of a route.
    """

    bookable = [seat.price for seat in ticket.prices if seat_is_bookable(seat.inventory)]
    duration_minutes = lishi_to_minutes(ticket.lishi)
    if (
        not bookable
        or duration_minutes is None
        or not _CLOCK_RE.match(ticket.start_time)
        or not _CLOCK_RE.match(ticket.arrive_time)
    ):
        return None
    # ``yp_info_new`` is ordered most-expensive-first, so the representative fare
    # is the cheapest bookable class rather than the first one.
    second_class = next(
        (
            seat.price
            for seat in ticket.prices
            if seat.seat_name == _SECOND_CLASS_SEAT_NAME
        ),
        None,
    )
    return {
        "train_code": ticket.train_code,
        "from_name": ticket.from_name,
        "from_code": ticket.from_telecode,
        "to_name": ticket.to_name,
        "to_code": ticket.to_telecode,
        "departure_time": ticket.start_time,
        "arrival_time": ticket.arrive_time,
        "duration_minutes": duration_minutes,
        "min_price_cny": min(bookable),
        "second_class_price_cny": second_class,
        "fare_summary": _fare_summary(ticket),
    }


def build_left_ticket_payload(
    tickets: Sequence[RailTicket],
    *,
    date: str,
    from_code: str,
    to_code: str,
) -> dict[str, Any]:
    """The ``get-tickets`` payload: strategy-free facts about bookable trains.

    Three provider-fact reductions happen here and nothing else:

    1. rows outside the queried stations' own city groups are dropped — 12306
       expanded the query to the group (see :func:`station_group_telecodes`), and
       a 上海南 → 杭州南 row answers a different trip than the one asked for;
    2. rows with no bookable class are dropped (:func:`seat_is_bookable`);
    3. the fare table is reduced to flat scalars (:func:`build_ticket_record`).

    Only ``MAX_TICKET_RECORDS`` rows can survive the sanitizer, and one real
    query returns up to ~390 rows, so the truncation order is load-bearing: rows
    on the *exact* queried station pair go first, then the city-group siblings,
    each keeping 12306's own row order.  That is the one ordering that cannot
    change the answer of the agent-layer policy — it never drops an exact-pair
    row in favour of a sibling, and it still carries siblings when the exact pair
    runs nothing (上海 → 兰州 arrives 兰州西, and a real leg beats no leg).
    Which of the surviving trains to bind stays entirely in the agent layer.
    """

    from_scope = station_group_telecodes(from_code)
    to_scope = station_group_telecodes(to_code)
    records = [
        record
        for record in (
            build_ticket_record(ticket)
            for ticket in tickets
            if ticket.from_telecode in from_scope and ticket.to_telecode in to_scope
        )
        if record is not None
    ]
    ordered = sorted(
        records,
        # Stable sort: 12306's row order survives inside each group.
        key=lambda record: (
            0
            if record["from_code"] == from_code and record["to_code"] == to_code
            else 1
        ),
    )
    return {
        "success": True,
        "provider": "12306",
        "tool_name": "get-tickets",
        "date": date,
        "from_station": from_code,
        "to_station": to_code,
        # How many bookable in-scope trains 12306 really had, so a truncated
        # answer never reads as "this is all there is".
        "bookable_train_count": len(ordered),
        "returned_train_count": min(len(ordered), MAX_TICKET_RECORDS),
        "results": ordered[:MAX_TICKET_RECORDS],
    }


# ---------------------------------------------------------------------------
# Interline decode (model-facing only: no code parses this text)
# ---------------------------------------------------------------------------

_ALL_LISHI_RE = re.compile(r"(?:(\d+)小时)?(\d+)分钟")


def normalize_all_lishi(all_lishi: str) -> str:
    """``9小时37分钟`` → ``09:37``, matching the get-tickets 历时 shape."""

    match = _ALL_LISHI_RE.search(all_lishi or "")
    if match is None:
        raise Rail12306Error(f"12306 中转历时无法解析: {all_lishi!r}")
    hours = match.group(1) or "0"
    return f"{int(hours):02d}:{int(match.group(2)):02d}"


def _interline_legs(itinerary: Mapping[str, Any]) -> list[RailTicket]:
    legs: list[RailTicket] = []
    for leg in itinerary.get("fullList") or ():
        inventory = {
            short: str(leg.get(f"{short}_num", "")) for short in _SEAT_COUNT_INDEX
        }
        legs.append(
            RailTicket(
                train_no=str(leg.get("train_no", "")),
                train_code=str(leg.get("station_train_code", "")),
                from_name=str(leg.get("from_station_name", "")),
                from_telecode=str(leg.get("from_station_telecode", "")),
                to_name=str(leg.get("to_station_name", "")),
                to_telecode=str(leg.get("to_station_telecode", "")),
                start_time=str(leg.get("start_time", "")),
                arrive_time=str(leg.get("arrive_time", "")),
                lishi=str(leg.get("lishi", "")),
                start_train_date=str(leg.get("start_train_date", "")),
                can_web_buy="",
                # Interline rows carry ``yp_info``, not ``yp_info_new``.
                prices=tuple(
                    decode_seat_prices(str(leg.get("yp_info", "")), inventory)
                ),
            )
        )
    return legs


def format_interline_text(itineraries: Sequence[Mapping[str, Any]]) -> str:
    """Nested per-itinerary text: header line, then tab-indented legs."""

    if not itineraries:
        return NO_TRAIN_MESSAGE
    parts = [
        "出发时间 -> 到达时间 | 出发车站 -> 中转车站 -> 到达车站 | 换乘标志 "
        "|换乘等待时间| 总历时\n"
    ]
    for itinerary in itineraries:
        transfer = (
            "同车换乘"
            if str(itinerary.get("same_train")) == "Y"
            else "同站换乘"
            if str(itinerary.get("same_station")) == "0"
            else "换站换乘"
        )
        parts.append(
            f"{itinerary.get('train_date')} {itinerary.get('start_time')} -> "
            f"{itinerary.get('arrive_date')} {itinerary.get('arrive_time')} | "
            f"{itinerary.get('from_station_name')} -> "
            f"{itinerary.get('middle_station_name')} -> "
            f"{itinerary.get('end_station_name')} | "
            f"{transfer} | {itinerary.get('wait_time')} | "
            f"{normalize_all_lishi(str(itinerary.get('all_lishi', '')))}\n"
        )
        nested = format_tickets_text(_interline_legs(itinerary))
        parts.append("\t" + nested.replace("\n", "\n\t"))
        parts.append("\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Rail12306Client:
    """One 12306 session per query: init for cookies + the live path, then query."""

    def __init__(
        self,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport
        self._timeout = timeout

    def _new_client(self, *, follow_redirects: bool) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=follow_redirects,
            headers=dict(_BROWSER_HEADERS),
        )

    async def _open_session(self, client: httpx.AsyncClient) -> str:
        """GET the init page: it both sets the session cookies and names the path."""

        response = await client.get(LEFT_TICKET_INIT_URL)
        if response.status_code != 200:
            raise QueryRefusedError(
                "12306 /otn/leftTicket/init returned HTTP "
                f"{response.status_code}"
            )
        return extract_left_ticket_path(response.text)

    async def fetch_left_tickets(
        self, *, date: str, from_code: str, to_code: str
    ) -> tuple[list[str], dict[str, str]]:
        """Return ``(data.result rows, data.map)`` for one exact station pair."""

        params = {
            "leftTicketDTO.train_date": date,
            "leftTicketDTO.from_station": from_code,
            "leftTicketDTO.to_station": to_code,
            "purpose_codes": "ADULT",
        }
        async with self._new_client(follow_redirects=False) as client:
            path = await self._open_session(client)
            response = await self._query_left_ticket(client, path, params)
            if response.status_code == 404:
                # The published path rotated between the init read and the
                # query: rediscover once.  This is the whole point of reading
                # CLeftTicketUrl instead of hardcoding a path.
                path = await self._open_session(client)
                response = await self._query_left_ticket(client, path, params)
            if response.status_code == 302:
                raise QueryRefusedError(
                    "12306 refused the remaining-ticket query (HTTP 302 -> "
                    f"{response.headers.get('location', '')}): the date is "
                    "outside the live inventory window or the session was "
                    "rejected"
                )
            if response.status_code != 200:
                raise QueryRefusedError(
                    "12306 remaining-ticket query returned HTTP "
                    f"{response.status_code} for path {path!r}"
                )
            payload = self._decode_json(response)
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise QueryRefusedError(f"12306 查询有误: {payload.get('messages') or data!r}")
        rows = [str(row) for row in (data.get("result") or ())]
        names = {
            str(key): str(value) for key, value in (data.get("map") or {}).items()
        }
        return rows, names

    async def _query_left_ticket(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: Mapping[str, str],
    ) -> httpx.Response:
        return await client.get(
            f"{API_BASE}/otn/{path.lstrip('/')}",
            params=dict(params),
            headers={"Referer": LEFT_TICKET_INIT_URL},
        )

    async def fetch_interline(
        self,
        *,
        date: str,
        from_code: str,
        to_code: str,
        middle_code: str = "",
    ) -> list[dict[str, Any]]:
        """Return every ``middleList`` itinerary 12306 offers for the pair."""

        async with self._new_client(follow_redirects=True) as discovery:
            # 12306 bounces this page through its login redirect; the interline
            # path is only published once the redirect chain settles.
            response = await discovery.get(LCQUERY_INIT_URL)
            if response.status_code != 200:
                raise QueryRefusedError(
                    "12306 /otn/lcQuery/init returned HTTP "
                    f"{response.status_code}"
                )
            lc_path = extract_lc_query_path(response.text)

        params = {
            "train_date": date,
            "from_station_telecode": from_code,
            "to_station_telecode": to_code,
            "middle_station": middle_code,
            "result_index": "0",
            "can_query": "Y",
            "isShowWZ": "N",
            "purpose_codes": "00",
            "channel": "E",
        }
        itineraries: list[dict[str, Any]] = []
        # No session cookie here, unlike the remaining-ticket query: 12306 scopes
        # ``JSESSIONID`` to ``Path=/otn`` and ``/lcquery/*`` answers 200 without
        # it (measured), so opening a session would be dead weight.
        async with self._new_client(follow_redirects=False) as client:
            while True:
                response = await client.get(
                    f"{API_BASE}/{lc_path.lstrip('/')}",
                    params=dict(params),
                    headers={"Referer": LCQUERY_INIT_URL},
                )
                if response.status_code != 200:
                    raise QueryRefusedError(
                        "12306 interline query returned HTTP "
                        f"{response.status_code} for path {lc_path!r}"
                    )
                payload = self._decode_json(response)
                data = payload.get("data")
                if not isinstance(data, Mapping):
                    raise QueryRefusedError(
                        f"12306 中转查询有误: {payload.get('errorMsg') or data!r}"
                    )
                itineraries.extend(
                    item
                    for item in (data.get("middleList") or ())
                    if isinstance(item, Mapping)
                )
                if str(data.get("can_query")) != "Y":
                    break
                params["result_index"] = str(data.get("result_index"))
        return itineraries

    @staticmethod
    def _decode_json(response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise QueryRefusedError(
                f"12306 返回了非 JSON 响应: {response.text[:200]!r}"
            ) from error
        if not isinstance(payload, Mapping):
            raise QueryRefusedError(f"12306 返回了非对象响应: {payload!r}")
        return payload


# ---------------------------------------------------------------------------
# Tool-facing entry points
# ---------------------------------------------------------------------------


async def get_tickets_payload(
    date: str,
    from_station: str,
    to_station: str,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    """``get-tickets``: real bookable trains for one station pair, structured."""

    from_code = _require_station_code(from_station, role="fromStation")
    to_code = _require_station_code(to_station, role="toStation")
    client = Rail12306Client(transport=transport)
    rows, names = await client.fetch_left_tickets(
        date=date, from_code=from_code, to_code=to_code
    )
    return build_left_ticket_payload(
        parse_left_ticket_rows(rows, names),
        date=date,
        from_code=from_code,
        to_code=to_code,
    )


async def get_interline_tickets_text(
    date: str,
    from_station: str,
    to_station: str,
    middle_station: str = "",
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> str:
    """``get-interline-tickets``: real one-transfer itineraries for a pair."""

    from_code = _require_station_code(from_station, role="fromStation")
    to_code = _require_station_code(to_station, role="toStation")
    middle_code = (
        _require_station_code(middle_station, role="middleStation")
        if (middle_station or "").strip()
        else ""
    )
    client = Rail12306Client(transport=transport)
    itineraries = await client.fetch_interline(
        date=date,
        from_code=from_code,
        to_code=to_code,
        middle_code=middle_code,
    )
    return format_interline_text(itineraries)


def get_station_code_of_citys_text(citys: str) -> str:
    """``get-station-code-of-citys``: the station representing each 中文 city.

    ``citys`` is a single ``|``-separated string (e.g. ``"上海|杭州"``); the
    arg name and shape are load-bearing for every caller in the repo.
    """

    result: dict[str, dict[str, str]] = {}
    for city in (citys or "").split("|"):
        name = city.strip()
        if not name:
            continue
        telecode = city_station_code(name)
        if telecode is None:
            result[name] = {"error": CITY_NOT_FOUND_MESSAGE}
            continue
        result[name] = {
            "station_code": telecode,
            "station_name": station_table()[telecode].name,
        }
    return json.dumps(result, ensure_ascii=False)


def station_snapshot_metadata() -> Optional[dict[str, Any]]:
    """Read the snapshot's sidecar provenance, or ``None`` if it cannot be read.

    Deliberately total: a governance probe must be able to report "unreadable"
    rather than take down whatever asked.  ``station_snapshot_freshness`` turns
    ``None`` into an explicit ``unknown`` verdict — never into "current".
    """

    try:
        return json.loads(STATION_SNAPSHOT_META_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def station_snapshot_fingerprint_drift() -> list[str]:
    """Ways the committed snapshot disagrees with the fingerprint it declares.

    Byte size and record count are the only fingerprint 12306 hands out, so they
    are the only thing that can catch a truncated or half-replaced refresh — the
    failure mode that otherwise shows up as a city quietly missing its stations.
    """

    metadata = station_snapshot_metadata()
    if metadata is None:
        return ["站表 meta 缺失或不可解析"]
    problems: list[str] = []
    declared_bytes = metadata.get("byte_size")
    if isinstance(declared_bytes, int):
        actual_bytes = STATION_SNAPSHOT_PATH.stat().st_size
        if actual_bytes != declared_bytes:
            problems.append(
                f"字节数不符：实际 {actual_bytes}，meta 声明 {declared_bytes}"
            )
    declared_records = metadata.get("record_count")
    if isinstance(declared_records, int):
        actual_records = len(station_table())
        if actual_records != declared_records:
            problems.append(
                f"记录数不符：实际 {actual_records}，meta 声明 {declared_records}"
            )
    return problems


def city_station_membership_drift(
    table: Mapping[str, Sequence[str]],
) -> list[str]:
    """Name every hand-maintained telecode the official snapshot disagrees with.

    Membership — does this telecode exist, and does 12306 file it under this city —
    is decidable offline from the committed snapshot in milliseconds, so the
    hand-written hub table never has to be trusted on that point ("no
    shape-level alternative criterion" holds only for the *ordering*,
    which upstream does not publish).

    Returns the drift instead of raising, and there is no "skip on drift" branch
    anywhere: a caller either reports it or fails, and the fix is to correct the
    table.
    """

    stations = station_table()
    problems: list[str] = []
    for city, telecodes in table.items():
        members = city_station_telecodes(city)
        for telecode in telecodes:
            station = stations.get(telecode)
            if station is None:
                problems.append(f"{city}/{telecode}：官方快照里没有这个电报码")
            elif telecode not in members:
                problems.append(
                    f"{city}/{telecode}：官方快照把它归在 {station.city}（{station.name}）"
                )
    return problems


__all__ = [
    "API_BASE",
    "CITY_NOT_FOUND_MESSAGE",
    "LCQUERY_INIT_URL",
    "LEFT_TICKET_INIT_URL",
    "MAX_TICKET_RECORDS",
    "NO_TRAIN_MESSAGE",
    "QueryRefusedError",
    "Rail12306Client",
    "Rail12306Error",
    "RailStation",
    "RailTicket",
    "SeatPrice",
    "STATION_SNAPSHOT_META_PATH",
    "STATION_SNAPSHOT_PATH",
    "StationNotFoundError",
    "build_left_ticket_payload",
    "build_ticket_record",
    "city_station_code",
    "city_station_membership_drift",
    "city_station_telecodes",
    "decode_seat_prices",
    "extract_lc_query_path",
    "extract_left_ticket_path",
    "format_interline_text",
    "format_ticket_status",
    "format_tickets_text",
    "get_interline_tickets_text",
    "get_station_code_of_citys_text",
    "get_tickets_payload",
    "lishi_to_minutes",
    "normalize_all_lishi",
    "parse_left_ticket_rows",
    "parse_station_snapshot",
    "resolve_station_code",
    "seat_is_bookable",
    "station_group_telecodes",
    "station_snapshot_fingerprint_drift",
    "station_snapshot_metadata",
    "station_table",
]
