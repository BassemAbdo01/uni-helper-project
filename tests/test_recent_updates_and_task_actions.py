"""Recent updates are recent by Brightspace's date, and a task has two actions.

"Recent updates" was the four newest AcademicUpdate rows, which are stamped
at SYNC time. "Import every term" brought first-year announcements in on the
same afternoon as this week's, so a CHEM 1101 notice from two years ago led
the panel, dated today, above anything from the term in progress. The panel
now reads the content rows: dated by what Brightspace said, on the student's
day, linked to the page Brightspace gave.

"What to do first" was one button per row: the whole card ticked the task
off, and there was no way to go and do it. The box ticks; the title opens
the work in the LMS when the LMS gave a link.
"""
import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

ORIGIN = "https://learn.school.ca"


@pytest.fixture()
def synced(client):
    client.post("/signup", data={"name": "S", "email": f"upd{uuid4().hex[:6]}@carleton.ca",
                                 "password": "updates-pass-1"})
    token = client.post("/api/sync/extension-key").get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/sync/courses", headers=headers, json={"courses": [
        {"source_uid": "1", "course_code": "CHEM1101", "course_name": "Chem", "term": "Fall 2024"},
        {"source_uid": "2", "course_code": "SYSC2310", "course_name": "Digital", "term": "Fall 2026"},
    ]})
    return headers


def push(client, headers, course, items):
    r = client.post("/api/sync/content", headers=headers, json={"course": course, "items": items})
    assert r.status_code == 200, r.get_json()


def test_an_old_announcement_imported_today_does_not_lead_the_panel(client, synced):
    today = date.today()
    push(client, synced, "CHEM1101", [{"content_type": "announcement", "title": "Directions to my office",
                                       "source_uid": "old", "published_at": "2024-03-05T14:00:00Z",
                                       "source_url": f"{ORIGIN}/d2l/lms/news/main.d2l?ou=1"}])
    push(client, synced, "SYSC2310", [{"content_type": "announcement", "title": "Lab 2 moved to Thursday",
                                       "source_uid": "new", "published_at": f"{(today - timedelta(days=1)).isoformat()}T15:00:00Z",
                                       "source_url": f"{ORIGIN}/d2l/lms/news/main.d2l?ou=2"}])
    body = client.get("/dashboard").get_data(as_text=True)
    assert body.index("Lab 2 moved to Thursday") < body.index("Directions to my office")
    # dated by Brightspace, not by the sync
    assert "Mar 05, 2024" in body.split("Directions to my office")[1][:200]   # the year, or it reads as this March
    assert today.strftime("%b %d") not in body.split("Directions to my office")[1][:200]


def test_an_update_links_to_the_page_brightspace_gave(client, synced):
    push(client, synced, "SYSC2310", [{"content_type": "announcement", "title": "Lab 2 moved to Thursday",
                                       "source_uid": "new", "published_at": f"{date.today().isoformat()}T15:00:00Z",
                                       "source_url": f"{ORIGIN}/d2l/lms/news/main.d2l?ou=2"}])
    body = client.get("/dashboard").get_data(as_text=True)
    assert f'class="upd" href="{ORIGIN}/d2l/lms/news/main.d2l?ou=2"' in body


def test_a_javascript_link_on_an_update_is_not_rendered(client, synced):
    push(client, synced, "SYSC2310", [{"content_type": "announcement", "title": "Evil notice",
                                       "source_uid": "evil", "published_at": f"{date.today().isoformat()}T15:00:00Z",
                                       "source_url": "javascript:alert(1)"}])
    body = client.get("/dashboard").get_data(as_text=True)
    assert "Evil notice" in body and "javascript:" not in body


def test_a_due_date_is_not_an_update(client, synced):
    """An assignment's published_at is its due date in the extension; a
    deadline belongs to the deadline lists, not to "what Brightspace said"."""
    push(client, synced, "SYSC2310", [{"content_type": "assignment", "title": "Lab 3 report",
                                       "source_uid": "a3", "published_at": f"{date.today().isoformat()}T15:00:00Z",
                                       "due_at": f"{(date.today() + timedelta(days=3)).isoformat()}T03:59:00Z"}])
    assert not nexora._recent_updates(_user(client), limit=4)


def test_an_undated_row_is_not_recent(client, synced):
    push(client, synced, "CHEM1101", [{"content_type": "announcement", "title": "Undated",
                                       "source_uid": "u", "published_at": ""}])
    assert not nexora._recent_updates(_user(client), limit=4)


def _user(client):
    with nexora.app.app_context():
        return nexora.User.query.order_by(nexora.User.id.desc()).first()


# ------------------------------------------------- what to do first: two actions

def test_a_queued_task_ticks_from_the_box_and_opens_from_the_title(client, synced):
    # The stored URL is the upload form, which is what every row imported
    # before the fix carries. What the page renders is the assignment -- see
    # tests/test_assignment_link.py: `folder_submit_files.d2l` is where a file
    # is uploaded, so a folder that takes no file answers Internal Error, which
    # is what this test used to pin as correct.
    stored = f"{ORIGIN}/d2l/lms/dropbox/user/folder_submit_files.d2l?db=5&ou=2"
    rendered = f"{ORIGIN}/d2l/le/dropbox/2/5/View"
    push(client, synced, "SYSC2310", [{"content_type": "assignment", "title": "Lab 1 Report Submission",
                                       "source_uid": "a1", "due_at": f"{(date.today() + timedelta(days=3)).isoformat()}T03:59:00Z",
                                       "source_url": stored}])
    body = client.get("/dashboard").get_data(as_text=True)
    card = body.split('<div class="task"', 1)[1].split("</div>", 1)[0]
    assert 'class="box"' in card and 'aria-label="Mark done: Lab 1 Report Submission"' in card
    # What this test is for: the title is an anchor into the LMS and the box
    # ticks. Both still hold; only the destination was wrong.
    assert f'href="{rendered}"' in card and 'target="_blank"' in card


def test_a_task_with_no_link_has_no_link(client, synced):
    push(client, synced, "SYSC2310", [{"content_type": "assignment", "title": "Unlinked work",
                                       "source_uid": "a2", "due_at": f"{(date.today() + timedelta(days=3)).isoformat()}T03:59:00Z"}])
    body = client.get("/dashboard").get_data(as_text=True)
    card = body.split('<div class="task"', 1)[1].split("</div>", 1)[0]
    assert "Unlinked work" in card and "<a " not in card
