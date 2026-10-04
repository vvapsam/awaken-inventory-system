"""Mixed gender, and the second name on a pair.

Three things have to be true together, which is why they are one file:
  - "Mixed" is a gender the form offers, the board columns by, and the
    results page filters on;
  - a category marked as a pair asks for a partner on the sign-up, and asks
    again at the door if it came through blank;
  - one entry with two names reads "Trina P./Vanessa S." everywhere the
    public can see it, and exactly as it always did when there is no partner.
"""
import os, re, time
os.environ["DATABASE_URL"] = ("postgresql+psycopg2://postgres@/pairs"
                              "?host=/home/claude/pgrun&port=5433")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_INITIAL_PIN", "123456")
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import Base, engine
from app import models as M
from app.event_routes import board_name, short_text
from fastapi.testclient import TestClient
from app.main import app

res = []
def ck(n, c):
    res.append((n, bool(c)))
    print("PASS" if c else "FAIL", n)

with engine.begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
Base.metadata.create_all(engine)

# ---------------------------------------------------------------- the shape --
ck("Mixed is a gender", ("x", "Mixed") in M.SEXES)

class Fake:
    def __init__(self, sex, tier=None):
        self.sex, self.tier, self.event = sex, tier, None
ck("no gender is unlisted", M.board_key(Fake(None)) == ""
   and M.board_key(Fake("z")) == "")
ck("no category is gender alone", M.board_key(Fake("x")) == ":x")

ck("short_text one word", short_text("Madonna") == "Madonna")
ck("short_text two", short_text("Vanessa Sampang") == "Vanessa S.")
ck("short_text three", short_text("Vanessa de la Cruz") == "Vanessa D.")
ck("short_text blank", short_text("") == "" and short_text(None) == "")

# ------------------------------------------------------------------ an event --
with Session(engine) as db:
    ev = M.Event(name="Leg 3", slug="leg3", mode=M.EVENT_OPEN, capacity=0)
    db.add(ev); db.flush()
    solo = M.EventRate(event_id=ev.id, label="Solo", amount=1500,
                       capacity=0, position=0, pairs=False)
    dbl = M.EventRate(event_id=ev.id, label="Doubles", amount=2500,
                      capacity=10, position=1, pairs=True)
    db.add_all([solo, dbl]); db.commit()
    EID, SOLO, DBL = ev.id, str(solo.id), str(dbl.id)

def fill(c, **extra):
    """One sign-up, through the real guard."""
    html = c.get("/r/leg3").text
    salt = re.search(r'name="salt" value="([^"]*)"', html).group(1)
    qa, qb = re.search(r'What is (\d+) plus (\d+)\?', html).groups()
    time.sleep(3.2)                       # the form's own "too fast" floor
    data = {"salt": salt, "nonce": "", "qanswer": str(int(qa) + int(qb)),
            "first_name": "Trina", "last_name": "Pangilinan",
            "country": "PH"}
    data.update(extra)
    return c.post("/r/leg3", data=data, follow_redirects=False)

with TestClient(app) as c:
    page = c.get("/r/leg3").text
    ck("form offers Mixed", 'value="x"' in page and "Mixed" in page)
    ck("form asks for a partner", 'id="pairq"' in page)
    ck("doubles is flagged a pair",
       re.search(r'value="%s"[^>]*\n?[^>]*data-pairs="1"' % DBL, page)
       is not None or ('data-pairs="1"' in page))
    ck("solo is not flagged a pair", 'data-pairs="0"' in page)

    # --- a mixed pair, with the partner given on the form
    r = fill(c, email="trina@x.com", sex="x", tier=DBL,
             partner_name="Vanessa Sampang")
    ck("pair registered", r.status_code == 303 and "err=" not in r.headers["location"])
    with Session(engine) as db:
        p = db.query(M.EventParticipant).filter_by(email="trina@x.com").one()
        ck("gender stored as mixed", p.sex == "x")
        ck("partner stored", p.partner_name == "Vanessa Sampang")
        ck("board reads both names", board_name(p) == "Trina P./Vanessa S.")
        ck("mixed pair lands in the mixed doubles column",
           M.board_key(p) == "%s:x" % DBL)
        cols = dict(M.board_columns(p.event))
        ck("column is gender then category",
           cols.get("%s:x" % DBL) == "Mixed \u2013 Doubles"
           and cols.get("%s:m" % DBL) == "Male \u2013 Doubles"
           and cols.get("%s:f" % DBL) == "Female \u2013 Doubles")
        ck("every category gets its three",
           cols.get("%s:m" % SOLO) == "Male \u2013 Solo")
        ck("unlisted column kept", cols.get("") == "Unlisted")
        ck("a category the event dropped falls back to gender alone",
           M.board_key(Fake("f", tier="999"), p.event) == ":f")
        TOK = p.token

    # --- a solo who typed a partner anyway: it must not travel
    c.cookies.clear()
    r = fill(c, email="solo@x.com", sex="f", tier=SOLO,
             partner_name="Somebody Else")
    ck("solo registered", r.status_code == 303 and "err=" not in r.headers["location"])
    with Session(engine) as db:
        s = db.query(M.EventParticipant).filter_by(email="solo@x.com").one()
        ck("solo carries no partner", s.partner_name is None)
        ck("a solo still reads as one name", board_name(s) == "Trina P.")

    # --- a pair who left it blank: the door has to ask
    c.cookies.clear()
    r = fill(c, email="blank@x.com", sex="x", tier=DBL, partner_name="")
    with Session(engine) as db:
        b = db.query(M.EventParticipant).filter_by(email="blank@x.com").one()
        ck("blank partner is blank", b.partner_name is None)
        BTOK = b.token

    # ------------------------------------------------------------ the door --
    c.post("/login", data={"username": "admin", "pin": "123456"})
    j = c.post("/events/%d/scan" % EID, data={"code": BTOK}).json()
    ck("scan says it is a pair", j.get("ok") and j.get("pairs") is True)
    ck("scan asks for the missing partner", j.get("ask_partner") is True)
    ck("scan names the category", j.get("cat") == "Doubles")

    sv = c.post("/events/%d/scan/partner" % EID,
                data={"token": BTOK, "name": "Vanessa Sampang"}).json()
    ck("door saved the partner", sv.get("ok")
       and sv.get("board") == "Trina P./Vanessa S.")
    with Session(engine) as db:
        b = db.query(M.EventParticipant).filter_by(email="blank@x.com").one()
        ck("partner is on the row", b.partner_name == "Vanessa Sampang")

    j = c.post("/events/%d/scan" % EID, data={"code": BTOK}).json()
    ck("scan stops asking once it is answered", j.get("ask_partner") is False
       and j.get("partner") == "Vanessa Sampang")
    ck("scan shows the board name", j.get("board") == "Trina P./Vanessa S.")

    # a solo is never asked
    j = c.post("/events/%d/scan" % EID, data={"code": TOK}).json()
    ck("the first pair still reads as a pair", j.get("pairs") is True)
    with Session(engine) as db:
        sid = db.query(M.EventParticipant).filter_by(email="solo@x.com").one().token
    j = c.post("/events/%d/scan" % EID, data={"code": sid}).json()
    ck("a solo is never asked for a partner",
       j.get("pairs") is False and j.get("ask_partner") is False)

    # ------------------------------------------------------- staff override --
    with Session(engine) as db:
        pid = db.query(M.EventParticipant).filter_by(email="trina@x.com").one().id
    c.post("/events/%d/people/%d/edit" % (EID, pid),
           data={"name": "Trina Pangilinan", "email": "trina@x.com",
                 "sex": "x", "category": "open", "country": "PH",
                 "partner_name": "Vanessa Sampang-Cruz", "pay": "keep",
                 "rsvp": "keep", "entry": "", "amount": ""},
           follow_redirects=False)
    with Session(engine) as db:
        p = db.get(M.EventParticipant, pid)
        ck("staff can fix the partner", p.partner_name == "Vanessa Sampang-Cruz")
        ck("gender override takes Mixed", p.sex == "x")
    c.post("/events/%d/people/%d/edit" % (EID, pid),
           data={"name": "Trina Pangilinan", "email": "trina@x.com",
                 "sex": "x", "category": "open", "country": "PH",
                 "partner_name": "", "pay": "keep", "rsvp": "keep",
                 "entry": "", "amount": ""},
           follow_redirects=False)
    with Session(engine) as db:
        ck("blank clears the partner",
           db.get(M.EventParticipant, pid).partner_name is None)

    # ---------------------------------------------------- the form builder --
    # The tick in the builder has to survive the round trip, and the sign-up
    # has to stop asking the moment it is unticked.
    d = c.get("/events/%d/form.json" % EID)
    if d.status_code != 200:                   # no such endpoint, read via save
        d = c.post("/events/%d/form/save" % EID, json={})
    doc = d.json()
    doc = doc.get("doc", doc)
    rates = doc["rates"]
    ck("builder reports the pair flag",
       [r["pairs"] for r in rates] == [False, True])
    for r in rates:
        r["pairs"] = False
    back = c.post("/events/%d/form/save" % EID,
                  json={"rates": rates, "pages": doc["pages"],
                        "look": doc["look"]}).json()
    ck("untick saves", all(not r["pairs"] for r in back["doc"]["rates"]))
    # A fresh visitor, or the cookie from the sign-ups above lands us on
    # somebody's own page instead of the form - which would pass this for the
    # wrong reason.
    c.cookies.clear()
    ck("sign-up stops asking once nothing is a pair",
       'id="pairq"' not in c.get("/r/leg3").text)
    c.post("/login", data={"username": "admin", "pin": "123456"})
    with Session(engine) as db:
        b = db.query(M.EventParticipant).filter_by(email="blank@x.com").one()
        ck("a name already typed is not thrown away",
           b.partner_name == "Vanessa Sampang")
    for r in rates:
        r["pairs"] = (r["label"] == "Doubles")
    c.post("/events/%d/form/save" % EID,
           json={"rates": rates, "pages": doc["pages"], "look": doc["look"]})
    c.cookies.clear()
    ck("re-tick comes back", 'id="pairq"' in c.get("/r/leg3").text)
    c.post("/login", data={"username": "admin", "pin": "123456"})

    # ------------------------------------------------------ the public board --
    c.post("/events/%d/board-link" % EID, follow_redirects=False)
    with Session(engine) as db:
        bt = db.get(M.Event, EID).board_token
    board = c.get("/l/%s" % bt).text
    ck("board link works", bt and "AWAKEN" in board)
    ck("board carries the pair as one name",
       "Trina P./Vanessa S." in board)
    ck("board labels the mixed doubles column",
       "Mixed" in board and "Doubles" in board)
    res_html = c.get("/l/%s/results" % bt).text
    ck("results chips are the event's categories",
       'data-v="%s"' % DBL in res_html and ">Doubles<" in res_html)

# ------------------------------------------- an event with no categories --
# An invitational where nobody picked one. The board must not collapse into a
# single nameless heap, and nobody may be dropped off it.
with Session(engine) as db:
    iv = M.Event(name="PFT", slug="pft", mode=M.EVENT_INVITE)
    db.add(iv); db.flush()
    for i, sx in enumerate(["m", "f", "x", None]):
        db.add(M.EventParticipant(event_id=iv.id, token="iv%d" % i,
                                  name="P%d Q" % i, first_name="P%d" % i,
                                  last_name="Q", email="iv%d@x.com" % i,
                                  sex=sx, rsvp="yes"))
    db.commit()
    iv = db.query(M.Event).filter_by(slug="pft").one()
    cols = M.board_rows(iv)
    ck("no categories falls back to gender alone",
       [g["label"] for g in cols] == ["Male", "Female", "Mixed", "Unlisted"])
    ck("nobody is dropped off a board with no categories",
       sum(len(g["rows"]) for g in cols) == 4)

bad = [n for n, ok in res if not ok]
print("\n%d/%d passed" % (len(res) - len(bad), len(res)))
if bad:
    print("FAILED: " + "; ".join(bad))
