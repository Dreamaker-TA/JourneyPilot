"""Deterministic candidate admission: normalize identity, score everything else."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..entities.delivery_bundle import (
    CandidateAdmissionResult,
    CandidateFitScores,
    DiningCandidate,
    FactAssertion,
    LodgingCandidate,
    ResearchCandidate,
    SourceRecord,
    TransportCandidate,
    VisitCandidate,
    WeatherImpact,
)
from ..entities.provider_evidence import TIMETABLED_TRANSPORT_CLASSES
from ..tools.governance import compiled_tool_source_id_is_about
from .destination_scope import is_within_destination
from .weather_impact_engine import day_weather_fit


_UNKNOWN_IDENTITY_MARKERS = (
    "unknown",
    "not verified",
    "unverified",
    "reference only",
    "placeholder",
    "no verified specific",
    "未检索",
    "未确认",
    "待确认",
    "不详",
    "未確認",
)

_DINING_PROVIDER_TYPE_MARKERS = (
    "餐饮服务",
    "餐厅",
    "餐馆",
    "饮食店",
    "咖啡",
    "酒吧",
    "居酒屋",
    "料理店",
    "食堂",
    "レストラン",
    "カフェ",
    "飲食店",
)
_DINING_PROVIDER_TYPE_TOKENS = {
    "restaurant",
    "cafe",
    "coffee",
    "bar",
    "pub",
    "izakaya",
    "fast_food",
    "food_court",
    "food_stall",
}
_LODGING_PROVIDER_TYPE_MARKERS = (
    "酒店",
    "旅馆",
    "旅館",
    "民宿",
)
_LODGING_PROVIDER_TYPE_TOKENS = {
    "hotel",
    "hostel",
    "guest_house",
    "motel",
    "resort",
    "apartment",
}

# Visit 的 provider 域没有 allowlist：博物馆、寺庙、公园、观景台、商场、amap 的
# 中文类别都算，逐一列举只会把没列到的真实场馆判假。能列举的是反面——一条
# provider 类型明确说自己是行政区划、边界或纯地名时，它描述的是一片区域，不是一个
# 能进门停留的地点。Nominatim 的形态是 ``boundary;administrative;city`` 一类，
# amap 的对应类别是「地名地址信息;行政地名」一族。
_ADMINISTRATIVE_PROVIDER_TYPE_MARKERS = (
    "行政区",
    "行政地名",
    "省级地名",
    "市级地名",
    "区县级地名",
    "乡镇级地名",
    "村庄级地名",
)
_ADMINISTRATIVE_PROVIDER_TYPE_TOKENS = {
    "boundary",
    "administrative",
    "political",
    "postcode",
    "city",
    "town",
    "village",
    "hamlet",
    "suburb",
    "quarter",
    "neighbourhood",
    "neighborhood",
    "borough",
    "county",
    "state",
    "province",
    "prefecture",
    "region",
    "municipality",
    "country",
    "continent",
}


# Candidate Gate uses this vocabulary to distinguish a missing exact answer
# from a truth failure.  Re-admission uses the same boundary to ensure a
# dynamic fact cannot be treated as current forever merely because its source
# remains active.
DYNAMIC_FACT_FIELD_MARKERS = frozenset(
    {
        "availability",
        "inventory",
        "price",
        "spend",
        "cost",
        "fare",
        "route",
        "departure",
        "arrival",
        "duration",
        "schedule",
        "time_window",
        "weather_context",
    }
)


def is_dynamic_fact_field(field_path: str) -> bool:
    """Return whether a persisted Fact describes a volatile live option."""

    normalized = str(field_path or "").casefold()
    return any(marker in normalized for marker in DYNAMIC_FACT_FIELD_MARKERS)


def _is_dining_provider_type(value: str) -> bool:
    normalized = " ".join(value.split()).casefold()
    if any(marker.casefold() in normalized for marker in _DINING_PROVIDER_TYPE_MARKERS):
        return True
    tokens = {
        token
        for token in re.split(r"[^a-z0-9_]+", normalized.replace("-", "_"))
        if token
    }
    return bool(tokens & _DINING_PROVIDER_TYPE_TOKENS)


def _is_lodging_provider_type(value: str) -> bool:
    normalized = " ".join(value.split()).casefold()
    if any(marker.casefold() in normalized for marker in _LODGING_PROVIDER_TYPE_MARKERS):
        return True
    tokens = {
        token
        for token in re.split(r"[^a-z0-9_]+", normalized.replace("-", "_"))
        if token
    }
    return bool(tokens & _LODGING_PROVIDER_TYPE_TOKENS)


def is_administrative_provider_type(value: str) -> bool:
    """一条 provider 类型是否描述行政区划/边界/纯地名，而不是一个具体地点。"""
    normalized = " ".join(value.split()).casefold()
    if any(
        marker.casefold() in normalized
        for marker in _ADMINISTRATIVE_PROVIDER_TYPE_MARKERS
    ):
        return True
    tokens = {
        token
        for token in re.split(r"[^a-z0-9_]+", normalized.replace("-", "_"))
        if token
    }
    return bool(tokens & _ADMINISTRATIVE_PROVIDER_TYPE_TOKENS)


def provider_place_type_matches_candidate_kind(
    value: str,
    candidate_kind: str,
) -> bool:
    """Apply the same provider-domain boundary before and during admission."""
    if candidate_kind == "dining":
        return _is_dining_provider_type(value)
    if candidate_kind == "lodging":
        return _is_lodging_provider_type(value)
    if candidate_kind == "visit":
        return not (
            _is_dining_provider_type(value) or _is_lodging_provider_type(value)
        )
    return False


def _identity_value_matches(asserted: Any, expected: str) -> bool:
    if not isinstance(asserted, str):
        return False
    return " ".join(asserted.split()).casefold() == " ".join(expected.split()).casefold()


def _fact_value_matches(asserted: Any, expected: Any) -> bool:
    if isinstance(expected, datetime):
        if not isinstance(asserted, str):
            return False
        try:
            parsed = datetime.fromisoformat(asserted.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed == expected
    if isinstance(expected, str):
        return _identity_value_matches(asserted, expected)
    if isinstance(expected, (dict, list)):
        return _without_absent_optional_values(asserted) == _without_absent_optional_values(
            expected
        )
    return asserted == expected


def _without_absent_optional_values(value: Any) -> Any:
    """Treat omitted Provider optionals and typed ``None`` fields identically."""
    if isinstance(value, dict):
        return {
            key: _without_absent_optional_values(child)
            for key, child in value.items()
            if child is not None
        }
    if isinstance(value, list):
        return [_without_absent_optional_values(child) for child in value]
    return value


_MISSING_SOURCE_SNAPSHOT_VALUE = object()
_SOURCE_LOCATOR_SEGMENT = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)(?P<indices>(?:\[\d+\])*)"
)


def _numeric_values_match(left: Any, right: Any) -> bool:
    return (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
        and float(left) == float(right)
    )


def _source_snapshot_value_at_locator(snapshot: Any, locator: str) -> Any:
    """Resolve a bounded source locator against its retained raw snapshot.

    Candidate price facts are allowed to map a provider field such as ``rate``
    into ``nightly_price_cny``.  We therefore validate the locator's actual
    scalar value instead of inferring a field name from the typed projection.
    The resolver intentionally accepts only plain dotted mapping keys and
    numeric list indexes; it never interprets expressions supplied by a model.
    """

    path = str(locator or "").strip()
    if path.startswith("snapshot."):
        path = path[len("snapshot.") :]
    elif path == "snapshot":
        return snapshot
    if not path:
        return _MISSING_SOURCE_SNAPSHOT_VALUE
    value = snapshot
    for segment in path.split("."):
        matched = _SOURCE_LOCATOR_SEGMENT.fullmatch(segment)
        if matched is None or not isinstance(value, Mapping):
            return _MISSING_SOURCE_SNAPSHOT_VALUE
        key = matched.group("key")
        if key not in value:
            return _MISSING_SOURCE_SNAPSHOT_VALUE
        value = value[key]
        for index_text in re.findall(r"\[(\d+)\]", matched.group("indices")):
            if not isinstance(value, list):
                return _MISSING_SOURCE_SNAPSHOT_VALUE
            index = int(index_text)
            if index >= len(value):
                return _MISSING_SOURCE_SNAPSHOT_VALUE
            value = value[index]
    return value


def fact_has_exact_numeric_source_support(
    fact: FactAssertion,
    *,
    expected_value: float | int,
    source_records: Iterable[SourceRecord],
) -> bool:
    """Require a verified numeric Fact to resolve to the same source value.

    A supporting source link alone is not enough for a displayed reference
    budget: otherwise a model can attach a legitimate source ID to a number
    that never appeared in that source's immutable snapshot.
    """

    if fact.status != "verified" or not _numeric_values_match(
        fact.asserted_value, expected_value
    ):
        return False
    sources_by_id = {
        source.source_record_id: source for source in source_records
    }
    return any(
        link.relation == "supports"
        and (source := sources_by_id.get(link.source_record_id)) is not None
        and source.lifecycle_status == "active"
        and _numeric_values_match(
            _source_snapshot_value_at_locator(source.snapshot, link.source_locator),
            expected_value,
        )
        for link in fact.source_links
    )


def _supporting_compiled_tool_sources(
    fact: FactAssertion,
    *,
    source_records: Sequence[SourceRecord],
    required_entity_id: str,
) -> list[SourceRecord]:
    """Every compiled tool source that supports ``fact`` *about* one entity.

    ``tools.governance.COMPILED_TOOL_SOURCE_ID_PREFIX`` separates "the server
    saw this tool call" from "the model typed a URL": a model-authored
    ``external_web`` record
    survives parsing with its body intact, but no model can put an
    ``external_tool`` record into a packet — the compiler substitutes its own
    registry entry or rejects the packet outright.

    The prefix alone is not enough, though.  It says *some* tool call happened
    this Run, not that the call was about this entity, so a model could hang a
    fabricated entity's claim on a genuine compiled source for a different
    place.  ``compiled_tool_source_id_is_about`` closes that: the id's final
    component is the digest of the entity the envelope returned, minted by
    ``research_packet_output`` from the very same helper.
    """

    sources_by_id = {source.source_record_id: source for source in source_records}
    matched: list[SourceRecord] = []
    for link in fact.source_links:
        if link.relation != "supports":
            continue
        if not compiled_tool_source_id_is_about(
            link.source_record_id, required_entity_id
        ):
            continue
        source = sources_by_id.get(link.source_record_id)
        if (
            source is not None
            and source.source_kind == "external_tool"
            and source.lifecycle_status == "active"
        ):
            matched.append(source)
    return matched


def fact_has_compiled_tool_source_support(
    fact: FactAssertion,
    *,
    source_records: Iterable[SourceRecord],
    required_entity_id: str,
) -> bool:
    """Require a claim to rest on a compiled source about ``required_entity_id``.

    This is the whole-entity grounding predicate.  The Research Packet compiler
    rewrites a candidate's identity facts to link its compiled source only for
    candidates whose ``place_id``/``route_id`` actually appeared in this round's
    Tool Gateway transcript, so "an identity fact supported by a compiled source
    whose id was minted for this very identity" is equivalent to "a Provider
    really returned this entity".  A candidate the model invented wholesale can
    satisfy neither half.
    """

    return bool(
        _supporting_compiled_tool_sources(
            fact,
            source_records=list(source_records),
            required_entity_id=required_entity_id,
        )
    )


# The reason a candidate whose kind this module does not recognize is refused.
# It is the candidate's own ``candidate_kind`` field that admission cannot
# ground, so naming that field reads correctly both in the admission record and
# in the Candidate Gate log line ("missing=candidate_kind") and in the research
# gap the Gate derives from it.
_UNKNOWN_CANDIDATE_KIND_FIELD = "candidate_kind"


def _identity_provenance_binding(
    candidate: ResearchCandidate,
) -> tuple[str, str] | None:
    """The one identity field whose fact must name the compiled entity source.

    Returns ``None`` only for a candidate kind this dispatch does not know, which
    the caller turns into a refusal — never into "no requirement".  See
    :func:`_missing_identity_provenance_binding`.

    The field chosen is the *entity identifier itself* — ``place_id`` for the
    three place domains, ``route_id`` for transport — because that is the exact
    string the compiled source id digests.  Hanging the requirement there makes
    the check closed over one value: the field's asserted value and the digest
    input are the same string, so no second field has to be trusted to relate
    them.  Any other identity field (a name, an address) would need the
    candidate's ``place_id`` as an unchecked intermediary and would still leave
    the identifier itself free to be invented.

    For transport this binds the *route*, because ``route_id`` is what
    ``_provider_route_source_id`` digests.  A ``route_id`` identifies one
    provider route — a train number, a service — not an individual leg, so this
    check alone once let a genuinely returned route carry model-authored
    timetable detail.  That is closed in
    :func:`_missing_declared_identity_fields`, which requires each of the leg's
    own required values to rest on a compiled source about the same route,
    rather than by digesting anything new here: changing the digest input would
    diverge from the mint site and reject every genuinely grounded candidate.
    """

    if isinstance(candidate, TransportCandidate):
        return "route_id", candidate.route_id
    if isinstance(candidate, (DiningCandidate, LodgingCandidate, VisitCandidate)):
        return "place_id", candidate.place_id
    return None


def _missing_identity_provenance_binding(
    candidate: ResearchCandidate,
    *,
    candidate_facts: Sequence[FactAssertion],
    source_records: Sequence[SourceRecord],
) -> Optional[str]:
    """The identity field this candidate cannot prove a Provider ever returned.

    An unrecognized candidate kind fails **closed**.  ``ResearchCandidate`` is a
    closed four-member discriminated union today, so that branch is unreachable —
    but a dispatch whose default is "no requirement" is exactly the shape this
    round exists to remove: a fifth kind added later would be admitted with no
    provider grounding at all and nothing would say so.  Refusal is also the only
    honest outcome available here; raising would take down the whole Run over a
    single candidate admission could simply decline.
    """

    binding = _identity_provenance_binding(candidate)
    if binding is None:
        return _UNKNOWN_CANDIDATE_KIND_FIELD
    field_path, raw_entity_id = binding
    entity_id = raw_entity_id.strip() if isinstance(raw_entity_id, str) else ""
    if not entity_id:
        return field_path
    if any(
        fact.field_path == field_path
        and fact.entity_ref.entity_id == candidate.candidate_id
        and _fact_value_matches(fact.asserted_value, entity_id)
        and fact_has_compiled_tool_source_support(
            fact,
            source_records=source_records,
            required_entity_id=entity_id,
        )
        for fact in candidate_facts
    ):
        return None
    return field_path


def normalize_lodging_price_evidence(
    candidate: ResearchCandidate,
    *,
    candidate_facts: Iterable[FactAssertion],
    source_records: Iterable[SourceRecord],
) -> ResearchCandidate:
    """Remove numeric lodging claims that cannot be found in source evidence.

    The function is deliberately non-destructive to the verified property
    identity: lack of a current rate should not discard an otherwise useful
    hotel.  It only suppresses unsupported price fields.  If a purported live
    quote has no surviving price evidence, it becomes a confirmation-only
    reference state so no consumer projection can present a model-authored
    number as a current quote.
    """

    if not isinstance(candidate, LodgingCandidate):
        return candidate
    facts = list(candidate_facts)
    sources = list(source_records)
    updates: dict[str, Any] = {}
    retained_price_fields = 0
    for field_path in ("nightly_price_cny", "total_price_cny"):
        value = getattr(candidate, field_path)
        if value is None:
            continue
        if any(
            fact.entity_ref.entity_id == candidate.candidate_id
            and fact.field_path == field_path
            and fact_has_exact_numeric_source_support(
                fact,
                expected_value=value,
                source_records=sources,
            )
            for fact in facts
        ):
            retained_price_fields += 1
        else:
            updates[field_path] = None
    effective_price_kind = candidate.price_kind
    if candidate.price_kind == "live_quote" and retained_price_fields == 0:
        updates["price_kind"] = "reference_estimate"
        effective_price_kind = "reference_estimate"
    if (
        effective_price_kind == "reference_estimate"
        and candidate.availability_status == "confirmed"
    ):
        updates["availability_status"] = "needs_confirmation"
    return candidate.model_copy(update=updates) if updates else candidate


def _active_lodging_budget_caps(
    candidate: ResearchCandidate,
    hard_constraints: Iterable[Mapping[str, Any]],
) -> list[tuple[str, Mapping[str, Any]]]:
    if not isinstance(candidate, LodgingCandidate):
        return []
    candidate_constraint_ids = set(candidate.active_constraint_ids)
    active: list[tuple[str, Mapping[str, Any]]] = []
    for item in hard_constraints:
        if not isinstance(item, Mapping):
            continue
        constraint_id = str(item.get("constraint_id") or "").strip()
        if (
            not constraint_id
            or constraint_id not in candidate_constraint_ids
            or str(item.get("status") or "active").strip().casefold() != "active"
            or str(item.get("category") or "").strip().casefold() != "budget_cap"
        ):
            continue
        active.append((constraint_id, item))
    return active


# A cap the candidate cannot yet be measured against scores mid-range: the
# property stays in the catalog and ranks below one with a sourced rate under
# the cap, above one whose sourced rate overshoots it.
_UNMEASURED_BUDGET_FIT = 0.5


def _lodging_budget_fit(
    candidate: ResearchCandidate,
    *,
    hard_constraints: Iterable[Mapping[str, Any]],
    candidate_facts: Iterable[FactAssertion],
    source_records: Iterable[SourceRecord],
) -> float:
    """Score a property against every explicit cap the user set.

    A user who says "at most" fixes a number, and only a date/stay-specific
    ``live_quote`` with a matching verified source value measures against it.
    A sourced rate under the cap scores 1.0; one above it scores the ratio by
    which it fits, so a 10% overshoot outranks a doubled price.
    """

    if not isinstance(candidate, LodgingCandidate):
        return 1.0
    facts = list(candidate_facts)
    sources = list(source_records)
    fit = 1.0
    for constraint_id, constraint in _active_lodging_budget_caps(
        candidate, hard_constraints
    ):
        params = constraint.get("params")
        if not isinstance(params, Mapping):
            fit = min(fit, _UNMEASURED_BUDGET_FIT)
            continue
        amount = params.get("amount")
        currency = str(params.get("currency") or "").strip().upper()
        per = str(params.get("per") or "").strip().casefold()
        if (
            not _numeric_values_match(amount, amount)
            or float(amount) <= 0
            or currency != "CNY"
            or per not in {"night", "day", "total"}
        ):
            fit = min(fit, _UNMEASURED_BUDGET_FIT)
            continue
        field_path = "total_price_cny" if per == "total" else "nightly_price_cny"
        price = getattr(candidate, field_path)
        if candidate.price_kind != "live_quote" or price is None:
            fit = min(fit, _UNMEASURED_BUDGET_FIT)
            continue
        price_facts = [
            fact
            for fact in facts
            if (
                fact.entity_ref.entity_id == candidate.candidate_id
                and fact.field_path == field_path
            )
        ]
        if not any(
            fact_has_exact_numeric_source_support(
                fact,
                expected_value=price,
                source_records=sources,
            )
            for fact in price_facts
        ):
            fit = min(fit, _UNMEASURED_BUDGET_FIT)
            continue
        fit = min(fit, 1.0 if float(price) <= float(amount) else float(amount) / float(price))
    return fit


def _missing_concrete_identity_fields(
    candidate: ResearchCandidate,
    identity_fact_values: Mapping[str, Sequence[Any]],
    *,
    candidate_facts: Sequence[FactAssertion],
    source_records: Sequence[SourceRecord],
) -> list[str]:
    """Every required identity field this candidate cannot honestly claim.

    Two layers answer that.  ``_missing_declared_identity_fields`` asks whether
    the candidate's own fields are concrete and are what its facts assert — a
    self-consistency check over the model's output.  The identity provenance
    binding then asks the question self-consistency cannot: was this entity
    *ever returned by a Provider*?  A wholly invented place or route can be
    perfectly self-consistent, so the binding is what keeps it out.
    """

    missing = _missing_declared_identity_fields(
        candidate,
        identity_fact_values,
        candidate_facts=candidate_facts,
        source_records=source_records,
    )
    unbound_identity_field = _missing_identity_provenance_binding(
        candidate,
        candidate_facts=candidate_facts,
        source_records=source_records,
    )
    if unbound_identity_field is not None and unbound_identity_field not in missing:
        missing.append(unbound_identity_field)
    return missing


def _missing_declared_identity_fields(
    candidate: ResearchCandidate,
    identity_fact_values: Mapping[str, Sequence[Any]],
    *,
    candidate_facts: Sequence[FactAssertion],
    source_records: Sequence[SourceRecord],
) -> list[str]:
    missing: list[str] = []
    required: tuple[tuple[str, Any], ...]
    if isinstance(candidate, LodgingCandidate):
        required = (
            ("property_name", candidate.property_name),
            ("place_id", candidate.place_id),
            ("provider_place_type", candidate.provider_place_type),
            ("provider_country_code", candidate.provider_country_code),
            ("address", candidate.address),
        )
    elif isinstance(candidate, DiningCandidate):
        required = (
            ("branch_name", candidate.branch_name),
            ("place_id", candidate.place_id),
            ("provider_place_type", candidate.provider_place_type),
            ("provider_country_code", candidate.provider_country_code),
            ("address", candidate.address),
        )
    elif isinstance(candidate, VisitCandidate):
        required = (
            ("name", candidate.name),
            ("place_id", candidate.place_id),
            ("provider_place_type", candidate.provider_place_type),
            ("provider_country_code", candidate.provider_country_code),
            ("address", candidate.address),
        )
    elif isinstance(candidate, TransportCandidate):
        required_values: list[tuple[str, Any]] = [
            ("route_id", candidate.route_id),
            ("selected_mode", candidate.selected_mode.value),
            ("duration_minutes", candidate.duration_minutes),
            (
                "segments",
                [segment.model_dump(mode="json") for segment in candidate.segments],
            ),
        ]
        if candidate.transport_class in TIMETABLED_TRANSPORT_CLASSES:
            required_values.extend(
                [
                    (
                        "departure_at",
                        candidate.departure_at,
                    ),
                    (
                        "arrival_at",
                        candidate.arrival_at,
                    ),
                ]
            )
        route_id = candidate.route_id.strip()
        # Only meaningful once the route itself is bound.  When it is not, the
        # provenance binding already names ``route_id`` and that is the whole
        # story — the Provider call never happened, so naming five more fields
        # derived from the same absent call tells the worker nothing new and
        # opens five research gaps where one is owed.
        route_is_bound = bool(route_id) and any(
            fact.field_path == "route_id"
            and fact.entity_ref.entity_id == candidate.candidate_id
            and _fact_value_matches(fact.asserted_value, route_id)
            and fact_has_compiled_tool_source_support(
                fact,
                source_records=source_records,
                required_entity_id=route_id,
            )
            for fact in candidate_facts
        )
        for field_path, value in required_values:
            if (
                value is None
                or (
                    field_path in {"departure_at", "arrival_at"}
                    and isinstance(value, datetime)
                    and value.utcoffset() is None
                )
                or not any(
                    _fact_value_matches(asserted, value)
                    for asserted in identity_fact_values.get(field_path, ())
                )
            ):
                missing.append(field_path)
                continue
            # Every one of these fields is a timetable claim, and none of them may be
            # satisfiable from prose.  The packet contract makes a verified transport
            # value occur in *a* supporting snapshot, but a model-authored
            # ``external_web`` record is a supporting snapshot the model wrote — so
            # without this binding, a candidate whose ``route_id`` is genuinely bound to
            # a compiled Provider record can still be admitted carrying a departure time
            # two hours off the one in that very record.  The binding is what makes the
            # leg's numbers the Provider's; the route's identity alone does not.
            if not route_is_bound:
                continue
            if not any(
                fact.field_path == field_path
                and fact.entity_ref.entity_id == candidate.candidate_id
                and _fact_value_matches(fact.asserted_value, value)
                and fact_has_compiled_tool_source_support(
                    fact,
                    source_records=source_records,
                    required_entity_id=route_id,
                )
                for fact in candidate_facts
            ):
                missing.append(field_path)
        return missing
    else:
        # This layer has no required-field tuple for an unrecognized kind, so it
        # names nothing.  That is not a pass: the caller's identity provenance
        # binding refuses the same candidate outright
        # (``_UNKNOWN_CANDIDATE_KIND_FIELD``), so the combined verdict fails
        # closed.  Never turn this branch into a standalone answer.
        return missing
    for field_path, value in required:
        normalized = value.strip().lower() if isinstance(value, str) else value
        if (
            isinstance(normalized, str)
            and (
                not normalized
                or any(marker in normalized for marker in _UNKNOWN_IDENTITY_MARKERS)
            )
        ):
            missing.append(field_path)
            continue
        if not any(
            _fact_value_matches(asserted, value)
            for asserted in identity_fact_values.get(field_path, ())
        ):
            missing.append(field_path)
    # Dining deliberately has no external-quality requirement.  A branch-level
    # review that resolves to this exact restaurant is a bonus, not a condition:
    # when one is retrieved the projection prints "外部评价已核验" beside the
    # option, and when none is the restaurant is still delivered, unmarked.  The
    # identity binding above already guarantees a Provider really returned this
    # place, which is the claim the itinerary actually rests on.  Requiring the
    # review instead deleted every restaurant in destinations whose script the
    # locality tokeniser cannot read (Thai, Cyrillic, Devanagari), trading a
    # usable meal for a verification the trip never needed.
    if (
        isinstance(candidate, DiningCandidate)
        and "provider_place_type" not in missing
        and not _is_dining_provider_type(candidate.provider_place_type)
    ):
        missing.append("provider_place_type")
    if (
        isinstance(candidate, LodgingCandidate)
        and "provider_place_type" not in missing
        and not _is_lodging_provider_type(candidate.provider_place_type)
    ):
        missing.append("provider_place_type")
    # Visit 的域边界是排除法，追加复核也只能是排除法：一个行政区划/边界记录能满足
    # 全部身份字段（有 place_id、有地址、有国码），但它不是一个能安排进某一天的
    # 停留点。Dining/Lodging 的追加复核问「是不是本域」，这一条问「是不是区域」。
    if (
        isinstance(candidate, VisitCandidate)
        and "provider_place_type" not in missing
        and is_administrative_provider_type(candidate.provider_place_type)
    ):
        missing.append("provider_place_type")
    return missing


# A constraint the researcher could not evaluate sits between a clean pass and
# a proven failure.
_UNKNOWN_CONSTRAINT_FIT = 0.5

# Scope is coarse: a place outside the controlled destination country is a poor
# fit for the trip, so it ranks last within its domain.
_OUT_OF_SCOPE_CONSTRAINT_FIT = 0.1


def _weather_fit(
    impacts: Sequence[WeatherImpact],
    evaluated_dates: set[date],
) -> float:
    """Average how suitable each evaluated day is for this candidate.

    An unscheduled candidate evaluated across the whole stay keeps a usable
    score as long as some day suits it; composition reads that score to put
    exposed options on the clearest days.
    """

    dates = set(evaluated_dates) or {impact.date for impact in impacts}
    if not dates:
        return 1.0
    return sum(
        day_weather_fit([impact for impact in impacts if impact.date == day])
        for day in dates
    ) / len(dates)


def _constraint_fit(
    candidate: ResearchCandidate,
    *,
    expected_destination_country_code: Optional[str],
    require_destination_country_scope: bool,
) -> float:
    """Score the candidate against the active hard constraints and trip scope."""

    evaluations = {item.constraint_id: item for item in candidate.constraint_evaluations}
    active = list(candidate.active_constraint_ids)
    if active:
        scores = []
        for constraint_id in active:
            evaluation = evaluations.get(constraint_id)
            if evaluation is None or evaluation.status == "unknown":
                scores.append(_UNKNOWN_CONSTRAINT_FIT)
            elif evaluation.status == "failed":
                scores.append(0.0)
            else:
                scores.append(1.0)
        fit = sum(scores) / len(scores)
    else:
        fit = 1.0
    if require_destination_country_scope and isinstance(
        candidate, (DiningCandidate, LodgingCandidate, VisitCandidate)
    ):
        expected_country = (expected_destination_country_code or "").strip().casefold()
        if (
            not expected_country
            or candidate.provider_country_code.strip().casefold() != expected_country
        ):
            fit = min(fit, _OUT_OF_SCOPE_CONSTRAINT_FIT)
    return fit


# Missing-field paths that are a *verdict about the candidate's own identity*
# rather than a fact somebody could go and look up.  ``place_id.destination_scope``
# is a geometric function of coordinates already in hand: this place is 80 km from
# the destination it claims, and no amount of further research moves it.  Anything
# that re-asks a Worker for *this candidate* on one of these paths is asking for a
# verbatim repeat of the same rejection — measured as a whole targeted research
# round returning the same ``candidate_id`` with the same ``missing``.
DETERMINISTIC_IDENTITY_VERDICT_FIELD_PATHS = frozenset({"place_id.destination_scope"})


def _out_of_destination_identity_fields(
    candidate: ResearchCandidate,
    *,
    destination_point: Optional[tuple[float, float]],
    identity_fact_values: Mapping[str, Sequence[Any]],
) -> list[str]:
    """Reject a located place that is not a stop in the destination it claims.

    The backstop half of the destination-scope rule.  The enum a worker selects from
    already drops far places (``research_packet_output._eligible_place_selection_options``),
    which is the half that keeps the *near* one instead of losing both; this half catches
    any path that reaches admission without having gone through that enum.

    **A rejection, not a fit score.**  Country scope right above is only a ranking
    signal, and composition does not act on rankings: every slot's option count is
    ``1 + (candidates − placed)``, so composition places every candidate it is given.
    A low ``constraint_fit`` would therefore change nothing at all.

    Coordinates come from the candidate's own verified identity facts, the same
    pair the map is drawn from.  No point on either side means the rule has
    nothing to say and the candidate passes — a destination whose geocode is
    missing is a supply outage, not a city full of out-of-scope places.
    """
    if destination_point is None:
        return []
    if not isinstance(candidate, (DiningCandidate, LodgingCandidate, VisitCandidate)):
        return []
    values: dict[str, float] = {}
    for field_path in ("latitude", "longitude"):
        for asserted in identity_fact_values.get(field_path, ()):  # first wins
            if isinstance(asserted, bool) or not isinstance(asserted, (int, float)):
                continue
            values[field_path] = float(asserted)
            break
    if set(values) != {"latitude", "longitude"}:
        return []
    if is_within_destination(
        values["latitude"],
        values["longitude"],
        destination_point[0],
        destination_point[1],
    ):
        return []
    return sorted(DETERMINISTIC_IDENTITY_VERDICT_FIELD_PATHS)


def admit_candidate(
    candidate: ResearchCandidate,
    *,
    fact_data_revision: int,
    weather_data_revision: int,
    selection_slot_id: Optional[str] = None,
    weather_impacts: Iterable[WeatherImpact] = (),
    weather_evaluated_dates: Iterable[date] = (),
    expected_destination_country_code: Optional[str] = None,
    require_destination_country_scope: bool = True,
    destination_point: Optional[tuple[float, float]] = None,
    identity_fact_values: Mapping[str, Sequence[Any]],
    hard_constraints: Iterable[Mapping[str, Any]] = (),
    candidate_facts: Iterable[FactAssertion] = (),
    source_records: Iterable[SourceRecord] = (),
) -> CandidateAdmissionResult:
    """Normalize one candidate into the catalog and score how well it fits.

    A concrete identity — a name, a resolvable place, a provider type matching
    the candidate kind — is what admission asserts. Budget, weather and
    constraint outcomes ride along as 0–1 ranking signals so composition can
    prefer the better option instead of losing the entity.

    ``candidate_facts`` and ``source_records`` are the evidence admission reads
    directly: a required field whose truth cannot be re-derived from a
    server-compiled source is treated as absent, not as asserted.
    """

    impacts = list(weather_impacts)
    evaluated_dates = set(weather_evaluated_dates)
    impact_ids = [impact.weather_impact_id for impact in impacts]
    if set(impact_ids) != set(candidate.weather_impact_ids):
        raise ValueError("candidate must carry the exact weather impacts evaluated by admission")
    facts = list(candidate_facts)
    sources = list(source_records)
    missing_fields = _missing_concrete_identity_fields(
        candidate,
        identity_fact_values,
        candidate_facts=facts,
        source_records=sources,
    )
    # Destination scope rides with identity, not with the fit scores: a place in
    # another city is not a worse option for this Day, it is not an option.
    # ``require_destination_country_scope`` gates it for the same reason it gates
    # the country check — a stored identity being re-admitted is not being scoped
    # afresh — so a bundle that already shipped does not lose its stops.
    if require_destination_country_scope:
        missing_fields = missing_fields + [
            field
            for field in _out_of_destination_identity_fields(
                candidate,
                destination_point=destination_point,
                identity_fact_values=identity_fact_values,
            )
            if field not in missing_fields
        ]
    fit_scores = CandidateFitScores(
        budget_fit=_lodging_budget_fit(
            candidate,
            hard_constraints=hard_constraints,
            candidate_facts=facts,
            source_records=sources,
        ),
        weather_fit=_weather_fit(impacts, evaluated_dates),
        constraint_fit=_constraint_fit(
            candidate,
            expected_destination_country_code=expected_destination_country_code,
            require_destination_country_scope=require_destination_country_scope,
        ),
    )
    common = dict(
        candidate_id=candidate.candidate_id,
        selection_slot_id=selection_slot_id,
        evaluated_fact_revision=fact_data_revision,
        evaluated_weather_revision=weather_data_revision,
        weather_impact_ids=impact_ids,
        checked_constraint_ids=list(candidate.active_constraint_ids),
        fit_scores=fit_scores,
    )
    if missing_fields:
        return CandidateAdmissionResult(
            **common,
            status="insufficient_for_admission",
            missing_field_paths=missing_fields,
        )
    return CandidateAdmissionResult(**common, status="passed")
