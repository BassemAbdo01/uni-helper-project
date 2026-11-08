"""B5: the calendar hides the past.

Three defects, all of them "the calendar is wrong about what is behind you":

1. `dleft < -7` dropped anything more than a week past, so paging back to
   October in December drew thirty-one empty squares around a term's work.
2. Completed deadlines never appeared on the calendar at all -- ticking one
   off removed it from the month, which reads as the record being lost rather
   than the work being finished.
3. The route measured from `date.today()` -- the server's date, UTC on Render
   -- for the month it opens on, `is_today` and `today_iso`, while every entry
   inside the grid was already on the student's clock through `days_until`.

A date written into a fixture is a measurement of when it was written (see
CLAUDE.md), so every date here is anchored to the student's own today.
"""

import os
import re
import sys
from datetime import timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture()
def student(client):
    email = f"cal{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": PASSWORD})
    assert client.post("/api/timezone",
                       json={"timezone": "America/Toronto"}).status_code == 200
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        return user.id, nexora._student_today(user)


def add(client, title, day, kind="due", course="SYSC2004"):
    res = client.post("/api/events", json={
        "title": title, "course": course, "date": day.isoformat(), "kind": kind})
    assert res.status_code in (200, 201), res.get_data(as_text=True)
    return res.get_json()["event"]


def month_page(client, day):
    page = client.get(f"/calendar?month={day.strftime('%Y-%m')}")
    assert page.status_code == 200
    return page.get_data(as_text=True)


def entry(html, title):
    """The one `<button class="ev...">` whose title is `title`, or ""."""
    for block in re.findall(r'<button type="button" class="ev[^>]*>.*?</button>',
                            html, re.S):
        if f">{title}<" in block:
            return block
    return ""


# --------------------------------------------------------------- the past

def test_work_from_two_months_ago_is_still_on_its_month(client, student):
    """`dleft < -7` emptied every month before this one."""
    _, today = student
    long_past = today - timedelta(days=62)
    add(client, "Assignment 1", long_past)
    assert entry(month_page(client, long_past), "Assignment 1"), \
        "a deadline two months old was dropped from its own month"


def test_the_past_is_muted_and_the_present_is_not(client, student):
    _, today = student
    past, soon = today - timedelta(days=30), today + timedelta(days=5)
    add(client, "Old Quiz", past)
    add(client, "New Quiz", soon)

    html = month_page(client, past)
    assert "past" in entry(html, "Old Quiz")
    html = month_page(client, soon)
    assert "past" not in entry(html, "New Quiz")


def test_a_final_that_has_been_sat_stops_lighting_its_day(client, student):
    """`hasfinal` is the loudest thing the grid can say about a day. A December
    that has been and gone must not shout it."""
    _, today = student
    past = today - timedelta(days=30)
    add(client, "SYSC2004 Final Exam", past, kind="exam")
    html = month_page(client, past)
    # The entry is still drawn, and still badged.
    assert 'data-major="FINAL"' in entry(html, "SYSC2004 Final Exam")
    # The day cell is not.
    cell = re.search(r'<div class="day[^"]*" data-date="%s"' % past.isoformat(), html)
    assert cell, html[:400]
    assert "hasfinal" not in cell.group(0)
    assert "hasmajor" not in cell.group(0)


def test_a_final_still_to_come_does_light_its_day(client, student):
    """The other half. Without this the test above would pass on a grid that
    never marks anything."""
    _, today = student
    ahead = today + timedelta(days=12)
    add(client, "SYSC2004 Final Exam", ahead, kind="exam")
    cell = re.search(r'<div class="day[^"]*" data-date="%s"' % ahead.isoformat(),
                     month_page(client, ahead))
    assert cell and "hasfinal" in cell.group(0) and "hasmajor" in cell.group(0)


# ---------------------------------------------------------- completed work

def test_a_completed_deadline_stays_on_its_day(client, student):
    """Ticking it off used to delete it from the calendar. A student asking
    "did I hand that in?" is asking about a DAY, and this is the only screen
    that answers in days."""
    _, today = student
    day = today - timedelta(days=10)
    made = add(client, "Lab Report 3", day)
    group = made.get("group_id") or ""
    assert group, made
    assert client.post(f"/api/tasks/{group}/complete",
                       json={"completed": True}).status_code == 200

    block = entry(month_page(client, day), "Lab Report 3")
    assert block, "a completed deadline vanished from its month"
    assert "done" in block


def test_a_completed_entry_is_never_labelled_overdue(client, student):
    """It is not overdue; it is finished. `status_for` answers from the date
    alone and would have said Overdue on a thing that was handed in."""
    _, today = student
    day = today - timedelta(days=10)
    made = add(client, "Lab Report 4", day)
    client.post(f"/api/tasks/{made['group_id']}/complete", json={"completed": True})

    block = entry(month_page(client, day), "Lab Report 4")
    assert "Done" in block
    assert "Overdue" not in block


def test_a_completed_exam_does_not_light_its_day_either(client, student):
    _, today = student
    ahead = today + timedelta(days=9)
    made = add(client, "SYSC2004 Midterm", ahead, kind="exam")
    client.post(f"/api/tasks/{made['group_id']}/complete", json={"completed": True})
    cell = re.search(r'<div class="day[^"]*" data-date="%s"' % ahead.isoformat(),
                     month_page(client, ahead))
    assert cell and "hasmajor" not in cell.group(0)


def test_completing_something_does_not_duplicate_it(client, student):
    """The route builds two card lists and concatenates them. A row that
    appeared in both would draw the same deadline twice on one day."""
    _, today = student
    day = today + timedelta(days=4)
    made = add(client, "Tutorial 2", day)
    client.post(f"/api/tasks/{made['group_id']}/complete", json={"completed": True})
    html = month_page(client, day)
    assert html.count(">Tutorial 2<") == 1, html.count(">Tutorial 2<")


# ------------------------------------------------- the student's own clock

def test_the_grid_marks_today_on_the_students_clock_not_the_servers(client):
    """Two zones 25 hours apart, so they are on different calendar days at
    every instant of the year -- the same construction
    `test_a_countdown_runs_on_the_students_clock` uses, and for the same
    reason: a test that compares the two clocks "now" passes by accident
    until the evening."""
    seen = {}
    for zone in ("Pacific/Kiritimati", "Pacific/Niue"):      # UTC+14 and UTC-11
        email = f"z{uuid4().hex[:6]}@carleton.ca"
        client.post("/signup", data={"name": "S", "email": email, "password": PASSWORD})
        assert client.post("/api/timezone", json={"timezone": zone}).status_code == 200
        with nexora.app.app_context():
            user = nexora.User.query.filter_by(email=email).first()
            expected = nexora._student_today(user)
        html = client.get("/calendar").get_data(as_text=True)
        marked = re.findall(r'<div class="day today[^"]*" data-date="([^"]+)"', html)
        # The default month is the student's month, so their today is in it.
        assert marked == [expected.isoformat()], (zone, marked, expected)
        seen[zone] = marked[0]
        client.get("/logout")
    assert seen["Pacific/Kiritimati"] != seen["Pacific/Niue"], seen
