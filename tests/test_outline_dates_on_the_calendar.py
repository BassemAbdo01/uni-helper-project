"""Part 2: the dates a confirmed outline states go on the calendar by themselves.

They were already extracted -- the pipeline pulls `important_dates` out of
every analysed outline -- and the calendar, the one place a student goes to
find out when things are, only offered them behind a button labelled "Check
announcements" that also spends a Gemini call.

Writing them without asking is only fair because nothing about them is a
guess: no model runs, and the student has confirmed the analysis. Three things
have to hold, and each one is a way the old "exams found in your course
material" panel went wrong:

- **current term only.** "Import everything" means a third-year account
  carries first-year outlines, and a date printed as "October 30" states no
  year, so it anchors to THIS October. That panel was deleted for offering a
  first-year MATH 1004 test as "Tue, Sep 29" this September.
- **never twice, and never again after a delete.** `delete_event` dismisses an
  imported row rather than removing it, so the uid survives and the date is
  not written back on the next page load.
- **the undo is the ordinary delete**, which is what makes the first point
  survivable at all.
"""

import json
import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture()
def student(client):
    email = f"od{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": PASSWORD})
    assert client.post("/api/timezone",
                       json={"timezone": "America/Toronto"}).status_code == 200
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        return user.id, nexora._student_today(user)


def course(user_id, code, term):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user_id, source_uid=f"ou-{code}",
            course_code=code, course_name=code, term=term))
        nexora.db.session.commit()


def outline(user_id, code, dates, confirmed=True):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user_id, course_full=code,
            grading_json=json.dumps({
                "confirmed": confirmed,
                "components": [{"name": "Midterm", "weight": 100}],
                "important_dates": [
                    {"description": d["what"], "date": d["when"]} for d in dates],
            })))
        nexora.db.session.commit()


def this_term(today):
    return nexora._current_term_label(today)


def last_year(today):
    season, year = nexora._term_meta(this_term(today))
    return f"{season} {year - 2}"


def tasks(user_id):
    with nexora.app.app_context():
        return [(t.course_full, t.due_raw, t.title, t.source, t.sync_state)
                for t in nexora.Task.query.filter_by(user_id=user_id).all()]


def apply_dates(client):
    res = client.post("/api/calendar/outline-dates")
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


# ------------------------------------------------------------ it works

def test_a_date_the_outline_states_lands_on_the_calendar(client, student):
    uid, today = student
    when = today + timedelta(days=21)
    course(uid, "SYSC2004", this_term(today))
    outline(uid, "SYSC2004", [{"what": "Midterm 2", "when": when.isoformat()}])

    data = apply_dates(client)
    assert data["added"] == 1, data
    rows = tasks(uid)
    assert len(rows) == 1
    assert rows[0][0] == "SYSC2004" and rows[0][1] == when.isoformat()
    assert rows[0][3] == "outline"


def test_it_is_not_charged_against_the_ai_allowance(client, student):
    """No model runs. Charging it against the quota that protects outline
    analysis would be charging for a database read."""
    uid, today = student
    course(uid, "SYSC2004", this_term(today))
    outline(uid, "SYSC2004", [{"what": "Midterm", "when": (today + timedelta(days=9)).isoformat()}])
    for _ in range(25):
        assert client.post("/api/calendar/outline-dates").status_code == 200


def test_a_get_cannot_spend_it(client, student):
    """POST for the reason /api/tour is: it writes, and a prefetch must not."""
    assert client.get("/api/calendar/outline-dates").status_code == 405


# ---------------------------------------------- the current-term gate

def test_a_first_year_outline_is_never_written_to_this_term(client, student):
    """The bug that got the old panel deleted. "October 30" states no year, so
    `_find_date` anchors it to this October -- the date is plausible, current,
    and from a course the student finished two years ago."""
    uid, today = student
    course(uid, "MATH1004", last_year(today))
    outline(uid, "MATH1004", [{"what": "Test 2", "when": (today + timedelta(days=14)).isoformat()}])

    assert apply_dates(client)["added"] == 0
    assert tasks(uid) == []


def test_the_gate_lets_this_term_through_from_the_same_account(client, student):
    """The other half -- without it the test above would pass on a feature
    that writes nothing at all."""
    uid, today = student
    course(uid, "MATH1004", last_year(today))
    course(uid, "SYSC2004", this_term(today))
    outline(uid, "MATH1004", [{"what": "Old Test", "when": (today + timedelta(days=14)).isoformat()}])
    outline(uid, "SYSC2004", [{"what": "Midterm 2", "when": (today + timedelta(days=15)).isoformat()}])

    assert apply_dates(client)["added"] == 1
    assert [t[0] for t in tasks(uid)] == ["SYSC2004"]


def test_a_course_with_no_term_at_all_is_left_out(client, student):
    """A course Brightspace never dated and that has no deadlines of its own
    answers neither question. Inventing a term is how the old panel went
    wrong, and this one WRITES."""
    uid, today = student
    course(uid, "GHOST1000", "")
    outline(uid, "GHOST1000", [{"what": "Test", "when": (today + timedelta(days=10)).isoformat()}])
    assert apply_dates(client)["added"] == 0


def test_a_course_with_no_stated_term_is_read_from_its_own_deadlines(client, student):
    """`_term_from_dates`, the same answer the dashboard's course groups give.
    A student who connected only the calendar feed has deadlines and no term."""
    uid, today = student
    course(uid, "FEED1000", "")
    client.post("/api/events", json={"title": "Lab 1", "course": "FEED1000",
                                     "date": (today + timedelta(days=3)).isoformat(),
                                     "kind": "due"})
    outline(uid, "FEED1000", [{"what": "Midterm", "when": (today + timedelta(days=20)).isoformat()}])
    assert apply_dates(client)["added"] == 1


# -------------------------------------------------- never twice, never back

def test_asking_twice_writes_once(client, student):
    """Three open tabs each ask. The uid is what stops the second one."""
    uid, today = student
    course(uid, "SYSC2004", this_term(today))
    outline(uid, "SYSC2004", [{"what": "Midterm 2", "when": (today + timedelta(days=21)).isoformat()}])
    assert apply_dates(client)["added"] == 1
    for _ in range(3):
        assert apply_dates(client)["added"] == 0
    assert len(tasks(uid)) == 1


def test_a_deleted_date_does_not_come_back(client, student):
    """The undo, and the worst way this could fail. `delete_event` dismisses an
    imported row rather than removing it, so the uid survives -- a row that was
    really deleted would be written again on the next page load, which is the
    delete looking broken."""
    uid, today = student
    course(uid, "SYSC2004", this_term(today))
    outline(uid, "SYSC2004", [{"what": "Midterm 2", "when": (today + timedelta(days=21)).isoformat()}])
    added = apply_dates(client)
    event_id = added["events"][0]["id"]

    assert client.delete(f"/api/events/{event_id}").status_code == 200
    assert apply_dates(client)["added"] == 0, "a deleted outline date was written back"

    rows = tasks(uid)
    assert len(rows) == 1 and rows[0][4] == "dismissed", rows
    # And it is off the calendar, not merely flagged.
    page = client.get(f"/calendar?month={(today + timedelta(days=21)).strftime('%Y-%m')}")
    assert "Midterm 2" not in page.get_data(as_text=True)


def test_a_date_the_student_already_has_is_not_duplicated(client, student):
    uid, today = student
    when = today + timedelta(days=21)
    course(uid, "SYSC2004", this_term(today))
    client.post("/api/events", json={"title": "Midterm 2", "course": "SYSC2004",
                                     "date": when.isoformat(), "kind": "exam"})
    outline(uid, "SYSC2004", [{"what": "Midterm 2", "when": when.isoformat()}])
    assert apply_dates(client)["added"] == 0


# -------------------------------------------------------- what it declines

def test_an_unconfirmed_outline_is_not_written(client, student):
    """A reading of an outline nobody has checked. `_compute_official_result`
    refuses to report a grade from one; writing dates out of it would be the
    app trusting it in one place and not the other. It stays a suggestion in
    the review banner."""
    uid, today = student
    course(uid, "SYSC2004", this_term(today))
    outline(uid, "SYSC2004",
            [{"what": "Midterm 2", "when": (today + timedelta(days=21)).isoformat()}],
            confirmed=False)
    assert apply_dates(client)["added"] == 0
    assert tasks(uid) == []


def test_a_date_that_has_passed_is_not_written(client, student):
    uid, today = student
    course(uid, "SYSC2004", this_term(today))
    outline(uid, "SYSC2004", [{"what": "Midterm 1", "when": (today - timedelta(days=5)).isoformat()}])
    assert apply_dates(client)["added"] == 0


def test_more_than_the_cap_is_never_written_in_one_go(client, student):
    """An undo that has to remove forty entries is not an undo."""
    uid, today = student
    course(uid, "SYSC2004", this_term(today))
    outline(uid, "SYSC2004", [
        {"what": f"Quiz {n}", "when": (today + timedelta(days=3 + n)).isoformat()}
        for n in range(nexora.OUTLINE_DATE_ADD_MAX + 6)])
    assert apply_dates(client)["added"] == nexora.OUTLINE_DATE_ADD_MAX


def test_the_page_only_asks_when_there_is_something_to_do(client, student):
    uid, today = student
    course(uid, "SYSC2004", this_term(today))
    outline(uid, "SYSC2004", [{"what": "Midterm 2", "when": (today + timedelta(days=21)).isoformat()}])
    assert "/api/calendar/outline-dates" in client.get("/calendar").get_data(as_text=True)
    apply_dates(client)
    assert "/api/calendar/outline-dates" not in client.get("/calendar").get_data(as_text=True)


# ------------------------------------------- "cannot say" is a third answer

def test_a_course_that_cannot_be_placed_is_still_OFFERED(student, client):
    """The first cut of the term gate excluded anything not provably current,
    and that swallowed an outline a student had just pasted into the grade
    calculator -- a CourseOutlineProfile with no SyncedCourse row behind it.

    A missing course row is not evidence of an old term. It is evidence of no
    course row. The preview offers it with a tick beside it; only the WRITE
    refuses. `_evaluate_pass_rules` again: "no" and "cannot say" are different
    facts."""
    uid, today = student
    outline(uid, "PASTED1000",
            [{"what": "Midterm 2", "when": (today + timedelta(days=12)).isoformat()}])
    with nexora.app.app_context():
        found = nexora._outline_dated_assessments(uid, today, nexora._user_zone(
            nexora.User.query.get(uid)))
    assert [f["course"] for f in found] == ["PASTED1000"]
    assert found[0]["term_is_current"] is None


def test_a_course_known_to_be_in_another_term_is_not_even_offered(student, client):
    """The one answer that is dropped for every caller."""
    uid, today = student
    course(uid, "MATH1004", last_year(today))
    outline(uid, "MATH1004",
            [{"what": "Test 2", "when": (today + timedelta(days=12)).isoformat()}])
    with nexora.app.app_context():
        found = nexora._outline_dated_assessments(uid, today, nexora._user_zone(
            nexora.User.query.get(uid)))
    assert found == []


def test_a_course_taken_twice_is_current_if_its_current_offering_is(student, client):
    """Two SyncedCourse rows share a code when a course is taken twice. The
    newest offering decides, the same rule `_newest_offering_per_code` gives
    every other screen -- otherwise whichever row the query yielded last would
    decide whether this term's dates are written."""
    uid, today = student
    course(uid, "ECOR1031", last_year(today))
    course(uid, "ECOR1031", this_term(today))
    outline(uid, "ECOR1031",
            [{"what": "Midterm 2", "when": (today + timedelta(days=12)).isoformat()}])
    assert apply_dates(client)["added"] == 1


# ------------------------------------------ a date is not always an exam

def test_a_project_demo_is_not_badged_as_an_exam(student, client):
    """Found by running it. `_exam_title` returns the literal "Exam" when it
    finds no exam phrase, which was right for its old caller and is a
    confidently wrong label on a calendar -- and two such rows reduce to one
    stem, so the second would be dropped as a duplicate of the first.

    `kind` had the same shape of bug, hardcoded "exam": a project demo badged
    EXAM wears the loudest mark this calendar has and earns the study
    planner's revision ladder."""
    uid, today = student
    course(uid, "SYSC2004", this_term(today))
    outline(uid, "SYSC2004", [
        {"what": "Project demo", "when": (today + timedelta(days=10)).isoformat()},
        {"what": "Poster session", "when": (today + timedelta(days=11)).isoformat()},
        {"what": "Midterm 2", "when": (today + timedelta(days=12)).isoformat()},
    ])
    assert apply_dates(client)["added"] == 3
    with nexora.app.app_context():
        rows = {t.title: t.kind for t in nexora.Task.query.filter_by(
            user_id=uid, source="outline").all()}
    assert rows == {"Project demo": "due", "Poster session": "due",
                    "Midterm 2": "exam"}, rows
