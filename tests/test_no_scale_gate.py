"""Analyzing an outline must not require a saved grading scale.

`/outline/analyze` answered 428 until the student had saved a grading scale in
Settings -- which is every new account -- and the grade calculator showed that
error three cards below the button, so the app's owner uploaded his outline
four times and reported the button as broken.

The scale is only needed to turn a LETTER rule ("C- or better") into a number,
and `_letter_requirement_result` already returns `passed: None` when it cannot:
pending, never failing. So the analysis runs with the scale on file (the
default when none is saved), and the response says when the outline has a
letter rule so the page can point at Settings.

Also fixed on the way: the fallback's letter-rule regex used `\b` after an
optional sign, and a word boundary cannot sit between "-" and a space, so a
signed letter never matched. "C- or better" was read as no rule at all.
"""

import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"

OUTLINE = """ELEC 2501 Circuits and Signals
Evaluation
Assignments (5)              15%
Laboratory work              15%
Midterm examination          25%
Final examination            45%
To pass the course a student must obtain at least 50% overall AND at least
40% on the final examination. A grade of C- or better is required to proceed
to ELEC 3500.
"""

PERCENT_ONLY = OUTLINE.split("A grade of")[0]


@pytest.fixture()
def fresh(client):
    """No grading scale saved -- every new account."""
    client.post("/signup", data={"name": "B", "email": f"nogate{uuid4().hex[:6]}@carleton.ca",
                                 "password": PASSWORD})
    return client


def analyze(client, text):
    uid = client.post("/api/course/ELEC2501/outline/upload", json={"text": text}).get_json()["source_uid"]
    return client.post("/api/course/ELEC2501/outline/analyze", json={"source_uid": uid})


def test_analysis_runs_without_a_saved_scale(fresh):
    res = analyze(fresh, OUTLINE)
    assert res.status_code == 200, res.get_json()
    comps = {c["name"]: c["weight"] for c in res.get_json()["analysis"]["components"]}
    assert comps == {"Assignments": 15.0, "Lab": 15.0, "Midterm": 25.0, "Final Exam": 45.0}


def test_the_response_says_the_scale_is_not_saved(fresh):
    assert analyze(fresh, OUTLINE).get_json()["scale_configured"] is False


def test_a_letter_rule_earns_a_note_pointing_at_settings(fresh):
    body = analyze(fresh, OUTLINE).get_json()
    assert "C-" in body["scale_note"]
    assert "Settings" in body["scale_note"]


def test_a_percent_only_outline_never_mentions_the_scale(fresh):
    """A student whose outline has no letter rule must not be sent to Settings
    for something that cannot change their result."""
    body = analyze(fresh, PERCENT_ONLY).get_json()
    assert body["scale_note"] == ""


def test_a_signed_letter_rule_is_read_in_the_letter_first_order(fresh):
    """"A grade of C- or better is required to proceed" -- the commonest way a
    prerequisite is written, and it was dropped twice over: wrong word order,
    and a \b that could not match after the minus sign."""
    rules = analyze(fresh, OUTLINE).get_json()["analysis"]["letter_requirements"]
    assert any(r["minimum_letter"] == "C-" for r in rules), rules
    assert any(r["scope"] == "prerequisite" for r in rules if r["minimum_letter"] == "C-")


def test_a_letter_rule_that_cannot_be_evaluated_is_pending_not_failing(fresh):
    """The whole reason the gate was safe to remove."""
    analyze(fresh, OUTLINE)
    fresh.post("/api/course/ELEC2501/outline/confirm")
    res = fresh.post("/api/course/ELEC2501/grade-outcomes", json={"target": 50})
    assert res.status_code == 200
    assert res.get_json()["official_status"] != "FAIL"


def test_the_page_puts_the_analysis_message_beside_the_buttons(fresh):
    """The error used to render at the foot of Step Three."""
    body = fresh.get("/grade-calculator").get_data(as_text=True)
    assert "intakeStatus" in body
    assert 'id="analysisMeta"' in body


def test_the_page_hides_the_results_until_there_is_something_in_them(fresh):
    body = fresh.get("/grade-calculator").get_data(as_text=True)
    assert 'id="resultsPanel" hidden' in body
    assert 'id="byHand"' in body      # but the by-hand path is still reachable
