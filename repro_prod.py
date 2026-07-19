"""Reproduce the production schema exactly: entity is the renamed pre-existing
`staff` table that LACKS the new columns (corkage_rate, affiliate_id) and
transactions LACKS subtype. Prior code never created these columns, and
create_all won't add them to an existing table -- only the additive ALTERs do.
Without the fix, startup crashes here. With it, it succeeds."""
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

# Build a fresh schema, then MUTATE it to look like old production:
with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with engine.begin() as conn:
    # entity -> staff (as prod had it), and strip the NEW columns so it matches
    # the pre-merge production schema.
    conn.execute(text("ALTER TABLE entity RENAME TO staff"))
    conn.execute(text("ALTER TABLE staff DROP COLUMN corkage_rate"))
    conn.execute(text("ALTER TABLE staff DROP COLUMN affiliate_id"))
    conn.execute(text("ALTER TABLE transactions DROP COLUMN subtype"))
    # legacy tables that still exist on prod (post transactions-merge)
    conn.execute(text("CREATE TABLE customers(id SERIAL PRIMARY KEY, name VARCHAR, phone VARCHAR, created_at TIMESTAMPTZ DEFAULT now())"))
    conn.execute(text("CREATE TABLE members(id SERIAL PRIMARY KEY, name VARCHAR, coach_id INT, corkage_rate NUMERIC(10,2), start_date DATE, is_active BOOLEAN DEFAULT TRUE)"))
    conn.execute(text("CREATE TABLE stock_movements(id SERIAL PRIMARY KEY, product_id INT, movement_type VARCHAR, quantity INT, unit_cost NUMERIC(10,2), note TEXT, staff_id INT, occurred_at TIMESTAMPTZ DEFAULT now(), created_at TIMESTAMPTZ DEFAULT now())"))
    # a real product, a real affiliate (staff), a customer, a member, movements, a credit tx
    conn.execute(text("INSERT INTO products(sku,name,unit,selling_price,cost_price,reorder_point,is_active) VALUES ('SIP','Sip','each',55,30,10,true)"))
    pid = conn.execute(text("SELECT id FROM products WHERE sku='SIP'")).scalar()
    conn.execute(text("INSERT INTO staff(name,person_type,has_access,role,permissions,affiliate_fee,is_active) VALUES ('Coach Ann','affiliate',false,'staff','',3000,true)"))
    aff = conn.execute(text("SELECT id FROM staff WHERE name='Coach Ann'")).scalar()
    conn.execute(text("INSERT INTO customers(name,phone) VALUES ('Ken Reyes','+63900')"))
    cust = conn.execute(text("SELECT id FROM customers WHERE name='Ken Reyes'")).scalar()
    conn.execute(text("INSERT INTO members(name,coach_id,corkage_rate,is_active) VALUES ('Client A',:a,3000,true)"), {"a": aff})
    conn.execute(text("INSERT INTO stock_movements(product_id,movement_type,quantity,unit_cost) VALUES (:p,'restock',100,30)"), {"p": pid})
    conn.execute(text("INSERT INTO stock_movements(product_id,movement_type,quantity) VALUES (:p,'waste',-5)"), {"p": pid})
    conn.execute(text("INSERT INTO transactions(type,status,customer_id,is_credit,discounted_qty,is_void) VALUES ('cash_sale','credit',:c,true,0,false)"), {"c": cust})

# Boot the app -> runs the startup migrations against this prod-like schema.
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    check("startup did not crash (healthz ok)", c.get("/healthz").json().get("ok"))
    diag = c.get("/diag/tables").json()
    check("new diag format live", "entity_by_person_type" in diag)
    print("DIAG:", diag)

check("legacy tables all dropped", not any(ex(t) for t in ["staff","customers","members","stock_movements"]))
with Session(engine) as db:
    ken = db.query(M.Staff).filter(M.Staff.name=="Ken Reyes").first()
    mem = db.query(M.Staff).filter(M.Staff.name=="Client A").first()
    tx = db.query(M.Transaction).filter(M.Transaction.type=="cash_sale").first()
    check("customer folded", ken and ken.person_type=="customer")
    check("member folded w/ affiliate+corkage", mem and mem.person_type=="member" and mem.affiliate_id==aff and float(mem.corkage_rate)==3000)
    check("tx.customer_id remapped", tx.customer_id==ken.id)
    adj = db.query(M.Transaction).filter(M.Transaction.type=="inventory_adjustment").all()
    check("2 inventory_adjustment txns", len(adj)==2)
    check("subtypes restock+waste", sorted(a.subtype for a in adj)==["restock","waste"])
    from app.main import stock_levels
    oh = {r["product"].id: r["on_hand"] for r in stock_levels(db)}
    check("on_hand 100-5=95", oh.get(pid)==95)

print()
bad = [n for n,ok in results if not ok]
print(f"{len(results)-len(bad)}/{len(results)} passed")
if bad: print("FAILED:", bad); raise SystemExit(1)
