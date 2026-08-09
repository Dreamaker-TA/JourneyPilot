"""The single authority for an itinerary entity's stated evidence basis.

The persisted :class:`~travel_agent.entities.delivery_bundle.EntityLineage` never
reaches a traveller: the public JSON boundary strips it, and the formal PDF is
rendered from the report projection.  Without a derived statement an entry
backed by an admitted research candidate and one the Itinerary Planner authored
against public knowledge render identically, while only the first can expand its
sources — the absence of citations reads as an unexplained gap rather than a
stated basis.

Two product surfaces make that statement: the workspace/report JSON and the
exported PDF.  They describe the same canonical entity, so they must never be
able to disagree about it.  This module is therefore the only place the
derivation lives; ``services/public_delivery.py`` and ``services/pdf_export.py`` both
read it and neither may re-derive from ``lineage``.  It sits in ``entities/``
because it is pure domain judgement over Bundle types, which keeps the
``services`` → ``api`` direction from being inverted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional

from .delivery_bundle import (
    EntityType,
    StructuredItineraryV2,
    TransportLeg,
    TransportMode,
)


CITED_SOURCE = "cited_source"
PUBLIC_REFERENCE = "public_reference"
REFERENCE_SERVICE = "reference_service"

# Product copy for the two bases that need stating.  ``cited_source`` entries
# already carry their citation marks, so naming them again would add noise.
# These strings are mirrored byte-for-byte by the frontend chip
# (``frontend/src/components/citations/EvidenceBasisChip.tsx``); the PDF uses
# the short label.
PUBLIC_REFERENCE_LABEL = "公开资料整理"
PUBLIC_REFERENCE_HINT = "由规划模型依据公开资料写入，未附来源链接"

# A real service a provider returned for a nearby date, carried onto the plan
# because the requested date is outside the supplier's booking window.  Its
# number and times are real; that they still hold on the traveller's date is
# exactly what was not confirmed, so the basis says so instead of passing it off
# as either a cited fact or the model's own writing.
REFERENCE_SERVICE_LABEL = "参考班次"
REFERENCE_SERVICE_HINT = "来自供应商的真实班次，但未对你的出行日期确认"

_BASIS_BY_LINEAGE_KIND = {
    "candidate_entity": CITED_SOURCE,
    "authored_entity": PUBLIC_REFERENCE,
    "reference_entity": REFERENCE_SERVICE,
}

# The itinerary collections that own an ``EntityLineage``, with the id field the
# public payload keeps for each.  ``custom_blocks`` is absent by design: a
# traveller's own arrangement makes no evidence claim.
LINEAGE_BEARING_COLLECTIONS = (
    ("visit_stops", EntityType.VISIT_STOP, "item_id"),
    ("dining_stops", EntityType.DINING_STOP, "item_id"),
    ("lodging_stays", EntityType.LODGING_STAY, "stay_id"),
    ("transport_legs", EntityType.TRANSPORT_LEG, "transport_leg_id"),
)

# A door-to-door walk short enough to read as geometry: it links two places
# rather than asserting anything about one, so no surface states a basis for it.
#
# Only ``walk`` qualifies — a metro leg never folds into a connector.  (Do not describe
# this as "station passage": that reads as though an in-station transfer counted.)  A
# real metro leg would fail the fare and transfer conditions anyway, and more to the
# point its line and fare *are* an evidenced assertion: suppressing that basis would
# delete a statement of honesty rather than noise.
#
# This is the rule's only definition.  Clients do not re-derive it from the
# thresholds — the public projection stamps the verdict onto each leg as
# ``is_micro_transport`` (``services/public_delivery.py``) and every surface reads
# that field, so the workspace, the report and the PDF cannot drift apart.
MICRO_TRANSPORT_MAX_DURATION_MINUTES = 10
MICRO_TRANSPORT_MAX_DISTANCE_METERS = 800


class PublicProjectionContractViolation(RuntimeError):
    """A public surface found a Bundle shape its own contract forbids."""


def is_micro_transport_leg(leg: TransportLeg) -> bool:
    """Whether a leg is a short connector rather than a planned movement."""

    return (
        leg.transport_class == "flexible"
        and leg.route_status == "ready"
        and leg.selected_mode is TransportMode.WALK
        and leg.duration_minutes is not None
        and leg.duration_minutes <= MICRO_TRANSPORT_MAX_DURATION_MINUTES
        and leg.distance_meters is not None
        and leg.distance_meters <= MICRO_TRANSPORT_MAX_DISTANCE_METERS
        and (leg.total_cost_cny is None or leg.total_cost_cny == 0)
        and leg.transfer_count == 0
        and leg.booking_status == "not_required"
    )


@dataclass(frozen=True)
class EvidenceBasisView:
    """One itinerary's evidence bases, derived once and read by every surface."""

    basis_by_entity: Mapping[tuple[EntityType, str], str]
    micro_transport_leg_ids: frozenset[str]

    @classmethod
    def from_itinerary(cls, itinerary: StructuredItineraryV2) -> "EvidenceBasisView":
        basis: Dict[tuple[EntityType, str], str] = {}
        for collection, entity_type, id_field in LINEAGE_BEARING_COLLECTIONS:
            for entity in getattr(itinerary, collection):
                # Keyed by type as well as id: itinerary ids are unique per
                # collection, not across the whole itinerary.
                basis[(entity_type, getattr(entity, id_field))] = _BASIS_BY_LINEAGE_KIND[
                    entity.lineage.lineage_kind
                ]
        return cls(
            basis_by_entity=basis,
            micro_transport_leg_ids=frozenset(
                leg.transport_leg_id
                for leg in itinerary.transport_legs
                if is_micro_transport_leg(leg)
            ),
        )

    def basis_for(self, entity_type: EntityType, entity_id: str) -> str:
        """Read one entity's basis, failing closed on an unattributable entity.

        ``DeliveryBundle`` already requires every report block to reference an
        itinerary entity, so a miss here means the payload contradicts its own
        contract.  Publishing it with a blank or guessed basis would let a
        model-authored entry pass as sourced, so the surface names it instead.
        """

        basis = self.basis_by_entity.get((entity_type, entity_id))
        if basis is None:
            raise PublicProjectionContractViolation(
                "public projection cannot state an evidence basis for "
                f"{entity_type.value}/{entity_id}: it is absent from the canonical itinerary"
            )
        return basis

    def stated_basis_for(
        self, entity_type: EntityType, entity_id: str
    ) -> Optional[str]:
        """The basis a rendered surface should state, or ``None`` to stay silent.

        Only two entities stay silent, matching ``evidenceBasisForEntity`` on the
        client: a custom block is the traveller's own arrangement, and a micro
        connector is a link between places rather than a claim about one.
        """

        if entity_type is EntityType.CUSTOM_BLOCK:
            return None
        if (
            entity_type is EntityType.TRANSPORT_LEG
            and entity_id in self.micro_transport_leg_ids
        ):
            return None
        return self.basis_for(entity_type, entity_id)
