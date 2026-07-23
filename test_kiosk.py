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

def open_flow(c, flow, key):
    """Open the kiosk page and return its one-time token."""
    html=c.get("/kiosk/%s?k=%s"%(flow,key)).text
    m=re.search(r'id="token" value="([^"]*)"', html)
    return m.group(1) if m else "", html

with engine.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)

with TestClient(app) as c:
    # ---- model + seed ----
    ck("kiosk_plans table exists", engine.connect().execute(text("SELECT to_regclass('public.kiosk_plans')")).scalar() is not None)
    with Session(engine) as db:
        KEY=_waiver_key(db)
        daypasses=db.query(M.KioskPlan).filter(M.KioskPlan.kind==M.KIOSK_DAYPASS).all()
        members=db.query(M.KioskPlan).filter(M.KioskPlan.kind==M.KIOSK_MEMBERSHIP).all()
        ck("seeded 1 day pass", len(daypasses)==1 and float(daypasses[0].price)==150)
        ck("seeded 3 memberships", len(members)==3)
        DP=daypasses[0].id
        MONTHLY=[p.id for p in members if p.name=="Monthly"][0]

    # ---- gate ----
    ck("no key -> gate", "front desk" in c.get("/kiosk/walkin").text and 'id="step1"' not in c.get("/kiosk/walkin").text)
    ck("bad flow -> redirect to /welcome", c.get("/kiosk/nope?k="+KEY, follow_redirects=False).status_code in (303,307))
    tok, html = open_flow(c,"walkin",KEY)
    ck("valid walkin page has token + steps", bool(tok) and 'id="step1"' in html)
    ck("walkin shows day pass price", "150" in html)
    sok, shtml = open_flow(c,"signup",KEY)
    ck("signup shows membership plans", "Monthly" in shtml and "Quarterly" in shtml)

    # ---- submit walk-in (cash) ----
    t1,_=open_flow(c,"walkin",KEY)
    r=c.post("/api/kiosk/submit", json={"flow":"walkin","key":KEY,"token":t1,
        "first_name":"Dom","last_name":"Roque","email":"d@x.com","phone":"0917111",
        "referral":"Facebook","signature":sig(),"plan_id":DP,"method":"cash"})
    ck("walkin cash submit ok", r.json().get("ok") and r.json().get("price")==150)
    with Session(engine) as db:
        o=db.query(M.Transaction).filter(M.Transaction.subtype=="kiosk_daypass").first()
        ck("walkin -> pending order subtype kiosk_daypass", o is not None and o.status=="pending")
        ck("walkin order has 1 line = Day Pass @150", len(o.items)==1 and float(o.items[0].unit_price)==150)
        ck("waiver stored for walkin", db.query(M.Waiver).filter(M.Waiver.first_name=="Dom").first() is not None)
        WALKIN_OID=o.id

    # ---- one-time token replay ----
    rr=c.post("/api/kiosk/submit", json={"flow":"walkin","key":KEY,"token":t1,
        "first_name":"Dom","last_name":"Roque","email":"d@x.com","phone":"0917111",
        "referral":"Facebook","signature":sig(),"plan_id":DP,"method":"cash"})
    ck("replay token -> 410 expired", rr.status_code==410 and rr.json().get("expired"))

    # ---- wrong-kind plan rejected (membership id in walk-in flow) ----
    tw,_=open_flow(c,"walkin",KEY)
    wp=c.post("/api/kiosk/submit", json={"flow":"walkin","key":KEY,"token":tw,
        "first_name":"A","last_name":"B","email":"a@b.c","phone":"1","referral":"X",
        "signature":sig(),"plan_id":MONTHLY,"method":"cash"})
    ck("wrong-kind plan rejected", not wp.json().get("ok"))
    ck("failed submit did NOT consume token", Session(engine).get(M.WaiverToken, tw).used is False)

    # ---- bank requires proof ----
    tb,_=open_flow(c,"signup",KEY)
    nb=c.post("/api/kiosk/submit", json={"flow":"signup","key":KEY,"token":tb,
        "first_name":"Mel","last_name":"Te","email":"m@x.com","phone":"0922",
        "referral":"TikTok","signature":sig(),"plan_id":MONTHLY,"method":"bank"})
    ck("bank without proof rejected", not nb.json().get("ok"))

    # ---- submit sign-up (bank w/ proof) ----
    ts,_=open_flow(c,"signup",KEY)
    s=c.post("/api/kiosk/submit", json={"flow":"signup","key":KEY,"token":ts,
        "first_name":"Mel","last_name":"Te","email":"m@x.com","phone":"09221234",
        "referral":"TikTok","signature":sig(),"plan_id":MONTHLY,"method":"bank","proof":proof()})
    ck("signup bank submit ok", s.json().get("ok") and s.json().get("plan")=="Monthly")
    with Session(engine) as db:
        so=db.query(M.Transaction).filter(M.Transaction.subtype=="kiosk_membership").first()
        ck("signup -> pending order kiosk_membership w/ proof", so is not None and so.proof is not None)
        SIGNUP_OID=so.id
        ck("no member entity yet (created on approval)", db.query(M.Staff).filter(M.Staff.person_type=="member").first() is None)

    # ---- staff approval ----
    c.post("/login", data={"username":"admin","pin":"123456"})
    # walk-in confirm -> sale, NO member
    c.post("/api/m/orders/%d/confirm"%WALKIN_OID)
    with Session(engine) as db:
        wo=db.get(M.Transaction, WALKIN_OID)
        ck("walkin confirmed", wo.status=="confirmed" and wo.converted_id)
        ck("walkin created NO member", db.query(M.Staff).filter(M.Staff.person_type=="member").count()==0)
    # sign-up confirm -> sale + member created + linked
    c.post("/api/m/orders/%d/confirm"%SIGNUP_OID)
    with Session(engine) as db:
        so=db.get(M.Transaction, SIGNUP_OID)
        mem=db.query(M.Staff).filter(M.Staff.person_type=="member").first()
        ck("signup confirmed", so.status=="confirmed" and so.converted_id)
        ck("signup created member entity", mem is not None and mem.name=="Mel Te" and mem.has_access is False)
        ck("member linked to order", so.customer_id==mem.id)
        sale=db.get(M.Transaction, so.converted_id)
        ck("sale linked to member", sale.customer_id==mem.id)

    # ---- returning member: same phone reused, not duplicated ----
    tr,_=open_flow(c,"signup",KEY)
    c.post("/api/kiosk/submit", json={"flow":"signup","key":KEY,"token":tr,
        "first_name":"Mel","last_name":"Te","email":"m@x.com","phone":"09221234",
        "referral":"TikTok","signature":sig(),"plan_id":MONTHLY,"method":"cash"})
    with Session(engine) as db:
        oid2=db.query(M.Transaction).filter(M.Transaction.subtype=="kiosk_membership",
                                            M.Transaction.status=="pending").first().id
    c.post("/api/m/orders/%d/confirm"%oid2)
    with Session(engine) as db:
        ck("returning member reused (still 1 member)", db.query(M.Staff).filter(M.Staff.person_type=="member").count()==1)

    # ---- hub forwards key to flows ----
    hub=c.get("/welcome?k="+KEY).text
    ck("hub links carry key to walkin", "/kiosk/walkin?k=%s"%KEY in hub)
    ck("hub links carry key to signup", "/kiosk/signup?k=%s"%KEY in hub)
    ck("hub buy drinks -> /order", 'href="/order"' in hub)
    ck("hub has no Pay my balance", "Pay my balance" not in hub)

    # ---- admin plans page ----
    a=c.get("/admin/kiosk")
    ck("admin kiosk 200 + hub QR", a.status_code==200 and "data:image/png;base64" in a.text)
    # a 'pay.' host must be rewritten to the portal host in the printed hub link
    ap=c.get("/admin/kiosk", headers={"host":"pay.awakengym.com"})
    ck("pay host rewritten to portal in hub link", "portal.awakengym.com/welcome?k=" in ap.text)
    # edit day pass price + add a membership
    c.post("/admin/kiosk", data={
        "name_%d"%DP:"Day Pass","sub_%d"%DP:"All-day","price_%d"%DP:"200","active_%d"%DP:"on",
        "new_name_1":"Weekly","new_sub_1":"7 days","new_price_1":"400"})
    with Session(engine) as db:
        ck("day pass price updated to 200", float(db.get(M.KioskPlan,DP).price)==200)
        ck("new Weekly membership added", db.query(M.KioskPlan).filter(M.KioskPlan.name=="Weekly").first() is not None)

fails=[n for n,ok in res if not ok]
print("\n%d/%d passed"%(sum(1 for _,ok in res if ok),len(res)),"| FAIL:",fails or "none")
