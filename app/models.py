import math
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Integer,
    LargeBinary, Numeric, String, Text,
)
from sqlalchemy.orm import relationship
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
    ("employee", "Employee"),
    ("affiliate", "Affiliate"),
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
    """A person or party. May have system access (login + role) and/or a
    relationship type (employee / affiliate / supplier). One unified table."""
    __tablename__ = "staff"
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
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    role_obj = relationship("Role")

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


class StockMovement(Base):
    __tablename__ = "stock_movements"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    movement_type = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)  # +adds, -removes
    unit_cost = Column(Numeric(10, 2))
    note = Column(Text)
    staff_id = Column(Integer, ForeignKey("staff.id"))
    occurred_at = Column(DateTime(timezone=True), default=now_utc)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    product = relationship("Product")
    staff = relationship("Staff")

    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('restock','waste','missing','adjustment','return')",
            name="stock_movements_type_check",
        ),
    )


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String)                    # optional contact number
    created_at = Column(DateTime(timezone=True), default=now_utc)


COACH_TYPES = [("affiliate", "Affiliate"), ("full_time", "Full Time")]


class Coach(Base):
    __tablename__ = "coaches"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    coach_type = Column(String, nullable=False, default="affiliate")
    affiliate_fee = Column(Numeric(10, 2), default=0)   # monthly, affiliates only
    start_date = Column(Date)
    next_billing = Column(Date)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)


class Member(Base):
    __tablename__ = "members"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    coach_id = Column(Integer, ForeignKey("coaches.id"))
    corkage_rate = Column(Numeric(10, 2), default=3000)  # monthly corkage per client
    start_date = Column(Date)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    coach = relationship("Coach")


# ---- Invoices (Transactions → Invoices) ----
class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    number = Column(String, unique=True, nullable=False)      # INV-0001
    bill_to_type = Column(String, default="other")            # coach / customer / other
    coach_id = Column(Integer, ForeignKey("coaches.id"))
    customer_id = Column(Integer, ForeignKey("customers.id"))
    bill_to_name = Column(String, nullable=False)             # snapshot name
    issue_date = Column(Date)
    due_date = Column(Date)
    period = Column(String)                                   # e.g. "August 2026"
    note = Column(Text)
    is_void = Column(Boolean, nullable=False, default=False)
    staff_id = Column(Integer, ForeignKey("staff.id"))
    created_at = Column(DateTime(timezone=True), default=now_utc)

    items = relationship("InvoiceItem", cascade="all, delete-orphan", backref="invoice")
    ipayments = relationship("InvoicePayment", cascade="all, delete-orphan", backref="invoice")
    coach = relationship("Coach")
    customer = relationship("Customer")

    @property
    def total(self):
        return sum(float(i.amount or 0) for i in self.items)

    @property
    def paid(self):
        return sum(float(p.amount or 0) for p in self.ipayments)

    @property
    def balance(self):
        return self.total - self.paid

    @property
    def status(self):
        if self.is_void:
            return "void"
        if self.total > 0 and self.balance <= 0.005:
            return "paid"
        if self.paid > 0.005:
            return "partial"
        return "unpaid"


class InvoiceItem(Base):
    __tablename__ = "invoice_items"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    description = Column(String, nullable=False)
    qty = Column(Numeric(10, 2), default=1)
    rate = Column(Numeric(10, 2), default=0)
    amount = Column(Numeric(10, 2), default=0)


class InvoicePayment(Base):
    __tablename__ = "invoice_payments"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    method = Column(String)
    note = Column(Text)
    paid_at = Column(DateTime(timezone=True), default=now_utc)
    staff_id = Column(Integer, ForeignKey("staff.id"))


class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True)
    sold_at = Column(DateTime(timezone=True), default=now_utc)
    staff_id = Column(Integer, ForeignKey("staff.id"))
    customer_id = Column(Integer, ForeignKey("customers.id"))  # optional
    is_credit = Column(Boolean, nullable=False, default=False)  # unpaid / on credit
    payment_method = Column(String)
    proof = Column(LargeBinary)              # proof-of-payment image (cash/bank sales)
    proof_mime = Column(String)
    pricing_group_id = Column(Integer, ForeignKey("pricing_groups.id", ondelete="SET NULL"))  # tier applied
    discount_person_id = Column(Integer, ForeignKey("staff.id", ondelete="SET NULL"))  # whose code
    discounted_qty = Column(Integer, nullable=False, default=0)   # item-units that got the tier discount
    note = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    staff = relationship("Staff", foreign_keys=[staff_id])
    discount_person = relationship("Staff", foreign_keys=[discount_person_id])
    customer = relationship("Customer")
    items = relationship("SaleItem", back_populates="sale", cascade="all, delete-orphan")

    @property
    def total(self):
        return sum(float(i.quantity) * float(i.unit_price) for i in self.items)


class SaleItem(Base):
    __tablename__ = "sale_items"
    id = Column(Integer, primary_key=True)
    sale_id = Column(Integer, ForeignKey("sales.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(10, 2), nullable=False)
    cost_price = Column(Numeric(10, 2))

    sale = relationship("Sale", back_populates="items")
    product = relationship("Product")


class Order(Base):
    """A customer self-checkout order placed from the public /order page."""
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    number = Column(String, unique=True, nullable=False)       # ORD-0001
    customer_name = Column(String, nullable=False)
    customer_phone = Column(String)
    payment_method = Column(String, nullable=False)            # cash | bank
    proof = Column(LargeBinary)                                # payment screenshot (bank)
    proof_mime = Column(String)
    amount = Column(Numeric(10, 2), nullable=False, default=0)  # snapshot total
    status = Column(String, nullable=False, default="pending")  # pending|confirmed|rejected
    # Automated screenshot checks (best-effort OCR; staff still confirm)
    check_amount_ok = Column(Boolean)          # True/False/None(unknown)
    check_detected_amount = Column(Numeric(10, 2))
    check_date_ok = Column(Boolean)
    check_detected_date = Column(String)
    check_note = Column(Text)
    staff_id = Column(Integer, ForeignKey("staff.id"))         # who confirmed/rejected
    sale_id = Column(Integer, ForeignKey("sales.id", ondelete="SET NULL"))  # created on confirm
    created_at = Column(DateTime(timezone=True), default=now_utc)
    decided_at = Column(DateTime(timezone=True))

    items = relationship("OrderItem", cascade="all, delete-orphan", backref="order")

    @property
    def total(self):
        return sum(float(i.qty) * float(i.unit_price) for i in self.items)


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"))
    name = Column(String, nullable=False)      # snapshot name
    qty = Column(Integer, nullable=False, default=1)
    unit_price = Column(Numeric(10, 2), nullable=False, default=0)


class PaymentSetting(Base):
    """Singleton (id=1): bank details + payment QR + logo shown on the customer page."""
    __tablename__ = "payment_settings"
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


class DiscountCode(Base):
    """A per-person code (employee or affiliate) that unlocks a pricing tier."""
    __tablename__ = "discount_codes"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)          # stored uppercase
    holder_name = Column(String, nullable=False)               # the employee / affiliate
    group_id = Column(Integer, ForeignKey("pricing_groups.id", ondelete="CASCADE"), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    group = relationship("PricingGroup")


class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    note = Column(Text)
    method = Column(String)  # cash / gcash / etc.
    screenshot = Column(LargeBinary)         # proof-of-payment image bytes
    screenshot_mime = Column(String)         # e.g. image/jpeg
    paid_at = Column(DateTime(timezone=True), default=now_utc)
    staff_id = Column(Integer, ForeignKey("staff.id"))

    customer = relationship("Customer")
    staff = relationship("Staff")
