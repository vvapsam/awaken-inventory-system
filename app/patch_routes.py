"""The awarding table — which patch this finisher has earned.

Registered from main.py via ``register(app, deps)``, like the other feature
modules, so this file never imports main.

One member of staff, one laptop at the awarding table, a queue of people who
have just finished. Search a name, confirm their age, and the standard decides
the patch. The whole screen exists so nobody has to hold a table of times in
their head at the end of a long morning.

Two decisions worth knowing:

* **The patch comes before the time.** Staff see which patch to hand over, and
  the finish time only after they ask for it. The job at the table is handing
  over the right thing; the time is the athlete's news, not a number staff
  need in order to do that. Revealing it second also stops the eye doing the
  arithmetic itself and second-guessing the answer.
* **The name is on every screen.** Three people at a trestle table and one
  screen is exactly how the wrong patch reaches the wrong person, so the name
  is at the top of every step and never scrolls away.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import get_db
from .models import Event, EventParticipant

#: The HYROX PFT standard, as seconds. Each bracket is (gold under, silver
#: under); anything at or past the second number is bronze.
#:
#:   under 45   gold < 22:00   silver 22:00–25:59   bronze 26:00+
#:   45 and up  gold < 24:00   silver 24:00–27:59   bronze 28:00+
#:
#: Hard-coded rather than made editable: it is a published standard, not a
#: setting, and a season that moves it is one edit here. Every boundary is
#: "under", so a time landing exactly on 22:00 is silver — which is what
#: "under 22:00" means and what the athlete will have been told.
PATCH_SENIOR_AGE = 45
PATCH_BANDS = {
    "senior": (24 * 60, 28 * 60),
    "open": (22 * 60, 26 * 60),
}
PATCH_LABELS = {"gold": "Gold", "silver": "Silver", "bronze": "Bronze"}
PATCH_EMOJI = {"gold": "🥇", "silver": "🥈", "bronze": "🥉"}

AGE_MIN, AGE_MAX = 5, 110


def band_for(age) -> str:
    return "senior" if (age or 0) >= PATCH_SENIOR_AGE else "open"


def patch_for(age, secs):
    """Which patch, from an age and a finish time. None if either is missing."""
    if age is None or secs is None:
        return None
    gold, silver = PATCH_BANDS[band_for(age)]
    if secs < gold:
        return "gold"
    if secs < silver:
        return "silver"
    return "bronze"


def mmss(secs) -> str:
    if secs is None:
        return "—"
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def register(app, deps):
    render = deps["render"]

    def guard(request, db):
        """Any signed-in member of staff.

        The people on this table are the people already trusted with the till.
        A separate permission would mean somebody locked out of the queue on
        the one morning it matters.
        """
        staff = current_staff(request, db)
        if not staff:
            return None, RedirectResponse("/login", status_code=303)
        return staff, None

    def _shell(request, name, db, staff, **ctx):
        ctx.setdefault("mmss", mmss)
        ctx.setdefault("labels", PATCH_LABELS)
        ctx.setdefault("emoji", PATCH_EMOJI)
        return render(request, name, db, staff, active="events", **ctx)

    def finishers(ev):
        """Everybody who has actually finished, by name.

        Only finishers: the table exists to hand somebody a patch for a race
        they completed, and a name on this list who has not finished is a name
        somebody will try to award.
        """
        rows = [p for p in ev.participants if p.finished_at]
        rows.sort(key=lambda p: (p.full_name or "").lower())
        return rows

    def live_events(db):
        evs = db.query(Event).order_by(Event.starts_at.desc()).all()
        return [e for e in evs if any(p.finished_at for p in e.participants)]

    @app.get("/patch", response_class=HTMLResponse)
    def patch_home(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        evs = live_events(db)
        if len(evs) == 1:
            return RedirectResponse("/patch/e/%d" % evs[0].id, status_code=303)
        return _shell(request, "patch_events.html", db, staff, events=evs)

    @app.get("/patch/e/{eid}", response_class=HTMLResponse)
    def patch_find(request: Request, eid: int, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/patch", status_code=303)
        return _shell(request, "patch_find.html", db, staff, ev=ev,
                      people=finishers(ev))

    def _person(request, db, pid):
        staff, redir = guard(request, db)
        if redir:
            return None, None, redir
        p = db.get(EventParticipant, pid)
        if not p or not p.finished_at:
            return None, None, RedirectResponse("/patch", status_code=303)
        return staff, p, None

    @app.get("/patch/p/{pid}", response_class=HTMLResponse)
    def patch_age(request: Request, pid: int, db: Session = Depends(get_db)):
        """Their age. Pre-filled if we have been told once already."""
        staff, p, redir = _person(request, db, pid)
        if redir:
            return redir
        # Not `p`: base.html does {% set p = request.url.path %}, so a
        # context variable of that name is silently shadowed and every name on
        # the page renders blank.
        return _shell(request, "patch_age.html", db, staff, who=p, ev=p.event,
                      bad=request.query_params.get("bad"))

    @app.post("/patch/p/{pid}")
    def patch_age_save(request: Request, pid: int, age: str = Form(""),
                       db: Session = Depends(get_db)):
        staff, p, redir = _person(request, db, pid)
        if redir:
            return redir
        try:
            n = int((age or "").strip())
        except ValueError:
            return RedirectResponse("/patch/p/%d?bad=1" % pid, status_code=303)
        if not (AGE_MIN <= n <= AGE_MAX):
            return RedirectResponse("/patch/p/%d?bad=1" % pid, status_code=303)
        p.age = n
        # Worked out here and stored, so the patch on the screen and the patch
        # in the record are the same answer rather than two calculations that
        # could drift if the standard is ever edited between them.
        p.patch = patch_for(n, p.race_seconds)
        db.commit()
        return RedirectResponse("/patch/p/%d/result" % pid, status_code=303)

    @app.get("/patch/p/{pid}/result", response_class=HTMLResponse)
    def patch_result(request: Request, pid: int,
                     db: Session = Depends(get_db)):
        staff, p, redir = _person(request, db, pid)
        if redir:
            return redir
        if p.age is None:
            return RedirectResponse("/patch/p/%d" % pid, status_code=303)
        return _shell(request, "patch_result.html", db, staff, who=p,
                      ev=p.event, patch=p.patch or patch_for(p.age,
                                                             p.race_seconds),
                      secs=p.race_seconds)

    @app.post("/patch/p/{pid}/given")
    def patch_given(request: Request, pid: int,
                    db: Session = Depends(get_db)):
        """Handed over. Stamped so the next person at the phone can see it.

        Not a lock — somebody can be given one again if theirs is dropped in a
        car park — but the queue is where a second patch gets handed to
        somebody who already has one, and the screen should say so.
        """
        staff, p, redir = _person(request, db, pid)
        if redir:
            return redir
        p.patch_at = datetime.now(timezone.utc)
        db.commit()
        return RedirectResponse("/patch/e/%d?done=%d" % (p.event_id, p.id),
                                status_code=303)
