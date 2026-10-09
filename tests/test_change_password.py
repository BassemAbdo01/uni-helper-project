"""A signed-in student can change their own password.

The profile page said this was "not built yet" and linked to /forgot-password,
which redirects a signed-in user straight to the dashboard -- so the only link
on the only page that mentioned passwords could never work for the one group of
people able to see it. Reported as "when i click reset password in profile
section it takes me back to dashboard". There was no way to change a password
at all; the fallback was to log out and claim to be locked out.
"""
from uuid import uuid4

import app as nexora


PW = "nexorademo1"


def _account(client):
    email = f"pw{uuid4().hex[:7]}@carleton.ca"
    client.post("/signup", data={"name": "P", "email": email, "password": PW})
    return email


def _change(client, current, new):
    return client.post("/api/password", json={"current": current, "new": new})


def test_the_profile_offers_a_form_rather_than_a_link_that_bounces(client):
    _account(client)
    body = client.get("/profile").get_data(as_text=True)
    assert 'id="pwForm"' in body
    assert "forgot-password" not in body, "that link redirects a signed-in user away"


def test_changing_it_works_and_the_new_password_signs_you_in(client):
    email = _account(client)
    assert _change(client, PW, "a-brand-new-one-99").status_code == 200
    client.post("/logout")
    res = client.post("/login", data={"email": email, "password": "a-brand-new-one-99"})
    assert res.status_code in (301, 302)
    assert "/login" not in res.headers.get("Location", "")


def test_the_old_password_stops_working(client):
    email = _account(client)
    _change(client, PW, "a-brand-new-one-99")
    client.post("/logout")
    res = client.post("/login", data={"email": email, "password": PW})
    assert b"Invalid email or password" in res.data


def test_the_current_password_is_required(client):
    """Without it, anyone who finds an unlocked laptop owns the account
    permanently rather than until it is locked again."""
    email = _account(client)
    res = _change(client, "not-my-password", "a-brand-new-one-99")
    assert res.status_code == 400
    assert "current password" in res.get_json()["error"]
    client.post("/logout")
    assert client.post("/login", data={"email": email, "password": PW}).status_code in (301, 302)


def test_a_short_password_is_refused_with_the_length_it_wants(client):
    _account(client)
    res = _change(client, PW, "abc")
    assert res.status_code == 400
    assert str(nexora.MIN_PASSWORD_LENGTH) in res.get_json()["error"]


def test_reusing_the_same_password_is_refused(client):
    _account(client)
    res = _change(client, PW, PW)
    assert res.status_code == 400
    assert "already have" in res.get_json()["error"]


def test_an_outstanding_reset_link_dies_with_the_change(client):
    """An old reset email in an inbox must not still open the account."""
    _account(client)
    with nexora.app.app_context():
        user = nexora.User.query.order_by(nexora.User.id.desc()).first()
        nexora.db.session.add(nexora.PasswordResetToken(
            id=uuid4().hex, user_id=user.id, token_hash="x" * 64,
            expires_at=nexora.datetime.utcnow() + nexora.timedelta(hours=1)))
        nexora.db.session.commit()
        uid = user.id
    _change(client, PW, "a-brand-new-one-99")
    with nexora.app.app_context():
        left = nexora.PasswordResetToken.query.filter_by(user_id=uid, used_at=None).count()
    assert left == 0


def test_it_needs_an_account(client):
    assert _change(client, PW, "a-brand-new-one-99").status_code in (302, 401)


# --------------------------------------------------------------------------
# Some people would rather do it from a link in their inbox. /forgot-password
# cannot serve them -- it redirects a signed-in user away, correctly, since it
# is for people who cannot get in. Taking that option away was a loss.
# --------------------------------------------------------------------------

def test_a_signed_in_student_can_ask_for_the_email(client, monkeypatch):
    _account(client)
    sent = {}
    monkeypatch.setattr(nexora, "email_is_configured", lambda: True)
    monkeypatch.setattr(nexora, "send_email",
                        lambda to, subject, body: sent.update(to=to, body=body) or True)
    res = client.post("/api/password/email-link")
    assert res.status_code == 200
    with nexora.app.app_context():
        user = nexora.User.query.order_by(nexora.User.id.desc()).first()
        assert sent["to"] == user.email
    assert "/reset-password/" in sent["body"]


def test_the_link_only_ever_goes_to_the_address_on_the_account(client, monkeypatch):
    """A field here would be a way to send Nexora-branded mail to anyone."""
    email = _account(client)
    sent = {}
    monkeypatch.setattr(nexora, "email_is_configured", lambda: True)
    monkeypatch.setattr(nexora, "send_email",
                        lambda to, subject, body: sent.update(to=to) or True)
    client.post("/api/password/email-link", json={"email": "someone-else@example.com"})
    assert sent["to"] == email


def test_it_says_so_plainly_when_email_is_not_set_up(client, monkeypatch):
    """Signed in, so there is no account to hide -- unlike /forgot-password,
    which has to stay vague."""
    _account(client)
    monkeypatch.setattr(nexora, "email_is_configured", lambda: False)
    res = client.post("/api/password/email-link")
    assert res.status_code == 503
    assert "not set up" in res.get_json()["error"]


def test_a_failed_send_is_reported_rather_than_claimed_as_sent(client, monkeypatch):
    _account(client)
    monkeypatch.setattr(nexora, "email_is_configured", lambda: True)
    monkeypatch.setattr(nexora, "send_email", lambda *a, **k: False)
    res = client.post("/api/password/email-link")
    assert res.status_code == 502
    assert "could not send" in res.get_json()["error"].lower()


def test_asking_again_invalidates_the_previous_link(client, monkeypatch):
    _account(client)
    monkeypatch.setattr(nexora, "email_is_configured", lambda: True)
    monkeypatch.setattr(nexora, "send_email", lambda *a, **k: True)
    client.post("/api/password/email-link")
    client.post("/api/password/email-link")
    with nexora.app.app_context():
        user = nexora.User.query.order_by(nexora.User.id.desc()).first()
        live = nexora.PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).count()
    assert live == 1


def test_it_needs_an_account_too(client):
    assert client.post("/api/password/email-link").status_code in (302, 401)
