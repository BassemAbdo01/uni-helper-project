"""A midterm must not look like a discussion-board post.

Every entry on the calendar rendered as the same small card with a 2px
coloured border -- "Final Exam" beside "Subscribe to the course discussion
board", identical weight, identical size. A term has three or four sittings
that decide the grade and they were invisible in a month of forty items.

Two things fix it, and both were asked for by name:

  * the three or four that matter are tagged, outlined and counted down to
  * every course keeps one colour, with a legend saying which is which

The trap is over-flagging. `_EXAM_WORDS` matches "quiz", and a course with a
weekly pre-lab quiz would light up every square -- which is the same as
lighting up none. "Midterm review session" and "Practice Final" are not the
sitting either, and a calendar that shouts at the wrong squares stops being
read.
"""
import os
import sys
from datetime import timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402


# --------------------------------------------------------------------------
# What counts as major
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Final Exam", "FINAL"),
    ("Final Examination", "FINAL"),
    ("FINAL", "FINAL"),
    ("Midterm 1", "MIDTERM"),
    ("Midterm Examination", "MIDTERM"),
    ("Mid-term test", "MIDTERM"),
    ("Unit 2 Test", "TEST"),
    ("Test 3", "TEST"),
    ("Exam 2", "EXAM"),
])
def test_the_things_that_decide_a_grade_are_labelled(title, expected):
    assert nexora._major_assessment_label("due", title) == expected


@pytest.mark.parametrize("title", [
    "Midterm review session",        # revising for it is not sitting it
    "Practice Final (ungraded)",
    "Past midterm solutions",
    "Final exam study guide",
    "Weekly Quiz 4",                 # a quiz is not a midterm
    "Pre-lab Quiz 2",
    "Lab Report 2",
    "Subscribe to the course discussion board",
    "Install IntelliJ on your computer",
])
def test_ordinary_work_is_not_flagged(title):
    """Flagging everything is the same as flagging nothing."""
    assert nexora._major_assessment_label("due", title) == ""


def test_an_imported_midterm_is_caught_by_its_title():
    """Brightspace hands over a midterm as kind='due', exactly like a
    discussion post -- it does not say which one is the exam. The title is the
    only signal for imported deadlines, which is nearly all of them."""
    assert nexora._major_assessment_label("due", "Midterm 1") == "MIDTERM"


def test_a_hand_added_exam_is_caught_by_its_kind():
    """And a student who adds one by hand may title it anything."""
    assert nexora._major_assessment_label("exam", "Chem in Ottawa Hall") == "EXAM"


# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------

def test_no_two_courses_share_a_colour():
    """The old rule was `sum(ord(ch) for ch in course) % 8`, which collides:
    any two codes whose letters happen to sum alike shared a colour, and a
    legend saying otherwise would be a lie."""
    codes = ["BIOL1103", "SYSC2004", "SYSC2310", "ELEC2501", "MATH1005", "COMP1405"]
    colours = nexora._course_colour_map(codes)
    assert len(set(colours.values())) == len(codes)


def test_the_old_hash_really_did_collide():
    """Pins the reason this changed, so nobody 'simplifies' it back."""
    def old(course):
        return sum(ord(ch) for ch in course) % 8
    # An anagram is the easy proof: same letters, same sum, same colour.
    assert old("SYSC2004") == old("SYSC4002")
    assert nexora._course_colour_map(["SYSC2004", "SYSC4002"])["SYSC2004"] != \
        nexora._course_colour_map(["SYSC2004", "SYSC4002"])["SYSC4002"]


def test_a_course_keeps_its_colour_whatever_month_is_open():
    """A course that is green in September must be green in October."""
    codes = ["BIOL1103", "SYSC2004", "SYSC2310"]
    assert nexora._course_colour_map(codes) == nexora._course_colour_map(reversed(codes))


def test_blank_courses_do_not_take_a_slot():
    colours = nexora._course_colour_map(["BIOL1103", "", None, "  ", "SYSC2004"])
    assert set(colours) == {"BIOL1103", "SYSC2004"}


# --------------------------------------------------------------------------
# On the page
# --------------------------------------------------------------------------

@pytest.fixture()
def student(client):
    email = f"cal{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "Rana Test", "email": email, "password": "cal-pass-123"})
    with nexora.app.app_context():
        uid = nexora.User.query.filter_by(email=email).first().id
        today = nexora._student_today(uid)
        for code, title, offset in [("BIOL1103", "Midterm Examination", 8),
                                    ("SYSC2004", "Lab Report 2", 9),
                                    ("SYSC2004", "Midterm review session", 7)]:
            nexora.db.session.add(nexora.Task(
                id=uuid4().hex, user_id=uid, course_full=code, title=title, base_title=title,
                kind="due", due_raw=(today + timedelta(days=offset)).isoformat(),
                group_id=uuid4().hex, is_deadline=True, source="manual"))
        nexora.db.session.commit()
        month = (today + timedelta(days=8)).strftime("%Y-%m")
    return month


def test_the_calendar_tags_the_midterm_and_carries_a_legend(client, student):
    html = client.get(f"/calendar?month={student}").get_data(as_text=True)
    assert 'class="ev major"' in html, "the midterm renders differently from a lab report"
    assert ">MIDTERM<" in html, "and says what it is"
    assert "hasmajor" in html, "the day itself is marked, so it is findable while scanning"
    assert "legend" in html and "BIOL1103" in html, "the colours are decodable"
    # The review session sits in the same month and must stay ordinary.
    assert html.count('class="ev major"') == 1


def test_a_major_entry_inside_two_weeks_counts_down(client, student):
    html = client.get(f"/calendar?month={student}").get_data(as_text=True)
    assert "IN 8 DAYS" in html, "a countdown says more than a date on the thing you revise for"
