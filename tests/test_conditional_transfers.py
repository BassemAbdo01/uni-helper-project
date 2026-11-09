"""A transferred weight is conditional far more often than it is automatic.

Reported from a real SYSC2004 F26 outline, with a screenshot. Nexora
extracted two schemes and offered both to a student who SAT the midterm and
scored 75:

    Scheme 1 (Stated Weighting)                     ... midterm 30, final 50
    Scheme 2 (Midterm Weight Transferred to Final)  ... final 80

The outline (p.10) says the transfer is for students "unable to write the
midterm exam", who "can request" it, with documentation, within three working
days. Measured with that student's own marks: scheme 1 is **90.5**, scheme 2
is **97.4**, and the official grade is the best eligible option -- so Nexora
reported a grade **6.9 points above the real one** on a policy the student had
confirmed.

This is B1 under a different vocabulary. `_MISSED_SCHEME` read the option's
NAME and matches missed|deferred|absent|excused|no-show|did not write; the
model named this one after the arithmetic, so the guard never fired.

Both directions are tested here, because they fail in opposite ways and only
one of them is visible:

- missing a conditional scheme OVERSTATES a grade, silently;
- gating an automatic one UNDERSTATES it -- the corpus's `weight_migration`,
  "if your percentage on the final is higher ... this adjustment is
  automatic", which every student is entitled to.
"""

import json
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

PASSWORD = "correct-horse-battery"

# Verbatim from the outline that was reported, p.10.
CONDITIONAL_SOURCE = """
Midterm Exam
There will be one in-person 75-minute midterm exam (Oct 22nd during the lecture
time). Details will be provided on Brightspace. Students who are unable to
write the midterm exam because of illness or other circumstances beyond their
control (e.g., family or religious obligations) can request that the weight of
the midterm will be added to the weight of the final exam. These requests must
be made no later than 3 working days after the exam date and must be fully
supported by appropriate documentation.
"""

# Verbatim from the corpus case `weight_migration`.
AUTOMATIC_SOURCE = """
Evaluation
Assignments          20%
Midterm test         30%
Final examination    50%
If your percentage on the final examination is higher than your percentage on
the midterm test, the midterm weight will be transferred to the final
examination, which will then be worth 80%. This adjustment is automatic and
applies only in the student's favour.
"""

SYSC_BASE = [
    {"name": "Popup quizzes", "weight": 5},
    {"name": "Lab work", "weight": 6},
    {"name": "Assignments", "weight": 9},
    {"name": "Midterm exam", "weight": 30},
    {"name": "Final exam", "weight": 50},
]


def sysc_analysis():
    return {
        "pass_mark": 50,
        "components": [dict(c) for c in SYSC_BASE],
        "grading_options": [
            {"option_name": "Scheme 1 (Stated Weighting)",
             "components": [dict(c) for c in SYSC_BASE]},
            {"option_name": "Scheme 2 (Midterm Weight Transferred to Final)",
             "components": [
                 {"name": "Popup quizzes", "weight": 5},
                 {"name": "Lab work", "weight": 6},
                 {"name": "Assignments", "weight": 9},
                 {"name": "Final exam", "weight": 80},
             ]},
        ],
    }


AUTO_BASE = [
    {"name": "Assignments", "weight": 20},
    {"name": "Midterm test", "weight": 30},
    {"name": "Final examination", "weight": 50},
]


def automatic_analysis():
    return {
        "pass_mark": 50,
        "components": [dict(c) for c in AUTO_BASE],
        "grading_options": [
            {"option_name": "Stated weighting",
             "components": [dict(c) for c in AUTO_BASE]},
            {"option_name": "Midterm weight transferred to the final",
             "components": [
                 {"name": "Assignments", "weight": 20},
                 {"name": "Final examination", "weight": 80},
             ]},
        ],
    }


SYSC_MARKS = [("Popup quizzes", 5.0, 5.0, 5), ("Lab work", 6.0, 6.0, 6),
              ("Assignments", 8.0, 9.0, 9), ("Midterm exam", 75.0, 100.0, 30),
              ("Final exam", 49.0, 50.0, 50)]


@pytest.fixture()
def student(client):
    email = f"ct{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": PASSWORD})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


def official(user_id, course, analysis, marks, missed=None):
    with nexora.app.app_context():
        body = dict(analysis)
        body["confirmed"] = True
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user_id, course_full=course,
            source_uid="uid", outline_title="outline",
            grading_json=json.dumps(body), pass_mark=50))
        for name, score, mx, weight in marks:
            nexora.db.session.add(nexora.GradeEntry(
                id=uuid4().hex, user_id=user_id, course_full=course, name=name,
                weight=weight, score=score, max_score=mx,
                missed=bool(missed and name == missed)))
        nexora.db.session.commit()
        return nexora._compute_official_result(user_id, course)


# --------------------------------------------------------------------------
# reading the trigger out of the outline
# --------------------------------------------------------------------------

def test_the_outlines_own_words_gate_the_scheme():
    """The model named the option after the arithmetic and left all four
    conditions out, so the condition has to be read from the source."""
    gated = nexora._gate_conditional_transfers(sysc_analysis(), CONDITIONAL_SOURCE)
    eligibility = gated["grading_options"][1].get("eligibility") or {}
    assert eligibility.get("requires_missing") == ["Midterm exam"]


def test_an_automatic_transfer_is_never_gated():
    """`weight_migration` in the corpus. Every student is entitled to this, so
    gating it would understate a grade -- the opposite error, equally wrong."""
    assert nexora._conditional_transfer_quotes(AUTOMATIC_SOURCE) == []
    gated = nexora._gate_conditional_transfers(automatic_analysis(), AUTOMATIC_SOURCE)
    assert not (gated["grading_options"][1].get("eligibility") or {}).get("requires_missing")


def test_the_reason_is_the_outlines_own_sentence():
    """A greyed-out scheme has to say why, in the document's words, or it
    reads as Nexora refusing to calculate something."""
    gated = nexora._gate_conditional_transfers(sysc_analysis(), CONDITIONAL_SOURCE)
    message = (gated["grading_options"][1].get("eligibility") or {}).get("message", "")
    assert "unable to" in message.lower()


def test_an_outline_that_moves_no_weight_is_left_alone():
    plain = "Assignments 40%. Final exam 60%. You must pass the final."
    before = sysc_analysis()
    after = nexora._gate_conditional_transfers(sysc_analysis(), plain)
    assert after["grading_options"] == before["grading_options"]


def test_a_model_that_already_said_so_is_not_restated():
    analysis = sysc_analysis()
    analysis["grading_options"][1]["eligibility"] = {
        "requires_missing": ["Midterm exam"], "message": "the model's own words"}
    gated = nexora._gate_conditional_transfers(analysis, CONDITIONAL_SOURCE)
    assert gated["grading_options"][1]["eligibility"]["message"] == "the model's own words"


# --------------------------------------------------------------------------
# what the student is actually shown
# --------------------------------------------------------------------------

def test_a_student_who_sat_the_midterm_gets_the_stated_scheme(client, student):
    """The reported bug, with the reported marks. 97.4 was being shown."""
    gated = nexora._gate_conditional_transfers(sysc_analysis(), CONDITIONAL_SOURCE)
    result, error = official(student, "SYSC2004", gated, SYSC_MARKS)
    assert error is None
    assert result["official_grade"] == pytest.approx(90.5, abs=0.1)


def test_the_conditional_scheme_is_shown_but_not_applied(client, student):
    """It is still computed and offered as a scenario -- "if you miss the
    midterm, this is what happens" -- because deleting it would hide a real
    option from a student who may need it."""
    gated = nexora._gate_conditional_transfers(sysc_analysis(), CONDITIONAL_SOURCE)
    result, _error = official(student, "SYSC2004", gated, SYSC_MARKS)
    scheme2 = [o for o in result["options"] if "Transferred" in o["option_name"]][0]
    assert scheme2["grade"] == pytest.approx(97.4, abs=0.1)
    assert scheme2["eligible"] is False


def test_marking_the_midterm_missed_opens_the_scheme(client, student):
    """`GradeEntry.missed` is the student saying "I did not write this", and
    only that flag opens it. An empty mark is not a missed assessment."""
    gated = nexora._gate_conditional_transfers(sysc_analysis(), CONDITIONAL_SOURCE)
    result, _error = official(student, "SYSC2004", gated, SYSC_MARKS,
                              missed="Midterm exam")
    assert result["official_grade"] == pytest.approx(97.4, abs=0.1)


def test_the_stated_scheme_never_gates_itself(client, student):
    """The guard's own explanation names the other option ("... Transferred
    ...") and the word "missed", and an earlier version swept that sentence
    into EVERY option's evidence -- so the stated scheme, which omits nothing,
    was also ruled ineligible and the student was shown no grade at all. That
    is worse than the bug being fixed."""
    gated = nexora._gate_conditional_transfers(sysc_analysis(), CONDITIONAL_SOURCE)
    result, _error = official(student, "SYSC2004", gated, SYSC_MARKS)
    stated = [o for o in result["options"] if "Stated" in o["option_name"]][0]
    assert stated["eligible"] is True
    assert result["official_grade"] is not None


def test_an_option_that_omits_nothing_can_never_be_conditional():
    """The invariant behind the test above, pinned on its own: the mechanism
    IS omission, so an option containing every component cannot be a
    missed-assessment scheme however its siblings are described."""
    analysis = sysc_analysis()
    analysis["rules"] = ["Scheme 2 (Midterm Weight Transferred to Final) applies "
                         "only when Midterm exam was missed."]
    conditional, eligible, _note = nexora._missed_scheme_state(
        analysis["grading_options"][0], analysis, {})
    assert conditional is False
    assert eligible is True


def test_the_automatic_transfer_still_wins_when_it_should(client, student):
    """End to end on the legitimate case: a student whose final beats their
    midterm gets the transferred weighting, with nothing to flag."""
    gated = nexora._gate_conditional_transfers(automatic_analysis(), AUTOMATIC_SOURCE)
    marks = [("Assignments", 18.0, 20.0, 20), ("Midterm test", 60.0, 100.0, 30),
             ("Final examination", 90.0, 100.0, 50)]
    result, _error = official(student, "WEIGHTMIG", gated, marks)
    assert result["official_grade"] == pytest.approx(90.0, abs=0.1)
