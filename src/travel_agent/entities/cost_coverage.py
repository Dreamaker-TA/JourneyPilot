"""The single sentence that says how much of the trip the money figure covers.

The same judgement was written three times — the workspace overview, the formal
report header, and the exported PDF — and the three had already drifted: the
workspace omitted the "still N items unpriced" clause the other two printed, so a
traveller comparing the card with the PDF saw the same trip described as more
certain in one place than the other.

The statement is produced once, during projection, and stored on
``TripReportDocument.cost_coverage_statement``.  Every surface prints that field.
Amount formatting lives here too: the PDF used ``¥{value:g}`` while the browser
used ``Intl.NumberFormat``, so the same number appeared as ``¥5600`` and
``¥5,600`` on two renderings of one plan.
"""

from __future__ import annotations

from typing import Optional

from .delivery_bundle import CostCoverageSummary


def format_cny(value: float) -> str:
    """Format an amount the one way every delivery surface prints it."""

    return f"¥{value:,.0f}"


def cost_coverage_statement(summary: CostCoverageSummary) -> Optional[str]:
    """State the money the plan actually knows, or say nothing at all.

    **Do not append 「仍有 N 项待确认」, and do not fall back to the bare words
    「费用待确认」.**  Neither tells a traveller anything they can act on: the count is an
    artefact of which suppliers happened to answer this round, and a line whose whole
    content is "unknown" is worse than no line — it reads as a defect in the plan rather
    than the ordinary fact that a museum does not publish a ticket API.

    So the rule is: print a number where one exists, print nothing where none does.  ``None`` means *render no line* and every surface honours it — the
    report header, the PDF summary row and the workspace overview all drop the
    segment rather than printing an empty one.  A per-item price follows the
    same rule one level down (``delivery_presentation._price``).
    """

    parts: list[str] = []
    if summary.coverage == "complete" and summary.estimated_total_cny is not None:
        parts.append(f"预计合计 {format_cny(summary.estimated_total_cny)}")
    elif summary.coverage == "partial" and summary.known_subtotal_cny is not None:
        parts.append(f"已知费用 {format_cny(summary.known_subtotal_cny)}")
    # The estimate is a separate clause, never folded into the number beside it:
    # one is what suppliers quoted, the other is what the rest of the trip
    # probably costs, and a reader has to be able to tell which is which.
    if summary.llm_estimated_total_cny is not None:
        parts.append(f"整趟预算估算 {format_cny(summary.llm_estimated_total_cny)}")
    return " · ".join(parts) or None
