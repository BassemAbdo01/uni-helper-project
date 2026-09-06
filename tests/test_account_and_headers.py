"""Two things a paid product cannot ship without.

**Getting out.** The privacy policy promised deletion by emailing a human, with
no SMTP configured behind it -- a promise that could not be kept. Deletion and
export are self-service now, and the tests below are mostly about the deleting
being both complete and hard to do by accident.

**Response headers.** There were none. Without X-Frame-Options any site can put
Nexora in an invisible iframe over its own buttons; without a CSP an injected
script tag can reach any host it likes.
"""

import json
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"


def signup(client, email="leaving@carleton.ca"):
    return client.post("/signup", data={"name": "Leaving Student", "email": email,
                                        "password": PASSWORD}, follow_redirects=False)


def seed_everything(email):
    """A row in every table a student can accumulate."""
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        nexora.db.session.add_all([
            nexora.Task(id=uuid4().hex, user_id=user.id, group_id="g", title="Essay",
                        base_title="Essay", course_full="BIOL1103", due_raw="2026-10-01"),
            nexora.CourseContent(id=uuid4().hex, user_id=user.id, course_full="BIOL1103",
                                 title="Lecture 1", source_uid=uuid4().hex),
            nexora.GradeEntry(id=uuid4().hex, user_id=user.id, course_full="BIOL1103",
                              name="Midterm", weight=30, score=70, max_score=100),
            nexora.CourseOutlineProfile(id=uuid4().hex, user_id=user.id,
                                        course_full="BIOL1103", grading_json="{}"),
            nexora.SyncedCourse(id=uuid4().hex, user_id=user.id, course_code="BIOL1103",
                                course_name="Biology", term="Fall 2026"),
            nexora.ClassBlock(id=uuid4().hex, user_id=user.id, label="Lecture",
                              weekday=0, start_min=600, end_min=660),
            nexora.StudySession(id=uuid4().hex, user_id=user.id, on_date="2026-09-10",
                                start_min=600, end_min=650, title="Essay"),
            nexora.StudyPreference(user_id=user.id),
            nexora.AcademicUpdate(id=uuid4().hex, user_id=user.id, course_full="BIOL1103",
                                  title="New announcement"),
        ])
        nexora.db.session.commit()


@pytest.fixture()
def student(client):
    signup(client)
    seed_everything("leaving@carleton.ca")
    return client


# ------------------------------------------------------------------ export

def test_everything_can_be_downloaded(student):
    res = student.get("/api/account/export")
    assert res.status_code == 200
    assert res.headers["Content-Type"].startswith("application/json")
    assert "attachment" in res.headers.get("Content-Disposition", "")
    body = json.loads(res.get_data(as_text=True))
    assert body["account"]["email"] == "leaving@carleton.ca"
    for table in ("Task", "GradeEntry", "SyncedCourse", "ClassBlock", "StudySession"):
        assert body["data"][table], f"{table} missing from the export"


def test_the_export_covers_every_table_a_student_fills(student):
    """Walked from the models rather than hand-listed, so a table added later
    is not quietly left out of an export somebody is relying on."""
    import app as nexora
    body = json.loads(student.get("/api/account/export").get_data(as_text=True))
    for model_name, _column in nexora._USER_TABLES:
        assert model_name in body["data"], f"{model_name} is not in the export"


def test_an_export_is_only_ever_your_own(client):
    signup(client, email="one@carleton.ca")
    seed_everything("one@carleton.ca")
    client.get("/logout")
    signup(client, email="two@carleton.ca")
    body = json.loads(client.get("/api/account/export").get_data(as_text=True))
    assert body["account"]["email"] == "two@carleton.ca"
    assert body["data"]["GradeEntry"] == []


def test_export_needs_a_login(client):
    assert client.get("/api/account/export").status_code in (302, 401)


# ---------------------------------------------------------------- deleting

def test_deleting_takes_the_password_and_the_word(student):
    assert student.delete("/api/account", json={}).status_code == 403
    assert student.delete("/api/account", json={"password": "wrong", "confirm": "DELETE"}).status_code == 403
    assert student.delete("/api/account", json={"password": PASSWORD}).status_code == 400
    assert student.delete("/api/account",
                          json={"password": PASSWORD, "confirm": "yes"}).status_code == 400


def test_a_wrong_password_deletes_nothing(student):
    student.delete("/api/account", json={"password": "wrong", "confirm": "DELETE"})
    import app as nexora
    with nexora.app.app_context():
        assert nexora.User.query.filter_by(email="leaving@carleton.ca").first() is not None


def test_deleting_removes_the_account_and_everything_under_it(student):
    import app as nexora
    with nexora.app.app_context():
        user_id = nexora.User.query.filter_by(email="leaving@carleton.ca").first().id

    res = student.delete("/api/account", json={"password": PASSWORD, "confirm": "DELETE"})
    assert res.status_code == 200

    with nexora.app.app_context():
        assert nexora.User.query.get(user_id) is None
        for model_name, column in nexora._USER_TABLES:
            model = getattr(nexora, model_name)
            left = model.query.filter_by(**{column: user_id}).count()
            assert left == 0, f"{left} {model_name} rows survived the delete"


def test_deleting_signs_you_out(student):
    student.delete("/api/account", json={"password": PASSWORD, "confirm": "DELETE"})
    assert student.get("/dashboard", follow_redirects=False).status_code in (301, 302)


def test_deleting_one_account_leaves_another_alone(client):
    signup(client, email="stays@carleton.ca")
    seed_everything("stays@carleton.ca")
    client.get("/logout")
    signup(client, email="goes@carleton.ca")
    seed_everything("goes@carleton.ca")
    client.delete("/api/account", json={"password": PASSWORD, "confirm": "DELETE"})
    import app as nexora
    with nexora.app.app_context():
        survivor = nexora.User.query.filter_by(email="stays@carleton.ca").first()
        assert survivor is not None
        assert nexora.GradeEntry.query.filter_by(user_id=survivor.id).count() == 1


def test_delete_needs_a_login(client):
    assert client.delete("/api/account", json={"password": PASSWORD, "confirm": "DELETE"}).status_code in (302, 401)


def test_the_profile_page_offers_both(student):
    body = student.get("/profile").get_data(as_text=True)
    assert "/api/account/export" in body
    assert 'id="deleteAccount"' in body


def test_the_privacy_policy_no_longer_promises_an_email_only_route(student):
    """It said "ask through the help page and it will be removed", with no SMTP
    behind it. A policy has to describe what the app does."""
    body = student.get("/privacy").get_data(as_text=True)
    assert "Export or delete" in body


# ------------------------------------------------------------ the headers

@pytest.mark.parametrize("path", ["/", "/login", "/privacy", "/support", "/healthz"])
def test_every_response_carries_the_headers(client, path):
    headers = client.get(path).headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Content-Security-Policy" in headers


def test_the_policy_allows_exactly_the_hosts_the_pages_use(client):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "https://fonts.gstatic.com" in csp        # the web fonts
    assert "https://cdn.jsdelivr.net" in csp         # three.js / DOMPurify
    assert "https://cdnjs.cloudflare.com" in csp
    # Course images come from whichever LMS the student uses, so that one host
    # cannot be named ahead of time.
    assert "img-src 'self' data: blob: https:" in csp


def test_hsts_is_sent_in_production(client):
    """conftest sets DATABASE_URL, so the suite runs the production branch --
    which is the branch that should carry HSTS."""
    import app as nexora
    assert nexora.IS_PRODUCTION is True
    assert client.get("/").headers["Strict-Transport-Security"].startswith("max-age=")


def test_hsts_is_gated_on_production(client, monkeypatch):
    """Sending it from a local http server pins localhost to https in the
    developer's own browser, which is a bad afternoon to debug."""
    import app as nexora
    monkeypatch.setattr(nexora, "IS_PRODUCTION", False)
    assert "Strict-Transport-Security" not in client.get("/").headers
