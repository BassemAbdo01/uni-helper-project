"""The stat row and the queue beneath it count the same list.

Reported as **"0 due this week"** printed directly above five items due in one
and three days. The stats were computed from `cards`, which `view_filter` has
already cut to the current view -- and the default view is "today", which keeps
only `dleft <= 0`. Everything due tomorrow or later was therefore removed
BEFORE the week was counted, so `soon` and `panic` were always empty and the
week could only ever equal the urgent count.

Same class as "37 courses in the extension, 11 on the dashboard": two numbers
taken from two different lists on one screen, and the one a student believes is
the one that is wrong.

Dates are relative to the day the suite runs. Nexora deliberately stops
carrying work far enough in the past, so a fixture written "today" quietly
stops being rendered once the real date moves -- a fixture dated in the past is
a test with a timer on it.
"""

import os
import re
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="stats@carleton.ca"):
    return client.post("/signup", data={"name": "Stats Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


@pytest.fixture()
def student(client):
    signup(client)
    return client


def in_days(n):
    return (date.today() + timedelta(days=n)).isoformat()


def add(client, title, days, kind="due", course="SYSC2004"):
    res = client.post("/api/events", json={"title": title, "date": in_days(days),
                                           "kind": kind, "course": course})
    assert res.status_code in (200, 201), res.get_data(as_text=True)
    return res


def dashboard(client):
    res = client.get("/dashboard")
    assert res.status_code == 200
    return res.get_data(as_text=True)


def week_stat(html):
    """The number printed as "N due this week"."""
    found = re.search(r">\s*(\d+)\s+due this week\s*<", html)
    assert found, "the 'due this week' stat is no longer on the page"
    return int(found.group(1))


def queue_titles(html):
    """What the queue beneath the stats actually lists."""
    return re.findall(r'<div class="task" data-group="[^"]*" data-status="([^"]*)"', html)


# --------------------------------------------------------------- the bug

def test_work_due_later_this_week_is_counted(student):
    """The reported case: nothing overdue, nothing due today, five things due
    within the week, and the stat said zero."""
    for i, days in enumerate((1, 1, 3, 3, 5), start=1):
        add(student, "Assignment %d" % i, days)

    assert week_stat(dashboard(student)) == 5


def test_the_stat_agrees_with_the_queue_below_it(student):
    """The actual defect is not the number, it is the disagreement."""
    add(student, "Due tomorrow", 1)
    add(student, "Due in three", 3)
    add(student, "Due in six", 6)

    html = dashboard(student)
    in_the_week = [s for s in queue_titles(html) if s in ("today", "panic", "soon")]
    assert week_stat(html) == len(in_the_week) == 3


# ------------------------------------------------- what the window means

def test_work_further_out_than_a_week_is_not_in_it(student):
    add(student, "Due in three", 3)
    add(student, "Due in twenty", 20)
    assert week_stat(dashboard(student)) == 1


def test_due_today_counts(student):
    add(student, "Due today", 0)
    assert week_stat(dashboard(student)) == 1


def test_overdue_is_not_counted_as_due_this_week(student):
    """It has its own cell, and work that was due last week is not due this
    one. Counting it in both makes the number disagree with its own label."""
    add(student, "Was due", -2)
    add(student, "Due in two", 2)

    html = dashboard(student)
    assert week_stat(html) == 1
    assert re.search(r'<div class="v">1</div>', html), "the overdue cell still reports it"


def test_the_seventh_day_is_in_and_the_eighth_is_out(student):
    add(student, "Day seven", 7)
    assert week_stat(dashboard(student)) == 1

    add(student, "Day eight", 8)
    assert week_stat(dashboard(student)) == 1


def test_nothing_due_really_does_say_zero(student):
    add(student, "Due in twenty", 20)
    assert week_stat(dashboard(student)) == 0
