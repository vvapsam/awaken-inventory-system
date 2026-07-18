"""AWAKEN mobile PWA — additive module.

Adds a phone-optimised interface at /m without touching the existing web UI.
All data comes from the same PostgreSQL tables used by the desktop app.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import engine, get_db
from .models import (
    Customer, Payment, Product, Sale, SaleItem, StockMovement,
    can, perm_set,
)

BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

router = APIRouter()

MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB per upload


def ensure_mobile_columns():
    """Idempotent migrations for the columns the mobile app needs."""
    with engine.begin() as conn:
        # product photos shown on the tap-to-sell tiles
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS image BYTEA"))
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS image_mime VARCHAR"))
        # proof-of-payment attached to a paid sale
        conn.execute(text("ALTER TABLE sales ADD COLUMN IF NOT EXISTS proof BYTEA"))
        conn.execute(text("ALTER TABLE sales ADD COLUMN IF NOT EXISTS proof_mime VARCHAR"))


# ---------------- helpers ----------------

def _guard(request, db, perm=None):
    staff = current_staff(request, db)
    if not staff:
        return None, JSONResponse({"error": "auth"}, status_code=401)
    if perm and not can(staff, perm):
        return None, JSONResponse({"error": "forbidden"}, status_code=403)
    return staff, None


def _stock_map(db):
    mov = dict(
        db.query(StockMovement.product_id,
                 func.coalesce(func.sum(StockMovement.quantity), 0))
        .group_by(StockMovement.product_id).all()
    )
    sold = dict(
        db.query(SaleItem.product_id, func.coalesce(func.sum(SaleItem.quantity), 0))
        .group_by(SaleItem.product_id).all()
    )
    out = {}
    for p in db.query(Product).filter(Product.is_active == True).all():  # noqa: E712
        out[p.id] = int(mov.get(p.id, 0) or 0) - int(sold.get(p.id, 0) or 0)
    return out


def _balances(db):
    charges = {}
    for s in db.query(Sale).filter(Sale.is_credit == True,
                                   Sale.customer_id != None).all():  # noqa: E712,E711
        charges[s.customer_id] = charges.get(s.customer_id, 0.0) + s.total
    paid = dict(
        db.query(Payment.customer_id, func.coalesce(func.sum(Payment.amount), 0))
        .group_by(Payment.customer_id).all()
    )
    out = {}
    for cid, ch in charges.items():
        bal = ch - float(paid.get(cid, 0) or 0)
        if bal > 0.005:
            out[cid] = bal
    return out


async def _read_image(upload):
    if upload is None or not getattr(upload, "filename", ""):
        return None, None
    data = await upload.read()
    if not data:
        return None, None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image too large (max 4 MB)")
    return data, (upload.content_type or "image/jpeg")


# ---------------- pages ----------------

@router.get("/m", response_class=HTMLResponse)
def mobile_home(request: Request, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("mobile.html", {"request": request, "staff": staff})


@router.get("/product-image/{pid}")
def product_image(pid: int, db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p or not p.image:
        return Response(status_code=404)
    return Response(content=p.image, media_type=p.image_mime or "image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/m/images", response_class=HTMLResponse)
def image_manager(request: Request, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff or staff.role != "admin":
        return RedirectResponse("/dashboard", status_code=303)
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()  # noqa: E712
    return templates.TemplateResponse(
        "mobile_images.html",
        {"request": request, "staff": staff, "products": products},
    )


@router.post("/m/images/{pid}")
async def image_upload(request: Request, pid: int, photo: UploadFile = None,
                       db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff or staff.role != "admin":
        return RedirectResponse("/dashboard", status_code=303)
    p = db.get(Product, pid)
    if p:
        try:
            data, mime = await _read_image(photo)
        except ValueError:
            return RedirectResponse("/m/images?err=size", status_code=303)
        if data:
            p.image, p.image_mime = data, mime
            db.commit()
    return RedirectResponse("/m/images?ok=1", status_code=303)


@router.post("/m/images/{pid}/clear")
def image_clear(request: Request, pid: int, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff or staff.role != "admin":
        return RedirectResponse("/dashboard", status_code=303)
    p = db.get(Product, pid)
    if p:
        p.image, p.image_mime = None, None
        db.commit()
    return RedirectResponse("/m/images", status_code=303)


# ---------------- JSON API ----------------

@router.get("/api/m/bootstrap")
def api_bootstrap(request: Request, db: Session = Depends(get_db)):
    staff, err = _guard(request, db)
    if err:
        return err
    stock = _stock_map(db)
    products = [
        {
            "id": p.id,
            "name": p.name,
            "sku": p.sku,
            "price": float(p.selling_price or 0),
            "stock": stock.get(p.id, 0),
            "image": f"/product-image/{p.id}" if p.image else None,
        }
        for p in db.query(Product)
        .filter(Product.is_active == True)  # noqa: E712
        .order_by(Product.name).all()
    ]
    bals = _balances(db)
    customers = [
        {"id": c.id, "name": c.name, "balance": round(bals.get(c.id, 0.0), 2)}
        for c in db.query(Customer).order_by(Customer.name).all()
    ]
    perms = sorted(perm_set(staff))
    return JSONResponse({
        "staff": {"name": staff.name, "role": staff.role},
        "perms": perms,
        "products": products,
        "customers": customers,
    })


@router.get("/api/m/customer/{cid}")
def api_customer_detail(request: Request, cid: int, db: Session = Depends(get_db)):
    staff, err = _guard(request, db)
    if err:
        return err
    c = db.get(Customer, cid)
    if not c:
        return JSONResponse({"error": "not found"}, status_code=404)
    tz = timezone(timedelta(hours=8))
    orders = []
    sales = (db.query(Sale)
             .filter(Sale.is_credit == True, Sale.customer_id == cid)  # noqa: E712
             .order_by(Sale.sold_at).all())
    for s in sales:
        orders.append({
            "id": s.id,
            "date": (s.sold_at.astimezone(tz).strftime("%b %d") if s.sold_at else ""),
            "total": round(s.total, 2),
            "items": [
                {"name": (i.product.name if i.product else "item"),
                 "qty": i.quantity,
                 "sub": round(float(i.quantity) * float(i.unit_price), 2)}
                for i in s.items
            ],
        })
    paid = float(db.query(func.coalesce(func.sum(Payment.amount), 0))
                 .filter(Payment.customer_id == cid).scalar() or 0)
    charges = sum(o["total"] for o in orders)
    return JSONResponse({
        "id": c.id, "name": c.name, "orders": orders,
        "charges": round(charges, 2), "paid": round(paid, 2),
        "balance": round(charges - paid, 2),
    })


@router.post("/api/m/customers")
async def api_customer_new(request: Request, db: Session = Depends(get_db)):
    staff, err = _guard(request, db, "sales.create")
    if err:
        return err
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    existing = db.query(Customer).filter(func.lower(Customer.name) == name.lower()).first()
    c = existing or Customer(name=name)
    if not existing:
        db.add(c)
        db.commit()
        db.refresh(c)
    return JSONResponse({"id": c.id, "name": c.name, "balance": 0})


@router.post("/api/m/sale")
async def api_mobile_sale(
    request: Request,
    items: str = Form(...),          # JSON: [{"product_id":1,"qty":2}, ...]
    payment: str = Form(...),        # unpaid | cash | bank
    customer_id: str = Form(""),
    proof: UploadFile = None,
    db: Session = Depends(get_db),
):
    staff, err = _guard(request, db, "sales.create")
    if err:
        return err

    try:
        lines = json.loads(items or "[]")
    except json.JSONDecodeError:
        return JSONResponse({"error": "bad items"}, status_code=400)
    if not lines:
        return JSONResponse({"error": "no items"}, status_code=400)

    payment = (payment or "").lower()
    if payment not in ("unpaid", "cash", "bank"):
        return JSONResponse({"error": "bad payment"}, status_code=400)

    is_credit = payment == "unpaid"
    cid = int(customer_id) if (customer_id or "").isdigit() else None

    # business rules mirrored from the mobile UI
    if is_credit and not cid:
        return JSONResponse({"error": "customer required for unpaid"}, status_code=400)

    try:
        img, mime = await _read_image(proof)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not is_credit and not img:
        return JSONResponse({"error": "proof required"}, status_code=400)

    sale = Sale(
        staff_id=staff.id,
        customer_id=cid if is_credit else None,
        is_credit=is_credit,
        payment_method=(None if is_credit else payment),
        note="via mobile",
    )
    if img:
        sale.proof, sale.proof_mime = img, mime
    db.add(sale)
    db.flush()

    total = 0.0
    for ln in lines:
        p = db.get(Product, int(ln.get("product_id") or 0))
        if not p:
            continue
        qty = max(1, int(ln.get("qty") or 1))
        db.add(SaleItem(sale_id=sale.id, product_id=p.id, quantity=qty,
                        unit_price=p.selling_price, cost_price=p.cost_price))
        total += qty * float(p.selling_price or 0)
    db.commit()
    return JSONResponse({"ok": True, "sale_id": sale.id, "total": round(total, 2)})


@router.get("/sale/{sid}/proof")
def sale_proof(request: Request, sid: int, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff:
        return Response(status_code=401)
    s = db.get(Sale, sid)
    if not s or not s.proof:
        return Response(status_code=404)
    return Response(content=s.proof, media_type=s.proof_mime or "image/jpeg")


@router.post("/api/m/movement")
async def api_mobile_movement(
    request: Request,
    product_id: int = Form(...),
    kind: str = Form(...),        # receive | loss
    qty: int = Form(...),
    amount: str = Form(""),
    memo: str = Form(""),
    db: Session = Depends(get_db),
):
    kind = (kind or "").lower()
    perm = "receive.create" if kind == "receive" else "adjust.create"
    staff, err = _guard(request, db, perm)
    if err:
        return err

    p = db.get(Product, product_id)
    if not p:
        return JSONResponse({"error": "product required"}, status_code=400)
    qty = max(1, int(qty or 1))

    if kind == "receive":
        try:
            amt = float(amount or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt <= 0:
            return JSONResponse({"error": "amount required"}, status_code=400)
        mv = StockMovement(product_id=p.id, movement_type="restock", quantity=qty,
                           unit_cost=round(amt / qty, 2), note=(memo or "received via mobile"),
                           staff_id=staff.id)
    elif kind == "loss":
        if not (memo or "").strip():
            return JSONResponse({"error": "memo required"}, status_code=400)
        mv = StockMovement(product_id=p.id, movement_type="waste", quantity=-qty,
                           note=memo.strip(), staff_id=staff.id)
    else:
        return JSONResponse({"error": "bad kind"}, status_code=400)

    db.add(mv)
    db.commit()
    return JSONResponse({"ok": True, "stock": _stock_map(db).get(p.id, 0)})


@router.post("/api/m/settle")
async def api_mobile_settle(
    request: Request,
    customer_id: int = Form(...),
    method: str = Form(...),      # cash | bank
    amount: str = Form(""),
    proof: UploadFile = None,
    db: Session = Depends(get_db),
):
    staff, err = _guard(request, db, "payments.create")
    if err:
        return err
    c = db.get(Customer, customer_id)
    if not c:
        return JSONResponse({"error": "customer not found"}, status_code=404)
    method = (method or "").lower()
    if method not in ("cash", "bank"):
        return JSONResponse({"error": "bad method"}, status_code=400)

    try:
        img, mime = await _read_image(proof)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not img:
        return JSONResponse({"error": "proof required"}, status_code=400)

    bal = _balances(db).get(customer_id, 0.0)
    try:
        amt = float(amount) if amount else bal
    except (TypeError, ValueError):
        amt = bal
    if amt <= 0:
        return JSONResponse({"error": "nothing to settle"}, status_code=400)

    db.add(Payment(customer_id=c.id, amount=round(amt, 2), method=method,
                   note="settled via mobile", screenshot=img,
                   screenshot_mime=mime, staff_id=staff.id))
    db.commit()
    return JSONResponse({"ok": True, "paid": round(amt, 2)})
