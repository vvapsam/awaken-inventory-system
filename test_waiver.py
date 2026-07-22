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
from app.waiver import _waiver_key
def sig():
    b=io.BytesIO(); Image.new("RGB",(200,80),(255,255,255)).save(b,"PNG"); return "data:image/png;base64,"+base64.b64encode(b.getvalue()).decode()
res=[]
def ck(n,c): res.append((n,bool(c))); print("PASS" if c else "FAIL",n)
with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with Session(engine) as db: KEY=_waiver_key(db)
with TestClient(app) as c:
    # gate: no key -> page shows "front desk", no form
    g0=c.get("/waiver")
    ck("no key -> gate (no form)", "front desk" in g0.text and 'id="waiver-form"' not in g0.text)
    # valid key -> form
    g1=c.get("/waiver?k="+KEY)
    ck("valid key -> form", 'id="waiver-form"' in g1.text)
    ck("form has referral select", 'id="referral"' in g1.text)
    ck("form has emergency fields", 'id="ename"' in g1.text and 'id="ephone"' in g1.text)
    # submit without key -> 403
    bad=c.post("/api/waiver", json={"first_name":"A","last_name":"B","email":"a@b.c","phone":"1","referral":"Facebook","signature":sig()})
    ck("submit without key rejected", bad.status_code==403 and not bad.json().get("ok"))
    # submit with bad key -> 403
    bad2=c.post("/api/waiver", json={"key":"WRONG","first_name":"A","last_name":"B","email":"a@b.c","phone":"1","referral":"Facebook","signature":sig()})
    ck("wrong key rejected", bad2.status_code==403)
    # referral required
    nr=c.post("/api/waiver", json={"key":KEY,"first_name":"A","last_name":"B","email":"a@b.c","phone":"1","referral":"","signature":sig()})
    ck("referral required", not nr.json().get("ok"))
    # valid submit
    ok=c.post("/api/waiver", json={"key":KEY,"first_name":"Juan","last_name":"Cruz","email":"j@x.com","phone":"0917","referral":"Instagram","emergency_name":"Maria","emergency_phone":"0918","signature":sig()})
    ck("valid submit ok", ok.json().get("ok"))
    with Session(engine) as db:
        w=db.query(M.Waiver).first()
        ck("stored referral", w.referral=="Instagram")
        ck("stored emergency", w.emergency_name=="Maria" and w.emergency_phone=="0918")
        ck("stored ip", bool(w.ip))
    # rate limit: same IP, push to the cap
    made=1
    for i in range(10):
        r=c.post("/api/waiver", json={"key":KEY,"first_name":"X"+str(i),"last_name":"Y","email":"x@y.z","phone":"1","referral":"Other","signature":sig()})
        if r.status_code==429: break
        made+=1
    ck("rate limit kicks in (<=8)", made<=8)
    # admin
    c.post("/login", data={"username":"admin","pin":"123456"})
    a=c.get("/admin/waivers")
    ck("admin page 200 + QR", a.status_code==200 and 'data:image/png;base64' in a.text and 'Scan to sign' in a.text)
    ck("admin shows source breakdown", 'How members found us' in a.text and 'Instagram' in a.text)
    with Session(engine) as db: wid=db.query(M.Waiver).first().id
    # delete
    c.post(f"/admin/waivers/{wid}/delete")
    with Session(engine) as db: ck("delete works", db.get(M.Waiver, wid) is None)
    # rotate key changes it
    before=KEY; c.post("/admin/waiver/rotate-key")
    with Session(engine) as db: ck("rotate changes key", _waiver_key(db)!=before)
fails=[n for n,ok in res if not ok]
print("\n%d/%d passed"%(sum(1 for _,ok in res if ok),len(res)),"| FAIL:",fails or "none")
