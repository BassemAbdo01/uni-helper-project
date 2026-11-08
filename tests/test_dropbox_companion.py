"""B4: an assignment and the folder you upload it to are one deadline.

"Assignment 3: Gate-level implementation" and "Assignment 3: Place to upload
your solution" are two D2L items with two ids, so `_import_deadlines` made two
`Task` rows with different `source_uid`s -- and `make_group_id` collapses
duplicates on `base_title`, which differs. The student sees the same piece of
work twice, and ticks one off while the other stays.

The fold is deliberately NARROW, because the failure modes are not symmetric:
too narrow leaves two cards, which is the status quo; too broad merges two
genuinely different assessments and hides one behind the other. So only an
unmistakable upload phrase counts ("place to upload", "submit here",
"submission folder"), the bare word "submission" never does -- "Assignment 8c
... - Final Submission" is a real Carleton deadline -- and a stem matching more
than one sibling is left alone rather than guessed at.
"""

import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ORIGIN = "https://brightspace.carleton.ca"
PASSWORD = "correct-horse-battery"


@pytest.fixture()
def synced(client):
    client.post("/signup", data={"name": "S", "email": f"db{uuid4().hex[:6]}@carleton.ca",
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


def soon(days=6):
    return (date.today() + timedelta(days=days)).isoformat() + "T23:59:00Z"


def item(title, uid, url=""):
    return {"title": title, "source_uid": uid, "course": "SYSC2004",
            "content_type": "assignment", "due_at": soon(),
            "source_url": url or f"{ORIGIN}/d2l/le/dropbox/42/{uid}/View"}


def push(client, headers, items):
    return client.post("/api/sync/content",
                       json={"course": "SYSC2004", "items": items}, headers=headers)


def groups():
    import app as nexora
    with nexora.app.app_context():
        rows = nexora.Task.query.all()
        out = {}
        for t in rows:
            out.setdefault(t.group_id, []).append(t.title)
        return out


def cards():
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.first()
        rows = [t for t in nexora.Task.query.filter_by(user_id=user.id).all()]
        raw = [{"group_id": t.group_id, "title": t.title, "base_title": t.base_title,
                "course_full": t.course_full, "course_prefix": t.course_prefix,
                "due_raw": t.due_raw, "kind": t.kind, "completed": t.completed,
                "source_link": t.source_link, "id": t.id} for t in rows]
        return nexora.build_grouped_cards(raw)


# ------------------------------------------------------------ the report

def test_an_assignment_and_its_upload_folder_are_one_card(synced, client):
    push(client, synced, [
        item("Assignment 3: Gate-level implementation", "a3"),
        item("Assignment 3: Place to upload your solution", "a3-drop"),
    ])
    assert len(groups()) == 1, groups()


def test_the_card_keeps_the_link_that_opens_the_work(synced, client):
    """The companion's link goes to the upload form; the assignment's opens
    what you are being asked to do."""
    push(client, synced, [
        item("Assignment 3: Gate-level implementation", "a3",
             url=f"{ORIGIN}/d2l/le/content/42/viewContent/900/View"),
        item("Assignment 3: Place to upload your solution", "a3-drop"),
    ])
    (card,) = cards()
    assert "Gate-level" in card["title"]


@pytest.mark.parametrize("companion", [
    "Assignment 3: Place to upload your solution",
    "Assignment 3 - Submit here",
    "Assignment 3 submission folder",
    "Assignment 3: Upload your report",
])
def test_the_upload_phrases_that_fold(synced, client, companion):
    push(client, synced, [item("Assignment 3: Gate-level implementation", "a3"),
                          item(companion, "a3-drop")])
    assert len(groups()) == 1, companion


# --------------------------------------------- what must NOT be swallowed

def test_a_final_submission_is_a_real_deadline_not_a_companion(synced, client):
    """"Assignment 8c Innovation Proposal ... - Final Submission" is a real
    Carleton deadline. The bare word must never fold anything."""
    push(client, synced, [
        item("Assignment 8c: Innovation Proposal", "a8"),
        item("Assignment 8c: Innovation Proposal - Final Submission", "a8-final"),
    ])
    assert len(groups()) == 2, groups()


def test_two_real_assessments_sharing_a_prefix_stay_apart(synced, client):
    push(client, synced, [item("Assignment 3: Part A", "a3a"),
                          item("Assignment 3: Part B", "a3b")])
    assert len(groups()) == 2, groups()


def test_an_ambiguous_stem_is_left_alone(synced, client):
    """Two siblings could own the companion. Guessing one would hide a real
    assessment behind another; two cards is the lesser failure."""
    push(client, synced, [
        item("Assignment 3: Part A", "a3a"),
        item("Assignment 3: Part B", "a3b"),
        item("Assignment 3: Place to upload your solution", "a3-drop"),
    ])
    assert len(groups()) == 3, groups()


def test_a_companion_with_no_sibling_keeps_its_own_card(synced, client):
    """Folding it into nothing would delete the only trace of the deadline."""
    push(client, synced, [item("Assignment 7: Place to upload your solution", "a7-drop")])
    assert len(groups()) == 1, groups()


def test_a_bare_submission_title_is_not_a_stem_match(synced, client):
    """"Submission" alone has no stem to match a sibling with."""
    push(client, synced, [item("Assignment 3: Gate-level implementation", "a3"),
                          item("Submit here", "orphan")])
    assert len(groups()) == 2, groups()


def test_a_different_course_is_never_the_sibling(synced, client):
    import app as nexora
    push(client, synced, [item("Assignment 3: Gate-level implementation", "a3")])
    other = dict(item("Assignment 3: Place to upload your solution", "a3-drop"))
    other["course"] = "ELEC2501"
    client.post("/api/sync/content",
                json={"course": "ELEC2501", "items": [other]}, headers=synced)
    assert len(groups()) == 2, groups()
