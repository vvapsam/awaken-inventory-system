"""What a delegator receives on paper: one invoice, or a whole statement.

Both are written for someone who was not in the room. An invoice says what one
charge is for. A statement answers the question that actually gets asked on the
phone — what do I owe, and what was it for — so it carries the open invoices
first and the sessions behind them underneath.

WHAT IS NOT ON IT
-----------------
What we paid the covering coach, and therefore the margin — because margin is
charge minus cost, and publishing either gives the other away by subtraction.

That is not enforced by a flag here. It is enforced by where the numbers come
from: this renders ``charge.lines``, and a line carries only what was charged.
The cost lives on the charge row (``coach_cost``), which this module reads
exactly nowhere, and on a booking's ``commission``, which it also never touches.
If you find yourself wanting a figure the lines haven't got, the answer is
almost never to reach for the one place that has it.

ReportLab rather than an HTML-to-PDF engine, for the same reason as the coach
statement: pure Python, so the Nixpacks build needs no cairo/pango, and a long
table breaks across pages properly rather than by luck.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

from .commission_pdf import INK, LINE, MUTED, NAVY, TEAL, TEAL_TINT, _styles

# --------------------------------------------------------------------------
# The peso sign
# --------------------------------------------------------------------------
#
# The 14 fonts every PDF reader is required to have do not include ₱, so a
# statement set in Helvetica prints a black box in the middle of every figure.
# The coach statement works around it by writing "PHP"; a delegator's statement
# cannot, because the document it has to match uses the sign on every line.
#
# So Liberation Sans ships with the app. It is metrically identical to
# Helvetica — the same string is the same width to two decimal places — so
# nothing in the layout below moves when it is used, and everything falls back
# to Helvetica if the files are ever missing from a build. Losing the glyph is
# a blemish; losing the document would not be.

_FONT_DIR = Path(__file__).with_name("static") / "fonts"
BODY, BOLD = "Helvetica", "Helvetica-Bold"
HAS_PESO = False

try:
    pdfmetrics.registerFont(TTFont("AwkSans", str(_FONT_DIR / "LiberationSans-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("AwkSans-Bold", str(_FONT_DIR / "LiberationSans-Bold.ttf")))
    pdfmetrics.registerFontFamily("AwkSans", normal="AwkSans", bold="AwkSans-Bold",
                                  italic="AwkSans", boldItalic="AwkSans-Bold")
    BODY, BOLD, HAS_PESO = "AwkSans", "AwkSans-Bold", True
except Exception:                                   # pragma: no cover - build issue
    pass

PESO_SIGN = chr(0x20B1)

#: Glyphs the standard PDF fonts do not have. They render as a black box, so
#: anything a person or another screen wrote goes through `_pdfsafe` first. The
#: peso sign is the one that actually bites: invoice line descriptions are
#: written once, by the code that raises the invoice, in the screens' own
#: currency format — and stored that way, so they arrive here already carrying
#: it. Same reason the coach statement uses "PHP " throughout.
_SUBS = ((chr(0x21B3), "+ "),            # downwards arrow with tip rightwards
         (chr(0x2192), "-> "),           # rightwards arrow
         (chr(0x2713), "y"), (chr(0x2717), "n"))
#: Only rewritten when the shipped font is missing and Helvetica has to stand
#: in — otherwise the sign is left exactly as somebody typed it.
_NO_GLYPH = ((PESO_SIGN, "PHP "),)


def _pdfsafe(text) -> str:
    """Anything a person typed, made safe for a ReportLab Paragraph.

    Two hazards, both from the same source — the value came from a name field
    or another screen, not from this file. Missing glyphs become a black box;
    a stray ``&`` or ``<`` raises a parse error mid-render and takes the whole
    document with it, which is how a client called "Smith & Co" turns into a
    500 on the invoice screen and nowhere else.
    """
    out = str(text or "")
    for bad, good in _SUBS if HAS_PESO else _SUBS + _NO_GLYPH:
        out = out.replace(bad, good)
    return escape(out, quote=False)


AMBER = colors.HexColor("#b0741a")
AMBER_TINT = colors.HexColor("#fdf6e9")
VOID = colors.HexColor("#b3261e")


def _pesos(v) -> str:
    """Money, as the statements AWAKEN sends by hand write it.

    Falls back to "PHP " only when the shipped font could not be registered,
    because a black box where a currency mark should be is worse than a word.
    """
    mark = PESO_SIGN if HAS_PESO else "PHP "
    return mark + "{:,.2f}".format(float(v or 0))


def _reface(st: dict) -> dict:
    """Put the shipped face on styles borrowed from the coach statement.

    Those styles are Helvetica, which has no peso sign — and this document
    prints one on every line. Since the shipped face is metrically identical,
    swapping it in moves nothing.
    """
    for style in st.values():
        style.fontName = BOLD if "Bold" in (style.fontName or "") else BODY
    return st


def _terms(charge) -> str:
    if charge.is_void:
        return ("<b>CANCELLED.</b> This invoice has been voided and is not "
                "payable.%s" % ((" " + _pdfsafe(charge.voided_reason))
                                if charge.voided_reason else ""))
    if charge.status == "paid":
        when = charge.paid_at.strftime("%-d %b %Y") if charge.paid_at else ""
        return "<b>Paid%s.</b> Thank you." % ((" " + when) if when else "")
    return ("Payable on receipt. Please quote <b>%s</b> on the transfer so we "
            "can match it to this invoice." % (charge.number or ""))


def invoice(charge, *, generated_by: str = "",
            company: str = "AWAKEN Fitness Center") -> bytes:
    """Render one delegator invoice and return the PDF bytes."""
    st = _reface(_styles())
    buf = io.BytesIO()
    period = charge.dates_label or charge.period_label or ""
    who = _pdfsafe(charge.delegator_name) or "-"
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=20 * mm,
        title="%s — %s" % (charge.number or "Invoice", who),
        author=company, subject="Delegation invoice")
    doc.footer_left = "%s · %s · generated %s" % (
        company, charge.number or "", datetime.now(timezone.utc).strftime("%d %b %Y"))

    lines = sorted(
        charge.lines,
        key=lambda l: (l.occurred_on or datetime.min.date(), l.booking_ref or "",
                       # The overtime line sits directly under the session it
                       # belongs to, never above it.
                       1 if (l.kind or "") == "overtime" else 0))
    ot_lines = [l for l in lines if (l.kind or "") == "overtime"]
    ot_total = sum((Decimal(str(l.amount or 0)) for l in ot_lines), Decimal(0))
    total = sum((Decimal(str(l.amount or 0)) for l in lines), Decimal(0))
    fees = total - ot_total
    clients = sorted({(l.description or "").split(" · ")[-1].strip()
                      for l in lines if (l.kind or "") != "overtime"})

    flow = []
    head = Table([[
        Paragraph("Invoice", st["title"]),
        Paragraph("<b>%s</b><br/>%s<br/>%s" % (company, charge.number or "", period),
                  st["sub"]),
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

    if charge.is_void:
        flow += [Paragraph(
            "CANCELLED", ParagraphStyle("v", fontName="Helvetica-Bold",
                                        fontSize=13, leading=16, textColor=VOID)),
            Spacer(1, 4)]

    flow.append(Paragraph("BILL TO", st["h2"]))
    flow.append(Paragraph(
        "<b>%s</b>" % who,
        ParagraphStyle("who", fontName="Helvetica-Bold", fontSize=13,
                       leading=16, textColor=INK)))
    flow.append(Paragraph(
        "%d session%s &nbsp;·&nbsp; %d client%s &nbsp;·&nbsp; %s"
        % (charge.sessions or 0, "" if charge.sessions == 1 else "s",
           len(clients), "" if len(clients) == 1 else "s", period), st["sub"]))
    flow.append(Spacer(1, 10))

    def _card(label, value, foot=""):
        cell = [Paragraph(label, st["label"]), Paragraph(value, st["figure"])]
        if foot:
            cell.append(Paragraph(foot, st["label"]))
        return cell

    cards = [_card("AMOUNT DUE", "<b>%s</b>" % _pesos(total)),
             _card("SESSION FEES", "<b>%s</b>" % _pesos(fees),
                   "%d session%s" % (charge.sessions or 0,
                                     "" if charge.sessions == 1 else "s"))]
    widths = [58 * mm, 58 * mm]
    if ot_total:
        cards.append(_card("OVERTIME", "<b>%s</b>" % _pesos(ot_total),
                           "%d session%s ran long"
                           % (len(ot_lines), "" if len(ot_lines) == 1 else "s")))
        widths = [58 * mm, 58 * mm, 58 * mm]
    card = Table([cards], colWidths=widths)
    cstyle = [
        ("BOX", (0, 0), (0, 0), 0.8, TEAL),
        ("BACKGROUND", (0, 0), (0, 0), TEAL_TINT),
        ("BOX", (1, 0), (-1, 0), 0.8, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
    ]
    if ot_total:
        cstyle += [("BOX", (2, 0), (2, 0), 0.8, AMBER),
                   ("BACKGROUND", (2, 0), (2, 0), AMBER_TINT)]
    card.setStyle(TableStyle(cstyle))
    flow += [card, Spacer(1, 4)]

    flow.append(Paragraph("SESSIONS", st["h2"]))
    data = [[Paragraph("<b>Date</b>", st["cell"]),
             Paragraph("<b>Booking</b>", st["cell"]),
             Paragraph("<b>Description</b>", st["cell"]),
             Paragraph("<b>Covered by</b>", st["cell"]),
             Paragraph("<b>Amount</b>", st["cellr"])]]
    ot_rows = []
    for i, l in enumerate(lines, start=1):
        is_ot = (l.kind or "") == "overtime"
        if is_ot:
            ot_rows.append(i)
        data.append([
            Paragraph(l.occurred_on.strftime("%d %b") if l.occurred_on else "",
                      st["cell"]),
            Paragraph(_pdfsafe(l.booking_ref), st["cell"]),
            Paragraph(("&nbsp;&nbsp;&nbsp;" if is_ot else "")
                      + _pdfsafe(l.description), st["cell"]),
            Paragraph(_pdfsafe(l.coach), st["cell"]),
            Paragraph(_pesos(l.amount), st["cellr"]),
        ])
    data.append([Paragraph("<b>Total due</b>", st["cell"]), "", "", "",
                 Paragraph("<b>%s</b>" % _pesos(total), st["cellr"])])

    table = Table(data, repeatRows=1,
                  colWidths=[14 * mm, 22 * mm, 71 * mm, 40 * mm, 27 * mm])
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK),
        ("SPAN", (0, -1), (3, -1)),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#fafbfc")),
    ]
    for i in ot_rows:
        style += [("BACKGROUND", (0, i), (-1, i), AMBER_TINT),
                  ("TEXTCOLOR", (4, i), (4, i), AMBER)]
    table.setStyle(TableStyle(style))
    flow.append(table)

    if ot_rows:
        flow += [Spacer(1, 5), Paragraph(
            "Shaded rows are overtime — hours a session ran beyond its "
            "scheduled length, charged at the hourly rate shown against them.",
            st["note"])]

    tail = []
    if charge.note:
        tail += [Paragraph("NOTE", st["h2"]),
                 Paragraph(_pdfsafe(charge.note), st["note"])]
    tail += [Paragraph("TERMS", st["h2"]), Paragraph(_terms(charge), st["note"]),
             Spacer(1, 6),
             Paragraph("Anything here look wrong? Reply to the email this "
                       "invoice came with and we will check it.", st["note"])]
    if generated_by:
        tail.append(Paragraph("Generated by %s." % generated_by, st["note"]))
    flow.append(KeepTogether(tail))

    doc.build(flow, onFirstPage=_page, onLaterPages=_page)
    return buf.getvalue()


def _page(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 12 * mm, doc.footer_left)
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, "Page %d" % doc.page)
    canvas.restoreState()


# ==========================================================================
# The statement
# ==========================================================================
#
# Drawn to match the statement AWAKEN already sends by hand — same letterhead,
# same teal band, same three-column breakdown, same account-activity ledger
# with payments picked out in rust. A delegator who has been reading that
# document for a year should not have to learn a second one because we started
# generating it.
#
# Palette lifted from that document rather than from the app's own, which is a
# slightly different teal. Two greens that nearly match read as a mistake; one
# that matches exactly reads as the same company.

S_TEAL = colors.HexColor("#2e8b9e")
S_TINT = colors.HexColor("#eaf4f6")
S_INK = colors.HexColor("#333333")
S_GREY = colors.HexColor("#6b6b6b")
S_RULE = colors.HexColor("#d9d9d9")
PAY_BG = colors.HexColor("#fbede6")
PAY_INK = colors.HexColor("#d9531e")

_W = A4[0] - 36 * mm          # usable width at 18mm margins


def _sstyles():
    st = _reface(_styles())
    def P(name, **kw):
        base = dict(fontName=BODY, fontSize=8.5, leading=11,
                    textColor=S_INK)
        base.update(kw)
        return ParagraphStyle(name, **base)
    st.update({
        "s_word": P("sw", fontName=BODY, fontSize=17, leading=20,
                    textColor=colors.HexColor("#2b2b2b")),
        "s_title": P("stt", fontName=BOLD, fontSize=24, leading=27,
                     textColor=S_TEAL, alignment=TA_RIGHT),
        "s_co": P("sco", fontName=BOLD, fontSize=9.5, leading=12.5),
        "s_coline": P("scl", fontSize=8, leading=11, textColor=S_GREY),
        "s_lab": P("sl", fontName=BOLD, fontSize=8, leading=11,
                   textColor=S_INK),
        "s_name": P("sn", fontSize=13, leading=17, textColor=S_INK),
        "s_meta": P("sm", fontSize=8.5, leading=13, textColor=S_INK),
        "s_duelab": P("sdl", fontName=BOLD, fontSize=11.5,
                      leading=14, textColor=colors.white),
        "s_duesub": P("sds", fontSize=7.5, leading=10,
                      textColor=colors.HexColor("#d6ecf1")),
        "s_duefig": P("sdf", fontName=BOLD, fontSize=21, leading=25,
                      textColor=colors.white, alignment=TA_RIGHT),
        "s_h": P("sh", fontName=BOLD, fontSize=8.5, leading=12,
                 textColor=S_INK, spaceBefore=13, spaceAfter=5),
        "s_th": P("sth", fontName=BOLD, fontSize=8, leading=11,
                  textColor=colors.white),
        "s_thr": P("sthr", fontName=BOLD, fontSize=8, leading=11,
                   textColor=colors.white, alignment=TA_RIGHT),
        "s_cat": P("scat", fontName=BOLD, fontSize=8.5, leading=11),
        "s_catr": P("scatr", fontName=BOLD, fontSize=8.5, leading=11,
                    alignment=TA_RIGHT),
        "s_sub": P("ssub", fontSize=8.5, leading=11, leftIndent=9),
        "s_r": P("sr", fontSize=8.5, leading=11, alignment=TA_RIGHT),
        "s_grand": P("sg", fontName=BOLD, fontSize=10, leading=13,
                     textColor=colors.white),
        "s_grandr": P("sgr", fontName=BOLD, fontSize=10, leading=13,
                      textColor=colors.white, alignment=TA_RIGHT),
        "s_doc": P("sd", fontName=BOLD, fontSize=8.5, leading=11,
                   textColor=S_TEAL),
        "s_date": P("sdt", fontName=BOLD, fontSize=8, leading=11,
                    textColor=S_TEAL),
        "s_item": P("si", fontSize=8.5, leading=11, leftIndent=9),
        "s_note": P("sni", fontSize=7.5, leading=10, textColor=S_GREY,
                    leftIndent=9),
        "s_pay": P("sp", fontName=BOLD, fontSize=8.5, leading=11,
                   textColor=PAY_INK),
        "s_payr": P("spr", fontName=BOLD, fontSize=8.5, leading=11,
                    textColor=PAY_INK, alignment=TA_RIGHT),
        "s_paydt": P("spd", fontName=BOLD, fontSize=8, leading=11,
                     textColor=PAY_INK),
        "s_sumlab": P("ssl", fontSize=8.5, leading=13, textColor=S_INK),
        "s_sumval": P("ssv", fontSize=8.5, leading=13, textColor=S_INK,
                      alignment=TA_RIGHT),
        "s_dueb": P("sdb", fontName=BOLD, fontSize=10.5, leading=14,
                    textColor=S_TEAL),
        "s_duebr": P("sdbr", fontName=BOLD, fontSize=10.5,
                     leading=14, textColor=S_TEAL, alignment=TA_RIGHT),
        "s_payh": P("sph", fontName=BOLD, fontSize=9, leading=12),
        "s_payl": P("spl", fontSize=8, leading=11.5, textColor=S_GREY),
    })
    return st


def _wordmark(st):
    """The AWAKEN logotype, letter-spaced the way it is set on the letterhead.

    Drawn as text rather than placed as an image: the PNG in static/ is white
    for use on the navy email header and would be invisible here, and a second
    dark copy is one more asset to keep in step with the first.
    """
    return Paragraph(
        "A&nbsp; W&nbsp; A&nbsp; K&nbsp; E&nbsp; N",
        st["s_word"])


def _flat(rows, widths, style, pad=4):
    t = Table(rows, colWidths=widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
    ] + style))
    return t


def statement(delegator, data, *, company: str = "") -> bytes:
    """One delegator's statement of account, in the house layout."""
    st = _sstyles()
    buf = io.BytesIO()
    co = data["company"]
    who = _pdfsafe(delegator.name) or "-"
    as_at = data["as_at"]
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=18 * mm,
        title="Statement %s — %s" % (data["stmt_no"], who),
        author=co["name"], subject="Statement of account")
    doc.footer_left = "%s · %s · %s" % (co["name"], data["stmt_no"],
                                        as_at.strftime("%d %b %Y"))

    flow = []

    # ---- letterhead -----------------------------------------------------
    flow.append(_flat(
        [[_wordmark(st), Paragraph("STATEMENT", st["s_title"])]],
        [_W * 0.55, _W * 0.45], [], pad=0))
    flow.append(Spacer(1, 7))
    lines = [Paragraph("<b>%s</b>" % _pdfsafe(co["name"]), st["s_co"])]
    for a in co["address"]:
        lines.append(Paragraph(_pdfsafe(a), st["s_coline"]))
    lines.append(Paragraph(_pdfsafe(co["email"]), st["s_coline"]))
    lines.append(Paragraph("%s &nbsp;•&nbsp; %s" % (_pdfsafe(co["phone"]),
                                                    _pdfsafe(co["web"])),
                           st["s_coline"]))
    flow += lines
    flow.append(Spacer(1, 12))

    # ---- who / which statement -----------------------------------------
    left = [Paragraph("STATEMENT FOR", st["s_lab"]), Spacer(1, 3),
            Paragraph(who, st["s_name"])]
    meta = [("Statement no.:", data["stmt_no"]),
            ("Statement date:", as_at.strftime("%m/%d/%Y")),
            ("Period:", data["period"] or "—"),
            ("Terms:", "Due on receipt")]
    right = [Paragraph(
        "".join("%s&nbsp;&nbsp;&nbsp;%s<br/>" % (k, _pdfsafe(v)) for k, v in meta),
        st["s_meta"])]
    band = Table([[left, right]], colWidths=[_W * 0.47, _W * 0.53])
    band.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), S_TINT),
        ("LEFTPADDING", (0, 0), (0, 0), 12),
        ("LEFTPADDING", (1, 0), (1, 0), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 11),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
    ]))
    flow += [band, Spacer(1, 12)]

    # ---- the figure everything else explains ----------------------------
    due = data["amount_due"]
    sub = "As of %s" % as_at.strftime("%d %b %Y")
    if data["current"]:
        sub += " · current charges due %s" % data["due_by"].strftime("%d %b %Y")
    hero = Table([[
        [Paragraph("TOTAL AMOUNT DUE", st["s_duelab"]),
         Paragraph(sub, st["s_duesub"])],
        Paragraph(_pesos(due), st["s_duefig"]),
    ]], colWidths=[_W * 0.5, _W * 0.5])
    hero.setStyle(TableStyle([
        ("VALIGN", (0, 0), (0, 0), "MIDDLE"),
        ("VALIGN", (1, 0), (1, 0), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), S_TEAL),
        ("LEFTPADDING", (0, 0), (0, 0), 14),
        ("RIGHTPADDING", (1, 0), (1, 0), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    flow += [hero, Spacer(1, 12)]

    # ---- breakdown -------------------------------------------------------
    flow.append(Paragraph("STATEMENT BREAKDOWN", st["s_h"]))
    cols = [_W * 0.37, _W * 0.21, _W * 0.21, _W * 0.21]
    rows = [[Paragraph("", st["s_th"]),
             Paragraph("Balance Forward", st["s_thr"]),
             Paragraph("Current Charges", st["s_thr"]),
             Paragraph("Total", st["s_thr"])]]
    style = [("BACKGROUND", (0, 0), (-1, 0), S_TEAL)]
    n = 1
    if not data["breakdown"]:
        rows.append([Paragraph("Nothing outstanding — the account is clear.",
                               st["s_sub"]), "", "",
                     Paragraph(_pesos(0), st["s_r"])])
        style.append(("LINEBELOW", (0, n), (-1, n), 0.5, S_RULE))
        n += 1
    for cat in data["breakdown"]:
        rows.append([Paragraph(_pdfsafe(cat["name"]), st["s_cat"]),
                     Paragraph(_pesos(cat["forward"]) if cat["forward"] else "",
                               st["s_catr"]),
                     Paragraph(_pesos(cat["current"]) if cat["current"] else "",
                               st["s_catr"]),
                     Paragraph(_pesos(cat["total"]), st["s_catr"])])
        style.append(("BACKGROUND", (0, n), (-1, n), S_TINT))
        n += 1
        for r in cat["rows"]:
            rows.append([
                Paragraph(_pdfsafe(r["label"]), st["s_sub"]),
                Paragraph(_pesos(r["forward"]) if r["forward"] else "", st["s_r"]),
                Paragraph(_pesos(r["current"]) if r["current"] else "", st["s_r"]),
                Paragraph(_pesos(r["forward"] + r["current"]), st["s_r"])])
            style.append(("LINEBELOW", (0, n), (-1, n), 0.5, S_RULE))
            n += 1
    rows.append([Paragraph("TOTAL AMOUNT DUE", st["s_grand"]),
                 Paragraph(_pesos(data["forward"]), st["s_grandr"]),
                 Paragraph(_pesos(data["current"]), st["s_grandr"]),
                 Paragraph(_pesos(due), st["s_grandr"])])
    style.append(("BACKGROUND", (0, n), (-1, n), S_TEAL))
    t = Table(rows, colWidths=cols, repeatRows=1)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("LEFTPADDING", (1, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ] + style))
    flow.append(t)

    if data["credit"]:
        flow += [Spacer(1, 6), Paragraph(
            "Includes %s paid in advance of invoicing, held on the account."
            % _pesos(data["credit"]), st["s_payl"])]

    # ---- full detail -----------------------------------------------------
    flow.append(PageBreak())
    flow.append(Paragraph("FULL DETAIL — ACCOUNT ACTIVITY (BY DATE)", st["s_h"]))
    acols = [_W * 0.16, _W * 0.62, _W * 0.22]
    arows = [[Paragraph("Date", st["s_th"]), Paragraph("Description", st["s_th"]),
              Paragraph("Amount", st["s_thr"])]]
    astyle = [("BACKGROUND", (0, 0), (-1, 0), S_TEAL)]
    n = 1
    for a in data["activity"]:
        when = a["on"].strftime("%m/%d/%Y") if a["on"] else ""
        if a["kind"] == "payment":
            arows.append([Paragraph(when, st["s_paydt"]),
                          Paragraph(_pdfsafe(a["ref"]), st["s_pay"]),
                          Paragraph("-" + _pesos(-a["amount"]), st["s_payr"])])
            astyle.append(("BACKGROUND", (0, n), (-1, n), PAY_BG))
            n += 1
            continue
        arows.append([Paragraph(when, st["s_date"]),
                      Paragraph("INVOICE %s" % _pdfsafe(a["ref"]), st["s_doc"]),
                      Paragraph("", st["s_r"])])
        astyle.append(("BACKGROUND", (0, n), (-1, n), S_TINT))
        n += 1
        for it in a["items"]:
            cell = [Paragraph(_pdfsafe(it["text"]), st["s_item"])]
            if it["note"]:
                cell.append(Paragraph(_pdfsafe(it["note"]), st["s_note"]))
            arows.append(["", cell, Paragraph(_pesos(it["amount"]), st["s_r"])])
            astyle.append(("LINEBELOW", (0, n), (-1, n), 0.5, S_RULE))
            n += 1
        arows.append(["", Paragraph("Invoice subtotal", st["s_cat"]),
                      Paragraph(_pesos(a["amount"]), st["s_catr"])])
        astyle.append(("LINEBELOW", (0, n), (-1, n), 0.8, S_INK))
        n += 1
    t = Table(arows, colWidths=acols, repeatRows=1)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("LEFTPADDING", (1, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ] + astyle))
    flow.append(t)

    # ---- reconciliation --------------------------------------------------
    led = data["ledger"]
    summ = Table([
        [Paragraph("Total invoiced", st["s_sumlab"]),
         Paragraph(_pesos(led["invoiced"]), st["s_sumval"])],
        [Paragraph("Total payments received", st["s_sumlab"]),
         Paragraph("-" + _pesos(led["received"]), st["s_sumval"])],
        [Paragraph("TOTAL AMOUNT DUE", st["s_dueb"]),
         Paragraph(_pesos(due), st["s_duebr"])],
    ], colWidths=[_W * 0.45, _W * 0.28], hAlign="RIGHT")
    summ.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEABOVE", (0, 2), (-1, 2), 0.8, S_RULE),
        ("BACKGROUND", (0, 2), (-1, 2), S_TINT),
    ]))
    flow += [Spacer(1, 14), summ, Spacer(1, 16)]

    # ---- how to pay ------------------------------------------------------
    pay = [Paragraph("Payment details", st["s_payh"]), Spacer(1, 4),
           Paragraph("Bank: %s<br/>Account Name: %s<br/>Account Number: %s"
                     % (_pdfsafe(co["bank"]), _pdfsafe(co["account_name"]),
                        _pdfsafe(co["account_number"])), st["s_payl"]),
           Spacer(1, 6),
           Paragraph("Once payment is made, send your proof of payment to %s."
                     % _pdfsafe(co["email"]), st["s_payl"])]
    box = Table([[pay]], colWidths=[_W])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f7f7f7")),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    flow.append(KeepTogether(box))

    doc.build(flow, onFirstPage=_page, onLaterPages=_page)
    return buf.getvalue()
