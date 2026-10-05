"""Everybody picks their own PIN.

The rule worth testing hardest is the one that would be worst to get wrong in
either direction: an account that owes us a PIN must not be able to reach the
app at all, and an account that has chosen one must never be asked again.
"""
import os
os.environ["DATABASE_URL"] = ("postgresql+psycopg2://postgres@/pin"
                              "?host=/home/claude/pgrun&port=5433")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_INITIAL_PIN", "123456")
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import engine
from app import models as M
from app.auth import hash_pin, verify_pin
from fastapi.testclient import TestClient
from app.main import app

res = []
def ck(n, c):
    res.append((n, bool(c)))
    print("PASS" if c else "FAIL", n)

with engine.begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

with TestClient(app) as c:
    with Session(engine) as db:
        from app.auth import hash_pin as hp
        ric = M.Staff(name="Ric Flores", person_type="staff", role="staff",
                      username="ric", is_active=True, has_access=True,
                      permissions="view_stock")
        ric.pin_hash, ric.pin_salt = hp("4321")
        ric.must_change_pin = True
        db.add(ric); db.commit()
        RIC = ric.id

    # ── the forced change ──────────────────────────────────────────────
    out = c.post("/login", data={"username": "ric", "pin": "4321"},
                 follow_redirects=False)
    ck("signing in with a PIN somebody else set lands on the change screen",
       out.status_code == 303 and out.headers["location"] == "/change-pin")
    page = c.get("/change-pin")
    ck("which says why", page.status_code == 200
       and "set for you" in page.text and "Ric" in page.text)

    # Nothing else opens.
    for path in ["/dashboard", "/admin/staff", "/expenses", "/race",
                 "/admin/vouchers", "/saved-reports", "/m"]:
        r = c.get(path, follow_redirects=False)
        ck("shut until it is done: %s" % path,
           r.status_code == 303 and r.headers["location"] == "/change-pin")
    ck("but signing out still works",
       c.get("/logout", follow_redirects=False).status_code == 303)

    c.post("/login", data={"username": "ric", "pin": "4321"},
           follow_redirects=False)

    # ── what will not do ───────────────────────────────────────────────
    for pin, again, why, msg in [
            ("12ab", "12ab", "letters", "digits only"),
            ("123", "123", "too short", "between"),
            ("1234567890123", "1234567890123", "too long", "between"),
            ("1234", "4321", "a typo in the second box", "not the same"),
            ("1111", "1111", "the same digit over and over", "over and over"),
            ("4321", "4321", "the PIN they already have", "already have")]:
        out = c.post("/change-pin", data={"pin": pin, "again": again})
        ck("refused: %s" % why, out.status_code == 200 and msg in out.text)
    with Session(engine) as db:
        ck("and none of it changed the PIN",
           verify_pin("4321", db.get(M.Staff, RIC).pin_hash,
                      db.get(M.Staff, RIC).pin_salt))

    # ── choosing one ───────────────────────────────────────────────────
    out = c.post("/change-pin", data={"pin": "905118", "again": "905118"},
                 follow_redirects=False)
    ck("a good one is taken", out.status_code == 303
       and "/change-pin" not in out.headers["location"])
    with Session(engine) as db:
        p = db.get(M.Staff, RIC)
        ck("the new PIN is theirs", verify_pin("905118", p.pin_hash, p.pin_salt))
        ck("the old one is gone", not verify_pin("4321", p.pin_hash, p.pin_salt))
        ck("and they are not asked again", p.must_change_pin is False)
        ck("with the date they chose it", p.pin_set_at is not None)

    ck("the app opens afterwards",
       c.get("/dashboard", follow_redirects=False).status_code == 200)
    c.post("/logout")
    bad = c.post("/login", data={"username": "ric", "pin": "4321"})
    ck("the old PIN no longer signs them in", "Wrong username or PIN" in bad.text)
    out = c.post("/login", data={"username": "ric", "pin": "905118"},
                 follow_redirects=False)
    ck("the new one does, straight into the app",
       out.status_code == 303 and out.headers["location"] != "/change-pin")

    # ── changing it voluntarily ────────────────────────────────────────
    page = c.get("/change-pin")
    ck("they can change it again whenever", page.status_code == 200
       and "set for you" not in page.text)
    c.post("/change-pin", data={"pin": "774120", "again": "774120"},
           follow_redirects=False)
    with Session(engine) as db:
        p = db.get(M.Staff, RIC)
        ck("without being locked out of anything",
           verify_pin("774120", p.pin_hash, p.pin_salt)
           and p.must_change_pin is False)
    ck("and the app is still open",
       c.get("/dashboard", follow_redirects=False).status_code == 200)

    # ── an admin resetting somebody ────────────────────────────────────
    c.post("/logout")
    c.post("/login", data={"username": "admin", "pin": "123456"})
    with Session(engine) as db:
        role = db.query(M.Role).filter(M.Role.name == "Staff").first()
        RID = role.id if role else ""
    c.post("/admin/staff/%d/edit" % RIC,
           data={"name": "Ric Flores", "person_type": "employee",
                 "has_access": "on", "is_active": "on", "username": "ric",
                 "role_id": str(RID), "pin": "500500"},
           follow_redirects=False)
    with Session(engine) as db:
        p = db.get(M.Staff, RIC)
        ck("an admin-set PIN works once", verify_pin("500500", p.pin_hash, p.pin_salt))
        ck("and is temporary by construction", p.must_change_pin is True)

    # The admin's own PIN is not temporary — there is nobody to hide it from.
    with Session(engine) as db:
        me = db.query(M.Staff).filter(M.Staff.username == "admin").one()
        ME, MYROLE = me.id, me.role_id
    c.post("/admin/staff/%d/edit" % ME,
           data={"name": "Admin", "person_type": "employee", "has_access": "on",
                 "is_active": "on", "username": "admin",
                 "role_id": str(MYROLE), "pin": "778899"},
           follow_redirects=False)
    with Session(engine) as db:
        ck("changing your own does not lock you out of it",
           db.get(M.Staff, ME).must_change_pin is False)
    ck("and you stay signed in",
       c.get("/admin/staff", follow_redirects=False).status_code == 200)

# ── the one-time push for everybody who already had a login ────────────
with engine.begin() as cn:
    cn.execute(text("UPDATE entity SET must_change_pin = false"))
    cn.execute(text("ALTER TABLE entity DROP COLUMN must_change_pin"))
with TestClient(app):
    pass
with Session(engine) as db:
    rows = db.query(M.Staff).filter(M.Staff.has_access.is_(True)).all()
    ck("the upgrade asks everybody with a login, once",
       bool(rows) and all(r.must_change_pin for r in rows))
    ck("and nobody without one", all(
        not r.must_change_pin
        for r in db.query(M.Staff).filter(M.Staff.has_access.is_(False))))
# A second boot must not re-force the people who have since chosen.
with Session(engine) as db:
    db.query(M.Staff).filter(M.Staff.username == "ric").update(
        {"must_change_pin": False})
    db.commit()
with TestClient(app):
    pass
with Session(engine) as db:
    ck("a later deploy does not ask them all over again",
       db.query(M.Staff).filter(M.Staff.username == "ric")
       .one().must_change_pin is False)

bad = [n for n, ok in res if not ok]
print("\n%d/%d passed" % (len(res) - len(bad), len(res)))
if bad:
    print("FAILED: " + "; ".join(bad))
