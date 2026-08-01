"""Unit tests for the commission engine.

Every case here is drawn from something actually present in a real Rezerv
export, or from a rule in the commission spec that the export can violate.
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.commissions import (                                   # noqa: E402
    BACKFILL_CREDIT_AND_FREE, BACKFILL_CREDIT_ONLY, CoachRate, Config, Delegator,
    delegation_code, FLAT, PERCENT, run, SessionRate,
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


def test_free_row_not_backfilled_when_scope_is_credit_only():
    cfg = config(backfill_scope=BACKFILL_CREDIT_ONLY)
    row = one(plan="36 Sessions", pay="Free", revenue="₱0.00", staff="Anjo R", cfg=cfg)
    assert row.revenue == D("0.00")
    assert row.adjustment is None


def test_comped_row_is_backfilled_by_default():
    """Default is credit_and_free: a comped session shows the session rate
    rather than ₱0, so the revenue column always carries a real amount."""
    cfg = config(backfill_scope=BACKFILL_CREDIT_AND_FREE)
    row = one(plan="36 Sessions", pay="Free", revenue="₱0.00", cfg=cfg)
    assert row.revenue == D("1500.00")
    assert row.adjustment == "zero_backfill"


def test_delegated_rows_keep_zero_revenue_even_when_backfilling():
    """The client pays the delegator, not AWAKEN — so a delegated booking has
    no gym revenue no matter how wide the backfill scope is."""
    cfg = config(backfill_scope=BACKFILL_CREDIT_AND_FREE)
    row = one(variant="Delegation – KP", plan="Drop-In", pay="Free",
              revenue="₱0.00", staff="Laurent J", cfg=cfg)
    assert row.revenue == D("0.00")
    assert row.commission == D("640.00")


def test_memberships_keep_zero_revenue_even_when_backfilling():
    cfg = config(backfill_scope=BACKFILL_CREDIT_AND_FREE)
    for plan in ("12 Months", "3 Months"):
        row = one(plan=plan, pay="Free", revenue="₱0.00", cfg=cfg)
        assert row.revenue == D("0.00"), plan


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
    still pays ₱750. The coach ran the session either way.

    Pinned to credit_only so the revenue really is ₱0 — under the default
    scope it would be backfilled and the test would prove nothing.
    """
    cfg = config(backfill_scope=BACKFILL_CREDIT_ONLY)
    row = one(staff="Anjo R", plan="36 Sessions", pay="Free", revenue="₱0.00", cfg=cfg)
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


def test_unreviewed_statuses_earn_nothing():
    for status in ("Cancelled", "No show", "Booked"):
        row = one(status=status)
        assert row.commission is None, status
        assert row.revenue == D("1700.00"), status  # raw revenue retained


# --------------------------------------------------- statuses that pay alone

def test_completed_pays_without_review():
    row = one(status="Completed", staff="Julio D")
    assert row.pays_by_status
    assert row.commission == D("1190.00")          # 70% of 1,700


def test_late_cancelled_pays_without_review():
    """The client was charged and the coach lost the hour, so it pays."""
    row = one(status="Late cancelled", staff="Julio D")
    assert row.pays_by_status
    assert not row.approved                        # nobody had to approve it
    assert row.commission == D("1190.00")


def test_late_cancelled_spelling_does_not_matter():
    for status in ("Late cancelled", "Late Cancelled", "late-cancelled",
                   "  LATE   CANCELLED  "):
        assert one(status=status, staff="Julio D").commission == D("1190.00"), status


def test_a_paying_status_still_gets_the_revenue_adjustments():
    """An approved row is not a special case, and neither is a late cancel:
    the same normalisation runs before the rate is applied."""
    row = one(status="Late cancelled", staff="Julio D", plan="24 Sessions",
              pay="Credit", revenue="₱0.00")
    assert row.adjustment == "zero_backfill"
    assert row.revenue == D("1600.00")
    assert row.commission == D("1120.00")          # 70% of the backfilled 1,600


def test_which_statuses_pay_is_configurable():
    cfg = config()
    cfg.settings.paid_statuses = frozenset({"completed"})
    row = one(status="Late cancelled", staff="Julio D", cfg=cfg)
    assert not row.pays_by_status
    assert row.commission is None


def test_a_status_that_pays_needs_no_approval_flag():
    """Approving a row that already pays must not double it."""
    cfg = config()
    row = one(status="Late cancelled", staff="Julio D", cfg=cfg)
    before = row.commission
    row.approved = True
    engine_recompute(row, cfg)
    assert row.commission == before


# ------------------------------------------------------- reviewer approval

def test_approving_a_cancelled_row_pays_it():
    """A cancellation that was still charged should be payable on review."""
    cfg = config()
    row = one(status="Cancelled", staff="Julio D", cfg=cfg)
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


# -------------------------------------------------------- status filtering

def test_only_selected_statuses_are_imported():
    cfg = config()
    cfg.settings.statuses = frozenset({"completed"})
    result = run(csv(status="Cancelled"), cfg)
    assert result.rows == []
    assert "not selected" in result.dropped[0].reason


def test_selected_statuses_pass_through():
    cfg = config()
    cfg.settings.statuses = frozenset({"completed", "late cancelled"})
    for status in ("Completed", "Late cancelled"):
        result = run(csv(status=status), cfg)
        assert len(result.rows) == 1, status


def test_no_status_filter_means_everything():
    cfg = config()
    assert cfg.settings.statuses is None
    for status in ("Completed", "Cancelled", "No show", "Booked"):
        assert len(run(csv(status=status), cfg).rows) == 1, status


def test_status_filter_is_case_insensitive():
    cfg = config()
    cfg.settings.statuses = frozenset({"no show"})
    assert len(run(csv(status="No Show"), cfg).rows) == 1


# ---------------------------------------------------- dated session rates

def _dated(cfg, *rates):
    cfg.settings.session_rates = tuple(rates)
    return cfg


def test_session_rate_picks_the_one_in_force_that_day():
    """A booking is valued with the rate that applied on its own date, not
    with whatever the rate happens to be now."""
    cfg = _dated(config(),
                 SessionRate("12 Sessions", D("1700"), 12,
                             effective_to=date(2026, 5, 31)),
                 SessionRate("12 Sessions", D("2000"), 12,
                             effective_from=date(2026, 6, 1)))
    row = one(plan="12 Sessions", pay="Credit", revenue="₱0.00", cfg=cfg)
    assert row.appointment_date == date(2026, 6, 3)
    assert row.revenue == D("2000.00")


def test_session_rate_before_the_increase_uses_the_old_rate():
    cfg = _dated(config(),
                 SessionRate("12 Sessions", D("1700"), 12,
                             effective_to=date(2026, 5, 31)),
                 SessionRate("12 Sessions", D("2000"), 12,
                             effective_from=date(2026, 6, 1)))
    assert cfg.settings.rate_for("12 Sessions", date(2026, 5, 30)).rate == D("1700")


def test_open_ended_rate_covers_everything():
    cfg = _dated(config(), SessionRate("12 Sessions", D("1700"), 12))
    for day in (date(2020, 1, 1), date(2030, 1, 1)):
        assert cfg.settings.rate_for("12 Sessions", day).rate == D("1700")


def test_overlapping_rates_take_the_most_recent():
    """Adding a new rate without closing the old one still does the right
    thing, rather than silently picking whichever was listed first."""
    cfg = _dated(config(),
                 SessionRate("12 Sessions", D("1700"), 12),
                 SessionRate("12 Sessions", D("2000"), 12,
                             effective_from=date(2026, 1, 1)))
    assert cfg.settings.rate_for("12 Sessions", date(2026, 6, 3)).rate == D("2000")


def test_plan_with_no_rate_is_not_backfilled():
    cfg = _dated(config(), SessionRate("12 Sessions", D("1700"), 12))
    row = one(plan="8 Sessions", pay="Credit", revenue="₱0.00", cfg=cfg)
    assert row.revenue == D("0.00")
    assert row.adjustment is None


def test_plan_match_is_case_insensitive():
    cfg = _dated(config(), SessionRate("12 SESSIONS", D("1700"), 12))
    assert cfg.settings.rate_for("12 sessions", date(2026, 6, 3)).rate == D("1700")


# --------------------------------------------------- Awaken Force rate card

def test_awaken_force_uses_its_own_rate_card():
    """AF is a separate card from the PT packs: 1 session ₱1,500, 8 ₱1,200."""
    cfg = config()
    assert cfg.settings.rate_for("Awaken Force", date(2026, 6, 3),
                                 package=D("9600")).rate == D("1200")
    assert cfg.settings.rate_for("Awaken Force", date(2026, 6, 3),
                                 package=D("1500")).rate == D("1500")


def test_awaken_force_pack_identified_by_the_exported_total():
    """Every AF row exports credit 1, so the pack size can only come from the
    package total — 8 × 1,200 = 9,600."""
    row = one(plan="Awaken Force", revenue="₱9600.00", staff="AR M")
    assert row.revenue == D("1200.00")
    assert "8-session pack" in row.adjustment_note
    assert row.commission == D("480.00")            # 40% of 1,200


def test_awaken_force_single_session_pack():
    row = one(plan="Awaken Force", revenue="₱1500.00", staff="AR M")
    assert row.revenue == D("1500.00")
    assert row.commission == D("600.00")            # 40% of 1,500


def test_session_count_comes_from_the_export_not_the_plan_name():
    """'Total pricing plan credit' carries the pack size for PT plans."""
    result = run(csv(plan="12 Sessions", pay="Credit", revenue="₱0.00"), config())
    assert result.rows[0].credits == 12


def test_credits_break_a_tie_when_no_package_total_matches():
    cfg = config()
    cfg.settings.session_rates = (
        SessionRate("Pack", D("900"), 4, "PT"),
        SessionRate("Pack", D("800"), 8, "PT"),
    )
    assert cfg.settings.rate_for("Pack", None, credits=8).rate == D("800")
    assert cfg.settings.rate_for("Pack", None, credits=4).rate == D("900")


# --------------------------------------------------------------------------
# per-plan overrides — one row per plan, each with its own basis and rate
# --------------------------------------------------------------------------

def test_two_plans_can_override_to_different_rates():
    """The point of a row per plan: Drop-In and Awaken Force need not agree.
    The old comma-separated column could only give both the same rate."""
    rate = CoachRate("Anjo", FLAT, D("750"), {
        "drop-in": (PERCENT, D("0.50")),
        "24 sessions": (PERCENT, D("0.60")),
    })
    cfg = config()
    cfg.coach_rates = dict(cfg.coach_rates, **{"Anjo R": rate})
    assert one(staff="Anjo R", plan="Drop-In", revenue="₱3000.00",
               cfg=cfg).commission == D("1500.00")
    assert one(staff="Anjo R", plan="24 Sessions", revenue="₱1600.00",
               cfg=cfg).commission == D("960.00")
    # and a plan with no row of its own still takes the ₱750 default
    assert one(staff="Anjo R", plan="12 Sessions", revenue="₱1700.00",
               cfg=cfg).commission == D("750.00")


def test_an_override_may_be_flat_while_the_default_is_percent():
    """A percent coach can still be paid a fixed amount on one plan."""
    rate = CoachRate("AR", PERCENT, D("0.40"), {"drop-in": (FLAT, D("900"))})
    cfg = config()
    cfg.coach_rates = dict(cfg.coach_rates, **{"AR M": rate})
    row = one(staff="AR M", plan="Drop-In", revenue="₱3000.00", cfg=cfg)
    assert row.rate_type == FLAT
    assert row.rule == "plan_override"
    assert row.commission == D("900.00")          # not 40% of 3000


def test_plan_match_is_case_and_space_insensitive():
    rate = CoachRate("AR", PERCENT, D("0.40"), {"drop-in": (PERCENT, D("0.50"))})
    cfg = config()
    cfg.coach_rates = dict(cfg.coach_rates, **{"AR M": rate})
    assert one(staff="AR M", plan="  DROP-IN ", revenue="₱1000.00",
               cfg=cfg).commission == D("500.00")


def test_a_plan_with_no_override_still_pays_the_default():
    rate = CoachRate("AR", PERCENT, D("0.40"), {"drop-in": (PERCENT, D("0.50"))})
    cfg = config()
    cfg.coach_rates = dict(cfg.coach_rates, **{"AR M": rate})
    row = one(staff="AR M", plan="12 Sessions", revenue="₱1700.00", cfg=cfg)
    assert row.rule == "coach_percent"
    assert row.commission == D("680.00")


def test_delegation_still_beats_a_plan_override():
    """Delegation is settled with the delegator, so an override on that plan
    must not reach it — otherwise a delegated Drop-In would pay twice."""
    rate = CoachRate("Anjo", FLAT, D("750"), {"drop-in": (PERCENT, D("0.50"))})
    cfg = config()
    cfg.coach_rates = dict(cfg.coach_rates, **{"Anjo R": rate})
    row = one(staff="Anjo R", variant="Delegation – KP", plan="Drop-In",
              revenue="₱3000.00", cfg=cfg)
    assert row.rule == "delegation"
    assert row.commission == D("640.00")


# ---------------------------------------------------------------- coach type


def _rate_row(coach_type="", person_type=None):
    """A CommissionCoachRate, optionally linked to a person."""
    from app.models import CommissionCoachRate, Staff
    r = CommissionCoachRate(coach="AR", staff_raw="AR M", coach_type=coach_type)
    if person_type is not None:
        r.entity = Staff(name="AR", person_type=person_type)
    return r


def test_the_tag_on_the_coach_wins():
    assert _rate_row("affiliate", "employee").kind == "affiliate"


def test_an_untagged_coach_inherits_the_person_type():
    assert _rate_row("", "affiliate").kind == "affiliate"


def test_a_coach_with_no_tag_and_no_person_is_untagged():
    assert _rate_row("").kind == ""
    assert _rate_row("", None).kind == ""


def test_a_person_type_that_is_not_a_coach_type_reads_as_untagged():
    """A coach linked to a customer or member record is not an affiliate.

    Passing the raw person_type through produced a chip with no label and no
    colour on the Coach rates screen.
    """
    for other in ("customer", "member", "supplier", "coach"):
        assert _rate_row("", other).kind == "", other
