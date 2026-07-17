from datetime import datetime, timezone
from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer,
    Numeric, String, Text,
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


class Staff(Base):
    __tablename__ = "staff"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)  # login handle
    name = Column(String, nullable=False)                   # display name
    role = Column(String, nullable=False, default="staff")
    pin_hash = Column(String, nullable=False)
    pin_salt = Column(String, nullable=False)
    permissions = Column(Text, nullable=False, default="")  # comma-separated keys
    phone = Column(String)  # for WhatsApp channel later
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        CheckConstraint("role IN ('admin','staff')", name="staff_role_check"),
    )


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    sku = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    unit = Column(String, nullable=False, default="each")
    selling_price = Column(Numeric(10, 2), nullable=False)
    cost_price = Column(Numeric(10, 2))
    reorder_point = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    __table_args__ = (
        CheckConstraint(
            "category IN ('Food&Beverage','Merchandise')", name="products_category_check"
        ),
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


class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True)
    sold_at = Column(DateTime(timezone=True), default=now_utc)
    staff_id = Column(Integer, ForeignKey("staff.id"))
    payment_method = Column(String)
    note = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now_utc)

    staff = relationship("Staff")
    items = relationship("SaleItem", back_populates="sale", cascade="all, delete-orphan")


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
