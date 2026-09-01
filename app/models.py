import math
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

#: The gym runs in one place, so every time a person types or reads is that
#: place's wall clock. Stored values are always real UTC instants; this is the
#: lens they are written and read through.
APP_TZ = os.environ.get("APP_TZ", "Asia/Manila")


def gym_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(APP_TZ)
    except Exception:
        return timezone(timedelta(hours=8))   # Manila has no daylight saving


def to_local(dt):
    """A stored instant, as the clock on the gym wall would read it."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(gym_tz())


def from_local(dt):
    """A time somebody typed, read as gym time, returned as a real instant."""
    if dt is None:
        return None
    return dt.replace(tzinfo=gym_tz()).astimezone(timezone.utc)

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Integer,
    LargeBinary, Numeric, String, Text, UniqueConstraint,
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
#
# `event_door` is deliberately separate from `manage_hyrox`. Working the door
# is a different job from running the event: somebody checking people in needs
# the list and the scanner and nothing else, and should not be able to email
# every participant or delete a timetable. `manage_hyrox` implies it (see
# can_door), so nobody who already ran events loses anything.
ACCESS_DEFS = [
    ("view_reports", "View reports"),
    ("view_stock", "View stock levels"),
    ("view_costs", "See costs & profit margins"),
    ("event_door", "Event check-in (door only)"),
    ("coach_race", "Coaching (race app)"),
]

# Access to the newer admin management areas. One toggle grants full access to
# that area (view + manage). Admin roles get all of these automatically.
ADMIN_AREA_DEFS = [
    ("manage_kiosk", "Kiosk & plans"),
    ("view_waivers", "Signed waivers"),
    ("manage_hyrox", "HYROX event"),
    ("manage_commissions", "Coach commissions"),
]

MODULE_KEYS = [f"{m}.{a}" for m, _ in MODULES for a, _ in ACTIONS]
ACCESS_KEYS = [k for k, _ in ACCESS_DEFS] + [k for k, _ in ADMIN_AREA_DEFS]
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


def can_coach(staff):
    """May this person work the race app?

    Deliberately wide: on the morning of a race the failure that actually
    costs you is a coach who cannot get into the app. The narrow permission
    exists so a coach can be given this and nothing else — not so that
    somebody already trusted with the event area gets locked out of it.
    """
    return can_any(staff, ("coach_race", "manage_hyrox"))


def can_door(staff):
    """May this person check participants in?

    Either the narrow door permission, or the full event area — running the
    event has always included standing at its door, and splitting the two
    should not take anything away from whoever already had it.
    """
    return can_any(staff, ("event_door", "manage_hyrox"))


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
    # --- customer profile (editable on the customer form; seeded from waivers) ---
    first_name = Column(String)
    last_name = Column(String)
    email = Column(String)
    emergency_name = Column(String)
    emergency_phone = Column(String)
    notes = Column(Text)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

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
    #: Where "Write a review" sends people. One link for the whole gym — it is
    #: the gym being reviewed, not the class — so it is typed here once rather
    #: than pasted onto every event and mistyped on one of them.
    review_url = Column(String)
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
    customer_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))  # linked customer
    signed_at = Column(DateTime(timezone=True), default=now_utc)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    # emergency_name / emergency_phone columns retained (unused) — see WaiverToken

    @property
    def full_name(self):
        return ("%s %s" % (self.first_name or "", self.last_name or "")).strip()


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


# ===================== Coach commissions =====================
# Deliberately NOT part of the `transactions` table: commission payouts move
# money the other way (AWAKEN -> coach) and mixing them in would make every
# query that sums transactions without filtering by type overstate revenue.

COMMISSION_FLAT = "flat"
COMMISSION_PERCENT = "percent"
COMMISSION_RATE_TYPES = [(COMMISSION_FLAT, "Fixed amount"),
                         (COMMISSION_PERCENT, "Percent of revenue")]

#: The statuses a Rezerv export can carry, in the order the report shows them.
BOOKING_STATUSES = ["Completed", "Cancelled", "Late cancelled", "No show", "Booked"]

IMPORT_REPLACE = "replace"
IMPORT_MERGE = "merge"

RUN_DRAFT = "draft"
RUN_FINALIZED = "finalized"
RUN_SUPERSEDED = "superseded"

# Default rules, seeded once on first startup. Rezerv writes "Rick F" for Ric,
# so the export spelling is stored alongside the coach.
COMMISSION_RATE_DEFAULTS = [
    dict(coach="Anjo", staff_raw="Anjo R", rate_type=COMMISSION_FLAT, rate_value=750,
         overrides=[("Drop-In", COMMISSION_PERCENT, 0.50),
                    ("Awaken Force", COMMISSION_PERCENT, 0.50)]),
    dict(coach="JC", staff_raw="JC S", rate_type=COMMISSION_FLAT, rate_value=750,
         overrides=[("Drop-In", COMMISSION_PERCENT, 0.50),
                    ("Awaken Force", COMMISSION_PERCENT, 0.50)]),
    dict(coach="Ric", staff_raw="Rick F", rate_type=COMMISSION_PERCENT, rate_value=0.50),
    dict(coach="Julio", staff_raw="Julio D", rate_type=COMMISSION_PERCENT, rate_value=0.70),
    dict(coach="AR", staff_raw="AR M", rate_type=COMMISSION_PERCENT, rate_value=0.40),
    dict(coach="Joseph", staff_raw="Joseph J", rate_type=COMMISSION_PERCENT, rate_value=0.40),
    dict(coach="Laurent", staff_raw="Laurent J", rate_type=COMMISSION_PERCENT, rate_value=0.40),
]

COMMISSION_DELEGATOR_DEFAULTS = [
    dict(name="Gab Rosario", codes="GR", rate=1000, cost=640),
    dict(name="Culver Padilla", codes="KP,CP", rate=1000, cost=640),
]

# key -> (default value, label, help)
COMMISSION_SETTING_DEFAULTS = {
    "hyrox_walkin_deduction": ("1000", "Hyrox walk-in deduction",
                               "Deducted from 'Hyrox Simulation (With Coach)' walk-ins."),
    "awaken_force_revenue": ("1200", "Awaken Force revenue",
                             "Per-session revenue; the export shows the package total."),
    "backfill_scope": ("credit_and_free", "Backfill scope",
                       "Which ₱0 session rows get the old per-session rate. "
                       "credit_and_free = prepaid AND comped sessions are backfilled. "
                       "credit_only = comped (Free) sessions keep ₱0 revenue. "
                       "Delegated sessions and memberships are never backfilled."),
    "default_delegator": ("KP", "Default delegator",
                          "Applied to a bare 'Delegation' variant that names no code."),
    "paid_statuses": ("completed,late cancelled", "Statuses that pay automatically",
                      "Comma-separated booking statuses that earn commission without "
                      "review. A late cancel is included because the client was still "
                      "charged and the coach still lost the hour. Every other status "
                      "earns nothing until approved on the coach's page. Changing this "
                      "affects the next import, and any draft you Recalculate — "
                      "finalized runs never move."),
}


class CommissionCoachRate(Base):
    """How one coach is paid. Editable; past runs are unaffected because the
    rate that fired is snapshotted onto every booking row."""
    __tablename__ = "commission_coach_rates"
    id = Column(Integer, primary_key=True)
    coach = Column(String, nullable=False)                  # display name
    staff_raw = Column(String, nullable=False)              # Rezerv spelling
    coach_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    # Affiliate or employee, for reporting. Held here as well as on the person
    # record because a coach can be paid commission long before anyone links
    # them to a record in Relationships — and until they are linked there is
    # nowhere else for the tag to live.
    coach_type = Column(String, nullable=False, default="")
    rate_type = Column(String, nullable=False, default=COMMISSION_PERCENT)
    rate_value = Column(Numeric(10, 4), nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)

    entity = relationship("Staff", foreign_keys=[coach_id])
    overrides = relationship(
        "CommissionCoachOverride", back_populates="rate",
        cascade="all, delete-orphan",
        order_by="CommissionCoachOverride.plan")

    @property
    def live_overrides(self):
        return [o for o in self.overrides if o.is_active]

    def plan_list(self):
        """Lowercased plans this coach is paid differently for."""
        return [o.plan.strip().lower() for o in self.live_overrides if o.plan]

    @property
    def kind(self) -> str:
        """'affiliate', 'employee', or '' — the tag here, else the person's.

        Anything else the person record might be — customer, member, supplier —
        is not a coach type and reads as untagged. Passing it through produced
        a chip with no label and no colour.
        """
        for value in (self.coach_type, self.entity.person_type if self.entity else ""):
            if (value or "") in ("affiliate", "employee"):
                return value
        return ""


# What a coach is to the business. 'employee' is labelled Employee/Coach
# because internally they are both — on payroll, and taking sessions.
COACH_TYPES = [("", "— untagged —"), ("employee", "Employee/Coach"),
               ("affiliate", "Affiliate")]
COACH_TYPE_LABELS = {"employee": "Employee/Coach", "affiliate": "Affiliate",
                     "": "Untagged"}


class CommissionCoachOverride(Base):
    """One plan that pays this coach differently from their default.

    A row per plan rather than a comma-separated column, so each plan carries
    its own basis and rate — Drop-In at 50% and Awaken Force at 60% is
    expressible, which the old single-override column could not do.
    """
    __tablename__ = "commission_coach_overrides"
    id = Column(Integer, primary_key=True)
    rate_id = Column(Integer, ForeignKey("commission_coach_rates.id",
                                         ondelete="CASCADE"), nullable=False)
    plan = Column(String, nullable=False)                   # as displayed
    rate_type = Column(String, nullable=False, default=COMMISSION_PERCENT)
    rate_value = Column(Numeric(10, 4), nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)

    rate = relationship("CommissionCoachRate", back_populates="overrides")

    # Deferred to commit time: editing two rows can swap their plans, which is
    # briefly a duplicate mid-statement even though the end state is valid.
    __table_args__ = (UniqueConstraint("rate_id", "plan",
                                       name="uq_coach_override_plan",
                                       deferrable=True, initially="DEFERRED"),)


class CommissionDelegator(Base):
    """Someone who brings their own clients and delegates sessions to a coach.

    `rate` is charged to them (AWAKEN income); `cost` is paid to the covering
    coach (AWAKEN expense). The difference is the delegation margin.
    """
    __tablename__ = "commission_delegators"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    entity_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    codes = Column(String, nullable=False, default="")      # "KP,CP"
    rate = Column(Numeric(10, 2), nullable=False, default=0)
    cost = Column(Numeric(10, 2), nullable=False, default=0)
    #: Charged per hour when a delegated session runs long. Billed to the
    #: delegator only — the covering coach's payout does not move, so every
    #: peso of overtime is margin. One rate, used as the default when somebody
    #: logs hours; an unusual session can still carry its own rate.
    ot_rate = Column(Numeric(10, 2), nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)

    entity = relationship("Staff", foreign_keys=[entity_id])

    def code_list(self):
        return [c.strip().upper() for c in (self.codes or "").split(",") if c.strip()]

    @property
    def margin(self):
        return float(self.rate or 0) - float(self.cost or 0)


class CommissionSessionRate(Base):
    """Per-session rate for a pricing plan, used to backfill a ₱0 export row.

    Dated rather than a flat lookup: rates change, and a booking must be valued
    with the rate that applied on the day it happened, not today's. Leaving
    effective_from empty means "as far back as there is data".
    """
    __tablename__ = "commission_session_rates"
    id = Column(Integer, primary_key=True)
    program = Column(String)                       # "Private Coaching" | "Awaken Force"
    plan = Column(String, nullable=False)          # matches "Pricing plan used"
    sessions = Column(Integer)                     # 1, 8, 12, 24, 36
    rate = Column(Numeric(10, 2), nullable=False, default=0)
    package_total = Column(Numeric(10, 2))         # what the export bills for the pack
    effective_from = Column(Date)                  # null = no lower bound
    effective_to = Column(Date)                    # null = still current
    is_active = Column(Boolean, nullable=False, default=True)
    note = Column(String)

    def covers(self, on):
        if on is None:
            return True
        if self.effective_from and on < self.effective_from:
            return False
        if self.effective_to and on > self.effective_to:
            return False
        return True


#: Seeded once, from the rates the commission spec has always used.
PT = "Private Coaching"
AWAKEN_FORCE = "Awaken Force"

COMMISSION_SESSION_RATE_DEFAULTS = [
    dict(program=PT, plan="1 Session", sessions=1, rate=1900),
    dict(program=PT, plan="8 Sessions", sessions=8, rate=1800),
    dict(program=PT, plan="12 Sessions", sessions=12, rate=1700),
    dict(program=PT, plan="24 Sessions", sessions=24, rate=1600),
    dict(program=PT, plan="36 Sessions", sessions=36, rate=1500),
    # Awaken Force has its own card. Every AF row exports identically
    # (credit 1, the package total as revenue), so the package_total is what
    # tells one package from another.
    dict(program=AWAKEN_FORCE, plan="Awaken Force", sessions=1, rate=1500,
         package_total=1500),
    dict(program=AWAKEN_FORCE, plan="Awaken Force", sessions=8, rate=1200,
         package_total=9600),
]


class CommissionSetting(Base):
    __tablename__ = "commission_settings"
    id = Column(Integer, primary_key=True)
    key = Column(String, nullable=False, unique=True)
    value = Column(Text, nullable=False, default="")


class CommissionRun(Base):
    __tablename__ = "commission_runs"
    id = Column(Integer, primary_key=True)
    period = Column(String, nullable=False)                 # "2026-06"
    period_label = Column(String)                           # "June 2026"
    period_start = Column(Date)
    period_end = Column(Date)
    source_filename = Column(String)
    source_sha256 = Column(String)
    status = Column(String, nullable=False, default=RUN_DRAFT)
    parsed_count = Column(Integer, nullable=False, default=0)
    kept_count = Column(Integer, nullable=False, default=0)
    dropped_count = Column(Integer, nullable=False, default=0)
    uploaded_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    uploaded_at = Column(DateTime(timezone=True), default=now_utc)
    finalized_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    finalized_at = Column(DateTime(timezone=True))
    # Receipt from the last import into this run: what was added, what was
    # skipped as a duplicate, what was filtered out by status.
    last_import_note = Column(Text)

    uploaded_by = relationship("Staff", foreign_keys=[uploaded_by_id])
    finalized_by = relationship("Staff", foreign_keys=[finalized_by_id])
    bookings = relationship("CommissionBooking", back_populates="run",
                            cascade="all, delete-orphan")
    payouts = relationship("CommissionPayout", cascade="all, delete-orphan")
    charges = relationship("CommissionCharge", cascade="all, delete-orphan")


class CommissionBooking(Base):
    """One row per booking in the export, with the rule and rate that fired."""
    __tablename__ = "commission_bookings"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("commission_runs.id", ondelete="CASCADE"),
                    nullable=False)
    # --- as exported ---
    booking_ref = Column(String)
    customer = Column(String)
    appointment_date = Column(Date)
    appointment_name = Column(String)
    variant = Column(String)
    staff_raw = Column(String)
    booking_status = Column(String)
    pricing_plan = Column(String)
    payment_method = Column(String)
    revenue_raw = Column(Numeric(10, 2), default=0)
    # --- resolved ---
    coach = Column(String)
    coach_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    delegator_id = Column(Integer, ForeignKey("commission_delegators.id",
                                              ondelete="SET NULL"))
    delegator_assumed = Column(Boolean, nullable=False, default=False)
    # --- normalized ---
    revenue = Column(Numeric(10, 2), default=0)
    adjustment = Column(String)
    adjustment_note = Column(String)
    # --- computed (snapshot: never recalculated) ---
    rule = Column(String)
    rate_type = Column(String)
    rate_value = Column(Numeric(10, 4))
    # A rate typed by hand on this row. Survives Recalculate — otherwise the
    # correction you just made would be silently undone by the next config edit.
    rate_manual = Column(Boolean, nullable=False, default=False)
    rate_manual_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    commission = Column(Numeric(10, 2))
    delegation_charge = Column(Numeric(10, 2))
    # --- overtime, typed by hand ---------------------------------------
    # The export carries no duration, so these are the one part of a booking
    # that a person enters rather than the import deriving. That is why
    # Recalculate leaves them alone: re-running the rates must never silently
    # erase hours somebody sat down and typed.
    #
    # `ot_rate` is snapshotted per row like every other rate here, so changing
    # a delegator's rate tomorrow cannot restate a session billed today.
    ot_hours = Column(Numeric(6, 2))
    ot_rate = Column(Numeric(10, 2))
    ot_charge = Column(Numeric(10, 2))
    ot_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    ot_at = Column(DateTime(timezone=True))
    # Whether this row's booking status pays without anyone approving it,
    # decided against the rules in force when the run was calculated. Stored
    # rather than re-derived so that changing which statuses pay cannot
    # restate a run that has already been read, signed off or paid.
    pays_by_status = Column(Boolean, nullable=False, default=False)
    # --- excluded rows are kept, flagged, and shown ---
    dropped_reason = Column(String)
    # --- reviewer approval for non-Completed bookings ---
    approved = Column(Boolean, nullable=False, default=False)
    approved_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    approved_at = Column(DateTime(timezone=True))
    # --- struck out by hand ---
    # A session the export says happened but which did not: a double booking,
    # a client who was billed twice, a row typed against the wrong coach. The
    # row is kept and shown struck through rather than deleted, because a
    # session that quietly vanishes between one reading of a statement and the
    # next is how a coach loses trust in the figures.
    voided = Column(Boolean, nullable=False, default=False)
    void_reason = Column(String)
    voided_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    voided_at = Column(DateTime(timezone=True))

    run = relationship("CommissionRun", back_populates="bookings")
    delegator = relationship("CommissionDelegator")
    approved_by = relationship("Staff", foreign_keys=[approved_by_id])
    voided_by = relationship("Staff", foreign_keys=[voided_by_id])
    ot_by = relationship("Staff", foreign_keys=[ot_by_id])

    @property
    def overtime(self):
        """What this row bills in overtime — zero unless it actually counts.

        A struck-out or unapproved session bills nothing, and that has to be
        true here as well as on the session fee. Asking one property rather
        than reading the column means the calendar, the totals, the invoice
        and the run all drop a voided row's overtime together.
        """
        if not self.is_commissionable or not self.delegator_id:
            return Decimal("0")
        return Decimal(str(self.ot_charge or 0))

    @property
    def is_commissionable(self):
        """Paid by its status, or approved by a reviewer — unless struck out.

        Voiding wins over both. Everything that counts sessions or sums money
        already asks this question, so one flag here reaches the coach's page,
        the run totals, the delegator's margin, the statement and the payout
        without any of them having to know the concept exists.
        """
        if self.voided:
            return False
        return bool(self.pays_by_status) or bool(self.approved)


class CommissionSignoff(Base):
    """One coach's figures on one run, confirmed correct and cleared to pay.

    Separate from the per-booking `approved` flag: that decides whether a row
    counts, this says the whole sheet has been checked. Finalizing pays only
    coaches that carry one, so nobody is paid on figures no one has read.
    """
    __tablename__ = "commission_signoffs"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("commission_runs.id", ondelete="CASCADE"),
                    nullable=False)
    coach = Column(String, nullable=False)
    sessions = Column(Integer)                     # what was signed off, for audit
    commission = Column(Numeric(10, 2))
    approved_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    approved_at = Column(DateTime(timezone=True))

    run = relationship("CommissionRun")
    approved_by = relationship("Staff", foreign_keys=[approved_by_id])

    __table_args__ = (UniqueConstraint("run_id", "coach",
                                       name="uq_commission_signoff_coach"),)


#: How long a coach's statement link stays open, in days.
STATEMENT_LINK_DAYS = 5

#: How long a delegator's link stays open. Shorter than a coach's because it
#: carries what somebody owes rather than what they are owed — a forwarded
#: invoice is a different kind of leak from a forwarded payslip, and a week is
#: long enough to read a month's figures and query them.
DELEGATOR_LINK_DAYS = 7


class CommissionDelegatorLink(Base):
    """A private, expiring URL showing one delegator their own month.

    Modelled on CommissionStatementLink and for the same reason: a delegator
    reads this on a phone, and an account with a password is an account nobody
    uses. It reaches exactly one delegator's one period.

    What it must never carry is the cost paid to the covering coach — and by
    subtraction, the margin. That is not enforced here; it is enforced by the
    page being built from a function that never computes those numbers. A flag
    on this row would be one `if` away from being wrong.
    """
    __tablename__ = "commission_delegator_links"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("commission_runs.id", ondelete="CASCADE"),
                    nullable=False)
    delegator_id = Column(Integer, ForeignKey("commission_delegators.id",
                                              ondelete="CASCADE"), nullable=False)
    token = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    created_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    expires_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    sent_to = Column(String)
    sent_at = Column(DateTime(timezone=True))
    first_opened_at = Column(DateTime(timezone=True))
    last_opened_at = Column(DateTime(timezone=True))
    opens = Column(Integer, nullable=False, default=0)

    run = relationship("CommissionRun")
    delegator = relationship("CommissionDelegator")
    created_by = relationship("Staff", foreign_keys=[created_by_id])

    # Same as the coach links: no unique constraint on (run_id, delegator_id).
    # Replacing a link revokes the old row rather than deleting it, so somebody
    # opening yesterday's email is told a newer one was sent.

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at < now_utc())

    @property
    def is_live(self):
        return not self.revoked_at and not self.is_expired

    @property
    def state(self):
        if self.revoked_at:
            return "revoked"
        if self.is_expired:
            return "expired"
        if self.opens:
            return "opened"
        if self.sent_at:
            return "sent"
        return "ready"


class CommissionComment(Base):
    """One message in the conversation about a coach's period.

    Kept per coach per run so a question stays attached to the figures it is
    about: finalizing July does not bury the thread, and a query about July
    never lands next to one about October. Nothing here changes a number — the
    thread sits alongside the money, it does not move it.
    """
    __tablename__ = "commission_comments"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("commission_runs.id", ondelete="CASCADE"),
                    nullable=False)
    coach = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    # Who wrote it. A coach writes through their statement link and has no
    # login, so they are identified by the thread rather than by an account.
    from_coach = Column(Boolean, nullable=False, default=False)
    author_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    # The session being asked about, when the coach quoted one. SET NULL rather
    # than CASCADE: deleting a hand-added row should not delete the question
    # someone asked about it.
    booking_id = Column(Integer, ForeignKey("commission_bookings.id",
                                            ondelete="SET NULL"))
    created_at = Column(DateTime(timezone=True), default=now_utc)
    # When the other side read it — the coach opening their link marks yours,
    # opening the coach page marks theirs.
    seen_at = Column(DateTime(timezone=True))

    run = relationship("CommissionRun")
    author = relationship("Staff", foreign_keys=[author_id])
    booking = relationship("CommissionBooking", foreign_keys=[booking_id])


#: A comment longer than this is a document, not a question.
COMMENT_MAX = 2000


class CommissionStatementLink(Base):
    """A private, expiring URL that shows one coach their own statement.

    A link rather than a login: coaches read these on a phone between clients,
    and an account they have to remember a password for is an account they
    won't use. The cost is that whoever holds the URL can read that statement,
    so the token is long and random, it expires, it can be revoked, and it
    exposes exactly one coach's one period — no other coach, no way into the
    app.
    """
    __tablename__ = "commission_statement_links"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("commission_runs.id", ondelete="CASCADE"),
                    nullable=False)
    coach = Column(String, nullable=False)
    token = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    created_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    expires_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    # delivery + reading, so "did they get it" has an answer
    sent_to = Column(String)
    sent_at = Column(DateTime(timezone=True))
    first_opened_at = Column(DateTime(timezone=True))
    last_opened_at = Column(DateTime(timezone=True))
    opens = Column(Integer, nullable=False, default=0)

    run = relationship("CommissionRun")
    created_by = relationship("Staff", foreign_keys=[created_by_id])

    # No unique constraint on (run_id, coach): replacing a link keeps the old
    # row, revoked. Otherwise a coach clicking yesterday's emailed link gets a
    # bare "not found" instead of "a newer one was sent".

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at < now_utc())

    @property
    def is_live(self):
        return not self.revoked_at and not self.is_expired

    @property
    def state(self):
        if self.revoked_at:
            return "revoked"
        if self.is_expired:
            return "expired"
        if self.opens:
            return "opened"
        if self.sent_at:
            return "sent"
        return "ready"


class CommissionPayout(Base):
    """What AWAKEN owes one coach for one run."""
    __tablename__ = "commission_payouts"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("commission_runs.id", ondelete="CASCADE"),
                    nullable=False)
    number = Column(String, unique=True)                    # COM-0001
    coach = Column(String)
    coach_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    period_label = Column(String)
    status = Column(String, nullable=False, default="unpaid")
    sessions = Column(Integer, nullable=False, default=0)
    commission_total = Column(Numeric(10, 2), default=0)    # non-delegated
    delegation_total = Column(Numeric(10, 2), default=0)    # delegated
    total = Column(Numeric(10, 2), default=0)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    paid_at = Column(DateTime(timezone=True))

    entity = relationship("Staff", foreign_keys=[coach_id])
    lines = relationship("CommissionPayoutLine", cascade="all, delete-orphan")


class CommissionPayoutLine(Base):
    __tablename__ = "commission_payout_lines"
    id = Column(Integer, primary_key=True)
    payout_id = Column(Integer, ForeignKey("commission_payouts.id", ondelete="CASCADE"),
                       nullable=False)
    booking_id = Column(Integer, ForeignKey("commission_bookings.id", ondelete="SET NULL"))
    booking_ref = Column(String)
    occurred_on = Column(Date)
    description = Column(String)
    basis = Column(String)                                  # "70% of ₱1,700.00"
    amount = Column(Numeric(10, 2), default=0)

    booking = relationship("CommissionBooking")


class CommissionCharge(Base):
    """What a delegator owes AWAKEN for one run.

    `coach_cost` is what we paid out on these sessions. It is ours, not theirs:
    it exists for the margin figure on the internal screen and must not reach
    the delegator's copy — not the PDF, not the statement. See
    ``commission_invoice_pdf``, which is built from the lines alone.
    """
    __tablename__ = "commission_charges"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("commission_runs.id", ondelete="CASCADE"),
                    nullable=False)
    number = Column(String, unique=True)                    # DEL-0001
    delegator_id = Column(Integer, ForeignKey("commission_delegators.id",
                                              ondelete="SET NULL"))
    delegator_name = Column(String)
    period_label = Column(String)
    #: The calendar month billed. Held on the invoice itself rather than
    #: reached through the run, so a statement can order and age invoices
    #: without a join — and so the document still reads correctly if the run
    #: behind it is ever re-imported.
    period_start = Column(Date)
    period_end = Column(Date)
    status = Column(String, nullable=False, default="unpaid")
    sessions = Column(Integer, nullable=False, default=0)
    total = Column(Numeric(10, 2), default=0)               # charged to them
    coach_cost = Column(Numeric(10, 2), default=0)          # paid out on their sessions
    note = Column(Text)                                     # shown on the invoice
    created_at = Column(DateTime(timezone=True), default=now_utc)
    issued_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    paid_at = Column(DateTime(timezone=True))
    voided_at = Column(DateTime(timezone=True))
    voided_reason = Column(String)

    delegator = relationship("CommissionDelegator")
    issued_by = relationship("Staff", foreign_keys=[issued_by_id])
    lines = relationship("CommissionChargeLine", cascade="all, delete-orphan")

    @property
    def margin(self):
        return float(self.total or 0) - float(self.coach_cost or 0)

    @property
    def is_void(self):
        return self.voided_at is not None

    @property
    def dates_label(self) -> str:
        """"1–15 Jun 2026", "28 Jun – 4 Jul 2026", or the month's own name.

        A range invoice is identified by its dates before anything else, so
        they are formatted once here rather than in each template that shows
        one. A range that happens to be a whole calendar month is named as that
        month: "1–31 Jul 2026" is the same thing said less clearly.
        """
        a, b = self.period_start, self.period_end
        if not a or not b:
            return self.period_label or ""
        if (a.day == 1 and b == date(a.year + (a.month == 12),
                                     (a.month % 12) + 1, 1) - timedelta(days=1)):
            return self.period_label or a.strftime("%B %Y")
        if a == b:
            return a.strftime("%-d %b %Y")
        if (a.year, a.month) == (b.year, b.month):
            return "%d–%s" % (a.day, b.strftime("%-d %b %Y"))
        if a.year == b.year:
            return "%s – %s" % (a.strftime("%-d %b"), b.strftime("%-d %b %Y"))
        return "%s – %s" % (a.strftime("%-d %b %Y"), b.strftime("%-d %b %Y"))


class DelegatorPayment(Base):
    """Money received from a delegator, against their account.

    Deliberately not against one invoice. Delegators pay round numbers and pay
    late: ₱50,000 lands against a ₱58,300 invoice, then ₱20,000 arrives
    covering the tail of one month and the head of the next. A payments table
    that insisted on an invoice would have to invent an answer to "which one",
    and would be wrong about half the time.

    So the account is what has a balance, and the invoices are settled from it
    oldest first — see ``account_ledger``. ``charge_id`` is provenance only, set
    when a payment obviously belongs to one document, and it is never what the
    arithmetic reads.
    """
    __tablename__ = "commission_delegator_payments"
    id = Column(Integer, primary_key=True)
    delegator_id = Column(Integer, ForeignKey("commission_delegators.id",
                                              ondelete="CASCADE"), nullable=False)
    paid_on = Column(Date, nullable=False)
    amount = Column(Numeric(10, 2), nullable=False, default=0)
    #: How it reads on the statement: "Payment received — June conduction".
    description = Column(String)
    method = Column(String)                 # bank transfer, cash, GCash
    reference = Column(String)              # their transfer reference
    #: Set only when the payment plainly settles one invoice. Descriptive.
    charge_id = Column(Integer, ForeignKey("commission_charges.id",
                                           ondelete="SET NULL"))
    recorded_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    created_at = Column(DateTime(timezone=True), default=now_utc)

    delegator = relationship("CommissionDelegator")
    recorded_by = relationship("Staff", foreign_keys=[recorded_by_id])

    @property
    def label(self) -> str:
        return self.description or "Payment received"


class CommissionChargeLine(Base):
    __tablename__ = "commission_charge_lines"
    id = Column(Integer, primary_key=True)
    charge_id = Column(Integer, ForeignKey("commission_charges.id", ondelete="CASCADE"),
                       nullable=False)
    booking_id = Column(Integer, ForeignKey("commission_bookings.id", ondelete="SET NULL"))
    booking_ref = Column(String)
    occurred_on = Column(Date)
    description = Column(String)
    coach = Column(String)
    amount = Column(Numeric(10, 2), default=0)
    #: "overtime" for the hours line that sits under a session, empty for the
    #: session itself. Named rather than matched on the description, because
    #: an invoice line that has to be identified by its own wording is a line
    #: that breaks the moment somebody improves the wording.
    kind = Column(String)


# ==========================================================================
# Sponsored events — a class someone else pays for, in exchange for reach
# ==========================================================================
#
# A sponsor covers a class so it can be free for the community. What they get
# back is a public post from each participant. That exchange only works if it
# is stated plainly and made easy to honour, so the whole model here is built
# around one private link per person that carries them from "you have a slot"
# through to "here is your discount code" without ever asking them to log in.

#: Statuses an event moves through.
EVENT_DRAFT = "draft"
EVENT_OPEN = "open"          # invitations out, slots being confirmed
EVENT_RUNNING = "running"    # the class has happened, Reel window open
EVENT_CLOSED = "closed"      # window shut, rewards settled
EVENT_STATUSES = [
    (EVENT_DRAFT, "Draft"), (EVENT_OPEN, "Open"),
    (EVENT_RUNNING, "Running"), (EVENT_CLOSED, "Closed"),
]

#: How a participant answered the invitation.
RSVP_NONE = ""
RSVP_YES = "yes"
RSVP_NO = "no"

#: Whether a submitted Reel carries the tags we asked for. Deliberately three
#: states: a Reel nobody has looked at is not the same as one that is fine.
TAGS_PENDING = "pending"
TAGS_OK = "ok"
TAGS_MISSING = "missing"
TAG_LABELS = {TAGS_PENDING: "To check", TAGS_OK: "All good",
              TAGS_MISSING: "Needs a word"}


#: How people get into an event.
EVENT_INVITE = "invite"
EVENT_OPEN = "open"
EVENT_MODES = [(EVENT_INVITE, "By invitation"), (EVENT_OPEN, "Open registration")]

#: Where a self-registration has got to.
#: ``draft`` — started, not yet paid for. A real row from the first Save, so a
#: trip out to the organiser's site can never cost somebody their answers.
PAY_DRAFT = "draft"
PAY_SUBMITTED = "submitted"
PAY_APPROVED = "approved"
PAY_RETURNED = "returned"
#: How long somebody gets to pay once you have sent the last call.
PAY_GRACE_HOURS = 24

PAY_LABELS = {
    PAY_DRAFT: "Not finished",
    PAY_SUBMITTED: "Waiting on us",
    PAY_APPROVED: "Approved",
    PAY_RETURNED: "Sent back",
}

#: Male / female, as asked for on the form.
SEXES = [("m", "Male"), ("f", "Female")]

#: The competitive category, crossing gender rather than replacing it: a field
#: of four - Advanced Men, Advanced Women, Open Men, Open Women. Both are asked
#: separately because they are separate questions, and a single list of four
#: would have to be re-cut every time either one of them changed.
#: The stored key stays "elite" while the label reads Advanced. Renaming the
#: key would mean rewriting every row on every past event, the ?cat= filter the
#: results page ships, and every saved link somebody has - all to change a
#: string nobody outside this file ever sees. The label is the only part that
#: was ever shown.
CATEGORIES = [("elite", "Advanced"), ("open", "Open")]
CATEGORY_LABELS = dict(CATEGORIES)
CATEGORY_KEYS = [k for k, _l in CATEGORIES]
#: Everybody is Open until somebody says otherwise. Nullable would put a fifth,
#: nameless column on the board on the morning this ships; the bigger field is
#: the safer guess, and moving a handful of people up to Advanced is a minute's
#: work where sorting an entire unassigned column is not.
CATEGORY_DEFAULT = "open"


def category_key(raw) -> str:
    """Whatever arrived, as one of the two. Anything else is Open."""
    v = (raw or "").strip().lower()
    return v if v in CATEGORY_KEYS else CATEGORY_DEFAULT


class Event(Base):
    """One sponsored class, with the terms of the sponsorship on it.

    Everything a participant is asked to do is configured here rather than
    written into a template, so a second sponsor with different terms is a new
    row and not a new page.
    """
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    #: Every public link this event has retired, newest first, newline
    #: separated. Kept rather than forgotten so an old link can say "this has
    #: expired" instead of "we can't find that" — the first is our decision
    #: and reads as one, the second reads like the person mistyped it.
    old_slugs = Column(Text)
    sponsor = Column(String)
    status = Column(String, nullable=False, default=EVENT_DRAFT)

    starts_at = Column(DateTime(timezone=True))
    #: Show the day but not the clock time. For a race the heats are drawn and
    #: published closer to the day, so a start time printed weeks ahead is a
    #: number somebody will plan around and then have to be corrected on. The
    #: date is still stored — the countdown and the ordering need it — this only
    #: decides whether anybody is shown it.
    time_tba = Column(Boolean, nullable=False, default=False)
    venue = Column(String)
    capacity = Column(Integer, nullable=False, default=30)
    bring = Column(String)                       # "Training gear, towel, water"
    perk = Column(String)                        # "Kenny Rogers meal on us"

    # --- what we ask for in return ---
    handles = Column(String)                     # "@awakenfitnessph @kennyrogersph"
    hashtag = Column(String)                     # "#FuelledByKennyRogers"
    # --- how people get in ---
    #: ``invite`` — you upload a list and each person gets a link.
    #: ``open`` — one public link, anyone can sign up and pay.
    #: One field, because everything else about an event is the same either way:
    #: the pass, the door, the Reel and the reward all work unchanged.
    mode = Column(String, nullable=False, default=EVENT_INVITE)
    #: Whether the public page is taking registrations right now. Deliberately a
    #: switch rather than a capacity check — a page that closes itself the moment
    #: the last slot goes will close on somebody mid-payment.
    signup_open = Column(Boolean, nullable=False, default=True)
    #: The advertised cut-off. A date is safe to close on automatically in a way
    #: that a full room is not: everybody can see it coming, and it does not
    #: arrive early because somebody else was quicker.
    signup_closes = Column(DateTime(timezone=True))
    #: Tombstone for the one-time correction of times that were stored as gym
    #: wall clock but labelled UTC. Kept so the fix can never run twice.
    tz_fixed = Column(Boolean, nullable=False, default=True)
    #: The step we cannot do for them: registering on the organiser's own site.
    external_url = Column(String)
    external_label = Column(String)          # "Register on HYROX"
    external_note = Column(Text)             # why, and why before paying
    #: The first two rates this system ever had, and now only the seed the
    #: event_rates rows were made from - see EventRate and the block in
    #: main.py that copies them across once. Nothing reads them any more;
    #: they stay because throwing away the only record of what an event
    #: charged before the migration would be throwing away history.
    tier_a_label = Column(String)            # "Members"
    tier_a_price = Column(Numeric(10, 2))
    tier_b_label = Column(String)            # "Non-members"
    tier_b_price = Column(Numeric(10, 2))
    #: How the rates are drawn on the sign-up: side-by-side tiles, one per
    #: line, or a dropdown. Tiles read best at two or three and get cramped
    #: on a phone at five, which is the whole reason this is a choice.
    rate_look = Column(String, nullable=False, default="tiles",
                       server_default="tiles")
    #: How to pay. A QR to scan, free-text bank details, or both.
    pay_qr = Column(LargeBinary)
    pay_qr_mime = Column(String)
    pay_qr_caption = Column(String)          # "GCash · 0917 555 0100"
    bank_details = Column(Text)
    pay_note = Column(String)                # "Put your name as the reference"
    #: The promise on the page, and in the email that follows it.
    review_hours = Column(Integer, nullable=False, default=24)

    #: Hours after the class within which a Reel has to be posted.
    reel_hours = Column(Integer, nullable=False, default=48)
    #: Reel submissions held shut, whatever the clock says. A class
    #: called off on the morning still has an end time in the diary, so
    #: the window would open on its own a few hours later and start
    #: asking people for a Reel of a class that never happened.
    reels_paused = Column(Boolean, nullable=False, default=False)

    #: Whether this event asks anybody for a Reel at all. NULL means "decide
    #: from the mode", which is the honest default: the Reel is the sponsor's
    #: side of a sponsored class - somebody comes free and posts about it - and
    #: an open event is one people paid to enter. Nobody who bought a race
    #: entry owes anybody a Reel.
    reels_on = Column(Boolean)

    #: How many minutes before a heat a coach may open it in the race app.
    #: Blank means the default below.
    #:
    #: A coach staring at a list of eighteen heats at eight in the morning is a
    #: coach who can open the wrong one, grab somebody else's athlete out of a
    #: heat three hours away and hold them there. Heats therefore stay shut
    #: until they are nearly due. They never shut again afterwards: heats run
    #: late, and a coach locked out of a race already on the floor has no way
    #: back to their own athlete.
    heat_open_mins = Column(Integer)

    #: Which columns the participant table shows, as a comma-separated list of
    #: keys. NULL means "whatever the defaults are" — which is not the same as
    #: the empty string, and the difference matters: an event set up before
    #: this existed should pick up a new default column when one is added,
    #: while an event somebody has actually chosen columns for should not have
    #: our choices pushed back onto it. Empty string is a real answer too:
    #: every optional column off.
    #:
    #: Stored per event rather than per user because the question is about the
    #: event, not about who is looking. A fitness test that records everyone's
    #: sex needs that column for everybody who opens it; a sponsor class never
    #: needs it at all.
    cols = Column(Text)
    #: The same, for the Can't make it tab. Its own column rather than a share
    #: of the one above: the two tables answer different questions — "who is
    #: coming and what do they still owe me" against "who dropped out, and
    #: what were they holding" — so one saved set would mean tuning one tab
    #: quietly wrecks the other.
    gone_cols = Column(Text)
    #: Hours from *their own* invitation within which somebody has to answer.
    #: Counted per person rather than from one fixed date, because somebody
    #: added to the list on the Thursday would otherwise inherit a deadline
    #: that expired on the Tuesday and lose a slot they were never asked about.
    #: The day this event used to be on, when it has been moved. Set only by
    #: a reschedule, and the only thing that makes the "date has changed" email
    #: sayable: a new date printed on its own is read as the one already in
    #: somebody's calendar, and they turn up on the wrong Sunday. Blank on an
    #: event that has never moved, which is almost all of them.
    moved_from = Column(DateTime(timezone=True))
    #: One sentence saying why it moved, shown above the two dates. A field
    #: rather than words in the template, because the template is shared by
    #: every event that ever moves and the reason never is - one class goes
    #: for weather, the next for a venue clash, and a template that has last
    #: time's reason typed into it will confidently send it again.
    moved_why = Column(String)
    confirm_hours = Column(Integer, nullable=False, default=48)
    #: A hard backstop, whatever the per-person clock says — the point past
    #: which an unanswered slot has to go to the waitlist so there is still
    #: time to fill it. Optional; the 48-hour clock does the work on its own.
    confirm_by = Column(DateTime(timezone=True))

    # --- what they get for it ---
    # Two, because a discount on one specific future event is worth nothing to
    # somebody not attending it, and a worthless reward is no reward at all.
    reward_a = Column(String)                    # "HYROX PFT"
    reward_a_detail = Column(String)             # "23 August · use it at checkout"
    reward_a_value = Column(String)              # "20% off"
    reward_b = Column(String)
    reward_b_detail = Column(String)
    reward_b_value = Column(String)
    #: Prefix for the codes we hand out, e.g. KR -> KR-4471.
    code_prefix = Column(String, nullable=False, default="EV")

    # --- start times, handed out at the door ---------------------------------
    #
    # Some classes run in waves: the first fifteen through the door start at
    # ten, everybody after them at eleven. Setting a first time switches the
    # whole thing on, so an event that says nothing here behaves exactly as it
    # always did.
    #
    # Assigned when somebody is scanned in rather than beforehand, because who
    # actually turns up is not who you planned for, and a slot handed to
    # somebody who never arrives is a wasted place in the earlier wave.
    slot_a_time = Column(String)                 # "10:00 AM"
    slot_a_cap = Column(Integer)                 # 15
    slot_b_time = Column(String)                 # "11:00 AM"

    # ---------------------------------------------------------------- heats
    # A fitness test is not a class. People go through it a few at a time on a
    # fixed clock, and every one of them has to be told their own time days
    # beforehand — which the two waves above cannot do, because they are
    # decided at the door on the day.
    #
    # These five hold the shape of the day; who is in which heat lives on the
    # participant. A first heat time is the switch: an event that leaves it
    # empty behaves exactly as it always did.
    heat_first = Column(String)                  # "10:00"  (24h, gym time)
    heat_last = Column(String)                   # "12:00"
    #: Minutes between heats. Ten is the HYROX PFT default.
    heat_every = Column(Integer, nullable=False, default=10)
    #: How many people are meant to be in one heat. A limit that warns rather
    #: than blocks — on the day you will want a fourth person in the 10:30 and
    #: the software should not be arguing with you about it.
    heat_cap = Column(Integer, nullable=False, default=3)
    #: How far before their heat they have to be in the building. Check-in and
    #: the warm-up both happen in this window, so it is the number that
    #: actually governs their morning — their heat time is the reason for it.
    heat_arrive = Column(Integer, nullable=False, default=30)
    #: When heat times were first sent for this event at all. Never cleared.
    #:
    #: The per-person flag is the better answer, but it cannot be recovered for
    #: events that predate it: moving somebody wipes heat_email_at, which was
    #: the only evidence they had ever been told. This survives because it only
    #: needs one person on the event to still be holding a stamp.
    #:
    #: It is also the safer default of the two mistakes available. Telling
    #: somebody new that their time "has changed" is mildly odd and they still
    #: get the right time; telling thirty-seven people who were moved "Your
    #: heat time" is how they decide it is the mail they already read, skip it,
    #: and turn up an hour wrong.
    heat_sent_at = Column(DateTime(timezone=True))
    #: The share link for the public timetable, or NULL for "no live link".
    #: A token rather than the slug, because the page carries other people's
    #: names: a guessable address for a start list is a start list anybody can
    #: find. Revoking sets this back to NULL and the old URL stops working,
    #: which is the whole point of it being reissuable.
    heat_token = Column(String, unique=True)
    heat_link_at = Column(DateTime(timezone=True))
    #: The live leaderboard's public link. Its own token rather than the
    #: timetable's: the timetable goes out days before to the people running,
    #: the board goes out on the morning to anybody watching, and revoking one
    #: should not take the other down.
    board_token = Column(String, unique=True)
    board_link_at = Column(DateTime(timezone=True))
    #: The sponsor's own logo, stored on the row rather than dropped in the
    #: static folder. A sponsor is a property of one event, not of the app, and
    #: the next one should be an upload rather than a deploy. It rides inside
    #: the email by Content-ID for the same reason the AWAKEN mark does —
    #: remote images are blocked by default in most mail clients, and a
    #: sponsor logo nobody sees is the one thing the sponsor will notice.
    sponsor_logo = Column(LargeBinary)
    sponsor_logo_mime = Column(String)

    #: The event's own header image, replacing the black bar and the AWAKEN
    #: mark at the top of its emails. Per event rather than per email: a class
    #: that has a look should carry it on everything it sends, and one image
    #: uploaded once is easier to get right than the same image chosen eight
    #: times. Rides by Content-ID for the same reason the other two do.
    banner = Column(LargeBinary)
    banner_mime = Column(String)

    #: The thank-you offer: money off for anybody who reviews us, and the day
    #: it stops. Both live on the event because the offer belongs to the class
    #: — a Christmas promo can be worth more than a Tuesday morning one — while
    #: the review link itself is the gym's and is typed once under Settings.
    #: No amount means no offer, and the box is not drawn at all.
    reward_amount = Column(Numeric(12, 2))
    reward_by = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), default=now_utc)
    created_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))

    created_by = relationship("Staff", foreign_keys=[created_by_id])
    participants = relationship("EventParticipant", back_populates="event",
                                cascade="all, delete-orphan")

    @property
    def ends_at(self):
        """When the class finishes, which is what the Reel window runs from.

        A class has no stored end time — one hour is the shape of every
        foundation class we run, and being an hour out on a 48-hour window
        changes nothing that matters.
        """
        return self.starts_at + timedelta(hours=1) if self.starts_at else None

    @property
    def reel_deadline(self):
        return (self.ends_at + timedelta(hours=self.reel_hours or 48)
                if self.ends_at else None)

    @property
    def when_text(self) -> str:
        """When the class is, written the way everybody should read it.

        One place decides, because this string goes on the public page, in two
        emails, on the pass and in your own tracker — and a date that reads four
        different ways is four chances for somebody to turn up on the wrong one.
        """
        if not self.starts_at:
            return ""
        fmt = "%a %d %b" if self.time_tba else "%a %d %b, %I:%M %p"
        return to_local(self.starts_at).strftime(fmt).replace(" 0", " ")

    def retired_slugs(self) -> list:
        return [x for x in (self.old_slugs or "").split("\n") if x.strip()]

    def retire_slug(self, fresh: str) -> None:
        """Put the current link out of use and take a new one.

        Capped, because this is a list nobody reads and every entry is a
        string somebody could otherwise keep alive forever by never clearing
        it. Twenty is more retirements than any class will ever have.
        """
        old = [self.slug] + [x for x in self.retired_slugs() if x != self.slug]
        self.old_slugs = "\n".join(old[:20])
        self.slug = fresh

    @property
    def reward_text(self) -> str:
        """The offer, written as money. Empty when there is no offer."""
        if not self.reward_amount:
            return ""
        amt = float(self.reward_amount)
        return "\u20b1%s" % (("%.2f" % amt).rstrip("0").rstrip(".")
                             if amt % 1 else "{:,.0f}".format(amt))

    @property
    def reward_by_text(self) -> str:
        """The last day to claim it, in gym time. No clock: a discount that
        expires at 11:59 is a discount somebody argues about at 12:05."""
        if not self.reward_by:
            return ""
        return to_local(self.reward_by).strftime("%a %d %b %Y").replace(" 0", " ")

    @property
    def closes_text(self) -> str:
        """The advertised cut-off, in gym time."""
        if not self.signup_closes:
            return ""
        return to_local(self.signup_closes).strftime(
            "%a %d %b, %I:%M %p").replace(" 0", " ")

    def signups_shut(self, now=None) -> bool:
        """Is the door closed to anybody new right now?

        Two ways to shut it and they are not the same thing: the switch is you
        deciding, the date is you having decided in advance. Either closes it.
        """
        if not self.signup_open:
            return True
        if not self.signup_closes:
            return False
        now = now or datetime.now(timezone.utc)
        closes = self.signup_closes
        if closes.tzinfo is None:
            closes = closes.replace(tzinfo=timezone.utc)
        return now >= closes

    # ---------------------------------------------------------------- rates
    def rate_rows(self) -> list:
        """Every rate this event has, in the order they are drawn."""
        return sorted(self.rates or [], key=lambda r: (r.position, r.id))

    def rates_open(self) -> list:
        """The ones somebody new may still pick."""
        return [r for r in self.rate_rows() if not r.closed]

    def rate(self, key):
        """The rate a participant picked, closed or not.

        Closed ones are found on purpose: somebody who paid the early-bird
        price still picked the early-bird rate, and the roster has to be able
        to say so.
        """
        want = (key or "").strip()
        if not want:
            return None
        return next((r for r in self.rate_rows() if r.key == want), None)

    def rate_label(self, key) -> str:
        r = self.rate(key)
        return r.label if r else ""

    @property
    def when_note(self) -> str:
        """The line that replaces a start time nobody has been given yet."""
        return "Start times to follow" if self.time_tba else ""

    @property
    def handle_list(self) -> list:
        return [h for h in (self.handles or "").replace(",", " ").split() if h]

    @property
    def rewards(self) -> list:
        """The reward choices, as (key, name, detail, value). Empty if unset."""
        out = []
        for key, name, detail, value in (
                ("a", self.reward_a, self.reward_a_detail, self.reward_a_value),
                ("b", self.reward_b, self.reward_b_detail, self.reward_b_value)):
            if (name or "").strip():
                out.append({"key": key, "name": name, "detail": detail or "",
                            "value": value or ""})
        return out

    def reward(self, key: str):
        return next((r for r in self.rewards if r["key"] == key), None)


#: How long an organiser's roster link stays live before it has to be
#: re-issued. Longer than a delegator's seven days because a sponsor works a
#: campaign, not a billing cycle — but not unlimited, because this page carries
#: other people's email addresses and a link nobody remembers issuing is the
#: one still working a year later.
ORGANISER_LINK_DAYS = 60

#: What a new organiser link is protected with unless somebody types something
#: else. A default only makes sense because the link is useless without the
#: token as well — the password is the second factor, not the only one.
ORGANISER_DEFAULT_PASS = "Kenny2026@"


class EventOrganiserLink(Base):
    """A private, password-gated roster for the people who paid for the class.

    A sponsor wants to see who is coming, and emailing them a spreadsheet each
    time is how the spreadsheet ends up three days stale in somebody's
    downloads folder. So they get a URL instead.

    Two things guard it, and they guard different failures. The token stops it
    being found; the password stops a forwarded link working for whoever the
    sponsor forwarded it to. Neither is enough alone — a token in a browser
    history is a token in somebody's browser history, and a password like
    Kenny2026@ would be guessed inside a minute if the address were public.

    The password is stored hashed, with the same PBKDF2 the staff PINs use.
    Nobody, including us, can read it back off this row; a forgotten one is
    reset, not recovered.
    """
    __tablename__ = "event_organiser_links"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"),
                      nullable=False)
    token = Column(String, unique=True, nullable=False)
    #: Who it was made for — "Kenny Rogers". Shown on the page so a sponsor
    #: with two events open knows which roster they are looking at.
    label = Column(String)
    pass_hash = Column(String)
    pass_salt = Column(String)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    created_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    expires_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    sent_to = Column(String)
    sent_at = Column(DateTime(timezone=True))
    first_opened_at = Column(DateTime(timezone=True))
    last_opened_at = Column(DateTime(timezone=True))
    opens = Column(Integer, nullable=False, default=0)

    event = relationship("Event")
    created_by = relationship("Staff", foreign_keys=[created_by_id])

    # No unique constraint on event_id: replacing a link revokes the old row
    # rather than deleting it, so somebody opening last month's email is told
    # a newer one was sent instead of getting a bare 404.

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at < now_utc())

    @property
    def is_live(self):
        return not self.revoked_at and not self.is_expired


class HeatPlan(Base):
    """One version of the timetable: the shape of the day, and who is in it.

    A day of heats is rarely right first time. You lay one out, then wonder
    whether starting at nine and running every eight minutes would finish
    earlier, or whether the two Urbinos should be split across heats. Doing
    that by editing the live timetable means the version you are experimenting
    with is the version thirty-eight people can already see — so instead each
    attempt is its own plan, and exactly one of them is live.

    The day shape lives here rather than only on the event because a version
    that could not change the interval would not be much of a version. The
    event's own ``heat_*`` columns and every participant's ``heat_time`` are
    kept as a copy of whichever plan is active: everything downstream — the
    emails, the public link, the door, the coach app — reads those and knows
    nothing about plans at all. Activating a plan is what writes the copy.
    """

    __tablename__ = "heat_plans"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"),
                      nullable=False)
    #: "Version 1", or whatever it gets renamed to. Shown on the tab.
    name = Column(String, nullable=False, default="Version 1")
    #: Order in the strip. Gaps are fine — the list is always read sorted.
    position = Column(Integer, nullable=False, default=0)

    heat_first = Column(String)
    heat_last = Column(String)
    heat_every = Column(Integer, nullable=False, default=10)
    heat_cap = Column(Integer, nullable=False, default=3)
    heat_arrive = Column(Integer, nullable=False, default=30)

    #: Exactly one per event. Enforced in code and by a partial unique index,
    #: because two live timetables is the one state with no honest answer to
    #: "what time am I racing".
    is_active = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), default=now_utc)
    created_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))

    event = relationship("Event", backref=backref(
        "heat_plans", cascade="all, delete-orphan",
        order_by="HeatPlan.position"))
    created_by = relationship("Staff", foreign_keys=[created_by_id])

    @property
    def assigned(self) -> int:
        return sum(1 for s in self.slots if s.heat_time)


class HeatSlot(Base):
    """One person's place in one version of the timetable.

    Only people who have been put somewhere get a row: an unassigned person is
    the absence of a slot, not a slot with an empty time. That way the tray of
    people still to place is the same question on every version — who has no
    row here — rather than two different kinds of nothing.
    """

    __tablename__ = "heat_slots"
    __table_args__ = (UniqueConstraint("plan_id", "participant_id",
                                       name="uq_slot_plan_person"),)

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("heat_plans.id", ondelete="CASCADE"),
                     nullable=False)
    participant_id = Column(Integer,
                            ForeignKey("event_participants.id",
                                       ondelete="CASCADE"), nullable=False)
    heat_time = Column(String, nullable=False)

    plan = relationship("HeatPlan", backref=backref(
        "slots", cascade="all, delete-orphan"))
    participant = relationship("EventParticipant")


#: Where somebody is in the race, as one word.
#:
#: Five of these are worked out rather than stored — the system already knows
#: when somebody was scanned in, when a coach took them, when their heat went
#: off and when they crossed the line, and a second copy of a fact is a second
#: chance to be wrong. The other five are judgements nothing can infer: a
#: disqualification, a start that never happened, a race abandoned halfway.
#: Those are set by hand, and setting one by hand freezes the row against the
#: derivation, which is the whole point of setting it.
#: How long before a heat the race app lets a coach in, when the event does not
#: say otherwise. Thirty minutes is about a warm-up: long enough to find your
#: athlete before the gun, short enough that the list on the phone is the heat
#: happening now rather than the whole day.
HEAT_OPEN_MINS = 30
#: Nobody is helped by a window of four seconds or of a week.
HEAT_OPEN_MIN, HEAT_OPEN_MAX = 1, 720


def heat_open_mins(event) -> int:
    """The event's own window, or the default. Always a sane number."""
    v = getattr(event, "heat_open_mins", None)
    try:
        v = int(v)
    except (TypeError, ValueError):
        return HEAT_OPEN_MINS
    return max(HEAT_OPEN_MIN, min(HEAT_OPEN_MAX, v))


def h12(t):
    """A heat time the way it is said out loud: "14:25" -> "2:25 PM".

    Heat times are stored as 24-hour strings because that is what sorts and
    what the timetable builder writes. Nothing on a race floor is read that
    way, though, and a coach glancing at a phone should not have to subtract
    twelve.
    """
    if not t:
        return ""
    try:
        h, m = int(str(t)[:2]), int(str(t)[3:5])
    except (ValueError, IndexError):
        return str(t)
    ampm = "AM" if h < 12 else "PM"
    h = h % 12 or 12
    return "%d:%02d %s" % (h, m, ampm)


#: Athletes exempt from the race rules, so a live event can be tested end to
#: end without waiting for a real heat. Matched on the full name, lower-cased.
#:
#: A test athlete differs from everybody else in exactly two ways:
#:
#: * their heat can be opened in the race app at any time, however far off it
#:   is -- but only *they* can be grabbed out of it early; anybody else sharing
#:   that heat still waits for the window; and
#: * their clock starts when a coach grabs them, not at the gun.
#:
#: Deliberately a short, visible list rather than a column and a checkbox: this
#: is scaffolding, and a name in a constant is easy to find and delete when the
#: testing is done. Every screen that shows one says so, because a clock that
#: behaves differently and does not admit it is how a real result gets doubted.
#: How the columns on the leaderboard are labelled, and the order they run in.
#: Category first because that is the bigger division - an Advanced woman is
#: racing the Advanced women, not the Open women - and the two Advanced columns
#: reading together on the left is what a spectator scanning for a winner
#: expects.
#:
#: Somebody whose gender is not recorded is not dropped. A results board that
#: silently omits people is worse than one with a short extra column, and they
#: go in one Unlisted column rather than one per category: it is a gap to be
#: closed, not a group to be ranked, and it disappears the moment it is filled
#: in. Every column here only appears if there is anybody in it.
BOARD_COLUMNS = ([("%s:%s" % (ck, sk), "%s %s" % (cl, "Men" if sk == "m"
                                                  else "Women"))
                  for ck, cl in CATEGORIES for sk, _sl in SEXES]
                 + [("", "Unlisted")])


def board_key(p) -> str:
    """Which column somebody belongs in."""
    if p.sex not in ("m", "f"):
        return ""
    return "%s:%s" % (category_key(getattr(p, "category", None)), p.sex)


def wants_reels(event) -> bool:
    """Does this event ask for a Reel?

    An explicit answer wins. With no answer, an invite event does and an open
    one does not - which is the difference between "we gave you a place, post
    about it" and "you paid to race".

    Everything Reel-shaped reads this: the participant's own page, the stages
    it can be in, the send lists and the reward panel. One question, asked in
    one place, so a Reel form cannot appear on a page whose email list is
    empty.
    """
    v = getattr(event, "reels_on", None)
    if v is not None:
        return bool(v)
    return getattr(event, "mode", None) != EVENT_OPEN


def station_shorts(stations) -> dict:
    """A short name per station, for a row that has to say where somebody is.

    "Burpee Broad Jump" becomes BBJ, "Wall Balls" WB, "Row" stays Row. Initials
    of the words, and a one-word name kept whole because "R" says nothing.

    If two stations would collapse to the same letters, *both* keep their full
    names. A board that says BB against two different stations is worse than a
    board with two long words on it - and shortening is a courtesy, not a rule
    worth being wrong for.
    """
    out, seen = {}, {}
    for st in stations:
        name = (st.name or "").strip()
        words = [w for w in name.split() if w]
        short = ("".join(w[0] for w in words).upper()
                 if len(words) > 1 else name)
        out[st.id] = short
        seen.setdefault(short, []).append(st.id)
    for short, ids in seen.items():
        if len(ids) > 1:
            for sid in ids:
                out[sid] = next(st.name for st in stations if st.id == sid)
    return out


def board_rows(event, now=None):
    """The whole field, ranked, split into the four groups.

    Returns [{"key", "label", "rows": [...]}] with an entry per group that has
    anybody in it - Advanced Men, Advanced Women, Open Men, Open Women, and an
    Unlisted column for anybody whose gender is not recorded yet.

    The order within a column is the order a spectator reads it: whoever is
    furthest through the race first.

      1. finishers, fastest first
      2. everybody still out there, deepest into the race first, and within a
         station whoever has been on it longest
      3. people whose heat has not gone off, by heat time
      4. DNF, DNS, DQ, cancelled, no-show

    Finishers are placed against each other and nobody else. A leader on
    station four is not "second" - they have not finished, and printing a
    number against them would be a placing that changes after somebody
    photographs it.
    """
    now = now or datetime.now(timezone.utc)
    stations = sorted(event.stations, key=lambda s: (s.position, s.id))
    nst = len(stations)
    shorts = station_shorts(stations)
    buckets = {k: [] for k, _l in BOARD_COLUMNS}
    for p in event.participants:
        # Not in the room: waitlisted, released, or said they cannot come.
        if p.waitlist or p.released_at or p.declined:
            continue
        st = race_status(p, now)
        runs = {r.station_id: r for r in (p.runs or [])}
        done = sum(1 for s in stations if s.id in runs and runs[s.id].ended_at)
        open_run = next((runs[s.id] for s in stations
                         if s.id in runs and not runs[s.id].ended_at), None)
        on = None
        st_row = None
        if open_run is not None:
            on = next((i + 1 for i, s in enumerate(stations)
                       if s.id == open_run.station_id), None)
            st_row = next((s for s in stations
                           if s.id == open_run.station_id), None)
        secs = p.race_seconds if p.finished_at else None
        elapsed = p.running_seconds(now)
        if st == "finished":
            tier = 0
        elif st in RACE_STATUS_OUT:
            tier = 3
        elif st == "in_progress" or done or open_run is not None:
            tier = 1
        else:
            tier = 2
        buckets.setdefault(board_key(p), []).append({
            "p": p, "status": st, "tier": tier,
            "secs": secs, "elapsed": elapsed or 0,
            "done": done, "on": on, "of": nst,
            "heat": p.heat_time or "",
            # Where they are and how far into it - "BBJ 34 / 80" rather than
            # "station 3 of 5". A spectator can find a lane from the first and
            # only count rows from the second.
            "st_name": st_row.name if st_row is not None else "",
            "st_short": shorts.get(st_row.id, "") if st_row is not None else "",
            "st_count": open_run.count if open_run is not None else None,
            "st_target": st_row.target if st_row is not None else None,
            "st_unit": st_row.unit if st_row is not None else "",
        })
    out = []
    for key, label in BOARD_COLUMNS:
        rows = buckets.get(key) or []
        rows.sort(key=lambda r: (
            r["tier"],
            r["secs"] if r["secs"] is not None else 0,      # fastest first
            -r["done"], -(r["on"] or 0), -r["elapsed"],     # deepest first
            r["heat"] or "99:99",
            (r["p"].full_name or "").lower(),
        ))
        place = 0
        for r in rows:
            if r["tier"] == 0:
                place += 1
                r["place"] = place
            else:
                r["place"] = None
        if rows:
            out.append({"key": key, "label": label, "rows": rows})
    return out


TEST_ATHLETES = {"van sampang"}


def is_test_athlete(p) -> bool:
    return (getattr(p, "full_name", "") or "").strip().lower() in TEST_ATHLETES


#: The longest a single station or a whole race is allowed to be, in seconds.
#: Twelve hours. Not a real race length - it is the line past which a typed
#: number is a typo rather than a correction, and letting one through would put
#: a nonsense on the board that nobody could explain.
CLOCK_MAX = 12 * 60 * 60


def parse_clock(text):
    """A typed duration as whole seconds, or None if there is nothing there.

    Takes the shapes a person actually types into a field labelled with a time
    already in it: ``3:20``, ``1:03:20``, and a bare ``200``. Blank means blank
    - "this station was not raced" is a real answer and is not the same as
    zero, so it comes back as None rather than 0 and the caller decides.

    Raises ValueError on anything else. An admin correcting a result at a
    trestle table needs to be told they fat-fingered it, not to have the row
    silently become 0:00 and go out on a finisher card.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) > 3:
        raise ValueError(raw)
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        raise ValueError(raw)
    if any(n < 0 for n in nums):
        raise ValueError(raw)
    # Only the leading field may run over: "90:00" is a plain ninety minutes,
    # but "1:90" is somebody who meant "1:30" and missed.
    if len(nums) > 1 and any(n > 59 for n in nums[1:]):
        raise ValueError(raw)
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    if secs > CLOCK_MAX:
        raise ValueError(raw)
    return secs


def station_splits(p, now=None):
    """One person's race, station by station, as the sheet reads it.

    Returns a row per station in race order — even the ones not reached, so
    the shape of the page does not change as somebody works through it.

    ``gap`` is the walk to the next lane: the stretch between one station
    closing and the next opening. It is its own number rather than folded into
    a split because it belongs to nobody's station, and hiding it inside one
    would quietly make that station look slower than it was raced.
    """
    stations = sorted(p.event.stations, key=lambda s: (s.position, s.id)) \
        if p.event else []
    runs = {r.station_id: r for r in (p.runs or [])}
    now = now or datetime.now(timezone.utc)
    out = []
    for i, st in enumerate(stations):
        r = runs.get(st.id)
        nxt = runs.get(stations[i + 1].id) if i + 1 < len(stations) else None
        gap = None
        if r and r.ended_at and nxt and nxt.started_at:
            gap = max(0, int((nxt.started_at - r.ended_at).total_seconds()))
        live = None
        if r and r.started_at and not r.ended_at:
            live = max(0, int((now - r.started_at).total_seconds()))
        out.append({
            "station": st, "name": st.name, "unit": st.unit,
            "target": st.target,
            "count": r.count if r else None,
            "secs": r.seconds if r else None,
            # A station still counting has no split yet, but it has a clock.
            "open_secs": live,
            "started": bool(r and r.started_at),
            "done": bool(r and r.ended_at),
            "gap": gap,
        })
    return out


def race_totals(p, now=None):
    """(on the stations, between them) in seconds, for one person.

    The two numbers the Time Summary shows side by side, computed the same way
    for the results page - the walks are not in the splits and are in the
    finish time, which is the whole reason both are worth printing.
    """
    rows = station_splits(p, now)
    raced = sum(r["secs"] for r in rows if r["secs"] is not None)
    moving = sum(r["gap"] for r in rows if r["gap"] is not None)
    return raced, moving


def station_field(event, now=None):
    """Every completed split on this event, station by station.

    ``{station_id: [(participant_id, seconds), ...]}`` sorted fastest first,
    which is what a rank and a spread are both read off.

    Only closed stations count. A station somebody is standing on has a clock
    but not a time, and ranking a race in progress against races that are over
    would put somebody first for having barely started.
    """
    out = {}
    for p in event.participants:
        if p.waitlist or p.released_at or p.declined:
            continue
        for r in station_splits(p, now):
            if r["secs"] is None:
                continue
            out.setdefault(r["station"].id, []).append((p.id, r["secs"]))
    for k in out:
        out[k].sort(key=lambda t: t[1])
    return out


def rank_in(pairs, pid):
    """(place, how many, how far through) for one person in one sorted list.

    ``through`` runs 1.0 for the fastest down to 0.0 for the slowest, which is
    what a shape wants to be drawn from - and is deliberately not a percentile,
    because with sixteen people a percentile implies a precision that sixteen
    people cannot carry.
    """
    ids = [i for i, _s in pairs]
    if pid not in ids:
        return None, len(pairs), None
    place = ids.index(pid) + 1
    n = len(pairs)
    through = 1.0 if n < 2 else 1.0 - (place - 1) / (n - 1)
    return place, n, through


def has_race(p) -> bool:
    """Is there anything to summarise for this person?

    Somebody standing at a trestle table wants to know whether pressing the
    button will show them a race or an empty page. Stations existing is not
    enough — a class where nobody has started yet has nothing to say.
    """
    if not p.event or not p.event.stations:
        return False
    return bool(p.finished_at or (p.runs or []))


RACE_STATUSES = [
    # Before registered, because they are not. Somebody who has been asked and
    # has not answered was reading "Registered" on the tracker, which is the
    # tracker asserting the one thing you are waiting to find out.
    #
    # "Not registered" rather than "For confirmation": the tracker is a list of
    # facts about people, and the fact here is that this person has not
    # answered. "For confirmation" reads like a task on your list instead, and
    # it sits directly opposite "Registered", which is what a yes makes them.
    ("for_confirmation", "Not registered"),
    ("registered",  "Registered"),
    ("checked_in",  "Checked in"),
    ("ready",       "Ready"),
    ("in_progress", "In progress"),
    ("finished",    "Finished"),
    ("dnf",         "DNF"),
    ("dns",         "DNS"),
    ("dq",          "DQ"),
    ("cancelled",   "Cancelled"),
    ("no_show",     "No show"),
]
RACE_STATUS_LABELS = dict(RACE_STATUSES)
RACE_STATUS_KEYS = [k for k, _ in RACE_STATUSES]

#: The ones a human sets. The rest arrive on their own and offering them by
#: hand would only let somebody pin a row to a lie the system can already see
#: through.
RACE_STATUS_MANUAL = ["dnf", "dns", "dq", "cancelled", "no_show"]
#: The ones that mean the race ended without a time. They sort to the
#: bottom of the leaderboard rather than being hidden: somebody looking
#: for a name should find it, and "DNF" is an answer.
RACE_STATUS_OUT = ["dnf", "dns", "dq", "cancelled", "no_show"]


def race_status(p, now=None, derived_only=False) -> str:
    """Where this participant is, in one word.

    Order matters and runs backwards from the finish: the furthest thing that
    has happened is the truest thing to say. Somebody who has finished is
    finished even though they were also, earlier, checked in.

    `derived_only` ignores a hand-set status and answers what the system would
    say on its own — which is what the "clear this" option has to be labelled
    with, since that is what clearing it would reveal.
    """
    if not derived_only and p.race_status_set in RACE_STATUS_KEYS:
        return p.race_status_set
    if p.finished_at:
        return "finished"
    # Their heat has gone off, or a station is already counting for them.
    started = bool(getattr(p, "runs", None))
    if not started:
        start = p.heat_start() if hasattr(p, "heat_start") else None
        if start:
            now = now or datetime.now(timezone.utc)
            started = now >= start
    if started and (p.coach_id or p.arrived_at):
        return "in_progress"
    if p.coach_id:
        return "ready"
    if p.arrived_at:
        return "checked_in"
    # Not coming is a status too, and the system already knows.
    if p.released_at or p.declined:
        return "cancelled"
    # Asked, and still deciding. Derived rather than stored, which is what
    # makes it survive a reset: clear somebody's answer and they land back
    # here on their own, with nothing to set and nothing to remember to set.
    #
    # Last but one, so everything further along the morning still wins - a
    # person who never answered and then walked in and was scanned reads
    # "Checked in", because they are.
    if p.rsvp == RSVP_NONE:
        return "for_confirmation"
    return "registered"


#: The question types, and what each one is for. Deliberately short: this is a
#: sign-up form, not a survey tool, and every type here is one somebody would
#: recognise from a Google Form without being told.
QUESTION_KINDS = [
    ("text",    "Short answer"),
    ("para",    "Paragraph"),
    ("number",  "Number"),
    ("email",   "Email address"),
    ("choice",  "Choose one"),
    ("checks",  "Tick all that apply"),
    ("select",  "Dropdown"),
    ("date",    "Date"),
    # A wall of text and one tick box. Not a question so much as a thing you
    # have to have agreed to, which is why what somebody agreed to is copied
    # onto the answer - see ParticipantAnswer.snapshot.
    ("terms",   "Terms and conditions"),
    # Asks nothing. Everything below it, until the next one, is on its own
    # page - which is the whole of how pagination works. A page break is a
    # thing in the list rather than a setting beside it, so it reorders and
    # deletes with the same two buttons as everything else.
    ("section", "Section \u2014 starts a new page"),
]
QUESTION_KIND_LABELS = dict(QUESTION_KINDS)
QUESTION_KIND_KEYS = [k for k, _l in QUESTION_KINDS]
#: The three that need a list of options written for them.
QUESTION_KINDS_WITH_OPTIONS = ("choice", "checks", "select")
#: Asks nothing and stores nothing.
QUESTION_KINDS_NO_ANSWER = ("section",)

#: The columns on a participant row that a question may be pointed at, and the
#: question types that fit each.
#:
#: Deliberately two. A form that can write to any column is a form that can
#: break the heats, the money or the door - see the note on save_doc. These
#: two are the opposite case: real fields the system reads, that the sign-up
#: had no way to collect, so the answer was being typed in again by hand
#: later or not captured at all.
#:
#: (key, label, allowed kinds, why it matters)
MAPPABLE = [
    ("age", "Age", ("number",),
     "The patch check reads this \u2014 which patch a time earns depends on it."),
    ("instagram", "Instagram handle", ("text",),
     "The Reel chase and the tag check read this."),
]
MAP_KEYS = [k for k, _l, _kinds, _why in MAPPABLE]
MAP_LABELS = {k: l for k, l, _kinds, _why in MAPPABLE}
MAP_WHY = {k: w for k, _l, _kinds, w in MAPPABLE}
MAP_KINDS = {k: kinds for k, _l, kinds, _why in MAPPABLE}


def map_fits(key, kind) -> bool:
    """Can a question of this type be pointed at this column?

    Asked on the way in as well as in the page. A number field is the only
    thing that can honestly fill an integer column, and "twenty-six" in an
    age is worse than a blank one.
    """
    return bool(key) and key in MAP_KINDS and kind in MAP_KINDS[key]


#: The fields the sign-up has always had, in the order it has always drawn
#: them. They live in the same list as everything else so they can be moved,
#: which is the only way "put the rate on page one" is ever possible.
#:
#: `locked` is the honest short list: without a name and an address there is
#: nobody to email, and without a rate there is nothing to pay. Everything
#: else - mobile, country, gender - is a question this gym happens to ask, and
#: the board has carried an "Unlisted" column for people with no gender since
#: the day it was written.
BUILTIN_FIELDS = [
    ("name",   "Name",             True),
    ("email",  "Email",            True),
    ("mobile", "Mobile",           False),
    ("country", "Country",         False),
    ("sex",    "Gender",           False),
    ("tier",   "Which rate applies", True),
]
BUILTIN_LABELS = {k: l for k, l, _r in BUILTIN_FIELDS}
BUILTIN_LOCKED = {k for k, _l, r in BUILTIN_FIELDS if r}
BUILTIN_KEYS = [k for k, _l, _r in BUILTIN_FIELDS]


class EventQuestion(Base):
    """One extra question on one event's sign-up form.

    The fields the system needs - name, email, gender, category, rate, payment
    - stay exactly where they are, in the page, in code. These are the ones the
    gym wants to ask on top: shirt size, injuries, whether they have raced
    before. Nothing here is read by the heats, the board or the results, which
    is what lets them be anything at all.

    Ordered by `position`, and drawn after the fixed fields on the first step.
    Before the payment step, never after: nobody abandons a sign-up at the
    shirt size and plenty abandon at the payment.
    """

    __tablename__ = "event_questions"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    title = Column(String, nullable=False)
    #: The description: the small grey line under the question. Optional, and
    #: worth having for anything where the answer depends on knowing something
    #: first - "we order a week ahead" is why somebody bothers to pick a size.
    #:
    #: Called `help` in here and "Description" everywhere a person sees it,
    #: because that is the word people use for it.
    help = Column(String)
    kind = Column(String, nullable=False, default="text")
    #: One option per line, for the three kinds that have them. Text rather
    #: than a table: an option has no identity worth keeping - renaming one is
    #: renaming a word, not migrating a row - and a textarea is the fastest
    #: way anybody has found to type six of them.
    options = Column(Text)
    required = Column(Boolean, nullable=False, default=False)
    position = Column(Integer, nullable=False, default=0)
    #: Which column on the participant row this question fills, if any - see
    #: MAPPABLE. The answer is written to the answers table either way; this
    #: is an *extra* copy, into the field the rest of the system already
    #: reads, so "how old are you?" on the form ends up where the patch check
    #: looks rather than in a row only a report can see.
    maps_to = Column(String)
    #: The line beside the tick box, on a terms field only. The terms
    #: themselves go in `options`, which is already the column for "the long
    #: text this kind of question needs" and would otherwise sit empty.
    tick = Column(String)
    #: Set when this row stands for one of the sign-up's own fields rather
    #: than a question somebody wrote - see BUILTIN_FIELDS. The row exists so
    #: the field can be moved and switched off; what it draws is still the
    #: markup the page has always drawn, because a name field is not a text
    #: question and pretending otherwise loses the autocomplete.
    builtin = Column(String)
    #: Not asked at all. Only ever true on a built-in that is not locked -
    #: deleting a question is how you get rid of a question, and a built-in
    #: cannot be deleted because it would come back on the next visit.
    hidden = Column(Boolean, nullable=False, default=False)

    event = relationship("Event", backref=backref(
        "questions", cascade="all, delete-orphan",
        order_by="EventQuestion.position"))

    @property
    def option_list(self) -> list:
        return [x.strip() for x in (self.options or "").splitlines() if x.strip()]

    @property
    def wants_options(self) -> bool:
        return not self.builtin and self.kind in QUESTION_KINDS_WITH_OPTIONS

    @property
    def is_section(self) -> bool:
        return not self.builtin and self.kind == "section"

    @property
    def maps_label(self) -> str:
        return MAP_LABELS.get(self.maps_to or "", "")

    @property
    def is_terms(self) -> bool:
        return not self.builtin and self.kind == "terms"

    @property
    def tick_line(self) -> str:
        """What it says next to the box, with a sensible default."""
        return (self.tick or "").strip() or \
            "I have read and agree to the terms and conditions"

    @property
    def locked(self) -> bool:
        """Required, and not up for discussion.

        Name, email and the rate. Without the first two there is nobody to
        email; without the third there is nothing to pay.
        """
        return self.builtin in BUILTIN_LOCKED

    @property
    def stores_answer(self) -> bool:
        return not self.builtin and self.kind not in QUESTION_KINDS_NO_ANSWER

    def __repr__(self):
        return "<EventQuestion %s>" % (self.title,)


class ParticipantAnswer(Base):
    """What one person answered to one question.

    A row per answer rather than a JSON blob on the participant, for one
    reason: the saved-reports feature can read a table. "Shirt sizes by count"
    is then a report somebody writes once, instead of a spreadsheet somebody
    counts every time.

    An agreement to a set of terms also carries a copy of the terms as they
    read at the moment it was given - see `snapshot`.

    Ticked boxes are stored as one row with the choices joined by a newline.
    They are read back as a list and written out joined by a comma, and that
    is the whole of it - a second table so that "Large, Medium" could be two
    rows would buy nothing anybody has asked for.
    """

    __tablename__ = "participant_answers"
    __table_args__ = (UniqueConstraint("participant_id", "question_id"),)

    id = Column(Integer, primary_key=True)
    participant_id = Column(Integer,
                            ForeignKey("event_participants.id",
                                       ondelete="CASCADE"),
                            nullable=False, index=True)
    question_id = Column(Integer,
                         ForeignKey("event_questions.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    value = Column(Text)
    #: Only ever set on a terms field: the exact wording that was on screen
    #: when they ticked the box.
    #:
    #: Copied rather than referenced on purpose. The terms are editable, and
    #: an agreement that points at whatever the text says today is not a
    #: record of anything - the forty people who signed in August have to keep
    #: the August wording however many times it is rewritten afterwards.
    snapshot = Column(Text)

    question = relationship("EventQuestion")
    participant = relationship("EventParticipant", backref=backref(
        "answers", cascade="all, delete-orphan"))

    @property
    def shown(self) -> str:
        """The answer as a person reads it."""
        return ", ".join(x for x in (self.value or "").splitlines() if x) \
            if "\n" in (self.value or "") else (self.value or "")


class SavedReport(Base):
    """One report: a name, and the SELECT that answers it.

    The point is that the question lives in the database rather than in a
    deploy. "Everybody's splits and what patch they earned" is a real question
    that will be asked again next event, and the previous answer was somebody
    reading thirty-eight rows off a screen into a spreadsheet.

    The SQL is stored, not the results. A report is a question, and the answer
    changes every time somebody finishes.

    What makes storing SQL safe is not this table, it is how it is run - see
    `run_report` in report_routes: one statement, SELECT only, inside a
    read-only transaction with a timeout and a row cap. A row here cannot
    delete anything however it is written, which is the only footing on which
    a text box that executes SQL belongs in an admin at all.
    """

    __tablename__ = "saved_reports"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    #: What it answers, in a sentence, for whoever opens the list in a year.
    notes = Column(Text)
    sql = Column(Text, nullable=False)
    #: Set on the ones this file ships. A corrected shipped report reaches the
    #: database on the next deploy, but only while `updated_at` is still null:
    #: somebody who edits a shipped report keeps their edit through every
    #: future deploy, because the day your own changes get silently reverted
    #: is the day you stop trusting it.
    builtin_key = Column(String, unique=True)
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True))

    def __repr__(self):
        return "<SavedReport %s>" % (self.name,)



#: How the rates are drawn. Three, because there are only three shapes a
#: short list of priced choices takes, and a fourth would be decoration.
RATE_LOOKS = [
    ("tiles", "Tiles"),
    ("list", "List"),
    ("drop", "Dropdown"),
]
RATE_LOOK_KEYS = [k for k, _l in RATE_LOOKS]


class EventRate(Base):
    """One rate somebody can pick on the sign-up: a label and what it costs.

    There were two, in four columns on the event, and two covers almost every
    event right up until the one it doesn't - early bird, student, walk-in,
    the coach who brings four people. A row each instead, so there can be as
    many as the gym charges.

    A participant stores this row's id, and separately the amount they were
    charged at the time. That second copy is the important one: the rate can
    be renamed or repriced tomorrow without restating what somebody paid
    today.

    Which is also why a rate in use is never deleted, only closed. Deleting it
    would leave a registration pointing at nothing - the money is still on the
    row, but what they picked would have no name.
    """

    __tablename__ = "event_rates"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    label = Column(String, nullable=False)
    amount = Column(Numeric(10, 2))
    position = Column(Integer, nullable=False, default=0)
    #: Still valid on the registrations that picked it, no longer offered to
    #: anybody new. The honest version of deleting an early-bird price.
    closed = Column(Boolean, nullable=False, default=False)

    event = relationship("Event", backref=backref(
        "rates", cascade="all, delete-orphan",
        order_by="EventRate.position"))

    @property
    def key(self) -> str:
        """What a participant's `tier` holds. A string, because that column is."""
        return str(self.id)

    def __repr__(self):
        return "<EventRate %s>" % (self.label,)


class EventStation(Base):
    """One workout station on an event, in the order it is raced.

    The whole station is configuration rather than code: a fitness test with
    different stations next month is rows in this table, not a deploy. The one
    field that is not obvious is `increment` — it is the size of the button a
    coach taps on their phone, so it is a coaching decision about how fast the
    reps come, not a unit conversion. 100 wall balls at +1 is a hundred taps
    while watching somebody's depth; at +5 it is twenty.
    """

    __tablename__ = "event_stations"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"),
                      nullable=False)
    #: Race order. Gaps are fine — the list is always read sorted.
    position = Column(Integer, nullable=False, default=0)
    name = Column(String, nullable=False)
    #: 'distance' or 'reps'. Only changes the unit shown; everything
    #: downstream compares two plain numbers, so there is no second code path.
    measure = Column(String, nullable=False, default="reps")
    target = Column(Integer, nullable=False, default=0)
    increment = Column(Integer, nullable=False, default=1)

    event = relationship("Event", backref=backref(
        "stations", cascade="all, delete-orphan",
        order_by="EventStation.position"))

    @property
    def unit(self) -> str:
        return "m" if self.measure == "distance" else "reps"

    @property
    def taps(self) -> int:
        """How many times a coach will press the button to finish this."""
        inc = max(1, self.increment or 1)
        return max(0, -(-(self.target or 0) // inc))


class StationRun(Base):
    """One person's attempt at one station: the count, and when it opened and
    closed.

    `ended_at` is stamped the instant the target is reached, not when the coach
    presses anything — a coach who looks up, says well done, and takes four
    seconds to reach for the phone should not cost the athlete four seconds.
    The gap between one station ending and the next opening is transition time
    and belongs to nobody's split.
    """

    __tablename__ = "station_runs"
    __table_args__ = (UniqueConstraint("participant_id", "station_id",
                                       name="uq_run_person_station"),)

    id = Column(Integer, primary_key=True)
    participant_id = Column(Integer,
                            ForeignKey("event_participants.id",
                                       ondelete="CASCADE"), nullable=False)
    station_id = Column(Integer, ForeignKey("event_stations.id",
                                            ondelete="CASCADE"), nullable=False)
    count = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))

    participant = relationship("EventParticipant", backref=backref(
        "runs", cascade="all, delete-orphan"))
    station = relationship("EventStation")

    @property
    def seconds(self):
        """How long this station took, or None while it is still open."""
        if not self.started_at or not self.ended_at:
            return None
        return int((self.ended_at - self.started_at).total_seconds())


class EventParticipant(Base):
    """One person's slot, and the whole trail of what they did with it.

    The token is the credential — there is no login anywhere in this flow.
    Participants read these on a phone between clients, and an account they
    have to remember a password for is an account they won't use. The cost is
    that whoever holds the URL can act as that person, so the token is long and
    random and reaches exactly one participant's one event.
    """
    __tablename__ = "event_participants"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    email = Column(String)
    token = Column(String, unique=True, nullable=False)

    # --- before the class ---
    invited_at = Column(DateTime(timezone=True))
    #: When the "post your Reel" email went out. Separate from `invited_at`
    #: because they are two different asks sent at two different moments, and
    #: the only way to know who still needs the second one is to have stamped
    #: it. Without this the send button can only guess.
    reel_email_at = Column(DateTime(timezone=True))
    opens = Column(Integer, nullable=False, default=0)
    last_opened_at = Column(DateTime(timezone=True))
    rsvp = Column(String, nullable=False, default=RSVP_NONE)
    rsvp_at = Column(DateTime(timezone=True))
    instagram = Column(String)
    acknowledged_at = Column(DateTime(timezone=True))
    #: Set when the confirmation window lapsed and the slot went elsewhere.
    released_at = Column(DateTime(timezone=True))
    #: When the "you're in, here's your pass" email went out. Stamped so a
    #: second confirm — somebody fixing their handle — doesn't send it twice.
    pass_email_at = Column(DateTime(timezone=True))
    #: When they were last nudged about a registration they never finished.
    #: Stamped so the second send can default to people who have not had one —
    #: chasing the same person four times is how you get filed as spam.
    nudged_at = Column(DateTime(timezone=True))
    #: When they were sent the last call to confirm. Same reasoning as the
    #: nudge: the default list is the people who have not had one, because a
    #: deadline reminder that arrives three times stops reading as a deadline.
    last_call_at = Column(DateTime(timezone=True))
    #: When they were scanned in at the door. The QR on their page is the
    #: fast path; the tracker has a button for the phone that died on the way.
    arrived_at = Column(DateTime(timezone=True))
    #: When they were told the class was called off. Stamped so a second send
    #: defaults to whoever has not had it — a cancellation that arrives twice
    #: reads as a second cancellation, and somebody rings to ask which.
    cancel_email_at = Column(DateTime(timezone=True))
    #: When the thank-you went out. What keeps "send to everyone who hasn't
    #: had it" from meaning "send it to Marc for the third time".
    thanks_email_at = Column(DateTime(timezone=True))
    #: Their place in the arrival order, and the start time it earned them.
    #: The number is kept as well as the time because it is what makes the
    #: assignment checkable — "why am I in the second wave" has an answer.
    #: Both are written once, on the first scan, and never recomputed: a start
    #: time that moved after somebody was told it is worse than no system.
    slot_no = Column(Integer)
    slot_time = Column(String)
    slot_at = Column(DateTime(timezone=True))

    #: Which heat they are in, as "HH:MM" in gym time. Deliberately not the
    #: same field as slot_time: that one is written by the scanner on the day
    #: and this one is decided by hand a week before, and a single column would
    #: mean the door quietly overwriting a time twenty people already have in
    #: their inbox.
    heat_time = Column(String)
    #: Which coach has them on their phone. Exclusive: one coach, one athlete,
    #: one screen — a coach juggling two timers will lose reps on both.
    coach_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    grabbed_at = Column(DateTime(timezone=True))
    #: When the last station closed. The official time is this minus the heat
    #: start, which is one subtraction between two fixed points and survives a
    #: coach being late to press anything.
    finished_at = Column(DateTime(timezone=True))
    #: Their age, as given at the awarding table. Kept rather than asked twice:
    #: it decides which patch bracket they fall in, and somebody coming back to
    #: the desk should not be asked their age again in front of a queue.
    #: Not a date of birth — nobody at a trestle table is doing that sum.
    age = Column(Integer)
    #: When they tapped through to Google to leave a review.
    #:
    #: Named for what actually happened, because that is all anybody can know.
    #: Google tells a website nothing about its own reviews - no callback, no
    #: return URL, and the reviews API hands back a display name and a photo,
    #: never an address to match a person by. So this is "they went", not "they
    #: reviewed", and a report built on it will read high. Calling the column
    #: reviewed_at would have made that error permanent and invisible.
    review_opened_at = Column(DateTime(timezone=True))
    #: Which patch they earned, worked out from age and finish time, and when
    #: it was handed over. The stamp is what stops a second member of staff
    #: handing a second patch to somebody who already has theirs.
    patch = Column(String)
    patch_at = Column(DateTime(timezone=True))
    #: A status set by hand, which freezes the row against the derivation.
    #: Null means "work it out from what has actually happened".
    race_status_set = Column(String)
    #: When they were last told it. Cleared whenever they are moved, because a
    #: time somebody was told is only true until you move them — see
    #: EventParticipant.heat_told.
    heat_email_at = Column(DateTime(timezone=True))
    #: Have they *ever* had a heat time from us? Never cleared, which is the
    #: whole point: heat_email_at is wiped every time somebody moves, so on its
    #: own it cannot tell "first time we're telling you" from "we're telling
    #: you again". That distinction is which of the two emails they get.
    heat_told_before = Column(Boolean, nullable=False, default=False)

    def heat_start(self):
        """Their heat as an actual moment, or None.

        The event's date plus their heat's clock time, read in gym time. This
        is the fixed point everything else is measured from — it never moves,
        whatever a coach does.

        One exception, and it is the whole of the exception: a test athlete
        (see ``TEST_ATHLETES``) starts when a coach grabs them. Overriding the
        answer *here* rather than in each screen means the race clock, the
        splits, the status column, the finish time, the patch and the card all
        follow from one line and cannot drift apart. Until they are grabbed
        there is no start at all, which is what "the timer starts on the grab"
        means.
        """
        if is_test_athlete(self):
            return self.grabbed_at
        if not self.heat_time or not self.event or not self.event.starts_at:
            return None
        try:
            h, m = int(self.heat_time[:2]), int(self.heat_time[3:])
        except (ValueError, IndexError):
            return None
        day = to_local(self.event.starts_at)
        return from_local(day.replace(hour=h, minute=m, second=0, microsecond=0))

    @property
    def race_seconds(self):
        """Official time in seconds, or None until the last station closes."""
        start = self.heat_start()
        if not start or not self.finished_at:
            return None
        return int((self.finished_at - start).total_seconds())

    def running_seconds(self, now=None):
        """Time on the clock right now. Always now minus the heat start —
        nothing a coach does is allowed to touch it."""
        start = self.heat_start()
        if not start:
            return None
        if self.finished_at:
            return int((self.finished_at - start).total_seconds())
        now = now or datetime.now(timezone.utc)
        return max(0, int((now - start).total_seconds()))

    @property
    def heat_told(self) -> bool:
        """Do they hold a heat time we have actually sent them?"""
        return bool(self.heat_time and self.heat_email_at)
    # --- self-registration (mode == open) ---
    first_name = Column(String)
    last_name = Column(String)
    mobile = Column(String)
    sex = Column(String)                     # 'm' / 'f'
    #: 'elite' / 'open'. NOT NULL with a default for the same reason as the
    #: country below: this crosses gender rather than replacing it, so a null
    #: here would put a fifth, nameless column on the leaderboard rather than
    #: leave a blank in a row.
    category = Column(String, nullable=False, default=CATEGORY_DEFAULT,
                      server_default=CATEGORY_DEFAULT)
    #: ISO-3166-1 alpha-2, for the flag on the leaderboard. NOT NULL with a
    #: default rather than nullable: everybody has a country, and a board where
    #: half the rows have no flag looks broken rather than private.
    country = Column(String, nullable=False, default="PH", server_default="PH")
    #: Which rate they picked, as the EventRate id in a string. It was 'a' or
    #: 'b' when there could only ever be two; the migration rewrote those to
    #: ids and nothing writes a letter any more.
    tier = Column(String)
    #: What they owed, stored per person rather than read off the event — so a
    #: price change tomorrow never restates what somebody paid today.
    amount = Column(Numeric(10, 2))
    external_done_at = Column(DateTime(timezone=True))
    #: When we told them we had their registration - sent the moment they
    #: finish the form. A stamp rather than a flag so it can never go twice,
    #: and so the roster can say when.
    signup_email_at = Column(DateTime(timezone=True))
    proof = Column(LargeBinary)
    proof_mime = Column(String)
    proof_ref = Column(String)
    pay_status = Column(String)              # None for invited people
    submitted_at = Column(DateTime(timezone=True))
    reviewed_at = Column(DateTime(timezone=True))
    reviewed_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    review_note = Column(Text)

    #: Their own answer-by, when it cannot be the event's. Somebody handed a
    #: slot off the waitlist the night before is being asked after the event's
    #: cut-off has passed — without a deadline of their own they would open
    #: their link to "this slot went to the waitlist", which is the one thing
    #: it did not do.
    confirm_due = Column(DateTime(timezone=True))
    #: On the waiting list rather than in the room. They are loaded at the same
    #: time as everybody else — the whole point is that the replacement is
    #: already on file when somebody drops out — but they are invisible to
    #: every send and every count until you give one of them a slot.
    waitlist = Column(Boolean, nullable=False, default=False)

    # --- after the class ---
    reel_url = Column(String)
    reel_at = Column(DateTime(timezone=True))
    tags = Column(String, nullable=False, default=TAGS_PENDING)
    tags_note = Column(String)
    reshared_at = Column(DateTime(timezone=True))

    # --- what they got ---
    reward_key = Column(String)                  # 'a' or 'b'
    reward_code = Column(String)
    reward_at = Column(DateTime(timezone=True))
    redeemed_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), default=now_utc)
    #: The moment their unpaid slot stops being held. Written when you send
    #: the last-call-to-pay email and not before, because a deadline nobody has
    #: been told about is not a deadline — it is a reason to argue later.
    pay_due_at = Column(DateTime(timezone=True))
    #: When somebody last changed this row by hand from the tracker.
    #:
    #: Deliberately not a SQLAlchemy `onupdate`. Every time a participant opens
    #: their own link we bump their view counter, which is an UPDATE — so an
    #: onupdate column would read "last updated" and mean "last looked at",
    #: which is the wrong answer to the question the column is asked.
    edited_at = Column(DateTime(timezone=True))

    event = relationship("Event", back_populates="participants")
    reviewed_by = relationship("Staff", foreign_keys=[reviewed_by_id])

    __table_args__ = (UniqueConstraint("event_id", "reward_code",
                                       name="uq_event_reward_code"),)

    @property
    def full_name(self) -> str:
        """First + last where we have them, otherwise whatever we were given."""
        both = " ".join(x for x in (self.first_name, self.last_name) if x)
        return both or (self.name or "")

    #: First and last for a form to pre-fill, whichever way the row was made.
    #: A self-registration fills first_name/last_name; a row added by hand off
    #: a list has only `name`. Splitting on the last space is a guess, but it
    #: is a guess the person can correct in the box, which is better than
    #: handing them two empty fields and their own name in the greeting.
    #: Everything that can happen to a participant, newest first when read.
    #: Used to answer "when did this last move, and what moved" without
    #: keeping a second copy of the truth in an updated_at column.
    @property
    def last_touch(self):
        marks = (
            (self.redeemed_at, "Reward redeemed"),
            (self.reward_at, "Reward issued"),
            (self.reel_at, "Reel posted"),
            (self.arrived_at, "Checked in"),
            (self.slot_at, "Start time assigned"),
            (self.reviewed_at, "Payment reviewed"),
            (self.submitted_at, "Receipt sent"),
            (self.external_done_at, "Organiser step done"),
            (self.acknowledged_at, "Agreed to the terms"),
            (self.rsvp_at, "Answered"),
            (self.released_at, "Slot released"),
            (self.cancel_email_at, "Cancellation sent"),
            (self.last_call_at, "Last call sent"),
            (self.nudged_at, "Nudged"),
            (self.reel_email_at, "Reel email sent"),
            (self.thanks_email_at, "Thank-you sent"),
            (self.pass_email_at, "Pass sent"),
            (self.invited_at, "Invited"),
            (self.pay_due_at, "Last call to pay sent"),
            (self.edited_at, "Edited"),
        )
        best = None
        for when, what in marks:
            if when is not None and (best is None or when > best[0]):
                best = (when, what)
        return best

    @property
    def changed_at(self):
        """When anything about this row last moved. Falls back to when it was made."""
        t = self.last_touch
        return t[0] if t else self.created_at

    @property
    def changed_what(self) -> str:
        t = self.last_touch
        return t[1] if t else "Added"

    @property
    def given(self) -> str:
        if self.first_name:
            return self.first_name
        return (self.name or "").strip().split(" ")[0] if self.name else ""

    @property
    def family(self) -> str:
        if self.last_name:
            return self.last_name
        bits = (self.name or "").strip().split(" ")
        return " ".join(bits[1:]) if len(bits) > 1 else ""

    @property
    def registering(self) -> bool:
        """A self-registration, at any stage."""
        return self.pay_status is not None

    @property
    def paid(self) -> bool:
        return self.pay_status == PAY_APPROVED

    @property
    def confirmed(self) -> bool:
        # A free registration is confirmed by the person saying yes, and by
        # nothing else. There is no payment to approve, so the pay_status a
        # free sign-up carries is bookkeeping rather than an answer — reading
        # it as one is how a class that costs nothing ended up with twenty
        # people sitting in a payment review queue, and how somebody who had
        # pressed Confirm read "Payment in review" on a class with no payments.
        if self.pay_status is not None and not self.free:
            return self.pay_status == PAY_APPROVED
        return self.rsvp == RSVP_YES

    @property
    def free(self) -> bool:
        """Did this registration cost anything?

        Read off the row, not the event: the amount is copied here when they
        pick their rate, so a class that is free today and priced tomorrow
        does not rewrite what somebody already did.
        """
        return not self.amount or Decimal(self.amount) <= 0

    @property
    def declined(self) -> bool:
        return self.rsvp == RSVP_NO

    @property
    def handle(self) -> str:
        """The Instagram handle with exactly one leading @, or ''."""
        h = (self.instagram or "").strip().lstrip("@")
        return "@" + h if h else ""

    @property
    def posted(self) -> bool:
        return bool(self.reel_url)

    @property
    def qualified(self) -> bool:
        """Earned the reward: confirmed, showed up in the list, posted a Reel.

        Deliberately not gated on the tag check. Taking a reward back from
        somebody who made a Reel and forgot one tag costs far more goodwill
        than the discount is worth — a missing tag gets a friendly message,
        not a clawback.
        """
        return self.confirmed and self.posted


#: An Instagram handle longer than this is not a handle.
HANDLE_MAX = 30


class EmailTemplate(Base):
    """An email whose wording somebody has taken over.

    Absent means untouched: the built-in version is used. That is deliberate —
    it makes "reset to original" a delete rather than a copy of a default that
    would then rot the moment the shipped wording improves, and it means a new
    install starts with no rows and the right emails.
    """
    __tablename__ = "email_templates"
    id = Column(Integer, primary_key=True)
    #: Which email. Matches a key in mail_defaults(); see email_routes.
    key = Column(String, unique=True, nullable=False)
    subject = Column(Text)
    body = Column(Text)
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    updated_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    updated_by = relationship("Staff", foreign_keys=[updated_by_id])


# ---------------------------------------------------------------------------
# Event planning
#
# A corporate enquiry is not an event yet. It is weeks of scope, budget
# options, equipment lists and a run sheet, argued over with somebody on the
# client's side who does not have a login here and never will. That is a
# different object from an Event, which assumes a date, a slug and a list of
# participants — so it lives in its own table rather than as fifteen nullable
# columns bolted onto one.
#
# The whole editable pack is kept as one JSON document. It is deliberate. The
# alternative is a table per section — scope lines, checklist tasks, budget
# rows, stations, staffing, run sheet — which is a dozen tables and a dozen
# migrations to add one column to a planning document nobody queries across.
# Nothing here is reported on, summed across plans or joined to; it is read and
# written whole, by one person at a time. The two numbers that *are* worth
# asking about across plans — the headcount and the chosen total — are lifted
# out into their own columns so the list screen never has to open the document.
# ---------------------------------------------------------------------------

#: How long an external planning link stays good for. Longer than a sponsor's
#: roster because planning runs for months, not the week around a class.
PLAN_LINK_DAYS = 120


class EventPlan(Base):
    """One planning pack, for one client, with its own external link."""
    __tablename__ = "event_plans"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    #: Who it is for — "San Miguel". Kept as free text rather than a link to a
    #: person or a delegator, because at the point somebody starts planning
    #: there is often nothing in the system to point at yet.
    client = Column(String)
    #: The whole editable document. See plan_seed.py for the shape.
    data = Column(Text, nullable=False, default="{}")
    #: Lifted out of the document so the list screen can show them without
    #: parsing every plan. Written by the same save that writes `data`, and
    #: treated as a cache of it — the document is what is true.
    headcount = Column(Integer)
    chosen_option = Column(String)
    chosen_total = Column(Numeric(12, 2))
    archived_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=now_utc)
    created_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    updated_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))

    #: The external link. One live link per plan, guarded the same way a
    #: sponsor's roster is: the token stops it being found, the password stops
    #: a forwarded link working for whoever it was forwarded to.
    token = Column(String, unique=True)
    pass_hash = Column(String)
    pass_salt = Column(String)
    link_made_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    first_opened_at = Column(DateTime(timezone=True))
    last_opened_at = Column(DateTime(timezone=True))
    opens = Column(Integer, nullable=False, default=0)

    created_by = relationship("Staff", foreign_keys=[created_by_id])
    updated_by = relationship("Staff", foreign_keys=[updated_by_id])
    comments = relationship(
        "EventPlanComment", back_populates="plan",
        cascade="all, delete-orphan",
        order_by="EventPlanComment.created_at")

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at < now_utc())

    @property
    def has_link(self):
        return bool(self.token)

    @property
    def is_live(self):
        return bool(self.token) and not self.revoked_at and not self.is_expired

    @property
    def open_comments(self):
        return [c for c in self.comments if not c.resolved_at]

    @property
    def title(self):
        return "%s — %s" % (self.name, self.client) if self.client else self.name


class EventPlanComment(Base):
    """Something the other side wanted to say, pinned to where they said it.

    The anchor is a string the page understands — "budget:addon:3",
    "checklist:12", "tab:Scope" — rather than a foreign key, because the thing
    being commented on lives inside a JSON document and may be deleted by the
    next edit. A comment whose anchor no longer resolves is not lost: it falls
    back to the section it was left in, which is still enough to answer "what
    were they objecting to".
    """
    __tablename__ = "event_plan_comments"
    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("event_plans.id", ondelete="CASCADE"),
                     nullable=False)
    #: Where on the page. Free-form on purpose — see the class docstring.
    anchor = Column(String, nullable=False, default="")
    #: A human-readable version of the anchor, captured when the comment was
    #: written. The row it pointed at may be gone by the time you read it.
    anchor_label = Column(String)
    #: They type their name once and the page remembers it. No accounts.
    author = Column(String, nullable=False, default="")
    body = Column(Text, nullable=False, default="")
    #: True when it came from inside the portal rather than off the link, so a
    #: reply from AWAKEN reads differently from the client's own note.
    from_staff = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    resolved_at = Column(DateTime(timezone=True))
    resolved_by_id = Column(Integer, ForeignKey("entity.id", ondelete="SET NULL"))

    plan = relationship("EventPlan", back_populates="comments")
    resolved_by = relationship("Staff", foreign_keys=[resolved_by_id])
