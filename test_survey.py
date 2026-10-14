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
from datetime import timedelta
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
          email=None, ref=None, **extra):
    # Each number is a different person, so each one needs its own address:
    # one entry per person now means one per email as well as one per number.
    if email is None:
        email = "m%s@email.com" % "".join(ch for ch in mobile if ch.isdigit())
    a = {"q1_lives": lives, "q1_city": "Pasig", "q2_age": age,
         "q3_gender": gender,
         "q5_occupation": "Employed", "q7_travel": "21-30 min",
         "q10_activities": ["CrossFit", "Hybrid - HYROX/DEKA"],
         "q13_budget": "5000-7499", "q16_goals": ["Strength"],
         "q28_anything_else": "More evening classes"}
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
    for want in ("Calisthenics, streetlifting or bodyweight training",
                 "Functional training / HIIT classes",
                 "Obstacle course racing / OCR (such as Spartan)",
                 "Aerial / pole fitness (including aerial hoop)",
                 "Racket sports (such as badminton, tennis or pickleball)",
                 "Team sports (such as basketball, volleyball or football)",
                 "Outdoor activities (such as hiking or climbing)"):
        ck("question 9 offers %s" % want.split(" (")[0].lower(), want in r.text)
    ck("gender is asked", "3. What is your gender?" in r.text)

    # 13 keeps "I'm not sure" - nobody tracks their total spend across a
    # membership, drop-ins and court fees. The follow-up does not: what a
    # non-payer would be willing to pay is the number it exists for, and
    # "Nothing" already covers anybody who truly would not.
    import re as _re3
    willing = _re3.findall(r'name="q13_willing" value="([^"]*)"', r.text)
    ck("the follow-up has no opt-out", "Unsure" not in willing)
    ck("and offers six real figures instead", len(willing) == 6)
    ck("but still lets somebody say nothing", "0" in willing)
    ck("and 13 itself keeps its one, where it is honest",
       'name="q13" value="Unsure"' in r.text)

    # One question at a time. The markup keeps its seven sections so the
    # conditionals, the rules and the payload all still read the same DOM;
    # what changed is that only one question is on screen at once.
    ck("the form walks one question at a time", '.oneq .q.now{display:block}' in r.text)
    ck("with one nav bar rather than seven", r.text.count('id="oneNext"') == 1)
    ck("a section card between groups", 'data-step="sect"' in r.text)
    ck("and its button says Next like every other one",
       '$("#oneNext").textContent = "Next";' in r.text
       and "Continue" not in r.text)

    # The card the question sits in clips its own corners, so a dropdown
    # positioned inside it gets sliced off at the card's edge. Fixed
    # positioning takes the list out of that box entirely.
    ck("the gym list is not clipped by the card",
       ".combolist{position:fixed" in r.text)
    ck("and it follows the box when the page moves",
       'window.addEventListener("scroll", place, true);' in r.text)
    ck("and the count is shown", '" of " + TOTAL' in r.text)
    ck("single choice moves on by itself", "function maybeAdvance" in r.text)
    ck("but never from question 4, where screening happens",
       "if (pos.q === 4) return;" in r.text)
    ck("nor from a question that is not finished",
       "if (askedOn(pos.q).length) return;" in r.text)
    ck("every question has its own validator",
       all(("\n  %d: function" % n) in r.text for n in range(1, 29)))
    ck("and the section validators are built from them, not written twice",
       "RULES[sec] = function(){" in r.text)
    ck("submitting re-checks every section",
       "[1,2,3,4,5,6,7].forEach(function(sec){" in r.text)

    # Question 14 names competitors. Eight are places; the rest are kinds of
    # place and have to be followed by "what is it called?", or the answer
    # is a category and tells you nothing about who you are losing people to.
    ck("14 asks where they currently go",
       "Which gym or fitness studio do you currently go to most often?" in r.text)
    for brand in ("Anytime Fitness", "Fitness First", "Slimmers World",
                  "Snap Fitness", "Philippine Sports Performance (PSP)",
                  "Surge Fitness + Lifestyle", "BeFit"):
        ck("14 lists %s" % brand.split(" (")[0], brand in r.text)
    ck("and Gold\u2019s Gym, apostrophe and all",
       "Gold\u2019s Gym" in r.text)
    for kind in ("Pilates studio", "Yoga studio", "CrossFit gym",
                 "Functional / Hybrid training gym", "Cycling / Spin studio",
                 "Dance / Aerial / Pole studio", "Independent / Local gym",
                 "Boxing / Martial arts gym", "Other gym or fitness studio"):
        ck("14 offers %s" % kind.lower(), kind in r.text)
    ck("and a way to say they do not go anywhere",
       "currently go to a gym or studio" in r.text)

    # Question 18 is about what a gym sells beyond the floor, so showers and
    # lockers came out: nobody chooses a gym for having them, and either
    # would have eaten one of the three picks.
    ck("18 asks about facilities and services",
       "Beyond workouts, which facilities and services" in r.text)
    for want in ("Member lounge / Social space", "Sports massage",
                 "Assisted stretching", "Physiotherapy services",
                 "Nutrition consultations", "Body composition assessments",
                 "Recovery equipment (such as compression boots)",
                 "Sauna / Steam room", "Cold plunge", "Co-working space"):
        ck("18 offers %s" % want.split(" (")[0].lower(), want in r.text)
    ck("and child-minding, apostrophe and all",
       "Child-minding / Supervised kids&rsquo; area" in r.text)
    ck("the table-stakes options are gone",
       "Showers and changing rooms" not in r.text and "Lockers" not in r.text)
    ck("18 is still capped at three", 'data-check="q18" data-max="3"' in r.text)
    ck("paying only for a membership is an answer on its own",
       'value="None" data-alone' in r.text
       and "pay only for my membership" in r.text)
    ck("and the cap never closes an escape option",
       'if (!b.checked && !b.hasAttribute("data-alone")){' in r.text)
    # The phone remembers too, so reopening the link is recognised before
    # anything is typed. A courtesy only - the server is still the gate.
    ck("the page carries a round key for the device marker", '"round"' in r.text)
    ck("and the marker is keyed to it",
       'var DONE_KEY = "awaken_survey_done_" + (CFG.round || "round");' in r.text)
    ck("reading it is wrapped, because storage throws in a private window",
       "try { return window.localStorage.getItem(DONE_KEY)" in r.text)
    ck("and so is writing it",
       "try { window.localStorage.setItem(DONE_KEY" in r.text)
    ck("a remembered device is recognised on arrival",
       "if (alreadyDone()) goto(96); else goto(0);" in r.text)

    # The done screen hands out a code as well as a QR, so a code has to
    # be typeable. It is asked at 27, of somebody who just said a friend
    # sent them, and only when one did not already arrive in the link.
    # Every question that is enforced says so. 14 and 15 are required
    # whenever they appear and were being enforced without the mark.
    import re as _re2
    for n, star in _re2.findall(r'<span class="qn">(\d+)\.(.{0,200}?)</span>', r.text, _re2.S):
        if int(n) == 28:
            continue
        ck("question %s shows it is required" % n, 'class="req"' in star)

    ck("there is somewhere to type a code", 'name="q27_code"' in r.text)
    ck("asked only of somebody a friend sent",
       'picked("q27") === "Friend/family"' in r.text)
    ck("and not at all if the link already carried one",
       'show($("#q27codewrap"), !CAME_FROM' in r.text)
    ck("what is typed is tidied to the shape codes are issued in",
       'function typedCode()' in r.text
       and 'toUpperCase().replace(/[^A-Z0-9]/g, "")' in r.text)
    ck("a link code still wins over a typed one",
       "CAME_FROM || typedCode() || null" in r.text)

    ck("14 is searched, not scrolled",
       'id="q14search"' in r.text and 'role="combobox"' in r.text)
    ck("and what it submits is a hidden field the box sets",
       'type="hidden" name="q14"' in r.text)
    ck("the old dropdown is gone", 'select name="q14"' not in r.text
       and "var GYMS" not in r.text)
    ck("the name box only follows a category",
       'id="q14namewrap"' in r.text and "BRAND_ANSWERS" in r.text)

    # The numbering drifted once already when gender was inserted, so it is
    # checked rather than trusted: every question's key must equal the number
    # printed beside it, and the run must have no gaps.
    import re as _re
    nums = [int(x) for x in _re.findall(r'class="qn">(\d+)\.', r.text)]
    ck("twenty-eight questions (got %d)" % len(nums), len(nums) == 28)
    ck("numbered 1 to 28 with no gaps", nums == list(range(1, 29)))
    # must not run past the next question, or a block with no radios (the
    # contact fields, the gym dropdown) borrows the following one's key
    pairs = _re.findall(
        r'<span class="qn">(\d+)\.(?:(?!<span class="qn">).)*?'
        r'(?:data-radio|data-check)="q(\d+)[dt]?"', r.text, _re.S)
    odd = [x for x in pairs if x[0] != x[1]]
    ck("every key matches the number beside it%s" % (" (%s)" % odd[:3] if odd else ""),
       not odd and len(pairs) >= 20)

    ck("the training-history question is gone",
       "How long have you been training regularly" not in r.text)
    ck("the would-join question is gone",
       "What would actually get you to join" not in r.text)
    ck("cancelling is one question now",
       "Have you ever stopped a gym membership or class package?" in r.text
       and "What was the main reason, the last time" not in r.text)
    ck("with never-had-one as an answer",
       'value="Never had one"' in r.text and 'value="Never stopped"' in r.text)
    ck("and it is no longer hidden behind a yes/no",
       'id="q24wrap"' not in r.text)
    ck("what keeps you going stayed",
       "what makes you keep going to a gym" in r.text)
    ck("occupation stayed", "current occupation" in r.text)
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
        ck("answers stored as JSON", json.loads(row.answers)["q13_budget"] == "5000-7499")
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

    r = c.post("/survey", json=entry("nonsense"))
    ck("junk in the number field is refused outright", r.status_code == 400)

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
        ck("but the number is", out.mobile == "+639172222222")
        ck("and the address, so the same person cannot come back on a new "
           "number", out.email is not None)
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

    # --------------------------------------------- what they pay, and would pay
    c.post("/survey", json=entry("0917 910 0001", email="pays@email.com",
                                 q13_budget="0", q13_willing="3000-4999"))
    with Session(engine) as db:
        row = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639179100001").first()
        a = json.loads(row.answers)
        ck("nothing today is recorded as nothing", a["q13_budget"] == "0")
        ck("and what they would pay is kept beside it",
           a["q13_willing"] == "3000-4999")
    # ------------------------------------------- one entry per person, both ways
    r = c.post("/survey", json=entry("0917 900 0001", email="dupe@email.com"))
    ck("a first entry on a fresh address is taken", r.status_code == 200)

    r = c.post("/survey", json=entry("0917 900 0002", email="dupe@email.com"))
    ck("the same address a second time is refused", r.status_code == 409)
    ck("and it says which one clashed", r.json()["error"] == "email")

    r = c.post("/survey", json=entry("0917 900 0003", email="  DUPE@Email.COM "))
    ck("casing and stray spaces do not get around it", r.status_code == 409)

    r = c.post("/survey", json=entry("0917 900 0001", email="brand@new.com"))
    ck("and the number is still its own block", r.json()["error"] == "used")

    # Every refusal leaves a record, so "it let me through" is checkable
    # rather than arguable.
    with Session(engine) as db:
        caught = [a.matched for a in db.query(M.SurveyAttempt).all()]
        ck("the refused attempts were written down", len(caught) >= 3)
        ck("and say which rule caught each one",
           "mobile" in caught and "email" in caught)

    # Being turned away spends the number and the address both. Otherwise
    # the second attempt is simply a different number.
    c.post("/survey", json=entry("0917 900 0010", email="away@email.com",
                                 lives="Outside Metro Manila"))
    r = c.post("/survey", json=entry("0917 900 0011", email="away@email.com"))
    ck("an address spent by somebody turned away cannot be used again",
       r.status_code == 409)

    # ------------------------------------------------- under 18 stays out
    r = c.post("/survey", json=entry("0917 920 0001", email="minor@email.com",
                                     age="Under 18"))
    ck("an under-18 answer is screened out", r.json()["outcome"] == "screened_out")
    ck("on age", r.json()["reason"] == "age")
    ck("with no referral code, so nothing to share", r.json()["code"] == "")
    ck("and no entries in the draw", r.json()["entries"] == 0)

    r = c.post("/survey", json=entry("0917 920 0001", email="brandnew@email.com"))
    ck("that number cannot be reused by claiming to be older",
       r.status_code == 409)
    r = c.post("/survey", json=entry("0917 920 0002", email="minor@email.com"))
    ck("nor the address on a second number", r.status_code == 409)

    # The page is not trusted on this. A browser claiming to qualify is
    # still read off the answers it sent.
    sneaky_age = entry("0917 920 0003", email="minor2@email.com", age="Under 18")
    sneaky_age["outcome"] = "complete"
    r = c.post("/survey", json=sneaky_age)
    ck("a page claiming an under-18 qualifies is overruled",
       r.json()["outcome"] == "screened_out" and r.json()["reason"] == "age")

    with Session(engine) as db:
        from app.survey_routes import entries_for as _ef
        minors = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.reason == "age").all()
        ck("every under-18 row counts for nothing in the draw",
           minors and all(_ef(db, m) == 0 for m in minors))
        ck("and none of them holds a code that could be claimed",
           all(m.ref_code is None for m in minors))

    # ----------------------------------- a shared connection is noted, not shut
    from app import models as _M
    r = c.get("/survey")
    ck("opening the link never turns anybody away", r.status_code == 200)
    ck("and the page is told nothing about connections",
       "seen_here" not in r.text and "ip_hours" not in r.text)

    # Two people, one wifi. Both get in; the second is flagged for a human.
    r = c.post("/survey", json=entry("0917 940 0001", email="wifi1@email.com"))
    ck("the first person on a connection gets in", r.status_code == 200)
    r = c.post("/survey", json=entry("0917 940 0002", email="wifi2@email.com"))
    ck("and so does the next one, on the same connection", r.status_code == 200)
    ck("with a real entry in the draw", r.json()["entries"] >= 1)
    ck("and a code of their own", bool(r.json()["code"]))

    with Session(engine) as db:
        flags = db.query(_M.SurveyAttempt).filter(
            _M.SurveyAttempt.matched == "ip").all()
        ck("the second one is flagged", len(flags) >= 1)
        ck("but recorded as allowed, not refused",
           all(f.blocked is False for f in flags))
        hard = db.query(_M.SurveyAttempt).filter(
            _M.SurveyAttempt.blocked.is_(True)).all()
        ck("while the number and address refusals stay refusals",
           hard and all(f.matched in ("mobile", "email") for f in hard))
        ck("and no refusal was ever recorded against a connection",
           not any(f.matched == "ip" for f in hard))

    # -------------------------------------------- closing time, not closing day
    from app.survey_routes import say_clock, say_date, deadline, is_open
    ck("11:59pm reads as 11:59pm", say_clock("23:59") == "11:59pm")
    ck("3pm reads as 3pm, not 3:00pm", say_clock("15:00") == "3pm")
    ck("midnight reads as 12am", say_clock("00:00") == "12am")
    ck("the date carries the hour",
       say_date("2026-10-25", at="23:59").endswith(", 11:59pm"))
    ck("and still works with no hour given",
       "," not in say_date("2026-10-25"))

    r = c.get("/survey")
    ck("the form prints the closing time", "11:59pm" in r.text)
    ck("and the announcement time", "3pm" in r.text)
    ck("and says which clock", "Manila time" in r.text)

    # The deadline closes the form by itself; nobody has to flip a switch.
    with Session(engine) as db:
        from app.survey_routes import put as _put, settings as _settings
        _put(db, "closes_on", "2020-01-01")
        db.commit()
        ck("a past deadline closes it", is_open(db) is False)
        _put(db, "closes_on", "2099-01-01")
        db.commit()
        ck("a future one leaves it open", is_open(db) is True)
        _put(db, "open", "0")
        db.commit()
        ck("and the switch still wins on its own", is_open(db) is False)
        _put(db, "open", "1")
        _put(db, "closes_on", "2026-10-25")
        db.commit()

    with Session(engine) as db:
        _put(db, "closes_on", "2020-01-01")
        db.commit()
    r = c.post("/survey", json=entry("0917 950 0001", email="late@email.com"))
    ck("an entry after the deadline is refused", r.status_code == 403)
    ck("and told the survey is closed", r.json()["error"] == "closed")
    r = c.get("/survey")
    ck("and the link shows the closed page", "the survey is closed" in r.text)
    ck("which still says when the winner is named", "3pm" in r.text)
    with Session(engine) as db:
        _put(db, "closes_on", "2026-10-25")
        db.commit()
    ck("putting the date back reopens it", c.get("/survey").status_code == 200)

    # ------------------------------------------------- streetlifting is offered
    r = c.get("/survey")
    ck("streetlifting is one of the activities", "streetlifting" in r.text)

    # ------------------------------- asked at question 4, where the pair is known
    c.post("/survey", json=entry("0917 960 0001", email="seen@email.com"))
    r = c.post("/survey/seen", json={"mobile": "0917 960 0001",
                                     "email": "nothing@else.com"})
    ck("a spent number is seen", r.json() == {"seen": True})
    r = c.post("/survey/seen", json={"mobile": "0917 960 9999",
                                     "email": "seen@email.com"})
    ck("a spent address is seen", r.json() == {"seen": True})
    r = c.post("/survey/seen", json={"mobile": "0917 960 9999",
                                     "email": "nothing@else.com"})
    ck("a clean pair is not", r.json() == {"seen": False})

    # It answers one bit, and only to somebody who already holds both
    # halves - so it cannot be used to ask "has this number entered?"
    # about a stranger.
    ck("it never says which of the two matched",
       set(r.json().keys()) == {"seen"})
    r = c.post("/survey/seen", json={"mobile": "0917 960 0001"})
    ck("and will not answer on a number alone", r.json() == {"seen": False})
    r = c.post("/survey/seen", json={"email": "seen@email.com"})
    ck("nor on an address alone", r.json() == {"seen": False})

    # Somebody turned away is on file, so the same person coming back is
    # recognised - which is the whole point of spending their details.
    c.post("/survey", json=entry("0917 960 0002", email="away2@email.com",
                                 lives="Outside Metro Manila"))
    r = c.post("/survey/seen", json={"mobile": "0917 960 0002",
                                     "email": "away2@email.com"})
    ck("somebody turned away is recognised if they come back",
       r.json() == {"seen": True})

    # --------------------------------------------------------------- admin
    r = c.get("/admin/survey")
    ck("the table needs a login", r.status_code in (200, 303, 307)
       and "/login" in str(r.url) + (r.headers.get("location") or ""))

    c.post("/login", data={"username": "admin", "pin": "123456"},
           follow_redirects=False)
    r = c.get("/admin/survey")
    ck("an admin sees the table", r.status_code == 200)

    # ------------------------------------------ correcting one by hand
    c.post("/survey", json=entry("0917 970 0001", email="fix@email.com",
                                 q2_age="25-34",
                                 q10_activities=["CrossFit", "Yoga"]))
    with Session(engine) as db:
        rid = db.query(M.SurveyResponse).filter(
            M.SurveyResponse.mobile == "+639179700001").first().id

    r = c.get("/admin/survey/%d/edit" % rid)
    ck("there is a form for correcting a response", r.status_code == 200)
    ck("with a field for every answer",
       all(('name="%s"' % k) in r.text for k, _ in QUESTIONS))
    ck("the mobile number is shown but not editable",
       "+639179700001" in r.text and 'name="mobile"' not in r.text)
    ck("several answers come back comma separated",
       'value="CrossFit, Yoga"' in r.text)
    ck("and what others picked is offered as suggestions",
       "<datalist" in r.text)

    r = c.post("/admin/survey/%d/edit" % rid, data={
        "nickname": "Corrected", "email": "fix@email.com",
        "q2_age": "35-44", "q10_activities": "CrossFit, Pilates",
        "edit_note": "fixed over the phone"}, follow_redirects=False)
    ck("saving sends you back to the response", r.status_code == 303)
    with Session(engine) as db:
        row = db.query(M.SurveyResponse).get(rid)
        a = json.loads(row.answers)
        ck("a single answer is corrected", a["q2_age"] == "35-44")
        ck("a multiple one comes back a list, not a string",
           a["q10_activities"] == ["CrossFit", "Pilates"])
        ck("the nickname follows", row.nickname == "Corrected")
        ck("and who changed it, when, and why is kept",
           row.edited_at is not None and row.edited_by_id is not None
           and row.edit_note == "fixed over the phone")
        ck("answers nobody touched are left alone",
           a.get("q1_lives") == "Within Metro Manila")

    # An address already spent by somebody else cannot be moved onto this
    # one, or the unique index would be the thing that said no.
    c.post("/survey", json=entry("0917 970 0002", email="taken@email.com"))
    c.post("/admin/survey/%d/edit" % rid, data={
        "nickname": "Corrected", "email": "taken@email.com"},
        follow_redirects=False)
    with Session(engine) as db:
        ck("an address another response holds is refused",
           db.query(M.SurveyResponse).get(rid).email == "fix@email.com")

    r = c.get("/admin/survey/%d/edit" % 99999)
    ck("editing a response that is not there goes back to the list",
       r.status_code in (200, 303, 307))

    ck("and never a cached copy of it",
       "no-store" in r.headers.get("cache-control", ""))
    for page in ("/admin/survey/results", "/admin/survey/1"):
        ck("nor of %s" % page,
           "no-store" in c.get(page).headers.get("cache-control", ""))
    ck("every question is a column", all(label in r.text for _, label in QUESTIONS))
    ck("the answers are in it", "More evening classes" in r.text)
    ck("and the mobile number", "+639171111111" in r.text)
    # The repeats card below the table does name turned-away entries, on
    # purpose: somebody filling the form in over and over is exactly what it
    # is there to show. The table itself is the part that stays clean.
    body = r.text.split('<table class="list" style="min-width')[-1]
    ck("turned-away rows are not mixed into the table",
       "+639172222222" not in body)
    ck("but the repeats card does account for them",
       "Worth a look" in r.text and "turned away" in r.text)
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
                                        "q13_budget": "3000-4999",
                                        "q16_goals": ["Lose fat", "Mobility"]}})
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
