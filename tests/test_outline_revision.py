"""Tell Nexora what is wrong with the scheme, and confirm before it changes.

Reported with the SYSC2004 scheme bug: "there HAS TO BE A WAY for students to
talk to the Nexora AI and let him fix the schema like remove an entire section
or edit it but nexora has to be smart and warn the student and confirm with
the schema first."

The split is the calendar assistant's, and it is the whole feature: the model
reads the request, the server decides what it would do, and **nothing is
written until the student says yes**. `/outline/amend` applies immediately,
which is right for "the prof announced a bonus" and wrong for "delete the
midterm".

Every warning is computed on the server from facts it holds. None is asked of
the model -- a warning that depends on the model noticing is not a warning.
"""

import json
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

PASSWORD = "correct-horse-battery"

OUTLINE_TEXT = """
SYSC 2004 Course Outline
Evaluation and Marking Scheme
Popup quizzes during every lecture 5
Lab work (12 @ 0.5% each) 6
Assignments (3 @ 3% each) 9
Midterm exam 30
Final exam 50
Total 100
"""

SCHEME = {
    "confirmed": True,
    "pass_mark": 50,
    "components": [
        {"name": "Popup quizzes", "weight": 5, "category": "quiz"},
        {"name": "Lab work", "weight": 6, "category": "lab"},
        {"name": "Assignments", "weight": 9, "category": "assignment"},
        {"name": "Midterm exam", "weight": 30, "category": "exam"},
        {"name": "Final exam", "weight": 50, "category": "final"},
    ],
}


@pytest.fixture()
def student(client):
    email = f"rv{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": PASSWORD})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


def setup_course(user_id, course="SYSC2004", marks=(), analysis=None):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.CourseContent(
            id=uuid4().hex, user_id=user_id, course_full=course,
            source_uid="uid:outline", title="SYSC 2004 Course Outline",
            content_type="outline", content_text=OUTLINE_TEXT,
            module_name="", source_url=""))
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user_id, course_full=course,
            source_uid="uid:outline", outline_title="SYSC 2004 Course Outline",
            grading_json=json.dumps(analysis or SCHEME), pass_mark=50))
        for name, score, weight in marks:
            nexora.db.session.add(nexora.GradeEntry(
                id=uuid4().hex, user_id=user_id, course_full=course, name=name,
                weight=weight, score=score, max_score=100))
        nexora.db.session.commit()


def warnings_for(user_id, edits, course="SYSC2004"):
    with nexora.app.app_context():
        return nexora._outline_revision_warnings(
            user_id, course, SCHEME, edits, OUTLINE_TEXT)


# --------------------------------------------------------------------------
# the edits are checked against the scheme, never trusted
# --------------------------------------------------------------------------

def test_a_component_the_scheme_does_not_have_is_dropped():
    """The apply call makes a round trip through the browser, so what comes
    back is re-checked -- the `recourse` rule. Inventing a component the
    student never saw is not what they confirmed."""
    clean, dropped = nexora._revision_edits_against_scheme(
        SCHEME, {"remove_components": ["Midterm exam", "Participation"]})
    assert clean["remove_components"] == ["Midterm exam"]
    assert dropped == ["Participation"]


def test_a_name_is_resolved_to_the_schemes_own_spelling():
    """The model answers "midterm"; the applier keys on the exact name."""
    clean, _dropped = nexora._revision_edits_against_scheme(
        SCHEME, {"components": {"midterm exam": 0}})
    assert clean["components"] == {"Midterm exam": 0.0}


@pytest.mark.parametrize("weight", [-5, 140, "lots", None])
def test_a_weight_that_is_not_a_percentage_is_dropped(weight):
    clean, dropped = nexora._revision_edits_against_scheme(
        SCHEME, {"components": {"Midterm exam": weight}})
    assert "components" not in clean
    assert dropped == ["Midterm exam"]


# --------------------------------------------------------------------------
# the warnings, which are what "smart" has to mean
# --------------------------------------------------------------------------

def test_removing_a_component_with_a_mark_on_it_warns(client, student):
    """A silently orphaned mark is how a calculator starts disagreeing with
    itself. The mark is kept; the student is told."""
    setup_course(student, marks=[("Midterm exam", 75.0, 30)])
    notes = warnings_for(student, {"remove_components": ["Midterm exam"]})
    assert any("mark you entered" in n and "Midterm exam" in n for n in notes)


def test_removing_a_component_with_no_mark_does_not_warn_about_marks(client, student):
    setup_course(student)
    notes = warnings_for(student, {"remove_components": ["Midterm exam"]})
    assert not any("mark you entered" in n for n in notes)


def test_a_change_that_stops_the_weights_adding_up_warns(client, student):
    """The one error nobody sees until a grade is wrong."""
    setup_course(student)
    notes = warnings_for(student, {"remove_components": ["Midterm exam"]})
    assert any("not 100" in n for n in notes)


def test_a_change_that_keeps_the_total_does_not_warn_about_it(client, student):
    setup_course(student)
    notes = warnings_for(student, {"components": {"Midterm exam": 20, "Final exam": 60}})
    assert not any("not 100" in n for n in notes)


def test_contradicting_the_outline_quotes_the_outline(client, student):
    """"Confirm with the schema first". The student is usually right --
    outlines get amended, sections get cancelled -- but they should see the
    line they are overruling."""
    setup_course(student)
    notes = warnings_for(student, {"components": {"Midterm exam": 0}})
    assert any("Your outline says" in n and "Midterm" in n for n in notes)


def test_setting_a_weight_the_outline_already_states_does_not_warn(client, student):
    """Re-stating 30 for a component the outline puts at 30 is not a conflict,
    and warning about it would train the student to ignore the warnings."""
    setup_course(student)
    notes = warnings_for(student, {"components": {"Midterm exam": 30}})
    assert not any("Your outline says" in n for n in notes)


def test_something_that_refers_to_a_removed_component_warns(client, student):
    """A rule left pointing at nothing is how a scheme fails a student for a
    requirement nobody can evaluate -- `_evaluate_pass_rules` again."""
    analysis = dict(SCHEME)
    analysis["selection_rules"] = [
        {"type": "best_n_of_m", "components": ["Midterm exam", "Final exam"],
         "keep": 1, "output_component": "Best exam"}]
    setup_course(student, analysis=analysis)
    with nexora.app.app_context():
        notes = nexora._outline_revision_warnings(
            student, "SYSC2004", analysis,
            {"remove_components": ["Midterm exam"]}, OUTLINE_TEXT)
    assert any("Best exam" in n for n in notes)


# --------------------------------------------------------------------------
# preview writes nothing; apply writes
# --------------------------------------------------------------------------

def test_the_preview_changes_nothing(client, student, monkeypatch):
    setup_course(student, marks=[("Midterm exam", 75.0, 30)])
    monkeypatch.setattr(nexora, "_outline_revision_edits", lambda *a, **k: {
        "remove_components": ["Midterm exam"], "understood": "remove the midterm",
        "summary": "Removes Midterm exam."})

    res = client.post("/api/course/SYSC2004/outline/revise",
                      json={"instruction": "remove the midterm"})
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["applied"] is False
    assert body["understood"] == "remove the midterm"
    assert body["warnings"]

    with nexora.app.app_context():
        profile = nexora.CourseOutlineProfile.query.filter_by(
            user_id=student, course_full="SYSC2004").first()
        names = [c["name"] for c in json.loads(profile.grading_json)["components"]]
    assert "Midterm exam" in names, "the preview must not have written anything"


def test_the_preview_shows_the_scheme_afterwards(client, student, monkeypatch):
    setup_course(student)
    monkeypatch.setattr(nexora, "_outline_revision_edits", lambda *a, **k: {
        "remove_components": ["Midterm exam"], "components": {"Final exam": 80}})
    res = client.post("/api/course/SYSC2004/outline/revise",
                      json={"instruction": "drop the midterm, final is 80"})
    rows = {r["name"]: r for r in res.get_json()["preview"]}
    assert rows["Midterm exam"]["state"] == "removed"
    assert rows["Final exam"]["state"] == "changed"
    assert rows["Final exam"]["weight"] == 80
    assert rows["Lab work"]["state"] == "kept"


def test_apply_writes_the_change(client, student):
    setup_course(student)
    res = client.post("/api/course/SYSC2004/outline/revise",
                      json={"apply": True, "edits": {"remove_components": ["Midterm exam"]}})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["applied"] is True

    with nexora.app.app_context():
        profile = nexora.CourseOutlineProfile.query.filter_by(
            user_id=student, course_full="SYSC2004").first()
        names = [c["name"] for c in json.loads(profile.grading_json)["components"]]
    assert "Midterm exam" not in names


def test_apply_never_destroys_an_entered_mark(client, student):
    """The standing instruction in this project: never overwrite a score the
    student entered manually. The component leaves the scheme; the mark stays
    on file."""
    setup_course(student, marks=[("Midterm exam", 75.0, 30)])
    client.post("/api/course/SYSC2004/outline/revise",
                json={"apply": True, "edits": {"remove_components": ["Midterm exam"]}})
    with nexora.app.app_context():
        rows = {g.name: g.score for g in nexora.GradeEntry.query.filter_by(
            user_id=student, course_full="SYSC2004").all()}
    assert rows.get("Midterm exam") == 75.0


def test_apply_refuses_an_edit_that_matches_nothing(client, student):
    setup_course(student)
    res = client.post("/api/course/SYSC2004/outline/revise",
                      json={"apply": True, "edits": {"remove_components": ["Participation"]}})
    assert res.status_code == 422


def test_a_course_with_no_scheme_says_so(client, student):
    res = client.post("/api/course/MATH1004/outline/revise",
                      json={"instruction": "remove the midterm"})
    assert res.status_code == 404


def test_an_empty_instruction_is_refused(client, student):
    setup_course(student)
    res = client.post("/api/course/SYSC2004/outline/revise", json={"instruction": "   "})
    assert res.status_code == 400


# --------------------------------------------------------------------------
# a change must leave ONE answer behind it
# --------------------------------------------------------------------------

SCHEME_WITH_OPTIONS = {
    "confirmed": True,
    "pass_mark": 50,
    "components": SCHEME["components"],
    "grading_options": [
        {"option_name": "Scheme 1 (Stated Weighting)",
         "components": [dict(c) for c in SCHEME["components"]]},
        {"option_name": "Scheme 2 (Midterm Weight Transferred to Final)",
         "components": [
             {"name": "Popup quizzes", "weight": 5},
             {"name": "Lab work", "weight": 6},
             {"name": "Assignments", "weight": 9},
             {"name": "Final exam", "weight": 80},
         ]},
    ],
}


def test_a_kept_mark_stops_counting_toward_the_total(client, student):
    """Removing a component keeps its mark -- it is the student's -- but the
    row kept its WEIGHT too, so every total still divided by it. Measured in
    the browser: a removed 30% midterm left the calculator reporting 92.23%
    (119.9 out of 130) for a student whose scheme now totalled 100. Zeroing
    the weight is what the removal already promises in so many words."""
    setup_course(student, marks=[("Midterm exam", 75.0, 30)])
    client.post("/api/course/SYSC2004/outline/revise",
                json={"apply": True, "edits": {"remove_components": ["Midterm exam"]}})
    with nexora.app.app_context():
        row = nexora.GradeEntry.query.filter_by(
            user_id=student, course_full="SYSC2004", name="Midterm exam").first()
    assert row is not None, "the mark must never be destroyed"
    assert row.score == 75.0
    assert row.weight == 0.0, "but it must stop counting"


def test_a_weight_change_reaches_the_schemes_that_held_the_old_one(client, student):
    """An option carries its own copy of every weight. Leaving those behind is
    how the page came to show 92.23% while `_compute_official_result` read the
    stale option and said 68 -- two numbers for one student."""
    setup_course(student, analysis=SCHEME_WITH_OPTIONS)
    client.post("/api/course/SYSC2004/outline/revise",
                json={"apply": True, "edits": {"components": {"Lab work": 10}}})
    with nexora.app.app_context():
        profile = nexora.CourseOutlineProfile.query.filter_by(
            user_id=student, course_full="SYSC2004").first()
        saved = json.loads(profile.grading_json)
    for option in saved["grading_options"]:
        labs = [c for c in option["components"] if c["name"] == "Lab work"]
        if labs:
            assert labs[0]["weight"] == 10, option["option_name"]


def test_an_option_that_deliberately_differs_is_not_flattened(client, student):
    """Options exist precisely so a component can be weighted differently in
    each. Only a copy still holding the OLD value is updated."""
    setup_course(student, analysis=SCHEME_WITH_OPTIONS)
    client.post("/api/course/SYSC2004/outline/revise",
                json={"apply": True, "edits": {"components": {"Final exam": 55}}})
    with nexora.app.app_context():
        profile = nexora.CourseOutlineProfile.query.filter_by(
            user_id=student, course_full="SYSC2004").first()
        saved = json.loads(profile.grading_json)
    scheme2 = [o for o in saved["grading_options"] if "Transferred" in o["option_name"]][0]
    final = [c for c in scheme2["components"] if c["name"] == "Final exam"][0]
    assert final["weight"] == 80, "scheme 2 weighs the final at 80 on purpose"
