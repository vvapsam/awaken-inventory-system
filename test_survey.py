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


def entry(mobile, *, lives="Within Metro Manila", age="25-34", gender="Female",
          name="Maria",
          email="maria@email.com", ref=None, **extra):
    a = {"q1_lives": lives, "q1_city": "Pasig", "q2_age": age,
         "q3_gender": gender,
         "q5_occupation": "Employed", "q7_travel": "21-30 min",
         "q10_activities": ["CrossFit", "Hybrid - HYROX/DEKA"],
         "q14_budget": "5000-7499", "q17_goals": ["Strength"],
         "q31_anything_else": "More evening classes"}
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
    ck("the word exercise appears nowhere", "exercis" not in r.text.lower())
    for want in ("Calisthenics / bodyweight training",
                 "Functional training / HIIT classes",
                 "Obstacle course racing / OCR (such as Spartan)",
                 "Aerial / pole fitness (including aerial hoop)",
                 "Racket sports (such as badminton, tennis or pickleball)",
                 "Team sports (such as basketball, volleyball or football)",
                 "Outdoor activities (such as hiking or climbing)"):
        ck("question 9 offers %s" % want.split(" (")[0].lower(), want in r.text)
    ck("gender is asked", "3. What is your gender?" in r.text)
    for g in ("Female", "Male", "Non-binary", "Prefer not to say"):
        ck("gender offers %s" % g.lower(), ('value="%s"' % g) in r.text)
    ck("and it is question 3, beside age",
       r.text.index("What is your age group") < r.text.index("What is your gender")
       < r.text.index("How can we reach you"))
    ck("the services question is reworked",
       "Hybrid classes (HYROX or DEKA preparation)" in r.text
       and "Small group coaching, two to four people" in r.text
       and "Nutrition guidance" in r.text)
    ck("and no longer repeats the goals question",
       "Structured programs for a specific goal" not in r.text)
    ck("nineteen ways to answer question 10, Other and None included",
       r.text.count('name="q10" value=') == 19)
    ck("only one of them stands alone",
       r.text.count('name="q10" value="None" data-alone') == 1)
    ck("question 9 asks about fitness activities",
       "What fitness activities do you do now?" in r.text)
    ck("question 13 asks about fitness activities",
       "in total, on fitness activities?" in r.text)
    ck("and question 10 asks about training",
       "how many days a week did you train?" in r.text)

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
        ck("answers stored as JSON", json.loads(row.answers)["q14_budget"] == "5000-7499")
        ck("gender stored", json.loads(row.answers)["q3_gender"] == "Female")
        ck("a list answer survives", json.loads(row.answers)["q10_activities"] == ["CrossFit", "Hybrid - HYROX/DEKA"])
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
        ck("and gender is not one of them", "q3_gender" not in kept)
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

    # ------------------------------------------------- the link and setup
    r = c.get("/admin/survey")
    ck("the link is on the page", "/survey" in r.text and "Copy the link" in r.text)
    ck("and a QR to download", "/admin/survey/qr.png" in r.text)
    ck("one link per channel", all(("ch=" + code) in r.text
                                   for code, _ in __import__("app.survey_routes",
                                       fromlist=["CHANNELS"]).CHANNELS if code))
    r = c.get("/admin/survey/qr.png")
    ck("the QR is a real PNG", r.status_code == 200
       and r.content[:8] == b"\x89PNG\r\n\x1a\n" and len(r.content) > 400)
    r = c.get("/admin/survey/qr.png?ch=ig")
    ck("and one per channel too", r.status_code == 200 and len(r.content) > 400)

    r = c.get("/survey?ch=ig")
    ck("the channel reaches the form", '"channel": "ig"' in r.text)
    c.post("/survey", json={**entry("09195550001"), "channel": "ig"})
    with Session(engine) as db:
        g = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639195550001").first()
        ck("and is stored on the entry", g.channel == "ig")
    r = c.get("/admin/survey")
    ck("the channel tally is shown", "Instagram" in r.text)
    ck("and the channel is a column", ">ig<" in r.text)

    # dates print on the form, from the settings
    r = c.get("/survey")
    ck("the closing date prints", "Sun 25 October" in r.text)
    ck("the announcement date prints", "Fri 6 November" in r.text)
    c.post("/admin/survey/setup", data={"open": "1", "closes_on": "2026-11-01",
                                        "announces_on": "2026-11-15", "cap": "3"},
           follow_redirects=False)
    r = c.get("/survey")
    ck("a moved date moves on the form", "Sun 1 November" in r.text)
    ck("and in the fine print", "Sunday 1 November 2026" in r.text)
    ck("the cap follows the setting", '"cap": 3' in r.text)
    with Session(engine) as db:
        from app.survey_routes import entries_for
        me = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639171111111").first()
        ck("and the count obeys the new cap", entries_for(db, me) == 4)

    # closing it
    c.post("/admin/survey/setup", data={"closes_on": "2026-11-01",
                                        "announces_on": "2026-11-15", "cap": "3"},
           follow_redirects=False)
    r = c.get("/survey")
    ck("a closed survey shows a closed page", "That&rsquo;s a wrap" in r.text
       or "That\u2019s a wrap" in r.text or "wrap" in r.text)
    ck("and not the form", "START THE SURVEY" not in r.text.upper())
    r = c.post("/survey", json=entry("09198880000"))
    ck("and refuses late entries", r.status_code == 403)
    c.post("/admin/survey/setup", data={"open": "1", "closes_on": "2026-11-01",
                                        "announces_on": "2026-11-15", "cap": "5"},
           follow_redirects=False)
    r = c.post("/survey", json=entry("09198880000"))
    ck("reopening lets them back in", r.status_code == 200)

    # --------------------------------------------------------- the results
    # Some spread, or every question is one answer at 100% and the page
    # correctly folds the lot away.
    c.post("/survey", json=entry("09196660001", **{"q2_age": "35-44"}))
    c.post("/survey", json={**entry("09196660002"),
                            "answers": {**entry("09196660002")["answers"],
                                        "q2_age": "45-54", "q1_city": "Makati",
                                        "q5_occupation": "Student",
                                        "q14_budget": "3000-4999",
                                        "q17_goals": ["Lose fat", "Mobility"]}})
    r = c.get("/admin/survey/results")
    ck("the results page opens", r.status_code == 200)
    ck("it leads with how many answered", "Completed" in r.text and "In the draw" in r.text)
    ck("and how long it took them", "Typical time" in r.text)
    ck("questions are grouped as they were asked",
       all(h in r.text for h in ("Who answered", "What they want", "In their words")))
    ck("an answer is counted", "Employed" in r.text and "5000-7499" in r.text)
    ck("multi-select says the shares pass 100", "add past 100" in r.text)
    ck("the open box is read, not counted", "More evening classes" in r.text)
    ck("and repeats are folded with a count", "&times;" in r.text or "\u00d7" in r.text)
    ck("bars are one hue, not a ramp", r.text.count("background:#008080") <= 2)
    ck("a question everybody answered the same is not drawn as one full bar",
       "Everybody said the same" in r.text and "Within Metro Manila" in r.text)
    ck("the denominator is stated", "answered" in r.text and "not everybody" in r.text)
    ck("and the raw rows are one click away", '/admin/survey.csv' in r.text
       and 'href="/admin/survey"' in r.text)
    ck("a channel can be read on its own", "results?ch=ig" in r.text)
    r2 = c.get("/admin/survey/results?ch=ig")
    ck("and that view is narrower", r2.status_code == 200
       and "Completed" in r2.text)
    r3 = c.get("/admin/survey")
    ck("the table points at the results", "See the results" in r3.text)

    with Session(engine) as db:
        from app.survey_routes import count_one, free_text, median_seconds
        a = [{"x": "A"}, {"x": "B"}, {"x": "A"}, {"x": ""}, {}]
        answered, counts = count_one(a, "x")
        ck("counting ignores the blanks", answered == 3)
        ck("and orders by commonest", counts == [("A", 2), ("B", 1)])
        m = [{"y": ["A", "B"]}, {"y": ["B"]}, {"y": []}]
        answered, counts = count_one(m, "y")
        ck("a multi-select counts each pick", counts == [("B", 2), ("A", 1)])
        ck("against the people who answered it", answered == 2)
        wrote, said = free_text([{"t": "ab"}, {"t": "abcd"}, {"t": " "}, {"t": "ab"}], "t")
        ck("free text counts everybody who wrote", wrote == 3)
        ck("the same sentence twice is folded together", said == [("ab", 2), ("abcd", 1)])

bad = [n for n, ok in res if not ok]
print("\n%d checks, %d failed" % (len(res), len(bad)))
for n in bad:
    print("  FAILED:", n)
raise SystemExit(1 if bad else 0)
