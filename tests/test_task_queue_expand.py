"""The queue can be opened without ticking anything off.

Reported with a screenshot: four items due tomorrow, one in three days, and no
way to tell whether more were hiding behind them -- *"maybe theres 7 due in 1
day but they wouldnt see it until they mark a few as done, and when clicking
All tasks it should show them ALL tasks"*.

Two separate defects:

1. The queue rendered **twelve** cards and showed five. A student with eleven
   things due tomorrow could only read past the fifth by destroying the list.
2. **"All tasks" already widened the server filter** -- `view_filter(cards,
   "all")` returns every non-archived card -- and then the page showed the
   same five, because the cap is in the template. The link changed the data
   and nothing a student could see.

Visibility is asserted on the rendered `hidden` attribute here, and on
computed `display` in the browser. CLAUDE.md records a version of this feature
that shipped with all twelve painted while a test checking `el.hidden` passed.
"""
import re
from datetime import date, timedelta
from uuid import uuid4

import app as nexora


def _account(client):
    email = f"tq{uuid4().hex[:7]}@carleton.ca"
    client.post("/signup", data={"name": "Q", "email": email, "password": "nexorademo1"})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


def _tasks(user_id, count, days=1, prefix="Assignment"):
    due = (date.today() + timedelta(days=days)).isoformat()
    with nexora.app.app_context():
        for i in range(1, count + 1):
            title = f"{prefix} {i}"
            nexora.db.session.add(nexora.Task(
                id=uuid4().hex, user_id=user_id, title=title, base_title=title,
                group_id=nexora.make_group_id("CCDP2100C", title), due_raw=due,
                course_full="CCDP2100C", course_prefix="CCDP", kind="due",
                is_deadline=True, source="manual", sync_state="active",
                created_at="2026-10-01 00:00:00"))
        nexora.db.session.commit()


def _panel(body):
    return body.split('id="tasks"', 1)[1].split("</section>", 1)[0]


def _counts(body):
    """(rendered, hidden) for the queue cards ONLY.

    Counted with a regex bound to the task div. A bare `" hidden>"` search
    over the panel also matches the toast button and the done list, which is
    how the first version of this helper reported eight hidden cards out of
    eleven when six were.
    """
    panel = _panel(body)
    cards = re.findall(r'<div class="task"[^>]*>', panel)
    return len(cards), sum(1 for c in cards if c.endswith(" hidden>"))


# --------------------------------------------------------------------------
# Everything is rendered, five are shown
# --------------------------------------------------------------------------

def test_every_task_reaches_the_page_not_just_twelve(client):
    """Twenty due tomorrow. All twenty must be in the DOM, or the toggle has
    nothing to reveal and the student is back to ticking things off to read.

    Deliberately past the OLD cap of twelve: a fixture of eleven would render
    fully under both the old code and the new, and pin nothing."""
    uid = _account(client)
    _tasks(uid, 20)
    rendered, _ = _counts(client.get("/dashboard").get_data(as_text=True))
    assert rendered == 20, f"only {rendered} of 20 reached the page"


def test_only_five_start_visible(client):
    """The queue is still a queue. Rendering all of it must not paint all of
    it -- that is the complaint this feature answers, in reverse."""
    uid = _account(client)
    _tasks(uid, 11)
    rendered, hidden = _counts(client.get("/dashboard").get_data(as_text=True))
    assert rendered == 11
    assert hidden == 6, f"six of the eleven should start hidden, {hidden} were"


def test_the_toggle_appears_and_counts_what_is_there(client):
    uid = _account(client)
    _tasks(uid, 11)
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'id="queueMore"' in body
    assert "Show all 11" in body


def test_no_toggle_when_everything_already_fits(client):
    """A control that opens a list already fully on screen is noise."""
    uid = _account(client)
    _tasks(uid, 4)
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'id="queueMore"' not in body


def test_no_toggle_with_exactly_five(client):
    uid = _account(client)
    _tasks(uid, 5)
    assert 'id="queueMore"' not in client.get("/dashboard").get_data(as_text=True)


# --------------------------------------------------------------------------
# "All tasks" means all tasks
# --------------------------------------------------------------------------

def test_the_all_tasks_link_arrives_expanded(client):
    """It widened the server filter and showed the same five. The link said
    "All tasks" and delivered five, every time."""
    uid = _account(client)
    _tasks(uid, 11)
    body = client.get("/dashboard?view=all").get_data(as_text=True)
    rendered, hidden = _counts(body)
    assert rendered == 11
    assert hidden == 0, "arriving from All tasks must show all of them"
    assert "Show fewer" in _panel(body)


def test_the_ordinary_dashboard_is_still_collapsed(client):
    """Expanding by default everywhere would replace one complaint with
    another: a dashboard that opens on thirty rows is not a queue."""
    uid = _account(client)
    _tasks(uid, 11)
    _, hidden = _counts(client.get("/dashboard").get_data(as_text=True))
    assert hidden == 6


def test_the_queue_already_carried_every_task_in_every_view(client):
    """The queue is built with `view_filter(..., "all")` HARDCODED at
    app.py:4866 -- it has never respected `?view=`. So "All tasks" never
    widened the task list; its only effect was expanding the COURSES list.

    The five-card cap was the whole bug. Pinned because the obvious fix --
    making the queue honour `view` -- would NARROW it, which is the opposite
    of what was asked for."""
    uid = _account(client)
    _tasks(uid, 4, days=1, prefix="Soon")
    _tasks(uid, 4, days=21, prefix="Later")
    default, _ = _counts(client.get("/dashboard").get_data(as_text=True))
    every, _ = _counts(client.get("/dashboard?view=all").get_data(as_text=True))
    week, _ = _counts(client.get("/dashboard?view=week").get_data(as_text=True))
    assert default == every == week == 8, (default, every, week)
    assert "Later 1" in _panel(client.get("/dashboard").get_data(as_text=True)),         "a deadline three weeks out belongs in the queue, in every view"


def test_all_tasks_changes_how_much_is_shown_not_what_is_there(client):
    """Which is the only thing left for that link to mean."""
    uid = _account(client)
    _tasks(uid, 11)
    plain, plain_hidden = _counts(client.get("/dashboard").get_data(as_text=True))
    opened, opened_hidden = _counts(client.get("/dashboard?view=all").get_data(as_text=True))
    assert plain == opened == 11, "the set is identical"
    assert plain_hidden == 6 and opened_hidden == 0, "only the display differs"


# --------------------------------------------------------------------------
# The cap is loud
# --------------------------------------------------------------------------

def test_a_cap_that_bites_says_so(client, monkeypatch):
    """A limit a student cannot see is indistinguishable from a bug -- the
    rule COURSE_IMPORT_MAX already follows."""
    uid = _account(client)
    _tasks(uid, 9)
    monkeypatch.setattr(nexora, "TASK_PREVIEW_MAX", 4)
    body = client.get("/dashboard").get_data(as_text=True)
    assert "Showing 4 of 9" in body


def test_an_uncapped_list_says_nothing(client):
    uid = _account(client)
    _tasks(uid, 9)
    assert "Showing 9 of" not in client.get("/dashboard").get_data(as_text=True)


def test_the_cap_is_high_enough_for_a_real_term():
    """Twelve was the old number and it is what caused the report. A term with
    six courses and a dozen deadlines each is past it before December."""
    assert nexora.TASK_PREVIEW_MAX >= 100


# --------------------------------------------------------------------------
# The hiding has to be real
# --------------------------------------------------------------------------

def test_the_hidden_rule_still_outranks_the_display_rule():
    """`.task{display:grid}` beats a bare `[hidden]`, so the first version of
    this queue shipped with every card painted while a test that checked the
    attribute passed. The same trap applies to the new toggle, which is also
    toggled with `el.hidden`."""
    import io
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    css = io.open(os.path.join(here, "templates", "index.html"), encoding="utf-8").read()
    assert ".task[hidden]{display:none}" in css
    assert ".qmore[hidden]{display:none}" in css
    # Order is NOT what saves this. `.task[hidden]` is (0,2,0) against
    # `.task`'s (0,1,0), so specificity wins wherever the rule sits -- the
    # first version of this test asserted an ordering and failed on a comment
    # that quotes the rule. What matters is that an attribute-qualified rule
    # exists for every element toggled with `el.hidden`.
