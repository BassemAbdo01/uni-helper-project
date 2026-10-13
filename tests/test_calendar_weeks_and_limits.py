"""Three things reported in one sitting, all of them "it looks broken".

1. "i cant even add manually now" -- it added perfectly every time. The
   exams-only filter survives a reload in sessionStorage, saving reloads the
   month, and anything that is not an exam came back `display:none`. On the
   server and in the DOM and invisible.
2. A schedule printed by WEEK ("Week of 19 October") plus "my day is Tuesday"
   was turned into a date BY THE MODEL, which answered 2026-10-21 (a Wednesday)
   from a table and 2026-10-20 (correct) from the same facts as prose.
3. The AI allowance ran out after twelve attempts at a feature that was not
   working, and said so as "12 per 1 hour This limit protects the shared AI
   quota every student's outline analysis depends on."
"""
import io
import os
import re
from datetime import date
from uuid import uuid4

import app as nexora

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _signup(client):
    return client.post("/signup", data={"name": "S", "email": f"wk{uuid4().hex[:7]}@carleton.ca",
                                        "password": "nexorademo1"})


def _post(client, rows, instruction="", understood=""):
    data = {"image": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 40), "s.png", "image/png")}
    if instruction:
        data["instruction"] = instruction
    return client.post("/api/calendar/from-image", data=data,
                       content_type="multipart/form-data")


def _fake(monkeypatch, rows, understood=""):
    monkeypatch.setattr(nexora, "_gemini_image_to_outline_text",
                        lambda *a, **k: "Week | Activity\nWeek of 19 October | Test 1")
    monkeypatch.setattr(nexora, "_gemini_json",
                        lambda *a, **k: {"understood": understood, "rows": rows})


# --------------------------------------------------------------------------
# A week is not a day, and working out which day is arithmetic
# --------------------------------------------------------------------------

def test_the_weekday_inside_a_stated_week():
    """19 October 2026 is a Monday, so the Tuesday of that week is the 20th.
    The model said the 21st -- a Wednesday -- and said it confidently."""
    assert nexora._day_in_stated_week(date(2026, 10, 19), "Tuesday") == date(2026, 10, 20)
    assert nexora._day_in_stated_week(date(2026, 10, 19), "Monday") == date(2026, 10, 19)
    assert nexora._day_in_stated_week(date(2026, 10, 19), "Friday") == date(2026, 10, 23)


def test_the_week_is_anchored_on_its_monday_however_it_was_printed():
    """"Week of Sunday 18 October" and "Week of Monday 19 October" are the same
    week to the student reading the schedule, so they must resolve alike."""
    sunday, monday = date(2026, 10, 18), date(2026, 10, 19)
    assert nexora._day_in_stated_week(monday, "Tuesday") == date(2026, 10, 20)
    # the Sunday BEFORE that Monday belongs to the previous ISO week
    assert nexora._day_in_stated_week(sunday, "Tuesday") == date(2026, 10, 13)


def test_short_weekday_names_are_understood():
    assert nexora._day_in_stated_week(date(2026, 10, 19), "tue") == date(2026, 10, 20)
    assert nexora._day_in_stated_week(date(2026, 10, 19), "  THURS ") == date(2026, 10, 22)


def test_no_weekday_means_no_guess():
    """With nothing said about which day, the stated date stands. Picking a
    weekday for the student is exactly the invention this removes."""
    assert nexora._day_in_stated_week(date(2026, 10, 19), "") is None
    assert nexora._day_in_stated_week(date(2026, 10, 19), "someday") is None
    assert nexora._day_in_stated_week(None, "Tuesday") is None


def test_the_route_converts_a_week_row(client, monkeypatch):
    _signup(client)
    _fake(monkeypatch, [{"title": "Test 1", "kind": "exam", "date": "2026-10-19",
                         "year_printed": True, "names_a_week": True, "weekday": "Tuesday"}])
    (change,) = _post(client, None).get_json()["changes"]
    assert change["date"] == "2026-10-20"


def test_a_row_naming_a_real_day_is_untouched(client, monkeypatch):
    """Only a row that SAYS it names a week gets moved; a dated row must not be
    dragged onto some weekday."""
    _signup(client)
    _fake(monkeypatch, [{"title": "Test 1", "kind": "exam", "date": "2026-10-21",
                         "year_printed": True, "names_a_week": False, "weekday": "Tuesday"}])
    (change,) = _post(client, None).get_json()["changes"]
    assert change["date"] == "2026-10-21"


def test_the_year_is_settled_before_the_weekday(client, monkeypatch):
    """A week is usually printed without a year, so anchoring has to happen
    first or the weekday is counted inside the wrong week."""
    _signup(client)
    _fake(monkeypatch, [{"title": "Test 1", "kind": "exam", "date": "1999-10-19",
                         "year_printed": False, "names_a_week": True, "weekday": "Tuesday"}])
    (change,) = _post(client, None).get_json()["changes"]
    assert not change["date"].startswith("1999")
    year, month, day = (int(p) for p in change["date"].split("-"))
    assert date(year, month, day).strftime("%A") == "Tuesday"


# --------------------------------------------------------------------------
# The allowance
# --------------------------------------------------------------------------

def test_every_ai_route_is_covered_by_one_decorator():
    """Fifteen routes carried @limiter.limit(AI_LIMIT) by hand. They go through
     now so the rule has one definition -- and it deliberately charges
    for failures too: a vision call that reads an image and then answers 422 has
    already spent the quota this limit protects. Exempting failures was tried
    and backed out."""
    src = io.open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
    assert src.count("@ai_limit") >= 15
    assert "@limiter.limit(AI_LIMIT)" not in src
    assert "deduct_when" not in src


def test_the_allowance_is_tunable_without_a_deploy():
    assert "AI_LIMIT" in io.open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
    assert "per hour" in nexora.AI_LIMIT


def test_the_limit_is_not_the_old_one_that_locked_him_out():
    per_hour = int(re.search(r"(\d+)\s+per\s+hour", nexora.AI_LIMIT).group(1))
    assert per_hour >= 30, nexora.AI_LIMIT


def test_the_rate_limit_message_is_a_sentence_not_a_rule():
    """It read "12 per 1 hour This limit protects the shared AI quota every
    student's outline analysis depends on." -- which looks like a stack trace."""
    with nexora.app.test_request_context("/api/calendar/ai-events"):
        body = nexora.handle_rate_limited(
            type("E", (), {"description": "12 per 1 hour"})())
        text = (body[0].get_json() if isinstance(body, tuple) else body.get_json())
        said = " ".join(str(v) for v in text.values())
    assert "per 1 hour" not in said
    assert "12 per hour" in said
    assert said[0].isupper() and said.rstrip().endswith(".")


def test_an_unparsable_rule_still_reads_as_english():
    with nexora.app.test_request_context("/api/calendar/ai-events"):
        body = nexora.handle_rate_limited(type("E", (), {"description": ""})())
        text = (body[0].get_json() if isinstance(body, tuple) else body.get_json())
        said = " ".join(str(v) for v in text.values())
    assert "more requests than Nexora allows" in said


# --------------------------------------------------------------------------
# A filter must not swallow what was just added
# --------------------------------------------------------------------------

def _calendar_js():
    with io.open(os.path.join(HERE, "templates", "calendar.html"), encoding="utf-8") as fh:
        return fh.read()


def test_saving_a_non_exam_clears_the_exams_only_filter():
    """The data was always fine. The student could not see it, which is the
    same thing from where they are sitting."""
    js = _calendar_js()
    assert "unhideForKinds" in js
    assert "unhideForKinds([body.kind])" in js, "the manual save must call it"


def test_the_ai_apply_clears_it_too():
    """A schedule import adds Labs as `event`, so half of what the preview
    promised would come back invisible."""
    js = _calendar_js()
    assert re.search(r"unhideForKinds\(chosen\.map\(c => c\.kind\)", js)


def test_it_only_clears_the_filter_when_something_would_be_hidden():
    """The filter is a deliberate choice; clearing it for a row it would have
    shown anyway is Nexora overriding the student for nothing."""
    js = _calendar_js()
    body = re.search(r"const unhideForKinds = kinds => \{(.*?)\n  \};", js, re.S)
    assert body, "unhideForKinds not found"
    assert "every(k => k === 'exam')" in body.group(1)


def test_clearing_it_is_explained_rather_than_silent():
    js = _calendar_js()
    assert "nexora-filter-cleared" in js
    assert "switched off to show it" in js


def test_the_explanation_is_not_lost_when_the_toast_cannot_run_yet():
    """showToast is hoisted but its body touches `let undo`, which is in the
    temporal dead zone early in the script -- the first version threw there,
    the catch swallowed it, and the note had already been removed."""
    js = _calendar_js()
    block = re.search(r"setTimeout\(\(\) => \{(.*?)\}, 0\);", js, re.S)
    assert block, "the deferred note block is gone"
    inner = block.group(1)
    assert inner.index("showToast(note)") < inner.index("removeItem")
