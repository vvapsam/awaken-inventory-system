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

from datetime import date, datetime, timezone
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
from .models import (
    Account, ACCOUNT_KINDS,
    CommissionAdjustment, CommissionCoachRate, CommissionPayout,
    CommissionRun, RUN_DRAFT,
    EXPENSE_APPROVED, EXPENSE_DRAFT, EXPENSE_RETURNED, EXPENSE_STATUSES,
    EXPENSE_SUBMITTED, ExpenseLine, ExpenseReport,
    PaymentVoucher, Staff,
    VOUCHER_PAID, VOUCHER_UNPAID, VOUCHER_VOID,
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
