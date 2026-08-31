"""The grading-policy panel a student is asked to confirm.

Everything here is about that panel being *right*, because it is the one screen
in Nexora that asks someone to sign off on an interpretation of their own course
outline. A confident, specific, wrong statement there is worse than no statement:
they will believe it.
"""

import json
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="policy@carleton.ca", password="correct-horse-battery"):
    return client.post("/signup", data={"name": "Policy Student", "email": email,
                                        "password": password}, follow_redirects=False)


# An outline with two schemes that name their components directly, which is the
# ordinary shape and the one that used to fall through the matcher entirely.
TWO_SCHEMES = {
    "components": [
        {"name": "Midterm", "weight": 30, "category": "midterm"},
        {"name": "Final exam", "weight": 70, "category": "final"},
    ],
    "grading_options": [
        {"option_name": "Scheme 1", "components": [
            {"name": "Midterm", "weight": 30},
            {"name": "Final exam", "weight": 70},
        ]},
        {"option_name": "Scheme 2", "components": [
            {"name": "Midterm", "weight": 20},
            {"name": "Final exam", "weight": 60},
        ]},
    ],
    "pass_mark": 50,
}


@pytest.fixture()
def workspace(client):
    """A course whose outline has been analysed into two schemes."""
    import app as nexora
    signup(client)
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="policy@carleton.ca").first()
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full="SYSC2004",
            outline_title="SYSC 2004 outline",
            grading_json=json.dumps(TWO_SCHEMES), pass_mark=50))
        nexora.db.session.commit()
    return client


def test_a_weighted_component_is_never_called_unscored(workspace):
    """The midterm is worth 30% under one scheme and 20% under the other.

    Saying it is "not scored directly -- used for pass/fail check" tells a
    student their midterm does not count toward their mark, on the screen where
    they are being asked to confirm exactly that.
    """
    body = workspace.get("/course/SYSC2004?tab=grades").get_data(as_text=True)
    assert "Not scored directly" not in body


def test_the_range_across_schemes_is_shown(workspace):
    body = workspace.get("/course/SYSC2004?tab=grades").get_data(as_text=True)
    assert "20" in body and "30" in body
    # the label is a range because the two schemes disagree
    assert "by option" in body


def test_an_unmatched_component_admits_it_does_not_know(client):
    """Not finding a component in any scheme means the weight is unknown, which
    is a different claim from knowing it has none."""
    import app as nexora
    signup(client, email="unknown@carleton.ca")
    analysis = {
        "components": [{"name": "Participation", "weight": 0}],
        "grading_options": [{"option_name": "Scheme 1", "components": [
            {"name": "Final exam", "weight": 100}]}],
    }
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="unknown@carleton.ca").first()
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full="SYSC2004",
            grading_json=json.dumps(analysis), pass_mark=50))
        nexora.db.session.commit()

    body = client.get("/course/SYSC2004?tab=grades").get_data(as_text=True)
    assert "Weight not detected" in body
    assert "Not scored directly" not in body


def test_the_student_is_not_asked_to_set_an_environment_variable(client, monkeypatch):
    """Whichever model ran is the operator's problem. "Needs your review" is a
    list of facts about the student's own outline."""
    import app as nexora
    assert "GEMINI_REASONING_MODEL" not in io_read("templates/course_workspace.html")

    # and the notice the student does get has to be something they can act on
    source = io_read("app.py")
    start = source.index('if models_used and not any("pro"')
    window = source[start:start + 1400]
    assert "GEMINI_REASONING_MODEL" in window, "the operator detail should still be logged"
    assert "app.logger.warning" in window, "it belongs in the log"
    student_line = window[window.index("ambiguities.append"):]
    assert "GEMINI_REASONING_MODEL" not in student_line
    assert "worth checking more carefully" in student_line


def io_read(path):
    import io
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return io.open(os.path.join(root, path), encoding="utf-8").read()
