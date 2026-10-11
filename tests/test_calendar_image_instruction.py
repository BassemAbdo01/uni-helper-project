"""A picture plus a sentence: the sentence is half the request.

Reported with a weekly lab/tutorial schedule and this instruction: "for
everything thats written as tutorial here, treat it as Test, for example
Tutorial 2 would be Test 2 for elec". Nothing on that table says "Test", and
nothing on it says which course it belongs to -- so an image reader with no
instruction can only ever hand back what the student is already looking at.

The model decides what each row MEANS; the server still decides every date, and
nothing is written until the preview is confirmed.
"""
import io
from datetime import date
from uuid import uuid4

import app as nexora


def _signup(client):
    return client.post("/signup", data={"name": "S", "email": f"ins{uuid4().hex[:7]}@carleton.ca",
                                        "password": "nexorademo1"})


def _post(client, instruction=None):
    data = {"image": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 40), "s.png", "image/png")}
    if instruction is not None:
        data["instruction"] = instruction
    return client.post("/api/calendar/from-image", data=data,
                       content_type="multipart/form-data")


def _fake_gemini(monkeypatch, rows, understood="", capture=None):
    """Stand in for both passes. `capture` collects the prompt so a test can
    assert the instruction actually reached the model rather than being read
    off the form and dropped."""
    monkeypatch.setattr(nexora, "_gemini_image_to_outline_text",
                        lambda *a, **k: "Week | Activity | Note\n21-Sep | Tutorial (1) |")

    def fake_json(system, prompt, **kwargs):
        if capture is not None:
            capture.append(prompt)
        return {"understood": understood, "rows": rows}
    monkeypatch.setattr(nexora, "_gemini_json", fake_json)


# --------------------------------------------------------------------------
# The year. A course schedule is written for people who already know which term
# it is, so it prints "07-Sep" and nothing else -- and the year is then the one
# thing on the row nobody wrote down. This is the mistake that got the old
# "exams found in your course material" panel deleted: it offered a first-year
# MATH 1004 test as "Tue, Sep 29" this September.
# --------------------------------------------------------------------------

def test_a_row_with_no_year_takes_the_nearest_one_not_the_models_guess():
    today = date(2026, 10, 11)
    assert nexora._anchor_undated_year(9, 7, today) == date(2026, 9, 7)    # just gone
    assert nexora._anchor_undated_year(12, 7, today) == date(2026, 12, 7)  # ahead


def test_it_reaches_backwards_and_forwards_across_a_new_year():
    """A Winter schedule read in February runs Dec -> Apr, so the nearest
    occurrence has to be able to land in the year before as well as after."""
    today = date(2026, 2, 3)
    assert nexora._anchor_undated_year(12, 7, today) == date(2025, 12, 7)
    assert nexora._anchor_undated_year(4, 7, today) == date(2026, 4, 7)


def test_a_leap_day_it_cannot_reach_is_answered_with_nothing_not_a_crash():
    """Nearest-occurrence looks at last year, this year and next year only, and
    none of 2025/2026/2027 has a 29 February. None is the honest answer: the
    caller then keeps the date the model gave rather than moving the row onto a
    day that does not exist, or reaching two years out for it."""
    assert nexora._anchor_undated_year(2, 29, date(2026, 6, 1)) is None
    assert nexora._anchor_undated_year(2, 29, date(2027, 6, 1)) == date(2028, 2, 29)


def test_a_date_it_cannot_anchor_keeps_the_one_the_model_gave(client, monkeypatch):
    """Failing to anchor must never blank the row -- an entry the student can
    see in their picture disappearing is worse than a year that may be off."""
    _signup(client)
    _fake_gemini(monkeypatch, [{"title": "Leap test", "kind": "exam",
                                "date": "2028-02-29", "year_printed": False}])
    (change,) = _post(client).get_json()["changes"]
    assert change["date"] == "2028-02-29"


def test_the_server_overrides_a_year_the_row_never_printed(client, monkeypatch):
    """The model is asked for its best date AND whether a year was printed. When
    it was not, whatever year it chose is discarded."""
    _signup(client)
    _fake_gemini(monkeypatch, [{"title": "Test 2", "kind": "exam", "date": "2019-10-19",
                                "year_printed": False, "course": "SYSC2310"}])
    res = _post(client, "treat every tutorial as a test")
    assert res.status_code == 200
    (change,) = res.get_json()["changes"]
    assert change["date"].endswith("-10-19")
    assert not change["date"].startswith("2019"), "the model's year survived"


def test_a_printed_year_is_left_exactly_as_it_is(client, monkeypatch):
    """A timetable that DOES state its year is the authority on it, and
    re-anchoring would move a legitimately distant date."""
    _signup(client)
    _fake_gemini(monkeypatch, [{"title": "Final", "kind": "exam", "date": "2027-04-20",
                                "year_printed": True, "course": "SYSC2310"}])
    (change,) = _post(client).get_json()["changes"]
    assert change["date"] == "2027-04-20"


# --------------------------------------------------------------------------
# The instruction
# --------------------------------------------------------------------------

def test_the_instruction_is_sent_to_the_model(client, monkeypatch):
    """It is read off the form; the test that matters is that it arrives."""
    _signup(client)
    seen = []
    _fake_gemini(monkeypatch, [], capture=seen)
    _post(client, "treat every Tutorial as a Test for ELEC2501B")
    assert seen and "treat every Tutorial as a Test for ELEC2501B" in seen[0]
    assert "INSTRUCTION:" in seen[0]


def test_with_no_instruction_the_prompt_does_not_invent_one(client, monkeypatch):
    _signup(client)
    seen = []
    _fake_gemini(monkeypatch, [], capture=seen)
    _post(client)
    assert seen and "INSTRUCTION:" not in seen[0]


def test_an_over_long_instruction_is_cut_rather_than_refused(client, monkeypatch):
    """Refusing the upload over the sentence beside it would throw away the
    image too, and the student would have to find the file again."""
    _signup(client)
    seen = []
    _fake_gemini(monkeypatch, [], capture=seen)
    res = _post(client, "x" * 900)
    assert res.status_code == 200
    assert "x" * 500 in seen[0] and "x" * 501 not in seen[0]


def test_what_it_understood_comes_back_to_be_shown(client, monkeypatch):
    """A misread instruction silently reshapes a term, and would look entirely
    authoritative doing it -- the same reason /api/study-plan/ask returns
    `understood`."""
    _signup(client)
    _fake_gemini(monkeypatch, [], understood="Every Tutorial becomes a Test for SYSC2310.")
    assert _post(client, "tutorials are tests").get_json()["understood"] \
        == "Every Tutorial becomes a Test for SYSC2310."


# --------------------------------------------------------------------------
# What a row becomes
# --------------------------------------------------------------------------

def test_a_test_is_stored_as_an_exam_and_everything_else_as_an_event(client, monkeypatch):
    """EVENT_KINDS is due|exam|event, and a test is an exam -- the same mapping
    _recurrence_events uses. Two places disagreeing about what "test" means is
    how a date stops showing up in the exam filter."""
    _signup(client)
    _fake_gemini(monkeypatch, [
        {"title": "Test 2", "kind": "test", "date": "2026-10-19", "year_printed": True},
        {"title": "Lab 3", "kind": "lab", "date": "2026-11-02", "year_printed": True},
    ])
    kinds = {c["title"]: c["kind"] for c in _post(client).get_json()["changes"]}
    assert kinds == {"Test 2": "exam", "Lab 3": "event"}


def test_a_row_can_never_become_an_lms_deadline(client, monkeypatch):
    """`due` means a deadline an LMS gave us. This row came from a picture."""
    _signup(client)
    _fake_gemini(monkeypatch, [{"title": "Report", "kind": "due",
                                "date": "2026-11-02", "year_printed": True}])
    (change,) = _post(client).get_json()["changes"]
    assert change["kind"] != "due"


def test_a_row_the_instruction_excludes_is_dropped(client, monkeypatch):
    _signup(client)
    _fake_gemini(monkeypatch, [
        {"title": "Test 2", "kind": "exam", "date": "2026-10-19", "year_printed": True},
        {"title": "Lab 3", "kind": "event", "date": "2026-11-02", "year_printed": True,
         "skip": True},
    ])
    body = _post(client, "skip the labs").get_json()
    assert [c["title"] for c in body["changes"]] == ["Test 2"]
    assert any("scheduled nothing" in n for n in body["notes"])


# --------------------------------------------------------------------------
# The weeks that have already gone
# --------------------------------------------------------------------------

def test_a_date_that_has_passed_is_offered_but_marked(client, monkeypatch):
    """A term schedule always carries weeks that have happened. Dropping them
    silently is wrong -- the student can see them in their own picture -- so
    they are listed, flagged, and the sheet leaves them unticked."""
    _signup(client)
    _fake_gemini(monkeypatch, [
        {"title": "Test 1", "kind": "exam", "date": "2020-01-06", "year_printed": True},
        {"title": "Test 9", "kind": "exam", "date": "2099-01-06", "year_printed": True},
    ])
    body = _post(client).get_json()
    past = {c["title"]: c["past"] for c in body["changes"]}
    assert past == {"Test 1": True, "Test 9": False}
    assert any("already passed" in n for n in body["notes"])


def test_nothing_is_written_by_reading_an_image(client, monkeypatch):
    """Two calls, always: this one previews and the confirm applies."""
    _signup(client)
    _fake_gemini(monkeypatch, [{"title": "Test 2", "kind": "exam",
                                "date": "2026-10-19", "year_printed": True}])
    body = _post(client, "tutorials are tests").get_json()
    assert body["preview"] is True
    with nexora.app.app_context():
        user = nexora.User.query.order_by(nexora.User.id.desc()).first()
        assert nexora.Task.query.filter_by(user_id=user.id).count() == 0
