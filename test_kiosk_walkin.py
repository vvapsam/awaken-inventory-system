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

    # email format validation
    ck("lookup rejects bad email", not c.post("/api/kiosk/lookup", json={"email":"not-an-email"}).json().get("ok"))
    tb0,_=wk_token(c,KEY)
    be=c.post("/api/kiosk/submit", json={"flow":"walkin","token":tb0,"returning":False,
        "first_name":"A","last_name":"B","email":"bad@","phone":"1","referral":"X",
        "signature":sig(),"plan_id":OPEN,"method":"cash"})
    ck("new-visitor bad email rejected", not be.json().get("ok"))
    # signature required for new visitors
    tb1,_=wk_token(c,KEY)
    ns=c.post("/api/kiosk/submit", json={"flow":"walkin","token":tb1,"returning":False,
        "first_name":"A","last_name":"B","email":"a@b.com","phone":"1","referral":"X",
        "plan_id":OPEN,"method":"cash"})
    ck("new-visitor without signature rejected", not ns.json().get("ok") and "sign" in (ns.json().get("error") or "").lower())

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

    # HYROX self-solo books at seeded ₱1,000 (returning, cash)
    t4,_=wk_token(c,KEY)
    hs=c.post("/api/kiosk/submit", json={"flow":"walkin","token":t4,"returning":True,
        "email":"dom@x.com","plan_id":HY_SS,"method":"cash"})
    ck("HYROX self-solo books @1000", hs.json().get("ok") and hs.json().get("price")==1000)

    # HYROX coach-doubles books at seeded ₱2,500
    t6,_=wk_token(c,KEY)
    hb=c.post("/api/kiosk/submit", json={"flow":"walkin","token":t6,"returning":True,
        "email":"dom@x.com","plan_id":HY_CD,"method":"cash"})
    ck("HYROX coach-doubles books @2500", hb.json().get("ok") and hb.json().get("price")==2500)

    # walk-in BANK needs no screenshot (reservation confirmed at the desk)
    t7,_=wk_token(c,KEY)
    bk=c.post("/api/kiosk/submit", json={"flow":"walkin","token":t7,"returning":True,
        "email":"dom@x.com","plan_id":OPEN,"method":"bank"})
    ck("walk-in bank ok without proof", bk.json().get("ok"))

    # wrong-kind plan (membership id in walk-in) rejected
    t5,_=wk_token(c,KEY)
    wr=c.post("/api/kiosk/submit", json={"flow":"walkin","token":t5,"returning":True,
        "email":"dom@x.com","plan_id":MONTHLY,"method":"cash"})
    ck("wrong-kind plan rejected", not wr.json().get("ok"))

    # admin can zero a HYROX rate to disable it -> booking blocked
    c.post("/login", data={"username":"admin","pin":"123456"})
    c.post("/admin/kiosk", data={"price_%d"%HY_SS:"0"})
    t8,_=wk_token(c,KEY)
    hz=c.post("/api/kiosk/submit", json={"flow":"walkin","token":t8,"returning":True,
        "email":"dom@x.com","plan_id":HY_SS,"method":"cash"})
    ck("zeroed HYROX blocked", not hz.json().get("ok") and "priced" in (hz.json().get("error") or ""))
    ck("blocked submit did NOT consume token", Session(engine).get(M.WaiverToken,t8).used is False)

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
