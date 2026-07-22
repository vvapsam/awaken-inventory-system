import os, io, base64
os.environ["DATABASE_URL"]="postgresql+psycopg2://postgres@/awaken?host=/tmp&port=5433"
os.environ.setdefault("SECRET_KEY","test-secret")
from PIL import Image
from sqlalchemy import text
from app.db import Base, engine
from app import models as M
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.main import app
def sig_dataurl():
    b=io.BytesIO(); Image.new("RGB",(300,120),(255,255,255)).save(b,"PNG")
    return "data:image/png;base64,"+base64.b64encode(b.getvalue()).decode()
res=[]
def ck(n,c): res.append((n,bool(c))); print("PASS" if c else "FAIL",n)
with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with TestClient(app) as c:
    ck("waivers table created", engine.connect().execute(text("SELECT to_regclass('public.waivers')")).scalar() is not None)
    # public page loads, no login
    g=c.get("/waiver")
    ck("/waiver 200", g.status_code==200)
    ck("page has signature canvas", 'id="sig"' in g.text)
    ck("page uses /order-logo", '/order-logo' in g.text)
    ck("no AWAKEN wordmark text brand", 'class="brand"' not in g.text)
    # submit
    r=c.post("/api/waiver", json={"first_name":"Juan","last_name":"Dela Cruz","email":"juan@x.com","phone":"09171234567","signature":sig_dataurl()})
    ck("submit ok", r.json().get("ok"))
    wid=r.json().get("id")
    # validations
    ck("missing name rejected", not c.post("/api/waiver", json={"first_name":"","last_name":"X","email":"a@b.c","phone":"1","signature":sig_dataurl()}).json().get("ok"))
    ck("missing signature rejected", not c.post("/api/waiver", json={"first_name":"A","last_name":"B","email":"a@b.c","phone":"1","signature":""}).json().get("ok"))
    # stored correctly
    with Session(engine) as db:
        w=db.get(M.Waiver, wid)
        ck("stored name", w.first_name=="Juan" and w.last_name=="Dela Cruz")
        ck("stored email/phone", w.email=="juan@x.com" and w.phone=="09171234567")
        ck("signature bytes stored", w.signature and len(w.signature)>50)
    # signature endpoint requires admin
    anon=c.get(f"/waiver-sig/{wid}", follow_redirects=False)
    ck("sig anon -> redirect", anon.status_code in (302,303,307))
    # admin views
    c.post("/login", data={"username":"admin","pin":"123456"})
    lst=c.get("/admin/waivers")
    ck("admin list 200", lst.status_code==200)
    ck("list shows the name", "Dela Cruz" in lst.text)
    img=c.get(f"/waiver-sig/{wid}")
    ck("admin sig 200 png", img.status_code==200 and img.headers.get("content-type","").startswith("image/"))
fails=[n for n,ok in res if not ok]
print("\n%d/%d passed"%(sum(1 for _,ok in res if ok),len(res)),"| FAIL:",fails or "none")
