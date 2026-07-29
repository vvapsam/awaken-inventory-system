"""Coach commission engine.

Pure calculation — no database, no request objects, no I/O beyond the CSV bytes
handed in. Everything here is deterministic and unit-testable, because this is
the part where a subtle bug costs real money.

Pipeline:

    parse    -> rows                 read the Rezerv export
    clean    -> rows                 strip currency/whitespace, numeric revenue
    scope    -> rows, dropped        remove out-of-scope rows (recorded, never silent)
    resolve  -> rows, unmapped       staff -> coach, variant -> delegator
    normalize-> rows                 the revenue adjustments
    compute  -> rows                 commission + delegation charge

`run()` chains all six and returns a Result.

Notes on the source data (verified against a real June 2026 export, 359 rows):

* ``Payment method`` carries "Free"/"Credit"/"Manual". ``Pricing plan used``
  never contains the word "Free" — so the original "remove rows whose Pricing
  plan contains Free" rule matches nothing. It is kept literally on purpose:
  every delegation row is Payment method = Free, so widening the rule to that
  column would delete the entire delegation business.
* Delegation variants use an EN DASH: ``Delegation – KP``. One row reads
  ``Delegation KP (1HR)`` with no separator at all, and is a Hyrox booking.
  Code extraction therefore scans for known codes rather than splitting.
* Every delegation row carries ``Pricing plan used = Drop-In``, which is why
  delegation must be counted separately from Drop-In.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

ZERO = Decimal("0.00")


def money(value) -> Decimal:
    """Round half-up to 2dp. Used once, at the end of a calculation."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

FLAT = "flat"
PERCENT = "percent"

#: backfill scopes — which zero-revenue rows get a session rate applied
BACKFILL_CREDIT_ONLY = "credit_only"
BACKFILL_CREDIT_AND_FREE = "credit_and_free"

#: plans that never receive a zero-revenue backfill
NO_BACKFILL_PLANS = {"drop-in", "awaken force"}

#: pricing plans that are memberships rather than session packs
MEMBERSHIP_RE = re.compile(r"\b(month|months|year|annual)\b", re.I)

HYROX_WITH_COACH = "hyrox simulation (with coach)"


@dataclass(frozen=True)
class CoachRate:
    """How one coach is paid."""
    coach: str
    rate_type: str                      # FLAT | PERCENT
    rate_value: Decimal                 # 750.00, or 0.70
    override_plans: frozenset = frozenset()
    override_rate_type: str | None = None
    override_rate_value: Decimal | None = None

    def for_plan(self, plan: str) -> tuple[str, Decimal]:
        if self.override_rate_type and plan.strip().lower() in self.override_plans:
            return self.override_rate_type, self.override_rate_value
        return self.rate_type, self.rate_value


@dataclass(frozen=True)
class Delegator:
    """Someone who brings their own clients and delegates sessions to a coach.

    ``rate`` is what AWAKEN charges them per delegated session (income).
    ``cost`` is what AWAKEN pays the covering coach (expense).
    """
    name: str
    codes: frozenset                    # {"KP", "CP"} — the export's spellings
    rate: Decimal
    cost: Decimal

    @property
    def margin(self) -> Decimal:
        return self.rate - self.cost


@dataclass
class Settings:
    session_rates: dict = field(default_factory=lambda: {
        "1 session": Decimal("1900"),
        "8 sessions": Decimal("1800"),
        "12 sessions": Decimal("1700"),
        "24 sessions": Decimal("1600"),
        "36 sessions": Decimal("1500"),
    })
    hyrox_walkin_deduction: Decimal = Decimal("1000")
    awaken_force_revenue: Decimal = Decimal("1200")
    #: Conservative default: only prepaid-package rows are backfilled. Comped
    #: ("Free") sessions earn nothing until this is explicitly widened.
    backfill_scope: str = BACKFILL_CREDIT_ONLY
    #: Applied to a bare "Delegation" variant that names no code.
    default_delegator: str | None = None
    #: Literal reading of the original rule. Matches nothing in practice; see
    #: the module docstring for why it is deliberately not widened.
    excluded_plan_words: tuple = ("free",)


@dataclass
class Config:
    coach_rates: dict                   # rezerv staff name -> CoachRate
    delegators: list                    # list[Delegator]
    settings: Settings = field(default_factory=Settings)

    def delegator_for(self, code: str) -> Delegator | None:
        code = (code or "").strip().upper()
        for d in self.delegators:
            if code in d.codes:
                return d
        return None

    @property
    def all_codes(self) -> set:
        out = set()
        for d in self.delegators:
            out |= set(d.codes)
        return out


# --------------------------------------------------------------------------
# row model
# --------------------------------------------------------------------------

@dataclass
class Row:
    booking_ref: str = ""
    customer: str = ""
    appointment_date: date | None = None
    appointment_name: str = ""
    variant: str = ""
    staff_raw: str = ""
    booking_status: str = ""
    pricing_plan: str = ""
    payment_method: str = ""
    revenue_raw: Decimal = ZERO

    # resolved
    coach: str | None = None
    delegator: Delegator | None = None
    delegator_assumed: bool = False

    # normalized
    revenue: Decimal = ZERO
    adjustment: str | None = None
    adjustment_note: str = ""

    # computed
    rule: str | None = None
    rate_type: str | None = None
    rate_value: Decimal | None = None
    commission: Decimal | None = None
    delegation_charge: Decimal | None = None

    @property
    def is_delegation(self) -> bool:
        return self.delegator is not None

    @property
    def is_completed(self) -> bool:
        return self.booking_status.strip().lower() == "completed"


@dataclass
class Dropped:
    row: Row
    reason: str


@dataclass
class Result:
    rows: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    unmapped_staff: dict = field(default_factory=dict)     # name -> count
    unknown_codes: dict = field(default_factory=dict)      # code -> count
    parsed_count: int = 0

    @property
    def blocking(self) -> bool:
        """Finalizing must be refused while either of these is non-empty."""
        return bool(self.unmapped_staff) or bool(self.unknown_codes)

    def totals(self) -> dict:
        done = [r for r in self.rows if r.is_completed]
        return {
            "sessions": len(done),
            "revenue": money(sum((r.revenue for r in done), ZERO)),
            "commission": money(sum((r.commission or ZERO for r in done), ZERO)),
            "delegation_cost": money(sum(
                (r.commission or ZERO for r in done if r.is_delegation), ZERO)),
            "delegation_charged": money(sum(
                (r.delegation_charge or ZERO for r in done), ZERO)),
        }


# --------------------------------------------------------------------------
# 1. parse
# --------------------------------------------------------------------------

_CURRENCY = re.compile(r"[₱,\s]")

COLUMNS = {
    "booking ref": "booking_ref",
    "customer": "customer",
    "appointment date": "appointment_date",
    "appointment name": "appointment_name",
    "variant": "variant",
    "staff": "staff_raw",
    "booking status": "booking_status",
    "pricing plan used": "pricing_plan",
    "payment method": "payment_method",
    "revenue per booking": "revenue_raw",
}

#: Columns the original spec drops. Listed so the intent stays visible even
#: though we simply never read them.
IGNORED = (
    "paid on", "used credit", "total pricing plan credit", "sales channel",
    "slot per booking", "total slots", "duration (hour)", "duration",
    "total transaction revenue",
)


def _money(text: str) -> Decimal:
    text = _CURRENCY.sub("", text or "")
    if not text:
        return ZERO
    try:
        return Decimal(text)
    except Exception:
        return ZERO


def _parse_date(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse(data: bytes | str) -> list:
    """Read the export into Rows. Tolerant of header case and stray spaces."""
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(data))
    out = []
    for raw in reader:
        row = Row()
        for key, value in raw.items():
            field_name = COLUMNS.get((key or "").strip().lower())
            if not field_name:
                continue
            value = (value or "").strip()
            if field_name == "revenue_raw":
                row.revenue_raw = _money(value)
            elif field_name == "appointment_date":
                row.appointment_date = _parse_date(value)
            else:
                setattr(row, field_name, value)
        row.revenue = row.revenue_raw
        out.append(row)
    return out


# --------------------------------------------------------------------------
# 2. clean / 3. scope
# --------------------------------------------------------------------------

def clean(rows: Iterable) -> list:
    """Whitespace is already stripped at parse; collapse internal runs so
    "Anjo  R" (double space) matches "Anjo R"."""
    for row in rows:
        for attr in ("staff_raw", "variant", "pricing_plan", "appointment_name",
                     "booking_status", "payment_method", "customer"):
            setattr(row, attr, re.sub(r"\s+", " ", getattr(row, attr) or "").strip())
    return list(rows)


def scope(rows: Iterable, config: Config) -> tuple[list, list]:
    """Drop out-of-scope rows, recording why. Never silent."""
    kept, dropped = [], []
    words = tuple(w.lower() for w in config.settings.excluded_plan_words)
    for row in rows:
        if not row.staff_raw or row.staff_raw == "--":
            dropped.append(Dropped(row, "no staff assigned"))
            continue
        plan = row.pricing_plan.lower()
        hit = next((w for w in words if w in plan), None)
        if hit:
            dropped.append(Dropped(row, f'pricing plan contains "{hit}"'))
            continue
        kept.append(row)
    kept.sort(key=lambda r: (r.appointment_date or date.min, r.booking_ref))
    return kept, dropped


# --------------------------------------------------------------------------
# 4. resolve — staff to coach, variant to delegator
# --------------------------------------------------------------------------

_DELEGATION = re.compile(r"\bdelegation\b", re.I)
_TOKEN = re.compile(r"[A-Za-z]+")


def delegation_code(variant: str, known: set) -> str | None:
    """Extract the delegator code from a variant.

    Handles ``Delegation – KP`` (en dash), ``Delegation - GR`` (hyphen) and
    ``Delegation KP (1HR)`` (no separator) by scanning word tokens after the
    word "delegation" for anything the configuration recognises.
    """
    if not _DELEGATION.search(variant or ""):
        return None
    tail = _DELEGATION.split(variant, maxsplit=1)[-1]
    for token in _TOKEN.findall(tail):
        if token.upper() in known:
            return token.upper()
    return None


def resolve(rows: Iterable, config: Config) -> tuple[list, dict, dict]:
    unmapped: dict = {}
    unknown: dict = {}
    known = config.all_codes
    for row in rows:
        rate = config.coach_rates.get(row.staff_raw)
        if rate is None:
            unmapped[row.staff_raw] = unmapped.get(row.staff_raw, 0) + 1
        else:
            row.coach = rate.coach

        if _DELEGATION.search(row.variant or ""):
            code = delegation_code(row.variant, known)
            if code:
                row.delegator = config.delegator_for(code)
            else:
                default = config.settings.default_delegator
                row.delegator = config.delegator_for(default) if default else None
                row.delegator_assumed = row.delegator is not None
                if row.delegator is None:
                    unknown[row.variant] = unknown.get(row.variant, 0) + 1
    return list(rows), unmapped, unknown


# --------------------------------------------------------------------------
# 5. normalize — revenue adjustments (Completed rows only)
# --------------------------------------------------------------------------

def _is_membership(plan: str) -> bool:
    return bool(MEMBERSHIP_RE.search(plan or ""))


def _backfill_applies(row: Row, settings: Settings) -> bool:
    if row.revenue != ZERO or row.is_delegation:
        return False
    plan = row.pricing_plan.lower()
    if plan in NO_BACKFILL_PLANS or _is_membership(plan):
        return False
    if plan not in settings.session_rates:
        return False
    method = row.payment_method.lower()
    if settings.backfill_scope == BACKFILL_CREDIT_AND_FREE:
        return method in ("credit", "free")
    return method == "credit"


def normalize(rows: Iterable, config: Config) -> list:
    s = config.settings
    for row in rows:
        if not row.is_completed:
            continue

        # (a) zero-revenue backfill from the old per-session rates
        if _backfill_applies(row, s):
            rate = s.session_rates[row.pricing_plan.lower()]
            row.revenue = money(rate)
            row.adjustment = "zero_backfill"
            row.adjustment_note = (
                f"₱0 → ₱{rate:,.0f} ({row.pricing_plan} rate)")
            continue

        # (b) Hyrox walk-in deduction
        if (row.appointment_name.lower() == HYROX_WITH_COACH
                and "walk-in" in row.variant.lower()):
            before = row.revenue
            row.revenue = money(before - s.hyrox_walkin_deduction)
            row.adjustment = "hyrox_walkin"
            row.adjustment_note = (
                f"₱{before:,.0f} → ₱{row.revenue:,.0f} (walk-in −₱{s.hyrox_walkin_deduction:,.0f})")
            continue

        # (c) Awaken Force fixed per-session revenue
        if row.pricing_plan.lower() == "awaken force":
            before = row.revenue
            row.revenue = money(s.awaken_force_revenue)
            row.adjustment = "awaken_force"
            row.adjustment_note = f"₱{before:,.0f} → ₱{row.revenue:,.0f} (Awaken Force rate)"
    return list(rows)


# --------------------------------------------------------------------------
# 6. compute — commission and delegation charge
# --------------------------------------------------------------------------

def compute(rows: Iterable, config: Config) -> list:
    for row in rows:
        if not row.is_completed:
            continue

        # Delegation short-circuits every coach rule.
        if row.is_delegation:
            d = row.delegator
            row.rule = "delegation"
            row.rate_type = FLAT
            row.rate_value = d.cost
            row.commission = money(d.cost)
            row.delegation_charge = money(d.rate)
            continue

        rate = config.coach_rates.get(row.staff_raw)
        if rate is None:
            continue

        rate_type, rate_value = rate.for_plan(row.pricing_plan)
        row.rate_type, row.rate_value = rate_type, rate_value
        if rate_type == FLAT:
            row.rule = "coach_flat"
            row.commission = money(rate_value)
        else:
            row.rule = ("plan_override"
                        if rate.override_rate_type
                        and row.pricing_plan.lower() in rate.override_plans
                        else "coach_percent")
            row.commission = money(row.revenue * rate_value)
    return list(rows)


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def run(data: bytes | str, config: Config) -> Result:
    rows = clean(parse(data))
    parsed = len(rows)
    rows, dropped = scope(rows, config)
    rows, unmapped, unknown = resolve(rows, config)
    rows = normalize(rows, config)
    rows = compute(rows, config)
    return Result(rows=rows, dropped=dropped, unmapped_staff=unmapped,
                  unknown_codes=unknown, parsed_count=parsed)
