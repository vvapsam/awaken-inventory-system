import csv
import io
import os
from datetime import date, datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_staff, hash_pin, verify_pin
from . import mobile as mobile_mod
from .db import Base, engine, get_db
from .models import (
    CATEGORIES, MOVEMENT_TYPES, PAYMENT_METHODS, PERMISSION_KEYS,
    MODULES, ACTIONS, ACCESS_DEFS, RECEIVE_TYPES, ADJUST_TYPES,
    DEFAULT_STAFF_PERMS, ROLES, UNITS, can, can_any, perm_set, module_for_type,
    Customer, Payment, Product, Sale, SaleItem, Staff, StockMovement,
    Coach, Member, COACH_TYPES, Invoice, InvoiceItem, InvoicePayment,
)

APP_TZ = os.environ.get("APP_TZ", "Asia/Manila")


def _tz():
    if ZoneInfo:
        try:
            return ZoneInfo(APP_TZ)
        except Exception:
            pass
    return timezone(timedelta(hours=8))  # Manila fallback

BASE_DIR = os.path.dirname(__file__)
app = FastAPI(title="AWAKEN Inventory")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", "dev-insecure-change-me"),
    max_age=60 * 60 * 12,  # 12h sessions
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["peso"] = lambda v: "₱{:,.2f}".format(float(v or 0))
templates.env.globals["can"] = can
templates.env.globals["can_any"] = can_any

# Mobile PWA (additive: new routes only, existing desktop pages untouched)
from .mobile import router as mobile_router  # noqa: E402
app.include_router(mobile_router)


def _slugify(s):
    return "".join(c for c in (s or "").lower() if c.isalnum()) or "user"


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    # Lightweight migrations for databases created before these columns existed.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE staff ADD COLUMN IF NOT EXISTS permissions TEXT NOT NULL DEFAULT ''"))
        conn.execute(text("ALTER TABLE staff ADD COLUMN IF NOT EXISTS username TEXT"))
        conn.execute(text("ALTER TABLE sales ADD COLUMN IF NOT EXISTS customer_id INTEGER REFERENCES customers(id)"))
        conn.execute(text("ALTER TABLE sales ADD COLUMN IF NOT EXISTS is_credit BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier VARCHAR"))
        conn.execute(text("ALTER TABLE products DROP CONSTRAINT IF EXISTS products_category_check"))
        conn.execute(text("ALTER TABLE products ALTER COLUMN category DROP NOT NULL"))
        # Mobile PWA additions
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS image BYTEA"))
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS image_mime VARCHAR"))
        conn.execute(text("ALTER TABLE customers ADD COLUMN IF NOT EXISTS phone VARCHAR"))
        conn.execute(text("ALTER TABLE sales ADD COLUMN IF NOT EXISTS proof BYTEA"))
        conn.execute(text("ALTER TABLE sales ADD COLUMN IF NOT EXISTS proof_mime VARCHAR"))
    db = next(get_db())
    try:
        # Backfill usernames for any rows missing one, keeping them unique.
        taken = set(u for (u,) in db.query(Staff.username).all() if u)
        for st in db.query(Staff).filter((Staff.username == None) | (Staff.username == "")).all():  # noqa: E711
            base = _slugify(st.name)
            u, i = base, 1
            while u in taken:
                i += 1
                u = f"{base}{i}"
            st.username = u
            taken.add(u)
        db.commit()
        with engine.begin() as conn:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS staff_username_uq ON staff (username)"))
        # Bootstrap a first admin if none exists.
        if not db.query(Staff).filter(Staff.role == "admin").first():
            pin = os.environ.get("ADMIN_INITIAL_PIN", "123456")
            h, s = hash_pin(pin)
            db.add(Staff(username="admin", name="Admin", role="admin",
                         pin_hash=h, pin_salt=s, permissions=""))
            db.commit()
    finally:
        db.close()


# ---------- helpers ----------

def render(request, template, db, staff, **ctx):
    base = {"request": request, "staff": staff, "CATEGORIES": CATEGORIES,
            "UNITS": UNITS, "MOVEMENT_TYPES": MOVEMENT_TYPES,
            "PAYMENT_METHODS": PAYMENT_METHODS, "ROLES": ROLES,
            "MODULES": MODULES, "ACTIONS": ACTIONS, "ACCESS_DEFS": ACCESS_DEFS,
            "RECEIVE_TYPES": RECEIVE_TYPES, "ADJUST_TYPES": ADJUST_TYPES}
    base.update(ctx)
    return templates.TemplateResponse(template, base)


def require(request, db, admin=False, perm=None):
    """Return (staff, None) if allowed, else (None, RedirectResponse).

    - not logged in  -> login page
    - admin=True and not admin -> dashboard
    - perm set and user lacks it (and isn't admin) -> dashboard
    """
    staff = current_staff(request, db)
    if not staff:
        return None, RedirectResponse("/login", status_code=303)
    if admin and staff.role != "admin":
        return None, RedirectResponse("/dashboard", status_code=303)
    if perm and not can(staff, perm):
        return None, RedirectResponse("/dashboard", status_code=303)
    return staff, None


def stock_levels(db):
    """on_hand per active product = sum(stock_movements) - sum(sale_items)."""
    mov = dict(
        db.query(StockMovement.product_id, func.coalesce(func.sum(StockMovement.quantity), 0))
        .group_by(StockMovement.product_id).all()
    )
    sold = dict(
        db.query(SaleItem.product_id, func.coalesce(func.sum(SaleItem.quantity), 0))
        .group_by(SaleItem.product_id).all()
    )
    rows = []
    for p in db.query(Product).filter(Product.is_active).order_by(Product.category, Product.name):
        on_hand = int(mov.get(p.id, 0)) - int(sold.get(p.id, 0))
        rows.append({"product": p, "on_hand": on_hand, "low": on_hand <= p.reorder_point})
    return rows


def signed_qty(movement_type: str, qty: int, direction: str = "add") -> int:
    if movement_type in ("restock", "return"):
        return abs(qty)
    if movement_type in ("waste", "missing"):
        return -abs(qty)
    # adjustment: user chooses
    return abs(qty) if direction == "add" else -abs(qty)


# ---------- auth ----------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    return RedirectResponse("/dashboard" if staff else "/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), pin: str = Form(...), db: Session = Depends(get_db)):
    uname = (username or "").strip().lower()
    staff = db.query(Staff).filter(func.lower(Staff.username) == uname, Staff.is_active).first()
    if not staff or not verify_pin(pin, staff.pin_hash, staff.pin_salt):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Wrong username or PIN."}
        )
    request.session["staff_id"] = staff.id
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- dashboard + stock ----------

SALES_RANGES = [
    ("today", "Today"), ("yesterday", "Yesterday"), ("7d", "7 days"),
    ("30d", "30 days"), ("month", "This month"),
]


def _range_bounds(key):
    tz = _tz()
    now = datetime.now(tz)
    t0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = t0 + timedelta(days=1)
    if key == "today":
        return t0, end, "hour"
    if key == "yesterday":
        return t0 - timedelta(days=1), t0, "hour"
    if key == "7d":
        return t0 - timedelta(days=6), end, "day"
    if key == "30d":
        return t0 - timedelta(days=29), end, "day"
    if key == "month":
        return t0.replace(day=1), end, "day"
    return t0, end, "hour"


def _hour_label(h):
    suffix = "a" if h < 12 else "p"
    return f"{(h % 12) or 12}{suffix}"


def sales_summary(db, key):
    tz = _tz()
    start, end, gran = _range_bounds(key)
    start_u, end_u = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    rows = (
        db.query(Sale.sold_at, SaleItem.quantity, SaleItem.unit_price)
        .join(SaleItem, SaleItem.sale_id == Sale.id)
        .filter(Sale.sold_at >= start_u, Sale.sold_at < end_u).all()
    )
    # build ordered empty buckets
    buckets, index = [], {}
    if gran == "hour":
        for h in range(24):
            k = h
            index[k] = len(buckets)
            buckets.append({"label": _hour_label(h), "value": 0.0})
    else:
        d = start
        while d < end:
            k = d.date()
            index[k] = len(buckets)
            buckets.append({"label": f"{d.month}/{d.day}", "value": 0.0})
            d += timedelta(days=1)
    revenue = 0.0
    units = 0
    for sold_at, qty, price in rows:
        amt = float(qty) * float(price)
        revenue += amt
        units += int(qty)
        local = sold_at.astimezone(tz) if sold_at.tzinfo else sold_at.replace(tzinfo=timezone.utc).astimezone(tz)
        k = local.hour if gran == "hour" else local.date()
        if k in index:
            buckets[index[k]]["value"] += amt
    label = dict(SALES_RANGES).get(key, "Today")
    return {"range": key, "label": label, "revenue": round(revenue, 2),
            "units": units, "granularity": gran, "buckets": buckets}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    levels = stock_levels(db)
    low = [r for r in levels if r["low"]]
    can_reports = can(staff, "view_reports")
    summary = sales_summary(db, "today") if can_reports else None
    can_customers = can_any(staff, ["view_reports", "payments.create",
                                    "payments.edit", "payments.delete"])
    owing = [r for r in customer_balances(db) if r["balance"] > 0.005] if can_customers else []
    unpaid_total = sum(r["balance"] for r in owing)
    return render(request, "dashboard.html", db, staff, low=low,
                  product_count=len(levels), can_reports=can_reports,
                  summary=summary, sales_ranges=SALES_RANGES,
                  can_customers=can_customers, unpaid_total=unpaid_total,
                  unpaid_count=len(owing))


@app.get("/api/sales_summary")
def api_sales_summary(request: Request, range: str = "today", db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="view_reports")
    if redir:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if range not in dict(SALES_RANGES):
        range = "today"
    return JSONResponse(sales_summary(db, range))


@app.get("/stock", response_class=HTMLResponse)
def stock(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="view_stock")
    if redir:
        return redir
    return render(request, "stock.html", db, staff, levels=stock_levels(db))


# ---------- log a sale ----------

def find_or_create_customer(db, name):
    name = (name or "").strip()
    if not name:
        return None
    existing = db.query(Customer).filter(func.lower(Customer.name) == name.lower()).first()
    if existing:
        return existing
    c = Customer(name=name)
    db.add(c)
    db.flush()
    return c


# (The single-sale form was replaced by the Sales spreadsheet at /sales.
#  See the "Sales spreadsheet" section below for /sales, /api/sales, /sale/quick.)


# ---------- stock movements (restock / waste / missing / adjustment / return) ----------

@app.get("/movement/new", response_class=HTMLResponse)
def movement_form(request: Request, type: str = "restock", db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if type not in MOVEMENT_TYPES:
        type = "restock"
    # pick a type the user is actually allowed to create
    if not can(staff, f"{module_for_type(type)}.create"):
        allowed = [t for t in MOVEMENT_TYPES if can(staff, f"{module_for_type(t)}.create")]
        if not allowed:
            return RedirectResponse("/dashboard", status_code=303)
        type = allowed[0]
    # only offer the movement types this user may create
    allowed_types = [t for t in MOVEMENT_TYPES if can(staff, f"{module_for_type(t)}.create")]
    products = db.query(Product).filter(Product.is_active).order_by(Product.name).all()
    return render(request, "movement_new.html", db, staff, products=products,
                  mtype=type, allowed_types=allowed_types)


@app.post("/movement/new")
def movement_create(request: Request, movement_type: str = Form(...),
                    product_id: int = Form(...), quantity: int = Form(...),
                    direction: str = Form("add"), unit_cost: str = Form(""),
                    note: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm=f"{module_for_type(movement_type)}.create")
    if redir:
        return redir
    if movement_type not in MOVEMENT_TYPES:
        return RedirectResponse("/movement/new", status_code=303)
    q = signed_qty(movement_type, quantity, direction)
    uc = None
    if unit_cost.strip():
        try:
            uc = float(unit_cost)
        except ValueError:
            uc = None
    db.add(StockMovement(product_id=product_id, movement_type=movement_type,
                         quantity=q, unit_cost=uc, note=note or None, staff_id=staff.id))
    db.commit()
    return RedirectResponse("/stock", status_code=303)


@app.get("/movement/{mid}/edit", response_class=HTMLResponse)
def movement_edit(request: Request, mid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    m = db.get(StockMovement, mid)
    if not m:
        return RedirectResponse("/records", status_code=303)
    if not can(staff, f"{module_for_type(m.movement_type)}.edit"):
        return RedirectResponse("/records", status_code=303)
    return render(request, "movement_edit.html", db, staff, m=m, error=None)


@app.post("/movement/{mid}/edit")
def movement_update(request: Request, mid: int, quantity: int = Form(...),
                    direction: str = Form("add"), unit_cost: str = Form(""),
                    note: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    m = db.get(StockMovement, mid)
    if not m:
        return RedirectResponse("/records", status_code=303)
    if not can(staff, f"{module_for_type(m.movement_type)}.edit"):
        return RedirectResponse("/records", status_code=303)
    m.quantity = signed_qty(m.movement_type, quantity, direction)
    m.unit_cost = float(unit_cost) if unit_cost.strip() else None
    m.note = note or None
    db.commit()
    return RedirectResponse("/records", status_code=303)


@app.post("/movement/{mid}/delete")
def movement_delete(request: Request, mid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    m = db.get(StockMovement, mid)
    if not m:
        return RedirectResponse("/records", status_code=303)
    if not can(staff, f"{module_for_type(m.movement_type)}.delete"):
        return RedirectResponse("/records", status_code=303)
    db.delete(m)
    db.commit()
    return RedirectResponse("/records", status_code=303)


# ---------- sales: edit / delete ----------

@app.get("/sale/{sid}/edit", response_class=HTMLResponse)
def sale_edit(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="sales.edit")
    if redir:
        return redir
    sale = db.get(Sale, sid)
    if not sale:
        return RedirectResponse("/records", status_code=303)
    return render(request, "sale_edit.html", db, staff, sale=sale, error=None)


@app.post("/sale/{sid}/edit")
def sale_update(request: Request, sid: int, payment_method: str = Form("cash"),
                note: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="sales.edit")
    if redir:
        return redir
    sale = db.get(Sale, sid)
    if not sale:
        return RedirectResponse("/records", status_code=303)
    sale.payment_method = payment_method
    sale.note = note or None
    db.commit()
    return RedirectResponse("/records", status_code=303)


@app.post("/sale/{sid}/delete")
def sale_delete(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="sales.delete")
    if redir:
        return redir
    sale = db.get(Sale, sid)
    if sale:
        db.delete(sale)  # cascades to sale_items
        db.commit()
    return RedirectResponse("/records", status_code=303)


# ---------- admin: products ----------

@app.get("/admin/products", response_class=HTMLResponse)

@app.get("/admin/products", response_class=HTMLResponse)
def products_list(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, ["items.create", "items.edit", "items.delete"]):
        return RedirectResponse("/dashboard", status_code=303)
    products = db.query(Product).order_by(Product.is_active.desc(), Product.category, Product.name).all()
    return render(request, "products.html", db, staff, products=products)


def _category_suggestions(db):
    rows = db.query(Product.category).distinct().all()
    cats = sorted({(c or "").strip() for (c,) in rows if c and c.strip()})
    for base in CATEGORIES:
        if base not in cats:
            cats.append(base)
    return cats


@app.get("/admin/products/new", response_class=HTMLResponse)
def product_new(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="items.create")
    if redir:
        return redir
    return render(request, "product_form.html", db, staff, product=None, error=None,
                  category_suggestions=_category_suggestions(db))


@app.post("/admin/products/new")
def product_create(request: Request, sku: str = Form(...), name: str = Form(...),
                   category: str = Form(""), supplier: str = Form(""), unit: str = Form("each"),
                   selling_price: float = Form(...), cost_price: str = Form(""),
                   reorder_point: int = Form(0), db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="items.create")
    if redir:
        return redir
    if unit not in UNITS:
        return render(request, "product_form.html", db, staff, product=None,
                      error="Invalid unit.", category_suggestions=_category_suggestions(db))
    if db.query(Product).filter(Product.sku == sku).first():
        return render(request, "product_form.html", db, staff, product=None,
                      error=f"SKU '{sku}' already exists.",
                      category_suggestions=_category_suggestions(db))
    cp = float(cost_price) if cost_price.strip() else None
    db.add(Product(sku=sku.strip(), name=name.strip(),
                   category=(category.strip() or None), supplier=(supplier.strip() or None),
                   unit=unit, selling_price=selling_price, cost_price=cp,
                   reorder_point=reorder_point))
    db.commit()
    return RedirectResponse("/admin/products", status_code=303)


@app.get("/admin/products/{pid}/edit", response_class=HTMLResponse)
def product_edit(request: Request, pid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="items.edit")
    if redir:
        return redir
    product = db.get(Product, pid)
    return render(request, "product_form.html", db, staff, product=product, error=None,
                  category_suggestions=_category_suggestions(db))


@app.post("/admin/products/{pid}/edit")
def product_update(request: Request, pid: int, name: str = Form(...),
                   category: str = Form(""), supplier: str = Form(""), unit: str = Form("each"),
                   selling_price: float = Form(...), cost_price: str = Form(""),
                   reorder_point: int = Form(0), is_active: str = Form("on"),
                   db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="items.edit")
    if redir:
        return redir
    product = db.get(Product, pid)
    if not product:
        return RedirectResponse("/admin/products", status_code=303)
    product.name = name.strip()
    product.category = category.strip() or None
    product.supplier = supplier.strip() or None
    product.unit = unit
    product.selling_price = selling_price
    product.cost_price = float(cost_price) if cost_price.strip() else None
    product.reorder_point = reorder_point
    product.is_active = is_active == "on"
    db.commit()
    return RedirectResponse("/admin/products", status_code=303)


@app.post("/admin/products/{pid}/delete")
def product_delete(request: Request, pid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="items.delete")
    if redir:
        return redir
    product = db.get(Product, pid)
    if not product:
        return RedirectResponse("/admin/products", status_code=303)
    referenced = (
        db.query(SaleItem).filter(SaleItem.product_id == pid).first()
        or db.query(StockMovement).filter(StockMovement.product_id == pid).first()
    )
    if referenced:
        # keep history intact — deactivate instead of hard delete
        product.is_active = False
    else:
        db.delete(product)
    db.commit()
    return RedirectResponse("/admin/products", status_code=303)


# ---------- admin: staff ----------

@app.get("/admin/staff", response_class=HTMLResponse)
def staff_list(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    people = db.query(Staff).order_by(Staff.is_active.desc(), Staff.name).all()
    return render(request, "staff.html", db, staff, people=people)


@app.get("/admin/staff/new", response_class=HTMLResponse)
def staff_new(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    return render(request, "staff_form.html", db, staff, person=None, error=None)


def _clean_perms(values):
    return ",".join(v for v in values if v in PERMISSION_KEYS)


def _norm_username(u):
    return "".join(c for c in (u or "").strip().lower() if c.isalnum() or c in "._-")


@app.post("/admin/staff/new")
async def staff_create(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    form = await request.form()
    name = (form.get("name") or "").strip()
    username = _norm_username(form.get("username"))
    role = form.get("role") if form.get("role") in ROLES else "staff"
    pin = form.get("pin") or ""
    phone = (form.get("phone") or "").strip()
    perms = _clean_perms(form.getlist("permissions"))
    if not name or not username:
        return render(request, "staff_form.html", db, staff, person=None,
                      error="Name and username are required.")
    if db.query(Staff).filter(func.lower(Staff.username) == username).first():
        return render(request, "staff_form.html", db, staff, person=None,
                      error=f"Username '{username}' is already taken.")
    if len(pin) < 4:
        return render(request, "staff_form.html", db, staff, person=None,
                      error="PIN must be at least 4 digits.")
    h, s = hash_pin(pin)
    db.add(Staff(username=username, name=name, role=role, pin_hash=h, pin_salt=s,
                 permissions=perms, phone=phone or None))
    db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


@app.get("/admin/staff/{sid}/edit", response_class=HTMLResponse)
def staff_edit(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    person = db.get(Staff, sid)
    return render(request, "staff_form.html", db, staff, person=person, error=None)


@app.post("/admin/staff/{sid}/edit")
async def staff_update(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    person = db.get(Staff, sid)
    if not person:
        return RedirectResponse("/admin/staff", status_code=303)
    form = await request.form()
    name = (form.get("name") or "").strip()
    username = _norm_username(form.get("username"))
    pin = form.get("pin") or ""
    if username and username != person.username:
        if db.query(Staff).filter(func.lower(Staff.username) == username,
                                  Staff.id != person.id).first():
            return render(request, "staff_form.html", db, staff, person=person,
                          error=f"Username '{username}' is already taken.")
        person.username = username
    person.name = name or person.name
    person.role = form.get("role") if form.get("role") in ROLES else "staff"
    person.phone = (form.get("phone") or "").strip() or None
    person.is_active = form.get("is_active") == "on"
    person.permissions = _clean_perms(form.getlist("permissions"))
    if pin.strip():
        if len(pin) < 4:
            return render(request, "staff_form.html", db, staff, person=person,
                          error="PIN must be at least 4 digits.")
        person.pin_hash, person.pin_salt = hash_pin(pin)
    db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


# ---------- admin: reports ----------

def _range(period: str):
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    else:
        start = now - timedelta(days=3650)
    return start, now


@app.get("/admin/reports", response_class=HTMLResponse)
def reports(request: Request, period: str = "week", db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="view_reports")
    if redir:
        return redir
    start, end = _range(period)

    daily = (
        db.query(func.date(Sale.sold_at).label("day"),
                 func.sum(SaleItem.quantity).label("units"),
                 func.sum(SaleItem.quantity * SaleItem.unit_price).label("revenue"))
        .join(SaleItem, SaleItem.sale_id == Sale.id)
        .filter(Sale.sold_at >= start)
        .group_by(func.date(Sale.sold_at)).order_by(func.date(Sale.sold_at).desc()).all()
    )
    sellers = (
        db.query(Product.name,
                 func.sum(SaleItem.quantity).label("units"),
                 func.sum(SaleItem.quantity * SaleItem.unit_price).label("revenue"))
        .join(SaleItem, SaleItem.product_id == Product.id)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .filter(Sale.sold_at >= start)
        .group_by(Product.name).order_by(func.sum(SaleItem.quantity).desc()).all()
    )
    margins = (
        db.query(
            Product.name,
            func.sum(SaleItem.quantity * SaleItem.unit_price).label("revenue"),
            func.sum(SaleItem.quantity * func.coalesce(SaleItem.cost_price, Product.cost_price, 0)).label("cost"),
        )
        .join(SaleItem, SaleItem.product_id == Product.id)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .filter(Sale.sold_at >= start)
        .group_by(Product.name).all()
    )
    margin_rows = []
    for name, rev, cost in margins:
        rev = float(rev or 0)
        cost = float(cost or 0)
        gp = rev - cost
        pct = (gp / rev * 100) if rev else 0
        margin_rows.append({"name": name, "revenue": rev, "cost": cost, "gp": gp, "pct": pct})
    margin_rows.sort(key=lambda r: r["gp"], reverse=True)

    total_rev = sum(float(d.revenue or 0) for d in daily)
    total_units = sum(int(d.units or 0) for d in daily)

    return render(request, "reports.html", db, staff, period=period, daily=daily,
                  sellers=sellers, margins=margin_rows, levels=stock_levels(db),
                  total_rev=total_rev, total_units=total_units)


def inventory_valuation(db, end_u):
    """On-hand value per active product as of `end_u` (UTC upper bound, exclusive),
       grouped by category, with cost and retail totals."""
    mov = dict(
        db.query(StockMovement.product_id, func.coalesce(func.sum(StockMovement.quantity), 0))
        .filter(StockMovement.occurred_at < end_u)
        .group_by(StockMovement.product_id).all()
    )
    sold = dict(
        db.query(SaleItem.product_id, func.coalesce(func.sum(SaleItem.quantity), 0))
        .join(Sale, Sale.id == SaleItem.sale_id)
        .filter(Sale.sold_at < end_u)
        .group_by(SaleItem.product_id).all()
    )
    cats = {}
    tot_cost = tot_retail = 0.0
    tot_units = skus = 0
    for p in db.query(Product).filter(Product.is_active).order_by(Product.category, Product.name):
        on_hand = int(mov.get(p.id, 0)) - int(sold.get(p.id, 0))
        cost = float(p.cost_price) if p.cost_price is not None else None
        retail = float(p.selling_price)
        cval = (on_hand * cost) if cost is not None else None
        rval = on_hand * retail
        cat = p.category or "Uncategorized"
        g = cats.setdefault(cat, {"items": [], "cost": 0.0, "retail": 0.0, "has_missing": False})
        g["items"].append({"product": p, "on_hand": on_hand, "cost": cost,
                           "cval": cval, "retail": retail, "rval": rval})
        if cval is not None:
            g["cost"] += cval
        else:
            g["has_missing"] = True
        g["retail"] += rval
        tot_cost += (cval or 0.0)
        tot_retail += rval
        tot_units += on_hand
        skus += 1
    groups = [dict(category=k, **v) for k, v in sorted(cats.items())]
    return {"groups": groups, "tot_cost": tot_cost, "tot_retail": tot_retail,
            "tot_units": tot_units, "skus": skus}


@app.get("/admin/inventory-value", response_class=HTMLResponse)
def inventory_value(request: Request, as_of: str = "", view: str = "report",
                    db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="view_reports")
    if redir:
        return redir
    tz = _tz()
    now = datetime.now(tz)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    d = _parse_date(as_of, tz) or today0
    end_u = (d + timedelta(days=1)).astimezone(timezone.utc)
    val = inventory_valuation(db, end_u)
    is_today = d.date() == today0.date()
    return render(request, "inventory_value.html", db, staff, view=view,
                  as_of=d.strftime("%Y-%m-%d"),
                  as_of_label=f"{d:%B} {d.day}, {d.year}", is_today=is_today, **val)


# ===== TEMPORARY: dummy-data import / wipe (admin only). Safe to remove later. =====
_DUMMY_PRODUCTS = {
    "BANANA": ("Banana", "Fruits", "Market", 25, None),
    "POCARI-500": ("Pocari 500ml", "Sports Drink", "Otsuka Solar Philippines Inc", 80, 44),
    "EGG": ("Egg", None, "Market", 20, None),
    "SIP-WATER-500": ("Sip Water 500ml", "Water", "Pacific synergy", 25, 6),
    "SIP-YELLOW-500": ("Sip Yellow 500ml", "Water", "Pacific synergy", 75, 31),
    "SIP-PINK-500": ("Sip Pink 500ml", "Water", "Pacific synergy", 75, 31),
    "SIP-BLUE-500": ("Sip Blue 500ml", "Water", "Pacific synergy", 75, 31),
}


def _wipe_transactions(db):
    db.query(SaleItem).delete()
    db.query(Sale).delete()
    db.query(Payment).delete()
    db.query(StockMovement).delete()
    db.query(Customer).delete()
    db.commit()


@app.get("/admin/dummy", response_class=HTMLResponse)
def dummy_home(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if staff.role != "admin":
        return RedirectResponse("/dashboard", status_code=303)
    done = request.query_params.get("done", "")
    msg = ""
    if done == "imported":
        msg = f"<div class='savedmsg' style='background:#e5f5ec;border:1px solid #a9ddc0;color:#127a45;padding:10px 12px;border-radius:8px;margin:10px 0'>✓ Imported {request.query_params.get('n','')} records from the CSV.</div>"
    elif done == "wiped":
        msg = "<div class='savedmsg' style='background:#fdecea;border:1px solid #e6a49b;color:#9c2c1e;padding:10px 12px;border-radius:8px;margin:10px 0'>✓ All sales, stock movements, payments and customers were deleted.</div>"
    elif done == "repriced":
        msg = f"<div class='savedmsg' style='background:#e5f5ec;border:1px solid #a9ddc0;color:#127a45;padding:10px 12px;border-radius:8px;margin:10px 0'>✓ Repriced {request.query_params.get('sku','')} — updated {request.query_params.get('n','0')} past sale line(s).</div>"
    elif done == "noitem":
        msg = "<div class='savedmsg' style='background:#fdecea;border:1px solid #e6a49b;color:#9c2c1e;padding:10px 12px;border-radius:8px;margin:10px 0'>No item with that SKU.</div>"
    body = f"""<h1>Dummy data tools</h1>{msg}
    <p class="muted">Temporary tools for loading test data. Both actions affect the live database.</p>
    <div class="card"><h2 style="margin-top:0">Load dummy data</h2>
      <p class="muted small">Clears existing sales/movements/customers, then loads the AWAKEN Retail 2026 log (restocks + sales + credit customers).</p>
      <form method="post" action="/admin/import-dummy" onsubmit="return confirm('This wipes current transactions and loads the CSV data. Continue?')">
        <button class="btn primary" type="submit">Load dummy data from CSV</button></form></div>
    <div class="card"><h2 style="margin-top:0">Reprice an item</h2>
      <p class="muted small">Sets a new selling price on an item, and (optionally) rewrites that price onto every past sale of it.</p>
      <form method="post" action="/admin/reprice" class="two-col" style="align-items:end">
        <div><label>SKU</label><input name="sku" value="POCARI-500"></div>
        <div><label>New selling price (₱)</label><input name="price" type="number" step="0.01" value="80"></div>
        <label class="check" style="grid-column:1/-1"><input type="checkbox" name="apply_all" checked> Also update all past transactions of this item</label>
        <button class="btn primary" type="submit" style="grid-column:1/-1;justify-self:start">Apply new price</button>
      </form></div>
    <div class="card"><h2 style="margin-top:0">Wipe everything</h2>
      <p class="muted small">Deletes ALL sales, stock movements, payments and customers (keeps your product catalog). Use this after testing.</p>
      <form method="post" action="/admin/wipe-dummy" onsubmit="return confirm('Delete ALL sales, movements, payments and customers? This cannot be undone.')">
        <button class="btn" style="border-color:#c0392b;color:#c0392b" type="submit">Wipe all transactions</button></form></div>"""
    return render(request, "dummy.html", db, staff, body_html=body)


@app.post("/admin/import-dummy")
def dummy_import(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if staff.role != "admin":
        return RedirectResponse("/dashboard", status_code=303)
    import json as _json
    path = os.path.join(os.path.dirname(__file__), "seed_dummy.json")
    with open(path) as fh:
        recs = _json.load(fh)
    # ensure products exist
    prods = {}
    for sku, (name, cat, sup, price, cost) in _DUMMY_PRODUCTS.items():
        p = db.query(Product).filter(Product.sku == sku).first()
        if not p:
            p = Product(sku=sku, name=name, category=cat, supplier=sup,
                        unit="each", selling_price=price, cost_price=cost, reorder_point=0)
            db.add(p)
            db.flush()
        prods[sku] = p
    _wipe_transactions(db)
    n = 0
    for r in recs:
        p = prods.get(r["sku"])
        if not p:
            continue
        when = _sold_dt_from_date(r["date"])
        if r["kind"] == "in":
            db.add(StockMovement(product_id=p.id, movement_type="restock",
                                 quantity=int(r["qty"]), occurred_at=when, created_at=when,
                                 unit_cost=p.cost_price, note=r.get("note") or None, staff_id=staff.id))
        else:
            paid = r.get("paid", True)
            cust = None
            if r.get("customer"):
                cust = find_or_create_customer(db, r["customer"])
            sale = Sale(staff_id=staff.id, sold_at=when, is_credit=(not paid),
                        customer_id=(cust.id if cust else None), note=r.get("note") or None,
                        payment_method=(r.get("payment") or "cash") if paid else None)
            db.add(sale)
            db.flush()
            db.add(SaleItem(sale_id=sale.id, product_id=p.id, quantity=int(r["qty"]),
                            unit_price=p.selling_price, cost_price=p.cost_price))
        n += 1
    db.commit()
    return RedirectResponse(f"/admin/dummy?done=imported&n={n}", status_code=303)


@app.post("/admin/reprice")
def dummy_reprice(request: Request, sku: str = Form(...), price: float = Form(...),
                  apply_all: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if staff.role != "admin":
        return RedirectResponse("/dashboard", status_code=303)
    p = db.query(Product).filter(Product.sku == sku.strip()).first()
    if not p:
        return RedirectResponse("/admin/dummy?done=noitem", status_code=303)
    p.selling_price = price
    n = 0
    if apply_all == "on":
        items = db.query(SaleItem).filter(SaleItem.product_id == p.id).all()
        for it in items:
            it.unit_price = price
            n += 1
    db.commit()
    return RedirectResponse(f"/admin/dummy?done=repriced&sku={p.sku}&n={n}", status_code=303)


@app.post("/admin/wipe-dummy")
def dummy_wipe(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if staff.role != "admin":
        return RedirectResponse("/dashboard", status_code=303)
    _wipe_transactions(db)
    return RedirectResponse("/admin/dummy?done=wiped", status_code=303)
# ===== end temporary dummy-data tools =====


@app.get("/admin/reports/sales.csv")
def reports_csv(request: Request, period: str = "all", db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="view_reports")
    if redir:
        return redir
    start, end = _range(period)
    rows = (
        db.query(Sale.sold_at, Staff.name, Sale.payment_method, Product.sku,
                 Product.name, SaleItem.quantity, SaleItem.unit_price)
        .join(SaleItem, SaleItem.sale_id == Sale.id)
        .join(Product, Product.id == SaleItem.product_id)
        .outerjoin(Staff, Staff.id == Sale.staff_id)
        .filter(Sale.sold_at >= start).order_by(Sale.sold_at.desc()).all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["sold_at", "staff", "payment", "sku", "product", "qty", "unit_price", "line_total"])
    for sold_at, sname, pay, sku, pname, qty, price in rows:
        w.writerow([sold_at, sname or "", pay or "", sku, pname, qty, price, float(qty) * float(price)])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=awaken_sales.csv"},
    )


RECORD_PERMS = ["view_reports", "sales.edit", "sales.delete",
                "receive.edit", "receive.delete", "adjust.edit", "adjust.delete"]


@app.get("/records", response_class=HTMLResponse)
def records(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, RECORD_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    movements = (
        db.query(StockMovement).order_by(StockMovement.occurred_at.desc()).limit(100).all()
    )
    sales = db.query(Sale).order_by(Sale.sold_at.desc()).limit(50).all()
    return render(request, "records.html", db, staff, movements=movements, sales=sales)


# keep the old path working
@app.get("/admin/history")
def history_redirect():
    return RedirectResponse("/records", status_code=307)


# ---------- customers & payments ----------

CUSTOMER_VIEW_PERMS = ["view_reports", "payments.create", "payments.edit", "payments.delete"]


def customer_balances(db):
    """Per-customer: charges from credit sales, payments made, and balance."""
    charges = {}
    credit_sales = (
        db.query(Sale).filter(Sale.is_credit == True, Sale.customer_id != None).all()  # noqa: E712,E711
    )
    for s in credit_sales:
        charges[s.customer_id] = charges.get(s.customer_id, 0.0) + s.total
    paid = dict(
        db.query(Payment.customer_id, func.coalesce(func.sum(Payment.amount), 0))
        .group_by(Payment.customer_id).all()
    )
    rows = []
    for c in db.query(Customer).order_by(Customer.name).all():
        ch = charges.get(c.id, 0.0)
        pd = float(paid.get(c.id, 0) or 0)
        rows.append({"customer": c, "charges": ch, "paid": pd, "balance": ch - pd})
    return rows


@app.get("/customers", response_class=HTMLResponse)
def customers_list(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, CUSTOMER_VIEW_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    rows = customer_balances(db)
    outstanding = [r for r in rows if r["balance"] > 0.005]
    total_out = sum(r["balance"] for r in rows)
    return render(request, "customers.html", db, staff, rows=rows,
                  outstanding=outstanding, total_out=total_out)


@app.get("/customer/{cid}", response_class=HTMLResponse)
def customer_detail(request: Request, cid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, CUSTOMER_VIEW_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    customer = db.get(Customer, cid)
    if not customer:
        return RedirectResponse("/customers", status_code=303)
    sales = (
        db.query(Sale).filter(Sale.customer_id == cid, Sale.is_credit == True)  # noqa: E712
        .order_by(Sale.sold_at.desc()).all()
    )
    payments = (
        db.query(Payment).filter(Payment.customer_id == cid)
        .order_by(Payment.paid_at.desc()).all()
    )
    charges = sum(s.total for s in sales)
    paid = sum(float(p.amount) for p in payments)
    return render(request, "customer_detail.html", db, staff, customer=customer,
                  sales=sales, payments=payments, charges=charges, paid=paid,
                  balance=charges - paid)


@app.get("/customer/{cid}/pay", response_class=HTMLResponse)
def pay_form(request: Request, cid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="payments.create")
    if redir:
        return redir
    customer = db.get(Customer, cid)
    if not customer:
        return RedirectResponse("/customers", status_code=303)
    rows = {r["customer"].id: r for r in customer_balances(db)}
    balance = rows.get(cid, {}).get("balance", 0.0)
    return render(request, "customer_pay.html", db, staff, customer=customer,
                  balance=balance, error=None)


@app.post("/customer/{cid}/pay")
async def pay_create(request: Request, cid: int, amount: str = Form(...),
                     method: str = Form("cash"), note: str = Form(""),
                     screenshot: UploadFile = None, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="payments.create")
    if redir:
        return redir
    customer = db.get(Customer, cid)
    if not customer:
        return RedirectResponse("/customers", status_code=303)
    try:
        amt = float(amount)
    except (ValueError, TypeError):
        amt = 0
    if amt <= 0:
        return render(request, "customer_pay.html", db, staff, customer=customer,
                      balance=0, error="Enter a payment amount greater than zero.")
    img_bytes, img_mime = None, None
    if screenshot is not None and screenshot.filename:
        img_bytes = await screenshot.read()
        img_mime = screenshot.content_type or "image/jpeg"
        if len(img_bytes) > 8 * 1024 * 1024:  # 8 MB cap
            return render(request, "customer_pay.html", db, staff, customer=customer,
                          balance=0, error="Screenshot is too large (max 8 MB).")
    db.add(Payment(customer_id=cid, amount=amt, method=method, note=note or None,
                   screenshot=img_bytes, screenshot_mime=img_mime, staff_id=staff.id))
    db.commit()
    return RedirectResponse(f"/customer/{cid}", status_code=303)


@app.get("/payment/{pid}/screenshot")
def payment_screenshot(request: Request, pid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, CUSTOMER_VIEW_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    p = db.get(Payment, pid)
    if not p or not p.screenshot:
        return Response(status_code=404)
    return Response(content=p.screenshot, media_type=p.screenshot_mime or "image/jpeg")


@app.post("/payment/{pid}/delete")
def payment_delete(request: Request, pid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="payments.delete")
    if redir:
        return redir
    p = db.get(Payment, pid)
    if p:
        cid = p.customer_id
        db.delete(p)
        db.commit()
        return RedirectResponse(f"/customer/{cid}", status_code=303)
    return RedirectResponse("/customers", status_code=303)


# ---------- Sales spreadsheet (autosave grid) ----------

def _sold_dt_from_date(date_str):
    tz = _tz()
    now = datetime.now(tz)
    if not date_str:
        return now.astimezone(timezone.utc)
    try:
        y, m, d = [int(x) for x in str(date_str).split("-")]
        chosen = datetime(y, m, d, tzinfo=tz)
    except Exception:
        return now.astimezone(timezone.utc)
    dt = now if chosen.date() == now.date() else chosen.replace(hour=12, minute=0)
    return dt.astimezone(timezone.utc)


def _sale_row(sale):
    tz = _tz()
    it = sale.items[0] if sale.items else None
    local = sale.sold_at.astimezone(tz) if sale.sold_at else datetime.now(tz)
    up = float(it.unit_price) if it else 0.0
    qn = it.quantity if it else 0
    return {
        "id": sale.id,
        "date": local.strftime("%Y-%m-%d"),
        "product_id": it.product_id if it else None,
        "qty": qn,
        "unit_price": up,
        "total": up * qn,
        "paid": (not sale.is_credit),
        "customer_id": sale.customer_id,
        "staff": sale.staff.name if sale.staff else "",
    }


SALES_SHEET_PERMS = ["sales.create", "sales.edit", "view_reports"]


def _parse_date(s, tz):
    try:
        y, m, d = [int(x) for x in str(s).split("-")]
        return datetime(y, m, d, tzinfo=tz)
    except Exception:
        return None


@app.get("/sales", response_class=HTMLResponse)
def sales_sheet(request: Request, rng: str = "", db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, SALES_SHEET_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    tz = _tz()
    now = datetime.now(tz)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")

    qp = request.query_params

    # Optional customer search: show one customer's transactions.
    selected_customer = None
    if qp.get("customer"):
        try:
            selected_customer = db.get(Customer, int(qp.get("customer")))
        except (ValueError, TypeError):
            selected_customer = None

    range_key = qp.get("range", "") or rng
    from_s, to_s = qp.get("from", ""), qp.get("to", "")
    explicit_date = bool(qp.get("range") or from_s or to_s)
    if range_key == "7d":
        start, end_d = today0 - timedelta(days=6), today0
    elif range_key == "month":
        start, end_d = today0.replace(day=1), today0
    elif range_key == "all":
        start, end_d = None, today0
    elif from_s or to_s:
        start = _parse_date(from_s, tz) or today0
        end_d = _parse_date(to_s, tz) or today0
    elif selected_customer:  # customer view defaults to all their history
        range_key, start, end_d = "all", None, today0
    else:  # default: today
        range_key = range_key or "today"
        start, end_d = today0, today0

    q = db.query(Sale)
    if selected_customer:
        q = q.filter(Sale.customer_id == selected_customer.id)
    if start is not None:
        q = q.filter(Sale.sold_at >= start.astimezone(timezone.utc))
    q = q.filter(Sale.sold_at < (end_d + timedelta(days=1)).astimezone(timezone.utc))
    sales = q.order_by(Sale.sold_at.desc(), Sale.id.desc()).limit(1000).all()
    rows = [_sale_row(s) for s in sales]

    total = sum(r["total"] for r in rows)
    unpaid = sum(r["total"] for r in rows if not r["paid"])

    # Balances + sale counts, for the customer search dropdown hints.
    bal_by_id = {r["customer"].id: r for r in customer_balances(db)}
    counts = dict(
        db.query(Sale.customer_id, func.count(Sale.id))
        .filter(Sale.customer_id != None)  # noqa: E711
        .group_by(Sale.customer_id).all()
    )
    products = [
        {"id": p.id, "name": p.name, "price": float(p.selling_price)}
        for p in db.query(Product).filter(Product.is_active).order_by(Product.name).all()
    ]
    customers = [
        {"id": c.id, "name": c.name,
         "owed": round(float(bal_by_id.get(c.id, {}).get("balance", 0.0)), 2),
         "sales": int(counts.get(c.id, 0))}
        for c in db.query(Customer).order_by(Customer.name).all()
    ]
    cust_ctx = None
    if selected_customer:
        cust_ctx = {"id": selected_customer.id, "name": selected_customer.name,
                    "balance": float(bal_by_id.get(selected_customer.id, {}).get("balance", 0.0))}

    return render(request, "sales_sheet.html", db, staff, products=products,
                  customers=customers, rows=rows, today=today, total=total,
                  unpaid=unpaid, count=len(rows), range_key=range_key, cust=cust_ctx,
                  from_s=(start.strftime("%Y-%m-%d") if start else ""),
                  to_s=end_d.strftime("%Y-%m-%d"))


def _json_guard(request, db, perm):
    staff = current_staff(request, db)
    if not staff:
        return None, JSONResponse({"error": "auth"}, status_code=401)
    if perm and not can(staff, perm):
        return None, JSONResponse({"error": "forbidden"}, status_code=403)
    return staff, None


@app.post("/api/sales")
async def api_sale_create(request: Request, db: Session = Depends(get_db)):
    staff, err = _json_guard(request, db, "sales.create")
    if err:
        return err
    data = await request.json()
    p = db.get(Product, data.get("product_id") or 0)
    if not p:
        return JSONResponse({"error": "product required"}, status_code=400)
    try:
        qty = max(1, int(data.get("qty") or 1))
    except (ValueError, TypeError):
        qty = 1
    paid = bool(data.get("paid", True))
    cid = data.get("customer_id") or None
    up = data.get("unit_price")
    try:
        up = float(up) if up not in (None, "") else float(p.selling_price)
    except (ValueError, TypeError):
        up = float(p.selling_price)
    sale = Sale(staff_id=staff.id, sold_at=_sold_dt_from_date(data.get("date")),
                is_credit=(not paid), customer_id=cid,
                payment_method=(None if not paid else "cash"))
    db.add(sale)
    db.flush()
    db.add(SaleItem(sale_id=sale.id, product_id=p.id, quantity=qty,
                    unit_price=up, cost_price=p.cost_price))
    db.commit()
    db.refresh(sale)
    return JSONResponse(_sale_row(sale))


@app.patch("/api/sales/{sid}")
async def api_sale_update(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, err = _json_guard(request, db, "sales.edit")
    if err:
        return err
    sale = db.get(Sale, sid)
    if not sale:
        return JSONResponse({"error": "not found"}, status_code=404)
    data = await request.json()
    if "date" in data:
        sale.sold_at = _sold_dt_from_date(data.get("date"))
    if "paid" in data:
        sale.is_credit = not bool(data.get("paid"))
        sale.payment_method = None if sale.is_credit else (sale.payment_method or "cash")
    if "customer_id" in data:
        sale.customer_id = data.get("customer_id") or None
    it = sale.items[0] if sale.items else None
    if "product_id" in data and data.get("product_id"):
        p = db.get(Product, data.get("product_id"))
        if p:
            if not it:
                it = SaleItem(sale_id=sale.id, product_id=p.id, quantity=1,
                              unit_price=p.selling_price, cost_price=p.cost_price)
                db.add(it)
            else:
                it.product_id = p.id
                it.unit_price = p.selling_price
                it.cost_price = p.cost_price
    if "qty" in data and it:
        try:
            it.quantity = max(1, int(data.get("qty")))
        except (ValueError, TypeError):
            pass
    if "unit_price" in data and it and data.get("unit_price") not in (None, ""):
        try:
            it.unit_price = float(data.get("unit_price"))
        except (ValueError, TypeError):
            pass
    db.commit()
    db.refresh(sale)
    return JSONResponse(_sale_row(sale))


@app.delete("/api/sales/{sid}")
def api_sale_delete(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, err = _json_guard(request, db, "sales.delete")
    if err:
        return err
    sale = db.get(Sale, sid)
    if sale:
        db.delete(sale)
        db.commit()
    return JSONResponse({"ok": True})


@app.post("/api/customers")
async def api_customer_create(request: Request, db: Session = Depends(get_db)):
    staff, err = _json_guard(request, db, "sales.create")
    if err:
        return err
    data = await request.json()
    c = find_or_create_customer(db, data.get("name"))
    if not c:
        return JSONResponse({"error": "name required"}, status_code=400)
    db.commit()
    return JSONResponse({"id": c.id, "name": c.name})


# Simple single-sale entry (used by the phone/stacked view; plain form, no JS)
@app.post("/sale/quick")
def sale_quick(request: Request, product_id: int = Form(...), quantity: int = Form(1),
               date: str = Form(""), paid: str = Form(""), customer_id: str = Form(""),
               customer_name: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="sales.create")
    if redir:
        return redir
    p = db.get(Product, product_id)
    if not p:
        return RedirectResponse("/sales", status_code=303)
    is_paid = paid == "on"
    customer = None
    if customer_name.strip():
        customer = find_or_create_customer(db, customer_name)
    elif customer_id.strip():
        customer = db.get(Customer, int(customer_id))
    sale = Sale(staff_id=staff.id, sold_at=_sold_dt_from_date(date),
                is_credit=(not is_paid), customer_id=(customer.id if customer else None),
                payment_method=(None if not is_paid else "cash"))
    db.add(sale)
    db.flush()
    db.add(SaleItem(sale_id=sale.id, product_id=p.id, quantity=max(1, quantity),
                    unit_price=p.selling_price, cost_price=p.cost_price))
    db.commit()
    return RedirectResponse("/sales?saved=1", status_code=303)


# old single-sale form path now points to the sheet
@app.get("/sale/new")
def sale_new_redirect():
    return RedirectResponse("/sales", status_code=307)


# ================= Coaches & Corkage module =================

def require_admin(request, db):
    staff, redir = require(request, db)
    if redir:
        return None, redir
    if staff.role != "admin":
        return None, RedirectResponse("/dashboard", status_code=303)
    return staff, None


def _date_only(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError, AttributeError):
        return None


# Volume policy: the first N clients per coach bill at their set rate;
# every client beyond that is discounted to TIER_CORKAGE.
FIRST_TIER_CLIENTS = 5
TIER_CORKAGE = 2500.0


def _member_sort_key(m):
    return (m.start_date or date(2100, 1, 1), m.id)


def coach_corkage(members):
    """Tiered corkage total for one coach's active members (first 5 at their rate,
       the 6th onward capped at TIER_CORKAGE)."""
    ordered = sorted(members, key=_member_sort_key)
    total = 0.0
    for i, m in enumerate(ordered):
        base = float(m.corkage_rate or 0)
        total += base if i < FIRST_TIER_CLIENTS else min(base, TIER_CORKAGE)
    return total


def coach_rows(db):
    members = db.query(Member).filter(Member.is_active == True).all()  # noqa: E712
    by = {}
    for m in members:
        by.setdefault(m.coach_id, []).append(m)
    rows = []
    for c in db.query(Coach).order_by(Coach.is_active.desc(), Coach.name).all():
        ms = by.get(c.id, [])
        corkage = coach_corkage(ms)
        fee = float(c.affiliate_fee or 0) if c.coach_type == "affiliate" else 0.0
        monthly = (fee + corkage) if c.coach_type == "affiliate" else 0.0
        rows.append({"coach": c, "clients": len(ms), "corkage": corkage,
                     "fee": fee, "monthly": monthly,
                     "discounted": max(0, len(ms) - FIRST_TIER_CLIENTS)})
    return rows


def coach_summary(rows):
    aff = [r for r in rows if r["coach"].coach_type == "affiliate" and r["coach"].is_active]
    return {"total_monthly": sum(r["monthly"] for r in aff),
            "affiliate_count": len(aff),
            "member_count": sum(r["clients"] for r in aff)}


@app.get("/coaches", response_class=HTMLResponse)
def coaches_page(request: Request, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    rows = coach_rows(db)
    return render(request, "coaches.html", db, staff, rows=rows,
                  summ=coach_summary(rows), active="coaches",
                  today=datetime.now(_tz()).date())


@app.get("/coaches/new", response_class=HTMLResponse)
def coach_new(request: Request, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    today = datetime.now(_tz()).strftime("%Y-%m-%d")
    return render(request, "coach_form.html", db, staff, coach=None,
                  coach_types=COACH_TYPES, today=today)


@app.post("/coaches/new")
def coach_create(request: Request, name: str = Form(...), coach_type: str = Form("affiliate"),
                 affiliate_fee: str = Form("0"), start_date: str = Form(""),
                 next_billing: str = Form(""), is_active: str = Form(""),
                 db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    fee = 0.0
    try:
        fee = float(affiliate_fee) if affiliate_fee.strip() else 0.0
    except ValueError:
        fee = 0.0
    db.add(Coach(name=name.strip(), coach_type=coach_type,
                 affiliate_fee=(fee if coach_type == "affiliate" else 0),
                 start_date=_date_only(start_date), next_billing=_date_only(next_billing),
                 is_active=(is_active == "on")))
    db.commit()
    return RedirectResponse("/coaches", status_code=303)


@app.get("/coaches/{cid}/edit", response_class=HTMLResponse)
def coach_edit(request: Request, cid: int, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    coach = db.get(Coach, cid)
    if not coach:
        return RedirectResponse("/coaches", status_code=303)
    return render(request, "coach_form.html", db, staff, coach=coach,
                  coach_types=COACH_TYPES, today="")


@app.post("/coaches/{cid}/edit")
def coach_update(request: Request, cid: int, name: str = Form(...),
                 coach_type: str = Form("affiliate"), affiliate_fee: str = Form("0"),
                 start_date: str = Form(""), next_billing: str = Form(""),
                 is_active: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    coach = db.get(Coach, cid)
    if not coach:
        return RedirectResponse("/coaches", status_code=303)
    try:
        fee = float(affiliate_fee) if affiliate_fee.strip() else 0.0
    except ValueError:
        fee = 0.0
    coach.name = name.strip()
    coach.coach_type = coach_type
    coach.affiliate_fee = fee if coach_type == "affiliate" else 0
    coach.start_date = _date_only(start_date)
    coach.next_billing = _date_only(next_billing)
    coach.is_active = (is_active == "on")
    db.commit()
    return RedirectResponse("/coaches", status_code=303)


@app.get("/coaches/members", response_class=HTMLResponse)
def members_page(request: Request, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    members = db.query(Member).order_by(Member.is_active.desc(), Member.name).all()
    active = [m for m in members if m.is_active]
    total = sum(float(m.corkage_rate or 0) for m in active)
    avg = (total / len(active)) if active else 0.0
    return render(request, "members.html", db, staff, members=members,
                  total_corkage=total, member_count=len(active), avg_corkage=avg,
                  active="members")


@app.get("/coaches/members/new", response_class=HTMLResponse)
def member_new(request: Request, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    coaches = db.query(Coach).filter(Coach.is_active == True).order_by(Coach.name).all()  # noqa: E712
    today = datetime.now(_tz()).strftime("%Y-%m-%d")
    return render(request, "member_form.html", db, staff, member=None,
                  coaches=coaches, today=today)


@app.post("/coaches/members/new")
def member_create(request: Request, name: str = Form(...), coach_id: str = Form(""),
                  corkage_rate: str = Form("3000"), start_date: str = Form(""),
                  is_active: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    try:
        rate = float(corkage_rate) if corkage_rate.strip() else 3000.0
    except ValueError:
        rate = 3000.0
    db.add(Member(name=name.strip(), coach_id=(int(coach_id) if coach_id.strip() else None),
                  corkage_rate=rate, start_date=_date_only(start_date),
                  is_active=(is_active == "on")))
    db.commit()
    return RedirectResponse("/coaches/members", status_code=303)


@app.get("/coaches/members/{mid}/edit", response_class=HTMLResponse)
def member_edit(request: Request, mid: int, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    member = db.get(Member, mid)
    if not member:
        return RedirectResponse("/coaches/members", status_code=303)
    coaches = db.query(Coach).order_by(Coach.name).all()
    return render(request, "member_form.html", db, staff, member=member,
                  coaches=coaches, today="")


@app.post("/coaches/members/{mid}/edit")
def member_update(request: Request, mid: int, name: str = Form(...), coach_id: str = Form(""),
                  corkage_rate: str = Form("3000"), start_date: str = Form(""),
                  is_active: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    member = db.get(Member, mid)
    if not member:
        return RedirectResponse("/coaches/members", status_code=303)
    try:
        rate = float(corkage_rate) if corkage_rate.strip() else 3000.0
    except ValueError:
        rate = 3000.0
    member.name = name.strip()
    member.coach_id = int(coach_id) if coach_id.strip() else None
    member.corkage_rate = rate
    member.start_date = _date_only(start_date)
    member.is_active = (is_active == "on")
    db.commit()
    return RedirectResponse("/coaches/members", status_code=303)


@app.get("/coaches/billing", response_class=HTMLResponse)
def coaches_billing(request: Request, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    rows = [r for r in coach_rows(db) if r["coach"].coach_type == "affiliate" and r["coach"].is_active]
    return render(request, "coaches_billing.html", db, staff, rows=rows,
                  summ=coach_summary(coach_rows(db)), active="billing",
                  today=datetime.now(_tz()).date())


# ================= Transactions → Invoices & Payments =================

def can_invoices(staff):
    return staff.role == "admin" or can_any(
        staff, ["view_reports", "payments.create", "payments.edit", "payments.delete"])


def can_invoice_edit(staff):
    return staff.role == "admin" or can(staff, "payments.create")


def next_invoice_number(db):
    nums = []
    for (n,) in db.query(Invoice.number).all():
        try:
            nums.append(int(str(n).split("-")[-1]))
        except (ValueError, TypeError):
            pass
    return "INV-%04d" % ((max(nums) + 1) if nums else 1)


def _add_month(d):
    m = d.month + 1
    y = d.year + (1 if m > 12 else 0)
    m = 1 if m > 12 else m
    return date(y, m, min(d.day, 28))


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_invoices(staff):
        return RedirectResponse("/dashboard", status_code=303)
    invoices = db.query(Invoice).order_by(Invoice.created_at.desc(), Invoice.id.desc()).limit(500).all()
    now = datetime.now(_tz())
    inv_month = sum(i.total for i in invoices if not i.is_void and i.issue_date
                    and i.issue_date.year == now.year and i.issue_date.month == now.month)
    outstanding = sum(i.balance for i in invoices if not i.is_void)
    paid_total = sum(i.paid for i in invoices if not i.is_void)
    return render(request, "invoices.html", db, staff, invoices=invoices,
                  inv_month=inv_month, outstanding=outstanding, paid_total=paid_total,
                  can_edit=can_invoice_edit(staff))


@app.get("/invoices/new", response_class=HTMLResponse)
def invoice_new(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_invoice_edit(staff):
        return RedirectResponse("/invoices", status_code=303)
    tz = _tz()
    today = datetime.now(tz).date()
    coaches = db.query(Coach).filter(Coach.is_active == True).order_by(Coach.name).all()  # noqa: E712
    customers = db.query(Customer).order_by(Customer.name).all()
    return render(request, "invoice_form.html", db, staff, coaches=coaches,
                  customers=customers, number=next_invoice_number(db),
                  today=today.strftime("%Y-%m-%d"),
                  due=(today + timedelta(days=7)).strftime("%Y-%m-%d"))


@app.post("/invoices/new")
async def invoice_create(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_invoice_edit(staff):
        return RedirectResponse("/invoices", status_code=303)
    form = await request.form()
    bill_to = (form.get("bill_to_name") or "").strip()
    party = form.get("party") or ""  # "coach:3" / "customer:5" / ""
    coach_id = customer_id = None
    bill_type = "other"
    if party.startswith("coach:"):
        coach_id = int(party.split(":")[1]); bill_type = "coach"
        c = db.get(Coach, coach_id)
        if c and not bill_to:
            bill_to = c.name
    elif party.startswith("customer:"):
        customer_id = int(party.split(":")[1]); bill_type = "customer"
        c = db.get(Customer, customer_id)
        if c and not bill_to:
            bill_to = c.name
    if not bill_to:
        bill_to = "—"
    inv = Invoice(number=next_invoice_number(db), bill_to_type=bill_type,
                  coach_id=coach_id, customer_id=customer_id, bill_to_name=bill_to,
                  issue_date=_date_only(form.get("issue_date")),
                  due_date=_date_only(form.get("due_date")),
                  period=(form.get("period") or None), note=(form.get("note") or None),
                  staff_id=staff.id)
    db.add(inv)
    db.flush()
    for desc, qty, rate in zip(form.getlist("description"), form.getlist("qty"), form.getlist("rate")):
        if not (desc or "").strip():
            continue
        try:
            q = float(qty) if qty else 1
            r = float(rate) if rate else 0
        except ValueError:
            q, r = 1, 0
        db.add(InvoiceItem(invoice_id=inv.id, description=desc.strip(), qty=q, rate=r, amount=q * r))
    db.commit()
    return RedirectResponse(f"/invoices/{inv.id}", status_code=303)


@app.get("/invoices/{iid}", response_class=HTMLResponse)
def invoice_view(request: Request, iid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_invoices(staff):
        return RedirectResponse("/dashboard", status_code=303)
    inv = db.get(Invoice, iid)
    if not inv:
        return RedirectResponse("/invoices", status_code=303)
    return render(request, "invoice_view.html", db, staff, inv=inv,
                  methods=PAYMENT_METHODS, can_edit=can_invoice_edit(staff),
                  is_admin=(staff.role == "admin"),
                  today=datetime.now(_tz()).strftime("%Y-%m-%d"))


@app.post("/invoices/{iid}/pay")
def invoice_pay(request: Request, iid: int, amount: str = Form(...), method: str = Form("cash"),
                note: str = Form(""), date: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_invoice_edit(staff):
        return RedirectResponse("/invoices", status_code=303)
    inv = db.get(Invoice, iid)
    if not inv:
        return RedirectResponse("/invoices", status_code=303)
    try:
        amt = float(amount)
    except ValueError:
        amt = 0
    if amt > 0:
        when = _sold_dt_from_date(date) if date else datetime.now(timezone.utc)
        db.add(InvoicePayment(invoice_id=inv.id, amount=amt, method=method,
                              note=note or None, paid_at=when, staff_id=staff.id))
        db.commit()
    return RedirectResponse(f"/invoices/{iid}", status_code=303)


@app.post("/invoices/{iid}/void")
def invoice_void(request: Request, iid: int, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    inv = db.get(Invoice, iid)
    if inv:
        inv.is_void = True
        db.commit()
    return RedirectResponse(f"/invoices/{iid}", status_code=303)


@app.get("/payments", response_class=HTMLResponse)
def payments_page(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_invoices(staff):
        return RedirectResponse("/dashboard", status_code=303)
    rows = []
    for p in db.query(Payment).order_by(Payment.paid_at.desc()).limit(300).all():
        rows.append({"at": p.paid_at, "kind": "Customer payment",
                     "party": (p.customer.name if p.customer else "—"),
                     "amount": float(p.amount or 0), "method": p.method or "",
                     "link": f"/customer/{p.customer_id}"})
    for p in db.query(InvoicePayment).order_by(InvoicePayment.paid_at.desc()).limit(300).all():
        rows.append({"at": p.paid_at, "kind": "Invoice payment",
                     "party": (p.invoice.bill_to_name if p.invoice else "—"),
                     "amount": float(p.amount or 0), "method": p.method or "",
                     "link": f"/invoices/{p.invoice_id}"})
    tz = _tz()
    rows.sort(key=lambda r: r["at"] or datetime.now(timezone.utc), reverse=True)
    for r in rows:
        r["local"] = (r["at"].astimezone(tz) if r["at"] else datetime.now(tz))
    total = sum(r["amount"] for r in rows)
    return render(request, "payments.html", db, staff, rows=rows[:400], total=total)


@app.post("/coaches/billing/bill/{cid}")
def coach_bill(request: Request, cid: int, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    coach = db.get(Coach, cid)
    if not coach or coach.coach_type != "affiliate":
        return RedirectResponse("/coaches/billing", status_code=303)
    tz = _tz()
    today = datetime.now(tz).date()
    period = today.strftime("%B %Y")
    members = sorted(
        db.query(Member).filter(Member.coach_id == coach.id, Member.is_active == True).all(),  # noqa: E712
        key=_member_sort_key)
    inv = Invoice(number=next_invoice_number(db), bill_to_type="coach", coach_id=coach.id,
                  bill_to_name=coach.name, issue_date=today,
                  due_date=today + timedelta(days=7), period=period,
                  note="Auto-generated from Monthly billing", staff_id=staff.id)
    db.add(inv)
    db.flush()
    fee = float(coach.affiliate_fee or 0)
    if fee > 0:
        db.add(InvoiceItem(invoice_id=inv.id, description=f"Affiliate fee — {period}",
                           qty=1, rate=fee, amount=fee))
    for i, m in enumerate(members):
        base = float(m.corkage_rate or 0)
        rate = base if i < FIRST_TIER_CLIENTS else min(base, TIER_CORKAGE)
        db.add(InvoiceItem(invoice_id=inv.id, description=f"Corkage — {m.name}",
                           qty=1, rate=rate, amount=rate))
    coach.next_billing = _add_month(coach.next_billing or today)
    db.commit()
    return RedirectResponse(f"/invoices/{inv.id}", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- mobile PWA (additive: /m and /api/m/*) ----------
app.include_router(mobile_mod.router)
