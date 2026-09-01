"""A pass rule Nexora cannot check must never fail a student.

BIOL 1103 splits into a lecture half worth 60 and a lab half worth 40, and asks
for a pass in the combined mark and also in the lecture. The model expressed
both as derived components, and described the combined one in terms of the two
halves -- neither of which is a grade row. Every rule that could not be
evaluated was scored as failed, so a student who had entered all seven
components at full marks was shown FAIL, 100% and an F.

Two defences here: the compiler binds a rule to components that exist before it
is ever enforced, and the evaluator reports what it cannot answer as pending
instead of failed. The last test is the one that matters -- it fails a student
who has genuinely missed a minimum, so the fix cannot have been "stop failing
anyone".
"""

import json
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _compile_pass_rules,
    _evaluate_pass_rules,
    _grade_row_map,
    _normalize_advanced_grading_model,
)

COMPONENTS = [
    {"name": "In-class worksheets", "weight": 10, "category": "assignments"},
    {"name": "Unit tests", "weight": 30, "category": "exam"},
    {"name": "Final exam", "weight": 20, "category": "final"},
    {"name": "In-lab assignments", "weight": 24, "category": "lab"},
    {"name": "Tutorial assignments", "weight": 4, "category": "assignments"},
    {"name": "Final lab exam", "weight": 12, "category": "lab"},
    {"name": "Bonus opportunities", "weight": 2, "category": "bonus"},
]

LECTURE = ["In-class worksheets", "Unit tests", "Final exam"]
LAB = ["In-lab assignments", "Tutorial assignments", "Final lab exam"]


def biol1103():
    """The outline as the pipeline actually returned it, plus the two group
    requirements the prose states."""
    return _normalize_advanced_grading_model({
        "components": list(COMPONENTS),
        "input_components": list(COMPONENTS),
        "pass_mark": 50,
        "derived_components": [
            {"name": "Lecture component", "components": list(LECTURE), "minimum_percent": 50},
            {"name": "Laboratory component", "components": list(LAB), "minimum_percent": 50},
            # Written in terms of the other two, which are not grade rows.
            {"name": "Combined course", "components": ["Lecture component", "Laboratory component"],
             "minimum_percent": 50},
        ],
    })


def row(name, percent, weight=0):
    return SimpleNamespace(name=name, score=percent, max_score=100, weight=weight, category="")


def full_marks():
    return _grade_row_map([row(c["name"], 100, c["weight"]) for c in COMPONENTS])


def test_full_marks_are_not_a_failure():
    analysis = biol1103()
    passed, details = _evaluate_pass_rules(analysis, full_marks(), 100.0)
    assert passed is True
    assert [d for d in details if d["passed"] is False] == []


def test_a_group_covering_the_whole_scheme_becomes_the_overall_minimum():
    """"Combined course" is the course, and the calculator already has that
    number. Kept as a group it depended on six names resolving."""
    rules = biol1103()["pass_rules"]
    overall = [r for r in rules if r["type"] == "overall_minimum"]
    assert len(overall) == 1
    assert overall[0]["minimum_percent"] == 50


def test_a_nested_group_is_expanded_to_real_components():
    lecture = next(r for r in biol1103()["pass_rules"]
                   if "Lecture" in (r.get("message") or ""))
    assert lecture["components"] == LECTURE
    assert not lecture.get("unbound")


def test_a_group_naming_nothing_is_reported_rather_than_enforced():
    analysis = _compile_pass_rules({
        "components": list(COMPONENTS),
        "pass_rules": [{"type": "aggregate_minimum", "components": [],
                        "minimum_percent": 50,
                        "message": "Lecture component must meet the minimum."}],
    })
    rule = analysis["pass_rules"][0]
    assert rule.get("unbound") is True
    assert any("could not be matched" in str(a) for a in analysis["ambiguities"])

    passed, details = _evaluate_pass_rules(analysis, full_marks(), 100.0)
    assert passed is True
    assert details[0]["state"] == "pending"
    assert "not being enforced" in details[0]["note"]


def test_an_unentered_mark_is_pending_not_failed():
    """Mid-term, the final is blank. That is not a broken requirement."""
    analysis = biol1103()
    rows = _grade_row_map([row(c["name"], None if c["name"] == "Final exam" else 100, c["weight"])
                           for c in COMPONENTS])
    passed, details = _evaluate_pass_rules(analysis, rows, None)
    assert passed is True
    lecture = next(d for d in details if "Lecture" in d["message"])
    assert lecture["state"] == "pending"


def test_a_genuinely_missed_minimum_still_fails():
    """The fix must not be "stop failing anyone". A student who scores 10% on
    every lecture assessment has not passed the lecture half."""
    analysis = biol1103()
    rows = _grade_row_map([row(c["name"], 10 if c["name"] in LECTURE else 100, c["weight"])
                           for c in COMPONENTS])
    passed, details = _evaluate_pass_rules(analysis, rows, 65.0)
    assert passed is False
    lecture = next(d for d in details if "Lecture" in d["message"])
    assert lecture["state"] == "fail"
    assert lecture["actual"] == 10


# ---------------------------------------------------------------- end to end

def signup(client, email="biol@carleton.ca"):
    return client.post("/signup", data={"name": "Biology Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


@pytest.fixture()
def biology(client):
    import app as nexora
    signup(client)
    analysis = biol1103()
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="biol@carleton.ca").first()
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full="BIOL1103",
            outline_title="BIOL 1103 outline",
            grading_json=json.dumps(analysis), pass_mark=50))
        for component in COMPONENTS:
            nexora.db.session.add(nexora.GradeEntry(
                id=uuid4().hex, user_id=user.id, course_full="BIOL1103",
                name=component["name"], weight=component["weight"],
                score=component["weight"], max_score=component["weight"],
                category=component["category"]))
        nexora.db.session.commit()
    return client


def test_the_official_result_for_a_perfect_student_is_a_pass(biology):
    """The screenshot this was reported from: every card at 100%, and a red
    FAIL / 100% / F underneath it."""
    response = biology.post("/api/course/BIOL1103/grade-outcomes", json={"target": 50})
    assert response.status_code == 200
    body = response.get_json()
    assert body["official_status"] == "PASS"
    assert body["letter_grade"] != "F"
    assert [r for r in body["pass_rules"] if r["passed"] is False] == []
