"""AWAKEN Liability Waiver — public /waiver page + staff review list.

A visitor fills first/last name, email, phone, and signs on-screen. The signed
waiver (details + signature image) is stored and reviewable by admins at
/admin/waivers. Standalone from customer records for now.
"""

import base64
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import get_db
from .models import Waiver, can, can_any

router = APIRouter()
BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["peso"] = lambda v: "₱{:,.2f}".format(float(v or 0))
templates.env.globals["can"] = can
templates.env.globals["can_any"] = can_any

MAX_SIG = 3 * 1024 * 1024  # 3 MB safety cap on the signature image


def _err(msg, code=400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


@router.get("/waiver", response_class=HTMLResponse)
def waiver_page(request: Request):
    return templates.TemplateResponse("waiver.html", {"request": request})


@router.post("/api/waiver")
async def submit_waiver(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    fn = (data.get("first_name") or "").strip()
    ln = (data.get("last_name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    sig = data.get("signature") or ""
    if not fn or not ln:
        return _err("First and last name are required")
    if not email:
        return _err("Email is required")
    if not phone:
        return _err("Phone is required")
    # signature is a data URL: "data:image/png;base64,...."
    if not sig.startswith("data:image/"):
        return _err("Please sign before submitting")
    try:
        header, b64 = sig.split(",", 1)
        raw = base64.b64decode(b64)
    except Exception:
        return _err("Could not read the signature")
    if not raw or len(raw) > MAX_SIG:
        return _err("Signature is missing or too large")
    mime = "image/png"
    if header.startswith("data:") and ";" in header:
        mime = header[5:header.index(";")] or "image/png"

    w = Waiver(first_name=fn, last_name=ln, email=email or None, phone=phone or None,
               signature=raw, signature_mime=mime,
               signed_at=datetime.now(timezone.utc))
    db.add(w)
    db.commit()
    return {"ok": True, "id": w.id}


# ---- staff review ----
def _require_admin(request, db):
    staff = current_staff(request, db)
    if not staff:
        return None, RedirectResponse("/login", status_code=303)
    if staff.role != "admin":
        return None, RedirectResponse("/dashboard", status_code=303)
    return staff, None


@router.get("/admin/waivers", response_class=HTMLResponse)
def waivers_list(request: Request, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    rows = db.query(Waiver).order_by(Waiver.signed_at.desc()).limit(500).all()
    return templates.TemplateResponse("waivers.html",
                                      {"request": request, "staff": staff, "waivers": rows})


@router.get("/waiver-sig/{wid}")
def waiver_sig(request: Request, wid: int, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    w = db.get(Waiver, wid)
    if not w or not w.signature:
        return Response(status_code=404)
    return Response(content=w.signature, media_type=w.signature_mime or "image/png")
