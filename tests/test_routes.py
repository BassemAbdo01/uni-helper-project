"""Request-level tests.

The suite had 36 tests and none of them made a request. That is exactly how a
NameError in the analyze route reached production: the name resolved at import
time and only failed inside a branch, so a green suite proved nothing about
whether the site worked. These tests exercise the paths a student actually
walks, plus the authorization boundary between two accounts.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="student@carleton.ca", password="correct-horse-battery", name="Test Student"):
    return client.post("/signup", data={"name": name, "email": email, "password": password},
                       follow_redirects=False)


def login(client, email="student@carleton.ca", password="correct-horse-battery"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


# --------------------------------------------------------------------------
# Pages render at all
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/login", "/signup", "/healthz"])
def test_public_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_every_page_route_responds_for_a_signed_in_user(client):
    """A crash in any page handler shows up here, which is what was missing."""
    signup(client)
    for path in ["/dashboard", "/profile", "/settings", "/calendar", "/study-room",
                 "/grade-calculator", "/connect-brightspace", "/feedback"]:
        response = client.get(path)
        assert response.status_code < 500, f"{path} returned {response.status_code}"


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/dashboard", "/profile", "/settings", "/calendar",
    "/study-room", "/grade-calculator", "/connect-brightspace",
])
def test_protected_pages_redirect_when_signed_out(client, path):
    response = client.get(path)
    assert response.status_code in (301, 302), f"{path} did not redirect"
    assert "/login" in response.headers.get("Location", "")


def test_signup_then_dashboard(client):
    assert signup(client).status_code in (301, 302)
    assert client.get("/dashboard").status_code == 200


def test_login_with_wrong_password_is_refused(client):
    signup(client)
    client.get("/logout")
    response = login(client, password="not-the-password")
    assert response.status_code == 200          # re-renders the form
    assert client.get("/dashboard").status_code in (301, 302)


def test_logout_ends_the_session(client):
    signup(client)
    client.get("/logout")
    assert client.get("/dashboard").status_code in (301, 302)


# --------------------------------------------------------------------------
# Password and email policy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("password", ["a", "short", "1234567"])
def test_short_passwords_are_rejected(client, password):
    response = signup(client, password=password)
    assert response.status_code == 200                     # form re-rendered
    assert b"at least 8 characters" in response.data
    assert client.get("/dashboard").status_code in (301, 302)   # no account made


def test_malformed_email_is_rejected(client):
    response = signup(client, email="not-an-email")
    assert response.status_code == 200
    assert client.get("/dashboard").status_code in (301, 302)


def test_duplicate_email_is_refused(client):
    signup(client)
    client.get("/logout")
    response = signup(client, name="Impostor")
    assert b"already exists" in response.data


# --------------------------------------------------------------------------
# Authorization boundary between accounts
# --------------------------------------------------------------------------

def test_one_student_cannot_read_another_students_courses(client):
    import app as nexora
    from app import db

    signup(client, email="alice@carleton.ca")
    with nexora.app.app_context():
        alice = nexora.User.query.filter_by(email="alice@carleton.ca").first()
        db.session.add(nexora.GradeEntry(
            id="alice-secret-row", user_id=alice.id, course_full="SYSC 1006",
            name="Midterm", weight=30.0, score=91.0))
        db.session.commit()

    client.get("/logout")
    signup(client, email="mallory@carleton.ca", name="Mallory")

    body = client.get("/api/course/SYSC%201006/grades").get_data(as_text=True)
    assert "91" not in body, "another student's mark leaked into this account"


def test_extension_endpoints_reject_a_bad_token(client):
    response = client.get("/api/sync/status", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


def test_extension_endpoints_reject_a_missing_token(client):
    assert client.get("/api/sync/status").status_code == 401


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------

def test_unknown_page_returns_a_real_404_page(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert b"does not exist" in response.data


def test_unknown_api_path_returns_json_not_html(client):
    response = client.get("/api/no-such-endpoint")
    assert response.status_code == 404
    assert response.get_json()["error"]


def test_healthz_reports_the_database(client):
    payload = client.get("/healthz").get_json()
    assert payload["status"] == "ok"
    assert payload["database"] == "ok"


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

def test_login_is_rate_limited(rate_limited_client):
    codes = [rate_limited_client.post("/login", data={"email": "a@b.com", "password": "x"}).status_code
             for _ in range(14)]
    assert 429 in codes, "login accepted unlimited password attempts"


def test_rate_limited_api_call_returns_json(rate_limited_client):
    signup(rate_limited_client)
    codes = []
    for _ in range(60):
        response = rate_limited_client.post("/api/analyze-outline", json={"text": "Final exam 100%"})
        codes.append(response.status_code)
        if response.status_code == 429:
            assert response.get_json()["error"]
            break
    assert 429 in codes, "AI endpoint accepted unlimited requests"


def test_healthz_is_exempt_from_limits(rate_limited_client):
    codes = [rate_limited_client.get("/healthz").status_code for _ in range(40)]
    assert 429 not in codes, "monitoring probe got rate limited"


# --------------------------------------------------------------------------
# Production configuration
# --------------------------------------------------------------------------

def test_session_cookie_is_hardened(client):
    import app as nexora
    assert nexora.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert nexora.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert nexora.app.config["PERMANENT_SESSION_LIFETIME"].days == 14


def test_production_refuses_to_boot_without_a_secret_key():
    """The fallback key is public in the repo, so serving with it is worse than
    not serving at all."""
    import subprocess
    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env.pop("SECRET_KEY", None)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run([sys.executable, "-c", "import app"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode != 0, "app booted in production with no SECRET_KEY"
    assert "SECRET_KEY is not set" in result.stderr
