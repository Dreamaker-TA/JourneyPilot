"""工具轮次的那一行摘要：读得懂，而且永远不是一坨载荷。

`result_summary` 是 ToolExecutionEnvelope 里**给人读的**那一行 —— 它落在思维链上、
落在 evidence 的 title/snippet 上。它认不出形状时曾经 `json.dumps` 整个 provider 返回，
于是屏幕上出现过两条真实事故：

- `{"success": true, "provider": "nominatim", "resolution_method": …}`（地点查询零结果）
- `{"freshness_hint": {…}, "observed_at": …}`（路线查询命中快照缓存）

两条都不是失败：provider 答了，只是这一份摘要器认不出它答的形状。所以这里钉三件事 ——
认得出的形状读成人话、零结果与失败分开说、认不出时也绝不把载荷印出去。
"""

from __future__ import annotations

import pytest

from travel_agent.agents.utils import _summarize_tool_result
from travel_agent.tools.governance import (
    TOOL_FAILURE_SUMMARY,
    build_tool_execution_envelope,
    looks_like_machine_payload,
    summarize_tool_result,
)


def _place_search_result(results: list[dict]) -> dict:
    """`global_place_search` 真实返回的形状（`services/nominatim_place_search.py`）。"""

    return {
        "success": True,
        "provider": "nominatim",
        "resolution_method": "nominatim_text_search",
        "query": "深圳湾滨海休闲带",
        "requested_country_code": "cn",
        "destination_place_id": "osm:relation:3464395",
        "results": results,
        "observed_at": "2026-08-19T10:26:29+00:00",
        "retrieved_at": "2026-08-19T10:26:29+00:00",
    }


def _route_result(route: dict) -> dict:
    """`global_route_search` 真实返回的形状（快照缓存回填后 freshness_hint 排在最前）。"""

    return {
        "freshness_hint": {
            "published_at": "",
            "retrieved_at": "2026-08-19T10:26:29.833858+00:00",
            "source_type": "tool",
            "tool_name": "global_route_search",
        },
        "observed_at": "2026-08-19T10:26:29.833858+00:00",
        "success": True,
        "provider": "amap",
        "request": {"from_name": "深圳北站", "to_name": "深圳湾公园", "mode": "public_transit"},
        "routes": [route],
    }


_NORMALIZED_ROUTE = {
    "route_id": "amap:0f3a",
    "provider": "amap",
    "transport_class": "public_transit",
    "selected_mode": "metro",
    "from_endpoint": {"name": "深圳北站", "place_id": "amap:stop:1"},
    "to_endpoint": {"name": "深圳湾公园", "place_id": "amap:stop:2"},
    "duration_minutes": 42,
    "distance_meters": 21000,
}


@pytest.mark.parametrize(
    "result",
    [
        _place_search_result([]),
        _route_result(_NORMALIZED_ROUTE),
        _place_search_result([{"name": "深圳湾公园"}]),
        {"success": True, "provider": "x", "payload": {"nested": {"deep": [1, 2, 3]}}},
        {"routes": []},
        {"web": []},
        # MCP 那批工具把 JSON 装在 text content 里返回：压到这一步它还是个字符串。
        [{"text": '{"success": true, "unrecognized_shape": [1, 2]}'}],
        '{"success": true, "provider": "nominatim"}',
    ],
    ids=[
        "place-search-empty",
        "route-from-snapshot-cache",
        "place-search-hit",
        "unrecognized-dict",
        "empty-routes",
        "empty-web",
        "mcp-text-content",
        "raw-json-string",
    ],
)
def test_a_summary_is_never_a_payload(result):
    """认不认得出形状，摘要都不许以 `{` / `[` 开头。"""

    summary = summarize_tool_result(result)
    assert summary
    assert not looks_like_machine_payload(summary), summary


def test_a_recognized_shape_reads_as_a_sentence():
    assert summarize_tool_result(_place_search_result([{"name": "深圳湾公园"}, {"name": "红树林"}])) == (
        "找到 2 个结果：深圳湾公园、红树林"
    )
    assert summarize_tool_result(_route_result(_NORMALIZED_ROUTE)) == (
        "地铁 · 深圳北站 → 深圳湾公园 · 约 42 分钟"
    )


def test_zero_results_is_not_reported_as_a_failure():
    """查了但没找到 ≠ 这一步坏了。两句话必须不同，否则运维和用户都会去查一个不存在的故障。"""

    empty = summarize_tool_result(_place_search_result([]))
    assert empty != TOOL_FAILURE_SUMMARY
    assert empty == "没有匹配的结果"
    assert summarize_tool_result({"success": False, "error": "boom"}) == TOOL_FAILURE_SUMMARY


def test_the_envelope_carries_the_same_line():
    """信封上的 `result_summary` 就是屏幕上那一行 —— 它也不许是载荷。"""

    envelope = build_tool_execution_envelope(
        tool_name="global_route_search",
        arguments={"from_name": "深圳北站", "to_name": "深圳湾公园"},
        result=_route_result(_NORMALIZED_ROUTE),
        category="data",
    )
    assert envelope["result_summary"] == "地铁 · 深圳北站 → 深圳湾公园 · 约 42 分钟"
    # 载荷本身仍然在信封里（模型与 packet 解析读它），只是不走产品面那一行。
    assert envelope["sanitized_result"]["routes"][0]["route_id"] == "amap:0f3a"


def test_a_failed_round_never_formats_the_upstream_exception():
    """上游异常文本是英文实现细节，产品面只说结论。"""

    envelope = build_tool_execution_envelope(
        tool_name="global_route_search",
        arguments={},
        error="global route provider found no executable route",
        status="failed",
        category="data",
    )
    assert envelope["result_summary"] == TOOL_FAILURE_SUMMARY
    assert envelope["error"] == "global route provider found no executable route"


def test_the_non_envelope_path_uses_the_same_summarizer():
    """信封之外的那条路（工具自己的语义摘要器）也不许把载荷印出去。

    这条路上曾经另写着一份通用压缩，`str(result)[:120]`；现在它委派给 governance 那一份，
    而 `free_web_search` 的逐行预览也要挡一次——它的 content 有时整个就是一行 JSON。
    """

    payload_line = '{"web": [{"title": "x", "url": "https://example.com"}]}'
    summary = _summarize_tool_result("free_web_search", {"success": True, "content": payload_line})
    assert not looks_like_machine_payload(summary)
    assert payload_line not in summary

    unknown = _summarize_tool_result("some_unregistered_tool", {"success": True, "payload": {"a": [1, 2]}})
    assert unknown == "已取到结果"
