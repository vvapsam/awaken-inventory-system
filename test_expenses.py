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
    out = c.get("/expenses/%d" % RID, follow_redirects=False)
    ck("one person cannot open another's report through /expenses",
       out.status_code == 303 and out.headers["location"] == "/expenses")

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
                 "/admin/vouchers/new", "/admin/expenses/%d" % RID,
                 "/admin/vouchers/%d" % VID]:
        r = c.get(path)
        ck("page draws: %s" % path, r.status_code == 200)

bad = [n for n, ok in res if not ok]
print("\n%d/%d passed" % (len(res) - len(bad), len(res)))
if bad:
    print("FAILED: " + "; ".join(bad))
