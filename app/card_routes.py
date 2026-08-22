"""The finisher card — the athlete's time, as something they can keep.

Registered from main.py via ``register(app, deps)``, like the other feature
modules, so this file never imports main.

The awarding table already knows who finished and in what time. This turns that
same record into a card: the two marks, the P'F"T badge, the name, the time and
the sponsors. Staff reach it from the result screen once the time is revealed;
the athlete reaches it on their own phone through a link.

Three decisions worth knowing:

* **No new columns.** The card reads ``full_name`` and ``race_seconds`` off the
  participant, and the share link reuses the ``token`` every participant
  already has. A feature that needs no migration is a feature that cannot break
  a deploy.
* **One card, two shapes.** Square for a feed, tall for a story. Both are the
  same markup at a different size (see ``SHAPES``) rather than two templates,
  so a change to one cannot silently miss the other.
* **No image is generated.** The card is a web page. Nothing on the server
  turns HTML into a PNG — no headless browser in the container, no memory cost
  on every deploy. The trade is that saving it is a screenshot, and the
  sponsors sit where a careless crop loses them.
"""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .auth import current_staff
from .db import get_db
from .models import EventParticipant

#: The ten sponsor marks, in the order they run on the card, with their native
#: pixel widths. They were cut out of the single strip artwork so they can be
#: re-flowed: a square card fits five to a row, a tall one only four.
SPONSORS = [
    ("logo00.png", 119), ("logo01.png", 108), ("logo02.png", 87),
    ("logo03.png", 87), ("logo04.png", 95), ("logo05.png", 69),
    ("logo06.png", 91), ("logo07.png", 85), ("logo08.png", 103),
    ("logo09.png", 129),
]

#: How the card is drawn at each shape. Everything is a plain pixel number at
#: native size; the template scales the whole card with one transform, so these
#: are design decisions rather than anything the browser has to negotiate.
#:
#: ``sponsor`` is the scale each logo is drawn at, and ``rows`` groups them —
#: five and five across a square, four/three/three down a tall one, because
#: five of these logos across 1080 px would put Gardenia and Barrio Fiesta
#: below the size at which anybody can read them.
SHAPES = {
    "square": {
        "key": "square", "title": "Square", "note": "1920 x 1920",
        "cw": 1920, "ch": 1920,
        "top": 96, "headgap": 48, "markgap": 56,
        "awaken": 300, "hyrox": 240, "rule": 150, "badge": 1300,
        "name": 110, "labelgap": 52, "label": 72, "timegap": 32, "time": 600,
        "midpad": 0,
        "sponsor": 2.15, "spgap": 66, "rowgap": 46, "bottom": 104,
        "groups": [[0, 1, 2, 3, 4], [5, 6, 7, 8, 9]],
    },
    "story": {
        "key": "story", "title": "Story", "note": "1080 x 1920",
        "cw": 1080, "ch": 1920,
        "top": 80, "headgap": 44, "markgap": 56,
        "awaken": 300, "hyrox": 240, "rule": 150, "badge": 980,
        "name": 86, "labelgap": 46, "label": 56, "timegap": 30, "time": 400,
        "midpad": 10,
        "sponsor": 2.05, "spgap": 54, "rowgap": 40, "bottom": 74,
        "groups": [[0, 1, 2, 3], [4, 5, 6], [7, 8, 9]],
    },
}
DEFAULT_SHAPE = "square"


def mmss(secs) -> str:
    """A finish time the way it is said out loud."""
    if secs is None:
        return "--:--"
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def card_context(shape_key: str, chrome: int = 0) -> dict:
    """The shape, with its sponsor rows already sized.

    ``chrome`` is how much vertical room the surrounding page needs; the
    template subtracts it before working out how far the card can scale.
    """
    shape = SHAPES.get(shape_key) or SHAPES[DEFAULT_SHAPE]
    c = dict(shape)
    c["chrome"] = chrome
    c["rows"] = [
        [{"file": SPONSORS[i][0], "w": round(SPONSORS[i][1] * c["sponsor"])}
         for i in group]
        for group in c["groups"]
    ]
    return c


def register(app, deps):
    render = deps["render"]
    templates = deps["templates"]

    def _staff(request, db):
        staff = current_staff(request, db)
        if not staff:
            return None, RedirectResponse("/login", status_code=303)
        return staff, None

    def _shape(request) -> str:
        want = (request.query_params.get("shape") or "").strip().lower()
        return want if want in SHAPES else DEFAULT_SHAPE

    def _share_url(request, who) -> str:
        return str(request.base_url).rstrip("/") + "/c/" + who.token

    # ---------------------------------------------------------------- staff

    @app.get("/patch/p/{pid}/card", response_class=HTMLResponse)
    def card_for_staff(request: Request, pid: int,
                       db: Session = Depends(get_db)):
        """The card, inside the admin, reached from the result screen."""
        staff, redir = _staff(request, db)
        if redir:
            return redir
        who = db.get(EventParticipant, pid)
        if not who:
            return RedirectResponse("/patch", status_code=303)
        # Somebody who has not finished has no time to put on a card. Send
        # them back rather than drawing "--:--" and letting it be screenshotted.
        if not who.finished_at:
            return RedirectResponse("/patch/p/%d/result" % pid,
                                    status_code=303)
        shape = _shape(request)
        return render(
            request, "card_staff.html", db, staff, active="events",
            who=who, ev=who.event, shape=shape, shapes=SHAPES,
            card=card_context(shape, chrome=330),
            clock=mmss(who.race_seconds),
            share=_share_url(request, who))

    # --------------------------------------------------------------- public

    @app.get("/c/{token}", response_class=HTMLResponse)
    def card_public(request: Request, token: str,
                    db: Session = Depends(get_db)):
        """The athlete's own copy. No login, unguessable address."""
        who = db.query(EventParticipant).filter(
            EventParticipant.token == token).first()
        if not who or not who.finished_at:
            return HTMLResponse(
                "<!doctype html><meta charset='utf-8'>"
                "<title>Not found</title>"
                "<body style='font:16px system-ui;padding:40px'>"
                "<p>That link is not a finisher card.</p>", status_code=404)
        shape = _shape(request)
        return templates.TemplateResponse("card_public.html", {
            "request": request, "who": who.full_name,
            "clock": mmss(who.race_seconds),
            "card": card_context(shape, chrome=110),
            "shape": shape,
        })
