"""Event planning — the weeks before there is an event.

A corporate enquiry arrives as a conversation, not a booking. Somebody wants a
Mini HYROX for a hundred and twenty staff, and what follows is scope, two budget
options, an equipment list, a staffing plan and a run sheet, argued over with a
person on the client's side who has no login here and never will.

That pack used to be a file. Files get emailed, edited on both sides at once and
lose the argument about which copy is current. So it lives here instead: one
document per plan, saved as you type, with one external link the client opens.

Three decisions shape everything below.

**The pack is one JSON document, not fifteen tables.** Nothing in it is
reported on or joined to — it is read and written whole, by one person at a
time. The two numbers worth asking about across plans, the headcount and the
chosen total, are lifted into their own columns so the list screen never opens
the document. See the note on EventPlan in models.py.

**The client can read and comment, and that is all.** They can pin a note to any
row — a budget line, a checklist task, a station — and they cannot move a
number. Comments are a conversation you can answer; a silently edited budget is
one you find out about at invoice time.

**The template is copied, never referenced.** A new plan deep-copies the seed,
so editing San Miguel's equipment list cannot reach into anybody else's.
"""
import json
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .auth import hash_pin, verify_pin
from .db import get_db
from . import plan_seed
from .models import EventPlan, EventPlanComment, PLAN_LINK_DAYS

#: Suggested when a link is first made, and shown on screen so it can be
#: changed before it is sent. A shared default across every client would be one
#: forwarded email away from being everybody's password.
DEFAULT_PASS = "Plan2026@"

#: A comment longer than this is a document, and belongs in an email.
COMMENT_MAX = 4000
NAME_MAX = 80


def register(app, deps):
    render = deps["render"]
    require = deps["require"]
    require_admin = deps["require_admin"]
    templates = deps["templates"]

    # ---------------------------------------------------------------- gate --
    #
    # Same two guards as the sponsor roster, for the same two failures: the
    # token stops the page being found, the password stops a forwarded link
    # working for whoever it was forwarded to. Neither is enough alone.

    _fails: dict = {}
    MAX_TRIES = 8
    COOLDOWN = timedelta(minutes=10)

    def _blocked(token, now):
        n, since = _fails.get(token, (0, now))
        if now - since > COOLDOWN:
            _fails.pop(token, None)
            return None
        return COOLDOWN - (now - since) if n >= MAX_TRIES else None

    def _failed(token, now):
        n, since = _fails.get(token, (0, now))
        if now - since > COOLDOWN:
            n, since = 0, now
        _fails[token] = (n + 1, since)

    def _unlocked(request, token):
        return token in (request.session.get("plan_ok") or [])

    def _unlock(request, token):
        have = list(request.session.get("plan_ok") or [])
        if token not in have:
            have.append(token)
            request.session["plan_ok"] = have[-8:]

    # ------------------------------------------------------------- helpers --

    def _plan(db, pid):
        return db.get(EventPlan, pid)

    def _by_token(db, token):
        return db.query(EventPlan).filter(EventPlan.token == token).first()

    def _doc(plan) -> dict:
        """The stored pack, with anything a newer template added filled in."""
        try:
            saved = json.loads(plan.data or "{}")
        except ValueError:
            # A corrupt document is still a plan somebody is standing in front
            # of. Give them the template rather than a stack trace; the row is
            # left alone so nothing is destroyed by looking at it.
            saved = {}
        return plan_seed.hydrate(saved)

    def _num(v, fallback=None):
        try:
            return float(v)
        except (TypeError, ValueError):
            return fallback

    def _totals(doc):
        """The chosen option's total, computed the way the page computes it.

        Duplicated from the browser on purpose. The list screen and the
        Overview both want this number without loading the whole pack into a
        page first, and a number that only exists in JavaScript is a number
        that does not exist when you are looking at a list of plans.
        """
        b = doc.get("budget") or {}
        opt = (doc.get("selected") or "B").upper()
        o = opt.lower()
        must = sum((_num(r.get("u" + o), 0) or 0) * (_num(r.get("c" + o), 0) or 0)
                   for r in (b.get("must") or []))
        add = sum((_num(r.get("u" + o), 0) or 0) * (_num(r.get("c" + o), 0) or 0)
                  for r in (b.get("addon") or []) if r.get("on"))
        base = must + add
        cont = base * (_num(b.get("cont" + opt), 0) or 0) / 100.0
        return opt, must, add, base + cont

    def _cache(plan, doc):
        """Keep the two lifted columns honest with the document."""
        opt, _must, _add, total = _totals(doc)
        plan.chosen_option = opt
        try:
            plan.chosen_total = Decimal(str(round(total, 2)))
        except (InvalidOperation, ValueError):
            plan.chosen_total = None
        head = _num(doc.get("head"))
        plan.headcount = int(head) if head and head > 0 else None

    def _progress(doc):
        """Done/total for the two things that have a progress bar."""
        tasks = [r for r in (doc.get("checklist") or []) if not r.get("phase")]
        items = [r for r in (doc.get("equip") or []) if not r.get("grp")]
        # Not "items": a dict already has .items, and Jinja resolves the
        # method before the key — which renders as <built-in method items…>
        # on the page rather than failing anywhere you would notice.
        return {
            "tasks_done": sum(1 for r in tasks if r.get("s") == 2),
            "tasks_total": len(tasks),
            "kit_got": sum(1 for r in items if r.get("got")),
            "kit_total": len(items),
        }

    def _list_rows(db, archived=False):
        q = db.query(EventPlan)
        q = (q.filter(EventPlan.archived_at.isnot(None)) if archived
             else q.filter(EventPlan.archived_at.is_(None)))
        rows = q.order_by(EventPlan.updated_at.desc()).all()
        out = []
        for p in rows:
            doc = _doc(p)
            out.append({"p": p, **_progress(doc),
                        "open_comments": len(p.open_comments)})
        return out

    def _base(request):
        return str(request.base_url).rstrip("/")

    def _pack_ctx(plan, *, readonly, author=""):
        # No `request` in here — render() already passes one positionally, and
        # a second copy in the kwargs is a TypeError rather than a warning.
        doc = _doc(plan)
        opt, must, add, total = _totals(doc)
        return {
            "plan": plan,
            "doc_json": json.dumps(doc, ensure_ascii=False),
            "readonly": readonly,
            "author": author,
            "comments": [_comment_json(c) for c in plan.comments],
            "comments_json": json.dumps(
                [_comment_json(c) for c in plan.comments], ensure_ascii=False),
            "chosen": opt,
            "total": total,
            **_progress(doc),
        }

    def _comment_json(c):
        return {
            "id": c.id, "anchor": c.anchor or "", "label": c.anchor_label or "",
            "author": c.author or "Someone", "body": c.body or "",
            "staff": bool(c.from_staff),
            "at": (c.created_at.isoformat() if c.created_at else ""),
            "resolved": bool(c.resolved_at),
        }

    def _gone(request, reason, code=404):
        return templates.TemplateResponse(
            "plan_gone.html", {"request": request, "reason": reason},
            status_code=code)

    # ------------------------------------------------------------- screens --

    @app.get("/planning", response_class=HTMLResponse)
    def planning_list(request: Request, db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return redir
        return render(request, "plans.html", db, staff,
                      rows=_list_rows(db),
                      archived=_list_rows(db, archived=True),
                      template_name=plan_seed.TEMPLATE_NAME,
                      template_blurb=plan_seed.TEMPLATE_BLURB)

    @app.post("/planning/new")
    def planning_new(request: Request, name: str = Form(""),
                     client: str = Form(""), db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return redir
        doc = plan_seed.blank()
        plan = EventPlan(
            name=(name or "").strip() or plan_seed.TEMPLATE_NAME,
            client=(client or "").strip() or None,
            data=json.dumps(doc, ensure_ascii=False),
            created_by_id=staff.id, updated_by_id=staff.id)
        _cache(plan, doc)
        db.add(plan)
        db.commit()
        return RedirectResponse("/planning/%d" % plan.id, status_code=303)

    @app.get("/planning/{pid}", response_class=HTMLResponse)
    def planning_open(request: Request, pid: int,
                      db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return redir
        plan = _plan(db, pid)
        if not plan:
            return RedirectResponse("/planning", status_code=303)
        return render(request, "plan_pack.html", db, staff,
                      link_url=("%s/p/%s" % (_base(request), plan.token)
                                if plan.token else ""),
                      default_pass=DEFAULT_PASS,
                      **_pack_ctx(plan, readonly=False,
                                  author=staff.name or "AWAKEN"))

    @app.post("/planning/{pid}/save")
    async def planning_save(request: Request, pid: int,
                            db: Session = Depends(get_db)):
        """Autosave. Returns JSON because nothing on screen should move."""
        staff, redir = require(request, db)
        if redir:
            # A session that expired mid-edit must not look like a save. The
            # page keeps the text it is holding and says so.
            return JSONResponse({"ok": False, "reason": "signed out"},
                                status_code=401)
        plan = _plan(db, pid)
        if not plan:
            return JSONResponse({"ok": False, "reason": "gone"},
                                status_code=404)
        try:
            body = await request.json()
        except Exception:                                    # noqa: BLE001
            return JSONResponse({"ok": False, "reason": "bad json"},
                                status_code=400)
        doc = plan_seed.hydrate(body.get("doc") if isinstance(body, dict)
                                else None)
        plan.data = json.dumps(doc, ensure_ascii=False)
        plan.updated_by_id = staff.id
        plan.updated_at = datetime.now(timezone.utc)
        _cache(plan, doc)
        db.commit()
        return JSONResponse({"ok": True, "at": plan.updated_at.isoformat(),
                             "total": float(plan.chosen_total or 0)})

    @app.post("/planning/{pid}/rename")
    def planning_rename(request: Request, pid: int, name: str = Form(""),
                        client: str = Form(""),
                        db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return redir
        plan = _plan(db, pid)
        if plan:
            plan.name = (name or "").strip() or plan.name
            plan.client = (client or "").strip() or None
            plan.updated_by_id = staff.id
            db.commit()
        return RedirectResponse("/planning/%d" % pid, status_code=303)

    @app.post("/planning/{pid}/archive")
    def planning_archive(request: Request, pid: int, back: str = Form(""),
                         db: Session = Depends(get_db)):
        """Filed away, not deleted — and reversible from the same screen."""
        staff, redir = require(request, db)
        if redir:
            return redir
        plan = _plan(db, pid)
        if plan:
            plan.archived_at = (None if plan.archived_at
                                else datetime.now(timezone.utc))
            db.commit()
        return RedirectResponse(back or "/planning", status_code=303)

    @app.post("/planning/{pid}/duplicate")
    def planning_duplicate(request: Request, pid: int,
                           db: Session = Depends(get_db)):
        """The last client's pack as the next client's starting point.

        More useful than the shipped template once you have run one of these,
        because the numbers in it are numbers you actually paid.
        """
        staff, redir = require(request, db)
        if redir:
            return redir
        src = _plan(db, pid)
        if not src:
            return RedirectResponse("/planning", status_code=303)
        doc = _doc(src)
        copy = EventPlan(name=src.name + " (copy)", client=None,
                         data=json.dumps(doc, ensure_ascii=False),
                         created_by_id=staff.id, updated_by_id=staff.id)
        _cache(copy, doc)
        db.add(copy)
        db.commit()
        return RedirectResponse("/planning/%d" % copy.id, status_code=303)

    # ---------------------------------------------------------- the link ----

    @app.post("/planning/{pid}/link")
    def planning_link(request: Request, pid: int, password: str = Form(""),
                      db: Session = Depends(get_db)):
        """Mint or replace the external link.

        Replacing rotates the token as well as the password. A client who has
        been sent a link and then removed from the project should stop being
        able to read the budget, and only a new address does that.
        """
        staff, redir = require(request, db)
        if redir:
            return redir
        plan = _plan(db, pid)
        if not plan:
            return RedirectResponse("/planning", status_code=303)
        pw = (password or "").strip() or DEFAULT_PASS
        now = datetime.now(timezone.utc)
        h, s = hash_pin(pw)
        plan.token = secrets.token_urlsafe(24)
        plan.pass_hash, plan.pass_salt = h, s
        plan.link_made_at = now
        plan.expires_at = now + timedelta(days=PLAN_LINK_DAYS)
        plan.revoked_at = None
        plan.opens = 0
        plan.first_opened_at = plan.last_opened_at = None
        db.commit()
        return RedirectResponse("/planning/%d?link=new" % pid, status_code=303)

    @app.post("/planning/{pid}/link/revoke")
    def planning_link_revoke(request: Request, pid: int,
                             db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return redir
        plan = _plan(db, pid)
        if plan and plan.token:
            plan.revoked_at = datetime.now(timezone.utc)
            db.commit()
        return RedirectResponse("/planning/%d" % pid, status_code=303)

    # ----------------------------------------------------------- comments ---

    def _add_comment(db, plan, *, anchor, label, author, body, from_staff):
        body = (body or "").strip()[:COMMENT_MAX]
        if not body:
            return None
        c = EventPlanComment(
            plan_id=plan.id, anchor=(anchor or "")[:200],
            anchor_label=(label or "")[:200],
            author=(author or "").strip()[:NAME_MAX] or (
                "AWAKEN" if from_staff else "Guest"),
            body=body, from_staff=from_staff)
        db.add(c)
        db.commit()
        return c

    @app.post("/planning/{pid}/comment")
    def planning_comment(request: Request, pid: int, anchor: str = Form(""),
                         label: str = Form(""), body: str = Form(""),
                         db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return JSONResponse({"ok": False}, status_code=401)
        plan = _plan(db, pid)
        if not plan:
            return JSONResponse({"ok": False}, status_code=404)
        c = _add_comment(db, plan, anchor=anchor, label=label,
                         author=staff.name or "AWAKEN", body=body,
                         from_staff=True)
        return JSONResponse({"ok": bool(c),
                             "comment": _comment_json(c) if c else None})

    @app.post("/planning/comments/{cid}/resolve")
    def planning_comment_resolve(request: Request, cid: int,
                                 db: Session = Depends(get_db)):
        staff, redir = require(request, db)
        if redir:
            return JSONResponse({"ok": False}, status_code=401)
        c = db.get(EventPlanComment, cid)
        if not c:
            return JSONResponse({"ok": False}, status_code=404)
        if c.resolved_at:
            c.resolved_at, c.resolved_by_id = None, None
        else:
            c.resolved_at, c.resolved_by_id = (datetime.now(timezone.utc),
                                               staff.id)
        db.commit()
        return JSONResponse({"ok": True, "resolved": bool(c.resolved_at)})

    # -------------------------------------------------------- the outside ---

    @app.get("/p/{token}", response_class=HTMLResponse)
    def plan_public(request: Request, token: str,
                    db: Session = Depends(get_db)):
        plan = _by_token(db, token)
        if not plan:
            return _gone(request, "unknown")
        if plan.revoked_at:
            return _gone(request, "revoked", 410)
        if plan.is_expired:
            return _gone(request, "expired", 410)
        if not _unlocked(request, token):
            return templates.TemplateResponse(
                "plan_gate.html",
                {"request": request, "plan": plan, "token": token,
                 "wait": _blocked(token, datetime.now(timezone.utc))})
        now = datetime.now(timezone.utc)
        plan.opens = (plan.opens or 0) + 1
        plan.first_opened_at = plan.first_opened_at or now
        plan.last_opened_at = now
        db.commit()
        who = (request.session.get("plan_who") or {}).get(token, "")
        return templates.TemplateResponse(
            "plan_pack.html",
            {"request": request,
             **_pack_ctx(plan, readonly=True, author=who),
             "token": token, "link_url": "", "default_pass": ""})

    @app.post("/p/{token}")
    def plan_unlock(request: Request, token: str, password: str = Form(""),
                    db: Session = Depends(get_db)):
        plan = _by_token(db, token)
        if not plan:
            return _gone(request, "unknown")
        if plan.revoked_at:
            return _gone(request, "revoked", 410)
        if plan.is_expired:
            return _gone(request, "expired", 410)
        now = datetime.now(timezone.utc)
        wait = _blocked(token, now)
        if wait is None and plan.pass_hash and verify_pin(
                password or "", plan.pass_hash, plan.pass_salt or ""):
            _unlock(request, token)
            return RedirectResponse("/p/%s" % token, status_code=303)
        if wait is None:
            _failed(token, now)
        return templates.TemplateResponse(
            "plan_gate.html",
            {"request": request, "plan": plan, "token": token,
             "bad": wait is None, "wait": _blocked(token, now)},
            status_code=401)

    @app.post("/p/{token}/comment")
    def plan_public_comment(request: Request, token: str,
                            anchor: str = Form(""), label: str = Form(""),
                            author: str = Form(""), body: str = Form(""),
                            db: Session = Depends(get_db)):
        """The one thing the outside can write. Everything else is read-only.

        Guarded by the same unlock as the page: a token on its own cannot post
        a comment, or the address alone would be a way to write to the plan.
        """
        plan = _by_token(db, token)
        if not plan or not plan.is_live or not _unlocked(request, token):
            return JSONResponse({"ok": False}, status_code=403)
        who = dict(request.session.get("plan_who") or {})
        name = (author or "").strip()[:NAME_MAX]
        if name:
            # They type their name once, and the page stops asking.
            who[token] = name
            request.session["plan_who"] = who
        else:
            # A second note in the same session is from the same person. Losing
            # the name here is how a thread ends up half signed and half
            # "Guest", which reads like two people disagreeing with you.
            name = who.get(token, "")
        c = _add_comment(db, plan, anchor=anchor, label=label,
                         author=name, body=body, from_staff=False)
        return JSONResponse({"ok": bool(c),
                             "comment": _comment_json(c) if c else None})
