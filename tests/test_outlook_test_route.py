"""The Outlook feasibility probe: off by default, admin only, stores nothing.

This is a throwaway route that exists to answer one question -- can a Carleton
student consent to Mail.Read for themselves, or does the tenant demand an
administrator. What these tests pin is not the Microsoft flow, which cannot be
exercised without Microsoft. It is the three properties that make shipping a
probe to production acceptable at all:

  1. it is OFF unless an environment variable says otherwise
  2. it is invisible to every account except the admin's
  3. no token and no message is written anywhere

A feature flag that leaks when unset is not a feature flag.
"""
from uuid import uuid4

import app as nexora

ADMIN = nexora.ADMIN_EMAIL


def _signup(client, email, password="nexorademo1"):
    client.post("/signup", data={"name": "T", "email": email, "password": password})


def _admin(client):
    """The admin account, created directly: /signup would 302 as the same
    client is already signed in, and the gate reads current_user.email."""
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=ADMIN).first()
        if not user:
            user = nexora.User(name="Admin", email=ADMIN)
            user.password_hash = nexora.generate_password_hash("nexorademo1")
            nexora.db.session.add(user)
            nexora.db.session.commit()
    client.post("/login", data={"email": ADMIN, "password": "nexorademo1"})


# --------------------------------------------------------------------------
# Off by default
# --------------------------------------------------------------------------

def test_the_flag_is_off_unless_the_environment_says_otherwise():
    """Default off is the whole point: this ships to production disabled and
    is switched on from Render for one test."""
    assert nexora.OUTLOOK_TEST_ENABLED is False


def test_with_the_flag_off_the_page_says_so_and_offers_nothing(client, monkeypatch):
    monkeypatch.setattr(nexora, "OUTLOOK_TEST_ENABLED", False)
    _admin(client)
    body = client.get("/outlook-test").get_data(as_text=True)
    assert "OUTLOOK_TEST_ENABLED" in body
    assert "/outlook-test/start" not in body, "it must not offer a button it cannot honour"


def test_start_refuses_while_the_flag_is_off(client, monkeypatch):
    """The page hiding the button is not enough; the route is the gate."""
    monkeypatch.setattr(nexora, "OUTLOOK_TEST_ENABLED", False)
    _admin(client)
    res = client.get("/outlook-test/start")
    assert res.status_code == 200
    assert "OUTLOOK_TEST_ENABLED" in res.get_data(as_text=True)
    assert res.location is None, "it must not redirect to Microsoft"


def test_the_flag_alone_is_not_enough(client, monkeypatch):
    """On, but unconfigured. It must name what is missing rather than failing
    at Microsoft with something unreadable."""
    monkeypatch.setattr(nexora, "OUTLOOK_TEST_ENABLED", True)
    monkeypatch.setattr(nexora, "MS_CLIENT_ID", "")
    monkeypatch.setattr(nexora, "MS_CLIENT_SECRET", "")
    monkeypatch.setattr(nexora, "MS_REDIRECT_URI", "")
    _admin(client)
    body = client.get("/outlook-test").get_data(as_text=True)
    for name in ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_REDIRECT_URI"):
        assert name in body, name


# --------------------------------------------------------------------------
# Admin only
# --------------------------------------------------------------------------

def test_an_ordinary_account_cannot_reach_any_of_it(client, monkeypatch):
    monkeypatch.setattr(nexora, "OUTLOOK_TEST_ENABLED", True)
    _signup(client, f"nb{uuid4().hex[:7]}@carleton.ca")
    for path in ("/outlook-test", "/outlook-test/start", "/outlook-test/callback"):
        res = client.get(path)
        assert res.status_code == 302, path
        assert "/outlook-test" not in (res.location or ""), path


def test_an_anonymous_visitor_cannot_reach_any_of_it(client, monkeypatch):
    monkeypatch.setattr(nexora, "OUTLOOK_TEST_ENABLED", True)
    for path in ("/outlook-test", "/outlook-test/start", "/outlook-test/callback"):
        assert client.get(path).status_code in (302, 401), path


# --------------------------------------------------------------------------
# The callback cannot be driven from outside
# --------------------------------------------------------------------------

def test_a_callback_with_no_state_is_refused(client, monkeypatch):
    """Without this, anyone who can make the admin's browser issue a GET could
    feed an authorization code of their choosing into the exchange."""
    monkeypatch.setattr(nexora, "OUTLOOK_TEST_ENABLED", True)
    monkeypatch.setattr(nexora, "MS_CLIENT_ID", "id")
    monkeypatch.setattr(nexora, "MS_CLIENT_SECRET", "secret")
    monkeypatch.setattr(nexora, "MS_REDIRECT_URI", "https://e.test/outlook-test/callback")
    _admin(client)
    body = client.get("/outlook-test/callback?code=abc&state=guessed").get_data(as_text=True)
    assert "state parameter did not match" in body


# --------------------------------------------------------------------------
# What Microsoft said, verbatim
# --------------------------------------------------------------------------

def test_the_admin_consent_refusal_is_shown_with_its_code(client, monkeypatch):
    """AADSTS90094 is the answer this whole exercise exists to detect. It has
    to arrive on the page intact, not paraphrased into "sign-in failed"."""
    monkeypatch.setattr(nexora, "OUTLOOK_TEST_ENABLED", True)
    monkeypatch.setattr(nexora, "MS_CLIENT_ID", "id")
    monkeypatch.setattr(nexora, "MS_CLIENT_SECRET", "secret")
    monkeypatch.setattr(nexora, "MS_REDIRECT_URI", "https://e.test/outlook-test/callback")
    _admin(client)
    with client.session_transaction() as sess:
        sess["ms_state"] = "s1"
    body = client.get(
        "/outlook-test/callback?state=s1&error=consent_required"
        "&error_description=AADSTS90094%3A+The+grant+requires+admin+permission."
    ).get_data(as_text=True)
    # Whitespace-collapsed: the sentence wraps in the template, so a raw
    # substring search would fail on a line break rather than on the content.
    flat = " ".join(body.split())
    assert "AADSTS90094" in flat
    assert "requires admin permission" in flat, "Microsoft's own words, verbatim"
    assert "administrator to approve" in flat, "it must say what that means for the build"


def test_the_aadsts_code_is_read_out_of_whatever_microsoft_said():
    assert nexora._aadsts_code("AADSTS65001: no consent") == "AADSTS65001"
    assert nexora._aadsts_code("", "x AADSTS7000215 y") == "AADSTS7000215"
    assert nexora._aadsts_code("something else entirely") == ""
    assert nexora._aadsts_code(None) == ""


# --------------------------------------------------------------------------
# It stores nothing
# --------------------------------------------------------------------------

def test_no_token_is_ever_put_in_the_session():
    """Flask's session is a signed COOKIE and browsers drop one over ~4 KB. A
    Graph access token is 1-2 KB and a refresh token more, so stashing either
    would break the session silently rather than fail loudly. The source is the
    check here because there is no Microsoft to drive in a test."""
    import inspect
    src = inspect.getsource(nexora.outlook_test_callback)
    assert 'session["ms_state"]' not in src, "the callback pops state, never sets it"
    for forbidden in ("session['access_token']", 'session["access_token"]',
                      "session['refresh_token']", 'session["refresh_token"]'):
        assert forbidden not in src, forbidden


def test_no_model_was_added_to_hold_mail():
    """The rule was: no email content in the database. The cheapest way to keep
    it is to have nowhere to put it."""
    names = [m.class_.__name__ for m in nexora.db.Model.registry.mappers]
    for banned in ("Email", "MailMessage", "OutlookMessage", "OutlookToken"):
        assert banned not in names, banned


def test_the_secret_is_never_rendered(client, monkeypatch):
    monkeypatch.setattr(nexora, "OUTLOOK_TEST_ENABLED", True)
    monkeypatch.setattr(nexora, "MS_CLIENT_ID", "the-client-id")
    monkeypatch.setattr(nexora, "MS_CLIENT_SECRET", "the-super-secret-value")
    monkeypatch.setattr(nexora, "MS_REDIRECT_URI", "https://e.test/outlook-test/callback")
    _admin(client)
    body = client.get("/outlook-test").get_data(as_text=True)
    assert "the-super-secret-value" not in body
    assert "the-client-id" not in body


# --------------------------------------------------------------------------
# The scopes
# --------------------------------------------------------------------------

def test_only_read_scopes_are_requested():
    """No send permission, now or by accident later."""
    assert nexora.MS_SCOPES == ["User.Read", "Mail.Read"]
    assert not any("Send" in s or "Write" in s or "ReadWrite" in s
                   for s in nexora.MS_SCOPES)


def test_offline_access_is_not_passed_to_msal():
    """MSAL REJECTS offline_access, openid and profile as reserved -- it raises
    ValueError before any network call -- and adds all three itself. Listing it
    explicitly, which is the obvious reading of the requirement, would make the
    Connect button look broken."""
    assert "offline_access" not in nexora.MS_SCOPES
    assert "openid" not in nexora.MS_SCOPES
