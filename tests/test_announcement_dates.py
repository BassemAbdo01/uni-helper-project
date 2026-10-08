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
