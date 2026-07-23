"""AWAKEN HYROX relay — coach timer app + live scoreboard data.

Each coach opens /coach on their phone, picks the group they're timing, then taps
Start when the team begins a station and Finish when they complete it. Every finish
stamps that station's split and the /board scoreboard updates live.

Progress model (see HyroxGroup): `splits` is a CSV of completed station times (secs,
in order); `running_since` is set while the current station is being timed. So
done = len(splits), current station index = done, and total time = sum(splits) +
current elapsed. Public (no login) — it's an on-site event tool, like the kiosk.
"""

import base64
import io
import os
from datetime import datetime, timezone, timedelta

import qrcode
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import get_db
from .models import HyroxGroup, HYROX_STATIONS, HYROX_STATION_DETAIL, can, can_any

try:
    from zoneinfo import ZoneInfo
    MNL = ZoneInfo("Asia/Manila")
except Exception:                                          # pragma: no cover
    MNL = timezone(timedelta(hours=8))

router = APIRouter()
BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["peso"] = lambda v: "₱{:,.2f}".format(float(v or 0))
templates.env.globals["can"] = can
templates.env.globals["can_any"] = can_any

NSTN = len(HYROX_STATIONS)
SLOT_MIN = 15                                              # default gap between group starts


def _mmss(s):
    s = int(s or 0)
    return "%d:%02d" % (s // 60, s % 60)


templates.env.globals["mmss"] = _mmss


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    """Postgres returns tz-aware; SQLite/older rows may be naive UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _clock(dt):
    dt = _aware(dt)
    return dt.astimezone(MNL).strftime("%-I:%M %p") if dt else None


def _splits(g):
    return [int(x) for x in (g.splits or "").split(",") if x.strip()]


def _schedule_order(g):
    """Gun-start ordering: all Team A first, then all Team B; within each tag, by
    team (Eagles, Foxes, Pulag, Logan — from the seeded sort). So A's take the early
    slots (5:00, 5:15, …) and B's the later ones (6:00, 6:15, …)."""
    return (0 if (g.tag or "").upper() == "A" else 1, g.sort)


def apply_schedule(db, base_local_naive, interval=SLOT_MIN):
    """Set each group's gun start at `interval`-minute steps in schedule order.
    `base_local_naive` is a naive datetime interpreted in Manila local time."""
    base = base_local_naive.replace(tzinfo=MNL).astimezone(timezone.utc)
    rows = sorted(db.query(HyroxGroup).all(), key=_schedule_order)
    for i, g in enumerate(rows):
        g.start_at = base + timedelta(minutes=interval * i)
        g.finished_at = None
    db.commit()


def ensure_default_schedule(db):
    """First-run default: the next upcoming 5:00 AM, 15 min apart. Only if unset."""
    if db.query(HyroxGroup).filter(HyroxGroup.start_at.isnot(None)).first():
        return
    now_l = datetime.now(MNL)
    base = now_l.replace(hour=5, minute=0, second=0, microsecond=0)
    if base <= now_l:
        base += timedelta(days=1)
    apply_schedule(db, base.replace(tzinfo=None))


def _state(g):
    sl = _splits(g)
    done = len(sl)
    finished = done >= NSTN
    rs = _aware(g.running_since)
    station_running = rs is not None and not finished
    station_elapsed = int((_now() - rs).total_seconds()) if station_running else 0
    start_at = _aware(g.start_at)
    fin_at = _aware(g.finished_at)
    # The headline clock is the race clock: it runs from the fixed gun start and
    # freezes at the coach's Wallballs finish. Falls back to summed splits if no
    # schedule has been set yet.
    if start_at is None:
        started, ticking = True, station_running
        total = int(sum(sl) + station_elapsed)
    elif finished and fin_at is not None:
        started, ticking = True, False
        total = max(0, int((fin_at - start_at).total_seconds()))
    elif _now() >= start_at:
        started, ticking = True, not finished
        total = int((_now() - start_at).total_seconds())
    else:
        started, ticking = False, False
        total = 0
    return {
        "id": g.id, "name": g.name, "tag": g.tag, "emblem": g.emblem or "",
        "coach": g.coach or "", "color": g.color or "#18BE7C", "done": done, "splits": sl,
        "station_running": station_running, "station_elapsed": station_elapsed,
        "finished": finished, "started": started, "ticking": ticking,
        "start_at": start_at.isoformat() if start_at else None,
        "start_clock": _clock(start_at), "finish_clock": _clock(fin_at) if finished else None,
        "total": total,
    }


# --------------------------------------------------------------- coach pages
@router.get("/coach", response_class=HTMLResponse)
def coach_pick(request: Request, db: Session = Depends(get_db)):
    groups = [_state(g) for g in db.query(HyroxGroup).order_by(HyroxGroup.start_at.asc(), HyroxGroup.sort).all()]
    return templates.TemplateResponse("coach_pick.html", {"request": request, "groups": groups})


@router.get("/coach/{gid}", response_class=HTMLResponse)
def coach_timer(gid: int, request: Request, db: Session = Depends(get_db)):
    g = db.get(HyroxGroup, gid)
    if not g:
        return RedirectResponse("/coach", status_code=303)
    return templates.TemplateResponse("coach_timer.html", {
        "request": request, "g": _state(g), "stations": HYROX_STATIONS,
        "details": [HYROX_STATION_DETAIL.get(s, "") for s in HYROX_STATIONS]})


# --------------------------------------------------------------- live data API
@router.get("/api/hyrox/state")
def hyrox_state(db: Session = Depends(get_db)):
    gs = db.query(HyroxGroup).order_by(HyroxGroup.start_at.asc(), HyroxGroup.sort).all()
    return {"ok": True, "stations": HYROX_STATIONS, "groups": [_state(g) for g in gs]}


@router.post("/api/hyrox/{gid}/start")
async def hyrox_start(gid: int, db: Session = Depends(get_db)):
    g = db.get(HyroxGroup, gid)
    if not g:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if len(_splits(g)) < NSTN and g.running_since is None:
        g.running_since = _now()
        db.commit()
    db.refresh(g)
    return {"ok": True, "group": _state(g)}


@router.post("/api/hyrox/{gid}/finish")
async def hyrox_finish(gid: int, db: Session = Depends(get_db)):
    g = db.get(HyroxGroup, gid)
    if not g:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    sl = _splits(g)
    if g.running_since is not None and len(sl) < NSTN:
        sl.append(max(1, round((_now() - _aware(g.running_since)).total_seconds())))
        g.splits = ",".join(str(x) for x in sl)
        g.running_since = None
        if len(sl) >= NSTN:                # Wallballs done → stamp the race finish
            g.finished_at = _now()
        db.commit()
    db.refresh(g)
    return {"ok": True, "group": _state(g)}


@router.post("/api/hyrox/{gid}/undo")
async def hyrox_undo(gid: int, db: Session = Depends(get_db)):
    """Cancel a running station, or (if idle) step back and drop the last split."""
    g = db.get(HyroxGroup, gid)
    if not g:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if g.running_since is not None:
        g.running_since = None
    else:
        sl = _splits(g)
        if sl:
            sl.pop()
            g.splits = ",".join(str(x) for x in sl)
    if len(_splits(g)) < NSTN:              # no longer finished → unfreeze the clock
        g.finished_at = None
    db.commit()
    db.refresh(g)
    return {"ok": True, "group": _state(g)}


# --------------------------------------------------------------- staff admin
def _require_admin(request, db):
    staff = current_staff(request, db)
    if not staff:
        return None, RedirectResponse("/login", status_code=303)
    if staff.role != "admin":
        return None, RedirectResponse("/dashboard", status_code=303)
    return staff, None


def _host_base(request):
    host = (request.headers.get("host") or "").split(",")[0].strip()
    if not host or host == "pay.awakengym.com":
        host = "portal.awakengym.com"
    return "https://%s" % host


def _qr_data_uri(text):
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0b2a26", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@router.get("/admin/hyrox", response_class=HTMLResponse)
def hyrox_admin(request: Request, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    rows = db.query(HyroxGroup).order_by(HyroxGroup.start_at.asc(), HyroxGroup.sort).all()
    gs = [_state(g) for g in rows]
    base = _host_base(request)
    coach_url = base + "/coach"
    # Pre-fill the schedule form from the first group's current start (or upcoming 5:00).
    first = _aware(rows[0].start_at) if rows else None
    if first:
        first_l = first.astimezone(MNL)
    else:
        first_l = datetime.now(MNL).replace(hour=5, minute=0, second=0, microsecond=0)
    return templates.TemplateResponse("hyrox_admin.html", {
        "request": request, "staff": staff, "groups": gs,
        "board_url": base + "/board", "coach_url": coach_url,
        "coach_qr": _qr_data_uri(coach_url), "stations": HYROX_STATIONS,
        "sched_date": first_l.strftime("%Y-%m-%d"),
        "sched_time": first_l.strftime("%H:%M"),
        "sched_interval": SLOT_MIN})


@router.get("/admin/hyrox/results.csv")
def hyrox_results_csv(request: Request, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Group", "Coach", "Start"] + HYROX_STATIONS + ["Total", "Finished at"])
    for g in db.query(HyroxGroup).order_by(HyroxGroup.start_at.asc(), HyroxGroup.sort).all():
        st = _state(g)
        sl = st["splits"]
        cells = [_mmss(sl[i]) if i < len(sl) else "" for i in range(NSTN)]
        total = _mmss(st["total"]) if (st["finished"] or st["started"]) else ""
        w.writerow(["%s %s" % (g.name, g.tag), g.coach or "", st["start_clock"] or ""]
                   + cells + [total, st["finish_clock"] or ""])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=hyrox-results.csv"})


@router.post("/admin/hyrox/schedule")
def hyrox_schedule(request: Request, date: str = Form(...), time: str = Form(...),
                   interval: int = Form(SLOT_MIN), db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    try:
        base = datetime.strptime("%s %s" % (date.strip(), time.strip()), "%Y-%m-%d %H:%M")
        apply_schedule(db, base, max(1, int(interval or SLOT_MIN)))
    except (ValueError, TypeError):
        pass
    return RedirectResponse("/admin/hyrox", status_code=303)


@router.post("/admin/hyrox/reset")
def hyrox_reset(request: Request, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    for g in db.query(HyroxGroup).all():
        g.splits = ""
        g.running_since = None
        g.finished_at = None                 # clear results; keep the start schedule
    db.commit()
    return RedirectResponse("/admin/hyrox", status_code=303)
