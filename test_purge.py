"""Verify the pre-cutoff transaction purge: delete every transaction with
COALESCE(occurred_at, created_at) < 2026-07-18 00:00+08, retain the rest.
Checks item cascade-delete and self-reference SET NULL on retained rows."""
import os
os.environ["DATABASE_URL"] = "postgresql+psycopg2://postgres@/awaken?host=/tmp&port=5433"
os.environ["ADMIN_INITIAL_PIN"] = "123456"
os.environ.setdefault("SECRET_KEY", "test-secret")
from sqlalchemy import text
from app.db import Base, engine
from app import models as M
from sqlalchemy.orm import Session

CUTOFF = "2026-07-18 00:00:00+08"
results = []
def check(n, c): results.append((n, bool(c))); print("PASS" if c else "FAIL", n)

with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)

with Session(engine) as db:
    p = M.Product(sku="SIP", name="Sip", unit="each", selling_price=55, cost_price=30, reorder_point=0)
    db.add(p); db.commit(); pid = p.id

    def tx(kind, when, subtype=None, parent=None, items=None):
        t = M.Transaction(type=kind, subtype=subtype, status="done",
                          occurred_at=when, parent_id=parent)
        db.add(t); db.flush()
        for qty, price in (items or []):
            db.add(M.TransactionItem(transaction_id=t.id, product_id=pid, name="Sip", qty=qty, unit_price=price))
        db.commit()
        return t.id

    from datetime import datetime, timezone, timedelta
    tz8 = timezone(timedelta(hours=8))
    OLD = datetime(2026, 7, 10, 9, 0, tzinfo=tz8)      # before cutoff -> delete
    EDGE_BEFORE = datetime(2026, 7, 17, 23, 59, tzinfo=tz8)  # before cutoff -> delete
    KEEP = datetime(2026, 7, 18, 0, 1, tzinfo=tz8)     # yesterday -> retain
    TODAY = datetime(2026, 7, 19, 10, 0, tzinfo=tz8)   # today -> retain

    old_sale = tx("cash_sale", OLD, items=[(3, 55)])
    old_adj  = tx("inventory_adjustment", EDGE_BEFORE, subtype="restock", items=[(100, 30)])
    old_order = tx("order", OLD, items=[(2, 55)])
    keep_sale = tx("cash_sale", KEEP, items=[(1, 55)])
    keep_adj  = tx("inventory_adjustment", TODAY, subtype="restock", items=[(50, 30)])
    # a retained payment that points at the OLD order (tests SET NULL on delete)
    keep_pay = tx("payment", KEEP, parent=old_order)

    total_before = db.query(M.Transaction).count()
    items_before = db.query(M.TransactionItem).count()

# --- the purge (exactly what the endpoint will run) ---
with engine.begin() as conn:
    deleted = conn.execute(text(
        "DELETE FROM transactions WHERE COALESCE(occurred_at, created_at) < :c RETURNING id"
    ), {"c": CUTOFF}).fetchall()

with Session(engine) as db:
    ids = {r[0] for r in deleted}
    check("deleted the 3 pre-cutoff txns", ids == {old_sale, old_adj, old_order})
    remaining = {t.id for t in db.query(M.Transaction).all()}
    check("retained yesterday+today txns", remaining == {keep_sale, keep_adj, keep_pay})
    # items of deleted txns cascade-gone; retained items intact
    it_products = db.query(M.TransactionItem).all()
    check("orphaned items cascade-deleted", all(i.transaction_id in remaining for i in it_products))
    check("retained items intact", db.query(M.TransactionItem).count() == 2)  # keep_sale(1) + keep_adj(1)
    # retained payment's parent_id was SET NULL (pointed at deleted old_order)
    pay = db.get(M.Transaction, keep_pay)
    check("retained payment parent_id SET NULL", pay.parent_id is None)
    print(f"before={db.query(M.Transaction).count()+len(ids)} deleted={len(ids)} remaining={len(remaining)}")

print()
bad = [n for n, ok in results if not ok]
print(f"{len(results)-len(bad)}/{len(results)} passed")
if bad: print("FAILED:", bad); raise SystemExit(1)
