"""Biweekly labs.

`ClassBlock` was a weekly repeat with no way to say "every other week", and a
student with a biweekly lab was wrong whichever way they entered it: as a
weekly class it blocked three hours of study time on every off week, and left
out entirely the planner scheduled revision straight over the lab.

The anchor is a DATE the class falls on, not "odd or even weeks" -- whose
numbering, ISO, the term's, the department's? A student can always answer when
their next lab is.
"""

import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"


class Block:
    """The fields _block_occurs_on reads."""

    def __init__(self, weekday, repeat="weekly", anchor_date="",
                 start_min=9 * 60, end_min=12 * 60):
        self.weekday, self.repeat, self.anchor_date = weekday, repeat, anchor_date
        self.start_min, self.end_min = start_min, end_min


# A Thursday, and the Thursdays either side of it.
THU = date(2026, 9, 10)


def test_a_weekly_class_happens_every_week():
    import app as nexora
    block = Block(THU.weekday())
    for weeks in range(6):
        assert nexora._block_occurs_on(block, THU + timedelta(weeks=weeks))


def test_a_biweekly_class_happens_on_alternating_weeks():
    import app as nexora
    block = Block(THU.weekday(), repeat="biweekly", anchor_date=THU.isoformat())
    got = [nexora._block_occurs_on(block, THU + timedelta(weeks=w)) for w in range(6)]
    assert got == [True, False, True, False, True, False]


def test_it_alternates_backwards_too():
    """A student may anchor on their next lab while the plan covers this week."""
    import app as nexora
    block = Block(THU.weekday(), repeat="biweekly",
                  anchor_date=(THU + timedelta(weeks=1)).isoformat())
    assert nexora._block_occurs_on(block, THU) is False
    assert nexora._block_occurs_on(block, THU + timedelta(weeks=1)) is True
    assert nexora._block_occurs_on(block, THU - timedelta(weeks=1)) is True


def test_the_anchor_need_not_be_the_same_weekday():
    """Weeks are compared Monday-to-Monday, so answering with the Monday of the
    week works as well as answering with the lab's own day."""
    import app as nexora
    monday_of_that_week = THU - timedelta(days=THU.weekday())
    block = Block(THU.weekday(), repeat="biweekly",
                  anchor_date=monday_of_that_week.isoformat())
    assert nexora._block_occurs_on(block, THU) is True
    assert nexora._block_occurs_on(block, THU + timedelta(weeks=1)) is False


def test_a_biweekly_class_is_still_not_on_other_weekdays():
    import app as nexora
    block = Block(THU.weekday(), repeat="biweekly", anchor_date=THU.isoformat())
    assert nexora._block_occurs_on(block, THU + timedelta(days=1)) is False


def test_a_biweekly_block_with_no_anchor_is_assumed_to_run():
    """Losing a study slot costs an hour. Scheduling revision over a lab the
    student then misses costs the lab."""
    import app as nexora
    block = Block(THU.weekday(), repeat="biweekly", anchor_date="")
    for weeks in range(4):
        assert nexora._block_occurs_on(block, THU + timedelta(weeks=weeks))


def test_an_unreadable_anchor_is_treated_the_same_way():
    import app as nexora
    block = Block(THU.weekday(), repeat="biweekly", anchor_date="not a date")
    assert nexora._block_occurs_on(block, THU) is True


# ------------------------------------------- what the planner does with it

@pytest.fixture()
def student(client):
    client.post("/signup", data={"name": "S", "email": "bi@carleton.ca",
                                 "password": PASSWORD})
    import app as nexora
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email="bi@carleton.ca").first().id


def add_block(user_id, **kwargs):
    import app as nexora
    with nexora.app.app_context():
        nexora.db.session.add(nexora.ClassBlock(
            id=uuid4().hex, user_id=user_id, label="BIOL1103 Lab",
            kind="lab", course_full="BIOL1103", **kwargs))
        nexora.db.session.commit()


def free_on(user_id, day):
    """Minutes of study time the planner believes it has on one date."""
    import app as nexora
    with nexora.app.app_context():
        pref = nexora._study_preference(user_id)
        slots = nexora._free_slots(user_id, pref, day, 1)
        return sum(finish - begin for _d, begin, finish in slots)


def test_the_off_week_is_free_time_again(student):
    """The whole point. A weekly-only model charged the student three hours
    every week for a lab that runs fortnightly."""
    add_block(student, weekday=THU.weekday(), start_min=9 * 60, end_min=12 * 60,
              repeat="biweekly", anchor_date=THU.isoformat())
    on_week = free_on(student, THU)
    off_week = free_on(student, THU + timedelta(weeks=1))
    assert off_week > on_week, "the off week is not being given back"
    assert off_week - on_week >= 3 * 60, "the lab's hours were not returned"


def test_the_lab_is_still_protected_on_the_weeks_it_runs(student):
    """And the other half: nothing may be scheduled over it."""
    import app as nexora
    add_block(student, weekday=THU.weekday(), start_min=9 * 60, end_min=12 * 60,
              repeat="biweekly", anchor_date=THU.isoformat())
    with nexora.app.app_context():
        pref = nexora._study_preference(student)
        slots = nexora._free_slots(student, pref, THU, 1)
    for _day, begin, finish in slots:
        assert finish <= 9 * 60 or begin >= 12 * 60, \
            f"a study slot {begin}-{finish} overlaps the lab"


def test_a_weekly_lab_still_blocks_every_week(student):
    add_block(student, weekday=THU.weekday(), start_min=9 * 60, end_min=12 * 60,
              repeat="weekly")
    assert free_on(student, THU) == free_on(student, THU + timedelta(weeks=1))


# ------------------------------------------------------- saving one

def test_the_editor_can_save_a_biweekly_class(client, student):
    res = client.post("/api/timetable", json={
        "label": "BIOL1103 Lab", "weekday": "thursday", "start": "09:00",
        "end": "12:00", "kind": "lab", "repeat": "biweekly",
        "anchor_date": THU.isoformat()})
    assert res.status_code == 201
    block = res.get_json()["block"]
    assert block["repeat"] == "biweekly"
    assert block["anchor_date"] == THU.isoformat()


def test_an_unspecified_repeat_stays_weekly(client, student):
    res = client.post("/api/timetable", json={
        "label": "BIOL1103 Lecture", "weekday": "monday", "start": "09:00",
        "end": "10:00", "kind": "lecture"})
    assert res.get_json()["block"]["repeat"] == "weekly"


@pytest.mark.parametrize("word", ["biweekly", "fortnightly", "alternate",
                                  "every_other_week", "BIWEEKLY"])
def test_the_words_a_timetable_actually_uses_are_accepted(client, student, word):
    res = client.post("/api/timetable", json={
        "label": "Lab", "weekday": "friday", "start": "14:00", "end": "17:00",
        "kind": "lab", "repeat": word})
    assert res.get_json()["block"]["repeat"] == "biweekly"


def test_a_nonsense_anchor_is_dropped_rather_than_stored(client, student):
    res = client.post("/api/timetable", json={
        "label": "Lab", "weekday": "friday", "start": "14:00", "end": "17:00",
        "kind": "lab", "repeat": "biweekly", "anchor_date": "next tuesday"})
    assert res.get_json()["block"]["anchor_date"] == ""


def test_the_page_offers_the_choice(client, student):
    body = client.get("/study-plan").get_data(as_text=True)
    assert "Every other week" in body
    assert 'id="cAnchor"' in body


def test_the_timetable_reader_is_told_to_look_for_it():
    """A timetable usually says so in a word, and the planner blocks the wrong
    weeks if the reader ignores it."""
    import inspect
    import app as nexora
    source = inspect.getsource(nexora.timetable_analyze)
    assert "biweekly" in source
    assert "alternating" in source


# ------------------------- how Nexora learns WHICH week

def test_the_page_asks_which_week_for_a_biweekly_class(client, student):
    """A timetable says a class is biweekly; it almost never says which week
    is which. Guessing inverts the alternation and puts every study block on
    the weeks the student is actually in the lab."""
    body = client.get("/study-plan").get_data(as_text=True)
    assert "When is the next one?" in body
    assert 'id="anchorRows"' in body
    assert "renderAnchorAsk" in body


def test_it_says_plainly_what_happens_with_no_date(client, student):
    body = client.get("/study-plan").get_data(as_text=True)
    assert "keep that time clear every week" in body


def test_the_manual_editor_does_not_prefill_a_guess(client, student):
    """It used to default to today, which is exactly the guess that inverts
    the schedule."""
    body = client.get("/study-plan").get_data(as_text=True)
    assert "toISOString().slice(0, 10)" not in body


def test_an_undated_biweekly_class_is_labelled_as_such(client, student):
    """So the student can see Nexora does not know yet."""
    body = client.get("/study-plan").get_data(as_text=True)
    assert "every other week, date not set" in body


def test_the_analyzer_carries_the_flag_but_not_a_made_up_date(client, student, monkeypatch):
    import app as nexora
    monkeypatch.setattr(nexora, "_gemini_json", lambda *a, **k: {"blocks": [
        {"label": "BIOL Lab", "weekday": "thursday", "start": "09:00",
         "end": "12:00", "kind": "lab", "repeat": "biweekly"}]})
    res = client.post("/api/timetable/analyze", json={
        "text": "Thu 09:00-12:00 BIOL Lab (alternating weeks) Nesbitt 132"})
    block = res.get_json()["blocks"][0]
    assert block["repeat"] == "biweekly"
    assert block["anchor_date"] == "", "the reader invented a start week"
