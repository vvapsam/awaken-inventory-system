"""Reimbursements, and the voucher that pays somebody everything at once.

The two rules worth testing hardest are the ones that lose money when they
break: a line cannot exist without its receipt, and nothing can be paid twice.
"""
import os, io
os.environ["DATABASE_URL"] = ("postgresql+psycopg2://postgres@/exp"
                              "?host=/home/claude/pgrun&port=5433")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_INITIAL_PIN", "123456")
from datetime import date
from decimal import Decimal
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import Base, engine
from app import models as M
from fastapi.testclient import TestClient
from app.main import app

res = []
def ck(n, c):
    res.append((n, bool(c)))
    print("PASS" if c else "FAIL", n)

with engine.begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 400 + b"\xff\xd9"


def receipt(name="grab.jpg", blob=JPEG, mime="image/jpeg"):
    return {"receipt": (name, blob, mime)}


with TestClient(app) as c:                      # startup seeds the chart
    with Session(engine) as db:
        acc = {a.name: a.id for a in db.query(M.Account)}
        TRANSPORT = acc["Transportation / Delivery"]
        CONSUM = acc["Consumables"]
        # One coach who is also staff, and one person who is only staff.
        julio = M.Staff(name="Julio Reyes", person_type="staff", role="staff",
                        username="julio", is_active=True,
                        permissions="view_stock")
        db.add(julio); db.flush()
        db.add(M.CommissionCoachRate(coach="Julio Reyes", coach_id=julio.id,
                                     staff_raw="Julio", rate_type="percent",
                                     rate_value=0.4))
        db.commit()
        JULIO = julio.id
    # Give him a PIN we know.
    with Session(engine) as db:
        from app.auth import hash_pin
        s = db.get(M.Staff, JULIO)
        s.pin_hash, s.pin_salt = hash_pin("4321")
        s.has_access = True
        db.commit()

    # ── the person's own report ────────────────────────────────────────
    login = c.post("/login", data={"username": "julio", "pin": "4321"},
                   follow_redirects=False)
    ck("a plain member of staff can log in", login.status_code == 303)
    ck("and reaches their own expenses",
       "My expenses" in c.get("/expenses").text)

    c.post("/expenses/new", data={"on": "2026-10-04"}, follow_redirects=False)
    with Session(engine) as db:
        r = db.query(M.ExpenseReport).one()
        ck("a report is numbered", r.number == "ER-0001")
        ck("it belongs to the person who made it", r.staff_id == JULIO
           and r.person == "Julio Reyes")
        ck("it starts as a draft", r.status == M.EXPENSE_DRAFT)
        RID = r.id

    # A line with no receipt is refused.
    out = c.post("/expenses/%d/line" % RID,
                 data={"on": "2026-10-02", "account": str(TRANSPORT),
                       "amount": "480", "note": "Grab to the venue"},
                 follow_redirects=False)
    ck("no receipt, no line", "err=receipt" in out.headers["location"])
    with Session(engine) as db:
        ck("and nothing was written", db.query(M.ExpenseLine).count() == 0)

    # With one, it saves.
    c.post("/expenses/%d/line" % RID,
           data={"on": "2026-10-02", "account": str(TRANSPORT),
                 "amount": "480", "note": "Grab to the venue"},
           files=receipt(), follow_redirects=False)
    c.post("/expenses/%d/line" % RID,
           data={"on": "2026-10-03", "account": str(CONSUM),
                 "amount": "1,200.00", "note": "Water and ice"},
           files=receipt("sm.pdf", b"%PDF-1.4 x", "application/pdf"),
           follow_redirects=False)
    with Session(engine) as db:
        r = db.get(M.ExpenseReport, RID)
        ck("both lines saved", len(r.lines) == 2)
        ck("the total adds up", r.total == Decimal("1680.00"))
        ck("the receipt itself is on the row",
           all(l.receipt and l.receipt_mime for l in r.lines))
        ck("the amount parses through a comma",
           sorted(str(l.amount) for l in r.lines) == ["1200.00", "480.00"])
        LID = r.lines[0].id

    # No account, no line either.
    out = c.post("/expenses/%d/line" % RID,
                 data={"on": "2026-10-04", "account": "", "amount": "90"},
                 files=receipt(), follow_redirects=False)
    ck("a line needs an account", "err=missing" in out.headers["location"])

    ck("the person can open their own receipt",
       c.get("/expenses/%d/receipt/%d" % (RID, LID)).status_code == 200)

    c.post("/expenses/%d/submit" % RID, follow_redirects=False)
    with Session(engine) as db:
        r = db.get(M.ExpenseReport, RID)
        ck("submitting marks it pending", r.status == M.EXPENSE_SUBMITTED
           and r.submitted_at is not None)
        ck("and it is still editable", r.editable is True)

    # Still editable while pending — that is the whole promise.
    c.post("/expenses/%d/line" % RID,
           data={"on": "2026-10-04", "account": str(CONSUM), "amount": "300"},
           files=receipt(), follow_redirects=False)
    with Session(engine) as db:
        ck("a pending report still takes a line",
           len(db.get(M.ExpenseReport, RID).lines) == 3)

    # ── somebody else's ────────────────────────────────────────────────
    c.post("/logout")
    c.post("/login", data={"username": "admin", "pin": "123456"})
    with Session(engine) as db:
        other = (db.query(M.ExpenseReport).count())
    c.post("/expenses/new", follow_redirects=False)
    with Session(engine) as db:
        mine = (db.query(M.ExpenseReport)
                .filter(M.ExpenseReport.staff_id != JULIO).one())
        ck("an admin's own report is their own", mine.person != "Julio Reyes")
        ADMIN_RID = mine.id
    # The office may open somebody else's claim once it has been sent in —
    # that is the point of the office. A draft they are still writing is
    # theirs even from here.
    out = c.get("/expenses/%d" % RID, follow_redirects=False)
    ck("the office can open a submitted claim of somebody else's",
       out.status_code == 200)

    # ── the office starts one for somebody ─────────────────────────────
    c.post("/admin/expenses/new",
           data={"who": "Julio Reyes", "on": "2026-10-06"},
           follow_redirects=False)
    with Session(engine) as db:
        made = (db.query(M.ExpenseReport)
                .order_by(M.ExpenseReport.id.desc()).first())
        ck("it belongs to the person, not to whoever typed it",
           made.staff_id == JULIO and made.person == "Julio Reyes")
        ck("and the trail says who typed it",
           made.raised_for_them is True and made.created_by_id != JULIO)
        ck("the office can see its own unfinished one",
           made.office_visible is True)
        OFFICE_RID = made.id
    c.post("/expenses/%d/line" % OFFICE_RID,
           data={"on": "2026-10-06", "account": str(TRANSPORT),
                 "amount": "250", "note": "Paper receipt handed over"},
           files=receipt(), follow_redirects=False)
    with Session(engine) as db:
        ck("the office can put a line on it",
           len(db.get(M.ExpenseReport, OFFICE_RID).lines) == 1)
    page = c.get("/admin/expenses?show=writing").text
    ck("an office-raised draft is findable again", "ER-000" in page
       and "You are writing" in page)
    c.post("/expenses/%d/submit" % OFFICE_RID, follow_redirects=False)
    c.post("/admin/expenses/%d/approve" % OFFICE_RID, follow_redirects=False)
    with Session(engine) as db:
        ck("and it approves like any other",
           db.get(M.ExpenseReport, OFFICE_RID).status == M.EXPENSE_APPROVED)

    # Somebody else's private draft is still private.
    c.post("/logout")
    c.post("/login", data={"username": "julio", "pin": "4321"})
    c.post("/expenses/new", follow_redirects=False)
    with Session(engine) as db:
        secret = (db.query(M.ExpenseReport)
                  .filter_by(staff_id=JULIO, status=M.EXPENSE_DRAFT)
                  .order_by(M.ExpenseReport.id.desc()).first()).id
        ck("a self-started draft is not office-visible",
           db.get(M.ExpenseReport, secret).office_visible is False)
    c.post("/logout")
    c.post("/login", data={"username": "admin", "pin": "123456"})
    out = c.get("/expenses/%d" % secret, follow_redirects=False)
    ck("the office cannot open a draft somebody is writing for themselves",
       out.status_code == 303 and out.headers["location"] == "/expenses")
    ck("and it is not in the office's list",
       "ER-%04d" % 0 not in "" and
       ('/expenses/%d"' % secret) not in c.get("/admin/expenses?show=all").text)

    # ── the office ─────────────────────────────────────────────────────
    page = c.get("/admin/expenses").text
    ck("the office sees the submitted one", "ER-0001" in page)
    ck("a draft never reaches the office",
       ("ER-0002" not in page))

    out = c.post("/admin/expenses/%d/return" % RID,
                 data={"note": "The Grab receipt is for the 1st."},
                 follow_redirects=False)
    with Session(engine) as db:
        r = db.get(M.ExpenseReport, RID)
        ck("returning writes the note", r.status == M.EXPENSE_RETURNED
           and "1st" in r.review_note)

    # Retag a line: the person picked, the books are the office's.
    c.post("/admin/expenses/%d/line/%d/account" % (RID, LID),
           data={"account": str(CONSUM)}, follow_redirects=False)
    with Session(engine) as db:
        ck("the office can move a line to another account",
           db.get(M.ExpenseLine, LID).account_id == CONSUM)
    c.post("/admin/expenses/%d/line/%d/account" % (RID, LID),
           data={"account": str(TRANSPORT)}, follow_redirects=False)

    c.post("/admin/expenses/%d/approve" % RID, follow_redirects=False)
    with Session(engine) as db:
        r = db.get(M.ExpenseReport, RID)
        ck("approving locks it", r.status == M.EXPENSE_APPROVED
           and r.editable is False)
        ck("and it becomes payable", r.payable is True)
        TOTAL = r.total

    c.post("/logout")
    c.post("/login", data={"username": "julio", "pin": "4321"})
    out = c.post("/expenses/%d/line" % RID,
                 data={"on": "2026-10-05", "account": str(CONSUM),
                       "amount": "50"},
                 files=receipt(), follow_redirects=False)
    ck("an approved report takes nothing more",
       "err=locked" in out.headers["location"])
    c.post("/logout")
    c.post("/login", data={"username": "admin", "pin": "123456"})

    # ── a commission run to pay alongside it ───────────────────────────
    with Session(engine) as db:
        run = M.CommissionRun(period="Sep 2026", status=M.RUN_FINALIZED)
        db.add(run); db.flush()
        pay = M.CommissionPayout(run_id=run.id, number="COM-0001",
                                 coach="Julio Reyes", coach_id=JULIO,
                                 period_label="September 2026", sessions=38,
                                 commission_total=Decimal("22800"),
                                 total=Decimal("22800"))
        db.add(pay); db.flush()
        db.add(M.CommissionAdjustment(
            coach="Julio Reyes", coach_id=JULIO, occurred_on=date(2026, 7, 14),
            title="Overpaid July", amount=Decimal("-1500"),
            account_id=acc["Commissions"]))
        db.commit()
        RUN, PAYOUT = run.id, pay.id

    build = c.get("/admin/vouchers/new?who=Julio%20Reyes").text
    ck("the build screen offers the run", "COM-0001" in build)
    ck("it offers the approved report", "ER-0001" in build)
    ck("it offers the waiting adjustment", "Overpaid July" in build)

    with Session(engine) as db:
        AID = db.query(M.CommissionAdjustment).one().id

    # A dict of lists, not a list of tuples: this httpx sends the latter as an
    # empty body, which looks exactly like a form nobody filled in.
    c.post("/admin/vouchers/new",
           data={"who": "Julio Reyes", "payout": [str(PAYOUT)],
                 "report": [str(RID)], "adjustment": [str(AID)]},
           follow_redirects=False)
    with Session(engine) as db:
        v = db.query(M.PaymentVoucher).one()
        ck("the voucher is numbered", v.number == "PV-0001")
        ck("the three totals are frozen on it",
           v.commission_total == Decimal("22800")
           and v.expense_total == TOTAL
           and v.adjustment_total == Decimal("-1500"))
        ck("the net is their sum",
           v.total == Decimal("22800") + TOTAL - Decimal("1500"))
        ck("the payout is claimed",
           db.get(M.CommissionPayout, PAYOUT).voucher_id == v.id)
        ck("the report is claimed",
           db.get(M.ExpenseReport, RID).voucher_id == v.id)
        ck("the adjustment is claimed",
           db.get(M.CommissionAdjustment, AID).voucher_id == v.id)
        VID = v.id

    # Nothing can be paid twice.
    # Against the tick boxes, not the words: the page also carries an
    # add-an-adjustment form whose placeholder happens to read "Overpaid July".
    build = c.get("/admin/vouchers/new?who=Julio%20Reyes").text
    ck("a claimed run is off the next voucher",
       'name="payout" value="%d"' % PAYOUT not in build)
    ck("a claimed report is off it too",
       'name="report" value="%d"' % RID not in build)
    ck("a claimed adjustment is off it too",
       'name="adjustment" value="%d"' % AID not in build)
    with Session(engine) as db:
        from app.commission_routes import waiting_adjustments
        ck("and it stops being offered on the next run",
           waiting_adjustments(db, "Julio Reyes") == [])

    # Reopening the run it came from is refused.
    out = c.post("/commissions/%d/reopen" % RUN, follow_redirects=False)
    with Session(engine) as db:
        run = db.get(M.CommissionRun, RUN)
        ck("reopening a run on a voucher is refused",
           run.status == M.RUN_FINALIZED
           and "voucher" in (run.last_import_note or ""))

    c.post("/admin/vouchers/%d/pay" % VID,
           data={"on": "2026-10-15", "method": "Bank transfer",
                 "reference": "BPI 5512"}, follow_redirects=False)
    with Session(engine) as db:
        v = db.get(M.PaymentVoucher, VID)
        ck("paying records how and when", v.status == M.VOUCHER_PAID
           and v.method == "Bank transfer" and v.reference == "BPI 5512")
        ck("and marks the commission payout paid",
           db.get(M.CommissionPayout, PAYOUT).status == "paid")

    # Voiding puts every piece back.
    c.post("/admin/vouchers/%d/void" % VID, follow_redirects=False)
    with Session(engine) as db:
        ck("voiding keeps the number",
           db.get(M.PaymentVoucher, VID).number == "PV-0001")
        ck("the payout goes back to unpaid and unclaimed",
           db.get(M.CommissionPayout, PAYOUT).voucher_id is None
           and db.get(M.CommissionPayout, PAYOUT).status == "unpaid")
        ck("the report is claimable again",
           db.get(M.ExpenseReport, RID).voucher_id is None)
        ck("the adjustment waits again",
           db.get(M.CommissionAdjustment, AID).voucher_id is None)
    build = c.get("/admin/vouchers/new?who=Julio%20Reyes").text
    ck("and all three are offered again",
       'name="payout" value="%d"' % PAYOUT in build
       and 'name="report" value="%d"' % RID in build
       and 'name="adjustment" value="%d"' % AID in build)

    # A new voucher takes the next number, never the voided one's.
    c.post("/admin/vouchers/new",
           data={"who": "Julio Reyes", "payout": [str(PAYOUT)]},
           follow_redirects=False)
    with Session(engine) as db:
        nums = sorted(v.number for v in db.query(M.PaymentVoucher))
        ck("the series has no reused number", nums == ["PV-0001", "PV-0002"])
        VID2 = (db.query(M.PaymentVoucher).filter_by(number="PV-0002")
                .one()).id

    # ── putting a voided one back ──────────────────────────────────────
    # PV-0002 took the payout that PV-0001 let go of, so PV-0001 cannot
    # simply resume: half of it would be a voucher whose total no longer
    # matches what is on it.
    out = c.post("/admin/vouchers/%d/restore" % VID, follow_redirects=False)
    ck("it will not go back over something already claimed",
       "err=taken" in out.headers["location"])
    with Session(engine) as db:
        ck("and nothing moved",
           db.get(M.PaymentVoucher, VID).status == M.VOUCHER_VOID
           and db.get(M.CommissionPayout, PAYOUT).voucher_id == VID2)

    # Void the one that took it, and the way is clear.
    c.post("/admin/vouchers/%d/void" % VID2, follow_redirects=False)
    c.post("/admin/vouchers/%d/restore" % VID, follow_redirects=False)
    with Session(engine) as db:
        v = db.get(M.PaymentVoucher, VID)
        ck("a voided voucher goes back to unpaid",
           v.status == M.VOUCHER_UNPAID and v.voided_at is None)
        ck("under its own number", v.number == "PV-0001")
        ck("holding exactly what it held",
           db.get(M.CommissionPayout, PAYOUT).voucher_id == VID
           and db.get(M.ExpenseReport, RID).voucher_id == VID
           and db.get(M.CommissionAdjustment, AID).voucher_id == VID)
        ck("with the figures worked out again",
           v.total == Decimal("22800") + TOTAL - Decimal("1500"))
        ck("and nothing left to undo twice", v.released is None)
    out = c.post("/admin/vouchers/%d/restore" % VID, follow_redirects=False)
    ck("putting back one that is not void does nothing",
       out.headers["location"] == "/admin/vouchers/%d" % VID)

    # ── changing what is on an issued voucher ──────────────────────────
    page = c.get("/admin/vouchers/%d/edit" % VID)
    ck("an unpaid voucher can be edited", page.status_code == 200)
    ck("and everything on it starts ticked",
       page.text.count('name="payout" value="%d"' % PAYOUT) == 1
       and page.text.count('name="report" value="%d"' % RID) == 1
       and page.text.count('name="adjustment" value="%d"' % AID) == 1
       and page.text.count("checked") >= 3)

    # Drop the report and the adjustment; keep the commission.
    c.post("/admin/vouchers/%d/edit" % VID,
           data={"payout": [str(PAYOUT)]}, follow_redirects=False)
    with Session(engine) as db:
        v = db.get(M.PaymentVoucher, VID)
        ck("what was unticked goes back where it came from",
           db.get(M.ExpenseReport, RID).voucher_id is None
           and db.get(M.CommissionAdjustment, AID).voucher_id is None)
        ck("what was left stays on it",
           db.get(M.CommissionPayout, PAYOUT).voucher_id == VID)
        ck("and the total is worked out again",
           v.total == Decimal("22800") and v.expense_total == Decimal(0)
           and v.adjustment_total == Decimal(0))
        ck("the number is untouched", v.number == "PV-0001")

    # Put them back on.
    c.post("/admin/vouchers/%d/edit" % VID,
           data={"payout": [str(PAYOUT)], "report": [str(RID)],
                 "adjustment": [str(AID)]}, follow_redirects=False)
    with Session(engine) as db:
        ck("and they can be added again",
           db.get(M.PaymentVoucher, VID).total
           == Decimal("22800") + TOTAL - Decimal("1500"))

    out = c.post("/admin/vouchers/%d/edit" % VID, data={},
                 follow_redirects=False)
    ck("an edit that empties it is refused",
       "err=empty" in out.headers["location"])
    with Session(engine) as db:
        ck("and it still holds everything",
           db.get(M.CommissionPayout, PAYOUT).voucher_id == VID)

    # Once the money has gone, it is no longer open to this.
    c.post("/admin/vouchers/%d/pay" % VID,
           data={"on": "2026-10-15", "method": "Cash", "reference": "x"},
           follow_redirects=False)
    ck("a paid voucher cannot be edited",
       c.get("/admin/vouchers/%d/edit" % VID,
             follow_redirects=False).status_code == 303)
    out = c.post("/admin/vouchers/%d/edit" % VID,
                 data={"payout": [str(PAYOUT)]}, follow_redirects=False)
    ck("not even by posting at it",
       out.headers["location"] == "/admin/vouchers/%d" % VID)
    # Back to where the rest of the suite expects it.
    c.post("/admin/vouchers/%d/void" % VID, follow_redirects=False)

    # ── a deduction bigger than everything else ────────────────────────
    with Session(engine) as db:
        db.add(M.CommissionAdjustment(
            coach="Julio Reyes", coach_id=JULIO, occurred_on=date(2026, 9, 1),
            title="Laptop at cost", amount=Decimal("-9000"),
            account_id=acc["Retail"]))
        db.commit()
        BIG = (db.query(M.CommissionAdjustment)
               .filter_by(title="Laptop at cost").one().id)
    c.post("/admin/vouchers/new",
           data={"who": "Julio Reyes", "adjustment": [str(BIG)]},
           follow_redirects=False)
    with Session(engine) as db:
        v = (db.query(M.PaymentVoucher)
             .order_by(M.PaymentVoucher.id.desc()).first())
        ck("a voucher never pays a negative number", v.total == Decimal(0))
        carried = (db.query(M.CommissionAdjustment)
                   .filter_by(title="Carried from %s" % v.number).one())
        ck("the remainder carries to the next one",
           carried.amount == Decimal("-9000"))
        VBIG = v.id
    c.post("/admin/vouchers/%d/void" % VBIG, follow_redirects=False)
    with Session(engine) as db:
        ck("voiding takes the carry with it",
           db.query(M.CommissionAdjustment)
           .filter(M.CommissionAdjustment.title.like("Carried from%"))
           .count() == 0)

    # ── a pay run: everybody at once ───────────────────────────────────
    # Two more people owed something, so the run has a table rather than a row.
    with Session(engine) as db:
        run2 = db.query(M.CommissionRun).first()
        chriz = M.Staff(name="Chrizel Urbino", person_type="staff",
                        role="staff", username="chriz", is_active=True)
        ric = M.Staff(name="Ric Flores", person_type="staff", role="staff",
                      username="ric", is_active=True)
        db.add_all([chriz, ric]); db.flush()
        db.add(M.CommissionPayout(run_id=run2.id, number="COM-0002",
                                  coach="Ric Flores", coach_id=ric.id,
                                  period_label="September 2026", sessions=31,
                                  commission_total=Decimal("18400"),
                                  total=Decimal("18400")))
        # Chrizel is not a coach: reimbursement only.
        rep = M.ExpenseReport(number="ER-9001", staff_id=chriz.id,
                              person="Chrizel Urbino", occurred_on=date.today(),
                              status=M.EXPENSE_APPROVED)
        db.add(rep); db.flush()
        db.add(M.ExpenseLine(report_id=rep.id, occurred_on=date.today(),
                             account_id=CONSUM, amount=Decimal("3185"),
                             receipt=JPEG, receipt_mime="image/jpeg",
                             receipt_name="r.jpg"))
        # Ric is deducted more than he earns, so his voucher pays zero.
        db.add(M.CommissionAdjustment(coach="Ric Flores", coach_id=ric.id,
                                      occurred_on=date(2026, 9, 1),
                                      title="Laptop", amount=Decimal("-20000"),
                                      account_id=acc["Retail"]))
        db.commit()
        PERIOD = run2.id

    page = c.get("/admin/vouchers/run", params={"period": str(PERIOD)})
    ck("the run page draws", page.status_code == 200)
    ck("it lists everybody owed something",
       "Chrizel Urbino" in page.text and "Ric Flores" in page.text)
    ck("a zero row says what it will carry",
       "carries on" in page.text)

    with Session(engine) as db:
        RICPAY = db.query(M.CommissionPayout).filter_by(number="COM-0002").one().id
        RICADJ = db.query(M.CommissionAdjustment).filter_by(title="Laptop").one().id
        CHREP = db.query(M.ExpenseReport).filter_by(number="ER-9001").one().id

    # Ticking people but unticking every piece on their rows issues nothing.
    out = c.post("/admin/vouchers/run", data={
        "period": str(PERIOD), "reimb": "on", "adj": "on",
        "pay": ["Chrizel Urbino", "Ric Flores"], "piece": [],
    }, follow_redirects=False)
    ck("a run with nothing left on its rows is refused",
       "err=empty" in out.headers.get("location", ""))
    with Session(engine) as db:
        ck("and nothing was issued",
           db.query(M.PaymentVoucher).filter(
               M.PaymentVoucher.batch.isnot(None)).count() == 0)
    out = c.post("/admin/vouchers/run", data={
        "period": str(PERIOD), "reimb": "on", "adj": "on",
        "pay": ["Chrizel Urbino", "Ric Flores"],
        "piece": ["report:%d:Chrizel Urbino" % CHREP,
                  "payout:%d:Ric Flores" % RICPAY,
                  "adjustment:%d:Ric Flores" % RICADJ],
    }, follow_redirects=False)
    BATCH = out.headers["location"].rsplit("/", 1)[-1]
    ck("the run gets its own number", BATCH.startswith("PR-"))
    with Session(engine) as db:
        made = (db.query(M.PaymentVoucher).filter_by(batch=BATCH)
                .order_by(M.PaymentVoucher.id).all())
        ck("one voucher each, not one between them", len(made) == 2)
        ck("they are separate documents",
           len({v.number for v in made}) == 2)
        by = {v.person: v for v in made}
        ck("the reimbursement-only person is paid their report",
           by["Chrizel Urbino"].total == Decimal("3185.00")
           and by["Chrizel Urbino"].commission_total == Decimal(0))
        ck("the over-deducted one pays zero",
           by["Ric Flores"].total == Decimal(0))
        ck("and carries the remainder",
           db.query(M.CommissionAdjustment)
           .filter_by(title="Carried from %s" % by["Ric Flores"].number)
           .one().amount == Decimal("-1600.00"))
        RICV = by["Ric Flores"].id
        CHV = by["Chrizel Urbino"].id

    # Somebody left out of the run is still owed, and still offerable.
    run2page = c.get("/admin/vouchers/run", params={"period": str(PERIOD)}).text
    ck("what was just paid is off the next run",
       "Chrizel Urbino" not in run2page)

    done = c.get("/admin/vouchers/run/%s" % BATCH)
    ck("the run has a page of its own afterwards", done.status_code == 200
       and BATCH in done.text)

    # One reference pays several people.
    c.post("/admin/vouchers/run/%s/pay" % BATCH,
           data={"voucher": [str(CHV)], "on": "2026-10-15",
                 "method": "Bank transfer", "reference": "BPI batch 1015"},
           follow_redirects=False)
    with Session(engine) as db:
        ck("the ticked one is paid",
           db.get(M.PaymentVoucher, CHV).status == M.VOUCHER_PAID
           and db.get(M.PaymentVoucher, CHV).reference == "BPI batch 1015")
        ck("the unticked one is left alone",
           db.get(M.PaymentVoucher, RICV).status == M.VOUCHER_UNPAID)

    # Voiding one leaves the rest of the run standing.
    c.post("/admin/vouchers/%d/void" % RICV, follow_redirects=False)
    with Session(engine) as db:
        ck("voiding one voucher of a run leaves the others",
           db.get(M.PaymentVoucher, RICV).status == M.VOUCHER_VOID
           and db.get(M.PaymentVoucher, CHV).status == M.VOUCHER_PAID)
        ck("and it keeps its place in the run",
           db.get(M.PaymentVoucher, RICV).batch == BATCH)

    # ── somebody who is not a coach ────────────────────────────────────
    with Session(engine) as db:
        who = (db.query(M.Staff).filter(M.Staff.id != JULIO,
                                        M.Staff.is_active.is_(True))
               .first().name)
    page = c.get("/admin/vouchers/new", params={"who": who})
    ck("a non-coach still gets a build screen", page.status_code == 200)
    ck("with nothing in the commission stack",
       "Either they are not a coach" in page.text)

    # ── the add row, and a receipt the office may do without ───────────
    c.post("/admin/expenses/new",
           data={"who": "Julio Reyes", "on": "2026-10-07"},
           follow_redirects=False)
    with Session(engine) as db:
        DESK = (db.query(M.ExpenseReport)
                .order_by(M.ExpenseReport.id.desc()).first()).id
    own = c.get("/expenses/%d" % DESK).text
    ck("adding is the last row of the table, not a card below it",
       'class="addrow"' in own and 'form="addline"' in own)
    ck("and the file input is not demanded of the office",
       'required aria-label="The receipt"' not in own)

    # The office files a transfer it can see on the bank statement, no slip.
    c.post("/expenses/%d/line" % DESK,
           data={"on": "2026-10-07", "account": str(CONSUM), "amount": "75",
                 "note": "Petty cash, no slip came back"},
           follow_redirects=False)
    with Session(engine) as db:
        bare = (db.query(M.ExpenseLine)
                .filter(M.ExpenseLine.report_id == DESK,
                        M.ExpenseLine.receipt.is_(None)).all())
        ck("the office can file a line with no receipt", len(bare) == 1
           and bare[0].money == Decimal("75.00"))
        ck("and it is marked as having none", bare[0].receipt_name is None)
        BARE_LID = bare[0].id
    ck("there is nothing to open for it",
       c.get("/expenses/%d/receipt/%d" % (DESK, BARE_LID),
             follow_redirects=False).status_code == 303)
    c.post("/expenses/%d/submit" % DESK, follow_redirects=False)
    ck("and the review page says so rather than offering a dead link",
       "No receipt" in c.get("/admin/expenses/%d" % DESK).text)
    ck("the header counts the ones without one",
       "1 with no receipt" in c.get("/admin/expenses/%d" % DESK).text)

    # The person claiming their own money still has to produce one.
    c.post("/logout")
    c.post("/login", data={"username": "julio", "pin": "4321"})
    c.post("/expenses/new", data={"on": "2026-10-08"}, follow_redirects=False)
    with Session(engine) as db:
        OWN = (db.query(M.ExpenseReport).filter_by(staff_id=JULIO)
               .order_by(M.ExpenseReport.id.desc()).first()).id
    out = c.post("/expenses/%d/line" % OWN,
                 data={"on": "2026-10-08", "account": str(CONSUM), "amount": "90"},
                 follow_redirects=False)
    ck("their own claim still needs the receipt",
       "err=receipt" in out.headers["location"])
    with Session(engine) as db:
        ck("and nothing was written",
           db.query(M.ExpenseLine).filter_by(report_id=OWN).count() == 0)
    c.post("/expenses/%d/delete" % OWN, follow_redirects=False)
    c.post("/logout")
    c.post("/login", data={"username": "admin", "pin": "123456"})

    # ── sending it: a private link, and the page they open ─────────────
    #
    # The voucher under test is a fresh one for Julio, so the page has all
    # three tabs on it. Everything released by the void above is claimable
    # again, which is what makes that possible.
    # Release the payout from the voucher an earlier step left it on, so this
    # one carries all three stacks.
    with Session(engine) as db:
        held = db.get(M.CommissionPayout, PAYOUT).voucher_id
    if held:
        c.post("/admin/vouchers/%d/void" % held, follow_redirects=False)
    with Session(engine) as db:
        # An address to send to, and the sessions behind the commission —
        # the lines a real run writes when it is finalized.
        db.get(M.Staff, JULIO).email = "julio@awakengym.com"
        db.add(M.CommissionPayoutLine(
            payout_id=PAYOUT, occurred_on=date(2026, 9, 2),
            description="Private Coaching \u00b7 Marga Diaz \u00b7 10 Sessions",
            basis="70% of \u20b11,700.00", amount=Decimal("1190")))
        db.add(M.CommissionPayoutLine(
            payout_id=PAYOUT, occurred_on=date(2026, 9, 4),
            description="Awaken Force \u00b7 Dax Lim \u00b7 Drop-in",
            basis="flat \u20b1600.00", amount=Decimal("600")))
        AID2 = db.query(M.CommissionAdjustment).filter(
            M.CommissionAdjustment.coach == "Julio Reyes",
            M.CommissionAdjustment.voucher_id.is_(None),
            M.CommissionAdjustment.payout_id.is_(None)).first().id
        OFFICE_LID = db.get(M.ExpenseReport, OFFICE_RID).lines[0].id
        db.commit()
    c.post("/admin/vouchers/new",
           data={"who": "Julio Reyes", "payout": [str(PAYOUT)],
                 "report": [str(RID)], "adjustment": [str(AID2)]},
           follow_redirects=False)
    with Session(engine) as db:
        v = (db.query(M.PaymentVoucher)
             .filter(M.PaymentVoucher.status == M.VOUCHER_UNPAID,
                     M.PaymentVoucher.person == "Julio Reyes")
             .order_by(M.PaymentVoucher.id.desc()).first())
        SEND, SEND_NO = v.id, v.number
        NET = "{:,.2f}".format(float(v.total))

    # No mail configured yet: the button says so rather than failing quietly.
    out = c.post("/admin/vouchers/%d/send" % SEND, follow_redirects=False)
    ck("with no mail set up, the send says what is missing",
       "setup=" in out.headers.get("location", ""))
    with Session(engine) as db:
        ck("and nothing was minted for it",
           db.query(M.VoucherLink).filter_by(voucher_id=SEND).count() == 0)

    # A link on its own, for pasting into a chat.
    c.post("/admin/vouchers/%d/link" % SEND, follow_redirects=False)
    with Session(engine) as db:
        link = db.query(M.VoucherLink).filter_by(voucher_id=SEND).one()
        ck("a link can be made without emailing it", len(link.token) > 24)
        ck("it expires", link.expires_at is not None)
        ck("and it starts out unread", link.state == "ready")
        TOKEN = link.token
        LINK_ID = link.id

    # Pressing it again does not pile up links.
    c.post("/admin/vouchers/%d/link" % SEND, follow_redirects=False)
    with Session(engine) as db:
        ck("a working link is not replaced",
           db.query(M.VoucherLink).filter_by(voucher_id=SEND).count() == 1)

    page = c.get("/v/%s" % TOKEN)
    ck("the link opens without a login", page.status_code == 200)
    ck("it names the voucher and the person",
       SEND_NO in page.text and "Julio Reyes" in page.text)
    ck("it has the three tabs", "Commission" in page.text
       and "Adjustments" in page.text and "Expense reports" in page.text)
    ck("the commission tab lists the sessions",
       "Private Coaching" in page.text)
    ck("it shows the client", "Marga Diaz" in page.text)
    ck("but not the rate behind it", "70% of" not in page.text)
    ck("the adjustment carries its reason",
       "Overpaid July" in page.text)
    ck("the receipt is reachable from it",
       "/v/%s/receipt/" % TOKEN in page.text)
    ck("and nothing admin is linked from it",
       "/admin/" not in page.text and "/commissions/" not in page.text)

    with Session(engine) as db:
        ck("opening it is recorded",
           db.get(M.VoucherLink, LINK_ID).opens == 1
           and db.get(M.VoucherLink, LINK_ID).first_opened_at is not None)

    # The receipt, and only from a report on this voucher.
    shot = c.get("/v/%s/receipt/%d" % (TOKEN, LID))
    ck("the receipt comes back", shot.status_code == 200
       and shot.content == JPEG)
    ck("a line on somebody else's report does not",
       c.get("/v/%s/receipt/%d" % (TOKEN, OFFICE_LID)).status_code == 404)

    # "There is a problem" raises a hand; it moves nothing.
    c.post("/v/%s/ack" % TOKEN, data={"ok": "no"}, follow_redirects=False)
    with Session(engine) as db:
        link = db.get(M.VoucherLink, LINK_ID)
        ck("they can say something is wrong", link.acked_at is not None
           and link.ack_ok is False)
        ck("and it is only a flag", link.state == "answered"
           and db.get(M.PaymentVoucher, SEND).status == M.VOUCHER_UNPAID)
    ck("the page says so afterwards",
       "isn't right" in c.get("/v/%s" % TOKEN).text)

    # Mail configured: the email goes, and carries the link.
    sent = []
    import app.mailer as _mail
    _real_send = _mail.Mailer.send
    _mail.Mailer.send = lambda self, to, subject, text, html=None, **kw: (
        sent.append((to, subject, text, html)) or (True, "ok"))
    for k, v in [("SMTP_HOST", "smtp.test"), ("SMTP_USER", "u"),
                 ("SMTP_PASSWORD", "p"), ("MAIL_FROM", "admin@awakengym.com")]:
        os.environ[k] = v
    c.post("/admin/vouchers/%d/send" % SEND, follow_redirects=False)
    ck("the email goes out", len(sent) == 1)
    if sent:
        to, subject, text, html = sent[0]
        ck("to the address on their record", to == "julio@awakengym.com")
        ck("the subject names the voucher", SEND_NO in subject)
        ck("the body carries the net", NET in text and NET in html)
        ck("and the link to the page", "/v/" in text)
        ck("but no receipt or session detail in the email itself",
           "grab.jpg" not in text and "Marga Diaz" not in text)
    with Session(engine) as db:
        ck("the send is stamped on the link",
           db.get(M.VoucherLink, LINK_ID).sent_to == "julio@awakengym.com"
           and db.get(M.VoucherLink, LINK_ID).sent_at is not None)

    # Twice is not twice.
    out = c.post("/admin/vouchers/%d/send" % SEND, follow_redirects=False)
    ck("pressing send again does not email them again", len(sent) == 1
       and "skipped=1" in out.headers.get("location", ""))
    c.post("/admin/vouchers/%d/send?force=1" % SEND, follow_redirects=False)
    ck("but it can be forced", len(sent) == 2)

    # A whole pay run: one email each, and one person's missing address does
    # not stop the others.
    out = c.post("/admin/vouchers/run/%s/send" % BATCH, follow_redirects=False)
    where = out.headers.get("location", "")
    ck("a pay run sends everybody their own", "skipped=1" in where
       and "failed=1" in where)
    ck("and says why the one that couldn't go didn't",
       "email" in where)
    with Session(engine) as db:
        ck("the voided voucher of the run was left alone",
           db.query(M.VoucherLink)
           .filter_by(voucher_id=RICV).count() == 0)

    # Turning it off answers rather than vanishing.
    c.post("/admin/vouchers/%d/link/revoke" % SEND, follow_redirects=False)
    gone = c.get("/v/%s" % TOKEN)
    ck("a revoked link is turned off, not lost", gone.status_code == 410
       and "turned off" in gone.text)
    ck("a token nobody issued is a plain not-found",
       c.get("/v/nonesuch").status_code == 404)
    ck("and a revoked link's receipts close with it",
       c.get("/v/%s/receipt/%d" % (TOKEN, LID)).status_code == 404)

    # A fresh link for the same voucher retires nothing but itself.
    c.post("/admin/vouchers/%d/link" % SEND, follow_redirects=False)
    with Session(engine) as db:
        rows = (db.query(M.VoucherLink).filter_by(voucher_id=SEND)
                .order_by(M.VoucherLink.id.desc()).all())
        ck("the old row is kept so its URL still answers", len(rows) == 2)
        ck("the new one is the live one",
           rows[0].is_live and not rows[1].is_live)
        NEW = rows[0].token
    ck("and it opens", c.get("/v/%s" % NEW).status_code == 200)

    # ── the list page, several at a time ───────────────────────────────
    out = c.post("/admin/vouchers/send", data={"action": "send"},
                 follow_redirects=False)
    ck("ticking nothing says so rather than doing nothing quietly",
       "none=1" in out.headers.get("location", ""))

    # Links without email, for pasting into a chat.
    out = c.post("/admin/vouchers/send",
                 data={"action": "link", "voucher": [str(CHV), str(RICV)]},
                 follow_redirects=False)
    ck("links can be made for several at once",
       "linked=1" in out.headers.get("location", ""))
    with Session(engine) as db:
        ck("and the voided one is left out",
           db.query(M.VoucherLink).filter_by(voucher_id=RICV).count() == 0)
        ck("while the live one has one",
           db.query(M.VoucherLink).filter_by(voucher_id=CHV).count() == 1)

    # Emailing several: one of them has no address, and the rest still go.
    before = len(sent)
    out = c.post("/admin/vouchers/send",
                 data={"action": "send", "voucher": [str(SEND), str(CHV)]},
                 follow_redirects=False)
    where = out.headers.get("location", "")
    ck("one person's missing address does not stop the others",
       "sent=1" in where and "failed=1" in where)
    ck("the one that could go, went", len(sent) == before + 1)

    # The same press again: it has been sent, so it is left alone.
    out = c.post("/admin/vouchers/send",
                 data={"action": "send", "voucher": [str(SEND)]},
                 follow_redirects=False)
    ck("a voucher already sent is skipped, not sent twice",
       len(sent) == before + 1
       and "skipped=1" in out.headers.get("location", ""))

    out = c.post("/admin/vouchers/send",
                 data={"action": "resend", "voucher": [str(SEND)]},
                 follow_redirects=False)
    ck("sending again is a choice on the same screen",
       len(sent) == before + 2 and "sent=1" in out.headers.get("location", ""))

    page = c.get("/admin/vouchers").text
    ck("the list offers the tick boxes", 'name="voucher"' in page
       and 'action="/admin/vouchers/send"' in page)
    ck("and says who has theirs", "Opened" in page or "Sent" in page)
    ck("the list has no build-a-voucher picker, just the button",
       'New payment' in page and '<select name="who"' not in page
       and 'action="/admin/vouchers/new"' not in page)

    # ── filtering and paging the list ──────────────────────────────────
    ck("the list offers the filters",
       'name="status"' in page and 'name="since"' in page
       and 'name="until"' in page and 'name="per"' in page)
    ck("the filters are behind a funnel, shut until there is one on",
       '<details class="pop" >' in page or '<details class="pop">' in page)
    ck("and none of them is compulsory",
       'placeholder="Type a name&hellip;" >' in page
       or 'required' not in page.split('name="who"')[1].split('>')[0])
    ck("the bulk action is a menu rather than a card on the page",
       'With ticked' in page and 'Do it to the ticked ones' not in page)
    ck("an empty filter form is a real request",
       c.get("/admin/vouchers", params={"who": "", "status": "",
                                        "since": "", "until": ""})
        .text.count("PV-0001") >= 1)

    only_void = c.get("/admin/vouchers", params={"status": "void"}).text
    ck("a status narrows it", "Unpaid</span>" not in only_void)
    ck("and the empty case is not mistaken for an empty list",
       "Nothing matches that" in
       c.get("/admin/vouchers", params={"who": "nobody at all"}).text)

    # Against the rows, not the page: the filter's own name list is on it too.
    def whose(text):
        import re
        cells = re.findall(r"<td><b>([^<]+)</b></td>", text)
        return sorted({x for x in cells if not x.startswith("PV-")})

    mine = c.get("/admin/vouchers", params={"who": "Chrizel Urbino"}).text
    ck("a name narrows it to that person", whose(mine) == ["Chrizel Urbino"])
    ck("a surname on its own works too",
       whose(c.get("/admin/vouchers", params={"who": "reyes"}).text)
       == ["Julio Reyes"])
    # On the list a part-name shows everybody it could mean, rather than
    # resolving to one person and quietly hiding the rest.
    ck("a part-name shows everybody it matches",
       whose(c.get("/admin/vouchers", params={"who": "julio"}).text)
       == ["Julio Reyes"])

    ck("a date range with nothing in it comes back empty",
       "Nothing matches that" in
       c.get("/admin/vouchers",
             params={"since": "2020-01-01", "until": "2020-01-31"}).text)
    ck("and today's range has them all",
       "PV-0001" in c.get("/admin/vouchers",
                          params={"since": date.today().isoformat()}).text)

    # Paging: one row at a time is not offered, but 25 is, so ask for 25 and
    # check the page walks rather than repeats.
    small = c.get("/admin/vouchers", params={"per": 25}).text
    ck("the page size is honoured", 'value="25" selected' in small)
    with Session(engine) as db:
        total = db.query(M.PaymentVoucher).count()
    ck("the count is the whole filtered set, not the page",
       ">%d<" % total in c.get("/admin/vouchers").text)
    ck("a page past the end lands on the last one rather than empty",
       "PV-0001" in c.get("/admin/vouchers", params={"page": 999}).text)

    # ── a name that is typed, not scrolled ─────────────────────────────
    build = c.get("/admin/vouchers/new", params={"who": "Julio Reyes"}).text
    ck("the picker is a typed field", 'data-tah' in build
       and 'name="who"' in build and '<select name="who"' not in build)
    ck("and it carries the names to match against", '"Julio Reyes"' in build)

    def resolved(q):
        page = c.get("/admin/vouchers/new", params={"who": q}).text
        return ('<input type="hidden" name="who" value="Julio Reyes">' in page,
                "No single person matches" in page)

    ck("the wrong case still finds them", resolved("julio reyes")[0])
    ck("so does a surname on its own", resolved("reyes")[0])
    ck("a name nobody has says so rather than showing an empty voucher",
       c.get("/admin/vouchers/new", params={"who": "zzz"})
        .text.count("No single person matches") == 1)

    # Two people whose names start the same way: it will not pick one.
    with Session(engine) as db:
        db.add(M.Staff(name="Julio Santos", person_type="staff", role="staff",
                       is_active=True))
        db.commit()
    ck("and it refuses to guess between two people", resolved("ju")[1])
    ck("while the full name is still unambiguous", resolved("Julio Reyes")[0])

    # The button that issues it is at the top of the page, not the bottom.
    head = build.split('</h1>')[1][:1400]
    ck("the issue button is up in the header", 'form="vbuild"' in head)

    _mail.Mailer.send = _real_send

    for path in ["/expenses", "/admin/expenses", "/admin/vouchers",
                 "/admin/vouchers/new", "/admin/vouchers/run",
                 "/admin/vouchers/run/%s" % BATCH,
                 "/admin/expenses/%d" % RID,
                 "/admin/vouchers/%d" % VID,
                 "/admin/vouchers/%d" % SEND,
                 "/v/%s" % NEW]:
        r = c.get(path)
        ck("page draws: %s" % path, r.status_code == 200)

bad = [n for n, ok in res if not ok]
print("\n%d/%d passed" % (len(res) - len(bad), len(res)))
if bad:
    print("FAILED: " + "; ".join(bad))
