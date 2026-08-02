"""Sponsored event screens — the participant's link, and your side of it.

Registered from main.py via ``register(app, deps)``, same as the commission
screens, so this module never imports main.

The shape of the thing: a sponsor pays for a class so it can be free, and what
they get back is a public post from each participant. Two screens face the
participant (both on one private link, no login) and two face you — a tracker
and a queue of Reels to spot-check.

Design decisions worth knowing before reading the code:

* **The token is the credential.** Nobody logs in. People read these on a phone
  between clients; an account they have to remember a password for is an
  account they won't use.
* **One URL for the whole journey.** The same link is the invitation, the
  confirmation, the reference card, the submission form and the reward. What it
  shows depends on where the event and the person have got to, which means
  there is exactly one thing to remember.
* **The code is issued the moment a Reel is submitted.** Holding it until a
  human has checked the tags costs you the moment, and the moment is the
  reward. Checking happens afterwards, in the queue.
* **Nothing here punishes anybody.** A lapsed slot is offered back. A missing
  tag keeps its reward. The only lever after confirmation is the reward itself.
"""

from __future__ import annotations

import csv
import io
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import qrcode

from fastapi import Depends, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from sqlalchemy.orm import Session

from .db import get_db
from .mailer import Mailer, looks_like_email
from .models import (
    EVENT_CLOSED, EVENT_DRAFT, EVENT_OPEN, EVENT_RUNNING, EVENT_STATUSES,
    HANDLE_MAX, RSVP_NO, RSVP_NONE, RSVP_YES,
    TAGS_MISSING, TAGS_OK, TAGS_PENDING, TAG_LABELS,
    Event, EventParticipant,
)

#: AWAKEN's palette, and nothing else. Emails are the one place a third
#: party's brand tends to creep in — a sponsor's red here, a warning amber
#: there — and the result stops looking like it came from us at all.
#:
#: The header is black rather than the app's navy: it is the colour of the
#: brandmark itself, it lets a sponsor's logo sit on it without fighting, and
#: it reads as the brand rather than as a piece of software.
BLACK = "#14171a"
BLACK_SOFT = "#9aa3ab"
TEAL = "#008080"
TEAL_TINT = "#e6f2f2"
INK = "#1a232e"
MUTED = "#6b7683"
LINE = "#e4e8ed"
PANEL = "#f3f5f7"

#: The sponsor's logo rides under this Content-ID when the event has one.
SPONSOR_CID = "sponsor-logo"

#: The logo rides inside the message under this Content-ID, so it renders
#: without the reader having to allow remote images.
LOGO_CID = "awaken-logo"
_LOGO_PATH = Path(__file__).with_name("static") / "email-logo.png"


def _logo_bytes() -> bytes:
    try:
        return _LOGO_PATH.read_bytes()
    except OSError:
        return b""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "event"


def clean_handle(raw: str) -> str:
    """Whatever they pasted, reduced to a bare Instagram handle.

    People paste profile URLs, they paste with the @, they paste with a
    trailing space off a phone keyboard. A handle stored three different ways
    is a handle you cannot match a Reel against later, which is the single
    biggest reason this kind of tracking falls apart.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if "instagram.com" in s.lower():
        s = urlparse(s if "//" in s else "//" + s).path
    s = s.strip("/").split("/")[0].split("?")[0]
    s = s.lstrip("@").strip()
    return s[:HANDLE_MAX]


#: What an Instagram Reel or post URL looks like once we have one.
_REEL_RE = re.compile(
    r"^https?://(www\.)?instagram\.com/(reel|reels|p|tv)/[A-Za-z0-9_-]+/?",
    re.I)


def clean_reel_url(raw: str) -> str:
    """Normalise a pasted Reel link, or return '' if it isn't one.

    Instagram's share sheet appends a tracking query; keeping it would mean the
    same Reel submitted twice looks like two different URLs.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if not s.lower().startswith("http"):
        s = "https://" + s.lstrip("/")
    s = s.split("?")[0].rstrip("/")
    if not _REEL_RE.match(s + "/"):
        return ""
    return s


def new_token() -> str:
    return secrets.token_urlsafe(24)


def _aware(dt):
    """Postgres gives these back tz-aware; SQLite and hand-built ones may not."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def left_until(when, now=None) -> str:
    """'2 days, 4 hours' — a soft countdown, never a scolding one."""
    when = _aware(when)
    if not when:
        return ""
    now = now or datetime.now(timezone.utc)
    secs = int((when - now).total_seconds())
    if secs <= 0:
        return ""
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return "%d day%s, %d hour%s" % (days, "" if days == 1 else "s",
                                        hours, "" if hours == 1 else "s")
    if hours:
        return "%dh %02dm" % (hours, mins)
    return "%d minute%s" % (mins, "" if mins == 1 else "s")


def confirm_deadline(p: EventParticipant, now=None):
    """When *this* person's slot stops being held.

    Counted from their own invitation, not from one date shared by the whole
    list. Somebody added on the Thursday would otherwise inherit a deadline
    that expired on the Tuesday and lose a slot nobody ever asked them about.

    Before the invitation goes out the clock is quoted from now, which is
    exactly where it will start the moment the email sends — so the email can
    print the deadline it is itself creating, and nobody who was never asked
    can lapse.

    `confirm_by` still wins if it is sooner: it is the point past which there
    is no longer time to re-fill the slot, and no per-person clock can be
    allowed to run past that. Set the hours to nothing and the fixed date is
    the whole answer — one deadline for the room, which is what you want when
    the class is close enough that everybody is being asked at once.
    """
    now = now or datetime.now(timezone.utc)
    own = _aware(p.confirm_due)
    if own:
        # Set when somebody is asked outside the normal run — off the waitlist,
        # after the event's own cut-off. Theirs wins outright; the event date
        # is about having time to re-fill, and filling is what just happened.
        return own
    hours = p.event.confirm_hours or 0
    hard = _aware(p.event.confirm_by)
    if not hours:
        # The rolling clock is switched off, so a fixed date is the whole
        # answer — and with neither set there is no deadline at all and
        # nobody can lapse.
        return hard
    start = _aware(p.invited_at) or now
    soft = start + timedelta(hours=hours)
    return min(soft, hard) if hard else soft


#: The pass rides inside the confirmation email under this Content-ID.
PASS_CID = "event-pass"


def qr_png(url: str) -> bytes:
    """A QR of a URL, as PNG bytes.

    It encodes the check-in address rather than the token on its own, so any
    phone's own camera opens it — nothing to install, and nothing to teach
    whoever is on the door.
    """
    qr = qrcode.QRCode(box_size=10, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color=BLACK, back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def base_url(request: Request) -> str:
    """The public origin, honouring the proxy Railway puts in front of us."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get(
        "host") or request.url.netloc
    return "%s://%s" % (proto, host)


def counts(event: Event) -> dict:
    """Every headline number on the tracker, from one pass over the list."""
    # The waitlist is not in the room. Counting them in "confirmed 4 of 30"
    # or in a send list would mean every number on this page answers a
    # different question from the one you asked it.
    ps = [p for p in event.participants if not p.waitlist]
    waiting_list = [p for p in event.participants if p.waitlist]
    live = [p for p in ps if not p.released_at]
    confirmed = [p for p in live if p.confirmed]
    posted = [p for p in confirmed if p.posted]
    return {
        "total": len(ps),
        "waitlist": len(waiting_list),
        "live": len(live),
        # Still in play: nobody has said no and nobody has run out of time.
        # This is the list you work from, so it is the one the tab counts.
        "inplay": len([p for p in live if not p.declined]),
        "gone": len([p for p in ps if p.declined or p.released_at]),
        "confirmed": len(confirmed),
        "declined": len([p for p in ps if p.declined]),
        "waiting": len([p for p in live if p.rsvp == RSVP_NONE]),
        "released": len([p for p in ps if p.released_at]),
        "handles": len([p for p in confirmed if p.handle]),
        "invited": len([p for p in live if p.invited_at]),
        "reel_emailed": len([p for p in confirmed if p.reel_email_at]),
        "arrived": len([p for p in confirmed if p.arrived_at]),
        "to_arrive": len([p for p in confirmed if not p.arrived_at]),
        "posted": len(posted),
        "to_post": len(confirmed) - len(posted),
        "to_check": len([p for p in posted if p.tags == TAGS_PENDING]),
        "missing": len([p for p in posted if p.tags == TAGS_MISSING]),
        "reshared": len([p for p in posted if p.reshared_at]),
        "qualified": len([p for p in ps if p.qualified]),
        "redeemed": len([p for p in ps if p.redeemed_at]),
        "seats_left": max(0, (event.capacity or 0) - len(confirmed)),
    }


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def register(app, deps):
    render = deps["render"]
    require = deps["require"]
    require_admin = deps["require_admin"]
    templates = deps["templates"]

    def guard(request, db):
        """Admins, or anyone granted the HYROX event area."""
        return require(request, db, perm="manage_hyrox")

    # ------------------------------------------------------------ public ----

    def _participant(db, token: str):
        return (db.query(EventParticipant)
                .filter(EventParticipant.token == token).first())

    def _stage(p: EventParticipant, now=None) -> str:
        """Which screen this person should see.

        One place decides, so the page and every POST that guards on it can
        never disagree about what someone is allowed to do right now.
        """
        now = now or datetime.now(timezone.utc)
        ev = p.event
        if p.waitlist:
            return "waiting"
        if p.released_at:
            return "released"
        if p.declined:
            return "declined"
        if p.rsvp == RSVP_NONE:
            by = confirm_deadline(p, now)
            return "lapsed" if (by and now > by) else "confirm"
        # Confirmed from here on.
        if p.posted:
            return "rewarded"
        ends = _aware(ev.ends_at)
        if not ends or now < ends:
            return "ready"          # confirmed, class hasn't happened yet
        deadline = _aware(ev.reel_deadline)
        if deadline and now > deadline:
            return "late"           # window shut, still no Reel
        return "submit"

    def _public_ctx(request, p, now=None):
        now = now or datetime.now(timezone.utc)
        ev = p.event
        return {
            "request": request, "p": p, "ev": ev, "stage": _stage(p, now),
            "confirm_left": left_until(confirm_deadline(p, now), now),
            "confirm_by": _fmt_when(confirm_deadline(p, now)),
            "reel_left": left_until(ev.reel_deadline, now),
            "reward": ev.reward(p.reward_key) if p.reward_key else None,
            "now": now,
        }

    @app.get("/e/{token}", response_class=HTMLResponse)
    def event_public(request: Request, token: str, db: Session = Depends(get_db)):
        """One participant's page. No login — the token is the credential."""
        p = _participant(db, token)
        if not p:
            return templates.TemplateResponse(
                "event_gone.html", {"request": request, "reason": "unknown"},
                status_code=404)
        now = datetime.now(timezone.utc)
        p.opens = (p.opens or 0) + 1
        p.last_opened_at = now
        db.commit()
        return templates.TemplateResponse("event_public.html",
                                          _public_ctx(request, p, now))

    @app.post("/e/{token}/confirm")
    def event_confirm(request: Request, token: str,
                      rsvp: str = Form(""), instagram: str = Form(""),
                      ack: str = Form(""), db: Session = Depends(get_db)):
        """Attendance, handle and the sponsor acknowledgement, in one post."""
        p = _participant(db, token)
        if not p:
            return RedirectResponse("/", status_code=303)
        back = f"/e/{token}"
        now = datetime.now(timezone.utc)
        # Only somebody who has not answered yet, or who is already coming and
        # is fixing their handle. A no is final from this side: by the time
        # they reread the page their slot has been offered to somebody else,
        # and letting them take it back would hand one place to two people.
        # Putting somebody back is a decision made on the tracker, where you
        # can see what is actually free.
        if _stage(p, now) not in ("confirm", "ready"):
            return RedirectResponse(back, status_code=303)

        if rsvp == RSVP_NO:
            # Declining needs nothing else. Making it easy to say no is how the
            # slot comes back in time to fill it.
            p.rsvp, p.rsvp_at = RSVP_NO, now
            p.acknowledged_at = None
            db.commit()
            return RedirectResponse(back, status_code=303)

        handle = clean_handle(instagram)
        if rsvp != RSVP_YES or not handle or ack != "on":
            return RedirectResponse(back + "?missing=1", status_code=303)
        p.rsvp, p.rsvp_at = RSVP_YES, now
        p.instagram = handle
        p.acknowledged_at = now
        db.commit()
        # Their pass, straight away, while they are still holding the phone.
        _send_pass(db, p.event, p, base_url(request))
        return RedirectResponse(back + "?done=1", status_code=303)

    @app.post("/e/{token}/reel")
    def event_reel(request: Request, token: str,
                   url: str = Form(""), reward: str = Form(""),
                   db: Session = Depends(get_db)):
        """Submit the Reel and take the reward, in one step.

        The code is minted here and shown on the next screen. No approval gate:
        for a room of people you know by name the risk of somebody gaming it is
        close to nil, and if it happens you deactivate one code. Making
        everybody wait costs the moment, which is the thing the reward is for.
        """
        p = _participant(db, token)
        if not p:
            return RedirectResponse("/", status_code=303)
        back = f"/e/{token}"
        now = datetime.now(timezone.utc)
        if _stage(p, now) not in ("submit", "late"):
            return RedirectResponse(back, status_code=303)

        clean = clean_reel_url(url)
        if not clean:
            return RedirectResponse(back + "?bad=1", status_code=303)
        picked = p.event.reward(reward) or (p.event.rewards[0]
                                            if p.event.rewards else None)
        p.reel_url, p.reel_at = clean, now
        p.tags = TAGS_PENDING
        if picked:
            p.reward_key = picked["key"]
            p.reward_code = p.reward_code or _mint_code(db, p.event)
            p.reward_at = now
        db.commit()
        return RedirectResponse(back + "?posted=1", status_code=303)

    @app.post("/e/{token}/reward")
    def event_swap_reward(request: Request, token: str, reward: str = Form(""),
                          db: Session = Depends(get_db)):
        """Change which reward a code is against, keeping the code itself.

        Somebody picks the PFT discount then realises they aren't racing. That
        is a thirty-second fix, not a support conversation.
        """
        p = _participant(db, token)
        if not p or not p.reward_code or p.redeemed_at:
            return RedirectResponse(f"/e/{token}", status_code=303)
        picked = p.event.reward(reward)
        if picked:
            p.reward_key = picked["key"]
            db.commit()
        return RedirectResponse(f"/e/{token}", status_code=303)

    @app.get("/e/{token}/qr.png")
    def event_qr(token: str, request: Request, db: Session = Depends(get_db)):
        """Their pass, as a picture.

        It encodes the check-in URL rather than the token on its own, so any
        phone's own camera opens it — no scanner app to install and nothing to
        teach whoever is on the door. The page it opens needs a login, so a
        photographed QR is worth nothing to a participant.
        """
        p = _participant(db, token)
        if not p:
            return Response(status_code=404)
        png = qr_png("%s/i/%s" % (base_url(request), token))
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=300"})

    @app.get("/i/{token}", response_class=HTMLResponse)
    def event_checkin(request: Request, token: str, db: Session = Depends(get_db)):
        """Where a scanned QR lands — and the scan itself checks them in.

        No button. A queue at a door is the one place an extra tap is
        expensive, and whoever is holding the phone made the decision when
        they pointed it at somebody's screen. What comes back is the receipt:
        their name, big, and a way to undo it.

        Short path because it is what the QR has to carry, and a shorter
        payload is a QR with bigger squares — which is the difference between
        scanning first time in a gym doorway and not.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = _participant(db, token)
        if not p:
            return templates.TemplateResponse(
                "event_gone.html", {"request": request, "reason": "unknown"},
                status_code=404)
        # `who`, not `p`: base.html binds `p` to the request path, and a
        # participant landing in that name renders a blank screen at a door.
        # Already in means already in. Re-stamping would quietly rewrite the
        # arrival time each time a screen got scanned twice in a queue.
        fresh = not p.arrived_at
        if fresh:
            p.arrived_at = datetime.now(timezone.utc)
            db.commit()
        return render(request, "event_checkin.html", db, staff, active="events",
                      ev=p.event, who=p, stage=_stage(p), c=counts(p.event),
                      fresh=fresh)

    @app.post("/i/{token}")
    def event_checkin_mark(request: Request, token: str,
                           undo: str = Form(""), db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = _participant(db, token)
        if not p:
            return RedirectResponse("/events", status_code=303)
        # Undo is deliberately as easy as marking: the cost of a wrong scan at
        # a door is somebody standing there while you find a way to reverse it.
        p.arrived_at = None if undo else datetime.now(timezone.utc)
        db.commit()
        # Back to the door list, never to /i/ — landing there again would scan
        # them straight back in.
        return RedirectResponse("/events/%d?tab=door" % p.event_id,
                                status_code=303)

    @app.get("/events/{eid}/scan", response_class=HTMLResponse)
    def event_scanner(request: Request, eid: int, db: Session = Depends(get_db)):
        """The camera, inside the app.

        A phone's own camera app works too — the QR carries a URL for exactly
        that reason — but it is one person, one scan, one app switch. This
        keeps the camera open and the count on screen, which is what you want
        with a queue in front of you.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        return render(request, "event_scan.html", db, staff, active="events",
                      ev=ev, c=counts(ev))

    @app.post("/events/{eid}/scan")
    def event_scan_hit(request: Request, eid: int, code: str = Form(""),
                       db: Session = Depends(get_db)):
        """One scanned code. Answers in JSON so the camera never has to stop.

        Takes whatever the QR contained — we encode a full URL, so the token
        is the last path segment — and is deliberately forgiving about it, on
        the grounds that a door is a bad place to debug a string.
        """
        staff, redir = guard(request, db)
        if redir:
            return JSONResponse({"ok": False, "why": "signed out"},
                                status_code=401)
        ev = db.get(Event, eid)
        if not ev:
            return JSONResponse({"ok": False, "why": "no such event"},
                                status_code=404)
        token = (code or "").strip().rstrip("/").split("/")[-1].split("?")[0]
        p = _participant(db, token) if token else None
        if not p or p.event_id != eid:
            return JSONResponse({
                "ok": False,
                "why": "That code isn't for this class."
                       if p else "We don't recognise that code.",
                "counts": {"in": counts(ev)["arrived"],
                           "of": counts(ev)["confirmed"]},
            })
        fresh = not p.arrived_at
        if fresh:
            p.arrived_at = datetime.now(timezone.utc)
            db.commit()
        c = counts(ev)
        stage = _stage(p)
        # One short line under the name. Whoever is scanning reads it at arm's
        # length with somebody waiting, so it says what it means and stops.
        if not fresh:
            note = "Already scanned in at %s" % _aware(p.arrived_at).strftime(
                "%I:%M %p").lstrip("0")
        elif stage in ("ready", "submit", "late", "rewarded"):
            note = p.handle or "On the list"
        elif stage == "confirm":
            note = "Never confirmed — counted as here"
        elif p.waitlist:
            note = "Was on the waitlist"
        elif p.declined:
            note = "Had said they couldn't make it"
        else:
            note = "Their slot had lapsed"
        return JSONResponse({
            "ok": True, "fresh": fresh, "name": p.name, "note": note,
            "token": p.token,
            "counts": {"in": c["arrived"], "of": c["confirmed"]},
        })

    @app.post("/events/{eid}/people/{pid}/arrive")
    def event_arrive(request: Request, eid: int, pid: int,
                     db: Session = Depends(get_db)):
        """The same mark, from the tracker — for the phone that died on the way."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != eid:
            return RedirectResponse(f"/events/{eid}", status_code=303)
        p.arrived_at = None if p.arrived_at else datetime.now(timezone.utc)
        db.commit()
        return RedirectResponse(f"/events/{eid}?tab=door", status_code=303)

    def _mint_code(db, event: Event) -> str:
        """A short, per-person, single-use code — KR-4471.

        Sequential rather than random so the person on the door can read one
        off a phone screen without misreading a character, and so you can tell
        the sponsor how many were redeemed out of how many issued.
        """
        prefix = (event.code_prefix or "EV").upper()
        used = {p.reward_code for p in event.participants if p.reward_code}
        n = 4471
        while "%s-%d" % (prefix, n) in used:
            n += 1
        return "%s-%d" % (prefix, n)

    # ------------------------------------------------------------- admin ----

    @app.get("/events", response_class=HTMLResponse)
    def events_index(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        rows = db.query(Event).order_by(Event.id.desc()).all()
        return render(request, "events.html", db, staff, active="events",
                      events=[{"ev": e, "c": counts(e)} for e in rows],
                      statuses=EVENT_STATUSES)

    @app.post("/events/new")
    def event_new(request: Request, name: str = Form(...),
                  sponsor: str = Form(""), db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        slug = slugify(name)
        n, base = 2, slug
        while db.query(Event).filter_by(slug=slug).first():
            slug, n = "%s-%d" % (base, n), n + 1
        ev = Event(name=name.strip(), slug=slug, sponsor=sponsor.strip(),
                   created_by_id=getattr(staff, "id", None),
                   code_prefix=(sponsor.strip()[:2].upper() or "EV"))
        db.add(ev)
        db.commit()
        return RedirectResponse(f"/events/{ev.id}/settings", status_code=303)

    @app.get("/events/{eid}", response_class=HTMLResponse)
    def event_detail(request: Request, eid: int, tab: str = "people",
                     db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        _release_lapsed(db, ev)
        everyone = sorted(ev.participants, key=lambda p: (p.name or "").lower())
        waiting = [p for p in everyone if p.waitlist]
        rest = [p for p in everyone if not p.waitlist]
        # Somebody who said no, or who never answered in time, is not somebody
        # you are still chasing. Leaving them in the main table means every
        # count you read off it is a count of a list you no longer have.
        gone = [p for p in rest if p.declined or p.released_at]
        people = [p for p in rest if not (p.declined or p.released_at)]
        return render(request, "event_detail.html", db, staff, active="events",
                      ev=ev, tab=tab, people=people, waiting=waiting,
                      gone=gone,
                      upload=request.session.pop("event_upload", None),
                      c=counts(ev),
                      lists={k: len(v) for k, v in mail_lists(ev).items()},
                      reel_left=left_until(ev.reel_deadline),
                      base=base_url(request), tag_labels=TAG_LABELS,
                      statuses=EVENT_STATUSES,
                      mail=request.session.pop("event_mail", None),
                      mail_ready=Mailer().cfg.configured)

    def _release_lapsed(db, ev: Event) -> int:
        """Mark slots whose confirmation window has passed.

        Done on read rather than on a schedule — this app has no background
        worker, and a number that only updates when somebody is looking at it
        is exactly as correct as one that updates constantly.
        """
        now = datetime.now(timezone.utc)
        n = 0
        for p in ev.participants:
            if p.rsvp != RSVP_NONE or p.released_at or p.waitlist:
                continue
            # Each person's own clock. Somebody we have not written to yet has
            # a deadline quoted from now, so they can never lapse unasked.
            by = confirm_deadline(p, now)
            if by and now > by:
                p.released_at = now
                n += 1
        if n:
            db.commit()
        return n

    @app.get("/events/{eid}/settings", response_class=HTMLResponse)
    def event_settings(request: Request, eid: int, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        return render(request, "event_settings.html", db, staff, active="events",
                      ev=ev, statuses=EVENT_STATUSES)

    @app.post("/events/{eid}/settings")
    def event_settings_save(
            request: Request, eid: int,
            name: str = Form(...), sponsor: str = Form(""),
            status: str = Form(EVENT_DRAFT),
            starts_at: str = Form(""), venue: str = Form(""),
            capacity: str = Form("30"), bring: str = Form(""), perk: str = Form(""),
            handles: str = Form(""), hashtag: str = Form(""),
            reel_hours: str = Form("48"), confirm_hours: str = Form("48"),
            confirm_by: str = Form(""),
            code_prefix: str = Form("EV"),
            reward_a: str = Form(""), reward_a_detail: str = Form(""),
            reward_a_value: str = Form(""),
            reward_b: str = Form(""), reward_b_detail: str = Form(""),
            reward_b_value: str = Form(""),
            sponsor_logo: UploadFile = None, drop_logo: str = Form(""),
            db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)

        def dt(text):
            """A datetime-local value, read as UTC.

            The gym runs in one timezone and the server clock is UTC; storing
            what was typed keeps the countdown and the tracker agreeing with
            each other, which is what actually matters here.
            """
            text = (text or "").strip()
            if not text:
                return None
            try:
                return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            except ValueError:
                return None

        def num(text, default):
            try:
                return max(0, int(str(text).strip()))
            except (TypeError, ValueError):
                return default

        ev.name = name.strip() or ev.name
        ev.sponsor = sponsor.strip()
        ev.status = status if status in dict(EVENT_STATUSES) else ev.status
        ev.starts_at = dt(starts_at)
        ev.venue = venue.strip()
        ev.capacity = num(capacity, 30)
        ev.bring, ev.perk = bring.strip(), perk.strip()
        ev.handles, ev.hashtag = handles.strip(), hashtag.strip()
        ev.reel_hours = num(reel_hours, 48) or 48
        # Zero is a real answer here — it switches the rolling clock off and
        # hands the whole job to the fixed date below.
        ev.confirm_hours = num(confirm_hours, 48)
        ev.confirm_by = dt(confirm_by)
        ev.code_prefix = (code_prefix.strip()[:4].upper() or "EV")
        ev.reward_a, ev.reward_a_detail = reward_a.strip(), reward_a_detail.strip()
        ev.reward_a_value = reward_a_value.strip()
        ev.reward_b, ev.reward_b_detail = reward_b.strip(), reward_b_detail.strip()
        ev.reward_b_value = reward_b_value.strip()

        if drop_logo == "on":
            ev.sponsor_logo, ev.sponsor_logo_mime = None, None
        elif sponsor_logo is not None and getattr(sponsor_logo, "filename", ""):
            raw = sponsor_logo.file.read()
            # A logo bigger than this is a print asset somebody grabbed by
            # mistake, and mail servers reject large messages outright.
            if raw and len(raw) <= 2 * 1024 * 1024:
                ev.sponsor_logo = raw
                ev.sponsor_logo_mime = (sponsor_logo.content_type or "image/png")
        db.commit()
        return RedirectResponse(f"/events/{eid}/settings?saved=1", status_code=303)

    @app.post("/events/{eid}/people")
    def event_add_people(request: Request, eid: int, bulk: str = Form(""),
                         db: Session = Depends(get_db)):
        """Paste a list — one per line, 'Name, email' or just a name.

        Thirty people arrive as a list from a sign-up sheet or a chat thread.
        Typing them into thirty separate forms is the kind of task that makes
        somebody abandon a tool on the first day.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        seen = {(p.email or "").lower() for p in ev.participants if p.email}
        added = 0
        for line in (bulk or "").splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            parts = [x.strip() for x in re.split(r"[,\t;]", line) if x.strip()]
            name = parts[0]
            email = next((x for x in parts[1:] if looks_like_email(x)), "")
            if not name or (email and email.lower() in seen):
                continue
            if email:
                seen.add(email.lower())
            db.add(EventParticipant(event_id=ev.id, name=name[:120],
                                    email=email, token=new_token()))
            added += 1
        db.commit()
        return RedirectResponse(f"/events/{eid}?added={added}", status_code=303)

    #: What a column in an uploaded file might be called. People export from a
    #: sign-up sheet, a Google Form or a phone's contacts, and none of them
    #: agree on a name — so match on meaning rather than making you rename
    #: headers before you can upload.
    _CSV_FIELDS = {
        "name": ("name", "full name", "fullname", "participant", "member"),
        "email": ("email", "e-mail", "email address", "mail"),
        "instagram": ("instagram", "ig", "handle", "instagram handle", "@"),
        "waitlist": ("waitlist", "wait list", "waiting list", "list", "status",
                     "type"),
    }
    #: A waitlist cell can say any of these. Anything else means "in the room".
    _WAIT_WORDS = {"y", "yes", "true", "1", "w", "wait", "waitlist",
                   "waiting", "waiting list", "reserve", "standby"}

    def _csv_rows(raw: bytes):
        """(rows, error) — the uploaded file as dicts keyed by our field names."""
        for encoding in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        else:
            return [], "that file isn't readable as text"
        try:
            dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        try:
            header = next(reader)
        except StopIteration:
            return [], "that file is empty"
        # Map each column we recognise onto one of our field names.
        cols = {}
        for i, cell in enumerate(header):
            key = (cell or "").strip().lower().lstrip("@")
            for field, names in _CSV_FIELDS.items():
                if key in names and field not in cols:
                    cols[field] = i
        if "name" not in cols:
            return [], ("no <b>name</b> column — the first row has to be the "
                        "headings, and one of them has to say “name”")
        out = []
        for row in reader:
            def cell(field):
                i = cols.get(field)
                return (row[i].strip() if i is not None and i < len(row) else "")
            if not cell("name"):
                continue
            out.append({"name": cell("name"), "email": cell("email"),
                        "instagram": cell("instagram"),
                        "waitlist": cell("waitlist").lower() in _WAIT_WORDS})
        return out, ""

    @app.post("/events/{eid}/people/upload")
    def event_upload_people(request: Request, eid: int,
                            file: UploadFile = None,
                            db: Session = Depends(get_db)):
        """Load the whole list from one file, waitlist included.

        The waitlist has to arrive at the same moment as everybody else. Its
        entire purpose is that the replacement is already on file at the point
        somebody drops out — a waitlist you have to go and find is a waitlist
        that costs you the slot.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        back = f"/events/{eid}"
        if file is None or not (file.filename or ""):
            return RedirectResponse(back + "?up=nofile", status_code=303)
        raw = file.file.read(4 * 1024 * 1024)
        rows, err = _csv_rows(raw)
        if err:
            request.session["event_upload"] = {"error": err}
            return RedirectResponse(back + "?up=bad", status_code=303)
        seen = {(p.email or "").lower() for p in ev.participants if p.email}
        added = waiting = dupes = 0
        for r in rows:
            addr = r["email"]
            if addr and not looks_like_email(addr):
                addr = ""
            if addr and addr.lower() in seen:
                dupes += 1
                continue
            if addr:
                seen.add(addr.lower())
            db.add(EventParticipant(
                event_id=ev.id, name=r["name"][:120], email=addr,
                instagram=clean_handle(r["instagram"]),
                waitlist=r["waitlist"], token=new_token()))
            added += 1
            if r["waitlist"]:
                waiting += 1
        db.commit()
        request.session["event_upload"] = {
            "added": added, "waiting": waiting, "dupes": dupes}
        return RedirectResponse(back + "?up=ok", status_code=303)

    @app.post("/events/{eid}/people/{pid}/undecline")
    def event_undecline(request: Request, eid: int, pid: int,
                        db: Session = Depends(get_db)):
        """Put somebody back in the room after a no.

        People tap the wrong button, and plans change back. Without this the
        only repair is to delete them and start again, which throws away their
        link and their history over a mis-tap — and leaves you with a slot the
        waitlist has already been offered.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        p = db.get(EventParticipant, pid)
        if not ev or not p or p.event_id != eid:
            return RedirectResponse(f"/events/{eid}", status_code=303)
        p.rsvp = RSVP_NONE
        p.rsvp_at = None
        p.released_at = None
        # A fresh window, because whatever deadline applied has almost
        # certainly passed by the time somebody is being put back.
        p.confirm_due = (datetime.now(timezone.utc)
                         + timedelta(hours=ev.confirm_hours or 24))
        db.commit()
        return RedirectResponse(f"/events/{eid}?tab=gone&back={pid}",
                                status_code=303)

    @app.post("/events/{eid}/people/{pid}/promote")
    def event_promote(request: Request, eid: int, pid: int,
                      db: Session = Depends(get_db)):
        """Give a freed slot to somebody on the waitlist, and tell them.

        Promoting and inviting are one action on purpose. A slot that has been
        given to somebody who has not been told about it is a slot nobody is
        going to fill, and the gap between the two steps is exactly where a
        replacement gets forgotten.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        p = db.get(EventParticipant, pid)
        if not ev or not p or p.event_id != eid:
            return RedirectResponse(f"/events/{eid}", status_code=303)
        if p.waitlist:
            p.waitlist = False
            # A day to answer, from now. Not the event's date — that may
            # already be behind us, and a slot somebody cannot accept is not
            # a slot you have filled.
            p.confirm_due = (datetime.now(timezone.utc)
                             + timedelta(hours=ev.confirm_hours or 24))
            p.released_at = None
            db.commit()
        if looks_like_email(p.email or ""):
            request.session["event_mail"] = _send(
                db, ev, [p], "invite", base_url(request))
        else:
            request.session["event_mail"] = {
                "sent": [], "failed": [], "kind": "invite",
                "skipped": [{"name": p.name, "detail": "no email on record — "
                             "copy their link from the table instead"}]}
        return RedirectResponse(f"/events/{eid}?promoted={pid}", status_code=303)

    @app.post("/events/{eid}/people/{pid}/edit")
    def event_edit_person(request: Request, eid: int, pid: int,
                          name: str = Form(""), email: str = Form(""),
                          instagram: str = Form(""), db: Session = Depends(get_db)):
        """Fix somebody's details.

        Addresses arrive mistyped off a sign-up sheet, people change their
        handle, someone's name comes through as a nickname. Without this the
        only repair is to delete them and start again — which throws away their
        link, their confirmation and their Reel along with the typo.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != eid:
            return RedirectResponse(f"/events/{eid}", status_code=303)
        back = f"/events/{eid}"
        clean_name = name.strip()[:120]
        if not clean_name:
            # A nameless row is a row nobody can identify on the day.
            return RedirectResponse(back + "?edit=noname", status_code=303)
        addr = email.strip()
        if addr and not looks_like_email(addr):
            return RedirectResponse(back + "?edit=bademail", status_code=303)
        p.name = clean_name
        p.email = addr
        # Same cleaning as the participant's own page, so a handle typed here
        # and a handle typed there end up stored identically.
        p.instagram = clean_handle(instagram)
        db.commit()
        return RedirectResponse(back + "?edit=ok", status_code=303)

    @app.post("/events/{eid}/people/{pid}/remove")
    def event_remove_person(request: Request, eid: int, pid: int,
                            db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if p and p.event_id == eid and not p.posted:
            db.delete(p)
            db.commit()
        return RedirectResponse(f"/events/{eid}", status_code=303)

    @app.post("/events/{eid}/people/{pid}/tags")
    def event_set_tags(request: Request, eid: int, pid: int,
                       state: str = Form(TAGS_OK), note: str = Form(""),
                       db: Session = Depends(get_db)):
        """Record what you saw when you watched the Reel.

        Marking a Reel as missing a tag never touches the reward. It flags a
        friendly message, nothing more.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if p and p.event_id == eid and state in (TAGS_PENDING, TAGS_OK, TAGS_MISSING):
            p.tags = state
            p.tags_note = note.strip()[:200] or None
            db.commit()
        return RedirectResponse(f"/events/{eid}?tab=reels", status_code=303)

    @app.post("/events/{eid}/people/{pid}/flag")
    def event_flag(request: Request, eid: int, pid: int, what: str = Form(""),
                   db: Session = Depends(get_db)):
        """Toggle re-shared or redeemed — the two things only you can know."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        now = datetime.now(timezone.utc)
        if p and p.event_id == eid:
            if what == "reshared":
                p.reshared_at = None if p.reshared_at else now
            elif what == "redeemed":
                p.redeemed_at = None if p.redeemed_at else now
            db.commit()
        tab = "reels" if what in ("reshared", "redeemed") else "people"
        return RedirectResponse(f"/events/{eid}?tab={tab}", status_code=303)

    # -------------------------------------------------------------- mail ----

    def _send(db, ev, people, kind, base):
        """Send one kind of email to a list. Returns a receipt for the banner.

        Best-effort per person: one bad address must not stop the other
        twenty-nine going out.
        """
        mailer = Mailer()
        if not mailer.cfg.configured:
            return {"setup": "Mail isn't set up on the server yet — "
                             "add SMTP details under Settings."}
        sent, failed, skipped = [], [], []
        # Both marks travel inside the message. Most mail clients block remote
        # images until the reader asks for them, and a sponsor logo nobody sees
        # is the one thing the sponsor will notice.
        inline = {}
        logo = _logo_bytes()
        if logo:
            inline[LOGO_CID] = logo
        if ev.sponsor_logo:
            inline[SPONSOR_CID] = ev.sponsor_logo
        for p in people:
            if not looks_like_email(p.email or ""):
                skipped.append({"name": p.name, "detail": "no email on record"})
                continue
            url = "%s/e/%s" % (base, p.token)
            subject, text, html = (_reel_mail(ev, p, url) if kind == "reel"
                                   else _invite_mail(ev, p, url))
            ok, detail = mailer.send(p.email, subject, text, html=html,
                                     inline=inline or None)
            if ok:
                now = datetime.now(timezone.utc)
                if kind == "invite":
                    p.invited_at = now
                else:
                    p.reel_email_at = now
                sent.append({"name": p.name, "detail": p.email})
            else:
                failed.append({"name": p.name, "detail": detail})
        db.commit()
        return {"sent": sent, "failed": failed, "skipped": skipped, "kind": kind}

    def mail_lists(ev) -> dict:
        """Who each email would go to, right now.

        Every send button is labelled with one of these counts, so you can see
        what a button will do before you press it rather than after.
        """
        # Waitlisted people are deliberately unreachable: an invitation to
        # somebody who has not been given a slot is a promise we cannot keep.
        live = [p for p in ev.participants
                if not p.released_at and not p.waitlist]
        # Somebody with no address on record can never be emailed, so counting
        # them in "still to send" leaves a button that always offers to send to
        # one more person and never can. They get their own number instead.
        mailable = [p for p in live if looks_like_email(p.email or "")]
        confirmed = [p for p in mailable if p.confirmed]
        # Somebody who has told you they can't come is not somebody to invite
        # again. "Re-send to all" reaching them is the exact message that makes
        # a person stop reading anything you send.
        askable = [p for p in mailable if not p.declined]
        return {
            # The invitation: everyone who hasn't had one.
            "invite": [p for p in askable if not p.invited_at],
            "invite_all": askable,
            # The Reel email: everyone who came and hasn't been asked yet.
            "reel": [p for p in confirmed if not p.reel_email_at],
            # The nudge: everyone who was asked and still hasn't posted. A
            # separate list because sending the first ask again to somebody who
            # already posted is the fastest way to sour this.
            "nudge": [p for p in confirmed if p.reel_email_at and not p.posted],
            "reel_all": confirmed,
            # Not a send list — a to-do for you. Their link still works; it
            # just has to reach them some other way.
            "no_email": [p for p in live if not looks_like_email(p.email or "")],
        }

    @app.post("/events/{eid}/send")
    def event_send(request: Request, eid: int, kind: str = Form("invite"),
                   who: str = Form(""), pick: list[int] = Form(default=[]),
                   db: Session = Depends(get_db)):
        """Send one of the two emails.

        They are two separate sends on purpose. The invitation asks somebody to
        confirm a slot; the Reel email asks them to post and collect a code.
        They go out days apart, to different people, and folding them into one
        button that guesses from the event's status hides the second one — you
        cannot press a button you cannot see.

        `who` picks the audience within a kind, and every default is the
        smallest sensible list: nobody wants to email thirty people twice by
        accident.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        lists = mail_lists(ev)
        if who == "selected":
            # You ticked these names, so they are the list — no filtering by
            # who has already had it or who has confirmed. Hand-picking is the
            # escape hatch for every case the tidy lists don't cover, and
            # second-guessing it would defeat the point.
            chosen = set(pick)
            pool = [p for p in ev.participants if p.id in chosen]
            request.session["event_mail"] = _send(
                db, ev, pool, "invite" if kind != "reel" else "reel",
                base_url(request))
            return RedirectResponse(f"/events/{eid}", status_code=303)
        if kind == "reel":
            pool = lists["nudge"] if who == "nudge" else (
                lists["reel_all"] if who == "all" else lists["reel"])
        else:
            pool = lists["invite_all"] if who == "all" else lists["invite"]
        request.session["event_mail"] = _send(
            db, ev, pool, "invite" if kind != "reel" else "reel",
            base_url(request))
        return RedirectResponse(f"/events/{eid}", status_code=303)

    def _invite_mail(ev, p, url):
        """The 'you're in, confirm your slot' email."""
        when = _fmt_when(ev.starts_at)
        # Their own deadline: a fixed date if you set one, otherwise the clock
        # that starts the moment this email sends.
        by = _fmt_when(confirm_deadline(p))
        first = (p.name or "").split()[0] if p.name else "there"
        subject = "You're in — confirm your %s slot" % ev.name
        text = "\n".join([
            "You got a slot, %s." % first,
            "",
            "%s%s" % (ev.name, " · %s" % when if when else ""),
            "%s" % (ev.venue or ""),
            "",
            "%s is fuelling us after the class. All we ask in return is one "
            "Reel." % (ev.sponsor or "Our sponsor"),
            "",
            "Confirm your slot here: %s" % url,
            "",
            ("Let us know by %s — we're holding your slot until then, after "
             "that it goes to the next person." % by) if by else "",
        ])
        html = _shell(ev, """
          <h1 style="font-size:20px;font-weight:650;margin:0 0 14px">You got a slot, %s 🎉</h1>
          %s
          <p style="font-size:15px;margin:0 0 16px;color:#2b3642">%s is fuelling us after
             the class. All we ask in return is one Reel.</p>
          %s
          %s
          %s
        """ % (
            _esc(first),
            _facts(ev),
            _esc(ev.sponsor or "Our sponsor"),
            _button(url, "Confirm my slot →", "Takes under a minute"),
            _window_note(
                "<b>Let us know by %s.</b> We're holding your slot until then, "
                "after that it goes to the next person." % _esc(by)) if by else "",
            _rewards_block(ev, "What you get for sharing"),
        ))
        return subject, text, html

    def _pass_mail(ev, p, url):
        """"You're in — here's your pass." Sent the moment they confirm.

        The QR rides inside the message rather than as a link, because a
        doorway is exactly where somebody has no signal and no patience, and
        an email already sitting in their inbox opens without either.
        """
        when = _fmt_when(ev.starts_at)
        first = (p.name or "").split()[0] if p.name else "there"
        subject = "You're in — your pass for %s" % ev.name
        text = "\n".join([
            "You're confirmed, %s." % first,
            "",
            "%s%s" % (ev.name, " · %s" % when if when else ""),
            "%s" % (ev.venue or ""),
            "",
            "Show the code in this email at the door — we'll scan it. If you "
            "can't find it, your page has the same code: %s" % url,
        ])
        html = _shell(ev, """
          <h1 style="font-size:20px;font-weight:650;margin:0 0 14px">You're in, %s ✅</h1>
          <p style="font-size:15px;margin:0 0 18px;color:#2b3642">Show this at the door —
             we'll scan it. No need to print anything.</p>
          <table width="100%%" cellpadding="0" cellspacing="0"><tr><td align="center"
            style="border:1px solid #e4e8ed;border-radius:12px;padding:18px 18px 14px">
            <img src="cid:%s" alt="Your check-in code" width="200"
                 style="display:block;width:200px;height:200px">
            <div style="font-size:15px;font-weight:650;margin-top:10px">%s</div>
            <div style="font-size:12px;color:#6b7683">%s</div>
          </td></tr></table>
          %s
          %s
        """ % (
            _esc(first), PASS_CID, _esc(p.name), _esc(ev.name),
            _facts(ev),
            _window_note("Can't find this on the day? Your own page carries the same "
                         "code — <a href=\"%s\" style=\"color:#008080\">open it here</a>."
                         % _esc(url)),
        ))
        return subject, text, html

    def _send_pass(db, ev, p, base):
        """Best effort, and never in the way of a confirmation.

        Somebody confirming their slot must not see an error because our mail
        server had a bad minute — the confirmation is the thing that matters
        and it is already saved by the time we get here.
        """
        if p.pass_email_at or not looks_like_email(p.email or ""):
            return
        mailer = Mailer()
        if not mailer.cfg.configured:
            return
        url = "%s/e/%s" % (base, p.token)
        subject, text, html = _pass_mail(ev, p, url)
        inline = {PASS_CID: qr_png("%s/i/%s" % (base, p.token))}
        logo = _logo_bytes()
        if logo:
            inline[LOGO_CID] = logo
        if ev.sponsor_logo:
            inline[SPONSOR_CID] = ev.sponsor_logo
        try:
            ok, _ = mailer.send(p.email, subject, text, html=html, inline=inline)
        except Exception:
            ok = False
        if ok:
            p.pass_email_at = datetime.now(timezone.utc)
            db.commit()

    def _reel_mail(ev, p, url):
        """The 'thank you, here's your reward link' email."""
        first = (p.name or "").split()[0] if p.name else "there"
        by = _fmt_when(ev.reel_deadline)
        subject = "Thank you — here's your reward link"
        text = "\n".join([
            "Thank you, %s." % first,
            "",
            "Thanks for turning up, and to %s for fuelling us after."
            % (ev.sponsor or "Our sponsor"),
            "",
            "Submit your Reel and pick your reward: %s" % url,
            "",
            ("Your window is open until %s." % by) if by else "",
            ("Tag %s and use %s." % (" and ".join(ev.handle_list), ev.hashtag)
             if ev.handle_list else ""),
        ])
        html = _shell(ev, """
          <h1 style="font-size:20px;font-weight:650;margin:0 0 4px">Thank you, %s 🙌</h1>
          <p style="color:#6b7683;font-size:14px;margin:0 0 16px">%s</p>
          <p style="font-size:15px;margin:0 0 16px;color:#2b3642">You turned up and worked —
             that's the whole reason we run these. And thanks to %s for fuelling us
             after.</p>
          %s
          %s
          %s
        """ % (
            _esc(first),
            _esc(ev.name),
            _esc(ev.sponsor or "Our sponsor"),
            _button(url, "Submit my Reel &amp; get my code →", "Takes about 20 seconds"),
            _window_note(
                "<b>Your window is open until %s.</b> Tag %s%s and you're done."
                % (_esc(by), _esc(" and ".join(ev.handle_list)),
                   (", use <b>%s</b>" % _esc(ev.hashtag)) if ev.hashtag else "")
            ) if by else "",
            _rewards_block(ev, "Your reward"),
        ))
        return subject, text, html

    # ------------------------------------------------------------ export ----

    @app.get("/events/{eid}/sponsor-logo")
    def event_sponsor_logo(eid: int, db: Session = Depends(get_db)):
        """The sponsor's mark, for the participant's page.

        Public on purpose: it sits on a page that has no login, and it is a
        logo the sponsor publishes everywhere anyway.
        """
        ev = db.get(Event, eid)
        if not ev or not ev.sponsor_logo:
            return Response(status_code=404)
        return Response(content=ev.sponsor_logo,
                        media_type=ev.sponsor_logo_mime or "image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/events/{eid}/report.csv")
    def event_report(request: Request, eid: int, db: Session = Depends(get_db)):
        """The sheet the sponsor asks for. One row per participant.

        This report is what wins the next sponsorship, so it should cost a
        click rather than an evening of copying links out of a chat.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Name", "Instagram", "Confirmed", "Reel URL", "Posted at",
                    "Tags", "Reward", "Code", "Redeemed"])
        for p in sorted(ev.participants, key=lambda x: (x.name or "").lower()):
            r = ev.reward(p.reward_key) if p.reward_key else None
            w.writerow([
                p.name, p.handle,
                _fmt_when(p.rsvp_at) if p.confirmed else
                ("Declined" if p.declined else ""),
                p.reel_url or "", _fmt_when(p.reel_at) if p.reel_at else "",
                TAG_LABELS.get(p.tags, "") if p.posted else "",
                (r["name"] if r else ""), p.reward_code or "",
                _fmt_when(p.redeemed_at) if p.redeemed_at else "",
            ])
        name = "%s-sponsor-report.csv" % ev.slug
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition":
                                 'attachment; filename="%s"' % name})


# --------------------------------------------------------------------------
# email building blocks
# --------------------------------------------------------------------------

def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _fmt_when(dt) -> str:
    dt = _aware(dt)
    return dt.strftime("%a %d %b, %I:%M %p").replace(" 0", " ") if dt else ""


def _facts(ev) -> str:
    rows = [("When", _fmt_when(ev.starts_at)), ("Where", ev.venue),
            ("Bring", ev.bring), ("After", ev.perk)]
    cells = "".join(
        '<tr><td style="color:#6b7683;font-size:14px;padding:4px 12px 4px 0;'
        'width:76px">%s</td><td style="font-size:14px;font-weight:600">%s</td></tr>'
        % (_esc(k), _esc(v)) for k, v in rows if v)
    if not cells:
        return ""
    return ('<table style="background:#f3f5f7;border-radius:8px;padding:15px 17px;'
            'width:100%%;margin:0 0 20px"><tbody>%s</tbody></table>' % cells)


def _button(url, label, sub="") -> str:
    return (
        '<table width="100%%" style="margin:6px 0 4px"><tr><td align="center">'
        '<a href="%s" style="display:inline-block;background:#008080;color:#fff;'
        'text-decoration:none;font-weight:650;font-size:15px;padding:14px 32px;'
        'border-radius:8px">%s</a>'
        '%s</td></tr></table>'
        % (url, label,
           '<div style="font-size:12px;color:#6b7683;margin-top:9px">%s</div>'
           % _esc(sub) if sub else ""))


def _window_note(html) -> str:
    """A deadline, stated calmly.

    It used to be amber, which reads as a warning — the wrong note entirely for
    "we're holding your slot". Neutral panel with a navy rule says the same
    thing in AWAKEN's own colours, and stops the email carrying a palette the
    rest of the brand never uses.
    """
    return ('<table width="100%%" cellpadding="0" cellspacing="0" '
            'style="margin:20px 0 0"><tr>'
            '<td width="3" style="background:%s;border-radius:2px 0 0 2px"></td>'
            '<td style="background:%s;padding:13px 16px;font-size:13px;color:#3d4753;'
            'border-radius:0 8px 8px 0">%s</td></tr></table>'
            % (BLACK, PANEL, html))


def _rewards_block(ev, title) -> str:
    """The reward, or the choice of rewards.

    A tick against each line reads as a list of things you are getting. There
    is only ever one — so with more than one on offer they are drawn as
    alternatives with an "or" between them, and the caption says so outright.
    Somebody who turns up expecting both has been told the wrong thing by us,
    and that is a worse start than offering nothing.
    """
    rs = ev.rewards
    if not rs:
        return ""
    one = len(rs) == 1

    def card(r):
        # "A discount code for X", not "20% off X". The number is not settled
        # until the sponsor signs it off, and a figure printed in an email is
        # a figure we are held to — the offer is the code, not the percentage.
        return ('<table width="100%%" cellpadding="0" cellspacing="0"><tr>'
                '<td style="border:1px solid #e4e8ed;border-radius:8px;'
                'padding:11px 14px;font-size:14px;color:#2b3642">'
                '%sA discount code for <b>%s</b>%s</td></tr></table>'
                % ('<span style="color:#008080;font-weight:700">✓</span> ' if one else "",
                   _esc(r["name"]),
                   _esc(" · %s" % r["detail"] if r["detail"] else "")))

    divider = ('<div style="font-size:11px;font-weight:700;letter-spacing:1.4px;'
               'text-transform:uppercase;color:#9aa3ab;text-align:center;'
               'margin:8px 0">or</div>')
    items = divider.join(card(r) for r in rs)
    caption = "" if one else (
        '<div style="font-size:12px;color:#6b7683;margin:0 0 10px">'
        'Pick one when you send your Reel.</div>')
    return ('<div style="border-top:1px solid #e4e8ed;margin-top:24px;padding-top:20px">'
            '<div style="font-size:11px;font-weight:700;letter-spacing:1.4px;'
            'text-transform:uppercase;color:#6b7683;margin-bottom:10px">%s</div>'
            '%s%s</div>'
            % (_esc(title), caption, items))


def _shell(ev, body) -> str:
    """The AWAKEN wrapper every event email sits inside.

    Navy, teal and neutrals — nothing else. The sponsor used to get their own
    coloured bar under the header, which meant every email carried a third
    brand's palette and stopped looking like it came from AWAKEN. Naming them
    inside the header says the same thing and reads as a partnership rather
    than an advert.
    """
    if not ev.sponsor:
        sponsor_bar = ""
    else:
        # Their logo if we have one, their name if we don't — either way the
        # sponsor is named in the header, where the reader will actually see
        # it, rather than in a coloured bar that belongs to somebody else.
        mark = ('<img src="cid:%s" alt="%s" width="150" style="display:block;'
                'margin:9px auto 0;width:150px;height:auto">'
                % (SPONSOR_CID, _esc(ev.sponsor))) if ev.sponsor_logo else (
            '<div style="color:#ffffff;font-size:13px;font-weight:650;'
            'letter-spacing:.4px;margin-top:3px">%s</div>' % _esc(ev.sponsor))
        sponsor_bar = (
            '<div style="border-top:1px solid #2b3138;margin:18px auto 0;width:70%%"></div>'
            '<div style="color:%s;font-size:10px;font-weight:600;letter-spacing:2.2px;'
            'text-transform:uppercase;margin-top:14px">in partnership with</div>%s'
            % (BLACK_SOFT, mark))
    return """<!DOCTYPE html><html><body style="margin:0;background:#f3f5f7;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  color:#1a232e;line-height:1.5">
<table width="100%%" cellpadding="0" cellspacing="0" style="background:#f3f5f7;padding:22px 12px">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;
  border-radius:10px;overflow:hidden">
  <tr><td style="background:#14171a;padding:26px 30px 24px;text-align:center">
    <img src="cid:%s" alt="AWAKEN" width="142" style="display:block;margin:0 auto;
      width:142px;height:auto">
    %s
  </td></tr>
  <tr><td style="padding:28px 30px 30px">%s</td></tr>
  <tr><td style="background:#f3f5f7;padding:18px 30px;text-align:center;font-size:12px;
    color:#6b7683">Questions? Just reply to this email.<br>AWAKEN Fitness Center</td></tr>
</table>
</td></tr></table></body></html>""" % (LOGO_CID, sponsor_bar, body)
