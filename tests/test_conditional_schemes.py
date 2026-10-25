"""A conditional scheme applies only when its condition holds.

Reported from production: ELEC2501B showed **83%** from "Missed Midterm
Scheme" with a midterm mark entered. The standard scheme gives **82.2%**.

The winner is `max(grade)` over every option that is complete and eligible,
and eligibility had exactly two shapes, neither of which could express this
condition:

- `requires_completed` asks whether a mark is PRESENT, never absent.
- a `scheme_gate` compares another option's GRADE, not whether a mark exists.

So a missed-X scheme carried no eligibility, `required` was 0, `completed >= 0`
was always true -- and omitting the midterm redistributed its weight, which is
exactly why it scored HIGHER. The option that won was the one that should have
been impossible.

**An empty mark is not a missed assessment.** Before the midterm happens its
score is empty too, so gating on emptiness would make the missed scheme
eligible months early and show a grade that assumes the student skips it. The
gate is the student's own flag. Until they set it the option is computed and
offered as a SCENARIO, never as the official result.
"""
from uuid import uuid4

import app as nexora

# Weights chosen so the missed scheme scores HIGHER, which is the whole trap:
# dropping the midterm moves its 30 onto the rest.
ANALYSIS = {
    "components": [
        {"name": "Assignments", "weight": 20},
        {"name": "Midterm", "weight": 30},
        {"name": "Final Exam", "weight": 50},
    ],
    "grading_options": [
        {"option_name": "Standard Scheme", "components": [
            {"name": "Assignments", "weight": 20},
            {"name": "Midterm", "weight": 30},
            {"name": "Final Exam", "weight": 50},
        ]},
        {"option_name": "Missed Midterm Scheme", "components": [
            {"name": "Assignments", "weight": 20},
            {"name": "Final Exam", "weight": 80},
        ]},
    ],
}


class Row:
    def __init__(self, name, score, max_score=100, missed=False):
        self.name, self.score, self.max_score, self.missed = name, score, max_score, missed


def _rows(midterm=70.0, midterm_missed=False):
    rows = [Row("Assignments", 90.0), Row("Final Exam", 84.0)]
    if midterm is not None or midterm_missed:
        rows.append(Row("Midterm", midterm, missed=midterm_missed))
    return nexora._grade_row_map(rows)


def _options(row_map):
    return {o["option_name"]: o for o in
            [nexora._evaluate_option(opt, row_map, analysis=ANALYSIS)
             for opt in ANALYSIS["grading_options"]]}


# --------------------------------------------------------------------------
# The reported failure
# --------------------------------------------------------------------------

def test_a_missed_scheme_does_not_win_when_the_mark_is_entered():
    """The report, in one assertion. 90*0.2 + 70*0.3 + 84*0.5 = 81.0 standard;
    90*0.2 + 84*0.8 = 85.2 missed. The higher one must lose."""
    out = _options(_rows(midterm=70.0))
    assert out["Standard Scheme"]["eligible"] is True
    assert out["Missed Midterm Scheme"]["eligible"] is False, \
        "a scheme for missing the midterm applied with the midterm marked"
    assert out["Missed Midterm Scheme"]["grade"] > out["Standard Scheme"]["grade"], \
        "the fixture must keep the trap: the wrong option scores higher"


def test_an_empty_mark_is_not_a_missed_assessment():
    """Before the midterm happens its score is empty. Gating on emptiness
    would make this eligible months early and report a grade that assumes the
    student skips an assessment they have not sat yet."""
    out = _options(_rows(midterm=None))
    assert out["Missed Midterm Scheme"]["eligible"] is False
    assert out["Missed Midterm Scheme"]["scenario"] is True


def test_the_student_saying_so_is_what_opens_it():
    out = _options(_rows(midterm=None, midterm_missed=True))
    assert out["Missed Midterm Scheme"]["eligible"] is True
    assert out["Missed Midterm Scheme"]["complete"] is True


def test_a_scheme_that_needs_a_missed_assessment_cannot_be_computed():
    """The other half. Without it the standard scheme stays complete on a
    stale mark and out-scores the scheme it is meant to hand over to."""
    out = _options(_rows(midterm=70.0, midterm_missed=True))
    assert out["Standard Scheme"]["complete"] is False
    assert any("missed" in m.lower() for m in out["Standard Scheme"]["missing"])
    assert out["Missed Midterm Scheme"]["eligible"] is True


# --------------------------------------------------------------------------
# It is offered as a scenario, not hidden
# --------------------------------------------------------------------------

def test_an_unopened_scheme_is_still_computed_and_labelled():
    """A student planning around a possible absence wants the number. What
    they must not get is that number presented as their grade."""
    out = _options(_rows(midterm=70.0))["Missed Midterm Scheme"]
    assert out["scenario"] is True
    assert out["conditional"] is True
    assert out["grade"] > 0, "the scenario still carries its figure"
    assert "mark" in out["scenario_note"].lower()
    # `complete` and `eligible` are different facts and must not share an
    # answer. Every mark this option needs IS entered -- what is absent is its
    # condition. Folding the note into `missing` made `complete` False, which
    # left the figure unshowable and hid the problem from the audit.
    assert out["complete"] is True
    assert out["eligible"] is False
    assert not any("mark" in m.lower() and "missed" in m.lower()
                   for m in out["missing"]), "the reason belongs in scenario_note"


def test_an_ordinary_option_is_never_a_scenario():
    for row in _options(_rows(midterm=70.0)).values():
        if row["option_name"] == "Standard Scheme":
            assert row["scenario"] is False and row["conditional"] is False


# --------------------------------------------------------------------------
# What counts as a conditional scheme
# --------------------------------------------------------------------------

def test_the_wording_a_real_outline_uses():
    for name in ("Missed Midterm Scheme", "Deferred Exam Scheme",
                 "Scheme B - absent from the midterm",
                 "For students who did not write the midterm",
                 "Excused Absence Scheme", "No-show scheme"):
        option = {"option_name": name, "components": [
            {"name": "Assignments", "weight": 20}, {"name": "Final Exam", "weight": 80}]}
        conditional, eligible, _ = nexora._missed_scheme_state(
            option, ANALYSIS, _rows(midterm=70.0))
        assert conditional is True, name
        assert eligible is False, name


def test_an_ordinary_scheme_name_is_not_conditional():
    """Over-matching would make a normal alternative scheme permanently
    ineligible, which silently lowers a grade -- the opposite failure and
    just as bad."""
    for name in ("Standard Scheme", "Option 2", "Scheme A",
                 "Final exam weighted scheme", "Term work scheme"):
        option = {"option_name": name, "components": [
            {"name": "Assignments", "weight": 20}, {"name": "Final Exam", "weight": 80}]}
        conditional, eligible, _ = nexora._missed_scheme_state(
            option, ANALYSIS, _rows(midterm=70.0))
        assert conditional is False, name
        assert eligible is True, name


def test_requires_missing_is_honoured_when_the_outline_gives_it():
    """The schema field rides with the outline-finder corpus run. Reading it
    now means an analysis saved after that lands needs no migration."""
    option = {"option_name": "Alternative B",
              "eligibility": {"requires_missing": ["Midterm"]},
              "components": [{"name": "Assignments", "weight": 20},
                             {"name": "Final Exam", "weight": 80}]}
    conditional, eligible, _ = nexora._missed_scheme_state(option, ANALYSIS, _rows(midterm=70.0))
    assert conditional is True and eligible is False
    conditional, eligible, _ = nexora._missed_scheme_state(
        option, ANALYSIS, _rows(midterm=None, midterm_missed=True))
    assert conditional is True and eligible is True


# --------------------------------------------------------------------------
# End to end, through the official result
# --------------------------------------------------------------------------

def _account(client):
    email = f"cs{uuid4().hex[:7]}@carleton.ca"
    client.post("/signup", data={"name": "C", "email": email, "password": "nexorademo1"})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


def test_the_official_result_takes_the_standard_scheme(client):
    """End to end: the saved analysis, the saved marks, and the one function
    every screen asks. `_compute_official_result(user_id, course)` reads the
    policy off CourseOutlineProfile itself."""
    import json
    uid = _account(client)
    with nexora.app.app_context():
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=uid, course_full="ELEC2501B",
            grading_json=json.dumps(ANALYSIS)))
        for name, score in (("Assignments", 90.0), ("Midterm", 70.0), ("Final Exam", 84.0)):
            nexora.db.session.add(nexora.GradeEntry(
                id=uuid4().hex, user_id=uid, course_full="ELEC2501B",
                name=name, weight=0, score=score, max_score=100))
        nexora.db.session.commit()
        # It returns (result, error), not a bare dict.
        result, err = nexora._compute_official_result(uid, "ELEC2501B")
    assert err is None, err
    assert result["best_option"]["option_name"] == "Standard Scheme"
    assert result["official_grade"] == 81.0,         "the missed-midterm scheme scores 85.2 and must not be the answer"


def test_the_missed_flag_survives_a_mark_being_saved(client):
    """Saving a score must not silently clear the flag, and an older client
    that does not send the field must not unset it by omission."""
    uid = _account(client)
    client.post("/api/course/ELEC2501B/grades",
                json={"name": "Midterm", "weight": 30, "score": "", "missed": True})
    client.post("/api/course/ELEC2501B/grades",
                json={"name": "Midterm", "weight": 30, "score": "12/20"})
    rows = client.get("/api/course/ELEC2501B/grades").get_json()["grades"]
    assert rows[0]["missed"] is True


def test_the_flag_can_be_taken_back(client):
    uid = _account(client)
    client.post("/api/course/ELEC2501B/grades",
                json={"name": "Midterm", "weight": 30, "score": "", "missed": True})
    client.post("/api/course/ELEC2501B/grades",
                json={"name": "Midterm", "weight": 30, "score": "", "missed": False})
    assert client.get("/api/course/ELEC2501B/grades").get_json()["grades"][0]["missed"] is False


def test_flagging_missed_never_deletes_the_score(client):
    """The standing rule: never delete a grade entry with a real score. The
    flag changes what the schemes do with it, not whether it exists."""
    uid = _account(client)
    client.post("/api/course/ELEC2501B/grades",
                json={"name": "Midterm", "weight": 30, "score": "15/20"})
    client.post("/api/course/ELEC2501B/grades",
                json={"name": "Midterm", "weight": 30, "score": "15/20", "missed": True})
    row = client.get("/api/course/ELEC2501B/grades").get_json()["grades"][0]
    assert row["missed"] is True
    assert row["score"] == 15.0 and row["max_score"] == 20.0


def test_the_column_is_in_the_migration_map():
    import inspect
    src = inspect.getsource(nexora.ensure_schema_columns)
    block = src.split('"grade_entry"')[1].split("}")[0]
    assert "missed" in block


# --------------------------------------------------------------------------
# The page must not disagree with itself
# --------------------------------------------------------------------------

def test_the_calculator_excludes_a_missed_mark_in_one_place():
    """`pctOf` is the single definition the dial, the contribution line and
    the standing panel's `totals()` all read.

    Special-casing the dial alone was tried and caught in the browser: the
    standing panel went on counting the missed midterm and printed 81.00%
    directly beneath an official panel saying 85.20%. One screen disagreeing
    with itself is the exact failure this whole fix exists to remove."""
    import io as _io
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    page = _io.open(os.path.join(here, "templates", "grade_calculator.html"),
                    encoding="utf-8").read()
    body = page.split("const pctOf = c => {", 1)[1].split("};", 1)[0]
    assert "if (c.missed) return null;" in body,         "pctOf must be where a missed assessment stops counting"
    assert "missed: !!c.missed" in page, "the flag has to reach the server"


def test_the_toggle_is_not_hidden_behind_a_display_rule():
    """It is toggled with aria-pressed rather than [hidden], so no display
    rule can out-rank it -- the #courseState trap, avoided by construction."""
    import io as _io
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    page = _io.open(os.path.join(here, "templates", "grade_calculator.html"),
                    encoding="utf-8").read()
    assert '.missed-toggle[aria-pressed="true"]' in page
    assert "missed.setAttribute('aria-pressed'" in page
