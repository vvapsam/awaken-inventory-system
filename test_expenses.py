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

    for path in ["/expenses", "/admin/expenses", "/admin/vouchers",
                 "/admin/vouchers/new", "/admin/vouchers/run",
                 "/admin/vouchers/run/%s" % BATCH,
                 "/admin/expenses/%d" % RID,
                 "/admin/vouchers/%d" % VID]:
        r = c.get(path)
        ck("page draws: %s" % path, r.status_code == 200)

bad = [n for n, ok in res if not ok]
print("\n%d/%d passed" % (len(res) - len(bad), len(res)))
if bad:
    print("FAILED: " + "; ".join(bad))
