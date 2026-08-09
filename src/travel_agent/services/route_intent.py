"""决定这句话要走哪条路 —— 由模型判断，不由关键词表判断。

四条路的定义在 `entities/trip_input.py::RouteName`，这里负责的是**判断**：读一句自然语言，
说出它属于哪一条，以及这个判断是不是真的立不住。

判断只有两种结果：
  1. 判出了一条路 —— 直接走，不问用户。
  2. 两种读法确实各占一半 —— 才把 `requires_confirmation` 打开，让界面问一句。

第二种是「模型自己说它并列」，不是「模型什么都没看出来」。没看出来的那种情形在这里
**不存在**：判不出来就是判不出来，抛 `RouteIntentUnavailable`，由调用方诚实报错，
不许拿一条兜底路线冒充结论、也不许把空判断包装成一张「你希望我先做哪件事」的卡。

「有没有在跑的行程」是判断的前提之一，因为 `trip_refinement` 只在有行程时才成立。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..entities.trip_input import RouteAlternative, RouteDecision, RouteName
from ..models.router import BaseLLM, get_model_router
from ..utils.json_helpers import safe_parse_json

logger = logging.getLogger(__name__)

EXPLICIT_ROUTE_SIGNAL = "explicit_ui_action"

_MAX_ATTEMPTS = 2
_MAX_SIGNALS = 4
_MAX_TEXT_CHARS = 2000

_SYSTEM_PROMPT = """你是旅行助手的意图分流器。读用户这一句话，判断它该走哪条处理路径，只输出 JSON。

四条路径：
- trip_planning：用户想要一趟具体行程。目的地已经明确（哪怕只说了一个地名），需要的是安排。
  例：「我要去大梅沙」「下周想去大理」「帮我规划上海出发的成都四日游」「带我去看看北京」。
- destination_discovery：用户还不知道去哪，需要先比较、推荐目的地。
  例：「还没想好去哪」「推荐几个适合带孩子的地方」「日本和泰国哪个更适合十月」。
- fast_answer：用户问的是一条可以直接回答的事实或知识，不需要排行程。
  例：「日本签证需要哪些材料」「人民币换日元现在多少」「故宫周一开门吗」。
- trip_refinement：用户要改**已经存在**的那趟行程。只有当上下文说明当前有行程时才允许选它。
  例：「把第二天的博物馆换掉」「行程再加一天」。

判断口径：
- 只说一个地名 + 想去的意思（「我要去X」「想去X玩」），是 trip_planning，不是 fast_answer。
  提到地名不等于在问关于这个地方的问题。
- 带问号但问的是「怎么安排、帮我排」，仍然是规划；带问号且问的是一条事实，才是 fast_answer。
- 说了一个国家或大区域而没有具体城市、并且在挑选，是 destination_discovery。

`ambiguous` 只在**两种读法确实同等成立**时才为 true —— 也就是同一句话在两条路径下都完整
合理，且没有任何一边更像。这种情况很少。看不太准但有偏向时，选那个偏向并把 confidence 降低,
不要打 ambiguous。绝不能因为「这句话很短」或「信息不足」就打 ambiguous：信息不足是规划路径
自己会去追问的事，不是分流的问题。

输出（严格 JSON，不要多余文字）：
{
  "route": "trip_planning | destination_discovery | fast_answer | trip_refinement",
  "confidence": 0.0 到 1.0,
  "signals": ["判断依据，2-4 个短词，中文"],
  "ambiguous": true 或 false,
  "alternative_route": "并列的那条路径名；ambiguous 为 false 时填 null"
}"""


class RouteIntentUnavailable(RuntimeError):
    """意图判不出来。调用方必须诚实报错，不许拿默认路线顶上。"""


def explicit_route_decision(route: RouteName) -> RouteDecision:
    """界面已经明确点了某条路（确认卡、行程内追加消息）—— 不再判一遍。"""
    return RouteDecision(
        route=route,
        confidence=1.0,
        alternatives=[],
        signals=[EXPLICIT_ROUTE_SIGNAL],
        requires_trip_draft=route == RouteName.TRIP_PLANNING,
        requires_confirmation=False,
    )


def _context_line(has_trip_run: bool) -> str:
    return (
        "上下文：当前已有一趟正在进行的行程，trip_refinement 可选。"
        if has_trip_run
        else "上下文：当前没有任何行程，trip_refinement 不可选。"
    )


def _coerce_route(value: Any, *, has_trip_run: bool) -> Optional[RouteName]:
    if not isinstance(value, str):
        return None
    try:
        route = RouteName(value.strip())
    except ValueError:
        return None
    if route == RouteName.TRIP_REFINEMENT and not has_trip_run:
        return None
    return route


def _coerce_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.5
    return round(min(1.0, max(0.0, float(value))), 4)


def _coerce_signals(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return list(dict.fromkeys(cleaned))[:_MAX_SIGNALS]


def _decision_from_payload(
    payload: Dict[str, Any],
    *,
    has_trip_run: bool,
) -> Optional[RouteDecision]:
    route = _coerce_route(payload.get("route"), has_trip_run=has_trip_run)
    if route is None:
        return None

    confidence = _coerce_confidence(payload.get("confidence"))
    signals = _coerce_signals(payload.get("signals"))
    # 并列只有在模型**同时**给出 ambiguous 与一条不同的合法备选时才成立。少了任何一半，
    # 那就不是一次并列判断，而是一次半成品输出 —— 按「判出了一条路」处理。
    alternative = (
        _coerce_route(payload.get("alternative_route"), has_trip_run=has_trip_run)
        if payload.get("ambiguous") is True
        else None
    )
    tied = alternative is not None and alternative != route

    return RouteDecision(
        route=route,
        confidence=confidence,
        alternatives=[RouteAlternative(route=alternative, confidence=confidence)] if tied else [],
        signals=signals,
        requires_trip_draft=route == RouteName.TRIP_PLANNING,
        requires_confirmation=tied,
    )


async def classify_route(
    text: str,
    *,
    explicit_route: Optional[RouteName] = None,
    has_trip_run: bool = False,
    llm: Optional[BaseLLM] = None,
) -> RouteDecision:
    """判断这句话走哪条路。判不出来抛 `RouteIntentUnavailable`。"""
    if explicit_route is not None:
        return explicit_route_decision(explicit_route)

    normalized = text.strip()
    if not normalized:
        raise RouteIntentUnavailable("空消息没有意图可判")

    model = llm or get_model_router().get_fast()
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{_context_line(has_trip_run)}\n\n用户这一句：{normalized[:_MAX_TEXT_CHARS]}",
        },
    ]

    last_error: Optional[str] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            raw = await model.ainvoke(messages, response_format={"type": "json_object"})
        except Exception as exc:  # noqa: BLE001 —— 下一句就抛出去，不吞
            last_error = f"call_failed: {exc}"
            logger.warning("Route intent call failed | attempt=%s error=%s", attempt, exc)
            continue

        payload = safe_parse_json(raw)
        if not isinstance(payload, dict):
            last_error = f"unparseable: {raw[:200]}"
            logger.warning("Route intent output unparseable | attempt=%s raw=%.200s", attempt, raw)
            continue

        decision = _decision_from_payload(payload, has_trip_run=has_trip_run)
        if decision is None:
            last_error = f"unusable: {payload}"
            logger.warning("Route intent output unusable | attempt=%s payload=%s", attempt, payload)
            continue

        logger.info(
            "Route intent decided | route=%s confidence=%s tied=%s signals=%s",
            decision.route.value,
            decision.confidence,
            decision.requires_confirmation,
            decision.signals,
        )
        return decision

    raise RouteIntentUnavailable(last_error or "意图分流没有产出可用结果")
