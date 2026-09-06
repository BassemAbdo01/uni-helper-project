"""What the plan knows about *you*, not just about the calendar.

Two courses with the same weight and the same deadline should not get the same
hours if you are at 42% in one and 91% in the other. Nexora holds both numbers,
and this is the layer that spends them.

The second half is the outline's own words: a block that says "cellular
respiration, photosynthesis - work the module 4-6 problems" is one a person can
start. "Study BIOL for 90 minutes" is one they skip.
"""

import json
import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="insight@carleton.ca"):
    return client.post("/signup", data={"name": "Insight Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


@pytest.fixture()
def student(client):
    signup(client)
    return client


def seed_work(email, work=(), components=()):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        for title, course, offset, kind in work:
            nexora.db.session.add(nexora.Task(
                id=uuid4().hex, user_id=user.id,
                group_id=nexora.make_group_id(course, title), title=title,
                base_title=title, course_full=course, course_prefix=course[:4],
                due_raw=(date.today() + timedelta(days=offset)).isoformat(),
                kind=kind, source="manual", sync_state="active"))
        for course, name, weight in components:
            nexora.db.session.add(nexora.GradeEntry(
                id=uuid4().hex, user_id=user.id, course_full=course,
                name=name, weight=weight, score=None, max_score=100))
        nexora.db.session.commit()


def set_marks(email, marks):
    """marks: (course, name, weight, score out of 100)"""
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        for course, name, weight, score in marks:
            row = nexora.GradeEntry.query.filter_by(
                user_id=user.id, course_full=course, name=name).first()
            if not row:
                row = nexora.GradeEntry(id=uuid4().hex, user_id=user.id,
                                        course_full=course, name=name)
                nexora.db.session.add(row)
            row.weight, row.score, row.max_score = weight, score, 100
        nexora.db.session.commit()


def add_profile(email, course, grading, brief=None):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full=course,
            grading_json=json.dumps(grading),
            study_brief_json=json.dumps(brief) if brief else "",
            pass_mark=50))
        nexora.db.session.commit()


def generate(client):
    res = client.post("/api/study-plan/generate")
    assert res.status_code == 200
    return res.get_json()


def minutes_by_course(client):
    out = {}
    for s in generate(client)["sessions"]:
        out[s["course"]] = out.get(s["course"], 0) + s["minutes"]
    return out


# ----------------------------------------------- how you are actually doing

def test_the_course_you_are_failing_gets_more_of_the_week(student):
    """Identical assignments, identical weight, identical deadline. The only
    difference is that one course is going badly, which is exactly when a plan
    earns its keep."""
    seed_work("insight@carleton.ca",
              work=[("Assignment A", "BIOL1103", 8, "due"),
                    ("Assignment B", "SYSC2004", 8, "due")],
              components=[("BIOL1103", "Assignment A", 20), ("SYSC2004", "Assignment B", 20)])
    set_marks("insight@carleton.ca", [("BIOL1103", "Midterm", 30, 42),
                                      ("SYSC2004", "Midterm", 30, 91)])
    minutes = minutes_by_course(student)
    assert minutes.get("BIOL1103", 0) > minutes.get("SYSC2004", 0)


def test_struggling_earns_more_hours_than_coasting(student):
    seed_work("insight@carleton.ca", work=[("Essay", "BIOL1103", 9, "essay")],
              components=[("BIOL1103", "Essay", 20)])
    set_marks("insight@carleton.ca", [("BIOL1103", "Midterm", 40, 95)])
    coasting = sum(s["minutes"] for s in generate(student)["sessions"])
    set_marks("insight@carleton.ca", [("BIOL1103", "Midterm", 40, 45)])
    struggling = sum(s["minutes"] for s in generate(student)["sessions"])
    assert struggling > coasting


def test_where_you_stand_is_said_out_loud(student):
    """A block whose reason you cannot see is a block you skip."""
    seed_work("insight@carleton.ca", work=[("Assignment A", "BIOL1103", 8, "due")],
              components=[("BIOL1103", "Assignment A", 20)])
    set_marks("insight@carleton.ca", [("BIOL1103", "Midterm", 30, 41)])
    reasons = " ".join(s["reason"] for s in generate(student)["sessions"])
    assert "41%" in reasons


def test_one_tiny_mark_is_not_evidence_about_a_whole_course(student):
    """A 2% quiz scored badly says almost nothing, and must not rewrite a week."""
    seed_work("insight@carleton.ca", work=[("Essay", "BIOL1103", 9, "essay")],
              components=[("BIOL1103", "Essay", 20)])
    set_marks("insight@carleton.ca", [("BIOL1103", "Tiny quiz", 2, 10)])
    reasons = " ".join(s["reason"] for s in generate(student)["sessions"])
    assert "you are at" not in reasons


FINAL_ONLY = {
    "components": [{"name": "Final exam", "weight": 80}],
    "input_components": [{"name": "Final exam", "weight": 80}],
    "confirmed": True, "pass_mark": 50,
    "pass_rules": [{"type": "component_minimum", "component": "Final exam",
                    "minimum_percent": 50, "message": "You must pass the final exam."}],
}


def test_a_failing_pass_rule_pulls_time_toward_that_course(student):
    seed_work("insight@carleton.ca",
              work=[("Essay", "BIOL1103", 9, "essay"), ("Essay", "SYSC2004", 9, "essay")],
              components=[("BIOL1103", "Essay", 20), ("SYSC2004", "Essay", 20)])
    add_profile("insight@carleton.ca", "BIOL1103", FINAL_ONLY)
    set_marks("insight@carleton.ca", [("BIOL1103", "Final exam", 80, 20)])
    minutes = minutes_by_course(student)
    assert minutes.get("BIOL1103", 0) > minutes.get("SYSC2004", 0)


def test_a_rule_that_cannot_be_evaluated_does_not_pull_time(student):
    """The same principle as the grade panel: unknown is not failing. A pending
    rule must not quietly drag the week toward the wrong course."""
    seed_work("insight@carleton.ca",
              work=[("Essay", "BIOL1103", 9, "essay"), ("Essay", "SYSC2004", 9, "essay")],
              components=[("BIOL1103", "Essay", 20), ("SYSC2004", "Essay", 20)])
    add_profile("insight@carleton.ca", "BIOL1103", FINAL_ONLY)   # no mark entered
    reasons = " ".join(s["reason"] for s in generate(student)["sessions"])
    assert "must pass the final exam" not in reasons.lower()


# ------------------------------------------- the outline's own words, cached

BRIEF = {"Unit 2 Test": {"topics": ["cellular respiration", "photosynthesis"],
                         "how": "Work the module 4-6 problems closed-book"}}


def test_a_session_names_the_topics_the_outline_gave(student):
    seed_work("insight@carleton.ca", work=[("Unit 2 Test", "BIOL1103", 6, "exam")],
              components=[("BIOL1103", "Unit 2 Test", 30)])
    add_profile("insight@carleton.ca", "BIOL1103",
                {"components": [{"name": "Unit 2 Test", "weight": 30}]}, BRIEF)
    focuses = [s["focus"] for s in generate(student)["sessions"] if s["title"] == "Unit 2 Test"]
    assert focuses
    assert "cellular respiration" in focuses[0]
    assert "module 4-6" in focuses[0]


def test_without_a_brief_the_focus_is_still_something_you_can_start(student):
    seed_work("insight@carleton.ca", work=[("Unit 2 Test", "BIOL1103", 6, "exam")])
    sessions = generate(student)["sessions"]
    assert sessions
    for s in sessions:
        assert len(s["focus"]) > 20
        assert not s["focus"].lower().startswith("study")


def test_a_rebuild_never_reaches_gemini(student, monkeypatch):
    """The topics do not change between two rebuilds of the same week, so the
    planner must work entirely off the cached brief."""
    import app as nexora
    seed_work("insight@carleton.ca", work=[("Unit 2 Test", "BIOL1103", 6, "exam")])
    add_profile("insight@carleton.ca", "BIOL1103",
                {"components": [{"name": "Unit 2 Test", "weight": 30}]},
                {"Unit 2 Test": {"topics": ["mitosis"], "how": "Draw the phases from memory"}})

    def explode(*args, **kwargs):
        raise AssertionError("the planner reached Gemini")
    monkeypatch.setattr(nexora, "_gemini_json", explode)
    assert any("mitosis" in s["focus"] for s in generate(student)["sessions"])


def test_a_brief_naming_something_the_course_has_not_got_is_ignored(student):
    """The model is handed the exact assessment list. Anything else it returns
    is noise, and must not become a session nobody can find in their outline."""
    seed_work("insight@carleton.ca", work=[("Unit 2 Test", "BIOL1103", 6, "exam")])
    add_profile("insight@carleton.ca", "BIOL1103",
                {"components": [{"name": "Unit 2 Test", "weight": 30}]},
                {"Comprehensive Viva": {"topics": ["everything"], "how": "Panic"}})
    for s in generate(student)["sessions"]:
        assert "Panic" not in s["focus"]
        assert "everything" not in s["focus"]


def test_building_a_brief_needs_an_analysed_outline(student):
    res = student.post("/api/study-plan/brief", json={})
    assert res.status_code == 400
    assert "outline" in res.get_json()["error"]


def test_building_a_brief_needs_a_login(client):
    assert client.post("/api/study-plan/brief", json={}).status_code in (302, 401)


@pytest.mark.parametrize("title,expected", [
    ("Unit 2 Test", "Unit tests"),
    ("Unit 1 Test", "Unit tests"),
    ("Final exam", "Final exam"),
    ("In-lab assignment 3", "In-lab assignments"),
    ("Midterm II", "Midterms"),
    ("Assignment 4 - Collections", "Assignments"),
])
def test_a_numbered_piece_finds_the_group_it_belongs_to(title, expected):
    """BIOL 1103 lists "Unit tests" worth 30; the deadlines are "Unit 1 Test",
    "Unit 2 Test", "Unit 3 Test". Plain containment misses that in both
    directions, so every numbered test fell back to a generic focus line and
    to a default weight."""
    from app import _match_assessment
    table = {"Unit tests": "A", "Final exam": "B", "In-lab assignments": "C",
             "Midterms": "D", "Assignments": "E"}
    lookup = {k: k for k in table}
    assert _match_assessment(title, lookup) == expected


def test_something_the_outline_never_mentioned_finds_nothing():
    from app import _match_assessment
    assert _match_assessment("Bake sale", {"Unit tests": "A"}) is None
