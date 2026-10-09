"""Dates announced in prose should reach the calendar.

An instructor posts "the midterm exam date is set to Saturday Nov 7 at 2PM" as
an announcement. Brightspace never turns that into a calendar item, so Nexora
held every word of it and the calendar stayed empty.

This feature existed once and was removed: it read dates with a regular
expression, and once the extension imported every term it read every old
announcement too, offering a first-year test as "Tue, Sep 29" this September.
The note left behind said that if it came back it had to be current-term only,
future-dated only and year-aware. These tests are that note, enforced.
"""
from datetime import date, timedelta
from uuid import uuid4

import app as nexora


TODAY = date(2026, 10, 8)
REAL = ("Hello Bassem. This is to let you know that midterm exam date is set to "
        "Saturday Nov 7 at 2PM. The midterm exam Module has become available for "
        "you to review the information. Thank you, Rose")


def _user(client):
    with nexora.app.app_context():
        u = nexora.User(name="S", email=f"ann{uuid4().hex[:7]}@carleton.ca")
        u.password_hash = nexora.generate_password_hash("nexorademo1")
        nexora.db.session.add(u)
        nexora.db.session.commit()
        return u.id


def _announce(uid, title, body, published):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.CourseContent(
            id=uuid4().hex, user_id=uid, course_full="BIOL1103",
            content_type="announcement", title=title, content_text=body,
            published_at=published, source_uid=uuid4().hex, module_name="Announcements"))
        nexora.db.session.commit()


# --------------------------------------------------------------------------
# year awareness -- the reason it was removed
# --------------------------------------------------------------------------

def test_a_year_less_date_is_resolved_against_when_it_was_announced():
    """"Nov 7" in an announcement posted in October 2026 is November 2026."""
    assert nexora._find_date(REAL, date(2026, 10, 8)) == date(2026, 11, 7)


def test_the_same_words_announced_a_year_earlier_mean_a_different_year():
    assert nexora._find_date(REAL, date(2025, 10, 8)) == date(2025, 11, 7)


def test_a_stated_time_is_read_in_twenty_four_hour_form():
    assert nexora._find_time(REAL) == "14:00"


# --------------------------------------------------------------------------
# what reaches the model at all
# --------------------------------------------------------------------------

def test_an_announcement_is_not_vetoed_by_an_incidental_word(client):
    """_looks_like_an_exam is `_EXAM_WORDS and not _NOT_AN_EXAM`, and
    _NOT_AN_EXAM exists to stop the OLD panel offering "exam review session".
    Used as a gate on what the model may see, it threw away the very
    announcement this was built for -- on the word "review"."""
    assert nexora._looks_like_an_exam(REAL) is False      # the old gate refuses it
    assert nexora._EXAM_WORDS.search(REAL)                # the new one admits it

    uid = _user(client)
    _announce(uid, "Midterm exam date", REAL, "2026-10-08T15:23:00.000Z")
    with nexora.app.app_context():
        found = nexora._announcement_candidates(uid, TODAY, nexora.timezone.utc)
    assert [r.title for r, _ref, _b in found] == ["Midterm exam date"]


def test_an_announcement_with_no_date_is_never_a_candidate(client):
    uid = _user(client)
    _announce(uid, "Marks released",
              "Your midterm marks have been released on Brightspace.",
              "2026-10-07T12:00:00.000Z")
    with nexora.app.app_context():
        assert nexora._announcement_candidates(uid, TODAY, nexora.timezone.utc) == []


def test_an_old_announcement_is_not_read_at_all(client):
    """Importing every term means every term's announcements are on file."""
    uid = _user(client)
    _announce(uid, "Midterm exam date", REAL, "2024-10-08T15:23:00.000Z")
    with nexora.app.app_context():
        assert nexora._announcement_candidates(uid, TODAY, nexora.timezone.utc) == []


# --------------------------------------------------------------------------
# what the student is actually offered
# --------------------------------------------------------------------------

def test_a_date_already_on_the_calendar_is_not_offered_again(client):
    """Offering a midterm the student already has is how this became noise."""
    uid = _user(client)
    with nexora.app.app_context():
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=uid, title="Midterm Exam", base_title="Midterm Exam",
            group_id="g1", due_raw="2026-11-07", course_full="BIOL1103",
            course_prefix="BIOL1103", kind="exam", is_deadline=True, source="manual",
            sync_state="active", created_at="2026-10-01 00:00:00"))
        nexora.db.session.commit()
        assert nexora._already_on_the_calendar(uid, "BIOL1103", date(2026, 11, 7),
                                               "Midterm Exam") is True


def test_a_day_either_side_counts_as_already_having_it(client):
    """A timezone or an off-by-one in the announcement must not duplicate it."""
    uid = _user(client)
    with nexora.app.app_context():
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=uid, title="Midterm Exam", base_title="Midterm Exam",
            group_id="g1", due_raw="2026-11-08", course_full="BIOL1103",
            course_prefix="BIOL1103", kind="exam", is_deadline=True, source="manual",
            sync_state="active", created_at="2026-10-01 00:00:00"))
        nexora.db.session.commit()
        assert nexora._already_on_the_calendar(uid, "BIOL1103", date(2026, 11, 7),
                                               "Midterm Exam") is True


def test_another_students_entry_does_not_count_as_yours(client):
    mine, theirs = _user(client), _user(client)
    with nexora.app.app_context():
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=theirs, title="Midterm Exam", base_title="Midterm Exam",
            group_id="g1", due_raw="2026-11-07", course_full="BIOL1103",
            course_prefix="BIOL1103", kind="exam", is_deadline=True, source="manual",
            sync_state="active", created_at="2026-10-01 00:00:00"))
        nexora.db.session.commit()
        assert nexora._already_on_the_calendar(mine, "BIOL1103", date(2026, 11, 7),
                                               "Midterm Exam") is False


def test_the_scan_writes_nothing_by_itself(client):
    """It returns a preview in the assistant's shape; the student confirms."""
    uid = _user(client)
    _announce(uid, "Midterm exam date", REAL, "2026-10-08T15:23:00.000Z")
    with nexora.app.app_context():
        before = nexora.Task.query.filter_by(user_id=uid).count()
        nexora._announcement_candidates(uid, TODAY, nexora.timezone.utc)
        assert nexora.Task.query.filter_by(user_id=uid).count() == before


# --------------------------------------------------------------------------
# The calendar checks by itself -- but only when it is worth a Gemini call.
# --------------------------------------------------------------------------

def _signed_in(client):
    client.post("/signup", data={"name": "S", "email": f"cal{uuid4().hex[:7]}@carleton.ca",
                                 "password": "nexorademo1"})
    with nexora.app.app_context():
        return nexora.User.query.order_by(nexora.User.id.desc()).first().id


def test_a_calendar_with_nothing_announced_never_spends_a_call(client):
    """The decision to scan is plain SQL and a regex. No candidates, no call."""
    _signed_in(client)
    body = client.get("/calendar").get_data(as_text=True)
    assert "auto: true" not in body


def test_a_new_dated_announcement_makes_the_calendar_check(client):
    uid = _signed_in(client)
    _announce(uid, "Midterm exam date", REAL, "2026-10-08T15:23:00.000Z")
    body = client.get("/calendar").get_data(as_text=True)
    assert "auto: true" in body


def test_it_does_not_check_again_straight_away(client):
    """Opening the calendar ten times a day must not cost ten Gemini calls."""
    uid = _signed_in(client)
    _announce(uid, "Midterm exam date", REAL, "2026-10-08T15:23:00.000Z")
    assert "auto: true" in client.get("/calendar").get_data(as_text=True)
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(id=uid).first()
        user.announce_scan_at = nexora.datetime.utcnow()
        nexora.db.session.commit()
    assert "auto: true" not in client.get("/calendar").get_data(as_text=True)


def test_the_button_still_works_however_recently_it_scanned(client):
    """Pressing it is the student asking; a throttle on the automatic check must
    not disable the manual one."""
    uid = _signed_in(client)
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(id=uid).first()
        user.announce_scan_at = nexora.datetime.utcnow()
        nexora.db.session.commit()
    assert 'id="scanAnn"' in client.get("/calendar").get_data(as_text=True)


# --------------------------------------------------------------------------
# Exams only
# --------------------------------------------------------------------------

def test_the_exams_only_filter_is_on_every_month(client):
    """It lived inside the per-month course legend, so it vanished on a month
    with nothing in it -- exactly when someone is flicking ahead for tests."""
    _signed_in(client)
    body = client.get("/calendar?month=2030-07").get_data(as_text=True)
    assert 'id="examsOnly"' in body
    # no course chips on a month with nothing in it -- checking for the word
    # "Courses" anywhere in the page matched the rail and proved nothing
    assert 'class="lg lgc"' not in body
    assert ".cal.exams-only .ev:not(.major){display:none}" in body


def test_filtering_hides_rather_than_removes(client):
    """The editor, the colour map and the day cells all read those rows.
    Removing them to filter a view is how a calendar disagrees with itself."""
    _signed_in(client)
    body = client.get("/calendar").get_data(as_text=True)
    assert "display:none" in body.split(".cal.exams-only")[1][:60]


# --------------------------------------------------------------------------
# "Plan my day" planned from every task ever imported, oldest first.
# --------------------------------------------------------------------------

def _task_for(uid, title, due, source="ics"):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=uid, title=title, base_title=title,
            group_id=uuid4().hex, due_raw=due, course_full="SYSC2004",
            course_prefix="SYSC2004", kind="due", is_deadline=True, source=source,
            sync_state="active", created_at="2026-10-01 00:00:00"))
        nexora.db.session.commit()


def test_plan_my_day_ignores_work_from_a_previous_year(client):
    """An account that imported first year was told its next five hours were
    "Lab 0 Pre-lab quiz, Sep 07 2025" -- while the dashboard beside it correctly
    said one thing was due."""
    uid = _signed_in(client)
    _task_for(uid, "Lab 0 Pre-lab quiz", "2025-09-07")
    _task_for(uid, "Lab 1 Pre-lab quiz", "2025-09-14")
    data = client.get("/api/start-my-day").get_json()
    titles = [p["title"] for p in data["plan"]]
    assert "Lab 0 Pre-lab quiz" not in titles
    assert "Lab 1 Pre-lab quiz" not in titles
    assert data["count"] == 0


def test_plan_my_day_still_plans_work_that_is_actually_due(client):
    uid = _signed_in(client)
    with nexora.app.app_context():
        soon = (nexora._today_for_request() + nexora.timedelta(days=2)).isoformat()
    _task_for(uid, "Assignment 2", soon)
    data = client.get("/api/start-my-day").get_json()
    assert [p["title"] for p in data["plan"]] == ["Assignment 2"]
    assert data["total_minutes"] > 0


def test_plan_my_day_agrees_with_the_dashboard_queue(client):
    """They read one list now, because two screens disagreeing about what to do
    next is the same class of bug as two disagreeing about a grade."""
    uid = _signed_in(client)
    with nexora.app.app_context():
        soon = (nexora._today_for_request() + nexora.timedelta(days=1)).isoformat()
    _task_for(uid, "Old thing", "2025-09-07")
    _task_for(uid, "Real thing", soon)
    planned = [p["title"] for p in client.get("/api/start-my-day").get_json()["plan"]]
    page = client.get("/dashboard").get_data(as_text=True)
    assert planned == ["Real thing"]
    assert "Old thing" not in page


# --------------------------------------------------------------------------
# The outline states dates too, and it gets revised.
# --------------------------------------------------------------------------

def _outline(uid, course, dates):
    import json as _json
    with nexora.app.app_context():
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=uid, course_full=course,
            outline_title="Outline",
            grading_json=_json.dumps({"important_dates": dates})))
        nexora.db.session.commit()


def test_a_date_the_outline_states_is_offered(client):
    """The pipeline already pulled these out; the calendar never saw them."""
    uid = _signed_in(client)
    _outline(uid, "SYSC2310", [{"description": "Midterm 2", "date": "November 30, 2026"}])
    with nexora.app.app_context():
        found = nexora._outline_dated_assessments(uid, TODAY, nexora.timezone.utc)
    assert [(f["course"], f["date"].isoformat()) for f in found] == [("SYSC2310", "2026-11-30")]


def test_a_time_and_place_in_the_outline_are_kept(client):
    uid = _signed_in(client)
    _outline(uid, "SYSC2310", [{"description": "Midterm 2 at 6pm in Southam Hall",
                                "date": "October 30, 2026"}])
    with nexora.app.app_context():
        found = nexora._outline_dated_assessments(uid, TODAY, nexora.timezone.utc)
    assert found[0]["time"] == "18:00"
    assert "Southam" in found[0]["location"]


def test_reading_the_outline_costs_no_model_call(client):
    """It is already structured. Re-reading it with an LLM would spend quota to
    learn nothing -- and it is what lets a REVISED outline be picked up free."""
    uid = _signed_in(client)
    _outline(uid, "SYSC2310", [{"description": "Final exam", "date": "December 12, 2026"}])
    body = client.get("/calendar").get_data(as_text=True)
    assert "auto: true" in body          # the banner is offered...
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(id=uid).first()
        assert user.announce_scan_at is None   # ...without having spent a call


def test_a_revised_outline_brings_the_new_date_with_it(client):
    """"Sometimes it gets updated." A re-analysis rewrites grading_json, and the
    next scan reads whatever it now says."""
    import json as _json
    uid = _signed_in(client)
    _outline(uid, "SYSC2310", [{"description": "Midterm 2", "date": "November 30, 2026"}])
    with nexora.app.app_context():
        prof = nexora.CourseOutlineProfile.query.filter_by(user_id=uid).first()
        prof.grading_json = _json.dumps({"important_dates": [
            {"description": "Midterm 2", "date": "December 4, 2026"}]})
        nexora.db.session.commit()
        found = nexora._outline_dated_assessments(uid, TODAY, nexora.timezone.utc)
    assert [f["date"].isoformat() for f in found] == ["2026-12-04"]


def test_an_outline_date_already_on_the_calendar_is_not_offered(client):
    uid = _signed_in(client)
    _outline(uid, "SYSC2310", [{"description": "Midterm 2", "date": "November 30, 2026"}])
    with nexora.app.app_context():
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=uid, title="Midterm 2", base_title="Midterm 2",
            group_id="g9", due_raw="2026-11-30", course_full="SYSC2310",
            course_prefix="SYSC2310", kind="exam", is_deadline=True, source="manual",
            sync_state="active", created_at="2026-10-01 00:00:00"))
        nexora.db.session.commit()
    body = client.get("/calendar").get_data(as_text=True)
    assert "auto: true" not in body
