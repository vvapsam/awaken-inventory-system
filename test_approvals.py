import os, io, json
os.environ["DATABASE_URL"]="postgresql+psycopg2://postgres@/awaken?host=/tmp&port=5433"
os.environ.setdefault("SECRET_KEY","test-secret")
from PIL import Image
from sqlalchemy import text
from app.db import Base, engine
from app import models as M
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.main import app
def png():
    b=io.BytesIO(); Image.new("RGB",(30,30),(210,210,210)).save(b,"PNG"); return b.getvalue()
res=[]
def ck(n,c): res.append((n,bool(c))); print("PASS" if c else "FAIL",n)
with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with Session(engine) as db:
    db.add(M.Product(sku="POC",name="Pocari",selling_price=80,cost_price=50,reorder_point=3)); db.commit()
with TestClient(app) as c:
    # need settings row + logo etc? place_order works without. Get product id via public bootstrap
    ob=c.get("/api/order/bootstrap").json()
    pid=ob["products"][0]["id"] if ob.get("products") else None
    # restock as admin
    c.post("/login",data={"username":"admin","pin":"123456"})
    c.post("/movement/new",data={"movement_type":"restock","product_id":pid,"quantity":100,"unit_cost":"50"})
    c.get("/logout")
    # place two public orders: cash (no proof) + bank (proof)
    r1=c.post("/api/order",data={"items":json.dumps([{"product_id":pid,"qty":2}]),"name":"Juan Cruz","method":"cash"})
    ck("cash order placed",r1.json().get("ok"))
    r2=c.post("/api/order",data={"items":json.dumps([{"product_id":pid,"qty":1}]),"name":"Maria S","method":"bank"},files={"proof":("p.png",png(),"image/png")})
    ck("bank order placed",r2.json().get("ok"))
    # admin views approvals
    c.post("/login",data={"username":"admin","pin":"123456"})
    mo=c.get("/api/m/orders").json()
    ck("m/orders ok",mo.get("ok"))
    ck("2 pending",mo.get("count")==2)
    ords={o["customer"]:o for o in mo["orders"]}
    ck("bank order has proof",ords["Maria S"]["has_proof"] is True)
    ck("cash order no proof",ords["Juan Cruz"]["has_proof"] is False)
    ck("summary shows qty",("×" in ords["Juan Cruz"]["summary"]))
    # bootstrap badge
    boot=c.get("/api/m/bootstrap").json()
    ck("bootstrap orders_pending=2",boot.get("orders_pending")==2)
    ck("perms.orders true",boot["perms"].get("orders") is True)
    # confirm the cash order -> becomes sale, stock 100-2=98
    cash_id=ords["Juan Cruz"]["id"]
    cr=c.post(f"/api/m/orders/{cash_id}/confirm").json()
    ck("confirm ok",cr.get("ok"))
    with Session(engine) as db:
        o=db.get(M.Transaction,cash_id)
        ck("order now confirmed",o.status=="confirmed")
        ck("converted to a sale",o.converted_id is not None)
        sale=db.get(M.Transaction,o.converted_id)
        ck("sale is cash_sale paid",sale.type=="cash_sale" and sale.status=="paid")
    # stock check via mobile bootstrap
    boot=c.get("/api/m/bootstrap").json()
    onhand={p['name']:p['on_hand'] for p in boot['products']}
    ck("stock deducted 100-2=98",onhand["Pocari"]==98)
    ck("orders_pending now 1",boot.get("orders_pending")==1)
    ck("confirmed order shows in recent",any(s["summary"].count("×")>=1 for s in boot.get("recent",[])))
    # reject the bank order
    bank_id=ords["Maria S"]["id"]
    rj=c.post(f"/api/m/orders/{bank_id}/reject").json()
    ck("reject ok",rj.get("ok"))
    with Session(engine) as db:
        ck("order rejected",db.get(M.Transaction,bank_id).status=="rejected")
    ck("no pending left",c.get("/api/m/orders").json()["count"]==0)
    # double-confirm guard
    dc=c.post(f"/api/m/orders/{cash_id}/confirm").json()
    ck("re-confirm blocked",not dc.get("ok"))
fails=[n for n,ok in res if not ok]
print("\n%d/%d passed"%(sum(1 for _,ok in res if ok),len(res)),"| FAIL:",fails or "none")
