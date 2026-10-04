"""The chart of accounts, and tagging an adjustment to one.

What an adjustment is *for*, in the words the books use, so a figure can be
handed to a bookkeeper without anybody translating it first. The account is
ours: it is on our list and on our form, and nowhere on the coach's statement.
"""
import os, re
os.environ["DATABASE_URL"] = ("postgresql+psycopg2://postgres@/accts"
                              "?host=/home/claude/pgrun&port=5433")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_INITIAL_PIN", "123456")
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

with TestClient(app) as c:                      # startup seeds the chart
    with Session(engine) as db:
        rows = (db.query(M.Account)
                .order_by(M.Account.kind, M.Account.position).all())
        ck("the chart is seeded", len(rows) == len(M.ACCOUNT_SEED))
        for kind in ("expense", "income"):
            mine = [a.name for a in rows if a.kind == kind]
            ck("%s accounts come in the order given" % kind,
               mine == [n for k, n in M.ACCOUNT_SEED if k == kind])
        ck("both sides are seeded",
           {a.kind for a in rows} == {"expense", "income"})
        ck("an expense label reads as the books write it",
           next(a for a in rows if a.name == "Staff Salary").label
           == "Expense : Staff Salary")
        ck("so does a revenue one",
           next(a for a in rows if a.name == "Coach Corkage").label
           == "Revenue : Coach Corkage")
        ck("each side has its own Unknown",
           len([a for a in rows if a.name == "Unknown"]) == 2)
        SALARY = next(a.id for a in rows if a.name == "Staff Salary")
        BENEFIT = next(a.id for a in rows if a.name == "Staff benefits")

    c.post("/login", data={"username": "admin", "pin": "123456"})

    # --- the page -----------------------------------------------------------
    page = c.get("/admin/accounts")
    ck("the chart has a page", page.status_code == 200
       and "Staff benefits" in page.text)
    ck("it is on the commissions tabs",
       '/admin/accounts' in c.get("/commissions/adjustments").text)

    # --- a coach to hang an adjustment on -----------------------------------
    with Session(engine) as db:
        db.add(M.CommissionCoachRate(coach="Trina", staff_raw="Trina",
                                     rate_type="percent", rate_value=0.4))
        db.commit()

    c.post("/commissions/adjustments/new", data={
        "coach": "Trina", "on": "2026-07-14", "title": "Overpaid July",
        "note": "Two sessions counted twice.", "amount": "1500",
        "sign": "deduct", "account": str(SALARY)}, follow_redirects=False)
    with Session(engine) as db:
        a = db.query(M.CommissionAdjustment).one()
        ck("the adjustment carries its account", a.account_id == SALARY)
        ck("the account comes back by name",
           a.account.label == "Expense : Staff Salary")
        ck("the money is untouched by any of it", a.money == Decimal("-1500.00"))
        AID = a.id

    # --- filing gets corrected, even after it is paid ------------------------
    c.post("/commissions/adjustments/%d/account" % AID,
           data={"account": str(BENEFIT)}, follow_redirects=False)
    with Session(engine) as db:
        ck("it can be moved to another account",
           db.get(M.CommissionAdjustment, AID).account_id == BENEFIT)
    with Session(engine) as db:                 # pretend a payout carried it
        a = db.get(M.CommissionAdjustment, AID)
        r = M.CommissionRun(period="Jul 2026")
        db.add(r); db.flush()
        p = M.CommissionPayout(run_id=r.id, coach="Trina", number="COM-0001")
        db.add(p); db.flush()
        a.payout_id = p.id
        db.commit()
    c.post("/commissions/adjustments/%d/account" % AID,
           data={"account": str(SALARY)}, follow_redirects=False)
    with Session(engine) as db:
        a = db.get(M.CommissionAdjustment, AID)
        ck("a paid one can still be reclassified", a.account_id == SALARY)
        ck("reclassifying does not move the money",
           a.money == Decimal("-1500.00") and a.payout_id is not None)

    # --- the coach never sees it --------------------------------------------
    from app import commission_pdf
    with Session(engine) as db:
        run = db.query(M.CommissionRun).first()
        pdf = commission_pdf.statement(
            run, "Trina", [], adjustments=[db.get(M.CommissionAdjustment, AID)],
            generated_by="test")
    # Read the text, not the bytes: a PDF's streams are compressed, so
    # "is this string absent" against raw bytes is a test that always passes.
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(pdf)
        pdf_path = fh.name
    words = subprocess.run(["pdftotext", pdf_path, "-"],
                           capture_output=True, text=True).stdout
    ck("the statement says what the adjustment is",
       "Overpaid July" in words)
    ck("the statement says what it costs them", "1,500" in words)
    ck("the statement never names an account",
       "Staff Salary" not in words and "Expense :" not in words
       and "Staff welfare" not in words)

    # --- editing the chart ---------------------------------------------------
    c.post("/admin/accounts/%d" % BENEFIT,
           data={"name": "Staff welfare", "code": "5120"},
           follow_redirects=False)
    with Session(engine) as db:
        b = db.get(M.Account, BENEFIT)
        ck("renaming works", b.name == "Staff welfare" and b.code == "5120")

    c.post("/admin/accounts/new", data={"kind": "expense", "name": "rent"},
           follow_redirects=False)
    with Session(engine) as db:
        ck("a duplicate name is refused whatever its capitals",
           db.query(M.Account).filter(M.Account.name.ilike("rent")).count() == 1)

    c.post("/admin/accounts/new",
           data={"kind": "income", "name": "Sponsorship", "code": "4100"},
           follow_redirects=False)
    with Session(engine) as db:
        inc = db.query(M.Account).filter_by(name="Sponsorship").one()
        ck("another one can be added to a kind",
           inc.label == "Revenue : Sponsorship" and inc.code == "4100")
        ck("and it lands after the seeded ones",
           inc.position >= len([1 for k, _n in M.ACCOUNT_SEED
                                if k == "income"]))
        INC = inc.id

    # Two kinds can hold the same name without being the same account.
    c.post("/admin/accounts/new",
           data={"kind": "income", "name": "Rent"}, follow_redirects=False)
    with Session(engine) as db:
        ck("the same name on two sides is two accounts",
           db.query(M.Account).filter_by(name="Rent").count() == 2)

    # Unused: deleted outright. In use: closed, never deleted.
    c.post("/admin/accounts/%d/delete" % INC, follow_redirects=False)
    with Session(engine) as db:
        ck("an unused account is deleted", db.get(M.Account, INC) is None)
    c.post("/admin/accounts/%d/delete" % SALARY, follow_redirects=False)
    with Session(engine) as db:
        a = db.get(M.Account, SALARY)
        ck("an account in use is closed, not deleted",
           a is not None and a.closed is True)
        ck("what was booked to it keeps it",
           db.get(M.CommissionAdjustment, AID).account_id == SALARY)

    # A closed one is off the form but still on the row it was picked for.
    form = c.get("/commissions/adjustments").text
    ck("a closed account is off the picker",
       form.count(">Staff Salary<") <= 1)
    c.post("/commissions/adjustments/new", data={
        "coach": "Trina", "on": "2026-08-01", "title": "Shirt at cost",
        "amount": "600", "sign": "deduct", "account": str(SALARY)},
        follow_redirects=False)
    with Session(engine) as db:
        new = (db.query(M.CommissionAdjustment)
               .filter_by(title="Shirt at cost").one())
        ck("a closed account cannot be picked for a new one",
           new.account_id is None)

    # Bringing a retired one back rather than making a second copy.
    c.post("/admin/accounts/new",
           data={"kind": "expense", "name": "Staff Salary"},
           follow_redirects=False)
    with Session(engine) as db:
        same = db.query(M.Account).filter_by(name="Staff Salary").all()
        ck("re-adding a retired account reopens it",
           len(same) == 1 and same[0].closed is False)

    # --- the totals ----------------------------------------------------------
    page = c.get("/commissions/adjustments").text
    ck("the page totals by account", "By account" in page)
    ck("it counts the untagged ones", "1 with no account yet" in page)

# --- the seed runs once, not on every boot ---------------------------------
with Session(engine) as db:
    db.query(M.CommissionAdjustment).delete()
    db.query(M.Account).filter(M.Account.name == "Rent").delete()
    before = db.query(M.Account).filter_by(kind="income").count()
    db.commit()
with TestClient(app) as c:
    with Session(engine) as db:
        ck("a deleted account stays deleted",
           db.query(M.Account).filter_by(name="Rent").count() == 0)
        ck("and a kind that already has accounts is not re-seeded",
           db.query(M.Account).filter_by(kind="income").count() == before)

# A kind arriving later is seeded on its own, without disturbing the rest.
with Session(engine) as db:
    db.query(M.Account).filter_by(kind="income").delete()
    kept = db.query(M.Account).filter_by(kind="expense").count()
    db.commit()
with TestClient(app) as c:
    with Session(engine) as db:
        ck("an empty kind is seeded on the next boot",
           db.query(M.Account).filter_by(kind="income").count()
           == len([1 for k, _n in M.ACCOUNT_SEED if k == "income"]))
        ck("and the other side is left exactly alone",
           db.query(M.Account).filter_by(kind="expense").count() == kept)

bad = [n for n, ok in res if not ok]
print("\n%d/%d passed" % (len(res) - len(bad), len(res)))
if bad:
    print("FAILED: " + "; ".join(bad))
