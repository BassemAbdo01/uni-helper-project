"""Part 2: the current grade and its letter on each dashboard course card.

The card must call `_compute_official_result` and nothing else. Two screens
disagreeing about whether someone passed is the worst thing this app does, and
the grade calculator's own standing panel has already printed **D-** beside an
official panel reading **FAIL** for the same student, because only the server
knows that a 49 on the lecture component fails the course.

It must also stay cheap. `_compute_official_result` runs two queries per
course and `DASHBOARD_COURSE_LIMIT` is 400, so asking per card would put eight
hundred queries on the page a student opens first.
"""

import os
import re
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture()
def student(client):
    email = f"cg{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": PASSWORD})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


def course(user_id, code, name="A Course", term="Fall 2026"):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user_id, source_uid=f"ou-{code}",
            course_code=code, course_name=name, term=term))
        nexora.db.session.commit()


SCHEME = {
    "confirmed": True,
    "pass_mark": 50,
    "components": [
        {"name": "Midterm", "weight": 40},
        {"name": "Final Exam", "weight": 60},
    ],
}


def policy(user_id, code, analysis=None, confirmed=True):
    body = dict(analysis or SCHEME)
    body["confirmed"] = confirmed
    import json
    with nexora.app.app_context():
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user_id, course_full=code,
            grading_json=json.dumps(body)))
        nexora.db.session.commit()


def mark(user_id, code, name, weight, score):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.GradeEntry(
            id=uuid4().hex, user_id=user_id, course_full=code,
            name=name, weight=weight, score=score))
        nexora.db.session.commit()


def card(html, code):
    """The one <article class="course"> for `code`."""
    for block in re.findall(r'<article class="course"[^>]*>.*?</article>', html, re.S):
        if f'data-course="{code}"' in block:
            return block
    return ""


# ------------------------------------------------------ it shows the grade

def test_the_card_shows_the_grade_and_the_letter(client, student):
    course(student, "SYSC2004")
    policy(student, "SYSC2004")
    mark(student, "SYSC2004", "Midterm", 40, 80)
    mark(student, "SYSC2004", "Final Exam", 60, 90)

    block = card(client.get("/dashboard").get_data(as_text=True), "SYSC2004")
    assert block, "no course card rendered"
    assert "86.0%" in block, block          # .4*80 + .6*90
    assert "<i>A</i>" in block               # 86 is an A; A+ starts at 90


def test_the_card_agrees_with_the_grade_calculator(client, student):
    """The property that matters. Not "it shows a number" -- that the number
    is the one `_compute_official_result` gives, which is what every other
    screen asks."""
    course(student, "BIOL1103")
    policy(student, "BIOL1103")
    mark(student, "BIOL1103", "Midterm", 40, 71)
    mark(student, "BIOL1103", "Final Exam", 60, 64)

    with nexora.app.app_context():
        result, error = nexora._compute_official_result(student, "BIOL1103")
    assert error is None, error
    expected = f"{result['official_grade']:.1f}%"

    block = card(client.get("/dashboard").get_data(as_text=True), "BIOL1103")
    assert expected in block, (expected, block)


def test_a_failing_rule_is_said_on_the_card(client, student):
    """FAIL is a real answer. A card printing 72% beside an official panel
    saying FAIL is the same bug as two screens disagreeing about a pass."""
    course(student, "ELEC2501")
    policy(student, "ELEC2501", {
        "confirmed": True, "pass_mark": 50,
        "components": [{"name": "Midterm", "weight": 40},
                       {"name": "Final Exam", "weight": 60}],
        "pass_rules": [{"type": "component_minimum", "component": "Final Exam",
                        "minimum_percent": 50,
                        "message": "At least 50% on the final examination"}],
    })
    mark(student, "ELEC2501", "Midterm", 40, 95)
    mark(student, "ELEC2501", "Final Exam", 60, 40)

    block = card(client.get("/dashboard").get_data(as_text=True), "ELEC2501")
    assert "FAIL" in block, block


# ------------------------------------------------- it stays quiet otherwise

def test_a_course_with_no_marks_says_nothing(client, student):
    """An empty grade slot on every card reads as the feature being broken."""
    course(student, "SYSC2310")
    policy(student, "SYSC2310")
    block = card(client.get("/dashboard").get_data(as_text=True), "SYSC2310")
    assert block and 'class="grade' not in block, block


def test_an_unconfirmed_policy_says_nothing(client, student):
    """`_compute_official_result` answers 409 for one, and a reading of an
    outline nobody has checked must not become a grade on the dashboard."""
    course(student, "CHEM1101")
    policy(student, "CHEM1101", confirmed=False)
    mark(student, "CHEM1101", "Midterm", 40, 80)
    block = card(client.get("/dashboard").get_data(as_text=True), "CHEM1101")
    assert block and 'class="grade' not in block, block


def test_a_course_with_no_outline_at_all_says_nothing(client, student):
    course(student, "MATH1004")
    mark(student, "MATH1004", "Quiz", 10, 90)
    block = card(client.get("/dashboard").get_data(as_text=True), "MATH1004")
    assert block and 'class="grade' not in block, block


# ------------------------------------------------------------- it is cheap

def test_it_does_not_ask_about_a_course_that_cannot_answer(client, student):
    """Two queries decide who is worth asking about, and the expensive call is
    made only for courses that have both a confirmed policy and a mark. Thirty
    empty courses must cost thirty nothings."""
    for n in range(30):
        course(student, f"DEAD{n:03d}")
    course(student, "SYSC2004")
    policy(student, "SYSC2004")
    mark(student, "SYSC2004", "Midterm", 40, 80)

    asked = []
    real = nexora._compute_official_result

    def counting(user_id, code, *a, **kw):
        asked.append(code)
        return real(user_id, code, *a, **kw)

    nexora._compute_official_result = counting
    try:
        client.get("/dashboard")
    finally:
        nexora._compute_official_result = real
    assert asked == ["SYSC2004"], asked


def test_the_number_of_graded_cards_is_capped(client, student):
    """A guard against a pathological account, not a page size."""
    assert nexora.DASHBOARD_GRADE_MAX >= 20
    with nexora.app.app_context():
        codes = [f"C{n:03d}" for n in range(nexora.DASHBOARD_GRADE_MAX + 5)]
    for code in codes:
        course(student, code)
        policy(student, code)
        mark(student, code, "Midterm", 40, 80)
    with nexora.app.app_context():
        found = nexora._dashboard_course_grades(student, codes)
    assert len(found) == nexora.DASHBOARD_GRADE_MAX, len(found)


def test_an_outline_it_cannot_read_does_not_take_the_dashboard_down(client, student):
    """A card is decoration next to the page it sits on."""
    course(student, "WEIRD1000")
    policy(student, "WEIRD1000", {"confirmed": True,
                                  "grading_options": "not a list at all"})
    mark(student, "WEIRD1000", "Midterm", 40, 80)
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert card(page.get_data(as_text=True), "WEIRD1000")
