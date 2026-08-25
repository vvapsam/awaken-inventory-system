"""Saved reports: a question, stored as the SELECT that answers it.

Registered from main.py via ``register(app, deps)`` like the other feature
modules, so this file never imports main.

The whole feature is one text box that executes SQL, which is a thing you
should be nervous about. Three decisions make it safe enough to ship:

* **SELECT only, and one statement.** ``check_sql`` refuses anything else
  before it reaches the database. This is the courtesy check - it exists so a
  mistake reads as an error message rather than as a stack trace.
* **A read-only transaction.** This is the actual guard. Postgres itself
  refuses to write inside one, so a report that manages to be a DELETE fails
  at the database rather than at our regex. Anything that gets past the first
  check still cannot change a row.
* **A timeout and a row cap.** A cartesian join is an honest mistake and it
  should cost five seconds, not the site.

Read-only also means no report can be used to fix data, which is deliberate.
There is exactly one way to change something in this system and it is a screen
that knows what it is changing.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import get_db
from .models import SavedReport

#: How long a report may run before Postgres gives up on it.
TIMEOUT_MS = 8000
#: The most rows a run will return. The preview shows fewer still - see
#: PREVIEW_ROWS - but the CSV stops here too, because a report that wants a
#: million rows is a report that wants a different tool.
MAX_ROWS = 5000
PREVIEW_ROWS = 100

#: Statements that read. Everything else is refused by name as well as by the
#: read-only transaction, so the message says what was wrong.
_OPENERS = ("select", "with")

_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)


def strip_comments(sql: str) -> str:
    """The statement with its comments removed, for inspection only.

    Never what gets executed: a comment is often the only explanation a report
    has, and running a stripped copy would mean the thing that ran and the
    thing on screen were different text.
    """
    return _COMMENT_BLOCK.sub(" ", _COMMENT_LINE.sub(" ", sql or ""))


def check_sql(sql: str) -> str:
    """"" if this is safe to run, else why it isn't, in a sentence."""
    bare = strip_comments(sql).strip()
    if not bare:
        return "There's no query here yet."
    # One statement. A trailing semicolon is normal and fine; a second
    # statement after it is how "SELECT 1; DROP TABLE" gets written.
    if ";" in bare.rstrip().rstrip(";"):
        return ("One statement per report, please — there's a semicolon in "
                "the middle of this one.")
    first = bare.split(None, 1)[0].lower()
    if first not in _OPENERS:
        return ("A report has to start with SELECT or WITH. This one starts "
                "with “%s”, and reports are only allowed to read."
                % first.upper()[:20])
    return ""


def run_report(db: Session, sql: str, limit: int = MAX_ROWS) -> tuple:
    """(columns, rows) — or raises whatever Postgres thought of it.

    The read-only transaction is the guard that matters. It is set on the
    connection rather than asked of the query, so it holds however the SQL is
    written and whatever it turns out to do.
    """
    conn = db.connection()
    # A savepoint, so a failed report leaves the session usable - without it a
    # syntax error would poison the request's transaction and the error page
    # itself would fail to render.
    nested = conn.begin_nested()
    try:
        conn.exec_driver_sql("SET LOCAL statement_timeout = %d" % TIMEOUT_MS)
        conn.exec_driver_sql("SET LOCAL transaction_read_only = on")
        res = conn.execute(text(sql))
        cols = list(res.keys())
        rows = res.fetchmany(limit)
        return cols, [list(r) for r in rows]
    finally:
        # Always rolled back. A report has nothing to commit by construction,
        # and rolling back is what returns the connection to a writable state
        # for whatever the request does next.
        nested.rollback()


def cell(v) -> str:
    """One value, as a spreadsheet should read it."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


#: The report this file ships with. Seeded once, by key, and never overwritten
#: afterwards - see SavedReport.builtin_key.
#:
#: Every derived number in it is also computed in Python somewhere, which is
#: the risk with a report written in SQL: two definitions of the same thing,
#: drifting apart quietly until a CSV disagrees with the results page in front
#: of a sponsor. /tmp/test_reports.py pins it by running the report and the
#: app's own functions over the same participants and demanding they match.
HYROX_RESULTS_SQL = """\
-- HYROX PFT — every finisher, their splits, and the patch they earned.
--
-- heat_start is the event's date with their heat's clock time on it, read in
-- gym time. It mirrors EventParticipant.heat_start(); if that ever changes,
-- this has to change with it, and the test suite is what will say so.
WITH heat AS (
  SELECT
    ep.id,
    -- Mirrors EventParticipant.full_name: first + last where we have
    -- them, otherwise the single name the row was added with.
    NULLIF(btrim(concat_ws(' ', ep.first_name, ep.last_name)), '')
      AS full_name,
    ep.name,
    ep.email,
    ep.age,
    ep.finished_at,
    ep.patch,
    (
      (date_trunc('day', ep_ev.starts_at AT TIME ZONE 'Asia/Manila')
       + ep.heat_time::time) AT TIME ZONE 'Asia/Manila'
    ) AS heat_start
  FROM event_participants ep
  JOIN events ep_ev ON ep_ev.id = ep.event_id
  WHERE ep_ev.id = :event_id
    AND NOT ep.waitlist
    AND ep.released_at IS NULL
    AND ep.rsvp <> 'no'
    AND ep.heat_time IS NOT NULL
),
split AS (
  SELECT
    sr.participant_id,
    lower(es.name) AS station,
    EXTRACT(EPOCH FROM (sr.ended_at - sr.started_at))::int AS secs
  FROM station_runs sr
  JOIN event_stations es ON es.id = sr.station_id
  WHERE sr.started_at IS NOT NULL AND sr.ended_at IS NOT NULL
),
wide AS (
  SELECT
    participant_id,
    MAX(secs) FILTER (WHERE station LIKE '%run%')        AS run_s,
    MAX(secs) FILTER (WHERE station LIKE '%burpee%')     AS bbj_s,
    MAX(secs) FILTER (WHERE station LIKE '%lunge%')      AS lunge_s,
    MAX(secs) FILTER (WHERE station LIKE '%row%')        AS row_s,
    MAX(secs) FILTER (WHERE station LIKE '%push%')       AS push_s,
    MAX(secs) FILTER (WHERE station LIKE '%wall%')       AS wall_s
  FROM split
  GROUP BY participant_id
),
total AS (
  SELECT
    h.*,
    CASE WHEN h.finished_at IS NOT NULL AND h.heat_start IS NOT NULL
         THEN EXTRACT(EPOCH FROM (h.finished_at - h.heat_start))::int
    END AS total_s
  FROM heat h
)
SELECT
  COALESCE(t.full_name, t.name)                 AS "Name",
  t.email                                       AS "Email",
  t.age                                         AS "Age",
  -- mmss() written out: 2:00, 0:45, and 1:05:30 once it passes an hour.
  -- It mirrors patch_routes.mmss on purpose - a CSV and the results page
  -- printing the same time two different ways is the exact thing this
  -- report is not allowed to do. FMMI:SS alone gets the hour case wrong,
  -- which is what the CASE is for.
  CASE WHEN w.run_s >= 3600
       THEN to_char((w.run_s || ' second')::interval, 'FMHH24:MI:SS')
       ELSE to_char((w.run_s || ' second')::interval, 'FMMI:SS')
  END AS "Run Time",
  CASE WHEN w.bbj_s >= 3600
       THEN to_char((w.bbj_s || ' second')::interval, 'FMHH24:MI:SS')
       ELSE to_char((w.bbj_s || ' second')::interval, 'FMMI:SS')
  END AS "BBJ Time",
  CASE WHEN w.lunge_s >= 3600
       THEN to_char((w.lunge_s || ' second')::interval, 'FMHH24:MI:SS')
       ELSE to_char((w.lunge_s || ' second')::interval, 'FMMI:SS')
  END AS "Lunges Time",
  CASE WHEN w.row_s >= 3600
       THEN to_char((w.row_s || ' second')::interval, 'FMHH24:MI:SS')
       ELSE to_char((w.row_s || ' second')::interval, 'FMMI:SS')
  END AS "Row Time",
  CASE WHEN w.push_s >= 3600
       THEN to_char((w.push_s || ' second')::interval, 'FMHH24:MI:SS')
       ELSE to_char((w.push_s || ' second')::interval, 'FMMI:SS')
  END AS "HR Pushup Time",
  CASE WHEN w.wall_s >= 3600
       THEN to_char((w.wall_s || ' second')::interval, 'FMHH24:MI:SS')
       ELSE to_char((w.wall_s || ' second')::interval, 'FMMI:SS')
  END AS "WallBall Time",
  CASE WHEN t.total_s >= 3600
       THEN to_char((t.total_s || ' second')::interval, 'FMHH24:MI:SS')
       ELSE to_char((t.total_s || ' second')::interval, 'FMMI:SS')
  END AS "Total Time",
  -- The patch they earned, not the one they collected: only eight of
  -- thirty-eight came back to the table for theirs, and a column of blanks
  -- for people who ran a gold time is not a report.
  --
  -- Thresholds mirror PATCH_BANDS in patch_routes.py. Senior is 45 and over.
  CASE
    WHEN t.age IS NULL OR t.total_s IS NULL THEN NULL
    WHEN t.age >= 45 THEN CASE WHEN t.total_s < 24*60 THEN 'Gold'
                               WHEN t.total_s < 28*60 THEN 'Silver'
                               ELSE 'Bronze' END
    ELSE              CASE WHEN t.total_s < 22*60 THEN 'Gold'
                           WHEN t.total_s < 26*60 THEN 'Silver'
                           ELSE 'Bronze' END
  END                                           AS "Patch Reward"
FROM total t
LEFT JOIN wide w ON w.participant_id = t.id
ORDER BY t.total_s NULLS LAST, COALESCE(t.full_name, t.name)
"""

BUILTINS = [
    {
        "key": "hyrox_results",
        "name": "HYROX PFT — results and patches",
        "notes": ("Every finisher on one event: their six splits, their "
                  "official time, and the patch that time earned. Needs an "
                  "event picked above."),
        "sql": HYROX_RESULTS_SQL,
    },
]


def seed(db: Session) -> None:
    """Put the shipped reports in, once each, and never touch them again."""
    for b in BUILTINS:
        if db.query(SavedReport).filter(
                SavedReport.builtin_key == b["key"]).first():
            continue
        db.add(SavedReport(name=b["name"], notes=b["notes"], sql=b["sql"],
                           builtin_key=b["key"]))
    db.commit()


def wants_event(sql: str) -> bool:
    """Whether this report is about one event, from whether it asks for one."""
    return ":event_id" in (sql or "")


def register(app, deps):
    render = deps["render"]
    require = deps["require"]

    def guard(request, db):
        """Admins only.

        Not a permission of its own, and deliberately the strictest guard in
        the system. A saved report can read every table there is - wages,
        addresses, every commission ever run - so the question "who may write
        one" is really "who may read everything", and that is the admin
        answer. A door-staff permission that happened to include Reports would
        be a way around every other permission at once.
        """
        return require(request, db, admin=True)

    def _events(db):
        from .models import Event
        return db.query(Event).order_by(Event.id.desc()).all()

    @app.get("/saved-reports", response_class=HTMLResponse)
    def reports_list(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        seed(db)
        rows = db.query(SavedReport).order_by(SavedReport.name).all()
        return render(request, "saved_reports.html", db, staff, active="savedreports",
                      reports=rows)

    @app.get("/saved-reports/new", response_class=HTMLResponse)
    def report_new(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        return render(request, "report_edit.html", db, staff, active="savedreports",
                      r=None, err="")

    @app.post("/saved-reports")
    def report_create(request: Request, name: str = Form(""),
                      notes: str = Form(""), sql: str = Form(""),
                      db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        clean = name.strip()[:120]
        why = check_sql(sql)
        if not clean or why:
            return render(request, "report_edit.html", db, staff,
                          active="savedreports", r={"name": name, "notes": notes,
                                               "sql": sql, "id": None},
                          err=why or "Give it a name.")
        r = SavedReport(name=clean, notes=notes.strip() or None, sql=sql)
        db.add(r)
        db.commit()
        return RedirectResponse("/saved-reports/%d" % r.id, status_code=303)

    @app.get("/saved-reports/{rid}/edit", response_class=HTMLResponse)
    def report_edit(request: Request, rid: int,
                    db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        r = db.get(SavedReport, rid)
        if not r:
            return RedirectResponse("/saved-reports", status_code=303)
        return render(request, "report_edit.html", db, staff, active="savedreports",
                      r=r, err="")

    @app.post("/saved-reports/{rid}")
    def report_save(request: Request, rid: int, name: str = Form(""),
                    notes: str = Form(""), sql: str = Form(""),
                    db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        r = db.get(SavedReport, rid)
        if not r:
            return RedirectResponse("/saved-reports", status_code=303)
        why = check_sql(sql)
        if why:
            return render(request, "report_edit.html", db, staff,
                          active="savedreports", r=r, err=why)
        r.name = name.strip()[:120] or r.name
        r.notes = notes.strip() or None
        r.sql = sql
        r.updated_at = datetime.now(timezone.utc)
        db.commit()
        return RedirectResponse("/saved-reports/%d" % rid, status_code=303)

    @app.post("/saved-reports/{rid}/delete")
    def report_delete(request: Request, rid: int,
                      db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        r = db.get(SavedReport, rid)
        if r:
            db.delete(r)
            db.commit()
        return RedirectResponse("/saved-reports", status_code=303)

    def _params(request, r):
        """What this report needs bound, from what the URL was given."""
        if not wants_event(r.sql):
            return {}, None
        raw = (request.query_params.get("event") or "").strip()
        return ({"event_id": int(raw)} if raw.isdigit() else {},
                int(raw) if raw.isdigit() else None)

    # Before the {rid} route on purpose. FastAPI matches the first
    # pattern that fits, and "1.csv" fails {rid}'s int with a 422
    # rather than falling through to this one.
    @app.get("/saved-reports/{rid}.csv")
    def report_csv(request: Request, rid: int,
                   db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        r = db.get(SavedReport, rid)
        if not r:
            return RedirectResponse("/saved-reports", status_code=303)
        params, eid = _params(request, r)
        if wants_event(r.sql) and eid is None:
            return RedirectResponse("/saved-reports/%d" % rid, status_code=303)
        why = check_sql(r.sql)
        if why:
            return RedirectResponse("/saved-reports/%d" % rid, status_code=303)
        cols, rows = run_report(db, _bind(r.sql, params), MAX_ROWS)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for row in rows:
            w.writerow([cell(v) for v in row])
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        slug = re.sub(r"[^a-z0-9]+", "-", r.name.lower()).strip("-")[:60]
        return Response(
            buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition":
                     'attachment; filename="%s-%s.csv"' % (slug, stamp)})

    @app.get("/saved-reports/{rid}", response_class=HTMLResponse)
    def report_run(request: Request, rid: int,
                   db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        r = db.get(SavedReport, rid)
        if not r:
            return RedirectResponse("/saved-reports", status_code=303)
        params, eid = _params(request, r)
        cols, rows, err, total = [], [], "", 0
        needs = wants_event(r.sql) and eid is None
        if not needs:
            why = check_sql(r.sql)
            if why:
                err = why
            else:
                try:
                    cols, rows = run_report(
                        db, _bind(r.sql, params), MAX_ROWS)
                    total = len(rows)
                except Exception as exc:                     # noqa: BLE001
                    # Whatever Postgres said, said plainly. A report is
                    # somebody writing SQL; the error is the useful half.
                    err = str(getattr(exc, "orig", exc)).strip()[:600]
        return render(request, "report_run.html", db, staff, active="savedreports",
                      r=r, cols=cols, rows=rows[:PREVIEW_ROWS], total=total,
                      err=err, needs_event=needs, eid=eid,
                      events=_events(db) if wants_event(r.sql) else [],
                      capped=total >= MAX_ROWS, preview=PREVIEW_ROWS,
                      cell=cell)


def _bind(sql: str, params: dict) -> str:
    """The statement with its parameters bound.

    Substituted rather than passed through, because :event_id has to survive
    being inside a CTE and Postgres never sees the name - and every value here
    is an int this file parsed itself, so there is nothing a string could
    carry in.
    """
    out = sql
    for k, v in params.items():
        out = out.replace(":" + k, str(int(v)))
    return out
