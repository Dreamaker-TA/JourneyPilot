"""Build compact TripRun summaries deterministically at stable workflow boundaries."""

from __future__ import annotations

from typing import Any, Dict, Iterable

from ...entities.state import TravelAgentState
from ...entities.trip_summary_card import TripSummaryCard, TripSummaryFact


def _brief_dict(state: TravelAgentState) -> Dict[str, Any]:
    brief = state.research_brief
    if brief is None:
        return {}
    identity = brief.controlled_trip_identity
    return {
        "destination": " → ".join(
            item.name or item.display_name for item in identity.destinations
        ),
        "duration_days": identity.duration_days,
        "departure_city": identity.origin.name or identity.origin.display_name,
        "departure_city_status": "provided",
        "constraints": [
            item.public_summary for item in (state.intent_spec.active_items if state.intent_spec else [])
        ],
        "travel_style": identity.style.primary,
    }


def _as_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _as_labels(value: Any, limit: int = 5) -> list[str]:
    if isinstance(value, str):
        candidates: Iterable[Any] = value.replace("，", "、").split("、")
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = []
    labels: list[str] = []
    for candidate in candidates:
        label = _as_text(candidate, 28)
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def build_trip_summary_card(brief: Dict[str, Any], boundary: str) -> Dict[str, Any]:
    """Project structured trip facts into the consumer card without an LLM call."""
    destination = _as_text(brief.get("destination"), 40) or "本次旅行"
    duration_days = brief.get("duration_days")
    duration = f"{duration_days} 天" if isinstance(duration_days, int) and duration_days > 0 else "行程天数待定"
    departure_city = _as_text(brief.get("departure_city"), 40)
    departure_provided = brief.get("departure_city_status") == "provided" and bool(departure_city)
    if not departure_provided:
        departure_city = ""

    headline = (
        f"{departure_city} → {destination} · {duration}"
        if departure_provided
        else f"{destination} · {duration}"
    )
    facts: list[TripSummaryFact] = [
        TripSummaryFact(label="目的地", value=destination, state="confirmed"),
        TripSummaryFact(
            label="行程",
            value=duration,
            state="confirmed" if isinstance(duration_days, int) and duration_days > 0 else "default",
        ),
        TripSummaryFact(
            label="出发地",
            value=departure_city or "稍后决定",
            state="confirmed" if departure_provided else "deferred",
        ),
    ]
    budget = _as_text(brief.get("budget"), 40)
    if budget:
        facts.append(TripSummaryFact(label="预算", value=budget, state="confirmed"))

    priorities = _as_labels(brief.get("constraints")) or _as_labels(brief.get("travel_style"))
    if not priorities:
        priorities = ["行程可执行", "信息核验"]

    summary = (
        f"从{departure_city}出发前往{destination}，优先围绕已确认的旅行偏好收敛可执行方案。"
        if departure_provided
        else f"目的地为{destination}；出发地稍后决定，先收敛目的地内路线与住宿，交通方案可按出发地替换。"
    )
    if boundary == "verified":
        current_focus = "已完成关键结论核验，正在根据可信信息收敛最终行程。"
        next_milestone = "生成可执行行程"
    else:
        current_focus = "正在将已确认需求拆成交通、住宿和行程动线的调研任务。"
        next_milestone = "核验关键交通与开放信息"

    return TripSummaryCard(
        headline=headline,
        summary=summary,
        facts=facts,
        priorities=priorities,
        current_focus=current_focus,
        next_milestone=next_milestone,
        compact_line=f"{headline} · 调研中",
        requires_user_confirmation=False,
    ).model_dump()


async def trip_summary_card_after_brief_node(state: TravelAgentState) -> Dict[str, Any]:
    return {"trip_summary_card": build_trip_summary_card(_brief_dict(state), "brief")}
