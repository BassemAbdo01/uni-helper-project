"""Part 2: a one-click grading scale.

Carleton's published table is A+ 90-100, A 85-89, A- 80-84, B+ 77-79, B 73-76,
B- 70-72, C+ 67-69, C 63-66, C- 60-62, D+ 57-59, D 53-56, D- 50-52, F 0-49 --
which is `DEFAULT_GRADING_SCALE` exactly. So the preset REUSES it rather than
restating it: two copies of one table is how a letter comes to differ between
two screens, and this table decides what a transcript says.

What the preset buys is therefore not the numbers. It is `configured`: an
unconfigured scale is Nexora guessing, a student cannot tell that from a saved
answer, and `grading_scale_configured` is surfaced for exactly that reason.
"""

import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture()
def student(client):
    email = f"gs{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": PASSWORD})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


def preset(preset_id):
    return next(p for p in nexora.GRADING_SCALE_PRESETS if p["id"] == preset_id)


def test_carleton_is_offered(student, client):
    page = client.get("/settings").get_data(as_text=True)
    assert 'data-preset="carleton"' in page
    assert "Carleton University" in page


def test_the_preset_is_the_default_scale_itself_not_a_copy(student):
    """Measured against Carleton's own published table. They agree, so there
    must be ONE table -- a second copy drifts, and the drift would be a letter
    grade."""
    assert preset("carleton")["scale"] is nexora.DEFAULT_GRADING_SCALE


@pytest.mark.parametrize("letter,minimum", [
    ("A+", 90.0), ("A", 85.0), ("A-", 80.0),
    ("B+", 77.0), ("B", 73.0), ("B-", 70.0),
    ("C+", 67.0), ("C", 63.0), ("C-", 60.0),
    ("D+", 57.0), ("D", 53.0), ("D-", 50.0), ("F", 0.0),
])
def test_every_carleton_band(letter, minimum):
    bands = {b["letter"]: b["minimum"] for b in preset("carleton")["scale"]}
    assert bands[letter] == minimum


def test_saving_it_makes_the_scale_configured(student, client):
    """The point of the preset. Before: Nexora is guessing and says so.
    After: it is the student's answer."""
    with nexora.app.app_context():
        assert nexora._get_user_grading_scale(student)["configured"] is False

    carleton = preset("carleton")
    res = client.post("/api/grading-scale",
                      json={"university_name": carleton["name"],
                            "scale": carleton["scale"]})
    assert res.status_code == 200, res.get_data(as_text=True)

    with nexora.app.app_context():
        saved = nexora._get_user_grading_scale(student)
    assert saved["configured"] is True
    assert saved["university_name"] == "Carleton University"
    assert saved["scale"] == nexora._normalize_grading_scale(nexora.DEFAULT_GRADING_SCALE)


def test_the_letters_it_produces_are_carletons(student, client):
    """What a student actually sees. 85 is an A at Carleton and 84 is an A-;
    a scale that is one point out changes a transcript letter."""
    carleton = preset("carleton")
    client.post("/api/grading-scale",
                json={"university_name": carleton["name"], "scale": carleton["scale"]})
    with nexora.app.app_context():
        letters = {g: nexora._numeric_to_letter(student, g)
                   for g in (90, 89, 85, 84, 80, 79, 77, 73, 70, 67, 63, 60, 57, 53, 50, 49)}
    assert letters == {90: "A+", 89: "A", 85: "A", 84: "A-", 80: "A-", 79: "B+",
                       77: "B+", 73: "B", 70: "B-", 67: "C+", 63: "C", 60: "C-",
                       57: "D+", 53: "D", 50: "D-", 49: "F"}


def test_a_preset_carries_every_field_the_page_renders(student):
    """The page builds its buttons from this list, so a preset missing a name
    or a scale renders a button that does nothing."""
    for item in nexora.GRADING_SCALE_PRESETS:
        assert item["id"] and item["name"] and item["note"]
        assert len(nexora._normalize_grading_scale(item["scale"])) >= 2
