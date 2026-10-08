"""Taking a finalized run back, and taking approvals back.

The two things an admin reaches for when the import turns out to be wrong:
putting the run back to draft, and withdrawing the ticks that said it was
right. Both have to say plainly when they cannot happen.
"""
import os
os.environ["DATABASE_URL"] = ("postgresql+psycopg2://postgres@/reop"
                              "?host=/home/claude/pgrun&port=5433")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_INITIAL_PIN", "123456")
from datetime import date
from decimal import Decimal
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import engine
from app import models as M
from fastapi.testclient import TestClient
from app.main import app

res = []
def ck(n, c):
    res.append((n, bool(c)))
    print("PASS" if c else "FAIL", n)

with engine.begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))


def booking(run_id, coach, ref, status="Completed", rev="1700", comm="1190"):
    return M.CommissionBooking(
        run_id=run_id, booking_ref=ref, customer="A Client",
        appointment_date=date(2026, 9, 2), appointment_name="Private Coaching",
        staff_raw=coach, coach=coach, booking_status=status,
        pricing_plan="10 Sessions", revenue=Decimal(rev),
        revenue_raw=Decimal(rev), commission=Decimal(comm),
        rule="percent", rate_type="percent", rate_value=Decimal("0.7"),
        pays_by_status=(status == "Completed"))


with TestClient(app) as c:
    c.post("/login", data={"username": "admin", "pin": "123456"})
    with Session(engine) as db:
        run = M.CommissionRun(period="2026-09", period_label="September 2026",
                              status=M.RUN_DRAFT)
        db.add(run); db.flush()
        db.add(booking(run.id, "Ric Flores", "B1"))
        db.add(booking(run.id, "Ric Flores", "B2", status="No-show",
                       comm="0"))
        db.add(booking(run.id, "Laurent Javier", "B3"))
        db.commit()
        RID = run.id

    # ── withdrawing approvals, one and many ────────────────────────────
    c.post("/commissions/%d/signoff-many" % RID,
           data={"coach": ["Ric Flores", "Laurent Javier"], "confirm": "on"},
           follow_redirects=False)
    with Session(engine) as db:
        ck("several coaches approve at once",
           db.query(M.CommissionSignoff).filter_by(run_id=RID).count() == 2)

    page = c.get("/commissions/%d?tab=coaches" % RID).text
    ck("an approved coach can still be ticked",
       page.count('class="chk pick"') == 2)
    ck("and the bar offers taking it back", 'value="off"' in page
       and "Withdraw selected" in page)

    c.post("/commissions/%d/signoff-many" % RID,
           data={"coach": ["Ric Flores", "Laurent Javier"], "confirm": "off"},
           follow_redirects=False)
    with Session(engine) as db:
        ck("and several come back off at once",
           db.query(M.CommissionSignoff).filter_by(run_id=RID).count() == 0)
    ck("the notice says which way it went",
       "Withdrew approval from" in c.get("/commissions/%d?tab=coaches" % RID).text)

    # One at a time still works, from the coach's own screen.
    c.post("/commissions/%d/coach/Ric Flores/signoff" % RID,
           data={"confirm": "on"}, follow_redirects=False)
    ck("the coach screen offers withdrawing it",
       "Withdraw approval" in c.get("/commissions/%d/coach/Ric Flores" % RID).text)
    c.post("/commissions/%d/coach/Ric Flores/signoff" % RID,
           data={"confirm": "off"}, follow_redirects=False)
    with Session(engine) as db:
        ck("and it goes",
           db.query(M.CommissionSignoff).filter_by(run_id=RID).count() == 0)

    # ── un-approving a line item ───────────────────────────────────────
    with Session(engine) as db:
        B2 = db.query(M.CommissionBooking).filter_by(run_id=RID,
                                                     booking_ref="B2").one().id
    c.post("/commissions/%d/booking/%d/approve" % (RID, B2),
           follow_redirects=False)
    with Session(engine) as db:
        ck("a no-show can be brought in",
           db.get(M.CommissionBooking, B2).approved is True)
    c.post("/commissions/%d/booking/%d/approve" % (RID, B2),
           follow_redirects=False)
    with Session(engine) as db:
        ck("and taken back out again",
           db.get(M.CommissionBooking, B2).approved is False)

    # ── reopening, and the two things that stop it ─────────────────────
    c.post("/commissions/%d/signoff-many" % RID,
           data={"coach": ["Ric Flores", "Laurent Javier"], "confirm": "on"},
           follow_redirects=False)
    c.post("/commissions/%d/finalize" % RID, follow_redirects=False)
    with Session(engine) as db:
        ck("the run finalizes",
           db.get(M.CommissionRun, RID).status == M.RUN_FINALIZED)
        PAY = (db.query(M.CommissionPayout).filter_by(run_id=RID,
                                                      coach="Ric Flores")
               .one()).id

    # A bill holding one of its payouts.
    c.post("/admin/vouchers/new",
           data={"who": "Ric Flores", "payout": [str(PAY)]},
           follow_redirects=False)
    with Session(engine) as db:
        v = db.query(M.PaymentVoucher).one()
        ck("a bill claims the payout",
           db.get(M.CommissionPayout, PAY).voucher_id == v.id)
        BILL, BILLNO = v.id, v.number

    page = c.get("/commissions/%d" % RID).text
    ck("the run says so before anybody presses anything",
       "go back to draft yet" in page and 'class="alert bad"' in page)
    ck("naming who is holding it", "Ric Flores" in page and BILLNO in page)
    ck("with a way to the bill", "/admin/vouchers/%d" % BILL in page)
    ck("and the button is not offered",
       "Reopen as draft</button>" in page and "/reopen" not in page)

    out = c.post("/commissions/%d/reopen" % RID, follow_redirects=False)
    with Session(engine) as db:
        ck("posting at it anyway is refused",
           db.get(M.CommissionRun, RID).status == M.RUN_FINALIZED)

    # Void the bill and the way is clear.
    c.post("/admin/vouchers/%d/void" % BILL, follow_redirects=False)
    page = c.get("/commissions/%d" % RID).text
    ck("once the bill is void the button comes back",
       "/commissions/%d/reopen" % RID in page)
    c.post("/commissions/%d/reopen" % RID, follow_redirects=False)
    with Session(engine) as db:
        ck("and it reopens", db.get(M.CommissionRun, RID).status == M.RUN_DRAFT)
        ck("every approval is gone with it",
           db.query(M.CommissionSignoff).filter_by(run_id=RID).count() == 0)
        ck("and the payouts with them",
           db.query(M.CommissionPayout).filter_by(run_id=RID).count() == 0)
    ck("so every coach is asking to be approved again",
       "Awaiting approval" in c.get("/commissions/%d?tab=coaches" % RID).text)

bad = [n for n, ok in res if not ok]
print("\n%d/%d passed" % (len(res) - len(bad), len(res)))
if bad:
    print("FAILED: " + "; ".join(bad))
