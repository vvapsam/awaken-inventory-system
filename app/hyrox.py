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
from datetime import datetime, timezone

import qrcode
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import get_db
from .models import HyroxGroup, HYROX_STATIONS, can, can_any

router = APIRouter()
BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["peso"] = lambda v: "₱{:,.2f}".format(float(v or 0))
templates.env.globals["can"] = can
templates.env.globals["can_any"] = can_any

NSTN = len(HYROX_STATIONS)


def _now():
    return datetime.now(timezone.utc)


def _splits(g):
    return [int(x) for x in (g.splits or "").split(",") if x.strip()]


def _state(g):
    sl = _splits(g)
    done = len(sl)
    running = g.running_since is not None and done < NSTN
    elapsed = (_now() - g.running_since).total_seconds() if running else 0
    return {
        "id": g.id, "name": g.name, "tag": g.tag, "emblem": g.emblem or "",
        "color": g.color or "#18BE7C", "done": done, "splits": sl,
        "running": running, "finished": done >= NSTN,
        "station_elapsed": int(elapsed) if running else 0,
        "total": int(sum(sl) + (elapsed if running else 0)),
    }


# --------------------------------------------------------------- coach pages
@router.get("/coach", response_class=HTMLResponse)
def coach_pick(request: Request, db: Session = Depends(get_db)):
    groups = [{"id": g.id, "name": g.name, "tag": g.tag, "emblem": g.emblem or "",
               "color": g.color or "#18BE7C"}
              for g in db.query(HyroxGroup).order_by(HyroxGroup.sort).all()]
    return templates.TemplateResponse("coach_pick.html", {"request": request, "groups": groups})


@router.get("/coach/{gid}", response_class=HTMLResponse)
def coach_timer(gid: int, request: Request, db: Session = Depends(get_db)):
    g = db.get(HyroxGroup, gid)
    if not g:
        return RedirectResponse("/coach", status_code=303)
    return templates.TemplateResponse("coach_timer.html", {
        "request": request, "g": _state(g), "stations": HYROX_STATIONS})


# --------------------------------------------------------------- live data API
@router.get("/api/hyrox/state")
def hyrox_state(db: Session = Depends(get_db)):
    gs = db.query(HyroxGroup).order_by(HyroxGroup.sort).all()
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
        sl.append(max(1, round((_now() - g.running_since).total_seconds())))
        g.splits = ",".join(str(x) for x in sl)
        g.running_since = None
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
    gs = [_state(g) for g in db.query(HyroxGroup).order_by(HyroxGroup.sort).all()]
    base = _host_base(request)
    coach_url = base + "/coach"
    return templates.TemplateResponse("hyrox_admin.html", {
        "request": request, "staff": staff, "groups": gs,
        "board_url": base + "/board", "coach_url": coach_url,
        "coach_qr": _qr_data_uri(coach_url)})


@router.post("/admin/hyrox/reset")
def hyrox_reset(request: Request, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    for g in db.query(HyroxGroup).all():
        g.splits = ""
        g.running_since = None
    db.commit()
    return RedirectResponse("/admin/hyrox", status_code=303)
