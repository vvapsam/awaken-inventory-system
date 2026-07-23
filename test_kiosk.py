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

def img(fmt="PNG", mime=None):
    b=io.BytesIO(); Image.new("RGB",(200,80),(255,255,255)).save(b,fmt)
    mime=mime or ("image/png" if fmt=="PNG" else "image/jpeg")
    return "data:%s;base64,%s"%(mime, base64.b64encode(b.getvalue()).decode())
def sig(): return img("PNG")
def proof(): return img("JPEG")
res=[]
def ck(n,c): res.append((n,bool(c))); print("PASS" if c else "FAIL",n)
def token_of(c, flow, key):
    html=c.get("/kiosk/%s?k=%s"%(flow,key)).text
    m=re.search(r'id="token" value="([^"]*)"', html)
    return m.group(1) if m else ""

with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)

with TestClient(app) as c:
    ck("kiosk_plans table exists", engine.connect().execute(text("SELECT to_regclass('public.kiosk_plans')")).scalar() is not None)
    with Session(engine) as db:
        KEY=_waiver_key(db)
        members=db.query(M.KioskPlan).filter(M.KioskPlan.kind==M.KIOSK_MEMBERSHIP).all()
        walkin=db.query(M.KioskPlan).filter(M.KioskPlan.kind==M.KIOSK_WALKIN).all()
        ck("seeded 3 memberships", len(members)==3)
        ck("seeded walk-in activities", len(walkin)==6)
        ck("open gym 1000 + private 2000",
           any(p.activity=="open_gym" and float(p.price)==1000 for p in walkin) and
           any(p.activity=="private" and float(p.price)==2000 for p in walkin))
        hy={(bool(p.coached),bool(p.doubles)):float(p.price) for p in walkin if p.activity=="hyrox"}
        ck("4 HYROX rows priced (1000/1000/3000/2500)", len(hy)==4 and
           hy[(False,False)]==1000 and hy[(False,True)]==1000 and hy[(True,False)]==3000 and hy[(True,True)]==2500)
        MONTHLY=[p.id for p in members if p.name=="Monthly"][0]

    # ---- sign-up flow still works (opens directly, no key gate) ----
    sh=c.get("/kiosk/signup").text
    ck("signup opens (no gate) + shows plans", "Please scan the QR at the front desk" not in sh and "Monthly" in sh and "Quarterly" in sh)
    ts=token_of(c,"signup",KEY)
    s=c.post("/api/kiosk/submit", json={"flow":"signup","key":KEY,"token":ts,
        "first_name":"Mel","last_name":"Te","email":"mel@x.com","phone":"0922",
        "referral":"TikTok","signature":sig(),"plan_id":MONTHLY,"method":"bank","proof":proof()})
    ck("signup submit ok", s.json().get("ok"))
    with Session(engine) as db:
        so=db.query(M.Transaction).filter(M.Transaction.subtype=="kiosk_membership").first()
        ck("signup -> pending kiosk_membership", so is not None and so.status=="pending")
        SIGNUP_OID=so.id
        ck("no member before approval", db.query(M.Staff).filter(M.Staff.person_type=="member").first() is None)

    c.post("/login", data={"username":"admin","pin":"123456"})
    c.post("/api/m/orders/%d/confirm"%SIGNUP_OID)
    with Session(engine) as db:
        mem=db.query(M.Staff).filter(M.Staff.person_type=="member").first()
        ck("signup approval creates member", mem is not None and mem.name=="Mel Te")

    # ---- hub: walk-in + buy are live links; sign-up is coming-soon ----
    hub=c.get("/welcome").text
    ck("hub walk-in + buy are live links", 'href="/kiosk/walkin"' in hub and 'href="/order"' in hub)
    ck("hub sign-up is coming-soon (not a link)", 'href="/kiosk/signup"' not in hub and 'data-soon' in hub)
    ck("hub no pay balance", "Pay my balance" not in hub)

    # ---- admin ----
    a=c.get("/admin/kiosk")
    ck("admin 200 + hub QR", a.status_code==200 and "data:image/png;base64" in a.text)
    ck("admin shows walk-in + HYROX + memberships", "Open Gym" in a.text and "HYROX" in a.text and "membership" in a.text.lower())
    ap=c.get("/admin/kiosk", headers={"host":"pay.awakengym.com"})
    ck("pay host rewritten to portal", "portal.awakengym.com/welcome" in ap.text)
    # edit membership + add one
    c.post("/admin/kiosk", data={"hasactive_%d"%MONTHLY:"1","name_%d"%MONTHLY:"Monthly",
        "sub_%d"%MONTHLY:"30 days","price_%d"%MONTHLY:"1200","active_%d"%MONTHLY:"on",
        "new_name_1":"Weekly","new_price_1":"400"})
    with Session(engine) as db:
        ck("membership price updated", float(db.get(M.KioskPlan,MONTHLY).price)==1200)
        ck("new membership added", db.query(M.KioskPlan).filter(M.KioskPlan.name=="Weekly").first() is not None)

fails=[n for n,ok in res if not ok]
print("\n%d/%d passed"%(sum(1 for _,ok in res if ok),len(res)),"| FAIL:",fails or "none")
