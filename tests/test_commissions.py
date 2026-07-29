"""Unit tests for the commission engine.

Every case here is drawn from something actually present in a real Rezerv
export, or from a rule in the commission spec that the export can violate.
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.commissions import (                                   # noqa: E402
    BACKFILL_CREDIT_AND_FREE, Config, Delegator, delegation_code, run,
    recompute_row as engine_recompute,
)
from tests.conf_sample import config, DELEGATORS                # noqa: E402

D = Decimal
HEAD = ("Booking ref,Customer,Appointment date,Appointment name,Duration (hour),"
        "Variant,Staff,Booking status,Paid on,Pricing plan used,Payment method,"
        "Used credit,Total pricing plan credit,Sales channel,Slot per booking,"
        "Total slots,Revenue per booking,Total transaction revenue")


def csv(variant="Private Coaching", staff="Julio D", status="Completed",
        plan="12 Sessions", pay="Manual", revenue="₱1700.00",
        appt="Private Coaching", ref="BK1"):
    return (HEAD + "\n" + ",".join([
        ref, "A Client", "03 Jun 2026", appt, "01:30", variant, staff, status,
        "01 Jun 2026", plan, pay, "1", "12", "Business Portal", "--", "--",
        revenue, revenue]))


def one(**kw):
    cfg = kw.pop("cfg", None) or config()
    result = run(csv(**kw), cfg)
    assert result.rows, "row was unexpectedly dropped"
    return result.rows[0]


# ---------------------------------------------------------------- delegation

def test_delegation_code_en_dash():
    """The real export uses U+2013, not a hyphen."""
    assert delegation_code("Delegation – KP", {"KP", "GR"}) == "KP"


def test_delegation_code_hyphen_and_no_separator():
    assert delegation_code("Delegation - GR", {"KP", "GR"}) == "GR"
    # 'Delegation KP (1HR)' appears in the June export with no separator.
    assert delegation_code("Delegation KP (1HR)", {"KP", "GR"}) == "KP"


def test_delegation_code_ignores_non_delegation_variants():
    assert delegation_code("Private Coaching", {"KP"}) is None
    assert delegation_code("Members : Simulation (1HR)", {"KP"}) is None


def test_delegation_pays_cost_not_coach_rate():
    """Delegation overrides every coach rule — Julio's 70% must not apply."""
    row = one(variant="Delegation – GR", staff="Julio D", plan="Drop-In",
              pay="Free", revenue="₱0.00")
    assert row.rule == "delegation"
    assert row.commission == D("640.00")
    assert row.delegation_charge == D("1000.00")


def test_delegation_charge_uses_the_right_delegator():
    gab = one(variant="Delegation – GR", plan="Drop-In", pay="Free", revenue="₱0.00")
    assert gab.delegator.name == "Gab Rosario"
    culver = one(variant="Delegation – KP", plan="Drop-In", pay="Free", revenue="₱0.00")
    assert culver.delegator.name == "Culver Padilla"


def test_culver_matches_both_codes():
    for code in ("KP", "CP"):
        row = one(variant=f"Delegation – {code}", plan="Drop-In", pay="Free",
                  revenue="₱0.00")
        assert row.delegator.name == "Culver Padilla"


def test_bare_delegation_falls_back_to_default_and_is_flagged():
    row = one(variant="Delegation", plan="Drop-In", pay="Free", revenue="₱0.00")
    assert row.delegator.name == "Culver Padilla"
    assert row.delegator_assumed is True


def test_unknown_delegator_code_blocks_when_no_default():
    cfg = config()
    cfg.settings.default_delegator = None
    result = run(csv(variant="Delegation – ZZ", plan="Drop-In", pay="Free",
                     revenue="₱0.00"), cfg)
    assert result.unknown_codes
    assert result.blocking is True


def test_delegation_is_not_counted_as_dropin():
    """Every delegation row carries plan='Drop-In'; it must not be paid at the
    Drop-In override rate."""
    row = one(variant="Delegation – KP", staff="Anjo R", plan="Drop-In",
              pay="Free", revenue="₱0.00")
    assert row.rule == "delegation"
    assert row.commission == D("640.00")


# ------------------------------------------------------------------ revenue

def test_awaken_force_revenue_override():
    """Export shows ₱9,600 (the package); commission base is ₱1,200."""
    row = one(plan="Awaken Force", revenue="₱9600.00", staff="AR M")
    assert row.revenue == D("1200.00")
    assert row.commission == D("480.00")           # 40% of 1,200


def test_awaken_force_override_beats_flat_rate_for_anjo():
    row = one(plan="Awaken Force", revenue="₱9600.00", staff="Anjo R")
    assert row.revenue == D("1200.00")
    assert row.commission == D("600.00")           # 50% of 1,200, not ₱750 flat


def test_hyrox_walkin_deduction():
    row = one(appt="Hyrox Simulation (With Coach)",
              variant="Walk-In : Simulation (2HR)", plan="Drop-In",
              revenue="₱3000.00", staff="Laurent J")
    assert row.revenue == D("2000.00")
    assert row.commission == D("800.00")


def test_hyrox_deduction_not_applied_to_self_paced():
    row = one(appt="Hyrox Simulation (Self Paced)",
              variant="Simulation Access (1HR)", plan="Drop-In",
              revenue="₱1000.00", staff="Laurent J")
    assert row.revenue == D("1000.00")


def test_hyrox_deduction_not_applied_to_members():
    row = one(appt="Hyrox Simulation (With Coach)",
              variant="Members : Simulation (1HR)", plan="Drop-In",
              revenue="₱1500.00", staff="Laurent J")
    assert row.revenue == D("1500.00")


# ------------------------------------------------------------------ backfill

def test_credit_row_is_backfilled():
    row = one(plan="24 Sessions", pay="Credit", revenue="₱0.00")
    assert row.revenue == D("1600.00")
    assert row.adjustment == "zero_backfill"


def test_free_row_not_backfilled_by_default():
    """Conservative default — comped sessions earn nothing until asked."""
    row = one(plan="36 Sessions", pay="Free", revenue="₱0.00", staff="Anjo R")
    assert row.revenue == D("0.00")
    assert row.adjustment is None


def test_free_row_backfilled_when_scope_widened():
    cfg = config(backfill_scope=BACKFILL_CREDIT_AND_FREE)
    row = one(plan="36 Sessions", pay="Free", revenue="₱0.00", cfg=cfg)
    assert row.revenue == D("1500.00")


def test_membership_plans_never_backfilled():
    for plan in ("12 Months", "3 Months", "12 Months (Corporate)"):
        row = one(plan=plan, pay="Free", revenue="₱0.00")
        assert row.revenue == D("0.00"), plan


def test_dropin_never_backfilled():
    row = one(plan="Drop-In", pay="Credit", revenue="₱0.00")
    assert row.adjustment is None


def test_nonzero_revenue_is_never_backfilled():
    row = one(plan="12 Sessions", pay="Credit", revenue="₱1234.00")
    assert row.revenue == D("1234.00")


# --------------------------------------------------------------------- scope

def test_rows_without_staff_are_dropped_with_a_reason():
    result = run(csv(staff="--"), config())
    assert result.rows == []
    assert result.dropped[0].reason == "no staff assigned"


def test_free_rule_is_literal_and_does_not_delete_delegation():
    """'Free' lives in Payment method, never in Pricing plan. Widening the rule
    to Payment method would delete every delegation row."""
    result = run(csv(variant="Delegation – KP", plan="Drop-In", pay="Free",
                     revenue="₱0.00"), config())
    assert result.dropped == []
    assert len(result.rows) == 1


def test_plan_actually_containing_free_is_dropped():
    result = run(csv(plan="Free Trial"), config())
    assert result.rows == []
    assert "free" in result.dropped[0].reason


def test_unmapped_staff_blocks_the_run():
    result = run(csv(staff="Migz T"), config())
    assert result.unmapped_staff == {"Migz T": 1}
    assert result.blocking is True


def test_double_spaced_staff_name_still_matches():
    row = one(staff="Anjo  R")
    assert row.coach == "Anjo"


# ---------------------------------------------------------------- commission

def test_percent_coach():
    row = one(staff="Julio D", plan="12 Sessions", revenue="₱1700.00")
    assert row.commission == D("1190.00")          # 70%


def test_flat_coach():
    row = one(staff="Anjo R", plan="12 Sessions", revenue="₱1700.00")
    assert row.rate_type == "flat"
    assert row.commission == D("750.00")


def test_flat_coach_override_on_dropin():
    row = one(staff="JC S", plan="Drop-In", revenue="₱3000.00")
    assert row.rule == "plan_override"
    assert row.commission == D("1500.00")          # 50%, not ₱750


def test_flat_coach_is_paid_on_a_comped_session():
    """Confirmed rule: a flat rate ignores revenue, so a ₱0 comped session
    still pays ₱750. The coach ran the session either way."""
    row = one(staff="Anjo R", plan="36 Sessions", pay="Free", revenue="₱0.00")
    assert row.revenue == D("0.00")
    assert row.commission == D("750.00")


def test_flat_coach_comped_exception_is_delegation():
    """...except delegation, which pays the delegator's cost instead."""
    row = one(staff="Anjo R", variant="Delegation – KP", plan="Drop-In",
              pay="Free", revenue="₱0.00")
    assert row.rule == "delegation"
    assert row.commission == D("640.00")


def test_percent_coach_has_no_override():
    row = one(staff="AR M", plan="Drop-In", revenue="₱3000.00")
    assert row.commission == D("1200.00")          # 40% everywhere


def test_non_completed_rows_earn_nothing():
    for status in ("Cancelled", "Late cancelled", "No show", "Booked"):
        row = one(status=status)
        assert row.commission is None, status
        assert row.revenue == D("1700.00"), status  # raw revenue retained


# ------------------------------------------------------- reviewer approval

def test_approving_a_cancelled_row_pays_it():
    """A late cancel that was still charged should be payable on review."""
    cfg = config()
    row = one(status="Late cancelled", staff="Julio D", cfg=cfg)
    assert row.commission is None
    row.approved = True
    engine_recompute(row, cfg)
    assert row.commission == D("1190.00")          # 70% of 1,700


def test_un_approving_reverts_to_no_commission():
    cfg = config()
    row = one(status="Cancelled", staff="Julio D", cfg=cfg)
    row.approved = True
    engine_recompute(row, cfg)
    assert row.commission == D("1190.00")
    row.approved = False
    engine_recompute(row, cfg)
    assert row.commission is None
    assert row.revenue == D("1700.00")             # back to the exported figure


def test_approval_applies_the_same_revenue_adjustments():
    """An approved row is not a special case — Awaken Force still overrides."""
    cfg = config()
    row = one(status="No show", plan="Awaken Force", revenue="₱9600.00",
              staff="AR M", cfg=cfg)
    assert row.revenue == D("9600.00")             # untouched while unapproved
    row.approved = True
    engine_recompute(row, cfg)
    assert row.revenue == D("1200.00")
    assert row.commission == D("480.00")


def test_approved_delegation_row_pays_the_delegation_cost():
    cfg = config()
    row = one(status="Cancelled", variant="Delegation – KP", plan="Drop-In",
              pay="Free", revenue="₱0.00", staff="Julio D", cfg=cfg)
    row.approved = True
    engine_recompute(row, cfg)
    assert row.rule == "delegation"
    assert row.commission == D("640.00")
    assert row.delegation_charge == D("1000.00")


def test_completed_rows_are_commissionable_without_approval():
    row = one(status="Completed")
    assert row.is_commissionable is True
    assert row.approved is False


def test_approval_moves_a_row_into_the_totals():
    cfg = config()
    csv_two = csv(status="Cancelled", staff="Julio D") + "\n" + \
        ",".join(["BK2", "B Client", "04 Jun 2026", "Private Coaching", "01:30",
                  "Private Coaching", "Julio D", "Completed", "01 Jun 2026",
                  "12 Sessions", "Manual", "1", "12", "Business Portal", "--",
                  "--", "₱1700.00", "₱1700.00"])
    result = run(csv_two, cfg)
    assert result.totals()["sessions"] == 1        # only the Completed one
    cancelled = next(r for r in result.rows if r.booking_status == "Cancelled")
    cancelled.approved = True
    engine_recompute(cancelled, cfg)
    assert result.totals()["sessions"] == 2
    assert result.totals()["commission"] == D("2380.00")


def test_rounding_is_half_up_to_two_places():
    row = one(staff="Rick F", plan="Drop-In", revenue="₱357.15")
    assert row.commission == D("178.58")           # 178.575 -> 178.58


# ------------------------------------------------------------------- parsing

def test_currency_and_thousands_separators():
    # A real export writes ₱1700.00 with no separator, but a quoted value with
    # one must still parse rather than silently becoming ₱1.
    row = one(revenue='"₱1,700.00"')
    assert row.revenue == D("1700.00")


def test_rate_snapshot_is_recorded_on_every_row():
    """The stored rate is what makes a past run immutable."""
    row = one(staff="Julio D")
    assert (row.rate_type, row.rate_value) == ("percent", D("0.70"))
