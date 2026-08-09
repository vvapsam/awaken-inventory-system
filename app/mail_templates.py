"""Which emails exist, what each one may say, and what it says by default.

One entry per email. The `body` here is the shipped wording — the thing "reset
to original" goes back to — and it is written in the same language you get in
the editor, so what you see on that screen is genuinely the source, not a
rendering of something else kept elsewhere.

The plain-text half of every message is derived from the rendered HTML rather
than edited separately. Two copies of the same sentence is two copies to keep
in step, and the one nobody looks at is the one that goes stale.
"""
import html
import re

from .mailtpl import TemplateError, check, render, sanitise

#: Values every event email can use, with the example shown in the palette.
EVENT_VALUES = {
    "event.name": "HYROX Foundation Class",
    "event.when": "Sun 9 Aug, 10:00 AM",
    "event.venue": "AWAKEN Gym · Metrowalk, Pasig",
    "event.sponsor": "Kenny Rogers Roasters",
    "event.bring": "Training gear, towel, water",
    "event.perk": "Kenny Rogers meal on us",
    "event.closes": "Fri 21 Aug, 3:00 PM",
    "event.handles": "@awakengymph and @kennyrogersph",
    "event.hashtag": "#FuelledByKennyRogers",
}
PERSON_VALUES = {
    "record.name": "Marc Damil",
    "record.first_name": "Marc",
    "record.email": "marc@example.com",
    "record.link": "https://pay.awakengym.com/e/…",
}
PAY_VALUES = {
    "record.rate": "Member",
    "record.amount": "₱1,500",
}

SHELL_BODY = """<tr><td style="background:#14171a;padding:26px 30px 24px;text-align:center">
  ${block.logo}
  ${block.sponsor}
</td></tr>
<tr><td style="padding:28px 30px 30px">${block.body}</td></tr>
<tr><td style="background:#f3f5f7;padding:18px 30px;text-align:center;font-size:12px;
  color:#6b7683">Questions? Just reply to this email.<br>AWAKEN Fitness Center</td></tr>"""

INVITE_BODY = """<h1 style="font-size:20px;font-weight:650;margin:0 0 14px">You got a slot, ${record.first_name} 🎉</h1>
${block.facts}
<p style="font-size:15px;margin:0 0 16px;color:#2b3642">${event.sponsor} is fuelling us after
   the class. All we ask in return is one Reel.</p>
${block.button "Confirm my slot →" "Takes under a minute"}
${if record.deadline}${block.note}<b>Let us know by ${record.deadline}.</b> We're holding your slot until then, after that it goes to the next person.${/block.note}${/if}
${block.rewards "What you get for sharing"}"""

#: The second ask, and the last one. Deliberately shorter than the invitation:
#: they have already read the long version, and a wall of text on the morning of
#: a deadline gets skimmed past the one sentence that matters.
LASTCALL_BODY = """<h1 style="font-size:20px;font-weight:650;margin:0 0 12px">Last call, ${record.first_name}</h1>
<p style="font-size:15px;margin:0 0 16px;color:#2b3642">Your slot for
   <b>${event.name}</b> is still being held${if event.sponsor} — ${event.sponsor} are
   fuelling us after${/if}. We just need a yes.</p>
${if record.deadline}${block.note}<b>We need to hear from you by ${record.deadline}.</b> After that the slot goes to the next person on the list.${/block.note}${/if}
${block.button "Confirm my slot →" "One tap, and you're done"}
${block.facts}
<p style="font-size:14px;margin:16px 0 0;color:#6b7683">Can't make it after all?
   Use the same link to let us know — it means somebody on the waitlist gets to
   train, and that's genuinely useful to us.</p>"""

PASS_BODY = """<h1 style="font-size:20px;font-weight:650;margin:0 0 14px">You're in, ${record.first_name} ✅</h1>
<p style="font-size:15px;margin:0 0 18px;color:#2b3642">Show this at the door —
   we'll scan it. No need to print anything.</p>
${block.qr}
${block.facts}
${block.note}Can't find this on the day? Your own page carries the same code — <a href="${record.link}" style="color:#008080">open it here</a>.${/block.note}"""

FINISH_BODY = """<h1 style="font-size:20px;font-weight:650;margin:0 0 14px">${record.headline} 👋</h1>
<p style="font-size:15px;margin:0 0 18px;color:#2b3642">${record.lede}</p>
${block.checklist}
${block.button "Pick up where I left off →" "Everything you typed is still there"}
${block.note}<b>Nothing is held for you yet.</b> A slot is only yours once we've checked the payment${if event.closes} — and registration closes ${event.closes}${/if}.${/block.note}"""

RETURNED_BODY = """<h1 style="font-size:20px;font-weight:650;margin:0 0 14px">One more thing, ${record.first_name}</h1>
<p style="font-size:15px;margin:0 0 6px;color:#2b3642">We had a look at your
   payment and need another go at it.</p>
${if record.review_note}${block.note}<b>${record.review_note}</b>${/block.note}${/if}
<p style="font-size:15px;margin:16px 0 0;color:#2b3642">Nothing is lost —
   your details and your place in the queue are exactly where you left them.</p>
${block.button "Pick up where I left off →" "Takes a minute"}"""

#: The one nobody wants to send. Shortest of the lot on purpose: somebody
#: reading "cancelled" has stopped taking in sentences, so it says the thing,
#: says what happens next, and gets out of the way. No button, because there is
#: nothing for them to do — and a call-to-action under a cancellation reads as
#: though the class is still on.
CANCELLED_BODY = """<h1 style="font-size:20px;font-weight:650;margin:0 0 14px">Today\u2019s class is cancelled \u26a0\ufe0f</h1>
<p style="font-size:15px;margin:0 0 16px;color:#2b3642">Today\u2019s <b>${event.name}</b>${if event.sponsor} with ${event.sponsor}${/if} is cancelled due to the weather.</p>
<p style="font-size:15px;margin:0 0 16px;color:#2b3642">We\u2019ll announce the new schedule as soon as it\u2019s confirmed \u2014 you don\u2019t need to do anything, and your slot is safe.</p>
${block.note}Stay safe out there, everyone. See you soon! \U0001f64f\u2614${/block.note}"""

REEL_BODY = """<h1 style="font-size:20px;font-weight:650;margin:0 0 4px">Thank you, ${record.first_name} 🙌</h1>
<p style="color:#6b7683;font-size:14px;margin:0 0 16px">${event.name}</p>
<p style="font-size:15px;margin:0 0 16px;color:#2b3642">You turned up and worked —
   that's the whole reason we run these. And thanks to ${event.sponsor} for fuelling us
   after.</p>
${block.button "Submit my Reel &amp; get my code →" "Takes about 20 seconds"}
${if record.reel_deadline}${block.note}<b>Your window is open until ${record.reel_deadline}.</b> Tag ${event.handles}${if event.hashtag}, use <b>${event.hashtag}</b>${/if} and you're done.${/block.note}${/if}
${block.rewards "Your reward"}"""

#: name, blurb, where it's used, the values it may reference, the blocks it may
#: place, the paired blocks, and the shipped wording.
TEMPLATES = [
    {
        "key": "shell", "name": "The wrapper",
        "blurb": "The black header, the AWAKEN mark, the footer line. "
                 "Wraps every email below.",
        "where": "wraps them all", "subject": None,
        "values": dict(EVENT_VALUES),
        "blocks": {"block.logo": "the AWAKEN mark",
                   "block.sponsor": "“in partnership with …”, when there is one",
                   "block.body": "the email itself — leave this in"},
        "pairs": {},
        "body": SHELL_BODY,
    },
    {
        "key": "invite", "name": "The invitation",
        "blurb": "“You're in — confirm your slot.”",
        "where": "invite events",
        "subject": "You're in — confirm your ${event.name} slot",
        "values": dict(EVENT_VALUES, **PERSON_VALUES,
                       **{"record.deadline": "Tue 4 Aug, 3:00 PM"}),
        "blocks": {"block.facts": "When · Where · Bring · After",
                   "block.button": "the teal call-to-action",
                   "block.rewards": "both offers, with the “or”"},
        "pairs": {"block.note": "the bordered callout"},
        "body": INVITE_BODY,
    },
    {
        "key": "lastcall", "name": "Last call to confirm",
        "blurb": "“We still need a yes, and the deadline is today.”",
        "where": "invite events",
        "subject": "Last call — confirm your ${event.name} slot",
        "values": dict(EVENT_VALUES, **PERSON_VALUES,
                       **{"record.deadline": "Tue 4 Aug, 3:00 PM"}),
        "blocks": {"block.facts": "When · Where · Bring · After",
                   "block.button": "the teal call-to-action",
                   "block.rewards": "both offers, with the “or”"},
        "pairs": {"block.note": "the bordered callout"},
        "body": LASTCALL_BODY,
    },
    {
        "key": "pass", "name": "Your pass",
        "blurb": "Sent the moment somebody confirms. Carries the QR.",
        "where": "every event",
        "subject": "You're in — your pass for ${event.name}",
        "values": dict(EVENT_VALUES, **PERSON_VALUES),
        "blocks": {"block.qr": "their check-in code",
                   "block.facts": "When · Where · Bring · After",
                   "block.button": "the teal call-to-action"},
        "pairs": {"block.note": "the bordered callout"},
        "body": PASS_BODY,
    },
    {
        "key": "finish", "name": "Finish your registration",
        "blurb": "For anyone who started and stopped.",
        "where": "open registration",
        "subject": "Your ${event.name} slot isn't finished yet",
        "values": dict(EVENT_VALUES, **PERSON_VALUES, **PAY_VALUES, **{
            "record.headline": "You started, Marc — but you're not done",
            "record.lede": "We have your details. There are two things left…",
        }),
        "blocks": {"block.checklist": "how far they actually got",
                   "block.button": "the teal call-to-action",
                   "block.facts": "When · Where · Bring · After"},
        "pairs": {"block.note": "the bordered callout"},
        "body": FINISH_BODY,
    },
    {
        "key": "returned", "name": "Ask for a better receipt",
        "blurb": "Carries your reason for sending one back.",
        "where": "open registration",
        "subject": "One more thing about your ${event.name} registration",
        "values": dict(EVENT_VALUES, **PERSON_VALUES, **PAY_VALUES, **{
            "record.review_note": "The photo is too dark to read the amount.",
        }),
        "blocks": {"block.button": "the teal call-to-action"},
        "pairs": {"block.note": "the bordered callout"},
        "body": RETURNED_BODY,
    },
    {
        "key": "cancelled", "name": "Called off",
        "blurb": "\u201cToday\u2019s class is cancelled.\u201d Weather, or anything else.",
        "where": "every event",
        "subject": "${event.name}${if event.sponsor} x ${event.sponsor}${/if} "
                   "Update \u26a0\ufe0f",
        "values": dict(EVENT_VALUES, **PERSON_VALUES),
        "blocks": {"block.facts": "When \u00b7 Where \u00b7 Bring \u00b7 After"},
        "pairs": {"block.note": "the bordered callout"},
        "body": CANCELLED_BODY,
    },
    {
        "key": "reel", "name": "The Reel email",
        "blurb": "“Thank you — submit your Reel and pick your reward.”",
        "where": "every event",
        "subject": "Thank you — here's your reward link",
        "values": dict(EVENT_VALUES, **PERSON_VALUES, **{
            "record.reel_deadline": "Tue 11 Aug, 10:00 AM",
        }),
        "blocks": {"block.button": "the teal call-to-action",
                   "block.rewards": "both offers, with the “or”",
                   "block.facts": "When · Where · Bring · After"},
        "pairs": {"block.note": "the bordered callout"},
        "body": REEL_BODY,
    },
]

BY_KEY = {t["key"]: t for t in TEMPLATES}


def spec_of(key) -> dict:
    t = BY_KEY[key]
    return {"values": set(t["values"]), "blocks": set(t["blocks"]),
            "pairs": set(t["pairs"])}


def validate(key, subject, body) -> list:
    """Refuse what we can't render; return advisory warnings for the rest."""
    spec = spec_of(key)
    if subject is not None:
        check(subject, {"values": spec["values"], "blocks": set(), "pairs": set()})
    check(body, spec)
    return []


_TAG = re.compile(r"<[^>]+>")
_BLANK = re.compile(r"\n{3,}")
#: A row or a list item ends a line once, on the way out — matching the opening
#: tag too would put a blank line between every pair of facts.
_ROW = re.compile(r"(?i)<\s*/\s*(tr|li)\s*>")
#: These genuinely separate blocks of prose, so either end of one is a break.
_BREAK = re.compile(r"(?i)<\s*/?\s*(br|p|div|h1|h2|h3|table|ul)\b[^>]*>")
#: A cell boundary is a gap, not a line — "When" and the date belong together.
_CELL = re.compile(r"(?i)<\s*/\s*t[dh]\s*>")
_LINK = re.compile(r'(?i)<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
#: The two ticks carry meaning a reader would otherwise lose, so they become
#: words rather than characters. Everything else is left to html.unescape,
#: which knows the whole list and doesn't leave &#x27; sitting in a sentence.
_MARKS = (("&#10003;", "[x]"), ("&#9675;", "[ ]"), ("\u2192", ""))


def to_text(html_body: str) -> str:
    """The plain-text half, made from the HTML so the two can never disagree.

    Not a tag-stripper: a stripper runs the table cells together and glues a
    button's label to its own URL. This keeps the line breaks a reader would
    see, so the text half reads like something written rather than something
    scraped.
    """
    s = html_body or ""
    # Entities and arrows first, so a label is already clean by the time it
    # becomes "label: url" and doesn't end up as "Confirm my slot : http…".
    for a, b in _MARKS:
        s = s.replace(a, b)
    # The URL is the whole point of the plain-text half — it is what a
    # text-only client has instead of a button.
    s = _LINK.sub(lambda m: "\n%s: %s\n" % (_TAG.sub("", m.group(2)).strip(),
                                             m.group(1)), s)
    s = _CELL.sub(" ", s)
    s = _ROW.sub("\n", s)
    s = _BREAK.sub("\n", s)
    s = _TAG.sub("", s)
    # Entities last: unescaping earlier would turn a written-out &lt;b&gt; into
    # a tag and the tag-stripper would then eat it.
    s = html.unescape(s)
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in s.splitlines()]
    return _BLANK.sub("\n\n", "\n".join(lines)).strip()


def stored(db, key):
    """The row somebody has saved for this email, or None."""
    from .models import EmailTemplate
    return db.query(EmailTemplate).filter(EmailTemplate.key == key).first()


def source_of(db, key) -> tuple:
    """(subject, body) — theirs if they've taken it over, ours if not."""
    t = BY_KEY[key]
    row = stored(db, key)
    if row is None:
        return t["subject"], t["body"]
    return (row.subject if row.subject is not None else t["subject"],
            row.body if row.body is not None else t["body"])


def build(db, key, values, blocks, pairs, src=None) -> tuple:
    """Render one email. Returns (subject, text, html-body).

    `src` overrides what is stored, and exists for the editor: the preview
    beside the box has to show what you have typed, not what you last saved.
    It is the same renderer either way, so a preview cannot flatter a template
    that would come out differently when it actually goes.
    """
    subject_src, body_src = src if src is not None else source_of(db, key)
    spec = spec_of(key)
    subject = render(subject_src or "", spec, values, {}, {}) or None
    body = render(body_src or "", spec, values, blocks, pairs)
    return subject, to_text(body), body
