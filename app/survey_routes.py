"""The market research survey: a public form, and the table it fills.

Registered from main.py via ``register(app, deps)`` like the other feature
modules, so this file never imports main.

Three decisions shape everything here:

* **The mobile number is the identity, not the email.** A second email address
  is free and instant; a second SIM costs money and, under PH SIM registration
  law, has to be registered. Numbers are normalised before anything is
  compared, because ``0917 123 4567`` and ``+639171234567`` are one person and
  a de-duplication that does not know that silently does nothing.

* **Being turned away is still an entry.** Somebody outside Metro Manila gets
  a row, and their number is spent. Without that they could simply answer the
  first question differently a minute later.

* **The browser is not trusted about anything that matters.** It sends the
  answers; the server decides whether they qualify, issues the referral code
  and counts the entries. A form that let the page declare its own outcome
  would be a form where the outcome is whatever somebody wants it to be.
"""

from __future__ import annotations

import csv
import io
import json
import io
import secrets

import qrcode
from datetime import datetime, timezone, timedelta

from fastapi import Depends, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_db
from .models import (
    SURVEY_OUT_AGE, SURVEY_OUT_AREA, SURVEY_REFERRAL_CAP, SurveyResponse,
    SurveySetting, SurveyAttempt, SURVEY_IP_WINDOW_HOURS,
    norm_ph_mobile, norm_email, now_utc, to_local, from_local,
)

#: The questions, in the order they are asked, as (payload key, column head).
#: The admin table and the CSV are both built from this one list, so adding a
#: question to the form is a one-line change here and nothing else.
QUESTIONS = [
    ("q1_lives",            "Lives"),
    ("q1_city",             "City"),
    ("q2_age",              "Age"),
    ("q3_gender",           "Gender"),
    ("q5_occupation",       "Occupation"),
    ("q5_other",            "Occupation (other)"),
    ("q6_where",            "Works/studies"),
    ("q6_city",             "Work city"),
    ("q6_area",             "Work area"),
    ("q7_travel",           "Will travel"),
    ("q8_parking",          "Parking"),
    ("q9_status",           "Today"),
    ("q10_activities",      "Activities"),
    ("q10_other",           "Activities (other)"),
    ("q11_frequency",       "How often"),
    ("q12_level",           "Level"),
    ("q13_budget",          "Spends now/month"),
    ("q13_willing",         "Would pay/month"),
    ("q14_gym",             "Current gym"),
    ("q14_other",           "Gym (other)"),
    ("q14_branch",          "Branch"),
    ("q15_why",             "Why that gym"),
    ("q15_other",           "Why (other)"),
    ("q16_goals",           "Goals"),
    ("q16_other",           "Goals (other)"),
    ("q17_services",        "Would use"),
    ("q17_other",           "Services (other)"),
    ("q18_amenities",       "Amenities"),
    ("q18_other",           "Amenities (other)"),
    ("q19_missing",         "Missing today"),
    ("q19_other",           "Missing (other)"),
    ("q20_days",            "Days"),
    ("q20_times",           "Times"),
    ("q21_arrangement",     "Arrangement"),
    ("q22_payment",         "Payment"),
    ("q23_barriers",        "Barriers"),
    ("q23_other",           "Barriers (other)"),
    ("q24_reason",          "Ever stopped, and why"),
    ("q24_other",           "Stopped (other)"),
    ("q25_keeps_going",     "Keeps them going"),
    ("q25_other",           "Keeps going (other)"),
    ("q26_awaken",          "Knows Awaken"),
    ("q27_found_via",       "Found via"),
    ("q27_other",           "Found via (other)"),
    ("q28_anything_else",   "Anything else"),
]


#: Questions whose answers are sentences, not options. Counting these would
#: produce a bar chart of one-per-person, so they are listed instead.
FREE_TEXT = {"q6_area", "q14_branch", "q28_anything_else"} | {
    k for k, _ in QUESTIONS if k.endswith("_other")}

#: Where each question belongs, so the page reads in the order it was asked
#: rather than as forty-six cards in a row.
SECTIONS = [
    ("Who answered",        ["q1_lives", "q1_city", "q2_age", "q3_gender",
                             "q5_occupation", "q6_where", "q6_city"]),
    ("Getting there",       ["q7_travel", "q8_parking"]),
    ("What they do now",    ["q9_status", "q10_activities", "q11_frequency",
                             "q12_level", "q13_budget", "q13_willing",
                             "q14_gym", "q15_why"]),
    ("What they want",      ["q16_goals", "q17_services",
                             "q18_amenities", "q19_missing"]),
    ("When and how",        ["q20_days", "q20_times", "q21_arrangement",
                             "q22_payment"]),
    ("Sticking with it",    ["q23_barriers", "q24_reason",
                             "q25_keeps_going"]),
    ("Awaken",              ["q26_awaken", "q27_found_via"]),
    ("In their words",      ["q19_other", "q23_other", "q6_area", "q14_branch",
                             "q28_anything_else"]),
]

LABELS = dict(QUESTIONS)


def count_one(answers, key):
    """How many people gave each answer, commonest first.

    Returns (answered, [(value, n), ...]). The denominator is people who
    answered *this* question, not everybody: a question only some people see
    would otherwise read as though most of the gym ignored it.
    """
    counts = {}
    answered = 0
    for a in answers:
        v = a.get(key)
        vals = ([str(x) for x in v if str(x).strip()] if isinstance(v, list)
                else ([str(v)] if v is not None and str(v).strip() else []))
        if vals:
            answered += 1
        for x in vals:
            counts[x] = counts.get(x, 0) + 1
    return answered, sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def free_text(answers, key):
    """The sentences, with identical ones folded together.

    Returns (total written, [(text, how many said it), ...]). Two people
    asking for the same thing is the single most useful signal an open box
    gives you, and a list that printed it twice would bury it.
    """
    seen = {}
    total = 0
    for a in answers:
        v = a.get(key)
        if v is None or not str(v).strip():
            continue
        total += 1
        t = " ".join(str(v).split())
        seen[t] = seen.get(t, 0) + 1
    out = sorted(seen.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    return total, out


def median_seconds(rows):
    xs = sorted(r.seconds_taken for r in rows if r.seconds_taken)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) // 2


#: No O/0 and no I/1: this gets read off a phone screen and typed by hand.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 8

#: What the survey does before anybody changes anything. Dates as plain ISO;
#: the form prints them, so the office moves a date here rather than in a
#: template and a release.
DEFAULTS = {
    "open": "1",
    "closes_on": "2026-10-25",
    "closes_time": "23:59",
    "announces_on": "2026-11-06",
    "announces_time": "15:00",
    "cap": str(SURVEY_REFERRAL_CAP),
}

#: The links worth handing out separately. Same questions on each, so if the
#: channels disagree the sample is skewed and you can see it - which a
#: self-reported "where did you find this?" never quite tells you.
CHANNELS = [
    ("", "The plain link"),
    ("desk", "Front desk / poster"),
    ("ig", "Instagram"),
    ("fb", "Facebook"),
    ("email", "Email"),
    ("condo", "Neighbourhood / condo"),
    ("event", "Event"),
]


def settings(db):
    """Every setting, with the defaults filled in for whatever is missing."""
    out = dict(DEFAULTS)
    for row in db.query(SurveySetting).all():
        if row.key in out:
            out[row.key] = row.value
    return out


def put(db, key, value):
    row = db.query(SurveySetting).filter(SurveySetting.key == key).first()
    if row is None:
        row = SurveySetting(key=key)
        db.add(row)
    row.value = value


def is_open(db):
    """Open only while the switch is on *and* the deadline has not passed.

    The switch alone was not enough. "Entries close Saturday 11:59pm" has to
    be true without somebody remembering to flip something at midnight, or
    the first late entry is a fairness problem in a prize draw with a permit
    behind it.
    """
    cfg = settings(db)
    if cfg.get("open") == "0":
        return False
    ends = deadline(cfg.get("closes_on"), cfg.get("closes_time"))
    return ends is None or now_utc() < ends


def say_clock(hhmm):
    """``23:59`` as 11:59pm, and ``15:00`` as 3pm."""
    try:
        t = datetime.strptime(hhmm, "%H:%M").time()
    except (ValueError, TypeError):
        return ""
    hour = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return ("%d:%02d%s" % (hour, t.minute, suffix)) if t.minute else ("%d%s" % (hour, suffix))


def say_date(iso, long=False, at=None):
    """``2026-10-25`` as somebody would read it aloud, with the hour if given."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return iso or ""
    said = d.strftime("%A %-d %B %Y" if long else "%a %-d %B")
    clock = say_clock(at) if at else ""
    return ("%s, %s" % (said, clock)) if clock else said


def deadline(iso, hhmm):
    """The closing instant, read as gym time and returned as a real one."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
        t = datetime.strptime(hhmm or "23:59", "%H:%M").time()
    except (ValueError, TypeError):
        return None
    return from_local(datetime.combine(d, t))


def cap_of(db):
    try:
        return max(0, int(settings(db).get("cap") or 0))
    except ValueError:
        return SURVEY_REFERRAL_CAP


def public_link(request, ch=""):
    """The address to hand out. Built from the request so it is right on
    whatever host this is actually served from, rather than a constant that
    is wrong on staging."""
    base = str(request.base_url).rstrip("/") + "/survey"
    return base + ("?ch=" + ch if ch else "")


def _client_ip(request):
    """The address the request came from, honouring the proxy header.

    Railway terminates TLS in front of the app, so ``request.client`` is the
    proxy on every request and the real address is the first hop in
    X-Forwarded-For. Same reading waivers already use.
    """
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", None)


def new_code(db):
    """A code nobody else holds. Random, never derived from the number."""
    for _ in range(40):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if not db.query(SurveyResponse).filter(SurveyResponse.ref_code == code).first():
            return code
    raise RuntimeError("could not issue a referral code")


def decide(answers):
    """Does this entry qualify, and if not, why?

    Read from the answers on the server. The browser screens people out too,
    for their sake, but what it reports is a courtesy rather than a fact.
    """
    if (answers.get("q1_lives") or "") != "Within Metro Manila":
        return "screened_out", SURVEY_OUT_AREA
    if (answers.get("q2_age") or "") == "Under 18":
        return "screened_out", SURVEY_OUT_AGE
    return "complete", None


def entries_for(db, row, cap=None):
    """One entry, plus one per friend who finished on their code.

    Counted at read time rather than stored. A stored tally is a number that
    can be wrong, and this one is cheap: the draw is run once.
    """
    if row.is_out or not row.ref_code:
        return 0
    n = (db.query(func.count(SurveyResponse.id))
           .filter(SurveyResponse.referred_by == row.ref_code,
                   SurveyResponse.outcome == "complete")
           .scalar() or 0)
    cap = cap_of(db) if cap is None else cap
    if cap:
        n = min(n, cap)
    return 1 + n


def cell(value):
    """One answer as one piece of text, whatever shape it arrived in."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v) for v in value if str(v).strip())
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def register(app, deps):
    render = deps["render"]
    require = deps["require"]
    templates = deps["templates"]

    def guard(request, db):
        """Admins only. The table holds mobile numbers for everybody who
        answered, which is exactly the kind of list that should not be one
        permission tick away from the front desk."""
        return require(request, db, admin=True)

    # ---------------------------------------------------------------- public

    def ip_window(db):
        try:
            return max(0, int(settings(db).get("ip_hours") or SURVEY_IP_WINDOW_HOURS))
        except (TypeError, ValueError):
            return SURVEY_IP_WINDOW_HOURS

    def ip_recently_entered(db, ip, hours):
        """Did an entry come off this connection in the last few hours?

        Deliberately a window rather than a ban. One address can be a whole
        gym floor, an office or a carrier's mobile pool, so holding it
        forever would lock out everybody who came after the first person.
        A couple of hours catches the person going straight round again
        and lets the next honest one through this afternoon.
        """
        if not ip or hours <= 0:
            return None
        since = now_utc() - timedelta(hours=hours)
        return (db.query(SurveyResponse)
                  .filter(SurveyResponse.ip == ip,
                          SurveyResponse.created_at >= since)
                  .order_by(SurveyResponse.created_at.desc())
                  .first())

    def log_attempt(db, matched, *, mobile=None, email=None,
                    ip=None, user_agent=None):
        """Write down every refusal, so next time there is a record."""
        db.add(SurveyAttempt(matched=matched, mobile=mobile, email=email,
                             ip=ip, user_agent=(user_agent or "")[:400] or None))
        try:
            db.commit()
        except Exception:
            db.rollback()

    #: Nothing the survey serves may come out of a cache. The public page
    #: carries the questions, the wording and the answer to "has this
    #: connection already entered?"; the admin pages carry live counts and
    #: the list of refusals. A stale copy of either looks exactly like the
    #: duplicate check failing - old questions, old wording, a refusal that
    #: happened but is not on the page - and sends you hunting a bug that
    #: is not there. It cost an evening once.
    NO_CACHE = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    def fresh(response):
        for k, v in NO_CACHE.items():
            response.headers[k] = v
        return response

    @app.get("/survey", response_class=HTMLResponse)
    def survey_form(request: Request, ref: str = "", ch: str = "",
                    again: int = 0, db: Session = Depends(get_db)):
        """The form. No login — this is the whole point of it."""
        cfg = settings(db)
        # The deadline has to shut the door, not just the switch - otherwise
        # somebody answers twenty-eight questions after closing and is told
        # on the last screen.
        if not is_open(db):
            return templates.TemplateResponse("survey_closed.html", {
                "request": request,
                "announces": say_date(cfg.get("announces_on"), long=True,
                                      at=cfg.get("announces_time")),
            }, status_code=200, headers=NO_CACHE)
        known = ""
        if ref:
            hit = (db.query(SurveyResponse)
                     .filter(SurveyResponse.ref_code == ref.strip().upper())
                     .first())
            if hit is not None and not hit.is_out:
                known = hit.ref_code
        # Checked here rather than in the page: it needs no JavaScript, it
        # cannot be skipped, and it happens the instant the link is opened.
        hours = ip_window(db)
        seen = None if again else ip_recently_entered(db, _client_ip(request), hours)
        if seen is not None:
            log_attempt(db, "ip", ip=_client_ip(request),
                        user_agent=request.headers.get("user-agent"))

        base = str(request.base_url).rstrip("/")
        return templates.TemplateResponse("survey.html", {
            "request": request,
            "closes_short": say_date(cfg.get("closes_on"),
                                     at=cfg.get("closes_time")),
            "closes_long": say_date(cfg.get("closes_on"), long=True,
                                    at=cfg.get("closes_time")),
            "announces_short": say_date(cfg.get("announces_on"),
                                        at=cfg.get("announces_time")),
            "announces_long": say_date(cfg.get("announces_on"), long=True,
                                       at=cfg.get("announces_time")),
            "cfg": json.dumps({
                "share_base": base + "/survey",
                "cap": cap_of(db),
                "referred_by": known,
                "channel": (ch or "")[:24],
                "seen_here": seen is not None,
            }),
        }, headers=NO_CACHE)

    @app.post("/survey")
    async def survey_submit(request: Request, db: Session = Depends(get_db)):
        """One entry, complete or turned away.

        Everything that decides anything is worked out here. The page sends
        answers; it does not get a say in whether they qualify, what their
        code is, or how many entries they end up with.
        """
        if not is_open(db):
            return JSONResponse({"error": "closed"}, status_code=403)
        data = await request.json() or {}
        answers = data.get("answers") or {}
        person = data.get("person") or {}
        consent = data.get("consent") or {}
        dev = data.get("device") or {}

        mob = norm_ph_mobile(person.get("mobile_raw") or person.get("mobile"))
        if not mob:
            return JSONResponse({"error": "mobile"}, status_code=400)
        if not (consent.get("privacy") and consent.get("promo_rules")):
            return JSONResponse({"error": "consent"}, status_code=400)

        outcome, reason = decide(answers)

        ip = _client_ip(request)
        ua = dev.get("user_agent") or request.headers.get("user-agent")
        # The address is held for everybody, including the people we turn
        # away. Age is the reason: somebody under 18 cannot enter and cannot
        # claim, and if we forgot their address the same person comes back
        # with a second number and we would have no way to know.
        mail = norm_email(person.get("email"))

        # One entry per person, decided here and nowhere else. The page is
        # allowed to ask all the questions and press send; what it is not
        # allowed to do is be the thing that says yes.
        if db.query(SurveyResponse).filter(SurveyResponse.mobile == mob).first():
            log_attempt(db, "mobile", mobile=mob, email=mail, ip=ip, user_agent=ua)
            return JSONResponse({"error": "used"}, status_code=409)

        if mail and db.query(SurveyResponse).filter(
                SurveyResponse.email == mail).first():
            log_attempt(db, "email", mobile=mob, email=mail, ip=ip, user_agent=ua)
            return JSONResponse({"error": "email"}, status_code=409)

        # Only the screening answers are kept for somebody we turned away.
        # They did not consent to a market research record and we have no use
        # for one; the number is the only thing that has to survive.
        keep = answers if outcome == "complete" else {
            k: answers.get(k) for k in ("q1_lives", "q2_age")}

        came_from = (data.get("referral") or {}).get("referred_by") or ""
        came_from = came_from.strip().upper() or None
        if came_from:
            src = (db.query(SurveyResponse)
                     .filter(SurveyResponse.ref_code == came_from).first())
            if src is None or src.is_out:
                came_from = None

        row = SurveyResponse(
            outcome=outcome, reason=reason,
            mobile=mob, mobile_raw=(person.get("mobile_raw") or "")[:40] or None,
            nickname=(person.get("name") or "")[:80] or None if outcome == "complete" else None,
            email=mail,
            ref_code=new_code(db) if outcome == "complete" else None,
            referred_by=came_from,
            channel=((data.get("channel") or "")[:24] or None),
            answers=json.dumps(keep, ensure_ascii=False),
            consent_privacy=bool(consent.get("privacy")),
            consent_promo=bool(consent.get("promo_rules")),
            ip=ip,
            user_agent=(dev.get("user_agent") or "")[:400] or None,
            screen=(dev.get("screen") or "")[:40] or None,
            viewport=(dev.get("viewport") or "")[:40] or None,
            timezone_name=(dev.get("timezone") or "")[:60] or None,
            language=(dev.get("language") or "")[:40] or None,
            platform=(dev.get("platform") or "")[:60] or None,
            seconds_taken=int(data.get("seconds_taken") or 0) or None,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Two tabs, one number, submitted together. The first one won.
            db.rollback()
            dup = "used"
            if mail and db.query(SurveyResponse).filter(
                    SurveyResponse.email == mail,
                    SurveyResponse.mobile != mob).first():
                dup = "email"
            log_attempt(db, dup, mobile=mob, email=mail, ip=ip, user_agent=ua)
            return JSONResponse({"error": dup}, status_code=409)

        return JSONResponse({
            "outcome": outcome,
            "reason": reason or "",
            "code": row.ref_code or "",
            "entries": entries_for(db, row),
        })

    # ----------------------------------------------------------------- admin

    def _rows(db, show):
        q = db.query(SurveyResponse)
        if show == "complete":
            q = q.filter(SurveyResponse.outcome == "complete")
        elif show == "out":
            q = q.filter(SurveyResponse.outcome != "complete")
        return q.order_by(SurveyResponse.created_at.desc()).all()

    def _answers(row):
        try:
            return json.loads(row.answers or "{}")
        except ValueError:
            return {}

    @app.get("/admin/survey/qr.png")
    def survey_qr(request: Request, ch: str = "",
                  db: Session = Depends(get_db)):
        """The link as a square, for a poster or an Instagram story.

        Generated here rather than in the browser so it can be saved at a
        size that survives being printed and photographed off a wall.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        qr = qrcode.QRCode(box_size=14, border=3)
        qr.add_data(public_link(request, ch))
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image(fill_color="#0b2a26", back_color="white").save(buf, format="PNG")
        name = "awaken-survey%s.png" % (("-" + ch) if ch else "")
        return Response(buf.getvalue(), media_type="image/png",
                        headers={"Content-Disposition":
                                 'attachment; filename="%s"' % name})

    @app.post("/admin/survey/setup")
    async def survey_setup(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        form = await request.form()
        put(db, "open", "1" if form.get("open") else "0")
        for key in ("closes_on", "closes_time",
                    "announces_on", "announces_time"):
            val = (form.get(key) or "").strip()
            if val:
                put(db, key, val)
        cap = (form.get("cap") or "").strip()
        if cap.isdigit():
            put(db, "cap", cap)
        db.commit()
        return RedirectResponse("/admin/survey", status_code=303)

    def clusters(db):
        """Entries that share an address or a device.

        This is a review tool, not a gate. An IP address is a poor name for
        a person: gym wifi, an office network and a mobile carrier's NAT all
        put dozens of unrelated people behind one address, so blocking on it
        would turn away real respondents in order to stop one determined
        one. What it is good for is noticing that eleven entries arrived
        from one address on one phone in four minutes - which is a thing
        somebody should look at, and decide about, by hand.
        """
        out = []
        for label, col in (("Same network", SurveyResponse.ip),
                           ("Same device", SurveyResponse.user_agent)):
            groups = (db.query(col, func.count(SurveyResponse.id))
                        .filter(col.isnot(None), col != "")
                        .group_by(col)
                        .having(func.count(SurveyResponse.id) > 1)
                        .order_by(func.count(SurveyResponse.id).desc())
                        .limit(25).all())
            for value, n in groups:
                who = (db.query(SurveyResponse)
                         .filter(col == value)
                         .order_by(SurveyResponse.created_at).all())
                out.append({
                    "kind": label,
                    "value": (value[:70] + "\u2026") if len(value) > 70 else value,
                    "count": n,
                    "people": [{"id": r.id, "mobile": r.mobile,
                                "nickname": r.nickname or "",
                                "out": r.is_out,
                                "when": to_local(r.created_at)} for r in who],
                })
        return out

    def refusals(db, limit=40):
        rows = (db.query(SurveyAttempt)
                  .order_by(SurveyAttempt.created_at.desc())
                  .limit(limit).all())
        return [{"when": to_local(r.created_at), "matched": r.matched,
                 "mobile": r.mobile or "", "email": r.email or "",
                 "ip": r.ip or ""} for r in rows]

    @app.get("/admin/survey", response_class=HTMLResponse)
    def survey_list(request: Request, show: str = "complete",
                    db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        rows = _rows(db, show)
        tally = {
            "complete": db.query(func.count(SurveyResponse.id))
                          .filter(SurveyResponse.outcome == "complete").scalar() or 0,
            "out": db.query(func.count(SurveyResponse.id))
                     .filter(SurveyResponse.outcome != "complete").scalar() or 0,
        }
        packed = [{
            "row": r,
            "answers": _answers(r),
            "entries": entries_for(db, r),
            "when": to_local(r.created_at),
        } for r in rows]
        by_channel = [(label, code, db.query(func.count(SurveyResponse.id))
                       .filter(SurveyResponse.outcome == "complete",
                               SurveyResponse.channel == (code or None))
                       .scalar() or 0)
                      for code, label in CHANNELS]
        cfg = settings(db)
        return fresh(render(request, "survey_list.html", db, staff, active="survey",
                      rows=packed, questions=QUESTIONS, show=show, tally=tally,
                      cell=cell, cfg=cfg, channels=CHANNELS,
                      by_channel=by_channel, clusters=clusters(db),
                      refusals=refusals(db),
                      refused_total=db.query(func.count(SurveyAttempt.id)).scalar() or 0,
                      ip_hours=ip_window(db),
                      link=public_link(request, ""),
                      link_for=lambda c: public_link(request, c)))

    @app.get("/admin/survey/results", response_class=HTMLResponse)
    def survey_results(request: Request, ch: str = "",
                       db: Session = Depends(get_db)):
        """What the answers add up to.

        One question per block, commonest answer first. Deliberately plain
        bars rather than a wall of pie charts: the only thing anybody wants
        from a survey question is "how many said that, out of how many", and
        a bar whose length is the count answers it without being decoded.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        q = db.query(SurveyResponse).filter(SurveyResponse.outcome == "complete")
        if ch:
            q = q.filter(SurveyResponse.channel == (ch if ch != "-" else None))
        rows = q.all()
        answers = [_answers(r) for r in rows]
        total = len(rows)

        blocks, singles = [], []
        for heading, keys in SECTIONS:
            items = []
            for key in keys:
                label = LABELS.get(key, key)
                if key in FREE_TEXT:
                    wrote, said = free_text(answers, key)
                    if said:
                        items.append({"key": key, "label": label, "kind": "text",
                                      "said": said[:40], "more": max(0, len(said) - 40),
                                      "answered": wrote})
                    continue
                answered, counts = count_one(answers, key)
                if not counts:
                    continue
                # A single answer at 100% is not a chart - it is a fact, and
                # usually one the screening already guaranteed. Drawing one
                # full-width bar for it just teaches people to scroll past.
                if len(counts) < 2:
                    singles.append((label, counts[0][0], counts[0][1], answered))
                    continue
                top = counts[0][1]
                items.append({
                    "key": key, "label": label, "kind": "bars",
                    "answered": answered,
                    "multi": any(isinstance(a.get(key), list) for a in answers),
                    "rows": [{"value": v, "n": n,
                              "pct": round(100.0 * n / answered) if answered else 0,
                              "width": round(100.0 * n / top, 1) if top else 0}
                             for v, n in counts],
                })
            if items:
                blocks.append({"heading": heading, "cards": items})

        turned = (db.query(func.count(SurveyResponse.id))
                    .filter(SurveyResponse.outcome != "complete").scalar() or 0)
        med = median_seconds(rows)
        draw = sum(entries_for(db, r) for r in rows)
        return fresh(render(request, "survey_results.html", db, staff, active="survey",
                      blocks=blocks, singles=singles, total=total, turned=turned,
                      median_mins=(round(med / 60.0, 1) if med else None),
                      draw=draw, channels=CHANNELS, ch=ch,
                      by_channel=[(label, code,
                                   db.query(func.count(SurveyResponse.id))
                                     .filter(SurveyResponse.outcome == "complete",
                                             SurveyResponse.channel == (code or None))
                                     .scalar() or 0)
                                  for code, label in CHANNELS]))

    @app.get("/admin/survey.csv")
    def survey_csv(request: Request, show: str = "complete",
                   db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        buf = io.StringIO()
        w = csv.writer(buf)
        head = ["When", "Outcome", "Reason", "Nickname", "Mobile", "Email",
                "Entries", "Code", "Referred by", "Channel", "Seconds", "IP",
                "Device", "Screen", "Timezone"]
        w.writerow(head + [label for _, label in QUESTIONS])
        for r in _rows(db, show):
            a = _answers(r)
            when = to_local(r.created_at)
            w.writerow([
                when.strftime("%Y-%m-%d %H:%M") if when else "",
                r.outcome, r.reason or "", r.nickname or "", r.mobile,
                r.email or "", entries_for(db, r), r.ref_code or "",
                r.referred_by or "", r.channel or "", r.seconds_taken or "",
                r.ip or "",
                r.user_agent or "", r.screen or "", r.timezone_name or "",
            ] + [cell(a.get(k)) for k, _ in QUESTIONS])
        return Response(
            buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition":
                     'attachment; filename="awaken-survey.csv"'})

    @app.get("/admin/survey/{rid}", response_class=HTMLResponse)
    def survey_one(request: Request, rid: int, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        row = db.query(SurveyResponse).get(rid)
        if row is None:
            return fresh(render(request, "survey_list.html", db, staff, active="survey",
                          rows=[], questions=QUESTIONS, show="complete",
                          tally={"complete": 0, "out": 0}, cell=cell))
        return fresh(render(request, "survey_one.html", db, staff, active="survey",
                      row=row, answers=_answers(row), questions=QUESTIONS,
                      entries=entries_for(db, row), when=to_local(row.created_at),
                      cell=cell,
                      friends=(db.query(SurveyResponse)
                                 .filter(SurveyResponse.referred_by == (row.ref_code or "\x00"))
                                 .order_by(SurveyResponse.created_at).all())))
