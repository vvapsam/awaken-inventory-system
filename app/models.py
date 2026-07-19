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
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    role_obj = relationship("Role")
    affiliate = relationship("Staff", foreign_keys=[affiliate_id], remote_side=[id])

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
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


PRICING_KINDS = [("employee", "Employee"), ("affiliate", "Affiliate")]


class PricingGroup(Base):
    """A price tier (Employee / Affiliate) giving a % discount on selected products.

    Base price = the product's normal selling price (no tier). Employee tiers round
    the discounted price UP to a whole peso and can cap discounted items per day.
    """
    __tablename__ = "pricing_groups"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False, default="employee")   # employee | affiliate
    discount_percent = Column(Numeric(5, 2), nullable=False, default=0)  # e.g. 15.00
    round_up = Column(Boolean, nullable=False, default=False)   # ceil price to whole peso
    daily_item_limit = Column(Integer)                          # NULL = unlimited (e.g. 2)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    items = relationship("PricingGroupItem", cascade="all, delete-orphan", backref="group")

    def eligible_ids(self):
        return {i.product_id for i in self.items}

    def price_for(self, product):
        """Discounted price if the product is eligible, else its normal price."""
        base = float(product.selling_price or 0)
        if product.id in self.eligible_ids():
            raw = base * (1 - float(self.discount_percent or 0) / 100.0)
            if self.round_up:
                return float(math.ceil(raw - 1e-9))       # always round up to whole peso
            return round(raw, 2)
        return round(base, 2)


class PricingGroupItem(Base):
    __tablename__ = "pricing_group_items"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("pricing_groups.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)


# Legacy `discount_codes` (per-person codes) were folded into the Staff entity
# table; the table is migrated then dropped at startup. No ORM model remains.


# Legacy `payments` (customer balance payments) folded into transactions; dropped at startup.
