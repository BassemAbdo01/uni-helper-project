"""Adding to an outline that is already on file.

A grading policy changes after the outline is read -- a bonus announced on
Brightspace, weight moved off a cancelled quiz. The student pastes it into
the tutor and says "add it to the course outline", and the tutor answered
that it had no access to edit the outline. That is the app refusing the one
thing it is for.

The model reads the addition and returns structured EDITS; the server applies
them through the same code as a student's own correction. These tests stub
the model read and pin what the server does with it:

- an amount stated against a part of the course ("1% on your final lab
  grade", lab = 40%) is converted on the server, and the note shows the sum;
- an entered mark on another component survives an amendment;
- the tutor recognises the instruction, finds the course and the text (in the
  message, or a turn earlier when the student says "add it"), and reports what
  changed instead of declining.
"""
import json
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

LECTURE_LAB = {
    "components": [
        {"name": "Midterm", "weight": 20, "category": "exam"},
        {"name": "Final Exam", "weight": 40, "category": "exam"},
        {"name": "Lab Reports", "weight": 40, "category": "lab"},
    ],
    "pass_mark": 50, "pass_rules": [], "rules": [], "confirmed": True,
}

BRIGHTSPACE_TEXT = (
    "There are 2 bonus opportunities to help improve your lab grade: #1 - If you complete 6/8 content "
    "quizzes with at least 80%, a bonus 1% is added to your final lab grade (automatically applied at the "
    "end of term). Note that each content quiz is available for 2 weeks in the respective Brightspace "
    "module. #2 - Provide lab component feedback for a bonus 1% on your final lab grade (available in this "
    "module towards the end of November)."
)

MODEL_EDITS = {
    "add_components": [
        {"name": "Lab bonus: content quizzes", "weight": 1, "weight_of": "Lab Reports", "category": "bonus",
         "notes": "Complete 6 of 8 content quizzes with at least 80%."},
        {"name": "Lab bonus: component feedback", "weight": 1, "weight_of": "Lab Reports", "category": "bonus",
         "notes": "Provide lab component feedback, late November."},
    ],
    "components": {}, "add_pass_rules": [],
    "rules": ["Each content quiz is available for 2 weeks in its Brightspace module."],
    "summary": "Two 1% bonuses on the lab grade were added.",
}


@pytest.fixture()
def student(client, monkeypatch):
    email = f"amend{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": "amend-pass-123"})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        uid = user.id
        nexora.db.session.add(nexora.SyncedCourse(id=uuid4().hex, user_id=uid, source_uid="b1",
                                                  course_code="BIOL1103", course_name="Biology", term="Fall 2026"))
        nexora.db.session.add(nexora.CourseContent(id=uuid4().hex, user_id=uid, course_full="BIOL1103",
                                                   source_uid="outline-1", title="Outline", content_type="outline",
                                                   content_text="Midterm 20%, Final Exam 40%, Lab Reports 40%. " * 10))
        nexora.db.session.add(nexora.CourseOutlineProfile(id=uuid4().hex, user_id=uid, course_full="BIOL1103",
                                                          source_uid="outline-1", grading_json=json.dumps(LECTURE_LAB),
                                                          pass_mark=50))
        for c in LECTURE_LAB["components"]:
            nexora.db.session.add(nexora.GradeEntry(id=uuid4().hex, user_id=uid, course_full="BIOL1103",
                                                    name=c["name"], weight=c["weight"], category=c["category"],
                                                    score=17 if c["name"] == "Midterm" else None, max_score=20))
        nexora.db.session.commit()
    monkeypatch.setattr(nexora, "_outline_amendment_edits", lambda course, analysis, text: dict(MODEL_EDITS))
    return uid


def _rows(uid):
    with nexora.app.app_context():
        return {r.name: r for r in nexora.GradeEntry.query.filter_by(user_id=uid, course_full="BIOL1103").all()}


def _policy(uid):
    with nexora.app.app_context():
        p = nexora.CourseOutlineProfile.query.filter_by(user_id=uid, course_full="BIOL1103").first()
        return json.loads(p.grading_json)


def test_a_bonus_on_the_lab_grade_is_converted_on_the_server(client, student):
    r = client.post("/api/course/BIOL1103/outline/amend", json={"text": BRIGHTSPACE_TEXT})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    names = {c["name"]: c for c in d["analysis"]["components"]}
    quiz = names["Lab bonus: content quizzes"]
    assert quiz["weight"] == 0.4                      # 1% of a 40% lab, not 1% of the course
    assert quiz["category"] == "bonus"
    assert "40% of the course" in quiz["notes"] and "0.4% of the course" in quiz["notes"]
    assert names["Lab bonus: component feedback"]["weight"] == 0.4
    assert any("content quiz is available" in rule for rule in d["analysis"]["rules"])
    assert any(c.startswith("Added: Lab bonus: content quizzes (0.4%)") for c in d["changed"])


def test_an_entered_mark_survives_the_amendment(client, student):
    client.post("/api/course/BIOL1103/outline/amend", json={"text": BRIGHTSPACE_TEXT})
    rows = _rows(student)
    assert rows["Midterm"].score == 17 and rows["Midterm"].max_score == 20
    assert "Lab bonus: content quizzes" in rows and rows["Lab bonus: content quizzes"].score is None
    assert rows["Lab bonus: content quizzes"].notes.startswith("Complete 6 of 8")


def test_the_words_are_kept_with_the_outline_for_ask_to_quote(client, student):
    client.post("/api/course/BIOL1103/outline/amend", json={"text": BRIGHTSPACE_TEXT})
    with nexora.app.app_context():
        row = nexora.CourseContent.query.filter_by(user_id=student, source_uid="outline-1").first()
        assert "bonus 1% is added to your final lab grade" in row.content_text
    policy = _policy(student)
    assert policy["addenda"] and "6/8 content quizzes" in policy["addenda"][0]["text"]
    assert policy["confirmed"] is True     # the student asked for this; it does not un-confirm the policy


def test_amending_a_course_with_no_outline_says_so(client, student):
    r = client.post("/api/course/SYSC2004/outline/amend", json={"text": BRIGHTSPACE_TEXT})
    assert r.status_code == 422 and "no outline on file" in r.get_json()["error"]


# ------------------------------------------------------------------ the tutor

def test_the_tutor_recognises_the_instruction():
    yes = ["add it to the course outline", "Add this to my syllabus please", "can you update the outline with this",
           "put this in the grading scheme", "record this on my grade calculator",
           "Course: BIOL1103. Student question: add it to the course outline"]
    no = ["explain the outline to me", "what does the outline say about the final",
          "summarise lecture 4", "how much do I need to pass"]
    for q in yes:
        assert nexora._wants_outline_amendment(q), q
    for q in no:
        assert not nexora._wants_outline_amendment(q), q


def test_the_tutor_adds_what_the_student_pasted_a_turn_earlier(client, student):
    history = [
        {"role": "user", "content": BRIGHTSPACE_TEXT},
        {"role": "assistant", "content": "Noted -- two bonus opportunities on the lab grade."},
    ]
    r = client.post("/api/copilot", json={"message": "Course: BIOL1103. Student question: add it to the course outline",
                                          "history": history})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["answer"].startswith("Done")
    assert "Lab bonus: content quizzes" in d["answer"] and "0.4%" in d["answer"]
    assert "grade-calculator" in d["answer"]
    assert d["course"] == "BIOL1103" and len(d["outline_changed"]) >= 3
    assert "Lab bonus: content quizzes" in _rows(student)


def test_the_tutor_adds_text_that_comes_with_the_instruction(client, student):
    r = client.post("/api/copilot", json={"message": f"add this to the BIOL 1103 outline: {BRIGHTSPACE_TEXT}",
                                          "history": []})
    d = r.get_json()
    assert d["answer"].startswith("Done"), d
    assert "Lab bonus: component feedback" in _rows(student)


def test_the_tutor_asks_for_the_course_when_it_cannot_tell(client, student):
    r = client.post("/api/copilot", json={"message": f"add this to the outline: {BRIGHTSPACE_TEXT}", "history": []})
    d = r.get_json()
    assert "Which course" in d["answer"]
    assert "Lab bonus: content quizzes" not in _rows(student)


def test_the_tutor_asks_for_the_text_when_there_is_none(client, student):
    r = client.post("/api/copilot", json={"message": "Course: BIOL1103. Student question: add it to the outline",
                                          "history": []})
    d = r.get_json()
    assert "Paste the text" in d["answer"]


def test_a_bonus_on_a_group_uses_the_whole_groups_weight(client, student, monkeypatch):
    """BIOL 1103 is 60 lecture / 40 lab, and the lab is two rows (reports 28,
    lab exam 12) under a derived "Lab component". "1% on your final lab
    grade" is 1% of the 40, not of the largest row."""
    with nexora.app.app_context():
        p = nexora.CourseOutlineProfile.query.filter_by(user_id=student, course_full="BIOL1103").first()
        p.grading_json = json.dumps({
            "components": [
                {"name": "Lecture Midterm", "weight": 20}, {"name": "Final Exam", "weight": 40},
                {"name": "Lab Reports", "weight": 28}, {"name": "Final Lab Exam", "weight": 12},
            ],
            "derived_components": [{"name": "Lab component", "components": ["Lab Reports", "Final Lab Exam"]}],
            "pass_mark": 50, "pass_rules": [], "rules": [],
        })
        nexora.db.session.commit()
    monkeypatch.setattr(nexora, "_outline_amendment_edits", lambda course, analysis, text: {
        "add_components": [{"name": "Lab bonus: content quizzes", "weight": 1, "weight_of": "Lab component",
                            "category": "bonus", "notes": "6 of 8 quizzes at 80%+."}],
        "summary": "One bonus."})
    d = client.post("/api/course/BIOL1103/outline/amend", json={"text": BRIGHTSPACE_TEXT}).get_json()
    bonus = next(c for c in d["analysis"]["components"] if c["name"] == "Lab bonus: content quizzes")
    assert bonus["weight"] == 0.4
    assert "Lab component (40% of the course)" in bonus["notes"]
