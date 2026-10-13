"""The market research survey: one entry per person, and the table it fills.

The rules worth testing hardest are the ones that decide a prize. A number
that can enter twice, or a page that can declare its own outcome, is a draw
somebody can win by trying again.
"""
import os
os.environ["DATABASE_URL"] = ("postgresql+psycopg2://postgres@/surv"
                              "?host=/home/claude/pgrun&port=5433")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_INITIAL_PIN", "123456")
import json
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import Base, engine
from app import models as M
from app.survey_routes import QUESTIONS
from fastapi.testclient import TestClient
from app.main import app

res = []
def ck(n, c):
    res.append((n, bool(c)))
    print("PASS" if c else "FAIL", n)

with engine.begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

DEV = {"user_agent": "Mozilla/5.0 (iPhone)", "screen": "390x844",
       "viewport": "390x700", "timezone": "Asia/Manila", "language": "en-PH",
       "platform": "iPhone"}


def entry(mobile, *, lives="Within Metro Manila", age="25-34", name="Maria",
          email="maria@email.com", ref=None, **extra):
    a = {"q1_lives": lives, "q1_city": "Pasig", "q2_age": age,
         "q4_occupation": "Employed", "q6_travel": "21-30 min",
         "q9_activities": ["CrossFit", "Hybrid - HYROX/DEKA"],
         "q13_budget": "5000-7499", "q16_goals": ["Strength"],
         "q30_anything_else": "More evening classes"}
    a.update(extra)
    return {"seconds_taken": 412,
            "consent": {"privacy": True, "promo_rules": True},
            "person": {"name": name, "mobile_raw": mobile, "email": email},
            "referral": {"referred_by": ref},
            "answers": a, "device": DEV}


with TestClient(app) as c:
    # ---------------------------------------------------------- normalising
    n = M.norm_ph_mobile
    ck("09xx normalises", n("09171234567") == "+639171234567")
    ck("+63 normalises", n("+639171234567") == "+639171234567")
    ck("63 normalises", n("639171234567") == "+639171234567")
    ck("bare 9xx normalises", n("9171234567") == "+639171234567")
    ck("spaces and dashes ignored", n("0917-123 4567") == "+639171234567")
    ck("wrong prefix refused", n("08171234567") is None)
    ck("too short refused", n("091712345") is None)
    ck("junk refused", n("hello") is None and n("") is None)

    # ------------------------------------------------------------ the form
    r = c.get("/survey")
    ck("the form is public", r.status_code == 200)
    ck("and carries its settings", '"share_base"' in r.text and '"cap": 5' in r.text)
    ck("no code is minted in the browser", "function newCode" not in r.text)
    ck("and nothing is said about keeping the number", "is noted so you" not in r.text)

    # ------------------------------------------------------------ one entry
    r = c.post("/survey", json=entry("0917 111 1111"))
    ck("an entry is accepted", r.status_code == 200)
    first = r.json()
    ck("it completes", first["outcome"] == "complete")
    ck("a code comes back", len(first["code"]) == 8)
    ck("no lookalike characters in it", not set(first["code"]) & set("O0I1"))
    ck("one entry to start", first["entries"] == 1)

    with Session(engine) as db:
        row = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639171111111").first()
        ck("stored against the normalised number", row is not None)
        ck("what they typed is kept too", row.mobile_raw == "0917 111 1111")
        ck("answers stored as JSON", json.loads(row.answers)["q13_budget"] == "5000-7499")
        ck("a list answer survives", json.loads(row.answers)["q9_activities"] == ["CrossFit", "Hybrid - HYROX/DEKA"])
        ck("IP recorded", bool(row.ip))
        ck("device recorded", row.user_agent.startswith("Mozilla") and row.screen == "390x844")
        ck("timezone recorded", row.timezone_name == "Asia/Manila")
        ck("how long it took", row.seconds_taken == 412)
        ck("consent recorded", row.consent_privacy and row.consent_promo)

    # -------------------------------------------------------- one per person
    r = c.post("/survey", json=entry("+639171111111", name="Someone else"))
    ck("the same number cannot enter twice", r.status_code == 409)
    r = c.post("/survey", json=entry("0917 111 1111"))
    ck("nor in a different shape", r.status_code == 409)
    with Session(engine) as db:
        ck("and no second row was written",
           db.query(M.SurveyResponse).filter(
               M.SurveyResponse.mobile == "+639171111111").count() == 1)

    r = c.post("/survey/check", json={"mobile": "09171111111"})
    ck("the check says a used number is used", r.json()["ok"] is False and r.json()["why"] == "used")
    r = c.post("/survey/check", json={"mobile": "09172222222"})
    ck("and a fresh one is free", r.json()["ok"] is True)
    r = c.post("/survey/check", json={"mobile": "nonsense"})
    ck("and junk is neither", r.json()["ok"] is False and r.json()["why"] == "format")

    # ----------------------------------------------------------- screenouts
    r = c.post("/survey", json=entry("0917 222 2222", lives="Outside Metro Manila"))
    ck("outside Metro Manila is turned away", r.json()["outcome"] == "screened_out")
    ck("and told why", r.json()["reason"] == "area")
    ck("with no code issued", r.json()["code"] == "")
    r = c.post("/survey", json=entry("0917 333 3333", age="Under 18"))
    ck("under 18 is turned away", r.json()["reason"] == "age")

    r = c.post("/survey", json=entry("0917 222 2222"))
    ck("a number spent on a screenout cannot come back", r.status_code == 409)

    with Session(engine) as db:
        out = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639172222222").first()
        kept = json.loads(out.answers)
        ck("only the screening answers are kept", sorted(kept) == ["q1_lives", "q2_age"])
        ck("no nickname kept for somebody turned away", out.nickname is None)
        ck("no email either", out.email is None)
        ck("but the number is", out.mobile == "+639172222222")
        ck("and the IP, for spotting repeats", bool(out.ip))

    # ------------------------------------- the browser does not get a vote
    sneaky = entry("0917 444 4444", lives="Outside Metro Manila")
    sneaky["outcome"] = "complete"          # what a tampered page would send
    r = c.post("/survey", json=sneaky)
    ck("a page claiming to qualify is still screened on the answers",
       r.json()["outcome"] == "screened_out")

    r = c.post("/survey", json={**entry("0917 555 5555"),
                                "consent": {"privacy": True, "promo_rules": False}})
    ck("both consents are required", r.status_code == 400)
    r = c.post("/survey", json=entry("0812 345"))
    ck("an unusable number is refused", r.status_code == 400)

    # ------------------------------------------------------------ referrals
    code = first["code"]
    for i in range(3):
        c.post("/survey", json=entry("09176%06d" % i, ref=code))
    r = c.post("/survey/check", json={"mobile": "09171111111"})
    with Session(engine) as db:
        from app.survey_routes import entries_for
        me = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639171111111").first()
        ck("three friends, four entries", entries_for(db, me) == 4)

    for i in range(4):
        c.post("/survey", json=entry("09177%06d" % i, ref=code))
    with Session(engine) as db:
        from app.survey_routes import entries_for
        me = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639171111111").first()
        ck("the cap holds at five extra", entries_for(db, me) == 6)

    c.post("/survey", json=entry("09178000001", ref="NOTACODE"))
    with Session(engine) as db:
        g = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639178000001").first()
        ck("an invented code is dropped, not stored", g.referred_by is None)

    c.post("/survey", json=entry("09178000002", ref="+639172222222"))
    with Session(engine) as db:
        g = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639178000002").first()
        ck("and a screened-out person earns nobody anything", g.referred_by is None)

    # --------------------------------------------------------------- admin
    r = c.get("/admin/survey")
    ck("the table needs a login", r.status_code in (200, 303, 307)
       and "/login" in str(r.url) + (r.headers.get("location") or ""))

    c.post("/login", data={"username": "admin", "pin": "123456"},
           follow_redirects=False)
    r = c.get("/admin/survey")
    ck("an admin sees the table", r.status_code == 200)
    ck("every question is a column", all(label in r.text for _, label in QUESTIONS))
    ck("the answers are in it", "More evening classes" in r.text)
    ck("and the mobile number", "+639171111111" in r.text)
    ck("turned-away rows are not mixed in", "+639172222222" not in r.text)
    r = c.get("/admin/survey?show=out")
    ck("they have their own view", "+639172222222" in r.text)
    ck("with the reason", "Outside Metro Manila" in r.text)

    r = c.get("/admin/survey.csv")
    ck("the CSV downloads", r.status_code == 200 and "text/csv" in r.headers["content-type"])
    head = r.text.splitlines()[0]
    ck("with a column per question", all(label in head for _, label in QUESTIONS))
    ck("and the tech details", "IP" in head and "Device" in head)
    ck("list answers are flattened", "CrossFit | Hybrid - HYROX/DEKA" in r.text)

    with Session(engine) as db:
        rid = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639171111111").first().id
    r = c.get("/admin/survey/%d" % rid)
    ck("one response reads on its own", r.status_code == 200 and "Maria" in r.text)
    ck("showing the entries it has earned", ">6<" in r.text or "6" in r.text)
    ck("and who used their code", "Friends who used their code" in r.text)

bad = [n for n, ok in res if not ok]
print("\n%d checks, %d failed" % (len(res), len(bad)))
for n in bad:
    print("  FAILED:", n)
raise SystemExit(1 if bad else 0)
