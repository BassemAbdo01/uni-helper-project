"""The planner used the server's date. On Render that is UTC.

At 8 PM in Ottawa on a Tuesday, UTC is already Wednesday. So the plan started
tomorrow, Tuesday's evening slots were simply gone, and `_close_off_yesterday`
marked the untouched Tuesday blocks MISSED while the student still had the
whole evening to do them. "Every date is the student's day, not UTC" -- the
lesson the deadlines learnt, missing from the one feature that is entirely
about which day it is.

And a plan built at 3 PM offered 9 AM today.
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"


@pytest.fixture()
def toronto(client):
    import app as nexora
    client.post("/signup", data={"name": "B", "email": "clockplan@carleton.ca",
                                 "password": PASSWORD})
    client.post("/api/timezone", json={"timezone": "America/Toronto"})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email="clockplan@carleton.ca").first().id


def freeze(monkeypatch, utc_iso):
    """Pin 'now' to a UTC instant; the student's clock is derived from it."""
    import app as nexora
    fixed = datetime.fromisoformat(utc_iso).replace(tzinfo=ZoneInfo("UTC"))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(nexora, "datetime", FrozenDatetime)


def test_late_evening_is_still_today_for_the_student(toronto, monkeypatch):
    """8 PM Tuesday in Ottawa is 00:00 Wednesday UTC. The student's day has not
    ended, and the planner must agree."""
    import app as nexora
    freeze(monkeypatch, "2026-09-16T00:00:00")          # Tue 20:00 Eastern
    with nexora.app.app_context():
        assert nexora._student_today(toronto).isoformat() == "2026-09-15"


def test_an_evening_block_is_not_marked_missed_before_the_evening(toronto, monkeypatch):
    """The visible symptom: 'it marked my 9 PM session missed at 8 PM'."""
    import app as nexora
    from uuid import uuid4
    freeze(monkeypatch, "2026-09-16T00:00:00")          # Tue 20:00 Eastern
    with nexora.app.app_context():
        nexora.db.session.add(nexora.StudySession(
            id=uuid4().hex, user_id=toronto, on_date="2026-09-15",
            start_min=21 * 60, end_min=22 * 60, title="Tonight", state="planned"))
        nexora.db.session.commit()
        missed = nexora._close_off_yesterday(toronto)
        assert missed["count"] == 0
        row = nexora.StudySession.query.filter_by(title="Tonight").first()
        assert row.state == "planned"


def test_yesterday_is_retired_once_the_students_day_has_actually_ended(toronto, monkeypatch):
    import app as nexora
    from uuid import uuid4
    freeze(monkeypatch, "2026-09-16T12:00:00")          # Wed 08:00 Eastern
    with nexora.app.app_context():
        nexora.db.session.add(nexora.StudySession(
            id=uuid4().hex, user_id=toronto, on_date="2026-09-15",
            start_min=21 * 60, end_min=22 * 60, title="Last night", state="planned"))
        nexora.db.session.commit()
        assert nexora._close_off_yesterday(toronto)["count"] == 1


def test_a_plan_built_in_the_afternoon_does_not_offer_the_morning(toronto, monkeypatch):
    import app as nexora
    freeze(monkeypatch, "2026-09-15T19:00:00")          # Tue 15:00 Eastern
    with nexora.app.app_context():
        pref = nexora._study_preference(toronto)
        pref.earliest_min = 9 * 60
        pref.latest_min = 22 * 60
        nexora.db.session.commit()
        slots = nexora._free_slots(toronto, pref, nexora._student_today(toronto), 2)
        today = [(a, b) for d, a, b in slots if d.isoformat() == "2026-09-15"]
        assert today, "no window left today at all"
        assert min(a for a, _ in today) >= 15 * 60
        tomorrow = [(a, b) for d, a, b in slots if d.isoformat() == "2026-09-16"]
        assert min(a for a, _ in tomorrow) == 9 * 60


def test_the_first_slot_starts_on_the_next_quarter_hour(toronto, monkeypatch):
    """15:07 becomes 15:15, not 15:07 -- a block starting at seven minutes past
    reads as a mistake."""
    import app as nexora
    freeze(monkeypatch, "2026-09-15T19:07:00")          # Tue 15:07 Eastern
    with nexora.app.app_context():
        pref = nexora._study_preference(toronto)
        pref.earliest_min, pref.latest_min = 9 * 60, 22 * 60
        nexora.db.session.commit()
        slots = nexora._free_slots(toronto, pref, nexora._student_today(toronto), 1)
        assert slots[0][1] == 15 * 60 + 15


def test_a_plan_built_for_a_fixed_day_is_untouched_by_the_clock(toronto, monkeypatch):
    """`today=` is how tests and the insight page ask about a specific day.
    The now-cutoff applies only to the real today."""
    import app as nexora
    from datetime import date
    freeze(monkeypatch, "2026-09-15T19:00:00")
    with nexora.app.app_context():
        pref = nexora._study_preference(toronto)
        pref.earliest_min, pref.latest_min = 9 * 60, 22 * 60
        nexora.db.session.commit()
        slots = nexora._free_slots(toronto, pref, date(2026, 9, 20), 1)
        assert slots[0][1] == 9 * 60
