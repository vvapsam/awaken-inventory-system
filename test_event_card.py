"""An event's own finisher card, and the roster that reaches it.

The card every event drew carried the HYROX mark, the PFT badge and the
sponsor wall. That is right for the fitness test and wrong for a kids' class:
you cannot hand a seven-year-old a card that says Physical Fitness Test on it.

So an event can draw its own instead — the AWAKEN wordmark over a two-tone bar
off the event's name — and the patch page, which was the only way to reach a
card, now lists everybody racing rather than finishers only. That page is worth
opening the week before to check a name reads right, and it could not answer
that while it hid anyone who had not raced.

What has to hold together:
  - an event that was never told keeps the PFT card, exactly as before;
  - the event card draws at three shapes, landscape among them, and the two it
    shares with the PFT card are the same pixel sizes;
  - the bar reads off the name unless it is told otherwise;
  - a card opened before the race says 0:00 rather than "--:--";
  - the PFT card still refuses to draw for somebody who has not finished;
  - awarding a patch still needs a finish time to read a band off;
  - and nothing on the public link changed.
"""
import os, re
os.environ["DATABASE_URL"] = ("postgresql+psycopg2://postgres@/evcard"
                              "?host=/home/claude/pgrun&port=5433")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_INITIAL_PIN", "123456")
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import Base, engine
from app import models as M
from app.card_routes import (SHAPES, EVENT_SHAPES, LOGO_PCT, fit_time,
                             mmss, event_card_context)
from fastapi.testclient import TestClient
from app.main import app

res = []
def ck(n, c):
    res.append((n, bool(c)))
    print("PASS" if c else "FAIL", n)

with engine.begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

# ── the shapes ──
ck("square matches the PFT card's size",
   EVENT_SHAPES["square"]["cw"] == SHAPES["square"]["cw"] == 1920
   and EVENT_SHAPES["square"]["ch"] == SHAPES["square"]["ch"] == 1920)
ck("story matches the PFT card's size",
   (EVENT_SHAPES["story"]["cw"], EVENT_SHAPES["story"]["ch"])
   == (SHAPES["story"]["cw"], SHAPES["story"]["ch"]) == (1080, 1920))
ck("landscape is the square's width on the story's height",
   (EVENT_SHAPES["landscape"]["cw"], EVENT_SHAPES["landscape"]["ch"])
   == (1920, 1080))
ck("the PFT card gains no landscape", "landscape" not in SHAPES)
ck("the lockup is set to 42%", LOGO_PCT == 42)

# ── the time always fits ──
for shape in EVENT_SHAPES:
    w = EVENT_SHAPES[shape]["cw"]
    for clock in ("0:00", "24:18", "1:04:09", "12:34:56"):
        c = event_card_context(None, shape, clock)
        em = sum(0.30 if ch == ":" else 0.56 for ch in clock)
        ck("%s fits %s (%dpx)" % (clock, shape, c["time"]),
           c["time"] * em <= w - c["pad"] * 2 + 1)

ck("no time reads 0:00 when asked", mmss(None, blank="0:00") == "0:00")
ck("and dashes when not", mmss(None) == "--:--")
ck("fit_time never returns nothing", fit_time("", 100, 500) == 100)

with TestClient(app) as c:
    c.post("/login", data={"username": "admin", "pin": "123456"})
    when = datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc)

    with Session(engine) as db:
        kids = M.Event(name="Hybrid Kids 2", slug="hk2", mode=M.EVENT_OPEN,
                       capacity=50, status="running", starts_at=when,
                       card_style="event")
        pft = M.Event(name="HYROX Physical Fitness Test", slug="pft",
                      mode=M.EVENT_OPEN, capacity=40, status="running",
                      starts_at=when)
        db.add_all([kids, pft]); db.flush()
        st = M.EventStation(event_id=kids.id, position=0, name="Run",
                            measure="distance", target=400, increment=100)
        db.add(st); db.flush()
        done = M.EventParticipant(event_id=kids.id, name="Ellie Gazmen",
                                  email="e@x.com", token="tok-done",
                                  heat_time="08:10",
                                  finished_at=when + timedelta(minutes=34,
                                                               seconds=18))
        waiting = M.EventParticipant(event_id=kids.id, name="Zeus Padilla",
                                     email="z@x.com", token="tok-wait",
                                     heat_time="08:20")
        bench = M.EventParticipant(event_id=kids.id, name="On The List",
                                   email="w@x.com", token="tok-wl",
                                   waitlist=True)
        # `declined` is derived from the RSVP, not stored.
        gone = M.EventParticipant(event_id=kids.id, name="Pulled Out",
                                  email="g@x.com", token="tok-gone",
                                  rsvp=M.RSVP_NO)
        pftp = M.EventParticipant(event_id=pft.id, name="Alexander Montes",
                                  email="a@x.com", token="tok-pft")
        db.add_all([done, waiting, bench, gone, pftp]); db.commit()
        KIDS, PFT = kids.id, pft.id
        DONE, WAIT, PFTP = done.id, waiting.id, pftp.id

    # ── the bar reads off the name ──
    with Session(engine) as db:
        ev = db.get(M.Event, KIDS)
        ck("the name splits on its trailing number",
           ev.card_words == ("HYBRID KIDS", "2"))
        ev.card_title, ev.card_badge = "Hybrid Kids", "III"
        db.commit()
        ck("and is overridden when told",
           db.get(M.Event, KIDS).card_words == ("HYBRID KIDS", "III"))
        ev = db.get(M.Event, KIDS)
        ev.card_title = ev.card_badge = None
        db.commit()
        other = db.get(M.Event, PFT)
        ck("a name with no trailing number keeps all of itself",
           other.card_words[0].startswith("HYROX") and other.card_words[1] == "")

    # ── the roster ──
    page = c.get("/patch/e/%d" % KIDS).text
    ck("somebody who has not raced is listed", "Zeus Padilla" in page)
    ck("and so is the finisher", "Ellie Gazmen" in page)
    ck("the waitlist is not racing", "On The List" not in page)
    ck("nor is somebody who pulled out", "Pulled Out" not in page)
    ck("the unraced row says so", "Not raced yet" in page)
    # Twice each: once on the row, once on the button at the end.
    ck("everyone's card is reachable",
       page.count('/patch/p/%d/card' % WAIT) == 2
       and page.count('/patch/p/%d/card' % DONE) == 2)
    ck("the row itself opens the card",
       'data-href="/patch/p/%d/card"' % WAIT in page)
    ck("an event with its own card offers no patch",
       'href="/patch/p/%d"' % DONE not in page)

    pftpage = c.get("/patch/e/%d" % PFT).text
    ck("a PFT event still offers Award once raced, and not before",
       'href="/patch/p/%d"' % PFTP not in pftpage)

    # ── the card ──
    card = c.get("/patch/p/%d/card" % DONE)
    ck("the finisher's card draws", card.status_code == 200)
    ck("it is the event card, not the PFT one",
       "evcard" in card.text and "pft.png" not in card.text)
    ck("the wordmark is the real artwork",
       "/static/card/awaken_word.png" in card.text)
    ck("the bar carries the event", ">HYBRID KIDS<" in card.text)
    ck("and the badge", ">2<" in card.text)
    shown = re.search(r'class="ev-time"[^>]*>([^<]+)<', card.text)
    ck("the time is on it, measured from their heat",
       bool(shown) and shown.group(1).strip() == "24:18")
    ck("the year comes off the event", "Finisher · 2026" in card.text)
    ck("landscape is offered", "shape=landscape" in card.text)

    early = c.get("/patch/p/%d/card" % WAIT)
    ck("a card before the race still draws", early.status_code == 200)
    ck("and reads 0:00", ">0:00<" in early.text or "0:00" in early.text)

    ls = c.get("/patch/p/%d/card?shape=landscape" % DONE).text
    ck("landscape is 1920 x 1080",
       "--cw:1920px" in ls and "--ch:1080px" in ls)

    # ── the PFT card is untouched ──
    pftcard = c.get("/patch/p/%d/card" % PFTP, follow_redirects=False)
    ck("the PFT card still refuses somebody who has not finished",
       pftcard.status_code == 303)
    with Session(engine) as db:
        p = db.get(M.EventParticipant, PFTP)
        p.finished_at = when + timedelta(minutes=44, seconds=14)
        db.commit()
    pftcard = c.get("/patch/p/%d/card" % PFTP)
    ck("and draws its own card once they have",
       pftcard.status_code == 200 and "pft.png" in pftcard.text
       and "evcard" not in pftcard.text)
    ck("the PFT card has no landscape", "shape=landscape" not in pftcard.text)

    # ── awarding still needs a time ──
    r = c.get("/patch/p/%d" % WAIT, follow_redirects=False)
    ck("no finish time, no award screen",
       r.status_code == 303 and "/patch/e/" in r.headers["location"])

    # ── settings round trip ──
    page = c.get("/events/%d/settings" % KIDS).text
    ck("settings offers the card choice", 'name="card_style"' in page)
    ck("and shows what the bar would read", 'placeholder="HYBRID KIDS"' in page)

    # ── the public link is unchanged ──
    pub = c.get("/c/tok-done")
    ck("a finisher's public card opens", pub.status_code == 200)
    ck("it is the event card too", "evcard" in pub.text)
    ck("somebody who has not raced has no public card",
       c.get("/c/tok-wait").status_code == 404)

bad = [n for n, ok in res if not ok]
print("\n%d/%d passed" % (len(res) - len(bad), len(res)))
if bad:
    print("FAILED: " + "; ".join(bad))
