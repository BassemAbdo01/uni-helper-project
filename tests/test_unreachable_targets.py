"""What Nexora says when a target can no longer be reached.

The panel told a student "You need 158.33% on the final exam." That is the
correct answer to the wrong question: they had already lost too many marks
elsewhere for 91% to be possible, and a number above 100 is not advice, it is
arithmetic nobody can act on. It reads as the page being broken, and the
student reported it as exactly that.

The grade calculator had the mirror-image problem: with every assessment
marked it answered "there is nothing left to need" and ignored the target the
student had just typed, so the box looked dead.
"""

import json
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"

# BIOL 1103's real shape: 100% of graded weight across six components, a 20%
# final, and a 2% bonus on top.
SCHEME = {
    "confirmed": True, "pass_mark": 50,
    "components": [
        {"name": "In-Class Worksheets", "weight": 10, "category": "assignments"},
        {"name": "Unit Tests", "weight": 30, "category": "tests"},
        {"name": "Final Exam", "weight": 20, "category": "final"},
        {"name": "In-Lab Assignments", "weight": 24, "category": "labs"},
        {"name": "Tutorial Assignments", "weight": 4, "category": "tutorial"},
        {"name": "Final Lab Exam", "weight": 12, "category": "labs"},
        {"name": "Bonus Opportunities", "weight": 2, "category": "bonus"},
    ],
    "rules": [], "component_minimums": [], "grading_options": [],
}

# The marks from the report: 8/10, 20/30, 66.67/100, 20/24, 4/4, 7/12, 0/2.
MARKS = {
    "In-Class Worksheets": (8, 10), "Unit Tests": (20, 30),
    "Final Exam": (66.67, 100), "In-Lab Assignments": (20, 24),
    "Tutorial Assignments": (4, 4), "Final Lab Exam": (7, 12),
    "Bonus Opportunities": (0, 2),
}


@pytest.fixture()
def student(client):
    import app as nexora
    client.post("/signup", data={"name": "S", "email": "reach@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="reach@carleton.ca").first()
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full="BIOL1103",
            grading_json=json.dumps(SCHEME), pass_mark=50))
        for comp in SCHEME["components"]:
            score, out_of = MARKS[comp["name"]]
            nexora.db.session.add(nexora.GradeEntry(
                id=uuid4().hex, user_id=user.id, course_full="BIOL1103",
                name=comp["name"], weight=comp["weight"], category=comp["category"],
                score=score, max_score=out_of))
        nexora.db.session.commit()
        return user.id


def outcomes(client, target):
    res = client.post("/api/course/BIOL1103/grade-outcomes", json={"target": target})
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def test_the_server_says_where_a_perfect_final_actually_lands(student, client):
    """The number the panel needs to say "out of reach" honestly."""
    body = outcomes(client, 91)
    assert body["max_with_perfect_final"] is not None
    # 59 points banked outside the final, plus all 20 of the final.
    assert body["max_with_perfect_final"] == pytest.approx(79.0, abs=0.01)


def test_an_impossible_target_is_reported_as_over_a_hundred(student, client):
    """Not clamped to 100. The template needs to be able to tell the
    difference between "you need everything" and "you cannot get there"."""
    assert outcomes(client, 91)["required_final"] > 100


def test_a_reachable_target_still_gets_a_usable_number(student, client):
    body = outcomes(client, 70)
    # Below zero legitimately means "already secured"; the page clamps it.
    assert body["required_final"] <= 100


def test_the_target_is_echoed_back_so_the_page_can_name_it(student, client):
    assert outcomes(client, 91)["target"] == pytest.approx(91.0)


def test_the_bonus_is_not_counted_as_outstanding_work(student, client):
    """Every graded component is marked, so nothing is owed -- an unmarked or
    zeroed bonus must not make the course look unfinished."""
    body = outcomes(client, 50)
    assert body["remaining_weight"] == pytest.approx(0.0)
    assert body["official_status"] == "PASS"


def test_a_perfect_final_target_is_exactly_reachable(student, client):
    """The boundary: asking for precisely what a perfect final delivers must
    not be reported as out of reach."""
    body = outcomes(client, 79)
    assert body["required_final"] <= 100.01


# ------------------------------------------------- the page's own wording

def test_the_workspace_names_an_unreachable_target(student, client):
    """The template has to carry both branches, not just the number."""
    body = client.get("/course/BIOL1103").get_data(as_text=True)
    assert "is out of reach" in body
    assert "max_with_perfect_final" in body
    assert "required_final>100" in body.replace(" ", "")


def test_the_calculator_answers_the_target_it_was_given(student, client):
    """With everything marked it used to ignore the number typed in."""
    body = client.get("/grade-calculator").get_data(as_text=True)
    assert "is no longer reachable" in body
    assert "nothing left to need" in body


# ------------------------------------------------------ the course picker

def test_the_picker_is_grouped_by_term(client):
    """A flat list of every course a student has ever taken is unusable by
    second year; the report was thirty-one rows in one dropdown."""
    import app as nexora
    client.post("/signup", data={"name": "T", "email": "terms@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="terms@carleton.ca").first()
        for code, term in (("BIOL1103", "Fall 2026"), ("COMP1405", "Winter 2025"),
                           ("MATH1004", "Fall 2025")):
            nexora.db.session.add(nexora.SyncedCourse(
                id=uuid4().hex, user_id=user.id, course_code=code,
                course_name=code, term=term))
        nexora.db.session.commit()
    body = client.get("/grade-calculator").get_data(as_text=True)
    for label in ("Fall 2026", "Fall 2025", "Winter 2025"):
        assert f'<optgroup label="{label}">' in body, f"{label} is not a group"
    # Newest first, and the current term ahead of everything.
    assert body.index("Fall 2026") < body.index("Fall 2025") < body.index("Winter 2025")


def test_the_term_is_not_repeated_on_every_row(client):
    """It is the group heading now, so repeating it is noise."""
    import app as nexora
    client.post("/signup", data={"name": "T", "email": "terms2@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="terms2@carleton.ca").first()
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user.id, course_code="BIOL1103",
            course_name="Biology", term="Fall 2026"))
        nexora.db.session.commit()
    body = client.get("/grade-calculator").get_data(as_text=True)
    assert "BIOL1103 — Biology (Fall 2026)" not in body


# ------------------------------------------ one definition of a term

@pytest.mark.parametrize("value,expected", [
    ("Fall 2026", "Fall 2026"),
    ("winter 2025", "Winter 2025"),
    ("Autumn 2024", "Fall 2024"),
    ("2026.10", "Winter 2026"),
    ("2026.20", "Summer 2026"),
    ("2026.30", "Fall 2026"),
    ("", "Other courses"),
    ("Continuing Education", "Other courses"),
])
def test_a_term_string_means_one_thing_everywhere(value, expected):
    """The dashboard and the calculator must group courses by the same rule;
    this used to be a closure inside home()."""
    import app as nexora
    assert nexora._term_label(value) == expected


def test_terms_sort_newest_first():
    import app as nexora
    labels = ["Winter 2025", "Fall 2026", "Summer 2026", "Winter 2026"]
    assert sorted(labels, key=nexora._term_sort_key, reverse=True) == [
        "Fall 2026", "Summer 2026", "Winter 2026", "Winter 2025"]


@pytest.mark.parametrize("month,season", [(1, "Winter"), (4, "Winter"), (5, "Summer"),
                                          (8, "Summer"), (9, "Fall"), (12, "Fall")])
def test_the_current_term_comes_from_the_date(month, season):
    """Never from whichever group sorts first: an early Winter enrolment made
    in November would take the page over."""
    import app as nexora
    from datetime import date
    assert nexora._current_term_label(date(2026, month, 15)) == f"{season} 2026"
