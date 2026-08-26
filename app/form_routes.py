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

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .db import get_db
from .models import (BUILTIN_FIELDS, BUILTIN_KEYS, BUILTIN_LOCKED,
                     Event, EventQuestion,
                     ParticipantAnswer, QUESTION_KINDS, QUESTION_KIND_KEYS,
                     QUESTION_KINDS_WITH_OPTIONS)


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


def save_answers(db, p, answers) -> None:
    """Write them, replacing whatever was there.

    Replacing rather than adding, because somebody who goes back a step and
    resubmits is correcting themselves, not answering twice.
    """
    have = {a.question_id: a for a in (p.answers or [])}
    for qid, value in answers.items():
        row = have.get(qid)
        if row is None:
            db.add(ParticipantAnswer(participant_id=p.id, question_id=qid,
                                     value=value))
        else:
            row.value = value
    db.flush()


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
        rows = ensure_builtins(db, ev)
        return render(request, "event_form.html", db, staff, active="events",
                      ev=ev, kinds=QUESTION_KINDS,
                      with_options=QUESTION_KINDS_WITH_OPTIONS,
                      pagecount=len([q for q in rows if q.is_section]) + 1,
                      qs=rows)

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
