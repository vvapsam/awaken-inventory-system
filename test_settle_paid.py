import os, io, json, time
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
    c.post("/movement/new",data={"movement_type":"restock","product_id":pid,"quantity":200,"unit_cost":"50"})
    c.post("/admin/staff/new",data={"name":"Dom Roque","person_type":"customer","has_access":"off"})
    dom=Session(engine).query(M.Staff).filter_by(name="Dom Roque").first().id
    # two credit sales (80 and 160)
    c.post("/api/m/sale",data={"items":json.dumps([{"product_id":pid,"quantity":1}]),"payment":"unpaid","customer_id":str(dom)})
    c.post("/api/m/sale",data={"items":json.dumps([{"product_id":pid,"quantity":2}]),"payment":"unpaid","customer_id":str(dom)})
    d=c.get(f"/api/m/customer/{dom}").json()
    ck("balance 240 before settle",abs(d["balance"]-240)<0.01)
    ck("2 unpaid orders before",len(d["orders"])==2)
    ck("history both unpaid before",all(h["pay"]=="unpaid" for h in d["history"]))
    # settle full
    r=c.post("/api/m/settle",data={"customer_id":str(dom),"method":"cash"},files={"screenshot":("p.png",png(),"image/png")})
    ck("settle ok",r.json().get("ok"))
    d=c.get(f"/api/m/customer/{dom}").json()
    ck("balance 0 after settle",abs(d["balance"])<0.01)
    ck("no unpaid orders after settle",len(d["orders"])==0)
    ck("history both PAID after settle",all(h["pay"]=="paid" for h in d["history"]))
    # global history shows paid too
    hist=c.get("/api/m/history").json()["sales"]
    credit=[s for s in hist if s["customer"]=="Dom Roque"]
    ck("global history shows paid",credit and all(s["pay"]=="paid" for s in credit))
    # customer drops off owing list
    boot=c.get("/api/m/bootstrap").json()
    owing=[x["name"] for x in boot["balances"]["customers"]]
    ck("Dom no longer owing",("Dom Roque" not in owing))
    # settling again -> nothing
    r2=c.post("/api/m/settle",data={"customer_id":str(dom),"method":"cash"},files={"screenshot":("p.png",png(),"image/png")})
    ck("nothing to settle when 0",not r2.json().get("ok"))
    # partial-coverage math check (direct): new customer, 2 sales, one payment covering only first
    c.post("/admin/staff/new",data={"name":"Mel","person_type":"customer","has_access":"off"})
    mel=Session(engine).query(M.Staff).filter_by(name="Mel").first().id
    c.post("/api/m/sale",data={"items":json.dumps([{"product_id":pid,"quantity":1}]),"payment":"unpaid","customer_id":str(mel)})  # 80 (older)
    time.sleep(0.01)
    c.post("/api/m/sale",data={"items":json.dumps([{"product_id":pid,"quantity":2}]),"payment":"unpaid","customer_id":str(mel)})  # 160 (newer)
    # inject a partial payment of 80 directly (simulating legacy)
    with Session(engine) as db:
        p=M.Transaction(type="payment", customer_id=mel, payment_method="cash", status="paid")
        db.add(p); db.flush()
        db.add(M.TransactionItem(transaction_id=p.id, name="Payment received", qty=1, unit_price=80))
        db.commit()
    d=c.get(f"/api/m/customer/{mel}").json()
    ck("partial: balance 160",abs(d["balance"]-160)<0.01)
    paidn=[h for h in d["history"] if h["pay"]=="paid"]; unpaidn=[h for h in d["history"] if h["pay"]=="unpaid"]
    ck("partial: oldest (80) marked paid",len(paidn)==1 and abs(paidn[0]["total"]-80)<0.01)
    ck("partial: newer (160) still unpaid",len(unpaidn)==1 and abs(unpaidn[0]["total"]-160)<0.01)
fails=[n for n,ok in res if not ok]
print("\n%d/%d passed"%(sum(1 for _,ok in res if ok),len(res)),"| FAIL:",fails or "none")
