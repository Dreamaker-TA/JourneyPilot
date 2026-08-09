"""Current JourneyPilot input contract.

Planning workflows consume :class:`ControlledTripIdentity`; raw natural language is
kept as an event, never used as a second source of basic trip facts.
"""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaceKind(str, Enum):
    CITY = "city"
    ADMINISTRATIVE_AREA = "administrative_area"
    ISLAND = "island"
    SCENIC_AREA = "scenic_area"
    AIRPORT = "airport"
    TRAIN_STATION = "train_station"
    COUNTRY = "country"
    POI = "poi"
    HOTEL = "hotel"
    RESTAURANT = "restaurant"


# 一次旅行最多几个受控目的地。**这个数只在这里写一次**：它同时是入口校验的上限
# （``ControlledTripIdentity.validate_identity``，答中文）与引导式收集的种子上限
# （``GuidedIntakeState.seed_destinations``）。此前两处各写一个字面量 3，而其中一处
# 是 Pydantic 的字段长度规则、答的是英文 —— 见 validate_identity 里的注释。
MAX_CONTROLLED_DESTINATIONS = 3

ORIGIN_PLACE_KINDS = {PlaceKind.CITY, PlaceKind.AIRPORT, PlaceKind.TRAIN_STATION}
DESTINATION_PLACE_KINDS = {
    PlaceKind.CITY,
    PlaceKind.ADMINISTRATIVE_AREA,
    PlaceKind.ISLAND,
    PlaceKind.SCENIC_AREA,
}
ITINERARY_PLACE_KINDS = {
    PlaceKind.SCENIC_AREA,
    PlaceKind.POI,
    PlaceKind.HOTEL,
    PlaceKind.RESTAURANT,
    PlaceKind.TRAIN_STATION,
}


class PlaceIdentity(_StrictInputModel):
    place_id: str = Field(min_length=3)
    provider: Literal["osm", "amap", "manual_verified"]
    kind: PlaceKind
    name: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    country_code: str = Field(min_length=2, max_length=2)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    admin_path: List[str] = Field(default_factory=list)


class TravelParty(_StrictInputModel):
    adults: int = Field(ge=1, le=20)
    children: int = Field(ge=0, le=20)
    elderly_companions: bool = False
    accessibility_required: bool = False


class TravelStyle(_StrictInputModel):
    primary: str = Field(min_length=1)
    secondary_interests: List[str] = Field(default_factory=list)
    # 只有两个值，因为只有两个能被产出：``TripPlanner`` 按用户有没有动过那一栏写
    # ``current`` 或 ``suggested``，别处没有第二个写入方。
    #
    # 枚举里此前还有 ``preset`` 与 ``frequent``，**两个都没有任何产出方**。
    # ``preset`` 尤其误导：它看起来在说「这趟的风格来自用户挑的旅行风格」，而 preset
    # 从来不写 identity —— 它的节奏与预算走 Constraint Pack（见
    # ``preset/injector.py::PRESET_PACK_CONSTRAINT_CATEGORIES``），主题始终由
    # TripPlanner 那一栏决定。一个取不到的枚举值和一个过滤空集的过滤器是同一件事：
    # 那一行看起来像是在允许什么。
    source: Literal["current", "suggested"] = "suggested"


class ControlledTripIdentity(_StrictInputModel):
    origin: PlaceIdentity
    # ``max_length`` deliberately absent, and the cap enforced in the validator
    # below instead: a field-level length rule fires **before** the model
    # validator and answers in Pydantic's own English
    # ("List should have at most 3 items after validation, not 4"), while the
    # four sibling rules in ``validate_identity`` all answer in Chinese.  Batch 4
    # (E-05) measured exactly that: four hard input rules, three of them readable
    # to a Chinese-speaking user and the fourth not, from the same validator.
    # ``min_length=1`` stays on the field: an empty destination list cannot reach
    # the validator's own messages meaningfully, and "at least one" is a shape
    # rule rather than a product rule with its own wording.
    destinations: List[PlaceIdentity] = Field(min_length=1)
    start_date: date
    end_date: date
    party: TravelParty
    style: TravelStyle

    @model_validator(mode="after")
    def validate_identity(self) -> "ControlledTripIdentity":
        if self.origin.kind not in ORIGIN_PLACE_KINDS:
            raise ValueError("出发地只支持城市、机场或火车站")
        if len(self.destinations) > MAX_CONTROLLED_DESTINATIONS:
            raise ValueError(f"一次旅行最多 {MAX_CONTROLLED_DESTINATIONS} 个目的地")
        invalid = [item.display_name for item in self.destinations if item.kind not in DESTINATION_PLACE_KINDS]
        if invalid:
            raise ValueError(f"目的地只支持城市、行政区域、岛屿或景区型区域: {', '.join(invalid)}")
        if len({item.place_id for item in self.destinations}) != len(self.destinations):
            raise ValueError("目的地不能重复")
        days = (self.end_date - self.start_date).days + 1
        if days < 1:
            raise ValueError("结束日期不能早于开始日期")
        if days > 14:
            raise ValueError("单次旅行最多 14 天")
        if len(self.destinations) > days:
            raise ValueError("目的地数量不能超过行程天数")
        return self

    @property
    def duration_days(self) -> int:
        return (self.end_date - self.start_date).days + 1


class RouteName(str, Enum):
    TRIP_PLANNING = "trip_planning"
    DESTINATION_DISCOVERY = "destination_discovery"
    FAST_ANSWER = "fast_answer"
    TRIP_REFINEMENT = "trip_refinement"


class RouteAlternative(BaseModel):
    route: RouteName
    confidence: float = Field(ge=0, le=1)


class RouteDecision(BaseModel):
    route: RouteName
    confidence: float = Field(ge=0, le=1)
    alternatives: List[RouteAlternative] = Field(default_factory=list)
    signals: List[str] = Field(default_factory=list)
    requires_trip_draft: bool
    requires_confirmation: bool = False


class GuidedIntakeState(BaseModel):
    raw_input: str
    route_decision: RouteDecision
    controlled_identity: Optional[ControlledTripIdentity] = None
    seed_destinations: List[PlaceIdentity] = Field(default_factory=list, max_length=MAX_CONTROLLED_DESTINATIONS)
    missing_fields: List[
        Literal["origin", "destinations", "dates", "party", "style", "place_confirmation"]
    ] = Field(default_factory=list)
    ready_to_create: bool = False


# Identity slots that lock mid-run (origin/destination/route/dates/party).
# Bare 「时间」 is *not* a slot by itself: Chinese often means rest/opening/schedule
# phrasing (休息时间、时间表、时间安排). Trip-date intent uses 日期 or explicit
# trip-time compounds, or 「时间」 only when it is the direct object of change verbs
# without auxiliary schedule prefixes.
LockedIdentitySlot = Literal[
    "origin",
    "destination",
    "trip_dates",
    "party",
    "accessibility",
]


class LockedIdentityIntent(_StrictInputModel):
    classification: Literal["change_requested", "preservation_asserted", "none"]
    changed_slots: set[LockedIdentitySlot] = Field(default_factory=set)
    preserved_slots: set[LockedIdentitySlot] = Field(default_factory=set)


_LOCKED_IDENTITY_SLOT_PATTERNS: dict[LockedIdentitySlot, re.Pattern[str]] = {
    # Bare 起点/路线/老人/儿童/无障碍 are deliberately excluded. In normal
    # travel language they often describe a day route, care requirement, or
    # facility preference rather than a change to ControlledTripIdentity.
    "origin": re.compile(r"出发地|旅行出发城市|全程起点"),
    "destination": re.compile(r"目的地(?:顺序)?|途经目的地|到访城市"),
    "trip_dates": re.compile(
        r"行程日期|旅行日期|出发日期|返程日期|起止日期|日期"
        r"|出发时间|返程时间|行程时间|旅行时间|起止时间|出行时间|开始时间|结束时间"
    ),
    "party": re.compile(
        r"同行人数|同行人(?!的?无障碍)|成人人数?|儿童人数?|小孩人数?|老人人数?|老人同行|人数"
    ),
    "accessibility": re.compile(
        r"(?:(?:同行人|同行者|旅伴)的?(?:是否)?(?:需要)?)?无障碍(?:需求|要求)"
    ),
}
_LOCKED_IDENTITY_MUTATION = r"(?:改|改变|改成|改为|换|更换|调整|变更|增加|减少|取消|新增|删掉|移除)"
_LOCKED_IDENTITY_SLOT_EXPR = "(?:" + "|".join(
    pattern.pattern for pattern in _LOCKED_IDENTITY_SLOT_PATTERNS.values()
) + ")"
_PRESERVATION_JOINER = r"(?:、|和|与|及|以及|或|/|\s)+"
_PRESERVATION_VALUE = r"(?:不变|原样|原计划|照旧)"
_PRESERVATION_PATTERNS = (
    # Negative mutation may contain a human-readable list and may cross a comma
    # before reaching 「前提下」. It is removed before positive clause parsing.
    re.compile(
        rf"(?:在)?(?:不|不要|无需|无须|不能|禁止|别)(?:再)?{_LOCKED_IDENTITY_MUTATION}"
        r"(?:(?![。；;！？!?]).){0,80}?(?:的前提下|前提下|情况下|，|,|；|;|。|$)",
        re.IGNORECASE,
    ),
    # 保持/维持 + slot list + 不变/原计划.
    re.compile(
        rf"(?:保持|维持)\s*{_LOCKED_IDENTITY_SLOT_EXPR}"
        rf"(?:{_PRESERVATION_JOINER}{_LOCKED_IDENTITY_SLOT_EXPR})*\s*{_PRESERVATION_VALUE}",
        re.IGNORECASE,
    ),
    # slot list + 保持/维持 + 不变/原计划, plus compact 「日期照旧」.
    re.compile(
        rf"{_LOCKED_IDENTITY_SLOT_EXPR}"
        rf"(?:{_PRESERVATION_JOINER}{_LOCKED_IDENTITY_SLOT_EXPR})*\s*"
        rf"(?:(?:保持|维持)\s*{_PRESERVATION_VALUE}|照旧)",
        re.IGNORECASE,
    ),
    # A user may restate the already-persisted identity by value instead of by
    # field label (for example 「保持上海出发、日本三城、5月10日至16日不变」).
    # Treat the bounded keep...unchanged clause as preservation, never as a
    # second source from which identity values are parsed.
    re.compile(
        rf"(?:保持|维持)(?:(?![。；;！？!?]).){{1,80}}?{_PRESERVATION_VALUE}",
        re.IGNORECASE,
    ),
)
_POSITIVE_CLAUSE_SPLIT = re.compile(r"[，,；;。！？!?：:\n]+|(?:但是|但|不过|然而|只调整|仅调整)")
_MUTATION_PREFIX_FILLER = (
    r"(?:(?:一下|重新|当前|本次|这次|一个|一位|一名|一处|一座|新的|"
    r"\d+[个位名处座]?|[一二三四五六七八九十]+[个位名处座]?)\s*){0,4}"
)
_TRIP_TIME_CHANGE = re.compile(
    rf"{_LOCKED_IDENTITY_MUTATION}(?:一下|成|到|为|至)?时间(?!表|安排|段|充裕|充足)"
    rf"|(?<!休息)(?<!开放)(?<!营业)(?<!就餐)(?<!用餐)时间{_LOCKED_IDENTITY_MUTATION}",
    re.IGNORECASE,
)


def _locked_identity_slots_in(text: str) -> set[LockedIdentitySlot]:
    return {
        slot
        for slot, pattern in _LOCKED_IDENTITY_SLOT_PATTERNS.items()
        if pattern.search(text)
    }


def _preservation_spans(text: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    for pattern in _PRESERVATION_PATTERNS:
        matches.extend(pattern.finditer(text))
    return sorted(matches, key=lambda match: (match.start(), match.end()))


def classify_locked_identity_intent(text: str) -> LockedIdentityIntent:
    """Classify locked identity edits without treating preservation as mutation.

    This deterministic guard never derives or mutates ``ControlledTripIdentity``;
    the persisted TripRun field remains the sole source of identity truth.
    """

    normalized = " ".join(text.strip().split())
    if not normalized:
        return LockedIdentityIntent(classification="none")

    preserved_slots: set[LockedIdentitySlot] = set()
    preservation_spans = _preservation_spans(normalized)
    for match in preservation_spans:
        preserved_slots.update(_locked_identity_slots_in(match.group(0)))

    positive_text = list(normalized)
    for match in preservation_spans:
        positive_text[match.start():match.end()] = " " * (match.end() - match.start())
    positive = "".join(positive_text)

    changed_slots: set[LockedIdentitySlot] = set()
    for clause in _POSITIVE_CLAUSE_SPLIT.split(positive):
        clause = clause.strip()
        if not clause:
            continue
        for slot, slot_pattern in _LOCKED_IDENTITY_SLOT_PATTERNS.items():
            slot_expr = slot_pattern.pattern
            mutation = re.compile(
                rf"(?:把|将|请)?\s*(?:{slot_expr})\s*(?:要|需要|希望|想要|重新)?\s*"
                rf"{_LOCKED_IDENTITY_MUTATION}"
                rf"|{_LOCKED_IDENTITY_MUTATION}\s*{_MUTATION_PREFIX_FILLER}(?:{slot_expr})",
                re.IGNORECASE,
            )
            if mutation.search(clause):
                changed_slots.add(slot)

        if _TRIP_TIME_CHANGE.search(clause):
            changed_slots.add("trip_dates")

    classification: Literal["change_requested", "preservation_asserted", "none"]
    if changed_slots:
        classification = "change_requested"
    elif preservation_spans:
        classification = "preservation_asserted"
    else:
        classification = "none"
    return LockedIdentityIntent(
        classification=classification,
        changed_slots=changed_slots,
        preserved_slots=preserved_slots,
    )


# 分流判断本身不在这一层：它要调模型，而 entities 是无依赖的形状层。
# 判断在 `services/route_intent.py`。
