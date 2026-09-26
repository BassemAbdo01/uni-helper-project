"""A plan has to show what it is FOR.

The page rendered the week as a flat list of blocks -- "SYSC2004, 50 min",
over and over -- which says what to do in the next hour and nothing about
what it builds to, how much is already behind you, or whether it is enough.
The owner's words: "doesn't make a proper study plan, just a list of what to
do for 50 mins for each thing".

Two things answer that, and these pin both:

  * the runway -- per deadline, hours done against hours still booked, and
    how long there is
  * one colour per course, the SAME colour the calendar paints it

The trap that actually bit, in the browser, one screenshot after the runway
first rendered: `_major_assessment_label(...) or kind in _EXAM_KINDS` put an
EXAM tag on "Midterm review session", because the planner files a revision
block under an exam kind -- that is what it is revision FOR. The `or` on the
kind re-flagged exactly the titles `_NOT_AN_EXAM` exists to exclude.
"""
import os
import sys
from datetime import timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402


@pytest.fixture()
def planned(client):
    """A student with a midterm, a revision block for it, and ordinary work."""
    email = f"plan{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "Rana Test", "email": email, "password": "plan-pass-123"})
    with nexora.app.app_context():
        uid = nexora.User.query.filter_by(email=email).first().id
        today = nexora._student_today(uid)
        for code, title, offset in [("BIOL1103", "Midterm 1", 6),
                                    ("BIOL1103", "Midterm review session", 4),
                                    ("SYSC2004", "Lab Report 2", 5)]:
            nexora.db.session.add(nexora.Task(
                id=uuid4().hex, user_id=uid, course_full=code, title=title, base_title=title,
                kind="due", due_raw=(today + timedelta(days=offset)).isoformat(),
                group_id=uuid4().hex, is_deadline=True, source="manual"))

        def session(code, title, kind, days, state, actual=None, start=600, end=650):
            nexora.db.session.add(nexora.StudySession(
                id=uuid4().hex, user_id=uid, on_date=(today + timedelta(days=days)).isoformat(),
                start_min=start, end_min=end, course_full=code, title=title,
                item_kind=kind, state=state, actual_minutes=actual))

        session("BIOL1103", "Midterm 1", "exam", 1, "planned")            # 50 booked
        session("BIOL1103", "Midterm 1", "exam", 2, "planned")            # 50 booked
        session("BIOL1103", "Midterm 1", "exam", -1, "done", actual=90)   # 90 done
        session("BIOL1103", "Midterm review session", "exam", 1, "planned")
        session("SYSC2004", "Lab Report 2", "assignment", 1, "planned")
        session("BIOL1103", "Midterm 1", "exam", -2, "done", actual=0)    # no duration: not evidence
        nexora.db.session.commit()
    return uid


def runway_for(uid):
    with nexora.app.app_context():
        today = nexora._student_today(uid)
        sessions = nexora.StudySession.query.filter_by(user_id=uid).all()
        colours = nexora._student_course_colours(uid)
        rows = nexora._plan_runway(uid, sessions, today, colours)
    return {r["title"]: r for r in rows}


def test_the_runway_counts_hours_done_against_hours_booked(planned):
    """The number a student actually wants: how much of this is behind me."""
    item = runway_for(planned)["Midterm 1"]
    assert item["done"] == 90, "only actual_minutes count"
    assert item["planned"] == 100, "two 50-minute blocks still to come"
    assert item["total"] == 190
    assert item["pct"] == 47


def test_a_block_marked_done_with_no_duration_is_not_evidence(planned):
    """The same rule `_learned_effort` follows: recording a guess as a fact
    teaches the estimator its own guess back for ever."""
    assert runway_for(planned)["Midterm 1"]["done"] == 90     # not 90 + 0-minute row


def test_the_midterm_is_labelled_and_the_revision_for_it_is_not(planned):
    """The bug this file exists for. Both rows are filed under an exam kind by
    the planner, because the revision IS for the exam -- so a fallback on the
    kind re-flagged the title _NOT_AN_EXAM had just excluded."""
    rows = runway_for(planned)
    assert rows["Midterm 1"]["label"] == "MIDTERM"
    assert rows["Midterm 1"]["major"] is True
    assert rows["Midterm review session"]["label"] == ""
    assert rows["Midterm review session"]["major"] is False


def test_ordinary_work_appears_but_below_the_exam(planned):
    with nexora.app.app_context():
        today = nexora._student_today(planned)
        rows = nexora._plan_runway(planned, nexora.StudySession.query.filter_by(user_id=planned).all(),
                                   today, nexora._student_course_colours(planned))
    titles = [r["title"] for r in rows]
    assert "Lab Report 2" in titles
    # Lab Report 2 is due in 5 days, Midterm 1 in 6 -- but an exam outranks
    # ordinary work only on the SAME day, so the sooner one still leads.
    assert titles.index("Lab Report 2") < titles.index("Midterm 1")


def test_a_course_is_the_same_colour_on_the_calendar_and_the_plan(client, planned):
    """One definition of the FUNCTION was not enough: the two pages built the
    map from different inputs, so SYSC2004 came out blue on one and green on
    the other. Caught by reading both pages in the browser."""
    with nexora.app.app_context():
        colours = nexora._student_course_colours(planned)
    cal = client.get("/calendar").get_data(as_text=True)
    plan = client.get("/study-plan").get_data(as_text=True)
    for code, idx in colours.items():
        if code in cal and code in plan:
            assert f"--c{idx}" in cal and f"--c{idx}" in plan


def test_the_plan_can_be_seen_as_a_week():
    """"Just a list" was the complaint. The week lays the study blocks over the
    classes they were fitted around, which is the thing being shown."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "study_plan.html")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "function renderWeek" in src
    assert "occursOn" in src, "a fortnightly lab must not be drawn on its off week"
    assert "#planCard{--c0" in src or ",#planCard{--c0" in src, (
        "the colour tokens must be defined ON the plan card -- they were on a "
        "`.plan` class it does not carry, so every block fell back to amber")
