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
import time

import qrcode
from datetime import datetime, timezone

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
    SurveySetting, norm_ph_mobile, now_utc, to_local,
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
    ("q10_activities",       "Activities"),
    ("q10_other",            "Activities (other)"),
    ("q11_frequency",       "How often"),
    ("q12_training_since",  "Training for"),
    ("q13_level",           "Level"),
    ("q14_budget",          "Budget/month"),
    ("q15_gym",             "Current gym"),
    ("q15_other",           "Gym (other)"),
    ("q15_branch",          "Branch"),
    ("q16_why",             "Why that gym"),
    ("q16_other",           "Why (other)"),
    ("q17_goals",           "Goals"),
    ("q17_other",           "Goals (other)"),
    ("q18_motivation",      "Would join for"),
    ("q18_other",           "Would join (other)"),
    ("q19_services",        "Would use"),
    ("q19_other",           "Services (other)"),
    ("q20_amenities",       "Amenities"),
    ("q20_other",           "Amenities (other)"),
    ("q21_missing",         "Missing today"),
    ("q21_other",           "Missing (other)"),
    ("q22_days",            "Days"),
    ("q22_times",           "Times"),
    ("q23_arrangement",     "Arrangement"),
    ("q24_payment",         "Payment"),
    ("q25_barriers",        "Barriers"),
    ("q25_other",           "Barriers (other)"),
    ("q26_cancelled",       "Ever cancelled"),
    ("q27_reason",          "Cancel reason"),
    ("q27_other",           "Cancel (other)"),
    ("q28_keeps_going",     "Keeps them going"),
    ("q28_other",           "Keeps going (other)"),
    ("q29_awaken",          "Knows Awaken"),
    ("q30_found_via",       "Found via"),
    ("q30_other",           "Found via (other)"),
    ("q31_anything_else",   "Anything else"),
]


#: Questions whose answers are sentences, not options. Counting these would
#: produce a bar chart of one-per-person, so they are listed instead.
FREE_TEXT = {"q6_area", "q15_branch", "q31_anything_else"} | {
    k for k, _ in QUESTIONS if k.endswith("_other")}

#: Where each question belongs, so the page reads in the order it was asked
#: rather than as forty-six cards in a row.
SECTIONS = [
    ("Who answered",        ["q1_lives", "q1_city", "q2_age", "q3_gender",
                             "q5_occupation", "q6_where", "q6_city"]),
    ("Getting there",       ["q7_travel", "q8_parking"]),
    ("What they do now",    ["q9_status", "q10_activities", "q11_frequency",
                             "q12_training_since", "q13_level", "q14_budget",
                             "q15_gym", "q16_why"]),
    ("What they want",      ["q17_goals", "q18_motivation", "q19_services",
                             "q20_amenities", "q21_missing"]),
    ("When and how",        ["q22_days", "q22_times", "q23_arrangement",
                             "q24_payment"]),
    ("Sticking with it",    ["q25_barriers", "q26_cancelled", "q27_reason",
                             "q28_keeps_going"]),
    ("Awaken",              ["q29_awaken", "q30_found_via"]),
    ("In their words",      ["q21_other", "q25_other", "q6_area", "q15_branch",
                             "q31_anything_else"]),
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

#: Checking a number before the survey is filled in saves somebody answering
#: thirty questions only to be told their number is spent. It also answers
#: "has this number entered?" to anybody who asks, so it is held to a few
#: tries per address per hour.
CHECK_PER_HOUR = 40
_checks = {}


#: What the survey does before anybody changes anything. Dates as plain ISO;
#: the form prints them, so the office moves a date here rather than in a
#: template and a release.
DEFAULTS = {
    "open": "1",
    "closes_on": "2026-10-25",
    "announces_on": "2026-11-06",
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
    return settings(db).get("open") != "0"


def say_date(iso, long=False):
    """``2026-10-25`` as somebody would read it aloud."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return iso or ""
    return d.strftime("%A %-d %B %Y" if long else "%a %-d %B")


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


def _rate_ok(ip):
    now = time.time()
    hits = [t for t in _checks.get(ip or "", []) if now - t < 3600]
    hits.append(now)
    _checks[ip or ""] = hits
    return len(hits) <= CHECK_PER_HOUR


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

    @app.get("/survey", response_class=HTMLResponse)
    def survey_form(request: Request, ref: str = "", ch: str = "",
                    db: Session = Depends(get_db)):
        """The form. No login — this is the whole point of it."""
        cfg = settings(db)
        if cfg.get("open") == "0":
            return templates.TemplateResponse("survey_closed.html", {
                "request": request,
                "announces": say_date(cfg.get("announces_on"), long=True),
            }, status_code=200)
        known = ""
        if ref:
            hit = (db.query(SurveyResponse)
                     .filter(SurveyResponse.ref_code == ref.strip().upper())
                     .first())
            if hit is not None and not hit.is_out:
                known = hit.ref_code
        base = str(request.base_url).rstrip("/")
        return templates.TemplateResponse("survey.html", {
            "request": request,
            "closes_short": say_date(cfg.get("closes_on")),
            "closes_long": say_date(cfg.get("closes_on"), long=True),
            "announces_short": say_date(cfg.get("announces_on")),
            "announces_long": say_date(cfg.get("announces_on"), long=True),
            "cfg": json.dumps({
                "share_base": base + "/survey",
                "cap": cap_of(db),
                "referred_by": known,
                "channel": (ch or "")[:24],
            }),
        })

    @app.post("/survey/check")
    async def survey_check(request: Request, db: Session = Depends(get_db)):
        """Has this number been used? Asked once, as section one is left.

        It does tell a caller whether a given number has entered, which is why
        it is rate limited. The alternative - finding out after thirty
        questions - is worse for the honest majority.
        """
        ip = _client_ip(request)
        if not _rate_ok(ip):
            return JSONResponse({"error": "slow down"}, status_code=429)
        data = await request.json()
        mob = norm_ph_mobile((data or {}).get("mobile"))
        if not mob:
            return JSONResponse({"ok": False, "why": "format"})
        used = db.query(SurveyResponse).filter(SurveyResponse.mobile == mob).first()
        return JSONResponse({"ok": used is None, "why": "used" if used else ""})

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

        if db.query(SurveyResponse).filter(SurveyResponse.mobile == mob).first():
            return JSONResponse({"error": "used"}, status_code=409)

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
            email=(person.get("email") or "")[:160].lower() or None if outcome == "complete" else None,
            ref_code=new_code(db) if outcome == "complete" else None,
            referred_by=came_from,
            channel=((data.get("channel") or "")[:24] or None),
            answers=json.dumps(keep, ensure_ascii=False),
            consent_privacy=bool(consent.get("privacy")),
            consent_promo=bool(consent.get("promo_rules")),
            ip=_client_ip(request),
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
            return JSONResponse({"error": "used"}, status_code=409)

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
        for key in ("closes_on", "announces_on"):
            val = (form.get(key) or "").strip()
            if val:
                put(db, key, val)
        cap = (form.get("cap") or "").strip()
        if cap.isdigit():
            put(db, "cap", cap)
        db.commit()
        return RedirectResponse("/admin/survey", status_code=303)

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
        return render(request, "survey_list.html", db, staff, active="survey",
                      rows=packed, questions=QUESTIONS, show=show, tally=tally,
                      cell=cell, cfg=cfg, channels=CHANNELS,
                      by_channel=by_channel,
                      link=public_link(request, ""),
                      link_for=lambda c: public_link(request, c))

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
        return render(request, "survey_results.html", db, staff, active="survey",
                      blocks=blocks, singles=singles, total=total, turned=turned,
                      median_mins=(round(med / 60.0, 1) if med else None),
                      draw=draw, channels=CHANNELS, ch=ch,
                      by_channel=[(label, code,
                                   db.query(func.count(SurveyResponse.id))
                                     .filter(SurveyResponse.outcome == "complete",
                                             SurveyResponse.channel == (code or None))
                                     .scalar() or 0)
                                  for code, label in CHANNELS])

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
            return render(request, "survey_list.html", db, staff, active="survey",
                          rows=[], questions=QUESTIONS, show="complete",
                          tally={"complete": 0, "out": 0}, cell=cell)
        return render(request, "survey_one.html", db, staff, active="survey",
                      row=row, answers=_answers(row), questions=QUESTIONS,
                      entries=entries_for(db, row), when=to_local(row.created_at),
                      cell=cell,
                      friends=(db.query(SurveyResponse)
                                 .filter(SurveyResponse.referred_by == (row.ref_code or "\x00"))
                                 .order_by(SurveyResponse.created_at).all()))
