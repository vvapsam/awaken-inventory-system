import csv
import io
import os
import secrets
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
from sqlalchemy import func, text, or_
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_staff, hash_pin, verify_pin
from .db import Base, engine, get_db
from .models import (
    CATEGORIES, MOVEMENT_TYPES, PAYMENT_METHODS, PERMISSION_KEYS,
    MODULES, ACTIONS, ACCESS_DEFS, ADMIN_AREA_DEFS, RECEIVE_TYPES, ADJUST_TYPES,
    RACE_STATUS_LABELS, RACE_STATUS_MANUAL, race_status, h12, has_race,
    DEFAULT_STAFF_PERMS, ROLES, UNITS, can, can_any, perm_set, module_for_type,
    Product, Staff,
    PricingGroup, PricingGroupItem, PRICING_KINDS, PERSON_TYPES,
    ENTITY_TYPES, DISCOUNT_TYPES, Role,
    Transaction, TransactionItem,
    TRANSACTION_TYPES, TX_CASH_SALE, TX_ORDER, TX_INVOICE, TX_PAYMENT, TX_INVENTORY,
    KioskPlan, KIOSK_PLAN_DEFAULTS, KIOSK_WALKIN_DEFAULTS, KIOSK_WALKIN,
    KIOSK_HYROX_RATES,
    HyroxGroup, HYROX_GROUP_DEFAULTS, HYROX_COACH_DEFAULTS,
    Waiver,
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
app = FastAPI(title="AWAKEN System")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", "dev-insecure-change-me"),
    max_age=60 * 60 * 12,  # 12h sessions
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["peso"] = lambda v: "₱{:,.2f}".format(float(v or 0))
# Stored instants are UTC; every screen reads in gym time. `local` for showing
# a moment, `local_dt` for pre-filling a datetime-local input — the browser
# treats that value as a wall clock, so it has to be handed one.
from .models import to_local as _to_local
templates.env.filters["local"] = _to_local
templates.env.filters["local_dt"] = lambda d: (
    _to_local(d).strftime("%Y-%m-%dT%H:%M") if d else "")
#: A moment written the way a person says it: "11 August, 8:08 PM". `local`
#: hands back a datetime and Jinja prints its repr, which is fine beside a
#: field label and wrong in the middle of a sentence.
templates.env.filters["when"] = lambda d: (
    _to_local(d).strftime("%d %B, %I:%M %p").replace(" 0", " ") if d else "")


def _asset_version() -> str:
    """A cache-busting stamp for /static, from the newest file's mtime.

    StaticFiles sends no Cache-Control, so browsers apply heuristic caching and
    can serve a stylesheet from before the last deploy — which renders the app
    with old rules and new markup. Versioning the URL makes a deploy a new URL.
    """
    newest = 0.0
    static_dir = os.path.join(BASE_DIR, "static")
    for name in os.listdir(static_dir):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(static_dir, name)))
        except OSError:
            continue
    return str(int(newest))


#: 1.50 -> "1.5", 2.00 -> "2". Hours read as hours, not as money that lost
#: its currency symbol.
def _trim0(v):
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return v
    return ("%.2f" % f).rstrip("0").rstrip(".") or "0"


templates.env.filters["trim0"] = _trim0
templates.env.globals["ASSET_V"] = _asset_version()
templates.env.globals["can"] = can
templates.env.globals["can_any"] = can_any
# The race status is derived, so every template that shows it must ask the one
# function rather than each working it out again.
templates.env.globals["race_status"] = race_status
templates.env.globals["RACE_STATUS_LABELS"] = RACE_STATUS_LABELS
templates.env.globals["RACE_STATUS_MANUAL"] = RACE_STATUS_MANUAL
# Heat times are stored 24-hour because that is what sorts. Nothing on a
# race floor is read that way, and the admin now shows them too.
templates.env.globals["h12"] = h12
templates.env.globals["has_race"] = has_race
from .models import wants_reels as _wants_reels
templates.env.globals["wants_reels"] = _wants_reels
from .countries import COUNTRIES, country_code, country_name, flag
templates.env.globals["COUNTRIES"] = COUNTRIES
from .models import SEXES as _SEXES
templates.env.globals["SEXES"] = _SEXES
from .models import CATEGORIES as _CATEGORIES
templates.env.globals["CATEGORIES"] = _CATEGORIES
from .models import CATEGORY_LABELS as _CATEGORY_LABELS
templates.env.globals["CATEGORY_LABELS"] = _CATEGORY_LABELS
templates.env.globals["country_code"] = country_code
templates.env.globals["country_name"] = country_name
templates.env.globals["flag"] = flag

# Mobile PWA (additive: new routes only, existing desktop pages untouched)
from .mobile import router as mobile_router  # noqa: E402
app.include_router(mobile_router)

# Self-checkout: public /order page + staff order queue
from .order import router as order_router  # noqa: E402
app.include_router(order_router)

# Public liability waiver page + staff review list
from .waiver import router as waiver_router  # noqa: E402
app.include_router(waiver_router)

# Public kiosk flows (Walk-in day pass / Sign-up membership) + admin plans
from .kiosk import router as kiosk_router  # noqa: E402
app.include_router(kiosk_router)

# HYROX relay: coach timer app + live scoreboard API
from .hyrox import router as hyrox_router  # noqa: E402
app.include_router(hyrox_router)


def _slugify(s):
    return "".join(c for c in (s or "").lower() if c.isalnum()) or "user"


def _fix_typed_times():
    """Move times typed under the old rule onto the clock they always meant.

    A date somebody typed used to be saved with a UTC label stuck on a Manila
    wall-clock reading, so "6 PM" was stored as 6 PM UTC and every deadline
    built on it fired eight hours late. Storage is real instants now. This
    walks the rows written before that and re-reads each one as gym time,
    which is what the person meant when they typed it.

    Done in Python rather than as one SQL interval so it stays correct for a
    timezone that observes daylight saving — the offset is not a constant
    everywhere, even though it is here.
    """
    from .models import Event, from_local
    with engine.begin() as conn:
        stale = conn.execute(text(
            "SELECT id, starts_at, confirm_by FROM events "
            "WHERE NOT tz_fixed")).fetchall()
        for row in stale:
            vals = {"id": row[0]}
            for col, raw in (("starts_at", row[1]), ("confirm_by", row[2])):
                # Drop the label that was never true, then re-read the reading.
                vals[col] = from_local(raw.replace(tzinfo=None)) if raw else None
            conn.execute(text(
                "UPDATE events SET starts_at = :starts_at, "
                "confirm_by = :confirm_by, tz_fixed = TRUE WHERE id = :id"), vals)
        if stale:
            print("corrected the stored clock on %d event(s)" % len(stale))
        # From here on every row is written as a real instant already.
        conn.execute(text(
            "ALTER TABLE events ALTER COLUMN tz_fixed SET DEFAULT TRUE"))


@app.on_event("startup")
def startup():
    # The `staff` table was renamed to `entity`. Do it BEFORE create_all so
    # create_all doesn't make a fresh empty `entity` table (which would block the
    # rename). Guarded so a fresh DB (no staff table) just creates `entity`.
    with engine.begin() as conn:
        conn.execute(text(
            "DO $$ BEGIN IF to_regclass('public.staff') IS NOT NULL "
            "AND to_regclass('public.entity') IS NULL THEN "
            "ALTER TABLE staff RENAME TO entity; END IF; END $$;"))
        # `payment_settings` was renamed to `company_info`. Same rule: rename
        # BEFORE create_all so it doesn't create an empty `company_info`.
        conn.execute(text(
            "DO $$ BEGIN IF to_regclass('public.payment_settings') IS NOT NULL "
            "AND to_regclass('public.company_info') IS NULL THEN "
            "ALTER TABLE payment_settings RENAME TO company_info; END IF; END $$;"))
    Base.metadata.create_all(bind=engine)
    # Lightweight migrations for databases created before these columns existed.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS permissions TEXT NOT NULL DEFAULT ''"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS username TEXT"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.sales') IS NOT NULL THEN ALTER TABLE sales ADD COLUMN IF NOT EXISTS customer_id INTEGER REFERENCES customers(id); END IF; END $$;"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.sales') IS NOT NULL THEN ALTER TABLE sales ADD COLUMN IF NOT EXISTS is_credit BOOLEAN NOT NULL DEFAULT FALSE; END IF; END $$;"))
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier VARCHAR"))
        conn.execute(text("ALTER TABLE products DROP CONSTRAINT IF EXISTS products_category_check"))
        conn.execute(text("ALTER TABLE products ALTER COLUMN category DROP NOT NULL"))
        # Mobile PWA additions
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS image BYTEA"))
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS image_mime VARCHAR"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.customers') IS NOT NULL THEN ALTER TABLE customers ADD COLUMN IF NOT EXISTS phone VARCHAR; END IF; END $$;"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.sales') IS NOT NULL THEN ALTER TABLE sales ADD COLUMN IF NOT EXISTS proof BYTEA; END IF; END $$;"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.sales') IS NOT NULL THEN ALTER TABLE sales ADD COLUMN IF NOT EXISTS proof_mime VARCHAR; END IF; END $$;"))
        conn.execute(text("ALTER TABLE company_info ADD COLUMN IF NOT EXISTS logo BYTEA"))
        conn.execute(text("ALTER TABLE company_info ADD COLUMN IF NOT EXISTS logo_mime VARCHAR"))
        conn.execute(text("ALTER TABLE company_info ADD COLUMN IF NOT EXISTS waiver_key VARCHAR"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.waivers') IS NOT NULL THEN "
                          "ALTER TABLE waivers ADD COLUMN IF NOT EXISTS referral VARCHAR; "
                          "ALTER TABLE waivers ADD COLUMN IF NOT EXISTS emergency_name VARCHAR; "
                          "ALTER TABLE waivers ADD COLUMN IF NOT EXISTS emergency_phone VARCHAR; "
                          "ALTER TABLE waivers ADD COLUMN IF NOT EXISTS ip VARCHAR; END IF; END $$;"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.waivers') IS NOT NULL THEN "
                          "ALTER TABLE waivers ADD COLUMN IF NOT EXISTS customer_id INTEGER "
                          "REFERENCES entity(id) ON DELETE SET NULL; END IF; END $$;"))
        # Customer profile fields (editable customer form + list columns).
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS first_name VARCHAR"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS last_name VARCHAR"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS email VARCHAR"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS emergency_name VARCHAR"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS emergency_phone VARCHAR"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS notes TEXT"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.sales') IS NOT NULL THEN ALTER TABLE sales ADD COLUMN IF NOT EXISTS pricing_group_id INTEGER REFERENCES pricing_groups(id) ON DELETE SET NULL; END IF; END $$;"))
        # Kiosk walk-in activities (Open Gym / Private Coaching / HYROX matrix) —
        # kiosk_plans already exists (seeded 23 Jul), so these need explicit ALTERs.
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.kiosk_plans') IS NOT NULL THEN "
                          "ALTER TABLE kiosk_plans ADD COLUMN IF NOT EXISTS activity VARCHAR; "
                          "ALTER TABLE kiosk_plans ADD COLUMN IF NOT EXISTS coached BOOLEAN; "
                          "ALTER TABLE kiosk_plans ADD COLUMN IF NOT EXISTS doubles BOOLEAN; END IF; END $$;"))
        conn.execute(text("ALTER TABLE pricing_groups ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'employee'"))
        conn.execute(text("ALTER TABLE pricing_groups ADD COLUMN IF NOT EXISTS round_up BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE pricing_groups ADD COLUMN IF NOT EXISTS daily_item_limit INTEGER"))
        # Price levels v2: explicit per-item price + entity's assigned level.
        conn.execute(text("ALTER TABLE pricing_group_items ADD COLUMN IF NOT EXISTS price NUMERIC(10,2)"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS pricing_group_id INTEGER REFERENCES pricing_groups(id) ON DELETE SET NULL"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.sales') IS NOT NULL THEN ALTER TABLE sales ADD COLUMN IF NOT EXISTS discounted_qty INTEGER NOT NULL DEFAULT 0; END IF; END $$;"))
        # Unified people: staff table also holds employees/affiliates (may have no login)
        conn.execute(text("ALTER TABLE entity ALTER COLUMN username DROP NOT NULL"))
        conn.execute(text("ALTER TABLE entity ALTER COLUMN pin_hash DROP NOT NULL"))
        conn.execute(text("ALTER TABLE entity ALTER COLUMN pin_salt DROP NOT NULL"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS person_type VARCHAR"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS discount_code VARCHAR"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS entity_discount_code_uq ON entity (discount_code)"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS has_access BOOLEAN NOT NULL DEFAULT TRUE"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.sales') IS NOT NULL THEN ALTER TABLE sales ADD COLUMN IF NOT EXISTS discount_person_id INTEGER REFERENCES entity(id) ON DELETE SET NULL; END IF; END $$;"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL"))
        # Coaches merged into the entity table: affiliate/coach billing lives on staff.
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS affiliate_fee NUMERIC(10,2)"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS start_date DATE"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS next_billing DATE"))
        # Members merged into the entity table: corkage members carry a monthly rate
        # and point at their affiliate. These columns are new, so add them to the
        # pre-existing (renamed-from-staff) entity table before any query touches them.
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS corkage_rate NUMERIC(10,2)"))
        conn.execute(text("ALTER TABLE entity ADD COLUMN IF NOT EXISTS affiliate_id INTEGER REFERENCES entity(id) ON DELETE SET NULL"))
        # Stock movements merged into transactions as an 'inventory_adjustment' type;
        # subtype holds the movement kind (restock/waste/missing/adjustment).
        conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS subtype VARCHAR"))
        # members.coach_id / invoices.coach_id now point at staff(id). Drop the old
        # FKs to coaches so we can remap the values in the data migration below.
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.members') IS NOT NULL THEN ALTER TABLE members DROP CONSTRAINT IF EXISTS members_coach_id_fkey; END IF; END $$;"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.invoices') IS NOT NULL THEN ALTER TABLE invoices DROP CONSTRAINT IF EXISTS invoices_coach_id_fkey; END IF; END $$;"))
        # Only touch the legacy coaches table if it still exists.
        conn.execute(text(
            "DO $$ BEGIN IF to_regclass('public.coaches') IS NOT NULL THEN "
            "ALTER TABLE coaches ADD COLUMN IF NOT EXISTS staff_id INTEGER; END IF; END $$;"))
        # Unified transactions: markers so sales/orders/invoices fold in once.
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.sales') IS NOT NULL THEN ALTER TABLE sales ADD COLUMN IF NOT EXISTS tx_id INTEGER; END IF; END $$;"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.orders') IS NOT NULL THEN ALTER TABLE orders ADD COLUMN IF NOT EXISTS tx_id INTEGER; END IF; END $$;"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.invoices') IS NOT NULL THEN ALTER TABLE invoices ADD COLUMN IF NOT EXISTS tx_id INTEGER; END IF; END $$;"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.payments') IS NOT NULL THEN ALTER TABLE payments ADD COLUMN IF NOT EXISTS tx_id INTEGER; END IF; END $$;"))
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.stock_movements') IS NOT NULL THEN ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS tx_id INTEGER; END IF; END $$;"))
        # HYROX relay: fixed gun-start schedule + Wallballs finish stamp + coach name.
        conn.execute(text("DO $$ BEGIN IF to_regclass('public.hyrox_groups') IS NOT NULL THEN "
                          "ALTER TABLE hyrox_groups ADD COLUMN IF NOT EXISTS start_at TIMESTAMPTZ; "
                          "ALTER TABLE hyrox_groups ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ; "
                          "ALTER TABLE hyrox_groups ADD COLUMN IF NOT EXISTS coach VARCHAR; END IF; END $$;"))
    db = next(get_db())
    try:
        # Backfill usernames only for people WITH system access who are missing one.
        taken = set(u for (u,) in db.query(Staff.username).all() if u)
        for st in db.query(Staff).filter(
                Staff.has_access == True,  # noqa: E712
                (Staff.username == None) | (Staff.username == "")).all():  # noqa: E711
            base = _slugify(st.name)
            u, i = base, 1
            while u in taken:
                i += 1
                u = f"{base}{i}"
            st.username = u
            taken.add(u)
        db.commit()
        with engine.begin() as conn:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS entity_username_uq ON entity (username)"))
        # Seed the two built-in roles (Admin = full, Staff = default perms).
        admin_role = db.query(Role).filter(Role.name == "Admin").first()
        if not admin_role:
            admin_role = Role(name="Admin", is_admin=True, is_system=True, permissions="")
            db.add(admin_role)
        staff_role = db.query(Role).filter(Role.name == "Staff").first()
        if not staff_role:
            staff_role = Role(name="Staff", is_admin=False, is_system=True,
                              permissions=",".join(DEFAULT_STAFF_PERMS))
            db.add(staff_role)
        # Coach: the race app and nothing else. Seeded rather than left to be
        # built by hand, because "which permissions does a coach need" is a
        # question with one right answer and no reason to ask it twice.
        coach_role = db.query(Role).filter(Role.name == "Coach").first()
        if not coach_role:
            db.add(Role(name="Coach", is_admin=False, is_system=True,
                        permissions="coach_race"))
        db.commit()
        # Bootstrap a first admin if none exists.
        if not db.query(Staff).filter(Staff.role == "admin").first():
            pin = os.environ.get("ADMIN_INITIAL_PIN", "123456")
            h, s = hash_pin(pin)
            db.add(Staff(username="admin", name="Admin", role="admin",
                         role_id=admin_role.id, pin_hash=h, pin_salt=s, permissions=""))
            db.commit()
        # Backfill role_id on any access people missing it (admin→Admin, else Staff).
        for st in db.query(Staff).filter(Staff.role_id == None).all():  # noqa: E711
            st.role_id = admin_role.id if st.role == "admin" else staff_role.id
        db.commit()
        # Seed the kiosk membership plans once, if none exist.
        if not db.query(KioskPlan.id).filter(KioskPlan.kind == "membership").first():
            for d in KIOSK_PLAN_DEFAULTS:
                db.add(KioskPlan(**d))
            db.commit()
        # Seed the walk-in activities once (Open Gym / Private / HYROX matrix).
        if not db.query(KioskPlan.id).filter(KioskPlan.kind == KIOSK_WALKIN).first():
            for d in KIOSK_WALKIN_DEFAULTS:
                db.add(KioskPlan(**d))
            db.commit()
        # Backfill HYROX rates onto rows first seeded at ₱0 (before rates were known,
        # 23 Jul). Runs only while the whole grid is still unpriced, so it never
        # clobbers an owner's later edits.
        hy = db.query(KioskPlan).filter(KioskPlan.kind == KIOSK_WALKIN,
                                        KioskPlan.activity == "hyrox").all()
        if hy and all(float(p.price or 0) == 0 for p in hy):
            for p in hy:
                p.price = KIOSK_HYROX_RATES[(bool(p.coached), bool(p.doubles))]
            db.commit()
        # Seed the HYROX relay groups once.
        if not db.query(HyroxGroup.id).first():
            for d in HYROX_GROUP_DEFAULTS:
                db.add(HyroxGroup(**d))
            db.commit()
        # Backfill coach names onto existing groups that don't have one yet.
        _cbf = False
        for g in db.query(HyroxGroup).all():
            if not g.coach:
                nm = HYROX_COACH_DEFAULTS.get((g.name, g.tag))
                if nm:
                    g.coach = nm
                    _cbf = True
        if _cbf:
            db.commit()
        # Give the relay a default 15-min-interval start schedule (upcoming 5:00 AM)
        # so the board shows start times out of the box; admin can re-set the date/time.
        from .hyrox import ensure_default_schedule
        ensure_default_schedule(db)
        # Backfill customer first/last/email: from a linked waiver if present, else
        # split the stored name. Runs only for customers missing a first_name.
        _need = (db.query(Staff)
                 .filter(Staff.person_type == "customer", Staff.first_name.is_(None)).all())
        if _need:
            _wmap = {}
            for _w in (db.query(Waiver).filter(Waiver.customer_id.isnot(None))
                       .order_by(Waiver.signed_at.asc()).all()):
                _wmap[_w.customer_id] = _w
            for _c in _need:
                _w = _wmap.get(_c.id)
                if _w:
                    _c.first_name = _w.first_name or ""
                    _c.last_name = _w.last_name or ""
                    _c.email = _c.email or _w.email
                else:
                    _p = (_c.name or "").split(" ")
                    _c.first_name = _p[0] if _p else ""
                    _c.last_name = " ".join(_p[1:])
            db.commit()
        # One-time migration: fold legacy discount_codes into the people table.
        # Each code becomes a non-access person (Employee/Affiliate) carrying the code.
        # Read via raw SQL so the ORM model can be removed and the table dropped.
        # Guard with to_regclass — a missing table would abort the session txn.
        if db.execute(text("SELECT to_regclass('public.discount_codes')")).scalar():
            legacy = db.execute(text(
                "SELECT dc.code, dc.holder_name, dc.is_active, pg.kind "
                "FROM discount_codes dc LEFT JOIN pricing_groups pg ON pg.id = dc.group_id"
            )).fetchall()
        else:
            legacy = []
        if legacy:
            existing_codes = set(c for (c,) in db.query(Staff.discount_code).all() if c)
            for code, holder_name, is_active, kind in legacy:
                if not code or code in existing_codes:
                    continue
                db.add(Staff(
                    name=holder_name or code,
                    person_type=(kind or "employee"),
                    discount_code=code,
                    has_access=False,
                    is_active=bool(is_active),
                    permissions="",
                ))
                existing_codes.add(code)
            db.commit()
        # One-time migration: fold coaches into the entity table.
        #   affiliate coach -> entity type 'affiliate' (keeps fee/members/billing)
        #   full-time coach -> entity type 'coach'
        # Then remap members.coach_id and invoices.coach_id from coach ids to the
        # new staff ids. Idempotent via coaches.staff_id.
        if db.execute(text("SELECT to_regclass('public.coaches')")).scalar():
            unmigrated = db.execute(text(
                "SELECT id, name, coach_type, affiliate_fee, start_date, next_billing, "
                "is_active FROM coaches WHERE staff_id IS NULL"
            )).fetchall()
        else:
            unmigrated = []
        if unmigrated:
            mapping = {}
            for cid, name, coach_type, fee, sdate, nbill, active in unmigrated:
                is_aff = coach_type == "affiliate"
                ent = Staff(
                    name=name,
                    person_type=("affiliate" if is_aff else "coach"),
                    has_access=False,
                    is_active=bool(active),
                    permissions="",
                    affiliate_fee=(fee if is_aff else None),
                    start_date=sdate,
                    next_billing=(nbill if is_aff else None),
                )
                db.add(ent)
                db.flush()  # get ent.id
                mapping[cid] = ent.id
                db.execute(text("UPDATE coaches SET staff_id = :sid WHERE id = :cid"),
                           {"sid": ent.id, "cid": cid})
            db.commit()
            # remap references (read old coach id -> write new staff id)
            if db.execute(text("SELECT to_regclass('public.members')")).scalar():
                for old, new in mapping.items():
                    db.execute(text("UPDATE members SET coach_id = :new WHERE coach_id = :old"),
                               {"new": new, "old": old})
            if db.execute(text("SELECT to_regclass('public.invoices')")).scalar():
                for old, new in mapping.items():
                    db.execute(text("UPDATE invoices SET coach_id = :new "
                                    "WHERE coach_id = :old AND bill_to_type = 'coach'"),
                               {"new": new, "old": old})
            db.commit()
        # Cleanup: the legacy tables are now redundant — drop them (and the dead
        # sales.discount_code_id column) so the schema only keeps live tables.
        with engine.begin() as conn:
            conn.execute(text("DO $$ BEGIN IF to_regclass('public.sales') IS NOT NULL THEN ALTER TABLE sales DROP COLUMN IF EXISTS discount_code_id; END IF; END $$;"))
            conn.execute(text("DROP TABLE IF EXISTS discount_codes"))
            conn.execute(text("DROP TABLE IF EXISTS coaches"))
        # Fold sales, orders and invoices into the unified transactions table.
        _migrate_transactions(db)
        # Fold customers and members into the unified entity table.
        _migrate_entities(db)
        # Reviewer approval for non-Completed bookings. create_all() only makes
        # new tables, so an existing commission_bookings needs explicit ALTERs.
        with engine.begin() as conn:
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_bookings') IS NOT NULL THEN "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS approved BOOLEAN NOT NULL DEFAULT FALSE; "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS approved_by_id INTEGER "
                "REFERENCES entity(id) ON DELETE SET NULL; "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ; "
                "END IF; END $$;"))
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_runs') IS NOT NULL THEN "
                "ALTER TABLE commission_runs ADD COLUMN IF NOT EXISTS last_import_note TEXT; "
                "END IF; END $$;"))
            # Session rates gained a programme and a pack total, so Awaken
            # Force can sit alongside the Private Coaching packs.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_session_rates') IS NOT NULL THEN "
                "ALTER TABLE commission_session_rates ADD COLUMN IF NOT EXISTS program VARCHAR; "
                "ALTER TABLE commission_session_rates ADD COLUMN IF NOT EXISTS package_total NUMERIC(10,2); "
                "UPDATE commission_session_rates SET program = 'Private Coaching' "
                "WHERE program IS NULL; END IF; END $$;"))
            # Booking ref is the natural key for de-duplicating imports.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_bookings') IS NOT NULL THEN "
                "CREATE INDEX IF NOT EXISTS commission_bookings_ref_idx "
                "ON commission_bookings (booking_ref); END IF; END $$;"))
            # Statement links keep their history: replacing a coach's link
            # revokes the old row rather than overwriting it, so the URL in
            # last week's email can still say "a newer one was sent".
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_statement_links') "
                "IS NOT NULL THEN "
                "ALTER TABLE commission_statement_links "
                "  DROP CONSTRAINT IF EXISTS uq_statement_link_coach; "
                "END IF; END $$;"))
            # Which statuses pay without review became a setting, and the
            # answer is snapshotted per booking. Existing rows are back-filled
            # to Completed-only — the rule they were actually calculated under
            # — so no run that has already been read, signed off or paid moves
            # because of this upgrade. The new rule reaches a draft only when
            # someone imports again or presses Recalculate.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_bookings') IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM information_schema.columns "
                "                WHERE table_name = 'commission_bookings' "
                "                  AND column_name = 'pays_by_status') THEN "
                "ALTER TABLE commission_bookings ADD COLUMN "
                "  pays_by_status BOOLEAN NOT NULL DEFAULT FALSE; "
                "UPDATE commission_bookings SET pays_by_status = TRUE "
                " WHERE lower(btrim(booking_status)) = 'completed'; "
                "END IF; END $$;"))
            # Runs imported before the parsed_count fix stored zero, so their
            # header reads "438 of 0 rows". Every parsed row is still on the
            # table — dropped ones carry a reason — so the counts can be
            # rebuilt from it. Only touch runs that never got a count.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_bookings') IS NOT NULL "
                "AND to_regclass('public.commission_runs') IS NOT NULL THEN "
                "UPDATE commission_runs r SET "
                "  parsed_count = c.total, "
                "  kept_count = c.total - c.dropped, "
                "  dropped_count = c.dropped "
                "FROM (SELECT run_id, count(*) AS total, "
                "             count(dropped_reason) AS dropped "
                "        FROM commission_bookings GROUP BY run_id) c "
                "WHERE c.run_id = r.id AND coalesce(r.parsed_count, 0) = 0; "
                "END IF; END $$;"))
            # A rate typed by hand on one booking row.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_bookings') IS NOT NULL THEN "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  rate_manual BOOLEAN NOT NULL DEFAULT FALSE; "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  rate_manual_by_id INTEGER REFERENCES entity(id) ON DELETE SET NULL; "
                "END IF; END $$;"))
            # Overtime on a delegated session: an hourly rate on the delegator,
            # and the hours typed against one booking. Additive only — no
            # existing column moves, and a database that has never seen
            # overtime reads exactly as it did before.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_delegators') IS NOT NULL THEN "
                "ALTER TABLE commission_delegators ADD COLUMN IF NOT EXISTS "
                "  ot_rate NUMERIC(10,2) NOT NULL DEFAULT 0; "
                "END IF; END $$;"))
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_bookings') IS NOT NULL THEN "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  ot_hours NUMERIC(6,2); "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  ot_rate NUMERIC(10,2); "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  ot_charge NUMERIC(10,2); "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  ot_by_id INTEGER REFERENCES entity(id) ON DELETE SET NULL; "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  ot_at TIMESTAMPTZ; "
                "END IF; END $$;"))
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_charge_lines') "
                "IS NOT NULL THEN "
                "ALTER TABLE commission_charge_lines ADD COLUMN IF NOT EXISTS "
                "  kind VARCHAR; "
                "END IF; END $$;"))
            # commission_delegator_links is a new table, so create_all makes it.
            # Nothing to ALTER.
            # A delegator statement ages invoices and marks cancelled ones,
            # neither of which the charge row could answer before.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_charges') "
                "IS NOT NULL THEN "
                "ALTER TABLE commission_charges ADD COLUMN IF NOT EXISTS "
                "  period_start DATE; "
                "ALTER TABLE commission_charges ADD COLUMN IF NOT EXISTS "
                "  period_end DATE; "
                "ALTER TABLE commission_charges ADD COLUMN IF NOT EXISTS "
                "  note TEXT; "
                "ALTER TABLE commission_charges ADD COLUMN IF NOT EXISTS "
                "  issued_by_id INTEGER REFERENCES entity(id) ON DELETE SET NULL; "
                "ALTER TABLE commission_charges ADD COLUMN IF NOT EXISTS "
                "  voided_at TIMESTAMPTZ; "
                "ALTER TABLE commission_charges ADD COLUMN IF NOT EXISTS "
                "  voided_reason VARCHAR; "
                "END IF; END $$;"))
            # A statement is an open-item ledger: invoiced minus received. Any
            # invoice already flagged paid predates the payments table, so it
            # gets one entry standing in for the money that must have arrived —
            # otherwise the first statement anyone prints asks them for it
            # again. Idempotent through charge_id, which exists for exactly
            # this: it is provenance, never arithmetic.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_delegator_payments') "
                "IS NOT NULL AND to_regclass('public.commission_charges') "
                "IS NOT NULL THEN "
                "INSERT INTO commission_delegator_payments "
                "  (delegator_id, paid_on, amount, description, charge_id, created_at) "
                "SELECT c.delegator_id, "
                "       COALESCE(c.paid_at::date, c.created_at::date, CURRENT_DATE), "
                "       c.total, "
                "       'Payment received - ' || COALESCE(c.number, 'invoice'), "
                "       c.id, COALESCE(c.paid_at, c.created_at, now()) "
                "FROM commission_charges c "
                "WHERE c.status = 'paid' AND c.voided_at IS NULL "
                "  AND c.delegator_id IS NOT NULL AND COALESCE(c.total, 0) > 0 "
                "  AND NOT EXISTS (SELECT 1 FROM commission_delegator_payments p "
                "                  WHERE p.charge_id = c.id); "
                "END IF; END $$;"))
            # Invoices already on file predate period_start/period_end. Fill
            # them from the run they came from, once, so "what have we already
            # billed" can be answered by dates alone rather than by dates for
            # new invoices and a join for old ones.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_charges') "
                "IS NOT NULL AND to_regclass('public.commission_runs') "
                "IS NOT NULL THEN "
                "UPDATE commission_charges c SET "
                "  period_start = r.period_start, period_end = r.period_end "
                "FROM commission_runs r WHERE r.id = c.run_id "
                "  AND c.period_start IS NULL AND r.period_start IS NOT NULL; "
                "END IF; END $$;"))
            # The sponsor's logo, on the event rather than in the static
            # folder — a sponsor belongs to one event, and the next one should
            # be an upload rather than a deploy.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS sponsor_logo BYTEA; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS sponsor_logo_mime VARCHAR; "
                "END IF; END $$;"))
            # The confirmation clock, counted from each person's own invitation
            # rather than from one fixed date shared by everybody.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  confirm_hours INTEGER NOT NULL DEFAULT 48; "
                "END IF; END $$;"))
            # The advertised registration cut-off.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  signup_closes TIMESTAMPTZ; "
                "END IF; END $$;"))
            # How early the race app lets a coach open a heat. Nullable on
            # purpose: blank means "use the default", so an event nobody has
            # thought about behaves the same as one that has.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  heat_open_mins INTEGER; "
                # Nullable on purpose: NULL means "decide from the mode", so
                # every event already on the table keeps behaving as it did
                # without anybody having to go and tick anything.
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  reels_on BOOLEAN; "
                "END IF; END $$;"))
            # Room for the one-time correction below. The column defaults to
            # FALSE so that every row written under the old rule is picked up
            # exactly once; the default flips to TRUE once they have been.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  tz_fixed BOOLEAN NOT NULL DEFAULT FALSE; "
                "END IF; END $$;"))
            # A day without a clock time, for races whose heats are drawn later.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  time_tba BOOLEAN NOT NULL DEFAULT FALSE; "
                "END IF; END $$;"))
            # Open registration: a second way into an event, where the public
            # signs itself up and pays rather than being invited.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS mode VARCHAR NOT NULL DEFAULT 'invite'; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS signup_open BOOLEAN NOT NULL DEFAULT TRUE; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS external_url VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS external_label VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS external_note TEXT; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS tier_a_label VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS tier_a_price NUMERIC(10,2); "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS tier_b_label VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS tier_b_price NUMERIC(10,2); "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS pay_qr BYTEA; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS pay_qr_mime VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS pay_qr_caption VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS bank_details TEXT; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS pay_note VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS review_hours INTEGER NOT NULL DEFAULT 24; "
                "END IF; END $$;"))
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.event_participants') IS NOT NULL THEN "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS first_name VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS last_name VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS mobile VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS sex VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS tier VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS amount NUMERIC(10,2); "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS external_done_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS proof BYTEA; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS proof_mime VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS proof_ref VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS pay_status VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS reviewed_by_id INTEGER; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS review_note TEXT; "
                "END IF; END $$;"))
            # When the "post your Reel" email went out, kept apart from the
            # invitation because they are two different asks at two different
            # moments and each needs its own "who still needs this" list.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.event_participants') IS NOT NULL THEN "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  reel_email_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  waitlist BOOLEAN NOT NULL DEFAULT FALSE; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  confirm_due TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  arrived_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  pass_email_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  nudged_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  last_call_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  cancel_email_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  slot_no INTEGER; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  slot_time VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  slot_at TIMESTAMPTZ; "
                "END IF; END $$;"))
            # Start times handed out at the door: two waves and a cap on the
            # first. Nulls everywhere means the event does not use them.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS slot_a_time VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS slot_a_cap INTEGER; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS slot_b_time VARCHAR; "
                "END IF; END $$;"))
            # Reel submissions held shut by hand. A cancelled class still
            # has an end time in the diary, so without this the window
            # opens on its own and asks for a Reel of a class nobody ran.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  reels_paused BOOLEAN NOT NULL DEFAULT FALSE; "
                "END IF; END $$;"))
            # Which columns this event's participant table shows. Nullable on
            # purpose: NULL means "the defaults", which is how every existing
            # event keeps working and keeps picking up new default columns.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS cols TEXT; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS gone_cols TEXT; "
                "END IF; END $$;"))
            # The heat timetable: the shape of the day on the event, and which
            # heat each person is in on the participant. heat_first empty means
            # the event never opted in, so every existing event is untouched.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS heat_first VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS heat_last VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  heat_every INTEGER NOT NULL DEFAULT 10; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  heat_cap INTEGER NOT NULL DEFAULT 3; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  heat_arrive INTEGER NOT NULL DEFAULT 30; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS heat_token VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  heat_sent_at TIMESTAMPTZ; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  heat_link_at TIMESTAMPTZ; "
                # The leaderboard's own link. Separate from the timetable's:
                # they go to different people at different times, and a
                # timetable sent on Tuesday should not be revocable only by
                # taking the race-day board down with it.
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS board_token VARCHAR; "
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS "
                "  board_link_at TIMESTAMPTZ; "
                "END IF; END $$;"))
            # Unique separately: ADD COLUMN cannot carry it, and a partial
            # index keeps the many events with no link from colliding on NULL.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL THEN "
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_heat_token "
                "  ON events (heat_token) WHERE heat_token IS NOT NULL; "
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_board_token "
                "  ON events (board_token) WHERE board_token IS NOT NULL; "
                "END IF; END $$;"))
            # One live timetable per event. A partial index rather than a plain
            # unique: every event has many inactive plans and only ever one
            # active, and two live timetables is the one state with no honest
            # answer to "what time am I racing".
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.heat_plans') IS NOT NULL THEN "
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_heat_plan_active "
                "  ON heat_plans (event_id) WHERE is_active; "
                "END IF; END $$;"))
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.event_participants') "
                "IS NOT NULL THEN "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  heat_time VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  heat_email_at TIMESTAMPTZ; "
                # Backfilled true for anybody already holding a heat email, so
                # the first send after this deploy does not greet thirty-four
                # people who were told last week as though it were the first
                # time.
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  heat_told_before BOOLEAN NOT NULL DEFAULT FALSE; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  age INTEGER; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  patch VARCHAR; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  patch_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  race_status_set VARCHAR; "
                # NOT NULL with a default, so every row already on the table
                # is filled in by the ALTER itself. Everybody has a country,
                # and a board where half the flags are missing looks broken.
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  country VARCHAR NOT NULL DEFAULT 'PH'; "
                # Same shape, same reason. The category crosses gender rather
                # than replacing it, so a null here would put a fifth,
                # nameless column on the leaderboard - everybody starts Open
                # and gets moved up by hand.
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  category VARCHAR NOT NULL DEFAULT 'open'; "
                # Nullable on purpose: "we have never seen them go" and "they
                # went at 09:14" are the only two states, and a default would
                # invent a third.
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  review_opened_at TIMESTAMPTZ; "
                "UPDATE event_participants SET heat_told_before = TRUE "
                "  WHERE heat_email_at IS NOT NULL AND NOT heat_told_before; "
                "END IF; END $$;"))
            # And the event-level one, recovered from whoever is still holding
            # a stamp. On an event where everybody has since been moved, the
            # per-person backfill above finds nobody — this needs only one
            # survivor to know the event has told people before.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.events') IS NOT NULL "
                "AND to_regclass('public.event_participants') IS NOT NULL THEN "
                "UPDATE events SET heat_sent_at = s.last_sent FROM ("
                "  SELECT event_id, MAX(heat_email_at) AS last_sent "
                "  FROM event_participants WHERE heat_email_at IS NOT NULL "
                "  GROUP BY event_id) s "
                "WHERE s.event_id = events.id AND events.heat_sent_at IS NULL; "
                "END IF; END $$;"))
            # Who is coaching them, and when their last station closed.
            # event_stations and station_runs are new tables, so create_all
            # makes those — only the columns on an existing table need this.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.event_participants') "
                "IS NOT NULL THEN "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  coach_id INTEGER; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  grabbed_at TIMESTAMPTZ; "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  finished_at TIMESTAMPTZ; "
                "END IF; END $$;"))
            # When somebody last changed a participant row by hand. Only set
            # by the edit screen — see the note on the column.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.event_participants') IS NOT NULL THEN "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  edited_at TIMESTAMPTZ; "
                "END IF; END $$;"))
            # When an unpaid slot stops being held. Written by the last call
            # to pay and nothing else.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.event_participants') IS NOT NULL THEN "
                "ALTER TABLE event_participants ADD COLUMN IF NOT EXISTS "
                "  pay_due_at TIMESTAMPTZ; "
                "END IF; END $$;"))
            # Sessions struck out by hand: the export said they happened, they
            # didn't. Kept on the row rather than deleted so the exclusion is
            # visible and reversible.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_bookings') IS NOT NULL THEN "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  voided BOOLEAN NOT NULL DEFAULT FALSE; "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  void_reason VARCHAR; "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  voided_by_id INTEGER REFERENCES entity(id) ON DELETE SET NULL; "
                "ALTER TABLE commission_bookings ADD COLUMN IF NOT EXISTS "
                "  voided_at TIMESTAMPTZ; "
                "END IF; END $$;"))
            # Affiliate or employee, tagged on the coach for reporting. Seeded
            # from the linked person record where there is one; on this database
            # no coach is linked yet, so it starts empty and gets filled in on
            # the Coach rates screen.
            conn.execute(text(
                "DO $$ BEGIN IF to_regclass('public.commission_coach_rates') IS NOT NULL THEN "
                "ALTER TABLE commission_coach_rates ADD COLUMN IF NOT EXISTS "
                "  coach_type VARCHAR NOT NULL DEFAULT ''; "
                "UPDATE commission_coach_rates r SET coach_type = e.person_type "
                "  FROM entity e WHERE e.id = r.coach_id AND r.coach_type = '' "
                "    AND e.person_type IN ('employee', 'affiliate'); "
                "END IF; END $$;"))
            # Coach overrides moved from a comma-separated column with one
            # shared rate to a row per plan, each with its own basis and rate.
            # Copy first, then drop the old columns — leaving them would let a
            # deleted override come back on the next boot.
            conn.execute(text(
                "DO $$ BEGIN "
                "IF to_regclass('public.commission_coach_overrides') IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM information_schema.columns "
                "            WHERE table_name = 'commission_coach_rates' "
                "              AND column_name = 'override_plans') THEN "
                "  INSERT INTO commission_coach_overrides "
                "         (rate_id, plan, rate_type, rate_value, is_active) "
                "  SELECT DISTINCT r.id, initcap(btrim(p)), r.override_rate_type, "
                "         COALESCE(r.override_rate_value, 0), TRUE "
                "  FROM commission_coach_rates r, "
                "       unnest(string_to_array(r.override_plans, ',')) AS p "
                "  WHERE r.override_rate_type IS NOT NULL AND btrim(p) <> '' "
                "    AND NOT EXISTS (SELECT 1 FROM commission_coach_overrides x "
                "                    WHERE x.rate_id = r.id "
                "                      AND lower(x.plan) = lower(btrim(p))); "
                "  ALTER TABLE commission_coach_rates DROP COLUMN IF EXISTS override_plans; "
                "  ALTER TABLE commission_coach_rates DROP COLUMN IF EXISTS override_rate_type; "
                "  ALTER TABLE commission_coach_rates DROP COLUMN IF EXISTS override_rate_value; "
                "END IF; END $$;"))
        # Every events column exists by now, so the stored clock can be put
        # right. Runs after the ALTERs and only ever touches rows the tombstone
        # says have not been corrected.
        _fix_typed_times()
        # Seed commission rules (coach rates, delegators, settings). Idempotent.
        from . import commission_routes
        commission_routes.seed(db)
    finally:
        db.close()


def _migrate_entities(db):
    """Fold the legacy customers + members tables into the unified `entity` table
    (person_type customer / member), remap transactions.customer_id, then drop
    the old tables. Read via raw SQL so no ORM models are needed for them."""
    # customers -> entity(person_type='customer'), remap transactions.customer_id
    if _has_table(db, "customers"):
        # Drop the customer_id FK first so the remap can't transiently violate it.
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_customer_id_fkey"))
        cust_map = {}
        for r in db.execute(text(
                "SELECT id, name, phone FROM customers")).fetchall():
            e = Staff(name=r.name, person_type="customer", has_access=False,
                      role="staff", permissions="", phone=r.phone)
            db.add(e); db.flush()
            cust_map[r.id] = e.id
        db.commit()
        # remap each transaction's customer_id (old customer id -> new entity id);
        # single pass reading the original value, so id overlaps are safe.
        for tx in db.query(Transaction).filter(Transaction.customer_id != None).all():  # noqa: E711
            if tx.customer_id in cust_map:
                tx.customer_id = cust_map[tx.customer_id]
        db.commit()
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS customers CASCADE"))

    # members -> entity(person_type='member', affiliate_id + corkage_rate)
    if _has_table(db, "members"):
        for r in db.execute(text(
                "SELECT name, coach_id, corkage_rate, start_date, is_active "
                "FROM members")).fetchall():
            db.add(Staff(name=r.name, person_type="member", has_access=False, role="staff",
                         permissions="", affiliate_id=r.coach_id, corkage_rate=r.corkage_rate,
                         start_date=r.start_date, is_active=bool(r.is_active)))
        db.commit()
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS members CASCADE"))


def _dt(d, fallback=None):
    """Coerce a date/None into a datetime for occurred_at."""
    if isinstance(d, datetime):
        return d
    if isinstance(d, date):
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return fallback


def _has_table(db, tbl):
    return bool(db.execute(text("SELECT to_regclass(:n)"), {"n": "public." + tbl}).scalar())


def _migrate_transactions(db):
    """One-time fold of the legacy sales/orders/invoices/payments tables (+ their
    line items & invoice payments) into the unified transactions table. Reads via
    raw SQL so the old ORM models can be removed and the tables dropped afterwards.
    Idempotent via each source table's tx_id marker."""
    sale_tx = {}  # legacy sale.id -> transaction.id (for linking confirmed orders)

    # 1) cash sales
    if _has_table(db, "sales"):
        rows = db.execute(text(
            "SELECT id, sold_at, staff_id, customer_id, is_credit, payment_method, "
            "proof, proof_mime, pricing_group_id, discount_person_id, discounted_qty, "
            "note, created_at FROM sales WHERE tx_id IS NULL")).fetchall()
        for r in rows:
            tx = Transaction(
                type=TX_CASH_SALE, status=("credit" if r.is_credit else "paid"),
                occurred_at=r.sold_at, created_at=r.created_at or r.sold_at,
                staff_id=r.staff_id, customer_id=r.customer_id,
                payment_method=r.payment_method, is_credit=bool(r.is_credit),
                proof=r.proof, proof_mime=r.proof_mime,
                pricing_group_id=r.pricing_group_id, discount_person_id=r.discount_person_id,
                discounted_qty=r.discounted_qty or 0, note=r.note)
            db.add(tx); db.flush()
            for si in db.execute(text(
                    "SELECT si.product_id, si.quantity, si.unit_price, si.cost_price, p.name "
                    "FROM sale_items si LEFT JOIN products p ON p.id = si.product_id "
                    "WHERE si.sale_id = :sid"), {"sid": r.id}).fetchall():
                db.add(TransactionItem(transaction_id=tx.id, product_id=si.product_id,
                                       name=si.name or "Item", qty=si.quantity,
                                       unit_price=si.unit_price, cost_price=si.cost_price))
            db.execute(text("UPDATE sales SET tx_id = :t WHERE id = :s"), {"t": tx.id, "s": r.id})
            sale_tx[r.id] = tx.id
        db.commit()

    # 2) orders (link to the sale they became, if any)
    if _has_table(db, "orders"):
        rows = db.execute(text(
            "SELECT id, number, status, created_at, decided_at, staff_id, customer_name, "
            "customer_phone, payment_method, proof, proof_mime, amount, sale_id, "
            "check_amount_ok, check_detected_amount, check_date_ok, check_detected_date, "
            "check_note FROM orders WHERE tx_id IS NULL")).fetchall()
        for r in rows:
            tx = Transaction(
                type=TX_ORDER, number=r.number, status=r.status,
                occurred_at=r.created_at, created_at=r.created_at, decided_at=r.decided_at,
                staff_id=r.staff_id, customer_name=r.customer_name, customer_phone=r.customer_phone,
                payment_method=r.payment_method, proof=r.proof, proof_mime=r.proof_mime,
                amount_snapshot=r.amount,
                check_amount_ok=r.check_amount_ok, check_detected_amount=r.check_detected_amount,
                check_date_ok=r.check_date_ok, check_detected_date=r.check_detected_date,
                check_note=r.check_note, converted_id=sale_tx.get(r.sale_id))
            db.add(tx); db.flush()
            for oi in db.execute(text(
                    "SELECT product_id, name, qty, unit_price FROM order_items "
                    "WHERE order_id = :oid"), {"oid": r.id}).fetchall():
                db.add(TransactionItem(transaction_id=tx.id, product_id=oi.product_id,
                                       name=oi.name, qty=oi.qty, unit_price=oi.unit_price))
            db.execute(text("UPDATE orders SET tx_id = :t WHERE id = :o"), {"t": tx.id, "o": r.id})
        db.commit()

    # 3) invoices (+ items + payments)
    if _has_table(db, "invoices"):
        rows = db.execute(text(
            "SELECT id, number, is_void, issue_date, due_date, period, note, created_at, "
            "staff_id, customer_id, bill_to_name, bill_to_type, coach_id "
            "FROM invoices WHERE tx_id IS NULL")).fetchall()
        for r in rows:
            tx = Transaction(
                type=TX_INVOICE, number=r.number,
                status=("void" if r.is_void else "unpaid"),
                occurred_at=_dt(r.issue_date, r.created_at), created_at=r.created_at,
                staff_id=r.staff_id, customer_id=r.customer_id,
                customer_name=r.bill_to_name, bill_to_type=r.bill_to_type,
                coach_id=r.coach_id, issue_date=r.issue_date, due_date=r.due_date,
                period=r.period, is_void=bool(r.is_void), note=r.note)
            db.add(tx); db.flush()
            for it in db.execute(text(
                    "SELECT description, qty, rate FROM invoice_items WHERE invoice_id = :iid"),
                    {"iid": r.id}).fetchall():
                db.add(TransactionItem(transaction_id=tx.id, product_id=None,
                                       name=it.description, qty=it.qty, unit_price=it.rate))
            for pm in db.execute(text(
                    "SELECT amount, method, note, paid_at, staff_id FROM invoice_payments "
                    "WHERE invoice_id = :iid"), {"iid": r.id}).fetchall():
                pay = Transaction(
                    type=TX_PAYMENT, parent_id=tx.id, occurred_at=pm.paid_at,
                    created_at=pm.paid_at, staff_id=pm.staff_id, customer_id=r.customer_id,
                    customer_name=r.bill_to_name, payment_method=pm.method, note=pm.note,
                    status="paid")
                db.add(pay); db.flush()
                db.add(TransactionItem(transaction_id=pay.id, name="Invoice payment",
                                       qty=1, unit_price=pm.amount))
            db.execute(text("UPDATE invoices SET tx_id = :t WHERE id = :i"), {"t": tx.id, "i": r.id})
        db.commit()

    # 4) customer balance payments -> standalone payment transactions
    if _has_table(db, "payments"):
        for r in db.execute(text(
                "SELECT id, customer_id, amount, note, method, screenshot, screenshot_mime, "
                "paid_at, staff_id FROM payments WHERE tx_id IS NULL")).fetchall():
            pay = Transaction(
                type=TX_PAYMENT, occurred_at=r.paid_at, created_at=r.paid_at,
                staff_id=r.staff_id, customer_id=r.customer_id, payment_method=r.method,
                proof=r.screenshot, proof_mime=r.screenshot_mime, note=r.note, status="paid")
            db.add(pay); db.flush()
            db.add(TransactionItem(transaction_id=pay.id, name="Payment received",
                                   qty=1, unit_price=r.amount))
            db.execute(text("UPDATE payments SET tx_id = :t WHERE id = :p"), {"t": pay.id, "p": r.id})
        db.commit()

    # 5) stock_movements -> inventory_adjustment transactions (signed line-item qty)
    if _has_table(db, "stock_movements"):
        for r in db.execute(text(
                "SELECT id, product_id, movement_type, quantity, unit_cost, note, "
                "staff_id, occurred_at, created_at FROM stock_movements WHERE tx_id IS NULL")).fetchall():
            tx = Transaction(type=TX_INVENTORY, subtype=r.movement_type, status="done",
                             occurred_at=r.occurred_at, created_at=r.created_at or r.occurred_at,
                             staff_id=r.staff_id, note=r.note)
            db.add(tx); db.flush()
            pname = db.execute(text("SELECT name FROM products WHERE id = :p"),
                               {"p": r.product_id}).scalar()
            db.add(TransactionItem(transaction_id=tx.id, product_id=r.product_id,
                                   name=pname or "Item", qty=r.quantity, unit_price=r.unit_cost))
            db.execute(text("UPDATE stock_movements SET tx_id = :t WHERE id = :m"),
                       {"t": tx.id, "m": r.id})
        db.commit()

    # 6) drop the now-redundant legacy tables (children first for FK safety)
    with engine.begin() as conn:
        for tbl in ("sale_items", "sales", "order_items", "orders",
                    "invoice_items", "invoice_payments", "invoices", "payments",
                    "stock_movements"):
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl} CASCADE"))


# ---------- helpers ----------

def render(request, template, db, staff, **ctx):
    base = {"request": request, "staff": staff, "CATEGORIES": CATEGORIES,
            "UNITS": UNITS, "MOVEMENT_TYPES": MOVEMENT_TYPES,
            "PAYMENT_METHODS": PAYMENT_METHODS, "ROLES": ROLES,
            "MODULES": MODULES, "ACTIONS": ACTIONS, "ACCESS_DEFS": ACCESS_DEFS,
            "ADMIN_AREA_DEFS": ADMIN_AREA_DEFS,
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


def _tx_qty_map(db, tx_type, before=None):
    """product_id -> summed line-item qty for transactions of `tx_type`."""
    q = (db.query(TransactionItem.product_id, func.coalesce(func.sum(TransactionItem.qty), 0))
         .join(Transaction, Transaction.id == TransactionItem.transaction_id)
         .filter(Transaction.type == tx_type, TransactionItem.product_id != None))  # noqa: E711
    if before is not None:
        q = q.filter(Transaction.occurred_at < before)
    return dict(q.group_by(TransactionItem.product_id).all())


def _sold_qty_map(db, before=None):
    """product_id -> units sold via cash-sale transactions."""
    return _tx_qty_map(db, TX_CASH_SALE, before)


def _adjust_qty_map(db, before=None):
    """product_id -> net signed qty from inventory-adjustment transactions."""
    return _tx_qty_map(db, TX_INVENTORY, before)


def stock_levels(db):
    """on_hand per active product = signed inventory adjustments − units sold."""
    mov = _adjust_qty_map(db)
    sold = _sold_qty_map(db)
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

# Phones/tablets get sent to the mobile app (/m); desktops to the dashboard.
_MOBILE_UA_HINTS = ("mobi", "android", "iphone", "ipod", "ipad", "windows phone",
                    "blackberry", "webos", "opera mini", "iemobile", "silk")


def _is_mobile_ua(request):
    ua = (request.headers.get("user-agent") or "").lower()
    return any(h in ua for h in _MOBILE_UA_HINTS)


def _post_login_dest(request, staff=None):
    """Where signing in should land somebody.

    A coach with only the race permission would otherwise land on a screen
    full of things they cannot use — and nothing in the app links to the race
    app, so they would have to be told the address. Send them where their one
    job is.
    """
    if staff is not None and not can(staff, "manage_hyrox") \
            and staff.role != "admin" and can(staff, "coach_race"):
        return "/race"
    return "/m" if _is_mobile_ua(request) else "/dashboard"


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    # On the public payments subdomain (pay.awakengym.com), the root IS the
    # customer self-checkout menu — no staff login shown there.
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host.startswith("pay."):
        return templates.TemplateResponse("order.html", {"request": request})
    staff = current_staff(request, db)
    return RedirectResponse(
        _post_login_dest(request, staff) if staff else "/login",
        status_code=303)


@app.get("/board", response_class=HTMLResponse)
def hyrox_board(request: Request):
    # Public big-screen HYROX relay scoreboard. Currently shows sample data with
    # client-side ticking clocks; will be wired to live coach timings next.
    return templates.TemplateResponse("board.html", {"request": request})


@app.get("/welcome", response_class=HTMLResponse)
def welcome_hub(request: Request):
    # Public QR-landing hub: member scans one QR at the desk and picks an action.
    # Walk-in / Sign-up open directly (no key needed); "Buy drinks" → /order.
    return templates.TemplateResponse("welcome.html", {"request": request, "k": ""})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), pin: str = Form(...), db: Session = Depends(get_db)):
    uname = (username or "").strip().lower()
    staff = db.query(Staff).filter(func.lower(Staff.username) == uname,
                                   Staff.is_active, Staff.has_access).first()
    if not staff or not staff.pin_hash or not verify_pin(pin, staff.pin_hash, staff.pin_salt):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Wrong username or PIN."}
        )
    request.session["staff_id"] = staff.id
    return RedirectResponse(_post_login_dest(request, staff), status_code=303)


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
        db.query(Transaction.occurred_at, TransactionItem.qty, TransactionItem.unit_price)
        .join(TransactionItem, TransactionItem.transaction_id == Transaction.id)
        .filter(Transaction.type == TX_CASH_SALE,
                Transaction.occurred_at >= start_u, Transaction.occurred_at < end_u).all()
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
    existing = (db.query(Staff).filter(Staff.person_type == "customer",
                func.lower(Staff.name) == name.lower()).first())
    if existing:
        return existing
    c = Staff(name=name, person_type="customer", has_access=False, role="staff", permissions="")
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
    p = db.get(Product, product_id)
    tx = Transaction(type=TX_INVENTORY, subtype=movement_type, status="done",
                     staff_id=staff.id, note=note or None)
    db.add(tx); db.flush()
    db.add(TransactionItem(transaction_id=tx.id, product_id=product_id,
                           name=(p.name if p else "Item"), qty=q, unit_price=uc))
    db.commit()
    return RedirectResponse("/stock", status_code=303)


@app.get("/movement/{mid}/edit", response_class=HTMLResponse)
def movement_edit(request: Request, mid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    m = db.get(Transaction, mid)
    if not m or m.type != TX_INVENTORY:
        return RedirectResponse("/records", status_code=303)
    if not can(staff, f"{module_for_type(m.movement_type)}.edit"):
        return RedirectResponse("/records", status_code=303)
    tz = _tz()
    local = m.occurred_at.astimezone(tz) if m.occurred_at else datetime.now(tz)
    dt_value = local.strftime("%Y-%m-%dT%H:%M")
    products = db.query(Product).order_by(Product.name).all()
    return render(request, "movement_edit.html", db, staff, m=m, error=None,
                  dt_value=dt_value, products=products)


@app.post("/movement/{mid}/edit")
def movement_update(request: Request, mid: int, quantity: int = Form(...),
                    direction: str = Form("add"), unit_cost: str = Form(""),
                    note: str = Form(""), occurred_at: str = Form(""),
                    product_id: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    m = db.get(Transaction, mid)
    if not m or m.type != TX_INVENTORY:
        return RedirectResponse("/records", status_code=303)
    if not can(staff, f"{module_for_type(m.movement_type)}.edit"):
        return RedirectResponse("/records", status_code=303)
    dt = _parse_local_dt(occurred_at)
    if dt is not None:
        m.occurred_at = dt
    if m.items:
        m.items[0].qty = signed_qty(m.movement_type, quantity, direction)
        m.items[0].unit_price = float(unit_cost) if unit_cost.strip() else None
        if product_id.strip().isdigit():
            p = db.get(Product, int(product_id))
            if p:
                m.items[0].product_id = p.id
                m.items[0].name = p.name
    m.note = note or None
    db.commit()
    return RedirectResponse("/records", status_code=303)


@app.post("/movement/{mid}/delete")
def movement_delete(request: Request, mid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    m = db.get(Transaction, mid)
    if not m or m.type != TX_INVENTORY:
        return RedirectResponse("/records", status_code=303)
    if not can(staff, f"{module_for_type(m.movement_type)}.delete"):
        return RedirectResponse("/records", status_code=303)
    db.delete(m)
    db.commit()
    return RedirectResponse("/records", status_code=303)


# ---------- record delete with dependency check ----------

def _tx_ref(d):
    """A human label + link for a transaction that references another record."""
    peso = lambda v: "₱{:,.2f}".format(float(v or 0))
    when = (" · " + d.occurred_at.astimezone(_tz()).strftime("%b %d, %Y")) if d.occurred_at else ""
    if d.type == TX_PAYMENT:
        return {"kind": "Payment", "label": "Payment %s%s" % (peso(d.total or 0), when),
                "link": ("/customer/%d" % d.customer_id) if d.customer_id else ""}
    if d.type == TX_ORDER:
        return {"kind": "Order", "label": "Order %s%s" % (d.number or ("#%d" % d.id), when),
                "link": "/orders"}
    if d.type == TX_INVOICE:
        return {"kind": "Invoice", "label": "Invoice %s%s" % (d.number or ("#%d" % d.id), when),
                "link": "/invoices/%d" % d.id}
    if d.type == TX_CASH_SALE:
        return {"kind": "Sale", "label": "Sale #%d%s" % (d.id, when), "link": "/sale/%d/edit" % d.id}
    return {"kind": (d.type or "Record").title(), "label": "%s #%d" % ((d.type or "record"), d.id), "link": ""}


def _tx_dependents(db, tx):
    """Records whose FK points at this one (would be orphaned if it were deleted):
    payments applied to it (parent_id) and any order/invoice converted into it."""
    deps = (db.query(Transaction)
            .filter(Transaction.id != tx.id,
                    or_(Transaction.parent_id == tx.id, Transaction.converted_id == tx.id))
            .order_by(Transaction.occurred_at.desc()).all())
    return [_tx_ref(d) for d in deps]


def _can_delete_record(staff, tx):
    if tx.type == TX_CASH_SALE:
        return can(staff, "sales.delete")
    if tx.type == TX_INVENTORY:
        return can(staff, "%s.delete" % module_for_type(tx.movement_type))
    return False


@app.get("/record/{tid}/deps")
def record_deps(request: Request, tid: int, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff:
        return JSONResponse({"ok": False, "error": "Not signed in"}, status_code=401)
    tx = db.get(Transaction, tid)
    if not tx:
        return JSONResponse({"ok": False, "error": "Record not found"}, status_code=404)
    if not _can_delete_record(staff, tx):
        return JSONResponse({"ok": False, "error": "You can't delete this record"}, status_code=403)
    deps = _tx_dependents(db, tx)
    return {"ok": True, "deletable": len(deps) == 0, "deps": deps}


@app.post("/record/{tid}/delete")
def record_delete(request: Request, tid: int, db: Session = Depends(get_db)):
    staff = current_staff(request, db)
    if not staff:
        return JSONResponse({"ok": False, "error": "Not signed in"}, status_code=401)
    tx = db.get(Transaction, tid)
    if not tx:
        return JSONResponse({"ok": False, "error": "Record not found"}, status_code=404)
    if not _can_delete_record(staff, tx):
        return JSONResponse({"ok": False, "error": "You can't delete this record"}, status_code=403)
    deps = _tx_dependents(db, tx)
    if deps:
        return JSONResponse({"ok": False, "deps": deps}, status_code=409)
    db.delete(tx)          # items cascade; stock is recomputed from remaining movements
    db.commit()
    return {"ok": True}


# ---------- sales: edit / delete ----------

def _sale_paid(db, sale):
    """A sale counts as 'paid' — and so must NOT be deleted — if it was paid at
    the counter, or if any payment has been recorded against it."""
    if not sale.is_credit:
        return True  # paid at the counter
    return db.query(Transaction.id).filter(
        Transaction.type == TX_PAYMENT,
        Transaction.parent_id == sale.id).first() is not None


@app.get("/sale/{sid}/edit", response_class=HTMLResponse)
def sale_edit(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="sales.edit")
    if redir:
        return redir
    sale = db.get(Transaction, sid)
    if not sale or sale.type != TX_CASH_SALE:
        return RedirectResponse("/records", status_code=303)
    tz = _tz()
    local = sale.occurred_at.astimezone(tz) if sale.occurred_at else datetime.now(tz)
    dt_value = local.strftime("%Y-%m-%dT%H:%M")
    products = db.query(Product).order_by(Product.name).all()
    customers = db.query(Staff).filter(Staff.is_active).order_by(Staff.name).all()
    err = None
    if request.query_params.get("err") == "paid":
        err = "This sale has already been paid, so it can't be deleted. Refund or void it instead."
    return render(request, "sale_edit.html", db, staff, sale=sale, error=err,
                  dt_value=dt_value, products=products, customers=customers,
                  can_delete=can(staff, "sales.delete"), is_paid=_sale_paid(db, sale))


@app.post("/sale/{sid}/edit")
async def sale_update(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="sales.edit")
    if redir:
        return redir
    sale = db.get(Transaction, sid)
    if not sale or sale.type != TX_CASH_SALE:
        return RedirectResponse("/records", status_code=303)
    form = await request.form()

    # header ------------------------------------------------------------
    dt = _parse_local_dt(form.get("occurred_at"))
    if dt is not None:
        sale.occurred_at = dt
    cid = (form.get("customer_id") or "").strip()
    sale.customer_id = int(cid) if cid.isdigit() else None
    is_paid = (form.get("status") or "paid") == "paid"
    sale.is_credit = not is_paid
    sale.status = "paid" if is_paid else "credit"
    if is_paid:
        pm = (form.get("payment_method") or "cash").strip()
        sale.payment_method = pm if pm in PAYMENT_METHODS else "cash"
    else:
        sale.payment_method = None
    sale.note = (form.get("note") or "").strip() or None

    # existing line items: update qty / unit price / name, or delete ----
    remove_ids = set(form.getlist("item_remove"))
    ids = form.getlist("item_id")
    names = form.getlist("item_name")
    qtys = form.getlist("item_qty")
    prices = form.getlist("item_price")
    by_id = {str(it.id): it for it in sale.items}
    for i, iid in enumerate(ids):
        it = by_id.get(str(iid))
        if not it:
            continue
        if iid in remove_ids:
            db.delete(it)
            continue
        try:
            q = int(qtys[i]) if i < len(qtys) and str(qtys[i]).strip() else it.qty
        except ValueError:
            q = it.qty
        if q <= 0:
            db.delete(it)
            continue
        it.qty = q
        try:
            if i < len(prices) and str(prices[i]).strip():
                it.unit_price = round(float(prices[i]), 2)
        except ValueError:
            pass
        nm = names[i].strip() if i < len(names) else ""
        if nm:
            it.name = nm

    # optional new line -------------------------------------------------
    np = (form.get("new_product_id") or "").strip()
    nq = (form.get("new_qty") or "").strip()
    if np.isdigit() and nq:
        p = db.get(Product, int(np))
        try:
            q = int(nq)
        except ValueError:
            q = 0
        if p and q > 0:
            npr = (form.get("new_price") or "").strip()
            try:
                up = round(float(npr), 2) if npr else float(p.selling_price or 0)
            except ValueError:
                up = float(p.selling_price or 0)
            db.add(TransactionItem(transaction_id=sale.id, product_id=p.id,
                                   name=p.name, qty=q, unit_price=up,
                                   cost_price=p.cost_price))

    db.commit()
    return RedirectResponse("/records", status_code=303)


@app.post("/sale/{sid}/delete")
def sale_delete(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="sales.delete")
    if redir:
        return redir
    sale = db.get(Transaction, sid)
    if not sale or sale.type != TX_CASH_SALE:
        return RedirectResponse("/records", status_code=303)
    if _sale_paid(db, sale):
        # safety net: a paid sale must not be deleted
        return RedirectResponse("/sale/%d/edit?err=paid" % sid, status_code=303)
    db.delete(sale)  # cascades to items
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


def _price_levels(db):
    return db.query(PricingGroup).order_by(PricingGroup.name).all()


def _item_level_prices(db, product):
    """{group_id: explicit price} for this product across all levels."""
    if not product or not product.id:
        return {}
    rows = db.query(PricingGroupItem).filter(PricingGroupItem.product_id == product.id).all()
    return {r.group_id: float(r.price) for r in rows if r.price is not None}


def _apply_item_levels(db, product, form):
    """Upsert per-level prices from level_price_<gid> fields (blank = revert to Base)."""
    for key, val in form.multi_items():
        if not key.startswith("level_price_"):
            continue
        try:
            gid = int(key[len("level_price_"):])
        except ValueError:
            continue
        if not db.get(PricingGroup, gid):
            continue
        row = (db.query(PricingGroupItem)
               .filter_by(group_id=gid, product_id=product.id).first())
        sval = (val or "").strip()
        if sval:
            try:
                price = round(float(sval), 2)
            except ValueError:
                continue
            if price < 0:
                continue
            if row:
                row.price = price
            else:
                db.add(PricingGroupItem(group_id=gid, product_id=product.id, price=price))
        elif row:
            db.delete(row)


@app.get("/admin/products/new", response_class=HTMLResponse)
def product_new(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="items.create")
    if redir:
        return redir
    return render(request, "product_form.html", db, staff, product=None, error=None,
                  category_suggestions=_category_suggestions(db),
                  price_levels=_price_levels(db), level_prices={})


@app.post("/admin/products/new")
async def product_create(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="items.create")
    if redir:
        return redir
    form = await request.form()

    def back(error):
        return render(request, "product_form.html", db, staff, product=None, error=error,
                      category_suggestions=_category_suggestions(db),
                      price_levels=_price_levels(db), level_prices={})

    sku = (form.get("sku") or "").strip()
    name = (form.get("name") or "").strip()
    unit = form.get("unit") or "each"
    if not sku or not name:
        return back("SKU and name are required.")
    if unit not in UNITS:
        return back("Invalid unit.")
    try:
        selling_price = float(form.get("selling_price"))
    except (TypeError, ValueError):
        return back("Selling price is required.")
    if db.query(Product).filter(Product.sku == sku).first():
        return back(f"SKU '{sku}' already exists.")
    cost = (form.get("cost_price") or "").strip()
    try:
        reorder = int(form.get("reorder_point") or 0)
    except ValueError:
        reorder = 0
    product = Product(sku=sku, name=name,
                      category=((form.get("category") or "").strip() or None),
                      supplier=((form.get("supplier") or "").strip() or None),
                      unit=unit, selling_price=selling_price,
                      cost_price=(float(cost) if cost else None), reorder_point=reorder)
    db.add(product)
    db.flush()
    _apply_item_levels(db, product, form)
    db.commit()
    return RedirectResponse("/admin/products", status_code=303)


@app.get("/admin/products/{pid}/edit", response_class=HTMLResponse)
def product_edit(request: Request, pid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="items.edit")
    if redir:
        return redir
    product = db.get(Product, pid)
    return render(request, "product_form.html", db, staff, product=product, error=None,
                  category_suggestions=_category_suggestions(db),
                  price_levels=_price_levels(db),
                  level_prices=_item_level_prices(db, product))


@app.post("/admin/products/{pid}/edit")
async def product_update(request: Request, pid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="items.edit")
    if redir:
        return redir
    product = db.get(Product, pid)
    if not product:
        return RedirectResponse("/admin/products", status_code=303)
    form = await request.form()
    product.name = (form.get("name") or product.name).strip()
    product.category = (form.get("category") or "").strip() or None
    product.supplier = (form.get("supplier") or "").strip() or None
    unit = form.get("unit") or product.unit
    product.unit = unit if unit in UNITS else product.unit
    try:
        product.selling_price = float(form.get("selling_price"))
    except (TypeError, ValueError):
        pass
    cost = (form.get("cost_price") or "").strip()
    product.cost_price = float(cost) if cost else None
    try:
        product.reorder_point = int(form.get("reorder_point") or 0)
    except ValueError:
        pass
    product.is_active = form.get("is_active") == "on"
    _apply_item_levels(db, product, form)
    db.commit()
    return RedirectResponse("/admin/products", status_code=303)


@app.post("/admin/products/{pid}/field")
async def product_set_field(request: Request, pid: int, db: Session = Depends(get_db)):
    """Inline single-field update from the Items table. Allowlisted fields only."""
    staff, redir = require(request, db, perm="items.edit")
    if redir:
        return JSONResponse({"ok": False, "error": "Not allowed"}, status_code=403)
    data = await request.json()
    field = data.get("field")
    value = data.get("value")
    sval = "" if value is None else str(value).strip()
    p = db.get(Product, pid)
    if not p:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    peso = lambda v: "₱{:,.2f}".format(float(v or 0))
    try:
        if field == "name":
            if not sval:
                return JSONResponse({"ok": False, "error": "Name is required"}, status_code=400)
            p.name = sval; disp = p.name; raw = p.name
        elif field == "supplier":
            p.supplier = sval or None; disp = p.supplier or "—"; raw = p.supplier or ""
        elif field == "selling_price":
            p.selling_price = float(sval); disp = peso(p.selling_price); raw = "{:.2f}".format(p.selling_price)
        elif field == "cost_price":
            p.cost_price = float(sval) if sval else None
            disp = peso(p.cost_price) if p.cost_price is not None else "—"
            raw = "" if p.cost_price is None else "{:.2f}".format(p.cost_price)
        elif field == "reorder_point":
            p.reorder_point = int(float(sval or 0)); disp = str(p.reorder_point); raw = str(p.reorder_point)
        else:
            return JSONResponse({"ok": False, "error": "Field not editable"}, status_code=400)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Invalid value"}, status_code=400)
    db.commit()
    return {"ok": True, "display": disp, "raw": raw}


@app.post("/admin/products/{pid}/delete")
def product_delete(request: Request, pid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="items.delete")
    if redir:
        return redir
    product = db.get(Product, pid)
    if not product:
        return RedirectResponse("/admin/products", status_code=303)
    referenced = (
        db.query(TransactionItem).filter(TransactionItem.product_id == pid).first()
        or db.query(TransactionItem).filter(TransactionItem.product_id == pid).first()
    )
    if referenced:
        # keep history intact — deactivate instead of hard delete
        product.is_active = False
    else:
        db.delete(product)
    db.commit()
    return RedirectResponse("/admin/products", status_code=303)


# ---------- admin: staff ----------

ENTITY_TYPE_LABELS = dict(ENTITY_TYPES)


@app.get("/admin/staff", response_class=HTMLResponse)
def staff_list(request: Request, type: str = "", db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    ftype = type if type in ("customer", "employee", "affiliate", "coach", "member", "supplier") else ""
    q = db.query(Staff)
    if ftype:
        q = q.filter(Staff.person_type == ftype)
    people = q.order_by(Staff.is_active.desc(), Staff.name).all()
    return render(request, "staff.html", db, staff, people=people, usage={},
                  ftype=ftype, ftype_label=(ENTITY_TYPE_LABELS.get(ftype, "") if ftype else ""),
                  ENTITY_TYPES=ENTITY_TYPES)


def _roles(db):
    return db.query(Role).order_by(Role.is_admin.desc(), Role.name).all()


def _person_activity(db, pid):
    """A person's cross-role history — purchases, payments, balance and waivers —
    keyed by their entity id. Same data as the customer page, so it shows on any
    entity (an employee who also buys, a member who signed a waiver, etc.)."""
    sales = (db.query(Transaction)
             .filter(Transaction.type == TX_CASH_SALE, Transaction.customer_id == pid)
             .order_by(Transaction.occurred_at.desc()).limit(200).all())
    payments = (db.query(Transaction)
                .filter(Transaction.type == TX_PAYMENT, Transaction.parent_id == None,  # noqa: E711
                        Transaction.customer_id == pid)
                .order_by(Transaction.occurred_at.desc()).all())
    charges = sum(s.total for s in sales if s.is_credit)
    paid = sum(p.total for p in payments)
    waivers = (db.query(Waiver).filter(Waiver.customer_id == pid)
               .order_by(Waiver.signed_at.desc()).all())
    return {"sales": sales, "payments": payments, "charges": charges, "paid": paid,
            "balance": charges - paid, "waivers": waivers,
            "has_any": bool(sales or payments or waivers)}


def _form(request, db, staff, person=None, error=None, preset_type=""):
    levels = db.query(PricingGroup).order_by(PricingGroup.name).all()
    activity = _person_activity(db, person.id) if person else None
    return render(request, "staff_form.html", db, staff, person=person, error=error,
                  ENTITY_TYPES=ENTITY_TYPES, DISCOUNT_TYPES=list(DISCOUNT_TYPES),
                  roles=_roles(db), preset_type=preset_type, price_levels=levels,
                  activity=activity)


@app.get("/admin/staff/new", response_class=HTMLResponse)
def staff_new(request: Request, type: str = "", db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    preset = type if type in ("customer", "employee", "affiliate", "coach", "member", "supplier") else ""
    return _form(request, db, staff, person=None, preset_type=preset)


def _clean_perms(values):
    return ",".join(v for v in values if v in PERMISSION_KEYS)


def _norm_username(u):
    return "".join(c for c in (u or "").strip().lower() if c.isalnum() or c in "._-")


def _gen_code(db, name, person_type, exclude_id=None):
    """Make a unique personal discount code like EMP-JOHN01 / AFF-MARY01."""
    prefix = "AFF" if person_type == "affiliate" else "EMP"
    base = "".join(ch for ch in (name or "").upper() if ch.isalnum())[:4] or "CODE"
    q = db.query(Staff).filter(Staff.discount_code != None)  # noqa: E711
    if exclude_id:
        q = q.filter(Staff.id != exclude_id)
    taken = set(c for (c,) in q.with_entities(Staff.discount_code).all() if c)
    i = 1
    while True:
        c = f"{prefix}-{base}{i:02d}"
        if c not in taken:
            return c
        i += 1


def _norm_code(c):
    return "".join(ch for ch in (c or "").strip().upper() if ch.isalnum() or ch == "-")


def _apply_access(db, person, form, err):
    """Apply the login side of the form to `person`. Returns an error response
    (via `err`) or None on success."""
    username = _norm_username(form.get("username"))
    pin = form.get("pin") or ""
    if not username:
        return err("Username is required when Access is granted.")
    if username != (person.username or ""):
        if db.query(Staff).filter(func.lower(Staff.username) == username,
                                  Staff.id != (person.id or -1)).first():
            return err(f"Username '{username}' is already taken.")
        person.username = username
    # Role → drives admin flag + seeds permissions.
    role = None
    rid = form.get("role_id")
    if rid:
        try:
            role = db.get(Role, int(rid))
        except (TypeError, ValueError):
            role = None
    if role is None:
        role = db.query(Role).filter(Role.name == "Staff").first()
    person.role_id = role.id if role else None
    person.role = "admin" if (role and role.is_admin) else "staff"
    person.permissions = "" if (role and role.is_admin) else _clean_perms(form.getlist("permissions"))
    if pin.strip():
        if len(pin) < 4:
            return err("PIN must be at least 4 digits.")
        person.pin_hash, person.pin_salt = hash_pin(pin)
    elif not person.pin_hash:
        return err("Set a PIN (at least 4 digits) for this login.")
    return None


def _apply_type(db, person, person_type, form, err):
    """Apply the relationship type + assigned price level. Returns error or None."""
    person.person_type = person_type or None
    # Price level (pricing_group). Blank = Base price.
    pg = (form.get("pricing_group_id") or "").strip()
    if pg:
        try:
            gid = int(pg)
            person.pricing_group_id = gid if db.get(PricingGroup, gid) else None
        except ValueError:
            person.pricing_group_id = None
    else:
        person.pricing_group_id = None
    # Affiliate/coach billing fields (affiliates carry a fee + billing date).
    if person_type in ("affiliate", "coach"):
        person.start_date = _date_only(form.get("start_date"))
        if person_type == "affiliate":
            try:
                person.affiliate_fee = float(form.get("affiliate_fee")) if (form.get("affiliate_fee") or "").strip() else None
            except ValueError:
                person.affiliate_fee = None
            person.next_billing = _date_only(form.get("next_billing"))
        else:
            person.affiliate_fee = None
            person.next_billing = None
    else:
        person.affiliate_fee = None
        person.start_date = None
        person.next_billing = None
    return None


@app.post("/admin/staff/new")
async def staff_create(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    form = await request.form()
    name = (form.get("name") or "").strip()
    person_type = form.get("person_type") or ""
    if person_type not in ("customer", "employee", "affiliate", "coach", "member", "supplier"):
        person_type = ""
    has_access = form.get("has_access") == "on"

    def err(msg):
        return _form(request, db, staff, person=None, error=msg)

    if not name:
        return err("Name is required.")

    new = Staff(name=name, has_access=has_access, permissions="", role="staff",
                phone=(form.get("phone") or "").strip() or None,
                email=(form.get("email") or "").strip() or None)
    r = _apply_type(db, new, person_type, form, err)
    if r:
        return r
    if has_access:
        r = _apply_access(db, new, form, err)
        if r:
            return r
    db.add(new)
    db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


@app.get("/admin/staff/{sid}/edit", response_class=HTMLResponse)
def staff_edit(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    person = db.get(Staff, sid)
    return _form(request, db, staff, person=person)


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
    person_type = form.get("person_type") or ""
    if person_type not in ("customer", "employee", "affiliate", "coach", "member", "supplier"):
        person_type = ""
    has_access = form.get("has_access") == "on"

    def err(msg):
        return _form(request, db, staff, person=person, error=msg)

    person.name = name or person.name
    person.phone = (form.get("phone") or "").strip() or None
    # Only touch email when this form carried the field — a form that doesn't
    # show email shouldn't silently erase one set elsewhere.
    if "email" in form:
        person.email = (form.get("email") or "").strip() or None
    person.is_active = form.get("is_active") == "on"

    r = _apply_type(db, person, person_type, form, err)
    if r:
        return r

    person.has_access = has_access
    if has_access:
        r = _apply_access(db, person, form, err)
        if r:
            return r
    db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


# ---------- admin: roles ----------

@app.get("/admin/roles", response_class=HTMLResponse)
def roles_list(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    roles = _roles(db)
    counts = {r.id: db.query(func.count(Staff.id)).filter(Staff.role_id == r.id).scalar()
              for r in roles}
    return render(request, "roles.html", db, staff, roles=roles, role=None, counts=counts)


@app.get("/admin/roles/new", response_class=HTMLResponse)
def role_new(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    return render(request, "role_form.html", db, staff, role=None, error=None)


@app.get("/admin/roles/{rid}/edit", response_class=HTMLResponse)
def role_edit(request: Request, rid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    role = db.get(Role, rid)
    if not role:
        return RedirectResponse("/admin/roles", status_code=303)
    return render(request, "role_form.html", db, staff, role=role, error=None)


def _save_role(db, role, form):
    role.name = (form.get("name") or role.name or "Role").strip()
    if not role.is_system:  # system roles keep their admin flag
        role.is_admin = form.get("is_admin") == "on"
    role.permissions = "" if role.is_admin else _clean_perms(form.getlist("permissions"))


@app.post("/admin/roles/new")
async def role_create(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return render(request, "role_form.html", db, staff, role=None,
                      error="Role name is required.")
    if db.query(Role).filter(func.lower(Role.name) == name.lower()).first():
        return render(request, "role_form.html", db, staff, role=None,
                      error=f"A role named '{name}' already exists.")
    role = Role(name=name)
    _save_role(db, role, form)
    db.add(role)
    db.commit()
    return RedirectResponse("/admin/roles", status_code=303)


@app.post("/admin/roles/{rid}/edit")
async def role_update(request: Request, rid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    role = db.get(Role, rid)
    if not role:
        return RedirectResponse("/admin/roles", status_code=303)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if name and db.query(Role).filter(func.lower(Role.name) == name.lower(),
                                      Role.id != role.id).first():
        return render(request, "role_form.html", db, staff, role=role,
                      error=f"A role named '{name}' already exists.")
    _save_role(db, role, form)
    db.commit()
    # Re-stamp members' admin flag if this role's admin status changed.
    role_flag = "admin" if role.is_admin else "staff"
    db.query(Staff).filter(Staff.role_id == role.id).update({"role": role_flag})
    db.commit()
    return RedirectResponse("/admin/roles", status_code=303)


@app.post("/admin/roles/{rid}/delete")
def role_delete(request: Request, rid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    role = db.get(Role, rid)
    if role and not role.is_system:
        # Reassign anyone on this role to the built-in Staff role.
        fallback = db.query(Role).filter(Role.name == "Staff").first()
        for st in db.query(Staff).filter(Staff.role_id == role.id).all():
            st.role_id = fallback.id if fallback else None
            st.role = "staff"
        db.flush()
        db.delete(role)
        db.commit()
    return RedirectResponse("/admin/roles", status_code=303)


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

    TI, TX = TransactionItem, Transaction
    _cash = (TX.type == TX_CASH_SALE)
    daily = (
        db.query(func.date(TX.occurred_at).label("day"),
                 func.sum(TI.qty).label("units"),
                 func.sum(TI.qty * TI.unit_price).label("revenue"))
        .join(TI, TI.transaction_id == TX.id)
        .filter(_cash, TX.occurred_at >= start)
        .group_by(func.date(TX.occurred_at)).order_by(func.date(TX.occurred_at).desc()).all()
    )
    sellers = (
        db.query(Product.name,
                 func.sum(TI.qty).label("units"),
                 func.sum(TI.qty * TI.unit_price).label("revenue"))
        .join(TI, TI.product_id == Product.id)
        .join(TX, TX.id == TI.transaction_id)
        .filter(_cash, TX.occurred_at >= start)
        .group_by(Product.name).order_by(func.sum(TI.qty).desc()).all()
    )
    margins = (
        db.query(
            Product.name,
            func.sum(TI.qty * TI.unit_price).label("revenue"),
            func.sum(TI.qty * func.coalesce(TI.cost_price, Product.cost_price, 0)).label("cost"),
        )
        .join(TI, TI.product_id == Product.id)
        .join(TX, TX.id == TI.transaction_id)
        .filter(_cash, TX.occurred_at >= start)
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
    mov = _adjust_qty_map(db, before=end_u)
    sold = _sold_qty_map(db, before=end_u)
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
    db.query(TransactionItem).delete()
    db.query(Transaction).delete()
    db.query(Transaction).filter(Transaction.type == TX_INVENTORY).delete()
    db.query(Staff).filter(Staff.person_type == "customer").delete()
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
            mv = Transaction(type=TX_INVENTORY, subtype="restock", status="done",
                             occurred_at=when, created_at=when, staff_id=staff.id,
                             note=r.get("note") or None)
            db.add(mv); db.flush()
            db.add(TransactionItem(transaction_id=mv.id, product_id=p.id, name=p.name,
                                   qty=int(r["qty"]), unit_price=p.cost_price))
        else:
            paid = r.get("paid", True)
            cust = None
            if r.get("customer"):
                cust = find_or_create_customer(db, r["customer"])
            sale = Transaction(type=TX_CASH_SALE, status=("credit" if not paid else "paid"),
                        staff_id=staff.id, occurred_at=when, is_credit=(not paid),
                        customer_id=(cust.id if cust else None), note=r.get("note") or None,
                        payment_method=(r.get("payment") or "cash") if paid else None)
            db.add(sale)
            db.flush()
            db.add(TransactionItem(transaction_id=sale.id, product_id=p.id, name=p.name,
                            qty=int(r["qty"]), unit_price=p.selling_price, cost_price=p.cost_price))
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
        items = db.query(TransactionItem).filter(TransactionItem.product_id == p.id).all()
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
        db.query(Transaction.occurred_at, Staff.name, Transaction.payment_method, Product.sku,
                 Product.name, TransactionItem.qty, TransactionItem.unit_price)
        .join(TransactionItem, TransactionItem.transaction_id == Transaction.id)
        .join(Product, Product.id == TransactionItem.product_id)
        .outerjoin(Staff, Staff.id == Transaction.staff_id)
        .filter(Transaction.type == TX_CASH_SALE, Transaction.occurred_at >= start)
        .order_by(Transaction.occurred_at.desc()).all()
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
        db.query(Transaction).filter(Transaction.type == TX_INVENTORY)
        .order_by(Transaction.occurred_at.desc()).limit(100).all()
    )
    sales = (db.query(Transaction).filter(Transaction.type == TX_CASH_SALE)
             .order_by(Transaction.occurred_at.desc()).limit(50).all())
    return render(request, "records.html", db, staff, movements=movements, sales=sales)


ADJUST_VIEW_PERMS = ["view_reports", "adjust.create", "adjust.edit", "adjust.delete"]
RECEIVE_VIEW_PERMS = ["view_reports", "receive.create", "receive.edit",
                      "receive.delete"]

#: How many rows the page will draw before it starts leaving some out.
MOVEMENT_LIMIT = 500


def movement_types_for(staff):
    """Which kinds of stock movement this person is allowed to look at.

    Receiving and adjusting are separate permissions and always have been, so
    widening this page to cover both must not quietly hand somebody with only
    "Receive Inventory" a list of everything that has been written off. They
    each see their own half; anybody with reports sees all of it.
    """
    if can(staff, "view_reports"):
        return list(MOVEMENT_TYPES)
    out = []
    if can_any(staff, RECEIVE_VIEW_PERMS):
        out += RECEIVE_TYPES
    if can_any(staff, ADJUST_VIEW_PERMS):
        out += ADJUST_TYPES
    # Keep them in the order the constant declares, not the order granted.
    return [t for t in MOVEMENT_TYPES if t in out]


@app.get("/adjustments", response_class=HTMLResponse)
def adjustments_list(request: Request, db: Session = Depends(get_db)):
    """Everything that has moved stock — received and written off alike.

    It used to show only waste, missing and manual adjustments, which meant
    restocks appeared nowhere except buried in Records. "Where did my delivery
    go" is not a question a stock page should leave you asking, so the page now
    covers every movement type and the filter is what narrows it.
    """
    staff, redir = require(request, db)
    if redir:
        return redir
    allowed = movement_types_for(staff)
    if not allowed:
        return RedirectResponse("/dashboard", status_code=303)
    ftype = request.query_params.get("type") or "all"
    if ftype != "all" and ftype not in allowed:
        ftype = "all"
    base = db.query(Transaction).filter(Transaction.type == TX_INVENTORY,
                                        Transaction.subtype.in_(allowed))
    # Counted across everything they may see, not across what is on screen —
    # a filter whose numbers change when you use it cannot be used to compare.
    counts = dict(
        base.with_entities(Transaction.subtype, func.count(Transaction.id))
        .group_by(Transaction.subtype).all())
    q = base
    if ftype in allowed:
        q = q.filter(Transaction.subtype == ftype)
    movements = (q.order_by(Transaction.occurred_at.desc())
                 .limit(MOVEMENT_LIMIT).all())
    return render(request, "adjustments.html", db, staff, movements=movements,
                  ftype=ftype, types=allowed, counts=counts,
                  shown_total=sum(counts.get(t, 0) for t in allowed),
                  capped=len(movements) >= MOVEMENT_LIMIT,
                  RECEIVE_TYPES=RECEIVE_TYPES)


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
        db.query(Transaction).filter(
            Transaction.type == TX_CASH_SALE, Transaction.is_credit == True,  # noqa: E712
            Transaction.customer_id != None).all()  # noqa: E711
    )
    for s in credit_sales:
        charges[s.customer_id] = charges.get(s.customer_id, 0.0) + s.total
    paid = dict(
        db.query(Transaction.customer_id,
                 func.coalesce(func.sum(TransactionItem.qty * TransactionItem.unit_price), 0))
        .join(TransactionItem, TransactionItem.transaction_id == Transaction.id)
        .filter(Transaction.type == TX_PAYMENT, Transaction.parent_id == None,  # noqa: E711
                Transaction.customer_id != None)
        .group_by(Transaction.customer_id).all()
    )
    def _row(c):
        ch = charges.get(c.id, 0.0)
        pd = float(paid.get(c.id, 0) or 0)
        nm = (c.name or "").split(" ")
        return {"customer": c, "charges": ch, "paid": pd, "balance": ch - pd,
                "first": c.first_name or (nm[0] if nm else ""),
                "last": c.last_name or " ".join(nm[1:]),
                "email": c.email or "", "phone": c.phone or ""}

    rows = []
    seen = set()
    for c in db.query(Staff).filter(Staff.person_type == "customer").order_by(Staff.name).all():
        rows.append(_row(c))
        seen.add(c.id)
    # Non-customer entities (employees/affiliates/etc.) who bought on credit still
    # need their balance tracked so it can be settled.
    extra = [i for i in (set(charges) | set(paid)) if i is not None and i not in seen]
    if extra:
        for c in db.query(Staff).filter(Staff.id.in_(extra)).order_by(Staff.name).all():
            rows.append(_row(c))
    return rows


CUSTOMER_SORTS = [("name", "Name (A–Z)"), ("recent", "Recently added"),
                  ("updated", "Recently updated"), ("balance", "Highest balance")]


def _sort_customers(rows, sort):
    def created(r): return r["customer"].created_at or datetime.min.replace(tzinfo=timezone.utc)
    def updated(r): return (r["customer"].updated_at or r["customer"].created_at
                            or datetime.min.replace(tzinfo=timezone.utc))
    if sort == "recent":
        rows.sort(key=created, reverse=True)
    elif sort == "updated":
        rows.sort(key=updated, reverse=True)
    elif sort == "balance":
        rows.sort(key=lambda r: r["balance"], reverse=True)
    else:  # name
        rows.sort(key=lambda r: ((r["last"] or r["first"] or r["customer"].name or "").lower(),
                                 (r["first"] or "").lower()))
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
    sort = request.query_params.get("sort") or "name"
    if sort not in dict(CUSTOMER_SORTS):
        sort = "name"
    _sort_customers(rows, sort)
    return render(request, "customers.html", db, staff, rows=rows,
                  outstanding=outstanding, total_out=total_out,
                  sort=sort, CUSTOMER_SORTS=CUSTOMER_SORTS)


@app.get("/customer/{cid}", response_class=HTMLResponse)
def customer_detail(request: Request, cid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, CUSTOMER_VIEW_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    customer = db.get(Staff, cid)
    if not customer:
        return RedirectResponse("/customers", status_code=303)
    act = _person_activity(db, cid)  # ALL purchases + payments + waivers + balance
    return render(request, "customer_detail.html", db, staff, customer=customer,
                  sales=act["sales"], payments=act["payments"], charges=act["charges"],
                  paid=act["paid"], balance=act["balance"], waivers=act["waivers"])


def _save_customer_fields(c, form):
    fn = (form.get("first_name") or "").strip()
    ln = (form.get("last_name") or "").strip()
    c.first_name = fn or None
    c.last_name = ln or None
    c.email = (form.get("email") or "").strip() or None
    c.phone = (form.get("phone") or "").strip() or None
    c.emergency_name = (form.get("emergency_name") or "").strip() or None
    c.emergency_phone = (form.get("emergency_phone") or "").strip() or None
    c.notes = (form.get("notes") or "").strip() or None
    nm = ("%s %s" % (fn, ln)).strip()
    if nm:
        c.name = nm


@app.get("/customers/new", response_class=HTMLResponse)
def customer_new(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, CUSTOMER_VIEW_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "customer_form.html", db, staff, customer=None, error=None)


@app.post("/customers/new")
async def customer_new_save(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, CUSTOMER_VIEW_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    form = await request.form()
    if not (form.get("first_name") or "").strip() and not (form.get("last_name") or "").strip():
        return render(request, "customer_form.html", db, staff, customer=None,
                      error="Please enter at least a first or last name.")
    c = Staff(name="Customer", person_type="customer", has_access=False,
              role="staff", is_active=True, permissions="")
    _save_customer_fields(c, form)
    db.add(c)
    db.commit()
    return RedirectResponse("/customer/%d" % c.id, status_code=303)


@app.get("/customer/{cid}/edit", response_class=HTMLResponse)
def customer_edit(request: Request, cid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, CUSTOMER_VIEW_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    c = db.get(Staff, cid)
    if not c:
        return RedirectResponse("/customers", status_code=303)
    return render(request, "customer_form.html", db, staff, customer=c, error=None,
                  activity=_person_activity(db, c.id))


@app.post("/customer/{cid}/edit")
async def customer_edit_save(request: Request, cid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, CUSTOMER_VIEW_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    c = db.get(Staff, cid)
    if not c:
        return RedirectResponse("/customers", status_code=303)
    form = await request.form()
    _save_customer_fields(c, form)
    db.commit()
    return RedirectResponse("/customer/%d" % cid, status_code=303)


@app.get("/customer/{cid}/pay", response_class=HTMLResponse)
def pay_form(request: Request, cid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="payments.create")
    if redir:
        return redir
    customer = db.get(Staff, cid)
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
    customer = db.get(Staff, cid)
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
    pay = Transaction(type=TX_PAYMENT, customer_id=cid, payment_method=method,
                      note=note or None, proof=img_bytes, proof_mime=img_mime,
                      staff_id=staff.id, status="paid")
    db.add(pay)
    db.flush()
    db.add(TransactionItem(transaction_id=pay.id, name="Payment received",
                           qty=1, unit_price=amt))
    db.commit()
    return RedirectResponse(f"/customer/{cid}", status_code=303)


@app.get("/payment/{pid}/screenshot")
def payment_screenshot(request: Request, pid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_any(staff, CUSTOMER_VIEW_PERMS):
        return RedirectResponse("/dashboard", status_code=303)
    p = db.get(Transaction, pid)
    if not p or not p.proof:
        return Response(status_code=404)
    return Response(content=p.proof, media_type=p.proof_mime or "image/jpeg")


@app.post("/payment/{pid}/delete")
def payment_delete(request: Request, pid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, perm="payments.delete")
    if redir:
        return redir
    p = db.get(Transaction, pid)
    if p and p.type == TX_PAYMENT:
        cid = p.customer_id
        db.delete(p)
        db.commit()
        return RedirectResponse(f"/customer/{cid}", status_code=303)
    return RedirectResponse("/customers", status_code=303)


# ---------- Sales spreadsheet (autosave grid) ----------

def _parse_local_dt(s):
    """Parse a datetime-local / date string in app TZ → aware UTC (or None)."""
    tz = _tz()
    if not s or not str(s).strip():
        return None
    s = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


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
    local = sale.occurred_at.astimezone(tz) if sale.occurred_at else datetime.now(tz)
    up = float(it.unit_price) if it else 0.0
    qn = int(it.qty) if it else 0
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
            selected_customer = db.get(Staff, int(qp.get("customer")))
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
    else:  # default: show everything (all sales, every entry point incl. mobile)
        range_key = range_key or "all"
        start, end_d = None, today0

    q = db.query(Transaction).filter(Transaction.type == TX_CASH_SALE)
    if selected_customer:
        q = q.filter(Transaction.customer_id == selected_customer.id)
    if start is not None:
        q = q.filter(Transaction.occurred_at >= start.astimezone(timezone.utc))
    q = q.filter(Transaction.occurred_at < (end_d + timedelta(days=1)).astimezone(timezone.utc))
    sales = q.order_by(Transaction.occurred_at.desc(), Transaction.id.desc()).limit(5000).all()
    rows = [_sale_row(s) for s in sales]

    total = sum(r["total"] for r in rows)
    unpaid = sum(r["total"] for r in rows if not r["paid"])

    # Balances + sale counts, for the customer search dropdown hints.
    bal_by_id = {r["customer"].id: r for r in customer_balances(db)}
    counts = dict(
        db.query(Transaction.customer_id, func.count(Transaction.id))
        .filter(Transaction.type == TX_CASH_SALE, Transaction.customer_id != None)  # noqa: E711
        .group_by(Transaction.customer_id).all()
    )
    products = [
        {"id": p.id, "name": p.name, "price": float(p.selling_price)}
        for p in db.query(Product).filter(Product.is_active).order_by(Product.name).all()
    ]
    customers = [
        {"id": c.id, "name": c.name,
         "owed": round(float(bal_by_id.get(c.id, {}).get("balance", 0.0)), 2),
         "sales": int(counts.get(c.id, 0))}
        for c in db.query(Staff).filter(Staff.person_type == "customer").order_by(Staff.name).all()
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
    sale = Transaction(type=TX_CASH_SALE, status=("credit" if not paid else "paid"),
                staff_id=staff.id, occurred_at=_sold_dt_from_date(data.get("date")),
                is_credit=(not paid), customer_id=cid,
                payment_method=(None if not paid else "cash"))
    db.add(sale)
    db.flush()
    db.add(TransactionItem(transaction_id=sale.id, product_id=p.id, name=p.name,
                    qty=qty, unit_price=up, cost_price=p.cost_price))
    db.commit()
    db.refresh(sale)
    return JSONResponse(_sale_row(sale))


@app.patch("/api/sales/{sid}")
async def api_sale_update(request: Request, sid: int, db: Session = Depends(get_db)):
    staff, err = _json_guard(request, db, "sales.edit")
    if err:
        return err
    sale = db.get(Transaction, sid)
    if not sale or sale.type != TX_CASH_SALE:
        return JSONResponse({"error": "not found"}, status_code=404)
    data = await request.json()
    if "date" in data:
        sale.occurred_at = _sold_dt_from_date(data.get("date"))
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
                it = TransactionItem(transaction_id=sale.id, product_id=p.id, name=p.name,
                              qty=1, unit_price=p.selling_price, cost_price=p.cost_price)
                db.add(it)
            else:
                it.product_id = p.id
                it.unit_price = p.selling_price
                it.cost_price = p.cost_price
    if "qty" in data and it:
        try:
            it.qty = max(1, int(data.get("qty")))
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
    sale = db.get(Transaction, sid)
    if sale and sale.type == TX_CASH_SALE:
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
        customer = db.get(Staff, int(customer_id))
    sale = Transaction(type=TX_CASH_SALE, status=("credit" if not is_paid else "paid"),
                staff_id=staff.id, occurred_at=_sold_dt_from_date(date),
                is_credit=(not is_paid), customer_id=(customer.id if customer else None),
                payment_method=(None if not is_paid else "cash"))
    db.add(sale)
    db.flush()
    db.add(TransactionItem(transaction_id=sale.id, product_id=p.id, name=p.name,
                    qty=max(1, quantity), unit_price=p.selling_price, cost_price=p.cost_price))
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
    """Affiliate/coach entities with their member counts + monthly totals."""
    members = db.query(Staff).filter(Staff.person_type == "member", Staff.is_active == True).all()  # noqa: E712
    by = {}
    for m in members:
        by.setdefault(m.affiliate_id, []).append(m)
    rows = []
    for c in (db.query(Staff)
              .filter(Staff.person_type.in_(["affiliate", "coach"]))
              .order_by(Staff.is_active.desc(), Staff.name).all()):
        ms = by.get(c.id, [])
        corkage = coach_corkage(ms)
        fee = float(c.affiliate_fee or 0) if c.person_type == "affiliate" else 0.0
        monthly = (fee + corkage) if c.person_type == "affiliate" else 0.0
        rows.append({"coach": c, "clients": len(ms), "corkage": corkage,
                     "fee": fee, "monthly": monthly,
                     "discounted": max(0, len(ms) - FIRST_TIER_CLIENTS)})
    return rows


def coach_summary(rows):
    aff = [r for r in rows if r["coach"].person_type == "affiliate" and r["coach"].is_active]
    return {"total_monthly": sum(r["monthly"] for r in aff),
            "affiliate_count": len(aff),
            "member_count": sum(r["clients"] for r in aff)}


# Coaches are now entities. Keep the old URLs working by redirecting into the
# unified entity table (Coaches view) / entity form.
@app.get("/coaches")
def coaches_page(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/admin/staff?type=coach", status_code=303)


@app.get("/coaches/new")
def coach_new(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/admin/staff/new?type=coach", status_code=303)


@app.get("/coaches/members", response_class=HTMLResponse)
def members_page(request: Request, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    members = (db.query(Staff).filter(Staff.person_type == "member")
               .order_by(Staff.is_active.desc(), Staff.name).all())
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
    coaches = (db.query(Staff)
               .filter(Staff.person_type == "affiliate", Staff.is_active == True)  # noqa: E712
               .order_by(Staff.name).all())
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
    db.add(Staff(name=name.strip(), person_type="member", has_access=False, role="staff",
                 permissions="", affiliate_id=(int(coach_id) if coach_id.strip() else None),
                 corkage_rate=rate, start_date=_date_only(start_date),
                 is_active=(is_active == "on")))
    db.commit()
    return RedirectResponse("/coaches/members", status_code=303)


@app.get("/coaches/members/{mid}/edit", response_class=HTMLResponse)
def member_edit(request: Request, mid: int, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    member = db.get(Staff, mid)
    if not member:
        return RedirectResponse("/coaches/members", status_code=303)
    coaches = (db.query(Staff)
               .filter(Staff.person_type == "affiliate")
               .order_by(Staff.name).all())
    return render(request, "member_form.html", db, staff, member=member,
                  coaches=coaches, today="")


@app.post("/coaches/members/{mid}/edit")
def member_update(request: Request, mid: int, name: str = Form(...), coach_id: str = Form(""),
                  corkage_rate: str = Form("3000"), start_date: str = Form(""),
                  is_active: str = Form(""), db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    member = db.get(Staff, mid)
    if not member:
        return RedirectResponse("/coaches/members", status_code=303)
    try:
        rate = float(corkage_rate) if corkage_rate.strip() else 3000.0
    except ValueError:
        rate = 3000.0
    member.name = name.strip()
    member.affiliate_id = int(coach_id) if coach_id.strip() else None
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
    rows = [r for r in coach_rows(db) if r["coach"].person_type == "affiliate" and r["coach"].is_active]
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
    for (n,) in db.query(Transaction.number).filter(Transaction.type == TX_INVOICE).all():
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
    invoices = (db.query(Transaction).filter(Transaction.type == TX_INVOICE)
                .order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(500).all())
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
    coaches = (db.query(Staff)
               .filter(Staff.person_type == "affiliate", Staff.is_active == True)  # noqa: E712
               .order_by(Staff.name).all())
    customers = db.query(Staff).filter(Staff.person_type == "customer").order_by(Staff.name).all()
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
        c = db.get(Staff, coach_id)
        if c and not bill_to:
            bill_to = c.name
    elif party.startswith("customer:"):
        customer_id = int(party.split(":")[1]); bill_type = "customer"
        c = db.get(Staff, customer_id)
        if c and not bill_to:
            bill_to = c.name
    if not bill_to:
        bill_to = "—"
    inv = Transaction(type=TX_INVOICE, status="unpaid",
                  number=next_invoice_number(db), bill_to_type=bill_type,
                  coach_id=coach_id, customer_id=customer_id, customer_name=bill_to,
                  occurred_at=_dt(_date_only(form.get("issue_date")), datetime.now(timezone.utc)),
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
        db.add(TransactionItem(transaction_id=inv.id, name=desc.strip(), qty=q, unit_price=r))
    db.commit()
    return RedirectResponse(f"/invoices/{inv.id}", status_code=303)


@app.get("/invoices/{iid}", response_class=HTMLResponse)
def invoice_view(request: Request, iid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db)
    if redir:
        return redir
    if not can_invoices(staff):
        return RedirectResponse("/dashboard", status_code=303)
    inv = db.get(Transaction, iid)
    if not inv or inv.type != TX_INVOICE:
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
    inv = db.get(Transaction, iid)
    if not inv or inv.type != TX_INVOICE:
        return RedirectResponse("/invoices", status_code=303)
    try:
        amt = float(amount)
    except ValueError:
        amt = 0
    if amt > 0:
        when = _sold_dt_from_date(date) if date else datetime.now(timezone.utc)
        pay = Transaction(type=TX_PAYMENT, parent_id=inv.id, customer_id=inv.customer_id,
                          customer_name=inv.customer_name, payment_method=method,
                          note=note or None, occurred_at=when, staff_id=staff.id, status="paid")
        db.add(pay)
        db.flush()
        db.add(TransactionItem(transaction_id=pay.id, name="Invoice payment",
                               qty=1, unit_price=amt))
        db.commit()
    return RedirectResponse(f"/invoices/{iid}", status_code=303)


@app.post("/invoices/{iid}/void")
def invoice_void(request: Request, iid: int, db: Session = Depends(get_db)):
    staff, redir = require_admin(request, db)
    if redir:
        return redir
    inv = db.get(Transaction, iid)
    if inv and inv.type == TX_INVOICE:
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
    pays = (db.query(Transaction).filter(Transaction.type == TX_PAYMENT)
            .order_by(Transaction.occurred_at.desc()).limit(600).all())
    for p in pays:
        if p.parent_id:
            rows.append({"at": p.occurred_at, "kind": "Invoice payment",
                         "party": (p.parent.customer_name if p.parent else "—"),
                         "amount": float(p.total or 0), "method": p.payment_method or "",
                         "link": f"/invoices/{p.parent_id}"})
        else:
            rows.append({"at": p.occurred_at, "kind": "Customer payment",
                         "party": (p.customer.name if p.customer else "—"),
                         "amount": float(p.total or 0), "method": p.payment_method or "",
                         "link": f"/customer/{p.customer_id}"})
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
    coach = db.get(Staff, cid)
    if not coach or coach.person_type != "affiliate":
        return RedirectResponse("/coaches/billing", status_code=303)
    tz = _tz()
    today = datetime.now(tz).date()
    period = today.strftime("%B %Y")
    members = sorted(
        db.query(Staff).filter(Staff.person_type == "member", Staff.affiliate_id == coach.id, Staff.is_active == True).all(),  # noqa: E712
        key=_member_sort_key)
    inv = Transaction(type=TX_INVOICE, status="unpaid", number=next_invoice_number(db),
                  bill_to_type="coach", coach_id=coach.id, customer_name=coach.name,
                  occurred_at=_dt(today, datetime.now(timezone.utc)), issue_date=today,
                  due_date=today + timedelta(days=7), period=period,
                  note="Auto-generated from Monthly billing", staff_id=staff.id)
    db.add(inv)
    db.flush()
    fee = float(coach.affiliate_fee or 0)
    if fee > 0:
        db.add(TransactionItem(transaction_id=inv.id, name=f"Affiliate fee — {period}",
                               qty=1, unit_price=fee))
    for i, m in enumerate(members):
        base = float(m.corkage_rate or 0)
        rate = base if i < FIRST_TIER_CLIENTS else min(base, TIER_CORKAGE)
        db.add(TransactionItem(transaction_id=inv.id, name=f"Corkage — {m.name}",
                               qty=1, unit_price=rate))
    coach.next_billing = _add_month(coach.next_billing or today)
    db.commit()
    return RedirectResponse(f"/invoices/{inv.id}", status_code=303)


# ---------- pricing tiers (Employee / Affiliate discounts on selected items) ----------
def _code_used_today(db, person_id):
    """Total discounted item-units redeemed by a person's code so far today (Manila)."""
    tz = _tz()
    start = datetime.combine(datetime.now(tz).date(), datetime.min.time()).replace(tzinfo=tz)
    return int(db.query(func.coalesce(func.sum(Transaction.discounted_qty), 0))
               .filter(Transaction.type == TX_CASH_SALE,
                       Transaction.discount_person_id == person_id,
                       Transaction.occurred_at >= start).scalar() or 0)


@app.get("/admin/pricing", response_class=HTMLResponse)
def pricing_list(request: Request, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    groups = db.query(PricingGroup).order_by(PricingGroup.name).all()
    products = db.query(Product).filter(Product.is_active).order_by(Product.name).all()
    # price_map[gid][pid] = explicit price (only where set)
    price_map = {g.id: g.price_map() for g in groups}
    return render(request, "pricing.html", db, staff, groups=groups, products=products,
                  price_map=price_map)


@app.post("/admin/pricing/new")
def pricing_new(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    nm = (name or "").strip()
    if nm:
        db.add(PricingGroup(name=nm, kind="level", discount_percent=0))
        db.commit()
    return RedirectResponse("/admin/pricing", status_code=303)


@app.post("/admin/pricing/{gid}/rename")
def pricing_rename(request: Request, gid: int, name: str = Form(...),
                   is_active: str = Form(None), db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    group = db.get(PricingGroup, gid)
    if group:
        group.name = (name or "").strip() or group.name
        group.is_active = bool(is_active)
        db.commit()
    return RedirectResponse("/admin/pricing", status_code=303)


@app.post("/admin/pricing/save")
async def pricing_save(request: Request, db: Session = Depends(get_db)):
    """Save the whole level×item price matrix. Fields: price_<gid>_<pid> = price
    string (blank = use base price → no row)."""
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    form = await request.form()
    groups = db.query(PricingGroup).all()
    product_ids = {p.id for p in db.query(Product.id).all()}
    for g in groups:
        g.items.clear()
    db.flush()
    for key, val in form.items():
        if not key.startswith("price_"):
            continue
        try:
            _, gs, ps = key.split("_", 2)
            gid, pid = int(gs), int(ps)
        except (ValueError, TypeError):
            continue
        sval = (val or "").strip()
        if not sval or pid not in product_ids:
            continue
        try:
            price = round(float(sval), 2)
        except ValueError:
            continue
        if price < 0:
            continue
        group = next((g for g in groups if g.id == gid), None)
        if group is not None:
            group.items.append(PricingGroupItem(product_id=pid, price=price))
    db.commit()
    return RedirectResponse("/admin/pricing", status_code=303)


@app.post("/admin/pricing/{gid}/delete")
def pricing_delete(request: Request, gid: int, db: Session = Depends(get_db)):
    staff, redir = require(request, db, admin=True)
    if redir:
        return redir
    g = db.get(PricingGroup, gid)
    if g:
        db.query(Transaction).filter(Transaction.pricing_group_id == gid).update({"pricing_group_id": None})
        db.query(Staff).filter(Staff.pricing_group_id == gid).update({"pricing_group_id": None})
        db.flush()
        db.delete(g)
        db.commit()
    return RedirectResponse("/admin/pricing", status_code=303)


# Discount codes now live on people (see Users page). Keep the old URL working.
@app.get("/admin/discount-codes")
def codes_list(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/admin/staff", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ================= Coach commissions =================
# Registered from a separate module so main.py doesn't grow another 400 lines,
# and so the commission screens can't import main (which would be circular).
from . import commission_routes  # noqa: E402

commission_routes.register(app, {
    "render": render,
    "require": require,
    "require_admin": require_admin,
    # The coach statement page is public — it renders without a logged-in
    # staff, so it needs the raw template environment rather than render().
    "templates": templates,
    "tz": _tz,
})


# ================= Sponsored events =================
# A class a sponsor pays for, in exchange for a post from everyone who comes.
# Same registration pattern, and for the same reason.
from . import event_routes  # noqa: E402

# One secret per boot, for signing the registration puzzles. Per boot is fine:
# the worst a restart does is make somebody's half-solved puzzle stale, and the
# page mints a fresh one on the next load.
app.state.pow_secret = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(32)
event_routes.register(app, {
    "render": render,
    "require": require,
    "require_admin": require_admin,
    # The participant's page is public — no login anywhere in that flow.
    "templates": templates,
})

# ================= Coach app =================
# The phone on the floor on race day: pick a heat, grab a client, count. Three
# screens and no more — the leaderboard and the finisher board are admin-side,
# on a laptop or a wall, never in the hand of somebody watching a person race.
from . import coach_routes  # noqa: E402

coach_routes.register(app, {"templates": templates})

# The awarding table: which patch this finisher earned, from their age and
# their time. Same phone shell, different job and different queue.
from . import patch_routes  # noqa: E402

patch_routes.register(app, {"render": render})

# The finisher card: the same record the awarding table just read,
# drawn as something the athlete can keep. Needs `templates` as well as
# `render` because the athlete's copy has no admin chrome around it.
from . import card_routes  # noqa: E402

card_routes.register(app, {"render": render, "templates": templates})


# The words those emails are made of, editable under the gear rather than in a
# release. Registered after the events section because it renders its preview
# through exactly the same builders a real send uses.
from . import template_routes  # noqa: E402

template_routes.register(app, {
    "render": render,
    "require_admin": require_admin,
})


# ================= Event planning =================
# The weeks before there is an event: scope, budget options, equipment,
# staffing and a run sheet, with one password-gated link the client reads and
# comments on. Same registration pattern again.
from . import plan_routes  # noqa: E402

plan_routes.register(app, {
    "render": render,
    "require": require,
    "require_admin": require_admin,
    # The client's copy is public — there is no login on that side.
    "templates": templates,
})
