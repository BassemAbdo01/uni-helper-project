"""Dates are the one thing a model must not be creative about.

`/api/calendar/ai-events` lets a student say "biweekly tests for SYSC2004 from
October 10, skipping reading week". The model turns that sentence into a
recurrence; `_expand_recurrence` turns the recurrence into dates. Everything
below tests the arithmetic, because that is what actually lands in someone's
calendar -- and because a plan has to be the same plan when you look at it twice.
"""
from datetime import date

import app as nexora


TODAY = date(2026, 10, 7)


def iso(days):
    return [d.isoformat() for d in days]


def test_biweekly_means_fourteen_days_apart():
    days, _ = nexora._expand_recurrence(
        {"start": "2026-10-10", "repeat": "biweekly", "count": 4}, TODAY)
    assert iso(days) == ["2026-10-10", "2026-10-24", "2026-11-07", "2026-11-21"]


def test_weekly_runs_to_an_end_date_and_stops():
    days, _ = nexora._expand_recurrence(
        {"start": "2026-10-08", "repeat": "weekly", "until": "2026-10-29"}, TODAY)
    assert iso(days) == ["2026-10-08", "2026-10-15", "2026-10-22", "2026-10-29"]


def test_a_cancelled_date_is_dropped_and_does_not_cost_an_occurrence():
    """"Six tests, that one is cancelled" means six tests, not five."""
    days, notes = nexora._expand_recurrence(
        {"start": "2026-10-10", "repeat": "biweekly", "count": 4, "on_excluded": "drop",
         "skip_ranges": [["2026-11-01", "2026-11-14"]]}, TODAY)
    assert "2026-11-07" not in iso(days)      # the one inside reading week
    assert len(days) == 4                     # still four
    assert iso(days) == ["2026-10-10", "2026-10-24", "2026-11-21", "2026-12-05"]
    assert any("dropped" in n for n in notes)


# --------------------------------------------------------------------------
# A holiday week POSTPONES a schedule; it does not delete a test. This was
# reported from the real thing: biweekly from Oct 13 with no university from
# Oct 23-28 produced Oct 13 then Nov 10 -- a 28-day hole, carrying on as though
# nothing had moved, when what was meant was "we continue after the break".
# --------------------------------------------------------------------------

def test_a_holiday_week_moves_the_test_instead_of_deleting_it():
    days, notes = nexora._expand_recurrence(
        {"start": "2026-10-13", "repeat": "biweekly", "count": 5,
         "skip_ranges": [["2026-10-23", "2026-10-28"]]}, TODAY)
    # Oct 27 is in the break, so it moves to the next Tuesday and the rest
    # follow from there -- not back onto the original grid.
    assert iso(days) == ["2026-10-13", "2026-11-03", "2026-11-17", "2026-12-01", "2026-12-15"]
    assert any("moved" in n for n in notes)


def test_shifting_keeps_the_weekday():
    """A Tuesday test must not become a Thursday one: the weekday is half of a
    class schedule and nobody restates it."""
    days, _ = nexora._expand_recurrence(
        {"start": "2026-10-13", "repeat": "biweekly", "count": 4,
         "skip_ranges": [["2026-10-23", "2026-10-28"]]}, TODAY)
    assert {d.weekday() for d in days} == {1}      # all Tuesdays


def test_shift_is_the_default_because_a_week_off_postpones_work():
    with_default, _ = nexora._expand_recurrence(
        {"start": "2026-10-13", "repeat": "biweekly", "count": 3,
         "skip_ranges": [["2026-10-23", "2026-10-28"]]}, TODAY)
    explicit, _ = nexora._expand_recurrence(
        {"start": "2026-10-13", "repeat": "biweekly", "count": 3, "on_excluded": "shift",
         "skip_ranges": [["2026-10-23", "2026-10-28"]]}, TODAY)
    assert with_default == explicit


def test_a_long_closure_pushes_past_the_whole_of_it():
    days, _ = nexora._expand_recurrence(
        {"start": "2026-12-08", "repeat": "biweekly", "count": 3,
         "skip_ranges": [["2026-12-18", "2027-01-10"]]}, TODAY)
    # Dec 22 is inside the winter closure; it lands on the first Tuesday after
    assert iso(days) == ["2026-12-08", "2027-01-12", "2027-01-26"]


def test_the_note_names_the_date_that_moved_and_where_it_went():
    _, notes = nexora._expand_recurrence(
        {"start": "2026-10-13", "repeat": "biweekly", "count": 3,
         "skip_ranges": [["2026-10-23", "2026-10-28"]]}, TODAY)
    joined = " ".join(notes)
    assert "2026-10-27" in joined and "2026-11-03" in joined


def test_a_single_excluded_day_is_dropped():
    days, _ = nexora._expand_recurrence(
        {"start": "2026-10-10", "repeat": "weekly", "count": 3,
         "skip_dates": ["2026-10-17"]}, TODAY)
    assert iso(days) == ["2026-10-10", "2026-10-24", "2026-10-31"]


def test_a_range_given_backwards_still_excludes():
    days, _ = nexora._expand_recurrence(
        {"start": "2026-10-10", "repeat": "weekly", "count": 2,
         "skip_ranges": [["2026-10-17", "2026-10-11"]]}, TODAY)
    assert "2026-10-17" not in iso(days)


def test_monthly_clamps_rather_than_rolling_into_the_next_month():
    # Jan 31 + 1 month is not March 3
    days, _ = nexora._expand_recurrence(
        {"start": "2027-01-31", "repeat": "monthly", "count": 3}, TODAY)
    assert iso(days) == ["2027-01-31", "2027-02-28", "2027-03-31"]


def test_february_in_a_leap_year():
    days, _ = nexora._expand_recurrence(
        {"start": "2028-01-31", "repeat": "monthly", "count": 2}, TODAY)
    assert iso(days) == ["2028-01-31", "2028-02-29"]


def test_once_produces_exactly_one_date():
    days, _ = nexora._expand_recurrence({"start": "2026-10-10", "repeat": "once"}, TODAY)
    assert iso(days) == ["2026-10-10"]


def test_no_end_and_no_count_stops_at_twelve_and_says_so():
    """Silence here would fill a calendar to the horizon."""
    days, notes = nexora._expand_recurrence(
        {"start": "2026-10-10", "repeat": "weekly"}, TODAY)
    assert len(days) == 12
    assert any("12" in n for n in notes)


def test_the_cap_is_loud():
    days, notes = nexora._expand_recurrence(
        {"start": "2026-10-10", "repeat": "weekly", "count": 500}, TODAY)
    assert len(days) <= nexora.RECURRENCE_CAP
    # the number asked for has to appear too, not just the number given
    assert any("capped" in n.lower() for n in notes), notes
    assert any("500" in n for n in notes), notes


def test_nothing_is_created_beyond_the_horizon():
    days, notes = nexora._expand_recurrence(
        {"start": "2026-10-10", "repeat": "monthly", "until": "2099-01-01"}, TODAY)
    assert max(days) <= TODAY.replace(year=TODAY.year + 2)
    assert any("18 months" in n or "Capped" in n for n in notes)


def test_a_missing_start_produces_nothing_and_explains_why():
    days, notes = nexora._expand_recurrence({"repeat": "weekly", "count": 3}, TODAY)
    assert days == []
    assert notes and "start date" in notes[0]


def test_a_junk_skip_range_is_ignored_rather_than_crashing():
    days, _ = nexora._expand_recurrence(
        {"start": "2026-10-10", "repeat": "weekly", "count": 2,
         "skip_ranges": [["not-a-date", "also-not"], ["2026-10-17"]]}, TODAY)
    assert iso(days) == ["2026-10-10", "2026-10-17"]


def test_the_same_request_twice_is_the_same_dates():
    """The whole reason the model does not emit dates."""
    spec = {"start": "2026-10-10", "repeat": "biweekly", "count": 6,
            "skip_ranges": [["2026-12-20", "2027-01-04"]]}
    a, _ = nexora._expand_recurrence(spec, TODAY)
    b, _ = nexora._expand_recurrence(spec, TODAY)
    assert a == b


# --------------------------------------------------------------------------
# Event payloads
# --------------------------------------------------------------------------

def test_a_test_becomes_an_exam_so_the_calendar_outlines_it():
    days = [date(2026, 10, 10)]
    out = nexora._recurrence_events({"title": "Unit test", "kind": "test"}, days)
    assert out[0]["kind"] == "exam"
    assert out[0]["kind"] in nexora.EVENT_KINDS


def test_a_repeating_entry_is_numbered_but_a_single_one_is_not():
    many = nexora._recurrence_events(
        {"title": "Test"}, [date(2026, 10, 10), date(2026, 10, 24)])
    assert [e["title"] for e in many] == ["Test 1", "Test 2"]
    one = nexora._recurrence_events({"title": "Midterm"}, [date(2026, 10, 10)])
    assert one[0]["title"] == "Midterm"


def test_a_time_that_is_not_a_time_is_dropped_not_guessed():
    out = nexora._recurrence_events({"title": "T", "time": "half four"}, [date(2026, 10, 10)])
    assert out[0]["time"] == ""


def test_an_unknown_kind_falls_back_to_event():
    out = nexora._recurrence_events({"title": "T", "kind": "banquet"}, [date(2026, 10, 10)])
    assert out[0]["kind"] == "event"


def test_other_is_the_no_course_sentinel_not_a_hallucinated_code():
    """_normalize_course_code('') returns OTHER, so the guard that rejects a
    course the student does not have must not report the model's correct
    refusal to guess as "Ignored the course OTHER"."""
    assert nexora._normalize_course_code("") == "OTHER"
    # an event with no course still files under OTHER, which is correct
    out = nexora._recurrence_events({"title": "Thing", "course": ""}, [date(2026, 10, 10)])
    assert out[0]["course"] == "OTHER"
