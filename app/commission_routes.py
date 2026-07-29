"""Coach commission screens.

Registered from main.py via ``register(app, deps)`` so this module never
imports main — the render/require helpers arrive as dependencies instead.

Phases implemented here:
  2  configuration — coach rates, delegators, rules
  3  upload & preview — CSV in, batch review, per-coach detail
  4  finalize — coach payouts and delegator charges, in their own tables
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import commissions as engine
from .db import get_db
from .models import (
    BOOKING_STATUSES, IMPORT_MERGE, IMPORT_REPLACE,
    COMMISSION_DELEGATOR_DEFAULTS, COMMISSION_FLAT, COMMISSION_PERCENT,
    COMMISSION_RATE_DEFAULTS, COMMISSION_RATE_TYPES, COMMISSION_SETTING_DEFAULTS,
    COMMISSION_SESSION_RATE_DEFAULTS, CommissionSessionRate, AWAKEN_FORCE,
    RUN_DRAFT, RUN_FINALIZED, RUN_SUPERSEDED,
    CommissionBooking, CommissionCharge, CommissionChargeLine,
    CommissionCoachRate, CommissionDelegator, CommissionPayout,
    CommissionPayoutLine, CommissionRun, CommissionSetting, Staff,
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
            db.add(CommissionCoachRate(**d))
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
            override_plans=frozenset(r.plan_list()),
            override_rate_type=r.override_rate_type or None,
            override_rate_value=(Decimal(str(r.override_rate_value))
                                 if r.override_rate_value is not None else None),
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
    )
    return engine.Config(coach_rates=rates, delegators=delegators, settings=st)


def people(db: Session):
    """Coaches and affiliates, for linking a rate row to a real person rather
    than repeating their name as free text."""
    return (db.query(Staff)
            .filter(Staff.person_type.in_(("coach", "affiliate", "employee")))
            .order_by(Staff.name).all())


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


def blockers(run: CommissionRun) -> dict:
    """Unmapped staff and unrecognised delegator codes. Both block finalizing —
    a coach silently missing from a payout is the failure this prevents."""
    unmapped, unknown = {}, {}
    for b in _live(run):
        if not b.coach:
            unmapped[b.staff_raw or "(blank)"] = unmapped.get(b.staff_raw or "(blank)", 0) + 1
        if re.search(r"\bdelegation\b", b.variant or "", re.I) and not b.delegator_id:
            unknown[b.variant] = unknown.get(b.variant, 0) + 1
    return {"unmapped": unmapped, "unknown": unknown,
            "blocking": bool(unmapped or unknown)}


#: Statuses that carry no commission unless a reviewer approves them.
REVIEWABLE = ("cancelled", "late cancelled", "no show", "booked")


def pending(run: CommissionRun):
    """Non-Completed bookings still awaiting a decision."""
    return [b for b in _live(run) if not b.is_completed and not b.approved]


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
        elif not b.is_completed:
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
    row = engine.recompute_row(_to_engine_row(b, delegator), config)
    b.revenue = row.revenue
    b.adjustment = row.adjustment
    b.adjustment_note = row.adjustment_note
    b.rule = row.rule
    b.rate_type = row.rate_type
    b.rate_value = row.rate_value
    b.commission = row.commission
    b.delegation_charge = row.delegation_charge


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
    require = deps["require"]
    tz = deps["tz"]

    def guard(request, db):
        """Admins, or anyone granted the manage_commissions area."""
        staff, redir = require(request, db, perm="manage_commissions")
        return staff, redir

    # ---------------------------------------------------------------- phase 2

    @app.get("/admin/commission-rates", response_class=HTMLResponse)
    def commission_rates(request: Request, db: Session = Depends(get_db)):
        staff, redir = guard(request, db)
        if redir:
            return redir
        rates = db.query(CommissionCoachRate).order_by(CommissionCoachRate.coach).all()
        return render(request, "commission_rates.html", db, staff, rates=rates,
                      rate_types=COMMISSION_RATE_TYPES, people=people(db),
                      active="rates")

    @app.post("/admin/commission-rates/{rid}")
    def commission_rate_update(
            request: Request, rid: int,
            coach: str = Form(...), staff_raw: str = Form(...),
            coach_id: str = Form(""),
            rate_type: str = Form(COMMISSION_PERCENT), rate_value: str = Form("0"),
            override_plans: str = Form(""), override_rate_type: str = Form(""),
            override_rate_value: str = Form(""), is_active: str = Form(""),
            db: Session = Depends(get_db)):
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
        r.override_plans = ",".join(
            p.strip().lower() for p in override_plans.split(",") if p.strip())
        r.override_rate_type = override_rate_type or None
        r.override_rate_value = (_num(override_rate_value, override_rate_type)
                                 if override_rate_type and override_rate_value.strip()
                                 else None)
        r.is_active = (is_active == "on")
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
        return render(request, "commission_run.html", db, staff, run=run, tab=tab,
                      plans=plans, prows=prows, ptotals=ptotals,
                      totals=run_totals(run), block=blockers(run),
                      adjusted=adjusted, dropped=dropped, delegated=delegated,
                      by_delegator=by_delegator, coaches=coach_summary(run),
                      pending=waiting, pending_count=len(waiting),
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
            pending_count=len([b for b in rows if not b.is_completed and not b.approved]),
            is_draft=(run.status == RUN_DRAFT))

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
        db.commit()
        return RedirectResponse(f"/commissions/{rid}", status_code=303)

    @app.post("/commissions/{rid}/booking/{bid}/approve")
    def commission_approve(request: Request, rid: int, bid: int,
                           db: Session = Depends(get_db)):
        """Toggle whether one non-Completed booking earns commission."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        b = db.get(CommissionBooking, bid)
        back = f"/commissions/{rid}"
        if not run or not b or b.run_id != rid:
            return RedirectResponse(back, status_code=303)
        coach = b.coach or b.staff_raw
        if run.status != RUN_DRAFT or b.is_completed:
            # Completed rows always count; finalized runs are immutable.
            return RedirectResponse(f"{back}/coach/{coach}", status_code=303)
        b.approved = not b.approved
        b.approved_by_id = staff.id if b.approved else None
        b.approved_at = datetime.now(timezone.utc) if b.approved else None
        recompute(b, build_config(db), db)
        db.commit()
        return RedirectResponse(f"{back}/coach/{coach}#{b.booking_ref or ''}",
                                status_code=303)

    @app.post("/commissions/{rid}/coach/{coach}/approve-status")
    def commission_approve_status(request: Request, rid: int, coach: str,
                                  status: str = Form(...), include: str = Form("on"),
                                  db: Session = Depends(get_db)):
        """Approve (or un-approve) every booking of one status for one coach."""
        staff, redir = guard(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run or run.status != RUN_DRAFT:
            return RedirectResponse(f"/commissions/{rid}/coach/{coach}", status_code=303)
        want = (include == "on")
        config = build_config(db)
        for b in _live(run):
            if (b.coach or b.staff_raw) != coach or b.is_completed:
                continue
            if (b.booking_status or "").strip().lower() != status.strip().lower():
                continue
            b.approved = want
            b.approved_by_id = staff.id if want else None
            b.approved_at = datetime.now(timezone.utc) if want else None
            recompute(b, config, db)
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

    @app.post("/commissions/{rid}/finalize")
    def commission_finalize(request: Request, rid: int, db: Session = Depends(get_db)):
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        run = db.get(CommissionRun, rid)
        if not run or run.status != RUN_DRAFT:
            return RedirectResponse("/commissions", status_code=303)
        if blockers(run)["blocking"]:
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
    )
