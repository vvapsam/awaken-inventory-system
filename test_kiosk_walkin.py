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

def img(fmt="PNG"):
    b=io.BytesIO(); Image.new("RGB",(200,80),(255,255,255)).save(b,fmt)
    return "data:image/%s;base64,%s"%("png" if fmt=="PNG" else "jpeg", base64.b64encode(b.getvalue()).decode())
def sig(): return img("PNG")
def proof(): return img("JPEG")
res=[]
def ck(n,c): res.append((n,bool(c))); print("PASS" if c else "FAIL",n)
def wk_token(c, key):
    html=c.get("/kiosk/walkin?k="+key).text
    m=re.search(r'id="token" value="([^"]*)"', html)
    return m.group(1) if m else "", html

with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)

with TestClient(app) as c:
    with Session(engine) as db:
        KEY=_waiver_key(db)
        wk=db.query(M.KioskPlan).filter(M.KioskPlan.kind==M.KIOSK_WALKIN).all()
        OPEN=[p.id for p in wk if p.activity=="open_gym"][0]
        PRIV=[p.id for p in wk if p.activity=="private"][0]
        HY_SS=[p.id for p in wk if p.activity=="hyrox" and not p.coached and not p.doubles][0]
        HY_CD=[p.id for p in wk if p.activity=="hyrox" and p.coached and p.doubles][0]
        MONTHLY=db.query(M.KioskPlan).filter(M.KioskPlan.kind==M.KIOSK_MEMBERSHIP).first().id

    # page opens directly (no key gate) — reached from the public hub
    html=c.get("/kiosk/walkin").text
    ck("walkin opens to email step (no gate)", 'id="s-email"' in html and "Please scan the QR at the front desk" not in html)
    tok=re.search(r'id="token" value="([^"]*)"', html).group(1)
    ck("walkin issues a one-time token", bool(tok))
    ck("walkin renders activities + hyrox data", "Open Gym" in html and "HYROX" in html)

    # lookup: unknown email -> not found (no key needed)
    lk=c.post("/api/kiosk/lookup", json={"email":"nobody@x.com"})
    ck("lookup unknown -> not found", lk.json().get("ok") and lk.json().get("found") is False)

    # NEW visitor: sign waiver + Open Gym (cash)
    t1,_=wk_token(c,KEY)
    r=c.post("/api/kiosk/submit", json={"flow":"walkin","key":KEY,"token":t1,"returning":False,
        "first_name":"Dom","last_name":"Roque","email":"dom@x.com","phone":"0917000",
        "referral":"Facebook","signature":sig(),"plan_id":OPEN,"method":"cash"})
    ck("new walk-in Open Gym ok @1000", r.json().get("ok") and r.json().get("price")==1000)
    with Session(engine) as db:
        o=db.query(M.Transaction).filter(M.Transaction.subtype=="kiosk_walkin").first()
        ck("walk-in -> pending kiosk_walkin", o is not None and o.status=="pending")
        ck("waiver stored for new walk-in", db.query(M.Waiver).filter(M.Waiver.email=="dom@x.com").first() is not None)
        WOID=o.id
        wcount=db.query(M.Waiver).count()

    # lookup now finds Dom
    lk2=c.post("/api/kiosk/lookup", json={"key":KEY,"email":"dom@x.com"})
    ck("lookup known -> found + name", lk2.json().get("found") and lk2.json().get("first_name")=="Dom")

    # RETURNING: no signature, Private Coaching, matched by email
    t2,_=wk_token(c,KEY)
    rr=c.post("/api/kiosk/submit", json={"flow":"walkin","key":KEY,"token":t2,"returning":True,
        "email":"dom@x.com","plan_id":PRIV,"method":"cash"})
    ck("returning Private ok @2000 (no signature)", rr.json().get("ok") and rr.json().get("price")==2000)
    with Session(engine) as db:
        ck("returning did NOT add a waiver", db.query(M.Waiver).count()==wcount)
        ck("returning name from record", db.query(M.Transaction)
           .filter(M.Transaction.subtype=="kiosk_walkin").order_by(M.Transaction.id.desc()).first().customer_name=="Dom Roque")

    # returning with unknown email -> need_waiver
    t3,_=wk_token(c,KEY)
    nw=c.post("/api/kiosk/submit", json={"flow":"walkin","key":KEY,"token":t3,"returning":True,
        "email":"ghost@x.com","plan_id":OPEN,"method":"cash"})
    ck("returning unknown -> need_waiver 409", nw.status_code==409 and nw.json().get("need_waiver"))

    # HYROX at 0 -> blocked
    t4,_=wk_token(c,KEY)
    hz=c.post("/api/kiosk/submit", json={"flow":"walkin","key":KEY,"token":t4,"returning":True,
        "email":"dom@x.com","plan_id":HY_SS,"method":"cash"})
    ck("HYROX unpriced blocked", not hz.json().get("ok") and "priced" in (hz.json().get("error") or ""))
    ck("blocked submit did NOT consume token", Session(engine).get(M.WaiverToken,t4).used is False)

    # wrong-kind plan (membership id in walk-in) rejected
    t5,_=wk_token(c,KEY)
    wr=c.post("/api/kiosk/submit", json={"flow":"walkin","key":KEY,"token":t5,"returning":True,
        "email":"dom@x.com","plan_id":MONTHLY,"method":"cash"})
    ck("wrong-kind plan rejected", not wr.json().get("ok"))

    # admin sets a HYROX price, then it books
    c.post("/login", data={"username":"admin","pin":"123456"})
    c.post("/admin/kiosk", data={"price_%d"%HY_CD:"2500"})
    with Session(engine) as db:
        ck("admin set HYROX coach/doubles = 2500", float(db.get(M.KioskPlan,HY_CD).price)==2500)
    t6,_=wk_token(c,KEY)
    hb=c.post("/api/kiosk/submit", json={"flow":"walkin","key":KEY,"token":t6,"returning":True,
        "email":"dom@x.com","plan_id":HY_CD,"method":"cash"})
    ck("HYROX books after pricing @2500", hb.json().get("ok") and hb.json().get("price")==2500)

    # approve a walk-in order -> paid sale, NO member created
    c.post("/api/m/orders/%d/confirm"%WOID)
    with Session(engine) as db:
        wo=db.get(M.Transaction, WOID)
        ck("walk-in confirmed -> sale", wo.status=="confirmed" and wo.converted_id)
        ck("walk-in creates NO member", db.query(M.Staff).filter(M.Staff.person_type=="member").count()==0)

    # admin edits Open Gym price
    c.post("/admin/kiosk", data={"name_%d"%OPEN:"Open Gym","sub_%d"%OPEN:"All day","price_%d"%OPEN:"1200"})
    with Session(engine) as db:
        ck("Open Gym price updated to 1200", float(db.get(M.KioskPlan,OPEN).price)==1200)

fails=[n for n,ok in res if not ok]
print("\n%d/%d passed"%(sum(1 for _,ok in res if ok),len(res)),"| FAIL:",fails or "none")
