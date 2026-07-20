import os, io, json
os.environ["DATABASE_URL"] = "postgresql+psycopg2://postgres@/awaken?host=/tmp&port=5433"
os.environ.setdefault("SECRET_KEY", "test-secret")
from PIL import Image
from sqlalchemy import text
from app.db import Base, engine
from app import models as M
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.main import app

results = []
def check(n, c): results.append((n, bool(c))); print("PASS" if c else "FAIL", n)
def png():
    b = io.BytesIO(); Image.new("RGB", (20, 20), (200, 200, 200)).save(b, "PNG"); return b.getvalue()

with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with Session(engine) as db:
    p1 = M.Product(sku="SIP", name="Sip Water", selling_price=55, cost_price=30)
    p2 = M.Product(sku="POC", name="Pocari", selling_price=80, cost_price=50)
    db.add_all([p1, p2]); db.commit(); pid1, pid2 = p1.id, p2.id

with TestClient(app) as c:
    c.post("/login", data={"username": "admin", "pin": "123456"})
    c.post("/movement/new", data={"movement_type": "restock", "product_id": pid1, "quantity": 100, "unit_cost": "30"})
    c.post("/movement/new", data={"movement_type": "restock", "product_id": pid2, "quantity": 100, "unit_cost": "50"})
    # a customer to reassign to
    c.post("/admin/staff/new", data={"name": "Edit Target", "person_type": "customer", "has_access": "off"})
    with Session(engine) as db:
        cust_id = db.query(M.Staff).filter(M.Staff.name == "Edit Target").first().id

    # mobile cash sale: Sip x2 + Pocari x1
    r = c.post("/api/m/sale", data={"items": json.dumps([{"product_id": pid1, "quantity": 2},
                                                        {"product_id": pid2, "quantity": 1}]),
                                    "payment": "cash"},
               files={"proof": ("p.png", png(), "image/png")})
    sid = r.json()["sale_id"]
    with Session(engine) as db:
        s = db.get(M.Transaction, sid)
        items = sorted(s.items, key=lambda i: i.name)
        sip_item = [i for i in s.items if i.product_id == pid1][0]
        poc_item = [i for i in s.items if i.product_id == pid2][0]
        sip_iid, poc_iid = sip_item.id, poc_item.id
    check("sale created total 110+80? = 190", abs(float(s.total) - (55*2+80)) < 0.01)

    # GET the edit form renders
    g = c.get(f"/sale/{sid}/edit")
    check("edit form 200", g.status_code == 200)
    check("edit form has datetime field", 'name="occurred_at"' in g.text)
    check("edit form has item price field", 'name="item_price"' in g.text)

    # POST full edit:
    #  - change date to 2026-01-15 09:30
    #  - assign customer, mark unpaid
    #  - Sip: qty 2->3, price 55->40 ; Pocari: remove
    #  - add new line: Pocari x2 @ 70
    form = {
        "occurred_at": "2026-01-15T09:30",
        "customer_id": str(cust_id),
        "status": "unpaid",
        "note": "edited note",
        "item_id": [str(sip_iid), str(poc_iid)],
        "item_name": ["Sip Water", "Pocari"],
        "item_qty": ["3", "1"],
        "item_price": ["40", "80"],
        "item_remove": [str(poc_iid)],
        "new_product_id": str(pid2), "new_qty": "2", "new_price": "70",
    }
    c.post(f"/sale/{sid}/edit", data=form)

    with Session(engine) as db:
        s = db.get(M.Transaction, sid)
        check("customer reassigned", s.customer_id == cust_id)
        check("now unpaid/credit", s.is_credit is True and s.status == "credit")
        check("payment_method cleared on unpaid", s.payment_method is None)
        check("note updated", s.note == "edited note")
        # date moved to Jan 2026 (was created ~now)
        loc = s.occurred_at.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Manila"))
        check("date changed to 2026-01-15", loc.strftime("%Y-%m-%d %H:%M") == "2026-01-15 09:30")
        prod_ids = [i.product_id for i in s.items]
        check("pocari original line removed, sip + new pocari remain (2 lines)", len(s.items) == 2)
        sip = [i for i in s.items if i.product_id == pid1][0]
        check("sip qty 2->3", sip.qty == 3)
        check("sip price 55->40", abs(float(sip.unit_price) - 40) < 0.01)
        newpoc = [i for i in s.items if i.product_id == pid2]
        check("new pocari line qty2 @70", len(newpoc) == 1 and newpoc[0].qty == 2 and abs(float(newpoc[0].unit_price)-70) < 0.01)
        check("new total = 3*40 + 2*70 = 260", abs(float(s.total) - 260) < 0.01)

    # ---- movement edit: change date, qty, unit_cost, product ----
    with Session(engine) as db:
        mv = db.query(M.Transaction).filter(M.Transaction.type == "inventory_adjustment").first()
        mid = mv.id
    g = c.get(f"/movement/{mid}/edit")
    check("movement edit 200", g.status_code == 200)
    check("movement edit has date field", 'name="occurred_at"' in g.text)
    c.post(f"/movement/{mid}/edit", data={"occurred_at": "2026-02-01T08:00", "product_id": str(pid2),
                                          "quantity": "77", "direction": "add", "unit_cost": "45", "note": "fixed"})
    with Session(engine) as db:
        mv = db.get(M.Transaction, mid)
        check("movement qty updated to 77", mv.items[0].qty == 77)
        check("movement unit_cost 45", abs(float(mv.items[0].unit_price) - 45) < 0.01)
        check("movement product reassigned", mv.items[0].product_id == pid2)
        loc = mv.occurred_at.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Manila"))
        check("movement date changed", loc.strftime("%Y-%m-%d %H:%M") == "2026-02-01 08:00")

fails = [n for n, ok in results if not ok]
print("\n%d/%d passed" % (sum(1 for _, ok in results if ok), len(results)))
print("FAILURES:", fails if fails else "none")
