"""One station, different work per category.

A kids' test is the same nine stations for everybody and nowhere near the same
work: the 13-15s push 102 kg over 30 m where the 5-8s push 35 kg over 12.5, and
station five is a Frogger for the little ones and a burpee broad jump for
everyone else. Before this it was nine stations or four events.

What has to hold together:
  - a station with no variants behaves exactly as it always did;
  - a category's target reaches the coach's phone, the splits and the board;
  - a split row (Super Hero Boys) beats the category's own row;
  - 12.5 m survives the round trip, and a count can actually reach it;
  - the name and the load travel too, because Frogger is not a distance;
  - blanking a variant is how you go back to "same for everyone";
  - and the stations themselves can still be added and removed by hand.
"""
import os, re
os.environ["DATABASE_URL"] = ("postgresql+psycopg2://postgres@/statcat"
                              "?host=/home/claude/pgrun&port=5433")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_INITIAL_PIN", "123456")
from decimal import Decimal
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import Base, engine
from app import models as M
from fastapi.testclient import TestClient
from app.main import app

res = []
def ck(n, c):
    res.append((n, bool(c)))
    print("PASS" if c else "FAIL", n)

with engine.begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

with TestClient(app) as c:
    c.post("/login", data={"username": "admin", "pin": "123456"})

    with Session(engine) as db:
        ev = M.Event(name="Hybrid Kids", slug="hybrid-kids",
                     mode=M.EVENT_OPEN, capacity=50, status="running")
        db.add(ev); db.flush()
        mini = M.EventRate(event_id=ev.id, label="Mini Hero", amount=1500,
                           capacity=25, position=0)
        sup = M.EventRate(event_id=ev.id, label="Super Hero", amount=1500,
                          capacity=30, position=1)
        db.add_all([mini, sup]); db.flush()
        run = M.EventStation(event_id=ev.id, position=0, name="Run",
                             measure="distance", target=400, increment=100)
        sled = M.EventStation(event_id=ev.id, position=1, name="Sled push",
                              measure="distance", target=30, increment=5)
        frog = M.EventStation(event_id=ev.id, position=2, name="BBJ",
                              measure="distance", target=40, increment=5)
        db.add_all([run, sled, frog]); db.flush()
        MINI, SUP = mini.id, sup.id
        EV, RUN, SLED, FROG = ev.id, run.id, sled.id, frog.id

        # Mini Hero pushes a lighter sled a shorter way, and does a Frogger
        # instead of a burpee broad jump.
        db.add(M.StationVariant(station_id=SLED, rate_id=MINI, sex="",
                                target=Decimal("12.5"), load="35 kg"))
        db.add(M.StationVariant(station_id=FROG, rate_id=MINI, sex="",
                                target=20, name="Frogger"))
        # Super Hero is 70 kg, except the boys, who push 102.
        db.add(M.StationVariant(station_id=SLED, rate_id=SUP, sex="",
                                target=30, load="70 kg"))
        db.add(M.StationVariant(station_id=SLED, rate_id=SUP, sex="m",
                                target=30, load="102 kg"))

        kid = M.EventParticipant(event_id=EV, name="Ellie", email="e@x.com",
                                 token="t-mini", tier=str(MINI), sex="f",
                                 heat_time="08:10")
        girl = M.EventParticipant(event_id=EV, name="Sabine", email="s@x.com",
                                  token="t-shg", tier=str(SUP), sex="f",
                                  heat_time="08:20")
        boy = M.EventParticipant(event_id=EV, name="Federico", email="f@x.com",
                                 token="t-shb", tier=str(SUP), sex="m",
                                 heat_time="08:30")
        plain = M.EventParticipant(event_id=EV, name="Nobody", email="n@x.com",
                                   token="t-none", heat_time="08:40")
        db.add_all([kid, girl, boy, plain]); db.commit()
        KID, GIRL, BOY, PLAIN = kid.id, girl.id, boy.id, plain.id

    # ── resolution ──
    with Session(engine) as db:
        sp = {s.name: s for s in M.stations_for(db.get(M.EventParticipant, KID))}
        ck("mini hero's sled is 12.5 m", sp["Sled push"].target == 12.5)
        ck("and carries its load", sp["Sled push"].load == "35 kg")
        ck("mini hero does a Frogger", "Frogger" in sp)
        ck("the station it overrides is gone from their list", "BBJ" not in sp)
        ck("an untouched station is untouched", sp["Run"].target == 400)
        ck("the run still points at the real station",
           sp["Sled push"].id == SLED)

        sg = {s.name: s for s in M.stations_for(db.get(M.EventParticipant, GIRL))}
        sb = {s.name: s for s in M.stations_for(db.get(M.EventParticipant, BOY))}
        ck("super hero girls get the category row",
           sg["Sled push"].load == "70 kg")
        ck("super hero boys get the split row",
           sb["Sled push"].load == "102 kg")
        ck("both still race 30 m",
           sg["Sled push"].target == 30 and sb["Sled push"].target == 30)
        ck("the girls do a BBJ, not a Frogger", "BBJ" in sg)

        sn = {s.name: s for s in M.stations_for(db.get(M.EventParticipant, PLAIN))}
        ck("somebody with no category races the station as set",
           sn["Sled push"].target == 30 and sn["Sled push"].load == ""
           and "BBJ" in sn)

    # ── the number a coach counts to ──
    ck("taps over a decimal round up",
       M.StationSpec(type("S", (), {"name": "x", "position": 0, "id": 1,
                                    "measure": "distance", "target": 12.5,
                                    "increment": 5})()).taps == 3)
    ck("tidy keeps whole numbers whole", M.tidy(Decimal("300.00")) == 300)
    ck("tidy keeps the half", M.tidy(Decimal("12.50")) == 12.5)

    # ── the splits read the same resolved numbers ──
    with Session(engine) as db:
        rows = M.station_splits(db.get(M.EventParticipant, KID))
        by = {r["name"]: r for r in rows}
        ck("the sheet shows their own target", by["Sled push"]["target"] == 12.5)
        ck("and their own load", by["Sled push"]["load"] == "35 kg")
        ck("and the movement they actually do", "Frogger" in by)

    # ── the settings page ──
    page = c.get("/events/%d/stations" % EV).text
    ck("the page offers every category", "Mini Hero" in page and "Super Hero" in page)
    ck("a station that differs is already open",
       'data-panel="%d"' % SLED in page and 'data-panel="%d" hidden' % SLED not in page)
    ck("one that does not is folded shut", 'data-panel="%d" hidden' % RUN in page)
    ck("the half is shown as a half, not 12.50", 'value="12.5"' in page)
    ck("the default is shown as the placeholder, not 30.00",
       'placeholder="30"' in page)
    ck("the split row is drawn", 'name="v_%d_%d_m_l"' % (SLED, SUP) in page)

    # ── saving: add a station by hand, and change a variant, in one press ──
    def post(fields):
        r = c.post("/events/%d/stations" % EV, data=fields,
                   follow_redirects=False)
        return r.status_code
    base = {
        "sid": [str(RUN), str(SLED), str(FROG)],
        "name": ["Run", "Sled push", "BBJ"],
        "measure": ["distance", "distance", "distance"],
        "target": ["400", "30", "40"],
        "inc": ["100", "5", "5"],
    }
    keep_variants = {
        "v_%d_%d__t" % (SLED, MINI): "12.5",
        "v_%d_%d__l" % (SLED, MINI): "35 kg",
        "v_%d_%d__t" % (FROG, MINI): "20",
        "v_%d_%d__n" % (FROG, MINI): "Frogger",
        "v_%d_%d__l" % (SLED, SUP): "70 kg",
        "v_%d_%d_m_l" % (SLED, SUP): "102 kg",
    }
    add = dict(base)
    add["sid"] = base["sid"] + ["new"]
    add["name"] = base["name"] + ["Wall balls"]
    add["measure"] = base["measure"] + ["reps"]
    add["target"] = base["target"] + ["50"]
    add["inc"] = base["inc"] + ["5"]
    add.update(keep_variants)
    ck("saving redirects", post(add) == 303)
    with Session(engine) as db:
        ev = db.get(M.Event, EV)
        names = [s.name for s in sorted(ev.stations, key=lambda x: x.position)]
        ck("a station added by hand is kept", names == ["Run", "Sled push",
                                                        "BBJ", "Wall balls"])
        sled = db.get(M.EventStation, SLED)
        # Mini Hero, Super Hero, and the Super Hero boys' split.
        ck("every variant survived the save", len(sled.variants) == 3)
        ck("including the one that only changes the load",
           any(v.rate_id == SUP and not v.sex and v.load == "70 kg"
               and v.target is None for v in sled.variants))
        ck("12.5 came back as 12.5",
           float(next(v.target for v in sled.variants
                      if v.rate_id == MINI)) == 12.5)

    # remove one station, and blank a variant back to "same for everyone"
    gone = dict(base)
    gone["sid"] = [str(RUN), str(SLED)]
    gone["name"] = ["Run", "Sled push"]
    gone["measure"] = ["distance", "distance"]
    gone["target"] = ["400", "30"]
    gone["inc"] = ["100", "5"]
    gone["v_%d_%d__t" % (SLED, MINI)] = "12.5"
    gone["v_%d_%d__l" % (SLED, MINI)] = "35 kg"
    # the Super Hero rows are simply not posted — that is how they go away
    ck("second save redirects", post(gone) == 303)
    with Session(engine) as db:
        ev = db.get(M.Event, EV)
        ck("removing a station removes it",
           [s.name for s in ev.stations] == ["Run", "Sled push"])
        sled = db.get(M.EventStation, SLED)
        ck("a variant nobody posted is gone", len(sled.variants) == 1)
        ck("the one still posted stayed",
           sled.variants[0].rate_id == MINI)
        ck("the station a removed one hung off went with it",
           db.get(M.EventStation, FROG) is None)
        ck("and so did its variants",
           db.query(M.StationVariant).filter_by(station_id=FROG).count() == 0)
        girl = db.get(M.EventParticipant, GIRL)
        ck("super hero is back to the station's own numbers",
           {s.name: s for s in M.stations_for(girl)}["Sled push"].load == "")

    # ── the coach's phone counts to the athlete's own target ──
    with Session(engine) as db:
        staff = db.query(M.Staff).filter_by(username="admin").one()
        for pid in (KID,):
            p = db.get(M.EventParticipant, pid)
            p.coach_id = staff.id
        db.commit()
    # The first tap of the race opens station one itself, so walk through it
    # to reach the sled — which is the one with a 12.5 m target.
    r = c.post("/race/p/%d/tap" % KID, json={"count": 400})
    if r.status_code != 200 or "count" not in r.json():
        print("   coach response:", r.status_code, r.text[:200])
    ck("station one counts to its own target", r.json().get("count") == 400)
    ck("and closes at it", r.json().get("closed") is True)
    c.post("/race/p/%d/next" % KID, follow_redirects=False)

    r = c.post("/race/p/%d/tap" % KID, json={"count": 10})
    ck("a count below the target is taken", r.json().get("count") == 10)
    ck("the phone is told this athlete's target",
       float(r.json().get("target")) == 12.5)
    ck("and the station is still open", r.json().get("closed") is False)
    r = c.post("/race/p/%d/tap" % KID, json={"count": 15})
    ck("a tap past a 12.5 target closes it", r.json().get("closed") is True)
    ck("and is clamped to the target, not down to 12",
       r.json().get("count") == 12.5)

    # The same station, for somebody whose category never touched it.
    with Session(engine) as db:
        staff = db.query(M.Staff).filter_by(username="admin").one()
        db.get(M.EventParticipant, PLAIN).coach_id = staff.id
        db.commit()
    c.post("/race/p/%d/tap" % PLAIN, json={"count": 400})
    c.post("/race/p/%d/next" % PLAIN, follow_redirects=False)
    r = c.post("/race/p/%d/tap" % PLAIN, json={"count": 12})
    ck("no category means the station's own 30 m",
       float(r.json().get("target")) == 30 and r.json().get("closed") is False)

bad = [n for n, ok in res if not ok]
print("\n%d/%d passed" % (len(res) - len(bad), len(res)))
if bad:
    print("FAILED: " + "; ".join(bad))
