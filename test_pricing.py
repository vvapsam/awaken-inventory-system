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
    # stock both
    c.post("/movement/new", data={"movement_type": "restock", "product_id": pid1, "quantity": 100, "unit_cost": "30"})
    c.post("/movement/new", data={"movement_type": "restock", "product_id": pid2, "quantity": 100, "unit_cost": "50"})

    # --- create a price level ---
    c.post("/admin/pricing/new", data={"name": "Affiliate"})
    with Session(engine) as db:
        g = db.query(M.PricingGroup).filter(M.PricingGroup.name == "Affiliate").first()
        gid = g.id
    check("level created", gid is not None)

    # --- save matrix: Affiliate price for Sip=40, Pocari left blank(=base) ---
    c.post("/admin/pricing/save", data={f"price_{gid}_{pid1}": "40", f"price_{gid}_{pid2}": ""})
    with Session(engine) as db:
        g = db.get(M.PricingGroup, gid)
        pm = g.price_map()
        check("Sip override stored = 40", pm.get(pid1) == 40.0)
        check("Pocari blank -> no override", pid2 not in pm)
        p1 = db.get(M.Product, pid1); p2 = db.get(M.Product, pid2)
        check("price_for Sip = 40", g.price_for(p1) == 40.0)
        check("price_for Pocari = base 80", g.price_for(p2) == 80.0)

    # --- assign an entity to the level ---
    c.post("/admin/staff/new", data={"name": "Aff Guy", "person_type": "affiliate",
                                      "pricing_group_id": str(gid), "has_access": "off"})
    with Session(engine) as db:
        aff = db.query(M.Staff).filter(M.Staff.name == "Aff Guy").first()
        aff_id = aff.id
        check("entity assigned to level", aff.pricing_group_id == gid)

    # base buyer (no level)
    c.post("/admin/staff/new", data={"name": "Reg Cust", "person_type": "customer", "has_access": "off"})
    with Session(engine) as db:
        reg = db.query(M.Staff).filter(M.Staff.name == "Reg Cust").first()
        reg_id = reg.id
        check("base buyer has no level", reg.pricing_group_id is None)

    # --- bootstrap: products carry per-level prices; customers carry level ---
    boot = c.get("/api/m/bootstrap").json()
    prods = {p["name"]: p for p in boot["products"]}
    check("boot Sip has affiliate override", prods["Sip Water"]["prices"].get(str(gid)) == 40.0)
    check("boot Pocari has no override", str(gid) not in prods["Pocari"]["prices"])
    custs = {c2["name"]: c2 for c2 in boot["customers"]}
    check("boot Aff Guy level = gid", custs["Aff Guy"]["level"] == gid)
    check("boot Aff Guy level_name", custs["Aff Guy"]["level_name"] == "Affiliate")
    check("boot Reg Cust level None", custs["Reg Cust"]["level"] is None)

    # --- sale to affiliate (unpaid): Sip x2 + Pocari x1 => 40*2 + 80 = 160 ---
    r = c.post("/api/m/sale", data={"items": json.dumps([{"product_id": pid1, "quantity": 2},
                                                         {"product_id": pid2, "quantity": 1}]),
                                    "payment": "unpaid", "customer_id": str(aff_id)})
    j = r.json()
    check("aff sale ok", j.get("ok"))
    check("aff sale total = 160", abs(j.get("total", 0) - 160) < 0.005)
    with Session(engine) as db:
        s = db.query(M.Transaction).filter(M.Transaction.customer_id == aff_id).first()
        check("sale records level id", s.pricing_group_id == gid)
        prices = sorted(float(i.unit_price) for i in s.items)
        check("line prices [40,80]", prices == [40.0, 80.0])
        qtys = {float(i.unit_price): i.qty for i in s.items}
        check("Sip qty2 @40", qtys.get(40.0) == 2)
        check("Pocari qty1 @80", qtys.get(80.0) == 1)

    # --- cash sale to base buyer: Sip x1 => base 55 ---
    r = c.post("/api/m/sale", data={"items": json.dumps([{"product_id": pid1, "quantity": 1}]),
                                    "payment": "cash", "customer_id": str(reg_id)},
               files={"proof": ("p.png", png(), "image/png")})
    j = r.json()
    check("base sale total = 55", abs(j.get("total", 0) - 55) < 0.005)

    # --- walk-in cash sale (no buyer): base price ---
    r = c.post("/api/m/sale", data={"items": json.dumps([{"product_id": pid1, "quantity": 1}]),
                                    "payment": "cash"},
               files={"proof": ("p.png", png(), "image/png")})
    check("walk-in base total = 55", abs(r.json().get("total", 0) - 55) < 0.005)

    # --- unpaid without buyer rejected ---
    r = c.post("/api/m/sale", data={"items": json.dumps([{"product_id": pid1, "quantity": 1}]),
                                    "payment": "unpaid"})
    check("unpaid needs buyer -> err", not r.json().get("ok"))

    # --- delete level nulls entity assignment ---
    c.post(f"/admin/pricing/{gid}/delete")
    with Session(engine) as db:
        aff = db.get(M.Staff, aff_id)
        check("delete level -> entity level cleared", aff.pricing_group_id is None)
        check("delete level -> group gone", db.get(M.PricingGroup, gid) is None)

fails = [n for n, ok in results if not ok]
print("\n%d/%d passed" % (sum(1 for _, ok in results if ok), len(results)))
print("FAILURES:", fails if fails else "none")
