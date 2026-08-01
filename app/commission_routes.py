"""Coach commission screens.

Registered from main.py via ``register(app, deps)`` so this module never
imports main — the render/require helpers arrive as dependencies instead.

Phases implemented here:
  2  configuration — coach rates, delegators, rules
  3  upload & preview — CSV in, batch review, per-coach detail
  4  finalize — coach payouts and delegator charges, in their own tables
"""

from __future__ import annotations

import calendar
import hashlib
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import commissions as engine
from .db import get_db
from .mailer import Mailer, looks_like_email
from .models import (
    BOOKING_STATUSES, IMPORT_MERGE, IMPORT_REPLACE,
    COMMISSION_DELEGATOR_DEFAULTS, COMMISSION_FLAT, COMMISSION_PERCENT,
    COMMISSION_RATE_DEFAULTS, COMMISSION_RATE_TYPES, COMMISSION_SETTING_DEFAULTS,
    COMMISSION_SESSION_RATE_DEFAULTS, CommissionSessionRate, AWAKEN_FORCE,
    RUN_DRAFT, RUN_FINALIZED, RUN_SUPERSEDED,
    CommissionBooking, CommissionCharge, CommissionChargeLine,
    CommissionCoachOverride, CommissionCoachRate, CommissionDelegator, CommissionPayout,
    CommissionPayoutLine, CommissionRun, CommissionSetting, CommissionSignoff,
    CommissionStatementLink, STATEMENT_LINK_DAYS, Staff,
)

MONTHS = ("January February March April May June July August September "
          "October November December").split()


def _skey(status: str) -> str:
    """Form-field-safe key for a status name: 'Late cancelled' -> 'late_cancelled'."""
    return re.sub(r"[^a-z0-9]+", "_", (status or "").strip().lower()).strip("_")


# --------------------------------------------------------------------------
# seeding + config
# --------------------------------------------------------------------------

def seed(db: Session) -> None:
    """Idempotent. Runs at startup; safe on every boot."""
    if not db.query(CommissionCoachRate).first():
        for d in COMMISSION_RATE_DEFAULTS:
            d = dict(d)
            overrides = d.pop("overrides", [])
            rate = CommissionCoachRate(**d)
            rate.overrides = [
                CommissionCoachOverride(plan=plan, rate_type=rtype, rate_value=value)
                for plan, rtype, value in overrides]
            db.add(rate)
    if not db.query(CommissionDelegator).first():
        for d in COMMISSION_DELEGATOR_DEFAULTS:
            db.add(CommissionDelegator(**d))
    if not db.query(CommissionSessionRate).first():
        legacy = db.query(CommissionSetting).filter_by(key="session_rates").first()
        seeded = _legacy_session_rates(legacy.value) if legacy else []
        for d in (seeded or COMMISSION_SESSION_RATE_DEFAULTS):
            db.add(CommissionSessionRate(**d))
        db.commit()
    # Awaken Force used to live in a standalone setting. Give it rows in the
    # rate card so it sits beside the Private Coaching packs, once.
    if not db.query(CommissionSessionRate).filter(
            CommissionSessionRate.plan.ilike("awaken force")).first():
        for d in COMMISSION_SESSION_RATE_DEFAULTS:
            if d.get("program") == AWAKEN_FORCE:
                db.add(CommissionSessionRate(**d))
    existing = {s.key for s in db.query(CommissionSetting).all()}
    for key, (value, _label, _help) in COMMISSION_SETTING_DEFAULTS.items():
        if key not in existing:
            db.add(CommissionSetting(key=key, value=value))
    db.commit()

    # Link rates/delegators to real entity rows by name where we can, so the
    # coach on a payout is the same person as everywhere else in the app.
    for rate in db.query(CommissionCoachRate).filter(CommissionCoachRate.coach_id.is_(None)):
        match = (db.query(Staff).filter(Staff.name.ilike(rate.coach), Staff.is_active == True)  # noqa: E712
                 .first())
        if match:
            rate.coach_id = match.id
    for d in db.query(CommissionDelegator).filter(CommissionDelegator.entity_id.is_(None)):
        match = db.query(Staff).filter(Staff.name.ilike(d.name)).first()
        if match:
            d.entity_id = match.id
    db.commit()


def settings_map(db: Session) -> dict:
    out = {k: v for k, (v, _l, _h) in COMMISSION_SETTING_DEFAULTS.items()}
    for s in db.query(CommissionSetting).all():
        out[s.key] = s.value
    return out


def _legacy_session_rates(text: str) -> list:
    """One-time migration of the old \"12 sessions=1700,...\" setting string
    into rows, so an existing install keeps its numbers."""
    out = []
    for part in (text or "").split(","):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        try:
            rate = Decimal(value.strip())
        except Exception:
            continue
        digits = re.match(r"\s*(\d+)", name.strip())
        out.append(dict(plan=name.strip().title(), rate=rate,
                        sessions=int(digits.group(1)) if digits else None))
    return out


def build_config(db: Session) -> engine.Config:
    """Turn the editable database rows into an engine Config."""
    s = settings_map(db)
    rates = {}
    for r in db.query(CommissionCoachRate).filter(CommissionCoachRate.is_active == True):  # noqa: E712
        rates[r.staff_raw] = engine.CoachRate(
            coach=r.coach,
            rate_type=r.rate_type,
            rate_value=Decimal(str(r.rate_value or 0)),
            overrides={o.plan.strip().lower():
                       (o.rate_type, Decimal(str(o.rate_value or 0)))
                       for o in r.live_overrides if (o.plan or "").strip()},
        )
    delegators = []
    for d in db.query(CommissionDelegator).filter(CommissionDelegator.is_active == True):  # noqa: E712
        delegators.append(engine.Delegator(
            name=d.name, codes=frozenset(d.code_list()),
            rate=Decimal(str(d.rate or 0)), cost=Decimal(str(d.cost or 0))))

    st = engine.Settings(
        session_rates=tuple(
            engine.SessionRate(
                plan=r.plan, rate=Decimal(str(r.rate or 0)), sessions=r.sessions,
                program=r.program,
                package_total=(Decimal(str(r.package_total))
                               if r.package_total is not None else None),
                effective_from=r.effective_from, effective_to=r.effective_to)
            for r in db.query(CommissionSessionRate)
            .filter(CommissionSessionRate.is_active == True)),  # noqa: E712
        hyrox_walkin_deduction=Decimal(s["hyrox_walkin_deduction"] or 0),
        awaken_force_revenue=Decimal(s["awaken_force_revenue"] or 0),
        backfill_scope=s["backfill_scope"],
        default_delegator=(s.get("default_delegator") or None),
        paid_statuses=frozenset(
            k for k in (engine.status_key(p)
                        for p in (s.get("paid_statuses") or "").split(","))
            if k) or engine.DEFAULT_PAID_STATUSES,
    )
    return engine.Config(coach_rates=rates, delegators=delegators, settings=st)


def people(db: Session):
    """Coaches and affiliates, for linking a rate row to a real person rather
    than repeating their name as free text."""
    return (db.query(Staff)
            .filter(Staff.person_type.in_(("coach", "affiliate", "employee")))
            .order_by(Staff.name).all())


def plan_choices(db: Session) -> list:
    """Plans you can write an override against.

    Sourced from what the exports have actually contained, plus the session
    rate card, plus the two plans that are always priced separately — so the
    dropdown offers real plan spellings instead of trusting anyone to retype
    them.
    """
    seen, out = set(), []
    for name in ("Drop-In", "Awaken Force"):
        seen.add(name.lower())
        out.append(name)
    rows = db.query(CommissionSessionRate.plan).distinct().all()
    rows += db.query(CommissionBooking.pricing_plan).distinct().all()
    rows += db.query(CommissionCoachOverride.plan).distinct().all()
    extra = []
    for (plan,) in rows:
        plan = (plan or "").strip()
        if not plan or plan.lower() in seen:
            continue
        seen.add(plan.lower())
        extra.append(plan)
    return out + sorted(extra, key=str.lower)


def _delegator_ids(db: Session) -> dict:
    """Engine Delegators are frozen value objects; map name -> row id."""
    return {d.name: d.id for d in db.query(CommissionDelegator).all()}


def _next_number(db, model, prefix):
    nums = []
    for (n,) in db.query(model.number).all():
        try:
            nums.append(int(str(n).split("-")[-1]))
        except (ValueError, TypeError):
            pass
    return "%s-%04d" % (prefix, (max(nums) + 1) if nums else 1)


def _money(v) -> Decimal:
    return engine.money(Decimal(str(v or 0)))


def _fmt(v) -> str:
    return "₱{:,.2f}".format(float(v or 0))


# --------------------------------------------------------------------------
# summaries used by the preview screen
# --------------------------------------------------------------------------

def _live(run: CommissionRun):
    return [b for b in run.bookings if not b.dropped_reason]


def pivot(run: CommissionRun):
    """Coaches as rows, pricing plans as columns, session counts. Delegation is
    its own column — every delegation row carries plan='Drop-In' and would
    otherwise be miscounted there."""
    done = [b for b in _live(run) if b.is_commissionable]
    plans, rows = [], {}
    for b in done:
        key = "Delegation" if b.delegator_id else (b.pricing_plan or "—")
        if key not in plans:
            plans.append(key)
        r = rows.setdefault(b.coach or b.staff_raw, {})
        r[key] = r.get(key, 0) + 1
        r["_sessions"] = r.get("_sessions", 0) + 1
        r["_revenue"] = r.get("_revenue", Decimal(0)) + Decimal(str(b.revenue or 0))
        r["_commission"] = r.get("_commission", Decimal(0)) + Decimal(str(b.commission or 0))
    plans.sort(key=lambda p: (p == "Delegation", p))
    totals = {p: sum(r.get(p, 0) for r in rows.values()) for p in plans}
    totals["_sessions"] = sum(r.get("_sessions", 0) for r in rows.values())
    totals["_revenue"] = sum((r.get("_revenue", Decimal(0)) for r in rows.values()), Decimal(0))
    totals["_commission"] = sum((r.get("_commission", Decimal(0)) for r in rows.values()),
                                Decimal(0))
    return plans, sorted(rows.items()), totals


def run_totals(run: CommissionRun) -> dict:
    done = [b for b in _live(run) if b.is_commissionable]
    dele = [b for b in done if b.delegator_id]
    charged = sum((Decimal(str(b.delegation_charge or 0)) for b in dele), Decimal(0))
    cost = sum((Decimal(str(b.commission or 0)) for b in dele), Decimal(0))
    return {
        "sessions": len(done),
        "revenue": sum((Decimal(str(b.revenue or 0)) for b in done), Decimal(0)),
        "commission": sum((Decimal(str(b.commission or 0)) for b in done), Decimal(0)),
        "delegation_sessions": len(dele),
        "delegation_cost": cost,
        "delegation_charged": charged,
        "delegation_margin": charged - cost,
    }


def signoffs(run: CommissionRun, db: Session) -> dict:
    """coach -> the sign-off row, for the coaches whose figures are confirmed."""
    rows = db.query(CommissionSignoff).filter_by(run_id=run.id).all()
    return {s.coach: s for s in rows}


def void_signoff(db: Session, rid: int, coach: str | None = None) -> None:
    """Drop a sign-off because the figures behind it changed.

    A confirmation is only worth anything if it can't outlive the numbers it
    confirmed, so anything that moves the money clears it and asks again.
    """
    q = db.query(CommissionSignoff).filter_by(run_id=rid)
    if coach is not None:
        q = q.filter_by(coach=coach)
    for row in q.all():
        db.delete(row)


def unsigned(run: CommissionRun, db: Session) -> list:
    """Coaches carrying commission that nobody has confirmed yet."""
    signed = signoffs(run, db)
    return sorted({(b.coach or b.staff_raw) for b in _live(run)
                   if b.is_commissionable and (b.coach or b.staff_raw) not in signed})


def blockers(run: CommissionRun, db: Session | None = None) -> dict:
    """What stands between this run and a payout.

    Unmapped staff and unrecognised delegator codes would silently drop money;
    an unsigned coach means figures nobody has confirmed. All three stop a
    finalize rather than producing a payout somebody has to unpick later.
    """
    unmapped, unknown = {}, {}
    for b in _live(run):
        if not b.coach:
            unmapped[b.staff_raw or "(blank)"] = unmapped.get(b.staff_raw or "(blank)", 0) + 1
        if re.search(r"\bdelegation\b", b.variant or "", re.I) and not b.delegator_id:
            unknown[b.variant] = unknown.get(b.variant, 0) + 1
    # Only a draft can be finalized, so only a draft can be blocked. A
    # finalized run predates sign-off and must not be told it is missing one.
    waiting = (unsigned(run, db)
               if db is not None and run.status == RUN_DRAFT else [])
    return {"unmapped": unmapped, "unknown": unknown, "unsigned": waiting,
            "blocking": bool(unmapped or unknown or waiting)}


#: Statuses that carry no commission unless a reviewer approves them.
REVIEWABLE = ("cancelled", "late cancelled", "no show", "booked")


def pending(run: CommissionRun):
    """Bookings whose status doesn't pay on its own and that nobody has
    approved — the ones still awaiting a decision."""
    return [b for b in _live(run) if not b.pays_by_status and not b.approved]


def status_groups(run: CommissionRun, pick: str | None = None) -> dict:
    """Every booking in the run, bucketed by export status.

    The Rezerv export can be filtered to Completed only; when it isn't, this is
    where the rest of the data becomes visible instead of being buried inside
    each coach's page.
    """
    order = list(BOOKING_STATUSES)
    rows = _live(run)
    buckets, seen = [], set()
    for status in order + sorted({(b.booking_status or "—") for b in rows}):
        key = (status or "—").strip()
        if key.lower() in seen:
            continue
        seen.add(key.lower())
        items = [b for b in rows
                 if (b.booking_status or "—").strip().lower() == key.lower()]
        buckets.append({
            "status": key,
            "rows": sorted(items, key=lambda r: (r.appointment_date or date.min,
                                                 r.coach or r.staff_raw or "")),
            "count": len(items),
            "counting": len([b for b in items if b.is_commissionable]),
            "revenue": sum((Decimal(str(b.revenue or 0)) for b in items), Decimal(0)),
            "commission": sum((Decimal(str(b.commission or 0))
                               for b in items if b.is_commissionable), Decimal(0)),
        })
    chosen = next((b for b in buckets
                   if pick and b["status"].lower() == pick.strip().lower()), None)
    return {"buckets": buckets, "chosen": chosen,
            "total": len(rows)}


def coach_summary(run: CommissionRun):
    """Per-coach counts for the review screen — what's paying, what's waiting."""
    out = {}
    for b in _live(run):
        row = out.setdefault(b.coach or b.staff_raw, {
            "coach": b.coach or b.staff_raw, "counted": 0, "pending": 0,
            "approved": 0, "commission": Decimal(0)})
        if b.is_commissionable:
            row["counted"] += 1
            row["commission"] += Decimal(str(b.commission or 0))
            if b.approved:
                row["approved"] += 1
        elif not b.pays_by_status:
            row["pending"] += 1
    return sorted(out.values(), key=lambda r: r["coach"])


def _to_engine_row(b: CommissionBooking, delegator) -> engine.Row:
    """Bridge a stored booking back into an engine Row so a single booking can
    be recalculated with exactly the same rules as the batch."""
    return engine.Row(
        booking_ref=b.booking_ref or "", customer=b.customer or "",
        appointment_date=b.appointment_date,
        appointment_name=b.appointment_name or "", variant=b.variant or "",
        staff_raw=b.staff_raw or "", booking_status=b.booking_status or "",
        pricing_plan=b.pricing_plan or "", payment_method=b.payment_method or "",
        revenue_raw=Decimal(str(b.revenue_raw or 0)),
        coach=b.coach, delegator=delegator, approved=bool(b.approved),
    )


def delegator_map(db: Session) -> dict:
    """id -> engine Delegator, so a whole run can be recalculated without
    re-querying per booking."""
    return {d.id: engine.Delegator(
        name=d.name, codes=frozenset(d.code_list()),
        rate=Decimal(str(d.rate or 0)), cost=Decimal(str(d.cost or 0)))
        for d in db.query(CommissionDelegator).all()}


def recompute(b: CommissionBooking, config: engine.Config, db: Session,
              dmap: dict | None = None) -> None:
    """Recalculate one booking in place — after its approval flag changes, or
    when the rules behind it have been edited."""
    if dmap is None:
        dmap = delegator_map(db)
    delegator = dmap.get(b.delegator_id) if b.delegator_id else None
    # A hand-typed rate is held back from the engine and re-applied after, so
    # editing a coach's default can't quietly overwrite a deliberate correction.
    manual = None
    if b.rate_manual and b.rate_type:
        manual = (b.rate_type, Decimal(str(b.rate_value or 0)))
    row = engine.recompute_row(_to_engine_row(b, delegator), config)
    # Re-snapshot which statuses pay: Recalculate exists precisely so a rule
    # change reaches a draft that was imported under the old one.
    b.pays_by_status = row.pays_by_status
    b.revenue = row.revenue
    b.adjustment = row.adjustment
    b.adjustment_note = row.adjustment_note
    b.rule = row.rule
    b.rate_type = row.rate_type
    b.rate_value = row.rate_value
    b.commission = row.commission
    b.delegation_charge = row.delegation_charge
    if manual and row.commission is not None and row.rule != "delegation":
        b.rate_type, b.rate_value = manual
        b.rule = "manual"
        b.commission = engine.money(
            manual[1] if manual[0] == COMMISSION_FLAT
            else Decimal(str(row.revenue or 0)) * manual[1])
    elif manual and (row.commission is None or row.rule == "delegation"):
        # The row stopped being payable, or became a delegation settled with
        # the delegator; either way the manual rate no longer has anything to
        # apply to, so drop the flag rather than leave a stale marker.
        b.rate_manual = False


# --------------------------------------------------------------------------
# delegation — the delegator's side of the business
# --------------------------------------------------------------------------

#: Cell colours for the schedule matrix, in assignment order.
COACH_SWATCHES = ("c1", "c5", "c2", "c4", "c3", "c6", "c7", "c8")


def coach_codes(names) -> dict:
    """name -> a short code that fits a calendar cell.

    Initials where the name has several words, otherwise the first letters.
    Collisions get a digit rather than being silently merged — two coaches
    sharing a cell label would misread the whole matrix.
    """
    out, taken = {}, set()
    for name in sorted({(n or "").strip() for n in names if (n or "").strip()}):
        parts = [p for p in re.split(r"[\s.]+", name) if p]
        if len(parts) >= 2:
            code = (parts[0][0] + parts[-1][0]).upper()
        else:
            code = parts[0][:2].upper()
        base, n = code, 2
        while code in taken:
            code = "%s%d" % (base[:1], n)
            n += 1
        taken.add(code)
        out[name] = code
    return out


def delegated_rows(run: CommissionRun):
    """Every delegated booking in a run, oldest first."""
    return sorted((b for b in _live(run) if b.delegator_id),
                  key=lambda b: (b.appointment_date or date.min, b.booking_ref or ""))


def delegator_rollup(run: CommissionRun, db: Session) -> list:
    """One row per delegator: volume, reach, and the margin on their sessions."""
    rows = delegated_rows(run)
    by_id = {}
    for b in rows:
        d = by_id.setdefault(b.delegator_id, {
            "delegator": b.delegator, "rows": [], "clients": set(), "coaches": set()})
        d["rows"].append(b)
        if b.customer:
            d["clients"].add(b.customer.strip())
        if b.coach or b.staff_raw:
            d["coaches"].add((b.coach or b.staff_raw).strip())
    out = []
    for d in by_id.values():
        counted = [b for b in d["rows"] if b.is_commissionable]
        charged = sum((Decimal(str(b.delegation_charge or 0)) for b in counted), Decimal(0))
        cost = sum((Decimal(str(b.commission or 0)) for b in counted), Decimal(0))
        out.append({
            "delegator": d["delegator"],
            "sessions": len(d["rows"]),
            "counting": len(counted),
            "clients": len(d["clients"]),
            "coaches": len(d["coaches"]),
            "charged": charged,
            "cost": cost,
            "margin": charged - cost,
        })
    return sorted(out, key=lambda r: (-r["sessions"],
                                      (r["delegator"].name if r["delegator"] else "")))


def schedule_matrix(rows) -> dict:
    """Clients down the side, days of the month across the top.

    Reading down a column says who trained that day and with whom; reading
    across a row says whether a client has one regular coach or is being passed
    around. Neither question is answerable from a date-sorted list.
    """
    dates = [b.appointment_date for b in rows if b.appointment_date]
    if not dates:
        return {"days": [], "clients": [], "codes": {}, "colors": {}, "totals": {},
                "month": None}
    month_start = min(dates).replace(day=1)
    last = max(dates)
    # Whole calendar month, so the columns line up week to week even where a
    # day has no sessions.
    ndays = calendar.monthrange(month_start.year, month_start.month)[1]
    days = [month_start.replace(day=n) for n in range(1, ndays + 1)]
    if last.month != month_start.month or last.year != month_start.year:
        # An export straddling two months: fall back to the exact span.
        days = []
        cur = month_start
        while cur <= last:
            days.append(cur)
            cur = cur + timedelta(days=1)

    cells = {}
    for b in rows:
        if not b.appointment_date:
            continue
        cells.setdefault((b.customer or "—").strip(), {}) \
             .setdefault(b.appointment_date, []).append(b)
    names = sorted(cells, key=lambda c: (-sum(len(v) for v in cells[c].values()), c))
    codes = coach_codes((b.coach or b.staff_raw) for b in rows)
    colors = {name: COACH_SWATCHES[i % len(COACH_SWATCHES)]
              for i, name in enumerate(sorted(codes))}
    clients = []
    for name in names:
        row = []
        for day in days:
            got = cells[name].get(day, [])
            row.append({
                "bookings": got,
                "coach": (got[0].coach or got[0].staff_raw) if got else None,
                "n": len(got),
            })
        clients.append({"client": name, "cells": row,
                        "total": sum(len(v) for v in cells[name].values())})
    totals = {day: sum(len(cells[c].get(day, [])) for c in cells) for day in days}
    return {"days": days, "clients": clients, "codes": codes, "colors": colors,
            "totals": totals, "month": month_start}


def coach_groups(run: CommissionRun, coach: str):
    """One coach's bookings grouped by status, in the spec's order."""
    order = ["Completed", "Cancelled", "Late cancelled", "No show", "Booked"]
    rows = [b for b in _live(run) if (b.coach or b.staff_raw) == coach]
    groups = []
    seen = set()
    for status in order + sorted({(b.booking_status or "") for b in rows}):
        if status in seen:
            continue
        seen.add(status)
        items = [b for b in rows if (b.booking_status or "").lower() == status.lower()]
        if not items:
            continue
        items.sort(key=lambda b: (b.appointment_date or date.min, b.booking_ref or ""))
        groups.append({
            "status": status,
            "rows": items,
            # Whether this status pays without review, so the screen can say
            # "3 to review" on the statuses that need it and stay quiet on the
            # ones that don't.
            "pays_alone": all(b.pays_by_status for b in items),
            # How many of this group actually count — all of them when Completed,
            # otherwise however many the reviewer approved.
            "approved": len([b for b in items if b.is_commissionable]),
            "revenue": sum((Decimal(str(b.revenue or 0)) for b in items), Decimal(0)),
            "commission": sum((Decimal(str(b.commission or 0)) for b in items), Decimal(0)),
        })
    return groups


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def register(app, deps):
    render = deps["render"]
    require_admin = deps["require_admin"]
    templates = deps["templates"]
    require = deps["require"]
    tz = deps["tz"]

    def guard(request, db):
        """Admins, or anyone granted the manage_commissions area."""
        staff, redir = require(request, db, perm="manage_commissions")
        return staff, redir

    def guard_money(request, db):
        """Admins only.

        Reading a run is one thing; deciding what a coach gets paid is another.
        Approving figures and typing a rate onto a booking both change money
        leaving the business, so they need the admin role rather than the
        manage_commissions area a reviewer might hold.
        """
        return require_admin(request, db)

    # ---------------------------------------------------------------- phase 2

    @app.get("/admin/commission-rates", response_class=HTMLResponse)
    def commission_rates(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        rates = db.query(CommissionCoachRate).order_by(CommissionCoachRate.coach).all()
        return render(request, "commission_rates.html", db, staff, rates=rates,
                      rate_types=COMMISSION_RATE_TYPES, people=people(db),
                      plans=plan_choices(db), active="rates")

    @app.post("/admin/commission-rates/{rid}")
    def commission_rate_update(
            request: Request, rid: int,
            coach: str = Form(...), staff_raw: str = Form(...),
            coach_id: str = Form(""),
            rate_type: str = Form(COMMISSION_PERCENT), rate_value: str = Form("0"),
            is_active: str = Form(""),
            ov_id: list[str] = Form([]), ov_plan: list[str] = Form([]),
            ov_type: list[str] = Form([]), ov_value: list[str] = Form([]),
            ov_active: list[str] = Form([]),
            new_plan: str = Form(""), new_type: str = Form(COMMISSION_PERCENT),
            new_value: str = Form(""),
            db: Session = Depends(get_db)):
        """One save for the whole coach — default rate and every override row.

        The popup is the only place these are edited, so they commit together;
        that is what stops a half-saved coach from existing.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        r = db.get(CommissionCoachRate, rid)
        if not r:
            return RedirectResponse("/admin/commission-rates", status_code=303)
        r.coach_id = int(coach_id) if coach_id.strip().isdigit() else None
        linked = db.get(Staff, r.coach_id) if r.coach_id else None
        # The person record is the source of truth for the name; bookings are
        # grouped on this text, so it has to follow the entity.
        r.coach = (linked.name if linked else coach.strip())
        r.staff_raw = staff_raw.strip()
        r.rate_type = rate_type
        r.rate_value = _num(rate_value, rate_type)
        r.is_active = (is_active == "on")

        existing = {o.id: o for o in r.overrides}
        # No autoflush while the rows are half-rewritten: two rows swapping
        # plans is only a duplicate in between, and the constraint is deferred
        # to commit for exactly that reason.
        with db.no_autoflush:
            for i, oid in enumerate(ov_id):
                o = existing.get(int(oid)) if oid.strip().isdigit() else None
                if o is None:
                    continue
                plan = (ov_plan[i] if i < len(ov_plan) else "").strip()
                if not plan:                   # blanking the plan removes it
                    db.delete(o)
                    continue
                otype = (ov_type[i] if i < len(ov_type) else COMMISSION_PERCENT)
                o.plan = plan
                o.rate_type = otype
                o.rate_value = _num(ov_value[i] if i < len(ov_value) else "0", otype)
                o.is_active = ((ov_active[i] if i < len(ov_active) else "") == "on")

            # Renaming one override onto another's plan would leave two rules
            # for the same plan; the later row loses.
            kept = set()
            for o in list(r.overrides):
                key = (o.plan or "").strip().lower()
                if key in kept:
                    db.delete(o)
                else:
                    kept.add(key)

        if new_plan.strip():
            plan = new_plan.strip()
            db.flush()
            # Adding a plan the coach already overrides edits that row rather
            # than failing on the unique index — two rules for one plan would
            # be ambiguous, and the newer number is the one meant.
            dup = next((o for o in r.overrides
                        if (o.plan or "").strip().lower() == plan.lower()), None)
            if dup is not None:
                dup.plan, dup.rate_type = plan, new_type
                dup.rate_value, dup.is_active = _num(new_value, new_type), True
            else:
                db.add(CommissionCoachOverride(
                    rate_id=r.id, plan=plan, rate_type=new_type,
                    rate_value=_num(new_value, new_type), is_active=True))
        db.commit()
        return RedirectResponse("/admin/commission-rates", status_code=303)

    @app.post("/admin/commission-rates/{rid}/override-delete")
    def commission_override_delete(request: Request, rid: int,
                                   ov_delete: str = Form(""),
                                   db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        o = db.get(CommissionCoachOverride, int(ov_delete)) \
            if ov_delete.strip().isdigit() else None
        if o and o.rate_id == rid:
            db.delete(o)
            db.commit()
        return RedirectResponse("/admin/commission-rates", status_code=303)

    @app.post("/admin/commission-rates/new")
    def commission_rate_new(request: Request, coach: str = Form(""),
                            staff_raw: str = Form(...), coach_id: str = Form(""),
                            rate_type: str = Form(COMMISSION_PERCENT),
                            rate_value: str = Form("0"),
                            db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        cid = int(coach_id) if coach_id.strip().isdigit() else None
        linked = db.get(Staff, cid) if cid else None
        db.add(CommissionCoachRate(
            coach=(linked.name if linked else coach.strip()),
            coach_id=cid, staff_raw=staff_raw.strip(), rate_type=rate_type,
            rate_value=_num(rate_value, rate_type)))
        db.commit()
        return RedirectResponse("/admin/commission-rates", status_code=303)

    @app.get("/admin/commission-delegators", response_class=HTMLResponse)
    def commission_delegators(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        rows = db.query(CommissionDelegator).order_by(CommissionDelegator.name).all()
        return render(request, "commission_delegators.html", db, staff,
                      delegators=rows, people=people(db), active="delegators")

    @app.post("/admin/commission-delegators/{did}")
    def commission_delegator_update(
            request: Request, did: int, name: str = Form(...), codes: str = Form(""),
            entity_id: str = Form(""), rate: str = Form("0"), cost: str = Form("0"),
            is_active: str = Form(""), db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        d = db.get(CommissionDelegator, did)
        if not d:
            return RedirectResponse("/admin/commission-delegators", status_code=303)
        d.entity_id = int(entity_id) if entity_id.strip().isdigit() else None
        linked = db.get(Staff, d.entity_id) if d.entity_id else None
        d.name = (linked.name if linked else name.strip())
        d.codes = ",".join(c.strip().upper() for c in codes.split(",") if c.strip())
        d.rate = _num(rate, COMMISSION_FLAT)
        d.cost = _num(cost, COMMISSION_FLAT)
        d.is_active = (is_active == "on")
        db.commit()
        return RedirectResponse("/admin/commission-delegators", status_code=303)

    @app.post("/admin/commission-delegators/new")
    def commission_delegator_new(request: Request, name: str = Form(...),
                                 codes: str = Form(""), rate: str = Form("0"),
                                 cost: str = Form("0"),
                                 db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        db.add(CommissionDelegator(
            name=name.strip(),
            codes=",".join(c.strip().upper() for c in codes.split(",") if c.strip()),
            rate=_num(rate, COMMISSION_FLAT), cost=_num(cost, COMMISSION_FLAT)))
        db.commit()
        return RedirectResponse("/admin/commission-delegators", status_code=303)

    @app.get("/admin/commission-session-rates", response_class=HTMLResponse)
    def commission_session_rates(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        rows = (db.query(CommissionSessionRate)
                .order_by(CommissionSessionRate.program,
                          CommissionSessionRate.sessions,
                          CommissionSessionRate.effective_from).all())
        groups = {}
        for r in rows:
            groups.setdefault(r.program or "Other", []).append(r)
        return render(request, "commission_session_rates.html", db, staff,
                      rates=rows, groups=sorted(groups.items()),
                      active="session_rates")

    @app.post("/admin/commission-session-rates/{sid}")
    def commission_session_rate_update(
            request: Request, sid: int, plan: str = Form(...),
            program: str = Form(""), sessions: str = Form(""),
            rate: str = Form("0"), package_total: str = Form(""),
            effective_from: str = Form(""), effective_to: str = Form(""),
            note: str = Form(""), is_active: str = Form(""),
            db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        r = db.get(CommissionSessionRate, sid)
        if not r:
            return RedirectResponse("/admin/commission-session-rates", status_code=303)
        r.plan = plan.strip()
        r.program = program.strip() or None
        r.sessions = int(sessions) if sessions.strip().isdigit() else None
        r.rate = _num(rate, COMMISSION_FLAT)
        r.package_total = (_num(package_total, COMMISSION_FLAT)
                           if package_total.strip() else None)
        r.effective_from = _day(effective_from)
        r.effective_to = _day(effective_to)
        r.note = note.strip() or None
        r.is_active = (is_active == "on")
        db.commit()
        return RedirectResponse("/admin/commission-session-rates", status_code=303)

    @app.post("/admin/commission-session-rates/new")
    def commission_session_rate_new(
            request: Request, plan: str = Form(...), program: str = Form(""),
            sessions: str = Form(""), rate: str = Form("0"),
            package_total: str = Form(""), effective_from: str = Form(""),
            effective_to: str = Form(""), note: str = Form(""),
            db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        db.add(CommissionSessionRate(
            plan=plan.strip(), program=program.strip() or None,
            sessions=int(sessions) if sessions.strip().isdigit() else None,
            rate=_num(rate, COMMISSION_FLAT),
            package_total=(_num(package_total, COMMISSION_FLAT)
                           if package_total.strip() else None),
            effective_from=_day(effective_from), effective_to=_day(effective_to),
            note=note.strip() or None))
        db.commit()
        return RedirectResponse("/admin/commission-session-rates", status_code=303)

    @app.post("/admin/commission-session-rates/{sid}/delete")
    def commission_session_rate_delete(request: Request, sid: int,
                                       db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        r = db.get(CommissionSessionRate, sid)
        if r:
            db.delete(r)
            db.commit()
        return RedirectResponse("/admin/commission-session-rates", status_code=303)

    @app.get("/admin/commission-settings", response_class=HTMLResponse)
    def commission_settings(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        values = settings_map(db)
        defs = [(k, values.get(k, v), label, help_)
                for k, (v, label, help_) in COMMISSION_SETTING_DEFAULTS.items()]
        return render(request, "commission_settings.html", db, staff, defs=defs,
                      active="settings")

    @app.post("/admin/commission-settings")
    async def commission_settings_save(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        form = await request.form()
        for key in COMMISSION_SETTING_DEFAULTS:
            if key not in form:
                continue
            row = db.query(CommissionSetting).filter(CommissionSetting.key == key).first()
            if not row:
                row = CommissionSetting(key=key)
                db.add(row)
            row.value = (form.get(key) or "").strip()
        db.commit()
        return RedirectResponse("/admin/commission-settings", status_code=303)

    # ---------------------------------------------------------------- phase 3

    @app.get("/commissions", response_class=HTMLResponse)
    def commissions_index(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        runs = (db.query(CommissionRun)
                .order_by(CommissionRun.period.desc(), CommissionRun.id.desc()).all())
        rows = [{"run": r, "totals": run_totals(r)} for r in runs]
        return render(request, "commissions.html", db, staff, rows=rows)

    @app.get("/commissions/new", response_class=HTMLResponse)
    def commissions_new(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        return render(request, "commission_new.html", db, staff, error=None,
                      statuses=BOOKING_STATUSES, chosen=list(BOOKING_STATUSES),
                      mode=IMPORT_MERGE, skey=_skey)

    @app.post("/commissions/new")
    async def commissions_upload(request: Request, file: UploadFile = None,
                                 db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        form = await request.form()
        mode = form.get("mode") or IMPORT_MERGE
        chosen = [s for s in BOOKING_STATUSES if form.get("status_" + _skey(s))]
        if not chosen:
            chosen = list(BOOKING_STATUSES)

        def again(error):
            return render(request, "commission_new.html", db, staff, error=error,
                          statuses=BOOKING_STATUSES, chosen=chosen, mode=mode,
                          skey=_skey)

        if file is None:
            return again("Choose a CSV export first.")
        raw = await file.read()
        if not raw:
            return again("That file is empty.")

        config = build_config(db)
        config.settings.statuses = frozenset(s.lower() for s in chosen)
        try:
            result = engine.run(raw, config)
        except Exception as exc:                              # noqa: BLE001
            return again(f"Could not read that file: {exc}")
        if not result.rows and not result.dropped:
            return again("No rows found — is this the Paid Bookings export?")

        dates = [r.appointment_date for r in result.rows if r.appointment_date]
        start, end = (min(dates), max(dates)) if dates else (None, None)
        period = start.strftime("%Y-%m") if start else "unknown"
        label = f"{MONTHS[start.month - 1]} {start.year}" if start else "Unknown period"

        # A booking already paid out in a finalized run must never be imported
        # again — that is the double-payment case, and it is worth guarding
        # regardless of which mode was chosen.
        paid_refs = {
            ref for (ref,) in db.query(CommissionBooking.booking_ref)
            .join(CommissionRun, CommissionBooking.run_id == CommissionRun.id)
            .filter(CommissionRun.status == RUN_FINALIZED,
                    CommissionBooking.dropped_reason.is_(None)).all() if ref}

        existing = (db.query(CommissionRun)
                    .filter(CommissionRun.period == period,
                            CommissionRun.status == RUN_DRAFT).first())

        if mode == IMPORT_REPLACE and existing is not None:
            db.delete(existing)
            db.flush()
            existing = None

        run = existing
        merged_into = run is not None
        if run is None:
            run = CommissionRun(
                period=period, period_label=label,
                period_start=start, period_end=end,
                status=RUN_DRAFT, uploaded_by_id=staff.id,
                uploaded_at=datetime.now(timezone.utc))
            db.add(run)
            db.flush()

        run.source_filename = file.filename or "upload.csv"
        run.source_sha256 = hashlib.sha256(raw).hexdigest()
        if start and (run.period_start is None or start < run.period_start):
            run.period_start = start
        if end and (run.period_end is None or end > run.period_end):
            run.period_end = end

        have = {b.booking_ref for b in run.bookings if b.booking_ref}
        ids = _delegator_ids(db)
        coach_ids = {r.coach: r.coach_id
                     for r in db.query(CommissionCoachRate).all() if r.coach_id}

        added = dup = paid = 0
        for row in result.rows:
            ref = row.booking_ref
            if ref and ref in paid_refs:
                paid += 1
                continue
            if ref and ref in have:
                dup += 1
                continue
            db.add(_booking(run.id, row, ids, coach_ids))
            have.add(ref)
            added += 1
        for drop in result.dropped:
            ref = drop.row.booking_ref
            if ref and (ref in have or ref in paid_refs):
                continue
            b = _booking(run.id, drop.row, ids, coach_ids)
            b.dropped_reason = drop.reason
            db.add(b)
            have.add(ref)

        by_status = {}
        for d in result.dropped:
            if d.reason.startswith("status "):
                s = d.row.booking_status or "(blank)"
                by_status[s] = by_status.get(s, 0) + 1

        note = ["%s · %d row%s read" % (
            "Merged into this run" if merged_into and mode == IMPORT_MERGE
            else "Replaced the previous draft" if mode == IMPORT_REPLACE and merged_into
            else "New run", result.parsed_count,
            "" if result.parsed_count == 1 else "s")]
        note.append("%d imported" % added)
        if dup:
            note.append("%d skipped as already in this run" % dup)
        if paid:
            note.append("%d skipped — already paid in a finalized run" % paid)
        if by_status:
            note.append("filtered out by status: " + ", ".join(
                "%s %d" % (k, v) for k, v in sorted(by_status.items())))
        run.last_import_note = " · ".join(note)

        live = [b for b in run.bookings if not b.dropped_reason]
        run.parsed_count = len(run.bookings)
        run.kept_count = len(live)
        run.dropped_count = len(run.bookings) - len(live)
        db.commit()
        return RedirectResponse(f"/commissions/{run.id}", status_code=303)

    # ------------------------------------------------- coach statement links

    def _public_base(request: Request) -> str:
        """The URL a coach should be given. Railway terminates TLS at its proxy,
        so `request.base_url` reports http:// — pasting that into a message hands
        the coach a link that only works because of a redirect. Trust the
        proxy's forwarded proto when it is present."""
        base = str(request.base_url).rstrip("/")
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        if proto == "https" and base.startswith("http://"):
            base = "https://" + base[len("http://"):]
        return base

    def _links_for(db: Session, rid: int, coach: str):
        return (db.query(CommissionStatementLink)
                .filter_by(run_id=rid, coach=coach)
                .order_by(CommissionStatementLink.id.desc()).all())

    def _current_link(db: Session, rid: int, coach: str):
        """The newest link for a coach — older ones are kept, revoked."""
        rows = _links_for(db, rid, coach)
        return rows[0] if rows else None

    def _issue_link(db: Session, rid: int, coach: str, staff, now):
        """Mint a new link and retire any earlier one.

        The old row stays so its URL keeps answering — with "a newer one was
        sent" rather than a bare not-found, which is what a coach clicking last
        week's email deserves.
        """
        for old in _links_for(db, rid, coach):
            if not old.revoked_at:
                old.revoked_at = now
        link = CommissionStatementLink(
            run_id=rid, coach=coach, token=secrets.token_urlsafe(24),
            created_at=now, created_by_id=getattr(staff, "id", None),
            expires_at=now + timedelta(days=STATEMENT_LINK_DAYS), opens=0)
        db.add(link)
        return link

    def _coach_email(db: Session, coach: str):
        """The address on the coach's person record, if it looks usable."""
        person = db.query(Staff).filter(Staff.name == coach).first()
        email = (person.email or "").strip() if person and person.email else ""
        return email if looks_like_email(email) else ""

    def _statement_email(coach: str, run: CommissionRun, url: str, total, expires):
        """The message itself.

        Short on purpose. The statement is the page; this is the doorway to it,
        and every extra paragraph is one more thing between a coach on a phone
        and the number they opened the message for.
        """
        period = run.period_label or run.period
        subject = f"Your {period} commission — AWAKEN"
        gone = expires.strftime("%d %B") if expires else ""
        first = (coach or "").split()[0] if coach else "there"
        text = (
            f"Hi {first},\n\n"
            f"Your commission for {period} has been reviewed and comes to {_fmt(total)}.\n\n"
            f"See every session behind that figure here:\n{url}\n\n"
            + (f"The link works until {gone}.\n\n" if gone else "")
            + "If something doesn't look right, just reply to this message and we'll "
              "check it before payout.\n\n"
              "— AWAKEN Fitness Center\n")
        html = f"""\
<!DOCTYPE html><html><body style="margin:0;padding:0;background:#eef1f4">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f4;padding:26px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#fff;border-radius:12px;overflow:hidden;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<tr><td style="background:#132a44;padding:20px 26px">
  <div style="font-size:11px;letter-spacing:.3em;font-weight:700;color:#a9bcd8">A W A K E N</div>
  <div style="color:#fff;font-size:19px;font-weight:650;margin-top:6px">Your {period} commission</div>
</td></tr>
<tr><td style="padding:24px 26px 6px;color:#22303d;font-size:15px;line-height:1.55">
  <p style="margin:0 0 16px">Hi {first},</p>
  <p style="margin:0 0 18px">Your commission for {period} has been reviewed. Here's the total:</p>
  <div style="border:1px solid #bfe3dc;background:#f2fbf9;border-radius:10px;padding:16px 18px;text-align:center">
    <div style="font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:#0b6b60;font-weight:700">Total commission</div>
    <div style="font-size:30px;font-weight:700;color:#0b6b60;letter-spacing:-.02em;margin-top:2px">{_fmt(total)}</div>
  </div>
  <p style="margin:20px 0 0;text-align:center">
    <a href="{url}" style="display:inline-block;background:#132a44;color:#fff;text-decoration:none;
       font-weight:650;font-size:15px;padding:13px 30px;border-radius:9px">See every session &rarr;</a>
  </p>
  {'<p style="margin:16px 0 0;text-align:center;color:#7c8794;font-size:12.5px">This link works until ' + gone + '.</p>' if gone else ''}
  <p style="margin:22px 0 0;color:#5b6773;font-size:13.5px">If something doesn't look right, just reply to
    this message and we'll check it before payout.</p>
</td></tr>
<tr><td style="padding:20px 26px 24px;color:#96a0ab;font-size:11.5px">
  AWAKEN Fitness Center · This link is private to you — please don't forward it.
</td></tr>
</table></td></tr></table></body></html>"""
        return subject, text, html

    def _send_one(db: Session, run: CommissionRun, coach: str, staff, now,
                  base: str, mailer: Mailer, force: bool = False):
        """Make sure the coach has a live link, then email it.

        Returns (status, detail) where status is sent / skipped / failed.
        """
        email = _coach_email(db, coach)
        if not email:
            return "failed", "no email address on their record"
        link = _current_link(db, run.id, coach)
        if link is None or not link.is_live:
            link = _issue_link(db, run.id, coach, staff, now)
            db.flush()
        elif link.sent_at and not force:
            # Pressing the button twice shouldn't mail everyone twice.
            return "skipped", "already sent"
        url = f"{base}/statement/{link.token}"
        totals = _statement_context(run, coach, db)
        subject, text, html = _statement_email(
            coach, run, url, totals.get("total"), link.expires_at)
        ok, why = mailer.send(email, subject, text, html)
        if not ok:
            return "failed", why
        link.sent_to = email
        link.sent_at = now
        return "sent", email

    def _coach_rows(run: CommissionRun, coach: str):
        """Everything for this coach in this run — paid or not.

        Non-completed sessions are included deliberately: the coach was on the
        diary for them, and a statement that quietly omits a cancellation looks
        like a statement that lost one.
        """
        return sorted((b for b in _live(run) if (b.coach or b.staff_raw) == coach),
                      key=lambda b: (b.appointment_date or date.min, b.booking_ref or ""))

    def _statement_context(run: CommissionRun, coach: str, db: Session) -> dict:
        rows = _coach_rows(run, coach)
        paid = [b for b in rows if b.is_commissionable]
        dele = [b for b in paid if b.delegator_id]
        own = [b for b in paid if not b.delegator_id]
        order, seen, buckets = list(BOOKING_STATUSES), set(), []
        for status in order + sorted({(b.booking_status or "—") for b in rows}):
            key = (status or "—").strip()
            if key.lower() in seen:
                continue
            seen.add(key.lower())
            items = [b for b in rows
                     if (b.booking_status or "—").strip().lower() == key.lower()]
            if items:
                buckets.append({"status": key, "n": len(items)})
        return {
            "run": run, "coach": coach, "rows": rows,
            "total": sum((Decimal(str(b.commission or 0)) for b in paid), Decimal(0)),
            "paid_count": len(paid),
            "own_total": sum((Decimal(str(b.commission or 0)) for b in own), Decimal(0)),
            "own_count": len(own),
            "dele_total": sum((Decimal(str(b.commission or 0)) for b in dele), Decimal(0)),
            "dele_count": len(dele),
            "delegators": sorted({b.delegator.name for b in dele if b.delegator}),
            "buckets": buckets,
            "signoff": signoffs(run, db).get(coach),
        }

    @app.get("/statement/{token}", response_class=HTMLResponse)
    def public_statement(request: Request, token: str,
                         db: Session = Depends(get_db)):
        """The coach's own statement. No login — the token is the credential.

        Deliberately outside the app's auth: everything it can reach is one
        coach's one period, and it is read-only.
        """
        link = (db.query(CommissionStatementLink)
                .filter(CommissionStatementLink.token == token).first())
        if not link:
            return templates.TemplateResponse(
                "statement_gone.html",
                {"request": request, "reason": "unknown"}, status_code=404)
        if not link.is_live:
            return templates.TemplateResponse(
                "statement_gone.html",
                {"request": request,
                 "reason": "revoked" if link.revoked_at else "expired",
                 "days": STATEMENT_LINK_DAYS}, status_code=410)
        run = link.run
        link.opens = (link.opens or 0) + 1
        now = datetime.now(timezone.utc)
        link.first_opened_at = link.first_opened_at or now
        link.last_opened_at = now
        db.commit()
        ctx = _statement_context(run, link.coach, db)
        ctx.update({"request": request, "link": link})
        return templates.TemplateResponse("statement.html", ctx)

    @app.get("/commissions/{rid}/statements", response_class=HTMLResponse)
    def commission_statements(request: Request, rid: int,
                              db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run:
            return RedirectResponse("/commissions", status_code=303)
        signed = signoffs(run, db)
        links = {}
        for ln in (db.query(CommissionStatementLink).filter_by(run_id=rid)
                   .order_by(CommissionStatementLink.id).all()):
            links[ln.coach] = ln          # ordered ascending, so newest wins
        rows = []
        for c in coach_summary(run):
            person = (db.query(Staff).filter(Staff.name == c["coach"]).first())
            rows.append({**c, "signoff": signed.get(c["coach"]),
                         "link": links.get(c["coach"]),
                         "email": (person.email if person else None)})
        mail = Mailer()
        return render(request, "commission_statements.html", db, staff,
                      run=run, rows=rows, days=STATEMENT_LINK_DAYS,
                      base_url=_public_base(request),
                      # Shown once, after a send — a page refresh shouldn't
                      # keep reporting a result that has already been read.
                      result=request.session.pop("mail_result", None),
                      mail_ready=mail.cfg.configured,
                      mail_missing=mail.cfg.missing,
                      mail_from=mail.cfg.from_addr,
                      RUN_FINALIZED=RUN_FINALIZED)

    @app.post("/commissions/{rid}/statements/link")
    def commission_statement_link(request: Request, rid: int,
                                  coach: str = Form(...), action: str = Form("create"),
                                  db: Session = Depends(get_db)):
        """Create, refresh or revoke one coach's link. Admin only — it hands out
        access to money."""
        staff, redir = guard_money(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        back = f"/commissions/{rid}/statements"
        if not run:
            return RedirectResponse("/commissions", status_code=303)
        now = datetime.now(timezone.utc)
        if action == "revoke":
            for link in _links_for(db, rid, coach):
                if not link.revoked_at:
                    link.revoked_at = now
            db.commit()
            return RedirectResponse(back, status_code=303)
        if not signoffs(run, db).get(coach):
            # Sending unapproved figures means the coach finds the mistake.
            return RedirectResponse(back + "?blocked=" + coach, status_code=303)
        _issue_link(db, rid, coach, staff, now)
        db.commit()
        return RedirectResponse(back, status_code=303)

    @app.post("/commissions/{rid}/statements/send")
    def commission_statements_send(request: Request, rid: int,
                                   coach: str = Form(""), force: str = Form(""),
                                   db: Session = Depends(get_db)):
        """Email approved coaches their statement. With no `coach`, everyone
        approved who hasn't already had this month's link sent."""
        staff, redir = guard_money(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run:
            return RedirectResponse("/commissions", status_code=303)
        back = f"/commissions/{rid}/statements"
        mailer = Mailer()
        if not mailer.cfg.configured:
            request.session["mail_result"] = {
                "sent": [], "skipped": [], "failed": [],
                "setup": "Mail isn't set up yet — %s missing on the server."
                         % ", ".join(mailer.cfg.missing)}
            return RedirectResponse(back, status_code=303)
        approved = list(signoffs(run, db))
        targets = [coach] if coach else [c["coach"] for c in coach_summary(run)
                                         if c["coach"] in approved]
        if coach and coach not in approved:
            return RedirectResponse(back + "?blocked=" + coach, status_code=303)
        now = datetime.now(timezone.utc)
        base = _public_base(request)
        out = {"sent": [], "skipped": [], "failed": [], "setup": ""}
        for name in targets:
            status, detail = _send_one(db, run, name, staff, now, base, mailer,
                                       force=bool(force))
            if status == "sent":
                out["sent"].append({"coach": name, "detail": detail})
            elif status == "skipped":
                out["skipped"].append({"coach": name, "detail": detail})
            else:
                out["failed"].append({"coach": name, "detail": detail})
            db.commit()          # one coach's failure shouldn't undo the rest
        request.session["mail_result"] = out
        return RedirectResponse(back, status_code=303)

    @app.post("/commissions/{rid}/statements/link-all")
    def commission_statement_link_all(request: Request, rid: int,
                                      db: Session = Depends(get_db)):
        staff, redir = guard_money(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run:
            return RedirectResponse("/commissions", status_code=303)
        now = datetime.now(timezone.utc)
        for coach in signoffs(run, db):
            current = _current_link(db, rid, coach)
            if current is not None and current.is_live:
                continue          # a working link is not worth invalidating
            _issue_link(db, rid, coach, staff, now)
        db.commit()
        return RedirectResponse(f"/commissions/{rid}/statements", status_code=303)

    # ---------------------------------------------------------- delegation

    def _period_runs(db: Session):
        """Runs that carry delegated sessions, newest first — the period picker."""
        out = []
        for run in db.query(CommissionRun).order_by(CommissionRun.id.desc()).all():
            if run.status == RUN_SUPERSEDED:
                continue
            if any(b.delegator_id for b in run.bookings):
                out.append(run)
        return out

    def _pick_run(db: Session, request: Request):
        runs = _period_runs(db)
        want = (request.query_params.get("run") or "").strip()
        run = next((r for r in runs if str(r.id) == want), None) if want else None
        return (run or (runs[0] if runs else None)), runs

    @app.get("/commissions/delegation", response_class=HTMLResponse)
    def delegation_index(request: Request, db: Session = Depends(get_db)):
        """Delegation as its own section: the delegator is the customer here,
        so the figure that leads is margin, not commission."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        run, runs = _pick_run(db, request)
        rows = delegator_rollup(run, db) if run else []
        return render(request, "delegation.html", db, staff, active="delegation",
                      run=run, runs=runs, rows=rows,
                      totals={
                          "sessions": sum(r["sessions"] for r in rows),
                          "clients": len({(b.customer or "").strip()
                                          for b in (delegated_rows(run) if run else [])
                                          if (b.customer or "").strip()}),
                          "charged": sum((r["charged"] for r in rows), Decimal(0)),
                          "cost": sum((r["cost"] for r in rows), Decimal(0)),
                          "margin": sum((r["margin"] for r in rows), Decimal(0)),
                      })

    def _rollup_totals(run, rows) -> dict:
        """Footer figures for a delegator rollup.

        Clients and coaches are counted from the bookings rather than summed
        across rows, because one client trained by two delegators is one client.
        """
        live = delegated_rows(run) if (run and rows) else []
        return {
            "sessions": sum(r["sessions"] for r in rows),
            "clients": len({(b.customer or "").strip() for b in live if (b.customer or "").strip()}),
            "coaches": len({(b.coach or b.staff_raw or "").strip() for b in live
                            if (b.coach or b.staff_raw or "").strip()}),
            "charged": sum((r["charged"] for r in rows), Decimal(0)),
            "cost": sum((r["cost"] for r in rows), Decimal(0)),
            "margin": sum((r["margin"] for r in rows), Decimal(0)),
        }

    def _delegator_detail(run, did: int) -> dict:
        """One delegator's month: who they sent, who covered, what it earned.

        Shared by the standalone Delegation screen and the By delegator tab on a
        run, so the two can never drift into telling different stories.
        """
        rows = [b for b in delegated_rows(run) if b.delegator_id == did] if run else []
        counted = [b for b in rows if b.is_commissionable]
        charged = sum((Decimal(str(b.delegation_charge or 0)) for b in counted), Decimal(0))
        cost = sum((Decimal(str(b.commission or 0)) for b in counted), Decimal(0))

        by_client, by_coach = {}, {}
        for b in rows:
            for key, bucket in ((b.customer or "—").strip(), by_client), \
                               ((b.coach or b.staff_raw or "—").strip(), by_coach):
                d = bucket.setdefault(key, {"name": key, "sessions": 0,
                                            "charged": Decimal(0), "cost": Decimal(0),
                                            "first": None, "last": None,
                                            "others": set()})
                d["sessions"] += 1
                if b.is_commissionable:
                    d["charged"] += Decimal(str(b.delegation_charge or 0))
                    d["cost"] += Decimal(str(b.commission or 0))
                if b.appointment_date:
                    d["first"] = min(d["first"] or b.appointment_date, b.appointment_date)
                    d["last"] = max(d["last"] or b.appointment_date, b.appointment_date)
        for b in rows:
            by_client[(b.customer or "—").strip()]["others"].add(
                (b.coach or b.staff_raw or "—").strip())
            by_coach[(b.coach or b.staff_raw or "—").strip()]["others"].add(
                (b.customer or "—").strip())
        return dict(
            rows=rows, sessions=len(rows), counting=len(counted),
            charged=charged, cost=cost, margin=charged - cost,
            clients=sorted(by_client.values(), key=lambda r: (-r["sessions"], r["name"])),
            coaches=sorted(by_coach.values(), key=lambda r: (-r["sessions"], r["name"])),
            matrix=schedule_matrix(rows))

    @app.get("/commissions/delegation/{did}", response_class=HTMLResponse)
    def delegation_detail(request: Request, did: int, tab: str = "sessions",
                          db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        delegator = db.get(CommissionDelegator, did)
        run, runs = _pick_run(db, request)
        if not delegator:
            return RedirectResponse("/commissions/delegation", status_code=303)
        d = _delegator_detail(run, did)
        return render(request, "delegation_detail.html", db, staff, active="delegation",
                      run=run, runs=runs, delegator=delegator, tab=tab, **d)

    @app.get("/commissions/{rid}", response_class=HTMLResponse)
    def commission_run_view(request: Request, rid: int, tab: str = "summary",
                            db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run:
            return RedirectResponse("/commissions", status_code=303)
        plans, prows, ptotals = pivot(run)
        live = _live(run)
        adjusted = [b for b in live if b.adjustment]
        dropped = [b for b in run.bookings if b.dropped_reason]
        delegated = sorted([b for b in live if b.delegator_id],
                           key=lambda b: (b.delegator.name if b.delegator else "",
                                          b.appointment_date or date.min))
        by_delegator = {}
        for b in delegated:
            key = b.delegator.name if b.delegator else "—"
            d = by_delegator.setdefault(key, {"sessions": 0, "charged": Decimal(0),
                                              "cost": Decimal(0)})
            d["sessions"] += 1
            d["charged"] += Decimal(str(b.delegation_charge or 0))
            d["cost"] += Decimal(str(b.commission or 0))
        for d in by_delegator.values():
            d["margin"] = d["charged"] - d["cost"]
        waiting = pending(run)
        signed = signoffs(run, db)
        # By delegator: the rollup always, plus one delegator opened if the
        # name strip has a selection.
        dg_rows = delegator_rollup(run, db) if tab == "delegators" else []
        dg_pick, dg_detail = None, None
        if tab == "delegators":
            want = request.query_params.get("d")
            if want and want.isdigit():
                dg_pick = next((r for r in dg_rows
                                if r["delegator"].id == int(want)), None)
                if dg_pick:
                    dg_detail = _delegator_detail(run, dg_pick["delegator"].id)
        summary = coach_summary(run)
        emails = {p.name: p.email for p in db.query(Staff).all() if p.email}
        for row in summary:
            row["signoff"] = signed.get(row["coach"])
            row["email"] = emails.get(row["coach"])
        return render(request, "commission_run.html", db, staff, run=run, tab=tab,
                      plans=plans, prows=prows, ptotals=ptotals,
                      totals=run_totals(run), block=blockers(run, db),
                      adjusted=adjusted, dropped=dropped, delegated=delegated,
                      by_delegator=by_delegator, coaches=summary,
                      statuses=status_groups(run, request.query_params.get("status")),
                      status_pick=request.query_params.get("status") or "",
                      pending=waiting, pending_count=len(waiting),
                      can_pay=(getattr(staff, "role", "") == "admin"),
                      result=request.session.pop("signoff_result", None),
                      dg_rows=dg_rows, dg_pick=dg_pick, dg_detail=dg_detail,
                      dg_totals=_rollup_totals(run, dg_rows),
                      RUN_DRAFT=RUN_DRAFT, RUN_FINALIZED=RUN_FINALIZED)

    @app.get("/commissions/{rid}/coach/{coach}", response_class=HTMLResponse)
    def commission_coach_view(request: Request, rid: int, coach: str,
                              db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run:
            return RedirectResponse("/commissions", status_code=303)
        groups = coach_groups(run, coach)
        rows = [b for b in _live(run) if (b.coach or b.staff_raw) == coach]
        # One status at a time, picked from the filter — the page used to stack
        # every status down the page, which buried the one you came to read.
        pick = (request.query_params.get("status") or "").strip()
        chosen = next((g for g in groups
                       if g["status"].lower() == pick.lower()), None) if pick else None
        shown = (chosen["rows"] if chosen else
                 sorted(rows, key=lambda b: (b.appointment_date or date.min,
                                             b.booking_ref or "")))
        counted = [b for b in rows if b.is_commissionable]
        dele = [b for b in counted if b.delegator_id]
        return render(
            request, "commission_coach.html", db, staff, run=run, coach=coach,
            groups=groups,
            # `completed` is only still passed so the previous template keeps
            # rendering during a rolling deploy; harmless once both have landed.
            completed=next((g for g in groups if g["status"].lower() == "completed"), None),
            sessions=len(counted),
            revenue=sum((Decimal(str(b.revenue or 0)) for b in counted), Decimal(0)),
            commission=sum((Decimal(str(b.commission or 0)) for b in counted), Decimal(0)),
            delegation_total=sum((Decimal(str(b.commission or 0)) for b in dele), Decimal(0)),
            delegation_sessions=len(dele),
            approved_count=len([b for b in rows if b.approved]),
            pending_count=len([b for b in rows
                               if not b.pays_by_status and not b.approved]),
            shown=shown, chosen=chosen, pick=(chosen["status"] if chosen else ""),
            total_rows=len(rows),
            shown_revenue=sum((Decimal(str(b.revenue or 0)) for b in shown), Decimal(0)),
            shown_commission=sum((Decimal(str(b.commission or 0))
                                  for b in shown if b.is_commissionable), Decimal(0)),
            shown_counting=len([b for b in shown if b.is_commissionable]),
            manual_count=len([b for b in rows if b.rate_manual]),
            signoff=signoffs(run, db).get(coach),
            rate_types=COMMISSION_RATE_TYPES,
            # Approving and repricing are admin-only; everyone else reads.
            can_pay=(getattr(staff, "role", "") == "admin"),
            is_draft=(run.status == RUN_DRAFT))

    @app.get("/commissions/{rid}/coach/{coach}/statement.pdf")
    def commission_statement_pdf(request: Request, rid: int, coach: str,
                                 download: str = "", db: Session = Depends(get_db)):
        """The coach's statement as a PDF.

        Served inline by default so the browser's own viewer is the preview —
        no second rendering path to drift out of step with the real document.
        """
        from fastapi.responses import Response
        from . import commission_pdf

        staff, redir = guard(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run:
            return RedirectResponse("/commissions", status_code=303)
        rows = sorted(
            (b for b in _live(run)
             if (b.coach or b.staff_raw) == coach and b.is_commissionable),
            key=lambda r: (r.appointment_date or date.min, r.booking_ref or ""))
        pdf = commission_pdf.statement(
            run, coach, rows,
            signoff=signoffs(run, db).get(coach),
            generated_by=getattr(staff, "name", "") or "")
        name = "commission-%s-%s.pdf" % (
            re.sub(r"[^A-Za-z0-9]+", "-", coach).strip("-").lower(),
            re.sub(r"[^A-Za-z0-9]+", "-", run.period or "").strip("-").lower())
        disp = "attachment" if download else "inline"
        return Response(pdf, media_type="application/pdf", headers={
            "Content-Disposition": '%s; filename="%s"' % (disp, name)})

    @app.post("/commissions/{rid}/coach/{coach}/signoff")
    def commission_signoff(request: Request, rid: int, coach: str,
                           confirm: str = Form("on"), back: str = Form(""),
                           db: Session = Depends(get_db)):
        """Confirm one coach's figures are correct and clear them for payout.

        `back` lets the same route serve the coach's own screen and the row
        button on the By coach tab, returning you to whichever you came from.
        """
        staff, redir = guard_money(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        where = (f"/commissions/{rid}?tab=coaches" if back == "coaches"
                 else f"/commissions/{rid}/coach/{coach}")
        if not run or run.status != RUN_DRAFT:
            return RedirectResponse(where, status_code=303)
        existing = (db.query(CommissionSignoff)
                    .filter_by(run_id=rid, coach=coach).first())
        if confirm != "on":
            if existing:
                db.delete(existing)
                db.commit()
            return RedirectResponse(where, status_code=303)
        _signoff_one(db, run, coach, staff, datetime.now(timezone.utc))
        db.commit()
        return RedirectResponse(where, status_code=303)

    def _signoff_one(db: Session, run: CommissionRun, coach: str, staff, now):
        """Record that one coach's figures have been confirmed."""
        rows = [b for b in _live(run)
                if (b.coach or b.staff_raw) == coach and b.is_commissionable]
        row = (db.query(CommissionSignoff).filter_by(run_id=run.id, coach=coach).first()
               or CommissionSignoff(run_id=run.id, coach=coach))
        row.sessions = len(rows)
        row.commission = sum((Decimal(str(b.commission or 0)) for b in rows), Decimal(0))
        row.approved_by_id = getattr(staff, "id", None)
        row.approved_at = now
        db.add(row)
        return row

    @app.post("/commissions/{rid}/signoff-many")
    async def commission_signoff_many(request: Request, rid: int,
                                      db: Session = Depends(get_db)):
        """Approve several coaches from the By coach tab in one go.

        Ticking rows and pressing once is the whole point, so this takes a list
        rather than making the admin open seven screens to do the same thing.
        """
        staff, redir = guard_money(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        back = f"/commissions/{rid}?tab=coaches"
        if not run or run.status != RUN_DRAFT:
            return RedirectResponse(back, status_code=303)
        form = await request.form()
        wanted = [c for c in form.getlist("coach") if c]
        # Only coaches actually in this run — a name posted by hand shouldn't
        # mint a sign-off for someone who isn't on the sheet.
        live = {c["coach"] for c in coach_summary(run)}
        now = datetime.now(timezone.utc)
        done = [c for c in wanted if c in live]
        for coach in done:
            _signoff_one(db, run, coach, staff, now)
        db.commit()
        request.session["signoff_result"] = {"n": len(done), "names": done[:12]}
        return RedirectResponse(back, status_code=303)

    @app.post("/commissions/{rid}/booking/{bid}/rate")
    def commission_booking_rate(request: Request, rid: int, bid: int,
                                rate_type: str = Form(COMMISSION_PERCENT),
                                rate_value: str = Form("0"),
                                reset: str = Form(""),
                                db: Session = Depends(get_db)):
        """Set (or clear) a hand-typed rate on a single booking."""
        staff, redir = guard_money(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        b = db.get(CommissionBooking, bid)
        if not run or not b or b.run_id != rid:
            return RedirectResponse(f"/commissions/{rid}", status_code=303)
        coach = b.coach or b.staff_raw
        back = f"/commissions/{rid}/coach/{coach}#{b.booking_ref or ''}"
        if run.status != RUN_DRAFT:
            return RedirectResponse(back, status_code=303)
        if reset == "on":
            b.rate_manual = False
            b.rate_manual_by_id = None
        else:
            b.rate_manual = True
            b.rate_manual_by_id = staff.id
            b.rate_type = rate_type
            b.rate_value = _num(rate_value, rate_type)
        recompute(b, build_config(db), db)
        # Changing the money invalidates a sign-off — it confirmed figures
        # that no longer exist.
        void_signoff(db, rid, coach)
        db.commit()
        return RedirectResponse(back, status_code=303)

    @app.post("/commissions/{rid}/recalculate")
    def commission_recalculate(request: Request, rid: int,
                               db: Session = Depends(get_db)):
        """Re-apply the current rates and rules to every booking in a draft.

        Needed because coach rates, delegator amounts and the revenue rules are
        editable: without this, changing one only affects the next upload.
        Approvals are preserved; finalized runs are never touched.
        """
        staff, redir = guard(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run or run.status != RUN_DRAFT:
            return RedirectResponse(f"/commissions/{rid}", status_code=303)
        config = build_config(db)
        dmap = delegator_map(db)
        for b in _live(run):
            recompute(b, config, db, dmap)
        void_signoff(db, rid)
        db.commit()
        return RedirectResponse(f"/commissions/{rid}", status_code=303)

    @app.post("/commissions/{rid}/booking/{bid}/approve")
    def commission_approve(request: Request, rid: int, bid: int,
                           db: Session = Depends(get_db)):
        """Toggle whether one non-Completed booking earns commission."""
        staff, redir = guard_money(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        b = db.get(CommissionBooking, bid)
        back = f"/commissions/{rid}"
        if not run or not b or b.run_id != rid:
            return RedirectResponse(back, status_code=303)
        coach = b.coach or b.staff_raw
        if run.status != RUN_DRAFT or b.pays_by_status:
            # Rows that already pay on their status have nothing to approve;
            # finalized runs are immutable.
            return RedirectResponse(f"{back}/coach/{coach}", status_code=303)
        b.approved = not b.approved
        b.approved_by_id = staff.id if b.approved else None
        b.approved_at = datetime.now(timezone.utc) if b.approved else None
        recompute(b, build_config(db), db)
        void_signoff(db, rid, coach)
        db.commit()
        return RedirectResponse(f"{back}/coach/{coach}#{b.booking_ref or ''}",
                                status_code=303)

    @app.post("/commissions/{rid}/coach/{coach}/approve-status")
    def commission_approve_status(request: Request, rid: int, coach: str,
                                  status: str = Form(...), include: str = Form("on"),
                                  db: Session = Depends(get_db)):
        """Approve (or un-approve) every booking of one status for one coach."""
        staff, redir = guard_money(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run or run.status != RUN_DRAFT:
            return RedirectResponse(f"/commissions/{rid}/coach/{coach}", status_code=303)
        want = (include == "on")
        config = build_config(db)
        for b in _live(run):
            if (b.coach or b.staff_raw) != coach or b.pays_by_status:
                continue
            if (b.booking_status or "").strip().lower() != status.strip().lower():
                continue
            b.approved = want
            b.approved_by_id = staff.id if want else None
            b.approved_at = datetime.now(timezone.utc) if want else None
            recompute(b, config, db)
        void_signoff(db, rid, coach)
        db.commit()
        return RedirectResponse(f"/commissions/{rid}/coach/{coach}", status_code=303)

    @app.post("/commissions/{rid}/delete")
    def commission_run_delete(request: Request, rid: int,
                              db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if run and run.status == RUN_DRAFT:
            db.delete(run)
            db.commit()
        return RedirectResponse("/commissions", status_code=303)

    # ---------------------------------------------------------------- phase 4

    @app.post("/commissions/{rid}/reopen")
    def commission_reopen(request: Request, rid: int,
                          db: Session = Depends(get_db)):
        """Put a finalized run back to draft and tear up its documents.

        Admin only, and refused once any payout is marked paid: reopening a run
        somebody has already been paid from is how a coach ends up paid twice.
        Un-mark the payout first if that really is what you want.
        """
        staff, redir = guard_money(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run or run.status != RUN_FINALIZED:
            return RedirectResponse(f"/commissions/{rid}", status_code=303)
        payouts = db.query(CommissionPayout).filter_by(run_id=rid).all()
        charges = db.query(CommissionCharge).filter_by(run_id=rid).all()
        if any(p.status == "paid" for p in payouts):
            run.last_import_note = (
                "Reopen refused — %s already marked paid. Un-mark it on the "
                "payout first." % ", ".join(
                    p.coach for p in payouts if p.status == "paid"))
            db.commit()
            return RedirectResponse(f"/commissions/{rid}?tab=documents",
                                    status_code=303)
        n = len(payouts) + len(charges)
        for row in payouts + charges:
            db.delete(row)
        run.status = RUN_DRAFT
        run.finalized_at = None
        run.finalized_by_id = None
        # Sign-offs confirmed figures that are now editable again.
        void_signoff(db, rid)
        run.last_import_note = (
            "Reopened as a draft by %s · %d document%s deleted · every coach "
            "needs approving again." % (getattr(staff, "name", "someone"), n,
                                        "" if n == 1 else "s"))
        db.commit()
        return RedirectResponse(f"/commissions/{rid}", status_code=303)

    @app.post("/commissions/{rid}/finalize")
    def commission_finalize(request: Request, rid: int, db: Session = Depends(get_db)):
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run or run.status != RUN_DRAFT:
            return RedirectResponse("/commissions", status_code=303)
        if blockers(run, db)["blocking"]:
            return RedirectResponse(f"/commissions/{rid}", status_code=303)

        done = [b for b in _live(run) if b.is_commissionable]

        by_coach = {}
        for b in done:
            by_coach.setdefault(b.coach or b.staff_raw, []).append(b)
        for coach, rows in sorted(by_coach.items()):
            payout = CommissionPayout(
                run_id=run.id, number=_next_number(db, CommissionPayout, "COM"),
                coach=coach, coach_id=next((r.coach_id for r in rows if r.coach_id), None),
                period_label=run.period_label, sessions=len(rows))
            db.add(payout)
            db.flush()
            commission_total = delegation_total = Decimal(0)
            for b in sorted(rows, key=lambda r: (r.appointment_date or date.min,
                                                 r.booking_ref or "")):
                amount = Decimal(str(b.commission or 0))
                if b.delegator_id:
                    delegation_total += amount
                    basis = f"Delegation cost — {b.delegator.name if b.delegator else ''}"
                    desc = f"{b.appointment_name} · {b.customer} · Delegation"
                else:
                    commission_total += amount
                    basis = _basis(b)
                    desc = f"{b.appointment_name} · {b.customer} · {b.pricing_plan}"
                db.add(CommissionPayoutLine(
                    payout_id=payout.id, booking_id=b.id, booking_ref=b.booking_ref,
                    occurred_on=b.appointment_date, description=desc, basis=basis,
                    amount=amount))
            payout.commission_total = _money(commission_total)
            payout.delegation_total = _money(delegation_total)
            payout.total = _money(commission_total + delegation_total)

        by_del = {}
        for b in done:
            if b.delegator_id:
                by_del.setdefault(b.delegator_id, []).append(b)
        for did, rows in sorted(by_del.items()):
            d = db.get(CommissionDelegator, did)
            charge = CommissionCharge(
                run_id=run.id, number=_next_number(db, CommissionCharge, "DEL"),
                delegator_id=did, delegator_name=d.name if d else "—",
                period_label=run.period_label, sessions=len(rows))
            db.add(charge)
            db.flush()
            total = cost = Decimal(0)
            for b in sorted(rows, key=lambda r: (r.appointment_date or date.min,
                                                 r.booking_ref or "")):
                amount = Decimal(str(b.delegation_charge or 0))
                total += amount
                cost += Decimal(str(b.commission or 0))
                db.add(CommissionChargeLine(
                    charge_id=charge.id, booking_id=b.id, booking_ref=b.booking_ref,
                    occurred_on=b.appointment_date,
                    description=f"{b.appointment_name} · {b.customer}",
                    coach=b.coach or b.staff_raw, amount=amount))
            charge.total = _money(total)
            charge.coach_cost = _money(cost)

        run.status = RUN_FINALIZED
        run.finalized_by_id = staff.id
        run.finalized_at = datetime.now(timezone.utc)
        db.commit()
        return RedirectResponse(f"/commissions/{rid}?tab=documents", status_code=303)

    @app.get("/commissions/payouts/{pid}", response_class=HTMLResponse)
    def commission_payout_view(request: Request, pid: int,
                               db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        payout = db.get(CommissionPayout, pid)
        if not payout:
            return RedirectResponse("/commissions", status_code=303)
        return render(request, "commission_payout.html", db, staff, payout=payout)

    @app.post("/commissions/payouts/{pid}/paid")
    def commission_payout_paid(request: Request, pid: int,
                               db: Session = Depends(get_db)):
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        payout = db.get(CommissionPayout, pid)
        if payout:
            paid = payout.status != "paid"
            payout.status = "paid" if paid else "unpaid"
            payout.paid_at = datetime.now(timezone.utc) if paid else None
            db.commit()
        return RedirectResponse(f"/commissions/payouts/{pid}", status_code=303)

    @app.get("/commissions/charges/{cid}", response_class=HTMLResponse)
    def commission_charge_view(request: Request, cid: int,
                               db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        charge = db.get(CommissionCharge, cid)
        if not charge:
            return RedirectResponse("/commissions", status_code=303)
        return render(request, "commission_charge.html", db, staff, charge=charge)

    @app.post("/commissions/charges/{cid}/paid")
    def commission_charge_paid(request: Request, cid: int,
                               db: Session = Depends(get_db)):
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        charge = db.get(CommissionCharge, cid)
        if charge:
            paid = charge.status != "paid"
            charge.status = "paid" if paid else "unpaid"
            charge.paid_at = datetime.now(timezone.utc) if paid else None
            db.commit()
        return RedirectResponse(f"/commissions/charges/{cid}", status_code=303)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _num(text: str, rate_type: str) -> Decimal:
    """Percent fields are entered as 70, stored as 0.70."""
    try:
        value = Decimal((text or "0").strip().replace(",", "").replace("%", ""))
    except Exception:
        return Decimal(0)
    if rate_type == COMMISSION_PERCENT and value > 1:
        value = value / 100
    return value


def _day(text: str):
    """Parse a date input; blank means open-ended."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _basis(b: CommissionBooking) -> str:
    if b.rate_type == COMMISSION_PERCENT and b.rate_value is not None:
        return "{:g}% of {}".format(float(b.rate_value) * 100, _fmt(b.revenue))
    if b.rate_type == COMMISSION_FLAT:
        return "Fixed {}".format(_fmt(b.rate_value))
    return "—"


def _booking(run_id, row, delegator_ids, coach_ids) -> CommissionBooking:
    return CommissionBooking(
        run_id=run_id,
        booking_ref=row.booking_ref, customer=row.customer,
        appointment_date=row.appointment_date, appointment_name=row.appointment_name,
        variant=row.variant, staff_raw=row.staff_raw,
        booking_status=row.booking_status, pricing_plan=row.pricing_plan,
        payment_method=row.payment_method, revenue_raw=row.revenue_raw,
        coach=row.coach, coach_id=coach_ids.get(row.coach),
        delegator_id=(delegator_ids.get(row.delegator.name) if row.delegator else None),
        delegator_assumed=row.delegator_assumed,
        revenue=row.revenue, adjustment=row.adjustment,
        adjustment_note=row.adjustment_note,
        rule=row.rule, rate_type=row.rate_type, rate_value=row.rate_value,
        commission=row.commission, delegation_charge=row.delegation_charge,
        pays_by_status=row.pays_by_status,
    )
