"""Atomic application boundary for TripWorkspaceV2 mutations and undo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional
from uuid import uuid4

from pydantic import TypeAdapter

from ..entities.delivery_bundle import (
    DeliveryBundle,
    DeliveryRevisionManifest,
    FactStoreSnapshot,
    TripWorkspaceV2,
    WeatherContextSnapshot,
    WeatherImpact,
    bundle_content_hashes,
)
from .delivery_projection import build_delivery_projections
from .candidate_readmission import (
    CandidateReadmissionError,
    readmit_current_candidates,
    workspace_destination_country_codes,
    workspace_hard_constraint_pack,
)
from .weather_adjustment import build_weather_adjustment_proposals
from ..entities.workspace_v2_mutations import (
    ApplyWeatherAdjustmentMutation,
    DeleteCustomBlockMutation,
    DeleteLodgingStayMutation,
    DeleteTransportLegMutation,
    WorkspaceV2InversePatch,
    WorkspaceV2Mutation,
    WorkspaceV2MutationApplication,
    WorkspaceV2MutationError,
    WorkspaceSnapshotInversePatch,
    apply_workspace_v2_inverse,
    apply_workspace_v2_mutation,
)
from ..infrastructure.delivery_bundle_store import (
    BundleCommitKind,
    BundleIdempotencyMismatch,
    BundleCommitResult,
    BundleRevisionConflict,
    BundleRevisionVector,
    DeliveryBundleStore,
)


Clock = Callable[[], datetime]
IdFactory = Callable[[], str]
INVERSE_ADAPTER = TypeAdapter(WorkspaceV2InversePatch)


@dataclass(frozen=True)
class WorkspaceBundleMutationResult:
    application: WorkspaceV2MutationApplication
    commit: Optional[BundleCommitResult]


@dataclass(frozen=True)
class WorkspaceMutationPreview:
    application: WorkspaceV2MutationApplication
    total_cost_delta_cny: Optional[float]
    explicit_confirmation: bool = False

    @property
    def requires_confirmation(self) -> bool:
        return self.explicit_confirmation or self.total_cost_delta_cny not in (None, 0.0)


def _mutation_bundle(
    current: DeliveryBundle,
    workspace: TripWorkspaceV2,
    *,
    bundle_id: str,
    created_at: datetime,
    fact_snapshot: FactStoreSnapshot | None = None,
    weather_snapshot: WeatherContextSnapshot | None = None,
) -> DeliveryBundle:
    if fact_snapshot is None:
        fact_snapshot = current.fact_snapshot
    if weather_snapshot is None:
        weather_snapshot = current.weather_snapshot
    report, map_projection, source_index = build_delivery_projections(
        workspace,
        fact_snapshot,
        weather_snapshot,
        generated_at=created_at,
    )
    # A mutation changes the plan, not what the Run failed to research, so the
    # disclosure carries over unchanged from the Bundle being edited.
    coverage_disclosure = current.coverage_disclosure
    hashes = bundle_content_hashes(
        workspace=workspace,
        fact_snapshot=fact_snapshot,
        weather_snapshot=weather_snapshot,
        report_projection=report,
        map_projection=map_projection,
        source_index=source_index,
        coverage_disclosure=coverage_disclosure,
    )
    return DeliveryBundle(
        manifest=DeliveryRevisionManifest(
            run_id=current.manifest.run_id,
            bundle_id=bundle_id,
            workspace_revision=workspace.workspace_revision,
            fact_data_revision=fact_snapshot.fact_data_revision,
            weather_data_revision=weather_snapshot.weather_data_revision,
            contract_versions=current.manifest.contract_versions,
            content_hashes=hashes,
            created_at=created_at,
        ),
        workspace=workspace,
        fact_snapshot=fact_snapshot,
        weather_snapshot=weather_snapshot,
        report_projection=report,
        map_projection=map_projection,
        source_index=source_index,
        coverage_disclosure=coverage_disclosure,
    )


def _weather_with_readmission_impacts(
    current: DeliveryBundle,
    impacts: tuple[WeatherImpact, ...],
    *,
    workspace: TripWorkspaceV2 | None = None,
) -> WeatherContextSnapshot:
    """Persist newly evaluated Candidate impacts as an immutable weather revision.

    Candidate weather lineage must point into the same Bundle's weather
    snapshot.  When re-admission produces new impact records, advancing the
    weather revision is required: revision snapshots may never be replaced in
    place just to add a Candidate-specific derived impact.
    """

    merged = {
        item.weather_impact_id: item for item in current.weather_snapshot.impacts
    }
    for impact in impacts:
        merged[impact.weather_impact_id] = impact
    if (
        len(merged) == len(current.weather_snapshot.impacts)
        and all(
            merged[item.weather_impact_id] == item
            for item in current.weather_snapshot.impacts
        )
    ):
        return current.weather_snapshot
    merged_impacts = sorted(merged.values(), key=lambda item: item.weather_impact_id)

    provisional = current.weather_snapshot.model_copy(
        update={
            "weather_data_revision": current.manifest.weather_data_revision + 1,
            "impacts": merged_impacts,
            "adjustment_proposals": [],
        }
    )
    proposal_bundle = DeliveryBundle.model_construct(
        manifest=current.manifest,
        workspace=workspace or current.workspace,
        fact_snapshot=current.fact_snapshot,
        weather_snapshot=current.weather_snapshot,
        report_projection=current.report_projection,
        map_projection=current.map_projection,
        source_index=current.source_index,
    )
    return provisional.model_copy(
        update={
            "adjustment_proposals": build_weather_adjustment_proposals(
                proposal_bundle, provisional
            )
        }
    )


def _canonical_candidate_scopes(workspace: TripWorkspaceV2) -> dict[tuple[str, str], tuple[str, str | None]]:
    """Index every canonical Candidate materialization by stable entity identity."""

    itinerary = workspace.itinerary
    entities = (
        *(("visit", item.item_id, item) for item in itinerary.visit_stops),
        *(("dining", item.item_id, item) for item in itinerary.dining_stops),
        *(("lodging", item.stay_id, item) for item in itinerary.lodging_stays),
        *(("transport", item.transport_leg_id, item) for item in itinerary.transport_legs),
    )
    return {
        (kind, entity_id): (item.lineage.candidate_id, item.lineage.selection_slot_id)
        for kind, entity_id, item in entities
        if item.lineage.lineage_kind == "candidate_entity"
    }


def _new_candidate_materializations(
    before: TripWorkspaceV2,
    after: TripWorkspaceV2,
) -> tuple[tuple[str, str | None], ...]:
    """Find only Candidate lineages newly written by a pure reducer application."""

    previous = _canonical_candidate_scopes(before)
    current = _canonical_candidate_scopes(after)
    return tuple(
        dict.fromkeys(
            value
            for key, value in current.items()
            if previous.get(key) != value
        )
    )


class WorkspaceV2Service:
    def __init__(
        self,
        store: DeliveryBundleStore,
        *,
        clock: Clock = lambda: datetime.now(timezone.utc),
        id_factory: IdFactory = lambda: f"bundle_{uuid4().hex}",
    ) -> None:
        self._store = store
        self._clock = clock
        self._id_factory = id_factory

    def _require_current_candidate_materializations(
        self,
        current: DeliveryBundle,
        application: WorkspaceV2MutationApplication,
    ) -> None:
        """Reject every stale Candidate-to-canonical promotion in one boundary.

        The pure reducer can safely express all legacy operation shapes, but
        it must never be the authority for whether a Candidate is current.
        Re-admitting the changed lineages here covers selection slots, direct
        Visit/Transport replacements, route retries, and weather proposal
        replacements uniformly.
        """

        materializations = _new_candidate_materializations(
            current.workspace, application.workspace
        )
        if not materializations:
            return
        candidate_scopes: dict[str, list[str | None]] = {}
        for candidate_id, selection_slot_id in materializations:
            candidate_scopes.setdefault(candidate_id, []).append(selection_slot_id)
        try:
            readmission = readmit_current_candidates(
                current,
                candidate_scopes=candidate_scopes,
                constraint_pack=workspace_hard_constraint_pack(current.workspace),
                destination_country_codes=workspace_destination_country_codes(
                    current.workspace
                ),
                as_of=self._clock(),
            )
        except CandidateReadmissionError as exc:
            raise WorkspaceV2MutationError(exc.code, str(exc)) from exc
        admissions = {
            (item.candidate_id, item.selection_slot_id): item
            for item in readmission.admissions
        }
        candidates = {
            item.candidate_id: item for item in readmission.candidates
        }
        for candidate_id, selection_slot_id in materializations:
            candidate = candidates.get(candidate_id)
            admission = admissions.get((candidate_id, selection_slot_id))
            if (
                candidate is None
                or candidate.freshness_status != "current"
                or admission is None
                or admission.status != "passed"
            ):
                raise WorkspaceV2MutationError(
                    "candidate_not_current",
                    "Replacement Candidate did not pass current evidence and constraint re-admission",
                )
        if (
            readmission.catalog_changed
            or _weather_with_readmission_impacts(current, readmission.weather_impacts)
            != current.weather_snapshot
        ):
            raise WorkspaceV2MutationError(
                "candidate_refresh_required",
                "Refresh the current Candidate choices before confirming this replacement",
            )

    async def preview(
        self,
        *,
        run_id: str,
        expected: BundleRevisionVector,
        mutation: WorkspaceV2Mutation,
    ) -> WorkspaceMutationPreview:
        """Apply the reducer without persisting or moving the current Bundle pointer."""

        current = await self._current(run_id, expected)
        application = apply_workspace_v2_mutation(
            current.workspace,
            mutation,
            current.weather_snapshot,
        )
        self._require_current_candidate_materializations(current, application)
        before = current.workspace.itinerary.cost_summary.known_subtotal_cny
        after = application.workspace.itinerary.cost_summary.known_subtotal_cny
        delta = None if before is None or after is None else after - before
        return WorkspaceMutationPreview(
            application=application,
            total_cost_delta_cny=delta,
            explicit_confirmation=isinstance(
                mutation,
                (
                    ApplyWeatherAdjustmentMutation,
                    DeleteCustomBlockMutation,
                    DeleteLodgingStayMutation,
                    DeleteTransportLegMutation,
                ),
            ),
        )

    async def apply(
        self,
        *,
        run_id: str,
        expected: BundleRevisionVector,
        mutation: WorkspaceV2Mutation,
        idempotency_key: str,
    ) -> WorkspaceBundleMutationResult:
        """Apply a workspace mutation with optimistic CAS on commit.

        Read → pure compute → commit are **not** one DB lock.  Concurrent
        applies with the same ``expected`` vector: one commit wins; the other raises
        ``BundleRevisionConflict``.  Clients must re-read current + retry.  Correct
        fail-closed behavior, not silent last-write-wins.
        """
        mutation_payload = mutation.model_dump(mode="json")
        mutation_request = {
            "operation": "workspace_mutation",
            "mutation": mutation_payload,
            "base_bundle_id": expected.bundle_id,
            "base_workspace_revision": expected.workspace_revision,
            "base_fact_data_revision": expected.fact_data_revision,
            "base_weather_data_revision": expected.weather_data_revision,
        }
        replay = await self._store.get_commit(run_id, idempotency_key)
        if replay is not None:
            if replay.metadata.get("mutation_request") != mutation_request:
                raise BundleIdempotencyMismatch(
                    "idempotency key was used for another workspace mutation"
                )
            inverse = (
                INVERSE_ADAPTER.validate_python(replay.inverse_patch)
                if replay.inverse_patch is not None
                else None
            )
            return WorkspaceBundleMutationResult(
                application=WorkspaceV2MutationApplication(
                    workspace=replay.result.bundle.workspace,
                    changed=replay.metadata.get("mutation_outcome") != "no_op",
                    label=replay.metadata.get("label"),
                    inverse=inverse,
                ),
                commit=replay.result,
            )
        current = await self._current(run_id, expected)
        application = apply_workspace_v2_mutation(
            current.workspace,
            mutation,
            current.weather_snapshot,
        )
        self._require_current_candidate_materializations(current, application)
        if not application.changed:
            receipt = await self._store.record_noop_receipt(
                run_id=run_id,
                idempotency_key=idempotency_key,
                expected=expected,
                kind=BundleCommitKind.WORKSPACE_MUTATION_RECEIPT,
                idempotency_request=mutation_request,
                metadata={
                    "mutation": mutation_payload,
                    "mutation_request": mutation_request,
                    "mutation_outcome": "no_op",
                    "label": application.label,
                },
            )
            return WorkspaceBundleMutationResult(application=application, commit=receipt)
        bundle = _mutation_bundle(
            current,
            application.workspace,
            bundle_id=self._id_factory(),
            created_at=self._clock(),
        )
        commit = await self._store.commit(
            bundle=bundle,
            kind=BundleCommitKind.WORKSPACE_MUTATION,
            idempotency_key=idempotency_key,
            expected=expected,
            inverse_patch=application.inverse.model_dump(mode="json") if application.inverse else None,
            metadata={
                "mutation": mutation_payload,
                "mutation_request": mutation_request,
                "mutation_outcome": "committed",
                "label": application.label,
            },
            idempotency_request=mutation_request,
        )
        return WorkspaceBundleMutationResult(application=application, commit=commit)

    async def undo_current(
        self,
        *,
        run_id: str,
        expected: BundleRevisionVector,
        undo_of_mutation_id: str,
        idempotency_key: str,
    ) -> BundleCommitResult:
        undo_request = {
            "operation": "workspace_undo",
            "undo_of_mutation_id": undo_of_mutation_id,
            "base_bundle_id": expected.bundle_id,
            "base_workspace_revision": expected.workspace_revision,
            "base_fact_data_revision": expected.fact_data_revision,
            "base_weather_data_revision": expected.weather_data_revision,
        }
        replay = await self._store.get_commit(run_id, idempotency_key)
        if replay is not None:
            if replay.metadata.get("undo_request") != undo_request:
                raise BundleIdempotencyMismatch(
                    "idempotency key was used for another undo mutation"
                )
            return replay.result

        head = await self._store.get_undo_head(run_id)
        if head is None or head.mutation_id != undo_of_mutation_id:
            raise WorkspaceV2MutationError(
                "undo_head_changed", "The current workspace operation is no longer undoable"
            )
        inverse = INVERSE_ADAPTER.validate_python(head.inverse_patch)
        current = await self._current(run_id, expected)
        workspace = apply_workspace_v2_inverse(
            current.workspace,
            inverse,
            # A weather/fact catalog rebind may be the only revision after a
            # user selection or itinerary edit.  Rebase those narrow inverse
            # forms onto the current evidence; snapshot inverses remain
            # fail-closed because they could overwrite the refreshed catalog.
            allow_data_refresh_rebase=(
                current.workspace.workspace_revision > head.workspace_revision
            ),
        )
        # Undo can re-materialize a Candidate that a later mutation removed.
        # Treat that revival exactly like every other Candidate->canonical
        # transition; a stale source/cache/constraint verdict must not come
        # back merely because the inverse patch was valid when first written.
        self._require_current_candidate_materializations(
            current,
            WorkspaceV2MutationApplication(
                workspace=workspace,
                changed=workspace != current.workspace,
                label=head.label,
            ),
        )
        bundle = _mutation_bundle(
            current,
            workspace,
            bundle_id=self._id_factory(),
            created_at=self._clock(),
        )
        result = await self._store.commit(
            bundle=bundle,
            kind=BundleCommitKind.UNDO,
            idempotency_key=idempotency_key,
            expected=expected,
            inverse_patch=WorkspaceSnapshotInversePatch(
                applied_workspace_revision=workspace.workspace_revision,
                previous_workspace=current.workspace,
            ).model_dump(mode="json"),
            metadata={
                "undo_of_mutation_id": undo_of_mutation_id,
                "undo_request": undo_request,
                "undo_inverse_type": inverse.type,
                "label": head.label,
                "semantic_label": head.semantic_label,
                "undo_label": (
                    f"撤销：{head.semantic_label}"
                    if head.label.startswith("恢复：")
                    else f"恢复：{head.semantic_label}"
                ),
            },
            idempotency_request=undo_request,
        )
        return result

    async def _current(self, run_id: str, expected: BundleRevisionVector) -> DeliveryBundle:
        current = await self._store.get_current(run_id)
        if current is None or BundleRevisionVector.from_bundle(current) != expected:
            raise BundleRevisionConflict(
                BundleRevisionVector.from_bundle(current) if current is not None else None
            )
        return current
