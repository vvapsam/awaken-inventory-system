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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .db import get_db
from .mailer import Mailer, looks_like_email
from .models import (
    EVENT_CLOSED, EVENT_DRAFT, EVENT_OPEN, EVENT_RUNNING, EVENT_STATUSES,
    HANDLE_MAX, RSVP_NO, RSVP_NONE, RSVP_YES,
    TAGS_MISSING, TAGS_OK, TAGS_PENDING, TAG_LABELS,
    Event, EventParticipant,
)

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


def base_url(request: Request) -> str:
    """The public origin, honouring the proxy Railway puts in front of us."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get(
        "host") or request.url.netloc
    return "%s://%s" % (proto, host)


def counts(event: Event) -> dict:
    """Every headline number on the tracker, from one pass over the list."""
    ps = event.participants
    live = [p for p in ps if not p.released_at]
    confirmed = [p for p in live if p.confirmed]
    posted = [p for p in confirmed if p.posted]
    return {
        "total": len(ps),
        "live": len(live),
        "confirmed": len(confirmed),
        "declined": len([p for p in ps if p.declined]),
        "waiting": len([p for p in live if p.rsvp == RSVP_NONE]),
        "released": len([p for p in ps if p.released_at]),
        "handles": len([p for p in confirmed if p.handle]),
        "invited": len([p for p in live if p.invited_at]),
        "reel_emailed": len([p for p in confirmed if p.reel_email_at]),
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
        if p.released_at:
            return "released"
        if p.declined:
            return "declined"
        if p.rsvp == RSVP_NONE:
            by = _aware(ev.confirm_by)
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
            "confirm_left": left_until(ev.confirm_by, now),
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
        if _stage(p, now) not in ("confirm", "declined", "ready"):
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

    @app.post("/e/{token}/reopen")
    def event_reopen(request: Request, token: str, db: Session = Depends(get_db)):
        """"Still want in?" on a lapsed slot — flags it for your team.

        Not an automatic re-grant: the seat may genuinely be gone. It puts them
        back in front of you rather than leaving them at a dead end.
        """
        p = _participant(db, token)
        if p and _stage(p) in ("lapsed", "released"):
            p.released_at = None
            p.tags_note = "Asked to be let back in"
            db.commit()
        return RedirectResponse(f"/e/{token}", status_code=303)

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
        people = sorted(ev.participants, key=lambda p: (p.name or "").lower())
        return render(request, "event_detail.html", db, staff, active="events",
                      ev=ev, tab=tab, people=people, c=counts(ev),
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
        by = _aware(ev.confirm_by)
        if not by or datetime.now(timezone.utc) <= by:
            return 0
        n = 0
        for p in ev.participants:
            if p.rsvp == RSVP_NONE and not p.released_at:
                p.released_at = datetime.now(timezone.utc)
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
            reel_hours: str = Form("48"), confirm_by: str = Form(""),
            code_prefix: str = Form("EV"),
            reward_a: str = Form(""), reward_a_detail: str = Form(""),
            reward_a_value: str = Form(""),
            reward_b: str = Form(""), reward_b_detail: str = Form(""),
            reward_b_value: str = Form(""),
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
        ev.confirm_by = dt(confirm_by)
        ev.code_prefix = (code_prefix.strip()[:4].upper() or "EV")
        ev.reward_a, ev.reward_a_detail = reward_a.strip(), reward_a_detail.strip()
        ev.reward_a_value = reward_a_value.strip()
        ev.reward_b, ev.reward_b_detail = reward_b.strip(), reward_b_detail.strip()
        ev.reward_b_value = reward_b_value.strip()
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
        logo = _logo_bytes()
        for p in people:
            if not looks_like_email(p.email or ""):
                skipped.append({"name": p.name, "detail": "no email on record"})
                continue
            url = "%s/e/%s" % (base, p.token)
            subject, text, html = (_reel_mail(ev, p, url) if kind == "reel"
                                   else _invite_mail(ev, p, url))
            ok, detail = mailer.send(p.email, subject, text, html=html,
                                     inline={LOGO_CID: logo} if logo else None)
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
        live = [p for p in ev.participants if not p.released_at]
        # Somebody with no address on record can never be emailed, so counting
        # them in "still to send" leaves a button that always offers to send to
        # one more person and never can. They get their own number instead.
        mailable = [p for p in live if looks_like_email(p.email or "")]
        confirmed = [p for p in mailable if p.confirmed]
        return {
            # The invitation: everyone who hasn't had one.
            "invite": [p for p in mailable if not p.invited_at],
            "invite_all": mailable,
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
                   who: str = Form(""), db: Session = Depends(get_db)):
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
        by = _fmt_when(ev.confirm_by)
        first = (p.name or "").split()[0] if p.name else "there"
        subject = "You're in — confirm your %s slot" % ev.name
        text = "\n".join([
            "You got a slot, %s." % first,
            "",
            "%s%s" % (ev.name, " · %s" % when if when else ""),
            "%s" % (ev.venue or ""),
            "",
            "%s is covering this one so it can be free for the community. The "
            "way we say thank you is simple — everyone shares one Reel "
            "afterwards." % (ev.sponsor or "Our sponsor"),
            "",
            "Confirm your slot here: %s" % url,
            "",
            ("We're holding your slot until %s. If we haven't heard from you "
             "by then we'll pass it to someone on the waitlist. No hard "
             "feelings either way, just let us know." % by) if by else "",
        ])
        html = _shell(ev, """
          <h1 style="font-size:20px;font-weight:650;margin:0 0 4px">You got a slot, %s 🎉</h1>
          <p style="color:#6b7683;font-size:14px;margin:0 0 18px">Here's everything,
             and one thing we need back from you.</p>
          %s
          <p style="font-size:15px;margin:0 0 16px;color:#2b3642">%s is covering this one so it
             can be free for the community. The way we say thank you is simple — everyone
             shares one Reel afterwards.</p>
          %s
          %s
          %s
        """ % (
            _esc(first),
            _facts(ev),
            _esc(ev.sponsor or "Our sponsor"),
            _button(url, "Confirm my slot →", "Takes under a minute"),
            _window_note(
                "<b>Heads up on timing.</b> We're holding your slot until <b>%s</b>. "
                "If we haven't heard from you by then we'll pass it to someone on the "
                "waitlist. No hard feelings either way, just let us know." % _esc(by)
            ) if by else "",
            _rewards_block(ev, "What you get for sharing"),
        ))
        return subject, text, html

    def _reel_mail(ev, p, url):
        """The 'thank you, here's your reward link' email."""
        first = (p.name or "").split()[0] if p.name else "there"
        by = _fmt_when(ev.reel_deadline)
        subject = "Thank you — here's your reward link"
        text = "\n".join([
            "Thank you, %s." % first,
            "",
            "Thanks for turning up. %s backed this one so it could be free for "
            "the community." % (ev.sponsor or "Our sponsor"),
            "",
            "Submit your Reel and pick your reward: %s" % url,
            "",
            ("Your window is open until %s." % by) if by else "",
            ("Tag %s and use %s." % (" and ".join(ev.handle_list), ev.hashtag)
             if ev.handle_list else ""),
        ])
        html = _shell(ev, """
          <h1 style="font-size:20px;font-weight:650;margin:0 0 4px">Thank you, %s 🙌</h1>
          <p style="color:#6b7683;font-size:14px;margin:0 0 18px">%s</p>
          <p style="font-size:15px;margin:0 0 16px;color:#2b3642">You turned up and worked.
             That's the whole reason we run these. %s backed it so it could be free for the
             community.</p>
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
            _rewards_block(ev, "Choose one when you submit"),
        ))
        return subject, text, html

    # ------------------------------------------------------------ export ----

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
    return ('<div style="background:#fbf4e6;border:1px solid #eddfbe;border-radius:8px;'
            'padding:13px 16px;margin:20px 0 0;font-size:13px;color:#6a5518">%s</div>'
            % html)


def _rewards_block(ev, title) -> str:
    rs = ev.rewards
    if not rs:
        return ""
    items = "".join(
        '<div style="font-size:14px;color:#2b3642;margin-bottom:8px">'
        '<span style="color:#008080;font-weight:700">✓</span> '
        '<b>%s</b> — %s%s</div>'
        % (_esc(r["value"]), _esc(r["name"]),
           " · %s" % _esc(r["detail"]) if r["detail"] else "")
        for r in rs)
    return ('<div style="border-top:1px solid #e4e8ed;margin-top:24px;padding-top:20px">'
            '<div style="font-size:11px;font-weight:700;letter-spacing:1.4px;'
            'text-transform:uppercase;color:#6b7683;margin-bottom:10px">%s</div>%s</div>'
            % (_esc(title), items))


def _shell(ev, body) -> str:
    """The AWAKEN wrapper every event email sits inside."""
    sponsor_bar = (
        '<div style="background:#b3121b;color:#fff;text-align:center;padding:7px 20px;'
        'font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase">'
        'Powered by %s</div>' % _esc(ev.sponsor)) if ev.sponsor else ""
    return """<!DOCTYPE html><html><body style="margin:0;background:#f3f5f7;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  color:#1a232e;line-height:1.5">
<table width="100%%" cellpadding="0" cellspacing="0" style="background:#f3f5f7;padding:22px 12px">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;
  border-radius:10px;overflow:hidden">
  <tr><td style="background:#03224e;padding:26px 30px 22px;text-align:center">
    <img src="cid:%s" alt="AWAKEN" width="142" style="display:block;margin:0 auto;
      width:142px;height:auto">
  </td></tr>
  %s
  <tr><td style="padding:28px 30px 30px">%s</td></tr>
  <tr><td style="background:#f3f5f7;padding:18px 30px;text-align:center;font-size:12px;
    color:#6b7683">Questions? Just reply to this email.<br>AWAKEN Fitness Center</td></tr>
</table>
</td></tr></table></body></html>""" % (LOGO_CID, sponsor_bar, body)
