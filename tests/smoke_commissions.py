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
    # Start from no runs at all. Finalized runs have no delete route (by
    # design), and leaving one behind would make the double-payment guard skip
    # every booking on the next pass — correct behaviour, wrong for a test.
    from app.db import SessionLocal                 # noqa: E402
    from app.models import (CommissionBooking, CommissionRun,   # noqa: E402
                            CommissionSignoff)
    _db = SessionLocal()
    for _run in _db.query(CommissionRun).all():
        _db.delete(_run)
    _db.commit()
    _db.close()

    r = c.post("/login", data={"username": "admin", "pin": "1234"}, follow_redirects=False)
    check("login", r.status_code == 303, r.headers.get("location", ""))

    print("\nphase 2 — configuration")
    for url in ("/admin/commission-rates", "/admin/commission-delegators",
                "/admin/commission-session-rates", "/admin/commission-settings"):
        r = c.get(url)
        check(url, r.status_code == 200)

    r = c.get("/admin/commission-rates")
    n_coaches = len(set(re.findall(r"/admin/commission-rates/(\d+)", r.text)))
    check("  seven coaches seeded", n_coaches == 7, "found %d" % n_coaches)
    r = c.get("/admin/commission-delegators")
    check("  Gab + Culver seeded", "Gab Rosario" in r.text and "Culver Padilla" in r.text)
    check("  Culver carries both codes", "KP,CP" in r.text)
    r = c.get("/admin/commission-session-rates")
    n_rates = len(set(re.findall(r"/admin/commission-session-rates/(\d+)\"", r.text)))
    check("  seven session rates seeded (5 PT + 2 AF)", n_rates == 7, "found %d" % n_rates)
    check("  session rates are dated", "effective_from" in r.text)
    check("  grouped by programme",
          "Private Coaching" in r.text and "Awaken Force" in r.text)
    check("  Awaken Force pack totals present", "9600" in r.text and "1200" in r.text)
    r = c.get("/admin/commission-rates")
    check("  coach is a person picker, not free text", 'name="coach_id"' in r.text)
    check("  rows open a popup", 'data-modal="coach-' in r.text)
    check("  a panel exists per coach",
          len(re.findall(r'class="scrim" id="coach-\d+"', r.text)) == 7)
    check("  override count chip shows 2 for the flat coaches",
          r.text.count('</svg>2</span>') == 2)
    check("  coaches with no override show a dash", r.text.count('ov none') == 5)

    print("\n  overrides — a row per plan")
    rid_anjo = re.search(r'action="/admin/commission-rates/(\d+)"[\s\S]{0,400}?Anjo',
                         r.text) or re.search(r'/admin/commission-rates/(\d+)', r.text)
    # find Anjo's panel specifically
    for m in re.finditer(r'<div class="scrim" id="coach-(\d+)">([\s\S]*?)\n</div>', r.text):
        if ">Anjo<" in m.group(2):
            anjo, panel = m.group(1), m.group(2)
            break
    else:
        anjo, panel = None, ""
    check("  found Anjo's panel", anjo is not None)
    check("    two override rows", panel.count('name="ov_id"') == 2)
    check("    plan is a dropdown, not a textbox", 'name="ov_plan"' in panel)
    check("    each row carries its own basis and rate",
          panel.count('name="ov_type"') == 2 and panel.count('name="ov_value"') == 2)
    check("    an add row is present", 'name="new_plan"' in panel)

    ids = re.findall(r'name="ov_id" value="(\d+)"', panel)
    # edit one override to a different basis, add a third plan, in one save
    r2 = c.post(f"/admin/commission-rates/{anjo}", data={
        "coach": "Anjo", "staff_raw": "Anjo R", "coach_id": "",
        "rate_type": "flat", "rate_value": "750", "is_active": "on",
        "ov_id": ids, "ov_plan": ["Drop-In", "Awaken Force"],
        "ov_type": ["flat", "percent"], "ov_value": ["900", "60"],
        "ov_active": ["on", "on"],
        "new_plan": "12 Sessions", "new_type": "percent", "new_value": "55",
    }, follow_redirects=False)
    check("    save returns to the list", r2.status_code == 303)
    page = c.get("/admin/commission-rates").text
    panel = [m.group(2) for m in
             re.finditer(r'<div class="scrim" id="coach-(\d+)">([\s\S]*?)\n</div>', page)
             if m.group(1) == anjo][0]
    check("    third override added", panel.count('name="ov_id"') == 3)
    check("    chip now counts 3", '</svg>3</span>' in page)
    check("    one plan is flat while another is percent",
          'value="900"' in panel and 'value="60"' in panel)

    # the engine must see the new numbers
    from app.db import SessionLocal as _SL                      # noqa: E402
    from app.commission_routes import build_config as _bc       # noqa: E402
    _d = _SL(); _cfg = _bc(_d); _d.close()
    ov = _cfg.coach_rates["Anjo R"].overrides
    check("    engine sees three plans", len(ov) == 3, str(sorted(ov)))
    check("    engine sees Drop-In as flat 900", ov.get("drop-in") == ("flat", 900))
    check("    a plan with no row falls back to the default",
          _cfg.coach_rates["Anjo R"].for_plan("36 Sessions") == ("flat", 750))

    # delete the one we added, put Anjo back the way he was
    newest = re.findall(r'name="ov_id" value="(\d+)"', panel)
    gone = [i for i in newest if i not in ids][0]
    r2 = c.post(f"/admin/commission-rates/{anjo}/override-delete",
                data={"ov_delete": gone}, follow_redirects=False)
    check("    delete removes just that plan", r2.status_code == 303)
    page = c.get("/admin/commission-rates").text
    check("    chip back to 2", '</svg>2</span>' in page)
    c.post(f"/admin/commission-rates/{anjo}", data={
        "coach": "Anjo", "staff_raw": "Anjo R", "coach_id": "",
        "rate_type": "flat", "rate_value": "750", "is_active": "on",
        "ov_id": ids, "ov_plan": ["Drop-In", "Awaken Force"],
        "ov_type": ["percent", "percent"], "ov_value": ["50", "50"],
        "ov_active": ["on", "on"],
    }, follow_redirects=False)
    _d = _SL(); _cfg = _bc(_d); _d.close()
    check("    restored to 50/50",
          _cfg.coach_rates["Anjo R"].overrides.get("drop-in") == ("percent", 0.50))

    r = c.get("/admin/commission-delegators")
    check("  delegator is a person picker", 'name="entity_id"' in r.text)

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
    # The run *is* blocked at this point — every coach still needs approving.
    # What must not appear is an unmapped-staff or unknown-delegator blocker.
    check("  no unmapped-staff blocker", "don't match any coach" not in r.text)
    check("  no unknown-delegator blocker", "isn't configured" not in r.text)
    check("  unapproved coaches block on their own", "haven't been approved yet" in r.text)
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

    print("\nimport modes — duplicates and status selection")
    src = open(CSV, encoding="utf-8-sig").read().splitlines()
    head, body = src[0], src[1:]

    def upload(text, mode="merge", statuses=("completed", "cancelled",
                                             "late_cancelled", "no_show", "booked")):
        data = {"mode": mode}
        for s in statuses:
            data["status_" + s] = "on"
        return c.post("/commissions/new",
                      files={"file": ("x.csv", text.encode(), "text/csv")},
                      data=data, follow_redirects=False)

    # start clean for this period
    for r0 in re.findall(r"/commissions/(\d+)\"", c.get("/commissions").text):
        c.post(f"/commissions/{r0}/delete", follow_redirects=False)

    half, rest = body[:150], body[150:]
    r = upload("\n".join([head] + half))
    dup_rid = r.headers.get("location", "").rstrip("/").split("/")[-1]
    page = c.get(f"/commissions/{dup_rid}").text
    check("  first import lands", r.status_code == 303)
    check("  import receipt shown", "Last import" in page)
    check("  run rows are clickable", 'data-href="/commissions/' in c.get("/commissions").text)

    # same file again in merge mode → everything is a duplicate
    r = upload("\n".join([head] + half))
    page = c.get(f"/commissions/{dup_rid}").text
    check("  re-import merges into the same run",
          r.headers.get("location", "").endswith("/" + dup_rid))
    check("  duplicates skipped, none added",
          "0 imported" in page and "already in this run" in page)

    # add the remaining rows — new refs only
    r = upload("\n".join([head] + half + rest))
    page = c.get(f"/commissions/{dup_rid}").text
    m = re.search(r"(\d+) imported", page)
    # "imported" counts rows that survive scoping, so rows with no staff
    # assigned are stored as dropped and don't appear in that figure.
    expected = sum(1 for line in rest if line.split(",")[6].strip() != "--")
    check("  merge adds only the unseen refs", m and int(m.group(1)) == expected,
          "%s, expected %d" % (m.group(0) if m else "no count", expected))

    # status selection
    mixed = "\n".join([head] + body + [
        ",".join(["ONLYCANC"] + body[0].split(",")[1:7] + ["Cancelled"] + body[0].split(",")[8:])])
    r = upload(mixed, mode="replace", statuses=("completed",))
    rid2 = r.headers.get("location", "").rstrip("/").split("/")[-1]
    page = c.get(f"/commissions/{rid2}?tab=dropped").text
    check("  unticked status is dropped with a reason", "not selected" in page)
    check("  status filter reported on the receipt",
          "filtered out by status" in c.get(f"/commissions/{rid2}").text)
    c.post(f"/commissions/{rid2}/delete", follow_redirects=False)

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

    print("\nsearch + columns on the row tables")
    page = c.get(f"/commissions/{rid}?tab=delegation").text
    check("  delegation has a search box", 'data-filter="#deleg"' in page)
    check("    and a Customer column", ">Customer</th>" in page)
    check("    and a Session column", ">Session</th>" in page)
    check("    customers are actually rendered",
          len(re.findall(r"<td>[A-Z][a-z]+ [A-Z]", page)) > 5)
    check("    money cells carry values for live totals",
          'data-money="charged"' in page and 'data-value=' in page)
    check("    subtotals sit in a tfoot so search can hide them",
          "<tfoot>" in page)
    for tab, tid in (("adjustments", "adj"), ("dropped", "drop")):
        p2 = c.get(f"/commissions/{rid}?tab={tab}").text
        check(f"  {tab} has a search box", ('data-filter="#%s"' % tid) in p2)
        check("    and a Customer column", ">Customer</th>" in p2)
    p2 = c.get(f"/commissions/{rid}?tab=statuses&status=Completed").text
    check("  status detail has a search box", 'data-filter="#stat"' in p2)
    # every tab that lists rows gets one, not only the ones with an obvious need
    for tab, tid in (("summary", "pivot"), ("coaches", "coaches")):
        p3 = c.get(f"/commissions/{rid}?tab={tab}").text
        check(f"  {tab} has a search box", ('data-filter="#%s"' % tid) in p3)
    coach0 = re.search(r'/commissions/%s/coach/([^"?#/]+)' % rid,
                       c.get(f"/commissions/{rid}?tab=coaches").text).group(1)
    p3 = c.get(f"/commissions/{rid}/coach/{coach0}").text
    check("  a coach page searches all its status tables at once",
          'data-filter="table.bktbl"' in p3)
    check("    every search box names the note it writes into",
          p3.count('data-note=') == p3.count('class="tsearch"'))

    print("\ncoach page — status filter instead of stacked groups")
    page = c.get(f"/commissions/{rid}/coach/{coach0}").text
    check("  filter pills are present", 'href="?status=' in page)
    check("    All is selected by default", '<a href="?" class="on">All' in page)
    check("    one table, not one per status", page.count('class="bktbl"') == 1)
    check("    the flat view names each row's status", ">Status</th>" in page)
    p2 = c.get(f"/commissions/{rid}/coach/{coach0}?status=Completed").text
    check("  picking a status filters the table", "Completed · " in p2)
    check("    and drops the Status column, since every row shares it",
          ">Status</th>" not in p2)
    n_all = page.count('<tr id=')
    n_one = p2.count('<tr id=')
    check("    fewer rows than All", 0 < n_one <= n_all, "%d of %d" % (n_one, n_all))
    p3 = c.get(f"/commissions/{rid}/coach/{coach0}?status=Nonexistent").text
    check("  an unknown status falls back to All rather than an empty page",
          p3.count('<tr id=') == n_all)

    print("\nstatuses tab")
    page = c.get(f"/commissions/{rid}?tab=statuses").text
    check("  tab renders", "Every booking, by status" in page)
    check("  Completed bucket present", ">Completed</b>" in page)
    check("  bucket counts add up to the run",
          str(int(re.search(r'<td>Total</td><td style="text-align:right">(\d+)</td>',
                            page).group(1))) ==
          re.search(r'<td>Total</td><td style="text-align:right">(\d+)</td>',
                    page).group(1))
    page = c.get(f"/commissions/{rid}?tab=statuses&status=Completed").text
    check("  picking a status lists its bookings", "booking" in page and "Counts?" in page)

    print("\nper-row rate override")
    coach0 = re.search(r'/commissions/%s/coach/([^"?#]+)' % rid,
                       c.get(f"/commissions/{rid}?tab=coaches").text).group(1)
    page = c.get(f"/commissions/{rid}/coach/{coach0}").text
    check("  rate cell is editable", 'name="rate_value"' in page)
    bid = re.search(r'/commissions/%s/booking/(\d+)/rate' % rid, page).group(1)
    r = c.post(f"/commissions/{rid}/booking/{bid}/rate",
               data={"rate_type": "flat", "rate_value": "1234"},
               follow_redirects=False)
    check("  set a rate on one row", r.status_code == 303)
    page = c.get(f"/commissions/{rid}/coach/{coach0}").text
    check("    row is marked manual", "Manual" in page)
    check("    row now pays the typed amount", "1,234.00" in page)
    r = c.post(f"/commissions/{rid}/recalculate", follow_redirects=False)
    page = c.get(f"/commissions/{rid}/coach/{coach0}").text
    check("    survives Recalculate", "1,234.00" in page and "Manual" in page)
    r = c.post(f"/commissions/{rid}/booking/{bid}/rate",
               data={"reset": "on"}, follow_redirects=False)
    page = c.get(f"/commissions/{rid}/coach/{coach0}").text
    check("    reset puts it back", "1,234.00" not in page)

    print("\nPDF statement")
    r = c.get(f"/commissions/{rid}/coach/{coach0}/statement.pdf")
    check("  renders", r.status_code == 200 and r.content[:5] == b"%PDF-")
    check("  served inline for preview",
          "inline" in r.headers.get("content-disposition", ""))
    check("  is a real document", len(r.content) > 3000, "%d bytes" % len(r.content))
    r2 = c.get(f"/commissions/{rid}/coach/{coach0}/statement.pdf?download=1")
    check("  download variant attaches",
          "attachment" in r2.headers.get("content-disposition", ""))

    print("\nadmin-only: approving and repricing")
    # A reviewer with manage_commissions can read the run but must not be able
    # to approve a coach or type a rate onto a booking. Hiding the buttons is
    # not enough — the routes themselves have to refuse.
    from app.auth import hash_pin                                # noqa: E402
    from app.models import Staff as _Staff                       # noqa: E402
    _d = _SL()
    reviewer = _d.query(_Staff).filter_by(username="reviewer").first()
    if not reviewer:
        reviewer = _Staff(name="Reviewer", username="reviewer", role="staff",
                          person_type="employee", is_active=True)
        _d.add(reviewer)
    reviewer.role = "staff"
    reviewer.permissions = "manage_commissions"
    reviewer.pin_hash, reviewer.pin_salt = hash_pin("4321")
    _d.commit()
    _d.close()

    from fastapi.testclient import TestClient as _TC             # noqa: E402
    with _TC(app) as rc:
        r = rc.post("/login", data={"username": "reviewer", "pin": "4321"},
                    follow_redirects=False)
        check("  reviewer can log in", r.status_code == 303)
        r = rc.get(f"/commissions/{rid}/coach/{coach0}")
        check("  reviewer can read a coach page", r.status_code == 200)
        check("    but sees no rate boxes", 'name="rate_value"' not in r.text)
        check("    and no approve button", "/signoff" not in r.text)
        check("    and is told why", "Admin only" in r.text or "An admin has to" in r.text)
        r = rc.post(f"/commissions/{rid}/coach/{coach0}/signoff",
                    data={"confirm": "on"}, follow_redirects=False)
        check("    sign-off route refuses", r.headers.get("location") == "/dashboard",
              r.headers.get("location", str(r.status_code)))
        r = rc.post(f"/commissions/{rid}/booking/{bid}/rate",
                    data={"rate_type": "flat", "rate_value": "99999"},
                    follow_redirects=False)
        check("    rate route refuses", r.headers.get("location") == "/dashboard",
              r.headers.get("location", str(r.status_code)))
        r = rc.post(f"/commissions/{rid}/booking/{bid}/approve", follow_redirects=False)
        check("    booking approve refuses", r.headers.get("location") == "/dashboard",
              r.headers.get("location", str(r.status_code)))
        r = rc.post(f"/commissions/{rid}/coach/{coach0}/approve-status",
                    data={"status": "Cancelled", "include": "on"},
                    follow_redirects=False)
        check("    bulk approve refuses", r.headers.get("location") == "/dashboard",
              r.headers.get("location", str(r.status_code)))
    _d = _SL()
    n_signed = _d.query(CommissionSignoff).filter_by(run_id=int(rid)).count()
    _b = _d.get(CommissionBooking, int(bid))
    check("    nothing was signed off", n_signed == 0, str(n_signed))
    check("    no rate was written", not _b.rate_manual)
    _d.close()

    print("\ndelegation section")
    r = c.get("/commissions/delegation")
    check("  /commissions/delegation", r.status_code == 200)
    check("    reachable from the commission tabs",
          "/commissions/delegation" in c.get("/commissions").text)
    dids = sorted(set(re.findall(r"/commissions/delegation/(\d+)", r.text)))
    check("    both delegators listed", len(dids) == 2, str(dids))
    check("    leads with margin", ">Margin<" in r.text)

    # the screen must agree with the engine, to the peso
    from app.commission_routes import (delegator_rollup as _roll,      # noqa: E402
                                       delegated_rows as _drows,
                                       schedule_matrix as _matrix)
    from app.models import CommissionRun as _Run                        # noqa: E402
    _d = _SL()
    _runs = [x for x in _d.query(_Run).all() if any(b.delegator_id for b in x.bookings)]
    _run = max(_runs, key=lambda x: x.id)
    _rollup = _roll(_run, _d)
    for _r in _rollup:
        check("    %s shown at %s margin" % (_r["delegator"].name, _r["margin"]),
              "₱{:,.2f}".format(float(_r["margin"])) in r.text)
    _total = sum(float(_r["charged"]) for _r in _rollup)
    check("    totals reconcile with the engine",
          "₱{:,.2f}".format(_total) in r.text, "₱{:,.2f}".format(_total))

    for did in dids:
        for tab in ("sessions", "schedule", "clients", "coaches"):
            rr = c.get(f"/commissions/delegation/{did}?tab={tab}")
            check(f"  delegator {did} · {tab}", rr.status_code == 200)

    big = max(_rollup, key=lambda x: x["sessions"])
    page = c.get("/commissions/delegation/%d?tab=schedule" % big["delegator"].id).text
    _rows = [b for b in _drows(_run) if b.delegator_id == big["delegator"].id]
    m = _matrix(_rows)
    check("  schedule is a whole calendar month",
          page.count('class="d') >= len(m["days"]), "%d days" % len(m["days"]))
    check("    one row per client", page.count('class="cli" title=') == len(m["clients"]))
    check("    every coach has a distinct code",
          len(set(m["codes"].values())) == len(m["codes"]), str(m["codes"]))
    check("    cells add up to the session count",
          sum(cl["total"] for cl in m["clients"]) == big["sessions"])
    check("    day totals add up too", sum(m["totals"].values()) == big["sessions"])
    check("    legend names every coach",
          all(name in page for name in m["codes"]))
    _d.close()

    print("\nphase 4 — finalize")
    r = c.post(f"/commissions/{rid}/finalize", follow_redirects=False)
    check("finalize is blocked until coaches are approved",
          c.get(f"/commissions/{rid}?tab=documents").text.count("/commissions/payouts/") == 0)
    page = c.get(f"/commissions/{rid}").text
    check("  and says who is waiting", "haven't been approved yet" in page)

    for coach in re.findall(r'href="/commissions/%s/coach/([^"?#]+)"' % rid, page) or []:
        pass
    signed = 0
    for coach in sorted(set(re.findall(
            r'/commissions/%s/coach/([^"?#/]+)' % rid,
            c.get(f"/commissions/{rid}?tab=coaches").text))):
        rr = c.post(f"/commissions/{rid}/coach/{coach}/signoff",
                    data={"confirm": "on"}, follow_redirects=False)
        if rr.status_code == 303:
            signed += 1
    check("  approve each coach", signed == 7, "signed %d" % signed)
    page = c.get(f"/commissions/{rid}?tab=coaches").text
    check("  coaches tab shows the sign-off", page.count(">Approved<") >= 7)

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

    print("\ncoach statement links")
    from app.models import (CommissionStatementLink as _Link,       # noqa: E402
                            STATEMENT_LINK_DAYS as _DAYS)
    r = c.get(f"/commissions/{rid}/statements")
    check("  statements screen", r.status_code == 200)
    check("    reachable from a finalized run",
          "/statements" in c.get(f"/commissions/{rid}").text)
    r = c.post(f"/commissions/{rid}/statements/link-all", follow_redirects=False)
    check("  create links for approved coaches", r.status_code == 303)
    page = c.get(f"/commissions/{rid}/statements").text
    toks = re.findall(r"/statement/([A-Za-z0-9_\-]{20,})", page)
    check("    one link per approved coach", len(toks) == 7, "%d links" % len(toks))
    check("    tokens are long and random", all(len(t) >= 30 for t in toks))
    check("    no two coaches share a token", len(set(toks)) == len(toks))

    _d = _SL()
    _ln = _d.query(_Link).filter_by(token=toks[0]).first()
    _span = (_ln.expires_at - _ln.created_at).days
    check("    links expire after %d days" % _DAYS, _span == _DAYS, "%d days" % _span)
    _coach, _rid_of = _ln.coach, _ln.run_id
    _d.close()

    # the public page must work with no session at all
    from fastapi.testclient import TestClient as _TC2             # noqa: E402
    with _TC2(app) as anon:
        rr = anon.get("/statement/" + toks[0])
        check("  a stranger with the link can read it", rr.status_code == 200)
        check("    it is that coach's statement", _coach in rr.text)
        check("    no app navigation on the page", "side-nav" not in rr.text)
        check("    status filters are present", 'id="sfil"' in rr.text)
        check("    non-completed rows are included",
              rr.text.count('data-status="') >= 1)
        check("    search engines are told to stay out", "noindex" in rr.text)
        check("    the same stranger cannot reach the app",
              anon.get("/commissions", follow_redirects=False).status_code == 303)
        check("  an unknown token 404s", anon.get("/statement/nope-nope-nope").status_code == 404)

    # revoking kills it immediately
    r = c.post(f"/commissions/{rid}/statements/link",
               data={"coach": _coach, "action": "revoke"}, follow_redirects=False)
    check("  revoke", r.status_code == 303)
    with _TC2(app) as anon:
        rr = anon.get("/statement/" + toks[0])
        check("    a revoked link stops working", rr.status_code == 410)
        check("    and says why", "turned off" in rr.text)

    # a fresh link replaces the old one
    r = c.post(f"/commissions/{rid}/statements/link",
               data={"coach": _coach}, follow_redirects=False)
    page = c.get(f"/commissions/{rid}/statements").text
    fresh = re.findall(r"/statement/([A-Za-z0-9_\-]{20,})", page)
    check("  a new link is issued", toks[0] not in fresh)
    with _TC2(app) as anon:
        rr = anon.get("/statement/" + toks[0])
        check("    the old URL is dead", rr.status_code == 410)
        check("    and points at the newer one", "newer one was sent" in rr.text)

    print("\nregression — existing pages still render")
    for url in ("/dashboard", "/sales", "/admin/staff", "/coaches/billing",
                "/invoices", "/customers", "/stock", "/admin/reports"):
        rr = c.get(url)
        check(url, rr.status_code == 200, str(rr.status_code))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
