"""The extra questions on an event's sign-up form.

Registered from main.py via ``register(app, deps)`` like the other feature
modules, so this file never imports main.

A Google Form, scoped to the one thing it is for. Add a question, pick a type,
say whether it is required, drag it up or down. That is the whole feature, and
the restraint is the point: the fields the system actually reads - name, email,
gender, category, rate, payment - are not in here and cannot be broken from
here. Anything in this file is a question the gym wants to ask, which is
exactly why it can be anything.

Answers land in their own table rather than a blob on the participant, so the
saved-reports feature can read them. "Shirt sizes by count" is then a report,
not a counting exercise.
"""

from __future__ import annotations

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .db import get_db
from .models import (Event, EventQuestion, ParticipantAnswer,
                     QUESTION_KINDS, QUESTION_KIND_KEYS,
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
        return render(request, "event_form.html", db, staff, active="events",
                      ev=ev, kinds=QUESTION_KINDS,
                      with_options=QUESTION_KINDS_WITH_OPTIONS,
                      qs=sorted(ev.questions, key=lambda q: (q.position, q.id)))

    @app.post("/events/{eid}/form/add")
    def form_add(request: Request, eid: int, title: str = Form(""),
                 kind: str = Form("text"), db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = _ev(db, eid)
        if not ev:
            return RedirectResponse("/events", status_code=303)
        clean = title.strip()[:200]
        if not clean:
            return back(eid)
        db.add(EventQuestion(event_id=eid, title=clean, kind=clean_kind(kind),
                             position=len(ev.questions)))
        db.commit()
        return back(eid)

    @app.post("/events/{eid}/form/{qid}")
    def form_save(request: Request, eid: int, qid: int, title: str = Form(""),
                  help: str = Form(""), kind: str = Form("text"),
                  options: str = Form(""), required: str = Form(""),
                  db: Session = Depends(get_db)):
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
        q.kind = clean_kind(kind)
        q.options = options.strip() or None
        q.required = required == "on"
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
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        ev = _ev(db, eid)
        q = db.get(EventQuestion, qid)
        if ev and q and q.event_id == eid:
            db.delete(q)
            db.flush()
            renumber(ev)
            db.commit()
        return back(eid)
