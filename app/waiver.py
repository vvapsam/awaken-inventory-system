"""AWAKEN Liability Waiver — public /waiver intake form + staff review.

Members scan a QR at the desk (which carries a secret key), fill their details
+ "how did you find us", sign on their own phone, and submit. The key keeps the
bare URL from being spammed; a per-IP rate limit backs it up. Admins review,
print the QR, rotate the key, and delete entries at /admin/waivers.
"""

import base64
import io
import os
import secrets
from datetime import datetime, timedelta, timezone

import qrcode
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import get_db
from .models import Waiver, WaiverToken, PaymentSetting, can, can_any

router = APIRouter()
BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["peso"] = lambda v: "₱{:,.2f}".format(float(v or 0))
templates.env.globals["can"] = can
templates.env.globals["can_any"] = can_any

MAX_SIG = 3 * 1024 * 1024          # 3 MB signature cap
RATE_MAX = 8                        # max waivers per IP per hour
RATE_WINDOW = timedelta(hours=1)
TOKEN_TTL = timedelta(minutes=45)   # an opened waiver link is good for 45 min, one use

REFERRAL_OPTIONS = [
    "Facebook", "Instagram", "TikTok", "Google search",
    "Walked by / saw the gym", "Friend / referral", "Flyer / poster", "Other",
]


def _err(msg, code=400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


def _settings(db):
    ps = db.get(PaymentSetting, 1)
    if not ps:
        ps = PaymentSetting(id=1, bank_name="", account_name="")
        db.add(ps); db.commit(); db.refresh(ps)
    return ps


def _waiver_key(db):
    ps = _settings(db)
    if not ps.waiver_key:
        ps.waiver_key = secrets.token_urlsafe(9)
        db.commit()
    return ps.waiver_key


def _client_ip(request):
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def _prune_tokens(db):
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    db.query(WaiverToken).filter(WaiverToken.created_at < cutoff).delete(synchronize_session=False)
    db.commit()


# ------------------------------------------------------------------ public page
@router.get("/waiver", response_class=HTMLResponse)
def waiver_page(request: Request, k: str = "", db: Session = Depends(get_db)):
    valid = bool(k) and k == _waiver_key(db)
    token = ""
    if valid:
        _prune_tokens(db)
        token = secrets.token_urlsafe(12)
        db.add(WaiverToken(token=token))
        db.commit()
    return templates.TemplateResponse("waiver.html", {
        "request": request, "valid": valid, "k": k, "token": token,
        "referrals": REFERRAL_OPTIONS})


@router.post("/api/waiver")
async def submit_waiver(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    if (data.get("key") or "") != _waiver_key(db):
        return _err("This link is invalid or expired. Please scan the QR at the front desk.", 403)

    # one-time link: the token issued when this page opened must be unused + fresh
    tok = (data.get("token") or "").strip()
    row = db.get(WaiverToken, tok) if tok else None
    now = datetime.now(timezone.utc)
    if (not row or row.used or
            (row.created_at and (now - row.created_at) > TOKEN_TTL)):
        return JSONResponse({"ok": False, "expired": True,
                             "error": "This link has already been used or has expired. "
                                      "Please scan the QR at the front desk again."},
                            status_code=410)

    ip = _client_ip(request)
    if ip:
        since = now - RATE_WINDOW
        recent = (db.query(func.count(Waiver.id))
                  .filter(Waiver.ip == ip, Waiver.created_at >= since).scalar() or 0)
        if recent >= RATE_MAX:
            return _err("Too many submissions from this device. Please try again later.", 429)

    fn = (data.get("first_name") or "").strip()
    ln = (data.get("last_name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    referral = (data.get("referral") or "").strip()
    sig = data.get("signature") or ""
    if not fn or not ln:
        return _err("First and last name are required")
    if not email:
        return _err("Email is required")
    if not phone:
        return _err("Phone is required")
    if not referral:
        return _err("Please tell us how you found us")
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
               referral=referral or None,
               signature=raw, signature_mime=mime, ip=ip or None,
               signed_at=now)
    db.add(w)
    row.used = True                 # consume the one-time link
    db.commit()
    return {"ok": True, "id": w.id}


# ------------------------------------------------------------------ staff review
def _require_admin(request, db):
    staff = current_staff(request, db)
    if not staff:
        return None, RedirectResponse("/login", status_code=303)
    if staff.role != "admin":
        return None, RedirectResponse("/dashboard", status_code=303)
    return staff, None


def _qr_data_uri(text):
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(text); qr.make(fit=True)
    img = qr.make_image(fill_color="#0b2a26", back_color="white")
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@router.get("/admin/waivers", response_class=HTMLResponse)
def waivers_list(request: Request, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    rows = db.query(Waiver).order_by(Waiver.signed_at.desc()).limit(500).all()
    # marketing breakdown
    sources = {}
    for w in rows:
        key = w.referral or "—"
        sources[key] = sources.get(key, 0) + 1
    sources = sorted(sources.items(), key=lambda kv: -kv[1])
    key = _waiver_key(db)
    host = (request.headers.get("host") or "pay.awakengym.com").split(",")[0].strip()
    link = "https://%s/waiver?k=%s" % (host, key)
    return templates.TemplateResponse("waivers.html", {
        "request": request, "staff": staff, "waivers": rows, "sources": sources,
        "waiver_link": link, "waiver_qr": _qr_data_uri(link)})


@router.get("/waiver-sig/{wid}")
def waiver_sig(request: Request, wid: int, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    w = db.get(Waiver, wid)
    if not w or not w.signature:
        return Response(status_code=404)
    return Response(content=w.signature, media_type=w.signature_mime or "image/png")


@router.post("/admin/waivers/{wid}/delete")
def waiver_delete(request: Request, wid: int, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    w = db.get(Waiver, wid)
    if w:
        db.delete(w); db.commit()
    return RedirectResponse("/admin/waivers", status_code=303)


@router.post("/admin/waiver/rotate-key")
def waiver_rotate(request: Request, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    ps = _settings(db)
    ps.waiver_key = secrets.token_urlsafe(9)
    db.commit()
    return RedirectResponse("/admin/waivers", status_code=303)
