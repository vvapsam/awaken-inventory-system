import os, io, base64, re
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
def get_token(c, key):
    html=c.get("/waiver?k="+key).text
    m=re.search(r'id="token" value="([^"]*)"', html)
    return m.group(1) if m else ""
with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)
with Session(engine) as db: KEY=_waiver_key(db)
with TestClient(app) as c:
    ck("waiver_tokens table exists", engine.connect().execute(text("SELECT to_regclass('public.waiver_tokens')")).scalar() is not None)
    # gate
    ck("no key -> gate", "front desk" in c.get("/waiver").text and 'id="waiver-form"' not in c.get("/waiver").text)
    # valid page issues a token, no emergency fields
    html=c.get("/waiver?k="+KEY).text
    ck("valid page has token", 'id="token" value="' in html and get_token(c,KEY))
    ck("emergency removed from form", 'id="ename"' not in html and 'Emergency contact' not in html)
    # submit needs a valid token
    ntok=c.post("/api/waiver", json={"key":KEY,"first_name":"A","last_name":"B","email":"a@b.c","phone":"1","referral":"Facebook","signature":sig()})
    ck("submit without token -> 410 expired", ntok.status_code==410 and ntok.json().get("expired"))
    # valid submit
    t1=get_token(c,KEY)
    ok=c.post("/api/waiver", json={"key":KEY,"token":t1,"first_name":"Juan","last_name":"Cruz","email":"j@x.com","phone":"0917","referral":"Instagram","signature":sig()})
    ck("valid submit ok", ok.json().get("ok"))
    # replay same token -> expired (one-time)
    replay=c.post("/api/waiver", json={"key":KEY,"token":t1,"first_name":"Juan","last_name":"Cruz","email":"j@x.com","phone":"0917","referral":"Instagram","signature":sig()})
    ck("replay token -> 410 expired", replay.status_code==410 and replay.json().get("expired"))
    with Session(engine) as db:
        w=db.query(M.Waiver).first()
        ck("stored referral", w.referral=="Instagram")
        ck("emergency not stored", w.emergency_name is None and w.emergency_phone is None)
        ck("token consumed (used=True)", db.get(M.WaiverToken, t1).used is True)
    # referral still required
    t2=get_token(c,KEY)
    nr=c.post("/api/waiver", json={"key":KEY,"token":t2,"first_name":"A","last_name":"B","email":"a@b.c","phone":"1","referral":"","signature":sig()})
    ck("referral required", not nr.json().get("ok"))
    ck("failed validation does NOT consume token", not (lambda:(lambda s:s and s.used)(Session(engine).get(M.WaiverToken,t2)))())
    # wrong key rejected
    ck("wrong key rejected", c.post("/api/waiver", json={"key":"WRONG","token":get_token(c,KEY),"first_name":"A","last_name":"B","email":"a@b.c","phone":"1","referral":"X","signature":sig()}).status_code==403)
    # admin: no Emergency column, has QR + delete
    c.post("/login", data={"username":"admin","pin":"123456"})
    a=c.get("/admin/waivers")
    ck("admin 200, QR present", a.status_code==200 and 'data:image/png;base64' in a.text)
    ck("admin no Emergency column", '<th>Emergency</th>' not in a.text)
    with Session(engine) as db: wid=db.query(M.Waiver).first().id
    c.post(f"/admin/waivers/{wid}/delete")
    with Session(engine) as db: ck("delete works", db.get(M.Waiver, wid) is None)
    before=KEY; c.post("/admin/waiver/rotate-key")
    with Session(engine) as db: ck("rotate changes key", _waiver_key(db)!=before)
fails=[n for n,ok in res if not ok]
print("\n%d/%d passed"%(sum(1 for _,ok in res if ok),len(res)),"| FAIL:",fails or "none")
