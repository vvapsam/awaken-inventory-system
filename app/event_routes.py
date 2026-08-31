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
import hashlib
import hmac
import io
import math
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse, urlsplit, parse_qsl, urlencode

import qrcode

from fastapi import Depends, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

from .auth import current_staff, hash_pin, verify_pin
from .db import get_db
from .mailer import Mailer, looks_like_email
from . import mail_templates
from .models import (
    EVENT_CLOSED, EVENT_DRAFT, EVENT_INVITE, EVENT_MODES, EVENT_OPEN,
    EVENT_RUNNING, EVENT_STATUSES,
    HANDLE_MAX, PAY_APPROVED, PAY_DRAFT, PAY_GRACE_HOURS, PAY_LABELS,
    PAY_RETURNED,
    PAY_SUBMITTED, RSVP_NO, RSVP_NONE, RSVP_YES, SEXES,
    TAGS_MISSING, TAGS_OK, TAGS_PENDING, TAG_LABELS,
    Event, EventParticipant, EventOrganiserLink, EventRate, EventStation,
    PaymentSetting, StationRun,
    HeatPlan, HeatSlot,
    ORGANISER_LINK_DAYS, ORGANISER_DEFAULT_PASS,
    from_local, to_local,
    can_door,
    RACE_STATUSES, RACE_STATUS_LABELS, RACE_STATUS_MANUAL,
    HEAT_OPEN_MINS, HEAT_OPEN_MIN, HEAT_OPEN_MAX,
    station_splits, has_race, race_status, is_test_athlete,
    board_rows, RACE_STATUS_OUT, h12, Staff, wants_reels,
    race_totals, station_field, rank_in, station_shorts,
    parse_clock, CLOCK_MAX,
    CATEGORIES, CATEGORY_LABELS, CATEGORY_DEFAULT, category_key,
)
from .countries import country_code, country_name, flag as country_flag
# The sponsor strip is cut and sized once, for the finisher card. The
# board shows the same marks at the same relative weights rather than
# keeping a second copy that can drift from it.
from .card_routes import SPONSORS
from . import form_routes
# One definition of a clock, borrowed rather than copied. patch_routes owns it
# because that is where a time is first read out loud to somebody.
from .patch_routes import mmss

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
BANNER_CID = "event-banner"

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


def _clock(raw: str) -> str:
    """A start time as somebody would say it: "10:00 AM".

    Accepts what a phone's time input sends ("10:00") and what a person types
    ("10am", "10:00 AM"), because this field is filled in on a phone at least
    as often as on a laptop.
    """
    text = (raw or "").strip().upper().replace(".", "")
    if not text:
        return ""
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?$", text)
    if not m:
        return text[:20]
    hour, minute, half = int(m.group(1)), m.group(2) or "00", m.group(3)
    if half:
        if hour == 12:
            hour = 0 if half == "AM" else 12
        elif half == "PM":
            hour += 12
    if not 0 <= hour <= 23:
        return text[:20]
    suffix = "AM" if hour < 12 else "PM"
    shown = hour % 12 or 12
    return "%d:%s %s" % (shown, minute, suffix)


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
    hard = _aware(p.event.confirm_by)
    if own:
        # Set when somebody is asked outside the normal run — off the waitlist,
        # or put back after saying no. They get their own window so they are
        # not handed a slot that expired before they were offered it.
        #
        # But the announced date is still a ceiling while it is ahead of us.
        # One date told to the whole room has to mean the same thing for
        # everybody in it, or the answer to "when do I need to hear back?" is
        # different depending on how somebody got their slot — and only one of
        # those answers is the one printed in the email.
        #
        # Once that date has passed it can no longer govern: somebody being
        # asked for the first time after it cannot be held to a deadline that
        # was already behind them, which is the case this branch exists for.
        if hard and hard > now:
            return min(own, hard)
        return own
    hours = p.event.confirm_hours or 0
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


#: How hard the browser has to work before a registration is accepted. Sixteen
#: bits is about half a second on a phone and nothing to a laptop — the point is
#: not to stop one person, it is to make ten thousand attempts cost real money.
POW_BITS = 16
#: How long a minted puzzle stays good for.
POW_TTL = 60 * 30
#: Nothing fills a form this fast except a script.
MIN_FILL_SECONDS = 3


def pow_challenge(secret: str, salt: str) -> str:
    """A puzzle tied to this event and this minute, signed so it can't be forged."""
    return hmac.new(secret.encode(), salt.encode(), hashlib.sha256).hexdigest()[:24]


def pow_salt(now=None) -> str:
    """A fresh salt that carries the minute it was minted, in the clear.

    The time has to be readable by us and unforgeable by them, which is why it
    lives in the salt rather than in a form field: the salt is what the
    challenge is signed over, so changing the timestamp changes the puzzle and
    the answer they already found stops working.
    """
    stamp = int((now or datetime.now(timezone.utc)).timestamp())
    return "%d.%s" % (stamp, secrets.token_urlsafe(9))


def pow_age(salt: str, now=None):
    """How many seconds ago that salt was minted, or None if it isn't ours."""
    head = (salt or "").split(".", 1)[0]
    try:
        minted = int(head)
    except ValueError:
        return None
    return (now or datetime.now(timezone.utc)).timestamp() - minted


def pow_question(secret: str, salt: str):
    """The plain sum shown to a browser that can't run the puzzle.

    Derived from the same signed salt rather than made up by the page, because
    a question the client invents is a question the client can answer for
    itself — and then the whole gate is one POST away from being skipped.
    """
    digest = hmac.new(secret.encode(), ("q:" + salt).encode(),
                      hashlib.sha256).digest()
    return digest[0] % 9 + 2, digest[1] % 9 + 2


def pow_ok(challenge: str, nonce: str, bits: int = POW_BITS) -> bool:
    """Did they find a nonce whose hash starts with enough zero bits?"""
    if not challenge or not nonce or len(str(nonce)) > 32:
        return False
    digest = hashlib.sha256(("%s:%s" % (challenge, nonce)).encode()).digest()
    need, seen = bits, 0
    for byte in digest:
        for shift in range(7, -1, -1):
            if (byte >> shift) & 1:
                return False
            seen += 1
            if seen >= need:
                return True
    return True


def money(v) -> str:
    """₱1,500 — no trailing zeroes nobody reads."""
    if v is None:
        return ""
    v = Decimal(v)
    return "₱{:,.0f}".format(v) if v == v.to_integral() else "₱{:,.2f}".format(v)


#: The one address every public link is built from — participant pages, coach
#: statements, delegator links, QR codes, the lot.
#:
#: Pinned rather than read off the request because a link is a thing somebody
#: keeps. Deriving it from whichever host an admin happened to be logged into
#: meant the same invitation went out as pay.awakengym.com one week and
#: portal.awakengym.com the next, and a printed QR outlives the habit that
#: produced it. Override with PUBLIC_BASE_URL if the address ever moves.
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL")
                   or "https://portal.awakengym.com").strip().rstrip("/")


def base_url(request: Request) -> str:
    """The public origin. One address, whatever host you are reading this on.

    Falls back to the request's own host only if PUBLIC_BASE_URL is explicitly
    blanked — useful on a laptop, where localhost is the only origin that
    works, and never the case in production.
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get(
        "host") or request.url.netloc
    return "%s://%s" % (proto, host)


#: How long a day of heats may be. Not a limit anybody should ever reach —
#: it is there so a fat-fingered "every 1 minute" from 6am to 10pm cannot ask
#: the page to draw a thousand rows.
HEAT_MAX = 200


def hhmm(raw) -> str:
    """Any way somebody writes a time, as 24-hour "HH:MM", or "".

    Accepts "10:00 AM", "10am", "1400", "2:05 pm". Returns "" for anything it
    cannot read rather than guessing — a heat at a time nobody meant is worse
    than a heat that refuses to be created.
    """
    t = (raw or "").strip().lower().replace(".", "")
    if not t:
        return ""
    pm = t.endswith("pm")
    am = t.endswith("am")
    if pm or am:
        t = t[:-2].strip()
    if ":" in t:
        parts = t.split(":", 1)
    elif t.isdigit() and len(t) == 4:
        parts = [t[:2], t[2:]]
    else:
        parts = [t, "0"]
    try:
        h, m = int(parts[0]), int(parts[1] or 0)
    except ValueError:
        return ""
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= m < 60):
        return ""
    return "%02d:%02d" % (h, m)


def clock12(t: str) -> str:
    """"14:20" as "2:20 PM" — how everybody at the gym says it."""
    t = hhmm(t)
    if not t:
        return ""
    h, m = int(t[:2]), int(t[3:])
    return "%d:%02d %s" % (((h + 11) % 12) + 1, m, "AM" if h < 12 else "PM")


#: Where "How was your race?" sends people. Business Profile -> Ask for
#: reviews gives you this address; it opens Google's review composer directly,
#: and on a phone it hands off to the Maps app.
#:
#: A constant with an environment override rather than a column: it is one
#: address for the whole gym, not a fact about any one event, and putting it in
#: the database would mean a settings field to maintain for a value that
#: changes roughly never. Setting GOOGLE_REVIEW_URL on the server beats it
#: without a deploy if that day ever comes.
#: Failed find-my-result attempts, by address. In memory on purpose: it is a
#: speed bump, not an audit, and a restart forgiving somebody is the right
#: failure. One gym, one morning - a table here never grows.
_ME_FAILS: dict = {}

REVIEW_URL = os.environ.get(
    "GOOGLE_REVIEW_URL", "https://g.page/r/CaH2Yg5cIoVBEBM/review")


def short_name(p) -> str:
    """"Chrizel Urbino" as "Chrizel U." — enough to find yourself, and not a
    contact list.

    The initial comes off the first word of the family name, so "De la Cruz"
    reads "D." rather than something that looks like a typo. Somebody with one
    name keeps it whole: an initial-less "Madonna" is right, and "Madonna ."
    is not.
    """
    given, family = (p.given or "").strip(), (p.family or "").strip()
    if not given:
        return (p.name or "").strip() or "—"
    first = family.split(" ")[0] if family else ""
    return "%s %s." % (given, first[0].upper()) if first else given


def shift(t: str, minutes: int) -> str:
    """A time this many minutes earlier, clamped to the same day."""
    t = hhmm(t)
    if not t:
        return ""
    v = int(t[:2]) * 60 + int(t[3:]) - minutes
    v = max(0, min(v, 24 * 60 - 1))
    return "%02d:%02d" % (v // 60, v % 60)


def heat_times(ev) -> list:
    """Every heat on this event, in order, as "HH:MM".

    Empty when the event has no first heat set — which is how an event opts
    out, and how every event that existed before heats did keeps behaving
    exactly as it always has.
    """
    first, last = hhmm(ev.heat_first), hhmm(ev.heat_last)
    every = max(1, ev.heat_every or 10)
    if not first:
        return []
    start = int(first[:2]) * 60 + int(first[3:])
    end = (int(last[:2]) * 60 + int(last[3:])) if last else start
    if end < start:
        end = start
    out = []
    v = start
    while v <= end and len(out) < HEAT_MAX:
        out.append("%02d:%02d" % (v // 60, v % 60))
        v += every
    return out


def arrive_at(ev, t: str) -> str:
    """When somebody in this heat has to be in the building."""
    return shift(t, ev.heat_arrive or 0)


def gap_text(minutes: int) -> str:
    """"30 minutes", "1 hour", "1 hour 15 minutes"."""
    m = max(0, int(minutes or 0))
    if m < 60:
        return "%d minute%s" % (m, "" if m == 1 else "s")
    h, r = divmod(m, 60)
    out = "%d hour%s" % (h, "" if h == 1 else "s")
    return out if not r else "%s %d minute%s" % (out, r, "" if r == 1 else "s")


#: The optional columns on the participant tables: key, heading, whether the
#: column only makes sense on an open-registration event, and which tabs may
#: show it — with its default on each.
#:
#: Everything here reads a field the system already collects. This is about
#: what is *shown*, not about inventing new data to store.
#:
#: A tab a key does not name cannot show that column and is not offered it.
#: "Confirmed" is the clearest case: on Can't make it, the What-happened column
#: already says whether somebody declined or ran out of time, so offering a
#: second column to say it again is offering a way to make the table worse.
#:
#: The name, the row actions and each tab's own subject column are not in this
#: list on purpose. A table you can switch the names off in is not a table.
#:
#: `open_only` is what stops the chooser lying. Sex, mobile, entry and payment
#: are only ever filled in by somebody registering themselves; offering them on
#: an invite-only event would tick a box that produces a column of dashes.
EVENT_COLUMNS = [
    # key          label           open_only  default, per tab
    ("payment",   "Payment",       True,  {"people": True,  "gone": False}),
    ("emails",    "Emails",        False, {"people": True,  "gone": False}),
    ("instagram", "Instagram",     False, {"people": True,  "gone": True}),
    ("confirmed", "Confirmed",     False, {"people": True}),
    ("ack",       "Acknowledged",  False, {"people": True}),
    ("reel",      "Reel",          False, {"people": True}),
    ("reward",    "Reward",        False, {"people": True}),
    ("added",     "Added",         False, {"people": True,  "gone": False}),
    ("updated",   "Last update",   False, {"people": True,  "gone": False}),
    ("link",      "Link",          False, {"people": True,  "gone": False}),
    ("gender",    "Gender",        True,  {"people": False, "gone": False}),
    # Not open_only: the category is about who somebody is racing, which is
    # a question an invite event has too. On by default on the roster,
    # because the only way anybody gets out of Open is somebody reading
    # down the list and moving them.
    ("category",  "Category",      False, {"people": True,  "gone": False}),
    ("age",       "Age",           False, {"people": True,  "gone": False}),
    ("reviewed",  "Review",        False, {"people": True,  "gone": False}),
    ("mobile",    "Mobile",        True,  {"people": False, "gone": False}),
    ("entry",     "Entry",         True,  {"people": False, "gone": False}),
    ("slot",      "Slot",          False, {"people": False}),
    ("heat",      "Heat",          False, {"people": False, "gone": False}),
    ("status",    "Status",        False, {"people": True,  "gone": False}),
]

#: Which column of the event row each tab's choice is kept in. Separate
#: columns rather than one shared list, because the two tabs are answering
#: different questions: the participant list is "who is coming and what do
#: they still owe me", and Can't make it is "who dropped out, and what were
#: they holding". One saved set would mean tuning one tab quietly wrecks the
#: other.
COLUMN_SCOPES = {"people": "cols", "gone": "gone_cols"}


def columns_for(event: Event, scope: str = "people") -> list:
    """The columns this tab could show, in table order.

    Returns (key, label, on) so the chooser and the table read the same list
    and neither can drift from the other.
    """
    raw = getattr(event, COLUMN_SCOPES.get(scope, "cols"), None)
    chosen = None if raw is None else {x for x in raw.split(",") if x}
    out = []
    for key, label, open_only, defaults in EVENT_COLUMNS:
        if scope not in defaults:
            continue
        if open_only and event.mode != "open":
            continue
        out.append((key, label,
                    defaults[scope] if chosen is None else key in chosen))
    return out


def visible_cols(event: Event, scope: str = "people") -> set:
    """Just the keys that are on — what the table actually tests against."""
    return {k for k, _label, on in columns_for(event, scope) if on}


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
        # Raced and done, which is what the awarding table works from.
        "finished": len([p for p in ps if p.finished_at]),
        "confirmed": len(confirmed),
        "declined": len([p for p in ps if p.declined]),
        "waiting": len([p for p in live if p.rsvp == RSVP_NONE]),
        "released": len([p for p in ps if p.released_at]),
        "handles": len([p for p in confirmed if p.handle]),
        "invited": len([p for p in live if p.invited_at]),
        "reel_emailed": len([p for p in confirmed if p.reel_email_at]),
        "to_review": len([p for p in ps if p.pay_status == PAY_SUBMITTED]),
        "unfinished": len([p for p in ps if p.pay_status == PAY_DRAFT]),
        "to_nudge": len([p for p in ps
                         if p.pay_status == PAY_DRAFT and not p.nudged_at]),
        "nudged": len([p for p in ps if p.nudged_at]),
        "returned": len([p for p in ps if p.pay_status == PAY_RETURNED]),
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

def _assign_slot(db: Session, ev: Event, p: EventParticipant) -> None:
    """Give this arrival its number and its start time.

    Module level rather than nested in register() so the mobile door
    screen can call the same function: somebody checked in off a list has
    to get the same slot as somebody scanned, or the two ways in quietly
    disagree on the day.

    Order of arrival, not order of registration: the first fifteen through
    the door get the early wave. A place held for somebody who never turns
    up is a place wasted, which is the whole reason this happens at the
    scanner rather than the night before.

    Written once. If they already have a time, they keep it — telling
    somebody ten o'clock and then moving them is worse than having no
    system at all.
    """
    if not ev.slot_a_time or p.slot_no:
        return
    # Lock the event row so two phones on the door cannot hand out the
    # same number. Cheap here — one row, held for the length of a scan.
    db.query(Event).filter(Event.id == ev.id).with_for_update().first()
    taken = (db.query(func.max(EventParticipant.slot_no))
             .filter(EventParticipant.event_id == ev.id).scalar()) or 0
    n = taken + 1
    cap = ev.slot_a_cap or 0
    p.slot_no = n
    # No second time set means one wave with no ceiling: everybody gets the
    # first time rather than nobody getting anything after the cap.
    p.slot_time = (ev.slot_a_time if (not cap or n <= cap or not ev.slot_b_time)
                   else ev.slot_b_time)
    p.slot_at = datetime.now(timezone.utc)


def register(app, deps):
    render = deps["render"]
    require = deps["require"]
    require_admin = deps["require_admin"]
    templates = deps["templates"]

    def guard(request, db):
        """Admins, or anyone granted the HYROX event area."""
        return require(request, db, perm="manage_hyrox")

    def door_guard(request, db):
        """Anyone allowed to work a door: the narrow permission, or the area.

        Standing at the door is a different job from running the event, so the
        two screens somebody on the door actually touches - the scanner and
        the arrive toggle - ask only for `event_door`. Everything else in this
        module still asks for the full area.
        """
        staff = current_staff(request, db)
        if staff and can_door(staff):
            return staff, None
        return require(request, db, perm="event_door")

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
        # Before the clock, not after it. A class called off on the morning
        # still has its end time in the diary, so reading the hold below that
        # line meant the people who most needed telling — the ones tapping the
        # link right now, hours before the class was due to end — were handed a
        # pass to a class that isn't happening, and the hold only bit later.
        # Anyone already holding a Reel link lands here too, which is the one
        # place that can answer the question they arrived with.
        # An event that asks for nothing has one confirmed state and stays in
        # it: they have a slot, here is the pass. No window, no form, no
        # deadline that quietly turns the page into an ask.
        if not wants_reels(ev):
            return "ready"
        if ev.reels_paused:
            return "paused"
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

    # ------------------------------------------------- open registration ----

    def _rate(r):
        return {"key": r.key, "label": r.label, "price": r.amount,
                "money": money(r.amount), "closed": bool(r.closed)}

    def _tiers(ev, db=None):
        """The rates somebody new may pick. Empty if none are set."""
        if db is not None:
            form_routes.ensure_rates(db, ev)
        return [_rate(r) for r in ev.rates_open() if (r.label or "").strip()]

    def _tier(ev, key):
        """The rate somebody picked, closed or not.

        Closed ones are found on purpose. An early-bird price that is no longer
        offered is still what that person picked, and every screen that says
        what they owe has to be able to name it.
        """
        r = ev.rate(key)
        return _rate(r) if r else None

    def _pickable(ev, key):
        """The rate somebody may pick right now, or nothing."""
        t = _tier(ev, key)
        return None if (t is None or t["closed"]) else t

    def _signup_ctx(request, ev, p=None, db=None, **kw):
        ctx = {"request": request, "ev": ev, "p": p, "tiers": _tiers(ev, db),
               # Formatted here, with the same function the email uses. The
               # page and the email are quoting one deadline; spelling it two
               # ways is how somebody ends up believing there are two.
               "pay_due_text": _fmt_when(p.pay_due_at) if p and p.pay_due_at
               else "",
               "sexes": SEXES, "money": money, "shut": ev.signups_shut(),
               # Full is not the same as closed, and saying "closed" to
               # somebody who could have had the next cancellation is a
               # different, worse sentence.
               "full": _full(ev), "capacity": ev.capacity or 0,
               "closes_left": left_until(ev.signup_closes),
               "pow_bits": POW_BITS, "step": kw.pop("step", 1),
               # Every field on the form, in the order the builder puts them,
               # and the same list cut into pages at each section. The page's
               # own fields are in there too - they are rows like any other
               # now, which is what lets the rate sit on page one.
               #
               # `was` is what they typed last time round, so a form that
               # comes back with one box missing does not throw away the
               # other five.
               "pages": form_routes.pages(form_routes.plan(ev)),
               "pagecount": len(form_routes.pages(form_routes.plan(ev))),
               "was": kw.pop("was", {})}
        ctx.update(kw)
        return ctx

    def _mint_pow(request, ev):
        """A fresh puzzle, signed with the session secret so it can't be made up."""
        salt = pow_salt()
        secret = request.app.state.pow_secret
        key = "%s:%s" % (ev.id, salt)
        return salt, pow_challenge(secret, key), pow_question(secret, key)

    def _signup_guard(request, ev, form) -> str:
        """Everything that has to be true before we write a row. '' means fine.

        None of this is visible to a person. The honeypot is a field no human
        sees, the clock catches anything that filled the form faster than a
        person can read it, and the puzzle costs a script real time per attempt.
        """
        if (form.get("website") or "").strip():
            return "bot"
        salt, nonce = form.get("salt") or "", form.get("nonce") or ""
        # The clock lives in the salt, which we signed, so neither the floor nor
        # the expiry can be edited by whoever is posting.
        age = pow_age(salt)
        if age is None:
            return "pow"
        if age < MIN_FILL_SECONDS:
            return "fast"
        if age > POW_TTL:
            return "stale"
        secret = request.app.state.pow_secret
        key = "%s:%s" % (ev.id, salt)
        if not pow_ok(pow_challenge(secret, key), nonce):
            # The plain question is the way through for a browser that could not
            # run the puzzle at all.
            a, b = pow_question(secret, key)
            try:
                if int(form.get("qanswer") or -1) == a + b:
                    return ""
            except ValueError:
                pass
            return "pow"
        return ""

    @app.get("/r/{slug}", response_class=HTMLResponse)
    def signup_page(request: Request, slug: str, db: Session = Depends(get_db)):
        """The public front door of an open event."""
        ev = db.query(Event).filter(Event.slug == slug).first()
        if not ev or ev.mode != EVENT_OPEN:
            return templates.TemplateResponse(
                "event_gone.html", {"request": request, "reason": "unknown"},
                status_code=404)
        # Somebody who started before comes straight back to where they were.
        tok = request.cookies.get("reg_%d" % ev.id)
        if tok:
            p = _participant(db, tok)
            if p and p.event_id == ev.id:
                return RedirectResponse("/r/%s/%s" % (slug, tok), status_code=303)
        salt, challenge, (qa, qb) = _mint_pow(request, ev)
        return templates.TemplateResponse("event_signup.html", _signup_ctx(
            request, ev, db=db, step=0, salt=salt, challenge=challenge,
            qa=qa, qb=qb, err=request.query_params.get("err", "")))

    @app.post("/r/{slug}")
    async def signup_start(request: Request, slug: str,
                           db: Session = Depends(get_db)):
        """Their details — and the first row we write.

        Written here, before anything else, so the trip out to the organiser's
        own site can never cost somebody what they have already typed.
        """
        ev = db.query(Event).filter(Event.slug == slug).first()
        if not ev or ev.mode != EVENT_OPEN:
            return RedirectResponse("/", status_code=303)
        if ev.signups_shut():
            return RedirectResponse("/r/%s" % slug, status_code=303)
        form = await request.form()
        bad = _signup_guard(request, ev, form)
        if bad:
            return RedirectResponse("/r/%s?err=%s" % (slug, bad), status_code=303)

        first = (form.get("first_name") or "").strip()[:60]
        last = (form.get("last_name") or "").strip()[:60]
        email = (form.get("email") or "").strip()
        mobile = re.sub(r"[^0-9+ ]", "", (form.get("mobile") or "").strip())[:24]
        sex = (form.get("sex") or "").strip()
        # Not required. Somebody who skips it, or whose browser sends nothing,
        # gets the default rather than a bounced form - a flag is decoration on
        # a board, not a thing worth turning anybody away over.
        country = country_code(form.get("country"))
        tier = (form.get("tier") or "").strip()
        form_routes.ensure_rates(db, ev)
        picked = _pickable(ev, tier)
        # What is required is whatever the form builder says is required, not
        # what this function used to assume. Name, email and a rate are the
        # three that cannot be switched off; the rest is the gym's business.
        need = {q.builtin for q in form_routes.plan(ev)
                if q.builtin and q.required}
        if not (first and last and picked and looks_like_email(email)):
            return RedirectResponse("/r/%s?err=missing" % slug, status_code=303)
        if ("mobile" in need and not mobile) or \
                ("sex" in need and sex not in ("m", "f")):
            return RedirectResponse("/r/%s?err=missing" % slug, status_code=303)
        if sex not in ("m", "f"):
            # An event that does not ask leaves it unset, which the board has
            # had an "Unlisted" column for since the day it was written.
            sex = None
        # The gym's own questions. A required one left blank is refused the
        # same way a missing email is - and the browser catches almost all of
        # them first, so this is the floor rather than the door.
        answers, missing = form_routes.read_answers(form, ev)
        if missing:
            return RedirectResponse("/r/%s?err=missing" % slug, status_code=303)
        # One registration per address. Coming back with the same email lands
        # you on your own row rather than making a second one.
        seen = (db.query(EventParticipant)
                .filter(EventParticipant.event_id == ev.id,
                        func.lower(EventParticipant.email) == email.lower())
                .first())
        # Full is checked here, at the moment of writing, rather than only
        # when the page was drawn - two people filling the form at once would
        # otherwise both get in on the last slot.
        #
        # Somebody already on the list is never turned away by it. They are
        # finishing a registration, not taking a new slot, and refusing them
        # would strand a row that already exists.
        if seen is None and _full(ev):
            return RedirectResponse("/r/%s?err=full" % slug, status_code=303)
        if seen:
            # Somebody put on the list by hand, now registering themselves.
            # Their row has a name and an email and nothing else — no rate, no
            # pay_status — so without this it would bounce them back to a page
            # that has no amount to ask for, forever. Adopt the row: it keeps
            # one person as one row, and keeps whatever was already true of
            # them (their slot, their place in the order).
            if seen.pay_status is None:
                seen.first_name, seen.last_name = first, last
                seen.name = "%s %s" % (first, last)
                seen.mobile, seen.sex = mobile, sex
                seen.country = country
                seen.tier = picked["key"]
                seen.amount = picked["price"]
                seen.pay_status = PAY_DRAFT
                seen.submitted_at = None
                form_routes.save_answers(db, seen, answers)
                db.commit()
                if picked["price"]:
                    _send_signup(db, ev, seen, base_url(request))
            resp = RedirectResponse("/r/%s/%s" % (slug, seen.token), status_code=303)
            resp.set_cookie("reg_%d" % ev.id, seen.token, max_age=60 * 60 * 24 * 60,
                            httponly=True, samesite="lax")
            return resp
        p = EventParticipant(
            event_id=ev.id, token=new_token(), name="%s %s" % (first, last),
            first_name=first, last_name=last, email=email, mobile=mobile,
            sex=sex, country=country, tier=picked["key"],
            amount=picked["price"], pay_status=PAY_DRAFT)
        db.add(p)
        db.flush()
        form_routes.save_answers(db, p, answers)
        db.commit()
        # "We've got it", now, while they are still looking at the screen that
        # sent it. A free registration gets its pass instead, a moment later,
        # when the next page lets them in.
        if picked["price"]:
            _send_signup(db, ev, p, base_url(request))
        resp = RedirectResponse("/r/%s/%s" % (slug, p.token), status_code=303)
        resp.set_cookie("reg_%d" % ev.id, p.token, max_age=60 * 60 * 24 * 60,
                        httponly=True, samesite="lax")
        return resp

    def _taken(ev) -> int:
        """How many slots are actually held.

        The same set the tracker counts in "29 of 30", deliberately: the
        number on the door and the number on your screen must be the same
        number, or one of them is lying.

        A registration mid-payment is not in it. That is the existing rule
        everywhere else - approving is what takes the slot, not submitting -
        and the finish-your-registration email says so in as many words.
        """
        return len([p for p in ev.participants
                    if not p.waitlist and not p.released_at and p.confirmed])

    def _full(ev) -> bool:
        """Is the room full? No capacity set means no limit, as before."""
        cap = ev.capacity or 0
        return cap > 0 and _taken(ev) >= cap

    def _free(p) -> bool:
        """Does this registration cost anything?

        Read off the participant, not the event: the amount was copied onto
        the row when they picked their rate, so a class that is free today and
        priced tomorrow does not rewrite what anybody already did.
        """
        return not p.amount or Decimal(p.amount) <= 0

    def _let_in(db, p, base=None) -> None:
        """Straight in, for a registration with nothing to pay.

        The same state approving a payment leaves behind, because it is the
        same thing: a confirmed slot. And the same email: their pass, with the
        QR on it, because for a free class that *is* the confirmation.
        """
        if p.pay_status == PAY_APPROVED:
            return
        now = datetime.now(timezone.utc)
        p.pay_status = PAY_APPROVED
        p.reviewed_at = p.reviewed_at or now
        p.review_note = None
        p.rsvp, p.rsvp_at = RSVP_YES, p.rsvp_at or now
        p.acknowledged_at = p.acknowledged_at or now
        p.signup_email_at = p.signup_email_at or now
        db.commit()
        if base:
            _send_pass(db, p.event, p, base)

    def _reg(db, slug, token):
        """Their row on this event's registration, at whatever stage.

        Deliberately not `p.registering`. That property means "has a
        pay_status", which somebody added to the list by hand does not — and
        they are exactly the person most likely to be holding one of these
        links, because the finish-your-registration email hands them one. A
        list that could be sent an email to a page that then denied knowing
        them was the bug; the row exists, so the page exists.
        """
        p = _participant(db, token)
        if not p or p.event.slug != slug or p.event.mode != EVENT_OPEN:
            return None
        return p

    @app.get("/r/{slug}/{token}", response_class=HTMLResponse)
    def signup_step(request: Request, slug: str, token: str,
                    db: Session = Depends(get_db)):
        """Wherever they got to, shown again."""
        p = _reg(db, slug, token)
        if not p:
            return templates.TemplateResponse(
                "event_gone.html", {"request": request, "reason": "unknown"},
                status_code=404)
        ev = p.event
        if p.pay_status == PAY_APPROVED:
            step = 5
        elif p.pay_status == PAY_SUBMITTED:
            step = 4
        elif p.pay_status == PAY_RETURNED:
            step = 3
        elif p.pay_status is None:
            # Added to the list by hand and never registered. The payment step
            # cannot draw for them — there is no rate on the row, so there is
            # no amount to ask for — so they get the details form with what we
            # already know filled in. Submitting it adopts this row rather than
            # making a second one; see signup_start.
            step = 0
        elif ev.external_url and not p.external_done_at:
            step = 2
        elif _free(p):
            # Nothing to pay, so nothing to send us and nothing for anybody to
            # check. A receipt step on a free class is a locked door with no
            # room behind it.
            _let_in(db, p, base_url(request))
            step = 5
        else:
            step = 3
        if step == 0:
            salt, challenge, (qa, qb) = _mint_pow(request, ev)
            return templates.TemplateResponse("event_signup.html", _signup_ctx(
                request, ev, p, db=db, step=0, salt=salt, challenge=challenge,
                qa=qa, qb=qb, err=request.query_params.get("err", "")))
        return templates.TemplateResponse("event_signup.html", _signup_ctx(
            request, ev, p, db=db, step=step, tier=_tier(ev, p.tier),
            err=request.query_params.get("err", "")))

    @app.post("/r/{slug}/{token}/external")
    def signup_external(request: Request, slug: str, token: str,
                        done: str = Form(""), db: Session = Depends(get_db)):
        p = _reg(db, slug, token)
        if not p:
            return RedirectResponse("/", status_code=303)
        if not done:
            return RedirectResponse("/r/%s/%s?err=tick" % (slug, token),
                                    status_code=303)
        p.external_done_at = datetime.now(timezone.utc)
        db.commit()
        return RedirectResponse("/r/%s/%s" % (slug, token), status_code=303)

    @app.post("/r/{slug}/{token}/pay")
    def signup_pay(request: Request, slug: str, token: str,
                   proof: UploadFile = None, ref: str = Form(""),
                   db: Session = Depends(get_db)):
        """The receipt. Required — a registration without one is not a
        registration, it is a person who read a page."""
        p = _reg(db, slug, token)
        if not p:
            return RedirectResponse("/", status_code=303)
        back = "/r/%s/%s" % (slug, token)
        raw = proof.file.read(6 * 1024 * 1024) if proof is not None else b""
        if not raw and not p.proof:
            return RedirectResponse(back + "?err=proof", status_code=303)
        if raw:
            mime = (proof.content_type or "").lower()
            if not mime.startswith("image/"):
                return RedirectResponse(back + "?err=notimage", status_code=303)
            p.proof, p.proof_mime = raw, mime
        p.proof_ref = (ref or "").strip()[:60]
        p.pay_status = PAY_SUBMITTED
        p.submitted_at = datetime.now(timezone.utc)
        p.review_note = None
        db.commit()
        return RedirectResponse(back, status_code=303)

    @app.get("/r/{slug}/{token}/proof")
    def signup_proof(slug: str, token: str, db: Session = Depends(get_db)):
        """Their own receipt back, so the page can show what they sent."""
        p = _reg(db, slug, token)
        if not p or not p.proof:
            return Response(status_code=404)
        return Response(p.proof, media_type=p.proof_mime or "image/jpeg",
                        headers={"Cache-Control": "private, max-age=300"})

    @app.get("/events/{eid}/pay-qr")
    def event_pay_qr(eid: int, db: Session = Depends(get_db)):
        ev = db.get(Event, eid)
        if not ev or not ev.pay_qr:
            return Response(status_code=404)
        return Response(ev.pay_qr, media_type=ev.pay_qr_mime or "image/png",
                        headers={"Cache-Control": "public, max-age=900"})

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

    @app.get("/e/{token}/pass", response_class=HTMLResponse)
    def event_pass(request: Request, token: str,
                   db: Session = Depends(get_db)):
        """The code, and nothing else.

        What "Show my QR pass" in an email opens. It used to open the whole
        participant page, which carries the confirmation, the sponsor terms
        and - once the class is over - a form asking for a Reel. Somebody at a
        door at half six in the morning wants the square, not a form.

        A pass is proof of a slot, so somebody who has not confirmed one has
        nothing to show: they are sent to their own page, which is where the
        question they still have to answer lives.
        """
        p = _participant(db, token)
        if not p:
            return templates.TemplateResponse(
                "event_gone.html", {"request": request, "reason": "unknown"},
                status_code=404)
        if _stage(p) not in ("ready", "submit", "late", "rewarded"):
            return RedirectResponse("/e/%s" % token, status_code=303)
        now = datetime.now(timezone.utc)
        p.opens = (p.opens or 0) + 1
        p.last_opened_at = now
        db.commit()
        return templates.TemplateResponse("event_pass.html", {
            "request": request, "p": p, "ev": p.event,
            "arrive": clock12(arrive_at(p.event, p.heat_time)),
            "day": _heat_day(p.event)})

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

        # A handle already on file stands in for one the form did not send,
        # and an agreement already given stands in for the tickbox. This is
        # what makes a re-confirmation a single tap: somebody being asked again
        # because the day moved has already handed over both, and asking twice
        # for a thing we are holding is how a one-question page turns back into
        # a form. Anything they do type still wins - the field is still there
        # for anybody whose handle has changed.
        handle = clean_handle(instagram) or clean_handle(p.instagram or "")
        agreed = ack == "on" or p.acknowledged_at is not None
        if rsvp != RSVP_YES or not handle or not agreed:
            return RedirectResponse(back + "?missing=1", status_code=303)
        p.rsvp, p.rsvp_at = RSVP_YES, now
        p.instagram = handle
        p.acknowledged_at = p.acknowledged_at or now
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
            # The same assignment the in-app scanner does. This is the path a
            # phone's own camera app takes — point it at a code and it opens
            # this URL — so it is the one most arrivals actually come through.
            # Leaving the slot logic only in the other scanner meant most
            # people got checked in with no start time at all.
            _assign_slot(db, p.event, p)
            db.commit()
        return render(request, "event_checkin.html", db, staff, active="events",
                      ev=p.event, who=p, stage=_stage(p), c=counts(p.event),
                      fresh=fresh, waves=_wave_counts(p.event)
                      if p.event.slot_a_time else None)

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

    def _wave_counts(ev: Event) -> dict:
        """How full each start time is. Shown on the scanner so whoever is on
        the door can see the first wave closing before it closes."""
        out = {}
        for p in ev.participants:
            if p.slot_time:
                out[p.slot_time] = out.get(p.slot_time, 0) + 1
        waves = []
        for t, cap in ((ev.slot_a_time, ev.slot_a_cap), (ev.slot_b_time, None)):
            if t:
                waves.append({"time": t, "n": out.get(t, 0), "cap": cap})
        return {"waves": waves}

    @app.get("/events/{eid}/scan", response_class=HTMLResponse)
    def event_scanner(request: Request, eid: int, db: Session = Depends(get_db)):
        """The camera, inside the app.

        A phone's own camera app works too — the QR carries a URL for exactly
        that reason — but it is one person, one scan, one app switch. This
        keeps the camera open and the count on screen, which is what you want
        with a queue in front of you.
        """
        staff, redir = door_guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        return render(request, "event_scan.html", db, staff, active="events",
                      ev=ev, c=counts(ev),
                      waves=_wave_counts(ev) if ev.slot_a_time else None)

    @app.post("/events/{eid}/scan")
    def event_scan_hit(request: Request, eid: int, code: str = Form(""),
                       db: Session = Depends(get_db)):
        """One scanned code. Answers in JSON so the camera never has to stop.

        Takes whatever the QR contained — we encode a full URL, so the token
        is the last path segment — and is deliberately forgiving about it, on
        the grounds that a door is a bad place to debug a string.
        """
        staff, redir = door_guard(request, db)
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
            _assign_slot(db, ev, p)
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
            "email": (p.email or "").strip(),
            "handle": p.handle,
            "slot": p.slot_time or "",
            "slot_no": p.slot_no,
            "waves": _wave_counts(ev) if ev.slot_a_time else None,
            "counts": {"in": c["arrived"], "of": c["confirmed"]},
        })

    @app.post("/events/{eid}/people/{pid}/race-status")
    def event_race_status(request: Request, eid: int, pid: int,
                          value: str = Form(""),
                     db: Session = Depends(get_db)):
        """Pin a race status by hand, or hand the row back to the derivation.

        Only the judgements are offered - a disqualification, a start that
        never happened, a race abandoned halfway. The five the system works out
        for itself are not settable, because pinning one would only let
        somebody freeze a row onto a fact the timer can already see through.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != eid:
            return RedirectResponse("/events/%d" % eid, status_code=303)
        want = (value or "").strip()
        p.race_status_set = want if want in RACE_STATUS_MANUAL else None
        db.commit()
        return RedirectResponse("/events/%d" % eid, status_code=303)

    @app.get("/events/{eid}/people/{pid}/race-status.json")
    def event_race_status_read(request: Request, eid: int, pid: int,
                          db: Session = Depends(get_db)):
        """What the status is now — asked after a change.

        The page cannot work out for itself what clearing a pin reveals, and
        guessing in the browser is how the badge and the truth drift apart.
        """
        staff, redir = guard(request, db)
        if redir:
            return JSONResponse({"ok": False}, status_code=403)
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != eid:
            return JSONResponse({"ok": False}, status_code=404)
        now_status = race_status(p)
        derived = race_status(p, derived_only=True)
        return {"ok": True, "status": now_status,
                "label": RACE_STATUS_LABELS[now_status],
                # What clearing the pin would reveal — the label the
                # "automatic" option has to carry.
                "derived": derived,
                "derived_label": RACE_STATUS_LABELS[derived],
                "pinned": bool(p.race_status_set)}

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
        # Oldest first: somebody who has been waiting nineteen hours on a
        # 24-hour promise is the one to look at, not the newest arrival.
        review = sorted([x for x in rest if x.pay_status == PAY_SUBMITTED],
                        key=lambda x: _aware(x.submitted_at) or datetime.now(timezone.utc))
        # Longest-stalled first — the row id is the order they arrived in.
        unfinished = sorted([x for x in rest if x.pay_status == PAY_DRAFT],
                            key=lambda x: x.id)
        return render(request, "event_detail.html", db, staff, active="events",
                      ev=ev, tab=tab, people=people, waiting=waiting,
                      gone=gone, review=review, unfinished=unfinished,
                      templates=sendable_templates(ev), money=money,
                      pay_labels=PAY_LABELS, status_moves=STATUS_MOVES,
                      col_list=columns_for(ev), cols=visible_cols(ev),
                      gone_col_list=columns_for(ev, "gone"),
                      gone_cols=visible_cols(ev, "gone"),
                      upload=request.session.pop("event_upload", None),
                      c=counts(ev),
                      lists={k: len(v) for k, v in mail_lists(ev).items()},
                      reel_left=left_until(ev.reel_deadline),
                      closes_left=left_until(ev.signup_closes),
                      base=base_url(request), tag_labels=TAG_LABELS,
                      statuses=EVENT_STATUSES,
                      mail=request.session.pop("event_mail", None),
                      org_link=_org_current(db, eid),
                      org_history=_org_links_for(db, eid),
                      org_default=ORGANISER_DEFAULT_PASS,
                      org_new=request.session.pop("org_new", None),
                      is_admin=(staff.role == "admin"),
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
            if p.released_at or p.waitlist:
                continue
            # An unpaid slot past the deadline we put in writing. Only ever
            # somebody who was actually told — pay_due_at is written by the
            # last-call email and by nothing else — so this can no more
            # release an unwarned person than the confirm clock below can.
            #
            # Released rather than deleted, and rather than marked declined:
            # they did not say no, they ran out of time, and those read
            # differently to whoever picks this up next. It is also the state
            # the tracker already knows how to show and how to reverse.
            if (p.pay_due_at and not p.paid and now > _aware(p.pay_due_at)
                    and p.pay_status != PAY_SUBMITTED):
                p.released_at = now
                n += 1
                continue
            if p.rsvp != RSVP_NONE:
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

    @app.get("/events/{eid}/p/{pid}/times", response_class=HTMLResponse)
    def event_times(request: Request, eid: int, pid: int,
                    db: Session = Depends(get_db)):
        """One person's race, station by station.

        The hinge between the participant list and everything that happens
        after somebody finishes: this is where staff land from the row, and
        where the patch and the finisher card are reached from. Read-only —
        nothing here writes, so a mis-tap at a trestle table costs a back
        button and nothing else.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != eid:
            return RedirectResponse("/events/%d" % eid, status_code=303)
        now = datetime.now(timezone.utc)
        rows = station_splits(p, now)
        # The sum of the splits, which is not the finish time — the walks
        # between stations are in the finish and not in here. Showing both is
        # the point: the difference is the transition time, and an athlete
        # asking "where did my time go" is usually asking about that.
        raced = sum(r["secs"] for r in rows if r["secs"] is not None)
        moving = sum(r["gap"] for r in rows if r["gap"] is not None)
        return render(request, "event_times.html", db, staff, active="events",
                      ev=p.event, who=p, rows=rows, mmss=mmss,
                      # Zero rather than None: a race that has not happened
                      # reads 0:00 across the board, which is a page you can
                      # look at the night before. A dash there would say "we
                      # do not know", and we do - nothing has happened.
                      raced=raced, moving=moving,
                      status=race_status(p, now),
                      running=p.running_seconds(now) or 0,
                      started=has_race(p), test=is_test_athlete(p),
                      is_admin=(staff.role == "admin"),
                      just_reset=bool(request.query_params.get("reset")),
                      just_edited=bool(request.query_params.get("edited")),
                      just_freed=bool(request.query_params.get("freed")),
                      coach=(db.get(Staff, p.coach_id).name
                             if p.coach_id and db.get(Staff, p.coach_id)
                             else None),
                      start=p.heat_start(), base=base_url(request))

    def _clocktext(secs):
        """A stored number of seconds as the text that goes in the field.

        Not ``_clock`` - that name is taken, module-level, by the one that
        reads a heat's "HH:MM" off a settings form. Two helpers a thousand
        lines apart both called _clock is how a nested def silently eats the
        one above it.
        """
        return "" if secs is None else mmss(secs)

    def _edit_rows(p, vals=None):
        """The station rows as an editable table.

        Pre-filled from what is stored unless we are re-drawing after a bad
        entry, in which case the admin gets back exactly what they typed - a
        form that empties itself when you mistype one field in it is a form
        that gets abandoned.
        """
        rows = station_splits(p)
        out = []
        for i, r in enumerate(rows):
            st = r["station"]
            sk, wk = "split_%d" % st.id, "walk_%d" % st.id
            out.append({
                "station": st, "name": r["name"],
                "unit": r["unit"], "target": r["target"], "count": r["count"],
                "split_key": sk, "walk_key": wk,
                "split": (vals.get(sk, "") if vals is not None
                          else _clocktext(r["secs"])),
                "walk": (vals.get(wk, "") if vals is not None
                         else _clocktext(r["gap"])),
                # The last station has nothing to walk to.
                "last": i == len(rows) - 1,
            })
        return out

    @app.get("/events/{eid}/p/{pid}/times/edit", response_class=HTMLResponse)
    def event_times_edit(request: Request, eid: int, pid: int,
                         db: Session = Depends(get_db)):
        """Correct a race by hand. Admins only.

        A stopwatch on a phone in a loud room gets things wrong: a coach taps
        Done a lane late, somebody's clock drifts, a station gets started for
        the wrong athlete. The results are what an athlete goes home with, so
        somebody has to be able to fix them.

        Every number on the page is editable and none of them are derived from
        each other - the splits, the walks and the finish time each stand on
        their own. That is deliberate. Making the finish recompute from the
        splits would be tidier arithmetic and the wrong tool: the finish time
        is the one that was called out and written down, and an admin fixing a
        mis-tapped split must not silently restate it.
        """
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != eid:
            return RedirectResponse("/events/%d" % eid, status_code=303)
        return render(request, "event_times_edit.html", db, staff,
                      active="events", ev=p.event, who=p,
                      rows=_edit_rows(p), finish=_clocktext(p.race_seconds),
                      start=p.heat_start(), err="")

    @app.post("/events/{eid}/p/{pid}/times/edit")
    async def event_times_save(request: Request, eid: int, pid: int,
                               db: Session = Depends(get_db)):
        """Write the corrected race back.

        The stamps are rebuilt as one chain rather than patched field by
        field. A split and a walk are both differences between two moments, so
        editing one in isolation would have to shove a neighbour to stay
        arithmetically possible - and an admin who fixed one row and found a
        different row had moved would rightly stop trusting the page. Rebuild
        from the top and every number on screen is the number that gets saved.

        The anchor is where the first raced station already started, so a
        correction lower down never moves the top of somebody's race.
        """
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != eid:
            return RedirectResponse("/events/%d" % eid, status_code=303)
        form = await request.form()
        vals = {k: (form.get(k) or "").strip() for k in form.keys()}

        def bad(msg):
            return render(request, "event_times_edit.html", db, staff,
                          active="events", ev=p.event, who=p,
                          rows=_edit_rows(p, vals), finish=vals.get("finish", ""),
                          start=p.heat_start(), err=msg)

        stations = sorted(p.event.stations, key=lambda s: (s.position, s.id)) \
            if p.event else []
        runs = {r.station_id: r for r in (p.runs or [])}

        # Read the whole form before writing anything. A page that saved the
        # first four rows and then refused the fifth would leave a race that is
        # neither the old one nor the new one.
        plan = []
        for st in stations:
            try:
                split = parse_clock(vals.get("split_%d" % st.id, ""))
                walk = parse_clock(vals.get("walk_%d" % st.id, ""))
            except ValueError:
                return bad("%s: a time reads like m:ss or h:mm:ss \u2014 3:20, or "
                           "1:03:20. Nothing was saved." % st.name)
            plan.append((st, split, walk))
        try:
            finish = parse_clock(vals.get("finish", ""))
        except ValueError:
            return bad("The finish time reads like m:ss or h:mm:ss \u2014 22:41, "
                       "or 1:04:09. Nothing was saved.")

        start = p.heat_start()
        if finish is not None and not start:
            return bad("There is no start to measure a finish time from - "
                       "this heat has no time set, or the event has no date. "
                       "Set those first. Nothing was saved.")
        # The top of the race stays where it was. Only if nothing has ever been
        # raced do we fall back to the gun.
        anchor = next((runs[st.id].started_at for st, _s, _w in plan
                       if runs.get(st.id) and runs[st.id].started_at), None)
        anchor = anchor or start
        if any(s is not None for _st, s, _w in plan) and not anchor:
            return bad("There is no start to measure the stations from - "
                       "this heat has no time set, or the event has no date. "
                       "Set those first. Nothing was saved.")

        cursor = anchor
        for st, split, walk in plan:
            run = runs.get(st.id)
            if split is None:
                # Cleared. The station was not raced, which is a different
                # thing from having been raced in no time.
                if run:
                    db.delete(run)
                continue
            if not run:
                run = StationRun(participant_id=p.id, station_id=st.id,
                                 count=st.target or 0)
                db.add(run)
            run.started_at = cursor
            run.ended_at = cursor + timedelta(seconds=split)
            cursor = run.ended_at + timedelta(seconds=walk or 0)
        p.finished_at = (start + timedelta(seconds=finish)
                         if finish is not None else None)
        db.commit()
        return RedirectResponse("/events/%d/p/%d/times?edited=1" % (eid, pid),
                                status_code=303)

    @app.post("/events/{eid}/p/{pid}/reset")
    def event_times_reset(request: Request, eid: int, pid: int,
                          db: Session = Depends(get_db)):
        """Put somebody back to the moment before a coach grabbed them.

        Admins only, and there is no undo. It clears the splits, the finish
        time, the coach and the grab, which is what makes a test re-runnable:
        they drop back into the heat as free, a coach grabs them again, and the
        clock starts from zero.

        What it deliberately leaves alone is everything that is not the race.
        Their check-in stands, because they are still in the building. Their
        age stands, because it did not change. The patch stands, because if one
        has been handed over then it is in somebody's hand and deleting the
        record does not get it back -- the awarding screen will simply ask
        again and say it was already collected, which is the true state of
        things.
        """
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != eid:
            return RedirectResponse("/events/%d" % eid, status_code=303)
        for r in list(p.runs or []):
            db.delete(r)
        p.finished_at = None
        p.coach_id = None
        p.grabbed_at = None
        # A pinned DNF or DQ was a judgement about a race that no longer
        # exists. Leaving it would show "DNF" against somebody standing on the
        # line waiting to start.
        p.race_status_set = None
        db.commit()
        return RedirectResponse("/events/%d/p/%d/times?reset=1" % (eid, pid),
                                status_code=303)

    @app.post("/events/{eid}/p/{pid}/release")
    def event_release_coach(request: Request, eid: int, pid: int,
                            db: Session = Depends(get_db)):
        """Hand somebody back so another coach can grab them.

        One field, and deliberately only one: ``coach_id``. The splits stay,
        the finish time stays, and the grab time stays.

        That last one matters more than it looks. For a test athlete the grab
        *is* the start of their clock, so clearing it would restart their race
        - and the grab route keeps whatever is already there rather than
        stamping a new one, so the next coach picks them up with the clock
        still running. Somebody mid-race whose coach's phone died gets a new
        coach, not a new race.

        Not admin-only, unlike Reset: this destroys nothing and undoes itself
        the moment somebody grabs them again.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != eid:
            return RedirectResponse("/events/%d" % eid, status_code=303)
        p.coach_id = None
        db.commit()
        return RedirectResponse("/events/%d/p/%d/times?freed=1" % (eid, pid),
                                status_code=303)

    @app.get("/events/{eid}/settings", response_class=HTMLResponse)
    def event_settings(request: Request, eid: int, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        return render(request, "event_settings.html", db, staff, active="events",
                      ev=ev, statuses=EVENT_STATUSES, modes=EVENT_MODES,
                      # How many yeses are standing. Nought means the reset has
                      # nothing to clear, so the button is not drawn at all.
                      held=len([p for p in ev.participants
                                if p.pay_status == PAY_APPROVED
                                or (p.pay_status is None
                                    and p.rsvp == RSVP_YES)]),
                      default_heat_open=HEAT_OPEN_MINS,
                      heat_open_min=HEAT_OPEN_MIN, heat_open_max=HEAT_OPEN_MAX,
                      base=base_url(request))

    @app.post("/events/{eid}/settings")
    def event_settings_save(
            request: Request, eid: int,
            name: str = Form(...), sponsor: str = Form(""),
            status: str = Form(EVENT_DRAFT),
            starts_at: str = Form(""), time_tba: str = Form(""),
            venue: str = Form(""),
            capacity: str = Form("30"), bring: str = Form(""), perk: str = Form(""),
            handles: str = Form(""), hashtag: str = Form(""),
            reel_hours: str = Form("48"), confirm_hours: str = Form("48"),
            reels_paused: str = Form(""), heat_open_mins: str = Form(""),
            reels_on: str = Form(""),
            confirm_by: str = Form(""), moved_from: str = Form(""),
            moved_why: str = Form(""),
            mode: str = Form(EVENT_INVITE), signup_open: str = Form(""),
            signup_closes: str = Form(""), slug: str = Form(""),
            external_url: str = Form(""), external_label: str = Form(""),
            external_note: str = Form(""),
            pay_qr_caption: str = Form(""), bank_details: str = Form(""),
            pay_note: str = Form(""), review_hours: str = Form("24"),
            pay_qr: UploadFile = None, drop_pay_qr: str = Form(""),
            code_prefix: str = Form("EV"),
            slot_a_time: str = Form(""), slot_a_cap: str = Form(""),
            slot_b_time: str = Form(""),
            reward_a: str = Form(""), reward_a_detail: str = Form(""),
            reward_a_value: str = Form(""),
            reward_b: str = Form(""), reward_b_detail: str = Form(""),
            reward_b_value: str = Form(""),
            sponsor_logo: UploadFile = None, drop_logo: str = Form(""),
            banner: UploadFile = None, drop_banner: str = Form(""),
            reward_amount: str = Form(""), reward_by: str = Form(""),
            db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)

        def dt(text):
            """A datetime-local value, read as gym time.

            Whoever fills this in is looking at a clock on a wall in Pasig, so
            that is the clock the value means. It is stored as a real instant,
            which is the only way a deadline can be compared to "now" and land
            when the page said it would.
            """
            text = (text or "").strip()
            if not text:
                return None
            try:
                return from_local(datetime.fromisoformat(text))
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
        ev.time_tba = bool(time_tba)
        ev.venue = venue.strip()
        ev.capacity = num(capacity, 30)
        ev.bring, ev.perk = bring.strip(), perk.strip()
        ev.handles, ev.hashtag = handles.strip(), hashtag.strip()
        ev.reel_hours = num(reel_hours, 48) or 48
        ev.reels_paused = (reels_paused == "on")
        # Three answers, not two: yes, no, and "you decide" - which is stored
        # as NULL so an event never has to be revisited when the default is.
        ev.reels_on = {"yes": True, "no": False}.get(
            (reels_on or "").strip().lower())
        # Blank means "use the default", which is a different answer from any
        # number — so it is stored as NULL rather than as 30. Change the default
        # later and every event that never had an opinion follows it.
        w = (heat_open_mins or "").strip()
        ev.heat_open_mins = (max(HEAT_OPEN_MIN, min(HEAT_OPEN_MAX, num(w, 0)))
                             if w and num(w, 0) else None)
        # Zero is a real answer here — it switches the rolling clock off and
        # hands the whole job to the fixed date below.
        ev.confirm_hours = num(confirm_hours, 48)
        ev.confirm_by = dt(confirm_by)
        # Blank is the normal state and has to stay reachable: an event that
        # was moved and then moved back has not moved.
        ev.moved_from = dt(moved_from)
        ev.moved_why = moved_why.strip()[:200] or None

        def price(text):
            try:
                v = Decimal(str(text).replace(",", "").strip())
                return v if v >= 0 else None
            except Exception:
                return None

        ev.mode = mode if mode in (EVENT_INVITE, EVENT_OPEN) else EVENT_INVITE
        ev.signup_open = bool(signup_open)
        ev.signup_closes = dt(signup_closes)
        # The slug is the URL you post, so a typo in the name at the moment the
        # event was created must not own it forever. It is deliberately not
        # rewritten when the name changes: somebody may already have shared it,
        # and a link that quietly stops working is worse than an ugly one.
        want = slugify(slug) if slug.strip() else ""
        if want and want != ev.slug:
            taken = (db.query(Event)
                     .filter(Event.slug == want, Event.id != ev.id).first())
            if taken:
                db.rollback()
                return RedirectResponse(f"/events/{eid}/settings?err=slug",
                                        status_code=303)
            ev.slug = want
        ev.external_url = external_url.strip()[:500]
        ev.external_label = external_label.strip()[:80]
        ev.external_note = external_note.strip()
        # The rates are edited on the registration form now - see the note on
        # Event.tier_a_label. This page must not write them, or a save here
        # would quietly blank the only record of what the event used to charge.
        ev.pay_qr_caption = pay_qr_caption.strip()[:120]
        ev.bank_details = bank_details.strip()
        ev.pay_note = pay_note.strip()[:200]
        ev.review_hours = num(review_hours, 24) or 24
        if drop_pay_qr:
            ev.pay_qr, ev.pay_qr_mime = None, None
        elif pay_qr is not None and (pay_qr.filename or ""):
            raw = pay_qr.file.read(2 * 1024 * 1024)
            if raw and (pay_qr.content_type or "").startswith("image/"):
                ev.pay_qr, ev.pay_qr_mime = raw, pay_qr.content_type
        ev.code_prefix = (code_prefix.strip()[:4].upper() or "EV")
        # Start times handed out at the door. A blank first time switches the
        # whole thing off, which is how an event that does not run in waves
        # keeps behaving exactly as it always did.
        ev.slot_a_time = _clock(slot_a_time)
        ev.slot_b_time = _clock(slot_b_time)
        try:
            cap = int((slot_a_cap or "").strip() or 0)
        except ValueError:
            cap = 0
        ev.slot_a_cap = cap if cap > 0 else None
        if not ev.slot_a_time:
            ev.slot_a_cap = ev.slot_b_time = None
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
        if drop_banner == "on":
            ev.banner, ev.banner_mime = None, None
        elif banner is not None and getattr(banner, "filename", ""):
            raw = banner.file.read()
            # Wider than a logo, so a wider ceiling — but still a ceiling. A
            # 6 MB header image is a message the recipient's mail server
            # bounces, and a bounce is not a thing anybody goes looking for.
            if raw and len(raw) <= 3 * 1024 * 1024:
                ev.banner = raw
                ev.banner_mime = (banner.content_type or "image/png")
        # The offer. Blank means no offer at all rather than zero pesos off:
        # the box simply is not drawn, and the thank-you is a thank-you.
        amt = (reward_amount or "").replace(",", "").replace("\u20b1", "").strip()
        try:
            ev.reward_amount = Decimal(amt) if amt else None
        except (InvalidOperation, ValueError):
            pass
        by = (reward_by or "").strip()
        # A date, not a moment: what is typed is the last day it can be
        # claimed, so it is stored as the end of that day rather than its
        # first second. Nobody's discount should expire at breakfast.
        if not by:
            ev.reward_by = None
        else:
            try:
                ev.reward_by = from_local(
                    datetime.fromisoformat(by).replace(hour=23, minute=59))
            except ValueError:
                pass
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

    @app.post("/events/{eid}/confirmations/reset")
    def event_reset_confirmations(request: Request, eid: int,
                                  db: Session = Depends(get_db)):
        """Ask the whole room again.

        For the case this was built for: a class that moved and came back
        smaller. Thirty-three people hold a yes to a Sunday morning that no
        longer exists, and fifteen slots. Leaving those yeses standing means
        the tracker says the class is full of people who agreed to something
        else, and the door turns away the first person who answers the new
        email.

        Only a yes is cleared. A no is an answer, not a stale confirmation —
        somebody who told you they can't come has not become undecided because
        you moved the date, and putting them back on the list to be counted and
        chased is the opposite of listening to them.

        Nothing else is touched: their link, their history, their emails and
        the reason anybody was released all survive, so this is a question
        being asked again rather than a list being rebuilt.

        And no per-person clock is stamped. That is the whole reason this
        exists rather than clicking "put them back" thirty-three times — that
        route hands out a fresh 24-hour window each, which on an event with one
        announced deadline would quietly expire before the date printed in the
        email everybody just received.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        n = 0
        for p in ev.participants:
            # Two shapes of yes, because there are two shapes of event. On an
            # invited class a yes is a button somebody pressed. On an open,
            # paid one it is a payment we approved — EventParticipant.confirmed
            # reads the payment and ignores the button entirely — so clearing
            # rsvp there would clear nothing and quietly report success.
            if p.pay_status == PAY_APPROVED:
                # Back to the queue, not refused: the receipt, the amount, the
                # rate and their place in the list all stay exactly where they
                # were. Approving is what takes a slot, so this frees the room
                # without losing the record that somebody paid for it — and
                # approving the first fifteen who answer *is* first come,
                # first served.
                #
                # No email. This is the tracker being made true; the message
                # explaining it is one you send yourself, once, to everybody.
                p.pay_status = PAY_SUBMITTED
                p.reviewed_at = None
                p.reviewed_by_id = None
                p.released_at = None
                p.acknowledged_at = None
                p.rsvp, p.rsvp_at = RSVP_NONE, None
                n += 1
                continue
            if p.rsvp != RSVP_YES:
                continue
            p.rsvp = RSVP_NONE
            p.rsvp_at = None
            # The sponsor agreement goes with the answer. It was given for a
            # class on a particular day, and this is a different day: asking
            # again costs one tick box, and not asking means holding somebody
            # to a thing they agreed to about something else.
            p.acknowledged_at = None
            # Cleared, not set: with no personal window they fall back to the
            # event's own announced deadline, which is the one date the email
            # quotes and the only one that can be true for everybody at once.
            p.confirm_due = None
            # A slot that lapsed under the old date is not lapsed under the
            # new one — they are being asked again, from now.
            p.released_at = None
            n += 1
        db.commit()
        return RedirectResponse("/events/%d?reset=%d" % (eid, n),
                                status_code=303)

    #: What you can move somebody to by hand, and what each one means. Kept
    #: here rather than spread through the handler so the page and the route
    #: are reading the same list, and so adding one is one edit.
    STATUS_MOVES = {
        "declined": "Can't make it",
        "released": "Slot released",
        "waitlist": "Waitlist",
        "back": "Back on the list",
    }

    # ---------------------------------------------------------- stations ----

    MEASURES = {"distance": "m", "reps": "reps"}

    @app.get("/events/{eid}/stations", response_class=HTMLResponse)
    def event_stations(request: Request, eid: int, db: Session = Depends(get_db)):
        """The workout stations, in the order they are raced."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        rows = sorted(ev.stations, key=lambda s: (s.position, s.id))
        # A station cannot change under somebody who is already racing it —
        # their count would suddenly mean something else, and there is no way
        # to tell afterwards which taps were against which target.
        racing = db.query(StationRun).join(EventStation).filter(
            EventStation.event_id == eid,
            StationRun.started_at.isnot(None)).count()
        # What a reset would actually throw away, so the confirm can name it
        # rather than asking "are you sure?" about an unknown quantity.
        finished = sum(1 for p in ev.participants if p.finished_at)
        return render(request, "event_stations.html", db, staff, active="events",
                      ev=ev, rows=rows, measures=MEASURES, locked=bool(racing),
                      racing=racing, finished=finished,
                      taps=sum(r.taps for r in rows))

    @app.post("/events/{eid}/stations/reset")
    def event_stations_reset(request: Request, eid: int,
                             db: Session = Depends(get_db)):
        """Throw away every race on this event and start the day again.

        Wanted after every rehearsal, and once for real if a coach counts the
        wrong person. It clears the counts, the splits, the finish times and
        who is on whose phone — everything the coach app wrote — and leaves the
        stations themselves alone, because the race is the thing you are
        redoing, not the way it was set up.

        It also unlocks the two things a started race holds shut: editing the
        stations, and making a different timetable version live.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        runs = (db.query(StationRun).join(EventStation)
                .filter(EventStation.event_id == eid).all())
        gone = len(runs)
        for r in runs:
            db.delete(r)
        people = 0
        for p in ev.participants:
            if p.finished_at or p.coach_id or p.grabbed_at:
                p.finished_at = None
                p.coach_id = None
                p.grabbed_at = None
                people += 1
        db.commit()
        return RedirectResponse(
            "/events/%d/stations?reset=%d&people=%d" % (eid, gone, people),
            status_code=303)

    @app.post("/events/{eid}/stations")
    async def event_stations_save(request: Request, eid: int,
                                  db: Session = Depends(get_db)):
        """Save the whole list in one go — order, names, targets, increments."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        started = db.query(StationRun).join(EventStation).filter(
            EventStation.event_id == eid,
            StationRun.started_at.isnot(None)).count()
        if started:
            return RedirectResponse("/events/%d/stations?locked=1" % eid,
                                    status_code=303)
        form = await request.form()
        ids = form.getlist("sid")
        names = form.getlist("name")
        measures = form.getlist("measure")
        targets = form.getlist("target")
        incs = form.getlist("inc")

        def num(raw, lo, hi, fallback):
            try:
                return max(lo, min(hi, int(str(raw).strip())))
            except (TypeError, ValueError):
                return fallback

        keep = set()
        for i, raw_id in enumerate(ids):
            name = (names[i] if i < len(names) else "").strip()
            if not name:
                continue                      # a nameless row is a deleted row
            row = (db.get(EventStation, int(raw_id))
                   if str(raw_id).isdigit() else None)
            if row is None or row.event_id != eid:
                row = EventStation(event_id=eid)
                db.add(row)
            row.position = i
            row.name = name[:80]
            m = measures[i] if i < len(measures) else "reps"
            row.measure = m if m in MEASURES else "reps"
            row.target = num(targets[i] if i < len(targets) else 0, 1, 100000, 1)
            row.increment = num(incs[i] if i < len(incs) else 1, 1, 10000, 1)
            db.flush()
            keep.add(row.id)
        for row in list(ev.stations):
            if row.id not in keep:
                db.delete(row)
        db.commit()
        return RedirectResponse("/events/%d/stations?saved=1" % eid,
                                status_code=303)

    # --------------------------------------------------- timetable versions ----
    #
    # A day of heats is rarely right first time, and the version you are
    # experimenting with must never be the version thirty-eight people can
    # already see. So each attempt is its own plan and exactly one is live.
    #
    # The trick that keeps this cheap: the event's own heat_* columns and every
    # participant's heat_time stay as a copy of whichever plan is active.
    # Emails, the public link, the door scanner and the coach app all read
    # those and know nothing about plans. Activating is what writes the copy.

    PLAN_MAX = 8

    def plans_for(db, ev):
        """Every version, in order, back-filling one from the live timetable.

        An event that has been arranged already must not lose that work the
        first time this page loads — so whatever is on the event right now
        becomes Version 1, active, and the page looks exactly as it did.
        """
        rows = sorted(ev.heat_plans, key=lambda p: (p.position, p.id))
        if rows:
            return rows
        plan = HeatPlan(
            event_id=ev.id, name="Version 1", position=0, is_active=True,
            heat_first=ev.heat_first, heat_last=ev.heat_last,
            heat_every=ev.heat_every or 10, heat_cap=ev.heat_cap or 3,
            heat_arrive=ev.heat_arrive if ev.heat_arrive is not None else 30)
        db.add(plan)
        db.flush()
        live = set(heat_times(ev))
        for p in ev.participants:
            if p.heat_time in live:
                plan.slots.append(HeatSlot(participant_id=p.id,
                                           heat_time=p.heat_time))
        db.commit()
        db.refresh(ev)
        return [plan]

    def active_plan(db, ev):
        rows = plans_for(db, ev)
        return next((p for p in rows if p.is_active), rows[0])

    def plan_or_active(db, ev, raw):
        """The version being edited: the one asked for, else the live one."""
        rows = plans_for(db, ev)
        if raw and str(raw).isdigit():
            got = next((p for p in rows if p.id == int(raw)), None)
            if got:
                return got
        return next((p for p in rows if p.is_active), rows[0])

    def plan_racing(db, eid) -> bool:
        """Has anybody started? Then the live timetable is load-bearing.

        Every race clock is measured from a heat time, so switching versions
        mid-race would move the fixed point somebody is already running
        against. Editing a draft stays fine — only going live is barred.
        """
        return bool(db.query(StationRun).join(EventStation).filter(
            EventStation.event_id == eid,
            StationRun.started_at.isnot(None)).count())

    def plan_diff(db, ev, plan):
        """What activating this version would actually do.

        Two numbers, because they carry different weight: how many people move
        at all, and how many of those are holding an email that says something
        else. The second one is the one that costs a phone call.
        """
        want = {s.participant_id: s.heat_time for s in plan.slots}
        live = set(heat_times(plan))
        moved, retold = 0, 0
        for p in ev.participants:
            new = want.get(p.id)
            if new not in live:
                new = None
            if (p.heat_time or None) == (new or None):
                continue
            moved += 1
            if p.heat_email_at:
                retold += 1
        return {"moved": moved, "retold": retold}

    def apply_plan(db, ev, plan):
        """Copy a version onto the event and its people. This is 'going live'.

        Moving somebody clears their sent stamp, exactly as dragging them does
        on the timetable — a time in somebody's inbox is wrong the instant you
        move them, and a tick that survives the move is how a person turns up
        an hour early holding proof they were told to.
        """
        for other in plan.event.heat_plans:
            other.is_active = (other.id == plan.id)
        ev.heat_first = plan.heat_first
        ev.heat_last = plan.heat_last
        ev.heat_every = plan.heat_every or 10
        ev.heat_cap = plan.heat_cap or 3
        ev.heat_arrive = plan.heat_arrive if plan.heat_arrive is not None else 30
        want = {s.participant_id: s.heat_time for s in plan.slots}
        live = set(heat_times(plan))
        moved, retold = 0, 0
        now = datetime.now(timezone.utc)
        for p in ev.participants:
            new = want.get(p.id)
            if new not in live:
                new = None
            if (p.heat_time or None) == (new or None):
                continue
            if p.heat_email_at:
                retold += 1
            p.heat_time = new
            p.heat_email_at = None
            p.edited_at = now
            moved += 1
        db.commit()
        return {"moved": moved, "retold": retold}

    @app.post("/events/{eid}/heats/version")
    def heat_version_new(request: Request, eid: int,
                         copy_from: str = Form(""),
                         db: Session = Depends(get_db)):
        """A new version — a copy of one you have, or an empty day.

        Copying is the default because that is what trying something means:
        you want the day you already built, with one thing different.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        rows = plans_for(db, ev)
        if len(rows) >= PLAN_MAX:
            return RedirectResponse("/events/%d/heats?vfull=1" % eid,
                                    status_code=303)
        src = plan_or_active(db, ev, copy_from) if copy_from else None
        n = max((p.position for p in rows), default=-1) + 1
        plan = HeatPlan(
            event_id=ev.id, name="Version %d" % (len(rows) + 1), position=n,
            is_active=False, created_by_id=staff.id,
            heat_first=src.heat_first if src else ev.heat_first,
            heat_last=src.heat_last if src else ev.heat_last,
            heat_every=(src or ev).heat_every or 10,
            heat_cap=(src or ev).heat_cap or 3,
            heat_arrive=(src or ev).heat_arrive if (src or ev).heat_arrive
            is not None else 30)
        db.add(plan)
        db.flush()
        if src:
            for slot in src.slots:
                plan.slots.append(HeatSlot(
                    participant_id=slot.participant_id,
                    heat_time=slot.heat_time))
        db.commit()
        return RedirectResponse("/events/%d/heats?v=%d&vnew=1" % (eid, plan.id),
                                status_code=303)

    @app.post("/events/{eid}/heats/version/{pid}/name")
    def heat_version_name(request: Request, eid: int, pid: int,
                          name: str = Form(""),
                          db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        plan = db.get(HeatPlan, pid) if ev else None
        if not plan or plan.event_id != eid:
            return RedirectResponse("/events/%d/heats" % eid, status_code=303)
        plan.name = (name or "").strip()[:40] or plan.name
        db.commit()
        return RedirectResponse("/events/%d/heats?v=%d" % (eid, pid),
                                status_code=303)

    @app.post("/events/{eid}/heats/version/{pid}/delete")
    def heat_version_delete(request: Request, eid: int, pid: int,
                            db: Session = Depends(get_db)):
        """Throw a version away. Never the live one, and never the last one.

        Deleting what is live would leave the event with a timetable nobody
        can point at — the times would still be on the people, but there would
        be no version that explains them.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        rows = plans_for(db, ev)
        plan = next((p for p in rows if p.id == pid), None)
        if not plan or plan.is_active or len(rows) < 2:
            return RedirectResponse("/events/%d/heats?vkeep=1" % eid,
                                    status_code=303)
        db.delete(plan)
        db.commit()
        return RedirectResponse("/events/%d/heats?vgone=1" % eid,
                                status_code=303)

    @app.post("/events/{eid}/heats/version/{pid}/activate")
    def heat_version_activate(request: Request, eid: int, pid: int,
                              db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        rows = plans_for(db, ev)
        plan = next((p for p in rows if p.id == pid), None)
        if not plan:
            return RedirectResponse("/events/%d/heats" % eid, status_code=303)
        if plan_racing(db, eid):
            return RedirectResponse("/events/%d/heats?v=%d&vlocked=1"
                                    % (eid, pid), status_code=303)
        out = apply_plan(db, ev, plan)
        return RedirectResponse(
            "/events/%d/heats?vlive=%d&vmoved=%d&vretold=%d"
            % (eid, pid, out["moved"], out["retold"]), status_code=303)

    # ------------------------------------------------ public heat timetable ----
    #
    # Thirty-five people all want to know one thing: what time do I run, and
    # when do I have to be there. Answering that thirty-five times by DM is a
    # morning, and an emailed time goes stale the moment somebody is moved.
    # So the timetable gets a URL, and the URL is always current.
    #
    # It carries other people's names, so it is a token rather than the event
    # slug: a guessable address for a start list is a start list anybody can
    # find. Names are shortened to a first name and an initial, which is
    # enough to find yourself and not enough to be a contact list. No email,
    # no phone, no payment, nothing about anybody's money.

    def _heat_public_ctx(ev, now=None):
        """Everything the public page shows, worked out in one place.

        Shared by the page and its tests so there is no second opinion about
        who appears on a public URL.
        """
        now = now or datetime.now(timezone.utc)
        times = heat_times(ev)
        known = set(times)
        people = sorted(
            [p for p in ev.participants
             if not (p.waitlist or p.declined or p.released_at)
             and p.heat_time in known],
            key=lambda p: ((p.given or "").lower(), (p.family or "").lower()))
        rows = []
        for t in times:
            here = [short_name(p) for p in people if p.heat_time == t]
            if not here:
                continue                # an empty heat is not news to anybody
            rows.append({"t": t, "arrive": arrive_at(ev, t), "people": here})
        # Which heat is happening, so somebody holding this page at the venue
        # can see where the day has got to. Only ever on the day itself.
        live = None
        if ev.starts_at and to_local(now).date() == to_local(ev.starts_at).date():
            mins = to_local(now).hour * 60 + to_local(now).minute
            for r in rows:
                start = int(r["t"][:2]) * 60 + int(r["t"][3:])
                if start <= mins < start + max(1, ev.heat_every or 10):
                    live = r["t"]
                    break
        return {"rows": rows, "live": live,
                "counted": sum(len(r["people"]) for r in rows)}

    @app.get("/h/{token}", response_class=HTMLResponse)
    def heat_public(request: Request, token: str,
                    db: Session = Depends(get_db)):
        """The timetable, for the people who are in it."""
        ev = (db.query(Event)
              .filter(Event.heat_token == token).first()) if token else None
        if not ev:
            # One page for "never existed" and "revoked" alike: the row is
            # gone either way, and a page that could tell them apart would be
            # a page that confirms a guess.
            return templates.TemplateResponse(
                "heat_public.html", {"request": request, "ev": None},
                status_code=404)
        ctx = _heat_public_ctx(ev)
        return templates.TemplateResponse("heat_public.html", {
            "request": request, "ev": ev, "clock12": clock12,
            "arrive_gap": gap_text(ev.heat_arrive or 0), **ctx})

    def _board_ctx(ev, now=None):
        """The board's columns, as plain data both the page and the feed read.

        One builder, so the first paint and every refresh afterwards can never
        disagree about who is where.

        Everything on a participant that is not their name, their flag and
        their race is dropped here rather than in the template. A page is one
        careless `{{ }}` away from leaking an address; a context that never
        held the address cannot.
        """
        cols = []
        for col in board_rows(ev, now):
            out = []
            for r in col["rows"]:
                p = r["p"]
                out.append({
                    "id": p.id,
                    "name": short_name(p),
                    # The strip mixes all four groups, so each card has to
                    # say which one it is. Two letters, because that is all
                    # the room a card has - "A\u00b7W" is the Advanced women's
                    # column, and the colour on the chip says it again for
                    # anybody reading from across a room.
                    #
                    # Taken off the label rather than written here, so renaming
                    # a category renames its letter with it. This used to be a
                    # hardcoded "E".
                    "sx": "%s\u00b7%s" % (
                        CATEGORY_LABELS[category_key(p.category)][:1].upper(),
                        {"m": "M", "f": "W"}.get(p.sex, "-")),
                    "elite": category_key(p.category) == "elite",
                    # Whether they belong in the strip rather than a column.
                    # Decided here, so the page and the feed cannot disagree
                    # about who is on the floor.
                    "racing": r["status"] == "in_progress",
                    "cc": country_code(p.country),
                    "flag": country_flag(p.country),
                    "country": country_name(p.country),
                    "place": r["place"],
                    "status": r["status"],
                    "label": RACE_STATUS_LABELS[r["status"]],
                    "out": r["status"] in RACE_STATUS_OUT,
                    "secs": r["secs"],
                    "time": mmss(r["secs"]) if r["secs"] is not None else "",
                    "done": r["done"], "on": r["on"], "of": r["of"],
                    "heat": h12(r["heat"]),
                    # Where they are right now. The short name is what fits on
                    # a card; the full one is the title, for anybody who does
                    # not know that BBJ is the burpee broad jump.
                    "st": r["st_short"], "st_full": r["st_name"],
                    "reps": (None if r["st_count"] is None else
                             "%s / %s%s" % (r["st_count"], r["st_target"],
                                            (" " + r["st_unit"])
                                            if r["st_unit"] else "")),
                    # The clock a spectator watches. Sent as a number of
                    # seconds at this instant; the page ticks it on from there
                    # rather than asking again every second.
                    "elapsed": r["elapsed"] if r["status"] == "in_progress" else None,
                })
            # The page draws the two categories as boxes of two columns
            # inside each, so it needs the split as data rather than by
            # picking the key apart in JavaScript. Sent from here for the
            # usual reason: the first paint and every refresh afterwards read
            # the same builder and cannot disagree about it.
            key = col["key"]
            cat = key.split(":")[0] if ":" in key else ""
            cols.append({
                "key": key,
                "label": col["label"],
                "cat": cat,
                "catLabel": CATEGORY_LABELS.get(cat, ""),
                # Inside a category's box the column is just "Men" - repeating
                # the category above it and again on it is the box saying its
                # own name twice.
                "short": (col["label"].split(" ", 1)[1]
                          if cat else col["label"]),
                "rows": out,
            })
        return cols

    @app.get("/l/{token}", response_class=HTMLResponse)
    def board_public(request: Request, token: str,
                     db: Session = Depends(get_db)):
        """The live leaderboard, for anybody with the link."""
        ev = (db.query(Event)
              .filter(Event.board_token == token).first()) if token else None
        if not ev:
            # One page for "never existed" and "revoked" alike - two different
            # answers would between them confirm a guess.
            return templates.TemplateResponse(
                "board_public.html", {"request": request, "ev": None},
                status_code=404)
        return templates.TemplateResponse("board_public.html", {
            "request": request, "ev": ev, "cols": _board_ctx(ev),
            "sponsors": SPONSORS})

    def _results_ctx(ev, now=None):
        """Everybody's race, once the racing is over.

        Built off the same ``board_rows`` the live board reads, so a placing
        never differs between the two screens. What it adds is the pair of
        numbers the Time Summary shows staff - the stations and the walks -
        and an overall order across the whole field, which the board has no
        use for and a results page is mostly about.

        This is a public page, so the same rule applies here as on the board:
        it carries a name, a flag, a group and a clock, and nothing else that
        is on the row. The age is on the row and does not travel - it is on
        file for a patch, not for a page anybody can open.
        """
        groups, rows = [], []
        for col in board_rows(ev, now):
            picked = []
            for r in col["rows"]:
                p = r["p"]
                raced, moving = race_totals(p, now)
                row = {
                    "id": p.id,
                    "name": short_name(p),
                    "cat": (col["key"].split(":")[0]
                            if ":" in col["key"] else ""),
                    "sex": p.sex if p.sex in ("m", "f") else "",
                    "group": col["label"],
                    "flag": country_flag(p.country),
                    "cc": country_code(p.country),
                    "country": country_name(p.country),
                    # Where they came in their own group. The overall number
                    # is added below, once every group has been read.
                    "gplace": r["place"],
                    "place": None,
                    "status": r["status"],
                    "label": RACE_STATUS_LABELS[r["status"]],
                    "out": r["status"] in RACE_STATUS_OUT,
                    "done": r["status"] == "finished",
                    "secs": r["secs"],
                    "time": mmss(r["secs"]) if r["secs"] is not None else "",
                    # Only for a race that is over. Half a field's worth of
                    # stations and a walk that is still being walked are not
                    # a result, and printing them next to finished ones on a
                    # results page invites somebody to compare them.
                    "raced": mmss(raced) if (raced and r["status"] == "finished")
                             else "",
                    "moving": mmss(moving) if (moving and r["status"] == "finished")
                              else "",
                    "heat": h12(r["heat"]),
                }
                picked.append(row)
                rows.append(row)
            if picked:
                groups.append({
                    "key": col["key"], "label": col["label"],
                    "cat": picked[0]["cat"], "sex": picked[0]["sex"],
                    # Three is a podium. Fewer than three is however many
                    # there are, because a podium with an empty step on it
                    # looks like a page that failed to load.
                    "top": [r for r in picked if r["done"]][:3],
                    "raced": len(picked),
                    "finished": sum(1 for r in picked if r["done"]),
                })

        # One order across the whole field. Finishers by time, then everybody
        # who started and did not finish, then the judged-out rows - and only
        # the finishers get a number, here for the same reason as on the
        # board: a placing against somebody who did not finish is not a
        # placing, it is a row number.
        rows.sort(key=lambda r: (
            0 if r["done"] else (2 if r["out"] else 1),
            r["secs"] if r["secs"] is not None else 0,
            r["name"].lower(),
        ))
        n = 0
        for r in rows:
            if r["done"]:
                n += 1
                r["place"] = n
        return {"rows": rows, "groups": groups, "finishers": n,
                "field": len(rows),
                # The board is the morning. Once nobody is on the floor it is
                # a page about a race that has stopped, so it stops being
                # offered - and comes back on its own for the next event.
                "racing": sum(1 for r in rows
                              if r["status"] == "in_progress")}

    def _board_event(db, token):
        return (db.query(Event)
                .filter(Event.board_token == token).first()) if token else None

    @app.get("/l/{token}/results", response_class=HTMLResponse)
    def results_public(request: Request, token: str,
                       db: Session = Depends(get_db)):
        """Everybody's times, on the link that was already handed out.

        The same token as the live board rather than a second one to print and
        share. During the morning that link is the board; afterwards it is the
        results, and revoking it still takes down both at once - which is what
        somebody pressing Revoke means.
        """
        ev = _board_event(db, token)
        if not ev:
            return templates.TemplateResponse(
                "board_public.html", {"request": request, "ev": None},
                status_code=404)
        return templates.TemplateResponse("results_public.html", {
            "request": request, "ev": ev, "token": token, "view": "results",
            "review_url": REVIEW_URL, **_results_ctx(ev)})

    @app.get("/l/{token}/podium", response_class=HTMLResponse)
    def podium_public(request: Request, token: str,
                      db: Session = Depends(get_db)):
        """The top of each group - four winners, not one."""
        ev = _board_event(db, token)
        if not ev:
            return templates.TemplateResponse(
                "board_public.html", {"request": request, "ev": None},
                status_code=404)
        return templates.TemplateResponse("podium_public.html", {
            "request": request, "ev": ev, "token": token, "view": "podium",
            **_results_ctx(ev)})

    #: How far out a station is drawn on the shape when it is the only result
    #: on it. Dead centre would say "worst in the field" about somebody who is
    #: also the best in it; the middle is the honest answer to "compared with
    #: whom".
    SHAPE_ALONE = 0.5

    def _radar(shape, cx=120.0, cy=112.0, r=78.0):
        """The polygon, its rings and its labels, worked out here.

        Geometry in Python rather than trigonometry in a template: a chart
        drawn by string concatenation in Jinja is a chart nobody can check.
        """
        n = len(shape)
        if n < 3:
            return None
        step = 2 * math.pi / n
        # Start at the top and go clockwise, so the first station is where a
        # reader's eye already is.
        angs = [-math.pi / 2 + i * step for i in range(n)]

        def at(i, frac, rad=None):
            rr = (rad if rad is not None else r) * frac
            return (round(cx + math.cos(angs[i]) * rr, 1),
                    round(cy + math.sin(angs[i]) * rr, 1))

        def poly(frac):
            return " ".join("%s,%s" % at(i, frac) for i in range(n))

        labels = []
        for i, sp in enumerate(shape):
            x, y = at(i, 1.0, r + 19)
            # Nudge the text off the point rather than centring it on top of
            # the axis, except at the top and bottom where centred is right.
            dx = math.cos(angs[i])
            anchor = "middle" if abs(dx) < 0.3 else ("start" if dx > 0
                                                     else "end")
            labels.append({"name": sp["name"], "x": x,
                           "y": round(y + (4 if abs(dx) >= 0.3 else
                                           (10 if math.sin(angs[i]) > 0
                                            else -2)), 1),
                           "anchor": anchor})
        return {
            "cx": cx, "cy": cy,
            "rings": [poly(f) for f in (1.0, 0.66, 0.33)],
            "spokes": [at(i, 1.0) for i in range(n)],
            "you": " ".join("%s,%s" % at(i, max(0.08, shape[i]["v"]))
                            for i in range(n)),
            "labels": labels,
        }

    def _athlete_ctx(ev, p, now=None):
        """One person's race, and where it sits in the field that ran it.

        Ranked against everybody at this event rather than against their own
        group. "4th of 16" is a number somebody can picture; "2nd of 4" is a
        number that mostly describes how few Advanced women entered.
        """
        now = now or datetime.now(timezone.utc)
        field = station_field(ev, now)
        rows, shape = [], []
        for r in station_splits(p, now):
            st = r["station"]
            pairs = field.get(st.id, [])
            place, of, through = rank_in(pairs, p.id)
            rows.append({
                "name": st.name,
                "count": r["count"], "target": r["target"], "unit": r["unit"],
                "secs": r["secs"], "split": mmss(r["secs"]) if r["secs"] is not None else "",
                "gap": r["gap"], "walk": mmss(r["gap"]) if r["gap"] is not None else "",
                "place": place, "of": of,
                # Every result on this station, as a position from 0 (fastest)
                # to 1 (slowest), for the strip of dots. Sixteen dots is the
                # field; a curve fitted to sixteen points is an opinion.
                # Everybody the same time is a real answer on a station with
                # a hard cap, and it belongs in the middle of the line rather
                # than piled on the fast end - "left" would read as fastest
                # when nobody was.
                "dots": ([{"at": 50 if pairs[-1][1] == pairs[0][1] else
                           round((sec - pairs[0][1]) * 100.0
                                 / (pairs[-1][1] - pairs[0][1]), 2),
                           "me": pid == p.id}
                          for pid, sec in pairs] if pairs else []),
                "fastest": mmss(pairs[0][1]) if pairs else "",
                "slowest": mmss(pairs[-1][1]) if pairs else "",
            })
            shape.append({
                "name": station_shorts(sorted(ev.stations,
                                              key=lambda s: (s.position, s.id))
                                       ).get(st.id, st.name),
                "full": st.name,
                "v": (SHAPE_ALONE if (through is None or of < 2) else through),
                "known": place is not None,
            })

        raced, moving = race_totals(p, now)
        # The walks, ranked the same way. It is the number nobody looks at and
        # the one with a minute hiding in it.
        walks = sorted(((q.id, race_totals(q, now)[1])
                        for q in ev.participants
                        if not (q.waitlist or q.released_at or q.declined)
                        and race_status(q, now) == "finished"),
                       key=lambda t: t[1])
        wplace, wof, _wt = rank_in(walks, p.id)

        st = race_status(p, now)
        cols = board_rows(ev, now)
        mine = next((c for c in cols
                     for r in c["rows"] if r["p"].id == p.id), None)
        gplace = next((r["place"] for c in cols for r in c["rows"]
                       if r["p"].id == p.id), None)
        overall = _results_ctx(ev, now)
        oplace = next((r["place"] for r in overall["rows"] if r["id"] == p.id),
                      None)
        return {
            "who": {
                "name": short_name(p),
                "flag": country_flag(p.country),
                "country": country_name(p.country),
                "group": mine["label"] if mine else "",
                "cat": (mine["key"].split(":")[0]
                        if mine and ":" in mine["key"] else ""),
                "status": st, "label": RACE_STATUS_LABELS[st],
                "done": st == "finished",
                "finish": mmss(p.race_seconds) if p.finished_at else "",
                "raced": mmss(raced), "moving": mmss(moving),
                # What share of the race was spent walking. The number that
                # makes somebody look at their transitions for the first time.
                "walkpct": (round(moving * 100.0 / (raced + moving))
                            if (raced + moving) else 0),
                "gplace": gplace, "oplace": oplace,
                "field": overall["finishers"],
                "wplace": wplace, "wof": wof,
            },
            "rows": rows, "shape": shape, "radar": _radar(shape),
            "walkdots": ([{"at": 50 if walks[-1][1] == walks[0][1] else
                           round((sec - walks[0][1]) * 100.0
                                 / (walks[-1][1] - walks[0][1]), 2),
                           "me": pid == p.id}
                          for pid, sec in walks] if walks else []),
            "walkfast": mmss(walks[0][1]) if walks else "",
            "walkslow": mmss(walks[-1][1]) if walks else "",
        }

    @app.get("/l/{token}/p/{pid}", response_class=HTMLResponse)
    def athlete_public(request: Request, token: str, pid: int,
                       db: Session = Depends(get_db)):
        """One athlete's race, for anybody holding the results link.

        Public by decision, so the same rule as the other two pages: a name
        shortened to a first name and an initial, a flag, a group and clocks.
        The age on the row is on file for a patch and does not travel here.
        """
        ev = _board_event(db, token)
        if not ev:
            return templates.TemplateResponse(
                "board_public.html", {"request": request, "ev": None},
                status_code=404)
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != ev.id or p.waitlist or p.released_at \
                or p.declined:
            # The same answer as a bad token. Two different ones would between
            # them confirm a guess about who is on this event.
            return templates.TemplateResponse(
                "board_public.html", {"request": request, "ev": None},
                status_code=404)
        ctx = _results_ctx(ev)
        return templates.TemplateResponse("athlete_public.html", {
            "request": request, "ev": ev, "token": token, "view": "athlete",
            "finishers": ctx["finishers"], "review_url": REVIEW_URL,
            "cardlink": "/l/%s/me" % token if p.finished_at else "",
            "racing": ctx["racing"], **_athlete_ctx(ev, p)})

    # ---------------------------------------------------- find my result ----
    #: How many wrong email/age pairs one address may try before it is asked to
    #: wait. Email plus age is a guessable pair - an address somebody already
    #: knows, and a number between about 15 and 70 - so the thing standing
    #: between a stranger and a member's full name is this counter rather than
    #: the strength of the secret. Generous enough that a person mistyping
    #: their own address four times never meets it.
    ME_TRIES, ME_WINDOW = 8, 15 * 60

    def _me_throttled(request) -> bool:
        """True if this address has been guessing. Also does the recording."""
        who = (request.client.host if request.client else "?") or "?"
        now = datetime.now(timezone.utc).timestamp()
        seen = [t for t in _ME_FAILS.get(who, []) if now - t < ME_WINDOW]
        _ME_FAILS[who] = seen
        return len(seen) >= ME_TRIES

    def _me_failed(request):
        who = (request.client.host if request.client else "?") or "?"
        _ME_FAILS.setdefault(who, []).append(
            datetime.now(timezone.utc).timestamp())

    def _me_of(request, ev, db):
        """The participant this browser has already proved it is, or None."""
        pid = request.session.get("me_%d" % ev.id)
        if not pid:
            return None
        p = db.get(EventParticipant, pid)
        # Re-checked on every request rather than trusted from the cookie: a
        # row that has since been removed from the event must stop resolving.
        if not p or p.event_id != ev.id or p.waitlist or p.released_at \
                or p.declined:
            return None
        return p

    def _me_ctx(request, ev, token, p, db, err=""):
        opened = bool(p and (p.review_opened_at
                             or request.session.get("me_open_%d" % ev.id)))
        return {
            "request": request, "ev": ev, "token": token, "view": "me",
            "racing": _results_ctx(ev)["racing"],
            "review_url": REVIEW_URL, "err": err,
            "stage": "card" if p else "ask",
            "who": ({"full": p.full_name,
                     "flag": country_flag(p.country),
                     "finish": mmss(p.race_seconds) if p.finished_at else "",
                     "done": bool(p.finished_at),
                     "pid": p.id} if p else None),
            # The stations are built only once they have been past the ask.
            # Not rendered-then-hidden: markup on the page is markup anybody
            # can read with the developer tools, and a lock you can open with
            # a keyboard shortcut is one that annoys the honest and stops
            # nobody.
            "opened": opened,
            "rows": (_athlete_ctx(ev, p)["rows"] if (opened and p) else []),
        }

    @app.get("/l/{token}/me", response_class=HTMLResponse)
    def find_me(request: Request, token: str, db: Session = Depends(get_db)):
        """"That's me" - the way to your own card.

        The results list stays open to anybody: times at a public race are not
        a secret, and spectators, family and the gym's own website all need it.
        The card is different. It carries a full name, where the list carefully
        says "Atheena G.", so it asks first.
        """
        ev = _board_event(db, token)
        if not ev:
            return templates.TemplateResponse(
                "board_public.html", {"request": request, "ev": None},
                status_code=404)
        # A deliberate "show me it anyway" from somebody who reviewed months
        # ago, or is on a laptop. It opens the card for this browser only and
        # writes nothing: a review that did not happen through us must not
        # leave a stamp saying it did.
        if request.query_params.get("open"):
            request.session["me_open_%d" % ev.id] = 1
        return templates.TemplateResponse(
            "find_me.html", _me_ctx(request, ev, token,
                                    _me_of(request, ev, db), db))

    @app.post("/l/{token}/me", response_class=HTMLResponse)
    def find_me_check(request: Request, token: str, email: str = Form(""),
                      age: str = Form(""), db: Session = Depends(get_db)):
        """Email plus age, against this event's list."""
        ev = _board_event(db, token)
        if not ev:
            return templates.TemplateResponse(
                "board_public.html", {"request": request, "ev": None},
                status_code=404)
        if _me_throttled(request):
            return templates.TemplateResponse(
                "find_me.html",
                _me_ctx(request, ev, token, None, db, err="slow"),
                status_code=429)
        want = (email or "").strip().lower()
        try:
            years = int((age or "").strip())
        except ValueError:
            years = None
        hit = next((q for q in ev.participants
                    if (q.email or "").strip().lower() == want and want
                    and q.age is not None and q.age == years
                    and not (q.waitlist or q.released_at or q.declined)), None)
        if not hit:
            _me_failed(request)
            # One message for a wrong address and a wrong age alike. Two
            # different ones would tell a stranger which half they had right,
            # which is most of the work of guessing the other.
            return templates.TemplateResponse(
                "find_me.html", _me_ctx(request, ev, token, None, db,
                                        err="nomatch"),
                status_code=404)
        request.session["me_%d" % ev.id] = hit.id
        return RedirectResponse("/l/%s/me" % token, status_code=303)

    @app.get("/l/{token}/me/review")
    def find_me_review(request: Request, token: str,
                       db: Session = Depends(get_db)):
        """Stamp the row, then hand them to Google.

        Through the server rather than straight off the button, because a link
        that goes directly to Google is a link we never hear about. This is the
        only moment the system learns anything at all, and even then what it
        learns is that they went - see EventParticipant.review_opened_at.
        """
        ev = _board_event(db, token)
        p = _me_of(request, ev, db) if ev else None
        if not ev or not p:
            return RedirectResponse("/l/%s/me" % token, status_code=303)
        # First trip only. A second one is the same person coming back for
        # another look, and moving the stamp would lose the day they went.
        if not p.review_opened_at:
            p.review_opened_at = datetime.now(timezone.utc)
            db.commit()
        return RedirectResponse(REVIEW_URL, status_code=303)

    @app.get("/l/{token}/feed.json")
    def board_feed(request: Request, token: str,
                   db: Session = Depends(get_db)):
        """What the page asks for every few seconds."""
        ev = (db.query(Event)
              .filter(Event.board_token == token).first()) if token else None
        if not ev:
            return JSONResponse({"ok": False}, status_code=404)
        return {"ok": True, "cols": _board_ctx(ev)}

    @app.post("/events/{eid}/board-link")
    def board_link_new(request: Request, eid: int,
                       db: Session = Depends(get_db)):
        """Mint the leaderboard's link, or replace the one there is."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        ev.board_token = secrets.token_urlsafe(18)
        ev.board_link_at = datetime.now(timezone.utc)
        db.commit()
        return RedirectResponse("/events/%d/heats?board=new" % eid,
                                status_code=303)

    @app.post("/events/{eid}/board-link/revoke")
    def board_link_revoke(request: Request, eid: int,
                          db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        ev.board_token = None
        ev.board_link_at = None
        db.commit()
        return RedirectResponse("/events/%d/heats?board=off" % eid,
                                status_code=303)

    @app.post("/events/{eid}/heat-link")
    def heat_link_new(request: Request, eid: int,
                      db: Session = Depends(get_db)):
        """Mint a share link, or replace the one there is.

        Replacing is the same action as making one: there is only ever a
        single live link per event, and reissuing is how you take back a URL
        that went somewhere you did not mean it to.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        ev.heat_token = secrets.token_urlsafe(18)
        ev.heat_link_at = datetime.now(timezone.utc)
        db.commit()
        return RedirectResponse("/events/%d/heats?link=new" % eid,
                                status_code=303)

    @app.post("/events/{eid}/heat-link/revoke")
    def heat_link_revoke(request: Request, eid: int,
                         db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        ev.heat_token = None
        ev.heat_link_at = None
        db.commit()
        return RedirectResponse("/events/%d/heats?link=off" % eid,
                                status_code=303)

    # ------------------------------------------------------------- heats ----

    @app.get("/events/{eid}/heats", response_class=HTMLResponse)
    def event_heats(request: Request, eid: int, v: str = "",
                    db: Session = Depends(get_db)):
        """The timetable: every heat down the page, who is in each one.

        Which version you are looking at rides in the URL rather than the
        session, so a refresh, the back button and a link you send yourself all
        land on the same one.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        versions = plans_for(db, ev)
        plan = plan_or_active(db, ev, v)
        # The day shape and the assignments both come off the version being
        # edited, so a draft can differ from what is live in either.
        times = heat_times(plan)
        known = set(times)
        everyone = sorted(
            [p for p in ev.participants
             if not (p.waitlist or p.declined or p.released_at)],
            key=lambda p: (p.name or "").lower())
        where = {s.participant_id: s.heat_time for s in plan.slots}
        # A heat that no longer exists is not a heat. Rebuilding the day
        # shorter, or on a different interval, strands whoever was in the rows
        # that went — and silently dropping them is how somebody arrives on
        # Saturday to find they were never on the list. They come back to the
        # unassigned tray, where you cannot miss them.
        rows = [{"t": t, "arrive": arrive_at(plan, t),
                 "people": [p for p in everyone if where.get(p.id) == t]}
                for t in times]
        free = [p for p in everyone if where.get(p.id) not in known]
        live = plan.is_active
        return render(request, "event_heats.html", db, staff, active="events",
                      ev=ev, rows=rows, free=free, times=times,
                      plan=plan, versions=versions, is_live=live,
                      can_activate=not plan_racing(db, eid),
                      diff=None if live else plan_diff(db, ev, plan),
                      total=len(everyone),
                      room=len(times) * max(1, plan.heat_cap or 1),
                      arrive_gap=gap_text(plan.heat_arrive or 0),
                      # Telling people only means anything on the live version:
                      # a draft time is not a time anybody has.
                      told=len([p for p in everyone
                                if live and p.heat_time in known
                                and p.heat_email_at]),
                      waiting=len([p for p in everyone
                                   if live and p.heat_time in known
                                   and not p.heat_email_at]),
                      clock12=clock12,
                      mail_ready=Mailer().cfg.configured,
                      mail=request.session.pop("event_mail", None),
                      base=base_url(request))

    @app.post("/events/{eid}/heats/day")
    def event_heat_day(request: Request, eid: int,
                       first: str = Form(""), last: str = Form(""),
                       every: str = Form("10"), cap: str = Form("3"),
                       arrive: str = Form("30"), plan_id: str = Form(""),
                       db: Session = Depends(get_db)):
        """The shape of the day. Rebuilding it never moves anybody by itself.

        Whoever is left standing on a time that no longer exists shows up in
        the unassigned tray on the next screen, which is the honest answer:
        the software cannot know whether you meant to move them earlier or
        later, and guessing would put somebody in a heat nobody chose.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)

        def num(raw, lo, hi, fallback):
            try:
                return max(lo, min(hi, int((raw or "").strip())))
            except ValueError:
                return fallback

        plan = plan_or_active(db, ev, plan_id)
        plan.heat_first = hhmm(first)
        plan.heat_last = hhmm(last)
        plan.heat_every = num(every, 1, 240, 10)
        plan.heat_cap = num(cap, 1, 99, 3)
        plan.heat_arrive = num(arrive, 0, 240, 30)
        # No first heat means the event is not running heats at all, and a
        # last heat on its own would draw a timetable of one row at a time
        # nobody typed.
        if not plan.heat_first:
            plan.heat_last = None
        # A draft only changes itself. The live version writes through, so
        # editing the one that is live behaves exactly as it always did.
        if plan.is_active:
            apply_plan(db, ev, plan)
        db.commit()
        return RedirectResponse("/events/%d/heats?v=%d&day=ok" % (eid, plan.id),
                                status_code=303)

    @app.post("/events/{eid}/heats/save")
    async def event_heat_save(request: Request, eid: int,
                              db: Session = Depends(get_db)):
        """Save the whole timetable in one go.

        The page posts every assignment, not a diff, because the thing on
        screen is the thing you mean — a diff would have to guess at rows two
        people edited in different tabs, and quietly lose one of them.

        Moving somebody clears their sent stamp. The time in their inbox is
        wrong the instant you move them, and a tick that survives the move is
        how a person turns up an hour early with an email proving they were
        told to.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        form = await request.form()
        # The URL first: the page rebuilds this form's fields on every change,
        # so the query string is the copy that cannot be clobbered.
        plan = plan_or_active(db, ev, request.query_params.get("v")
                              or form.get("plan_id"))
        known = set(heat_times(plan))
        # "<participant id>:<HH:MM>", one per assigned person.
        want = {}
        for raw in form.getlist("at"):
            pid, _sep, t = (raw or "").partition(":")
            t = hhmm(t)
            if pid.isdigit() and t in known:
                want[int(pid)] = t
        # The version is the thing being edited, so it is rewritten whole.
        # An unassigned person is the absence of a slot rather than a slot
        # with nothing in it — one kind of nothing, on every version.
        mine = {p.id for p in ev.participants}
        want = {pid: t for pid, t in want.items() if pid in mine}
        had = {s.participant_id: s.heat_time for s in plan.slots}
        moved = sum(1 for pid in set(had) | set(want)
                    if had.get(pid) != want.get(pid))
        for slot in list(plan.slots):
            if want.get(slot.participant_id) != slot.heat_time:
                # Removed from the collection rather than deleted through the
                # session: apply_plan reads plan.slots straight after this, and
                # a deleted row stays in a loaded collection until it expires —
                # which would leave somebody taken off the timetable still
                # holding the time they were taken off.
                plan.slots.remove(slot)
        # Flushed before the inserts: a person moving from 10:00 to 10:10 is a
        # delete and an insert on the same (plan, person), and the unique index
        # bites if the insert lands first.
        db.flush()
        for pid, t in want.items():
            if had.get(pid) != t:
                plan.slots.append(HeatSlot(participant_id=pid, heat_time=t))
        db.flush()
        # The live version writes through to the people, which is what makes
        # editing it behave exactly as it always did.
        if plan.is_active:
            apply_plan(db, ev, plan)
        db.commit()
        return RedirectResponse("/events/%d/heats?v=%d&saved=%d"
                                % (eid, plan.id, moved), status_code=303)

    @app.post("/events/{eid}/heats/send")
    def event_heat_send(request: Request, eid: int, pid: str = Form(""),
                        db: Session = Depends(get_db)):
        """Their heat time, to one person or to everyone still owed it."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        known = set(heat_times(ev))
        if (pid or "").strip().isdigit():
            p = db.get(EventParticipant, int(pid))
            # A heat that no longer exists is not a time worth emailing.
            pool = ([p] if p and p.event_id == eid and p.heat_time in known
                    else [])
        else:
            pool = mail_lists(ev)["heat"]
        request.session["event_mail"] = _send(
            db, ev, pool, "heat", base_url(request))
        return RedirectResponse("/events/%d/heats" % eid, status_code=303)

    @app.post("/events/{eid}/columns")
    async def event_set_columns(request: Request, eid: int,
                                scope: str = Form("people"),
                                db: Session = Depends(get_db)):
        """Choose which columns one of this event's tables shows.

        Saved as a list of the columns that are *on*, not of the ones that are
        off, so a column added to the system later does not silently turn
        itself on for an event somebody has already made a decision about.

        The empty string is stored deliberately when nothing is ticked. NULL
        would mean "never chose", and an event where you have switched every
        optional column off is not the same as an event you have never
        touched — the first should stay bare, the second should keep picking
        up the defaults.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        # Only keys that exist, and only ones that mean something on this
        # event. A posted key for a column this event cannot show would sit in
        # the row forever, doing nothing, until the mode changed and it
        # suddenly did something nobody asked for.
        if scope not in COLUMN_SCOPES:
            return RedirectResponse("/events/%d" % eid, status_code=303)
        # getlist, because a set of tickboxes posts one repeated field and
        # anything that reads only the first value would quietly save one
        # column and drop the rest.
        ticked = set((await request.form()).getlist("col"))
        setattr(ev, COLUMN_SCOPES[scope],
                ",".join(k for k, _label, _on in columns_for(ev, scope)
                         if k in ticked))
        db.commit()
        return RedirectResponse("/events/%d?%s" % (
            eid, urlencode({"tab": scope, "cols": "ok"})), status_code=303)

    @app.post("/events/{eid}/columns/reset")
    def event_reset_columns(request: Request, eid: int,
                            scope: str = Form("people"),
                            db: Session = Depends(get_db)):
        """Back to the defaults — and back to *following* the defaults."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev or scope not in COLUMN_SCOPES:
            return RedirectResponse("/events", status_code=303)
        setattr(ev, COLUMN_SCOPES[scope], None)
        db.commit()
        return RedirectResponse("/events/%d?%s" % (
            eid, urlencode({"tab": scope, "cols": "reset"})), status_code=303)

    @app.post("/events/{eid}/people/{pid}/status")
    def event_set_status(request: Request, eid: int, pid: int,
                         to: str = Form(""), db: Session = Depends(get_db)):
        """Move somebody by hand, without deleting them.

        Until now the only action on a row was Remove, which throws away their
        link, their answer and their history to record a fact as ordinary as
        "they messaged me, they can't come". People tell you things off the
        system — a DM, at the door, in person — and the tracker has to be able
        to hold that.

        Every move is reversible, and none of them writes an email: this
        records something you already know, it does not announce it.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        p = db.get(EventParticipant, pid)
        if not ev or not p or p.event_id != eid or to not in STATUS_MOVES:
            return RedirectResponse(f"/events/{eid}", status_code=303)
        now = datetime.now(timezone.utc)
        if to == "declined":
            # Their own no, recorded on their behalf. Also clears a release:
            # somebody who ran out of time and then told you they can't come
            # has said something, and that is the truer of the two.
            p.rsvp, p.rsvp_at = RSVP_NO, now
            p.released_at = None
            p.waitlist = False
        elif to == "released":
            # They did not say no; the slot is simply free again. Kept
            # separate from a decline because those read differently to
            # whoever picks this up next month.
            p.released_at = now
            p.waitlist = False
        elif to == "waitlist":
            # Off the count and out of every send until you give them a slot
            # back. Their answer is cleared with them: a yes to a slot they no
            # longer hold would be counted, and it would be wrong.
            p.waitlist = True
            p.released_at = None
            p.rsvp, p.rsvp_at = RSVP_NONE, None
        else:
            # Back on the list, with a fresh window — whatever deadline
            # applied has almost certainly passed by now.
            p.rsvp, p.rsvp_at = RSVP_NONE, None
            p.released_at = None
            p.waitlist = False
            p.confirm_due = now + timedelta(hours=ev.confirm_hours or 24)
        p.edited_at = now
        name = p.name
        db.commit()
        # Back to the tab you were looking at, not to the top of the page. The
        # referer is rebuilt rather than appended to: moving three people in a
        # row otherwise stacks three ?moved= on the end of the URL, and only
        # the path and query are kept so a referer from anywhere else cannot
        # bounce us off site.
        ref = urlsplit(request.headers.get("referer") or "")
        q = [(k, v) for k, v in parse_qsl(ref.query)
             if k not in ("moved", "who")]
        q += [("moved", to), ("who", name or "")]
        path = ref.path if ref.path.startswith("/events/%d" % eid) \
            else "/events/%d" % eid
        return RedirectResponse("%s?%s" % (path, urlencode(q)), status_code=303)

    @app.post("/events/{eid}/people/{pid}/approve")
    def event_approve_payment(request: Request, eid: int, pid: int,
                              db: Session = Depends(get_db)):
        """Their money is good. Mint the pass and tell them.

        Approving is what takes the slot — not submitting. Somebody halfway
        through paying has not got a place yet, and holding one for them is how
        a class ends up full of people who never finished.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        p = db.get(EventParticipant, pid)
        if not ev or not p or p.event_id != eid:
            return RedirectResponse(f"/events/{eid}", status_code=303)
        p.pay_status = PAY_APPROVED
        p.reviewed_at = datetime.now(timezone.utc)
        p.reviewed_by_id = staff.id
        p.review_note = None
        # A paid registration is a confirmed slot, so everything downstream —
        # the door, the Reel email, the counts — treats them like anybody else.
        p.rsvp, p.rsvp_at = RSVP_YES, p.rsvp_at or datetime.now(timezone.utc)
        p.acknowledged_at = p.acknowledged_at or datetime.now(timezone.utc)
        p.pass_email_at = None
        db.commit()
        _send_pass(db, ev, p, base_url(request))
        return RedirectResponse(f"/events/{eid}?tab=review&ok={pid}", status_code=303)

    @app.post("/events/{eid}/people/{pid}/return")
    def event_return_payment(request: Request, eid: int, pid: int,
                             why: str = Form(""), db: Session = Depends(get_db)):
        """Ask for a better receipt, rather than refusing them.

        A rejection that makes somebody start over is how you lose a paying
        customer to a blurry photo. Their details, their rate and their place in
        the queue all stay; only the receipt is asked for again.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        p = db.get(EventParticipant, pid)
        if not ev or not p or p.event_id != eid:
            return RedirectResponse(f"/events/{eid}", status_code=303)
        note = (why or "").strip()[:600]
        if not note:
            return RedirectResponse(f"/events/{eid}?tab=review&err=why",
                                    status_code=303)
        p.pay_status = PAY_RETURNED
        p.review_note = note
        p.reviewed_at = datetime.now(timezone.utc)
        p.reviewed_by_id = staff.id
        db.commit()
        _send_returned(db, ev, p, base_url(request))
        return RedirectResponse(f"/events/{eid}?tab=review&sent={pid}",
                                status_code=303)

    @app.get("/events/{eid}/people/{pid}/proof")
    def event_proof(request: Request, eid: int, pid: int,
                    db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        p = db.get(EventParticipant, pid)
        if not p or p.event_id != eid or not p.proof:
            return Response(status_code=404)
        return Response(p.proof, media_type=p.proof_mime or "image/jpeg",
                        headers={"Cache-Control": "private, max-age=300"})

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
                          instagram: str = Form(""), country: str = Form(""),
                          sex: str = Form(""), category: str = Form(""),
                          age: str = Form(""),
                          db: Session = Depends(get_db)):
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
        # Anything unreadable becomes the default rather than blank: a flagless
        # row on the board reads as a bug, not as a preference.
        p.country = country_code(country)
        # Blank is a real answer - "we do not know" - and is not the same as
        # guessing. Anything that is not one of the two is stored as nothing.
        want = (sex or "").strip().lower()
        p.sex = want if want in {k for k, _l in SEXES} else None
        # No blank branch: the category has no "not set". Anything unreadable
        # lands on Open, which is where somebody who has not been looked at
        # yet belongs anyway.
        p.category = category_key(category)
        # Blank clears it. Until now the only way an age got on file was a
        # member of staff typing it at the awarding table, so most rows have
        # none - which matters more than it used to, because it is half of
        # what somebody answers to reach their own card.
        want_age = (age or "").strip()
        if not want_age:
            p.age = None
        else:
            try:
                n = int(want_age)
            except ValueError:
                n = None
            # The same bracket the patch desk accepts. Anything outside it is
            # a typo, and a typo here locks somebody out of their own card.
            if n is not None and 5 <= n <= 110:
                p.age = n
        # Stamped here rather than by an onupdate, so "last update" means
        # somebody changed something and not "a participant opened their link".
        p.edited_at = datetime.now(timezone.utc)
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

    def _send(db, ev, people, kind, base, pay_by=""):
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
        if ev.banner:
            inline[BANNER_CID] = ev.banner
        # The deadline you typed, read as gym time — whoever fills that box is
        # looking at a clock on a wall in Pasig. Parsed once for the whole
        # send, so twenty people get one deadline rather than twenty that
        # differ by the time it took to loop.
        chosen_due = None
        if kind == "payby" and (pay_by or "").strip():
            try:
                chosen_due = from_local(
                    datetime.fromisoformat(pay_by.strip()))
            except ValueError:
                chosen_due = None
        for p in people:
            if not looks_like_email(p.email or ""):
                skipped.append({"name": p.name, "detail": "no email on record"})
                continue
            # Somebody mid-registration has to land back in the flow they
            # left, not on the participant page — that one assumes a slot they
            # have not got yet. Same for a receipt we've sent back: the whole
            # ask is "do the payment step again".
            if kind in ("finish", "returned", "payby"):
                # Somebody mid-registration has to land back in the flow they
                # left, not on the participant page - that one assumes a slot
                # they have not got yet.
                url = "%s/r/%s/%s" % (base, ev.slug, p.token)
            elif kind in ("heat", "pass"):
                # These two say "show my QR pass" on the button, so that is
                # what the button opens. Anything else is a page somebody has
                # to read past while a queue builds behind them.
                url = "%s/e/%s/pass" % (base, p.token)
            else:
                url = "%s/e/%s" % (base, p.token)
            build = {"reel": _reel_mail, "finish": _finish_mail,
                     "returned": _returned_mail,
                     "lastcall": _lastcall_mail, "payby": _payby_mail,
                     "reconfirm": _reconfirm_mail,
                     "cancelled": _cancelled_mail,
                     "thanks": _thanks_mail,
                     "heat": _heat_mail}.get(kind, _invite_mail)
            # One button, two wordings. Somebody who has never had a time from
            # us gets "Your heat time"; somebody who has gets "Your heat time
            # has changed", because to them it is not news, it is a
            # correction — and an email that reads like the first one is an
            # email they skim and ignore.
            if kind == "heat" and heat_kind(ev, p) == "heatnew":
                build = _heat_new_mail
            if kind == "payby":
                # Set here rather than after a successful send, because the
                # sentence in the email is this value. Deciding it afterwards
                # would let the email and the row disagree about the one thing
                # the email is for.
                p.pay_due_at = chosen_due or (datetime.now(timezone.utc)
                                              + timedelta(hours=PAY_GRACE_HOURS))
                db.flush()
            subject, text, html = build(db, ev, p, url)
            ok, detail = mailer.send(p.email, subject, text, html=html,
                                     inline=inline or None)
            if ok:
                now = datetime.now(timezone.utc)
                if kind == "invite":
                    p.invited_at = now
                elif kind == "finish":
                    p.nudged_at = now
                elif kind == "reel":
                    p.reel_email_at = now
                elif kind == "cancelled":
                    p.cancel_email_at = now
                elif kind == "thanks":
                    p.thanks_email_at = now
                elif kind == "payby":
                    # Already stamped, before the send — the email quotes this
                    # exact moment, so it has to be decided before the words
                    # are built rather than after they have gone.
                    pass
                elif kind == "heat":
                    p.heat_email_at = now
                    # Neither of these is ever unset. They are what make the
                    # *next* one read as a change rather than a repeat — the
                    # person's own flag, and the event's, which survives
                    # everybody on it being moved.
                    p.heat_told_before = True
                    ev.heat_sent_at = ev.heat_sent_at or now
                elif kind == "lastcall":
                    # Not invited_at: that is "when we first asked", and the
                    # per-person confirm clock is counted from it. Restarting
                    # it here would hand somebody a fresh 48 hours on the
                    # morning the whole thing closes.
                    p.last_call_at = now
                # "returned" stamps nothing on purpose. The review queue owns
                # that state, and marking a row reviewed because somebody
                # re-sent the email would move it out of a queue it still
                # belongs in.
                sent.append({"name": p.name, "detail": p.email})
            else:
                failed.append({"name": p.name, "detail": detail})
        db.commit()
        return {"sent": sent, "failed": failed, "skipped": skipped, "kind": kind}

    #: Every email the system can send on purpose, in the order you'd meet
    #: them. Data rather than a row of buttons, because the alternative is a
    #: new button on that bar every time there is a new thing to say — and the
    #: bar is where you go when the tidy lists don't cover your case.
    def sendable_templates(ev) -> list:
        """Every template, always selectable, with a note on where each fits.

        These used to grey out the ones that didn't match the event's mode, and
        that was the wrong call. This bar only ever sends to names you have
        ticked yourself: you have already told us who, so being told you may not
        is the software second-guessing a decision it does not have the context
        to make. A class that half-registered on paper, a person who needs the
        receipt email again a week after review \u2014 every one of those is a real
        case that the rule locked out and nothing replaced.

        The notes stay, because "usually sent from To review" is worth knowing.
        A note is guidance; a disabled radio is a wall.
        """
        openreg = ev.mode == EVENT_OPEN
        return [
            {"key": "finish", "name": "Finish your registration",
             "blurb": "\u201cYou started but haven't paid yet.\u201d For anyone "
                      "mid-registration.",
             "ok": True,
             "why": "" if openreg else "usually open-registration"},
            {"key": "invite", "name": "The invitation",
             "blurb": "\u201cYou're in \u2014 confirm your slot.\u201d Asks them "
                      "to confirm a slot you're holding.",
             "ok": True,
             "why": "" if not openreg else "usually invite events"},
            {"key": "lastcall", "name": "Last call to confirm",
             "blurb": "\u201cWe still need a yes, and the deadline is today.\u201d "
                      "For anyone asked who hasn't answered.",
             "ok": True,
             "why": "" if not openreg else "usually invite events"},
            {"key": "reconfirm", "name": "The date has changed",
             "blurb": "\u201cCan you still make it?\u201d Says why it moved, "
                      "and prints the old date beside the new one.",
             "ok": bool(ev.moved_from),
             "why": "" if ev.moved_from else "set \u201cMoved from\u201d in "
                                             "Settings first"},
            {"key": "reel", "name": "The Reel email",
             "blurb": "\u201cThank you \u2014 submit your Reel and pick your "
                      "reward.\u201d",
             "ok": wants_reels(ev),
             "why": "" if wants_reels(ev) else "this event does not ask for a Reel"},
            {"key": "returned", "name": "Ask for a better receipt",
             "blurb": "Carries your reason for sending one back, when there is "
                      "one on the row.",
             "ok": True, "why": "usually from To review"},
            {"key": "payby", "name": "Last call to pay",
             "blurb": "\u201c24 hours to pay, or the slot goes to the next "
                      "person.\u201d Stamps the deadline it quotes.",
             "ok": True, "why": "usually Not finished"},
            {"key": "heat", "name": "Your heat time",
             "blurb": "\u201cArrive at 9:50, your heat is 10:20.\u201d Only says "
                      "anything to somebody who has a heat.",
             "ok": True, "why": "usually from the timetable"},
            # Not a separate button on the timetable — the send there picks
            # this wording by itself for anybody who has had a time before.
            # It is listed here so it can be previewed and edited like the
            # rest, and sent by hand if you ever want to.
            {"key": "heatnew", "name": "Your heat time has changed",
             "blurb": "\u201cWe\u2019ve moved your heat \u2014 here is the new "
                      "one.\u201d Sent automatically to anybody who was already "
                      "told a different time.",
             "ok": True, "why": "chosen for you when you send heat times"},
            {"key": "thanks", "name": "Thank you & review",
             "blurb": "\u201cThank you for training with us.\u201d Asks for a "
                      "review, and carries the offer for anybody who leaves one.",
             "ok": True,
             "why": "" if ev.reward_text else "set the reward under Settings "
                                              "to show the offer"},
            {"key": "cancelled", "name": "Called off",
             "blurb": "\u201cToday's class is cancelled.\u201d Goes to everyone "
                      "who thinks they're coming, and everyone still deciding.",
             "ok": True, "why": "check the wording first"},
        ]

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
        # The heats that currently exist. A row can hold a heat_time the day
        # no longer contains — see the "heat" list below.
        live_heats = set(heat_times(ev))
        # Somebody who has told you they can't come is not somebody to invite
        # again. "Re-send to all" reaching them is the exact message that makes
        # a person stop reading anything you send.
        askable = [p for p in mailable if not p.declined]
        # Somebody who signed themselves up was never invited and must never be.
        # The invitation asks "do you want this slot?" — a question with an
        # awkward answer for a person who has already paid for it, and a
        # confirm link that would step on their registration.
        invitable = [p for p in askable if not p.registering]
        return {
            # The invitation: everyone who hasn't had one.
            "invite": [p for p in invitable if not p.invited_at],
            "invite_all": invitable,
            # The last call: asked, and still hasn't said yes or no. Anyone
            # never asked is deliberately out — a "last call" to somebody who
            # was never called is the first they'd hear of it, and it reads as
            # a deadline they were set up to miss.
            "lastcall": [p for p in invitable
                         if p.invited_at and not p.confirmed
                         and not p.last_call_at],
            "lastcall_all": [p for p in invitable
                             if p.invited_at and not p.confirmed],
            # The date has changed: everybody still on the list who has not
            # answered for the new day. Deliberately not filtered on
            # invited_at the way the last call is - a reschedule is news to
            # everyone holding a slot, including anybody added since, and
            # somebody who never got the original invitation is exactly the
            # person who must not now be left out of the one that moves it.
            "reconfirm": [p for p in invitable if not p.confirmed],
            "reconfirm_all": invitable,
            # The Reel email: everyone who came and hasn't been asked yet.
            "reel": ([p for p in confirmed if not p.reel_email_at]
                     if wants_reels(ev) else []),
            # The thank-you: everyone who came and hasn't had it. Confirmed
            # rather than arrived, because the door is not always scanned and
            # a class that nobody checked in would otherwise offer an empty
            # list — the tick boxes remain there for the exceptions.
            "thanks": [p for p in confirmed if not p.thanks_email_at],
            "thanks_all": confirmed,
            # The nudge: everyone who was asked and still hasn't posted. A
            # separate list because sending the first ask again to somebody who
            # already posted is the fastest way to sour this.
            "nudge": ([p for p in confirmed
                       if p.reel_email_at and not p.posted]
                      if wants_reels(ev) else []),
            "reel_all": confirmed if wants_reels(ev) else [],
            # Started and stalled. Deliberately not the ones we sent back
            # for a better receipt — those have already had an email carrying
            # your reason, and a second, vaguer nudge on top of it reads as
            # not having been listened to.
            "unfinished": [p for p in mailable
                           if p.pay_status == PAY_DRAFT and not p.nudged_at],
            "unfinished_all": [p for p in mailable if p.pay_status == PAY_DRAFT],
            # The last call to pay. Everybody holding an unpaid slot — the
            # ones who started and stopped, and the ones we sent back for a
            # better receipt, because both are people whose place is not yet
            # theirs. Not the ones waiting on us: their money is in, and
            # telling them to hurry up would be our mistake, not theirs.
            "payby": [p for p in mailable
                      if p.pay_status in (PAY_DRAFT, PAY_RETURNED)
                      and not p.pay_due_at],
            "payby_all": [p for p in mailable
                          if p.pay_status in (PAY_DRAFT, PAY_RETURNED)],
            # Called off. Everybody who thinks they are coming, plus everybody
            # still deciding — a cancellation is the one email that has to
            # reach people who never replied, because they are the ones most
            # likely to turn up at a locked door. Declined people are left out:
            # they already said they weren't coming.
            "cancelled": [p for p in askable if not p.cancel_email_at],
            "cancelled_all": askable,
            # Heat times. Only people whose heat is still on the timetable —
            # an email whose entire content is a time is worse than silence
            # when that time is wrong. Rebuilding the day shorter leaves a
            # stale heat_time on the row (deliberately: the timetable shows
            # those people in the unassigned tray so you cannot miss them),
            # and without this check the next send would post it to them.
            #
            # "Not yet told" also catches anybody moved since, because moving
            # somebody clears the stamp: the time in their inbox is wrong and
            # they are owed the corrected one.
            "heat": [p for p in mailable
                     if p.heat_time in live_heats and not p.heat_email_at],
            "heat_all": [p for p in mailable if p.heat_time in live_heats],
            # Not a send list — a to-do for you. Their link still works; it
            # just has to reach them some other way.
            "no_email": [p for p in live if not looks_like_email(p.email or "")],
        }

    @app.post("/events/{eid}/send")
    def event_send(request: Request, eid: int, kind: str = Form("invite"),
                   who: str = Form(""), pick: list[int] = Form(default=[]),
                   pay_by: str = Form(""),
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
        # A template that does not apply to this event is not a thing you can
        # send, however the request got here.
        allowed = {t["key"] for t in sendable_templates(ev) if t["ok"]}
        if kind not in allowed:
            return RedirectResponse(f"/events/{eid}?err=template", status_code=303)
        if who == "selected":
            # You ticked these names, so they are the list — no filtering by
            # who has already had it or who has confirmed. Hand-picking is the
            # escape hatch for every case the tidy lists don't cover, and
            # second-guessing it would defeat the point.
            chosen = set(pick)
            pool = [p for p in ev.participants if p.id in chosen]
            request.session["event_mail"] = _send(
                db, ev, pool, kind, base_url(request), pay_by=pay_by)
            return RedirectResponse(f"/events/{eid}", status_code=303)
        if kind == "reel":
            pool = lists["nudge"] if who == "nudge" else (
                lists["reel_all"] if who == "all" else lists["reel"])
        elif kind == "finish":
            pool = (lists["unfinished_all"] if who == "all"
                    else lists["unfinished"])
        elif kind == "lastcall":
            pool = (lists["lastcall_all"] if who == "all"
                    else lists["lastcall"])
        elif kind == "payby":
            pool = lists["payby_all"] if who == "all" else lists["payby"]
        elif kind == "cancelled":
            pool = (lists["cancelled_all"] if who == "all"
                    else lists["cancelled"])
        elif kind == "thanks":
            pool = lists["thanks_all"] if who == "all" else lists["thanks"]
        elif kind == "heat":
            pool = lists["heat_all"] if who == "all" else lists["heat"]
        elif kind == "returned":
            # There is no sensible "everyone" for this one — it carries a reason
            # written about one payment. Reachable only by ticking names, which
            # is the branch above; anything else sends to nobody rather than
            # guessing a list.
            pool = []
        else:
            pool = lists["invite_all"] if who == "all" else lists["invite"]
        request.session["event_mail"] = _send(
            db, ev, pool, kind, base_url(request), pay_by=pay_by)
        return RedirectResponse(f"/events/{eid}", status_code=303)

    @app.get("/events/{eid}/preview/{kind}", response_class=HTMLResponse)
    def event_preview(request: Request, eid: int, kind: str,
                      db: Session = Depends(get_db)):
        """The email as it will arrive, built from a real person on the list.

        A preview off dummy data proves the template renders; it does not tell
        you whether the sentence about where somebody got to is the right
        sentence, which is the only thing worth checking before you send this
        one to twenty people.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        # Every template, not only the ones this event can send. A preview is
        # a look at wording and harms nobody, and somebody deciding whether to
        # switch a template on wants to read it first.
        if not ev or kind not in {t["key"] for t in sendable_templates(ev)}:
            return RedirectResponse(f"/events/{eid}", status_code=303)
        lists = mail_lists(ev)
        # Whoever this email is most about, so the preview shows the sentences
        # that actually vary. Falls through to anybody on the list rather than
        # refusing: a preview off the wrong person still shows the wording.
        pool = {"finish": lists["unfinished_all"],
                "reel": lists["reel_all"],
                "lastcall": lists["lastcall_all"],
                "payby": lists["payby_all"],
                "heat": lists["heat_all"] or lists["heat"],
                # Same pool: both are previews of a heat time, and the only
                # person worth showing either against is somebody who has one.
                "heatnew": lists["heat_all"] or lists["heat"],
                "returned": [p for p in ev.participants
                             if (p.review_note or "").strip()]}.get(
                    kind, lists["invite_all"])
        who = pool[0] if pool else (ev.participants[0] if ev.participants else None)
        if who is None:
            return HTMLResponse(
                "<p style='font:15px system-ui;padding:28px'>Nobody on the list "
                "to preview against yet.</p>")
        base = base_url(request)
        if kind in ("finish", "returned", "payby"):
            url = "%s/r/%s/%s" % (base, ev.slug, who.token)
        elif kind in ("heat", "heatnew", "pass"):
            url = "%s/e/%s/pass" % (base, who.token)
        else:
            url = "%s/e/%s" % (base, who.token)
        build = {"reel": _reel_mail, "finish": _finish_mail,
                 "returned": _returned_mail,
                 "lastcall": _lastcall_mail, "payby": _payby_mail,
                 "reconfirm": _reconfirm_mail,
                 "cancelled": _cancelled_mail, "heatnew": _heat_new_mail,
                 "thanks": _thanks_mail,
                 "heat": _heat_mail}.get(kind, _invite_mail)
        # A preview that shows a different email from the one that would
        # actually go out is worse than no preview: it is the page you check
        # *before* sending to thirty-eight people.
        if kind == "heat" and heat_kind(ev, who) == "heatnew":
            build = _heat_new_mail
        subject, _text, html = build(db, ev, who, url)
        # The inline marks are Content-IDs in a real message; a browser needs
        # the routes instead, so the preview swaps them.
        # The same file the message inlines, so the preview is not a
        # different picture from the one that actually goes out.
        html = html.replace("cid:%s" % LOGO_CID, "/static/email-logo.png")
        html = html.replace("cid:%s" % SPONSOR_CID,
                            "/events/%d/sponsor-logo" % ev.id)
        html = html.replace("cid:%s" % BANNER_CID,
                            "/events/%d/banner" % ev.id)
        banner = (
            "<div style=\"font:14px system-ui;background:#1a232e;color:#fff;"
            "padding:11px 16px\">Preview &middot; <b>%s</b> &middot; as %s "
            "would receive it. Nothing has been sent.</div>"
            % (_esc(subject), _esc(who.full_name or who.name)))
        return HTMLResponse(banner + html)

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
        url = ("%s/r/%s/%s" % (base, ev.slug, p.token) if p.registering
               else "%s/e/%s/pass" % (base, p.token))
        subject, text, html = _pass_mail(db, ev, p, url)
        inline = {PASS_CID: qr_png("%s/i/%s" % (base, p.token))}
        logo = _logo_bytes()
        if logo:
            inline[LOGO_CID] = logo
        if ev.sponsor_logo:
            inline[SPONSOR_CID] = ev.sponsor_logo
        if ev.banner:
            inline[BANNER_CID] = ev.banner
        try:
            ok, _ = mailer.send(p.email, subject, text, html=html, inline=inline)
        except Exception:
            ok = False
        if ok:
            p.pass_email_at = datetime.now(timezone.utc)
            db.commit()

    def _send_signup(db, ev, p, base):
        """"We've got it" — the moment somebody finishes the form.

        Best effort, and never in the way of a registration: the row is
        already written by the time we get here, and a mail server having a
        bad minute must not turn a completed sign-up into an error page.

        Stamped so it goes exactly once. Somebody who comes back to their link
        and resubmits is correcting themselves, not registering again.

        Only for a registration with something to pay. A free one is already
        in, and gets the pass instead - telling somebody to go and pay for a
        class that costs nothing is worse than saying nothing at all.
        """
        if p.signup_email_at or not looks_like_email(p.email or ""):
            return
        mailer = Mailer()
        if not mailer.cfg.configured:
            return
        url = "%s/r/%s/%s" % (base, ev.slug, p.token)
        subject, text, html = _finish_mail(db, ev, p, url)
        inline = {}
        logo = _logo_bytes()
        if logo:
            inline[LOGO_CID] = logo
        if ev.sponsor_logo:
            inline[SPONSOR_CID] = ev.sponsor_logo
        if ev.banner:
            inline[BANNER_CID] = ev.banner
        try:
            ok, _ = mailer.send(p.email, subject, text, html=html,
                                inline=inline or None)
        except Exception:
            ok = False
        if ok:
            p.signup_email_at = datetime.now(timezone.utc)
            db.commit()

    def _send_returned(db, ev, p, base):
        """Best effort. A failed email must not undo a review you already made."""
        if not looks_like_email(p.email or ""):
            return
        mailer = Mailer()
        if not mailer.cfg.configured:
            return
        url = "%s/r/%s/%s" % (base, ev.slug, p.token)
        subject, text, html = _returned_mail(db, ev, p, url)
        inline = {}
        logo = _logo_bytes()
        if logo:
            inline[LOGO_CID] = logo
        if ev.sponsor_logo:
            inline[SPONSOR_CID] = ev.sponsor_logo
        if ev.banner:
            inline[BANNER_CID] = ev.banner
        try:
            mailer.send(p.email, subject, text, html=html, inline=inline or None)
        except Exception:
            pass

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

    @app.get("/events/{eid}/banner")
    def event_banner(eid: int, db: Session = Depends(get_db)):
        """The event's header image. Public, like the sponsor's mark: it is the
        top of an email anybody on the list already has in their inbox."""
        ev = db.get(Event, eid)
        if not ev or not ev.banner:
            return Response(status_code=404)
        return Response(content=ev.banner,
                        media_type=ev.banner_mime or "image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    # --------------------------------------------------- organiser roster ----
    #
    # The people who paid for the class want to see who is coming. Emailing a
    # spreadsheet each time is how the spreadsheet ends up three days stale in
    # somebody's downloads folder, so they get a URL.
    #
    # Two guards, for two different failures. The token stops it being found.
    # The password stops a forwarded link working for whoever it was forwarded
    # to — which is the likely one, because sponsors forward things. Neither is
    # enough alone, which is why both are here and why the password is hashed
    # rather than kept where a database dump would read it.

    #: Failed unlock attempts, in memory, per token. A brake rather than a
    #: lock: it resets on restart and a determined attacker can wait it out.
    #: What actually makes guessing impractical is PBKDF2 at 120k iterations —
    #: this just stops a script trying a dictionary in one afternoon.
    _org_fails: dict = {}
    ORG_MAX_TRIES = 8
    ORG_COOLDOWN = timedelta(minutes=10)

    def _org_blocked(token: str, now):
        n, since = _org_fails.get(token, (0, now))
        if now - since > ORG_COOLDOWN:
            _org_fails.pop(token, None)
            return None
        if n >= ORG_MAX_TRIES:
            return ORG_COOLDOWN - (now - since)
        return None

    def _org_failed(token: str, now):
        n, since = _org_fails.get(token, (0, now))
        if now - since > ORG_COOLDOWN:
            n, since = 0, now
        _org_fails[token] = (n + 1, since)

    def _org_link(db, token: str):
        return (db.query(EventOrganiserLink)
                .filter(EventOrganiserLink.token == token).first())

    def _org_links_for(db, eid: int):
        return (db.query(EventOrganiserLink)
                .filter(EventOrganiserLink.event_id == eid)
                .order_by(EventOrganiserLink.id.desc()).all())

    def _org_current(db, eid: int):
        return next((l for l in _org_links_for(db, eid) if l.is_live), None)

    def _org_unlocked(request, token: str) -> bool:
        return token in (request.session.get("org_ok") or [])

    def _org_unlock(request, token: str):
        # Kept in the signed session cookie, so the password is typed once per
        # browser rather than on every page of the roster.
        have = list(request.session.get("org_ok") or [])
        if token not in have:
            have.append(token)
            request.session["org_ok"] = have[-8:]

    def _org_roster(ev) -> dict:
        """Who is coming, as an organiser needs to read it.

        Two groups are left off, for the same reason: they are not in the room.

        Somebody who said no is not a participant, and listing them gives a
        sponsor a headline number they then have to mentally correct. Somebody
        whose confirmation window lapsed is the same story told differently —
        their slot has already gone to the next person, so showing them would
        double-count the seat.

        The waitlist is kept apart rather than dropped, because those people
        may yet be in the room and a sponsor deciding on catering wants to
        know they exist.
        """
        def row(p):
            return {
                "name": p.full_name or p.name or "",
                "email": (p.email or "").strip(),
                "handle": p.handle,
                "status": "Confirmed" if p.confirmed else "Awaiting reply",
                "confirmed": p.confirmed,
                "arrived": bool(p.arrived_at),
                "posted": bool(p.reel_url),
            }

        def coming(p):
            return not p.waitlist and not p.declined and not p.released_at

        live = [p for p in ev.participants if coming(p)]
        waiting = [p for p in ev.participants if p.waitlist]
        rows = sorted((row(p) for p in live),
                      key=lambda r: (not r["confirmed"], r["name"].lower()))
        return {
            "rows": rows,
            "waitlist": sorted((row(p) for p in waiting),
                               key=lambda r: r["name"].lower()),
            "confirmed": sum(1 for r in rows if r["confirmed"]),
            "handles": sum(1 for r in rows if r["handle"]),
            "arrived": sum(1 for r in rows if r["arrived"]),
        }

    def _org_gone(request, reason: str, code: int = 410):
        return templates.TemplateResponse(
            "event_gone.html", {"request": request, "reason": reason},
            status_code=code)

    @app.get("/o/{token}", response_class=HTMLResponse)
    def organiser_roster(request: Request, token: str,
                         db: Session = Depends(get_db)):
        """The sponsor's own view of who is coming."""
        link = _org_link(db, token)
        # An unknown token is a 404 and says nothing else. Telling a stranger
        # that a link "expired" confirms it once existed.
        if not link:
            return _org_gone(request, "unknown", 404)
        if link.revoked_at:
            return _org_gone(request, "revoked")
        if link.is_expired:
            return _org_gone(request, "expired")
        ev = link.event
        if not ev:
            return _org_gone(request, "unknown", 404)
        if not _org_unlocked(request, token):
            return templates.TemplateResponse(
                "organiser_gate.html",
                {"request": request, "ev": ev, "link": link, "error": None,
                 "wait": _org_blocked(token, datetime.now(timezone.utc))})
        now = datetime.now(timezone.utc)
        link.opens = (link.opens or 0) + 1
        link.first_opened_at = link.first_opened_at or now
        link.last_opened_at = now
        db.commit()
        return templates.TemplateResponse(
            "organiser_roster.html",
            {"request": request, "ev": ev, "link": link, "token": token,
             "counts": counts(ev), **_org_roster(ev)})

    @app.post("/o/{token}")
    def organiser_unlock(request: Request, token: str,
                         password: str = Form(""),
                         db: Session = Depends(get_db)):
        link = _org_link(db, token)
        if not link:
            return _org_gone(request, "unknown", 404)
        if not link.is_live:
            return _org_gone(request, "revoked" if link.revoked_at else "expired")
        now = datetime.now(timezone.utc)
        wait = _org_blocked(token, now)
        if wait is None and link.pass_hash and verify_pin(
                password or "", link.pass_hash, link.pass_salt or ""):
            _org_fails.pop(token, None)
            _org_unlock(request, token)
            return RedirectResponse("/o/%s" % token, status_code=303)
        if wait is None:
            _org_failed(token, now)
            wait = _org_blocked(token, now)
        return templates.TemplateResponse(
            "organiser_gate.html",
            {"request": request, "ev": link.event, "link": link, "wait": wait,
             "error": "That password is not right."},
            status_code=401)

    @app.get("/o/{token}/roster.csv")
    def organiser_csv(request: Request, token: str,
                      db: Session = Depends(get_db)):
        link = _org_link(db, token)
        if not link or not link.is_live or not _org_unlocked(request, token):
            return RedirectResponse("/o/%s" % token, status_code=303)
        ev = link.event
        data = _org_roster(ev)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Name", "Email", "Instagram", "Status"])
        for r in data["rows"] + data["waitlist"]:
            w.writerow([r["name"], r["email"], r["handle"], r["status"]])
        return Response(buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition":
            'attachment; filename="%s-participants.csv"' % ev.slug})

    @app.post("/events/{eid}/organiser-link")
    def organiser_link_new(request: Request, eid: int,
                           password: str = Form(""), label: str = Form(""),
                           db: Session = Depends(get_db)):
        """Mint a link, replacing whatever was live before.

        Replacing revokes rather than deletes, so a sponsor opening an older
        email is told a newer one was sent. Admin only: this hands somebody
        else a list of other people's email addresses.
        """
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        ev = db.get(Event, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        now = datetime.now(timezone.utc)
        for old in _org_links_for(db, eid):
            if old.is_live:
                old.revoked_at = now
        raw = (password or "").strip() or ORGANISER_DEFAULT_PASS
        h, salt = hash_pin(raw)
        link = EventOrganiserLink(
            event_id=eid, token=new_token(),
            label=(label or "").strip() or ev.sponsor or None,
            pass_hash=h, pass_salt=salt, created_by_id=staff.id,
            # Both stamped from the same instant. Letting the column default
            # fill created_at a few milliseconds later makes "expires in 60
            # days" arrive as 59 days and change, which is the sort of detail
            # that only ever surfaces in an argument about a link.
            created_at=now, expires_at=now + timedelta(days=ORGANISER_LINK_DAYS))
        db.add(link)
        db.commit()
        # The password is echoed back once, here, because it is the only
        # moment anybody can read it — the row keeps a hash. If she loses it
        # the answer is a new link, not a recovery.
        request.session["org_new"] = {"token": link.token, "password": raw}
        return RedirectResponse("/events/%d?tab=organiser" % eid, status_code=303)

    @app.post("/events/{eid}/organiser-link/revoke")
    def organiser_link_revoke(request: Request, eid: int,
                              db: Session = Depends(get_db)):
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        now = datetime.now(timezone.utc)
        for link in _org_links_for(db, eid):
            if link.is_live:
                link.revoked_at = now
        db.commit()
        return RedirectResponse("/events/%d?tab=organiser" % eid, status_code=303)

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
    """A stored instant, written as the clock on the gym wall reads it."""
    dt = to_local(_aware(dt))
    return dt.strftime("%a %d %b, %I:%M %p").replace(" 0", " ") if dt else ""


def _facts(ev, p=None) -> str:
    """When · Where · Bring · After.

    "When" is dropped for anybody who has a heat time. That row carries the
    event's blanket start — 10:00 AM for everybody — and printing it under a
    personal heat of 10:20 gives one email two different answers to the only
    question it exists to settle. Their own time is two inches higher up, in
    larger type, and is the one they should act on.
    """
    when = "" if (p is not None and p.heat_time) else " — ".join(
        x for x in (ev.when_text, ev.when_note) if x)
    rows = [("When", when),
            ("Where", ev.venue), ("Bring", ev.bring), ("After", ev.perk)]
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


def _warn_note(html) -> str:
    """The same callout, in amber, for the one email you hope never to send.

    _window_note is deliberately neutral because "we're holding your slot" is
    not a warning. A cancellation is: the reader has to change their morning,
    and the line that tells them so should be the first thing their eye lands
    on. Amber is the only place in these emails that colour carries meaning,
    which is exactly why it is kept for this.
    """
    return ('<table width="100%%" cellpadding="0" cellspacing="0" '
            'style="margin:18px 0 20px"><tr>'
            '<td width="3" style="background:#e0a010;border-radius:2px 0 0 2px"></td>'
            '<td style="background:#fdf5e0;padding:13px 16px;font-size:14px;'
            'color:#5c4708;border-radius:0 8px 8px 0">%s</td></tr></table>'
            % html)


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


# --------------------------------------------------------- the wording ----
# Every email is now assembled the same way: work out the values and the
# blocks for this person, then hand them to whatever source is stored for
# that template — theirs if somebody has edited it under Settings, ours if
# not. One path, so an edited email cannot take a different route through
# the code from an unedited one.

def _mail_values(ev, p, url, key) -> dict:
    """Everything ${...} can stand for, for this person, on this email."""
    first = ((p.first_name or "").strip()
             or ((p.name or "").split()[0] if p.name else "")
             or "there")
    v = {
        "event.name": ev.name or "",
        "event.when": ev.when_text,
        "event.venue": ev.venue or "",
        "event.sponsor": ev.sponsor or "Our sponsor",
        # The stand-in above is what an email says when a sponsor exists but
        # nobody typed the name. It is never empty, so it cannot be tested —
        # this is the one that answers "is there a sponsor at all?", which is
        # a different question and the one a sentence about them depends on.
        "event.has_sponsor": "1" if (ev.sponsor or "").strip() else "",
        "event.bring": ev.bring or "",
        "event.perk": ev.perk or "",
        "event.closes": ev.closes_text,
        "event.handles": " and ".join(ev.handle_list),
        "event.hashtag": ev.hashtag or "",
        # Empty when no offer has been set, which is what makes the whole
        # bottom half of the thank-you disappear rather than promise nothing.
        "event.reward": ev.reward_text,
        "event.reward_by": ev.reward_by_text,
        "record.name": p.full_name or p.name or "",
        "record.first_name": first,
        "record.email": p.email or "",
        "record.link": url,
        "record.rate": ev.rate_label(p.tier),
        "record.amount": money(p.amount),
        "record.deadline": _fmt_when(confirm_deadline(p)),
        # Empty on an event that has never moved, which is the signal the
        # "date has changed" email should not be going out at all.
        "event.was": _fmt_when(ev.moved_from) if ev.moved_from else "",
        "event.moved_why": ev.moved_why or "",
        # Not yet sent means not yet stamped, and a preview reading "Pay by ."
        # would be worse than useless. What it will be is the honest stand-in.
        "record.pay_deadline": _fmt_when(
            p.pay_due_at or (datetime.now(timezone.utc)
                             + timedelta(hours=PAY_GRACE_HOURS))),
        "record.reel_deadline": _fmt_when(ev.reel_deadline),
        "record.review_note": p.review_note or "",
        "record.heat": clock12(p.heat_time),
        "record.arrive": clock12(arrive_at(ev, p.heat_time)) if p.heat_time else "",
        "record.arrive_gap": gap_text(ev.heat_arrive or 0),
    }
    if key == "finish":
        v["record.headline"], v["record.lede"] = _finish_lines(ev, p, first)
    return v

def _finish_lines(ev, p, first) -> tuple:
    """The two sentences that depend on how far somebody actually got.

    Computed rather than typed, because the whole point of this email is
    that it does not tell half its readers to do something they did last
    week. Editable as ${record.headline} and ${record.lede} — you choose
    where they sit, we choose which of the two they are.
    """
    done = bool(p.external_done_at)
    outside = ev.external_label or "the organiser's site"
    if ev.external_url and not done:
        return ("You started, %s — but you're not done" % first,
                "We have your details. There are two things left, and the "
                "first one happens on somebody else's site.")
    if ev.external_url:
        return ("You're nearly there, %s" % first,
                "You've registered with %s — the last thing is paying and "
                "sending us the receipt." % outside)
    return ("You started, %s — but you're not done" % first,
            "We have your details. All that's left is paying and sending "
            "us the receipt.")

def _checklist(ev, p) -> str:
    """Three lines saying what is done and what isn't."""
    done = bool(p.external_done_at)
    steps = [(True, "Your details", "in")]
    if ev.external_url:
        steps.append((done, "Register with %s"
                      % (ev.external_label or "the organiser's site"),
                      "done" if done else "still to do"))
    steps.append((False, "Pay %s and send the receipt"
                  % (money(p.amount) or "the fee"), "still to do"))
    rows = "".join(
        '<tr><td style="font-size:14px;padding:5px 10px 5px 0;width:22px;'
        'color:%s">%s</td><td style="font-size:14px;color:%s">%s</td>'
        '<td style="font-size:13px;color:#8a939c;text-align:right;'
        'white-space:nowrap">%s</td></tr>'
        % (TEAL if ok else "#c9ced4", "&#10003;" if ok else "&#9675;",
           "#8a939c" if ok else INK, _esc(label), _esc(note))
        for ok, label, note in steps)
    return ('<table width="100%%" cellpadding="0" cellspacing="0" '
            'style="border:1px solid #e4e8ed;border-radius:10px;'
            'padding:14px 16px;margin:0 0 18px">%s</table>' % rows)

def _qr_block(ev, p) -> str:
    return ('<table width="100%%" cellpadding="0" cellspacing="0"><tr><td '
            'align="center" style="border:1px solid #e4e8ed;'
            'border-radius:12px;padding:18px 18px 14px">'
            '<img src="cid:%s" alt="Your check-in code" width="200" '
            'style="display:block;width:200px;height:200px">'
            '<div style="font-size:15px;font-weight:650;margin-top:10px">%s</div>'
            '<div style="font-size:12px;color:#6b7683">%s</div>'
            '</td></tr></table>'
            % (PASS_CID, _esc(p.full_name or p.name), _esc(ev.name)))

def _heat_block(ev, p) -> str:
    """The two times, side by side, arrival first and in teal.

    Arrival leads because it is the only one they have to act on; the heat
    time is the reason for it. A table rather than flexbox because half of
    these are read in Outlook.
    """
    if not p.heat_time:
        return ""
    def cell(label, value, sub, colour):
        return ('<td style="padding:0 22px 0 0;vertical-align:top">'
                '<div style="font-size:11px;text-transform:uppercase;'
                'letter-spacing:.08em;color:#6b7683;font-weight:600">%s</div>'
                '<div style="font-size:23px;font-weight:650;color:%s;'
                'line-height:1.15">%s</div>'
                '<div style="font-size:12px;color:#6b7683">%s</div></td>'
                % (_esc(label), colour, _esc(value), _esc(sub)))
    return ('<table cellpadding="0" cellspacing="0" style="background:#f3f5f7;'
            'border-radius:8px;padding:16px 18px;width:100%%;margin:0 0 18px">'
            '<tr>%s%s</tr></table>'
            % (cell("Arrive by", clock12(arrive_at(ev, p.heat_time)),
                    "Check in & warm up", "#008080"),
               cell("Your heat", clock12(p.heat_time), _heat_day(ev),
                    "#1a232e")))


def _heat_day(ev) -> str:
    """The date under somebody's heat time — the date, and not the event's
    own start time.

    ``when_text`` is "Sat 22 Aug, 10:00 AM", and printing that under a heat of
    10:20 puts two different times in one box.
    """
    if not ev.starts_at:
        return ""
    return to_local(ev.starts_at).strftime("%a %d %b").replace(" 0", " ")


def _moved_block(ev) -> str:
    """The old day and the new one, one above the other.

    Struck through rather than removed, and labelled Was and Now rather than
    left to be inferred. A rescheduled date printed on its own is read as
    confirmation of the date already in somebody's calendar - the two have to
    be in the same glance for the change to register at all.

    Nothing at all if the event has not moved, so an email sent by mistake
    reads as odd rather than as a date that has gone missing.
    """
    if not ev.moved_from:
        return ""
    return (
        '<table style="background:#f3f5f7;border-radius:8px;width:100%%;'
        'margin:0 0 18px"><tbody>'
        '<tr><td style="color:#6b7683;font-size:14px;padding:15px 12px 4px 17px;'
        'width:56px">Was</td><td style="font-size:14px;color:#8a939c;'
        'text-decoration:line-through;padding:15px 17px 4px 0">%s</td></tr>'
        '<tr><td style="color:#6b7683;font-size:14px;padding:4px 12px 15px 17px">'
        'Now</td><td style="font-size:14px;font-weight:700;color:#008080;'
        'padding:4px 17px 15px 0">%s</td></tr>'
        '</tbody></table>'
        % (_esc(_fmt_when(ev.moved_from)), _esc(ev.when_text or "")))


def _stars_block(inner) -> str:
    """Five stars, then your sentence.

    The stars are the picture, not the ask: the words underneath say "a
    review", never "a five-star review". Asking for the rating rather than the
    opinion is what gets a listing's reviews filtered, and it buys nothing —
    somebody who liked the class was going to give five anyway.
    """
    return ('<table width="100%%" style="margin:2px 0 0"><tr><td align="center">'
            '<div style="font-size:30px;letter-spacing:7px;color:#f5a623;'
            'line-height:1.15">&#9733;&#9733;&#9733;&#9733;&#9733;</div>'
            '<div style="font-size:15px;color:#2b3642;margin-top:12px">%s</div>'
            '</td></tr></table>' % inner)


def _review_button(db, label, sub) -> str:
    """The button, pointed at the gym's review link.

    Nothing at all when no link has been set. A button that opens nowhere is
    worse than no button: the person taps it, gets a broken page, and the one
    thing you asked them to do is now the thing that went wrong.
    """
    url = ((db.get(PaymentSetting, 1) or PaymentSetting()).review_url or "").strip()
    return _button(url, label, sub) if url else ""


def _voucher_block(ev, inner) -> str:
    """The offer, with the numbers from the event and the words from you.

    Drawn only when there is an amount. The alternative — an empty box, or a
    box reading "&#8369;0 off" — is an email that promises something the front
    desk then has to refuse.
    """
    if not ev.reward_text:
        return ""
    till = ('<div style="font-size:12px;color:#6b7683;margin-top:9px">Claim by %s</div>'
            % _esc(ev.reward_by_text)) if ev.reward_by_text else ""
    return (
        '<table width="100%%" cellpadding="0" cellspacing="0" style="margin:4px 0 0">'
        '<tr><td align="center" style="border:1px dashed %s;background:%s;'
        'border-radius:10px;padding:18px 20px">'
        '<div style="display:inline-block;background:%s;color:#fff;font-size:10px;'
        'font-weight:800;letter-spacing:1.8px;text-transform:uppercase;'
        'border-radius:4px;padding:4px 10px;margin-bottom:11px">For reviewers</div>'
        '<div style="font-size:27px;font-weight:800;color:#006a6a;'
        'letter-spacing:-.02em">%s off</div>'
        '<div style="font-size:14px;color:#2b3642;margin-top:6px;line-height:1.55">%s</div>'
        '%s</td></tr></table>'
        % (TEAL, TEAL_TINT, TEAL, _esc(ev.reward_text), inner, till))


def _terms_line(inner) -> str:
    """The small print, and deliberately small.

    Centred under the box so it reads as a footnote to the offer rather than a
    paragraph of its own, and at the size print like this is read at — which is
    to say, when somebody is already standing at the desk arguing about it.
    """
    return ('<div style="font-size:9px;color:#9aa3ab;margin:10px 0 0;'
            'line-height:1.5;text-align:center">%s</div>' % inner)


def _mail_blocks(db, ev, p, url, key) -> tuple:
    """The pieces the system builds, and the one you wrap your own words in."""
    blocks = {
        "block.facts": lambda *a: _facts(ev, p),
        "block.button": lambda label="Open →", sub="": _button(url, label, sub),
        "block.rewards": lambda title="What you get": _rewards_block(ev, title),
        "block.checklist": lambda *a: _checklist(ev, p),
        "block.qr": lambda *a: _qr_block(ev, p),
        "block.heat": lambda *a: _heat_block(ev, p),
        "block.moved": lambda *a: _moved_block(ev),
        "block.review": lambda label="Write a review \u2192", sub="":
            _review_button(db, label, sub),
    }
    pairs = {"block.note": lambda inner, *a: _window_note(inner),
             "block.warn": lambda inner, *a: _warn_note(inner),
             "block.stars": lambda inner, *a: _stars_block(inner),
             "block.voucher": lambda inner, *a: _voucher_block(ev, inner),
             "block.terms": lambda inner, *a: _terms_line(inner)}
    return blocks, pairs

def _compose(db, ev, p, url, key, src=None) -> tuple:
    """(subject, text, html) for one email to one person."""
    values = _mail_values(ev, p, url, key)
    blocks, pairs = _mail_blocks(db, ev, p, url, key)
    subject, text, body = mail_templates.build(db, key, values, blocks, pairs,
                                               src=src)
    return subject, text, _shell(db, ev, body, src=src if key == "shell" else None)

def _invite_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "invite")


def _pass_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "pass")


def _finish_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "finish")


def _returned_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "returned")


def _thanks_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "thanks")


def _lastcall_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "lastcall")


def _reconfirm_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "reconfirm")


def _reel_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "reel")


def _payby_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "payby")


def heat_kind(ev, p) -> str:
    """Which of the two heat emails this person should get.

    "heatnew" if we have told them before, or if this event has told anybody
    before. The second half is what covers an event whose people have all been
    moved since — moving wipes the per-person stamp, so on its own that flag
    would quietly claim thirty-seven people had never heard from us.

    Biased towards "changed" on purpose. A new person told their time "has
    changed" is mildly odd and still gets the right time; somebody who was
    moved and told "Your heat time" decides it is the mail they already read.
    """
    return "heatnew" if (p.heat_told_before or ev.heat_sent_at) else "heat"


def _heat_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "heat")


def _heat_new_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "heatnew")


def _cancelled_mail(db, ev, p, url):
    return _compose(db, ev, p, url, "cancelled")


def _shell(db, ev, body, src=None) -> str:
    """The AWAKEN wrapper every event email sits inside.

    The document scaffolding is fixed — nobody needs to edit a doctype, and a
    broken one breaks every email at once. The three rows inside it are the
    wrapper template, so the header, the mark and the footer line are yours.

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
    # An event with its own banner shows it instead of the AWAKEN mark. The
    # mark is in the banner already — that is what a banner is — so the header
    # carries one logo rather than two.
    #
    # Two ways it can land, because the wrapper is editable. The shipped
    # wrapper has a full-bleed row for it; a wrapper somebody edited before
    # this existed has no such row, so `block.logo` hands back the banner too.
    # Inset on black rather than edge to edge, but never the wrong image.
    banner_img = ('<img src="cid:%s" alt="%s" width="560" style="display:block;'
                  'width:100%%;max-width:560px;height:auto;border:0">'
                  % (BANNER_CID, _esc(ev.name or "AWAKEN"))) if ev.banner else ""
    # A banner takes the whole header, so the sponsor would drop off the top of
    # the email entirely. They get their own black strip underneath it instead
    # — the same words, still above the fold, and the sponsor still named on
    # every message we send about their class.
    banner = banner_img + (
        '<table width="100%%" cellpadding="0" cellspacing="0"><tr>'
        '<td style="background:#14171a;padding:4px 30px 20px;text-align:center">'
        '%s</td></tr></table>' % sponsor_bar if banner_img and sponsor_bar else "")
    logo = banner_img or (
        '<img src="cid:%s" alt="AWAKEN" width="142" style="display:block;'
        'margin:0 auto;width:142px;height:auto">' % LOGO_CID)
    rows = mail_templates.build(
        db, "shell",
        {"event.banner": "1" if ev.banner else "",
         "event.plain_header": "" if ev.banner else "1",
         "event.name": ev.name or "", "event.when": ev.when_text,
         "event.venue": ev.venue or "", "event.sponsor": ev.sponsor or "",
         "event.bring": ev.bring or "", "event.perk": ev.perk or "",
         "event.closes": ev.closes_text,
         "event.handles": " and ".join(ev.handle_list),
         "event.hashtag": ev.hashtag or ""},
        {"block.logo": lambda *a: logo,
         "block.banner": lambda *a: banner,
         "block.sponsor": lambda *a: sponsor_bar,
         "block.body": lambda *a: body},
        {}, src=src)[2]
    return """<!DOCTYPE html><html><body style="margin:0;background:#f3f5f7;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  color:#1a232e;line-height:1.5">
<table width="100%%" cellpadding="0" cellspacing="0" style="background:#f3f5f7;padding:22px 12px">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;
  border-radius:10px;overflow:hidden">
%s
</table>
</td></tr></table></body></html>""" % rows


# ------------------------------------------------------ the editor's eye ----
# What the preview beside the editing box is rendered against. A stand-in
# rather than a real event on purpose: the examples match the ones in the token
# palette, so somebody dropping ${record.first_name} into a sentence sees the
# same Marc it said they would, on an evening when there may be no event on the
# system at all. Checking the wording against a real list is a different job,
# and the per-event preview already does it.

def _sample_pair():
    """A believable event and a believable person, neither of them saved."""
    now = datetime.now(timezone.utc)
    ev = Event(
        id=0, name="HYROX Foundation Class", slug="sample",
        sponsor="Kenny Rogers Roasters", status=EVENT_OPEN,
        starts_at=now + timedelta(days=5), venue="AWAKEN Gym · Metrowalk, Pasig",
        capacity=30, bring="Training gear, towel, water",
        perk="Kenny Rogers meal on us", handles="@awakengymph @kennyrogersph",
        hashtag="#FuelledByKennyRogers", mode=EVENT_OPEN,
        signup_closes=now + timedelta(days=3),
        external_url="https://hyrox.com/pft", external_label="HYROX",
        tier_a_label="Member", tier_a_price=Decimal("1500"),
        tier_b_label="Non-member", tier_b_price=Decimal("1800"),
        reward_a="HYROX PFT", reward_a_detail="23 August · use it at checkout",
        reward_a_value="20% off",
        reward_b="AWAKEN day pass", reward_b_detail="Any day, any time",
        reward_b_value="Free",
        confirm_hours=48, reel_hours=48, review_hours=24, code_prefix="KR",
    )
    # Two rates, unsaved like the rest of this, so a preview of the email can
    # say which one Marc picked.
    ev.rates = [EventRate(id=1, label="Member", amount=Decimal("1500"), position=0),
                EventRate(id=2, label="Non-member", amount=Decimal("1800"), position=1)]
    p = EventParticipant(
        id=0, name="Marc Damil", first_name="Marc", last_name="Damil",
        email="marc@example.com", token="sample", tier="1",
        amount=Decimal("1500"), review_note="The photo is too dark to read "
        "the amount.", invited_at=now - timedelta(hours=3),
    )
    p.event = ev
    return ev, p


def sample_sponsor(db):
    """A real sponsor logo for the stand-in event to wear, if there is one.

    The template editor's preview had the sponsor's name set but no artwork,
    so it drew the fallback text where a live send draws the mark. That made
    the preview quietly wrong about the one thing people check it for. The
    newest event that has a logo lends it — the picture in the editor is then
    the picture that goes out.
    """
    return (db.query(Event).filter(Event.sponsor_logo.isnot(None))
            .order_by(Event.id.desc()).first())


def sample_email(db, key, subject_src, body_src) -> tuple:
    """(subject, html) for one template source, rendered off the stand-in.

    Same renderer, same blocks and the same wrapper as a real send — a preview
    that took a shortcut would be a preview you could not trust, which is worse
    than no preview at all.
    """
    ev, p = _sample_pair()
    lender = sample_sponsor(db)
    if lender is not None:
        ev.sponsor = lender.sponsor or ev.sponsor
        ev.sponsor_logo = lender.sponsor_logo
        ev.sponsor_logo_mime = lender.sponsor_logo_mime
    url = "https://awakengym.com/e/7Kq2f9"
    src = (subject_src, body_src)
    if key == "shell":
        # The wrapper has nothing to wrap on its own, so it gets the shipped
        # invitation inside it — you are looking at the header and the footer.
        values = _mail_values(ev, p, url, "invite")
        blocks, pairs = _mail_blocks(db, ev, p, url, "invite")
        body = mail_templates.build(db, "invite", values, blocks, pairs)[2]
        return None, _shell(db, ev, body, src=src)
    subject, _text, html = _compose(db, ev, p, url, key, src=src)
    return subject, html
