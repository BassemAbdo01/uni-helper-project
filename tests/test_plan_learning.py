"""Nexora learning how long this student's work actually takes, and coping
when a day goes by untouched.

Before this, `StudySession.state` was written by Done/Skip and read by
**nothing**. Nexora measured nothing about what a student actually did, which
is what blocked every "learn the student" idea: "Zeiad usually needs 3h15m for
SYSC labs" requires having recorded six labs.

And a plan block whose day had passed stayed `planned` for ever -- `_save_plan`
only replaces sessions from today forward -- so missed work accumulated as
ghosts that no rebuild could clear and no count could trust.
"""

import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"
TODAY = date.today()


@pytest.fixture()
def student(client):
    import app as nexora
    client.post("/signup", data={"name": "Z", "email": "learn@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="learn@carleton.ca").first()
        nexora.db.session.add(nexora.ClassBlock(
            id=uuid4().hex, user_id=user.id, label="Lecture", weekday=0,
            start_min=8 * 60, end_min=9 * 60, kind="lecture"))
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id, group_id="g1", title="Lab 7",
            base_title="Lab 7", course_full="SYSC2100", kind="due",
            is_deadline=True, due_raw=(TODAY + timedelta(days=6)).isoformat()))
        nexora.db.session.commit()
        return user.id


def finish_labs(user_id, count, minutes, course="SYSC2100", kind="lab"):
    """`count` finished items, each having really taken `minutes`."""
    import app as nexora
    with nexora.app.app_context():
        for n in range(count):
            nexora.db.session.add(nexora.StudySession(
                id=uuid4().hex, user_id=user_id, item_kind=kind,
                on_date=(TODAY - timedelta(days=40 - n * 4)).isoformat(),
                start_min=10 * 60, end_min=12 * 60, course_full=course,
                title=f"Lab {n + 1}", task_id=f"lab{n + 1}",
                state="done", actual_minutes=minutes))
        nexora.db.session.commit()


def planned_minutes(client, title="Lab 7"):
    body = client.post("/api/study-plan/generate").get_json()
    return sum(s["minutes"] for s in (body.get("sessions") or [])
               if s.get("title") == title)


# ------------------------------------------------- learning the estimate

def test_the_generic_guess_is_used_with_no_history(student, client):
    import app as nexora
    assert planned_minutes(client) == pytest.approx(
        nexora._BASE_EFFORT["lab"] * 60, abs=30)


def test_six_real_labs_change_the_next_estimate(student, client):
    """The whole point: measured beats guessed."""
    before = planned_minutes(client)
    finish_labs(student, 6, 195)
    after = planned_minutes(client)
    assert after > before
    assert after == pytest.approx(195, abs=30)


def test_it_takes_more_than_a_couple_of_samples(student, client):
    """One long night with a broken laptop must not redefine what a lab costs."""
    import app as nexora
    finish_labs(student, nexora.LEARNED_EFFORT_MIN_SAMPLES - 1, 400)
    assert planned_minutes(client) == pytest.approx(
        nexora._BASE_EFFORT["lab"] * 60, abs=30)


def test_the_median_not_the_mean(student):
    """One abandoned six-hour session would drag every future estimate."""
    import app as nexora
    finish_labs(student, 4, 60)
    with nexora.app.app_context():
        nexora.db.session.add(nexora.StudySession(
            id=uuid4().hex, user_id=student, item_kind="lab", course_full="SYSC2100",
            on_date=(TODAY - timedelta(days=2)).isoformat(), start_min=9 * 60,
            end_min=10 * 60, title="Lab 99", task_id="lab99",
            state="done", actual_minutes=480))
        nexora.db.session.commit()
        learned = nexora._learned_effort(student)
    assert learned[("SYSC2100", "lab")] == pytest.approx(1.0, abs=0.3)


def test_a_session_marked_done_with_no_duration_teaches_nothing(student):
    """Recording the PLANNED length as the actual one would teach the estimator
    its own guess back, for ever."""
    import app as nexora
    with nexora.app.app_context():
        for n in range(6):
            nexora.db.session.add(nexora.StudySession(
                id=uuid4().hex, user_id=student, item_kind="lab", course_full="SYSC2100",
                on_date=(TODAY - timedelta(days=10 + n)).isoformat(),
                start_min=10 * 60, end_min=12 * 60, title=f"L{n}", task_id=f"t{n}",
                state="done", actual_minutes=0))
            nexora.db.session.commit()
        assert nexora._learned_effort(student) == {}


def test_several_blocks_on_one_item_are_summed(student):
    """An assignment is several sessions; what it cost is the total."""
    import app as nexora
    with nexora.app.app_context():
        for n in range(nexora.LEARNED_EFFORT_MIN_SAMPLES):
            for part in range(3):
                nexora.db.session.add(nexora.StudySession(
                    id=uuid4().hex, user_id=student, item_kind="assignment",
                    course_full="ELEC2501", title=f"A{n}", task_id=f"a{n}",
                    on_date=(TODAY - timedelta(days=20 - n)).isoformat(),
                    start_min=10 * 60, end_min=11 * 60,
                    state="done", actual_minutes=50))
        nexora.db.session.commit()
        learned = nexora._learned_effort(student)
    assert learned[("ELEC2501", "assignment")] == pytest.approx(2.5, abs=0.1)


def test_learning_is_per_course_and_per_kind(student):
    import app as nexora
    finish_labs(student, 4, 195, course="SYSC2100", kind="lab")
    finish_labs(student, 4, 45, course="BIOL1103", kind="quiz")
    with nexora.app.app_context():
        learned = nexora._learned_effort(student)
    assert learned[("SYSC2100", "lab")] > learned[("BIOL1103", "quiz")]


def test_an_absurd_duration_is_capped(student, client):
    """A forgotten timer must not teach it that a lab takes a day and a half."""
    res = client.post("/api/study-plan/generate")
    sessions = res.get_json()["sessions"]
    assert sessions
    body = client.post(f"/api/study-plan/{sessions[0]['id']}",
                       json={"state": "done", "minutes": 99999}).get_json()
    assert body["session"]["actual_minutes"] == 480


def test_marking_it_unfinished_again_forgets_the_duration(student, client):
    sessions = client.post("/api/study-plan/generate").get_json()["sessions"]
    sid = sessions[0]["id"]
    client.post(f"/api/study-plan/{sid}", json={"state": "done", "minutes": 90})
    body = client.post(f"/api/study-plan/{sid}", json={"state": "planned"}).get_json()
    assert body["session"]["actual_minutes"] == 0


def test_the_page_asks_how_long_it_took(student, client):
    body = client.get("/study-plan").get_data(as_text=True)
    assert "How long did that actually take" in body
    assert "only learns from real answers" in body


# ------------------------------------------------- the day that went by

def miss_yesterday(user_id):
    import app as nexora
    with nexora.app.app_context():
        for start, end, course, title in ((10 * 60, 11 * 60, "SYSC2100", "Lab 7"),
                                          (13 * 60, 14 * 60 + 30, "BIOL1103", "Quiz 3")):
            nexora.db.session.add(nexora.StudySession(
                id=uuid4().hex, user_id=user_id, item_kind="lab",
                on_date=(TODAY - timedelta(days=1)).isoformat(),
                start_min=start, end_min=end, course_full=course,
                title=title, state="planned"))
        nexora.db.session.commit()


def test_a_missed_day_is_noticed_and_measured(student, client):
    miss_yesterday(student)
    body = client.post("/api/study-plan/generate").get_json()
    assert body["missed"]["count"] == 2
    assert body["missed"]["minutes"] == 150
    assert body["missed"]["courses"] == ["BIOL1103", "SYSC2100"]


def test_it_says_what_it_did_without_telling_anyone_off(student, client):
    miss_yesterday(student)
    message = client.post("/api/study-plan/generate").get_json()["catch_up"]
    assert "2h30" in message
    assert "back in your plan" in message
    for scold in ("should have", "failed", "you did not do", "behind schedule"):
        assert scold not in message.lower()


def test_missed_blocks_stop_lingering_as_planned(student, client):
    """They used to stay `planned` for ever, so no count could be trusted."""
    import app as nexora
    miss_yesterday(student)
    client.post("/api/study-plan/generate")
    with nexora.app.app_context():
        ghosts = nexora.StudySession.query.filter_by(
            user_id=student, state="planned").filter(
            nexora.StudySession.on_date < TODAY.isoformat()).count()
        assert ghosts == 0
        assert nexora.StudySession.query.filter_by(
            user_id=student, state="missed").count() == 2


def test_work_someone_actually_did_is_never_retired(student, client):
    import app as nexora
    with nexora.app.app_context():
        nexora.db.session.add(nexora.StudySession(
            id=uuid4().hex, user_id=student, item_kind="lab", course_full="SYSC2100",
            on_date=(TODAY - timedelta(days=1)).isoformat(), start_min=9 * 60,
            end_min=10 * 60, title="Done one", state="done", actual_minutes=55))
        nexora.db.session.commit()
    client.post("/api/study-plan/generate")
    with nexora.app.app_context():
        row = nexora.StudySession.query.filter_by(title="Done one").first()
        assert row.state == "done"
        assert row.actual_minutes == 55


def test_nothing_missed_says_nothing(student, client):
    assert client.post("/api/study-plan/generate").get_json()["catch_up"] == ""


def test_the_missed_work_comes_back_into_the_plan(student, client):
    """Its deadline has not moved, so it is still the thing to do."""
    miss_yesterday(student)
    body = client.post("/api/study-plan/generate").get_json()
    assert any(s["title"] == "Lab 7" for s in body["sessions"])


# --------------------------------------------- the exam revision ladder

def test_revision_climbs_through_stages(client):
    """Six blocks all saying "revise" is the advice that makes plans useless.
    Reading, then weak topics, then problems, then a past paper, then repair."""
    import app as nexora
    client.post("/signup", data={"name": "Z", "email": "ladder@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="ladder@carleton.ca").first()
        for weekday in range(6):
            nexora.db.session.add(nexora.ClassBlock(
                id=uuid4().hex, user_id=user.id, label="L", weekday=weekday,
                start_min=8 * 60, end_min=9 * 60, kind="lecture"))
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id, group_id="m1", title="Midterm 2",
            base_title="Midterm 2", course_full="ELEC2501", kind="due",
            is_deadline=True, due_raw=(TODAY + timedelta(days=13)).isoformat()))
        nexora.db.session.commit()
    client.post("/api/study-preferences", json={"focus": "exams"})
    sessions = client.post("/api/study-plan/generate").get_json()["sessions"]
    stages = [s["focus"] for s in sessions]
    assert any("rebuild your notes" in f for f in stages), stages
    assert any("weakest on" in f for f in stages), stages
    assert any("past paper" in f for f in stages), stages
    # And the stages arrive in order, furthest-out first.
    first_read = next(i for i, f in enumerate(stages) if "rebuild your notes" in f)
    first_paper = next(i for i, f in enumerate(stages) if "past paper" in f)
    assert first_read < first_paper


def test_revision_is_spread_not_spent(client):
    """A five-hour midterm poured its whole budget into the first two free days
    and stopped eleven days out, so the later rungs could never be reached."""
    import app as nexora
    client.post("/signup", data={"name": "Z", "email": "spread@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="spread@carleton.ca").first()
        for weekday in range(6):
            nexora.db.session.add(nexora.ClassBlock(
                id=uuid4().hex, user_id=user.id, label="L", weekday=weekday,
                start_min=8 * 60, end_min=9 * 60, kind="lecture"))
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id, group_id="m1", title="Midterm 2",
            base_title="Midterm 2", course_full="ELEC2501", kind="due",
            is_deadline=True, due_raw=(TODAY + timedelta(days=13)).isoformat()))
        nexora.db.session.commit()
    client.post("/api/study-preferences", json={"focus": "exams"})
    sessions = client.post("/api/study-plan/generate").get_json()["sessions"]
    days = {s["date"] for s in sessions}
    assert len(days) >= 4, f"revision crammed into {len(days)} day(s)"
    # The last block should be close to the exam, not a fortnight before it.
    latest = max(date.fromisoformat(d) for d in days)
    assert (TODAY + timedelta(days=13) - latest).days <= 4


@pytest.mark.parametrize("days_left,expected", [
    (14, "rebuild your notes"), (11, "rebuild your notes"),
    (10, "weakest on"), (8, "weakest on"),
    (7, "problem sets"), (5, "problem sets"),
    (4, "past paper"), (3, "past paper"),
    (2, "repair"),
    (1, "light review"), (0, "light review"),
])
def test_each_rung_covers_the_days_it_should(days_left, expected):
    import app as nexora
    assert expected in nexora._exam_stage(days_left)


def test_the_outlines_own_pointer_survives_the_stage(client):
    """The rung is HOW; the outline's "module 4-6" is WHERE. A block needs
    both, and the first version of this dropped the pointer."""
    import app as nexora
    item = {"kind": "midterm", "due": TODAY + timedelta(days=6),
            "brief": {"topics": ["cellular respiration"], "how": "module 4-6"}}
    focus = nexora._focus_for(item, TODAY)
    assert "cellular respiration" in focus
    assert "module 4-6" in focus
    assert "problem sets" in focus
