import os, io, json
os.environ["DATABASE_URL"] = "postgresql+psycopg2://postgres@/awaken?host=/tmp&port=5433"
os.environ["ADMIN_INITIAL_PIN"] = "123456"
os.environ.setdefault("SECRET_KEY", "test-secret")

from PIL import Image
from sqlalchemy import text
from app.db import Base, engine
from app import models as M
from sqlalchemy.orm import Session

results = []
def check(n, c): results.append((n, bool(c))); print("PASS" if c else "FAIL", n)
def ex(t):
    with engine.connect() as c: return c.execute(text("SELECT to_regclass(:n)"), {"n": "public." + t}).scalar() is not None
def png():
    b = io.BytesIO(); Image.new("RGB", (20, 20), (200, 200, 200)).save(b, "PNG"); return b.getvalue()

from fastapi.testclient import TestClient
from app.main import app

# ================= PART A: fresh flows =================
with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with Session(engine) as db:
    p = M.Product(sku="SIP", name="Sip", selling_price=55, cost_price=30); db.add(p); db.commit(); pid = p.id

with TestClient(app) as c:
    check("A: legacy tables absent", not any(ex(t) for t in ["staff","customers","members","stock_movements","sales","payments"]))
    check("A: entity + transactions present", ex("entity") and ex("transactions"))
    c.post("/login", data={"username": "admin", "pin": "123456"})
    # receive stock 100 via desktop movement
    c.post("/movement/new", data={"movement_type": "restock", "product_id": pid, "quantity": 100, "unit_cost": "30"})
    lv = M.__dict__  # noqa
    with Session(engine) as db:
        from app.main import stock_levels
        oh = {r["product"].id: r["on_hand"] for r in stock_levels(db)}
    check("A: restock -> on_hand 100", oh[pid] == 100)
    with Session(engine) as db:
        check("A: restock is inventory_adjustment", db.query(M.Transaction).filter(M.Transaction.type=="inventory_adjustment", M.Transaction.subtype=="restock").count() == 1)
    # mobile sale 3
    r = c.post("/api/m/sale", data={"items": json.dumps([{"product_id": pid, "quantity": 3}]), "payment": "cash"},
               files={"proof": ("p.png", png(), "image/png")})
    check("A: sale ok", r.json().get("ok"))
    boot = c.get("/api/m/bootstrap").json()
    check("A: on_hand 100-3=97", boot["products"][0]["on_hand"] == 97)
    # mobile loss 2 (waste)
    c.post("/api/m/movement", data={"kind": "loss", "product_id": pid, "quantity": 2, "memo": "Spoiled"})
    boot = c.get("/api/m/bootstrap").json()
    check("A: after loss 97-2=95", boot["products"][0]["on_hand"] == 95)
    # customer credit sale + payment
    with Session(engine) as db:
        c.post("/api/m/sale", data={"items": json.dumps([{"product_id": pid, "quantity": 1}]), "payment": "unpaid", "customer_id": ""})  # no cust -> err, ignore
    # create customer via api then credit sale
    r = c.post("/api/m/customers", data={"name": "Ken Reyes", "phone": "+63900"})
    cust_id = r.json().get("customer", {}).get("id")
    with Session(engine) as db:
        check("A: customer is entity", db.query(M.Staff).filter(M.Staff.id==cust_id, M.Staff.person_type=="customer").first() is not None)
    c.post("/api/m/sale", data={"items": json.dumps([{"product_id": pid, "quantity": 2}]), "payment": "unpaid", "customer_id": str(cust_id)})
    c.post(f"/customer/{cust_id}/pay", data={"amount": "50", "method": "cash"})
    from app.main import customer_balances
    with Session(engine) as db:
        bal = next(r for r in customer_balances(db) if r["customer"].id == cust_id)
    check("A: customer balance 110-50=60", abs(bal["balance"] - 60) < 0.01)
    # affiliate + member + corkage billing
    c.post("/admin/staff/new", data={"name": "Coach Ann", "person_type": "affiliate", "affiliate_fee": "3000", "next_billing": "2026-08-01"})
    with Session(engine) as db:
        ann = db.query(M.Staff).filter(M.Staff.name == "Coach Ann").first()
    c.post("/coaches/members/new", data={"name": "Client A", "coach_id": str(ann.id), "corkage_rate": "3000", "is_active": "on"})
    with Session(engine) as db:
        mem = db.query(M.Staff).filter(M.Staff.name == "Client A").first()
    check("A: member is entity type member", mem and mem.person_type == "member" and mem.affiliate_id == ann.id)
    r = c.post(f"/coaches/billing/bill/{ann.id}", follow_redirects=False)
    check("A: billing runs", r.status_code == 303)
    with Session(engine) as db:
        inv = db.query(M.Transaction).filter(M.Transaction.type == "invoice").order_by(M.Transaction.id.desc()).first()
        check("A: corkage invoice 3000+3000=6000", abs(inv.total - 6000) < 0.01)
    # pages render
    for path in ["/dashboard", "/records", "/sales", "/invoices", "/orders", "/payments",
                 "/customers", f"/customer/{cust_id}", "/admin/reports", "/admin/inventory-value",
                 "/coaches/members", "/coaches/billing", "/admin/staff?type=member", "/stock", "/m"]:
        check(f"A: GET {path}", c.get(path).status_code == 200)

# ================= PART B: production-like migration =================
engine.dispose()
with engine.begin() as conn: conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with engine.begin() as conn:
    conn.execute(text("ALTER TABLE entity RENAME TO staff"))
    conn.execute(text("CREATE TABLE customers(id SERIAL PRIMARY KEY, name VARCHAR, phone VARCHAR, created_at TIMESTAMPTZ DEFAULT now())"))
    conn.execute(text("CREATE TABLE members(id SERIAL PRIMARY KEY, name VARCHAR, coach_id INT, corkage_rate NUMERIC(10,2), start_date DATE, is_active BOOLEAN DEFAULT TRUE)"))
    conn.execute(text("CREATE TABLE stock_movements(id SERIAL PRIMARY KEY, product_id INT, movement_type VARCHAR, quantity INT, unit_cost NUMERIC(10,2), note TEXT, staff_id INT, occurred_at TIMESTAMPTZ DEFAULT now(), created_at TIMESTAMPTZ DEFAULT now())"))
    conn.execute(text("INSERT INTO products(sku,name,unit,selling_price,cost_price,reorder_point,is_active) VALUES ('SIP','Sip','each',55,30,0,true)"))
    pid2 = conn.execute(text("SELECT id FROM products WHERE sku='SIP'")).scalar()
    conn.execute(text("INSERT INTO staff(name,person_type,has_access,role,permissions,affiliate_fee,is_active) VALUES ('Coach Ann','affiliate',false,'staff','',3000,true)"))
    aff = conn.execute(text("SELECT id FROM staff WHERE name='Coach Ann'")).scalar()
    conn.execute(text("INSERT INTO customers(name,phone) VALUES ('Ken Reyes','+63900')"))
    cust = conn.execute(text("SELECT id FROM customers WHERE name='Ken Reyes'")).scalar()
    conn.execute(text("INSERT INTO members(name,coach_id,corkage_rate,is_active) VALUES ('Client A',:a,3000,true)"), {"a": aff})
    conn.execute(text("INSERT INTO stock_movements(product_id,movement_type,quantity,unit_cost) VALUES (:p,'restock',100,30)"), {"p": pid2})
    conn.execute(text("INSERT INTO stock_movements(product_id,movement_type,quantity) VALUES (:p,'waste',-5)"), {"p": pid2})
    conn.execute(text("INSERT INTO transactions(type,status,customer_id,is_credit,discounted_qty,is_void) VALUES ('cash_sale','credit',:c,true,0,false)"), {"c": cust})

with TestClient(app): pass
check("B: all legacy tables dropped", not any(ex(t) for t in ["staff","customers","members","stock_movements"]))
with Session(engine) as db:
    ken = db.query(M.Staff).filter(M.Staff.name == "Ken Reyes").first()
    mem = db.query(M.Staff).filter(M.Staff.name == "Client A").first()
    tx = db.query(M.Transaction).filter(M.Transaction.type == "cash_sale").first()
    check("B: customer folded", ken and ken.person_type == "customer")
    check("B: member folded w/ affiliate+corkage", mem and mem.person_type == "member" and mem.affiliate_id is not None and float(mem.corkage_rate) == 3000)
    check("B: tx.customer_id remapped", tx.customer_id == ken.id)
    adj = db.query(M.Transaction).filter(M.Transaction.type == "inventory_adjustment").all()
    check("B: 2 inventory_adjustment txns", len(adj) == 2)
    subs = sorted(a.subtype for a in adj)
    check("B: subtypes restock+waste preserved", subs == ["restock", "waste"])
    from app.main import stock_levels
    oh = {r["product"].id: r["on_hand"] for r in stock_levels(db)}
    # 100 restock - 5 waste = 95 (no sales of this product)
    check("B: on_hand 100-5=95 from folded movements", oh.get(pid2) == 95)

print()
bad = [n for n, ok in results if not ok]
print(f"{len(results)-len(bad)}/{len(results)} passed")
if bad:
    print("FAILED:", bad); raise SystemExit(1)
