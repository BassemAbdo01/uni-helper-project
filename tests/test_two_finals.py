"""A course with a lecture final and a lab final.

`\\bfinal\\b` matches both, and BIOL 1103 has both. The single-scheme solver was
fixed first; four other places kept the same blind spot, and the worst of them
was `_evaluate_option`, where the hypothetical final mark was handed to every
component containing the word "final" at once. The required-final answer for an
alternative scheme was then a number the student could not act on.

There is one definition now -- `_final_name_score` / `_pick_final_name` -- and
these tests pin each site that consumes it.
"""

import json
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"

TWO_FINALS = {
    "confirmed": True, "pass_mark": 50,
    "components": [
        {"name": "Term Work", "weight": 40, "category": "assignments"},
        {"name": "Final Exam", "weight": 40, "category": "final"},
        {"name": "Final Lab Exam", "weight": 20, "category": "labs"},
    ],
    "grading_options": [
        {"option_name": "Standard", "components": [
            {"name": "Term Work", "weight": 40},
            {"name": "Final Exam", "weight": 40},
            {"name": "Final Lab Exam", "weight": 20},
        ]},
    ],
    "rules": [], "component_minimums": [],
}

# Term work 50% (20 points banked), lab exam 0%, the final not yet sat.
MARKS = {"Term Work": 50, "Final Exam": None, "Final Lab Exam": 0}


def _seed(nexora, email, marks):
    user = nexora.User.query.filter_by(email=email).first()
    nexora.db.session.add(nexora.CourseOutlineProfile(
        id=uuid4().hex, user_id=user.id, course_full="SYSC2100",
        grading_json=json.dumps(TWO_FINALS), pass_mark=50))
    rows = []
    for comp in TWO_FINALS["components"]:
        row = nexora.GradeEntry(
            id=uuid4().hex, user_id=user.id, course_full="SYSC2100",
            name=comp["name"], weight=comp["weight"], category=comp["category"],
            score=marks[comp["name"]], max_score=100)
        nexora.db.session.add(row)
        rows.append(row)
    nexora.db.session.commit()
    return user.id, rows


@pytest.fixture()
def two_finals(client):
    import app as nexora
    client.post("/signup", data={"name": "S", "email": "twofinals@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        return _seed(nexora, "twofinals@carleton.ca", MARKS)[0]


# ------------------------------------------------ the alternative-scheme path

def test_the_lecture_final_is_the_one_the_option_overrides(two_finals):
    """Not the lab exam, and not both."""
    import app as nexora
    with nexora.app.app_context():
        rows = nexora.GradeEntry.query.filter_by(user_id=two_finals,
                                                 course_full="SYSC2100").all()
        ev = nexora._evaluate_option(TWO_FINALS["grading_options"][0],
                                     nexora._grade_row_map(rows),
                                     final_override=100, analysis=TWO_FINALS)
    assert ev["final_source"] == "Final Exam"


def test_only_one_component_receives_the_hypothetical_mark(two_finals):
    """20 points of term work, a perfect 40-point final, a zero lab exam = 60.
    Overriding both finals gave 80, and the solver then reported a required
    final far below the truth."""
    import app as nexora
    with nexora.app.app_context():
        rows = nexora.GradeEntry.query.filter_by(user_id=two_finals,
                                                 course_full="SYSC2100").all()
        ev = nexora._evaluate_option(TWO_FINALS["grading_options"][0],
                                     nexora._grade_row_map(rows),
                                     final_override=100, analysis=TWO_FINALS)
    assert ev["grade"] == pytest.approx(60.0)


def test_the_required_final_is_solved_against_the_real_final(two_finals, client):
    """0.4 x final + 20 banked >= 50 needs 75%. Overriding the lab exam as well
    made 50% look like enough."""
    body = client.post("/api/course/SYSC2100/grade-outcomes",
                       json={"target": 50}).get_json()
    assert body["mode"] == "multiple_options"
    assert body["best_required_final"] is not None
    assert body["best_required_final"]["required_final"] == pytest.approx(75.0, abs=0.2)


def test_a_missing_lab_exam_still_blocks_the_solve(client):
    """Only the overridden component's "not entered" message may be ignored.
    Matching the words "final exam" also swallowed a missing "Final Lab Exam"
    and solved a target that could not be reached."""
    import app as nexora
    client.post("/signup", data={"name": "S", "email": "gap@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        _seed(nexora, "gap@carleton.ca",
              {"Term Work": 50, "Final Exam": None, "Final Lab Exam": None})
    body = client.post("/api/course/SYSC2100/grade-outcomes",
                       json={"target": 50}).get_json()
    assert body["best_required_final"] is None, \
        "solved a target while the lab exam was still unmarked"


def test_a_rule_about_the_lab_exam_is_not_answered_with_the_final(client):
    """A component_minimum on "Final Lab Exam" is not a rule about the final,
    so a perfect hypothetical final must not satisfy it."""
    import app as nexora
    analysis = dict(TWO_FINALS, pass_rules=[
        {"type": "component_minimum", "component": "Final Lab Exam",
         "minimum_percent": 50, "message": "Must pass the lab exam"}])
    client.post("/signup", data={"name": "S", "email": "labrule@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        _user, rows = _seed(nexora, "labrule@carleton.ca", MARKS)
        _ok, results = nexora._evaluate_pass_rules(
            analysis, nexora._grade_row_map(rows), 60.0, final_override=100)
    lab = next(r for r in results if "lab" in str(r["message"]).lower())
    assert lab["passed"] is False, "the lab-exam rule was answered with the final's mark"


def test_a_group_overrides_only_its_final_member():
    """_group_state had the same blind spot: an aggregate holding both finals
    got two overridden components."""
    import app as nexora

    class Row:
        def __init__(self, name, weight, score):
            self.name, self.weight, self.score = name, weight, score
            self.max_score, self.category = 100, "assessment"

    rows = [Row("Term Work", 40, 50), Row("Final Exam", 40, None),
            Row("Final Lab Exam", 20, 0)]
    percent, state = nexora._group_state(
        TWO_FINALS, nexora._grade_row_map(rows),
        ["Term Work", "Final Exam", "Final Lab Exam"], final_override=100)
    # 50% of 40, 100% of 40, 0% of 20, over 100 weight.
    assert state == "ok"
    assert percent == pytest.approx(60.0, abs=0.01)


# ------------------------------------------------------------- the scorer

@pytest.mark.parametrize("name,expected", [
    ("Final Exam", 4), ("final examination", 4), ("  Final Exam  ", 4),
    ("Final Exam (cumulative)", 3),
    ("Final", 2), ("Final Assessment", 2),
    ("Final Lab Exam", 1), ("Lab Final", 1),
    ("Midterm", 0), ("Assignments", 0), ("", 0), (None, 0),
])
def test_how_much_a_name_means_the_final_exam(name, expected):
    import app as nexora
    assert nexora._final_name_score(name) == expected


def test_the_heavier_component_wins_a_tie():
    import app as nexora
    assert nexora._pick_final_name(
        [("Final Project", 10), ("Final Report", 30)]) == "Final Report"


def test_the_lecture_final_beats_the_lab_final_even_when_lighter():
    """Score first, weight only as the tie-break: a 12% lecture final is still
    the final when the lab exam is worth 40%."""
    import app as nexora
    assert nexora._pick_final_name(
        [("Final Lab Exam", 40), ("Final Exam", 12)]) == "Final Exam"


def test_a_course_with_no_final_gets_no_guess():
    import app as nexora
    assert nexora._pick_final_name([("Midterm", 40), ("Essay", 60)]) is None
