"""Two faults reported together on 2026-09-08, both from a value that was
never converted to the student's own frame.

1. The rail said the last sync happened at **2:04 AM** when it was 10:04 PM in
   Ottawa. Every datetime column in this app holds UTC and the templates were
   calling `.strftime()` straight on them. Same fault as the deadline dates,
   in a place nobody thought to look.

2. A student's CURRENT courses -- the five with actual deadlines in them --
   were grouped under "Other courses" while finished enrolments filled the
   page. Brightspace had stated no term for them, and the dashboard opens on
   the current term, so the courses he is taking right now were the ones
   hidden behind the picker.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"


# ------------------------------------------------- the clock a student reads

def test_a_late_evening_sync_is_not_shown_as_the_early_hours():
    """10:04 PM in Ottawa is 02:04Z. Printing the stored value said 2:04 AM,
    which reads as the app being a day out."""
    import app as nexora

    class U:
        timezone = "America/Toronto"

    stored = datetime(2026, 9, 9, 2, 4)          # what UTC holds
    assert nexora.local_stamp(stored, user=U()) == "Sep 08 at 10:04 PM"


def test_the_same_instant_reads_differently_in_another_zone():
    import app as nexora

    class Tokyo:
        timezone = "Asia/Tokyo"

    stored = datetime(2026, 9, 9, 2, 4)
    assert nexora.local_stamp(stored, user=Tokyo()) == "Sep 09 at 11:04 AM"


def test_an_aware_timestamp_is_converted_not_relabelled():
    import app as nexora

    class U:
        timezone = "America/Toronto"

    aware = datetime(2026, 9, 9, 2, 4, tzinfo=timezone.utc)
    assert nexora.local_stamp(aware, user=U()) == "Sep 08 at 10:04 PM"


def test_no_timestamp_prints_nothing_rather_than_a_placeholder_date():
    import app as nexora
    assert nexora.local_stamp(None) == ""


def test_the_rail_shows_the_students_own_clock(client):
    """The reported screen: the rail's Auto-sync line on every page."""
    import app as nexora
    client.post("/signup", data={"name": "S", "email": "clock@carleton.ca",
                                 "password": PASSWORD})
    assert client.post("/api/timezone",
                       json={"timezone": "America/Toronto"}).status_code == 200
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="clock@carleton.ca").first()
        user.last_sync = datetime(2026, 9, 9, 2, 4)
        nexora.db.session.commit()
    body = client.get("/dashboard").get_data(as_text=True)
    assert "Sep 08 at 10:04 PM" in body
    assert "Sep 09 at 02:04 AM" not in body


# ------------------------------------------------ which term a course is in

def test_a_courses_own_deadlines_say_which_term_it_is_in():
    import app as nexora
    assert nexora._term_from_dates(
        ["2026-09-10", "2026-10-07", "2026-11-30"]) == "Fall 2026"
    assert nexora._term_from_dates(
        ["2026-01-20", "2026-03-04"]) == "Winter 2026"
    assert nexora._term_from_dates(["2026-06-01"]) == "Summer 2026"


def test_the_common_term_wins_over_a_stray_date():
    import app as nexora
    assert nexora._term_from_dates(
        ["2026-09-10", "2026-10-07", "2026-11-30", "2027-01-05"]) == "Fall 2026"


def test_a_course_straddling_the_new_year_belongs_to_the_term_starting():
    """A tie goes to the later term: work in December and January is a course
    beginning, not one ending."""
    import app as nexora
    assert nexora._term_from_dates(["2026-12-10", "2027-01-14"]) == "Winter 2027"


@pytest.mark.parametrize("dates", [[], ["", None], ["not a date"]])
def test_nothing_to_go_on_means_no_guess(dates):
    """Inventing a term is worse than admitting to none."""
    import app as nexora
    assert nexora._term_from_dates(dates) == ""


def test_a_course_with_deadlines_and_no_stated_term_is_not_filed_under_other(client):
    """The reported screen: BIOL1103 and SYSC2004A sitting under 'Other
    courses' while the page opened on Fall 2026 and showed neither."""
    import app as nexora
    from uuid import uuid4

    client.post("/signup", data={"name": "S", "email": "terms@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="terms@carleton.ca").first()
        for code, due in [("SYSC2004A", "2026-10-07"), ("BIOL1103", "2026-11-12")]:
            nexora.db.session.add(nexora.Task(
                id=uuid4().hex, user_id=user.id,
                group_id=nexora.make_group_id(code, "Assignment 1"),
                title="Assignment 1", base_title="Assignment 1",
                course_full=code, course_prefix=nexora.course_prefix(code),
                due_raw=due, kind="due", is_deadline=True, source="ics",
                sync_state="active", created_at=""))
        nexora.db.session.commit()

    body = client.get("/dashboard").get_data(as_text=True)
    assert "Fall 2026" in body
    assert "Other courses" not in body


def test_a_stated_term_is_never_overridden_by_a_guess(client):
    """Brightspace saying 'Winter 2026' outranks arithmetic on dates. A course
    can legitimately carry work outside its own term."""
    import app as nexora
    from uuid import uuid4

    client.post("/signup", data={"name": "S", "email": "stated@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="stated@carleton.ca").first()
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user.id, source_uid="s1",
            course_code="ECOR1031", course_name="Programming",
            term="Winter 2026"))
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id,
            group_id=nexora.make_group_id("ECOR1031", "Lab 1"),
            title="Lab 1", base_title="Lab 1", course_full="ECOR1031",
            course_prefix="ECOR", due_raw="2026-10-07", kind="due",
            is_deadline=True, source="ics", sync_state="active", created_at=""))
        nexora.db.session.commit()

    body = client.get("/dashboard").get_data(as_text=True)
    assert "Winter 2026" in body
