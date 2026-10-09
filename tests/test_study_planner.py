"""The study planner.

Most study plans are abandoned for four reasons, and each has a test here: they
schedule over your classes, they spread time evenly instead of by what is at
stake, they cram a fortnight of revision into the night before, and they tell
you "study BIOL for 90 minutes" instead of what to actually do.

The scheduling is arithmetic on purpose. A plan has to be the same plan when
you look at it twice, and has to be able to say why every block is where it is.
"""

import json
import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def signup(client, email="plan@carleton.ca"):
    return client.post("/signup", data={"name": "Plan Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


def hm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@pytest.fixture()
def student(client):
    signup(client)
    return client


# ------------------------------------------------------------- the timetable

def test_a_class_can_be_added_by_hand(student):
    res = student.post("/api/timetable", json={
        "label": "BIOL 1103 Lecture", "course": "BIOL1103", "weekday": "monday",
        "start": "11:35", "end": "12:55", "location": "Southam Hall", "kind": "lecture"})
    assert res.status_code == 201
    block = res.get_json()["block"]
    assert block["weekday"] == MON
    assert (block["start"], block["end"]) == (11 * 60 + 35, 12 * 60 + 55)
    assert block["start_label"] == "11:35"


@pytest.mark.parametrize("given,expected", [
    ("11:35", 11 * 60 + 35), ("9:00", 9 * 60), ("09:00", 9 * 60),
    ("4:35 pm", 16 * 60 + 35), ("4:35PM", 16 * 60 + 35), ("8am", 8 * 60),
    ("12pm", 12 * 60), ("12am", 0), ("1.30", 60 + 30),
])
def test_times_are_read_the_way_timetables_write_them(student, given, expected):
    body = {"label": "Class", "weekday": "tue", "start": given, "end": "23:00"}
    assert student.post("/api/timetable", json=body).get_json()["block"]["start"] == expected


@pytest.mark.parametrize("day,expected", [
    ("monday", MON), ("Mon", MON), ("tues", TUE), ("Wednesday", WED),
    ("thurs", THU), ("R", THU), ("fri", FRI), ("3", THU),
])
def test_days_are_read_the_way_timetables_write_them(student, day, expected):
    body = {"label": "Class", "weekday": day, "start": "10:00", "end": "11:00"}
    assert student.post("/api/timetable", json=body).get_json()["block"]["weekday"] == expected


@pytest.mark.parametrize("body,message", [
    ({"label": " "}, "name"),
    ({"weekday": "someday"}, "day"),
    ({"start": "half past nine"}, "start time"),
    ({"end": "nonsense"}, "end time"),
    ({"start": "14:00", "end": "13:00"}, "end after it starts"),
    ({"start": "14:00", "end": "14:00"}, "end after it starts"),
])
def test_a_class_that_makes_no_sense_is_refused(student, body, message):
    payload = {"label": "Class", "weekday": "monday", "start": "10:00", "end": "11:00"}
    payload.update(body)
    res = student.post("/api/timetable", json=payload)
    assert res.status_code == 400
    assert message in res.get_json()["error"]


def test_a_class_can_be_edited_and_deleted(student):
    block_id = student.post("/api/timetable", json={
        "label": "Lab", "weekday": "friday", "start": "14:00", "end": "17:00"}).get_json()["block"]["id"]
    edited = student.post(f"/api/timetable/{block_id}", json={"start": "13:00"}).get_json()["block"]
    assert edited["start"] == 13 * 60
    assert edited["end"] == 17 * 60          # untouched
    assert student.delete(f"/api/timetable/{block_id}").status_code == 200
    assert student.get("/api/timetable").get_json()["blocks"] == []


def test_editing_cannot_invert_a_class(student):
    block_id = student.post("/api/timetable", json={
        "label": "Lab", "weekday": "friday", "start": "14:00", "end": "17:00"}).get_json()["block"]["id"]
    res = student.post(f"/api/timetable/{block_id}", json={"end": "09:00"})
    assert res.status_code == 400
    assert student.get("/api/timetable").get_json()["blocks"][0]["end"] == 17 * 60


def test_a_whole_week_can_be_confirmed_at_once(student):
    res = student.post("/api/timetable", json={"replace": True, "blocks": [
        {"label": "BIOL Lecture", "weekday": "monday", "start": "11:35", "end": "12:55"},
        {"label": "BIOL Lecture", "weekday": "wednesday", "start": "11:35", "end": "12:55"},
        {"label": "not a class", "weekday": "someday", "start": "x", "end": "y"},
    ]})
    body = res.get_json()
    assert (body["added"], body["skipped"]) == (2, 1)
    assert len(body["blocks"]) == 2


def test_another_students_timetable_is_untouchable(client):
    signup(client, email="owner2@carleton.ca")
    block_id = client.post("/api/timetable", json={
        "label": "Lab", "weekday": "monday", "start": "09:00", "end": "10:00"}).get_json()["block"]["id"]
    client.get("/logout")
    signup(client, email="intruder2@carleton.ca")
    assert client.post(f"/api/timetable/{block_id}", json={"start": "08:00"}).status_code == 404
    assert client.delete(f"/api/timetable/{block_id}").status_code == 404
    assert client.get("/api/timetable").get_json()["blocks"] == []


# ------------------------------------------------------------- preferences

def test_preferences_have_workable_defaults(student):
    pref = student.get("/api/study-preferences").get_json()["preferences"]
    assert pref["earliest_min"] < pref["latest_min"]
    assert 20 <= pref["session_min"] <= 180
    assert pref["days_off"] == []


def test_preferences_are_clamped_rather_than_trusted(student):
    pref = student.post("/api/study-preferences", json={
        "earliest_min": -500, "latest_min": 99999, "session_min": 3,
        "max_daily_min": 100000}).get_json()["preferences"]
    assert pref["earliest_min"] == 0
    assert pref["latest_min"] == 24 * 60
    assert pref["session_min"] == 20
    assert pref["max_daily_min"] == 12 * 60


def test_hours_that_leave_no_room_are_refused(student):
    res = student.post("/api/study-preferences",
                       json={"earliest_min": 600, "latest_min": 620, "session_min": 50})
    assert res.status_code == 400
    assert "no room" in res.get_json()["error"]


def test_taking_every_day_off_is_refused(student):
    res = student.post("/api/study-preferences", json={"days_off": [0, 1, 2, 3, 4, 5, 6]})
    assert res.status_code == 400


# -------------------------------------------------------------- the planner

def seed_week(email, classes=(), work=(), components=()):
    """classes: (weekday, start, end); work: (title, course, days_from_now, kind);
    components: (course, name, weight)"""
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        for weekday, start, end in classes:
            nexora.db.session.add(nexora.ClassBlock(
                id=uuid4().hex, user_id=user.id, label="Class", weekday=weekday,
                start_min=start, end_min=end, kind="lecture"))
        for title, course, offset, kind in work:
            due = (date.today() + timedelta(days=offset)).isoformat()
            nexora.db.session.add(nexora.Task(
                id=uuid4().hex, user_id=user.id,
                group_id=nexora.make_group_id(course, title), title=title,
                base_title=title, course_full=course, course_prefix=course[:4],
                due_raw=due, kind=kind, source="manual", sync_state="active"))
        for course, name, weight in components:
            nexora.db.session.add(nexora.GradeEntry(
                id=uuid4().hex, user_id=user.id, course_full=course,
                name=name, weight=weight, score=None, max_score=100))
        nexora.db.session.commit()


def generate(client):
    res = client.post("/api/study-plan/generate")
    assert res.status_code == 200
    return res.get_json()


def test_nothing_is_planned_over_a_class(student):
    """The first reason plans get abandoned."""
    seed_week("plan@carleton.ca",
              classes=[(d, 9 * 60, 17 * 60) for d in range(7)],
              work=[("Assignment 1", "BIOL1103", 5, "due")])
    student.post("/api/study-preferences",
                 json={"earliest_min": 9 * 60, "latest_min": 17 * 60, "buffer_min": 0})
    body = generate(student)
    assert body["created"] == 0
    assert "free time" in body["reason"]


def test_a_gap_between_classes_gets_used(student):
    """Two classes with a hole between them: the hole is where studying goes."""
    seed_week("plan@carleton.ca",
              classes=[(d, 9 * 60, 11 * 60) for d in range(7)]
                      + [(d, 14 * 60, 16 * 60) for d in range(7)],
              work=[("Project", "BIOL1103", 9, "project")])
    student.post("/api/study-preferences", json={
        "session_min": 50, "buffer_min": 0, "break_min": 0,
        "earliest_min": 9 * 60, "latest_min": 18 * 60})
    sessions = generate(student)["sessions"]
    assert sessions, "the 11:00-14:00 and 16:00-18:00 gaps should have been used"
    for s in sessions:
        in_midday = s["start"] >= 11 * 60 and s["end"] <= 14 * 60
        in_evening = s["start"] >= 16 * 60 and s["end"] <= 18 * 60
        assert in_midday or in_evening, f"{s['start_label']}-{s['end_label']} overlaps a class"


def test_a_buffer_is_left_around_a_class(student):
    """A block that starts the minute a lecture ends is one nobody attends."""
    seed_week("plan@carleton.ca", classes=[(TUE, 9 * 60, 11 * 60)],
              work=[("Assignment 1", "BIOL1103", 6, "due")])
    student.post("/api/study-preferences", json={"buffer_min": 30, "earliest_min": 9 * 60})
    tuesday = (date.today() + timedelta(days=(TUE - date.today().weekday()) % 7)).isoformat()
    for s in generate(student)["sessions"]:
        if s["date"] == tuesday:
            assert s["start"] >= 11 * 60 + 30 or s["end"] <= 9 * 60 - 30


def test_a_day_off_stays_clear(student):
    seed_week("plan@carleton.ca", work=[("Assignment 1", "BIOL1103", 10, "due")])
    student.post("/api/study-preferences", json={"days_off": [SAT, SUN]})
    for s in generate(student)["sessions"]:
        assert date.fromisoformat(s["date"]).weekday() not in (SAT, SUN)


def test_sessions_stay_inside_the_hours_that_were_asked_for(student):
    seed_week("plan@carleton.ca", work=[("Assignment 1", "BIOL1103", 9, "due")])
    student.post("/api/study-preferences", json={"earliest_min": 10 * 60, "latest_min": 20 * 60})
    for s in generate(student)["sessions"]:
        assert s["start"] >= 10 * 60
        assert s["end"] <= 20 * 60


def test_a_daily_cap_is_respected(student):
    seed_week("plan@carleton.ca",
              work=[(f"Assignment {i}", "BIOL1103", 4, "due") for i in range(1, 7)])
    student.post("/api/study-preferences", json={"max_daily_min": 120, "session_min": 50, "break_min": 0})
    by_day = {}
    for s in generate(student)["sessions"]:
        by_day[s["date"]] = by_day.get(s["date"], 0) + s["minutes"]
    assert by_day, "something should have been planned"
    assert max(by_day.values()) <= 120


def test_the_heavier_course_gets_more_of_the_week(student):
    """The second reason plans get abandoned: even time for uneven stakes."""
    seed_week("plan@carleton.ca",
              work=[("Midterm", "BIOL1103", 8, "exam"), ("Quiz", "SYSC2004", 8, "quiz")],
              components=[("BIOL1103", "Midterm", 35), ("SYSC2004", "Quiz", 3)])
    minutes = {}
    for s in generate(student)["sessions"]:
        minutes[s["course"]] = minutes.get(s["course"], 0) + s["minutes"]
    assert minutes.get("BIOL1103", 0) > minutes.get("SYSC2004", 0)


def test_nothing_is_scheduled_after_its_deadline(student):
    seed_week("plan@carleton.ca", work=[("Assignment 1", "BIOL1103", 2, "due")])
    due = (date.today() + timedelta(days=2)).isoformat()
    for s in generate(student)["sessions"]:
        if s["title"] == "Assignment 1":
            assert s["date"] <= due


def test_revision_for_a_distant_exam_does_not_start_today(student):
    """The third reason: a plan that either crams or starts a month early."""
    seed_week("plan@carleton.ca", work=[("Final exam", "BIOL1103", 40, "exam")],
              components=[("BIOL1103", "Final exam", 40)])
    body = generate(student)
    assert all(s["title"] != "Final exam" for s in body["sessions"])


def test_revision_for_a_near_exam_is_spread_over_days(student):
    seed_week("plan@carleton.ca", work=[("Midterm", "BIOL1103", 7, "exam")],
              components=[("BIOL1103", "Midterm", 30)])
    days = {s["date"] for s in generate(student)["sessions"] if s["title"] == "Midterm"}
    assert len(days) >= 2, "revision crammed into one day is the thing to avoid"


def test_overdue_work_comes_first(student):
    seed_week("plan@carleton.ca",
              work=[("Late report", "BIOL1103", -3, "due"), ("Next week essay", "SYSC2004", 9, "due")])
    sessions = generate(student)["sessions"]
    assert sessions
    assert sessions[0]["title"] == "Late report"


def test_every_session_says_what_to_do_and_why(student):
    """The fourth reason: "study BIOL, 90 minutes" is not a plan."""
    seed_week("plan@carleton.ca", work=[("Midterm", "BIOL1103", 6, "exam")],
              components=[("BIOL1103", "Midterm", 25)])
    for s in generate(student)["sessions"]:
        assert len(s["focus"]) > 20
        assert s["reason"]
        assert "25%" in s["reason"] or "due in" in s["reason"]


def test_one_thing_does_not_eat_a_whole_day(student):
    """Interleaving beats blocking for retention, and a day spent entirely on
    one course is how the other one quietly slips.

    Only while both still have work: once one is fully scheduled, the rest of
    the plan being the other one is right, not a bug."""
    seed_week("plan@carleton.ca",
              work=[("Project", "BIOL1103", 10, "project"), ("Essay", "SYSC2004", 10, "essay")],
              components=[("BIOL1103", "Project", 30), ("SYSC2004", "Essay", 25)])
    sessions = generate(student)["sessions"]
    # The first WHOLE day, which is never today. Today's window opens at the
    # next quarter-hour after now, so how many blocks it holds depends on the
    # hour the suite runs at -- and a day truncated to exactly two blocks is
    # legitimately allowed to hold two of the same title, because
    # MAX_CONSECUTIVE_SAME is 2. Judging interleaving on that is judging it on
    # a fragment. This failed for the first time at 16:00 on a Friday with the
    # planner behaving correctly.
    from datetime import date as _date
    today_iso = _date.today().isoformat()
    by_day = {}
    for s in sessions:
        by_day.setdefault(s["date"], []).append(s)
    first_full = next(day for day in sorted(by_day)
                      if day > today_iso and len(by_day[day]) >= 2)
    assert len({s["title"] for s in by_day[first_full]}) > 1, "the first full day is all one thing"

    run, previous, longest = 0, None, 0
    for s in sessions[:len(sessions) // 2]:
        run = run + 1 if s["title"] == previous else 1
        previous = s["title"]
        longest = max(longest, run)
    assert longest <= 3


def test_the_same_week_generated_twice_is_the_same_week(student):
    """A plan that reshuffles itself between two looks is not a plan."""
    seed_week("plan@carleton.ca",
              work=[("Midterm", "BIOL1103", 8, "exam"), ("Essay", "SYSC2004", 5, "essay")],
              components=[("BIOL1103", "Midterm", 30), ("SYSC2004", "Essay", 20)])
    first = [(s["date"], s["start"], s["title"]) for s in generate(student)["sessions"]]
    second = [(s["date"], s["start"], s["title"]) for s in generate(student)["sessions"]]
    assert first == second


def test_a_session_can_be_ticked_off_and_survives_a_regenerate(student):
    seed_week("plan@carleton.ca", work=[("Assignment 1", "BIOL1103", 8, "due")])
    first = generate(student)["sessions"][0]
    assert student.post(f"/api/study-plan/{first['id']}", json={"state": "done"}).status_code == 200
    after = generate(student)["sessions"]
    kept = [s for s in after if s["id"] == first["id"]]
    assert kept and kept[0]["state"] == "done", "regenerating must not erase finished work"


def test_a_skipped_session_is_remembered_too(student):
    seed_week("plan@carleton.ca", work=[("Assignment 1", "BIOL1103", 8, "due")])
    first = generate(student)["sessions"][0]
    student.post(f"/api/study-plan/{first['id']}", json={"state": "skipped"})
    assert student.get("/api/study-plan").get_json()["sessions"][0]["state"] == "skipped"


def test_an_unknown_state_is_refused(student):
    seed_week("plan@carleton.ca", work=[("Assignment 1", "BIOL1103", 8, "due")])
    first = generate(student)["sessions"][0]
    assert student.post(f"/api/study-plan/{first['id']}", json={"state": "maybe"}).status_code == 400


def test_no_timetable_says_so_rather_than_planning_nothing(student):
    seed_week("plan@carleton.ca", work=[("Assignment 1", "BIOL1103", 5, "due")])
    body = generate(student)
    assert body["created"] > 0     # a student with no classes is simply free


def test_nothing_due_says_so(student):
    seed_week("plan@carleton.ca", classes=[(MON, 9 * 60, 11 * 60)])
    body = generate(student)
    assert body["created"] == 0
    assert "Nothing is due" in body["reason"]


def test_a_calendar_event_is_not_homework(student):
    """An event a student put on their calendar is not work to be scheduled."""
    seed_week("plan@carleton.ca", work=[("Dentist", "OTHER", 3, "event")])
    body = generate(student)
    assert all(s["title"] != "Dentist" for s in body["sessions"])


def test_one_students_plan_is_never_anothers(client):
    signup(client, email="mine@carleton.ca")
    seed_week("mine@carleton.ca", work=[("My essay", "BIOL1103", 6, "due")])
    generate(client)
    client.get("/logout")
    signup(client, email="theirs@carleton.ca")
    assert client.get("/api/study-plan").get_json()["sessions"] == []


def test_the_endpoints_need_a_login(client):
    for path in ("/api/timetable", "/api/study-preferences", "/api/study-plan"):
        assert client.get(path).status_code in (302, 401)
    assert client.post("/api/study-plan/generate").status_code in (302, 401)
