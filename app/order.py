"""AWAKEN Self-Checkout — public customer ordering + staff confirmation.

Public (no login) page at /order lets a customer scan a counter QR, pick items,
enter their name and pay by Cash (at counter) or Bank transfer (QR + proof
screenshot). The screenshot is read with OCR to check the amount is at least the
total and the date is today. Orders land in a staff queue for a person to
confirm — only then does it become a real Sale and deduct stock.
"""
import io
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import get_db
from .models import (
    PaymentSetting, Product, can, can_any,
    Transaction, TransactionItem, TX_ORDER, TX_CASH_SALE,
)

router = APIRouter()
BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["peso"] = lambda v: "₱{:,.2f}".format(float(v or 0))
templates.env.globals["can"] = can
templates.env.globals["can_any"] = can_any

MAX_IMAGE = 10 * 1024 * 1024


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("APP_TZ", "Asia/Manila"))
    except Exception:
        return timezone(timedelta(hours=8))


def _today():
    return datetime.now(_tz()).date()


def can_orders(staff):
    """Counter staff who can log sales can manage self-checkout orders."""
    return can(staff, "sales.create") or (staff and staff.role == "admin")


def _err(msg, code=400, **extra):
    return JSONResponse({"ok": False, "error": msg, **extra}, status_code=code)


def get_settings(db):
    ps = db.get(PaymentSetting, 1)
    if not ps:
        ps = PaymentSetting(id=1, bank_name="", account_name="")
        db.add(ps)
        db.commit()
        db.refresh(ps)
    return ps


def _read_image(upload):
    data = upload.file.read()
    if not data:
        raise ValueError("Empty file")
    if len(data) > MAX_IMAGE:
        raise ValueError("Image is too large (max 10 MB)")
    mime = upload.content_type or "image/jpeg"
    if not mime.startswith("image/"):
        raise ValueError("Attachment must be an image")
    return data, mime


def next_order_number(db):
    n = db.query(func.count(Transaction.id)).filter(Transaction.type == TX_ORDER).scalar() or 0
    return "ORD-%04d" % (n + 1)


# ----------------------------------------------------------- OCR verification
_MONTHS = {}
for _i, _m in enumerate(["january", "february", "march", "april", "may", "june",
                         "july", "august", "september", "october", "november",
                         "december"], 1):
    _MONTHS[_m] = _i
    _MONTHS[_m[:3]] = _i


def _find_date(text, today):
    cands = []
    for y, mo, d in re.findall(r'(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})', text):
        try:
            cands.append(date(int(y), int(mo), int(d)))
        except ValueError:
            pass
    for mon, d, y in re.findall(r'([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,?\s*(20\d{2}))?', text):
        mi = _MONTHS.get(mon.lower())
        if not mi:
            continue
        try:
            cands.append(date(int(y) if y else today.year, mi, int(d)))
        except ValueError:
            pass
    for d, mon, y in re.findall(r'(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(20\d{2})', text):
        mi = _MONTHS.get(mon.lower())
        if not mi:
            continue
        try:
            cands.append(date(int(y), mi, int(d)))
        except ValueError:
            pass
    for a, b, y in re.findall(r'(\d{1,2})/(\d{1,2})/(20\d{2})', text):
        for mo, d in ((int(a), int(b)), (int(b), int(a))):
            try:
                cands.append(date(int(y), mo, d))
            except ValueError:
                pass
    if not cands:
        return None
    pool = [c for c in cands if abs((c - today).days) <= 370] or cands
    return min(pool, key=lambda c: abs((c - today).days))


def verify_proof(image_bytes, amount_due, today):
    """Best-effort OCR read. Returns dict with amount/date checks.
    A check value of None means 'could not determine from the image'."""
    out = {"amount_ok": None, "detected_amount": None,
           "date_ok": None, "detected_date": None, "note": ""}
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        # upscale small screenshots a bit for cleaner OCR
        if max(img.size) < 1000:
            f = 1000 / max(img.size)
            img = img.resize((int(img.width * f), int(img.height * f)))
        text = pytesseract.image_to_string(img)
    except Exception as e:  # OCR unavailable / unreadable — leave as unknown
        out["note"] = "Could not read the screenshot automatically."
        return out

    # Only treat currency-tagged or 2-decimal figures as money — this avoids
    # reading reference/account numbers (which have neither) as the amount.
    cur = re.findall(r'(?:php|piso|₱|₱)\s*([0-9][0-9,]*(?:\.[0-9]{2})?)', text, re.I)
    dec = re.findall(r'(?<![0-9])([0-9][0-9,]*\.[0-9]{2})(?![0-9])', text)
    pool = cur if cur else dec
    vals = []
    for n in pool:
        try:
            v = float(n.replace(",", ""))
        except ValueError:
            continue
        if 0 < v < 1_000_000:
            vals.append(v)
    if vals:
        det = max(vals)
        out["detected_amount"] = round(det, 2)
        out["amount_ok"] = (det + 0.001) >= float(amount_due)

    d = _find_date(text, today)
    if d is not None:
        out["detected_date"] = d.isoformat()
        out["date_ok"] = (d >= today)

    parts = []
    if out["amount_ok"] is True:
        parts.append("amount ✓ (₱%s detected)" % f"{out['detected_amount']:,.2f}")
    elif out["amount_ok"] is False:
        parts.append("amount ✗ (only ₱%s detected, ₱%s due)"
                     % (f"{out['detected_amount']:,.2f}", f"{float(amount_due):,.2f}"))
    else:
        parts.append("amount not detected")
    if out["date_ok"] is True:
        parts.append("date ✓ (%s)" % out["detected_date"])
    elif out["date_ok"] is False:
        parts.append("date ✗ (%s — not today)" % out["detected_date"])
    else:
        parts.append("date not detected")
    out["note"] = "; ".join(parts)
    return out


# ================================================================= PUBLIC
@router.get("/order", response_class=HTMLResponse)
@router.get("/pay/retail", response_class=HTMLResponse)   # branded path alias
def order_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("order.html", {"request": request})


@router.get("/ocr-health")
def ocr_health():
    """Ops check: confirms the OCR engine that validates payment screenshots is
    live on this server. No user data involved."""
    out = {"ocr_available": False}
    try:
        import pytesseract
        out["tesseract_version"] = str(pytesseract.get_tesseract_version())
        out["ocr_available"] = True
    except Exception as e:
        out["error"] = str(e)
    try:
        import PIL
        out["pillow"] = PIL.__version__
    except Exception as e:
        out["pillow_error"] = str(e)
    return out


@router.get("/api/order/bootstrap")
def order_bootstrap(db: Session = Depends(get_db)):
    products = [
        {"id": p.id, "name": p.name, "price": float(p.selling_price or 0),
         "has_image": bool(p.image)}
        for p in db.query(Product).filter(Product.is_active).order_by(Product.name).all()
    ]
    ps = get_settings(db)
    return {
        "ok": True,
        "products": products,
        "pay": {"bank_name": ps.bank_name or "", "account_name": ps.account_name or "",
                "has_qr": bool(ps.qr)},
        "has_logo": bool(ps.logo),
    }


@router.get("/order-logo")
def order_logo(db: Session = Depends(get_db)):
    ps = db.get(PaymentSetting, 1)
    if not ps or not ps.logo:
        return Response(status_code=404)
    return Response(content=ps.logo, media_type=ps.logo_mime or "image/png",
                    headers={"Cache-Control": "no-store"})


@router.get("/order-qr")
def order_qr(db: Session = Depends(get_db)):
    ps = db.get(PaymentSetting, 1)
    if not ps or not ps.qr:
        return Response(status_code=404)
    return Response(content=ps.qr, media_type=ps.qr_mime or "image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/api/order")
async def place_order(
    request: Request,
    items: str = Form(...),
    name: str = Form(...),
    phone: str = Form(None),
    method: str = Form(...),
    proof: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    name = (name or "").strip()
    if not name:
        return _err("Please enter your name")
    method = (method or "").lower().strip()
    if method not in ("cash", "bank"):
        return _err("Choose a payment method")
    try:
        raw = json.loads(items or "[]")
    except Exception:
        return _err("Bad item data")
    if not raw:
        return _err("Your cart is empty")

    lines = []
    total = 0.0
    for it in raw:
        p = db.get(Product, int(it["product_id"]))
        qty = int(it["qty"])
        if not p or qty <= 0:
            return _err("Invalid item in cart")
        lines.append((p, qty))
        total += float(p.selling_price) * qty

    proof_bytes = proof_mime = None
    checks = None
    if method == "bank":
        # screenshot required
        if proof is None or not getattr(proof, "filename", ""):
            return _err("Please attach your payment screenshot")
        try:
            proof_bytes, proof_mime = _read_image(proof)
        except ValueError as e:
            return _err(str(e))
        checks = verify_proof(proof_bytes, total, _today())
        # Block on a *confident* failure; unknowns pass through for staff to check.
        if checks["amount_ok"] is False:
            return _err("The screenshot shows ₱%s, but the total due is ₱%s. "
                        "Please pay the full amount and upload the correct screenshot."
                        % (f"{checks['detected_amount']:,.2f}", f"{total:,.2f}"),
                        checks=checks)
        if checks["date_ok"] is False:
            return _err("The screenshot appears dated %s, not today. "
                        "Please upload today's payment screenshot."
                        % checks["detected_date"], checks=checks)

    order = Transaction(
        type=TX_ORDER,
        number=next_order_number(db),
        customer_name=name,
        customer_phone=(phone or "").strip() or None,
        payment_method=method,
        proof=proof_bytes, proof_mime=proof_mime,
        amount_snapshot=total, status="pending",
    )
    if checks:
        order.check_amount_ok = checks["amount_ok"]
        order.check_detected_amount = checks["detected_amount"]
        order.check_date_ok = checks["date_ok"]
        order.check_detected_date = checks["detected_date"]
        order.check_note = checks["note"]
    db.add(order)
    db.flush()
    for p, qty in lines:
        db.add(TransactionItem(transaction_id=order.id, product_id=p.id, name=p.name,
                               qty=qty, unit_price=p.selling_price))
    db.commit()
    db.refresh(order)
    return {"ok": True, "number": order.number, "method": method, "total": total,
            "checks": checks}


# ================================================================= STAFF
def _require_orders(request, db):
    staff = current_staff(request, db)
    if not staff:
        return None, RedirectResponse("/login", status_code=303)
    if not can_orders(staff):
        return None, RedirectResponse("/dashboard", status_code=303)
    return staff, None


def pending_order_count(db):
    return db.query(func.count(Transaction.id)).filter(
        Transaction.type == TX_ORDER, Transaction.status == "pending").scalar() or 0


@router.get("/orders", response_class=HTMLResponse)
def orders_queue(request: Request, db: Session = Depends(get_db)):
    staff, redir = _require_orders(request, db)
    if redir:
        return redir
    pending = (db.query(Transaction)
               .filter(Transaction.type == TX_ORDER, Transaction.status == "pending")
               .order_by(Transaction.created_at.desc()).all())
    recent = (db.query(Transaction)
              .filter(Transaction.type == TX_ORDER, Transaction.status != "pending")
              .order_by(Transaction.decided_at.desc()).limit(20).all())
    return templates.TemplateResponse(
        "orders_queue.html",
        {"request": request, "staff": staff, "pending": pending, "recent": recent,
         "tz": _tz()},
    )


@router.get("/orders/{oid}", response_class=HTMLResponse)
def order_detail(request: Request, oid: int, db: Session = Depends(get_db)):
    staff, redir = _require_orders(request, db)
    if redir:
        return redir
    o = db.get(Transaction, oid)
    if not o or o.type != TX_ORDER:
        return RedirectResponse("/orders", status_code=303)
    return templates.TemplateResponse(
        "order_detail.html",
        {"request": request, "staff": staff, "o": o, "tz": _tz()},
    )


@router.get("/order-proof/{oid}")
def order_proof(request: Request, oid: int, db: Session = Depends(get_db)):
    staff, redir = _require_orders(request, db)
    if redir:
        return redir
    o = db.get(Transaction, oid)
    if not o or not o.proof:
        return Response(status_code=404)
    return Response(content=o.proof, media_type=o.proof_mime or "image/jpeg")


@router.post("/orders/{oid}/confirm")
def order_confirm(request: Request, oid: int, db: Session = Depends(get_db)):
    staff, redir = _require_orders(request, db)
    if redir:
        return redir
    o = db.get(Transaction, oid)
    if o and o.type == TX_ORDER and o.status == "pending":
        # Turn the order into a real paid sale (deducts stock, shows in Retail).
        sale = Transaction(
            type=TX_CASH_SALE, status="paid", staff_id=staff.id, is_credit=False,
            payment_method=o.payment_method, proof=o.proof, proof_mime=o.proof_mime,
            note="Self-checkout %s · %s" % (o.number, o.customer_name))
        db.add(sale)
        db.flush()
        for it in o.items:
            prod = db.get(Product, it.product_id) if it.product_id else None
            db.add(TransactionItem(transaction_id=sale.id, product_id=it.product_id,
                                   name=it.name, qty=it.qty, unit_price=it.unit_price,
                                   cost_price=(prod.cost_price if prod else None)))
        o.status = "confirmed"
        o.converted_id = sale.id
        o.staff_id = staff.id
        o.decided_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse("/orders", status_code=303)


@router.post("/orders/{oid}/reject")
def order_reject(request: Request, oid: int, db: Session = Depends(get_db)):
    staff, redir = _require_orders(request, db)
    if redir:
        return redir
    o = db.get(Transaction, oid)
    if o and o.type == TX_ORDER and o.status == "pending":
        o.status = "rejected"
        o.staff_id = staff.id
        o.decided_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse("/orders", status_code=303)


# ================================================================= ADMIN SETTINGS
@router.get("/admin/order-settings", response_class=HTMLResponse)
def order_settings_page(request: Request, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if staff.role != "admin":
        return RedirectResponse("/dashboard", status_code=303)
    ps = get_settings(db)
    return templates.TemplateResponse(
        "order_settings.html", {"request": request, "staff": staff, "ps": ps})


@router.post("/admin/order-settings")
async def order_settings_save(
    request: Request,
    bank_name: str = Form(""),
    account_name: str = Form(""),
    qr: UploadFile = File(None),
    logo: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    staff = current_staff(request, db)
    if not staff or staff.role != "admin":
        return RedirectResponse("/login", status_code=303)
    ps = get_settings(db)
    ps.bank_name = bank_name.strip()
    ps.account_name = account_name.strip()
    if qr is not None and getattr(qr, "filename", ""):
        try:
            ps.qr, ps.qr_mime = _read_image(qr)
        except ValueError:
            pass
    if logo is not None and getattr(logo, "filename", ""):
        try:
            ps.logo, ps.logo_mime = _read_image(logo)
        except ValueError:
            pass
    db.commit()
    return RedirectResponse("/admin/order-settings", status_code=303)
