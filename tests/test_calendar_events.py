"""Events a student owns: adding, editing, deleting, and undoing the delete.

An event is a Task rather than a table of its own, so these tests also pin the
part that is easy to get wrong: deleting something the LMS supplied has to
survive the next sync. A delete that quietly undoes itself an hour later is
worse than no delete button.
"""

import os
import re
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="events@carleton.ca"):
    return client.post("/signup", data={"name": "Event Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


@pytest.fixture()
def student(client):
    signup(client)
    return client


def event_titles(client, month="2026-10"):
    """The titles actually rendered into the month grid.

    Searching the whole page is not good enough: the add-event form carries
    placeholder text like "Midterm 1", so a deleted event still appeared to be
    on the calendar.
    """
    body = client.get(f"/calendar?month={month}").get_data(as_text=True)
    return re.findall(r'class="ev".*?<strong>(.*?)</strong>', body, re.S)


def add(client, **payload):
    body = {"title": "Midterm 1", "date": "2026-10-15", "course": "BIOL1103"}
    body.update(payload)
    return client.post("/api/events", json=body)


# ---------------------------------------------------------------- creating

def test_an_event_can_be_added(student):
    res = add(student, time="14:30", kind="exam", location="Southam Hall")
    assert res.status_code == 201
    event = res.get_json()["event"]
    assert event["title"] == "Midterm 1"
    assert event["date"] == "2026-10-15"
    assert event["time"] == "14:30"
    assert event["kind"] == "exam"
    assert event["location"] == "Southam Hall"
    assert event["imported"] is False


def test_a_new_event_shows_up_on_the_calendar(student):
    add(student, date="2026-10-15")
    assert "Midterm 1" in event_titles(student)


def test_a_new_event_shows_up_on_the_dashboard(student):
    add(student)
    assert "Midterm 1" in student.get("/dashboard").get_data(as_text=True)


@pytest.mark.parametrize("payload,message", [
    ({"title": "   "}, "name"),
    ({"date": ""}, "date"),
    ({"date": "the 15th"}, "date"),
    ({"time": "25:00"}, "24-hour"),
    ({"time": "2pm"}, "24-hour"),
])
def test_bad_input_is_refused_with_something_readable(student, payload, message):
    res = add(student, **payload)
    assert res.status_code == 400
    assert message in res.get_json()["error"]


def test_a_blank_time_is_allowed(student):
    res = add(student, time="")
    assert res.status_code == 201
    assert res.get_json()["event"]["time"] == ""


def test_an_unknown_kind_falls_back_rather_than_failing(student):
    assert add(student, kind="party").get_json()["event"]["kind"] == "event"


# ----------------------------------------------------------------- editing

def test_an_event_can_be_edited(student):
    event_id = add(student).get_json()["event"]["id"]
    res = student.post(f"/api/events/{event_id}",
                       json={"title": "Midterm 1 (rescheduled)", "date": "2026-10-22",
                             "time": "09:00", "location": "Minto 2000"})
    assert res.status_code == 200
    event = res.get_json()["event"]
    assert event["title"] == "Midterm 1 (rescheduled)"
    assert event["date"] == "2026-10-22"
    assert event["time"] == "09:00"
    assert event["location"] == "Minto 2000"


def test_editing_only_the_time_leaves_everything_else_alone(student):
    event_id = add(student, location="Southam Hall").get_json()["event"]["id"]
    event = student.post(f"/api/events/{event_id}", json={"time": "18:00"}).get_json()["event"]
    assert event["time"] == "18:00"
    assert event["title"] == "Midterm 1"
    assert event["location"] == "Southam Hall"


def test_editing_someone_elses_event_is_a_404(client):
    signup(client, email="owner@carleton.ca")
    event_id = add(client).get_json()["event"]["id"]
    client.get("/logout")
    signup(client, email="intruder@carleton.ca")
    assert client.post(f"/api/events/{event_id}", json={"title": "Mine now"}).status_code == 404
    assert client.delete(f"/api/events/{event_id}").status_code == 404


# ---------------------------------------------------------------- deleting

def test_a_manual_event_is_deleted_outright(student):
    event_id = add(student).get_json()["event"]["id"]
    assert student.delete(f"/api/events/{event_id}").status_code == 200
    assert "Midterm 1" not in event_titles(student)


def test_deleting_returns_enough_to_undo_it(student):
    event_id = add(student, time="14:30", location="Southam Hall").get_json()["event"]["id"]
    deleted = student.delete(f"/api/events/{event_id}").get_json()["deleted"]
    assert deleted["title"] == "Midterm 1"
    assert deleted["time"] == "14:30"
    assert deleted["location"] == "Southam Hall"


def test_an_imported_deadline_stays_deleted_after_a_sync(student):
    """The one that matters. A row deletion would be re-imported on the next
    refresh and the student would watch their delete undo itself."""
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="events@carleton.ca").first()
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id, group_id="g1", title="Lab report 2",
            base_title="Lab report 2", course_full="BIOL1103", course_prefix="BIOL",
            due_raw="2026-10-20", kind="due", source="canvas",
            source_uid="canvas:99", sync_state="active"))
        nexora.db.session.commit()
        task_id = nexora.Task.query.filter_by(user_id=user.id, source_uid="canvas:99").first().id

    assert student.delete(f"/api/events/{task_id}").status_code == 200
    assert "Lab report 2" not in event_titles(student)

    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="events@carleton.ca").first()
        # the row survives, marked, which is what a later sync checks
        row = nexora.Task.query.filter_by(user_id=user.id, source_uid="canvas:99").first()
        assert row is not None
        assert row.sync_state == "dismissed"
        # and a re-import leaves it alone
        nexora._import_canvas_tasks(user, [{
            "course": "BIOL1103", "title": "Lab report 2",
            "due": "2026-10-20", "uid": "canvas:99"}])
        nexora.db.session.commit()
        assert nexora.Task.query.filter_by(user_id=user.id, source_uid="canvas:99").first().sync_state == "dismissed"

    assert "Lab report 2" not in event_titles(student)


def test_a_deleted_import_can_be_restored(student):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="events@carleton.ca").first()
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id, group_id="g2", title="Quiz 4",
            base_title="Quiz 4", course_full="SYSC2004", course_prefix="SYSC",
            due_raw="2026-10-09", kind="due", source="ics",
            source_uid="ics:44", sync_state="active"))
        nexora.db.session.commit()
        task_id = nexora.Task.query.filter_by(user_id=user.id, source_uid="ics:44").first().id

    student.delete(f"/api/events/{task_id}")
    assert student.post(f"/api/events/{task_id}/restore").status_code == 200
    assert "Quiz 4" in event_titles(student)


def test_the_endpoints_refuse_an_anonymous_caller(client):
    assert client.post("/api/events", json={"title": "x", "date": "2026-01-01"}).status_code in (302, 401)
    assert client.delete("/api/events/whatever").status_code in (302, 401)
