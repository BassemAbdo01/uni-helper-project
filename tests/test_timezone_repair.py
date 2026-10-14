"""The two things I had called limitations, and they were not.

1. "Existing rows keep their wrong dates until the next sync." Only the derived
   day was stored, so nothing could re-derive it. `Task.source_due_at` keeps the
   instant the LMS gave, and `_recompute_task_dates` re-dates everything locally
   with no network -- so correcting a timezone repairs history immediately
   instead of asking a student to press sync for our mistake.

2. "The timezone is only right after a student loads a page." The extension runs
   in the browser and Canvas states the setting in its own profile API. Both
   reach the server before any page view, and both state their own setting
   rather than being guessed at.
"""

import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"
LATE = "2026-09-11T03:59:00Z"        # 11:59 PM on the 10th, Eastern


def signup(client, email="tz@carleton.ca"):
    client.post("/signup", data={"name": "S", "email": email, "password": PASSWORD})
    token = client.post("/api/sync/extension-key").get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/sync/courses", json={"courses": [{
        "source_uid": "u1", "course_code": "BIOL1103", "course_name": "Biology",
        "brightspace_url": "https://learn.school.ca/d2l/home/1",
        "term": "Fall 2026"}], "complete": True}, headers=headers)
    return headers


def push_deadline(client, headers, timezone=None):
    payload = {"course": "BIOL1103", "items": [
        {"content_type": "assignment", "title": "Lab Report 1",
         "source_uid": "a1", "due_at": LATE}]}
    if timezone is not None:
        payload["timezone"] = timezone
    return client.post("/api/sync/content", json=payload, headers=headers)


def only_task():
    import app as nexora
    with nexora.app.app_context():
        return nexora.Task.query.first()


# ----------------------------------- 1. history is repairable, locally

def test_the_instant_is_kept_not_just_the_day(client):
    headers = signup(client)
    push_deadline(client, headers)
    assert only_task().source_due_at == LATE


def test_correcting_the_timezone_re_dates_what_is_already_stored(client):
    """No network, no re-sync, no asking the student to fix our mistake."""
    import app as nexora
    headers = signup(client)
    push_deadline(client, headers)
    with nexora.app.app_context():
        # As it would have been imported while the account was still UTC.
        nexora.Task.query.first().due_raw = "2026-09-11"
        nexora.User.query.first().timezone = ""
        nexora.db.session.commit()

    body = client.post("/api/timezone", json={"timezone": "America/Toronto"}).get_json()
    assert body["recomputed"] == 1
    assert only_task().due_raw == "2026-09-10"


def test_a_row_with_no_stored_instant_is_left_alone(client):
    """Imported before the column existed. Nothing to re-derive from, and
    inventing a shift would be worse than leaving it for the next sync."""
    import app as nexora
    client.post("/signup", data={"name": "S", "email": "old@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="old@carleton.ca").first()
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id, group_id="g", title="Legacy",
            base_title="Legacy", course_full="BIOL1103", due_raw="2026-09-11",
            source_due_at=""))
        nexora.db.session.commit()
    assert client.post("/api/timezone",
                       json={"timezone": "America/Toronto"}).get_json()["recomputed"] == 0
    assert only_task().due_raw == "2026-09-11"


def test_re_reporting_the_same_zone_changes_nothing(client):
    """The reporter fires on page views; it must not walk every deadline each
    time."""
    headers = signup(client)
    push_deadline(client, headers)
    client.post("/api/timezone", json={"timezone": "America/Toronto"})
    again = client.post("/api/timezone", json={"timezone": "America/Toronto"}).get_json()
    assert again["recomputed"] == 0


def test_moving_zone_moves_the_dates_again(client):
    """A student who travels, or who fixes a wrong setting."""
    headers = signup(client)
    push_deadline(client, headers)
    client.post("/api/timezone", json={"timezone": "America/Toronto"})
    assert only_task().due_raw == "2026-09-10"
    client.post("/api/timezone", json={"timezone": "Asia/Tokyo"})
    assert only_task().due_raw == "2026-09-11"


# ------------------------- 2. the zone arrives before any page view

def test_the_extension_supplies_the_zone_with_its_first_sync(client):
    """The request that creates every deadline is the one that needs it."""
    import app as nexora
    headers = signup(client)
    push_deadline(client, headers, timezone="America/Toronto")
    with nexora.app.app_context():
        assert nexora.User.query.first().timezone == "America/Toronto"
    assert only_task().due_raw == "2026-09-10", "dated in UTC despite a reported zone"


def test_a_zone_the_student_chose_outranks_a_client_reporting_one(client):
    """Someone who told Nexora where they are must not be overwritten by a
    browser in an airport."""
    import app as nexora
    headers = signup(client)
    client.post("/api/timezone", json={"timezone": "Asia/Tokyo"})
    push_deadline(client, headers, timezone="America/Toronto")
    with nexora.app.app_context():
        assert nexora.User.query.first().timezone == "Asia/Tokyo"


@pytest.mark.parametrize("reported", ["Mars/Olympus", "", "not a zone"])
def test_an_unusable_reported_zone_is_ignored(client, reported):
    import app as nexora
    headers = signup(client)
    push_deadline(client, headers, timezone=reported)
    with nexora.app.app_context():
        assert (nexora.User.query.first().timezone or "") == ""


def test_the_extension_actually_sends_it():
    """The server accepting the field is useless if the client never posts it."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = open(os.path.join(root, "extension", "background.js"), encoding="utf-8").read()
    assert "resolvedOptions().timeZone" in source
    assert "timezone: browserTimezone()" in source


def test_canvas_states_its_own_zone_and_that_is_used():
    """users/self/profile carries time_zone, so a token user needs no page view
    either."""
    import inspect
    import app as nexora
    source = inspect.getsource(nexora)
    assert 'profile.get("time_zone")' in source


# --------------------------- the feed's own timestamp shapes

# Every date below is anchored to the day the suite RUNS, not to the autumn it
# was written in. The calendar drops anything more than a week past
# (`dleft < -7`), so a fixture dated 6 October quietly stopped being rendered on
# 14 October 2026 and this file failed for a reason with nothing to do with
# timezones. What these tests pin is the RELATIONSHIP between the four DTSTART
# shapes and the day each one lands on; where they sit in the year is incidental,
# and hard-coding it made them expire.
_ANCHOR = date(2026, 10, 13)
_SHIFT = (date.today() - _ANCHOR).days


def _d(iso):
    """A fixture date, kept the same distance from today as when it was written."""
    return (date.fromisoformat(iso) + timedelta(days=_SHIFT)).isoformat()


def _stamp(iso):
    """The same date in the feed's own yyyymmdd."""
    return _d(iso).replace("-", "")


D2L_FEED = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//D2L//Brightspace//EN
BEGIN:VEVENT
UID:utc-late
SUMMARY:Lab 04 - Inheritance is due - SYSC2004A
DTSTART:{_stamp('2026-10-07')}T020000Z
END:VEVENT
BEGIN:VEVENT
UID:tzid-late
SUMMARY:Lab 05 - JUnit is due - SYSC2004A
DTSTART;TZID=America/Toronto:{_stamp('2026-10-13')}T220000
END:VEVENT
BEGIN:VEVENT
UID:floating-early
SUMMARY:Week 5 Quiz is due - SYSC2004A
DTSTART:{_stamp('2026-10-15')}T003000
END:VEVENT
BEGIN:VEVENT
UID:allday
SUMMARY:Reading week starts - SYSC2004A
DTSTART;VALUE=DATE:{_stamp('2026-10-26')}
END:VEVENT
END:VCALENDAR
"""


def import_feed(client):
    import io as _io
    return client.post("/import",
                       data={"ics_file": (_io.BytesIO(D2L_FEED.encode()), "feed.ics")},
                       content_type="multipart/form-data", follow_redirects=True)


def days_by_title():
    import app as nexora
    with nexora.app.app_context():
        return {t.title: (t.due_raw, t.due_time or "") for t in nexora.Task.query.all()}


def test_a_ten_pm_deadline_is_not_pushed_to_the_next_day(client):
    """Reported from the live site with screenshots: Brightspace said Tuesday
    Oct 6 at 10:00 PM and Nexora said Wednesday Oct 7. 22:00 Eastern is 02:00Z
    the following morning."""
    client.post("/signup", data={"name": "B", "email": "feed@carleton.ca",
                                 "password": PASSWORD})
    client.post("/api/timezone", json={"timezone": "America/Toronto"})
    import_feed(client)
    found = days_by_title()
    lab04 = next(v for k, v in found.items() if "Inheritance" in k)
    assert lab04 == (_d("2026-10-06"), "22:00")


def test_a_tzid_time_is_honoured(client):
    client.post("/signup", data={"name": "B", "email": "feed2@carleton.ca",
                                 "password": PASSWORD})
    client.post("/api/timezone", json={"timezone": "America/Toronto"})
    import_feed(client)
    lab05 = next(v for k, v in days_by_title().items() if "JUnit" in k)
    assert lab05 == (_d("2026-10-13"), "22:00")


def test_a_floating_time_is_wall_clock_not_utc(client):
    """The ics library labels a zone-less DTSTART as UTC, so after parsing it
    is indistinguishable from a real Z. Converting it pulled 00:30 back to
    20:30 the previous evening -- a day early."""
    client.post("/signup", data={"name": "B", "email": "feed3@carleton.ca",
                                 "password": PASSWORD})
    client.post("/api/timezone", json={"timezone": "America/Toronto"})
    import_feed(client)
    quiz = next(v for k, v in days_by_title().items() if "Week 5 Quiz" in k)
    assert quiz == (_d("2026-10-15"), "00:30")


def test_an_all_day_entry_is_never_shifted(client):
    client.post("/signup", data={"name": "B", "email": "feed4@carleton.ca",
                                 "password": PASSWORD})
    client.post("/api/timezone", json={"timezone": "Asia/Tokyo"})
    import_feed(client)
    reading = next(v for k, v in days_by_title().items() if "Reading week" in k)
    assert reading[0] == _d("2026-10-26")


def test_the_calendar_shows_the_time_so_it_can_be_checked(client):
    """A day that agrees but a time that does not is a bug worth seeing."""
    client.post("/signup", data={"name": "B", "email": "feed5@carleton.ca",
                                 "password": PASSWORD})
    client.post("/api/timezone", json={"timezone": "America/Toronto"})
    import_feed(client)
    body = client.get("/calendar?month=" + _d("2026-10-06")[:7]).get_data(as_text=True)
    assert "22:00" in body
    assert 'data-date="%s"' % _d("2026-10-06") in body


def test_settings_lets_a_student_see_and_change_it(client):
    client.post("/signup", data={"name": "B", "email": "feed6@carleton.ca",
                                 "password": PASSWORD})
    body = client.get("/settings").get_data(as_text=True)
    assert "Time zone" in body
    assert "America/Toronto" in body
    assert "resolvedOptions().timeZone" in body
