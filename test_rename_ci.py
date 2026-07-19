"""Verify payment_settings -> company_info rename on BOTH a fresh DB and a
prod-like DB where payment_settings already exists with data (bank details,
QR, logo) that must survive the rename."""
import os
os.environ["DATABASE_URL"] = "postgresql+psycopg2://postgres@/awaken?host=/tmp&port=5433"
os.environ["ADMIN_INITIAL_PIN"] = "123456"
os.environ.setdefault("SECRET_KEY", "test-secret")
from sqlalchemy import text
from app.db import Base, engine
from app import models as M
from sqlalchemy.orm import Session

results = []
def check(n, c): results.append((n, bool(c))); print("PASS" if c else "FAIL", n)
def ex(t):
    with engine.connect() as c: return c.execute(text("SELECT to_regclass(:n)"), {"n": "public."+t}).scalar() is not None

from fastapi.testclient import TestClient
from app.main import app

# ---------- PART A: fresh DB ----------
with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with TestClient(app):
    pass
check("A: company_info exists", ex("company_info"))
check("A: payment_settings absent", not ex("payment_settings"))
with Session(engine) as db:
    ps = M.PaymentSetting(id=1, bank_name="BPI", account_name="AWAKEN")
    db.add(ps); db.commit()
    got = db.get(M.PaymentSetting, 1)
    check("A: model reads/writes company_info", got and got.bank_name == "BPI")

# ---------- PART B: prod-like (payment_settings pre-exists with data) ----------
engine.dispose()
with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with engine.begin() as conn:
    # rename company_info back to the OLD name and seed it as prod would have it
    conn.execute(text("ALTER TABLE company_info RENAME TO payment_settings"))
    conn.execute(text("INSERT INTO payment_settings(id,bank_name,account_name,qr_mime,logo_mime) "
                      "VALUES (1,'BDO','AWAKEN Fitness','image/png','image/png')"))
    conn.execute(text("UPDATE payment_settings SET qr=decode('deadbeef','hex'), logo=decode('cafe','hex') WHERE id=1"))

with TestClient(app):
    pass
check("B: renamed to company_info", ex("company_info"))
check("B: old payment_settings gone", not ex("payment_settings"))
with Session(engine) as db:
    ps = db.get(M.PaymentSetting, 1)
    check("B: bank_name preserved", ps and ps.bank_name == "BDO")
    check("B: account_name preserved", ps and ps.account_name == "AWAKEN Fitness")
    check("B: qr bytes preserved", ps and ps.qr == b"\xde\xad\xbe\xef")
    check("B: logo bytes preserved", ps and ps.logo == b"\xca\xfe")

# ---------- PART C: idempotent (second boot is a no-op) ----------
with TestClient(app):
    pass
check("C: still company_info after 2nd boot", ex("company_info"))
with Session(engine) as db:
    check("C: data still intact", db.get(M.PaymentSetting, 1).bank_name == "BDO")

print()
bad = [n for n, ok in results if not ok]
print(f"{len(results)-len(bad)}/{len(results)} passed")
if bad: print("FAILED:", bad); raise SystemExit(1)
