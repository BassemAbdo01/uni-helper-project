"""Which number is the final-exam minimum, when a sentence holds two.

"To pass the course a student must obtain at least 50% overall AND at least
40% on the final examination" was read by the deterministic parser as a 50%
final minimum: the gap between a threshold and the word "final" matched from
the FIRST "at least" and skipped straight over the second. A student at 45% on
the final would have been told they failed. Found in the browser while
pasting an outline into the grade calculator.

The parser runs on every outline as the fast path for a clean table and as the
fallback when Gemini is unavailable, so it is not an edge case.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TABLE = "Assignments 40%. Final examination 60%. "


@pytest.mark.parametrize("sentence,expected", [
    ("To pass the course a student must obtain at least 50% overall AND at least 40% on the final examination.", 40),
    ("Students must obtain at least 40% on the final examination and at least 50% overall to pass.", 40),
    ("A minimum of 50% overall is required. The final examination requires a minimum of 45%.", 45),
    ("Students must obtain at least 35% on the final exam to pass the course.", 35),
    ("Students must pass the final examination with at least 40%.", 40),
    ("To pass the course a student must obtain at least 50% overall AND at least 40% on the "
     "final examination. A student who scores below 40% on the final examination will receive "
     "a grade no higher than D.", 40),
])
def test_the_finals_own_threshold_wins_whichever_order_the_sentence_uses(sentence, expected):
    import app as nexora
    assert nexora._local_outline_analysis(TABLE + sentence)["final_minimum"] == expected


def test_an_overall_mark_alone_is_not_a_final_minimum():
    import app as nexora
    out = nexora._local_outline_analysis(TABLE + "A passing grade of 50% is required to pass the course.")
    assert out["final_minimum"] is None
    assert out["pass_mark"] == 50


def test_a_final_named_inside_an_aggregate_is_not_its_own_minimum():
    """"50% on the theory component (quizzes, tutorials and final exam)" is a
    rule about the group, not about the final."""
    import app as nexora
    out = nexora._local_outline_analysis(
        TABLE + "Students must obtain at least 50% on the theory component (quizzes, tutorials and final exam).")
    assert out["final_minimum"] is None


def test_the_rule_shown_to_the_student_carries_the_right_number():
    import app as nexora
    out = nexora._local_outline_analysis(
        TABLE + "To pass the course a student must obtain at least 50% overall AND at least 40% on the final examination.")
    finals = [r for r in out["pass_rules"] if r.get("component") == "Final Exam"]
    assert finals and finals[0]["minimum_percent"] == 40
    assert "40%" in finals[0]["message"]
