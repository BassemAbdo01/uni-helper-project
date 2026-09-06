"""A free month, then paid — and what an expired account can still do.

The rule that matters most is the last section: expiry makes Nexora read-only,
not dark. Taking away work a student already entered is how you get written
about, and it is not what they are being asked to pay for. They pay for it
staying current.
"""

import os
import sys
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"


def signup(client, email="trial@carleton.ca", ref=None):
    data = {"name": "Trial Student", "email": email, "password": PASSWORD}
    if ref:
        data["ref"] = ref
    return client.post("/signup", data=data, follow_redirects=False)


def set_access(email, trial_ends=None, paid_until=None):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        user.trial_ends_at = trial_ends
        user.paid_until = paid_until
        nexora.db.session.commit()


def expire(email):
    set_access(email, trial_ends=datetime.utcnow() - timedelta(days=1))


# ------------------------------------------------------------- the trial

def test_a_new_account_gets_a_free_month(client):
    import app as nexora
    signup(client)
    body = client.get("/api/plan").get_json()
    assert body["state"] == "trial"
    assert body["active"] is True
    assert body["days_left"] >= nexora.TRIAL_DAYS - 1


def test_no_card_is_asked_for(client):
    """Card-up-front collapses the number of students who ever start."""
    body = client.get("/signup").get_data(as_text=True).lower()
    for word in ("card number", "credit card", "payment method", "cvv"):
        assert word not in body


def test_the_trial_cannot_be_restarted(client):
    """_start_trial is idempotent, so a second call cannot extend anyone."""
    import app as nexora
    signup(client)
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="trial@carleton.ca").first()
        first = user.trial_ends_at
        nexora._start_trial(user)
        nexora.db.session.commit()
        assert user.trial_ends_at == first


def test_an_existing_account_is_grandfathered_not_paywalled(client):
    """People who used Nexora while it was free -- including the testers who
    found the bugs -- do not meet a paywall the day this ships."""
    import app as nexora
    signup(client, email="early@carleton.ca")
    set_access("early@carleton.ca", trial_ends=None, paid_until=None)
    with nexora.app.app_context():
        nexora.grandfather_existing_accounts()
        user = nexora.User.query.filter_by(email="early@carleton.ca").first()
        assert user.paid_until is not None
        assert (user.paid_until - datetime.utcnow()).days > 300


# ---------------------------------------------------- what the gate blocks

GATED = [
    ("post", "/api/study-plan/generate", None),
    ("post", "/api/study-plan/brief", {}),
    ("post", "/api/analyze-outline", {"text": "Midterm 40% Final 60%"}),
    ("post", "/api/course/BIOL1103/outline/analyze", {}),
    ("post", "/api/course/BIOL1103/study-tools/generate", {}),
    ("post", "/api/exams/accept", {"events": []}),
]


@pytest.mark.parametrize("method,path,payload", GATED)
def test_an_expired_account_cannot_spend_your_money(client, method, path, payload):
    signup(client)
    expire("trial@carleton.ca")
    call = getattr(client, method)
    res = call(path, json=payload) if payload is not None else call(path)
    assert res.status_code == 402, f"{path} was not gated"
    body = res.get_json()
    assert body["subscription_required"] is True
    assert "ended" in body["error"]


@pytest.mark.parametrize("method,path,payload", GATED)
def test_the_same_calls_work_during_the_trial(client, method, path, payload):
    signup(client)
    call = getattr(client, method)
    res = call(path, json=payload) if payload is not None else call(path)
    assert res.status_code != 402


def test_syncing_stops_when_the_month_is_up(client):
    """This is the whole value proposition: it goes stale without you."""
    signup(client)
    token = client.post("/api/sync/extension-key").get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    ok = client.post("/api/sync/courses", json={"courses": []}, headers=headers)
    assert ok.status_code == 200

    expire("trial@carleton.ca")
    blocked = client.post("/api/sync/courses", json={"courses": []}, headers=headers)
    assert blocked.status_code == 402
    assert blocked.get_json()["subscription_required"] is True


# ------------------------------------------- what an expired account keeps

def test_everything_already_imported_is_still_readable(client):
    import app as nexora
    signup(client)
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="trial@carleton.ca").first()
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user.id, course_code="BIOL1103",
            course_name="Biology", term="Fall 2026"))
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id, group_id="g", title="Essay",
            base_title="Essay", course_full="BIOL1103", due_raw="2026-10-01"))
        nexora.db.session.commit()
    expire("trial@carleton.ca")

    for path in ("/dashboard", "/calendar", "/grade-calculator", "/study-plan",
                 "/settings", "/profile", "/course/BIOL1103"):
        assert client.get(path).status_code == 200, f"{path} should still open"
    assert "BIOL1103" in client.get("/dashboard").get_data(as_text=True)


def test_marks_can_still_be_entered_by_hand(client):
    """They are the student's own work, not a service being rendered."""
    signup(client)
    expire("trial@carleton.ca")
    res = client.post("/api/course/BIOL1103/grades",
                      json={"name": "Midterm", "weight": 30, "score": "18/20"})
    assert res.status_code == 200


def test_the_data_can_always_be_taken_out(client):
    """Holding someone's data hostage behind a lapsed subscription is not a
    business model, it is a hostage situation."""
    signup(client)
    expire("trial@carleton.ca")
    assert client.get("/api/account/export").status_code == 200
    assert client.delete("/api/account",
                         json={"password": PASSWORD, "confirm": "DELETE"}).status_code == 200


# ------------------------------------------------------------- referrals

def code_of(email):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        return nexora._referral_code_for(user)


def activate(client):
    """What a referred account has to do before the credit is paid."""
    token = client.post("/api/sync/extension-key").get_json()["token"]
    return client.post("/api/sync/courses", json={"courses": [{
        "source_uid": "https://learn.school.ca/d2l/home/1",
        "course_code": "BIOL1103", "course_name": "Biology",
        "brightspace_url": "https://learn.school.ca/d2l/home/1"}]},
        headers={"Authorization": f"Bearer {token}"})


def months_of(email):
    import app as nexora
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().referral_months or 0


def test_a_referral_pays_only_once_the_friend_actually_uses_it(client):
    """Paying on signup is the whole exploit: ten throwaway addresses, a free
    year."""
    signup(client, email="host@carleton.ca")
    code = code_of("host@carleton.ca")
    client.get("/logout")

    signup(client, email="friend@carleton.ca", ref=code)
    assert months_of("host@carleton.ca") == 0, "credited before the friend did anything"
    activate(client)
    assert months_of("host@carleton.ca") == 1


def test_a_referral_is_never_paid_twice_for_one_friend(client):
    signup(client, email="host2@carleton.ca")
    code = code_of("host2@carleton.ca")
    client.get("/logout")
    signup(client, email="friend2@carleton.ca", ref=code)
    activate(client)
    activate(client)
    activate(client)
    assert months_of("host2@carleton.ca") == 1


def test_you_cannot_refer_yourself(client):
    signup(client, email="self@carleton.ca")
    code = code_of("self@carleton.ca")
    client.get("/logout")
    signup(client, email="self2@carleton.ca", ref=code)
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="self2@carleton.ca").first()
        user.email = "self@carleton.ca-alias"
        nexora.db.session.commit()
    # the real guard: a code that is not a real account is simply ignored
    client.get("/logout")
    signup(client, email="nobody@carleton.ca", ref="ZZZZZZZ")
    with nexora.app.app_context():
        assert nexora.User.query.filter_by(email="nobody@carleton.ca").first().referred_by == ""


def test_referral_credit_is_capped(client):
    import app as nexora
    signup(client, email="popular@carleton.ca")
    code = code_of("popular@carleton.ca")
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="popular@carleton.ca").first()
        user.referral_months = nexora.REFERRAL_MONTHS_MAX
        before = user.paid_until
        nexora.db.session.commit()
    client.get("/logout")
    signup(client, email="latecomer@carleton.ca", ref=code)
    activate(client)
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="popular@carleton.ca").first()
        assert user.referral_months == nexora.REFERRAL_MONTHS_MAX
        assert user.paid_until == before


def test_a_credit_adds_to_the_time_left_rather_than_replacing_it(client):
    """Crediting someone on day two of their trial must not cost them the
    twenty-eight days they had."""
    import app as nexora
    signup(client, email="mid@carleton.ca")
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="mid@carleton.ca").first()
        remaining = user.trial_ends_at
        nexora._grant_days(user, 30)
        nexora.db.session.commit()
        assert user.paid_until > remaining + timedelta(days=29)


def test_the_plan_endpoint_hands_back_a_shareable_link(client):
    signup(client)
    referral = client.get("/api/plan").get_json()["referral"]
    assert referral["code"]
    assert f"ref={referral['code']}" in referral["url"]
    assert referral["max"] >= 1


def test_no_price_is_claimed_until_one_is_set(client):
    """PRICE_LABEL is unset, so nothing should be quoting an amount."""
    signup(client)
    expire("trial@carleton.ca")
    body = client.post("/api/study-plan/generate").get_json()
    assert body["price"] is None
    assert "$" not in body["error"]


def test_the_plan_endpoint_needs_a_login(client):
    assert client.get("/api/plan").status_code in (302, 401)
