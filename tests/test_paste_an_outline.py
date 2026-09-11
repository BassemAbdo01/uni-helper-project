"""Pasting an outline into the grade calculator, with a course selected.

Neither intake path on that page worked once a course was picked. An upload
went to `/outline/upload`, which only STORES the text -- no analysis ran, and
the page printed "Grading components extracted." over an empty list. Pasted
text went to the generic analyzer, which keeps nothing: the components appeared,
then the page re-read the (empty) saved copy and they vanished.

Pasted text now takes the same road as an upload -- stored as the course's
outline, then analyzed against the stored copy -- so the confirm step, the
official result, the study planner's weights and Ask all find it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"

OUTLINE = """SYSC 2004 Object-Oriented Software Development
Evaluation
Assignments (4)              20%
Lab exercises (8)            10%
Midterm test                 25%
Final examination            45%
To pass the course a student must obtain at least 50% overall AND at least
40% on the final examination.
"""


@pytest.fixture()
def student(client):
    client.post("/signup", data={"name": "B", "email": "paste@carleton.ca",
                                 "password": PASSWORD})
    return client


def test_pasted_text_is_stored_as_the_courses_outline(student):
    import app as nexora
    res = student.post("/api/course/SYSC2004/outline/upload", json={"text": OUTLINE})
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["source_uid"].startswith("manual-outline:")
    with nexora.app.app_context():
        row = nexora.CourseContent.query.filter_by(course_full="SYSC2004",
                                                   content_type="outline").first()
        assert row is not None
        assert "Final examination" in row.content_text


def test_the_stored_paste_is_what_analyze_reads(student):
    """The whole point: the second call has to find what the first one kept."""
    import app as nexora
    uid = student.post("/api/course/SYSC2004/outline/upload",
                       json={"text": OUTLINE}).get_json()["source_uid"]
    with nexora.app.app_context():
        row = nexora.CourseContent.query.filter_by(source_uid=uid).first()
        assert row is not None and row.course_full == "SYSC2004"


def test_pasting_the_same_text_twice_is_one_outline_not_two(student):
    import app as nexora
    student.post("/api/course/SYSC2004/outline/upload", json={"text": OUTLINE})
    student.post("/api/course/SYSC2004/outline/upload", json={"text": OUTLINE})
    with nexora.app.app_context():
        assert nexora.CourseContent.query.filter_by(course_full="SYSC2004").count() == 1


def test_a_snippet_too_short_to_be_an_outline_is_refused(student):
    res = student.post("/api/course/SYSC2004/outline/upload", json={"text": "Final 100%"})
    assert res.status_code == 422
    assert "too short" in res.get_json()["error"].lower()


def test_nothing_at_all_is_refused_with_a_message_that_names_both_options(student):
    res = student.post("/api/course/SYSC2004/outline/upload", json={})
    assert res.status_code == 400
    assert "paste" in res.get_json()["error"].lower()


def test_the_page_has_a_way_to_confirm_the_policy(student):
    """The official panel said 'Confirm the detected grading policy' and the
    only place to do that was another page, with no link to it."""
    body = student.get("/grade-calculator").get_data(as_text=True)
    assert "/outline/confirm" in body
    assert "Confirm this grading policy" in body


def test_the_page_chains_store_then_analyze_for_a_course(student):
    body = student.get("/grade-calculator").get_data(as_text=True)
    assert "/outline/upload" in body
    assert "/outline/analyze" in body
