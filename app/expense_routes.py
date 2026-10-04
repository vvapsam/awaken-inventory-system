"""Reimbursements, and the one voucher that pays somebody everything at once.

Two halves of the same money problem.

**An expense report** is what somebody laid out of their own pocket: a batch of
lines, each with the date on the receipt, which account it belongs to, what it
cost and the receipt itself. They write it on their phone, a line at a time,
and each line is saved the moment it is added — a receipt photo must never be
lost because the fifth upload failed. Nothing reaches the office until they
submit, and once submitted they can still change it right up until it is
approved. Approving locks it.

**A payment voucher** is what actually leaves the business. One person's
commission for a chosen period, plus their approved reimbursements, plus
whatever adjustments are waiting on them, on one document with one net figure
and one transfer.

The rule that makes the voucher safe is one column. A payout, a report and an
adjustment each carry a `voucher_id`, and issuing sets it. Nothing that is on a
voucher can be put on another, so "pay everything at once" cannot quietly pay
September twice.

The rule that makes it honest is that it **gathers rather than recalculates**.
The commission figure is whatever the run decided. The reimbursement is
whatever was approved. The voucher adds them up and records that the money
left; open any of the three from here and it still says what it always said.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import Depends, Form, Request
#: Starlette's, not FastAPI's. `fastapi.UploadFile` is a *subclass*, and what a
#: parsed form actually holds is the parent — so `isinstance(part,
#: fastapi.UploadFile)` is False for every real upload, and every receipt is
#: silently refused. One import, one afternoon.
from starlette.datastructures import UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .db import get_db
#: The wordmark that rides inside an email, borrowed rather than re-read: it
#: is held in memory there, and a voucher going out should not depend on the
#: filesystem being readable at that moment.
from .commission_routes import LOGO_CID, _logo_bytes
from .event_routes import base_url
from .mailer import Mailer, looks_like_email
from .models import (
    Account, ACCOUNT_KINDS,
    CommissionAdjustment, CommissionCoachRate, CommissionPayout,
    CommissionRun, RUN_DRAFT,
    EXPENSE_APPROVED, EXPENSE_DRAFT, EXPENSE_RETURNED, EXPENSE_STATUSES,
    EXPENSE_SUBMITTED, ExpenseLine, ExpenseReport,
    PaymentVoucher, Staff,
    VOUCHER_LINK_DAYS, VOUCHER_PAID, VOUCHER_UNPAID, VOUCHER_VOID, VoucherLink,
    now_utc,
)

#: What a receipt may weigh. Generous, because a modern phone photo is 4-6 MB
#: and the person holding it cannot choose; refused above it, because a 40 MB
#: video of a receipt is somebody's mistake rather than their evidence.
RECEIPT_MAX = 12 * 1024 * 1024

#: How the money left. Free text would give four spellings of "bank transfer"
#: inside a month, and the books cannot add those up.
PAY_METHODS = ["Bank transfer", "GCash", "Cash", "Cheque", "Other"]


def _money(raw) -> Decimal | None:
    """A typed amount as a positive two-place figure, or nothing."""
    try:
        value = Decimal(str(raw or "").replace(",", "").strip() or "0")
    except (InvalidOperation, ValueError):
        return None
    value = value.quantize(Decimal("0.01"))
    return value if value > 0 else None


def _day(raw):
    try:
        return date.fromisoformat((raw or "").strip()[:10])
    except (ValueError, TypeError):
        return None


def next_number(db: Session, model, prefix: str, field: str = "number") -> str:
    """ER-0007, PV-0003, PR-0002. Counted off the highest already issued.

    Off the numbers rather than off a count of rows: a voided voucher keeps its
    number, and reusing it would put two different documents in the books under
    one reference.

    `field` is which column holds the series. A pay run has no row of its own —
    it is a string stamped on every voucher issued together — so its series is
    counted off that column instead.
    """
    col = getattr(model, field)
    best = 0
    for (num,) in db.query(col).filter(col.isnot(None)).distinct():
        head, _, tail = (num or "").rpartition("-")
        if head == prefix and tail.isdigit():
            best = max(best, int(tail))
    return "%s-%04d" % (prefix, best + 1)


def open_accounts(db: Session, keep=None) -> list:
    """The chart as a form offers it: open accounts, in their own order.

    `keep` holds one closed account on the list — the one a row already
    carries. Dropping it would silently retag that row the moment anybody
    opened its form.
    """
    rows = (db.query(Account)
            .order_by(Account.kind, Account.position, Account.id).all())
    return [a for a in rows if not a.closed or a.id == keep]


def report_total(report) -> Decimal:
    return report.total


def by_account(lines) -> list:
    """What a set of lines comes to, per account, in the chart's own order.

    The figure a bookkeeper actually wants. Built from the lines rather than
    stored, because retagging a line must move the money with it.
    """
    tally, order = {}, []
    for line in lines:
        key = line.account_id
        if key not in tally:
            tally[key] = {"account": line.account, "total": Decimal(0), "n": 0}
            order.append(key)
        tally[key]["total"] += line.money
        tally[key]["n"] += 1
    rows = [tally[k] for k in order]
    rows.sort(key=lambda r: (r["account"] is None,
                             (r["account"].kind if r["account"] else ""),
                             (r["account"].position if r["account"] else 0)))
    return rows


def register(app, deps):
    render = deps["render"]
    require = deps["require"]
    require_admin = deps["require_admin"]
    #: The voucher page somebody opens from their email has nobody logged in,
    #: so it renders through the raw environment rather than render().
    templates = deps["templates"]
    tz = deps.get("tz")

    def money_guard(request, db):
        """The expense area: admins, or whoever holds the commissions area."""
        return require(request, db, perm="manage_commissions")

    def _mine(db, staff):
        return (db.query(ExpenseReport)
                .filter(ExpenseReport.staff_id == getattr(staff, "id", 0))
                .order_by(ExpenseReport.id.desc()).all())

    def _is_office(staff) -> bool:
        """Whoever runs the money: an admin, or the commissions area."""
        return (getattr(staff, "role", "") == "admin"
                or "manage_commissions" in (getattr(staff, "permissions", "")
                                            or ""))

    def _owned(db, staff, rid):
        """One report, to the person whose it is — or to the office.

        Ownership is the first answer, because an expense report is about
        somebody's own money and "may I see this" is really "is it mine",
        which no permission can say. The office is the second answer, because
        somebody hands over a fistful of paper receipts and asks the office to
        type them: the claim is still theirs, and the office still has to be
        able to open it.

        One person still cannot open another person's claim.
        """
        report = db.get(ExpenseReport, rid)
        if report is None:
            return None
        if report.staff_id == getattr(staff, "id", None):
            return report
        # The office, and only to what the office may see at all: a draft
        # somebody is writing for themselves stays theirs even from here.
        return report if (_is_office(staff) and report.office_visible) else None

    # ── the person's own reports ───────────────────────────────────────

    @app.get("/expenses", response_class=HTMLResponse)
    def my_expenses(request: Request, db: Session = Depends(get_db)):
        """Everything this person has claimed. Theirs alone, whoever they are.

        No permission on it: every member of staff can be out of pocket, and a
        reimbursement form somebody has to be granted is a form that does not
        get used.
        """
        staff, redir = require(request, db)
        if redir:
            return redir
        rows = _mine(db, staff)
        return render(request, "expenses_mine.html", db, staff,
                      active="expenses", rows=rows,
                      owed=sum((r.total for r in rows
                                if r.status == EXPENSE_APPROVED
                                and r.voucher_id is None), Decimal(0)))

    def _start_report(db, staff, *, owner, person, on="") -> ExpenseReport:
        report = ExpenseReport(
            number=next_number(db, ExpenseReport, "ER"),
            staff_id=owner, person=person or "",
            occurred_on=_day(on) or date.today(),
            status=EXPENSE_DRAFT, created_by_id=getattr(staff, "id", None))
        db.add(report)
        db.commit()
        return report

    @app.post("/expenses/new")
    def my_expense_new(request: Request, on: str = Form(""),
                       db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return redir
        report = _start_report(db, staff, owner=staff.id,
                               person=staff.name or "", on=on)
        return RedirectResponse("/expenses/%d" % report.id, status_code=303)

    @app.post("/admin/expenses/new")
    def office_expense_new(request: Request, who: str = Form(""),
                           on: str = Form(""),
                           db: Session = Depends(get_db)):
        """Start one for somebody else.

        Paper receipts handed across the counter, a coach with no phone, a
        claim somebody asked about in person. The report belongs to them - it
        shows on their list, it is paid on their voucher - and `created_by_id`
        records who actually typed it.
        """
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        person = (who or "").strip()
        owner = dict(_people(db)).get(person)
        if not person:
            return RedirectResponse("/admin/expenses?err=who", status_code=303)
        report = _start_report(db, staff, owner=owner, person=person, on=on)
        return RedirectResponse("/expenses/%d" % report.id, status_code=303)

    @app.get("/expenses/{rid}", response_class=HTMLResponse)
    def my_expense(request: Request, rid: int, db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return redir
        report = _owned(db, staff, rid)
        if report is None:
            return RedirectResponse("/expenses", status_code=303)
        return render(request, "expense_report.html", db, staff,
                      active="expenses", r=report,
                      accounts=open_accounts(db),
                      ACCOUNT_KINDS=ACCOUNT_KINDS,
                      today=date.today().isoformat(),
                      # Whose eyes. The page is the same either way; what
                      # changes is whether it says "you" or names them.
                      mine=(report.staff_id == staff.id),
                      office=_is_office(staff),
                      groups=by_account(report.lines))

    @app.post("/expenses/{rid}/line")
    async def my_expense_line(request: Request, rid: int,
                              db: Session = Depends(get_db)):
        """One receipt, saved on its own.

        Its own request rather than part of a big save, because the thing most
        likely to fail here is the upload, and a failed upload must cost the
        person one line rather than the five they had already typed.
        """
        staff, redir = require(request, db)
        if redir:
            return redir
        report = _owned(db, staff, rid)
        if report is None:
            return RedirectResponse("/expenses", status_code=303)
        back = "/expenses/%d" % rid
        if not report.editable:
            return RedirectResponse(back + "?err=locked", status_code=303)
        form = await request.form()
        amount = _money(form.get("amount"))
        account = db.get(Account, int(form.get("account")))  \
            if (form.get("account") or "").strip().isdigit() else None
        if account is not None and account.closed:
            account = None
        upload = form.get("receipt")
        blob = b""
        if isinstance(upload, UploadFile) and (upload.filename or ""):
            blob = await upload.read()
        # Three separate refusals, because "that didn't save" with no reason is
        # how somebody presses the same button four times.
        if not blob:
            return RedirectResponse(back + "?err=receipt", status_code=303)
        if len(blob) > RECEIPT_MAX:
            return RedirectResponse(back + "?err=big", status_code=303)
        if amount is None or account is None:
            return RedirectResponse(back + "?err=missing", status_code=303)
        db.add(ExpenseLine(
            report_id=report.id,
            occurred_on=_day(form.get("on")) or date.today(),
            account_id=account.id, amount=amount,
            note=(form.get("note") or "").strip()[:200],
            receipt=blob,
            receipt_mime=(getattr(upload, "content_type", "")
                          or "application/octet-stream"),
            receipt_name=(upload.filename or "receipt")[:120]))
        # A line arriving on a report that was sent back puts it in front of
        # the office again on its own: somebody fixing what was asked of them
        # should not have to remember to resubmit.
        if report.status == EXPENSE_RETURNED:
            report.status = EXPENSE_DRAFT
        db.commit()
        return RedirectResponse(back + "?added=1", status_code=303)

    @app.post("/expenses/{rid}/line/{lid}/delete")
    def my_expense_line_delete(request: Request, rid: int, lid: int,
                               db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return redir
        report = _owned(db, staff, rid)
        line = db.get(ExpenseLine, lid)
        if report is not None and report.editable and line is not None \
                and line.report_id == report.id:
            db.delete(line)
            db.commit()
        return RedirectResponse("/expenses/%d" % rid, status_code=303)

    @app.post("/expenses/{rid}/submit")
    def my_expense_submit(request: Request, rid: int,
                          db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return redir
        report = _owned(db, staff, rid)
        if report is None:
            return RedirectResponse("/expenses", status_code=303)
        back = "/expenses/%d" % rid
        if not report.lines:
            return RedirectResponse(back + "?err=empty", status_code=303)
        if report.status in (EXPENSE_DRAFT, EXPENSE_RETURNED):
            report.status = EXPENSE_SUBMITTED
            report.submitted_at = now_utc()
            db.commit()
        return RedirectResponse(back, status_code=303)

    @app.post("/expenses/{rid}/delete")
    def my_expense_delete(request: Request, rid: int,
                          db: Session = Depends(get_db)):
        """Throw away a draft. Anything the office has seen is a record."""
        staff, redir = require(request, db)
        if redir:
            return redir
        report = _owned(db, staff, rid)
        if report is not None and report.status == EXPENSE_DRAFT:
            db.delete(report)
            db.commit()
        return RedirectResponse("/expenses", status_code=303)

    @app.get("/expenses/{rid}/receipt/{lid}")
    def expense_receipt(request: Request, rid: int, lid: int,
                        db: Session = Depends(get_db)):
        """The receipt itself — to the person it belongs to, or to the office.

        Served inline so the browser's own viewer is the preview. Two readers
        and no third: a receipt carries somebody's address, their card's last
        four and where they were on a Tuesday night.
        """
        staff, redir = require(request, db)
        if redir:
            return redir
        line = db.get(ExpenseLine, lid)
        if line is None or line.report_id != rid or not line.receipt:
            return RedirectResponse("/expenses", status_code=303)
        if _owned(db, staff, rid) is None:
            return RedirectResponse("/expenses", status_code=303)
        return Response(
            content=line.receipt,
            media_type=line.receipt_mime or "application/octet-stream",
            headers={"Content-Disposition": 'inline; filename="%s"'
                     % (line.receipt_name or "receipt")})

    # ── the office ─────────────────────────────────────────────────────

    @app.get("/admin/expenses", response_class=HTMLResponse)
    def expenses_admin(request: Request, show: str = "pending",
                       db: Session = Depends(get_db)):
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        # A draft somebody is writing for themselves is never here: they are
        # not asking anybody for anything yet, and showing it would invite a
        # half-written claim to be approved. A draft the office typed *for*
        # somebody is the office's own unfinished work and has to be findable.
        rows = [r for r in db.query(ExpenseReport)
                .order_by(ExpenseReport.submitted_at.desc().nullslast(),
                          ExpenseReport.id.desc()).all()
                if r.office_visible]
        buckets = {
            "writing": [r for r in rows if r.status == EXPENSE_DRAFT],
            "pending": [r for r in rows if r.status in (EXPENSE_SUBMITTED,
                                                        EXPENSE_RETURNED)],
            "approved": [r for r in rows if r.status == EXPENSE_APPROVED
                         and r.voucher_id is None],
            "paid": [r for r in rows if r.voucher_id is not None],
            "all": rows,
        }
        shown = buckets.get(show, buckets["pending"])
        return render(request, "expenses_admin.html", db, staff,
                      active="expenses", rows=shown, show=show,
                      people=[n for n, _i in _people(db)],
                      today=date.today().isoformat(),
                      counts={k: len(v) for k, v in buckets.items()},
                      waiting=sum((r.total for r in buckets["pending"]),
                                  Decimal(0)),
                      owed=sum((r.total for r in buckets["approved"]),
                               Decimal(0)))

    @app.get("/admin/expenses/{rid}", response_class=HTMLResponse)
    def expense_review(request: Request, rid: int,
                       db: Session = Depends(get_db)):
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        report = db.get(ExpenseReport, rid)
        if report is None or not report.office_visible:
            return RedirectResponse("/admin/expenses", status_code=303)
        if report.status == EXPENSE_DRAFT:
            # Still being written. There is nothing to review yet; the page
            # that can add lines to it is the one they want.
            return RedirectResponse("/expenses/%d" % rid, status_code=303)
        return render(request, "expense_review.html", db, staff,
                      active="expenses", r=report,
                      accounts=open_accounts(db),
                      ACCOUNT_KINDS=ACCOUNT_KINDS,
                      groups=by_account(report.lines),
                      can_pay=(getattr(staff, "role", "") == "admin"))

    @app.post("/admin/expenses/{rid}/line/{lid}/account")
    def expense_retag(request: Request, rid: int, lid: int,
                      account: str = Form(""),
                      db: Session = Depends(get_db)):
        """Move one line to a different account before approving it.

        The person picked what they thought it was; the books are the office's.
        Allowed after approval too, for the same reason an adjustment can be
        reclassified: the money is settled, but filing gets corrected.
        """
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        line = db.get(ExpenseLine, lid)
        if line is not None and line.report_id == rid:
            acct = db.get(Account, int(account)) \
                if account.strip().isdigit() else None
            line.account_id = acct.id if acct is not None else None
            db.commit()
        return RedirectResponse("/admin/expenses/%d" % rid, status_code=303)

    @app.post("/admin/expenses/{rid}/approve")
    def expense_approve(request: Request, rid: int,
                        db: Session = Depends(get_db)):
        """Approve it. Admin only, and it locks.

        Approving is the moment the business owes the money, so it is the
        admin role rather than the commissions area somebody reviewing might
        hold.
        """
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        report = db.get(ExpenseReport, rid)
        if report is not None and report.status in (EXPENSE_SUBMITTED,
                                                    EXPENSE_RETURNED):
            if not report.lines:
                return RedirectResponse("/admin/expenses/%d?err=empty" % rid,
                                        status_code=303)
            report.status = EXPENSE_APPROVED
            report.reviewed_at = now_utc()
            report.reviewed_by_id = staff.id
            report.review_note = ""
            db.commit()
        return RedirectResponse("/admin/expenses/%d" % rid, status_code=303)

    @app.post("/admin/expenses/{rid}/return")
    def expense_return(request: Request, rid: int, note: str = Form(""),
                       db: Session = Depends(get_db)):
        """Send it back with a note. Nothing is deleted.

        Returned rather than rejected: the person fixes what was asked and
        sends the same report again, so the trail is one claim with a
        correction in it rather than two claims and a guess about which was
        real.
        """
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        report = db.get(ExpenseReport, rid)
        if report is not None and report.status in (EXPENSE_SUBMITTED,
                                                    EXPENSE_APPROVED):
            # An approved report can be sent back only while nothing has paid
            # it — after that the money has left and the correction is a fresh
            # adjustment, not an edit.
            if report.voucher_id is not None:
                return RedirectResponse("/admin/expenses/%d?err=paid" % rid,
                                        status_code=303)
            report.status = EXPENSE_RETURNED
            report.reviewed_at = now_utc()
            report.reviewed_by_id = staff.id
            report.review_note = (note or "").strip()[:800]
            db.commit()
        return RedirectResponse("/admin/expenses/%d" % rid, status_code=303)

    # ── payment vouchers ───────────────────────────────────────────────
    # Registered before /admin/vouchers/{vid}: FastAPI matches in registration
    # order, so with the wildcard first a GET to /admin/vouchers/new would be
    # read as voucher id "new".

    def _people(db) -> list:
        """Everybody who could be owed something, by display name.

        Three sources, because there is no one table of "people we pay": staff
        with a login, coaches with a rate, and anybody who already has a claim
        or a voucher. Missing a name here means a person nobody can pay.
        """
        names = {}
        for s in db.query(Staff).filter(Staff.is_active.is_(True)):
            if (s.name or "").strip():
                names[s.name.strip()] = s.id
        for r in db.query(CommissionCoachRate):
            if (r.coach or "").strip():
                names.setdefault(r.coach.strip(), r.coach_id)
        for r in db.query(ExpenseReport):
            if (r.person or "").strip():
                names.setdefault(r.person.strip(), r.staff_id)
        return sorted(names.items())

    def _claimable(db, who: str, staff_id):
        """The three stacks a voucher is built from, for one person.

        Each is "finished, and nothing has claimed it yet". A run still open, a
        report still waiting on approval and an adjustment already on a payout
        are all absent — not hidden, simply not yet owed or already settled.
        """
        # Only payouts from a run that is actually finalized. A payout on a
        # draft run is a figure still being argued about, and a voucher is not
        # the place to settle that argument.
        live = {r.id for r in db.query(CommissionRun)
                .filter(CommissionRun.status != RUN_DRAFT)}
        payouts = [p for p in db.query(CommissionPayout)
                   .filter(CommissionPayout.coach == who,
                           CommissionPayout.voucher_id.is_(None))
                   .order_by(CommissionPayout.id.desc()).all()
                   if p.run_id in live]
        reports = (db.query(ExpenseReport)
                   .filter(ExpenseReport.status == EXPENSE_APPROVED,
                           ExpenseReport.voucher_id.is_(None))
                   .filter((ExpenseReport.person == who)
                           | (ExpenseReport.staff_id == staff_id))
                   .order_by(ExpenseReport.id.desc()).all())
        adjustments = (db.query(CommissionAdjustment)
                       .filter(CommissionAdjustment.coach == who,
                               CommissionAdjustment.payout_id.is_(None),
                               CommissionAdjustment.voucher_id.is_(None))
                       .order_by(CommissionAdjustment.occurred_on.asc()
                                 .nullsfirst(),
                                 CommissionAdjustment.id.asc()).all())
        return payouts, reports, adjustments

    @app.get("/admin/vouchers", response_class=HTMLResponse)
    def vouchers_page(request: Request, db: Session = Depends(get_db)):
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        rows = (db.query(PaymentVoucher)
                .order_by(PaymentVoucher.id.desc()).all())
        return render(request, "vouchers.html", db, staff, active="vouchers",
                      rows=rows, people=[n for n, _i in _people(db)],
                      unpaid=sum((v.money for v in rows
                                  if v.status == VOUCHER_UNPAID), Decimal(0)),
                      can_pay=(getattr(staff, "role", "") == "admin"))

    @app.get("/admin/vouchers/new", response_class=HTMLResponse)
    def voucher_new(request: Request, who: str = "",
                    db: Session = Depends(get_db)):
        """Build one. Tick what goes on it; nothing is recalculated."""
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        people = _people(db)
        picked = (who or "").strip()
        staff_id = dict(people).get(picked)
        payouts, reports, adjustments = ([], [], [])
        if picked:
            payouts, reports, adjustments = _claimable(db, picked, staff_id)
        return render(request, "voucher_build.html", db, staff,
                      active="vouchers", people=[n for n, _i in people],
                      picked=picked, payouts=payouts, reports=reports,
                      adjustments=adjustments,
                      accounts=open_accounts(db),
                      ACCOUNT_KINDS=ACCOUNT_KINDS,
                      today=date.today().isoformat(),
                      can_pay=(getattr(staff, "role", "") == "admin"))

    def issue_voucher(db, staff, *, who, staff_id, payouts, reports,
                      adjustments, note="", batch=None):
        """One voucher: claim every piece, and freeze the figures.

        The claim and the freeze happen together on purpose. A voucher that
        recorded its total without claiming its parts could pay the same
        payout twice; one that claimed them without freezing would restate
        itself every time somebody edited an adjustment upstream.

        One function, so paying a person on their own and paying forty people
        in a run cannot drift apart on the rule that matters.
        """
        commission = sum((Decimal(str(p.total or 0)) for p in payouts),
                         Decimal(0))
        expenses = sum((r.total for r in reports), Decimal(0))
        adjust = sum((a.money for a in adjustments), Decimal(0))
        net = commission + expenses + adjust
        # A voucher never pays a negative number. If the deductions came to
        # more than everything else, it pays zero and the remainder is written
        # back as a fresh waiting adjustment — the same rule a payout follows,
        # so the money is neither forgiven nor taken twice.
        carry = Decimal(0)
        if net < 0:
            carry = net
            net = Decimal(0)

        voucher = PaymentVoucher(
            number=next_number(db, PaymentVoucher, "PV"),
            staff_id=staff_id, person=who, status=VOUCHER_UNPAID,
            issued_at=now_utc(), issued_by_id=getattr(staff, "id", None),
            commission_total=commission, expense_total=expenses,
            adjustment_total=adjust, total=net,
            batch=batch, note=(note or "").strip()[:800])
        db.add(voucher)
        db.flush()
        for p in payouts:
            p.voucher_id = voucher.id
        for r in reports:
            r.voucher_id = voucher.id
        for a in adjustments:
            a.voucher_id = voucher.id
            a.paid_at = now_utc()
        if carry:
            db.add(CommissionAdjustment(
                coach=who, coach_id=staff_id, occurred_on=date.today(),
                title="Carried from %s" % voucher.number,
                description="More was being deducted than this voucher could "
                            "cover. The remainder waits for the next one.",
                amount=carry, created_by_id=getattr(staff, "id", None)))
        return voucher

    def pay_voucher(db, voucher, *, on, method, reference, proof=None,
                    proof_mime=None):
        """Record that the money left. Shared by one voucher and by a run."""
        if voucher is None or voucher.status != VOUCHER_UNPAID:
            return False
        if proof:
            voucher.proof = proof
            voucher.proof_mime = proof_mime or "application/octet-stream"
        voucher.paid_on = on or date.today()
        voucher.method = method if method in PAY_METHODS else "Other"
        voucher.reference = (reference or "").strip()[:120]
        voucher.status = VOUCHER_PAID
        # The payouts on it are paid by the same act. One record of when the
        # money left, read by both screens, so they cannot disagree.
        for p in (db.query(CommissionPayout)
                  .filter(CommissionPayout.voucher_id == voucher.id)):
            p.status = "paid"
            p.paid_at = now_utc()
        return True

    @app.post("/admin/vouchers/new")
    async def voucher_issue(request: Request, db: Session = Depends(get_db)):
        """One person, with exactly the pieces that were ticked."""
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        form = await request.form()
        who = (form.get("who") or "").strip()
        if not who:
            return RedirectResponse("/admin/vouchers/new", status_code=303)
        back = "/admin/vouchers/new?who=%s" % who
        staff_id = dict(_people(db)).get(who)
        payouts, reports, adjustments = _claimable(db, who, staff_id)

        want_p = {int(v) for v in form.getlist("payout") if str(v).isdigit()}
        want_r = {int(v) for v in form.getlist("report") if str(v).isdigit()}
        want_a = {int(v) for v in form.getlist("adjustment") if str(v).isdigit()}
        take_p = [p for p in payouts if p.id in want_p]
        take_r = [r for r in reports if r.id in want_r]
        take_a = [a for a in adjustments if a.id in want_a]
        if not (take_p or take_r or take_a):
            return RedirectResponse(back + "&err=empty", status_code=303)

        voucher = issue_voucher(db, staff, who=who, staff_id=staff_id,
                                payouts=take_p, reports=take_r,
                                adjustments=take_a,
                                note=form.get("note") or "")
        db.commit()
        return RedirectResponse("/admin/vouchers/%d" % voucher.id,
                                status_code=303)

    # ── a pay run ──────────────────────────────────────────────────────
    # Everybody who is owed something, on one page. Registered before
    # /admin/vouchers/{vid}: that path takes an int, so "run" would not fall
    # through to here, it would simply be refused.

    def _periods(db) -> list:
        """The finalized runs a pay run can choose between, newest first."""
        return (db.query(CommissionRun)
                .filter(CommissionRun.status != RUN_DRAFT)
                .order_by(CommissionRun.id.desc()).all())

    def _run_rows(db, *, period="", reimb=True, adj=True) -> list:
        """One row per person who is owed anything under this rule.

        Built by asking the same `_claimable` the one-person screen asks, then
        narrowing it, so a run can never offer something that screen would
        refuse. Somebody owed nothing has no row at all — which is what makes
        an empty page mean "there is nothing to pay" rather than "something is
        filtered out".
        """
        rows = []
        for who, staff_id in _people(db):
            payouts, reports, adjustments = _claimable(db, who, staff_id)
            if period == "none":
                payouts = []
            elif period.isdigit():
                payouts = [p for p in payouts if p.run_id == int(period)]
            if not reimb:
                reports = []
            if not adj:
                adjustments = []
            if not (payouts or reports or adjustments):
                continue
            commission = sum((Decimal(str(p.total or 0)) for p in payouts),
                             Decimal(0))
            expenses = sum((r.total for r in reports), Decimal(0))
            adjusted = sum((a.money for a in adjustments), Decimal(0))
            net = commission + expenses + adjusted
            rows.append({
                "who": who, "staff_id": staff_id,
                "payouts": payouts, "reports": reports,
                "adjustments": adjustments,
                "commission": commission, "expenses": expenses,
                "adjusted": adjusted,
                # What the voucher will actually pay, and what it will have to
                # carry — said on the row rather than discovered afterwards.
                "net": net if net > 0 else Decimal(0),
                "carry": -net if net < 0 else Decimal(0),
            })
        return rows

    @app.get("/admin/vouchers/run", response_class=HTMLResponse)
    def pay_run(request: Request, period: str = "", reimb: str = "on",
                adj: str = "on", db: Session = Depends(get_db)):
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        periods = _periods(db)
        # Default to the newest finalized run, because that is the one being
        # paid nine times out of ten and nobody should have to pick it.
        picked = (period or "").strip()
        if not picked:
            picked = str(periods[0].id) if periods else "none"
        rows = _run_rows(db, period=picked, reimb=(reimb == "on"),
                         adj=(adj == "on"))
        return render(request, "pay_run.html", db, staff, active="vouchers",
                      rows=rows, periods=periods, picked=picked,
                      reimb=(reimb == "on"), adj=(adj == "on"),
                      today=date.today().isoformat(),
                      totals={
                          "commission": sum((r["commission"] for r in rows),
                                            Decimal(0)),
                          "expenses": sum((r["expenses"] for r in rows),
                                          Decimal(0)),
                          "adjusted": sum((r["adjusted"] for r in rows),
                                          Decimal(0)),
                          "net": sum((r["net"] for r in rows), Decimal(0)),
                      },
                      can_pay=(getattr(staff, "role", "") == "admin"))

    @app.post("/admin/vouchers/run")
    async def pay_run_issue(request: Request, db: Session = Depends(get_db)):
        """A voucher each, for everybody ticked.

        Several separate documents issued together, never one document with
        several people on it. Voiding one has to leave the others alone, and a
        person has to be able to open theirs without reading anybody else's.
        """
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        form = await request.form()
        picked = (form.get("period") or "none").strip()
        back = "/admin/vouchers/run?period=%s" % picked
        rows = _run_rows(db, period=picked,
                         reimb=(form.get("reimb") == "on"),
                         adj=(form.get("adj") == "on"))
        paying = set(form.getlist("pay"))
        # Every piece that is still ticked, as "kind:id:person". Unticking one
        # inside a row is an exception to the rule set at the top, so it is
        # carried as what remains rather than as what was removed.
        keep = set()
        for token in form.getlist("piece"):
            kind, _, rest = str(token).partition(":")
            ident, _, person = rest.partition(":")
            if ident.isdigit():
                keep.add((kind, int(ident), person))

        batch = next_number(db, PaymentVoucher, "PR", field="batch")
        made = 0
        for row in rows:
            if row["who"] not in paying:
                continue
            take_p = [p for p in row["payouts"]
                      if ("payout", p.id, row["who"]) in keep]
            take_r = [r for r in row["reports"]
                      if ("report", r.id, row["who"]) in keep]
            take_a = [a for a in row["adjustments"]
                      if ("adjustment", a.id, row["who"]) in keep]
            if not (take_p or take_r or take_a):
                continue
            issue_voucher(db, staff, who=row["who"], staff_id=row["staff_id"],
                          payouts=take_p, reports=take_r, adjustments=take_a,
                          batch=batch)
            made += 1
        if not made:
            return RedirectResponse(back + "&err=empty", status_code=303)
        db.commit()
        return RedirectResponse("/admin/vouchers/run/%s" % batch,
                                status_code=303)

    @app.get("/admin/vouchers/run/{batch}", response_class=HTMLResponse)
    def pay_run_done(request: Request, batch: str,
                     db: Session = Depends(get_db)):
        """One pay run, afterwards — and for ever after.

        Addressed by its own number rather than by a list of ids in the query
        string, so "show me the 15 October run" is a link somebody can keep.
        """
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        rows = (db.query(PaymentVoucher)
                .filter(PaymentVoucher.batch == batch)
                .order_by(PaymentVoucher.id.asc()).all())
        if not rows:
            return RedirectResponse("/admin/vouchers", status_code=303)
        return render(request, "pay_run_done.html", db, staff,
                      active="vouchers", batch=batch, rows=rows,
                      methods=PAY_METHODS, today=date.today().isoformat(),
                      links={v.id: _current_vlink(db, v.id) for v in rows},
                      mail_ready=Mailer().cfg.configured,
                      unpaid=[v for v in rows if v.status == VOUCHER_UNPAID],
                      due=sum((v.money for v in rows
                               if v.status == VOUCHER_UNPAID), Decimal(0)),
                      can_pay=(getattr(staff, "role", "") == "admin"))

    @app.post("/admin/vouchers/run/{batch}/pay")
    async def pay_run_pay(request: Request, batch: str,
                          db: Session = Depends(get_db)):
        """One bank run pays several people, so one reference covers them.

        Each voucher still records its own figure; what they share is when the
        money left and how it went.
        """
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        form = await request.form()
        want = {int(v) for v in form.getlist("voucher") if str(v).isdigit()}
        on = _day(form.get("on")) or date.today()
        method = (form.get("method") or "").strip()
        reference = form.get("reference") or ""
        for v in (db.query(PaymentVoucher)
                  .filter(PaymentVoucher.batch == batch)):
            if v.id in want:
                pay_voucher(db, v, on=on, method=method, reference=reference)
        db.commit()
        return RedirectResponse("/admin/vouchers/run/%s" % batch,
                                status_code=303)

    @app.get("/admin/vouchers/{vid}", response_class=HTMLResponse)
    def voucher_page(request: Request, vid: int,
                     db: Session = Depends(get_db)):
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        voucher = db.get(PaymentVoucher, vid)
        if voucher is None:
            return RedirectResponse("/admin/vouchers", status_code=303)
        payouts = (db.query(CommissionPayout)
                   .filter(CommissionPayout.voucher_id == vid).all())
        reports = (db.query(ExpenseReport)
                   .filter(ExpenseReport.voucher_id == vid).all())
        adjustments = (db.query(CommissionAdjustment)
                       .filter(CommissionAdjustment.voucher_id == vid).all())
        # What the books take from it. The commission goes to its own account
        # by definition; everything else carries the account it was filed to.
        lines = [l for r in reports for l in r.lines]
        rows = by_account(lines)
        return render(request, "voucher.html", db, staff, active="vouchers",
                      v=voucher, payouts=payouts, reports=reports,
                      adjustments=adjustments, groups=rows,
                      methods=PAY_METHODS,
                      today=date.today().isoformat(),
                      # Sending: the live link, where it went, and whether
                      # they have said anything back about it.
                      link=_current_vlink(db, vid),
                      base=base_url(request),
                      their_email=_their_email(db, voucher),
                      mail_ready=Mailer().cfg.configured,
                      can_pay=(getattr(staff, "role", "") == "admin"))

    @app.post("/admin/vouchers/{vid}/pay")
    async def voucher_pay(request: Request, vid: int,
                          db: Session = Depends(get_db)):
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        voucher = db.get(PaymentVoucher, vid)
        if voucher is None or voucher.status != VOUCHER_UNPAID:
            return RedirectResponse("/admin/vouchers", status_code=303)
        form = await request.form()
        blob = mime = None
        upload = form.get("proof")
        if isinstance(upload, UploadFile) and (upload.filename or ""):
            data = await upload.read()
            if len(data) <= RECEIPT_MAX:
                blob, mime = data, upload.content_type
        pay_voucher(db, voucher, on=_day(form.get("on")),
                    method=(form.get("method") or "").strip(),
                    reference=form.get("reference") or "",
                    proof=blob, proof_mime=mime)
        db.commit()
        return RedirectResponse("/admin/vouchers/%d" % vid, status_code=303)

    @app.post("/admin/vouchers/{vid}/void")
    def voucher_void(request: Request, vid: int,
                     db: Session = Depends(get_db)):
        """Release everything on it and keep the number.

        Void rather than delete: the number was issued, and a gap in a numbered
        series is a question nobody can answer a year later. The pieces go back
        to exactly where they were, so a corrected voucher can pick them up.
        """
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        voucher = db.get(PaymentVoucher, vid)
        if voucher is None or voucher.status == VOUCHER_VOID:
            return RedirectResponse("/admin/vouchers", status_code=303)
        for p in (db.query(CommissionPayout)
                  .filter(CommissionPayout.voucher_id == vid)):
            p.voucher_id = None
            p.status = "unpaid"
            p.paid_at = None
        for r in (db.query(ExpenseReport)
                  .filter(ExpenseReport.voucher_id == vid)):
            r.voucher_id = None
        for a in (db.query(CommissionAdjustment)
                  .filter(CommissionAdjustment.voucher_id == vid)):
            a.voucher_id = None
            a.paid_at = None
        # The remainder this voucher created, if it created one, goes with it.
        # Leaving it would deduct the same money twice on the next voucher.
        for a in (db.query(CommissionAdjustment)
                  .filter(CommissionAdjustment.title
                          == "Carried from %s" % voucher.number,
                          CommissionAdjustment.payout_id.is_(None),
                          CommissionAdjustment.voucher_id.is_(None))):
            db.delete(a)
        voucher.status = VOUCHER_VOID
        voucher.voided_at = now_utc()
        db.commit()
        return RedirectResponse("/admin/vouchers/%d" % vid, status_code=303)

    @app.get("/admin/vouchers/{vid}/proof")
    def voucher_proof(request: Request, vid: int,
                      db: Session = Depends(get_db)):
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        voucher = db.get(PaymentVoucher, vid)
        if voucher is None or not voucher.proof:
            return RedirectResponse("/admin/vouchers", status_code=303)
        return Response(content=voucher.proof,
                        media_type=voucher.proof_mime or "image/png",
                        headers={"Content-Disposition": "inline"})

    # ── sending it: a private link, and the email that carries it ──────
    #
    # A voucher is the one document where the business says "this is what we
    # paid you and this is why", so it is worth more than a figure in a
    # message. The email answers *how much*; the link answers *why*, a tab
    # each for the commission, the adjustments and the reimbursements, every
    # line and every receipt.
    #
    # A link rather than a login, for the reason the coach statement is one:
    # this is read on a phone, and a password somebody has to remember is a
    # page they will not open. The token is long and random, it expires, it
    # can be revoked, and it reaches exactly one person's one payment.

    def _vlinks_for(db, vid: int) -> list:
        return (db.query(VoucherLink).filter(VoucherLink.voucher_id == vid)
                .order_by(VoucherLink.id.desc()).all())

    def _current_vlink(db, vid: int):
        """The newest link for a voucher — older ones are kept, revoked."""
        rows = _vlinks_for(db, vid)
        return rows[0] if rows else None

    def _issue_vlink(db, vid: int, staff, now):
        """Mint a link and retire any earlier one.

        The old row stays so its URL keeps answering — with "a newer one was
        sent" rather than a bare not-found, which is what somebody clicking
        last month's email deserves.
        """
        for old in _vlinks_for(db, vid):
            if not old.revoked_at:
                old.revoked_at = now
        link = VoucherLink(voucher_id=vid, token=secrets.token_urlsafe(24),
                           created_at=now,
                           created_by_id=getattr(staff, "id", None),
                           expires_at=now + timedelta(days=VOUCHER_LINK_DAYS),
                           opens=0)
        db.add(link)
        return link

    def _their_email(db, voucher) -> str:
        """The address on their person record, if it looks usable."""
        person = None
        if voucher.staff_id:
            person = db.get(Staff, voucher.staff_id)
        if person is None:
            person = db.query(Staff).filter(Staff.name == voucher.person).first()
        email = (person.email or "").strip() if person and person.email else ""
        return email if looks_like_email(email) else ""

    def _voucher_email(voucher, url: str, expires):
        """The message itself.

        The totals are in it, so it answers "how much" without anybody
        opening anything. The button is for "why".
        """
        first = (voucher.person or "").split()[0] if voucher.person else "there"
        gone = expires.strftime("%d %B") if expires else ""
        when = voucher.paid_on.strftime("%d %B") if voucher.paid_on else ""
        paid = voucher.status == VOUCHER_PAID
        money = lambda v: "₱{:,.2f}".format(float(v or 0))
        subject = "Your payment from AWAKEN — %s" % voucher.number
        head = ("Your payment has gone out." if paid
                else "Your payment is being processed.")
        bits = []
        if voucher.commission_total:
            bits.append(("Commission", money(voucher.commission_total), False))
        if voucher.expense_total:
            bits.append(("Reimbursed", money(voucher.expense_total), False))
        if voucher.adjustment_total:
            bits.append(("Adjustments",
                         ("−" if voucher.adjustment_total < 0 else "")
                         + money(abs(voucher.adjustment_total)),
                         voucher.adjustment_total < 0))
        text = ("Hi %s,\n\n%s\n\nNet: %s\n" % (first, head, money(voucher.total))
                + "".join("%s: %s\n" % (b[0], b[1]) for b in bits)
                + (("\nPaid %s%s%s\n"
                    % (when, " by " + voucher.method if voucher.method else "",
                       " · " + voucher.reference if voucher.reference else ""))
                   if paid else "")
                + "\nEvery line behind that figure — commission, "
                  "adjustments and expense reports:\n%s\n\n" % url
                + ("The link works until %s.\n\n" % gone if gone else "")
                + "If something doesn't look right, just reply to this "
                  "message.\n\n— AWAKEN Fitness Center\n")
        rows = "".join(
            '<tr style="border-top:1px solid #eef1f4">'
            '<td style="padding:11px 18px;color:#6b7683">%s</td>'
            '<td style="padding:11px 18px;text-align:right;font-weight:650%s">%s</td>'
            '</tr>' % (label, ";color:#a8392b" if down else "", amount)
            for label, amount, down in bits)
        html = """\
<!DOCTYPE html><html><body style="margin:0;padding:0;background:#eef1f4">
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="background:#eef1f4;padding:26px 12px">
<tr><td align="center">
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#fff;border-radius:12px;overflow:hidden;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<tr><td style="background:#132a44;padding:20px 26px">
  <img src="cid:%(cid)s" alt="AWAKEN" width="142" height="38"
       style="display:block;border:0;outline:none;text-decoration:none;height:auto;width:142px;max-width:142px">
  <div style="color:#fff;font-size:19px;font-weight:650;margin-top:12px">%(number)s</div>
</td></tr>
<tr><td style="padding:24px 26px 6px;color:#22303d;font-size:15px;line-height:1.55">
  <p style="margin:0 0 14px">Hi %(first)s,</p>
  <p style="margin:0 0 18px">%(head)s</p>
  <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="border:1px solid #e4e8ed;border-radius:9px;border-collapse:separate;overflow:hidden;font-size:13.5px">
  <tr><td colspan="2" style="background:#f8fafb;padding:14px 18px;text-align:center;border-bottom:1px solid #e4e8ed">
    <div style="font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#6b7683">Net</div>
    <div style="font-size:32px;font-weight:700;letter-spacing:-.02em;color:#1a232e;margin-top:2px">%(net)s</div>
    %(paidline)s
  </td></tr>
  %(rows)s
  </table>
  <p style="margin:20px 0 0;text-align:center">
    <a href="%(url)s" style="display:inline-block;background:#008080;color:#fff;text-decoration:none;
       font-weight:650;font-size:15px;padding:14px 30px;border-radius:8px">Open the voucher</a>
  </p>
  <p style="margin:11px 0 0;text-align:center;color:#7c8794;font-size:12.5px">Commission, adjustments
    and expense reports, line by line. No login.%(until)s</p>
  <p style="margin:20px 0 0;padding-top:16px;border-top:1px solid #eef1f4;color:#5b6773;font-size:13.5px">
    Something wrong? Reply to this email.</p>
</td></tr>
<tr><td style="padding:20px 26px 24px;color:#96a0ab;font-size:11.5px">
  AWAKEN Fitness Center · this shows only your own payment.
</td></tr>
</table></td></tr></table></body></html>""" % {
            "cid": LOGO_CID, "number": voucher.number, "first": first,
            "head": head, "net": money(voucher.total), "rows": rows,
            "url": url,
            "paidline": ('<div style="font-size:12.5px;color:#6b7683;margin-top:3px">%s</div>'
                         % " · ".join(x for x in
                                           [when, (voucher.method or "").lower(),
                                            voucher.reference or ""] if x)
                         if paid else ""),
            "until": (' The link works until <b style="color:#1a232e">%s</b>.' % gone
                      if gone else ""),
        }
        return subject, text, html

    def _send_voucher(db, voucher, staff, now, base, mailer, force=False):
        """Make sure they have a live link, then email it.

        Returns (status, detail) where status is sent / skipped / failed.
        """
        if voucher.status == VOUCHER_VOID:
            return "skipped", "voided"
        email = _their_email(db, voucher)
        if not email:
            return "failed", "no email address on their record"
        link = _current_vlink(db, voucher.id)
        if link is None or not link.is_live:
            link = _issue_vlink(db, voucher.id, staff, now)
            db.flush()
        elif link.sent_at and not force:
            # Pressing the button twice shouldn't mail everybody twice.
            return "skipped", "already sent"
        subject, text, html = _voucher_email(
            voucher, "%s/v/%s" % (base, link.token), link.expires_at)
        ok, why = mailer.send(email, subject, text, html,
                             inline={LOGO_CID: _logo_bytes()})
        if not ok:
            return "failed", why
        link.sent_to = email
        link.sent_at = now
        return "sent", email

    def _tally(dest: str, out: dict):
        """Carry the result of a send in the URL rather than in a session.

        A send is a thing that happened to somebody else's inbox; it should
        survive a refresh and be forwardable to whoever asks "did Julio get
        his?", and a flash message in a cookie does neither.
        """
        bits = ["%s=%d" % (k, len(v)) for k, v in out.items() if v]
        if out.get("failed"):
            bits.append("why=" + out["failed"][0][1].replace(" ", "+")[:80])
        if not bits:
            bits = ["sent=0"]
        return dest + ("&" if "?" in dest else "?") + "&".join(bits)

    @app.post("/admin/vouchers/{vid}/link")
    def voucher_link(request: Request, vid: int, db: Session = Depends(get_db)):
        """Make a link without emailing it — for pasting into a chat."""
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        voucher = db.get(PaymentVoucher, vid)
        if voucher is None:
            return RedirectResponse("/admin/vouchers", status_code=303)
        current = _current_vlink(db, vid)
        if current is None or not current.is_live:
            _issue_vlink(db, vid, staff, now_utc())
            db.commit()
        return RedirectResponse("/admin/vouchers/%d" % vid, status_code=303)

    @app.post("/admin/vouchers/{vid}/link/revoke")
    def voucher_link_revoke(request: Request, vid: int,
                            db: Session = Depends(get_db)):
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        now = now_utc()
        for link in _vlinks_for(db, vid):
            if not link.revoked_at:
                link.revoked_at = now
        db.commit()
        return RedirectResponse("/admin/vouchers/%d" % vid, status_code=303)

    @app.post("/admin/vouchers/{vid}/send")
    def voucher_send(request: Request, vid: int, force: str = "",
                     db: Session = Depends(get_db)):
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        voucher = db.get(PaymentVoucher, vid)
        if voucher is None:
            return RedirectResponse("/admin/vouchers", status_code=303)
        dest = "/admin/vouchers/%d" % vid
        mailer = Mailer()
        if not mailer.cfg.configured:
            return RedirectResponse(
                dest + "?setup=" + "+".join(mailer.cfg.missing),
                status_code=303)
        status, detail = _send_voucher(db, voucher, staff, now_utc(),
                                       base_url(request), mailer,
                                       force=bool(force))
        db.commit()
        out = {"sent": [], "skipped": [], "failed": []}
        out[status].append((voucher.person, detail))
        return RedirectResponse(_tally(dest, out), status_code=303)

    @app.post("/admin/vouchers/run/{batch}/send")
    def pay_run_send(request: Request, batch: str, force: str = "",
                     db: Session = Depends(get_db)):
        """Email everybody in one pay run their own voucher.

        One at a time, committing each: one person with no address on their
        record must not stop the other eleven going out.
        """
        staff, redir = money_guard(request, db)
        if redir:
            return redir
        rows = (db.query(PaymentVoucher).filter(PaymentVoucher.batch == batch)
                .order_by(PaymentVoucher.id.asc()).all())
        if not rows:
            return RedirectResponse("/admin/vouchers", status_code=303)
        dest = "/admin/vouchers/run/%s" % batch
        mailer = Mailer()
        if not mailer.cfg.configured:
            return RedirectResponse(
                dest + "?setup=" + "+".join(mailer.cfg.missing),
                status_code=303)
        now, base = now_utc(), base_url(request)
        out = {"sent": [], "skipped": [], "failed": []}
        for v in rows:
            status, detail = _send_voucher(db, v, staff, now, base, mailer,
                                           force=bool(force))
            out[status].append((v.person, detail))
            db.commit()
        return RedirectResponse(_tally(dest, out), status_code=303)

    # ── the page they open ─────────────────────────────────────────────

    def _session_row(line) -> dict:
        """One payout line as the person who was paid wants to read it.

        The session and the client, and what it came to. Not the rate: the
        percentage and the price the gym charged are the business's side of
        the arrangement, and a document whose job is "here is your money"
        does not need to restate the deal on every row.

        The client comes off the booking where the booking is still there, and
        otherwise off the description it was written into when the run was
        finalized — which is the whole reason that description carries it.
        """
        parts = [b.strip() for b in (line.description or "").split("\u00b7")]
        if (line.basis or "") == "Adjustment":
            return {"on": line.occurred_on, "what": line.description or "",
                    "client": "", "amount": Decimal(str(line.amount or 0))}
        client = ""
        if line.booking is not None and line.booking.customer:
            client = line.booking.customer
        elif len(parts) > 1:
            client = parts[1]
        return {"on": line.occurred_on, "what": parts[0] if parts else "",
                "client": client, "amount": Decimal(str(line.amount or 0))}

    def _public_context(db, voucher) -> dict:
        """One voucher, in three tabs, with nothing else reachable from it.

        Built from what the voucher already claimed rather than by asking the
        question again: these are the very rows whose `voucher_id` is this
        voucher, so the page cannot drift from the figure that was paid.
        """
        payouts = (db.query(CommissionPayout)
                   .filter(CommissionPayout.voucher_id == voucher.id)
                   .order_by(CommissionPayout.id.asc()).all())
        reports = (db.query(ExpenseReport)
                   .filter(ExpenseReport.voucher_id == voucher.id)
                   .order_by(ExpenseReport.id.asc()).all())
        adjustments = (db.query(CommissionAdjustment)
                       .filter(CommissionAdjustment.voucher_id == voucher.id)
                       .order_by(CommissionAdjustment.occurred_on.asc()
                                 .nullsfirst(),
                                 CommissionAdjustment.id.asc()).all())
        # The sessions behind each commission figure — the payout's own lines,
        # which were written when the run was finalized and have said the same
        # thing ever since. Reading the run again would be asking a question
        # whose answer has already been given and paid.
        blocks = [{"payout": p,
                   "period": p.period_label or "",
                   "rows": [_session_row(l) for l in
                            sorted(p.lines, key=lambda l: (l.occurred_on
                                                           or date.min, l.id))]}
                  for p in payouts]
        return {
            "v": voucher, "payouts": payouts, "reports": reports,
            "adjustments": adjustments, "blocks": blocks,
            "commission": sum((Decimal(str(p.total or 0)) for p in payouts),
                              Decimal(0)),
            "expenses": sum((r.total for r in reports), Decimal(0)),
            "adjusted": sum((a.money for a in adjustments), Decimal(0)),
        }

    @app.get("/v/{token}", response_class=HTMLResponse)
    def public_voucher(request: Request, token: str,
                       db: Session = Depends(get_db)):
        """Their own voucher. No login — the token is the credential.

        Deliberately outside the app's auth: everything it can reach is one
        person's one payment, and the only thing it can change is whether
        they have said it looks right.
        """
        link = db.query(VoucherLink).filter(VoucherLink.token == token).first()
        if link is None:
            return templates.TemplateResponse(
                "voucher_gone.html",
                {"request": request, "reason": "unknown"}, status_code=404)
        if not link.is_live:
            return templates.TemplateResponse(
                "voucher_gone.html",
                {"request": request,
                 "reason": "revoked" if link.revoked_at else "expired",
                 "days": VOUCHER_LINK_DAYS}, status_code=410)
        now = now_utc()
        link.opens = (link.opens or 0) + 1
        link.first_opened_at = link.first_opened_at or now
        link.last_opened_at = now
        db.commit()
        ctx = _public_context(db, link.voucher)
        ctx.update({"request": request, "link": link,
                    "said": request.query_params.get("said", "")})
        return templates.TemplateResponse("voucher_public.html", ctx)

    @app.get("/v/{token}/receipt/{lid}")
    def public_receipt(request: Request, token: str, lid: int,
                       db: Session = Depends(get_db)):
        """A receipt from a report on this voucher, and only from one.

        The line is found through the voucher rather than by id, so changing
        the number in the URL reaches nothing.
        """
        link = db.query(VoucherLink).filter(VoucherLink.token == token).first()
        if link is None or not link.is_live:
            return Response(status_code=404)
        line = db.get(ExpenseLine, lid)
        if line is None or not line.receipt:
            return Response(status_code=404)
        report = db.get(ExpenseReport, line.report_id)
        if report is None or report.voucher_id != link.voucher_id:
            return Response(status_code=404)
        return Response(content=line.receipt,
                        media_type=line.receipt_mime or "application/octet-stream",
                        headers={"Content-Disposition": 'inline; filename="%s"'
                                 % (line.receipt_name or "receipt")})

    @app.post("/v/{token}/ack")
    async def public_ack(request: Request, token: str,
                         db: Session = Depends(get_db)):
        """"Yes" or "there is a problem", from the person who was paid.

        It raises a hand; it moves no money. A voucher that reversed itself
        because somebody tapped the wrong button on a phone would be worse
        than one nobody could question.
        """
        link = db.query(VoucherLink).filter(VoucherLink.token == token).first()
        if link is None or not link.is_live:
            return RedirectResponse("/v/%s" % token, status_code=303)
        form = await request.form()
        ok = (form.get("ok") or "") == "yes"
        link.acked_at = now_utc()
        link.ack_ok = ok
        link.ack_note = (form.get("note") or "").strip()[:2000]
        db.commit()
        return RedirectResponse("/v/%s?said=%s" % (token, "yes" if ok else "no"),
                                status_code=303)
