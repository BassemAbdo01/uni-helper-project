"""The outline finder: which document is this course's outline.

Five places asked that question and answered it differently, and two of them
-- Ask-the-outline and the planner's course brief -- matched on
`content_type` alone, which is the EXTENSION's answer from scrape time. So a
student who picked their outline by hand could have it analysed and then not
be able to ask it a question: Ask found no text and answered from the saved
numbers with no document behind it.

What these tests pin is the shape of the decision, never a score:

- a title that can only be naming the outline is trusted on its own
- a title that merely suggests it has to be corroborated by the contents
- nothing that is ABOUT one assessment is ever the outline
- the list is ORDERED, because the page reads the first entry
- `confident` separates offering a document from reading one
- an analysis nobody asked for never deletes a mark somebody typed
"""

import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

PASSWORD = "correct-horse-battery"

GRADING_BODY = """
COURSE OUTLINE -- ELEC 2501
Instructor: Dr. A. Patel.  Office hours: Tuesdays 10:00-11:30, ME 4158.
Prerequisite: ELEC 2501 or equivalent. Academic integrity applies.
Evaluation:
Assignments 20%
Midterm Examination 30%
Final Examination 50%
Late penalty: 10% per day.
"""

LAB_BODY = """
LAB 3 -- Operational Amplifiers. Bring your own breadboard.
Build the circuit in figure 2 and record the gain at each frequency.
Your report is due one week after the session. Late reports are not accepted.
"""


class Row(object):
    """A content row's duck type, for the scoring tests that need no database."""

    def __init__(self, title, kind="page", body=""):
        self.title = title
        self.content_type = kind
        self.content_text = body
        self.source_uid = "uid:" + title


@pytest.fixture()
def student(client):
    """A signed-in account. `/signup` logs the client in, so the same client
    then speaks for this student."""
    email = f"of{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": PASSWORD})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


def content(user_id, course, title, kind="page", body="", uid=None):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.CourseContent(
            id=uuid4().hex, user_id=user_id, course_full=course,
            source_uid=uid or ("uid:" + title), title=title, content_type=kind,
            content_text=body, module_name="", source_url=""))
        nexora.db.session.commit()


def profile(user_id, course, source_uid, title=""):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user_id, course_full=course,
            source_uid=source_uid, outline_title=title, grading_json="{}"))
        nexora.db.session.commit()


def mark(user_id, course, name, score, weight=30, max_score=100):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.GradeEntry(
            id=uuid4().hex, user_id=user_id, course_full=course, name=name,
            weight=weight, score=score, max_score=max_score))
        nexora.db.session.commit()


def marks(user_id, course):
    with nexora.app.app_context():
        return [(g.name, g.score) for g in nexora.GradeEntry.query.filter_by(
            user_id=user_id, course_full=course).all()]


# --------------------------------------------------------------------------
# the signal
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Course Outline",
    "ECOR 1032 Course Outline",
    "Syllabus",
    "Course Syllabus Winter 2026",
    "Course Information",
    "Green Sheet",
    "Grading Scheme",
    "BIOL 1103 Course Policies",
])
def test_a_title_that_names_the_outline_is_trusted_on_its_own(title):
    """No body at all. A strong title is the document saying what it is, and a
    row whose text Brightspace has not handed over yet still has to be
    offerable -- that is the normal first state."""
    score, confident, _why = nexora._outline_candidate_score(Row(title))
    assert score > 0
    assert confident is True


@pytest.mark.parametrize("title", [
    "ELEC 2501 Outline.pdf",
    "Outline",
    "First Day Handout",
    "About this course",
])
def test_a_title_that_only_suggests_the_outline_needs_the_contents_to_agree(title):
    """"Essay outline template" is a real course file. The word alone cannot be
    enough, so these are confident only once the body proves it."""
    bare = nexora._outline_candidate_score(Row(title))
    with_body = nexora._outline_candidate_score(Row(title, body=GRADING_BODY))
    assert bare[0] > 0, "still worth offering"
    assert bare[1] is False, "but not worth reading unasked"
    assert with_body[1] is True
    assert with_body[0] > bare[0]


@pytest.mark.parametrize("title", [
    "Essay outline template",
    "Assignment 1: outline of your term paper",
    "Outline quiz",
    "Lab 3 manual",
])
def test_a_document_about_one_assessment_is_never_the_outline(title):
    score, confident, _why = nexora._outline_candidate_score(Row(title, body=LAB_BODY))
    assert score == 0
    assert confident is False


@pytest.mark.parametrize("title", [
    "01 SLIDES", "Lecture 4", "Piazza", "Welcome!", "Grades FAQ",
    "Thermodynamics.pptx", "Module 3 Discussion Board", "data.csv",
    "Lecture 3 Recording.mp4", "Java Install Guide",
])
def test_ordinary_course_material_is_not_a_candidate(title):
    """Titles from a real account. The cost of a false positive is an analysis
    of the wrong document, so the ordinary contents of a course score nothing."""
    assert nexora._outline_candidate_score(Row(title, kind="lecture"))[0] == 0


def test_the_academic_integrity_page_quotes_the_outline_and_is_not_it():
    """Body hints alone must never qualify a row. The integrity page, the
    office-hours page and the deferred-exam policy all read like an outline
    because they quote one."""
    body = ("Academic Integrity Policy. Plagiarism is a serious offence. "
            "Office hours are listed on the course outline. Instructor "
            "approval is required. See the undergraduate calendar. "
            "Prerequisite rules are unaffected.")
    assert nexora._outline_candidate_score(Row("Academic Integrity Policy", body=body))[0] == 0


def test_an_addendum_is_a_candidate_but_never_the_leading_one():
    """It is an addition TO the outline -- the amendment route exists for this
    text -- so losing it is wrong and leading with it is worse: the scheme
    would be built from the correction alone."""
    outline = nexora._outline_candidate_score(Row("Course Outline", body=GRADING_BODY))
    addendum = nexora._outline_candidate_score(
        Row("Course Outline Addendum", body=GRADING_BODY))
    assert addendum[0] > 0
    assert addendum[0] < outline[0]


# --------------------------------------------------------------------------
# the order, which is the half the page depends on
# --------------------------------------------------------------------------

def test_the_candidates_come_back_best_first(client, student):
    """The workspace analyses the FIRST candidate that has text. Left unordered
    that was whichever row the query happened to yield -- the
    `_newest_offering_per_code` bug one screen over."""
    content(student, "ELEC2501", "Week 2 reading outline", body=LAB_BODY)
    content(student, "ELEC2501", "ELEC 2501 Outline.pdf", kind="resource",
            body=GRADING_BODY)
    content(student, "ELEC2501", "ELEC 2501 Course Outline", kind="outline",
            body=GRADING_BODY)

    with nexora.app.app_context():
        picks = nexora._outline_candidates(student, "ELEC2501")
        titles = [c["row"].title for c in picks]
        row, confident = nexora._best_outline_row(student, "ELEC2501")
        assert titles[0] == "ELEC 2501 Course Outline"
        assert row.title == "ELEC 2501 Course Outline"
        assert confident is True


def test_a_row_with_no_text_yet_is_still_offered(client, student):
    """A detected outline Brightspace has not handed over is the normal first
    state. The analyze route answers `needs_content` for it and the extension
    fetches it, so handing back nothing would stall that."""
    content(student, "CHEM1101", "CHEM 1101 Course Outline", kind="outline", body="")
    with nexora.app.app_context():
        row, _confident = nexora._best_outline_row(student, "CHEM1101")
        assert row is not None
        assert row.title == "CHEM 1101 Course Outline"


def test_a_course_with_no_outline_anywhere_answers_nothing(client, student):
    content(student, "MATH1004", "Lecture 1", kind="lecture",
            body="Limits and continuity. Nothing about marks here.")
    with nexora.app.app_context():
        assert nexora._outline_candidates(student, "MATH1004") == []
        assert nexora._best_outline_row(student, "MATH1004") == (None, False)


# --------------------------------------------------------------------------
# one resolver, so the document read is the document analysed
# --------------------------------------------------------------------------

def test_the_row_the_profile_was_built_from_wins(client, student):
    """`profile.source_uid` records a DECISION -- the student picked that file,
    or confirmed the analysis of it -- and a decision outranks a guess."""
    content(student, "SYSC2004", "Course Outline", kind="outline", body=GRADING_BODY)
    content(student, "SYSC2004", "Syllabus (corrected)", kind="resource",
            body=GRADING_BODY, uid="uid:picked")
    profile(student, "SYSC2004", "uid:picked", "Syllabus (corrected)")

    with nexora.app.app_context():
        assert nexora._outline_row_for(student, "SYSC2004").source_uid == "uid:picked"


def test_a_profile_pointing_at_a_row_that_is_gone_falls_back(client, student):
    """`_import_content` deletes a course's rows and re-imports them, so a
    recorded uid can stop existing. Returning None there would silently empty
    Ask-the-outline."""
    content(student, "SYSC2004", "Course Outline", kind="outline", body=GRADING_BODY)
    profile(student, "SYSC2004", "uid:deleted-by-a-sync", "gone")

    with nexora.app.app_context():
        row = nexora._outline_row_for(student, "SYSC2004")
        assert row is not None
        assert row.title == "Course Outline"


def test_ask_the_outline_reads_a_document_the_extension_never_typed(client, student):
    """The failure this fixes. The outline is a `resource` as far as the
    extension is concerned, so Ask found no text and answered from the saved
    numbers with no document behind it -- which is exactly what
    `_verify_quote_in_source` exists to prevent."""
    content(student, "ELEC2501", "ELEC 2501 Outline.pdf", kind="resource",
            body=GRADING_BODY)
    with nexora.app.app_context():
        row = nexora._outline_row_for(student, "ELEC2501")
        assert row is not None
        assert "Final Examination 50%" in row.content_text


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------

def test_the_route_reads_the_best_candidate_and_names_it(client, student):
    """A scheme read out of the wrong file looks exactly as authoritative as
    one read out of the right file, so the answer has to say which it was."""
    content(student, "ELEC2501", "ELEC 2501 Outline.pdf", kind="resource",
            body=GRADING_BODY)

    res = client.post("/api/course/ELEC2501/outline/analyze", json={})
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["source_guessed"] is True
    assert body["source_title"] == "ELEC 2501 Outline.pdf"


def test_a_document_the_student_named_is_never_second_guessed(client, student):
    content(student, "ELEC2501", "Course Outline", kind="outline", body=GRADING_BODY)
    content(student, "ELEC2501", "Syllabus", kind="resource", body=GRADING_BODY,
            uid="uid:chosen")

    res = client.post("/api/course/ELEC2501/outline/analyze",
                      json={"source_uid": "uid:chosen"})
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["source_guessed"] is False
    assert body["source_title"] == "Syllabus"


def test_the_404_says_what_to_do_instead_of_describing_nexoras_state(client, student):
    content(student, "MATH1004", "Lecture 1", kind="lecture", body="Limits.")
    res = client.post("/api/course/MATH1004/outline/analyze", json={})
    assert res.status_code == 404
    message = res.get_json()["error"].lower()
    assert "upload" in message or "paste" in message


# --------------------------------------------------------------------------
# never overwrite a score the student entered manually without asking
# --------------------------------------------------------------------------

def test_an_unasked_analysis_does_not_delete_entered_marks(client, student):
    """The route's last act is to delete every GradeEntry for the course --
    right for an analysis someone PRESSED, and not something to do unprompted
    to marks that exist nowhere else. Measured before the guard: one
    hand-entered Midterm 27/30 was removed."""
    content(student, "ELEC2501", "Course Outline", kind="outline", body=GRADING_BODY)
    mark(student, "ELEC2501", "Midterm", 27, weight=30, max_score=30)

    res = client.post("/api/course/ELEC2501/outline/analyze", json={"auto": True})
    assert res.status_code == 409
    assert res.get_json()["skipped"] == "entered_marks"
    assert marks(student, "ELEC2501") == [("Midterm", 27.0)]


def test_an_analysis_the_student_asked_for_still_replaces_the_calculator(client, student):
    """The guard is about being UNASKED, not about the delete. A new outline
    replacing the old scheme is what pressing Analyze means."""
    content(student, "ELEC2501", "Course Outline", kind="outline", body=GRADING_BODY)
    mark(student, "ELEC2501", "Old guess", 5, weight=10, max_score=10)

    res = client.post("/api/course/ELEC2501/outline/analyze", json={})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert "Old guess" not in {name for name, _score in marks(student, "ELEC2501")}


def test_an_unasked_analysis_runs_when_there_is_nothing_to_lose(client, student):
    """An unscored row is the assessment list, not a mark. Refusing on those
    would stop a detected outline being read at all for every student who has
    one."""
    content(student, "ELEC2501", "Course Outline", kind="outline", body=GRADING_BODY)
    mark(student, "ELEC2501", "Midterm", None, weight=30)

    res = client.post("/api/course/ELEC2501/outline/analyze", json={"auto": True})
    assert res.status_code == 200, res.get_data(as_text=True)
