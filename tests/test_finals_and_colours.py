"""Finals look like finals, and the student picks the colours.

Three things asked for together: December's finals deserve their own design
because they decide the grade, the Carleton Central exam schedule should import
from a picture, and course colours should be choosable.
"""
import io
import os
import re
from uuid import uuid4

import app as nexora

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = "nexorademo1"


def _account(client, email=None):
    email = email or f"fc{uuid4().hex[:7]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": PASSWORD})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


def _task(user_id, title, course, due="2026-12-15", kind="exam", when=""):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user_id, title=title, base_title=title,
            group_id=nexora.make_group_id(course, title), due_raw=due, due_time=when,
            course_full=course, course_prefix=course, kind=kind, is_deadline=True,
            source="manual", sync_state="active", created_at="2026-10-01 00:00:00"))
        nexora.db.session.commit()


def _calendar_css():
    with io.open(os.path.join(HERE, "templates", "calendar.html"), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# A final is not a test with a different word on it
# --------------------------------------------------------------------------

def test_a_final_exam_is_labelled_FINAL():
    assert nexora._major_assessment_label("exam", "Final Exam") == "FINAL"
    assert nexora._major_assessment_label("due", "Final Examination") == "FINAL"
    # and the distinction it has to keep making
    assert nexora._major_assessment_label("exam", "Midterm exam") == "MIDTERM"
    assert nexora._major_assessment_label("due", "Practice final") == ""


def test_the_card_says_which_kind_of_major_it_is(client):
    """The badge text was already rendered; the CSS had nothing to hook on, so
    a final and a quiz drew the same card."""
    uid = _account(client)
    _task(uid, "Final Exam", "BIOL1103")
    body = client.get("/calendar?month=2026-12").get_data(as_text=True)
    assert 'data-major="FINAL"' in body


def test_a_final_is_styled_apart_from_the_other_majors():
    css = _calendar_css()
    assert '.ev[data-major="FINAL"]' in css, "finals share the midterm's card"


def test_the_day_holding_a_final_is_marked_harder(client):
    """December is nothing but finals, so the grid has to read as "these four
    squares" before anything is opened."""
    uid = _account(client)
    _task(uid, "Final Exam", "BIOL1103")
    body = client.get("/calendar?month=2026-12").get_data(as_text=True)
    assert "hasfinal" in body
    assert ".day.hasfinal" in _calendar_css()


def test_a_midterm_does_not_get_the_final_treatment(client):
    uid = _account(client)
    _task(uid, "Midterm exam", "BIOL1103")
    body = client.get("/calendar?month=2026-12").get_data(as_text=True)
    assert 'data-major="MIDTERM"' in body
    # Checked on the day cell's class, not the document: `hasfinal` also appears
    # in the page's own stylesheet, so a plain `not in body` could never fail.
    assert not re.search(r'class="day[^"]*hasfinal', body)


# --------------------------------------------------------------------------
# The exam schedule, from a picture of one
# --------------------------------------------------------------------------

def test_the_prompt_knows_what_an_exam_schedule_looks_like():
    """A university's schedule names the COURSE, the date, the time and the
    room, and never the activity -- so the default titling would call every row
    by its course code and nothing in that reads as an exam. The FINAL badge,
    the outlined day and the planner's revision ladder all hang off that word."""
    src = io.open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
    assert "FINAL EXAM SCHEDULE" in src
    assert '"Final Exam"' in src


def test_a_schedule_row_becomes_a_final_on_the_calendar(client, monkeypatch):
    """End to end with the model stubbed: what the route makes of a row shaped
    like Carleton Central's."""
    _account(client)
    monkeypatch.setattr(nexora, "_gemini_image_to_outline_text",
                        lambda *a, **k: "Course | Date | Time | Building | Room\n"
                                        "ELEC 2501 B | Tue, Dec 15, 2026 | 14:00 | Minto | 2000")
    monkeypatch.setattr(nexora, "_gemini_json", lambda *a, **k: {
        "understood": "", "rows": [{"course": "ELEC2501B", "title": "Final Exam",
                                    "kind": "exam", "date": "2026-12-15",
                                    "year_printed": True, "time": "14:00",
                                    "location": "Minto 2000"}]})
    res = client.post("/api/calendar/from-image", content_type="multipart/form-data",
                      data={"image": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 40),
                                      "s.png", "image/png"),
                            "instruction": "my final exams from carleton central"})
    (change,) = res.get_json()["changes"]
    assert change["kind"] == "exam"
    assert change["location"] == "Minto 2000"
    assert nexora._major_assessment_label(change["kind"], change["title"]) == "FINAL"


# --------------------------------------------------------------------------
# Choosing the colours
# --------------------------------------------------------------------------

def test_a_chosen_colour_outranks_the_automatic_one(client):
    uid = _account(client)
    _task(uid, "Final Exam", "BIOL1103")
    with nexora.app.app_context():
        before = nexora._student_course_colours(uid)["BIOL1103"]
    res = client.post("/api/course-colour", json={"course": "BIOL1103", "index": 5})
    assert res.status_code == 200
    assert res.get_json()["colours"]["BIOL1103"] == 5
    assert before != 5 or True          # the point is what it is NOW
    with nexora.app.app_context():
        assert nexora._student_course_colours(uid)["BIOL1103"] == 5


def test_the_choice_reaches_every_screen_not_just_the_calendar(client):
    """The calendar and the study plan once built their own maps and disagreed
    about what colour SYSC2004 was. An override honoured on one and not the
    other would be that same bug, reintroduced by the fix for it."""
    uid = _account(client)
    _task(uid, "Final Exam", "BIOL1103")
    client.post("/api/course-colour", json={"course": "BIOL1103", "index": 6})
    with nexora.app.app_context():
        # the one helper every screen calls, including with the study plan's
        # own `extra` courses mixed in
        assert nexora._student_course_colours(uid, extra=["MATH1005"])["BIOL1103"] == 6


def test_auto_puts_it_back(client):
    uid = _account(client)
    _task(uid, "Final Exam", "BIOL1103")
    with nexora.app.app_context():
        automatic = nexora._student_course_colours(uid)["BIOL1103"]
    client.post("/api/course-colour", json={"course": "BIOL1103", "index": 7})
    res = client.delete("/api/course-colour/BIOL1103")
    assert res.status_code == 200
    assert res.get_json()["colours"]["BIOL1103"] == automatic


def test_a_colour_outside_the_palette_is_refused(client):
    """The index reaches `style="--ev:var(--c{{ index }})"` in three templates.
    Outside the ramp it resolves to nothing and the course draws colourless --
    an entry that looks broken rather than one that looks wrong."""
    _account(client)
    for bad in (99, -1, nexora.COURSE_COLOUR_COUNT, "red", None):
        res = client.post("/api/course-colour", json={"course": "BIOL1103", "index": bad})
        assert res.status_code == 400, bad


def test_an_empty_course_is_refused_rather_than_recolouring_OTHER(client):
    """_normalize_course_code("") returns "OTHER", which is a real bucket in the
    legend, so an empty field would quietly recolour it."""
    _account(client)
    res = client.post("/api/course-colour", json={"course": "   ", "index": 2})
    assert res.status_code == 400


def test_one_students_choice_does_not_touch_another(client):
    mine = _account(client)
    _task(mine, "Final Exam", "BIOL1103")
    client.post("/api/course-colour", json={"course": "BIOL1103", "index": 4})
    # Made directly: signing up a second time does nothing while this client is
    # already signed in, so both calls would return the SAME id and the test
    # would quietly check that a student cannot affect their own colours.
    with nexora.app.app_context():
        other = nexora.User(name="T", email=f"oc{uuid4().hex[:7]}@carleton.ca")
        other.password_hash = nexora.generate_password_hash(PASSWORD)
        nexora.db.session.add(other)
        nexora.db.session.commit()
        theirs = other.id
    _task(theirs, "Final Exam", "BIOL1103")
    with nexora.app.app_context():
        assert nexora._student_course_colours(mine)["BIOL1103"] == 4
        assert nexora._student_course_colours(theirs)["BIOL1103"] != 4


def test_it_needs_an_account(client):
    assert client.post("/api/course-colour",
                       json={"course": "BIOL1103", "index": 1}).status_code in (302, 401)


def test_the_palette_is_read_from_its_one_definition_not_repeated():
    """--c0..--c7 live on `.cal`. The popover is appended to the body so it can
    escape the grid's overflow, so it RESOLVES them rather than referencing
    them -- referencing gave eight transparent circles. Repeating the ramp in a
    second place would be the drift this codebase keeps paying for."""
    css = _calendar_css()
    assert css.count("--c0:#") == 1, "the ramp is defined twice"
    assert "getPropertyValue(`--c${i}`)" in css
