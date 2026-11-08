"""B3: "Final Subtopic Report" is not a final exam.

Reported from a real workspace: an entry titled "Week 7 Quiz ... Final
Subtopic Report ... Final Submission" wore the **FINAL** badge, which is the
loudest thing the calendar can say about a day. The qualifier in

    r"\\bfinal(\\s+(exam|examination|test|assessment))?\\b"

was OPTIONAL, so the bare word matched wherever it appeared -- and "final" is
an ordinary adjective on coursework: final draft, final submission, final
report, final project.

`_NOT_AN_EXAM` could never have caught these. That list is about *practice*
and *review* -- a rehearsal of the thing, not a different thing wearing the
same word.

The rule is a POSITIVE signal, which is the same decision `_material_section`
made: a list of what counts as naming the final is short, while a list of every
noun "final" might be modifying is endless and wrong the first time a course
invents a new one. Anything unrecognised still gets its EXAM or TEST badge if
those words are there, so this can only ever DOWNGRADE a tag, never remove a
row from the calendar.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as nexora


def label(title, kind="due"):
    return nexora._major_assessment_label(kind, title)


# ----------------------------------------------------- the reported case

def test_a_final_report_is_not_the_final_exam():
    assert label("Final Subtopic Report - Final Submission") == ""


@pytest.mark.parametrize("title", [
    "Final Submission",
    "Final Draft",
    "Final Report",
    "Final Project",
    "Final Essay",
    "Final Presentation",
    "Final Reflection",
    "Assignment 4 - Final Version",
])
def test_final_as_an_adjective_on_coursework_is_not_a_final(title):
    assert label(title) == "", title


# ------------------------------------------------------ what IS a final

@pytest.mark.parametrize("title", [
    "Final Exam",
    "Final Examination",
    "FINAL EXAM",
    "final exam",
    "Final Test",
    "Final Assessment",
    "SYSC 2004 Final Exam",
    "Final Exam - Minto 2000",
])
def test_the_qualified_word_is_a_final(title):
    assert label(title) == "FINAL", title


@pytest.mark.parametrize("title", [
    "Final",
    "Finals",
    "SYSC 2004 Final",
    "ELEC2501B Final",
])
def test_the_word_standing_alone_as_the_assessment_is_a_final(title):
    """A university's exam schedule prints the course and the word, and
    nothing else -- there is no noun for it to be modifying."""
    assert label(title) == "FINAL", title


# -------------------------------------------------------- what it is not

def test_a_final_lab_exam_is_an_exam_but_not_the_final():
    """BIOL 1103 has both, which is exactly why `_final_name_score` exists.
    Downgraded, never dropped: it is still an exam on the calendar."""
    assert label("Final Lab Exam") == "EXAM"


def test_practice_and_review_are_still_vetoed():
    assert label("Practice final") == ""
    assert label("Final exam review session") == ""


def test_the_other_badges_are_untouched():
    assert label("Midterm exam") == "MIDTERM"
    assert label("Midterm 2") == "MIDTERM"
    assert label("Unit 3 Test") == "TEST"
    assert label("Quiz 4") == ""


def test_a_hand_added_exam_still_reads_as_one():
    """`kind` is the student's own word for it and still decides when the
    title says nothing."""
    assert label("Something", kind="exam") == "EXAM"
    assert label("Something", kind="final") == "FINAL"
