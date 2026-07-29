"""Coach commission statements as PDF.

One statement per coach per run: the sessions that paid, what each paid and
why, and the total. This is the document a coach receives, so it is written to
be read by someone who was not in the room when the numbers were produced —
every line says which booking it came from and which rule priced it.

ReportLab rather than an HTML-to-PDF engine: it is pure Python, so the Nixpacks
build needs no cairo/pango system packages, and page breaks in a long table are
handled properly rather than by luck.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

INK = colors.HexColor("#1a232e")
MUTED = colors.HexColor("#6b7683")
LINE = colors.HexColor("#e4e8ed")
TEAL = colors.HexColor("#008080")
TEAL_TINT = colors.HexColor("#e6f2f2")
NAVY = colors.HexColor("#03224e")
LOW = colors.HexColor("#fbf4e6")

#: The peso sign is absent from the standard PDF fonts (and from the TTFs
#: ReportLab bundles), so it renders as a black box. "PHP" is unambiguous and
#: needs no embedded font, which keeps the Nixpacks build free of font packages.
PESO = "PHP "


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold",
                                fontSize=17, leading=21, textColor=INK,
                                alignment=0, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontName="Helvetica",
                              fontSize=9.5, leading=13, textColor=MUTED),
        "h2": ParagraphStyle("h", parent=base["Normal"], fontName="Helvetica-Bold",
                             fontSize=7.5, leading=10, textColor=MUTED,
                             spaceBefore=12, spaceAfter=4),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontName="Helvetica",
                               fontSize=8, leading=10.5, textColor=INK),
        "cellr": ParagraphStyle("cr", parent=base["Normal"], fontName="Helvetica",
                                fontSize=8, leading=10.5, textColor=INK,
                                alignment=TA_RIGHT),
        "note": ParagraphStyle("n", parent=base["Normal"], fontName="Helvetica",
                               fontSize=7.5, leading=10.5, textColor=MUTED),
        "label": ParagraphStyle("l", parent=base["Normal"], fontName="Helvetica",
                                fontSize=7, leading=9, textColor=MUTED),
        "figure": ParagraphStyle("f", parent=base["Normal"],
                                 fontName="Helvetica-Bold", fontSize=14,
                                 leading=18, textColor=INK, spaceBefore=1),
    }


def peso(value) -> str:
    return PESO + "{:,.2f}".format(float(value or 0))


def _rate_label(b) -> str:
    """Say why the row paid what it paid, in the words the screen uses."""
    if b.rule == "delegation":
        name = b.delegator.name if b.delegator else "delegation"
        return "Delegation cost · %s" % name
    if b.rate_type == "percent":
        return "%g%% of revenue" % (float(b.rate_value or 0) * 100)
    if b.rate_type == "flat":
        return "Fixed %s" % peso(b.rate_value)
    return "—"


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 12 * mm, doc.footer_left)
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, "Page %d" % doc.page)
    canvas.restoreState()


def statement(run, coach: str, rows, *, signoff=None, generated_by: str = "",
              company: str = "AWAKEN Fitness Center") -> bytes:
    """Render one coach's statement for one run and return the PDF bytes.

    ``rows`` are the bookings that count — the caller decides that, using the
    same `is_commissionable` rule the screens use, so the statement can never
    disagree with the preview it was generated from.
    """
    st = _styles()
    buf = io.BytesIO()
    period = run.period_label or run.period or ""
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=20 * mm,
        title="%s — commission %s" % (coach, period),
        author=company, subject="Coach commission statement")
    doc.footer_left = "%s · %s · generated %s" % (
        company, period, datetime.now(timezone.utc).strftime("%d %b %Y"))

    total = sum((Decimal(str(b.commission or 0)) for b in rows), Decimal(0))
    revenue = sum((Decimal(str(b.revenue or 0)) for b in rows), Decimal(0))
    delegated = [b for b in rows if b.delegator_id]
    dele_total = sum((Decimal(str(b.commission or 0)) for b in delegated), Decimal(0))

    flow = []
    head = Table([[
        Paragraph("Commission statement", st["title"]),
        Paragraph("<b>%s</b><br/>%s" % (company, period), st["sub"]),
    ]], colWidths=[105 * mm, 69 * mm])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 1.4, NAVY),
    ]))
    flow += [head, Spacer(1, 9)]

    flow.append(Paragraph(
        "<b>%s</b>" % coach,
        ParagraphStyle("coach", fontName="Helvetica-Bold", fontSize=13,
                       leading=16, textColor=INK)))
    flow.append(Paragraph(
        "%d session%s counted &nbsp;·&nbsp; %s revenue"
        % (len(rows), "" if len(rows) == 1 else "s", peso(revenue)), st["sub"]))
    flow.append(Spacer(1, 10))

    # Two stacked paragraphs per card rather than one with <br/>: a single
    # paragraph has to share one leading between a 7pt label and a 15pt figure,
    # which makes them collide.
    def _card(label, value, foot=""):
        cell = [Paragraph(label, st["label"]), Paragraph(value, st["figure"])]
        if foot:
            cell.append(Paragraph(foot, st["label"]))
        return cell

    card = Table([[
        _card("TOTAL COMMISSION", "<b>%s</b>" % peso(total)),
        _card("SESSIONS", "<b>%d</b>" % len(rows)),
        _card("OF WHICH DELEGATION", "<b>%s</b>" % peso(dele_total),
              "%d session%s" % (len(delegated),
                                "" if len(delegated) == 1 else "s")),
    ]], colWidths=[58 * mm, 48 * mm, 68 * mm])
    card.setStyle(TableStyle([
        ("BOX", (0, 0), (0, 0), 0.8, TEAL),
        ("BACKGROUND", (0, 0), (0, 0), TEAL_TINT),
        ("BOX", (1, 0), (1, 0), 0.8, LINE),
        ("BOX", (2, 0), (2, 0), 0.8, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
    ]))
    flow += [card, Spacer(1, 4)]

    flow.append(Paragraph("SESSIONS", st["h2"]))
    data = [[Paragraph("<b>Date</b>", st["cell"]),
             Paragraph("<b>Booking</b>", st["cell"]),
             Paragraph("<b>Client</b>", st["cell"]),
             Paragraph("<b>Session</b>", st["cell"]),
             Paragraph("<b>Revenue</b>", st["cellr"]),
             Paragraph("<b>Rate applied</b>", st["cell"]),
             Paragraph("<b>Commission</b>", st["cellr"])]]
    marks = []
    for i, b in enumerate(rows, start=1):
        label = _rate_label(b)
        if b.rate_manual:
            label += " · manual"
            marks.append(i)
        if not b.is_completed:
            label += " · %s, approved" % (b.booking_status or "").lower()
        data.append([
            Paragraph(b.appointment_date.strftime("%d %b") if b.appointment_date else "",
                      st["cell"]),
            Paragraph(b.booking_ref or "", st["cell"]),
            Paragraph(b.customer or "", st["cell"]),
            Paragraph(b.appointment_name or "", st["cell"]),
            Paragraph(peso(b.revenue), st["cellr"]),
            Paragraph(label, st["cell"]),
            Paragraph(peso(b.commission), st["cellr"]),
        ])
    data.append([Paragraph("<b>Total</b>", st["cell"]), "", "", "", "", "",
                 Paragraph("<b>%s</b>" % peso(total), st["cellr"])])

    table = Table(data, repeatRows=1,
                  colWidths=[13 * mm, 21 * mm, 30 * mm, 32 * mm, 24 * mm, 28 * mm, 26 * mm])
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK),
        ("SPAN", (0, -1), (5, -1)),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#fafbfc")),
    ]
    for i in marks:
        style.append(("BACKGROUND", (0, i), (-1, i), LOW))
    table.setStyle(TableStyle(style))
    flow.append(table)

    if marks:
        flow += [Spacer(1, 5), Paragraph(
            "Shaded rows carry a rate set by hand for that session rather than "
            "the standard rate.", st["note"])]

    tail = [Paragraph("HOW THIS WAS CALCULATED", st["h2"]),
            Paragraph(
                "Every session is priced by the rule shown against it. A fixed "
                "rate pays the same regardless of what the session billed; a "
                "percentage pays a share of that session's revenue. Delegated "
                "sessions are settled with the delegator, so they pay the "
                "delegation cost rather than the coach's own rate. Sessions "
                "that were not completed appear only where they were reviewed "
                "and approved.", st["note"])]
    if signoff is not None:
        who = signoff.approved_by.name if signoff.approved_by else "—"
        when = (signoff.approved_at.strftime("%d %b %Y")
                if signoff.approved_at else "")
        tail += [Spacer(1, 8), Paragraph(
            "Approved by <b>%s</b> on %s. Figures confirmed correct and "
            "cleared for payout." % (who, when), st["note"])]
    else:
        tail += [Spacer(1, 8), Paragraph(
            "<b>Draft — not yet approved.</b> These figures are still under "
            "review and may change.", st["note"])]
    if generated_by:
        tail += [Paragraph("Generated by %s." % generated_by, st["note"])]
    flow.append(KeepTogether(tail))

    doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
