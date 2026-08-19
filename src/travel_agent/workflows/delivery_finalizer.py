"""Persist a complete v2 Delivery Bundle before completing its TripRun."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from langchain_core.runnables import RunnableConfig

from ..entities.delivery_bundle import (
    DeliveryBundle,
    DeliveryFailureRecord,
    DeliveryRevisionManifest,
    GateClass,
    InternalFailureClass,
    ResearchDomain,
    RunCoverageDisclosure,
    TerminalAttribution,
    TripWorkspaceV2,
    bundle_content_hashes,
)
from ..entities.evidence_basis import PublicProjectionContractViolation
from ..entities.state import TravelAgentState
from ..entities.trip_run import TripRunStatus, build_trip_run_completion_audit
from ..infrastructure.database import get_db_session
from ..infrastructure.delivery_bundle_store import (
    BundleCommitKind,
    DeliveryBundleStore,
    InMemoryDeliveryBundleStore,
)
from ..infrastructure.trip_run_store import InMemoryTripRunStore, TripRunStore
from ..services.public_delivery import public_delivery_bundle
from ..services.weather_adjustment import build_weather_adjustment_proposals
from .run_control import (
    DeliveryDeadlineExceeded,
    await_delivery_operation,
    remaining_delivery_seconds,
)


DEFAULT_PERSIST_RETRY_BUDGET = 3


class DeliveryFinalizationError(RuntimeError):
    def __init__(self, record: DeliveryFailureRecord, cause: Exception):
        super().__init__(record.public_message)
        self.record = record
        self.__cause__ = cause


def _terminal_recorded_at(state: TravelAgentState, bundle: Optional[DeliveryBundle] = None) -> datetime:
    """Use the durable deadline observation, never a fresh lifecycle clock."""

    if state.run_deadline is not None:
        return state.run_deadline.last_observed_at
    if bundle is not None:
        return bundle.manifest.created_at
    if state.minimum_delivery_draft is not None:
        return state.minimum_delivery_draft.planning_authorized_at
    return datetime.now(timezone.utc)


def _completion_audit_with_terminal(
    state: TravelAgentState,
    *,
    closure_status: str,
    reason_code: str,
    gate_class: Optional[GateClass] = None,
    bundle: Optional[DeliveryBundle] = None,
) -> Dict[str, Any]:
    """Bind the final lifecycle outcome to the sealed Draft's audit projection."""

    audit = build_trip_run_completion_audit(state)
    draft = state.minimum_delivery_draft
    if not audit or draft is None:
        return audit
    if bundle is not None:
        # `delivery_bundle` reaches LangGraph state after this atomic commit,
        # so enrich the pre-commit audit from the already validated immutable
        # Bundle rather than waiting for a later snapshot that might not run.
        document = bundle.report_projection.document
        audit["formal_delivery"] = {
            "bundle_id": bundle.manifest.bundle_id,
            "has_bundle": True,
            "report_ready": bundle.report_projection.status == "ready",
            # ``document.sections`` / ``document.summary`` do not exist on
            # ``TripReportDocument`` and never have.  This has not raised only
            # because ``days`` is ``min_length=1`` and ``or`` short-circuits before
            # reaching them — so the guard was one falsy ``days`` away from an
            # AttributeError inside the atomic finalize path, which is the worst
            # place in the pipeline to find one.
            "report_content_nonempty": bool(
                document and (document.days or document.overview or document.title)
            ),
            "projection_consistent": True,
        }
    terminal = TerminalAttribution(
        draft_id=draft.draft_id,
        closure_status=closure_status,  # type: ignore[arg-type]
        reason_code=reason_code,
        recorded_at=_terminal_recorded_at(state, bundle),
        delivery_bundle_id=(bundle.manifest.bundle_id if bundle is not None else None),
        gate_class=gate_class,
    )
    audit["terminal_attribution"] = terminal.model_dump(mode="json")
    return audit


def _require_sealed_completion_generation(state: TravelAgentState) -> None:
    """Reject direct finalization that bypasses the authorized 5/6/8 contract."""

    draft = state.minimum_delivery_draft
    if (
        draft is None
        or draft.run_id != state.run_id
        or not draft.planning_authorized
        or draft.planning_authorized_at is None
    ):
        raise ValueError(
            "delivery finalizer requires a sealed minimum delivery draft for this run"
        )
    deadline = state.run_deadline
    if (
        deadline is None
        or deadline.draft_id != draft.draft_id
        or deadline.planning_authorized_at != draft.planning_authorized_at
    ):
        raise ValueError(
            "delivery finalizer requires the sealed Draft deadline snapshot"
        )


def _config_value(config: Optional[RunnableConfig], key: str) -> Any:
    return (config or {}).get("configurable", {}).get(key)


def reduce_coverage_disclosure(state: TravelAgentState) -> RunCoverageDisclosure:
    """Say which research domains this Run finished without usable results for.

    Both inputs were already computed and then dropped.  The non-blocking
    ``delivery_quality_gaps`` the quality gate writes had exactly one reader, and
    it only looked at the blocking ones; the durable ``GateFailureAttribution``
    records reached the completion audit and stopped there.  A plan simply came
    out with fewer entries than asked for and said nothing about it.

    Only the domain is carried forward.  Reason codes, provider names and worker
    names stay here: on a product surface they read either as blame aimed at a
    supplier or as an internal error, and neither is what happened.

    Both inputs record *attempts*, and the sentence they feed is about the
    *outcome* — "this domain produced no usable results".  A domain that failed a
    round and then succeeded satisfies the first and contradicts the second, so
    the delivered itinerary has the final word: a domain with entities on the plan
    is never disclosed as empty.  Measured on the closing reruns before this
    filter existed: a plan carrying two real HSR legs with fares, four dining
    stops and seven local legs disclosed all three of those domains as having no
    usable results.
    """

    domains: list[ResearchDomain] = []
    for gap in state.delivery_quality_gaps:
        if gap.research_domain is not None and gap.research_domain not in domains:
            domains.append(gap.research_domain)
    for attribution in state.gate_failure_attributions.values():
        domain = attribution.research_domain
        if domain is not None and domain not in domains:
            domains.append(domain)
    delivered = _delivered_research_domains(state.trip_workspace_v2)
    return RunCoverageDisclosure(
        domains_without_results=sorted(
            (domain for domain in domains if domain not in delivered),
            key=lambda item: item.value,
        )
    )


def _delivered_research_domains(
    workspace: Optional[TripWorkspaceV2],
) -> frozenset[ResearchDomain]:
    """Which research domains actually put something on the delivered plan.

    Read off the itinerary rather than off the catalog: a candidate that was
    admitted and then not placed is not something the traveller received, and the
    sentence this feeds is about what they are looking at.
    """

    if workspace is None:
        return frozenset()
    itinerary = workspace.itinerary
    delivered: set[ResearchDomain] = set()
    if itinerary.visit_stops:
        delivered.add(ResearchDomain.VISIT)
    if itinerary.dining_stops:
        delivered.add(ResearchDomain.DINING)
    if itinerary.lodging_stays:
        delivered.add(ResearchDomain.LODGING)
    for leg in itinerary.transport_legs:
        if leg.transport_class == "long_distance":
            delivered.add(ResearchDomain.LONG_DISTANCE_TRANSPORT)
        else:
            delivered.add(ResearchDomain.LOCAL_TRANSPORT)
    return frozenset(delivered)


def assemble_delivery_bundle(state: TravelAgentState) -> DeliveryBundle:
    workspace = state.trip_workspace_v2
    facts = state.fact_store_snapshot
    weather = state.weather_context
    report = state.report_projection
    map_projection = state.map_projection
    source_index = state.source_index_projection
    if any(value is None for value in (workspace, facts, weather, report, map_projection, source_index)):
        raise ValueError("delivery finalizer requires every v2 snapshot and projection")
    assert workspace is not None
    assert facts is not None
    assert weather is not None
    assert report is not None
    assert map_projection is not None
    assert source_index is not None
    revisions = (
        workspace.workspace_revision,
        facts.fact_data_revision,
        weather.weather_data_revision,
    )
    report_revisions = (
        report.source_workspace_revision,
        report.source_fact_data_revision,
        report.source_weather_data_revision,
    )
    if (
        report.status != "ready"
        or report.document is None
        or report.generated_at is None
        or report_revisions != revisions
        or map_projection.source_workspace_revision != workspace.workspace_revision
        or source_index.source_fact_data_revision != facts.fact_data_revision
    ):
        raise ValueError(
            "delivery finalizer requires ready projections from the current bundle revisions"
        )
    weather = weather.model_copy(
        update={
            "impacts": sorted(
                state.weather_impacts.values(),
                key=lambda item: item.weather_impact_id,
            )
        }
    )
    coverage_disclosure = reduce_coverage_disclosure(state)
    hashes = bundle_content_hashes(
        workspace=workspace,
        fact_snapshot=facts,
        weather_snapshot=weather,
        report_projection=report,
        map_projection=map_projection,
        source_index=source_index,
        coverage_disclosure=coverage_disclosure,
    )
    identity = ":".join([workspace.run_id, *[hashes[key] for key in sorted(hashes)]])
    bundle_id = f"bundle_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
    created_at = report.generated_at
    if created_at is None:
        raise ValueError("ready delivery report has no generated timestamp")
    bundle = DeliveryBundle(
        manifest=DeliveryRevisionManifest(
            run_id=workspace.run_id,
            generation_id=workspace.generation_id,
            bundle_id=bundle_id,
            workspace_revision=workspace.workspace_revision,
            fact_data_revision=facts.fact_data_revision,
            weather_data_revision=weather.weather_data_revision,
            contract_versions={
                "workspace": workspace.contract_version,
                "facts": facts.contract_version,
                "weather": weather.contract_version,
            },
            content_hashes=hashes,
            created_at=created_at,
        ),
        workspace=workspace,
        fact_snapshot=facts,
        weather_snapshot=weather,
        report_projection=report,
        map_projection=map_projection,
        source_index=source_index,
        coverage_disclosure=coverage_disclosure,
    )
    # Delivery is the first projection of this bundle: a forecast-driven,
    # material weather impact that is already present here is new by definition
    # (there is no prior baseline).  Build adjustment proposals with an empty
    # prior so the traveller can apply/dismiss them immediately, instead of
    # waiting for a committing weather refresh that never fires while the
    # forecast is fresh and fully covered.
    proposals = build_weather_adjustment_proposals(
        bundle,
        weather,
        previous_impacts={},
    )
    return bundle.model_copy(
        update={
            "weather_snapshot": weather.model_copy(
                update={"adjustment_proposals": proposals}
            )
        }
    )


async def _mark_failed(
    trip_store: TripRunStore,
    state: TravelAgentState,
    record: DeliveryFailureRecord,
) -> None:
    failure_payload = {
        "failure_class": record.failure_class.value,
        "operation": record.operation,
        "attempts": record.attempts,
    }
    try:
        run = await trip_store.get_run(state.run_id)
        if run is not None and run.status != TripRunStatus.RUNNING:
            # A stop that lands during finalize is not a reason to lose the fact
            # that finalize was attempted and failed.  The status write is
            # correctly refused — the Run is already terminal — but the event was
            # the only durable record of the failure class, the operation and how
            # many attempts it took, and it went out with the status write.  The
            # persist path retries up to five times, so this window is as wide as
            # the whole commit loop.
            await trip_store.append_event_once(
                state.run_id,
                "run.delivery_failed",
                {**failure_payload, "run_status": run.status.value},
                idempotency_key=f"{state.run_id}:delivery_failed:{record.operation}",
            )
            return
        if run is not None and run.status == TripRunStatus.RUNNING:
            await trip_store.transition_status(
                state.run_id,
                TripRunStatus.FAILED,
                current_node="delivery_finalizer",
                error_code=record.failure_class.value,
                error_message=record.public_message,
                event_type="run.delivery_failed",
                payload=failure_payload,
                terminal_reason_code=f"delivery_integrity_{record.failure_class.value}",
                terminal_gate_class=GateClass.COMPOSITION.value,
                completion_audit=_completion_audit_with_terminal(
                    state,
                    closure_status="failed",
                    reason_code=f"delivery_integrity_{record.failure_class.value}",
                    gate_class=GateClass.COMPOSITION,
                ) or None,
            )
    except Exception:
        # The original persistence exception remains authoritative. A failed
        # lifecycle write must never be disguised as successful completion.
        return


async def _commit_initial_delivery_atomically(
    *,
    bundle_store: DeliveryBundleStore,
    trip_store: TripRunStore,
    state: TravelAgentState,
    bundle: DeliveryBundle,
) -> DeliveryBundle:
    """Commit the first Bundle and its visible terminal truth as one unit.

    PostgreSQL stores share one transaction, so a failed `delivery.ready`,
    completed status, or terminal event rolls back every snapshot/head/commit
    row as well.  The in-memory stores mirror that boundary for contract tests.

    LangGraph checkpoint writes (separate psycopg pool) are **not** in this
    transaction.  User-facing truth is TripRun + Bundle only; if a
    process dies after checkpoint but before this commit, resume must re-enter
    finalizer and rely on ``run_id:delivery:create`` idempotency rather than
    treating the graph position as delivery proof.
    """

    if bundle.manifest.run_id != state.run_id:
        raise ValueError("delivery bundle run id does not match finalizer state")
    commit_kwargs = {
        "bundle": bundle,
        "kind": BundleCommitKind.CREATE,
        "idempotency_key": f"{state.run_id}:delivery:create",
        "metadata": {"operation": "initial_delivery"},
    }
    completion_audit = _completion_audit_with_terminal(
        state,
        closure_status="completed",
        reason_code="delivery_bundle_ready",
        bundle=bundle,
    )

    async def complete(current: DeliveryBundle, *, session: Any | None = None) -> None:
        if current.manifest.bundle_id != bundle.manifest.bundle_id:
            raise RuntimeError("persisted delivery bundle is not the current bundle")
        completed, _ready_event, _terminal_event = await await_delivery_operation(
            trip_store.complete_delivery(
                state.run_id,
                bundle_id=current.manifest.bundle_id,
                manifest=current.manifest.model_dump(mode="json"),
                completion_audit=completion_audit or None,
                **({"session": session} if session is not None else {}),
            ),
            operation="trip_run_complete_delivery",
        )
        if completed.status != TripRunStatus.COMPLETED:
            raise RuntimeError("TripRun completion was not durably recorded")

    if isinstance(bundle_store, InMemoryDeliveryBundleStore) and isinstance(
        trip_store, InMemoryTripRunStore
    ):
        bundle_snapshot = bundle_store.snapshot_for_initial_delivery()
        trip_snapshot = trip_store.snapshot_for_initial_delivery()
        try:
            commit = await await_delivery_operation(
                bundle_store.commit(**commit_kwargs),
                operation="delivery_bundle_commit",
            )
            current = await await_delivery_operation(
                bundle_store.get_current(state.run_id),
                operation="delivery_bundle_readback",
            )
            if current is None or current.manifest.bundle_id != commit.bundle.manifest.bundle_id:
                raise RuntimeError("persisted delivery bundle is not the current bundle")
            await complete(current)
            return current
        except Exception:
            bundle_store.restore_initial_delivery(bundle_snapshot)
            trip_store.restore_initial_delivery(trip_snapshot)
            raise

    if isinstance(bundle_store, InMemoryDeliveryBundleStore) or isinstance(
        trip_store, InMemoryTripRunStore
    ):
        raise RuntimeError("delivery finalizer requires matched persistence stores")

    async with get_db_session() as session:
        commit = await await_delivery_operation(
            bundle_store.commit(**commit_kwargs, session=session),
            operation="delivery_bundle_commit",
        )
        current = await await_delivery_operation(
            bundle_store.get_current_in_session(session, state.run_id),
            operation="delivery_bundle_readback",
        )
        if current is None or current.manifest.bundle_id != commit.bundle.manifest.bundle_id:
            raise RuntimeError("persisted delivery bundle is not the current bundle")
        await complete(current, session=session)
        return current


async def delivery_finalizer_node(
    state: TravelAgentState,
    config: Optional[RunnableConfig] = None,
) -> Dict[str, Any]:
    bundle_store = _config_value(config, "delivery_bundle_store")
    trip_store = _config_value(config, "trip_run_store")
    if not isinstance(bundle_store, DeliveryBundleStore):
        raise ValueError("delivery_bundle_store is required")
    if not isinstance(trip_store, TripRunStore):
        raise ValueError("trip_run_store is required")
    try:
        _require_sealed_completion_generation(state)
    except Exception as exc:
        record = DeliveryFailureRecord(
            failure_class=InternalFailureClass.CONTRACT_VIOLATION,
            operation="validate_sealed_completion_generation",
            attempts=1,
            retry_exhausted=True,
            public_message="旅行方案未能完成，请重新生成。",
        )
        await _mark_failed(trip_store, state, record)
        raise DeliveryFinalizationError(record, exc) from exc
    try:
        remaining_delivery_seconds("delivery_finalizer")
    except DeliveryDeadlineExceeded as exc:
        record = DeliveryFailureRecord(
            failure_class=InternalFailureClass.PERSISTENCE_FAILURE,
            operation="delivery_deadline_guard",
            attempts=1,
            retry_exhausted=True,
            public_message="旅行方案未能在交付时限内完成。",
        )
        await _mark_failed(trip_store, state, record)
        raise DeliveryFinalizationError(record, exc) from exc
    try:
        bundle = assemble_delivery_bundle(state)
    except Exception as exc:
        record = DeliveryFailureRecord(
            failure_class=InternalFailureClass.CONTRACT_VIOLATION,
            operation="assemble_delivery_bundle",
            attempts=1,
            retry_exhausted=True,
            public_message="旅行方案未能完成，请重新生成。",
        )
        await _mark_failed(trip_store, state, record)
        raise DeliveryFinalizationError(record, exc) from exc
    try:
        # Dry-run the public projection before the Bundle becomes the Run's
        # committed result.  The projection is deterministic and side-effect free
        # — no I/O, no clock, no randomness — so running it twice costs nothing
        # and answers the one question the commit cannot take back: whether this
        # Bundle can be shown to the traveller at all.  Skip it and a Bundle that
        # fails projection is committed and marked COMPLETED, with only the first
        # serving request finding out — that is what
        # `InternalFailureClass.PROJECTION_FAILURE` is for.
        # The Run is still RUNNING at this point, so FAILED is a lawful transition;
        # after the commit it would not be.
        public_delivery_bundle(bundle)
    except PublicProjectionContractViolation as exc:
        record = DeliveryFailureRecord(
            failure_class=InternalFailureClass.PROJECTION_FAILURE,
            operation="public_delivery_projection_dry_run",
            attempts=1,
            retry_exhausted=True,
            public_message="旅行方案未能完成，请重新生成。",
        )
        await _mark_failed(trip_store, state, record)
        raise DeliveryFinalizationError(record, exc) from exc

    retry_budget = max(1, min(int(_config_value(config, "delivery_persist_retry_budget") or DEFAULT_PERSIST_RETRY_BUDGET), 5))
    last_error: Exception = RuntimeError("delivery persistence did not run")
    for attempt in range(1, retry_budget + 1):
        try:
            current = await _commit_initial_delivery_atomically(
                bundle_store=bundle_store,
                trip_store=trip_store,
                state=state,
                bundle=bundle,
            )
            return {
                "delivery_bundle": current,
                "delivery_persisted": True,
                "delivery_failure": None,
                "is_completed": True,
                "terminal_attribution": TerminalAttribution(
                    draft_id=state.minimum_delivery_draft.draft_id,
                    closure_status="completed",
                    reason_code="delivery_bundle_ready",
                    recorded_at=_terminal_recorded_at(state, current),
                    delivery_bundle_id=current.manifest.bundle_id,
                ) if state.minimum_delivery_draft is not None else None,
            }
        except Exception as exc:
            last_error = exc
            if isinstance(exc, DeliveryDeadlineExceeded):
                break
            if attempt < retry_budget:
                try:
                    await await_delivery_operation(
                        asyncio.sleep(0),
                        operation="delivery_persist_retry_backoff",
                    )
                except DeliveryDeadlineExceeded as deadline_exc:
                    last_error = deadline_exc
                    break

    record = DeliveryFailureRecord(
        failure_class=InternalFailureClass.PERSISTENCE_FAILURE,
        operation="persist_bundle_then_complete_run",
        attempts=retry_budget,
        retry_exhausted=True,
        public_message="旅行方案未能完成，请重新生成。",
    )
    await _mark_failed(trip_store, state, record)
    raise DeliveryFinalizationError(record, last_error) from last_error
