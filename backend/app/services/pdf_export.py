"""
PDF export for a single interviewer's scorecard (Interview Assessment Form
and the legacy default form both render through the same path, driven
entirely by the form's schema -- no per-form-name branching).

Mirrors excel_export.py's io.BytesIO + StreamingResponse convention.
"""
import io

from fastapi.responses import StreamingResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

_NAVY = colors.HexColor("#001451")
_MUTED = colors.HexColor("#5a6178")
_BORDER = colors.HexColor("#e5e8f2")

_styles = getSampleStyleSheet()
_h1 = ParagraphStyle("sc_h1", parent=_styles["Heading1"], textColor=_NAVY, fontSize=16, spaceAfter=2)
_meta_label = ParagraphStyle("sc_meta_label", parent=_styles["Normal"], textColor=_MUTED, fontSize=7, leading=9)
_meta_value = ParagraphStyle("sc_meta_value", parent=_styles["Normal"], textColor=colors.black, fontSize=10, leading=12)
_section_hd = ParagraphStyle("sc_section_hd", parent=_styles["Normal"], textColor=_MUTED, fontSize=9,
                              spaceBefore=12, spaceAfter=4, leading=11)
_field_label = ParagraphStyle("sc_field_label", parent=_styles["Normal"], fontSize=9, textColor=_MUTED, leading=11)
_field_value = ParagraphStyle("sc_field_value", parent=_styles["Normal"], fontSize=10, leading=13, spaceAfter=6)
_footer = ParagraphStyle("sc_footer", parent=_styles["Normal"], fontSize=8, textColor=_MUTED, leading=10)


def _meta_cell(label: str, value: str):
    return [Paragraph(label.upper(), _meta_label), Paragraph(value or "—", _meta_value)]


def _rating_value(val) -> str:
    labels = {1: "Poor", 2: "Below avg", 3: "Average", 4: "Good", 5: "Excellent"}
    try:
        n = int(val)
        return f"{n}/5 — {labels.get(n, '')}"
    except (TypeError, ValueError):
        return "—"


def render_scorecard_pdf(
    *,
    organization: str,
    candidate_name: str,
    requisition: str,
    department: str | None,
    round_name: str,
    scheduled_at_str: str | None,
    interviewer_name: str,
    schema: list,
    form_data: dict,
    overall_score,
    submitted_by_name: str | None,
    submitted_at_str: str | None,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"Interview Assessment — {candidate_name}",
    )
    story = []

    story.append(Paragraph(organization or "", ParagraphStyle("org", parent=_styles["Normal"], fontSize=9, textColor=_MUTED)))
    story.append(Paragraph("Interview Assessment Form", _h1))
    story.append(Spacer(1, 8))

    meta_rows = [
        [*_meta_cell("Candidate", candidate_name), *_meta_cell("Position", requisition)],
        [*_meta_cell("Round", round_name), *_meta_cell("Date", scheduled_at_str)],
        [*_meta_cell("Department", department), *_meta_cell("Interviewer", interviewer_name)],
    ]
    meta_table = Table(meta_rows, colWidths=[30 * mm, 55 * mm, 30 * mm, 55 * mm])
    meta_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, _BORDER),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 10))

    last_section = object()  # sentinel so a None-section first field still renders sectionless
    for f in schema:
        if f.get("parent"):
            continue  # rating notes render inline in the legacy form; skip standalone

        section = f.get("section")
        if section != last_section:
            last_section = section
            if section:
                story.append(Paragraph(section, _section_hd))

        label = f.get("label", f["key"])
        ftype = f.get("type")
        if ftype == "rating_5":
            value = _rating_value(form_data.get(f["key"]))
        elif ftype == "single_choice":
            value = form_data.get(f["key"]) or "—"
        else:  # text / textarea
            value = form_data.get(f["key"]) or "—"

        story.append(Paragraph(label, _field_label))
        story.append(Paragraph(str(value).replace("\n", "<br/>"), _field_value))

    if overall_score is not None:
        story.append(Spacer(1, 6))
        story.append(Paragraph("OVERALL SCORE", _field_label))
        story.append(Paragraph(f"{overall_score:.1f} / 5", ParagraphStyle(
            "sc_overall", parent=_field_value, fontSize=13, textColor=_NAVY,
        )))

    story.append(Spacer(1, 18))
    sig_text = (
        f"Submitted by {submitted_by_name} on {submitted_at_str}"
        if submitted_by_name else
        "Not yet submitted — this is a draft snapshot."
    )
    story.append(Paragraph(sig_text, _footer))

    doc.build(story)
    buf.seek(0)
    return buf.getvalue()


def stream_pdf(pdf_bytes: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
