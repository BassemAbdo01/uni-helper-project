"""Asking the outline a question in your own words.

`/api/course/<code>/ask` already existed on the course workspace: grounded in
the outline text, forbidden from doing its own grade arithmetic, and required
to quote the outline verbatim for every claim. Two things were wrong with it.

It was **target-blind**. It always asked `_compute_official_result` for a bare
pass, so a student asking "what do I need to get 80" was handed the numbers for
50 -- and since the model may not compute, it either hedged or answered the
wrong question with the app's own arithmetic behind it.

And it was **the one Gemini-backed route with no `AI_LIMIT`**, on a page a
student can sit and retype questions into.

`_target_from_question` reads the mark out of the sentence deterministically. A
model misreading "85" as "58" once in a hundred times is a student planning a
term around a number nobody checked.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"


@pytest.fixture()
def user_id(client):
    import app as nexora
    client.post("/signup", data={"name": "B", "email": "ask@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email="ask@carleton.ca").first().id


def read(user_id, question, analysis=None):
    import app as nexora
    with nexora.app.app_context():
        return nexora._target_from_question(user_id, question, analysis or {"pass_mark": 50})


# --------------------------------------------------- a mark the student named

@pytest.mark.parametrize("question,expected", [
    ("what do i need to get 80", 80.0),
    ("what do I need to get an 80%", 80.0),
    ("what do i need to end up with 75 percent", 75.0),
    ("how much do I need to score 62 overall", 62.0),
    ("what do i need to finish with 90", 90.0),
])
def test_a_percentage_the_question_asks_for(user_id, question, expected):
    assert read(user_id, question)["target"] == expected


# ------------------------------------------------------- a letter, their own

@pytest.mark.parametrize("question,letter", [
    ("how much do i need for a B+", "B+"),
    ("what do I need to get an A", "A"),
    ("can I still get an A-", "A-"),
])
def test_a_letter_grade_is_read_off_the_students_own_scale(user_id, question, letter):
    """Not a hardcoded table: a university whose A starts at 80 must not be
    answered with someone else's 85."""
    import app as nexora
    got = read(user_id, question)
    with nexora.app.app_context():
        scale = nexora._get_user_grading_scale(user_id)["scale"]
    expected = next(b["minimum"] for b in scale if b["letter"] == letter)
    assert got["target"] == expected
    assert got["basis"] == "your saved grading scale"


def test_the_lower_case_a_in_a_sentence_is_not_the_grade_A(user_id):
    """"what do I need to pass" contains "a" twice and neither is an A."""
    got = read(user_id, "what do i need to pass")
    assert got["target"] == 50.0
    assert got["basis"] == "this course's pass mark"


# ------------------------------------------------------------- passing marks

def test_passing_uses_the_courses_own_pass_mark(user_id):
    """An outline that says 60 to pass must not be answered with 50."""
    got = read(user_id, "how much do i need to pass", {"pass_mark": 60})
    assert got["target"] == 60.0


@pytest.mark.parametrize("question", [
    "how much do i need to pass",
    "what do I need on the final to pass",
    "what do i need to avoid failing",
])
def test_the_ways_students_ask_about_passing(user_id, question):
    assert read(user_id, question)["target"] == 50.0


# ------------------------------------------- what must NOT be read as a target

def test_a_mark_already_earned_is_not_a_target(user_id):
    """"I got 80 on the midterm" is a statement, not a goal. Answering it as
    one would compute what is needed to finish at 80 and present it as though
    the student had asked."""
    assert read(user_id, "I got 80 on the midterm, am I ok") is None


def test_a_weight_is_not_a_target(user_id):
    """A percent sign is not enough on its own."""
    assert read(user_id, "is the final worth 40%") is None


def test_a_course_code_is_not_a_mark(user_id):
    assert read(user_id, "does SYSC2004 have a lab hurdle") is None
    assert read(user_id, "what is the SYSC3120 policy") is None


def test_a_question_naming_no_mark_at_all(user_id):
    """The caller keeps the pass default rather than inventing a target."""
    assert read(user_id, "what happens if I miss the quiz") is None
    assert read(user_id, "") is None


def test_a_number_out_of_range_is_ignored(user_id):
    assert read(user_id, "what do I need to get 400") is None


# ------------------------------------------------------------- the endpoint

def test_the_route_is_rate_limited_like_every_other_gemini_route(rate_limited_client):
    """It was the only Gemini-backed route with no AI_LIMIT, on a page a
    student can sit and retype questions into. The shared quota is the scarce
    resource, so the limit has to bite before the view runs at all."""
    rate_limited_client.post("/signup", data={"name": "B", "email": "rl@carleton.ca",
                                              "password": PASSWORD})
    codes = [rate_limited_client.post("/api/course/SYSC2004/ask",
                                      json={"question": "how much do I need to pass"}).status_code
             for _ in range(15)]
    assert 429 in codes, "Ask ran unlimited times against the shared Gemini quota"


def test_asking_without_an_outline_says_so_rather_than_guessing(client):
    client.post("/signup", data={"name": "B", "email": "noout@carleton.ca",
                                 "password": PASSWORD})
    res = client.post("/api/course/SYSC2004/ask", json={"question": "how much do I need to pass"})
    assert res.status_code == 409
    assert "outline" in res.get_json()["error"].lower()


def test_an_empty_question_is_refused(client):
    client.post("/signup", data={"name": "B", "email": "eq@carleton.ca",
                                 "password": PASSWORD})
    res = client.post("/api/course/SYSC2004/ask", json={"question": "   "})
    assert res.status_code == 400


# ------------------------------- the target has to reach the arithmetic

SCHEME = {
    "confirmed": True, "pass_mark": 50,
    "components": [
        {"name": "Assignments", "weight": 40, "category": "assignments"},
        {"name": "Final Exam", "weight": 60, "category": "final"},
    ],
    "rules": [], "component_minimums": [], "grading_options": [],
}


@pytest.fixture()
def confirmed_course(client, monkeypatch):
    """A course with a confirmed scheme, an outline to quote, and half its
    marks in -- so `_compute_official_result` returns a real required_final."""
    import json as _json
    from uuid import uuid4
    import app as nexora

    client.post("/signup", data={"name": "B", "email": "wired@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="wired@carleton.ca").first()
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full="TEST1000",
            grading_json=_json.dumps(SCHEME), pass_mark=50))
        nexora.db.session.add(nexora.CourseContent(
            id=uuid4().hex, user_id=user.id, course_full="TEST1000",
            title="Outline", content_type="outline",
            content_text="Assignments are worth 40%. The Final Exam is worth 60%."))
        nexora.db.session.add(nexora.GradeEntry(
            id=uuid4().hex, user_id=user.id, course_full="TEST1000",
            name="Assignments", weight=40, score=30, max_score=40,
            category="assignments"))
        # Unmarked, and the row the required-final solver works against.
        nexora.db.session.add(nexora.GradeEntry(
            id=uuid4().hex, user_id=user.id, course_full="TEST1000",
            name="Final Exam", weight=60, score=None, max_score=100,
            category="final"))
        nexora.db.session.commit()

    seen = {}

    def fake_gemini(system_prompt, user_prompt, *a, **k):
        seen["prompt"] = user_prompt
        return {"answer": "See the numbers.", "citations": [], "confidence": "high"}

    monkeypatch.setattr(nexora, "_gemini_json", fake_gemini)
    return seen


def ask(client, question):
    return client.post("/api/course/TEST1000/ask", json={"question": question})


def test_asking_for_a_higher_grade_computes_for_that_grade(confirmed_course, client):
    """The whole point. The model may not do arithmetic, so if the target never
    reaches `_compute_official_result` the student is handed the numbers for a
    bare pass while being answered as though they asked about 80."""
    data = ask(client, "what do I need to get 80").get_json()
    assert data["asked_about"] == "80%"
    assert data["numbers"]["target"] == 80.0


def test_the_required_final_actually_changes_with_the_target(confirmed_course, client):
    """A number that does not move when the question moves is not an answer."""
    pass_case = ask(client, "how much do I need to pass").get_json()
    high_case = ask(client, "what do I need to get 80").get_json()
    assert pass_case["numbers"]["target"] == 50.0
    assert high_case["numbers"]["target"] == 80.0
    assert high_case["numbers"]["required_final"] > pass_case["numbers"]["required_final"]


def test_the_model_is_told_which_target_the_numbers_answer(confirmed_course, client):
    """Handing it numbers for 80 while it thinks they are for a pass is how a
    grounded answer becomes a confidently wrong one."""
    ask(client, "what do I need to get 80")
    assert "80%" in confirmed_course["prompt"]


def test_a_question_naming_no_mark_still_answers_the_pass_case(confirmed_course, client):
    data = ask(client, "what happens if I miss an assignment").get_json()
    assert data["asked_about"] == ""
    assert data["numbers"]["target"] == 50.0


def test_the_page_is_told_what_was_understood(confirmed_course, client):
    """A misread target would look completely authoritative. Same reason
    /api/study-plan/ask returns `understood`."""
    data = ask(client, "how much do i need for a B+").get_json()
    assert data["asked_about"].startswith("B+")
    assert data["asked_basis"] == "your saved grading scale"
