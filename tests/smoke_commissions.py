"""End-to-end smoke test: log in, upload a real export, finalize, open everything.

Run with a live database:

    DATABASE_URL=... python3 tests/smoke_commissions.py path/to/export.csv
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient          # noqa: E402
from app.main import app                          # noqa: E402

CSV = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/sample.csv"
fails = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("  " + detail if detail else ""))
    if not ok:
        fails.append(label)


with TestClient(app) as c:
    r = c.post("/login", data={"username": "admin", "pin": "1234"}, follow_redirects=False)
    check("login", r.status_code == 303, r.headers.get("location", ""))

    print("\nphase 2 — configuration")
    for url in ("/admin/commission-rates", "/admin/commission-delegators",
                "/admin/commission-settings"):
        r = c.get(url)
        check(url, r.status_code == 200)

    r = c.get("/admin/commission-rates")
    n_coaches = len(set(re.findall(r"/admin/commission-rates/(\d+)", r.text)))
    check("  seven coaches seeded", n_coaches == 7, "found %d" % n_coaches)
    r = c.get("/admin/commission-delegators")
    check("  Gab + Culver seeded", "Gab Rosario" in r.text and "Culver Padilla" in r.text)
    check("  Culver carries both codes", "KP,CP" in r.text)

    print("\nphase 3 — upload & preview")
    r = c.get("/commissions")
    check("/commissions", r.status_code == 200)
    r = c.get("/commissions/new")
    check("/commissions/new", r.status_code == 200)

    with open(CSV, "rb") as fh:
        r = c.post("/commissions/new", files={"file": ("export.csv", fh, "text/csv")},
                   follow_redirects=False)
    check("upload redirects to run", r.status_code == 303, r.headers.get("location", ""))
    run_url = r.headers.get("location", "")
    rid = run_url.rstrip("/").split("/")[-1]

    for tab in ("summary", "adjustments", "dropped", "delegation", "coaches"):
        r = c.get(f"/commissions/{rid}?tab={tab}")
        check(f"  tab={tab}", r.status_code == 200)

    r = c.get(f"/commissions/{rid}?tab=summary")
    check("  delegation has its own pivot column", ">Delegation<" in r.text)
    check("  no unmapped-staff blocker", "Finalizing is blocked" not in r.text)
    for coach in ("Anjo", "Julio", "Laurent"):
        check(f"  coach {coach} in pivot", f">{coach}</b>" in r.text)
        r2 = c.get(f"/commissions/{rid}/coach/{coach}")
        check(f"  /coach/{coach}", r2.status_code == 200)

    # The screen must agree with the engine, to the peso.
    from tests.conf_sample import config as engine_config
    from app.commissions import run as engine_run
    expected = engine_run(open(CSV, "rb").read(), engine_config()).totals()
    page = c.get(f"/commissions/{rid}?tab=summary").text
    for label, value in (("commission", expected["commission"]),
                         ("delegation charged", expected["delegation_charged"]),
                         ("delegation cost", expected["delegation_cost"])):
        shown = "₱{:,.2f}".format(float(value))
        check(f"  screen shows {label} {shown}", shown in page)

    print("\nreview — non-Completed statuses")
    # The Paid Bookings export is Completed-only, so synthesise the other
    # statuses to prove the review flow end to end.
    src = open(CSV, encoding="utf-8-sig").read().splitlines()
    head, body = src[0], src[1:]
    extra = []
    for i, status in enumerate(("Cancelled", "Late cancelled", "No show", "Booked")):
        cells = body[i].split(",")
        cells[0] = "REVIEW%d" % i
        cells[7] = status
        extra.append(",".join(cells))
    mixed = "\n".join([head] + body + extra)
    r = c.post("/commissions/new", files={"file": ("mixed.csv", mixed.encode(), "text/csv")},
               follow_redirects=False)
    mid = r.headers.get("location", "").rstrip("/").split("/")[-1]
    page = c.get(f"/commissions/{mid}?tab=coaches").text
    check("  coach table lists awaiting-review counts", "Awaiting review" in page)
    r = c.get(f"/commissions/{mid}?tab=summary")
    check("  preview flags rows awaiting review", "awaiting review" in r.text)

    # Find the coach page holding a REVIEW row and approve one booking.
    approved_any = False
    for coach in ("Anjo", "AR", "JC", "Ric", "Laurent", "Joseph", "Julio"):
        page = c.get(f"/commissions/{mid}/coach/{coach}").text
        m = re.search(r'/commissions/%s/booking/(\d+)/approve' % mid, page)
        if not m:
            continue
        before = page
        check(f"  {coach}: review controls present", "Include" in before)
        r = c.post(f"/commissions/{mid}/booking/{m.group(1)}/approve",
                   follow_redirects=False)
        check("  approve toggles", r.status_code == 303)
        after = c.get(f"/commissions/{mid}/coach/{coach}").text
        check("  approved row now counts", "✓ Yes" in after)
        r = c.post(f"/commissions/{mid}/booking/{m.group(1)}/approve",
                   follow_redirects=False)
        after2 = c.get(f"/commissions/{mid}/coach/{coach}").text
        check("  un-approve reverts", "✓ Yes" not in after2)
        # bulk approve a whole status for this coach
        r = c.post(f"/commissions/{mid}/coach/{coach}/approve-status",
                   data={"status": "Cancelled", "include": "on"},
                   follow_redirects=False)
        check("  bulk approve-status", r.status_code == 303)
        approved_any = True
        break
    check("  found a coach with reviewable rows", approved_any)
    c.post(f"/commissions/{mid}/delete", follow_redirects=False)

    # Re-uploading a period supersedes the earlier draft, so rebuild it before
    # finalizing. (That supersede behaviour is deliberate — see commissions_upload.)
    with open(CSV, "rb") as fh:
        r = c.post("/commissions/new", files={"file": ("export.csv", fh, "text/csv")},
                   follow_redirects=False)
    rid = r.headers.get("location", "").rstrip("/").split("/")[-1]
    check("  re-upload recreates the draft", r.status_code == 303)

    print("\nphase 4 — finalize")
    r = c.post(f"/commissions/{rid}/finalize", follow_redirects=False)
    check("finalize", r.status_code == 303, r.headers.get("location", ""))
    r = c.get(f"/commissions/{rid}?tab=documents")
    check("documents tab", r.status_code == 200)
    payouts = sorted(set(re.findall(r"/commissions/payouts/(\d+)", r.text)))
    charges = sorted(set(re.findall(r"/commissions/charges/(\d+)", r.text)))
    check("  7 coach payouts", len(payouts) == 7, f"got {len(payouts)}")
    check("  2 delegation charges", len(charges) == 2, f"got {len(charges)}")

    for pid in payouts:
        rr = c.get(f"/commissions/payouts/{pid}")
        check(f"  payout {pid}", rr.status_code == 200)
    for cid in charges:
        rr = c.get(f"/commissions/charges/{cid}")
        check(f"  charge {cid}", rr.status_code == 200)

    r = c.post(f"/commissions/payouts/{payouts[0]}/paid", follow_redirects=False)
    check("  mark paid", r.status_code == 303)

    r = c.post(f"/commissions/{rid}/finalize", follow_redirects=False)
    check("  finalizing twice is refused", r.status_code == 303)
    r = c.get(f"/commissions/{rid}?tab=documents")
    check("  still 7 payouts after re-finalize",
          len(set(re.findall(r"/commissions/payouts/(\d+)", r.text))) == 7)

    print("\nregression — existing pages still render")
    for url in ("/dashboard", "/sales", "/admin/staff", "/coaches/billing",
                "/invoices", "/customers", "/stock", "/admin/reports"):
        rr = c.get(url)
        check(url, rr.status_code == 200, str(rr.status_code))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
