"""The extra questions on an event's sign-up form.

Registered from main.py via ``register(app, deps)`` like the other feature
modules, so this file never imports main.

A Google Form, scoped to the one thing it is for. Add a question, pick a type,
say whether it is required, move it up or down.

The sign-up's own fields are in the same list. They have to be: "put the rate
on the first page" and "ask for the mobile after the shirt size" are ordinary
requests, and they are impossible if half the form is in the list and half is
nailed into the template. So each built-in field gets a row too, marked with
`builtin`, and the page draws the markup it always drew for it - a name field
is not a short-answer question, and pretending otherwise loses the browser's
autocomplete.

Three of them cannot be switched off: name, email, and the rate. Without the
first two there is nobody to email; without the third there is nothing to pay.
Everything else, built-in or not, is a question this gym happens to ask.

Answers land in their own table rather than a blob on the participant, so the
saved-reports feature can read them. "Shirt sizes by count" is then a report,
not a counting exercise.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .db import get_db
from .models import (BUILTIN_FIELDS, BUILTIN_KEYS, BUILTIN_LOCKED,
                     Event, EventParticipant, EventQuestion, EventRate,
                     ParticipantAnswer, QUESTION_KINDS, QUESTION_KIND_KEYS,
                     QUESTION_KINDS_WITH_OPTIONS, RATE_LOOKS, RATE_LOOK_KEYS,
                     MAPPABLE, MAP_LABELS, map_fits, to_local)


def clean_kind(raw: str) -> str:
    """A type we know, or the one that can hold anything."""
    want = (raw or "").strip().lower()
    return want if want in QUESTION_KIND_KEYS else "text"


def renumber(ev) -> None:
    """Positions back to 0,1,2… after anything moves or goes.

    Cheap, and it means nothing else in the system has to cope with a gap or a
    tie. A list of six questions is not a place to be clever about ordering.
    """
    for i, q in enumerate(sorted(ev.questions, key=lambda x: (x.position, x.id))):
        q.position = i


class Ghost:
    """A built-in field on an event whose form has never been opened.

    The rows get written the first time somebody opens the builder. Until
    then the sign-up page still has to draw six fields in the right order, so
    it draws these: the same shape, in the order the page has always used, and
    nothing written to the database by somebody merely looking at a form.
    """

    id = None
    kind = "builtin"
    options = None
    option_list = []
    hidden = False
    is_section = False
    stores_answer = False
    wants_options = False

    def __init__(self, key, label, locked, position):
        self.builtin = key
        self.title = label
        self.help = None
        self.locked = locked
        self.required = locked
        self.position = position


def ensure_builtins(db, ev) -> list:
    """Give this event's built-in fields their rows, once.

    Written on the first visit to the builder rather than when the event is
    created, so every event that already exists gets them too, and in the
    order the sign-up page has always drawn them. Any questions already
    written move down to sit after them - which is where they already were.
    """
    have = {q.builtin for q in ev.questions if q.builtin}
    missing = [f for f in BUILTIN_FIELDS if f[0] not in have]
    if not missing:
        renumber(ev)
        return sorted(ev.questions, key=lambda q: (q.position, q.id))
    # Existing questions keep their order, below everything built-in.
    for q in sorted(ev.questions, key=lambda q: (q.position, q.id)):
        if not q.builtin:
            q.position += 1000
    for i, (key, label, locked) in enumerate(BUILTIN_FIELDS):
        if key in have:
            continue
        db.add(EventQuestion(event_id=ev.id, title=label, kind="builtin",
                             builtin=key, required=locked,
                             position=BUILTIN_KEYS.index(key)))
    db.flush()
    db.expire(ev, ["questions"])
    renumber(ev)
    db.commit()
    return sorted(ev.questions, key=lambda q: (q.position, q.id))


def plan(ev) -> list:
    """Every field on this event's sign-up, in the order it is asked.

    Real rows if the builder has been opened, ghosts if it has not, and the
    hidden ones dropped - so the page that draws the form and the page that
    reads it back are looking at the same list.
    """
    rows = sorted(ev.questions, key=lambda q: (q.position, q.id))
    have = {q.builtin for q in rows if q.builtin}
    if not have:
        ghosts = [Ghost(k, l, r, i) for i, (k, l, r) in enumerate(BUILTIN_FIELDS)]
        return ghosts + [q for q in rows if not q.hidden]
    out = [q for q in rows if not q.hidden]
    # A field added to BUILTIN_FIELDS after this event was seeded would
    # otherwise vanish from the form. Put it back, at the end.
    for i, (k, l, r) in enumerate(BUILTIN_FIELDS):
        if k not in have:
            out.append(Ghost(k, l, r, 900 + i))
    return out


def pages(rows) -> list:
    """The plan, cut into pages at every section.

    A list of (section-or-None, [fields]). One page and no section header is
    the ordinary case and the one that must stay ordinary: a form with no
    sections is a form with no Next button.
    """
    out, head, cur = [], None, []
    for q in rows:
        if q.is_section:
            if cur or head is not None:
                out.append((head, cur))
            head, cur = q, []
        else:
            cur.append(q)
    out.append((head, cur))
    return out


def answers_for(p) -> list:
    """One person's answers, in the order the questions are asked.

    Questions that were added after they registered simply are not there, which
    is the honest answer: nobody asked them.
    """
    by_q = {a.question_id: a for a in (p.answers or [])}
    return [(q, by_q.get(q.id)) for q in
            sorted(p.event.questions, key=lambda q: (q.position, q.id))
            if by_q.get(q.id) is not None]


def read_answers(form, ev) -> tuple:
    """(answers, missing) from a submitted sign-up form.

    `answers` is {question_id: value}; `missing` is the required ones left
    blank, so the page can say which rather than just refusing.
    """
    out, missing = {}, []
    for q in sorted(ev.questions, key=lambda x: (x.position, x.id)):
        # A section asks nothing, and a built-in's answer is a column on the
        # participant, not a row in here.
        if not q.stores_answer or q.hidden:
            continue
        key = "q%d" % q.id
        if q.is_terms:
            # One box. Ticked or not, and the answer is the moment it
            # happened - what was agreed to is copied on in save_answers,
            # from the question, because that is where the wording lives.
            ticked = bool((form.get(key) or "").strip())
            if q.required and not ticked:
                missing.append(q)
            if ticked:
                out[q.id] = "Agreed \u00b7 " + agreed_at()
            continue
        if q.kind == "checks":
            # Several boxes share one name, so take them all and keep the
            # order the options are written in rather than the order the
            # browser happened to send.
            picked = [v for v in form.getlist(key) if (v or "").strip()]
            order = {o: i for i, o in enumerate(q.option_list)}
            picked.sort(key=lambda v: order.get(v, 999))
            value = "\n".join(picked)
        else:
            value = (form.get(key) or "").strip()
        if q.required and not value:
            missing.append(q)
        if value:
            out[q.id] = value
    return out, missing


def write_maps(p, answers) -> None:
    """Copy the mapped answers onto the participant row itself.

    An *extra* copy, never instead of the answer: the answers table stays the
    record of what somebody typed into the form, and this is the same value
    put where the rest of the system already looks for it. If a mapping is
    later removed or repointed, nothing that was collected is lost.

    A value that will not convert is skipped rather than forced. "twenty-six"
    in an age column is worse than an empty one - the answer is still on the
    answer row, and somebody can read it.
    """
    for q in (p.event.questions or []):
        if not q.maps_to or q.id not in answers:
            continue
        raw = (answers.get(q.id) or "").strip()
        if not raw:
            continue
        if q.maps_to == "age":
            digits = re.sub(r"[^0-9]", "", raw)
            if digits and 1 <= int(digits) <= 120:
                p.age = int(digits)
        elif q.maps_to == "instagram":
            # Stored without the @: it is a handle, and half of them type it
            # and half do not.
            p.instagram = raw.lstrip("@").strip()[:60] or None


def agreed_at() -> str:
    """Now, in gym time, as somebody reading a roster would write it."""
    return to_local(datetime.now(timezone.utc)).strftime(
        "%d %b %Y, %I:%M %p").lstrip("0").replace(" 0", " ")


def save_answers(db, p, answers) -> None:
    """Write them, replacing whatever was there.

    Replacing rather than adding, because somebody who goes back a step and
    resubmits is correcting themselves, not answering twice.
    """
    have = {a.question_id: a for a in (p.answers or [])}
    terms = {q.id: q.options for q in p.event.questions if q.is_terms}
    for qid, value in answers.items():
        row = have.get(qid)
        if row is None:
            row = ParticipantAnswer(participant_id=p.id, question_id=qid,
                                    value=value)
            db.add(row)
        else:
            row.value = value
        if qid in terms:
            # The wording as it read when they ticked it. Copied, not looked
            # up later: the terms are editable and an agreement that points at
            # today's text is a record of nothing.
            row.snapshot = terms[qid]
    write_maps(p, answers)
    db.flush()


# ------------------------------------------------------------------ rates

def money_in(raw) -> Decimal:
    """What somebody typed into an amount box, as a number or nothing.

    Deliberately forgiving: "1,500", "P1500", "1500.00" and " 1500 " are all
    the same amount, and refusing any of them would be pedantry aimed at the
    person least able to do anything about it.
    """
    txt = re.sub(r"[^0-9.]", "", str(raw or ""))
    if not txt:
        return None
    try:
        return Decimal(txt)
    except InvalidOperation:
        return None


def money_out(v) -> str:
    """A stored amount, as it should appear back in the box."""
    if v is None:
        return ""
    q = Decimal(v)
    return str(int(q)) if q == q.to_integral_value() else str(q)


def rate_use(db, ev) -> dict:
    """{rate id: how many people picked it}.

    The number that decides whether a rate may be deleted at all. One person
    on it and deleting stops being tidying up and starts being erasing what
    somebody paid for.
    """
    out = {}
    rows = (db.query(EventParticipant.tier)
            .filter(EventParticipant.event_id == ev.id).all())
    for (t,) in rows:
        if t:
            out[t] = out.get(t, 0) + 1
    return {r.id: out.get(r.key, 0) for r in ev.rate_rows()}


def ensure_rates(db, ev) -> list:
    """Copy an event's two old rates into rows, once, if nobody has yet.

    The same thing the startup block does in SQL, in Python, for the event in
    front of us. Both exist on purpose: the startup pass catches every event
    at once, and this catches an event created by something that never ran it
    - a fixture, a restore, a copy from another environment. Running twice
    finds rows already there and does nothing.
    """
    if ev.rates:
        return ev.rate_rows()
    old = [(ev.tier_a_label, ev.tier_a_price), (ev.tier_b_label, ev.tier_b_price)]
    made = []
    for i, (label, price) in enumerate(old):
        if not (label or "").strip():
            continue
        r = EventRate(event_id=ev.id, label=label.strip(), amount=price,
                      position=i)
        db.add(r)
        made.append((("a", "b")[i], r))
    if not made:
        return []
    db.flush()
    # Whoever already picked 'a' or 'b' picked the row that letter became.
    for letter, r in made:
        (db.query(EventParticipant)
           .filter(EventParticipant.event_id == ev.id,
                   EventParticipant.tier == letter)
           .update({"tier": r.key}, synchronize_session=False))
    db.commit()
    db.expire(ev, ["rates"])
    return ev.rate_rows()


def fill_counts(db, ev) -> dict:
    """Per mapped question: how many answers exist, and how many would land.

    "Would land" is the number that matters and the one that is not obvious:
    an answer whose target column is already filled is left alone, so the
    button can say what it will actually do rather than how many rows it will
    look at.
    """
    out = {}
    mapped = [q for q in ev.questions if q.maps_to]
    if not mapped:
        return out
    rows = (db.query(ParticipantAnswer, EventParticipant)
            .join(EventParticipant,
                  EventParticipant.id == ParticipantAnswer.participant_id)
            .filter(EventParticipant.event_id == ev.id).all())
    by_q = {}
    for a, p in rows:
        by_q.setdefault(a.question_id, []).append((a, p))
    for q in mapped:
        got = by_q.get(q.id, [])
        fillable = 0
        for a, p in got:
            if not (a.value or "").strip():
                continue
            if getattr(p, q.maps_to, None) in (None, ""):
                fillable += 1
        out[q.id] = {"answered": len(got), "fillable": fillable}
    return out


def fill_from_answers(db, ev, q) -> int:
    """Write a mapped question's existing answers onto the rows, once.

    For the case this was built for: the question was already being asked and
    answered before anybody thought to point it at a column. The answers were
    never lost - they are in participant_answers, which is the whole reason
    the value goes to two places - so filling the column afterwards is a copy,
    not a recovery.

    Only ever fills a blank. Somebody whose handle was typed in by hand keeps
    what was typed; a form answer does not get to overwrite a correction.
    """
    if not q or not q.maps_to:
        return 0
    rows = (db.query(ParticipantAnswer, EventParticipant)
            .join(EventParticipant,
                  EventParticipant.id == ParticipantAnswer.participant_id)
            .filter(EventParticipant.event_id == ev.id,
                    ParticipantAnswer.question_id == q.id).all())
    done = 0
    for a, p in rows:
        if getattr(p, q.maps_to, None) not in (None, ""):
            continue
        before = getattr(p, q.maps_to, None)
        write_maps(p, {q.id: a.value})
        if getattr(p, q.maps_to, None) != before:
            done += 1
    if done:
        db.commit()
    return done


# ------------------------------------------------------------- the document

def field_json(q, counts=None) -> dict:
    n = (counts or {}).get(q.id) or {}
    return {
        "id": q.id,
        "title": q.title or "",
        "help": q.help or "",
        "kind": "tier" if q.builtin == "tier" else (q.kind or "text"),
        "opts": q.options or "",
        "tick": q.tick or "",
        "map": q.maps_to or "",
        "req": 1 if q.required else 0,
        "off": 1 if q.hidden else 0,
        "builtin": q.builtin or "",
        "lock": 1 if q.locked else 0,
        "answered": n.get("answered", 0),
        "fillable": n.get("fillable", 0),
    }


def doc(db, ev) -> dict:
    """The whole form as one object, which is what the builder edits.

    The builder is a page of JavaScript over this, rather than a form per
    question, for one reason: the thing being edited is the *order* as much as
    the questions, and an order is a whole-document fact. Sending the document
    back means a drag, a rename and a new question are all the same save.
    """
    rows = ensure_builtins(db, ev)
    ensure_rates(db, ev)
    counts = fill_counts(db, ev)
    pages, cur = [], {"sid": None, "title": "", "help": "", "fields": []}
    for q in rows:
        if q.is_section:
            if cur["fields"] or cur["sid"] is not None or pages:
                pages.append(cur)
            cur = {"sid": q.id, "title": q.title or "", "help": q.help or "",
                   "fields": []}
        else:
            cur["fields"].append(field_json(q, counts))
    pages.append(cur)
    # A leading section means page one *is* that section, not an empty page
    # above it.
    if len(pages) > 1 and not pages[0]["fields"] and pages[0]["sid"] is None:
        pages = pages[1:]

    use = rate_use(db, ev)
    return {
        "pages": pages,
        "kinds": [[k, l] for k, l in QUESTION_KINDS if k != "section"],
        "withOpts": list(QUESTION_KINDS_WITH_OPTIONS),
        "looks": [[k, l] for k, l in RATE_LOOKS],
        "maps": [[k, l, list(kinds), why] for k, l, kinds, why in MAPPABLE],
        "look": ev.rate_look or "tiles",
        "rates": [{"id": r.id, "label": r.label, "amt": money_out(r.amount),
                   "closed": bool(r.closed), "used": use.get(r.id, 0)}
                  for r in ev.rate_rows()],
    }


#: What each built-in field actually writes. Spelled out here rather than
#: inferred, because the page's job is to be checkable and "name" quietly
#: filling three columns is exactly the kind of thing somebody needs told.
BUILTIN_COLS = {
    "name": (["event_participants.first_name", "event_participants.last_name",
              "event_participants.name"], ""),
    "email": (["event_participants.email"], ""),
    "mobile": (["event_participants.mobile"], ""),
    "country": (["event_participants.country"], "Two letters, ISO-3166."),
    "sex": (["event_participants.sex"], "'m' or 'f'."),
    "tier": (["event_participants.tier", "event_participants.amount"],
             "The rate's id, and what it cost at the moment they picked it."),
}


def map_rows(db, ev) -> list:
    """The form, page by page, with where every answer goes.

    Built from the same plan the sign-up page draws, so the two cannot
    disagree - a field that is not on this list is not on the form.
    """
    ensure_builtins(db, ev)
    kinds = dict(QUESTION_KINDS)
    out = []
    for head, fields in pages(plan(ev)):
        shown = []
        for q in fields:
            if q.builtin:
                cols, note = BUILTIN_COLS.get(q.builtin, ([], ""))
                cols = [{"name": c, "hot": False} for c in cols]
                label = "The sign-up's own field"
            elif q.is_terms:
                cols = [{"name": "participant_answers.value", "hot": False},
                        {"name": "participant_answers.snapshot", "hot": False}]
                note = ("The agreement, and a copy of the wording as it read "
                        "when they ticked it.")
                label = kinds.get(q.kind, q.kind)
            else:
                cols = [{"name": "participant_answers.value", "hot": False}]
                note = ""
                label = kinds.get(q.kind, q.kind)
                if q.maps_to:
                    cols.append({"name": "event_participants." + q.maps_to,
                                 "hot": True})
                    note = MAP_LABELS.get(q.maps_to, "")
            shown.append({
                "title": q.title, "help": q.help, "required": q.required,
                "hidden": getattr(q, "hidden", False),
                "type_label": label, "cols": cols, "note": note,
            })
        out.append((head, shown))
    return out


def save_doc(db, ev, body) -> None:
    """Write the document back.

    Two rules make this safe to run on every keystroke.

    Nothing is deleted implicitly. A question that is simply absent from the
    document is left alone; only the ids in `deleted` go, which means a bug in
    the page cannot quietly take a question and its answers with it.

    And a built-in is only ever edited in the ways a built-in can be. What it
    asks for is not in this payload at all, so no amount of posting can turn
    the mobile field into a dropdown.
    """
    rows = {q.id: q for q in ev.questions}
    pos, seen = 0, set()

    for n, page in enumerate(body.get("pages") or []):
        title = (page.get("title") or "").strip()[:200]
        help_ = (page.get("help") or "").strip()[:300]
        sid = page.get("sid")
        sec = rows.get(sid) if sid else None
        if sec is not None and not sec.is_section:
            sec = None
        if n == 0 and not title and not help_:
            # Page one with no heading needs no page break in front of it.
            if sec is not None:
                db.delete(sec)
                sec = None
        elif sec is None:
            sec = EventQuestion(event_id=ev.id, title=title, kind="section",
                                position=pos)
            db.add(sec)
            db.flush()
            rows[sec.id] = sec
        if sec is not None:
            sec.title, sec.help = title, help_ or None
            sec.position, pos = pos, pos + 1
            seen.add(sec.id)

        for f in page.get("fields") or []:
            q = rows.get(f.get("id"))
            if q is None:
                q = EventQuestion(event_id=ev.id, title="", kind="text",
                                  position=pos)
                db.add(q)
                db.flush()
                rows[q.id] = q
            q.title = (f.get("title") or "").strip()[:200]
            q.help = (f.get("help") or "").strip()[:300] or None
            if q.builtin:
                q.required = True if q.locked else bool(f.get("req"))
                q.hidden = False if q.locked else bool(f.get("off"))
            else:
                q.kind = clean_kind(f.get("kind"))
                q.options = (f.get("opts") or "").strip() or None
                q.tick = (f.get("tick") or "").strip()[:200] or None
                # Where the answer *also* goes. Checked here rather than
                # trusted, twice over: the column has to be one of the two we
                # offer, and the question has to be a type that can honestly
                # fill it. A form that could name its own column is a form
                # that can write to the payment status.
                want = (f.get("map") or "").strip()
                q.maps_to = want if map_fits(want, q.kind) else None
                q.required = bool(f.get("req"))
                q.hidden = False
            q.position, pos = pos, pos + 1
            seen.add(q.id)

    # One column, one question. Two questions both writing to age is not a
    # mapping, it is a race - whichever answer is saved last wins and nobody
    # can tell which was asked. First one in the form keeps it.
    claimed = set()
    for q in sorted(rows.values(), key=lambda x: (x.position, x.id)):
        if not q.maps_to:
            continue
        if q.maps_to in claimed:
            q.maps_to = None
        else:
            claimed.add(q.maps_to)

    for qid in (body.get("deleted") or []):
        q = rows.get(qid)
        if q is not None and not q.builtin and q.id not in seen:
            db.delete(q)

    save_rates(db, ev, body)
    db.flush()
    db.expire(ev, ["questions"])
    ensure_builtins(db, ev)
    db.commit()


def save_rates(db, ev, body) -> None:
    """The rates, in the order they are drawn.

    A rate somebody has already picked is never deleted here, whatever the
    page asks for. It is closed instead - off the sign-up, still on their
    registration - because the alternative is a paid row pointing at a rate
    with no name.
    """
    if "rates" not in body:
        return
    have = {r.id: r for r in ev.rate_rows()}
    use = rate_use(db, ev)
    keep = set()
    for i, item in enumerate(body.get("rates") or []):
        label = (item.get("label") or "").strip()[:80]
        r = have.get(item.get("id"))
        if r is None:
            if not label:
                continue          # a blank new row is somebody who changed their mind
            r = EventRate(event_id=ev.id, label=label, position=i)
            db.add(r)
            db.flush()
            have[r.id] = r
        r.label = label
        r.amount = money_in(item.get("amt"))
        r.closed = bool(item.get("closed"))
        r.position = i
        keep.add(r.id)

    for rid in (body.get("ratesGone") or []):
        r = have.get(rid)
        if r is None or rid in keep:
            continue
        if use.get(rid, 0):
            r.closed = True       # somebody paid this. It stops being offered, not history.
        else:
            db.delete(r)

    look = (body.get("look") or "").strip()
    if look in RATE_LOOK_KEYS:
        ev.rate_look = look


def register(app, deps):
    render = deps["render"]
    require = deps["require"]

    def guard(request, db):
        return require(request, db, perm="manage_hyrox")

    def _ev(db, eid):
        return db.get(Event, eid)

    def back(eid):
        return RedirectResponse("/events/%d/form" % eid, status_code=303)

    @app.get("/events/{eid}/form", response_class=HTMLResponse)
    def form_builder(request: Request, eid: int,
                     db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = _ev(db, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        return render(request, "event_form.html", db, staff, active="events",
                      ev=ev, doc=doc(db, ev))

    @app.get("/events/{eid}/form/map", response_class=HTMLResponse)
    def form_map(request: Request, eid: int, db: Session = Depends(get_db)):
        """Every field, and the column its answer lands in. Read-only."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = _ev(db, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        return render(request, "form_map.html", db, staff, active="events",
                      ev=ev, pages=map_rows(db, ev))

    @app.post("/events/{eid}/form/{qid}/fill")
    def form_fill(request: Request, eid: int, qid: int,
                  db: Session = Depends(get_db)):
        """Copy a mapped question's existing answers onto the rows.

        For the form that was already collecting something before anybody
        pointed it at a column. Nothing was lost in the meantime - it is all
        in participant_answers - so this is a copy across, and it only ever
        fills a blank.
        """
        staff, redir = guard(request, db)
        if redir:
            return JSONResponse({"ok": False}, status_code=403)
        ev = _ev(db, eid)
        q = db.get(EventQuestion, qid)
        if not ev or not q or q.event_id != eid:
            return JSONResponse({"ok": False}, status_code=404)
        done = fill_from_answers(db, ev, q)
        return JSONResponse({"ok": True, "filled": done, "doc": doc(db, ev)})

    @app.post("/events/{eid}/form/save")
    async def form_save_all(request: Request, eid: int,
                            db: Session = Depends(get_db)):
        """The whole document, on every change. There is no Save button.

        Returns the document as it now stands rather than just "ok", because
        the page has just invented ids for anything new and needs the real
        ones - and because a rate it asked to delete may have come back as
        closed instead, which it has to be able to show.
        """
        staff, redir = guard(request, db)
        if redir:
            return JSONResponse({"ok": False}, status_code=403)
        ev = _ev(db, eid)
        if not ev:
            return JSONResponse({"ok": False}, status_code=404)
        body = await request.json()
        save_doc(db, ev, body)
        return JSONResponse({"ok": True, "doc": doc(db, ev)})

    @app.post("/events/{eid}/form/add")
    def form_add(request: Request, eid: int, title: str = Form(""),
                 kind: str = Form("text"), db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = _ev(db, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        ensure_builtins(db, ev)
        clean = title.strip()[:200]
        if not clean:
            # A section is a page break, and a page break with no name is a
            # perfectly ordinary thing to want.
            if clean_kind(kind) != "section":
                return back(eid)
            clean = "Next"
        db.add(EventQuestion(event_id=eid, title=clean, kind=clean_kind(kind),
                             position=len(ev.questions)))
        db.commit()
        return back(eid)

    @app.post("/events/{eid}/form/{qid}")
    def form_save(request: Request, eid: int, qid: int, title: str = Form(""),
                  help: str = Form(""), kind: str = Form("text"),
                  options: str = Form(""), required: str = Form(""),
                  hidden: str = Form(""), db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        q = db.get(EventQuestion, qid)
        if not q or q.event_id != eid:
            return back(eid)
        clean = title.strip()[:200]
        if clean:
            q.title = clean
        q.help = help.strip()[:300] or None
        if not q.builtin:
            q.kind = clean_kind(kind)
            q.options = options.strip() or None
        # A built-in can be reworded, moved, and - unless it is one of the
        # three - switched off. What it asks for is not up for editing: a
        # mobile field that has become a dropdown is not a mobile field.
        q.required = True if q.locked else (required == "on")
        if q.builtin and not q.locked:
            q.hidden = hidden == "on"
        db.commit()
        return back(eid)

    @app.post("/events/{eid}/form/{qid}/move")
    def form_move(request: Request, eid: int, qid: int, dir: str = Form(""),
                  db: Session = Depends(get_db)):
        """Up or down one. Buttons rather than dragging, on purpose.

        Dragging is the fiddly half of a form builder and the half that breaks
        on a phone. Six questions reorder fine with two arrows.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = _ev(db, eid)
        q = db.get(EventQuestion, qid)
        if not ev or not q or q.event_id != eid:
            return back(eid)
        rows = sorted(ev.questions, key=lambda x: (x.position, x.id))
        i = rows.index(q)
        j = i - 1 if dir == "up" else i + 1
        if 0 <= j < len(rows):
            rows[i], rows[j] = rows[j], rows[i]
            for n, row in enumerate(rows):
                row.position = n
            db.commit()
        return back(eid)

    @app.post("/events/{eid}/form/{qid}/delete")
    def form_delete(request: Request, eid: int, qid: int,
                    db: Session = Depends(get_db)):
        """Gone, and every answer to it with it.

        The cascade is deliberate. An answer to a question nobody can read any
        more is not a record, it is a column of orphaned text that shows up in
        an export with no heading.

        Built-in fields do not go this way. Deleting one would only mean it
        came back on the next visit, so the ones that can be switched off are
        switched off instead.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = _ev(db, eid)
        q = db.get(EventQuestion, qid)
        if ev and q and q.event_id == eid and not q.builtin:
            db.delete(q)
            db.flush()
            renumber(ev)
            db.commit()
        return back(eid)
