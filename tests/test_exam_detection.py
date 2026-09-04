"""Pulling exam dates out of outlines and announcements.

Deliberately a regex and not the model: a wrong exam date is worse than no exam
date, and this runs on ordinary page views where a Gemini call would eat the
quota the outline pipeline needs.

The interesting failures are all in the reading. "Oct 30" has no year, "12am"
is not noon, a review session is not a sitting, and an announcement telling
students where to find last year's practice midterm must not put an exam on
anybody's calendar.
"""

import json
import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _exam_title,
    _find_date,
    _find_location,
    _find_time,
    _looks_like_an_exam,
)

REF = date(2026, 9, 2)


# ------------------------------------------------------------------ dates

@pytest.mark.parametrize("text,expected", [
    ("The midterm is on 2026-10-30.", date(2026, 10, 30)),
    ("Midterm: Oct 30", date(2026, 10, 30)),
    ("Midterm: Oct. 30, 2026", date(2026, 10, 30)),
    ("Midterm on October 30th", date(2026, 10, 30)),
    ("held 30 October 2026", date(2026, 10, 30)),
    ("held on 3 November", date(2026, 11, 3)),
    ("Final exam December 12, 2026", date(2026, 12, 12)),
    ("Unit 1 Test in week 5, Sep 28", date(2026, 9, 28)),
])
def test_dates_are_read_out_of_ordinary_wording(text, expected):
    assert _find_date(text, REF) == expected


def test_a_month_with_no_year_takes_the_term_in_progress():
    """An outline written in September saying "Oct 30" means this October."""
    assert _find_date("Midterm Oct 30", REF) == date(2026, 10, 30)


def test_a_month_already_past_rolls_to_next_year():
    """In September, "February 3" is the coming February, not one gone by."""
    assert _find_date("Exam February 3", REF) == date(2027, 2, 3)


def test_a_date_a_few_weeks_back_is_left_in_the_past():
    """A window of grace, so a recent sitting is not reported as a year away."""
    assert _find_date("Test August 20", REF) == date(2026, 8, 20)


@pytest.mark.parametrize("text", [
    "The exam date is TBD.",
    "See Brightspace for the schedule.",
    "Midterm in week 7.",
    "Exam on the 30th",
])
def test_wording_with_no_real_date_yields_nothing(text):
    assert _find_date(text, REF) is None


def test_an_impossible_date_is_refused():
    assert _find_date("Exam on February 30", REF) is None


# ------------------------------------------------------------------ times

@pytest.mark.parametrize("text,expected", [
    ("starts at 6pm", "18:00"),
    ("starts at 6 PM", "18:00"),
    ("at 6:30 pm", "18:30"),
    ("at 9:05 a.m.", "09:05"),
    ("18:30 in Southam", "18:30"),
    ("at 12pm", "12:00"),
    ("at 12am", "00:00"),
    ("at 11:59 pm", "23:59"),
])
def test_times_are_read_in_the_forms_people_write_them(text, expected):
    assert _find_time(text) == expected


def test_no_time_mentioned_means_all_day():
    assert _find_time("Midterm on Oct 30 in Southam Hall") is None


# -------------------------------------------------------------- locations

@pytest.mark.parametrize("text,expected", [
    ("held in Southam Hall", "Southam Hall"),
    ("in the Minto Centre 2000", "Minto Centre 2000"),
    ("Room: Mackenzie Building 3380", "Mackenzie Building 3380"),
    ("at Kailash Mital Theatre", "Kailash Mital Theatre"),
])
def test_a_room_is_picked_up_when_one_is_named(text, expected):
    assert expected in _find_location(text)


def test_no_room_named_is_left_blank():
    assert _find_location("The midterm is on Oct 30.") == ""


# --------------------------------------------------------- is it an exam?

@pytest.mark.parametrize("text", [
    "Midterm 2 will be held Oct 30",
    "Final Exam schedule released",
    "Unit 1 Test is next week",
    "Quiz 3 on Friday",
])
def test_a_real_sitting_is_recognised(text):
    assert _looks_like_an_exam(text)


@pytest.mark.parametrize("text", [
    "Practice midterm posted",
    "Review session for the final exam",
    "Past exams are in the library",
    "Solutions to the midterm are up",
    "Study guide for the final",
    "Extra office hours before the exam",
    "Mock test available",
])
def test_material_about_an_exam_is_not_an_exam(text):
    """The commonest false positive by far, and the one that would put a
    fictional midterm on someone's calendar."""
    assert not _looks_like_an_exam(text)


def test_something_with_no_exam_word_is_not_one():
    assert not _looks_like_an_exam("Lab 4 report due Friday")


@pytest.mark.parametrize("text,expected", [
    ("Midterm 2 will be held Oct 30", "Midterm 2"),
    ("Final Exam is cumulative", "Final Exam"),
    ("Unit 1 Test in week 5", "Unit 1 Test"),
])
def test_the_entry_is_named_after_what_it_is(text, expected):
    assert _exam_title(text, "BIOL1103").lower().startswith(expected.lower()[:8])


# ------------------------------------------------------------ end to end

def signup(client, email="exam@carleton.ca"):
    return client.post("/signup", data={"name": "Exam Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


def soon(days):
    return date.today() + timedelta(days=days)


@pytest.fixture()
def course(client):
    """A student with one analysed outline and a few announcements."""
    import app as nexora
    signup(client)
    midterm = soon(30)
    final = soon(75)
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="exam@carleton.ca").first()
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user.id, course_code="BIOL1103",
            course_name="Foundations of Biology I", term="Fall 2026"))
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full="BIOL1103",
            grading_json=json.dumps({"important_dates": [
                {"date": midterm.isoformat(), "description": "Unit 1 Test in week 5"},
                {"date": final.isoformat(), "description": "Final Exam period starts"},
                {"date": soon(5).isoformat(), "description": "Last day to withdraw with fee adjustment"},
            ]}), pass_mark=50))
        for title, text, kind in [
            ("Midterm 2 details",
             f"Midterm 2 will be held on {soon(45).strftime('%B %-d' if os.name != 'nt' else '%B %d').replace(' 0', ' ')} at 6:30 pm in Southam Hall.",
             "announcement"),
            ("Practice midterm posted",
             "A practice midterm from last year is now in the Resources module.",
             "announcement"),
            ("Lecture 12", "Polymorphism and inheritance.", "lecture"),
        ]:
            nexora.db.session.add(nexora.CourseContent(
                id=uuid4().hex, user_id=user.id, course_full="BIOL1103",
                title=title, content_text=text, content_type=kind,
                source_uid=uuid4().hex))
        nexora.db.session.commit()
    return client


def suggestions(client):
    res = client.get("/api/exams/suggestions")
    assert res.status_code == 200
    return res.get_json()["suggestions"]


def test_exams_in_the_outline_are_offered(course):
    titles = [s["title"] for s in suggestions(course)]
    assert any("Test" in t for t in titles)
    assert any("Final" in t for t in titles)


def test_an_administrative_date_is_not_offered_as_an_exam(course):
    assert not any("withdraw" in s["title"].lower() for s in suggestions(course))


def test_an_announcement_becomes_a_suggestion_with_its_time_and_room(course):
    found = [s for s in suggestions(course) if s["source"] == "Announcement"]
    assert found, "the midterm announcement should have been read"
    assert found[0]["time"] == "18:30"
    assert "Southam" in found[0]["location"]


def test_a_practice_paper_never_becomes_an_exam(course):
    assert not any("practice" in s["detail"].lower() for s in suggestions(course))


def test_a_lecture_is_not_scanned(course):
    assert not any("Polymorphism" in s.get("detail", "") for s in suggestions(course))


def test_nothing_is_added_until_the_student_says_so(course):
    """These come out of prose by regex. Silently writing them onto a calendar
    would make a misread date indistinguishable from a real one."""
    before = course.get("/calendar").get_data(as_text=True)
    suggestions(course)
    assert course.get("/calendar").get_data(as_text=True) == before


def test_accepting_puts_them_on_the_calendar(course):
    picked = suggestions(course)[:2]
    res = course.post("/api/exams/accept", json={"events": picked})
    assert res.status_code == 200
    assert res.get_json()["count"] == 2
    month = picked[0]["date"][:7]
    assert picked[0]["title"] in course.get(f"/calendar?month={month}").get_data(as_text=True)


def test_an_accepted_exam_stops_being_suggested(course):
    picked = suggestions(course)
    course.post("/api/exams/accept", json={"events": picked})
    assert suggestions(course) == []


def test_accepting_the_same_thing_twice_does_not_duplicate_it(course):
    picked = suggestions(course)[:1]
    course.post("/api/exams/accept", json={"events": picked})
    assert course.post("/api/exams/accept", json={"events": picked}).get_json()["count"] == 0


def test_a_sitting_that_has_already_happened_is_not_suggested(client):
    import app as nexora
    signup(client, email="past-exam@carleton.ca")
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="past-exam@carleton.ca").first()
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full="SYSC2004",
            grading_json=json.dumps({"important_dates": [
                {"date": (date.today() - timedelta(days=10)).isoformat(),
                 "description": "Midterm 1"}]}), pass_mark=50))
        nexora.db.session.commit()
    assert suggestions(client) == []


def test_accept_refuses_an_empty_request(course):
    assert course.post("/api/exams/accept", json={"events": []}).status_code == 400


def test_the_endpoints_need_a_login(client):
    assert client.get("/api/exams/suggestions").status_code in (302, 401)
    assert client.post("/api/exams/accept", json={"events": []}).status_code in (302, 401)
