"""The endpoints the published extension actually calls.

These had no tests at all, which meant the storage logic behind them could be
refactored -- as it was, to let Canvas share it -- with a fully green suite and
no evidence either way. The extension is installed on students' machines and
cannot be rolled back as easily as the server, so its contract is worth pinning
down: the auth boundary, the shapes it sends, and the fact that syncing twice
does not pile up duplicates.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="ext.student@carleton.ca", password="correct-horse-battery"):
    return client.post("/signup", data={"name": "Ext Student", "email": email,
                                        "password": password}, follow_redirects=False)


@pytest.fixture()
def token(client):
    """A signed-in student and the key their extension would be given."""
    signup(client)
    res = client.post("/api/sync/extension-key")
    assert res.status_code == 200
    return res.get_json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


COURSES = [{
    "source_uid": "https://learn.school.ca/d2l/home/8801",
    "course_code": "SYSC 2004",
    "course_name": "Object-Oriented Software Development",
    "term": "Winter 2026",
    "brightspace_url": "https://learn.school.ca/d2l/home/8801",
}]

ITEMS = [{
    "course": "SYSC2004",
    "title": "Lecture 12 - Polymorphism",
    "content_type": "lecture",
    "source_uid": "d2l:8801:topic:551",
    "source_url": "https://learn.school.ca/d2l/le/content/8801/viewContent/551/View",
    "module_name": "Week 9",
    "content_text": "Inheritance models an is-a relationship.",
}]


# --------------------------------------------------------------------------
# The authorization boundary
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/sync/courses", "/api/sync/content"])
def test_sync_endpoints_refuse_an_anonymous_caller(client, path):
    assert client.post(path, json={}).status_code == 401


@pytest.mark.parametrize("path", ["/api/sync/courses", "/api/sync/content"])
def test_sync_endpoints_refuse_a_wrong_token(client, path):
    signup(client)
    res = client.post(path, json={}, headers=auth("not-a-real-token"))
    assert res.status_code == 401


def test_a_token_only_writes_into_its_own_account(client):
    """The key identifies the student; it must not be usable against another."""
    import app as nexora
    signup(client, email="first@carleton.ca")
    first_token = client.post("/api/sync/extension-key").get_json()["token"]
    client.get("/logout")

    signup(client, email="second@carleton.ca")
    client.get("/logout")

    client.post("/api/sync/courses", json={"courses": COURSES}, headers=auth(first_token))
    with nexora.app.app_context():
        rows = nexora.SyncedCourse.query.all()
        assert len(rows) == 1
        owner = nexora.User.query.get(rows[0].user_id)
        assert owner.email == "first@carleton.ca"


# --------------------------------------------------------------------------
# Courses
# --------------------------------------------------------------------------

def test_courses_import(client, token):
    import app as nexora
    res = client.post("/api/sync/courses", json={"courses": COURSES}, headers=auth(token))
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True and body["added"] == 1

    with nexora.app.app_context():
        row = nexora.SyncedCourse.query.filter_by(course_code="SYSC2004").first()
        assert row is not None
        assert row.course_name == "Object-Oriented Software Development"
        assert row.term == "Winter 2026"


def test_importing_the_same_courses_twice_changes_nothing(client, token):
    client.post("/api/sync/courses", json={"courses": COURSES}, headers=auth(token))
    second = client.post("/api/sync/courses", json={"courses": COURSES}, headers=auth(token))
    body = second.get_json()
    assert body["unchanged"] == 1 and body["added"] == 0


def test_courses_must_be_a_list(client, token):
    res = client.post("/api/sync/courses", json={"courses": "SYSC2004"}, headers=auth(token))
    assert res.status_code == 400


def test_a_removed_course_is_not_silently_re_added(client, token):
    """Removing a course writes a tombstone; the next sync must respect it."""
    import app as nexora
    from uuid import uuid4

    client.post("/api/sync/courses", json={"courses": COURSES}, headers=auth(token))
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="ext.student@carleton.ca").first()
        nexora.db.session.add(nexora.HiddenCourse(
            id=uuid4().hex, user_id=user.id, course_code="SYSC2004"))
        nexora.SyncedCourse.query.filter_by(user_id=user.id).delete()
        nexora.db.session.commit()

    client.post("/api/sync/courses", json={"courses": COURSES}, headers=auth(token))
    with nexora.app.app_context():
        assert nexora.SyncedCourse.query.filter_by(course_code="SYSC2004").count() == 0


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------

def test_content_import(client, token):
    import app as nexora
    res = client.post("/api/sync/content", json={"items": ITEMS}, headers=auth(token))
    assert res.status_code == 200
    assert res.get_json()["added"] == 1

    with nexora.app.app_context():
        row = nexora.CourseContent.query.filter_by(title="Lecture 12 - Polymorphism").first()
        assert row is not None
        assert row.course_full == "SYSC2004"
        assert row.module_name == "Week 9"
        assert "is-a relationship" in row.content_text


def test_reimporting_identical_content_is_a_no_op(client, token):
    client.post("/api/sync/content", json={"items": ITEMS}, headers=auth(token))
    second = client.post("/api/sync/content", json={"items": ITEMS}, headers=auth(token))
    assert second.get_json()["unchanged"] == 1


def test_changed_content_updates_and_is_announced(client, token):
    """A student should learn that a lecture changed, not silently get new text."""
    import app as nexora
    client.post("/api/sync/content", json={"items": ITEMS}, headers=auth(token))

    changed = [dict(ITEMS[0], content_text="Rewritten for the second edition.")]
    res = client.post("/api/sync/content", json={"items": changed}, headers=auth(token))
    assert res.get_json()["updated"] == 1

    with nexora.app.app_context():
        row = nexora.CourseContent.query.filter_by(source_uid="d2l:8801:topic:551").first()
        assert "second edition" in row.content_text
        assert nexora.AcademicUpdate.query.filter_by(update_type="content_updated").count() == 1


def test_content_for_a_removed_course_is_ignored(client, token):
    import app as nexora
    from uuid import uuid4

    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="ext.student@carleton.ca").first()
        nexora.db.session.add(nexora.HiddenCourse(
            id=uuid4().hex, user_id=user.id, course_code="SYSC2004"))
        nexora.db.session.commit()

    client.post("/api/sync/content", json={"items": ITEMS}, headers=auth(token))
    with nexora.app.app_context():
        assert nexora.CourseContent.query.count() == 0


def test_an_item_with_no_title_is_skipped(client, token):
    import app as nexora
    client.post("/api/sync/content", json={"items": [dict(ITEMS[0], title="  ")]},
                headers=auth(token))
    with nexora.app.app_context():
        assert nexora.CourseContent.query.count() == 0


def test_replace_courses_clears_before_importing(client, token):
    """Smart Sync replaces a course's content wholesale; stale items must go."""
    import app as nexora
    client.post("/api/sync/content", json={"items": ITEMS}, headers=auth(token))

    replacement = [dict(ITEMS[0], source_uid="d2l:8801:topic:999", title="Lecture 13 - Interfaces")]
    client.post("/api/sync/content",
                json={"items": replacement, "replace_courses": ["SYSC2004"]},
                headers=auth(token))

    with nexora.app.app_context():
        titles = {c.title for c in nexora.CourseContent.query.all()}
        assert titles == {"Lecture 13 - Interfaces"}
