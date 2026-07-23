import math
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Integer,
    LargeBinary, Numeric, String, Text,
)
from sqlalchemy.orm import relationship, backref
from .db import Base


def now_utc():
    return datetime.now(timezone.utc)


CATEGORIES = ["Food&Beverage", "Merchandise"]
UNITS = ["each"]
MOVEMENT_TYPES = ["restock", "waste", "missing", "adjustment", "return"]
PAYMENT_METHODS = ["cash", "card", "gcash", "other"]
ROLES = ["admin", "staff"]

# ---- Form-based permission matrix ----
# Each module has Create / Edit / Delete actions. Keys look like "sales.create".
MODULES = [
    ("sales", "Sales"),
    ("items", "Items"),
    ("receive", "Receive Inventory"),
    ("adjust", "Adjustment"),
    ("payments", "Payments"),
]
ACTIONS = [("create", "Create"), ("edit", "Edit"), ("delete", "Delete")]

# Report / visibility access toggles (not create/edit/delete).
ACCESS_DEFS = [
    ("view_reports", "View reports"),
    ("view_stock", "View stock levels"),
    ("view_costs", "See costs & profit margins"),
]

MODULE_KEYS = [f"{m}.{a}" for m, _ in MODULES for a, _ in ACTIONS]
ACCESS_KEYS = [k for k, _ in ACCESS_DEFS]
PERMISSION_KEYS = MODULE_KEYS + ACCESS_KEYS

DEFAULT_STAFF_PERMS = ["sales.create", "receive.create", "adjust.create", "view_stock"]

# Which module a stock-movement type belongs to.
RECEIVE_TYPES = ["restock", "return"]
ADJUST_TYPES = ["waste", "missing", "adjustment"]


def module_for_type(mtype):
    return "receive" if mtype in RECEIVE_TYPES else "adjust"


def perm_set(staff):
    if staff is None:
        return set()
    if staff.role == "admin":
        return set(PERMISSION_KEYS)
    return set(p for p in (staff.permissions or "").split(",") if p)


def can(staff, key):
    if staff is None:
        return False
    if staff.role == "admin":
        return True
    return key in perm_set(staff)


def can_any(staff, keys):
    return any(can(staff, k) for k in keys)


PERSON_TYPES = [("", "— none —"), ("employee", "Employee"), ("affiliate", "Affiliate")]
# All relationship types an entity can be (one unified table).
ENTITY_TYPES = [
    ("", "— none —"),
    ("customer", "Customer"),
    ("employee", "Employee"),
    ("affiliate", "Affiliate"),
    ("coach", "Coach"),
    ("member", "Member"),
    ("supplier", "Supplier"),
]
# Types that carry a personal discount code / pricing tier.
DISCOUNT_TYPES = ("employee", "affiliate")


class Role(Base):
    """A named role with a default permission set. Assigning a role to an entity
    fills in that entity's permissions."""
    __tablename__ = "roles"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    is_admin = Column(Boolean, nullable=False, default=False)   # full access
    permissions = Column(Text, nullable=False, default="")      # comma-separated keys
    is_system = Column(Boolean, nullable=False, default=False)  # built-in, undeletable
    created_at = Column(DateTime(timezone=True), default=now_utc)

    def perm_list(self):
        return [p for p in (self.permissions or "").split(",") if p]


class Staff(Base):
    """An entity: any person or party — staff/login, employee, affiliate, coach,
    supplier, customer, or member — all in one `entity` table, tagged by
    person_type. May have system access (login + role) and/or billing fields."""
    __tablename__ = "entity"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)                   # display name
    # --- relationship side ---
    person_type = Column(String)                            # '', employee, affiliate, supplier
    discount_code = Column(String, unique=True)             # personal code (E/A only)
    # --- system access side (only when has_access) ---
    has_access = Column(Boolean, nullable=False, default=True)
    username = Column(String, unique=True)                  # login handle (access only)
    role = Column(String, nullable=False, default="staff")  # 'admin'/'staff' (drives checks)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="SET NULL"))
    pin_hash = Column(String)
    pin_salt = Column(String)
    permissions = Column(Text, nullable=False, default="")  # comma-separated keys
    phone = Column(String)
    # --- affiliate / coach billing (affiliates only) ---
    affiliate_fee = Column(Numeric(10, 2))                  # monthly affiliate fee
    start_date = Column(Date)
    next_billing = Column(Date)
    # --- member (an affiliate's corkage client) ---
    corkage_rate = Column(Numeric(10, 2))                   # monthly corkage (members)
    affiliate_id = Column(Integer, ForeignKey("entity.id")) # member -> their affiliate
    # --- pricing ---
    pricing_group_id = Column(Integer, ForeignKey("pricing_groups.id", ondelete="SET NULL"))
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    role_obj = relationship("Role")
    affiliate = relationship("Staff", foreign_keys=[affiliate_id], remote_side=[id])
    pricing_group = relationship("PricingGroup", foreign_keys=[pricing_group_id])

    __table_args__ = (
        CheckConstraint("role IN ('admin','staff')", name="staff_role_check"),
    )


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    sku = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    supplier = Column(String)                    # optional supplier / source
    category = Column(String)                    # free-form category (optional)
    unit = Column(String, nullable=False, default="each")
    selling_price = Column(Numeric(10, 2), nullable=False)
    cost_price = Column(Numeric(10, 2))
    reorder_point = Column(Integer, nullable=False, default=0)
    image = Column(LargeBinary)              # product photo bytes (mobile tiles)
    image_mime = Column(String)              # e.g. image/jpeg
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    __table_args__ = (
        CheckConstraint("unit IN ('each')", name="products_unit_check"),
    )


# Stock movements were merged into transactions (type='inventory_adjustment',
# subtype = restock/waste/…); the legacy stock_movements table is dropped at startup.


# Customers were merged into the unified `entity` table (person_type='customer');
# the legacy `customers` table is migrated then dropped at startup.


# Coaches were merged into the unified Staff/entity table; the legacy `coaches`
# table is migrated then dropped at startup. No ORM model remains for it.


# Members were merged into the unified `entity` table (person_type='member',
# with corkage_rate + affiliate_id); the legacy `members` table is dropped at startup.


# Legacy sales/orders/invoices (+ their items & payments) were merged into the
# unified transactions table below; those tables are migrated then dropped at startup.


# ===================== Unified transactions =====================
# One table for every money movement, distinguished by `type`:
#   cash_sale  – an instant retail sale (paid, or is_credit=unpaid)
#   order      – a customer self-checkout order awaiting staff confirmation
#   invoice    – a billing document (affiliate corkage / customer / other)
#   payment    – money received (against an invoice, or a customer balance)
#   inventory_adjustment – a stock movement (subtype = restock/waste/missing/…)
TX_CASH_SALE = "cash_sale"
TX_ORDER = "order"
TX_INVOICE = "invoice"
TX_PAYMENT = "payment"
TX_INVENTORY = "inventory_adjustment"
TRANSACTION_TYPES = [
    (TX_CASH_SALE, "Cash sale"),
    (TX_ORDER, "Order"),
    (TX_INVOICE, "Invoice"),
    (TX_PAYMENT, "Payment"),
    (TX_INVENTORY, "Inventory adjustment"),
]


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    type = Column(String, nullable=False)                       # cash_sale | order | invoice | payment | inventory_adjustment
    subtype = Column(String)                                    # inventory kind: restock/waste/…
    number = Column(String, unique=True)                        # ORD-/INV- (sales: none)
    status = Column(String, nullable=False, default="paid")     # see per-type notes below
    occurred_at = Column(DateTime(timezone=True), default=now_utc)  # sold_at / issue_date
    created_at = Column(DateTime(timezone=True), default=now_utc)
    decided_at = Column(DateTime(timezone=True))                # order confirm/reject time

    staff_id = Column(Integer, ForeignKey("entity.id"))
    customer_id = Column(Integer, ForeignKey("entity.id"))
    customer_name = Column(String)                              # order walk-in / invoice bill-to
    customer_phone = Column(String)

    # payment / proof (cash_sale, order)
    payment_method = Column(String)
    is_credit = Column(Boolean, nullable=False, default=False)  # unpaid retail sale
    proof = Column(LargeBinary)
    proof_mime = Column(String)
    amount_snapshot = Column(Numeric(10, 2))                    # order total snapshot
    note = Column(Text)

    # retail discount (cash_sale)
    pricing_group_id = Column(Integer, ForeignKey("pricing_groups.id", ondelete="SET NULL"))
    discount_person_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    discounted_qty = Column(Integer, nullable=False, default=0)

    # order self-checkout OCR checks + link to the sale it became
    check_amount_ok = Column(Boolean)
    check_detected_amount = Column(Numeric(10, 2))
    check_date_ok = Column(Boolean)
    check_detected_date = Column(String)
    check_note = Column(Text)
    converted_id = Column(Integer, ForeignKey("transactions.id", ondelete="SET NULL"))

    # a payment applies to its parent transaction (e.g. an invoice); null = standalone
    parent_id = Column(Integer, ForeignKey("transactions.id", ondelete="SET NULL"))

    # invoice fields
    bill_to_type = Column(String)                              # coach | customer | other
    coach_id = Column(Integer, ForeignKey("entity.id"))
    issue_date = Column(Date)
    due_date = Column(Date)
    period = Column(String)
    is_void = Column(Boolean, nullable=False, default=False)

    staff = relationship("Staff", foreign_keys=[staff_id])
    discount_person = relationship("Staff", foreign_keys=[discount_person_id])
    coach = relationship("Staff", foreign_keys=[coach_id])
    customer = relationship("Staff", foreign_keys=[customer_id])
    items = relationship("TransactionItem", back_populates="transaction",
                         cascade="all, delete-orphan")
    # payments (and any child transactions) that apply to this one
    children = relationship("Transaction", foreign_keys=[parent_id],
                            backref=backref("parent", remote_side=[id]))

    @property
    def total(self):
        return sum(float(i.qty) * float(i.unit_price) for i in self.items)

    # ---- inventory_adjustment convenience (one line item) ----
    @property
    def movement_type(self):
        return self.subtype

    @property
    def quantity(self):
        return int(self.items[0].qty) if self.items else 0

    @property
    def unit_cost(self):
        return float(self.items[0].unit_price) if (self.items and self.items[0].unit_price is not None) else None

    @property
    def product(self):
        return self.items[0].product if self.items else None

    @property
    def paid(self):
        return sum(c.total for c in self.children
                   if c.type == TX_PAYMENT and not c.is_void)

    @property
    def balance(self):
        return self.total - self.paid

    @property
    def bill_to_name(self):
        return self.customer_name

    @property
    def ipayments(self):
        """Child payment transactions applied to this one (for invoice views)."""
        return [c for c in self.children if c.type == TX_PAYMENT and not c.is_void]

    @property
    def invoice_status(self):
        if self.is_void:
            return "void"
        if self.total > 0 and self.balance <= 0.005:
            return "paid"
        if self.paid > 0.005:
            return "partial"
        return "unpaid"


class TransactionItem(Base):
    __tablename__ = "transaction_items"
    id = Column(Integer, primary_key=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"))     # null for free-text invoice lines
    name = Column(String, nullable=False)                       # snapshot / invoice description
    qty = Column(Numeric(10, 2), nullable=False, default=1)
    unit_price = Column(Numeric(10, 2), nullable=False, default=0)
    cost_price = Column(Numeric(10, 2))

    transaction = relationship("Transaction", back_populates="items")
    product = relationship("Product")

    @property
    def amount(self):
        return float(self.qty or 0) * float(self.unit_price or 0)


class PaymentSetting(Base):
    """Singleton (id=1): bank details + payment QR + logo shown on the customer page."""
    __tablename__ = "company_info"
    id = Column(Integer, primary_key=True)
    bank_name = Column(String)
    account_name = Column(String)
    qr = Column(LargeBinary)
    qr_mime = Column(String)
    logo = Column(LargeBinary)                 # storefront logo (customer /order header)
    logo_mime = Column(String)
    waiver_key = Column(String)                # secret token embedded in the /waiver QR
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


PRICING_KINDS = [("employee", "Employee"), ("affiliate", "Affiliate")]


class PricingGroup(Base):
    """A named price level (e.g. Affiliate, Employee) that holds an explicit
    per-item price for some products.

    Base price = the product's normal selling price, used for anyone not on a
    level and for any item this level hasn't set an explicit price for. A level
    only overrides the items it has a `PricingGroupItem` row (with a price) for.

    The legacy columns (kind / discount_percent / round_up / daily_item_limit)
    are retained for backward compatibility but are no longer used.
    """
    __tablename__ = "pricing_groups"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False, default="employee")   # legacy, unused
    discount_percent = Column(Numeric(5, 2), nullable=False, default=0)  # legacy, unused
    round_up = Column(Boolean, nullable=False, default=False)   # legacy, unused
    daily_item_limit = Column(Integer)                          # legacy, unused
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    items = relationship("PricingGroupItem", cascade="all, delete-orphan", backref="group")

    def price_map(self):
        """{product_id: explicit price} for items this level overrides."""
        return {i.product_id: float(i.price) for i in self.items if i.price is not None}

    def eligible_ids(self):
        return {i.product_id for i in self.items if i.price is not None}

    def price_for(self, product):
        """This level's explicit price for the product, else its base price."""
        base = float(product.selling_price or 0)
        for i in self.items:
            if i.product_id == product.id and i.price is not None:
                return round(float(i.price), 2)
        return round(base, 2)


class PricingGroupItem(Base):
    __tablename__ = "pricing_group_items"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("pricing_groups.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    price = Column(Numeric(10, 2))                              # explicit price at this level


class Waiver(Base):
    """A signed liability waiver from the public /waiver page."""
    __tablename__ = "waivers"
    id = Column(Integer, primary_key=True)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    email = Column(String)
    phone = Column(String)
    referral = Column(String)                       # "how did you find us"
    emergency_name = Column(String)
    emergency_phone = Column(String)
    signature = Column(LargeBinary)                 # signature PNG bytes
    signature_mime = Column(String, default="image/png")
    ip = Column(String)                             # submitter IP (rate limiting)
    signed_at = Column(DateTime(timezone=True), default=now_utc)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    # emergency_name / emergency_phone columns retained (unused) — see WaiverToken


HYROX_STATIONS = ["Run", "Ski", "Sled Push", "Sled Pull", "Burpee Broad Jump",
                  "Row", "Farmer Carry", "Lunges", "Wallballs"]

# Per-station target shown on the coach app.
HYROX_STATION_DETAIL = {
    "Run": "2 laps each",
    "Ski": "200m each",
    "Sled Push": "12.5m each",
    "Sled Pull": "12.5m each",
    "Burpee Broad Jump": "10m",
    "Row": "200m each",
    "Farmer Carry": "10m each",
    "Lunges": "10m each",
    "Wallballs": "5 reps each",
}


class HyroxGroup(Base):
    """A team in the HYROX relay. Progress is stored as `splits` (CSV of completed
    station times in seconds, in order) plus `running_since` (set while the current
    station is being timed). Completed count = len(splits); current station index =
    that count; total time = sum(splits) + current elapsed."""
    __tablename__ = "hyrox_groups"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    tag = Column(String, nullable=False)                    # 'A' | 'B'
    emblem = Column(String)                                 # emoji
    color = Column(String)
    sort = Column(Integer, nullable=False, default=0)
    coach = Column(String)                                  # coach name shown on the board
    splits = Column(Text, nullable=False, default="")       # CSV secs, one per done station
    running_since = Column(DateTime(timezone=True))         # set while current station times
    start_at = Column(DateTime(timezone=True))             # fixed gun start (schedule)
    finished_at = Column(DateTime(timezone=True))          # stamped when Wallballs finished
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


# Coach per (team, tag). Backfilled onto existing rows on startup if unset.
HYROX_COACH_DEFAULTS = {
    ("Eagles", "A"): "AR", ("Eagles", "B"): "AR",
    ("Foxes", "A"): "Jan", ("Foxes", "B"): "Jan",
    ("Pulag Pythons", "A"): "Van", ("Pulag Pythons", "B"): "JC",
    ("Logan Leopards", "A"): "Melvin", ("Logan Leopards", "B"): "Corbett",
}

HYROX_GROUP_DEFAULTS = [
    dict(name="Eagles", tag="A", emblem="🦅", color="#c99a3f", sort=0, coach="AR"),
    dict(name="Eagles", tag="B", emblem="🦅", color="#c99a3f", sort=1, coach="AR"),
    dict(name="Foxes", tag="A", emblem="🦊", color="#e8703a", sort=2, coach="Jan"),
    dict(name="Foxes", tag="B", emblem="🦊", color="#e8703a", sort=3, coach="Jan"),
    dict(name="Pulag Pythons", tag="A", emblem="🐍", color="#18BE7C", sort=4, coach="Van"),
    dict(name="Pulag Pythons", tag="B", emblem="🐍", color="#18BE7C", sort=5, coach="JC"),
    dict(name="Logan Leopards", tag="A", emblem="🐆", color="#e0a021", sort=6, coach="Melvin"),
    dict(name="Logan Leopards", tag="B", emblem="🐆", color="#e0a021", sort=7, coach="Corbett"),
]


class WaiverToken(Base):
    """A one-time token issued when someone opens /waiver via the QR. Consumed on
    submit (or expires after a while), so an opened waiver link can't be reused."""
    __tablename__ = "waiver_tokens"
    token = Column(String, primary_key=True)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=now_utc)


class KioskPlan(Base):
    """A priced option offered in the public kiosk flows (behind the QR hub):
    a `daypass` (Walk-in) or a `membership` plan (Sign up). Admin-editable at
    /admin/kiosk so the owner sets real prices without a redeploy."""
    __tablename__ = "kiosk_plans"
    id = Column(Integer, primary_key=True)
    kind = Column(String, nullable=False)                   # 'walkin' | 'membership' | 'daypass'(legacy)
    name = Column(String, nullable=False)                   # e.g. "Open Gym", "Monthly"
    subtitle = Column(String)                               # e.g. "Full-day access"
    price = Column(Numeric(10, 2), nullable=False, default=0)
    sort = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    # --- walk-in activities only ---
    activity = Column(String)                               # 'open_gym' | 'private' | 'hyrox'
    coached = Column(Boolean)                               # HYROX variant: with a coach?
    doubles = Column(Boolean)                               # HYROX variant: doubles?
    created_at = Column(DateTime(timezone=True), default=now_utc)
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


KIOSK_DAYPASS = "daypass"          # legacy (superseded by walk-in activities)
KIOSK_MEMBERSHIP = "membership"
KIOSK_WALKIN = "walkin"

# Membership plans seeded on first startup (placeholders — owner edits at /admin/kiosk).
KIOSK_PLAN_DEFAULTS = [
    dict(kind=KIOSK_MEMBERSHIP, name="Monthly", subtitle="Unlimited access · 30 days",
         price=1000, sort=0),
    dict(kind=KIOSK_MEMBERSHIP, name="Quarterly", subtitle="3 months · save 10%",
         price=2700, sort=1),
    dict(kind=KIOSK_MEMBERSHIP, name="Annual", subtitle="12 months · best value",
         price=9600, sort=2),
]

# Walk-in activities seeded on first startup. Open Gym + Private Coaching prices
# are confirmed; the four HYROX rates are placeholders (₱0) the owner must set.
KIOSK_WALKIN_DEFAULTS = [
    dict(kind=KIOSK_WALKIN, activity="open_gym", name="Open Gym",
         subtitle="Full-day access", price=1000, sort=0),
    dict(kind=KIOSK_WALKIN, activity="private", name="Private Coaching",
         subtitle="1-on-1 session", price=2000, sort=1),
    dict(kind=KIOSK_WALKIN, activity="hyrox", name="Self-paced · Solo",
         coached=False, doubles=False, price=1000, sort=10),
    dict(kind=KIOSK_WALKIN, activity="hyrox", name="Self-paced · Doubles",
         coached=False, doubles=True, price=1000, sort=11),
    dict(kind=KIOSK_WALKIN, activity="hyrox", name="With a coach · Solo",
         coached=True, doubles=False, price=3000, sort=12),
    dict(kind=KIOSK_WALKIN, activity="hyrox", name="With a coach · Doubles",
         coached=True, doubles=True, price=2500, sort=13),
]

# HYROX rates by (coached, doubles) — used to backfill rows first seeded at ₱0.
KIOSK_HYROX_RATES = {(False, False): 1000, (False, True): 1000,
                     (True, False): 3000, (True, True): 2500}


# Legacy `discount_codes` (per-person codes) were folded into the Staff entity
# table; the table is migrated then dropped at startup. No ORM model remains.


# Legacy `payments` (customer balance payments) folded into transactions; dropped at startup.
