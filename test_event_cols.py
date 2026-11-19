"""A question on the form, shown as a column on the roster.

The gym asks things the system has no field for — "Parent's Name" on a kids'
class being the one that started this. The answer was already stored a row at
a time and already readable, but only by opening one person's drawer at a
time, which is not how you check twenty-one of them the week of the event.

So the column chooser offers the form's own questions alongside the fixed
columns. Nothing new is collected; this is about what is shown.

What has to hold together:
  - a question somebody wrote is offered, and the built-ins are not, because
    those are already columns or are the name itself;
  - a section is not offered, because it asks nothing;
  - every one of them is off until it is ticked — a nine-question form must
    not arrive as nine new columns;
  - ticking one saves it, draws the header and draws each person's answer;
  - the header count and the body count stay equal, or the table is skewed
    and the edit row underneath spans the wrong width.
"""
import os, re
os.environ["DATABASE_URL"] = ("postgresql+psycopg2://postgres@/evcols"
                              "?host=/home/claude/pgrun&port=5433")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_INITIAL_PIN", "123456")
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import Base, engine
from app import models as M
from app.event_routes import columns_for, question_columns, visible_cols
from fastapi.testclient import TestClient
from app.main import app

res = []
def ck(n, c):
    res.append((n, bool(c)))
    print("PASS" if c else "FAIL", n)

with engine.begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

with TestClient(app) as c:
    c.post("/login", data={"username": "admin", "pin": "123456"})

    # ── a kids' class: the child is the participant, the parent is a question ──
    with Session(engine) as db:
        ev = M.Event(name="Hybrid Kids", slug="hybrid-kids",
                     mode=M.EVENT_OPEN, capacity=50)
        db.add(ev); db.flush()
        rate = M.EventRate(event_id=ev.id, label="Mini Hero", amount=1500,
                           capacity=25, position=0)
        db.add(rate)
        parent = M.EventQuestion(event_id=ev.id, title="Parent's Name",
                                 kind="text", required=True, position=0)
        shirt = M.EventQuestion(event_id=ev.id, title="Shirt size",
                                kind="choice", options="S\nM\nL", position=1)
        # Not questions: a built-in stands for a field the page already draws,
        # a section asks nothing, and a hidden one is switched off.
        builtin = M.EventQuestion(event_id=ev.id, title="Participant's Name",
                                  kind="text", builtin="name", position=2)
        section = M.EventQuestion(event_id=ev.id, title="Page two",
                                  kind="section", position=3)
        off = M.EventQuestion(event_id=ev.id, title="Old question",
                              kind="text", hidden=True, position=4)
        db.add_all([parent, shirt, builtin, section, off]); db.flush()

        kid = M.EventParticipant(event_id=ev.id, name="Alfonso Gabriel Forbes",
                                 email="francesqforbes@gmail.com",
                                 token="tok-kid-1")
        hand = M.EventParticipant(event_id=ev.id, name="Added By Hand",
                                  email="hand@example.com", token="tok-kid-2")
        db.add_all([kid, hand]); db.flush()
        db.add(M.ParticipantAnswer(participant_id=kid.id, question_id=parent.id,
                                   value="Francesca Forbes"))
        db.add(M.ParticipantAnswer(participant_id=kid.id, question_id=shirt.id,
                                   value="M"))
        db.commit()
        EV, PARENT_Q, SHIRT_Q, KID = ev.id, parent.id, shirt.id, kid.id

    # ── what is offered ──
    with Session(engine) as db:
        ev = db.get(M.Event, EV)
        keys = [k for k, _l, _q in question_columns(ev)]
        ck("the gym's own questions are offered",
           keys == ["q%d" % PARENT_Q, "q%d" % SHIRT_Q])
        labels = {k: l for k, l, _q in question_columns(ev)}
        ck("offered under the question's own title",
           labels["q%d" % PARENT_Q] == "Parent's Name")

        offered = {k for k, _l, _on in columns_for(ev, "people")}
        ck("a built-in is not offered twice", "q%d" % (SHIRT_Q + 1) not in offered)
        ck("a section is not offered", not any(
            k for k, l, _on in columns_for(ev, "people") if l == "Page two"))
        ck("a hidden question is not offered", not any(
            k for k, l, _on in columns_for(ev, "people") if l == "Old question"))

        on_now = {k for k, _l, on in columns_for(ev, "people") if on}
        ck("off until somebody ticks it",
           "q%d" % PARENT_Q not in on_now and "q%d" % SHIRT_Q not in on_now)

        # Can't make it draws no question columns, so it must not offer any:
        # a tick box that does nothing is the chooser lying.
        ck("not offered on Can't make it",
           not [k for k, _l, _on in columns_for(ev, "gone") if k.startswith("q")])

    def roster(html):
        """Just the participant table — not the whole page.

        The edit drawer under each row already lists every answer, so a test
        that searched the page would pass before the column existed.
        """
        m = re.search(r'<table id="ppl">.*?</table>', html, re.S)
        return m.group(0) if m else ""

    # ── before it is ticked ──
    page = c.get("/events/%d" % EV).text
    ck("the answer is not on the roster yet",
       "Francesca Forbes" not in roster(page))
    ck("it is in the drawer all the same", "Francesca Forbes" in page)
    ck("and it is offered in the chooser", 'value="q%d"' % PARENT_Q in page)

    # ── tick it the way the page does ──
    with Session(engine) as db:
        ev = db.get(M.Event, EV)
        default_on = [k for k, _l, on in columns_for(ev, "people") if on]
    r = c.post("/events/%d/columns" % EV,
               data={"scope": "people", "col": default_on + ["q%d" % PARENT_Q]},
               follow_redirects=False)
    ck("saving the choice redirects back", r.status_code == 303)

    page = c.get("/events/%d" % EV).text
    # Autoescaped, so the apostrophe is &#39; in the markup — asserting the
    # raw title here would fail on a column that is drawing perfectly.
    ck("the header carries the question's title",
       ">Parent&#39;s Name</th>" in page)
    ck("the child's name is still the participant column",
       "Alfonso Gabriel Forbes" in page)
    ck("and the parent now reads across the row",
       "Francesca Forbes" in roster(page))
    ck("the one not ticked stays off", ">Shirt size</th>" not in page)

    # ── the table is not skewed ──
    # The row actions column has no header text, so a header count that
    # matches the body count is the whole check: one <th> per <td>.
    head = re.search(r'<table id="ppl">.*?<thead>(.*?)</thead>', page, re.S)
    body = re.search(r'<table id="ppl">.*?<tbody>(.*?)</tbody>', page, re.S)
    ths = len(re.findall(r"<th[ >]", head.group(1))) if head else -1
    first_row = re.split(r"</tr>", body.group(1))[0] if body else ""
    tds = len(re.findall(r"<td[ >]", first_row))
    ck("a header cell for every body cell (%d/%d)" % (ths, tds), ths == tds)

    # ── somebody added by hand never filled the form in ──
    row = next((x for x in re.split(r"</tr>", body.group(1))
                if "Added By Hand" in x), "")
    ck("no answer reads as a dash, not an error", "&mdash;" in row or "—" in row)

    # ── renaming the question keeps the column on ──
    with Session(engine) as db:
        q = db.get(M.EventQuestion, PARENT_Q)
        q.title = "Parent or guardian"
        db.commit()
    page = c.get("/events/%d" % EV).text
    ck("the column survives a rename", ">Parent or guardian</th>" in page
       and "Francesca Forbes" in page)

    # ── and the reset puts it back to the standard set ──
    c.post("/events/%d/columns/reset" % EV, data={"scope": "people"},
           follow_redirects=False)
    page = c.get("/events/%d" % EV).text
    ck("reset drops it again", ">Parent or guardian</th>" not in page)

bad = [n for n, ok in res if not ok]
print("\n%d/%d passed" % (len(res) - len(bad), len(res)))
if bad:
    print("FAILED: " + "; ".join(bad))
