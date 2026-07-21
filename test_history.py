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
    b=io.BytesIO(); Image.new("RGB",(20,20),(200,200,200)).save(b,"PNG"); return b.getvalue()
res=[]
def ck(n,c): res.append((n,bool(c))); print("PASS" if c else "FAIL",n)
with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with Session(engine) as db:
    db.add(M.Product(sku="POC",name="Pocari",selling_price=80,cost_price=50,reorder_point=3))
    db.add(M.Product(sku="SP",name="Sip Pink",selling_price=75,cost_price=40,reorder_point=5))
    db.commit()
with TestClient(app) as c:
    c.post("/login",data={"username":"admin","pin":"123456"})
    boot=c.get("/api/m/bootstrap").json()
    pids={p['name']:p['id'] for p in boot['products']}
    c.post("/movement/new",data={"movement_type":"restock","product_id":pids["Pocari"],"quantity":100,"unit_cost":"50"})
    c.post("/movement/new",data={"movement_type":"restock","product_id":pids["Sip Pink"],"quantity":100,"unit_cost":"40"})
    c.post("/admin/staff/new",data={"name":"Dom Roque","person_type":"customer","has_access":"off"})
    with Session(engine) as db: dom=db.query(M.Staff).filter_by(name="Dom Roque").first().id
    # cash sale
    c.post("/api/m/sale",data={"items":json.dumps([{"product_id":pids["Pocari"],"quantity":2},{"product_id":pids["Sip Pink"],"quantity":1}]),"payment":"cash"},files={"proof":("p.png",png(),"image/png")})
    # bank sale
    c.post("/api/m/sale",data={"items":json.dumps([{"product_id":pids["Pocari"],"quantity":1}]),"payment":"bank"},files={"proof":("p.png",png(),"image/png")})
    # unpaid credit sale to Dom
    c.post("/api/m/sale",data={"items":json.dumps([{"product_id":pids["Sip Pink"],"quantity":3}]),"payment":"unpaid","customer_id":str(dom)})
    # history endpoint
    h=c.get("/api/m/history").json()
    ck("history ok",h.get("ok"))
    ck("scope all for admin",h.get("scope")=="all")
    ck("today count=3",h["today"]["count"]==3)
    ck("today items=3+1+3=7",h["today"]["items"]==7)
    ck("today total=235+80+225=540",abs(h["today"]["total"]-(235+80+225))<0.01)
    sales=h["sales"]
    ck("3 sales listed",len(sales)==3)
    pays=sorted(s["pay"] for s in sales)
    ck("pays cash/bank/unpaid",pays==["bank","cash","unpaid"])
    unpaid=[s for s in sales if s["pay"]=="unpaid"][0]
    ck("unpaid has customer Dom",unpaid["customer"]=="Dom Roque")
    ck("summary has multiplier",any("×" in s["summary"] for s in sales))
    ck("time present",all(s["time"] for s in sales))
    # bootstrap recent
    boot=c.get("/api/m/bootstrap").json()
    ck("bootstrap recent present",len(boot.get("recent",[]))==3)
    ck("sales_scope all",boot.get("sales_scope")=="all")
fails=[n for n,ok in res if not ok]
print("\n%d/%d passed"%(sum(1 for _,ok in res if ok),len(res)),"| FAIL:",fails or "none")
