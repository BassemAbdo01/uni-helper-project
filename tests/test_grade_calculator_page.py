"""The grade calculator as the one place every course's marks live.

Opening a workspace per course to read a grading scheme was most of what a
student did on this app. The picker replaces that. The course workspace is
deliberately left working, so the tests here also check the two do not diverge:
the same marks, and the same official result, from either page.
"""

import json
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COMPONENTS = [
    {"name": "Midterm", "weight": 30, "category": "midterm"},
    {"name": "Final exam", "weight": 70, "category": "final"},
]


def signup(client, email="calc@carleton.ca"):
    return client.post("/signup", data={"name": "Calc Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


@pytest.fixture()
def student(client):
    """Two courses: one analysed with marks, one only synced."""
    import app as nexora
    signup(client)
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="calc@carleton.ca").first()
        for code, name, term in [("SYSC2004", "Object-Oriented Software Development", "Fall 2026"),
                                 ("MATH1104", "Linear Algebra", "Fall 2026")]:
            nexora.db.session.add(nexora.SyncedCourse(
                id=uuid4().hex, user_id=user.id, course_code=code,
                course_name=name, term=term))
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full="SYSC2004",
            grading_json=json.dumps({
                "components": COMPONENTS, "input_components": COMPONENTS,
                "pass_mark": 50, "confirmed": True,
                "pass_rules": [{"type": "component_minimum", "component": "Final exam",
                                "minimum_percent": 50,
                                "message": "You must pass the final exam."}],
            }), pass_mark=50))
        for c in COMPONENTS:
            nexora.db.session.add(nexora.GradeEntry(
                id=uuid4().hex, user_id=user.id, course_full="SYSC2004",
                name=c["name"], weight=c["weight"], score=None, max_score=100,
                category=c["category"]))
        nexora.db.session.commit()
    return client


# ------------------------------------------------------------- the picker

def test_the_page_offers_the_students_courses(student):
    body = student.get("/grade-calculator").get_data(as_text=True)
    assert 'id="coursePick"' in body
    assert 'value="SYSC2004"' in body
    assert 'value="MATH1104"' in body


def test_a_course_with_no_outline_says_so_in_the_list(student):
    body = student.get("/grade-calculator").get_data(as_text=True)
    option = body[body.index('value="MATH1104"'):]
    assert "outline not read yet" in option[:300]


def test_an_analysed_course_is_not_flagged_as_unread(student):
    body = student.get("/grade-calculator").get_data(as_text=True)
    option = body[body.index('value="SYSC2004"'):]
    assert "outline not read yet" not in option[:200]


def test_there_is_still_a_way_to_use_it_without_a_course(student):
    """A student can be handed an outline for a course they have not enrolled
    in yet, which is what this page was originally for."""
    body = student.get("/grade-calculator").get_data(as_text=True)
    assert 'value=""' in body and "nothing saved" in body


def test_a_course_in_the_address_is_preselected(student):
    body = student.get("/grade-calculator?course=SYSC2004").get_data(as_text=True)
    assert 'value="SYSC2004" selected' in body


def test_a_course_that_is_not_theirs_is_ignored_rather_than_shown(client):
    """?course= comes off a URL, so it is untrusted; a code the student has no
    enrolment in must not appear as though Nexora holds data for it."""
    signup(client, email="nobody@carleton.ca")
    body = client.get("/grade-calculator?course=CHEM9999").get_data(as_text=True)
    assert "CHEM9999" not in body


def test_another_students_courses_are_never_listed(client):
    import app as nexora
    signup(client, email="one@carleton.ca")
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="one@carleton.ca").first()
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user.id, course_code="PRIV1000",
            course_name="Private course", term="Fall 2026"))
        nexora.db.session.commit()
    client.get("/logout")
    signup(client, email="two@carleton.ca")
    assert "PRIV1000" not in client.get("/grade-calculator").get_data(as_text=True)


def test_a_hidden_course_is_not_offered(student):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="calc@carleton.ca").first()
        row = nexora.SyncedCourse.query.filter_by(user_id=user.id, course_code="MATH1104").first()
        row.is_hidden = True
        nexora.db.session.commit()
    assert 'value="MATH1104"' not in student.get("/grade-calculator").get_data(as_text=True)


# ------------------------------------------ the same answer as the workspace

def test_marks_entered_here_are_the_courses_marks(student):
    """The page writes through the course's own endpoint, so a mark typed here
    is the same row the workspace reads."""
    student.post("/api/course/SYSC2004/grades",
                 json={"name": "Midterm", "weight": 30, "score": "24/30"})
    grades = student.get("/api/course/SYSC2004/grades").get_json()["grades"]
    midterm = next(g for g in grades if g["name"] == "Midterm")
    assert midterm["score"] == 24
    assert midterm["max_score"] == 30


def test_a_mark_out_of_something_is_not_read_as_a_percentage(student):
    """Two boxes mean 4 out of 20, which is 20%. Sending the bare 4 made it 4%
    -- the same class of bug as the single "score %" field that came before."""
    student.post("/api/course/SYSC2004/grades",
                 json={"name": "Midterm", "weight": 30, "score": "4/20", "max_score": 20})
    midterm = next(g for g in student.get("/api/course/SYSC2004/grades").get_json()["grades"]
                   if g["name"] == "Midterm")
    assert (midterm["score"], midterm["max_score"]) == (4, 20)


def test_the_official_result_comes_from_the_one_calculator(student):
    student.post("/api/course/SYSC2004/grades",
                 json={"name": "Midterm", "weight": 30, "score": "27/30"})
    student.post("/api/course/SYSC2004/grades",
                 json={"name": "Final exam", "weight": 70, "score": "63/70"})
    result = student.post("/api/course/SYSC2004/grade-outcomes", json={"target": 50}).get_json()
    assert result["official_status"] == "PASS"
    assert round(result["official_grade"]) == 90


def test_a_failed_rule_reaches_this_page_too(student):
    student.post("/api/course/SYSC2004/grades",
                 json={"name": "Midterm", "weight": 30, "score": "30/30"})
    student.post("/api/course/SYSC2004/grades",
                 json={"name": "Final exam", "weight": 70, "score": "20/70"})
    result = student.post("/api/course/SYSC2004/grade-outcomes", json={"target": 50}).get_json()
    assert result["official_status"] == "FAIL"
    failed = [r for r in result["pass_rules"] if r["passed"] is False]
    assert failed and "final exam" in failed[0]["message"].lower()


def test_the_workspace_still_works(student):
    """Left alone on purpose: it is the riskiest file in the repo and this
    change is about where things live, not how they are calculated."""
    assert student.get("/course/SYSC2004?tab=grades").status_code == 200


def test_the_page_needs_a_login(client):
    assert client.get("/grade-calculator").status_code in (302, 401)
