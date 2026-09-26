"""A subscribed calendar is only useful while a glance at it is worth taking.

The feed sent everything, and that was the only option. On a real term that
is unusable: one September day carried ten items -- "Lab Plagiarism Quiz",
"Submission Folder", "Lab 1 Quiz - Available", "Modules 1 and 2 Quiz" -- and
the two dates a student actually keeps a calendar for were somewhere in the
middle of them. Reported with a screenshot of a Google Calendar month with no
white space left in it.

Nexora is where you go to see everything. A personal calendar is a different
surface with a different job, so the feed now has a scope, and the narrow one
is the default: the option that floods a calendar should be the one you
choose, not the one you get.
"""
import os
import sys
from datetime import timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

# Titles taken from the reported screenshot, plus the near-misses that make
# this hard: a review session and a practice paper are not the sitting.
ITEMS = [
    ("BIOL1103", "Midterm Examination", True),
    ("SYSC2004", "Midterm 1", True),
    ("SYSC2310", "Unit 2 Test", True),
    ("BIOL1103", "Final Exam", True),
    ("BIOL1103", "Lab Plagiarism Quiz", False),
    ("BIOL1103", "Lab 1 Quiz - Available", False),
    ("SYSC2310", "Pre-course Quiz_2026", False),
    ("ELEC2501B", "Submission Folder", False),
    ("SYSC2004", "Midterm review session", False),
    ("SYSC2310", "Practice Final (ungraded)", False),
    ("CCDP2100C", "Assignment #1 Group", False),
]


@pytest.fixture()
def student(client):
    email = f"feed{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "Rana Test", "email": email, "password": "feed-pass-123"})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        today = nexora._student_today(user.id)
        for i, (code, title, _major) in enumerate(ITEMS):
            nexora.db.session.add(nexora.Task(
                id=uuid4().hex, user_id=user.id, course_full=code, title=title, base_title=title,
                kind="due", due_raw=(today + timedelta(days=3 + i)).isoformat(),
                due_time="23:59", group_id=uuid4().hex, is_deadline=True, source="manual"))
        nexora.db.session.commit()
        uid = user.id
    token = client.get("/api/calendar/feed").get_json()["url"].rsplit("/", 1)[-1].replace(".ics", "")
    return uid, token


def titles_in(body):
    return [line.split("SUMMARY:", 1)[1].strip()
            for line in body.splitlines() if line.startswith("SUMMARY:")]


def test_everything_is_still_available(client, student):
    """Existing subscribers keep the feed they subscribed to."""
    body = client.get(f"/calendar/feed/{student[1]}.ics").get_data(as_text=True)
    assert len(titles_in(body)) == len(ITEMS)


def test_exams_only_sends_the_dates_a_calendar_is_kept_for(client, student):
    body = client.get(f"/calendar/feed/{student[1]}.ics?only=exams").get_data(as_text=True)
    got = titles_in(body)
    assert len(got) == 4
    for code, title, major in ITEMS:
        present = any(title in g for g in got)
        assert present is major, f"{title!r} should {'be' if major else 'not be'} in the exam feed"


def test_a_review_session_is_not_the_sitting(client, student):
    """The whole difference between a useful filter and a broken one."""
    body = client.get(f"/calendar/feed/{student[1]}.ics?only=exams").get_data(as_text=True)
    joined = " ".join(titles_in(body))
    assert "review session" not in joined
    assert "Practice Final" not in joined


def test_the_two_calendars_are_named_differently(client, student):
    """A student may end up with both subscribed, and has to be able to tell
    which is which in Google's sidebar -- not least to unsubscribe one."""
    everything = client.get(f"/calendar/feed/{student[1]}.ics").get_data(as_text=True)
    exams = client.get(f"/calendar/feed/{student[1]}.ics?only=exams").get_data(as_text=True)
    assert "X-WR-CALNAME:Nexora" in everything
    assert "X-WR-CALNAME:Nexora exams" in exams


def test_an_unknown_scope_is_not_a_silent_empty_calendar(client, student):
    """A typo in the query string must not hand back a calendar with nothing
    in it -- that looks exactly like a broken feed."""
    body = client.get(f"/calendar/feed/{student[1]}.ics?only=banana").get_data(as_text=True)
    assert len(titles_in(body)) == len(ITEMS)


def test_the_sheet_offers_the_choice_and_defaults_to_exams(client, student):
    html = client.get("/calendar").get_data(as_text=True)
    assert 'data-scope="exams"' in html and 'data-scope=""' in html
    assert 'data-scope="exams" class="on"' in html, "the narrow one is the default"
