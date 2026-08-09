"""One fast-tier reading of what the whole trip is likely to cost.

Suppliers price very little of a plan.  Measured on the reference run, 16 billable
components carried 3 prices; the other 13 are museums, metro rides and meals that
no API quotes.  The delivered plan therefore stated 「已知费用 ¥210」 and nothing
about the trip — a number that is true and useless, because ¥210 is not what the
traveller is going to spend.

A budget is an approximate thing by nature, so it is asked of a model rather than
summed: the itinerary already names every component, and what is missing is only
the ordinary price of each.  The answer is kept in its own field
(``CostCoverageSummary.llm_estimated_total_cny``) and never added into the
supplier totals — those must stay exactly what suppliers said.

Three things this node will not do:

* invent a number when the call fails, returns nothing parseable, or answers with
  something that is not a plausible amount — the field simply stays ``None`` and
  every surface says nothing;
* run once composition is closed — a guess is not worth spending the delivery
  budget on;
* touch anything else on the workspace.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..entities.delivery_bundle import TripWorkspaceV2
from ..entities.state import TravelAgentState
from ..models.router import get_model_router
from ..utils.json_helpers import safe_parse_json
from .run_deadline import observe_run_deadline

logger = logging.getLogger(__name__)

# A trip budget below this is not a budget, and above it the model has almost
# certainly answered in the wrong unit or for the wrong party.  The bound exists
# to reject a broken answer, not to shape a plausible one, so it is deliberately
# wide.
_MIN_PLAUSIBLE_CNY = 1.0
_MAX_PLAUSIBLE_CNY = 5_000_000.0

_MAX_LISTED_COMPONENTS = 60

_SYSTEM_PROMPT = """你是旅行预算估算助手。你只回一个 JSON 对象，不写任何解释。

给你的是一份已经排好的行程清单。有些项目带着确定价格，多数没有——没有价格不代表
免费，只代表没人给过报价。你的任务是按常识补齐那些没有价格的项目，给出**整趟行程
的人均之外的总花费估算**（所有出行人合计，单位人民币元）。

口径：
- 只算行程里列出的这些项目：门票、餐费、住宿、城际与市内交通。
- 不含购物、纪念品、小费和意外开销。
- 已经给出确定价格的项目按给出的数字算，不要改。
- 没有价格的项目按该城市、该档次的常见价格估。
- 免费的项目就按 0 算。

只输出：{"estimated_total_cny": <数字>}"""


def _component_lines(workspace: TripWorkspaceV2) -> list[str]:
    """Name every billable component and say whether it already has a price."""

    itinerary = workspace.itinerary
    lines: list[str] = []

    def add(kind: str, name: str, price: Optional[float], extra: str = "") -> None:
        priced = f"已知 ¥{price:.0f}" if price is not None else "无报价"
        lines.append(f"- [{kind}] {name}{extra} · {priced}")

    for stop in itinerary.visit_stops:
        add("景点", stop.name, stop.estimated_cost_cny)
    for stop in itinerary.dining_stops:
        add("餐饮", stop.name, stop.estimated_cost_cny, f"（{stop.meal_type}）")
    for stay in itinerary.lodging_stays:
        add("住宿", stay.name, stay.total_price_cny, f"（{stay.nights} 晚）")
    for leg in itinerary.transport_legs:
        route = f"{leg.from_endpoint.name} → {leg.to_endpoint.name}"
        add("交通", route, leg.total_cost_cny, f"（{leg.selected_mode.value}）")
    return lines[:_MAX_LISTED_COMPONENTS]


def _party_line(state: TravelAgentState) -> str:
    party = (state.controlled_trip_identity or {}).get("party")
    if not isinstance(party, dict):
        return "出行人数未知，按 2 名成人估算"
    adults = party.get("adults")
    children = party.get("children")
    parts = []
    if isinstance(adults, int) and adults > 0:
        parts.append(f"成人 {adults} 人")
    if isinstance(children, int) and children > 0:
        parts.append(f"儿童 {children} 人")
    return "、".join(parts) if parts else "出行人数未知，按 2 名成人估算"


def _build_prompt(state: TravelAgentState, workspace: TripWorkspaceV2) -> str:
    itinerary = workspace.itinerary
    lines = _component_lines(workspace)
    return "\n".join(
        [
            f"行程：{itinerary.title}",
            f"天数：{itinerary.duration_days} 天",
            f"出行人：{_party_line(state)}",
            "",
            f"计费项目（共 {len(lines)} 项）：",
            *lines,
        ]
    )


def _parse_amount(raw: str) -> Optional[float]:
    payload = safe_parse_json(raw)
    if not isinstance(payload, dict):
        return None
    value = payload.get("estimated_total_cny")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    if not _MIN_PLAUSIBLE_CNY <= amount <= _MAX_PLAUSIBLE_CNY:
        return None
    return round(amount, 2)


def _with_estimate(workspace: TripWorkspaceV2, amount: float) -> TripWorkspaceV2:
    itinerary = workspace.itinerary
    summary = itinerary.cost_summary.model_copy(
        update={"llm_estimated_total_cny": amount}
    )
    return TripWorkspaceV2.model_validate(
        workspace.model_copy(
            update={"itinerary": itinerary.model_copy(update={"cost_summary": summary})}
        ).model_dump(mode="json")
    )


async def budget_estimate_node(state: TravelAgentState) -> Dict[str, Any]:
    """Ask once for the trip's likely total, or leave the field empty."""

    workspace = state.trip_workspace_v2
    if workspace is None:
        return {}
    if not _component_lines(workspace):
        return {}
    deadline = state.run_deadline
    if deadline is not None:
        _observed, observation = observe_run_deadline(deadline)
        if observation.composition_closed:
            logger.info("Budget estimate skipped: composition window is closed")
            return {}

    try:
        raw = await get_model_router().get_fast().ainvoke(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_prompt(state, workspace)},
            ],
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001 - an estimate is never worth failing delivery for
        logger.warning("Budget estimate call failed, shipping without one | error=%s", exc)
        return {}

    amount = _parse_amount(raw)
    if amount is None:
        logger.warning("Budget estimate unusable, shipping without one | raw=%.200s", raw)
        return {}
    logger.info("Budget estimate written | amount_cny=%s", amount)
    return {"trip_workspace_v2": _with_estimate(workspace, amount)}
