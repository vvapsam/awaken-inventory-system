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
from .models import EventParticipant, to_local

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

#: The event card's own shapes. Square and story match the PFT card above so
#: an export from either drops into the same slot; landscape is the square's
#: width on the story's height, which is the one a screen at the venue wants.
EVENT_SHAPES = {
    "square": {"key": "square", "title": "Square", "note": "1920 x 1920",
               "cw": 1920, "ch": 1920, "type": 1.00},
    "story": {"key": "story", "title": "Story", "note": "1080 x 1920",
              "cw": 1080, "ch": 1920, "type": 1.00},
    #: Type is pegged to the short side or a 1920-wide card sets a name
    #: taller than the room beneath it. The nudge back up is because 1080 of
    #: type on a 1920 canvas reads small beside a lockup half the width.
    "landscape": {"key": "landscape", "title": "Landscape",
                  "note": "1920 x 1080", "cw": 1920, "ch": 1080,
                  "type": 1.16},
}

#: How wide the HYBRID KIDS bar runs, as a share of the card. One number
#: drives the whole lockup: the wordmark sits at 80% of the bar, which is
#: roughly how the posters stack the two.
LOGO_PCT = 42
#: static/card/awaken_word.png, 1108 x 229. Held here rather than measured at
#: request time: it is a fixed asset, and opening a PNG to draw a card would
#: be a file read on every view to learn a number that never changes.
WORDMARK_AR = 4.8384


def fit_time(text: str, want: int, avail: int) -> int:
    """A type size that keeps the time inside the card.

    "24:18" and "1:04:09" are not the same number of characters, and a card
    that runs its own time off the edge is worse than one with a smaller time
    on it. The digits are tabular, so a width per character is arithmetic
    rather than a guess.
    """
    em = 0.0
    for ch in str(text or ""):
        em += 0.30 if ch == ":" else 0.26 if ch == "." else 0.56
    return int(min(want, avail / max(0.8, em)))


def event_card_context(ev, shape_key: str, clock: str,
                       chrome: int = 0) -> dict:
    """Every number the event card is drawn from, at native size.

    Worked out here rather than in the template for the same reason the PFT
    card does it: these are design decisions, and a template full of
    arithmetic is a template nobody can tell is wrong.
    """
    shape = EVENT_SHAPES.get(shape_key) or EVENT_SHAPES[DEFAULT_SHAPE]
    c = dict(shape)
    w, h = c["cw"], c["ch"]
    base = min(w, h) * c["type"]
    bar_w = round(w * (LOGO_PCT / 100.0))
    logo_w = round(bar_w * 0.80)
    bar_h = round(bar_w * 0.148)
    title, badge = ev.card_words if ev is not None else ("", "")
    c.update({
        "chrome": chrome,
        "pad": round(w * 0.044 + base * 0.030),
        "barw": bar_w, "barh": bar_h,
        "logow": logo_w, "logoh": round(logo_w / WORDMARK_AR),
        "bargap": round(bar_h * 0.34),
        "hkfs": round(bar_h * 0.62),
        "badgew": round(bar_h * 1.08),
        "name": round(base * 0.050),
        "label": round(base * 0.0232),
        "labelgap": round(base * 0.020),
        "timegap": round(base * 0.011),
        "foot": round(base * 0.0204),
        "title": title, "badge": badge,
    })
    c["time"] = fit_time(clock, round(base * 0.207),
                         w - c["pad"] * 2)
    return c




def mmss(secs, blank: str = "--:--") -> str:
    """A finish time the way it is said out loud.

    ``blank`` is what no time at all reads as. The PFT card keeps the dashes —
    it is only ever drawn for somebody who finished. The event card is opened
    before the race to check a name, so there it reads 0:00 rather than a row
    of punctuation nobody can screenshot with a straight face.
    """
    if secs is None:
        return blank
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

    def _shapes_for(ev):
        return EVENT_SHAPES if (ev is not None
                                and ev.card_style == "event") else SHAPES

    def _shape(request, ev=None) -> str:
        want = (request.query_params.get("shape") or "").strip().lower()
        table = _shapes_for(ev)
        return want if want in table else DEFAULT_SHAPE

    def _foot_text(ev) -> str:
        """"Finisher · 2026" — the year off the event, not off today.

        A card opened in January for a race held in December should not
        quietly relabel itself.
        """
        when = to_local(ev.starts_at) if (ev and ev.starts_at) else None
        return "Finisher · %d" % when.year if when else "Finisher"

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
        ev = who.event
        event_style = ev is not None and ev.card_style == "event"
        # The PFT card is only ever drawn for a finisher: it has a patch and a
        # sponsor wall on it, and "--:--" under all that is not a card anybody
        # should be able to screenshot. The event card is different — it is
        # opened the week before to check a name reads right, so it draws with
        # the clock at zero and says so.
        if not who.finished_at and not event_style:
            return RedirectResponse("/patch/p/%d/result" % pid,
                                    status_code=303)
        shape = _shape(request, ev)
        clock = mmss(who.race_seconds, blank="0:00") if event_style \
            else mmss(who.race_seconds)
        card = (event_card_context(ev, shape, clock, chrome=330)
                if event_style else card_context(shape, chrome=330))
        if event_style:
            card["foot_text"] = _foot_text(ev)
        return render(
            request, "card_staff.html", db, staff, active="events",
            who=who, ev=ev, shape=shape, shapes=_shapes_for(ev),
            card=card, event_style=event_style,
            raced=bool(who.finished_at),
            clock=clock, share=_share_url(request, who))

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
        ev = who.event
        event_style = ev is not None and ev.card_style == "event"
        shape = _shape(request, ev)
        clock = mmss(who.race_seconds)
        card = (event_card_context(ev, shape, clock, chrome=110)
                if event_style else card_context(shape, chrome=110))
        if event_style:
            card["foot_text"] = _foot_text(ev)
        return templates.TemplateResponse("card_public.html", {
            "request": request, "who": who.full_name,
            "clock": clock, "card": card, "shape": shape,
            "event_style": event_style,
        })
