import os
os.environ["DATABASE_URL"] = "postgresql+psycopg2://postgres@/awaken?host=/tmp&port=5433"
os.environ.setdefault("SECRET_KEY", "test-secret")
from sqlalchemy import text
from app.db import Base, engine
from app import models as M
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.main import app

results = []
def check(n, c): results.append((n, bool(c))); print("PASS" if c else "FAIL", n)

with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with Session(engine) as db:
    p = M.Product(sku="SIP", name="Sip Water", selling_price=55, cost_price=30); db.add(p); db.commit(); pid = p.id

with TestClient(app) as c:
    c.post("/login", data={"username": "admin", "pin": "123456"})
    # two levels
    c.post("/admin/pricing/new", data={"name": "Employee"})
    c.post("/admin/pricing/new", data={"name": "Affiliate"})
    with Session(engine) as db:
        emp = db.query(M.PricingGroup).filter_by(name="Employee").first().id
        aff = db.query(M.PricingGroup).filter_by(name="Affiliate").first().id

    # GET item edit form shows the levels table
    g = c.get(f"/admin/products/{pid}/edit")
    check("edit form 200", g.status_code == 200)
    check("has field groups (fieldset)", "<fieldset" in g.text and "Price levels" in g.text)
    check("has level_price input for Employee", f'name="level_price_{emp}"' in g.text)

    # POST edit: set Employee=40, leave Affiliate blank, change base to 60
    c.post(f"/admin/products/{pid}/edit", data={
        "name": "Sip Water", "unit": "each", "selling_price": "60", "cost_price": "30",
        "reorder_point": "5", "is_active": "on",
        f"level_price_{emp}": "40", f"level_price_{aff}": "",
    })
    with Session(engine) as db:
        prod = db.get(M.Product, pid)
        check("base price updated to 60", abs(float(prod.selling_price) - 60) < 0.01)
        empg = db.get(M.PricingGroup, emp)
        affg = db.get(M.PricingGroup, aff)
        check("Employee price_for = 40", abs(empg.price_for(prod) - 40) < 0.01)
        check("Affiliate blank -> base 60", abs(affg.price_for(prod) - 60) < 0.01)
        check("only one PGItem row for this product", db.query(M.PricingGroupItem).filter_by(product_id=pid).count() == 1)

    # POST edit again: blank the Employee price -> should revert to base (row deleted)
    c.post(f"/admin/products/{pid}/edit", data={
        "name": "Sip Water", "unit": "each", "selling_price": "60", "cost_price": "30",
        "reorder_point": "5", "is_active": "on",
        f"level_price_{emp}": "", f"level_price_{aff}": "",
    })
    with Session(engine) as db:
        check("blanking Employee removes the override", db.query(M.PricingGroupItem).filter_by(product_id=pid).count() == 0)

    # NEW item with a level price set at creation
    c.post("/admin/products/new", data={
        "sku": "POC", "name": "Pocari", "unit": "each", "selling_price": "80",
        "cost_price": "50", "reorder_point": "0",
        f"level_price_{aff}": "68",
    })
    with Session(engine) as db:
        poc = db.query(M.Product).filter_by(sku="POC").first()
        check("new item created", poc is not None)
        affg = db.get(M.PricingGroup, aff)
        check("new item affiliate price 68", abs(affg.price_for(poc) - 68) < 0.01)

fails = [n for n, ok in results if not ok]
print("\n%d/%d passed" % (sum(1 for _, ok in results if ok), len(results)))
print("FAILURES:", fails if fails else "none")
