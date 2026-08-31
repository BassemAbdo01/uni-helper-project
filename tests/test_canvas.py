"""Canvas integration tests.

Canvas is reached over the network, so the only honest way to test the sync is
to replace the HTTP layer and assert on what reaches the database. The point
being proved is that Canvas data lands in the *same* tables the Brightspace
path writes to -- SyncedCourse, CourseContent, Task, AcademicUpdate -- because
that is what makes the dashboard, calendar and study tools work identically
whichever LMS a student is on.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="canvas.student@carleton.ca", password="correct-horse-battery"):
    return client.post("/signup", data={"name": "Canvas Student", "email": email,
                                        "password": password}, follow_redirects=False)


# --------------------------------------------------------------------------
# The address a student pastes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("canvas.school.edu", "https://canvas.school.edu"),
    ("https://canvas.school.edu", "https://canvas.school.edu"),
    ("https://canvas.school.edu/", "https://canvas.school.edu"),
    ("https://canvas.school.edu/courses/123", "https://canvas.school.edu"),
    ("HTTPS://Canvas.School.EDU/login", "https://canvas.school.edu"),
])
def test_base_url_accepts_whatever_a_student_pastes(raw, expected):
    from app import _canvas_base_url
    assert _canvas_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "notahost", "http://canvas.school.edu", "ftp://x.y"])
def test_base_url_rejects_what_it_cannot_use(raw):
    """Plain http is refused too: a token must never cross the network in clear."""
    from app import _canvas_base_url
    assert _canvas_base_url(raw) == ""


# --------------------------------------------------------------------------
# The token at rest
# --------------------------------------------------------------------------

def test_token_round_trips_through_encryption(client):
    from app import _canvas_encrypt, _canvas_decrypt
    token = "1234~aBcDeFgHiJkLmNoPqRsTuVwXyZ0987654321"
    blob = _canvas_encrypt(token)
    assert token not in blob, "the stored blob must not contain the token"
    assert _canvas_decrypt(blob) == token


def test_stored_token_is_not_readable_from_the_row(client, monkeypatch):
    """Reading the table must not be enough to use the credential."""
    import app as nexora
    signup(client)

    monkeypatch.setattr(nexora, "_canvas_request",
                        lambda base, token, path, params=None: ({"id": 7, "name": "A Student"}, ""))
    res = client.post("/api/canvas/connect", json={
        "base_url": "canvas.school.edu", "token": "1234~secret-token-value-goes-here"})
    assert res.status_code == 200

    with nexora.app.app_context():
        row = nexora.CanvasAccount.query.first()
        assert row is not None
        assert "secret-token-value" not in row.token_encrypted


# --------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------

def test_connect_rejects_a_bad_address(client):
    signup(client)
    res = client.post("/api/canvas/connect", json={"base_url": "nonsense", "token": "x" * 40})
    assert res.status_code == 400
    assert "web address" in res.get_json()["error"].lower()


def test_connect_rejects_an_obviously_short_token(client):
    signup(client)
    res = client.post("/api/canvas/connect", json={"base_url": "canvas.school.edu", "token": "abc"})
    assert res.status_code == 400


def test_connect_explains_a_rejected_token(client, monkeypatch):
    """A 401 from Canvas should tell the student to make a new token, not show a code."""
    import urllib.error
    import app as nexora
    signup(client)

    def unauthorized(base, token, path, params=None):
        raise urllib.error.HTTPError(base, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(nexora, "_canvas_request", unauthorized)
    res = client.post("/api/canvas/connect", json={
        "base_url": "canvas.school.edu", "token": "1234~" + "x" * 40})
    assert res.status_code == 400
    assert "new access token" in res.get_json()["error"].lower()


def test_sync_requires_a_connection_first(client):
    signup(client)
    assert client.post("/api/canvas/sync").status_code == 400


# --------------------------------------------------------------------------
# The sync itself
# --------------------------------------------------------------------------

CANVAS_FIXTURE = {
    "courses": [{
        "id": 101,
        "name": "Object-Oriented Software Development",
        "course_code": "SYSC 2004",
        "term": {"name": "Winter 2026"},
        "image_download_url": "https://canvas.school.edu/img/101.png",
    }],
    "courses/101/assignments": [
        {"id": 11, "name": "Lab 6 - Inheritance", "due_at": "2026-09-14T03:59:00Z",
         "html_url": "https://canvas.school.edu/courses/101/assignments/11",
         "description": "<p>Write a <b>subclass</b>.</p><script>alert(1)</script>"},
        {"id": 12, "name": "Reading (no due date)", "due_at": None,
         "html_url": "https://canvas.school.edu/courses/101/assignments/12",
         "description": ""},
    ],
    "courses/101/discussion_topics": [
        {"id": 21, "title": "Midterm 2 solutions posted", "message": "<p>See files.</p>"},
    ],
    "courses/101/modules": [
        {"name": "Week 9", "items": [
            {"id": 31, "title": "Lecture 12 - Polymorphism", "type": "Page",
             "html_url": "https://canvas.school.edu/courses/101/pages/lecture-12"},
            {"id": 32, "title": "Skip me", "type": "SubHeader"},
        ]},
    ],
}


@pytest.fixture()
def canvas_client(client, monkeypatch):
    """A signed-in student with Canvas connected and the network replaced."""
    import app as nexora
    signup(client)

    monkeypatch.setattr(nexora, "_canvas_request",
                        lambda base, token, path, params=None: ({"id": 7, "name": "A Student"}, ""))
    monkeypatch.setattr(nexora, "_canvas_list",
                        lambda base, token, path, params=None: CANVAS_FIXTURE.get(path, []))

    res = client.post("/api/canvas/connect", json={
        "base_url": "canvas.school.edu", "token": "1234~" + "x" * 40})
    assert res.status_code == 200
    return client


def test_sync_writes_into_the_same_tables_brightspace_uses(canvas_client):
    import app as nexora

    res = canvas_client.post("/api/canvas/sync")
    assert res.status_code == 200, res.get_data(as_text=True)
    summary = res.get_json()
    assert summary["courses"] == 1

    with nexora.app.app_context():
        course = nexora.SyncedCourse.query.filter_by(course_code="SYSC2004").first()
        assert course is not None
        assert course.course_name == "Object-Oriented Software Development"
        assert course.term == "Winter 2026"

        # an assignment with a due date becomes a Task, so deadlines and the
        # calendar work without a Canvas-specific code path
        task = nexora.Task.query.filter_by(course_full="SYSC2004").first()
        assert task is not None
        assert task.due_raw == "2026-09-14"
        assert task.source == "canvas"

        # one without a due date is content only, never a phantom deadline
        assert nexora.Task.query.count() == 1

        titles = {c.title for c in nexora.CourseContent.query.all()}
        assert "Lab 6 - Inheritance" in titles
        assert "Lecture 12 - Polymorphism" in titles
        assert "Skip me" not in titles, "module subheaders are not content"

        update = nexora.AcademicUpdate.query.filter_by(update_type="announcement").first()
        assert update is not None
        assert update.title == "Midterm 2 solutions posted"


def test_assignment_html_is_stored_as_text_without_scripts(canvas_client):
    import app as nexora
    canvas_client.post("/api/canvas/sync")
    with nexora.app.app_context():
        row = nexora.CourseContent.query.filter_by(title="Lab 6 - Inheritance").first()
        assert row is not None
        assert "<b>" not in row.content_text
        assert "alert(1)" not in row.content_text, "script contents must not be stored"
        assert "subclass" in row.content_text


def test_syncing_twice_does_not_duplicate_anything(canvas_client):
    """Canvas ids are stable, so a second sync updates rather than piles up."""
    import app as nexora
    canvas_client.post("/api/canvas/sync")
    canvas_client.post("/api/canvas/sync")
    with nexora.app.app_context():
        assert nexora.Task.query.count() == 1
        assert nexora.SyncedCourse.query.count() == 1
        assert nexora.AcademicUpdate.query.filter_by(update_type="announcement").count() == 1


def test_the_dashboard_shows_canvas_courses(canvas_client):
    """The real proof: data imported from Canvas reaches the page unchanged."""
    canvas_client.post("/api/canvas/sync")
    body = canvas_client.get("/dashboard").get_data(as_text=True)
    assert "SYSC2004" in body
    assert "Lab 6 - Inheritance" in body


def test_disconnect_forgets_the_token_but_keeps_the_coursework(canvas_client):
    import app as nexora
    canvas_client.post("/api/canvas/sync")
    assert canvas_client.post("/api/canvas/disconnect").status_code == 200
    with nexora.app.app_context():
        assert nexora.CanvasAccount.query.count() == 0
        assert nexora.SyncedCourse.query.count() == 1, "imported courses should survive"


def test_one_student_cannot_see_another_students_canvas(client, monkeypatch):
    import app as nexora
    signup(client, email="first@carleton.ca")
    monkeypatch.setattr(nexora, "_canvas_request",
                        lambda base, token, path, params=None: ({"id": 7, "name": "First"}, ""))
    client.post("/api/canvas/connect", json={"base_url": "canvas.school.edu",
                                             "token": "1234~" + "x" * 40})
    client.get("/logout")

    signup(client, email="second@carleton.ca")
    page = client.get("/connect-canvas").get_data(as_text=True)
    assert "canvas.school.edu" not in page, "a second student must not see the first's Canvas"
    assert client.post("/api/canvas/sync").status_code == 400
