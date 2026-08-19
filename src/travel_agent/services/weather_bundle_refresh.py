"""Atomic weather refresh for an immutable Delivery Bundle."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable
from uuid import uuid4

from pydantic import ValidationError

from ..entities.delivery_bundle import (
    DeliveryBundle,
    DeliveryRevisionManifest,
    EntityRef,
    EntityType,
    FactStoreSnapshot,
    TripWorkspaceV2,
    WeatherContextSnapshot,
    WeatherCoverage,
    WeatherDayContext,
    bundle_content_hashes,
)
from ..infrastructure.delivery_bundle_store import (
    BundleCommitKind,
    BundleIdempotencyMismatch,
    BundleRevisionConflict,
    BundleRevisionVector,
    DeliveryBundleStore,
)
from ..infrastructure.weather_provider import WeatherProviderRequest
from .delivery_projection import build_delivery_projections
from .candidate_readmission import (
    CandidateReadmissionError,
    readmit_current_catalog_candidates,
    workspace_destination_country_codes,
    workspace_hard_constraint_pack,
)
from .weather_adjustment import build_weather_adjustment_proposals
from .weather_context_builder import WeatherContextBuildResult, WeatherContextBuilder
from .weather_impact_engine import WeatherImpactEngine


logger = logging.getLogger(__name__)


Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

# A refused refresh is never a silent fallback: the reason code below is
# carried on the typed result, logged with its run id, and surfaced in the API
# receipt so a caller can always tell a refreshed Bundle from a refused one.
WEATHER_SNAPSHOT_INCONSISTENT = "weather_snapshot_inconsistent"


class WeatherRefreshRefused(RuntimeError):
    """A recomputed weather view cannot be safely committed for this Bundle.

    Carries a machine-readable ``code`` from the same vocabulary the Candidate
    re-admission boundary uses (``candidate_evidence_missing`` and friends),
    plus ``weather_snapshot_inconsistent`` for a recomputed snapshot that the
    Delivery Bundle contract itself rejects.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WeatherBundleRefreshResult:
    bundle: DeliveryBundle
    attempted: bool
    committed: bool
    used_previous_values: bool
    # Populated only when the refresh was attempted and deliberately refused.
    refusal_reason: str | None = None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def expired_weather_destination_ids(
    bundle: DeliveryBundle,
    *,
    now: datetime,
    forecast_horizon_days: int = 16,
) -> set[str]:
    """Return destinations that need an execution-relevant refresh."""
    now = _aware(now)
    assertion_index = {
        item.fact_assertion_id: item for item in bundle.fact_snapshot.fact_assertions
    }
    source_index = {
        item.source_record_id: item for item in bundle.fact_snapshot.source_records
    }
    stale: set[str] = set()
    horizon = now.date() + timedelta(days=forecast_horizon_days)
    for day in bundle.weather_snapshot.days:
        if day.data_kind == "seasonal_baseline":
            continue
        if day.data_kind == "unavailable":
            if now.date() <= day.date <= horizon:
                stale.add(day.destination_id)
            continue
        if not day.fact_assertion_ids:
            stale.add(day.destination_id)
            continue
        for assertion_id in day.fact_assertion_ids:
            assertion = assertion_index.get(assertion_id)
            if assertion is None or assertion.status != "verified":
                stale.add(day.destination_id)
                break
            if assertion.expires_at is not None and _aware(assertion.expires_at) <= now:
                stale.add(day.destination_id)
                break
            supporting_sources = [
                source_index.get(link.source_record_id)
                for link in assertion.source_links
                if link.relation == "supports"
            ]
            if not supporting_sources or all(
                source is None
                or source.provider_valid_until is None
                or (
                    _aware(source.provider_valid_until) <= now
                )
                for source in supporting_sources
            ):
                stale.add(day.destination_id)
                break
    return stale


def _coverage(days: Iterable[WeatherDayContext]) -> list[WeatherCoverage]:
    grouped: dict[str, list[WeatherDayContext]] = {}
    for day in days:
        grouped.setdefault(day.destination_id, []).append(day)
    result: list[WeatherCoverage] = []
    for destination_id, items in grouped.items():
        items = sorted(items, key=lambda item: item.date)
        available = [item.date for item in items if item.data_kind != "unavailable"]
        unavailable = [item.date for item in items if item.data_kind == "unavailable"]
        status = "complete" if available and not unavailable else "partial" if available else "unavailable"
        result.append(
            WeatherCoverage(
                destination_id=destination_id,
                start_date=items[0].date,
                end_date=items[-1].date,
                status=status,
                available_dates=available,
                unavailable_dates=unavailable,
            )
        )
    return sorted(result, key=lambda item: item.destination_id)


def _merge_unique(items: Iterable[object], *, key: Callable[[object], str]) -> list[object]:
    merged: dict[str, object] = {}
    for item in items:
        merged[key(item)] = item
    return list(merged.values())


def _fact_snapshot(
    bundle: DeliveryBundle,
    result: WeatherContextBuildResult,
) -> FactStoreSnapshot:
    sources = _merge_unique(
        [*bundle.fact_snapshot.source_records, *result.source_records],
        key=lambda item: item.source_record_id,
    )
    assertions = _merge_unique(
        [*bundle.fact_snapshot.fact_assertions, *result.fact_assertions],
        key=lambda item: item.fact_assertion_id,
    )
    provenance = _merge_unique(
        [*bundle.fact_snapshot.field_provenance, *result.field_provenance],
        key=lambda item: json.dumps(
            item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
        ),
    )
    return FactStoreSnapshot(
        fact_data_revision=bundle.manifest.fact_data_revision + 1,
        source_records=sources,
        fact_assertions=assertions,
        field_provenance=provenance,
    )


def _target_dates(bundle: DeliveryBundle) -> list[tuple[EntityRef, str, object, set[date]]]:
    itinerary = bundle.workspace.itinerary
    candidates = bundle.workspace.recommendation_catalog.candidate_index()
    day_dates = {day.day_id: day.date for day in itinerary.day_plans if day.date is not None}
    timeline_dates: dict[tuple[EntityType, str], set[date]] = {}
    for day in itinerary.day_plans:
        if day.date is None:
            continue
        for ref in day.timeline:
            timeline_dates.setdefault((ref.entity_type, ref.entity_id), set()).add(day.date)
    targets: list[tuple[EntityRef, str, object, set[date]]] = []
    entities = [
        (EntityType.VISIT_STOP, item.item_id, item) for item in itinerary.visit_stops
    ] + [
        (EntityType.DINING_STOP, item.item_id, item) for item in itinerary.dining_stops
    ] + [
        (EntityType.LODGING_STAY, item.stay_id, item) for item in itinerary.lodging_stays
    ] + [
        (EntityType.TRANSPORT_LEG, item.transport_leg_id, item)
        for item in itinerary.transport_legs
    ]
    for entity_type, entity_id, entity in entities:
        candidate = candidates.get(entity.lineage.candidate_id)
        if candidate is None:
            continue
        dates = set(timeline_dates.get((entity_type, entity_id), set()))
        day_id = getattr(entity, "day_id", None)
        if day_id in day_dates:
            dates.add(day_dates[day_id])
        if entity_type == EntityType.LODGING_STAY:
            current = entity.check_in_date
            while current < entity.check_out_date:
                dates.add(current)
                current += timedelta(days=1)
        departure = getattr(entity, "departure_at", None)
        if departure is not None:
            dates.add(departure.date())
        targets.append(
            (
                EntityRef(entity_type=entity_type, entity_id=entity_id),
                candidate.destination_id,
                candidate.weather_sensitivity,
                dates,
            )
        )
    return targets


def _frozen_weather_impact_refs(bundle: DeliveryBundle) -> set[str]:
    """Every weather impact id a frozen record still points at.

    Frozen itinerary lineage records what was considered when an entry was
    admitted, and the report projection copies those ids.  Nothing recomputes
    either, so they are the set the refreshed snapshot has to keep covering.
    """

    refs: set[str] = set()
    itinerary = bundle.workspace.itinerary
    for entity in [
        *itinerary.visit_stops,
        *itinerary.dining_stops,
        *itinerary.lodging_stays,
        *itinerary.transport_legs,
    ]:
        refs.update(entity.lineage.weather_impact_ids)
    document = bundle.report_projection.document
    if document is not None:
        for day in document.days:
            for block in day.blocks:
                refs.update(block.weather_impact_ids)
    return refs


def _carry_over_frozen_impacts(
    bundle: DeliveryBundle, weather: WeatherContextSnapshot
) -> tuple[WeatherContextSnapshot, frozenset[str], frozenset[tuple[str, date]]]:
    """Keep the impacts a frozen record still names, and say which Days they date.

    An impact id is content-addressed over the destination, the date, the target
    and the *condition* — so a condition that clears between two refreshes deletes
    the exact ids the itinerary was built around, and the Bundle contract then
    refuses the whole refresh.  A Run's weather could therefore stop updating
    forever, deterministically, because the sky improved.

    Carrying the old impact forward is honest bookkeeping: frozen lineage records
    what was considered at admission time, which is a fact about the past that a
    new forecast does not change.  The old assertions that support it are still in
    the merged fact snapshot, so nothing dangles.

    Also returns the ``(destination_id, date)`` of each carried impact, taken from
    the previous snapshot's own Day — an impact carries that Day's assertion ids,
    so the match is exact and needs no id archaeology.  That is what tells the
    projection a Day is showing a carried-forward history rather than only this
    refresh's forecast.
    """

    previous = {item.weather_impact_id: item for item in bundle.weather_snapshot.impacts}
    current = {item.weather_impact_id: item for item in weather.impacts}
    carried = {
        impact_id
        for impact_id in _frozen_weather_impact_refs(bundle)
        if impact_id not in current and impact_id in previous
    }
    if not carried:
        return weather, frozenset(), frozenset()
    day_keys: set[tuple[str, date]] = set()
    for impact_id in carried:
        impact = previous[impact_id]
        supporting = set(impact.fact_assertion_ids)
        for day in bundle.weather_snapshot.days:
            if day.date == impact.date and supporting & set(day.fact_assertion_ids):
                day_keys.add((day.destination_id, day.date))
    merged = {**current, **{impact_id: previous[impact_id] for impact_id in carried}}
    return (
        weather.model_copy(
            update={
                "impacts": sorted(
                    merged.values(), key=lambda item: item.weather_impact_id
                )
            }
        ),
        frozenset(carried),
        frozenset(day_keys),
    )


def _weather_snapshot(
    bundle: DeliveryBundle,
    result: WeatherContextBuildResult,
) -> tuple[WeatherContextSnapshot, bool, frozenset[tuple[str, date]]]:
    previous = {
        (item.destination_id, item.date): item for item in bundle.weather_snapshot.days
    }
    incoming = {
        (item.destination_id, item.date): item for item in result.weather_snapshot.days
    }
    used_previous = False
    retained: set[tuple[str, date]] = set()
    days: list[WeatherDayContext] = []
    for key in sorted(previous.keys() | incoming.keys()):
        new_day = incoming.get(key)
        old_day = previous.get(key)
        if (
            new_day is not None
            and new_day.data_kind == "unavailable"
            and old_day is not None
            and old_day.data_kind != "unavailable"
        ):
            days.append(old_day)
            used_previous = True
            retained.add(key)
        elif new_day is not None:
            days.append(new_day)
        elif old_day is not None:
            days.append(old_day)
            used_previous = True
            retained.add(key)

    impacts = []
    engine = WeatherImpactEngine()
    for target_ref, destination_id, sensitivity, target_dates in _target_dates(bundle):
        for day in days:
            if day.destination_id != destination_id:
                continue
            if target_dates and day.date not in target_dates:
                continue
            impacts.extend(
                engine.evaluate(
                    weather_day=day,
                    target_ref=target_ref,
                    sensitivity=sensitivity,
                )
            )
    impact_index = {item.weather_impact_id: item for item in impacts}
    return (
        WeatherContextSnapshot(
            weather_data_revision=bundle.manifest.weather_data_revision + 1,
            trip_start_date=bundle.weather_snapshot.trip_start_date,
            trip_end_date=bundle.weather_snapshot.trip_end_date,
            days=days,
            coverage=_coverage(days),
            impacts=sorted(impact_index.values(), key=lambda item: item.weather_impact_id),
            retrieved_at=result.weather_snapshot.retrieved_at,
        ),
        used_previous,
        frozenset(retained),
    )


def _rebind_catalog_for_snapshot(
    bundle: DeliveryBundle,
    *,
    facts: FactStoreSnapshot,
    weather: WeatherContextSnapshot,
    now: datetime,
) -> tuple[TripWorkspaceV2, WeatherContextSnapshot]:
    """Atomically re-evaluate catalog evidence against a new fact/weather view.

    Recomputing only canonical impacts leaves the catalog/admission lineage at the old
    weather revision.  So: build a lightweight staging bundle, deterministically
    re-admit every Candidate, and include its evaluated impact records in the same
    immutable weather snapshot before the final Bundle is validated and committed.
    """

    catalog = bundle.workspace.recommendation_catalog
    candidate_ids = tuple(catalog.candidate_index())
    if not candidate_ids:
        refreshed_packets = [
                packet.model_copy(
                    update={"fact_data_revision": facts.fact_data_revision}
                )
                for packet in catalog.research_packets
            ]
        refreshed_catalog = catalog.model_copy(
            update={
                "generation_id": bundle.manifest.generation_id,
                "fact_data_revision": facts.fact_data_revision,
                "weather_data_revision": weather.weather_data_revision,
                "research_packets": refreshed_packets,
                "admission_results": [],
                "candidate_discovery_records": [
                    record
                    for packet in refreshed_packets
                    for record in packet.candidate_discovery_records
                ],
                "candidate_ranking_scores": [],
            }
        )
        return (
            bundle.workspace.model_copy(
                update={
                    "workspace_revision": bundle.workspace.workspace_revision + 1,
                    "recommendation_catalog": refreshed_catalog,
                }
            ),
            weather,
        )

    staging_manifest = DeliveryRevisionManifest(
        run_id=bundle.manifest.run_id,
        generation_id=bundle.manifest.generation_id,
        bundle_id=bundle.manifest.bundle_id,
        workspace_revision=bundle.manifest.workspace_revision,
        fact_data_revision=facts.fact_data_revision,
        weather_data_revision=weather.weather_data_revision,
        contract_versions=bundle.manifest.contract_versions,
        content_hashes=bundle.manifest.content_hashes,
        created_at=now,
    )
    # Old Candidate impact ids may intentionally be absent from the newly
    # computed weather view.  model_construct is safe here because this is a
    # private, short-lived input to re-admission; the final bundle is created
    # through normal validation below.
    staging = DeliveryBundle.model_construct(
        manifest=staging_manifest,
        workspace=bundle.workspace,
        fact_snapshot=facts,
        weather_snapshot=weather,
        report_projection=bundle.report_projection,
        map_projection=bundle.map_projection,
        source_index=bundle.source_index,
    )
    try:
        readmission = readmit_current_catalog_candidates(
            staging,
            candidate_ids=candidate_ids,
            constraint_pack=workspace_hard_constraint_pack(bundle.workspace),
            destination_country_codes=workspace_destination_country_codes(
                bundle.workspace
            ),
            as_of=now,
            # A weather refresh increments the global fact revision by adding
            # weather facts. The constraint proof is carried forward only when
            # its candidate-fact/constraint/evaluation fingerprints still
            # match exactly; changed candidate evidence remains unknown.
            allow_fact_revision_rebind=True,
            # Older completed runs can lack a persisted controlled-country
            # anchor.  This maintenance-only rebind preserves their prior
            # canonical identity instead of inventing a country scope; every
            # new Candidate materialization still uses the strict default.
            allow_unanchored_existing_identity=True,
        )
    except CandidateReadmissionError as exc:
        raise WeatherRefreshRefused(
            exc.code,
            f"weather refresh cannot safely re-admit current catalog: {exc.code}",
        ) from exc
    merged_impacts = {
        item.weather_impact_id: item for item in weather.impacts
    }
    for impact in readmission.weather_impacts:
        merged_impacts[impact.weather_impact_id] = impact
    rebound_weather = weather.model_copy(
        update={
            "impacts": sorted(
                merged_impacts.values(), key=lambda item: item.weather_impact_id
            )
        }
    )
    # A newly forecast condition lands on the candidate's weather fit score and
    # on the typed adjustment proposal, so the refreshed catalog carries the new
    # forecast without touching the entities already on the itinerary.
    return (
        bundle.workspace.model_copy(
            update={
                "workspace_revision": bundle.workspace.workspace_revision + 1,
                "recommendation_catalog": readmission.catalog,
            }
        ),
        rebound_weather,
    )


def _build_refreshed_bundle(
    bundle: DeliveryBundle,
    result: WeatherContextBuildResult,
    *,
    now: datetime,
    id_factory: IdFactory,
) -> tuple[DeliveryBundle, bool]:
    """Recompute one whole candidate Bundle revision from a new forecast.

    Every step here is pure: it either yields a fully valid next Bundle or
    refuses.  Re-admission refuses through ``WeatherRefreshRefused``; the
    Delivery Bundle contract refuses through ``ValidationError`` when the
    recomputed weather view no longer covers frozen canonical lineage.  The
    caller turns both into a typed refusal instead of an unhandled error.
    """

    facts = _fact_snapshot(bundle, result)
    weather, used_previous, retained_days = _weather_snapshot(bundle, result)
    workspace, weather = _rebind_catalog_for_snapshot(
        bundle,
        facts=facts,
        weather=weather,
        now=now,
    )
    # After re-admission has merged back everything it re-evaluated, whatever a
    # frozen record still names and the new view no longer has is carried over.
    weather, carried_impact_ids, carried_days = _carry_over_frozen_impacts(
        bundle, weather
    )
    staging_for_proposals = DeliveryBundle.model_construct(
        manifest=DeliveryRevisionManifest(
            run_id=bundle.manifest.run_id,
            generation_id=bundle.manifest.generation_id,
            bundle_id=bundle.manifest.bundle_id,
            workspace_revision=workspace.workspace_revision,
            fact_data_revision=facts.fact_data_revision,
            weather_data_revision=weather.weather_data_revision,
            contract_versions=bundle.manifest.contract_versions,
            content_hashes=bundle.manifest.content_hashes,
            created_at=now,
        ),
        workspace=workspace,
        fact_snapshot=facts,
        weather_snapshot=bundle.weather_snapshot,
        report_projection=bundle.report_projection,
        map_projection=bundle.map_projection,
        source_index=bundle.source_index,
    )
    weather = weather.model_copy(
        update={
            "adjustment_proposals": build_weather_adjustment_proposals(
                staging_for_proposals,
                weather,
                carried_over_impact_ids=carried_impact_ids,
            )
        }
    )
    report, map_projection, source_index = build_delivery_projections(
        workspace,
        facts,
        weather,
        generated_at=now,
        historical_weather_days=retained_days | carried_days,
    )
    # Refreshing the weather does not change which domains the Run researched.
    coverage_disclosure = bundle.coverage_disclosure
    hashes = bundle_content_hashes(
        workspace=workspace,
        fact_snapshot=facts,
        weather_snapshot=weather,
        report_projection=report,
        map_projection=map_projection,
        source_index=source_index,
        coverage_disclosure=coverage_disclosure,
    )
    generated_id = id_factory()
    bundle_id = generated_id or f"bundle_{hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:24]}"
    refreshed = DeliveryBundle(
        manifest=DeliveryRevisionManifest(
            run_id=bundle.manifest.run_id,
            generation_id=bundle.manifest.generation_id,
            bundle_id=bundle_id,
            workspace_revision=workspace.workspace_revision,
            fact_data_revision=facts.fact_data_revision,
            weather_data_revision=weather.weather_data_revision,
            contract_versions=bundle.manifest.contract_versions,
            content_hashes=hashes,
            created_at=now,
        ),
        workspace=workspace,
        fact_snapshot=facts,
        weather_snapshot=weather,
        report_projection=report,
        map_projection=map_projection,
        source_index=source_index,
        coverage_disclosure=coverage_disclosure,
    )
    return refreshed, used_previous


class WeatherBundleRefreshService:
    def __init__(
        self,
        store: DeliveryBundleStore,
        builder: WeatherContextBuilder,
        *,
        clock: Clock = lambda: datetime.now(timezone.utc),
        id_factory: IdFactory = lambda: f"bundle_{uuid4().hex}",
    ) -> None:
        self._store = store
        self._builder = builder
        self._clock = clock
        self._id_factory = id_factory

    async def refresh_if_needed(
        self,
        *,
        run_id: str,
        expected: BundleRevisionVector,
        idempotency_key: str,
        operation: str = "weather_refresh_before_export",
    ) -> WeatherBundleRefreshResult:
        refresh_request = {
            "operation": operation,
            "base_bundle_id": expected.bundle_id,
            "base_workspace_revision": expected.workspace_revision,
            "base_fact_data_revision": expected.fact_data_revision,
            "base_weather_data_revision": expected.weather_data_revision,
        }
        replay = await self._store.get_commit(run_id, idempotency_key)
        if replay is not None:
            if replay.metadata.get("weather_refresh_request") != refresh_request:
                raise BundleIdempotencyMismatch(
                    "idempotency key was used for another weather refresh"
                )
            return WeatherBundleRefreshResult(
                replay.result.bundle,
                bool(replay.metadata["attempted"]),
                bool(replay.metadata["committed"]),
                bool(replay.metadata["used_previous_values"]),
            )
        current = await self._store.get_current(run_id)
        if current is None or BundleRevisionVector.from_bundle(current) != expected:
            raise BundleRevisionConflict(
                BundleRevisionVector.from_bundle(current) if current is not None else None
            )
        bundle = current
        now = self._clock()
        destination_ids = expired_weather_destination_ids(bundle, now=now)
        if not destination_ids:
            receipt = await self._store.record_noop_receipt(
                run_id=run_id,
                idempotency_key=idempotency_key,
                expected=expected,
                kind=BundleCommitKind.WEATHER_REFRESH_RECEIPT,
                idempotency_request=refresh_request,
                metadata={
                    "operation": operation,
                    "weather_refresh_request": refresh_request,
                    "refresh_outcome": "no_refresh_needed",
                    "attempted": False,
                    "committed": False,
                    "used_previous_values": False,
                },
            )
            return WeatherBundleRefreshResult(
                receipt.bundle, False, False, False
            )
        grouped: dict[str, list[WeatherDayContext]] = {}
        for day in bundle.weather_snapshot.days:
            if day.destination_id in destination_ids:
                grouped.setdefault(day.destination_id, []).append(day)
        requests = [
            WeatherProviderRequest(
                destination_id=destination_id,
                destination_name=destination_id,
                latitude=items[0].latitude,
                longitude=items[0].longitude,
                timezone=items[0].timezone,
                start_date=min(item.date for item in items),
                end_date=max(item.date for item in items),
            )
            for destination_id, items in sorted(grouped.items())
        ]
        result = await self._builder.build(
            requests=requests,
            weather_data_revision=bundle.manifest.weather_data_revision + 1,
            trip_start_date=bundle.weather_snapshot.trip_start_date,
            trip_end_date=bundle.weather_snapshot.trip_end_date,
        )
        if not any(day.data_kind != "unavailable" for day in result.weather_snapshot.days):
            latest = await self._store.get_current(run_id)
            if latest is None or BundleRevisionVector.from_bundle(latest) != expected:
                raise BundleRevisionConflict(
                    BundleRevisionVector.from_bundle(latest)
                    if latest is not None
                    else None
                )
            return WeatherBundleRefreshResult(bundle, True, False, True)

        try:
            refreshed, used_previous = _build_refreshed_bundle(
                bundle,
                result,
                now=now,
                id_factory=self._id_factory,
            )
        except (WeatherRefreshRefused, ValidationError) as exc:
            # An opportunistic refresh must never become an unhandled error,
            # and must never quietly pretend it succeeded either.  Report the
            # unchanged current Bundle together with the reason it refused.
            reason = (
                exc.code
                if isinstance(exc, WeatherRefreshRefused)
                else WEATHER_SNAPSHOT_INCONSISTENT
            )
            logger.warning(
                "weather refresh refused run_id=%s bundle_id=%s reason=%s: %s",
                run_id,
                bundle.manifest.bundle_id,
                reason,
                exc,
            )
            latest = await self._store.get_current(run_id)
            if latest is None or BundleRevisionVector.from_bundle(latest) != expected:
                raise BundleRevisionConflict(
                    BundleRevisionVector.from_bundle(latest)
                    if latest is not None
                    else None
                ) from exc
            return WeatherBundleRefreshResult(bundle, True, False, False, reason)
        commit = await self._store.commit(
            bundle=refreshed,
            kind=BundleCommitKind.WEATHER_FACT_REFRESH,
            idempotency_key=idempotency_key,
            expected=expected,
            metadata={
                "operation": operation,
                "weather_refresh_request": refresh_request,
                "refresh_outcome": "committed",
                "attempted": True,
                "committed": True,
                "used_previous_values": used_previous,
            },
            idempotency_request=refresh_request,
        )
        if commit.idempotent_replay:
            stored = await self._store.get_commit(run_id, idempotency_key)
            if (
                stored is None
                or stored.metadata.get("weather_refresh_request") != refresh_request
            ):
                raise BundleIdempotencyMismatch(
                    "idempotency key was used for another weather refresh"
                )
            return WeatherBundleRefreshResult(
                stored.result.bundle,
                bool(stored.metadata["attempted"]),
                bool(stored.metadata["committed"]),
                bool(stored.metadata["used_previous_values"]),
            )
        return WeatherBundleRefreshResult(
            commit.bundle,
            True,
            True,
            used_previous,
        )
