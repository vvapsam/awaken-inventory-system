"""AWAKEN kiosk flows — public Walk-in (day pass) and Sign up (membership).

A member scans the one hub QR at the desk (it carries the same secret key as the
waiver), lands on /welcome, and picks an option. Walk-in and Sign up open here:
they sign the waiver, choose a plan, and pay (cash or bank transfer). Each
submission stores a Waiver and creates a *pending* order in the staff approval
queue — nothing is final until staff confirm. Confirming a Sign-up also creates
and links a member record (handled in order._confirm_order).

Prices/plans are admin-editable at /admin/kiosk, so the owner sets real numbers
without a redeploy. The key + one-time token + per-IP rate limit (shared with the
waiver) keep the public URL from being flooded.
"""

import base64
import io
import os
import secrets
from datetime import datetime, timezone

import qrcode
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import get_db
from .models import (
    Waiver, WaiverToken, KioskPlan, Transaction, TransactionItem, TX_ORDER,
    KIOSK_MEMBERSHIP, KIOSK_WALKIN, can, can_any,
)
from .waiver import (
    _settings, _client_ip, _prune_tokens,
    REFERRAL_OPTIONS, TOKEN_TTL, RATE_WINDOW, RATE_MAX, MAX_SIG,
)
from .order import next_order_number

router = APIRouter()
BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["peso"] = lambda v: "₱{:,.2f}".format(float(v or 0))
templates.env.globals["can"] = can
templates.env.globals["can_any"] = can_any

MAX_PROOF = 10 * 1024 * 1024               # 10 MB payment screenshot cap

FLOWS = ("walkin", "signup")


def _err(msg, code=400, **extra):
    d = {"ok": False, "error": msg}
    d.update(extra)
    return JSONResponse(d, status_code=code)


def _active_plans(db, kind):
    return (db.query(KioskPlan)
            .filter(KioskPlan.kind == kind, KioskPlan.is_active == True)  # noqa: E712
            .order_by(KioskPlan.sort, KioskPlan.id).all())


def _plan_brief(p):
    return {"id": p.id, "name": p.name, "subtitle": p.subtitle or "",
            "price": float(p.price or 0)} if p else None


def _walkin_context(db):
    """Open Gym + Private Coaching (flat) and the HYROX 2×2 rate grid."""
    rows = _active_plans(db, KIOSK_WALKIN)
    og = next((p for p in rows if p.activity == "open_gym"), None)
    pv = next((p for p in rows if p.activity == "private"), None)
    hy = [{"id": p.id, "coached": bool(p.coached), "doubles": bool(p.doubles),
           "name": p.name, "price": float(p.price or 0)}
          for p in rows if p.activity == "hyrox"]
    return {"open_gym": _plan_brief(og), "private": _plan_brief(pv), "hyrox": hy}


def _latest_waiver_by_email(db, email):
    email = (email or "").strip().lower()
    if not email:
        return None
    return (db.query(Waiver).filter(func.lower(Waiver.email) == email)
            .order_by(Waiver.signed_at.desc()).first())


def _decode_data_uri(s, cap, default_mime):
    """('data:image/png;base64,...') -> (raw_bytes, mime) or (None, error_str)."""
    if not s or not s.startswith("data:image/"):
        return None, "missing"
    try:
        header, b64 = s.split(",", 1)
        raw = base64.b64decode(b64)
    except Exception:
        return None, "unreadable"
    if not raw or len(raw) > cap:
        return None, "size"
    mime = default_mime
    if header.startswith("data:") and ";" in header:
        mime = header[5:header.index(";")] or default_mime
    return raw, mime


# ------------------------------------------------------------------ public flow
@router.get("/kiosk/{flow}", response_class=HTMLResponse)
def kiosk_page(flow: str, request: Request, db: Session = Depends(get_db)):
    if flow not in FLOWS:
        return RedirectResponse("/welcome", status_code=303)
    # Reached from the public Welcome hub, so no secret key is required here — a
    # one-time token + per-IP rate limit + staff approval guard against abuse.
    # (The standalone /waiver page keeps its own key gate.)
    _prune_tokens(db)
    token = secrets.token_urlsafe(12)
    db.add(WaiverToken(token=token))
    db.commit()
    ps = _settings(db)
    bank = {"bank_name": ps.bank_name or "", "account_name": ps.account_name or "",
            "has_qr": bool(ps.qr)}
    if flow == "signup":
        plans = [_plan_brief(p) for p in _active_plans(db, KIOSK_MEMBERSHIP)]
        return templates.TemplateResponse("kiosk.html", {
            "request": request, "flow": "signup", "valid": True, "k": "",
            "token": token, "plans": plans, "bank": bank, "referrals": REFERRAL_OPTIONS})
    # walk-in: email lookup → (new: waiver / returning: skip) → activity → pay
    wk = _walkin_context(db)
    return templates.TemplateResponse("kiosk_walkin.html", {
        "request": request, "valid": True, "k": "", "token": token,
        "open_gym": wk["open_gym"], "private": wk["private"], "hyrox": wk["hyrox"],
        "bank": bank, "referrals": REFERRAL_OPTIONS})


@router.post("/api/kiosk/lookup")
async def kiosk_lookup(request: Request, db: Session = Depends(get_db)):
    """Walk-in email lookup: has this person signed a waiver before?"""
    data = await request.json()
    email = (data.get("email") or "").strip()
    if not email or "@" not in email:
        return _err("Please enter a valid email")
    w = _latest_waiver_by_email(db, email)
    if w:
        return {"ok": True, "found": True, "first_name": w.first_name,
                "last_name": w.last_name, "phone": w.phone or ""}
    return {"ok": True, "found": False}


def _check_token(db, tok):
    """Return (row, error_response). error_response is set if the token is bad."""
    row = db.get(WaiverToken, tok) if tok else None
    now = datetime.now(timezone.utc)
    if (not row or row.used or (row.created_at and (now - row.created_at) > TOKEN_TTL)):
        return None, JSONResponse(
            {"ok": False, "expired": True,
             "error": "This link has already been used or has expired. "
                      "Please scan the QR at the front desk again."}, status_code=410)
    return row, None


@router.post("/api/kiosk/submit")
async def kiosk_submit(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    flow = (data.get("flow") or "").strip()
    if flow not in FLOWS:
        return _err("Unknown flow")

    row, terr = _check_token(db, (data.get("token") or "").strip())
    if terr:
        return terr

    now = datetime.now(timezone.utc)
    ip = _client_ip(request)
    if ip:
        since = now - RATE_WINDOW
        recent = (db.query(func.count(Waiver.id))
                  .filter(Waiver.ip == ip, Waiver.created_at >= since).scalar() or 0)
        if recent >= RATE_MAX:
            return _err("Too many submissions from this device. Please try again later.", 429)

    method = (data.get("method") or "").lower().strip()
    if method not in ("cash", "bank"):
        return _err("Choose a payment method")

    # chosen plan must exist, be active, match this flow, and be priced
    want_kind = KIOSK_MEMBERSHIP if flow == "signup" else KIOSK_WALKIN
    try:
        plan = db.get(KioskPlan, int(data.get("plan_id")))
    except (TypeError, ValueError):
        plan = None
    if not plan or plan.kind != want_kind or not plan.is_active:
        return _err("Please choose a valid option")
    price = float(plan.price or 0)
    if price <= 0:
        return _err("This option isn't priced yet — please see the front desk.")

    proof_bytes = proof_mime = None
    if method == "bank":
        proof_bytes, proof_mime = _decode_data_uri(data.get("proof") or "", MAX_PROOF, "image/jpeg")
        if proof_bytes is None:
            return _err("Please attach your payment screenshot" if proof_mime == "missing"
                        else "Screenshot is missing or too large")

    # ---- identity: returning walk-in matched by email; everyone else signs ----
    fn = (data.get("first_name") or "").strip()
    ln = (data.get("last_name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    returning = flow == "walkin" and bool(data.get("returning"))

    if returning:
        w = _latest_waiver_by_email(db, email)
        if not w:
            return _err("We couldn't find your record — please sign the waiver.",
                        409, need_waiver=True)
        fn, ln, phone = w.first_name, w.last_name, (w.phone or "")
    else:
        referral = (data.get("referral") or "").strip()
        if not fn or not ln:
            return _err("First and last name are required")
        if not email:
            return _err("Email is required")
        if not phone:
            return _err("Phone is required")
        if not referral:
            return _err("Please tell us how you found us")
        sig_raw, sig_mime = _decode_data_uri(data.get("signature") or "", MAX_SIG, "image/png")
        if sig_raw is None:
            return _err("Please sign before submitting" if sig_mime == "missing"
                        else "Signature is missing or too large")
        db.add(Waiver(first_name=fn, last_name=ln, email=email or None, phone=phone or None,
                      referral=referral or None, signature=sig_raw, signature_mime=sig_mime,
                      ip=ip or None, signed_at=now))

    # ---- pending order (lands in the staff approval queue) ----
    subtype = "kiosk_membership" if flow == "signup" else "kiosk_walkin"
    label = "Membership sign-up" if flow == "signup" else "Walk-in"
    order = Transaction(
        type=TX_ORDER, subtype=subtype, number=next_order_number(db),
        customer_name=("%s %s" % (fn, ln)).strip(), customer_phone=phone or None,
        payment_method=method, proof=proof_bytes, proof_mime=proof_mime,
        amount_snapshot=price, status="pending",
        note="%s · %s" % (label, plan.name))
    db.add(order)
    db.flush()
    db.add(TransactionItem(transaction_id=order.id, product_id=None, name=plan.name,
                           qty=1, unit_price=price))
    row.used = True                     # consume the one-time link
    db.commit()
    db.refresh(order)
    return {"ok": True, "number": order.number, "flow": flow, "plan": plan.name,
            "price": price, "method": method, "first_name": fn}


# ------------------------------------------------------------------ staff admin
def _require_admin(request, db):
    staff = current_staff(request, db)
    if not staff:
        return None, RedirectResponse("/login", status_code=303)
    if staff.role != "admin":
        return None, RedirectResponse("/dashboard", status_code=303)
    return staff, None


def _qr_data_uri(text):
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0b2a26", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _hub_link(request, db):
    host = (request.headers.get("host") or "").split(",")[0].strip()
    if not host or host == "pay.awakengym.com":
        host = "portal.awakengym.com"
    return "https://%s/welcome" % host


def _hyrox_grid(rows):
    """The four HYROX rows keyed by (coached, doubles) for a 2×2 admin grid."""
    by = {(bool(p.coached), bool(p.doubles)): p for p in rows}
    return {
        "self_solo": by.get((False, False)), "self_dbl": by.get((False, True)),
        "coach_solo": by.get((True, False)), "coach_dbl": by.get((True, True)),
    }


@router.get("/admin/kiosk", response_class=HTMLResponse)
def kiosk_admin(request: Request, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    walkin = _active_plans(db, KIOSK_WALKIN) + [
        p for p in db.query(KioskPlan).filter(KioskPlan.kind == KIOSK_WALKIN,
                                              KioskPlan.is_active == False).all()]  # noqa: E712
    open_gym = next((p for p in walkin if p.activity == "open_gym"), None)
    private = next((p for p in walkin if p.activity == "private"), None)
    hyrox = _hyrox_grid([p for p in walkin if p.activity == "hyrox"])
    memberships = (db.query(KioskPlan).filter(KioskPlan.kind == KIOSK_MEMBERSHIP)
                   .order_by(KioskPlan.sort, KioskPlan.id).all())
    link = _hub_link(request, db)
    return templates.TemplateResponse("kiosk_admin.html", {
        "request": request, "staff": staff, "open_gym": open_gym, "private": private,
        "hyrox": hyrox, "memberships": memberships,
        "hub_link": link, "hub_qr": _qr_data_uri(link)})


@router.post("/admin/kiosk")
async def kiosk_admin_save(request: Request, db: Session = Depends(get_db)):
    staff, redir = _require_admin(request, db)
    if redir:
        return redir
    form = await request.form()
    for p in db.query(KioskPlan).all():
        pid = str(p.id)
        if form.get("del_%s" % pid):
            db.delete(p)
            continue
        if ("name_%s" % pid) in form:
            nm = (form.get("name_%s" % pid) or "").strip()
            if nm:
                p.name = nm
        if ("sub_%s" % pid) in form:
            p.subtitle = (form.get("sub_%s" % pid) or "").strip() or None
        if ("price_%s" % pid) in form:
            try:
                p.price = float(form.get("price_%s" % pid) or 0)
            except ValueError:
                pass
        if ("hasactive_%s" % pid) in form:      # only rows that render a toggle
            p.is_active = bool(form.get("active_%s" % pid))
    # add new membership plans (blank rows are ignored)
    for i in range(1, 6):
        nm = (form.get("new_name_%d" % i) or "").strip()
        if not nm:
            continue
        try:
            pr = float(form.get("new_price_%d" % i) or 0)
        except ValueError:
            pr = 0
        db.add(KioskPlan(kind=KIOSK_MEMBERSHIP, name=nm,
                         subtitle=(form.get("new_sub_%d" % i) or "").strip() or None,
                         price=pr, sort=100 + i))
    db.commit()
    return RedirectResponse("/admin/kiosk", status_code=303)
