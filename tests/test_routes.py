"""Request-level tests.

The suite had 36 tests and none of them made a request. That is exactly how a
NameError in the analyze route reached production: the name resolved at import
time and only failed inside a branch, so a green suite proved nothing about
whether the site worked. These tests exercise the paths a student actually
walks, plus the authorization boundary between two accounts.
"""

import os
import sys
from uuid import uuid4

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
    assert nexora.app.config["PERMANENT_SESSION_LIFETIME"].days == 30
    assert nexora.app.config["REMEMBER_COOKIE_DURATION"].days == 30
    assert nexora.app.config["REMEMBER_COOKIE_HTTPONLY"] is True


def test_signing_in_actually_sets_a_remember_cookie(client):
    """The config said 14 days and the login code never opted in.

    `login_user(user)` was called with no `remember`, so Flask-Login set no
    remember cookie at all, and `session.permanent` was never set, so the
    session cookie died with the browser. Asserting the CONFIG passed happily
    the whole time. Reported as being logged out at least once a day.
    """
    email = f"stay{uuid4().hex[:6]}@carleton.ca"
    res = client.post("/signup", data={"name": "S", "email": email,
                                       "password": "nexorademo1"})
    jar = {c.key: c for c in client.cookie_jar} if hasattr(client, "cookie_jar") else {}
    if not jar:                       # werkzeug >= 2.3 keeps cookies on the client
        jar = {c.key: c for c in client._cookies.values()} if hasattr(client, "_cookies") else {}
    names = set(jar) or {h.split("=")[0] for _, h in res.headers if _ == "Set-Cookie"}
    assert any("remember" in n.lower() for n in names), f"no remember cookie in {names}"


def test_the_session_survives_the_browser_closing(client):
    """A non-permanent session cookie is dropped when the browser closes, which
    is most of what "logged out every day" was."""
    import app as nexora
    client.post("/signup", data={"name": "S", "email": f"perm{uuid4().hex[:6]}@carleton.ca",
                                 "password": "nexorademo1"})
    with client.session_transaction() as sess:
        assert sess.permanent is True


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


# --------------------------------------------------------------------------
# Password reset
# --------------------------------------------------------------------------

@pytest.fixture()
def mailbox(monkeypatch):
    """Capture outgoing mail instead of sending it."""
    import app as nexora
    sent = []
    monkeypatch.setattr(nexora, "email_is_configured", lambda: True)
    monkeypatch.setattr(nexora, "send_email",
                        lambda to, subject, body: sent.append({"to": to, "subject": subject, "body": body}) or True)
    return sent


def _reset_token_from(mailbox):
    link = next(w for w in mailbox[-1]["body"].split() if "/reset-password/" in w)
    return link.rsplit("/", 1)[1]


def test_reset_is_refused_honestly_when_email_is_not_configured(client):
    import app as nexora
    assert not nexora.email_is_configured()          # no SMTP env in tests
    response = client.post("/forgot-password", data={"email": "student@carleton.ca"})
    assert response.status_code == 200
    assert b"isn&#39;t switched on yet" in response.data or b"isn't switched on yet" in response.data
    # No input to type an address into, because nothing could be sent to it.
    assert b'name="email"' not in response.data


def test_reset_link_lets_a_student_set_a_new_password(client, mailbox):
    signup(client)
    client.get("/logout")

    client.post("/forgot-password", data={"email": "student@carleton.ca"})
    token = _reset_token_from(mailbox)

    assert client.get(f"/reset-password/{token}").status_code == 200
    assert client.post(f"/reset-password/{token}",
                       data={"password": "a-brand-new-password"}).status_code in (301, 302)
    assert client.get("/dashboard").status_code == 200     # signed in by the reset

    client.get("/logout")
    login(client, password="correct-horse-battery")        # the old one
    assert client.get("/dashboard").status_code in (301, 302)
    login(client, password="a-brand-new-password")
    assert client.get("/dashboard").status_code == 200


def test_reset_response_does_not_reveal_whether_an_account_exists(client, mailbox):
    signup(client)
    client.get("/logout")
    known = client.post("/forgot-password", data={"email": "student@carleton.ca"}).data
    unknown = client.post("/forgot-password", data={"email": "nobody@nowhere.test"}).data
    assert known == unknown, "the reply differs, so this form enumerates accounts"


def test_reset_token_is_single_use(client, mailbox):
    signup(client)
    client.get("/logout")
    client.post("/forgot-password", data={"email": "student@carleton.ca"})
    token = _reset_token_from(mailbox)
    client.post(f"/reset-password/{token}", data={"password": "a-brand-new-password"})
    assert client.get(f"/reset-password/{token}").status_code == 400


def test_reset_token_is_stored_hashed(client, mailbox):
    import app as nexora
    signup(client)
    client.get("/logout")
    client.post("/forgot-password", data={"email": "student@carleton.ca"})
    token = _reset_token_from(mailbox)
    with nexora.app.app_context():
        rows = nexora.PasswordResetToken.query.all()
        assert rows
        assert all(token not in (r.token_hash or "") for r in rows)


def test_expired_reset_token_is_rejected(client, mailbox):
    import app as nexora
    from app import db
    from datetime import datetime, timedelta
    signup(client)
    client.get("/logout")
    client.post("/forgot-password", data={"email": "student@carleton.ca"})
    token = _reset_token_from(mailbox)
    with nexora.app.app_context():
        row = nexora.PasswordResetToken.query.first()
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
    assert client.get(f"/reset-password/{token}").status_code == 400


def test_reset_rejects_a_short_password(client, mailbox):
    signup(client)
    client.get("/logout")
    client.post("/forgot-password", data={"email": "student@carleton.ca"})
    token = _reset_token_from(mailbox)
    response = client.post(f"/reset-password/{token}", data={"password": "abc"})
    assert b"at least 8 characters" in response.data


# --------------------------------------------------------------------------
# Gemini degradation
# --------------------------------------------------------------------------

def test_an_unusable_fallback_is_refused_rather_than_shown():
    """A near-empty policy with a Confirm button under it is worse than an error."""
    import app as nexora
    assert nexora._fallback_is_usable({"components": [{"name": "Final", "weight": 100}]})
    assert nexora._fallback_is_usable({"grading_options": [{"option_name": "A"}]})
    assert not nexora._fallback_is_usable({"components": []})
    assert not nexora._fallback_is_usable({"components": [{"name": "Final", "weight": 12}]})
    assert not nexora._fallback_is_usable({})


def test_quota_errors_are_worded_differently_from_faults():
    import app as nexora
    assert nexora._looks_like_quota_error(RuntimeError("model: HTTP 429: quota exceeded"))
    assert nexora._looks_like_quota_error(RuntimeError("rate limit reached"))
    assert not nexora._looks_like_quota_error(RuntimeError("connection reset"))


def test_a_model_that_returns_429_is_parked_and_skipped():
    import app as nexora
    nexora._gemini_model_cooldowns.clear()
    chain = ["model-a", "model-b", "model-c"]
    assert nexora._usable_gemini_models(chain) == chain
    nexora._mark_gemini_model_unavailable("model-a", 600)
    assert nexora._usable_gemini_models(chain) == ["model-b", "model-c"]
    # Never let a cooldown be the reason there is nothing left to try.
    for m in chain:
        nexora._mark_gemini_model_unavailable(m, 600)
    assert nexora._usable_gemini_models(chain) == chain
    nexora._gemini_model_cooldowns.clear()


def test_reset_refuses_the_password_already_in_use(client, mailbox):
    """A student resetting because they think they were hacked must not be able
    to end the flow with the same password they started with."""
    signup(client)
    client.get("/logout")
    client.post("/forgot-password", data={"email": "student@carleton.ca"})
    token = _reset_token_from(mailbox)

    response = client.post(f"/reset-password/{token}", data={"password": "correct-horse-battery"})
    assert response.status_code == 200
    assert b"already your password" in response.data
    assert client.get("/dashboard").status_code in (301, 302), "refused reset still signed them in"

    # The link must survive the refusal, or a slip costs them the whole flow.
    assert client.post(f"/reset-password/{token}",
                       data={"password": "a-genuinely-new-one"}).status_code in (301, 302)
    assert client.get("/dashboard").status_code == 200


# --------------------------------------------------------------------------
# Landing page
# --------------------------------------------------------------------------

def test_landing_invites_a_signed_out_visitor_to_join(client):
    body = client.get("/").get_data(as_text=True)
    assert "/signup" in body and "/login" in body
    assert "Start free" in body
    assert "Open your dashboard" not in body


def test_landing_sends_a_signed_in_student_to_their_dashboard(client):
    signup(client)
    body = client.get("/").get_data(as_text=True)
    assert "Open your dashboard" in body
    # Offering "Start free" to someone who already has an account is the tell
    # that a link was added outside the authenticated branch.
    assert "Start free" not in body


def test_landing_has_no_dead_links(client):
    """Every call to action has to go somewhere; the concept build shipped with
    three href="#" placeholders."""
    for _ in range(2):
        assert 'href="#"' not in client.get("/").get_data(as_text=True)
        signup(client)


def test_landing_still_carries_the_scene_and_the_product_panels(client):
    body = client.get("/").get_data(as_text=True)
    assert "three@0.160.0" in body           # the scroll scene survived the port
    assert body.count('class="panel') >= 3   # sync, grades, and the study cards
    assert "Concept build" not in body       # placeholder footer is gone


def test_dashboard_serves_the_redesign(client):
    """The redesign replaced the old dashboard outright; ?v=next was the preview
    flag while both existed and must not resurrect the retired template."""
    signup(client)

    body = client.get("/dashboard").get_data(as_text=True)
    assert "nxv2-" not in body, "the old dashboard markup should be gone"
    assert "Fraunces" in body and "nexora-app.css" in body   # shares the site's design
    assert "Mission control" in body

    assert client.get("/dashboard?v=next").get_data(as_text=True) == body


def test_dashboard_redesign_survives_an_empty_account(client):
    """A brand new student has no courses, tasks, deadlines or updates, which is
    exactly the state every empty-state branch has to handle."""
    signup(client, email="brand.new@carleton.ca")
    body = client.get("/dashboard").get_data(as_text=True)
    assert "Connect Brightspace" in body
    assert "Your week is clear" in body


def test_the_dashboard_shows_every_course_not_the_first_eight(client):
    """Courses were ordered by updated_at and cut to eight, so each sync floated
    a different eight to the top and the rest vanished. It read exactly like the
    importer dropping courses at random, which is what it was reported as."""
    import app as nexora
    from uuid import uuid4
    signup(client, email="many@carleton.ca")

    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="many@carleton.ca").first()
        for i in range(14):
            nexora.db.session.add(nexora.SyncedCourse(
                id=uuid4().hex, user_id=user.id, source_uid=f"uid-{i}",
                course_code=f"TEST{1000 + i}", course_name=f"Course {i}",
                term="Winter 2026"))
        nexora.db.session.commit()

    body = client.get("/dashboard").get_data(as_text=True)
    for i in range(14):
        assert f"TEST{1000 + i}" in body, f"TEST{1000 + i} is missing from the dashboard"


def test_course_order_does_not_move_between_loads(client):
    """Ordering by updated_at rearranged the page after every sync."""
    import app as nexora
    from datetime import datetime, timedelta
    from uuid import uuid4
    signup(client, email="stable@carleton.ca")

    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="stable@carleton.ca").first()
        for i, code in enumerate(["ECOR1033D", "PHYS1004", "MATH1104F"]):
            nexora.db.session.add(nexora.SyncedCourse(
                id=uuid4().hex, user_id=user.id, source_uid=f"u{i}",
                course_code=code, course_name=code, term="Winter 2026"))
        nexora.db.session.commit()

    def order(body):
        return [c for c in ["ECOR1033D", "MATH1104F", "PHYS1004"] if c in body]

    first = order(client.get("/dashboard").get_data(as_text=True))

    # touch one course, as a sync would
    with nexora.app.app_context():
        row = nexora.SyncedCourse.query.filter_by(course_code="PHYS1004").first()
        row.updated_at = datetime.utcnow() + timedelta(hours=1)
        nexora.db.session.commit()

    assert order(client.get("/dashboard").get_data(as_text=True)) == first


# --------------------------------------------------- public pricing claims

# Every page a stranger can read, that talks about price at all.
PRICING_PAGES = ["/", "/signup", "/what-do-i-need"]


@pytest.mark.parametrize("path", PRICING_PAGES)
def test_no_public_page_promises_free_forever(client, path):
    """"Free while you study. No card." shipped to production for one deploy
    after the trial went live, on the landing page and again on /signup -- the
    page where someone actually commits. It reads as a promise of permanent
    free access, and it is the kind of claim that costs a refund argument
    rather than a bug report."""
    body = client.get(path).get_data(as_text=True).lower()
    for claim in ("free while you study", "free forever", "always free",
                  "free for life", "free, forever"):
        assert claim not in body, f"{path} promises permanent free access: {claim!r}"


@pytest.mark.parametrize("path", ["/", "/signup"])
def test_the_trial_length_comes_from_the_setting(client, path):
    """Not typed into the template. TRIAL_DAYS is an environment variable, so a
    hardcoded number is wrong the moment it is changed -- and nobody edits a
    marketing page because they changed a setting."""
    import app as nexora
    body = client.get(path).get_data(as_text=True)
    assert f"{nexora.TRIAL_DAYS} days" in body, f"{path} does not quote TRIAL_DAYS"


def test_the_corpus_claim_matches_the_corpus(client):
    """The landing page cites the eval as proof. A number that drifts from the
    corpus it describes is a claim nobody checked."""
    import app as nexora
    from evals.outline_corpus import CASES
    assert nexora.OUTLINE_CORPUS_CASES == len(CASES), (
        f"landing page claims {nexora.OUTLINE_CORPUS_CASES} grading patterns, "
        f"the corpus has {len(CASES)}")
    body = client.get("/").get_data(as_text=True)
    assert f"{len(CASES)}/{len(CASES)}" in body


def test_no_price_is_quoted_while_price_label_is_unset(client):
    """PRICE_LABEL is the only place an amount may come from."""
    import app as nexora
    if nexora.PRICE_LABEL:
        pytest.skip("a price has been set, so quoting one is correct")
    import re
    # A bare "$" is not a price -- these pages are full of $('id') helpers and
    # JS template literals. A dollar sign followed by a digit is.
    money = re.compile(r"\$\s?\d")
    for path in PRICING_PAGES:
        body = client.get(path).get_data(as_text=True)
        found = money.search(body)
        assert not found, (f"{path} quotes a price with PRICE_LABEL unset: "
                           f"{body[max(0, found.start()-40):found.end()+40]!r}")
