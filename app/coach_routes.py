"""AWAKEN coach app — one phone, one athlete, one station at a time.

Registered from main.py via ``register(app, deps)``, like the other feature
modules, so this file never imports main.

What a coach actually does on race day, in order: pick the event, pick the heat
they are working, grab the person they are coaching, then count. That is three
screens and no more. Everything else — the leaderboard, the finisher board, the
results sheet — lives on the admin side, on a laptop or a wall, not in the hand
of somebody watching another human do burpees.

The rules that shaped this, and that the code has to keep:

* **The race clock is fixed.** Running time is always ``now`` minus the official
  heat start. Nothing on the coach's phone can move it, so a coach who is late
  to press anything costs their athlete nothing. The station timers are a
  separate stopwatch and they exist only to say *where* the time went.
* **A station closes itself at the target.** The end time is stamped at the
  moment the last rep landed, not when the coach got round to pressing
  something. The phone knows that moment even if the network does not.
* **The gap before NEXT STATION is real.** She walks to the next lane, gets
  water, waits for a rower. That time belongs to nobody's split, and the sheet
  shows it as its own column rather than hiding it in a rounding error.
* **Grabbing is exclusive.** One coach, one athlete, one screen — a coach
  juggling two timers will lose reps on both.
* **It lives at ``/race``, not ``/coach``.** ``/coach`` is the older stand-alone
  HYROX group timer and is still mounted; this is the event-driven one, built
  on real participants and configurable stations. Two apps at one URL would
  mean whichever loaded first quietly won.
* **Taps survive a dead signal.** Every save sends the *absolute* count plus the
  moment it happened, so a request that arrives late is simply superseded by
  the next one. There is no queue to get out of order, and a dropout costs
  nothing but a spinner.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import get_db
from .models import (
    can_coach, h12, heat_open_mins, is_test_athlete,
    EVENT_CLOSED, EVENT_DRAFT, Event, EventParticipant, EventStation,
    RSVP_NO, Staff, StationRun, to_local,
)

def hhmm_key(t: str) -> str:
    """'10:20' -> '1020'. Used in URLs so a heat is one clean path segment."""
    return (t or "").replace(":", "")


def mmss(secs) -> str:
    """Seconds as the clock a coach reads out loud."""
    if secs is None:
        return "—"
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def _moment(raw, run, now):
    """Trust the phone about *when*, within reason.

    The phone knows the instant the last rep landed even when the network
    does not, and that instant is the honest end of the station. But a
    phone with a wrong clock, or a replayed request, must not be able to
    invent a time — so it is clamped to the station's own window.
    """
    try:
        at = datetime.fromtimestamp(float(raw) / 1000.0, timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return now
    floor = run.started_at or now
    if at < floor:
        return floor
    if at > now + timedelta(seconds=2):
        return now
    return at


def register(app, deps):
    templates = deps["templates"]

    # ------------------------------------------------------------- guard ----

    def guard(request, db):
        """Anyone on the Coach role, or anyone running the event area.

        Wider than the admin event area on purpose: on the morning of a race
        the failure that actually costs you is a coach who cannot get into the
        app. The blast radius is a rep count, and every count is visible and
        correctable from the admin side.

        It is no longer *every* signed-in member of staff, though — a coach
        should be able to be given this and nothing else, and somebody trusted
        with the till has no business on the race floor by default.
        """
        staff = current_staff(request, db)
        if not staff:
            return None, RedirectResponse("/login", status_code=303)
        if not can_coach(staff):
            return None, RedirectResponse("/", status_code=303)
        return staff, None

    def _shell(request, name, **ctx):
        ctx["request"] = request
        ctx.setdefault("mmss", mmss)
        ctx.setdefault("h12", h12)
        # Dates are stored in UTC. A screen that reads them raw calls an event
        # starting at 6am on the 23rd "Sat 22 Aug", which on a race morning is
        # a coach checking they are at the right event.
        ctx.setdefault("day", lambda d: to_local(d).strftime("%a %-d %b")
                       if d else "")
        return templates.TemplateResponse(name, ctx)

    # ------------------------------------------------------------- model ----

    def stations_of(ev):
        return sorted(ev.stations, key=lambda s: (s.position, s.id))

    def runs_of(p):
        return {r.station_id: r for r in p.runs}

    def race_state(p):
        """Where this athlete is, as one dict every screen and POST agrees on.

        One place decides, so the page and the endpoint that acts on it can
        never disagree about which station is open.
        """
        sts = stations_of(p.event)
        runs = runs_of(p)
        state = {"stations": sts, "runs": runs, "open": None, "open_run": None,
                 "next": None, "closed": [], "done": False, "index": 0}
        if not sts:
            return state
        for i, s in enumerate(sts):
            r = runs.get(s.id)
            if r is None:
                # Nothing opened yet at this position: this is what NEXT
                # STATION (or the first tap) will open.
                state["next"] = s
                state["index"] = i
                return state
            if r.ended_at is None:
                state["open"], state["open_run"], state["index"] = s, r, i
                return state
            state["closed"].append((s, r))
        state["done"] = True
        state["index"] = len(sts)
        return state

    def open_station(db, p, st, when=None):
        """Create the run that starts a station's own stopwatch.

        Station one starts at the *heat* time, not at the first tap — the walk
        from the line to the first rep is part of that station, and a coach
        should not have to press anything at the gun for it to count.
        """
        now = datetime.now(timezone.utc)
        if when is None:
            first = stations_of(p.event)[:1]
            when = (p.heat_start() or now) if (first and first[0].id == st.id) else now
        run = StationRun(participant_id=p.id, station_id=st.id, count=0,
                         started_at=min(when, now))
        db.add(run)
        db.flush()
        return run

    def close_run(db, p, st, run, when):
        run.ended_at = when
        sts = stations_of(p.event)
        if sts and sts[-1].id == st.id:
            p.finished_at = when

    def coach_name(db, cid):
        if not cid:
            return None
        s = db.get(Staff, cid)
        return s.name if s else None

    def live_events(db):
        """Events worth opening on a phone: not a draft, not closed, and with
        a race set up. An event with no stations has nothing to count."""
        evs = (db.query(Event)
               .filter(~Event.status.in_([EVENT_DRAFT, EVENT_CLOSED]))
               .order_by(Event.starts_at.desc()).all())
        return [e for e in evs if e.stations]

    def racers(db, ev, heat=None):
        q = (db.query(EventParticipant)
             .filter(EventParticipant.event_id == ev.id,
                     EventParticipant.heat_time.isnot(None),
                     EventParticipant.released_at.is_(None),
                     EventParticipant.rsvp != RSVP_NO))
        if heat is not None:
            q = q.filter(EventParticipant.heat_time == heat)
        rows = q.all()
        rows.sort(key=lambda p: ((p.heat_time or ""), (p.full_name or "").lower()))
        return rows

    # ------------------------------------------------------------ screens ---

    @app.get("/race", response_class=HTMLResponse)
    def coach_home(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        evs = live_events(db)
        if len(evs) == 1:
            return RedirectResponse("/race/e/%d" % evs[0].id, status_code=303)
        return _shell(request, "coach_events.html", staff=staff, events=evs)

    @app.get("/race/e/{eid}", response_class=HTMLResponse)
    def coach_heats(request: Request, eid: int, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/race", status_code=303)
        rows = racers(db, ev)
        heats = {}
        for p in rows:
            h = heats.setdefault(p.heat_time, {"time": p.heat_time, "n": 0,
                                               "taken": 0, "mine": 0,
                                               "done": 0, "test": False})
            h["n"] += 1
            if is_test_athlete(p):
                h["test"] = True
            if p.coach_id:
                h["taken"] += 1
            if p.coach_id == staff.id:
                h["mine"] += 1
            if p.finished_at:
                h["done"] += 1
        order = sorted(heats.values(), key=lambda h: h["time"])
        now = datetime.now(timezone.utc)
        for h in order:
            h["key"] = hhmm_key(h["time"])
            h["at"] = _heat_moment(ev, h["time"])
            h["opens"] = heat_opens(ev, h["at"])
            # A heat holding a test athlete can always be entered. Only they
            # can be grabbed out of it early, though - the grab below still
            # asks the window about everybody else.
            h["open"] = h["test"] or heat_is_open(ev, h["at"], now)
            # The page has a clock on it already. Handing it the moment each
            # shut heat comes due means a coach standing on the list at 9:49
            # sees it open by itself, rather than learning that a phone needs
            # pulling down to refresh while their athlete is walking to the
            # line.
            h["opens_ms"] = (int(h["opens"].timestamp() * 1000)
                             if h["opens"] is not None and not h["open"]
                             else None)
            # Gym time, not UTC, and said the way the room says it.
            h["opens_label"] = (h12(to_local(h["opens"]).strftime("%H:%M"))
                                if h["opens"] is not None else "")
        # Somebody who typed or bookmarked a heat that has not opened yet is
        # sent back here; saying why beats a list that simply refuses to move.
        early = (request.query_params.get("early") or "").strip()
        early = h12("%s:%s" % (early[:2], early[2:4])) if len(early) >= 4 else ""
        return _shell(request, "coach_heats.html", staff=staff, ev=ev,
                      heats=order, now=now, window=heat_open_mins(ev),
                      early=early, stations=len(stations_of(ev)))

    def _heat_moment(ev, t):
        if not t or not ev.starts_at:
            return None
        try:
            h, m = int(t[:2]), int(t[3:])
        except (ValueError, IndexError):
            return None
        from .models import from_local
        day = to_local(ev.starts_at)
        return from_local(day.replace(hour=h, minute=m, second=0, microsecond=0))

    def heat_opens(ev, at):
        """The moment the race app lets a coach into this heat, or None.

        None means there is nothing to work out from — an event with no date,
        or a heat time nobody could parse — and an unanswerable question is not
        grounds for locking a coach out on a race morning.
        """
        if at is None:
            return None
        return at - timedelta(minutes=heat_open_mins(ev))

    def heat_is_open(ev, at, now=None):
        """Is this heat one a coach may work right now?

        Open from ``heat_open_mins`` before the gun, and never shut again. The
        one-sidedness is the point: a heat three hours out is a heat a coach can
        only open by mistake, but a heat that started forty minutes ago may
        still have somebody on the floor, and a coach shut out of a race already
        running has no way back to their own athlete.
        """
        opens = heat_opens(ev, at)
        if opens is None:
            return True
        return (now or datetime.now(timezone.utc)) >= opens

    @app.get("/race/e/{eid}/h/{hm}", response_class=HTMLResponse)
    def coach_heat(request: Request, eid: int, hm: str,
                   db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/race", status_code=303)
        heat = "%s:%s" % (hm[:2], hm[2:4]) if len(hm) >= 4 else hm
        at = _heat_moment(ev, heat)
        rows_for_gate = racers(db, ev, heat)
        # Greying the row on the list is a courtesy, not a lock: this address
        # is four digits and a coach who has worked one heat can type another.
        # The gate has to be here, where the athletes are.
        if not (heat_is_open(ev, at)
                or any(is_test_athlete(x) for x in rows_for_gate)):
            return RedirectResponse("/race/e/%d?early=%s" % (ev.id, hm),
                                    status_code=303)
        rows = rows_for_gate
        people = []
        for p in rows:
            st = race_state(p)
            total = len(st["stations"])
            people.append({
                "p": p,
                # Shown on the row, because a clock that behaves differently
                # and does not admit it is how a real result gets doubted.
                "test": is_test_athlete(p),
                "grabbable": is_test_athlete(p) or heat_is_open(ev, at),
                "mine": p.coach_id == staff.id,
                "coach": coach_name(db, p.coach_id),
                "done": bool(p.finished_at),
                # Human numbering, and only once they have actually started —
                # "station 1 of 5" against somebody standing on the line reads
                # as though the race has begun.
                "started": bool(st["runs"]),
                "pos": min(st["index"] + 1, total) if total else 0,
                "total": total,
            })
        return _shell(request, "coach_heat.html", staff=staff, ev=ev, heat=heat,
                      hm=hm, people=people, at=at,
                      now=datetime.now(timezone.utc))

    def _load(request, db, pid):
        staff, redir = guard(request, db)
        if redir:
            return None, None, redir
        p = db.get(EventParticipant, pid)
        if not p:
            return None, None, RedirectResponse("/race", status_code=303)
        return staff, p, None

    @app.post("/race/p/{pid}/grab")
    def coach_grab(request: Request, pid: int, db: Session = Depends(get_db)):
        staff, p, redir = _load(request, db, pid)
        if redir:
            return redir
        back = "/race/e/%d/h/%s" % (p.event_id, hhmm_key(p.heat_time))
        # And again on the way in. A POST does not have to come from a page we
        # drew, and a grab is the thing the window exists to prevent — holding
        # somebody out of a heat three hours away, out of sight of the coach
        # who will actually be running it.
        if not (is_test_athlete(p)
                or heat_is_open(p.event, _heat_moment(p.event, p.heat_time))):
            return RedirectResponse(
                "/race/e/%d?early=%s" % (p.event_id, hhmm_key(p.heat_time)),
                status_code=303)
        # Exclusive: taken is taken. The other phone is already counting, and
        # two people counting the same athlete is worse than one.
        if p.coach_id and p.coach_id != staff.id:
            return RedirectResponse(back + "?taken=1", status_code=303)
        p.coach_id = staff.id
        p.grabbed_at = p.grabbed_at or datetime.now(timezone.utc)
        db.commit()
        return RedirectResponse("/race/p/%d" % p.id, status_code=303)

    @app.post("/race/p/{pid}/drop")
    def coach_drop(request: Request, pid: int, db: Session = Depends(get_db)):
        staff, p, redir = _load(request, db, pid)
        if redir:
            return redir
        if p.coach_id == staff.id:
            p.coach_id = None
            db.commit()
        return RedirectResponse("/race/e/%d/h/%s"
                                % (p.event_id, hhmm_key(p.heat_time)),
                                status_code=303)

    @app.get("/race/p/{pid}", response_class=HTMLResponse)
    def coach_race(request: Request, pid: int, db: Session = Depends(get_db)):
        staff, p, redir = _load(request, db, pid)
        if redir:
            return redir
        st = race_state(p)
        start = p.heat_start()
        now = datetime.now(timezone.utc)
        splits = []
        for s, r in st["closed"]:
            splits.append({"name": s.name, "secs": r.seconds,
                           "count": r.count, "unit": s.unit})
        # The stretch since the last station closed: real time, and the coach
        # should be able to see it running rather than wonder if it counts.
        between = None
        if st["closed"] and not st["open"] and not st["done"]:
            between = int((now - st["closed"][-1][1].ended_at).total_seconds())

        # Four phases, decided here rather than in the template, so the screen
        # and the endpoints can never disagree about what is happening.
        #   pre      nothing started — station one is waiting for the first tap
        #   open     a station is counting
        #   between  one closed, the next not yet opened
        #   done     the last station closed
        if st["done"]:
            phase = "done"
        elif st["open"]:
            phase = "open"
        elif not st["runs"]:
            phase = "pre"
        else:
            phase = "between"

        # In "pre" the coach still sees the counting screen: the first tap is
        # what opens station one, and a NEXT STATION button on the start line
        # would be a button that starts a race nobody is in yet.
        show_st = st["open"] if phase == "open" else (
            st["next"] if phase == "pre" else None)
        # An event whose stations have not been set up has nothing to show and
        # nothing to count. Say so, rather than letting the counting screen ask
        # a station that is not there for its target — which is a white
        # "Internal Server Error" in a coach's hand on a race morning.
        if phase in ("open", "pre") and show_st is None:
            phase = "nostations"
        # Station one's own clock starts at the heat, so it is already running
        # before anybody taps. Everything after it starts when it is opened.
        if phase == "open" and st["open_run"] and st["open_run"].started_at:
            st_ms = int(st["open_run"].started_at.timestamp() * 1000)
        elif phase == "pre" and start:
            st_ms = int(start.timestamp() * 1000)
        else:
            st_ms = None

        return _shell(
            request, "coach_race.html", staff=staff, p=p, ev=p.event,
            stations=st["stations"], phase=phase, show_st=show_st,
            count=(st["open_run"].count if st["open_run"] else 0),
            next_st=st["next"], index=st["index"],
            splits=splits, between=between, st_ms=st_ms,
            start_ms=int(start.timestamp() * 1000) if start else None,
            now_ms=int(now.timestamp() * 1000),
            run_secs=p.running_seconds(now),
            race_secs=p.race_seconds,
            mine=(p.coach_id == staff.id), test=is_test_athlete(p),
            coach=coach_name(db, p.coach_id))

    # -------------------------------------------------------------- taps ----

    @app.post("/race/p/{pid}/tap")
    async def coach_tap(request: Request, pid: int,
                        db: Session = Depends(get_db)):
        """Save the absolute count. Idempotent on purpose — see the module note."""
        staff, p, redir = _load(request, db, pid)
        if redir:
            return JSONResponse({"ok": False, "error": "signed out"}, 401)
        if p.coach_id != staff.id:
            return JSONResponse({"ok": False, "error": "not yours"}, 409)
        body = await request.json()
        st = race_state(p)
        now = datetime.now(timezone.utc)
        station, run = st["open"], st["open_run"]
        if station is None:
            if st["next"] is None:
                return JSONResponse({"ok": False, "error": "race over"}, 409)
            # The very first tap of the race opens station one itself, so the
            # coach never has to press Start at the gun. Every *other* station
            # is opened by NEXT STATION and nothing else — a stray tap after a
            # station closes must not start the next one's clock while the
            # athlete is still walking to it.
            if st["runs"]:
                return JSONResponse({"ok": False, "error": "between stations"},
                                    409)
            station = st["next"]
            run = open_station(db, p, station)
        count = body.get("count")
        try:
            count = int(count)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "bad count"}, 400)
        run.count = max(0, min(int(station.target or 0), count))
        if run.count >= (station.target or 0) and run.ended_at is None:
            close_run(db, p, station, run, _moment(body.get("at_ms"), run, now))
        db.commit()
        return JSONResponse({
            "ok": True, "count": run.count, "target": station.target,
            "closed": run.ended_at is not None,
            "station_secs": run.seconds,
            "finished": bool(p.finished_at),
        })

    @app.post("/race/p/{pid}/next")
    def coach_next(request: Request, pid: int, db: Session = Depends(get_db)):
        """Close nothing, open the next. The gap before this press is the
        between-stations time, and it belongs to nobody's split."""
        staff, p, redir = _load(request, db, pid)
        if redir:
            return redir
        if p.coach_id != staff.id:
            return RedirectResponse("/race/p/%d" % p.id, status_code=303)
        st = race_state(p)
        if st["open"] is None and st["next"] is not None:
            open_station(db, p, st["next"], datetime.now(timezone.utc))
            db.commit()
        return RedirectResponse("/race/p/%d" % p.id, status_code=303)

    @app.post("/race/p/{pid}/reopen")
    def coach_reopen(request: Request, pid: int, db: Session = Depends(get_db)):
        """Put the last closed station back into counting.

        Quiet on purpose. A saved time from a wrong count is worse than no
        time, so the way back exists — but it is not a button anybody hits by
        accident with a thumb.
        """
        staff, p, redir = _load(request, db, pid)
        if redir:
            return redir
        if p.coach_id != staff.id:
            return RedirectResponse("/race/p/%d" % p.id, status_code=303)
        st = race_state(p)
        if st["open"] is None and st["closed"]:
            s, r = st["closed"][-1]
            r.ended_at = None
            r.count = max(0, min(r.count, (s.target or 1) - 1))
            p.finished_at = None
            db.commit()
        return RedirectResponse("/race/p/%d" % p.id, status_code=303)

    @app.post("/race/p/{pid}/stop")
    def coach_stop(request: Request, pid: int, db: Session = Depends(get_db)):
        """End the open station where it stands.

        For the athlete who stops at 60 of 75. Without this the only way out of
        a station is the target, and somebody who cannot finish it strands the
        phone for the rest of the day. The count is kept as it is, so the sheet
        can tell an unfinished station from a finished one.
        """
        staff, p, redir = _load(request, db, pid)
        if redir:
            return redir
        if p.coach_id != staff.id:
            return RedirectResponse("/race/p/%d" % p.id, status_code=303)
        st = race_state(p)
        if st["open"] is not None:
            close_run(db, p, st["open"], st["open_run"],
                      datetime.now(timezone.utc))
            db.commit()
        return RedirectResponse("/race/p/%d" % p.id, status_code=303)
