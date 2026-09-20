"""Can a student trust the calendar?

Two failures found on 2026-09-06, both silent, both capable of making someone
miss an assignment:

1. The extension scrapes D2L's own `DueDate` for every assignment and quiz and
   sends it as `due_at`. Nothing consumed it -- `CourseContent` has no date
   column -- so a student who did exactly what the connect page says ("install
   the extension, press Import my courses") got a calendar with no deadlines in
   it at all. An empty calendar looks like a calm week.

2. Dates were taken in UTC. A Brightspace deadline of 11:59 PM Eastern is
   03:59Z the NEXT DAY, so the commonest deadline time in a university was
   shown a day late. That is worse than missing: it is confidently wrong.
"""

import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"


@pytest.fixture()
def synced(client):
    """A student in Toronto who has imported one course."""
    client.post("/signup", data={"name": "S", "email": "cal@carleton.ca",
                                 "password": PASSWORD})
    assert client.post("/api/timezone",
                       json={"timezone": "America/Toronto"}).status_code == 200
    token = client.post("/api/sync/extension-key").get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/sync/courses", json={"courses": [{
        "source_uid": "u1", "course_code": "BIOL1103", "course_name": "Biology",
        "brightspace_url": "https://learn.school.ca/d2l/home/42",
        "term": "Fall 2026"}], "complete": True}, headers=headers)
    return headers


def push(client, headers, items):
    return client.post("/api/sync/content",
                       json={"course": "BIOL1103", "items": items}, headers=headers)


def deadlines():
    import app as nexora
    with nexora.app.app_context():
        return {t.title: t.due_raw for t in nexora.Task.query.all()}


# ------------------------------------------------- the day has to be right

def test_a_late_evening_deadline_lands_on_the_students_own_day(synced, client):
    """11:59 PM Eastern on the 10th is 03:59Z on the 11th. Dating it the 11th
    tells a student the work is due tomorrow when it is due tonight."""
    push(client, synced, [{"content_type": "assignment", "title": "Lab Report 1",
                           "source_uid": "a1", "due_at": "2026-09-11T03:59:00Z"}])
    assert deadlines()["Lab Report 1"] == "2026-09-10"


def test_a_midday_deadline_is_unaffected(synced, client):
    push(client, synced, [{"content_type": "quiz", "title": "Quiz 1",
                           "source_uid": "q1", "due_at": "2026-09-18T17:00:00Z"}])
    assert deadlines()["Quiz 1"] == "2026-09-18"


def test_a_bare_date_is_never_shifted():
    """A date with no time is already a calendar day. Applying an offset it
    never had would move every all-day event."""
    import app as nexora
    from zoneinfo import ZoneInfo
    assert nexora.local_date_iso("2026-09-10", ZoneInfo("America/Toronto")) == "2026-09-10"
    assert nexora.local_date_iso("2026-09-10", ZoneInfo("Asia/Tokyo")) == "2026-09-10"


def test_the_same_instant_is_a_different_day_in_a_different_zone():
    import app as nexora
    from zoneinfo import ZoneInfo
    stamp = "2026-09-11T03:59:00Z"
    assert nexora.local_date_iso(stamp, ZoneInfo("America/Toronto")) == "2026-09-10"
    assert nexora.local_date_iso(stamp, ZoneInfo("Asia/Tokyo")) == "2026-09-11"
    assert nexora.local_date_iso(stamp, ZoneInfo("UTC")) == "2026-09-11"


@pytest.mark.parametrize("value", ["", None, "not a date", "sometime next week"])
def test_unparseable_dates_produce_no_deadline(value):
    """Never invent a day. A wrong date is worse than no date."""
    import app as nexora
    assert nexora.local_date_iso(value) == ""


def test_a_timestamp_with_no_offset_is_read_as_utc():
    """Brightspace and Canvas both send UTC; assuming local would shift a
    correct date the wrong way."""
    import app as nexora
    from zoneinfo import ZoneInfo
    assert nexora.local_date_iso("2026-09-11T03:59:00",
                                 ZoneInfo("America/Toronto")) == "2026-09-10"


# ------------------------------------- the deadline has to exist at all

def test_a_synced_assignment_becomes_a_visible_deadline(synced, client):
    # A date a few days out, not a fixed one: the calendar hides work more
    # than a week overdue, so a pinned September date failed on its own once
    # September wore on. 03:59Z is 11:59 PM Eastern the evening before.
    from datetime import date, timedelta
    due = date.today() + timedelta(days=3)
    push(client, synced, [{"content_type": "assignment", "title": "Lab Report 1",
                           "source_uid": "a1", "due_at": f"{due.isoformat()}T03:59:00Z"}])
    shown = due - timedelta(days=1)
    for page in (f"/calendar?month={shown.strftime('%Y-%m')}", "/dashboard"):
        assert "Lab Report 1" in client.get(page).get_data(as_text=True), \
            f"the deadline never reached {page}"


def test_it_reaches_the_feed_google_subscribes_to(synced, client):
    import app as nexora
    push(client, synced, [{"content_type": "quiz", "title": "Quiz 1",
                           "source_uid": "q1", "due_at": "2026-09-18T17:00:00Z"}])
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="cal@carleton.ca").first()
        token = nexora._calendar_token(user, create=True)
        nexora.db.session.commit()
    feed = client.get(f"/calendar/feed/{token}.ics").get_data(as_text=True)
    assert "Quiz 1" in feed
    assert "20260918" in feed


def test_content_without_a_due_date_creates_no_deadline(synced, client):
    """Slides are not a deadline. A calendar full of things that are not due is
    as untrustworthy as an empty one."""
    push(client, synced, [
        {"content_type": "resource", "title": "Week 3 slides", "source_uid": "r1"},
        {"content_type": "lecture", "title": "Lecture 4", "source_uid": "l1"},
        {"content_type": "assignment", "title": "No date given", "source_uid": "a9"},
    ])
    assert deadlines() == {}


# --------------------------------------------- re-syncing must be stable

def test_re_syncing_does_not_duplicate_a_deadline(synced, client):
    item = {"content_type": "assignment", "title": "Lab Report 1",
            "source_uid": "a1", "due_at": "2026-09-11T03:59:00Z"}
    for _ in range(3):
        push(client, synced, [item])
    import app as nexora
    with nexora.app.app_context():
        assert nexora.Task.query.filter_by(title="Lab Report 1").count() == 1


def test_a_moved_deadline_is_updated_not_added(synced, client):
    push(client, synced, [{"content_type": "assignment", "title": "Lab Report 1",
                           "source_uid": "a1", "due_at": "2026-09-11T03:59:00Z"}])
    push(client, synced, [{"content_type": "assignment", "title": "Lab Report 1",
                           "source_uid": "a1", "due_at": "2026-09-25T03:59:00Z"}])
    assert deadlines() == {"Lab Report 1": "2026-09-24"}


def test_deleting_a_deadline_survives_the_next_sync(synced, client):
    """Re-importing something the student deleted undoes their decision every
    time the extension runs."""
    import app as nexora
    item = {"content_type": "assignment", "title": "Lab Report 1",
            "source_uid": "a1", "due_at": "2026-09-11T03:59:00Z"}
    push(client, synced, [item])
    with nexora.app.app_context():
        task = nexora.Task.query.filter_by(title="Lab Report 1").first()
        task.sync_state = "dismissed"
        nexora.db.session.commit()
    push(client, synced, [item])
    with nexora.app.app_context():
        rows = nexora.Task.query.filter_by(title="Lab Report 1").all()
        assert len(rows) == 1
        assert rows[0].sync_state == "dismissed", "the delete was undone by a sync"
    assert "Lab Report 1" not in client.get("/calendar").get_data(as_text=True)


def test_a_deadline_attached_to_unchanged_content_is_still_created(synced, client):
    """The content loop short-circuits an item whose hash has not changed, so
    deadlines run in their own pass -- otherwise a student who synced before
    this existed would never get their deadlines."""
    import app as nexora
    item = {"content_type": "assignment", "title": "Lab Report 1",
            "source_uid": "a1", "due_at": "2026-09-11T03:59:00Z"}
    push(client, synced, [item])
    with nexora.app.app_context():
        nexora.Task.query.delete()          # as if imported before deadlines existed
        nexora.db.session.commit()
    push(client, synced, [item])            # identical payload, unchanged hash
    assert deadlines() == {"Lab Report 1": "2026-09-10"}


# ------------------------------------------------------ the timezone itself

def test_an_unknown_timezone_is_refused(client):
    """Storing something the app cannot honour would date deadlines wrongly."""
    client.post("/signup", data={"name": "S", "email": "tz@carleton.ca",
                                 "password": PASSWORD})
    assert client.post("/api/timezone", json={"timezone": "Mars/Olympus"}).status_code == 400
    assert client.post("/api/timezone", json={"timezone": ""}).status_code == 400
    import app as nexora
    with nexora.app.app_context():
        assert (nexora.User.query.filter_by(email="tz@carleton.ca").first().timezone or "") == ""


def test_a_student_who_never_reports_one_falls_back_to_utc(client):
    """No guessing from an IP. UTC is what every date meant before this."""
    import app as nexora
    client.post("/signup", data={"name": "S", "email": "notz@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="notz@carleton.ca").first()
        assert nexora._user_zone(user) is not None
        assert nexora.local_date_iso("2026-09-11T03:59:00Z",
                                     nexora._user_zone(user)) == "2026-09-11"


def test_the_page_asks_the_browser_for_it(client):
    client.post("/signup", data={"name": "S", "email": "ask@carleton.ca",
                                 "password": PASSWORD})
    body = client.get("/dashboard").get_data(as_text=True)
    assert "resolvedOptions().timeZone" in body
    assert "/api/timezone" in body


def test_the_reporter_is_not_shown_to_a_stranger(client):
    """It would 401, and an anonymous page has no account to store it on."""
    assert "/api/timezone" not in client.get("/").get_data(as_text=True)


# ------------------------------------------- finding the timetable upload

def test_a_first_time_student_sees_the_upload_first(client):
    """The upload was the second card, under an empty plan, and a student
    reported that they could not find it at all."""
    client.post("/signup", data={"name": "S", "email": "plan1@carleton.ca",
                                 "password": PASSWORD})
    body = client.get("/study-plan").get_data(as_text=True)
    assert 'class="card lead"' in body, "the timetable card is not promoted"
    assert "Add your timetable" in body
    assert "Start here" in body


def test_a_returning_student_sees_their_plan_first(client):
    """Setup before output only while there is setup to do."""
    import app as nexora
    client.post("/signup", data={"name": "S", "email": "plan2@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="plan2@carleton.ca").first()
        nexora.db.session.add(nexora.ClassBlock(
            id=uuid4().hex, user_id=user.id, label="BIOL1103 Lecture",
            weekday=0, start_min=9 * 60, end_min=10 * 60, kind="lecture"))
        nexora.db.session.commit()
    body = client.get("/study-plan").get_data(as_text=True)
    assert 'class="card lead"' not in body
    assert body.index("The plan") < body.index("Timetable")


def test_canvas_deadlines_use_the_students_zone_too():
    """11:59 PM is Canvas's default due time, so this path had the same
    day-late defect: str(value)[:10] is the UTC date."""
    import app as nexora
    from zoneinfo import ZoneInfo
    toronto = ZoneInfo('America/Toronto')
    assert nexora._canvas_due_date('2026-09-11T03:59:00Z', toronto) == '2026-09-10'
    assert nexora._canvas_due_date('2026-09-18T17:00:00Z', toronto) == '2026-09-18'
    assert nexora._canvas_due_date(None, toronto) == ''
    assert nexora._canvas_due_date('', toronto) == ''
