"""Editing the words the system sends out.

Every event email is now a piece of text on a screen rather than a string in a
file, which is the difference between "we'll change that next deploy" and
"we changed it". The engine is in mailtpl.py, the shipped wording and the rules
for each email are in mail_templates.py; this is only the screen.

Two decisions worth stating, because both are load-bearing:

Nothing is copied into the database until somebody edits it. An email with no
saved row renders from the shipped default, so improvements to the wording in a
release still reach a gym that never touched this screen — and "reset to
original" is a delete rather than a copy of a copy.

A placeholder we don't recognise refuses the save. It would otherwise ship as
literal text to thirty people, and ${record.frist_name} in an inbox is the kind
of mistake nobody spots until it has already gone.
"""
from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .db import get_db
from . import event_routes, mail_templates
from .mailtpl import TemplateError, sanitise, unclosed
from .models import EmailTemplate, now_utc


def register(app, deps):
    render = deps["render"]
    require_admin = deps["require_admin"]

    def _rows(db):
        """Every template, with whether somebody has taken it over."""
        saved = {r.key: r for r in db.query(EmailTemplate).all()}
        out = []
        for t in mail_templates.TEMPLATES:
            row = saved.get(t["key"])
            out.append(dict(t, edited=row is not None,
                            edited_at=row.updated_at if row else None,
                            edited_by=(row.updated_by.name
                                       if row and row.updated_by else None)))
        return out

    @app.get("/admin/email-templates", response_class=HTMLResponse)
    def email_templates(request: Request, db: Session = Depends(get_db)):
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        return render(request, "email_templates.html", db, staff,
                      rows=_rows(db))

    def _editor(request, db, staff, key, subject, body, error=None,
                warnings=(), saved=False):
        t = mail_templates.BY_KEY[key]
        palette = [
            ("Values", "escaped, and safe to put anywhere",
             sorted((n, str(v)) for n, v in t["values"].items())),
            ("Blocks", "we build these; drop one in on its own line",
             sorted(t["blocks"].items())),
            ("Wrapped", "put your own words between the two",
             sorted(t["pairs"].items())),
        ]
        return render(request, "email_template.html", db, staff,
                      t=t, key=key, subject=subject, body=body,
                      error=error, warnings=list(warnings), saved=saved,
                      palette=[p for p in palette if p[2]],
                      conditionals=_conditionals(t),
                      is_default=mail_templates.stored(db, key) is None)

    def _conditionals(t) -> list:
        """The values worth offering an ${if} for, and why these ones.

        The shipped wording already wraps the sometimes-empty values — a
        deadline on an event that has none, a hashtag nobody set. Reading them
        back off the default is better than a guess, and better than listing
        all of them: ${if event.name} is a chip nobody will ever want.
        """
        from .mailtpl import tokens
        seen, out = set(), []
        for close, name, args, _whole, _at in tokens(t["body"]):
            if name != "if" or close or not args or args[0] in seen:
                continue
            if args[0] not in t["values"]:
                continue
            seen.add(args[0])
            out.append(args[0])
        return out

    @app.get("/admin/email-templates/{key}", response_class=HTMLResponse)
    def email_template(request: Request, key: str,
                       db: Session = Depends(get_db)):
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        if key not in mail_templates.BY_KEY:
            return RedirectResponse("/admin/email-templates", status_code=303)
        subject, body = mail_templates.source_of(db, key)
        return _editor(request, db, staff, key, subject or "", body or "",
                       saved=request.query_params.get("saved") == "1")

    @app.post("/admin/email-templates/{key}", response_class=HTMLResponse)
    def email_template_save(request: Request, key: str,
                            subject: str = Form(""), body: str = Form(""),
                            db: Session = Depends(get_db)):
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        if key not in mail_templates.BY_KEY:
            return RedirectResponse("/admin/email-templates", status_code=303)
        t = mail_templates.BY_KEY[key]
        subject = (subject or "").strip() or None
        body, removed = sanitise(body or "")

        try:
            mail_templates.validate(key, subject if t["subject"] else None, body)
        except TemplateError as e:
            # Handed straight back with what they typed still in the box —
            # re-typing an email because the save bounced is how a small fix
            # turns into a worse one.
            return _editor(request, db, staff, key, subject or "", body,
                           error=str(e))

        # A render on the sample proves the thing can actually be built. It is
        # cheap, and it is the last moment where a failure costs nobody.
        try:
            event_routes.sample_email(db, key, subject, body)
        except Exception as e:                       # noqa: BLE001 — any of it
            return _editor(request, db, staff, key, subject or "", body,
                           error="We couldn't render that: %s" % e)

        warnings = []
        if removed:
            warnings.append("Took out " + ", ".join(removed)
                            + " — mail clients block those anyway.")
        left = unclosed(body)
        if left:
            warnings.append(
                "%d HTML tag%s look%s unclosed. Mail clients will close them "
                "for you, so this will still send — worth a look though."
                % (left, "" if left == 1 else "s", "s" if left == 1 else ""))

        row = mail_templates.stored(db, key)
        if row is None:
            row = EmailTemplate(key=key)
            db.add(row)
        row.subject = subject if t["subject"] else None
        row.body = body
        row.updated_at = now_utc()
        row.updated_by_id = staff.id
        db.commit()
        return _editor(request, db, staff, key, subject or "", body,
                       warnings=warnings, saved=True)

    @app.post("/admin/email-templates/{key}/reset")
    def email_template_reset(request: Request, key: str,
                             db: Session = Depends(get_db)):
        """Back to the shipped wording — by forgetting theirs, not copying ours."""
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        row = mail_templates.stored(db, key) if key in mail_templates.BY_KEY else None
        if row is not None:
            db.delete(row)
            db.commit()
        return RedirectResponse("/admin/email-templates/%s" % key,
                                status_code=303)

    @app.post("/admin/email-templates/{key}/preview", response_class=HTMLResponse)
    def email_template_preview(request: Request, key: str,
                               subject: str = Form(""), body: str = Form(""),
                               db: Session = Depends(get_db)):
        """The picture beside the box, built from whatever is in the box now."""
        staff, redir = require_admin(request, db)
        if redir:
            return redir
        if key not in mail_templates.BY_KEY:
            return HTMLResponse("", status_code=404)
        body, _removed = sanitise(body or "")
        try:
            mail_templates.validate(
                key, (subject or "").strip() or None
                if mail_templates.BY_KEY[key]["subject"] else None, body)
            _subject, html = event_routes.sample_email(
                db, key, (subject or "").strip() or None, body)
        except Exception as e:                       # noqa: BLE001
            return HTMLResponse(
                '<div style="font:14px/1.5 system-ui;padding:26px;color:#8a2f2f">'
                '<b>Can\'t draw that yet.</b><br>%s</div>'
                % event_routes._esc(str(e)))
        # Content-IDs are what a mail client resolves; a browser needs the
        # routes instead, and it is the same file either way.
        html = html.replace("cid:%s" % event_routes.LOGO_CID,
                            "/static/email-logo.png")
        lender = event_routes.sample_sponsor(db)
        html = html.replace(
            "cid:%s" % event_routes.SPONSOR_CID,
            "/events/%d/sponsor-logo" % lender.id if lender else "")
        html = html.replace("cid:%s" % event_routes.PASS_CID,
                            "/static/email-logo.png")
        return HTMLResponse(html)
