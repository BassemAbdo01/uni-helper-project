"""Reminders: the student says when, and Nexora notifies them that day.

Storing a reminder is the easy half. **Nexora has no scheduler** -- one gunicorn
web service, so code runs when a request arrives and at no other time -- so the
moment has to be computed on the student's own clock and acted on by an external
caller holding a token.

What is pinned here is therefore not "a row was written": it is that the clock
belongs to the student, that a notification is sent exactly once however often
the dispatcher is called, that a dispatcher which has been down does not deliver
a day of stale notices at once, and that a send which failed is not recorded as
a send. Dates are relative to the day the suite runs, because Nexora
deliberately stops carrying work far enough in the past -- a fixture dated in
the past is a test with a timer on it.
"""

import os
import re
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="remind@carleton.ca"):
    return client.post("/signup", data={"name": "Remind Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


@pytest.fixture()
def student(client):
    signup(client)
    return client


@pytest.fixture()
def sent(monkeypatch):
    """Capture outgoing mail instead of sending it. Returns the list."""
    import app as nexora
    box = []

    def fake_send(to_address, subject, body):
        box.append({"to": to_address, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(nexora, "send_email", fake_send)
    monkeypatch.setattr(nexora, "email_is_configured", lambda: True)
    return box


def set_zone(name):
    import app as nexora
    from app import db
    with nexora.app.app_context():
        user = nexora.User.query.first()
        user.timezone = name
        db.session.commit()
        return user.id


def make_reminder(client, day, time_raw="09:00", title="Email Prof Smith", **extra):
    body = {"title": title, "date": day.isoformat() if hasattr(day, "isoformat") else day,
            "kind": "reminder", "course": "OTHER"}
    if time_raw:
        body["time"] = time_raw
    body.update(extra)
    res = client.post("/api/events", json=body)
    assert res.status_code in (200, 201), res.get_data(as_text=True)
    return res


def only_reminder():
    import app as nexora
    with nexora.app.app_context():
        rows = nexora.Task.query.filter_by(kind="reminder").all()
        assert len(rows) == 1
        return rows[0].id


def reload_row(task_id):
    import app as nexora
    with nexora.app.app_context():
        return nexora.Task.query.get(task_id)


# ------------------------------------------------- the moment is the student's

def test_a_reminder_fires_on_the_students_clock_not_the_servers():
    """Nine in the morning means nine where the student is.

    Pacific/Kiritimati is UTC+14 and Pacific/Niue is UTC-11, so they are 25
    hours apart and on different calendar days at every instant of the year.
    A test that compares the student's clock against "now" passes by accident
    on any machine whose zone matches the server's, which is most of them.
    """
    import app as nexora
    from zoneinfo import ZoneInfo

    day = date.today() + timedelta(days=10)
    east = nexora._reminder_instant_utc(day.isoformat(), "09:00",
                                        ZoneInfo("Pacific/Kiritimati"))
    west = nexora._reminder_instant_utc(day.isoformat(), "09:00",
                                        ZoneInfo("Pacific/Niue"))
    assert west - east == timedelta(hours=25)
    # And neither is the naive reading, which is the bug this exists to stop.
    naive = datetime(day.year, day.month, day.day, 9, 0)
    assert east != naive and west != naive


def test_a_reminder_with_no_time_is_for_the_morning_not_midnight():
    import app as nexora
    from datetime import timezone

    day = date.today() + timedelta(days=3)
    instant = nexora._reminder_instant_utc(day.isoformat(), "", timezone.utc)
    assert instant.hour == nexora.REMINDER_DEFAULT_HOUR
    assert instant.hour != 0


def test_a_row_with_no_readable_date_is_never_due():
    import app as nexora
    from datetime import timezone
    assert nexora._reminder_instant_utc("", "09:00", timezone.utc) is None
    assert nexora._reminder_instant_utc("not-a-date", "09:00", timezone.utc) is None


# ----------------------------------------------------------------- dispatching

def test_nothing_is_sent_before_the_moment(student, sent):
    import app as nexora
    set_zone("UTC")
    day = date.today() + timedelta(days=2)
    make_reminder(student, day, "09:00")

    with nexora.app.app_context():
        counts = nexora._dispatch_reminders(
            now_utc=datetime(day.year, day.month, day.day, 8, 0))
    assert counts["sent"] == 0
    assert sent == []


def test_the_reminder_is_sent_at_its_moment_and_names_the_time(student, sent):
    import app as nexora
    set_zone("UTC")
    day = date.today() + timedelta(days=2)
    make_reminder(student, day, "09:00", notes="Bring the lab notes")

    with nexora.app.app_context():
        counts = nexora._dispatch_reminders(
            now_utc=datetime(day.year, day.month, day.day, 9, 0))

    assert counts["sent"] == 1
    assert len(sent) == 1
    assert "Email Prof Smith" in sent[0]["subject"]
    assert "09:00" in sent[0]["body"]
    assert "Bring the lab notes" in sent[0]["body"]
    assert sent[0]["to"] == "remind@carleton.ca"


def test_it_is_sent_exactly_once_however_often_the_dispatcher_runs(student, sent):
    """A cron firing every five minutes will overlap a slow mail server.

    The attempt is stamped and committed before the message goes out precisely
    so a second caller cannot send it again.
    """
    import app as nexora
    set_zone("UTC")
    day = date.today() + timedelta(days=2)
    make_reminder(student, day, "09:00")
    moment = datetime(day.year, day.month, day.day, 9, 0)

    with nexora.app.app_context():
        for _ in range(4):
            nexora._dispatch_reminders(now_utc=moment)
    assert len(sent) == 1


def test_a_dispatcher_that_has_been_down_does_not_deliver_stale_notices(student, sent):
    """Fourteen notifications of things that already happened is worse than none."""
    import app as nexora
    set_zone("UTC")
    day = date.today() - timedelta(days=1)
    make_reminder(student, day, "09:00")

    with nexora.app.app_context():
        counts = nexora._dispatch_reminders(
            now_utc=datetime(day.year, day.month, day.day, 9, 0)
            + timedelta(hours=nexora.REMINDER_CATCHUP_HOURS + 1))

    assert counts["sent"] == 0
    assert counts["too_late"] == 1
    assert sent == []


def test_a_reminder_too_late_to_send_is_never_recorded_as_sent(student, sent):
    """"Could not be sent" and "was sent" are different facts.

    Stamping it as sent would be a lie told to the one place that could ever
    reveal the dispatcher had been down.
    """
    import app as nexora
    set_zone("UTC")
    day = date.today() - timedelta(days=1)
    make_reminder(student, day, "09:00")
    task_id = only_reminder()

    with nexora.app.app_context():
        nexora._dispatch_reminders(
            now_utc=datetime(day.year, day.month, day.day, 9, 0)
            + timedelta(hours=nexora.REMINDER_CATCHUP_HOURS + 1))

    assert (reload_row(task_id).reminder_state or "") != "sent"


def test_a_failed_send_is_not_recorded_as_sent(student, monkeypatch):
    import app as nexora
    monkeypatch.setattr(nexora, "send_email", lambda *a, **k: False)
    monkeypatch.setattr(nexora, "email_is_configured", lambda: True)
    set_zone("UTC")
    day = date.today() + timedelta(days=2)
    make_reminder(student, day, "09:00")
    task_id = only_reminder()

    with nexora.app.app_context():
        counts = nexora._dispatch_reminders(
            now_utc=datetime(day.year, day.month, day.day, 9, 0))

    assert counts["failed"] == 1 and counts["sent"] == 0
    assert reload_row(task_id).reminder_state == "failed"


def test_a_failed_send_is_retried_later_but_not_immediately(student, monkeypatch):
    import app as nexora
    attempts = []
    monkeypatch.setattr(nexora, "email_is_configured", lambda: True)
    monkeypatch.setattr(nexora, "send_email",
                        lambda *a, **k: attempts.append(1) and False or False)
    set_zone("UTC")
    day = date.today() + timedelta(days=2)
    make_reminder(student, day, "09:00")
    moment = datetime(day.year, day.month, day.day, 9, 0)

    with nexora.app.app_context():
        nexora._dispatch_reminders(now_utc=moment)
        nexora._dispatch_reminders(now_utc=moment + timedelta(minutes=1))
        assert len(attempts) == 1, "a refusing mail server must not be hammered"
        nexora._dispatch_reminders(
            now_utc=moment + timedelta(minutes=nexora.REMINDER_RETRY_MINUTES + 1))
        assert len(attempts) == 2


def test_a_reminder_that_is_done_or_deleted_is_not_sent(student, sent):
    import app as nexora
    from app import db
    set_zone("UTC")
    day = date.today() + timedelta(days=2)
    make_reminder(student, day, "09:00")
    task_id = only_reminder()

    with nexora.app.app_context():
        nexora.Task.query.get(task_id).completed = True
        db.session.commit()
        counts = nexora._dispatch_reminders(
            now_utc=datetime(day.year, day.month, day.day, 9, 0))
    assert counts["sent"] == 0 and sent == []


def test_with_no_smtp_nothing_is_sent_and_the_count_says_so(student, monkeypatch):
    """Degrading quietly is right for the student and must be visible to whoever
    runs it: a dispatcher sending nothing because SMTP is unset looks exactly
    like one with nothing to send."""
    import app as nexora
    monkeypatch.setattr(nexora, "email_is_configured", lambda: False)
    monkeypatch.setattr(nexora, "send_email",
                        lambda *a, **k: pytest.fail("must not try to send"))
    set_zone("UTC")
    day = date.today() + timedelta(days=2)
    make_reminder(student, day, "09:00")
    task_id = only_reminder()

    with nexora.app.app_context():
        counts = nexora._dispatch_reminders(
            now_utc=datetime(day.year, day.month, day.day, 9, 0))

    assert counts["email_configured"] is False
    assert counts["sent"] == 0
    # "Due now and nothing can send it" is its own count. Folded into
    # `waiting` it reads as "not due yet", which is the /api/sync/auto
    # mistake: a student whose feed was unreachable looked exactly like one
    # who had just synced.
    assert counts["blocked"] == 1
    assert counts["waiting"] == 0
    assert (reload_row(task_id).reminder_state or "") != "sent"


# ------------------------------------------------------------- the endpoint

def test_with_no_token_configured_the_route_does_not_exist(student, monkeypatch):
    import app as nexora
    monkeypatch.setattr(nexora, "REMINDER_DISPATCH_TOKEN", "")
    assert student.post("/api/reminders/dispatch").status_code == 404


def test_a_wrong_token_is_a_404_not_a_401(student, monkeypatch):
    """A route that answers differently to a bad credential confirms it exists.
    The Outlook probe taught this by answering 302 where a nonsense path
    answered 404."""
    import app as nexora
    monkeypatch.setattr(nexora, "REMINDER_DISPATCH_TOKEN", "the-real-token")
    assert student.post("/api/reminders/dispatch?token=wrong").status_code == 404
    assert student.post("/api/reminders/dispatch").status_code == 404


def test_the_right_token_dispatches(student, sent, monkeypatch):
    import app as nexora
    monkeypatch.setattr(nexora, "REMINDER_DISPATCH_TOKEN", "the-real-token")
    res = student.post("/api/reminders/dispatch",
                       headers={"X-Nexora-Token": "the-real-token"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["email_configured"] is True
    assert "sent" in body and "considered" in body


def test_the_endpoint_sends_a_reminder_whose_moment_has_passed(student, sent, monkeypatch):
    """End to end on the real clock: the cron calls, the student gets mail."""
    import app as nexora
    monkeypatch.setattr(nexora, "REMINDER_DISPATCH_TOKEN", "the-real-token")
    user_id = set_zone("America/Toronto")

    with nexora.app.app_context():
        user = nexora.User.query.get(user_id)
        local = nexora._student_now(user) - timedelta(minutes=5)
    make_reminder(student, local.date(), local.strftime("%H:%M"))

    res = student.post("/api/reminders/dispatch?token=the-real-token")
    assert res.status_code == 200
    assert res.get_json()["sent"] == 1
    assert len(sent) == 1


def test_a_get_is_not_allowed(student, monkeypatch):
    """POST for the reason /api/tour is: a prefetch must not spend it."""
    import app as nexora
    monkeypatch.setattr(nexora, "REMINDER_DISPATCH_TOKEN", "the-real-token")
    assert student.get("/api/reminders/dispatch?token=the-real-token").status_code == 405


# ------------------------------------------------- what a reminder is NOT

def test_a_reminder_is_not_a_deadline(student):
    import app as nexora
    make_reminder(student, date.today() + timedelta(days=4))
    with nexora.app.app_context():
        row = nexora.Task.query.filter_by(kind="reminder").first()
        assert row.is_deadline is False


def test_the_planner_never_schedules_study_time_for_a_reminder(student):
    """`reminder` is not in _BASE_EFFORT, so without the explicit skip it falls
    through to _item_kind(title) and "email prof about the assignment" is
    guessed as an assignment and given hours."""
    import app as nexora
    make_reminder(student, date.today() + timedelta(days=5),
                  title="Email prof about the assignment")
    with nexora.app.app_context():
        user = nexora.User.query.first()
        items = nexora._study_items(user.id)
    assert not [i for i in items if "Email prof" in str(i.get("title", ""))]


def test_opening_a_reminder_to_edit_it_keeps_it_a_reminder():
    """The sheet's kind whitelist decides what an edited entry becomes. Left at
    exam|due|event, a reminder opened and saved came back an exam -- and an exam
    is never emailed."""
    here = os.path.dirname(os.path.abspath(__file__))
    page = open(os.path.join(here, "..", "templates", "calendar.html"),
                encoding="utf-8").read()
    whitelist = re.search(r"\['exam','due','event'[^\]]*\]", page)
    assert whitelist, "the kind whitelist moved; check it still lists reminder"
    assert "reminder" in whitelist.group(0)


# -------------------------------------------------------------- the feed

def test_the_feed_gives_a_reminder_an_alarm_so_a_phone_notifies(student):
    import app as nexora
    make_reminder(student, date.today() + timedelta(days=6), "14:30")
    url = student.get("/api/calendar/feed").get_json()["url"]
    body = student.get("/" + url.split("/", 3)[3]).get_data(as_text=True)

    assert "BEGIN:VALARM" in body
    # Relative, not absolute: every DTSTART this feed writes is a floating
    # local time, and an absolute TRIGGER would have to be UTC.
    assert "TRIGGER:PT0S" in body
    assert "ACTION:DISPLAY" in body


def test_an_all_day_reminder_alarms_in_the_morning_not_at_midnight(student):
    import app as nexora
    make_reminder(student, date.today() + timedelta(days=6), time_raw="")
    url = student.get("/api/calendar/feed").get_json()["url"]
    body = student.get("/" + url.split("/", 3)[3]).get_data(as_text=True)
    assert "TRIGGER:PT%dH" % nexora.REMINDER_DEFAULT_HOUR in body


def test_an_exam_gets_no_alarm(student):
    """Only a reminder carries one. An alarm on every imported deadline would
    turn a subscribed calendar into a notification storm."""
    student.post("/api/events", json={"title": "Midterm 1", "kind": "exam",
                                      "date": (date.today() + timedelta(days=6)).isoformat(),
                                      "course": "BIOL1103", "time": "14:30"})
    url = student.get("/api/calendar/feed").get_json()["url"]
    body = student.get("/" + url.split("/", 3)[3]).get_data(as_text=True)
    assert "BEGIN:VALARM" not in body
