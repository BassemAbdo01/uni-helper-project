"""Two ways the grading engine could be confidently wrong.

**A bonus is additive.** It can only ever raise a mark, so its weight must not
appear below the line. Counting it there was wrong three ways at once on the
same outline, and every one of them is silent.

**An outline that states no weights must come back with none.** The pipeline
read four assessment names off an outline that said the weighting would be
announced later, gave them plausible weights, and scored itself high
confidence. That is the worst thing this app can do: a student plans a term
around a grading scheme nobody wrote.
"""

import json
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"

# 100% of graded weight, plus an optional 3% bonus on top.
SCHEME = {
    "confirmed": True,
    "pass_mark": 50,
    "components": [
        {"name": "Problem sets", "weight": 25, "category": "assignments"},
        {"name": "Midterm", "weight": 30, "category": "midterm"},
        {"name": "Final examination", "weight": 45, "category": "final"},
        {"name": "Bonus: tutorial attendance", "weight": 3, "category": "bonus"},
    ],
    "rules": [], "component_minimums": [], "grading_options": [],
}


@pytest.fixture()
def student(client):
    """A signed-up account with the bonus scheme saved and nothing marked."""
    import app as nexora
    client.post("/signup", data={"name": "S", "email": "bonus@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="bonus@carleton.ca").first()
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full="TEST1000",
            grading_json=json.dumps(SCHEME), pass_mark=50))
        nexora.db.session.commit()
        return user.id


def mark(user_id, marks):
    """Save one grade row per component. A value of None leaves it unmarked."""
    import app as nexora
    with nexora.app.app_context():
        nexora.GradeEntry.query.filter_by(user_id=user_id, course_full="TEST1000").delete()
        for comp in SCHEME["components"]:
            nexora.db.session.add(nexora.GradeEntry(
                id=uuid4().hex, user_id=user_id, course_full="TEST1000",
                name=comp["name"], weight=comp["weight"], category=comp["category"],
                score=marks.get(comp["name"])))
        nexora.db.session.commit()


def result(user_id):
    import app as nexora
    with nexora.app.app_context():
        out, err = nexora._compute_official_result(user_id, "TEST1000")
        assert err is None, err
        return out


PERFECT = {"Problem sets": 100, "Midterm": 100, "Final examination": 100}


# ----------------------------------------------------------------- the bonus

def test_declining_an_optional_bonus_does_not_cost_marks(student):
    """Perfect on every graded component is 100%, not 97.09%. Counting the
    bonus in the denominator charged a student for work nobody required."""
    mark(student, dict(PERFECT, **{"Bonus: tutorial attendance": 0}))
    out = result(student)
    assert out["official_grade"] == pytest.approx(100.0)
    assert out["official_status"] == "PASS"


def test_an_earned_bonus_lifts_the_mark_above_a_hundred(student):
    """Grade points are summed without normalising, so 3% of bonus is 3 points.
    Anything else silently under-reports everyone who did the extra work."""
    mark(student, dict(PERFECT, **{"Bonus: tutorial attendance": 100}))
    assert result(student)["official_grade"] == pytest.approx(103.0)


def test_both_grading_paths_agree_about_the_same_student(student):
    """_evaluate_option sums points; the single-scheme branch divides. They
    disagreed by three points on the same marks, which is the same class of bug
    as two screens disagreeing about whether someone passed."""
    import app as nexora
    mark(student, dict(PERFECT, **{"Bonus: tutorial attendance": 100}))
    single = result(student)["official_grade"]
    with nexora.app.app_context():
        rows = nexora.GradeEntry.query.filter_by(user_id=student,
                                                 course_full="TEST1000").all()
        option = {"option_name": "The same scheme", "components": SCHEME["components"]}
        evaluated = nexora._evaluate_option(option, nexora._grade_row_map(rows))
    assert evaluated["grade"] == pytest.approx(single)


def test_an_unmarked_bonus_does_not_hold_the_course_incomplete(student):
    """Bonus weight is never owed, so it cannot be outstanding. Leaving the row
    blank -- what every student who ignores the bonus does -- used to leave
    official_grade None and the whole result panel stuck on INCOMPLETE."""
    mark(student, PERFECT)
    out = result(student)
    assert out["remaining_weight"] == pytest.approx(0.0)
    assert out["official_grade"] == pytest.approx(100.0)
    assert out["official_status"] == "PASS"


def test_real_outstanding_work_still_holds_it_incomplete(student):
    """The guard above must not turn into 'nothing is ever outstanding'."""
    mark(student, {"Problem sets": 100, "Midterm": 100})
    out = result(student)
    assert out["remaining_weight"] == pytest.approx(45.0)
    assert out["official_grade"] is None
    assert out["official_status"] == "INCOMPLETE"


def test_current_standing_is_measured_against_graded_work(student):
    mark(student, {"Problem sets": 80, "Midterm": 90,
                   "Bonus: tutorial attendance": 0})
    # 0.8*25 + 0.9*30 = 47 points out of the 55 graded weight.
    assert result(student)["current_on_completed"] == pytest.approx(85.45, abs=0.01)


def test_the_planner_does_not_read_an_unearned_bonus_as_struggling(student):
    """_course_standing drives how many hours a course gets. An unearned bonus
    dragged the average down, so a course the student is acing pulled time --
    and became the 'worst' assessment, so the plan told them to revise it."""
    import app as nexora
    mark(student, dict(PERFECT, **{"Bonus: tutorial attendance": 0}))
    with nexora.app.app_context():
        standing = nexora._course_standing(student)["TEST1000"]
    assert standing["average"] == pytest.approx(100.0)
    assert "bonus" not in (standing["worst"][0] or "").lower()


# -------------------------------------------------- the invented scheme guard

NO_WEIGHTS = """Evaluation

Students will be assessed through a combination of laboratory work, a midterm
test, participation in seminars, and a final examination. The relative
weighting of these components will be announced in class during the first week
of term and posted to Brightspace at that time.
"""


def test_a_scheme_with_no_support_in_the_outline_is_refused():
    import app as nexora
    invented = {"components": [
        {"name": "Laboratory work", "weight": 25},
        {"name": "Midterm test", "weight": 25},
        {"name": "Seminar participation", "weight": 20},
        {"name": "Final examination", "weight": 30},
    ]}
    assert nexora._scheme_is_uncorroborated(invented, NO_WEIGHTS) is True


def test_a_single_stated_weight_is_enough_to_trust_the_rest():
    """All-or-nothing on purpose: a derived weight legitimately never appears
    in the text, so dropping components one at a time would delete real ones."""
    import app as nexora
    source = "Assignments 20%. Four labs at 6% each. Final examination the remainder."
    model = {"components": [
        {"name": "Assignments", "weight": 20},
        {"name": "Labs", "weight": 24},     # 4 x 6, never written as 24
        {"name": "Final examination", "weight": 56},
    ]}
    assert nexora._scheme_is_uncorroborated(model, source) is False


def test_a_syllabus_priced_in_points_is_not_mistaken_for_an_invention():
    """Points-based outlines state 100 and 200, not 20% and 40%. The converted
    weights must still count as corroborated."""
    import app as nexora
    source = ("Homework 100 points, labs 100 points, midterm 100 points, "
              "final examination 200 points, out of 500 points total.")
    model = {"components": [
        {"name": "Homework", "weight": 20}, {"name": "Labs", "weight": 20},
        {"name": "Midterm", "weight": 20}, {"name": "Final", "weight": 40},
    ]}
    assert nexora._scheme_is_uncorroborated(model, source) is False


def test_an_alternative_scheme_can_carry_the_corroboration():
    import app as nexora
    source = "Scheme A: term tests 40%, final 60%. Scheme B: final exam only."
    model = {"components": [], "grading_options": [
        {"option_name": "A", "components": [{"name": "Term tests", "weight": 40},
                                            {"name": "Final", "weight": 60}]},
    ]}
    assert nexora._scheme_is_uncorroborated(model, source) is False


def test_an_empty_reading_is_not_reported_as_an_invention():
    """Nothing extracted is a different failure with a different message."""
    import app as nexora
    assert nexora._scheme_is_uncorroborated({"components": []}, NO_WEIGHTS) is False


def test_a_decimal_weight_written_in_the_outline_counts():
    import app as nexora
    source = "Participation 7.5%, midterm 32.5%, final 60%."
    model = {"components": [{"name": "Participation", "weight": 7.5}]}
    assert nexora._scheme_is_uncorroborated(model, source) is False
