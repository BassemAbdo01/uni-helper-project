"""The .ics feed a student subscribes to from Google Calendar.

Google cannot sign in, so the token in the URL is the entire credential. That
makes two things worth pinning hard: the token must actually gate the feed, and
rotating it must break the old link. The rest is iCalendar being fussy -- an
unescaped comma or an over-long line makes Google drop an event silently, with
no error anywhere for the student to see.
"""

import os
import re
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="feed@carleton.ca"):
    return client.post("/signup", data={"name": "Feed Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


def soon(days=10):
    return (date.today() + timedelta(days=days)).isoformat()


@pytest.fixture()
def student(client):
    signup(client)
    return client


def feed_url(client):
    res = client.get("/api/calendar/feed")
    assert res.status_code == 200
    return res.get_json()["url"]


def path_of(url):
    return "/" + url.split("/", 3)[3]


def add(client, **payload):
    body = {"title": "Midterm 1", "date": soon(), "course": "BIOL1103"}
    body.update(payload)
    return client.post("/api/events", json=body)


def events_in(body):
    return re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", body, re.S)


# ------------------------------------------------------------- the link

def test_a_link_is_issued_on_request(student):
    url = feed_url(student)
    assert url.endswith(".ics")
    assert "/calendar/feed/" in url


def test_the_link_is_stable_until_it_is_rotated(student):
    first = feed_url(student)
    assert feed_url(student) == first
    rotated = student.post("/api/calendar/feed").get_json()["url"]
    assert rotated != first


def test_rotating_breaks_the_old_link(student):
    old = path_of(feed_url(student))
    student.post("/api/calendar/feed")
    assert student.get(old).status_code == 404


def test_turning_the_feed_off_stops_serving_it(student):
    url = path_of(feed_url(student))
    assert student.delete("/api/calendar/feed").status_code == 200
    assert student.get(url).status_code == 404


@pytest.mark.parametrize("token", ["", "short", "x" * 30, "../../etc/passwd"])
def test_a_wrong_token_gets_nothing(client, token):
    assert client.get(f"/calendar/feed/{token}.ics").status_code == 404


def test_one_students_link_never_shows_another_students_work(client):
    signup(client, email="a@carleton.ca")
    add(client, title="Private BIOL midterm")
    url = path_of(feed_url(client))
    client.get("/logout")
    signup(client, email="b@carleton.ca")
    add(client, title="Someone elses exam")
    body = client.get(url).get_data(as_text=True)
    assert "Private BIOL midterm" in body
    assert "Someone elses exam" not in body


# ------------------------------------------------------- what it contains

def test_the_feed_is_a_calendar_google_will_accept(student):
    add(student)
    body = student.get(path_of(feed_url(student))).get_data(as_text=True)
    assert body.startswith("BEGIN:VCALENDAR")
    assert body.rstrip().endswith("END:VCALENDAR")
    assert "VERSION:2.0" in body
    assert "PRODID:" in body
    # every line ends CRLF, which the spec requires and some clients enforce
    assert "\n" in body and body.count("\r\n") == body.count("\n")


def test_it_is_served_as_a_calendar_not_a_download(student):
    res = student.get(path_of(feed_url(student)))
    assert res.headers["Content-Type"] == "text/calendar; charset=utf-8"
    assert "attachment" not in res.headers.get("Content-Disposition", "")


def test_an_all_day_deadline_has_an_exclusive_end_date(student):
    """DTEND is exclusive in iCalendar. Setting it to the same day makes the
    event zero-length and Google hides it."""
    add(student, date="2026-10-15", time="")
    body = student.get(path_of(feed_url(student))).get_data(as_text=True)
    event = events_in(body)[0]
    assert "DTSTART;VALUE=DATE:20261015" in event
    assert "DTEND;VALUE=DATE:20261016" in event


def test_an_exam_with_a_time_becomes_a_timed_event(student):
    add(student, date="2026-10-15", time="18:00", kind="exam", location="Southam Hall")
    event = events_in(student.get(path_of(feed_url(student))).get_data(as_text=True))[0]
    assert "DTSTART:20261015T180000" in event
    assert "DTEND:20261015T190000" in event
    assert "LOCATION:Southam Hall" in event


def test_a_late_exam_does_not_end_before_it_starts(student):
    """23:00 plus an hour is midnight the next day, not hour 24."""
    add(student, date="2026-10-15", time="23:00")
    event = events_in(student.get(path_of(feed_url(student))).get_data(as_text=True))[0]
    assert "DTSTART:20261015T230000" in event
    assert "DTEND:20261016T000000" in event


def test_commas_and_semicolons_are_escaped(student):
    """Raw punctuation ends the property early and the event vanishes with no
    error the student could ever see."""
    add(student, title="Essay: sources, methods; conclusion")
    event = events_in(student.get(path_of(feed_url(student))).get_data(as_text=True))[0]
    assert r"sources\, methods\; conclusion" in event


def test_a_long_title_is_folded_and_still_readable(student):
    long_title = "Comparative analysis of eukaryotic and prokaryotic cellular respiration pathways under stress"
    add(student, title=long_title)
    body = student.get(path_of(feed_url(student))).get_data(as_text=True)
    assert all(len(line.encode("utf-8")) <= 75 for line in body.split("\r\n"))
    # unfolding puts it back together
    assert long_title in body.replace("\r\n ", "")


def test_the_course_code_rides_along_in_the_title(student):
    add(student, course="BIOL1103", title="Midterm 1")
    body = student.get(path_of(feed_url(student))).get_data(as_text=True)
    assert "SUMMARY:BIOL1103 - Midterm 1" in body


def test_an_event_with_no_course_is_not_labelled_OTHER(student):
    add(student, course="", title="Dentist")
    body = student.get(path_of(feed_url(student))).get_data(as_text=True)
    assert "SUMMARY:Dentist" in body
    assert "OTHER" not in body


def test_a_deleted_import_is_not_in_the_feed(student):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="feed@carleton.ca").first()
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id, group_id="g9", title="Dropped quiz",
            base_title="Dropped quiz", course_full="SYSC2004", course_prefix="SYSC",
            due_raw=soon(), kind="due", source="ics", source_uid="ics:77",
            sync_state="active"))
        nexora.db.session.commit()
        task_id = nexora.Task.query.filter_by(user_id=user.id, source_uid="ics:77").first().id
    url = path_of(feed_url(student))
    assert "Dropped quiz" in student.get(url).get_data(as_text=True)
    student.delete(f"/api/events/{task_id}")
    assert "Dropped quiz" not in student.get(url).get_data(as_text=True)


def test_ancient_work_is_left_out(student):
    add(student, title="Last year's essay", date=(date.today() - timedelta(days=300)).isoformat())
    add(student, title="This term's essay")
    body = student.get(path_of(feed_url(student))).get_data(as_text=True)
    assert "Last year" not in body
    assert "This term" in body


def test_an_assignment_with_open_and_close_rows_appears_once(student):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="feed@carleton.ca").first()
        for kind in ("opens", "due", "closes"):
            nexora.db.session.add(nexora.Task(
                id=uuid4().hex, user_id=user.id, group_id="shared", title="Assignment 3",
                base_title="Assignment 3", course_full="ECOR1031", course_prefix="ECOR",
                due_raw=soon(12), kind=kind, source="ics",
                source_uid=f"ics:a3:{kind}", sync_state="active"))
        nexora.db.session.commit()
    body = student.get(path_of(feed_url(student))).get_data(as_text=True)
    assert body.count("Assignment 3") == 1


def test_the_link_endpoint_needs_a_login(client):
    assert client.get("/api/calendar/feed").status_code in (302, 401)
