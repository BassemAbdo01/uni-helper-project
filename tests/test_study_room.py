"""The study room is the place a plan block gets done.

The room used to be boxes with labels: a static screen, a notebook whose
ticks saved nothing, a timer that recorded nothing. Now everything on the
desk carries the identity the drawer needs to act on it -- a task's group
to tick it, a deadline's link to open it, tonight's plan blocks to run the
clock against -- and the clock records a finished block with the minutes
that really passed, which is what the planner learns from.

These pin the data contract the page is built on; the rendering itself is
verified in a browser.
"""
import json
import os
import re
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

ORIGIN = "https://learn.school.ca"


@pytest.fixture()
def student(client):
    email = f"room{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "Rana Test", "email": email, "password": "room-pass-123"})
    token = client.post("/api/sync/extension-key").get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/sync/courses", headers=headers, json={"courses": [
        {"source_uid": "2", "course_code": "SYSC2310", "course_name": "Digital Systems", "term": "Fall 2026"},
    ]})
    due = (date.today() + timedelta(days=3)).isoformat()
    client.post("/api/sync/content", headers=headers, json={"course": "SYSC2310", "items": [
        {"content_type": "assignment", "title": "Lab 1 Report", "source_uid": "a1", "due_at": f"{due}T03:59:00Z",
         "source_url": f"{ORIGIN}/d2l/lms/dropbox/user/folder_submit_files.d2l?db=5&ou=2"},
    ]})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


def room_data(client):
    html = client.get("/study-room").get_data(as_text=True)
    m = re.search(r"window\.ROOM_DATA = (\{.*?\});\n", html)
    assert m, "the page carries its data"
    return html, json.loads(m.group(1))


def test_everything_on_the_desk_carries_what_the_drawer_needs(client, student):
    html, d = room_data(client)
    task = next(t for t in d["tasks"] if t["title"] == "Lab 1 Report")
    assert task["group_id"] and task["link"].startswith(ORIGIN)          # tick it, open it
    dl = next(x for x in d["deadlines"] if x["title"] == "Lab 1 Report")
    assert dl["link"].startswith(ORIGIN) and dl["status"] and dl["status_label"]
    assert d["courses"][0]["code"] == "SYSC2310"
    assert d["today"] == date.today().isoformat() or abs((date.fromisoformat(d["today"]) - date.today()).days) <= 1
    assert 0 <= d["now_min"] < 1440
    assert d["first_name"] == "Rana"
    for key in ("screen", "notebook", "shelf", "calendar", "clock", "lamp"):
        assert f'data-key="{key}"' in html


def test_the_screen_carries_tomorrow_when_today_is_empty(client, student):
    """At 23:50 with nothing left today, "no plan" is the wrong answer."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    with nexora.app.app_context():
        nexora.db.session.add(nexora.StudySession(id=uuid4().hex, user_id=student, on_date=tomorrow, start_min=540, end_min=590,
                                                  course_full="SYSC2310", title="Lab 1 Report", focus="Draft the results section",
                                                  reason="10% · due in 3 days", item_kind="assignment", state="planned"))
        nexora.db.session.commit()
    _, d = room_data(client)
    assert [b["date"] for b in d["blocks"]] == [tomorrow]
    assert d["blocks"][0]["minutes"] == 50 and d["blocks"][0]["focus"] == "Draft the results section"


def test_a_finished_block_is_recorded_with_the_minutes_that_passed(client, student):
    """What the room's clock posts, and what the planner then knows."""
    today = date.today().isoformat()
    sid = uuid4().hex
    with nexora.app.app_context():
        nexora.db.session.add(nexora.StudySession(id=sid, user_id=student, on_date=today, start_min=1200, end_min=1250,
                                                  course_full="SYSC2310", title="Lab 1 Report", item_kind="assignment", state="planned"))
        nexora.db.session.commit()
    r = client.post(f"/api/study-plan/{sid}", json={"state": "done", "minutes": 38})
    assert r.status_code == 200 and r.get_json()["session"]["actual_minutes"] == 38
    _, d = room_data(client)
    block = next(b for b in d["blocks"] if b["id"] == sid)
    assert block["state"] == "done" and block["actual_minutes"] == 38


def test_the_room_never_reaches_gemini(client, student):
    """A page that costs quota to open is one nobody opens twice."""
    import urllib.request
    calls = []
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: calls.append(a) or orig(*a, **k)
    try:
        assert client.get("/study-room").status_code == 200
    finally:
        urllib.request.urlopen = orig
    assert not calls
