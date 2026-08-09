"""Workflow boundary for deterministic v2 delivery projections."""

from __future__ import annotations

from typing import Any, Dict

from langchain_core.messages import AIMessage

from ..entities.state import TravelAgentState
from ..panels.constraint import dining_allergy_reminders
from ..services.delivery_projection import build_delivery_projections, build_fact_snapshot


def _workspace_with_dining_reminders(state: TravelAgentState):
    """Attach soft food-allergy reminders only to dining consumer objects."""
    workspace = state.trip_workspace_v2
    if workspace is None:
        return None
    reminders = dining_allergy_reminders(state.constraint_pack)
    if not reminders:
        return workspace
    itinerary = workspace.itinerary
    dining_stops = [
        item.model_copy(update={"dining_reminders": list(reminders)})
        for item in itinerary.dining_stops
    ]
    return workspace.model_copy(
        update={
            "itinerary": itinerary.model_copy(update={"dining_stops": dining_stops})
        }
    )


async def delivery_projection_node(state: TravelAgentState) -> Dict[str, Any]:
    if state.delivery_quality_route != "passed" or state.trip_workspace_v2 is None:
        raise ValueError("delivery projections require a workspace that passed every quality gate")
    if state.weather_context is None:
        raise ValueError("delivery projections require the planning weather snapshot")
    quality = state.recommendation_quality
    if quality is None or any(
        status != "passed"
        for status in (
            quality.schema_gate,
            quality.candidate_gate,
            quality.slot_gate,
            quality.itinerary_gate,
            quality.source_weather_gate,
        )
    ):
        raise ValueError("delivery projections cannot bypass deterministic quality gates")

    workspace = _workspace_with_dining_reminders(state)
    facts = build_fact_snapshot(
        workspace,
        weather_sources=state.weather_source_records,
        weather_facts=state.weather_fact_assertions,
        weather_provenance=state.weather_field_provenance,
    )
    generated_at = max(
        [state.weather_context.retrieved_at]
        + [
            packet.generated_at
            for packet in workspace.recommendation_catalog.research_packets
        ]
    )
    report, map_projection, source_index = build_delivery_projections(
        workspace,
        facts,
        state.weather_context,
        generated_at=generated_at,
    )
    return {
        "messages": [AIMessage(content="正式旅行交付内容已构建，正在保存。")],
        "trip_workspace_v2": workspace,
        "fact_store_snapshot": facts,
        "report_projection": report,
        "map_projection": map_projection,
        "source_index_projection": source_index,
    }
