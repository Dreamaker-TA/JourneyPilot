"""Server-side PDF export from one immutable Delivery Bundle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from io import BytesIO
import os
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from pydantic import Field
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..entities.delivery_bundle import (
    DeliveryBundle,
    EntityType,
    PublicCitationProjection,
    StrictModel,
)
from ..entities.coverage_disclosure import coverage_disclosure_notes
from ..entities.delivery_presentation import weather_freshness_text
from ..entities.provider_environment import ProviderEnvironmentView
from ..entities.evidence_basis import (
    PUBLIC_REFERENCE,
    PUBLIC_REFERENCE_LABEL,
    REFERENCE_SERVICE,
    REFERENCE_SERVICE_LABEL,
    EvidenceBasisView,
)


class ReportOutOfDateError(RuntimeError):
    pass


class PdfFontUnavailable(RuntimeError):
    pass


class WeatherCoverageSummary(StrictModel):
    complete_destinations: int = Field(ge=0)
    partial_destinations: int = Field(ge=0)
    unavailable_destinations: int = Field(ge=0)


class TripReportBuildContext(StrictModel):
    source_workspace_revision: int = Field(ge=0)
    source_fact_data_revision: int = Field(ge=0)
    source_weather_data_revision: int = Field(ge=0)
    weather_coverage_summary: WeatherCoverageSummary
    active_weather_proposal_ids: list[str] = Field(default_factory=list)


class PdfExportArtifact(StrictModel):
    bundle_id: str
    exported_at: datetime
    filename: str
    content: bytes
    build_context: TripReportBuildContext


def build_report_context(bundle: DeliveryBundle) -> TripReportBuildContext:
    report = bundle.report_projection
    manifest = bundle.manifest
    revisions = (
        manifest.workspace_revision,
        manifest.fact_data_revision,
        manifest.weather_data_revision,
    )
    report_revisions = (
        report.source_workspace_revision,
        report.source_fact_data_revision,
        report.source_weather_data_revision,
    )
    if report.status != "ready" or report.document is None or report_revisions != revisions:
        raise ReportOutOfDateError("report projection is not current")

    coverage_statuses = [item.status for item in bundle.weather_snapshot.coverage]
    decisions = {
        item.proposal_id: item.decision for item in bundle.workspace.weather_proposal_decisions
    }
    active_high_proposals = sorted(
        item.proposal_id
        for item in bundle.weather_snapshot.adjustment_proposals
        if item.severity == "high" and item.proposal_id not in decisions
    )
    return TripReportBuildContext(
        source_workspace_revision=manifest.workspace_revision,
        source_fact_data_revision=manifest.fact_data_revision,
        source_weather_data_revision=manifest.weather_data_revision,
        weather_coverage_summary=WeatherCoverageSummary(
            complete_destinations=coverage_statuses.count("complete"),
            partial_destinations=coverage_statuses.count("partial"),
            unavailable_destinations=coverage_statuses.count("unavailable"),
        ),
        active_weather_proposal_ids=active_high_proposals,
    )


# The export prints the lines the projection rendered; it formats **none** of them
# itself.  Giving it its own label table, mode words, timestamp/money formatting or
# segment renderer means the strings a traveller reads about one entry are authored
# twice, and they drift; such a table also rots silently — labels naming fields no
# itinerary entity carries just print nothing, indefinitely, with nothing to notice it.


def _rendered_text(details: dict[str, Any], key: str) -> str:
    value = details.get(key)
    return value if isinstance(value, str) and value else ""


def _rendered_lines(details: dict[str, Any], key: str) -> list[str]:
    value = details.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _citation_marks(ids: Iterable[str], citation_numbers: dict[str, int]) -> str:
    numbers = sorted({citation_numbers[item] for item in ids if item in citation_numbers})
    return "".join(f"<super>[{number}]</super>" for number in numbers)


# The two bases an entry has to state for itself, with the copy owned by
# ``entities/evidence_basis.py`` so the PDF and the browser chip cannot drift.
_STATED_BASIS_LABELS = {
    PUBLIC_REFERENCE: PUBLIC_REFERENCE_LABEL,
    REFERENCE_SERVICE: REFERENCE_SERVICE_LABEL,
}


def _evidence_basis_mark(block: Any, evidence_basis: EvidenceBasisView) -> str:
    """Name the basis of an entry that has no citation marks to speak for it.

    A ``cited_source`` block already carries its numbered marks, so restating
    its basis would only add noise.  A ``public_reference`` block has nothing —
    and an unexplained absence of sources reads as an oversight rather than the
    stated basis it is.  A ``reference_service`` block is the third: a real
    service the supplier could not confirm for this date, which on paper is the
    one that most needs saying.  Kept in the same muted register as the detail
    lines: a small grey clause, not a warning and not a coloured flag.
    """

    basis = evidence_basis.stated_basis_for(
        EntityType(block.entity_ref.entity_type), block.entity_ref.entity_id
    )
    label = _STATED_BASIS_LABELS.get(basis or "")
    if label is None:
        return ""
    return f' <font color="#7A838D" size="8">· {escape(label)}</font>'


_FONT_NAME = "JourneyPilotCJK"
_FONT_LOCK = Lock()
_FONT_CANDIDATES = (
    # TrueType-outline CJK fonts — embeddable by reportlab's TTFont parser.
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    # Noto CJK — on many Linux distros these ship CFF (PostScript) outlines,
    # which reportlab cannot embed; kept as lower-priority fallbacks for
    # platforms that package TrueType-outline builds.
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    # macOS.
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)


def _register_pdf_font() -> str:
    with _FONT_LOCK:
        if _FONT_NAME in pdfmetrics.getRegisteredFontNames():
            return _FONT_NAME
        configured = os.getenv("JOURNEYPILOT_PDF_FONT_PATH")
        candidates = ([configured] if configured else []) + list(_FONT_CANDIDATES)
        last_error: Exception | None = None
        for item in candidates:
            if not item or not Path(item).is_file():
                continue
            try:
                pdfmetrics.registerFont(TTFont(_FONT_NAME, str(item), subfontIndex=0))
                return _FONT_NAME
            except Exception as exc:  # e.g. TTFError on CFF/OpenType-outline fonts
                last_error = exc
                continue
        detail = f"; last error: {last_error}" if last_error else ""
        raise PdfFontUnavailable(
            "no embeddable CJK PDF font is installed; set JOURNEYPILOT_PDF_FONT_PATH"
            + detail
        )


def probe_pdf_renderer() -> str:
    """Verify the server can render CJK PDF text without creating an artifact.

    The probe renders one tiny in-memory page rather than exporting a report:
    a formal report needs a Delivery Bundle, while this check must never create
    or read a product artifact.
    """

    font = _register_pdf_font()
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=A4)
    canvas.setFont(font, 12)
    canvas.drawString(24 * mm, 270 * mm, "JourneyPilot PDF 预检")
    canvas.save()
    if not buffer.getvalue().startswith(b"%PDF"):
        raise RuntimeError("PDF renderer did not produce a PDF document")
    return font


@dataclass(frozen=True)
class PdfRendererProbe:
    """开机那一次渲染预检的结论。运行期结构不再变化，所以不复检。"""

    available: bool
    font: str | None = None
    problem: str | None = None


_last_probe: PdfRendererProbe | None = None


def record_pdf_probe(probe: PdfRendererProbe) -> None:
    global _last_probe
    _last_probe = probe


def last_pdf_probe() -> PdfRendererProbe | None:
    """readiness 读它。``None`` = 启动时没跑过预检。"""

    return _last_probe


def _styles():
    base = getSampleStyleSheet()
    font = _register_pdf_font()
    return {
        "title": ParagraphStyle(
            "JPTitle", parent=base["Title"], fontName=font, fontSize=25,
            leading=32, textColor=colors.HexColor("#18202A"), alignment=TA_LEFT,
            spaceAfter=10,
        ),
        "eyebrow": ParagraphStyle(
            "JPEyebrow", parent=base["Normal"], fontName=font, fontSize=9,
            leading=12, textColor=colors.HexColor("#2477E8"), spaceAfter=6,
        ),
        "h1": ParagraphStyle(
            "JPH1", parent=base["Heading1"], fontName=font, fontSize=16,
            leading=22, textColor=colors.HexColor("#18202A"), spaceBefore=13, spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "JPH2", parent=base["Heading2"], fontName=font, fontSize=12,
            leading=17, textColor=colors.HexColor("#18202A"), spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "JPBody", parent=base["BodyText"], fontName=font, fontSize=9.5,
            leading=15, textColor=colors.HexColor("#3E4650"), spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "JPSmall", parent=base["BodyText"], fontName=font, fontSize=8,
            leading=12, textColor=colors.HexColor("#626B75"), spaceAfter=3,
        ),
        "timeline_time": ParagraphStyle(
            "JPTimelineTime", parent=base["BodyText"], fontName=font, fontSize=8.5,
            leading=13, textColor=colors.HexColor("#626B75"), alignment=TA_LEFT,
        ),
        "timeline_title": ParagraphStyle(
            "JPTimelineTitle", parent=base["BodyText"], fontName=font, fontSize=10.5,
            leading=15, textColor=colors.HexColor("#18202A"), spaceAfter=2,
        ),
        "center": ParagraphStyle(
            "JPCenter", parent=base["BodyText"], fontName=font, fontSize=8,
            leading=11, textColor=colors.HexColor("#7A838D"), alignment=TA_CENTER,
        ),
    }


def _source_story(
    citations: list[PublicCitationProjection],
    citation_numbers: dict[str, int],
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    # Let the appendix follow the preceding report content naturally. A forced
    # page break after a compact weather section can otherwise leave an almost
    # empty page in a short itinerary.
    story: list[Any] = [Spacer(1, 4 * mm), Paragraph("来源", styles["h1"])]
    entity_labels = {
        "visit_stop": "景点与体验",
        "dining_stop": "餐饮安排",
        "lodging_stay": "住宿安排",
        "transport_leg": "交通安排",
        "custom_block": "自定义安排",
        "weather_day": "当日天气",
    }
    source_entries: dict[str, dict[str, Any]] = {}
    for citation in citations:
        number = citation_numbers[citation.citation_id]
        entity_label = entity_labels.get(citation.entity_ref.entity_type.value, "行程事实")
        for source in citation.sources:
            entry = source_entries.setdefault(
                source.source_record_id,
                {"source": source, "numbers": [], "entity_labels": []},
            )
            if number not in entry["numbers"]:
                entry["numbers"].append(number)
            if entity_label not in entry["entity_labels"]:
                entry["entity_labels"].append(entity_label)

    for entry in source_entries.values():
        source = entry["source"]
        number_marks = ", ".join(str(number) for number in entry["numbers"])
        entity_summary = "、".join(entry["entity_labels"])
        story.append(Paragraph(f"[{number_marks}] {entity_summary}", styles["h2"]))
        title = escape(source.title)
        if source.canonical_url:
            title = f'<link href="{escape(source.canonical_url)}" color="#2477E8">{title}</link>'
        story.append(Paragraph(title, styles["body"]))
        story.append(Paragraph(escape(source.public_excerpt), styles["small"]))
        story.append(Paragraph(f"取得时间：{source.retrieved_at.isoformat()}", styles["small"]))
        if source.rag_chunk_content:
            story.append(Paragraph(escape(source.rag_chunk_content), styles["small"]))
        story.append(Spacer(1, 3 * mm))
    return story


def _day_weather_label(
    day: Any, citation_numbers: dict[str, int]
) -> tuple[str, str]:
    if day is None:
        return "", ""
    label = day.condition_label or (
        "季节参考" if day.data_kind == "seasonal_baseline" else ""
    )
    values = [label]
    if day.low_c is not None and day.high_c is not None:
        values.append(f"{day.high_c:.0f}° / {day.low_c:.0f}°")
    # On paper a badge cannot be opened, so it prints the line the badge would have
    # revealed.  Same two states, same words, from the shared authority.
    freshness = weather_freshness_text(
        weather_data_state=day.weather_data_state, observed_at=day.observed_at
    )
    if freshness:
        values.append(freshness)
    return (
        " · ".join(item for item in values if item),
        _citation_marks(day.citation_ids, citation_numbers),
    )


def _timeline_row(
    block: Any,
    citation_numbers: dict[str, int],
    styles: dict[str, ParagraphStyle],
    evidence_basis: EvidenceBasisView,
) -> Table:
    details = block.details
    marks = _citation_marks(block.citation_ids, citation_numbers)
    marks += _evidence_basis_mark(block, evidence_basis)
    body = f"<b>{escape(block.title)}</b>{marks}"
    # Duration and price lead the entry here for the same reason they lead the
    # browser's timeline node: they are the two numbers a reader scans for.
    header = [
        _rendered_text(details, "duration_label"),
        _rendered_text(details, "price_label"),
    ]
    header_text = " · ".join(item for item in header if item)
    if header_text:
        body += f'<br/><font color="#626B75">{escape(header_text)}</font>'
    if block.summary.strip() != block.title.strip():
        body += f"<br/>{escape(block.summary)}"
    detail_lines = [
        *_rendered_lines(details, "facts"),
        *_rendered_lines(details, "notes"),
        *_rendered_lines(details, "segment_lines"),
    ]
    if detail_lines:
        body += "<br/><font color=\"#626B75\">" + "<br/>".join(
            escape(item) for item in detail_lines
        ) + "</font>"
    row = Table(
        [[
            Paragraph(escape(_rendered_text(details, "time_label")), styles["timeline_time"]),
            Paragraph(body, styles["timeline_title"]),
        ]],
        colWidths=[29 * mm, A4[0] - 73 * mm],
        hAlign="LEFT",
    )
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor("#D9E0E7")),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return row


def render_trip_report_pdf(
    bundle: DeliveryBundle,
    *,
    exported_at: datetime,
) -> PdfExportArtifact:
    context = build_report_context(bundle)
    document = bundle.report_projection.document
    assert document is not None
    citations = bundle.report_projection.citations
    citation_numbers = {
        citation.citation_id: index
        for index, citation in enumerate(citations, start=1)
    }
    # The same derivation the JSON boundary publishes, read from the shared
    # authority so this artifact cannot state a different basis for an entity
    # than the workspace card does.
    evidence_basis = EvidenceBasisView.from_itinerary(bundle.workspace.itinerary)
    styles = _styles()
    buffer = BytesIO()

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(_FONT_NAME, 8)
        canvas.setFillColor(colors.HexColor("#7A838D"))
        canvas.drawString(20 * mm, 11 * mm, "JourneyPilot")
        canvas.drawRightString(A4[0] - 20 * mm, 11 * mm, f"第 {doc.page} 页")
        canvas.restoreState()

    pdf = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=document.title,
        author="JourneyPilot",
        subject=f"Bundle {bundle.manifest.bundle_id}",
    )
    story: list[Any] = [
        Paragraph("JOURNEYPILOT · 完整旅行方案", styles["eyebrow"]),
        Paragraph(escape(document.title), styles["title"]),
        Paragraph(escape(document.overview), styles["body"]),
    ]
    # One sentence, computed once (``entities/cost_coverage.py``).  This surface,
    # the report header and the workspace overview each wrote their own from
    # ``cost_summary`` and had already drifted, including in how they formatted the
    # amount — the same number printed ``¥5600`` here and ``¥5,600`` in the browser.
    # A ``None`` statement means the plan has no price to state, and the segment
    # is dropped rather than joined as an empty one — otherwise the row reads
    # 「3 天 ·  · 导出于 …」.
    summary = [
        item
        for item in (
            f"{document.duration_days} 天",
            document.cost_coverage_statement,
            f"导出于 {exported_at.astimezone().strftime('%Y-%m-%d %H:%M')}",
        )
        if item
    ]
    story.extend([Paragraph(" · ".join(summary), styles["small"]), Spacer(1, 5 * mm)])

    if document.highlights:
        # 「全程亮点」, not 「全程核心路线」: the chapter was written for a field that
        # had no producer, and what materialization now derives into it covers the
        # cross-city services, the places, the food and the beds — not a route.
        story.append(Paragraph("全程亮点", styles["h1"]))
        for highlight in document.highlights:
            story.append(Paragraph(escape(highlight), styles["body"]))
        story.append(Spacer(1, 2 * mm))

    for day in document.days:
        heading = f"第 {day.day} 天"
        weather = next(
            (
                item for item in document.weather
                if item.destination_id == day.destination_id
                and item.date == day.date
                and item.data_kind != "unavailable"
            ),
            None,
        )
        weather_label, weather_marks = _day_weather_label(weather, citation_numbers)
        meta = " · ".join(
            item
            for item in [
                day.date.isoformat() if day.date else "",
                day.destination_name,
                weather_label,
                day.theme,
            ]
            if item
        )
        heading_story = Paragraph(escape(f"{heading}  {meta}") + weather_marks, styles["h1"])
        if not day.blocks:
            story.append(heading_story)
            continue
        first_row = _timeline_row(day.blocks[0], citation_numbers, styles, evidence_basis)
        story.append(KeepTogether([heading_story, first_row, Spacer(1, 2 * mm)]))
        for block in day.blocks[1:]:
            story.extend([
                _timeline_row(block, citation_numbers, styles, evidence_basis),
                Spacer(1, 2 * mm),
            ])

    alternative_selections = [
        (selection, [option for option in selection.options if not option.selected])
        for selection in document.selections
    ]
    alternative_selections = [
        (selection, options) for selection, options in alternative_selections if options
    ]
    if alternative_selections:
        story.append(Paragraph("其它合格选择", styles["h1"]))
        story.append(Paragraph("主行程只使用当前选择；以下选项来自同一次比较结果。", styles["small"]))
        for selection, options in alternative_selections:
            rows = []
            for option in options:
                marks = _citation_marks(option.citation_ids, citation_numbers)
                details = "；".join([*option.selection_reasons, option.tradeoff or ""])
                rows.append([
                    Paragraph(f"<b>{escape(option.name)}</b> · 可选{marks}", styles["body"]),
                    Paragraph(escape(details), styles["small"]),
                ])
            table = Table(rows, colWidths=[58 * mm, 108 * mm], repeatRows=0)
            table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor("#E2E7EC")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.extend([table, Spacer(1, 3 * mm)])

    if document.important_notes:
        story.append(Paragraph("出发前事项", styles["h1"]))
        for note in document.important_notes:
            story.append(Paragraph(f"• {escape(note)}", styles["body"]))

    # The exported document's own bottom notes region: what this Run did not
    # manage to cover.  Deliberately separate from 出发前事项 (things the traveller
    # has to go and do) and deliberately in the small register — it states a fact
    # about the plan, it is not a warning.  The PDF reads the Bundle directly and
    # never passes through the public projection, so it takes the same sentence
    # table rather than a second wording.
    coverage_notes = coverage_disclosure_notes(bundle.coverage_disclosure)
    sandbox_note = ProviderEnvironmentView.from_bundle(bundle).sandbox_note
    if sandbox_note is not None:
        coverage_notes = [*coverage_notes, sandbox_note]
    if coverage_notes:
        story.append(Paragraph("本次规划的覆盖情况", styles["h2"]))
        for note in coverage_notes:
            story.append(Paragraph(f"• {escape(note)}", styles["small"]))

    story.extend(_source_story(citations, citation_numbers, styles))
    pdf.build(story, onFirstPage=footer, onLaterPages=footer)
    filename = f"journeypilot-{bundle.manifest.bundle_id}.pdf"
    return PdfExportArtifact(
        bundle_id=bundle.manifest.bundle_id,
        exported_at=exported_at,
        filename=filename,
        content=buffer.getvalue(),
        build_context=context,
    )
