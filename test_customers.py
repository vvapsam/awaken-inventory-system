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
    b=io.BytesIO(); Image.new("RGB",(30,30),(200,200,200)).save(b,"PNG"); return b.getvalue()
res=[]
def ck(n,c): res.append((n,bool(c))); print("PASS" if c else "FAIL",n)
with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with Session(engine) as db:
    db.add(M.Product(sku="POC",name="Pocari",selling_price=80,cost_price=50,reorder_point=3)); db.commit()
with TestClient(app) as c:
    c.post("/login",data={"username":"admin","pin":"123456"})
    pid=c.get("/api/m/bootstrap").json()["products"][0]["id"]
    c.post("/movement/new",data={"movement_type":"restock","product_id":pid,"quantity":100,"unit_cost":"50"})
    c.post("/admin/staff/new",data={"name":"Dom Roque","person_type":"customer","has_access":"off"})
    with Session(engine) as db: dom=db.query(M.Staff).filter_by(name="Dom Roque").first().id
    # credit + cash sale to Dom
    c.post("/api/m/sale",data={"items":json.dumps([{"product_id":pid,"quantity":2}]),"payment":"unpaid","customer_id":str(dom)})
    c.post("/api/m/sale",data={"items":json.dumps([{"product_id":pid,"quantity":1}]),"payment":"cash","customer_id":str(dom)},files={"proof":("p.png",png(),"image/png")})
    d=c.get(f"/api/m/customer/{dom}").json()
    ck("detail ok",d.get("ok"))
    ck("balance = 160 (2x80 credit)",abs(d["balance"]-160)<0.01)
    ck("history has 2 purchases",d.get("purchases")==2 and len(d["history"])==2)
    ck("history rows have datelabel",all(h.get("datelabel") for h in d["history"]))
    ck("history has a pay tag",all(h.get("pay") for h in d["history"]))
    # self-checkout: place + confirm an order, check sc flag in general history
    c.get("/logout")
    c.post("/api/order",data={"items":json.dumps([{"product_id":pid,"qty":1}]),"name":"Walk-in Joe","method":"cash"})
    c.post("/login",data={"username":"admin","pin":"123456"})
    oid=c.get("/api/m/orders").json()["orders"][0]["id"]
    c.post(f"/api/m/orders/{oid}/confirm")
    hist=c.get("/api/m/history").json()["sales"]
    sc=[s for s in hist if s.get("sc")]
    ck("confirmed self-checkout tagged sc",len(sc)>=1)
    ck("normal sale not tagged sc",any(not s.get("sc") for s in hist))
fails=[n for n,ok in res if not ok]
print("\n%d/%d passed"%(sum(1 for _,ok in res if ok),len(res)),"| FAIL:",fails or "none")
