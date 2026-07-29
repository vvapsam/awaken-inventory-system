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


def _floor(d):
    """An open-ended effective_from sorts earliest."""
    return d or date.min


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
    """How one coach is paid.

    ``overrides`` maps a lowercased pricing plan to the (basis, rate) that
    plan pays instead of the default — so each plan can differ from the next,
    not just differ from the default as a group.
    """
    coach: str
    rate_type: str                      # FLAT | PERCENT
    rate_value: Decimal                 # 750.00, or 0.70
    overrides: dict = field(default_factory=dict)   # plan -> (type, value)

    def has_override(self, plan: str) -> bool:
        return (plan or "").strip().lower() in self.overrides

    def for_plan(self, plan: str) -> tuple[str, Decimal]:
        hit = self.overrides.get((plan or "").strip().lower())
        return hit if hit else (self.rate_type, self.rate_value)


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


@dataclass(frozen=True)
class SessionRate:
    """One plan's per-session rate, valid over a date range.

    Dated because rates change: a booking has to be valued with the rate that
    applied on the day it happened, otherwise re-running an old month quietly
    reprices it.
    """
    plan: str
    rate: Decimal
    sessions: int | None = None
    program: str | None = None
    #: What the export bills for the whole pack. Awaken Force reports credit 1
    #: on every row regardless of pack size, so this is the only thing that
    #: tells a 1-session pack from an 8-session one.
    package_total: Decimal | None = None
    effective_from: date | None = None
    effective_to: date | None = None

    def covers(self, on: date | None) -> bool:
        if on is None:
            return True
        if self.effective_from and on < self.effective_from:
            return False
        if self.effective_to and on > self.effective_to:
            return False
        return True


PT = "Private Coaching"
AWAKEN_FORCE = "Awaken Force"

DEFAULT_SESSION_RATES = (
    SessionRate("1 Session", Decimal("1900"), 1, PT),
    SessionRate("8 Sessions", Decimal("1800"), 8, PT),
    SessionRate("12 Sessions", Decimal("1700"), 12, PT),
    SessionRate("24 Sessions", Decimal("1600"), 24, PT),
    SessionRate("36 Sessions", Decimal("1500"), 36, PT),
    SessionRate("Awaken Force", Decimal("1500"), 1, AWAKEN_FORCE,
                package_total=Decimal("1500")),
    SessionRate("Awaken Force", Decimal("1200"), 8, AWAKEN_FORCE,
                package_total=Decimal("9600")),
)


@dataclass
class Settings:
    session_rates: tuple = DEFAULT_SESSION_RATES
    hyrox_walkin_deduction: Decimal = Decimal("1000")
    awaken_force_revenue: Decimal = Decimal("1200")
    #: Prepaid AND comped ₱0 session rows get the old per-session rate, so the
    #: revenue column always carries a real amount. Must stay in step with
    #: COMMISSION_SETTING_DEFAULTS in models.py — two defaults for one setting
    #: is how the app and the engine end up disagreeing.
    backfill_scope: str = BACKFILL_CREDIT_AND_FREE
    #: Applied to a bare "Delegation" variant that names no code.
    default_delegator: str | None = None
    #: Booking statuses to import, lowercased. None means take everything —
    #: filtering happens at import so a run only ever holds rows you chose.
    statuses: frozenset | None = None
    #: Literal reading of the original rule. Matches nothing in practice; see
    #: the module docstring for why it is deliberately not widened.
    excluded_plan_words: tuple = ("free",)

    def rate_for(self, plan: str, on: date | None,
                 package: Decimal | None = None,
                 credits: int | None = None) -> SessionRate | None:
        """The session rate for a plan on a given date.

        A plan can have several rates — Awaken Force sells a 1-session and an
        8-session pack under one plan name — so the pack is identified in
        order of how trustworthy the signal is:

        1. the exported package total, when a rate records one;
        2. the export's session count (``Total pricing plan credit``), which is
           right for the Private Coaching packs but always 1 for Awaken Force;
        3. whatever single rate is left.

        Where date ranges overlap the most recently effective wins, so a new
        rate can be added without closing the old one first.
        """
        key = (plan or "").strip().lower()
        live = [r for r in self.session_rates
                if r.plan.strip().lower() == key and r.covers(on)]
        if not live:
            return None

        def newest(rows):
            return max(rows, key=lambda r: _floor(r.effective_from))

        if len(live) > 1 and package is not None:
            exact = [r for r in live
                     if r.package_total is not None and r.package_total == package]
            if exact:
                return newest(exact)
        if len(live) > 1 and credits:
            same = [r for r in live if r.sessions == credits]
            if same:
                return newest(same)
        return newest(live)


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
    #: "Total pricing plan credit" — the pack size for session plans.
    credits: int | None = None

    # resolved
    coach: str | None = None
    delegator: Delegator | None = None
    delegator_assumed: bool = False

    #: Set by a reviewer to pay a non-Completed booking (a late cancel that was
    #: still charged, a no-show the coach turned up for). Completed rows never
    #: need it.
    approved: bool = False

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

    @property
    def is_commissionable(self) -> bool:
        """Completed by default, or any other status a reviewer has approved."""
        return self.is_completed or self.approved


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
        done = [r for r in self.rows if r.is_commissionable]
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
    "total pricing plan credit": "credits",
}

#: Columns the original spec drops. Listed so the intent stays visible even
#: though we simply never read them.
IGNORED = (
    "paid on", "used credit", "sales channel",
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
            elif field_name == "credits":
                row.credits = int(value) if value.strip().isdigit() else None
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
    wanted = config.settings.statuses
    for row in rows:
        if not row.staff_raw or row.staff_raw == "--":
            dropped.append(Dropped(row, "no staff assigned"))
            continue
        if wanted is not None and row.booking_status.strip().lower() not in wanted:
            dropped.append(Dropped(row, f'status "{row.booking_status}" not selected'))
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
    if settings.rate_for(row.pricing_plan, row.appointment_date,
                         package=row.revenue_raw or None,
                         credits=row.credits) is None:
        return False
    method = row.payment_method.lower()
    if settings.backfill_scope == BACKFILL_CREDIT_AND_FREE:
        return method in ("credit", "free")
    return method == "credit"


def normalize_row(row: Row, config: Config) -> Row:
    """Apply the three revenue adjustments to one commissionable row.

    Resets to the exported revenue first, so this is safe to re-run when a
    reviewer approves or un-approves a booking.
    """
    s = config.settings
    row.revenue = row.revenue_raw
    row.adjustment = None
    row.adjustment_note = ""
    if not row.is_commissionable:
        return row

    # (a) zero-revenue backfill from the per-session rate in force that day
    if _backfill_applies(row, s):
        found = s.rate_for(row.pricing_plan, row.appointment_date,
                           package=row.revenue_raw or None, credits=row.credits)
        row.revenue = money(found.rate)
        row.adjustment = "zero_backfill"
        row.adjustment_note = f"₱0 → ₱{found.rate:,.0f} ({row.pricing_plan} rate)"
        return row

    # (b) Hyrox walk-in deduction
    if (row.appointment_name.lower() == HYROX_WITH_COACH
            and "walk-in" in row.variant.lower()):
        before = row.revenue
        row.revenue = money(before - s.hyrox_walkin_deduction)
        row.adjustment = "hyrox_walkin"
        row.adjustment_note = (
            f"₱{before:,.0f} → ₱{row.revenue:,.0f} (walk-in −₱{s.hyrox_walkin_deduction:,.0f})")
        return row

    # (c) Awaken Force: the export bills the whole pack, so value the booking
    # at the per-session rate from its own rate card. The pack is identified by
    # the exported total, because every AF row reports credit 1.
    if row.pricing_plan.lower() == "awaken force":
        found = s.rate_for(row.pricing_plan, row.appointment_date,
                           package=row.revenue_raw or None, credits=row.credits)
        rate = found.rate if found else s.awaken_force_revenue
        before = row.revenue
        row.revenue = money(rate)
        row.adjustment = "awaken_force"
        sessions = found.sessions if found else None
        row.adjustment_note = "₱{:,.0f} → ₱{:,.0f} (Awaken Force{})".format(
            before, row.revenue,
            " · %s-session pack" % sessions if sessions else "")
    return row


def normalize(rows: Iterable, config: Config) -> list:
    for row in rows:
        normalize_row(row, config)
    return list(rows)


# --------------------------------------------------------------------------
# 6. compute — commission and delegation charge
# --------------------------------------------------------------------------

def compute_row(row: Row, config: Config) -> Row:
    """Commission + delegation charge for one row. Safe to re-run."""
    row.rule = row.rate_type = None
    row.rate_value = row.commission = row.delegation_charge = None
    if not row.is_commissionable:
        return row

    # Delegation short-circuits every coach rule.
    if row.is_delegation:
        d = row.delegator
        row.rule = "delegation"
        row.rate_type = FLAT
        row.rate_value = d.cost
        row.commission = money(d.cost)
        row.delegation_charge = money(d.rate)
        return row

    rate = config.coach_rates.get(row.staff_raw)
    if rate is None:
        return row

    rate_type, rate_value = rate.for_plan(row.pricing_plan)
    row.rate_type, row.rate_value = rate_type, rate_value
    overridden = rate.has_override(row.pricing_plan)
    if rate_type == FLAT:
        row.rule = "plan_override" if overridden else "coach_flat"
        row.commission = money(rate_value)
    else:
        row.rule = "plan_override" if overridden else "coach_percent"
        row.commission = money(row.revenue * rate_value)
    return row


def compute(rows: Iterable, config: Config) -> list:
    for row in rows:
        compute_row(row, config)
    return list(rows)


def recompute_row(row: Row, config: Config) -> Row:
    """Re-run normalisation and commission for a single row — used when a
    reviewer approves or un-approves a non-Completed booking."""
    normalize_row(row, config)
    return compute_row(row, config)


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
