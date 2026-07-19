"""AWAKEN Mobile — phone-first PWA (additive; new routes only).

Serves the installable mobile web app at /m plus a small JSON API. All the
business rules from the spec are enforced here on the server, never trusting the
client. Reuses the existing tables and helpers so the desktop admin is unaffected.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import get_db
from .models import (
    Customer, Product, StockMovement, Staff, PricingGroup, can, can_any,
    Transaction, TransactionItem, TX_CASH_SALE, TX_PAYMENT,
)

router = APIRouter()

BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["peso"] = lambda v: "₱{:,.2f}".format(float(v or 0))
templates.env.globals["can"] = can
templates.env.globals["can_any"] = can_any

MAX_IMAGE = 4 * 1024 * 1024  # ~4 MB cap on uploads
LOSS_MEMO_CHIPS = ["Expired", "Damaged", "Spoiled", "Missing", "Staff sample"]


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("APP_TZ", "Asia/Manila"))
    except Exception:
        return timezone(timedelta(hours=8))


def _today():
    return datetime.now(_tz()).date()


# ---- permission mapping (spec §5) ----
def can_sell(staff):
    return can(staff, "sales.create")


def can_adjust(staff):
    return can_any(staff, ["receive.create", "adjust.create"])


def can_receive(staff):
    return can(staff, "receive.create")


def can_loss(staff):
    return can(staff, "adjust.create")


def can_settle(staff):
    return can(staff, "payments.create")


def _err(msg, code=400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


def _read_image(upload: UploadFile):
    """Return (bytes, mime) or raise ValueError. Enforces size + type."""
    data = upload.file.read()
    if not data:
        raise ValueError("Empty file")
    if len(data) > MAX_IMAGE:
        raise ValueError("Image is too large (max 4 MB)")
    mime = upload.content_type or "image/jpeg"
    if not mime.startswith("image/"):
        raise ValueError("Attachment must be an image")
    return data, mime


def _on_hand_map(db):
    mov = dict(
        db.query(StockMovement.product_id,
                 func.coalesce(func.sum(StockMovement.quantity), 0))
        .group_by(StockMovement.product_id).all()
    )
    sold = dict(
        db.query(TransactionItem.product_id,
                 func.coalesce(func.sum(TransactionItem.qty), 0))
        .join(Transaction, Transaction.id == TransactionItem.transaction_id)
        .filter(Transaction.type == TX_CASH_SALE, TransactionItem.product_id != None)  # noqa: E711
        .group_by(TransactionItem.product_id).all()
    )
    return {"mov": mov, "sold": sold}


def _on_hand(maps, pid):
    return int(maps["mov"].get(pid, 0)) - int(maps["sold"].get(pid, 0))


# ---------------------------------------------------------------- page shell
@router.get("/m", response_class=HTMLResponse)
def mobile_app(request: Request, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("mobile.html", {"request": request, "staff": staff})


# ---------------------------------------------------------------- bootstrap
@router.get("/api/m/bootstrap")
def bootstrap(request: Request, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff:
        return _err("Not signed in", 401)

    perms = {
        "sell": can_sell(staff),
        "adjust": can_adjust(staff),
        "receive": can_receive(staff),
        "loss": can_loss(staff),
        "settle": can_settle(staff),
        "view_costs": can(staff, "view_costs"),
    }

    maps = _on_hand_map(db)
    products = []
    if perms["sell"] or perms["adjust"] or can(staff, "view_stock"):
        for p in (db.query(Product).filter(Product.is_active)
                  .order_by(Product.name).all()):
            products.append({
                "id": p.id,
                "name": p.name,
                "sku": p.sku,
                "price": float(p.selling_price or 0),
                "cost": float(p.cost_price or 0),
                "on_hand": _on_hand(maps, p.id),
                "reorder_point": p.reorder_point,
                "has_image": bool(p.image),
            })

    customers = [
        {"id": c.id, "name": c.name, "phone": c.phone or ""}
        for c in db.query(Customer).order_by(Customer.name).all()
    ]

    balances = {"total": 0.0, "count": 0, "customers": []}
    if can_settle(staff) or can(staff, "view_reports"):
        from .main import customer_balances
        rows = [r for r in customer_balances(db) if r["balance"] > 0.005]
        rows.sort(key=lambda r: r["balance"])
        balances = {
            "total": round(sum(r["balance"] for r in rows), 2),
            "count": len(rows),
            "customers": [
                {"id": r["customer"].id, "name": r["customer"].name,
                 "balance": round(r["balance"], 2)}
                for r in rows
            ],
        }

    return {
        "ok": True,
        "staff": {"name": staff.name, "role": staff.role},
        "perms": perms,
        "products": products,
        "customers": customers,
        "balances": balances,
        "loss_chips": LOSS_MEMO_CHIPS,
        "has_codes": db.query(Staff).filter(Staff.discount_code != None,  # noqa: E711
                                            Staff.is_active).first() is not None,
    }


def _code_used_today(db, person_id):
    start = datetime.combine(_today(), datetime.min.time()).replace(tzinfo=_tz())
    return int(db.query(func.coalesce(func.sum(Transaction.discounted_qty), 0))
               .filter(Transaction.type == TX_CASH_SALE,
                       Transaction.discount_person_id == person_id,
                       Transaction.occurred_at >= start).scalar() or 0)


def _resolve_code(db, code):
    """Look up a person by their personal discount code and the active pricing
    tier for their type. Returns (person, group) or (None, None)."""
    c = (code or "").strip().upper()
    if not c:
        return None, None
    person = (db.query(Staff)
              .filter(func.upper(Staff.discount_code) == c,
                      Staff.is_active, Staff.person_type != None).first())  # noqa: E711
    if not person or not person.person_type:
        return None, None
    group = (db.query(PricingGroup)
             .filter(PricingGroup.kind == person.person_type, PricingGroup.is_active)
             .order_by(PricingGroup.id).first())
    if not group:
        return None, None
    return person, group


@router.post("/api/m/discount-code")
def check_discount_code(request: Request, code: str = Form(...),
                        db: Session = Depends(get_db)):
    """Validate a discount code and return its tier so the Sell screen can preview."""
    staff = current_staff(request, db)
    if not staff or not can_sell(staff):
        return _err("Not allowed", 403)
    person, g = _resolve_code(db, code)
    if not person or not g:
        return _err("Invalid or inactive code", 404)
    limit = g.daily_item_limit
    used = _code_used_today(db, person.id) if limit else 0
    return {
        "ok": True,
        "code": person.discount_code, "holder": person.name, "kind": g.kind, "tier": g.name,
        "discount": float(g.discount_percent or 0), "round_up": bool(g.round_up),
        "product_ids": sorted(g.eligible_ids()),
        "daily_limit": limit, "used_today": used,
        "remaining": (max(0, limit - used) if limit else None),
    }


# ---------------------------------------------------------------- product image
@router.get("/product-image/{pid}")
def product_image(pid: int, db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p or not p.image:
        return Response(status_code=404)
    return Response(content=p.image, media_type=p.image_mime or "image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- admin: product photos
@router.get("/admin/product-images", response_class=HTMLResponse)
def product_images_page(request: Request, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if staff.role != "admin":
        return RedirectResponse("/dashboard", status_code=303)
    products = db.query(Product).filter(Product.is_active).order_by(Product.name).all()
    return templates.TemplateResponse(
        "mobile_images.html",
        {"request": request, "staff": staff, "products": products},
    )


@router.post("/admin/product-images/{pid}")
async def upload_product_image(
    request: Request, pid: int, photo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    staff = current_staff(request, db)
    if not staff or staff.role != "admin":
        return RedirectResponse("/login", status_code=303)
    p = db.get(Product, pid)
    if p:
        try:
            data, mime = _read_image(photo)
            p.image, p.image_mime = data, mime
            db.commit()
        except ValueError:
            pass
    return RedirectResponse("/admin/product-images", status_code=303)


@router.post("/admin/product-images/{pid}/delete")
def delete_product_image(request: Request, pid: int, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff or staff.role != "admin":
        return RedirectResponse("/login", status_code=303)
    p = db.get(Product, pid)
    if p:
        p.image = p.image_mime = None
        db.commit()
    return RedirectResponse("/admin/product-images", status_code=303)


# ---------------------------------------------------------------- create sale
@router.post("/api/m/sale")
async def create_sale(
    request: Request,
    items: str = Form(...),
    payment: str = Form(...),
    customer_id: str = Form(None),
    discount_code: str = Form(None),
    proof: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    staff = current_staff(request, db)
    if not staff:
        return _err("Not signed in", 401)
    if not can_sell(staff):
        return _err("You don't have permission to log sales", 403)

    # Discount code → person + pricing tier (Employee / Affiliate). Server is authoritative.
    person = group = None
    remaining = None  # None = unlimited; else remaining discounted units today
    if discount_code and discount_code.strip():
        person, group = _resolve_code(db, discount_code)
        if not person or not group:
            return _err("Invalid or inactive discount code")
        if group.daily_item_limit:
            remaining = max(0, group.daily_item_limit - _code_used_today(db, person.id))

    payment = (payment or "").lower().strip()
    if payment not in ("unpaid", "cash", "bank"):
        return _err("Select a payment method")

    try:
        raw = json.loads(items or "[]")
    except Exception:
        return _err("Bad item data")
    if not raw:
        return _err("Add at least one item")

    # Rule 1: cash/bank sale needs proof.  Rule 2: unpaid needs a customer.
    is_credit = payment == "unpaid"
    cust = None
    if is_credit:
        if not customer_id:
            return _err("An unpaid sale needs a customer")
        cust = db.get(Customer, int(customer_id))
        if not cust:
            return _err("Customer not found")
        proof_bytes = proof_mime = None
    else:
        if proof is None or not getattr(proof, "filename", ""):
            return _err("Attach a proof-of-payment screenshot")
        try:
            proof_bytes, proof_mime = _read_image(proof)
        except ValueError as e:
            return _err(str(e))

    # Build line items with snapshotted price + cost (Rule 7).
    lines = []
    for it in raw:
        p = db.get(Product, int(it["product_id"]))
        qty = int(it["quantity"])
        if not p or qty <= 0:
            return _err("Invalid item in cart")
        lines.append((p, qty))

    sale = Transaction(
        type=TX_CASH_SALE,
        status=("credit" if is_credit else "paid"),
        staff_id=staff.id,
        customer_id=cust.id if cust else None,
        is_credit=is_credit,
        payment_method=None if is_credit else payment,
        proof=proof_bytes if not is_credit else None,
        proof_mime=proof_mime if not is_credit else None,
        pricing_group_id=group.id if group else None,
        discount_person_id=person.id if person else None,
        note=("%s (%s)" % (group.name, person.name)) if group else None,
    )
    db.add(sale)
    db.flush()
    discounted_qty = 0
    for p, qty in lines:
        base = round(float(p.selling_price or 0), 2)
        eligible = group is not None and p.id in group.eligible_ids()
        # how many of this line's units can still be discounted (respects daily cap)
        n = 0
        if eligible:
            n = qty if remaining is None else min(qty, remaining)
            if remaining is not None:
                remaining -= n
        if eligible and n > 0:
            disc = group.price_for(p)
            db.add(TransactionItem(transaction_id=sale.id, product_id=p.id, name=p.name,
                                   qty=n, unit_price=disc, cost_price=p.cost_price))
            discounted_qty += n
            if n < qty:  # remainder at full price (cap reached)
                db.add(TransactionItem(transaction_id=sale.id, product_id=p.id, name=p.name,
                                       qty=qty - n, unit_price=base, cost_price=p.cost_price))
        else:
            db.add(TransactionItem(transaction_id=sale.id, product_id=p.id, name=p.name,
                                   qty=qty, unit_price=base, cost_price=p.cost_price))
    sale.discounted_qty = discounted_qty
    db.commit()
    db.refresh(sale)
    return {"ok": True, "sale_id": sale.id, "total": sale.total,
            "discounted_qty": discounted_qty}


# ---------------------------------------------------------------- stock movement
@router.post("/api/m/movement")
async def create_movement(
    request: Request,
    kind: str = Form(...),            # "receive" | "loss"
    product_id: str = Form(...),
    quantity: str = Form(...),
    amount: str = Form(None),         # receive only (total paid)
    memo: str = Form(None),           # loss only (reason)
    db: Session = Depends(get_db),
):
    staff = current_staff(request, db)
    if not staff:
        return _err("Not signed in", 401)

    kind = (kind or "").lower().strip()
    p = db.get(Product, int(product_id)) if product_id else None
    if not p:
        return _err("Choose an item")
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        return _err("Enter a quantity")
    if qty <= 0:
        return _err("Quantity must be greater than zero")

    if kind == "receive":
        if not can_receive(staff):
            return _err("You don't have permission to receive stock", 403)
        # Rule: receive needs a total amount paid.
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return _err("Enter the total amount paid")
        if amt < 0:
            return _err("Amount can't be negative")
        unit_cost = round(amt / qty, 2) if qty else 0
        mv = StockMovement(product_id=p.id, movement_type="restock",
                           quantity=abs(qty), unit_cost=unit_cost,
                           note=None, staff_id=staff.id)
    elif kind == "loss":
        if not can_loss(staff):
            return _err("You don't have permission to record losses", 403)
        # Rule 3: a loss needs a memo.
        memo = (memo or "").strip()
        if not memo:
            return _err("Add a memo/reason for the loss")
        mtype = "missing" if memo.lower() == "missing" else "waste"
        mv = StockMovement(product_id=p.id, movement_type=mtype,
                           quantity=-abs(qty), note=memo, staff_id=staff.id)
    else:
        return _err("Unknown movement type")

    db.add(mv)
    db.commit()
    maps = _on_hand_map(db)
    return {"ok": True, "new_on_hand": _on_hand(maps, p.id)}


# ---------------------------------------------------------------- add customer
@router.post("/api/m/customers")
async def add_customer(
    request: Request,
    name: str = Form(...),
    phone: str = Form(None),
    db: Session = Depends(get_db),
):
    staff = current_staff(request, db)
    if not staff:
        return _err("Not signed in", 401)
    if not (can_sell(staff) or can_settle(staff)):
        return _err("Not allowed", 403)
    name = (name or "").strip()
    if not name:
        return _err("Customer name is required")
    c = Customer(name=name, phone=(phone or "").strip() or None)
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"ok": True, "customer": {"id": c.id, "name": c.name, "phone": c.phone or ""}}


# ---------------------------------------------------------------- customer detail
@router.get("/api/m/customer/{cid}")
def customer_detail(request: Request, cid: int, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff:
        return _err("Not signed in", 401)
    if not (can_settle(staff) or can(staff, "view_reports")):
        return _err("Not allowed", 403)
    c = db.get(Customer, cid)
    if not c:
        return _err("Customer not found", 404)

    orders = []
    charges = 0.0
    credit_sales = (
        db.query(Transaction)
        .filter(Transaction.type == TX_CASH_SALE,
                Transaction.customer_id == cid, Transaction.is_credit == True)  # noqa: E712
        .order_by(Transaction.occurred_at.desc()).all()
    )
    for s in credit_sales:
        charges += s.total
        orders.append({
            "id": s.id,
            "date": (s.occurred_at.astimezone().strftime("%b %d, %Y")
                     if s.occurred_at else ""),
            "total": s.total,
            "items": [
                {"name": (li.name or (li.product.name if li.product else "—")),
                 "qty": int(li.qty),
                 "unit_price": float(li.unit_price or 0),
                 "line": float(li.qty) * float(li.unit_price or 0)}
                for li in s.items
            ],
        })

    pays = (db.query(Transaction)
            .filter(Transaction.type == TX_PAYMENT, Transaction.parent_id == None,  # noqa: E711
                    Transaction.customer_id == cid)
            .order_by(Transaction.occurred_at.desc()).all())
    payments = [
        {"id": pm.id, "amount": float(pm.total or 0),
         "method": pm.payment_method or "",
         "date": (pm.occurred_at.astimezone().strftime("%b %d, %Y")
                  if pm.occurred_at else "")}
        for pm in pays
    ]
    paid = sum(p["amount"] for p in payments)

    return {
        "ok": True,
        "customer": {"id": c.id, "name": c.name, "phone": c.phone or ""},
        "orders": orders,
        "payments": payments,
        "charges": round(charges, 2),
        "paid": round(paid, 2),
        "balance": round(charges - paid, 2),
    }


# ---------------------------------------------------------------- settle balance
@router.post("/api/m/settle")
async def settle(
    request: Request,
    customer_id: str = Form(...),
    method: str = Form(...),
    amount: str = Form(None),
    screenshot: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    staff = current_staff(request, db)
    if not staff:
        return _err("Not signed in", 401)
    if not can_settle(staff):
        return _err("You don't have permission to take payments", 403)

    c = db.get(Customer, int(customer_id)) if customer_id else None
    if not c:
        return _err("Customer not found")
    method = (method or "").lower().strip()
    if method not in ("cash", "bank"):
        return _err("Choose a payment method")
    # Rule 4: settling requires a screenshot.
    if screenshot is None or not getattr(screenshot, "filename", ""):
        return _err("Attach a payment screenshot")
    try:
        shot, shot_mime = _read_image(screenshot)
    except ValueError as e:
        return _err(str(e))

    # Determine amount: default to full current balance.
    from .main import customer_balances
    row = next((r for r in customer_balances(db) if r["customer"].id == c.id), None)
    balance = round(row["balance"], 2) if row else 0.0
    if amount is None or str(amount).strip() == "":
        amt = balance
    else:
        try:
            amt = float(amount)
        except ValueError:
            return _err("Invalid amount")
    if amt <= 0:
        return _err("Nothing to settle")

    pay = Transaction(type=TX_PAYMENT, customer_id=c.id, payment_method=method,
                      proof=shot, proof_mime=shot_mime, staff_id=staff.id, status="paid")
    db.add(pay)
    db.flush()
    db.add(TransactionItem(transaction_id=pay.id, name="Payment received",
                           qty=1, unit_price=amt))
    db.commit()
    row = next((r for r in customer_balances(db) if r["customer"].id == c.id), None)
    return {"ok": True, "paid": amt,
            "new_balance": round(row["balance"], 2) if row else 0.0}
