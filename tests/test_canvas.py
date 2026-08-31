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


# --------------------------------------------------------------------------
# Grades
#
# This is the part Canvas does better than the Brightspace path can: the
# weighting is published rather than inferred from an outline by a model, so it
# has to be arithmetically exact.
# --------------------------------------------------------------------------

WEIGHTED_GROUPS = [
    {"name": "Assignments", "group_weight": 20, "assignments": [
        {"id": 1, "name": "A1", "points_possible": 10,
         "submission": {"score": 9}},
        {"id": 2, "name": "A2", "points_possible": 30,
         "submission": {"score": 24}},
    ]},
    {"name": "Final Exam", "group_weight": 80, "assignments": [
        {"id": 3, "name": "Final", "points_possible": 100, "submission": {"score": None}},
    ]},
]


def test_group_weight_is_split_by_what_each_piece_is_out_of():
    """A1 out of 10 and A2 out of 30 share the group's 20%: 5 and 15."""
    from app import _canvas_grade_rows
    rows = {r["name"]: r for r in _canvas_grade_rows("SYSC2004", WEIGHTED_GROUPS, weighted=True)}
    assert rows["A1"]["weight"] == pytest.approx(5.0)
    assert rows["A2"]["weight"] == pytest.approx(15.0)
    assert rows["Final"]["weight"] == pytest.approx(80.0)
    assert sum(r["weight"] for r in rows.values()) == pytest.approx(100.0)


def test_scores_and_totals_come_across_verbatim():
    from app import _canvas_grade_rows
    rows = {r["name"]: r for r in _canvas_grade_rows("SYSC2004", WEIGHTED_GROUPS, weighted=True)}
    assert rows["A1"]["score"] == 9 and rows["A1"]["max_score"] == 10
    assert rows["A2"]["score"] == 24 and rows["A2"]["max_score"] == 30


def test_unmarked_work_has_no_score_rather_than_a_zero():
    """A zero and 'not marked yet' are different facts; conflating them would
    tell a student they are failing a final they have not sat."""
    from app import _canvas_grade_rows
    rows = {r["name"]: r for r in _canvas_grade_rows("SYSC2004", WEIGHTED_GROUPS, weighted=True)}
    assert rows["Final"]["score"] is None


def test_an_unweighted_course_falls_back_to_points():
    """Without group weights Canvas marks on total points, which is the same as
    every point carrying equal weight."""
    from app import _canvas_grade_rows
    rows = {r["name"]: r for r in _canvas_grade_rows("SYSC2004", WEIGHTED_GROUPS, weighted=False)}
    assert rows["A1"]["weight"] == pytest.approx(10)
    assert rows["A2"]["weight"] == pytest.approx(30)
    assert rows["Final"]["weight"] == pytest.approx(100)


def test_a_group_of_zero_point_work_cannot_divide_by_zero():
    from app import _canvas_grade_rows
    groups = [{"name": "Surveys", "group_weight": 10, "assignments": [
        {"id": 9, "name": "Survey", "points_possible": 0, "submission": None}]}]
    rows = _canvas_grade_rows("SYSC2004", groups, weighted=True)
    assert rows[0]["weight"] == 0.0


def test_grades_reach_the_database_and_survive_a_resync(canvas_client, monkeypatch):
    import app as nexora

    fixture = dict(CANVAS_FIXTURE)
    fixture["courses/101/assignment_groups"] = WEIGHTED_GROUPS
    monkeypatch.setattr(nexora, "_canvas_list",
                        lambda base, token, path, params=None: fixture.get(path, []))
    monkeypatch.setattr(nexora, "_canvas_request",
                        lambda base, token, path, params=None: ({"id": 7, "name": "A Student"}, ""))

    canvas_client.post("/api/canvas/sync")
    with nexora.app.app_context():
        rows = nexora.GradeEntry.query.filter_by(course_full="SYSC2004").all()
        assert len(rows) == 3
        by_name = {r.name: r for r in rows}
        assert by_name["A1"].score == 9
        assert by_name["Final"].score is None

    canvas_client.post("/api/canvas/sync")
    with nexora.app.app_context():
        assert nexora.GradeEntry.query.filter_by(course_full="SYSC2004").count() == 3


def test_a_mark_the_student_typed_is_never_overwritten(canvas_client, monkeypatch):
    """Canvas owns the rows it created. Anything entered by hand is the
    student's own record and stays untouched."""
    import app as nexora
    from uuid import uuid4

    with nexora.app.app_context():
        user = nexora.User.query.first()
        nexora.db.session.add(nexora.GradeEntry(
            id=uuid4().hex, user_id=user.id, course_full="SYSC2004",
            name="Participation (mine)", weight=5, score=100, max_score=100,
            category="Manual", notes=""))
        nexora.db.session.commit()

    fixture = dict(CANVAS_FIXTURE)
    fixture["courses/101/assignment_groups"] = WEIGHTED_GROUPS
    monkeypatch.setattr(nexora, "_canvas_list",
                        lambda base, token, path, params=None: fixture.get(path, []))
    canvas_client.post("/api/canvas/sync")

    with nexora.app.app_context():
        mine = nexora.GradeEntry.query.filter_by(name="Participation (mine)").first()
        assert mine is not None and mine.score == 100


# --------------------------------------------------------------------------
# Automatic refresh
#
# Brightspace refreshes on a Chrome alarm inside the extension. Canvas has no
# scheduler behind it, so the refresh rides on the student opening a page --
# which makes "does not fire when it should not" the property that matters.
# --------------------------------------------------------------------------

def test_status_reports_no_connection_for_a_student_without_canvas(client):
    signup(client, email="nocanvas@carleton.ca")
    assert client.get("/api/canvas/status").get_json() == {"connected": False}


def test_a_never_synced_connection_counts_as_stale(canvas_client):
    body = canvas_client.get("/api/canvas/status").get_json()
    assert body["connected"] is True
    assert body["stale"] is True
    assert body["last_sync"] is None


def test_a_fresh_connection_is_not_stale_and_auto_sync_declines(canvas_client):
    """Ten page loads must not mean ten syncs."""
    canvas_client.post("/api/canvas/sync")

    status = canvas_client.get("/api/canvas/status").get_json()
    assert status["stale"] is False

    result = canvas_client.post("/api/canvas/auto-sync").get_json()
    assert result == {"skipped": "fresh"}


def test_auto_sync_runs_once_the_data_has_aged(canvas_client):
    import app as nexora
    from datetime import datetime, timedelta

    canvas_client.post("/api/canvas/sync")
    with nexora.app.app_context():
        account = nexora.CanvasAccount.query.first()
        account.last_sync = datetime.utcnow() - timedelta(hours=48)
        nexora.db.session.commit()

    assert canvas_client.get("/api/canvas/status").get_json()["stale"] is True
    assert canvas_client.post("/api/canvas/auto-sync").get_json().get("ok") is True


def test_a_background_failure_stays_in_the_background(canvas_client, monkeypatch):
    """Nobody asked for this sync, so its failure must not become an error
    message in front of a student who was doing something else."""
    import app as nexora

    def explode(user):
        raise RuntimeError("Canvas is down")

    monkeypatch.setattr(nexora, "canvas_sync", explode)
    res = canvas_client.post("/api/canvas/auto-sync")
    assert res.status_code == 200
    assert res.get_json() == {"skipped": "failed"}

    # but it is recorded, so the Canvas page can explain itself
    with nexora.app.app_context():
        assert nexora.CanvasAccount.query.first().last_error


def test_auto_sync_does_nothing_without_a_connection(client):
    signup(client, email="nocanvas2@carleton.ca")
    assert client.post("/api/canvas/auto-sync").get_json() == {"skipped": "not-connected"}


def test_canvas_endpoints_need_a_login(client):
    for path in ("/api/canvas/status",):
        assert client.get(path).status_code == 302
    for path in ("/api/canvas/sync", "/api/canvas/auto-sync", "/api/canvas/connect"):
        assert client.post(path).status_code == 302


# --------------------------------------------------------------------------
# Lecture text
#
# Module items were first imported as a title and a link only, which left the
# study tools, the tutor and outline analysis dead for Canvas students on
# anything that was not an assignment description -- and the failure told them
# to install a browser extension that Canvas does not use.
# --------------------------------------------------------------------------

PAGE_MODULES = [{"name": "Week 9", "items": [
    {"id": 41, "title": "Lecture 12 - Polymorphism", "type": "Page",
     "page_url": "lecture-12-polymorphism",
     "html_url": "https://canvas.school.edu/courses/101/pages/lecture-12-polymorphism"},
]}]

PAGE_BODY = ("<h2>Polymorphism</h2><p>A reference of the superclass type may refer to an "
             "object of any subclass type, and the method that runs is chosen from the "
             "object's run-time type rather than the reference. This is dynamic dispatch, "
             "and it is what lets a method written against BankAccount keep working for "
             "every subclass added later.</p><script>alert('x')</script>")


@pytest.fixture()
def canvas_pages_client(client, monkeypatch):
    import app as nexora
    signup(client)

    fixture = dict(CANVAS_FIXTURE)
    fixture["courses/101/modules"] = PAGE_MODULES

    def fake_request(base, token, path, params=None):
        if path.startswith("courses/101/pages/"):
            return {"body": PAGE_BODY}, ""
        return {"id": 7, "name": "A Student"}, ""

    monkeypatch.setattr(nexora, "_canvas_request", fake_request)
    monkeypatch.setattr(nexora, "_canvas_list",
                        lambda base, token, path, params=None: fixture.get(path, []))

    assert client.post("/api/canvas/connect", json={
        "base_url": "canvas.school.edu", "token": "1234~" + "x" * 40}).status_code == 200
    return client


def test_a_page_arrives_with_its_text_not_just_its_title(canvas_pages_client):
    import app as nexora
    canvas_pages_client.post("/api/canvas/sync")
    with nexora.app.app_context():
        row = nexora.CourseContent.query.filter_by(title="Lecture 12 - Polymorphism").first()
        assert row is not None
        assert "dynamic dispatch" in row.content_text
        assert "<h2>" not in row.content_text
        assert "alert(" not in row.content_text


def test_study_tools_work_on_a_canvas_lecture(canvas_pages_client, monkeypatch):
    """The whole point: no 409, and no instruction to install an extension."""
    import app as nexora
    canvas_pages_client.post("/api/canvas/sync")

    monkeypatch.setattr(nexora, "_gemini_json",
                        lambda *a, **k: {"title": "Flashcards", "cards": [
                            {"front": "What is dynamic dispatch?", "back": "Method chosen at run time.",
                             "source": "Lecture 12"}]})

    with nexora.app.app_context():
        uid = nexora.CourseContent.query.filter_by(
            title="Lecture 12 - Polymorphism").first().source_uid

    res = canvas_pages_client.post("/api/course/SYSC2004/study-tools/generate",
                                   json={"source_uid": uid, "artifact_type": "flashcards"})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["artifact"]["data"]["cards"]


def test_text_is_fetched_on_demand_for_a_row_imported_without_it(canvas_pages_client, monkeypatch):
    """Rows imported before page bodies were read must still become usable
    rather than staying permanently empty."""
    import app as nexora
    canvas_pages_client.post("/api/canvas/sync")

    with nexora.app.app_context():
        row = nexora.CourseContent.query.filter_by(title="Lecture 12 - Polymorphism").first()
        row.content_text = ""          # as an older sync would have left it
        nexora.db.session.commit()
        uid, user_id = row.source_uid, row.user_id

    with nexora.app.app_context():
        user = nexora.User.query.get(user_id)
        row = nexora.CourseContent.query.filter_by(source_uid=uid).first()
        assert nexora._canvas_fill_content(user, row) is True
        assert "dynamic dispatch" in nexora.CourseContent.query.filter_by(
            source_uid=uid).first().content_text


def test_filling_content_declines_for_a_brightspace_row(client):
    """Brightspace still has to go through the extension; this must not claim
    to have handled it."""
    import app as nexora
    from uuid import uuid4
    signup(client, email="bs@carleton.ca")

    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="bs@carleton.ca").first()
        row = nexora.CourseContent(id=uuid4().hex, user_id=user.id, course_full="SYSC2004",
                                   title="A Brightspace lecture", source_uid="d2l:8801:topic:5",
                                   content_text="")
        nexora.db.session.add(row)
        nexora.db.session.commit()
        assert nexora._canvas_fill_content(user, row) is False


# --------------------------------------------------------------------------
# Session-fetched Canvas
#
# Some universities forbid students from generating access tokens: "Your Canvas
# administrators have chosen to limit your ability to generate your own access
# token." For those students the server cannot reach Canvas at all, so the
# extension fetches through the browser session and posts the payloads here.
# The point of these tests is that the *interpretation* is the same code --
# a course imported this way is indistinguishable from one imported with a token.
# --------------------------------------------------------------------------

def extension_token(client):
    return client.post("/api/sync/extension-key").get_json()["token"]


def ingest_body():
    """Exactly the paths the extension fetches, keyed the way the server asks."""
    # The course declares weighted assignment groups, so this exercises the
    # weighting arithmetic rather than the points fallback.
    weighted_course = [dict(CANVAS_FIXTURE["courses"][0], apply_assignment_group_weights=True)]
    return {
        "base_url": "https://canvas.school.edu",
        "pages": {
            "courses": weighted_course,
            "courses/101/assignments": CANVAS_FIXTURE["courses/101/assignments"],
            "courses/101/discussion_topics": CANVAS_FIXTURE["courses/101/discussion_topics"],
            "courses/101/modules": CANVAS_FIXTURE["courses/101/modules"],
            "courses/101/assignment_groups": WEIGHTED_GROUPS,
        },
    }


def test_ingest_needs_the_extension_key(client):
    signup(client, email="ingest.anon@carleton.ca")
    assert client.post("/api/canvas/ingest", json=ingest_body()).status_code == 401


def test_ingest_rejects_a_bad_address(client):
    signup(client, email="ingest.bad@carleton.ca")
    token = extension_token(client)
    body = dict(ingest_body(), base_url="not-a-host")
    res = client.post("/api/canvas/ingest", json=body,
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 400


def test_ingest_rejects_a_payload_that_is_not_paths(client):
    signup(client, email="ingest.shape@carleton.ca")
    token = extension_token(client)
    res = client.post("/api/canvas/ingest",
                      json={"base_url": "canvas.school.edu", "pages": []},
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 400


def test_a_session_import_lands_exactly_like_a_token_import(client):
    """The whole design: the browser is transport, the server is the parser, so
    a student whose university blocks tokens gets the same result."""
    import app as nexora
    signup(client, email="session@carleton.ca")
    token = extension_token(client)

    res = client.post("/api/canvas/ingest", json=ingest_body(),
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.get_data(as_text=True)

    with nexora.app.app_context():
        course = nexora.SyncedCourse.query.filter_by(course_code="SYSC2004").first()
        assert course is not None and course.term == "Winter 2026"

        task = nexora.Task.query.filter_by(course_full="SYSC2004").first()
        assert task is not None and task.due_raw == "2026-09-14"
        assert task.source == "canvas"

        titles = {c.title for c in nexora.CourseContent.query.all()}
        assert "Lab 6 - Inheritance" in titles
        assert "Skip me" not in titles

        assert nexora.AcademicUpdate.query.filter_by(update_type="announcement").count() == 1

        # the grade weighting is the same arithmetic, not a second copy
        grades = {g.name: g for g in nexora.GradeEntry.query.all()}
        assert grades["A1"].weight == pytest.approx(5.0)
        assert grades["Final"].score is None


def test_a_session_import_needs_no_stored_token(client):
    """There is no CanvasAccount here, and there must not need to be."""
    import app as nexora
    signup(client, email="notoken@carleton.ca")
    token = extension_token(client)

    client.post("/api/canvas/ingest", json=ingest_body(),
                headers={"Authorization": f"Bearer {token}"})

    with nexora.app.app_context():
        assert nexora.CanvasAccount.query.count() == 0
        assert nexora.SyncedCourse.query.count() == 1


def test_one_students_key_cannot_import_into_another_account(client):
    import app as nexora
    signup(client, email="first.canvas@carleton.ca")
    first = extension_token(client)
    client.get("/logout")
    signup(client, email="second.canvas@carleton.ca")
    client.get("/logout")

    client.post("/api/canvas/ingest", json=ingest_body(),
                headers={"Authorization": f"Bearer {first}"})
    with nexora.app.app_context():
        rows = nexora.SyncedCourse.query.all()
        assert len(rows) == 1
        assert nexora.User.query.get(rows[0].user_id).email == "first.canvas@carleton.ca"
