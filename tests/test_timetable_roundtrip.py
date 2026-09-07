"""Upload a timetable, confirm it, and have the classes actually be there.

Reported from the live site: the page said "Saved. Building your plan..." over
a completely empty week. Two independent bugs, and between them the upload path
had **never worked end to end** -- only "Add a class by hand" did, because an
`<input type=time>` sends "09:00".

1. `/api/timetable/analyze` returns `start`/`end` as minutes from midnight, and
   the confirm step posts them straight back. `_parse_clock(695)` returned None,
   so every class was rejected on save.
2. `_parse_weekday(0)` returned None. `str(value or "")` turns Monday into an
   empty string, because 0 is falsy. Same trap made `_parse_clock(0)` reject
   midnight.

And the save reported `ok: true` while dropping everything, which is what let
this survive: the UI had no way to know it had failed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"

# What the model returns for a real timetable, in the shape the prompt asks for.
READ_BLOCKS = {"blocks": [
    {"label": "BIOL 1103 Lecture", "course": "BIOL1103", "weekday": "monday",
     "start": "11:35", "end": "12:55", "location": "Southam Hall", "kind": "lecture"},
    {"label": "BIOL 1103 Lab", "course": "BIOL1103", "weekday": "thursday",
     "start": "09:00", "end": "12:00", "location": "Nesbitt 132", "kind": "lab",
     "repeat": "biweekly"},
    {"label": "MATH 1104 Tutorial", "course": "MATH1104", "weekday": "friday",
     "start": "16:35", "end": "17:55", "location": "Tory 340", "kind": "tutorial"},
]}


@pytest.fixture()
def student(client, monkeypatch):
    import app as nexora
    monkeypatch.setattr(nexora, "_gemini_json", lambda *a, **k: dict(READ_BLOCKS))
    client.post("/signup", data={"name": "S", "email": "tt@carleton.ca",
                                 "password": PASSWORD})
    return client


def analyze(client):
    # The page posts a file; JSON text is the same route with no upload.
    return client.post("/api/timetable/analyze", json={
        "text": "Mon 11:35-12:55 BIOL 1103 Lecture Southam Hall. "
                "Thu 09:00-12:00 BIOL 1103 Lab Nesbitt 132. "
                "Fri 16:35-17:55 MATH 1104 Tutorial Tory 340."})


# --------------------------------------------------------- the round trip

def test_the_upload_produces_proposals(student):
    res = analyze(student)
    assert res.status_code == 200, res.get_data(as_text=True)
    blocks = res.get_json()["blocks"]
    assert len(blocks) == 3
    assert {b["label"] for b in blocks} == {
        "BIOL 1103 Lecture", "BIOL 1103 Lab", "MATH 1104 Tutorial"}


def test_confirming_them_actually_saves_them(student):
    """The whole bug. This posts back exactly what analyze returned, which is
    what the page does."""
    proposed = analyze(student).get_json()["blocks"]
    res = student.post("/api/timetable", json={"blocks": proposed, "replace": True})
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["added"] == 3, f"only {body['added']} of 3 classes saved"
    assert body["skipped"] == 0
    assert len(body["blocks"]) == 3


def test_a_monday_class_survives(student):
    """weekday 0 is falsy, and every Monday class was refused."""
    proposed = analyze(student).get_json()["blocks"]
    saved = student.post("/api/timetable",
                         json={"blocks": proposed, "replace": True}).get_json()["blocks"]
    monday = [b for b in saved if b["weekday"] == 0]
    assert monday, "the Monday class was dropped"
    assert monday[0]["start"] == 11 * 60 + 35


def test_the_times_survive_the_round_trip(student):
    proposed = analyze(student).get_json()["blocks"]
    saved = student.post("/api/timetable",
                         json={"blocks": proposed, "replace": True}).get_json()["blocks"]
    by_label = {b["label"]: b for b in saved}
    assert by_label["BIOL 1103 Lab"]["start"] == 9 * 60
    assert by_label["BIOL 1103 Lab"]["end"] == 12 * 60
    assert by_label["MATH 1104 Tutorial"]["start"] == 16 * 60 + 35


def test_a_biweekly_lab_survives_the_round_trip(student):
    proposed = analyze(student).get_json()["blocks"]
    saved = student.post("/api/timetable",
                         json={"blocks": proposed, "replace": True}).get_json()["blocks"]
    lab = next(b for b in saved if b["label"] == "BIOL 1103 Lab")
    assert lab["repeat"] == "biweekly"


def test_the_page_then_shows_the_week(student):
    proposed = analyze(student).get_json()["blocks"]
    student.post("/api/timetable", json={"blocks": proposed, "replace": True})
    body = student.get("/study-plan").get_data(as_text=True)
    assert "Nesbitt 132" in body
    assert "3 classes" in body or "BIOL 1103 Lab" in body


def test_a_plan_can_then_be_built(student):
    """"Add your timetable first" has to stop being the answer."""
    proposed = analyze(student).get_json()["blocks"]
    student.post("/api/timetable", json={"blocks": proposed, "replace": True})
    res = student.post("/api/study-plan/generate")
    assert res.status_code == 200, res.get_data(as_text=True)
    assert "timetable first" not in res.get_data(as_text=True).lower()


# ------------------------------------------- a failed save must say so

def test_a_save_that_saves_nothing_is_not_reported_as_success(student):
    """The UI said "Saved. Building your plan..." over an empty week for as long
    as this feature has existed, because the server returned ok:true."""
    res = student.post("/api/timetable", json={"blocks": [
        {"label": "Broken", "weekday": "someday", "start": "??", "end": "??"},
        {"label": "Also broken", "weekday": "nope", "start": "", "end": ""},
    ], "replace": True})
    assert res.status_code == 422
    body = res.get_json()
    assert body["skipped"] == 2
    assert "could be saved" in body["error"]


def test_a_partial_save_still_succeeds(student):
    """One unreadable row must not throw away the rest of the week."""
    res = student.post("/api/timetable", json={"blocks": [
        {"label": "Good", "weekday": "monday", "start": "09:00", "end": "10:00"},
        {"label": "Bad", "weekday": "someday", "start": "??", "end": "??"},
    ], "replace": True})
    assert res.status_code == 200
    assert res.get_json()["added"] == 1
    assert res.get_json()["skipped"] == 1


# ------------------------------------------------------- the parsers

@pytest.mark.parametrize("value,expected", [
    (0, 0), (540, 540), (695, 695), (1439, 1439),      # minutes, as our API emits
    (1440, None), (-1, None),
    ("09:00", 540), ("11:35", 695), ("4:35pm", 995), ("9.15", 555),
    ("540", None),   # ambiguous on purpose: 540 minutes, or 5:40?
    (None, None), ("", None), (True, None),
])
def test_what_counts_as_a_time(value, expected):
    import app as nexora
    assert nexora._parse_clock(value) == expected


@pytest.mark.parametrize("value,expected", [
    (0, 0), (3, 3), (6, 6), (7, None), (-1, None),
    ("0", 0), ("monday", 0), ("Mon", 0), ("thursday", 3),
    (None, None), ("", None), (True, None),
])
def test_what_counts_as_a_weekday(value, expected):
    import app as nexora
    assert nexora._parse_weekday(value) == expected
