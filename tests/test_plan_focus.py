"""What the plan is FOR.

Reported from real use: the plan was full of pre-lab quizzes, lab reports and
milestones, and a student following it would reach their midterm having revised
nothing. The small items win on sheer number -- a term has twenty of them and
two exams -- so "plan everything" quietly means "plan the unimportant things".

Measured on a realistic term: 27 blocks with everything on, of which a single
project milestone took 8 and each pre-lab quiz took 2. Aimed at exams: 12
blocks, all revision.

The AI reads the REQUEST, never the schedule. A plan has to be the same plan
twice, has to explain every block, and must not cost quota to redraw -- so a
sentence becomes settings and the existing arithmetic builds the week.
"""

import os
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"
TODAY = date.today()

WORK = [
    ("Pre-lab quiz 5", 3, "BIOL1103"),
    ("Lab 04 report", 4, "BIOL1103"),
    ("Milestone 3", 5, "SYSC2004"),
    ("Weekly quiz 7", 7, "BIOL1103"),
    ("Midterm 2", 12, "BIOL1103"),
    ("Midterm 1", 9, "MATH1004"),
]


@pytest.fixture()
def student(client):
    import app as nexora
    client.post("/signup", data={"name": "S", "email": "focus@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="focus@carleton.ca").first()
        nexora.db.session.add(nexora.ClassBlock(
            id=uuid4().hex, user_id=user.id, label="Lecture", weekday=0,
            start_min=9 * 60, end_min=10 * 60, kind="lecture"))
        for title, days, course in WORK:
            nexora.db.session.add(nexora.Task(
                id=uuid4().hex, user_id=user.id, group_id=uuid4().hex, title=title,
                base_title=title, course_full=course, kind="due", is_deadline=True,
                due_raw=(TODAY + timedelta(days=days)).isoformat()))
        nexora.db.session.commit()
    return client


def titles(client):
    body = client.post("/api/study-plan/generate").get_json()
    return sorted({s.get("title") for s in (body.get("sessions") or [])})


# --------------------------------------------------------- the filter

def test_everything_is_the_default(student):
    """Unchanged behaviour for anyone who does not touch it."""
    got = titles(student)
    assert "Midterm 2" in got
    assert "Pre-lab quiz 5" in got


def test_aiming_at_exams_drops_the_filler(student):
    student.post("/api/study-preferences", json={"focus": "exams"})
    got = titles(student)
    assert got == ["Midterm 1", "Midterm 2"], got


def test_the_filler_was_really_crowding_it_out(student):
    """Not a matter of taste: count the blocks."""
    everything = student.post("/api/study-plan/generate").get_json()["sessions"]
    student.post("/api/study-preferences", json={"focus": "exams"})
    exams = student.post("/api/study-plan/generate").get_json()["sessions"]
    revision_before = [s for s in everything if "Midterm" in (s.get("title") or "")]
    assert len(exams) > len(revision_before) or len(everything) > len(exams) * 2


def test_a_named_course_narrows_it_further(student):
    student.post("/api/study-preferences",
                 json={"focus": "exams", "focus_courses": ["MATH1004"]})
    assert titles(student) == ["Midterm 1"]


def test_a_course_the_student_does_not_have_is_refused(student):
    """An unknown code would silently empty the plan."""
    body = student.post("/api/study-preferences",
                        json={"focus_courses": ["NOPE999"]}).get_json()
    assert body["preferences"]["focus_courses"] == []


def test_a_feed_only_student_can_still_name_a_course(student):
    """Their courses exist as deadlines, not SyncedCourse rows. Naming one was
    being discarded as unknown, so "only MATH" looked like it did nothing."""
    body = student.post("/api/study-preferences",
                        json={"focus_courses": ["BIOL1103"]}).get_json()
    assert body["preferences"]["focus_courses"] == ["BIOL1103"]


def test_switching_back_restores_everything(student):
    student.post("/api/study-preferences", json={"focus": "exams"})
    student.post("/api/study-preferences", json={"focus": "all", "focus_courses": []})
    assert "Pre-lab quiz 5" in titles(student)


def test_an_empty_exam_plan_explains_itself(client):
    """"Nothing is due" is wrong when the truth is "no exams are due"."""
    import app as nexora
    client.post("/signup", data={"name": "S", "email": "noex@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="noex@carleton.ca").first()
        nexora.db.session.add(nexora.ClassBlock(
            id=uuid4().hex, user_id=user.id, label="L", weekday=0,
            start_min=9 * 60, end_min=10 * 60, kind="lecture"))
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id, group_id="g", title="Pre-lab quiz 1",
            base_title="Pre-lab quiz 1", course_full="BIOL1103", kind="due",
            is_deadline=True, due_raw=(TODAY + timedelta(days=3)).isoformat()))
        nexora.db.session.commit()
    client.post("/api/study-preferences", json={"focus": "exams"})
    body = client.post("/api/study-plan/generate").get_json()
    assert not body["sessions"]
    assert "midterms or finals" in body["reason"]


# ------------------------------------------------- saying it in words

@pytest.fixture()
def reader(student, monkeypatch):
    import app as nexora
    monkeypatch.setattr(nexora, "_gemini_json", lambda *a, **k: {
        "focus": "exams", "courses": ["MATH1004"], "days": 21,
        "max_daily_minutes": 180,
        "understood": "Planning only your midterms and finals for MATH1004."})
    return student


def test_a_sentence_becomes_settings(reader):
    body = reader.post("/api/study-plan/ask", json={
        "question": "only midterms and finals for MATH, three weeks ahead"}).get_json()
    assert body["focus"] == "exams"
    assert body["courses"] == ["MATH1004"]
    assert body["days"] == 21
    assert body["max_daily_minutes"] == 180


def test_it_says_what_it_understood(reader):
    """A misreading must be visible, not silently reshape someone's term."""
    body = reader.post("/api/study-plan/ask",
                       json={"question": "midterms only"}).get_json()
    assert "MATH1004" in body["understood"]


def test_the_settings_actually_stick(reader):
    reader.post("/api/study-plan/ask", json={"question": "midterms only for MATH"})
    prefs = reader.get("/api/study-preferences").get_json()["preferences"]
    assert prefs["focus"] == "exams"
    assert prefs["focus_courses"] == ["MATH1004"]


def test_the_plan_it_returns_matches_a_plain_rebuild(reader):
    """The AI chose the settings; the arithmetic built the week. Asking again
    with the same settings has to give the same week."""
    asked = reader.post("/api/study-plan/ask",
                        json={"question": "midterms only for MATH"}).get_json()
    rebuilt = reader.post("/api/study-plan/generate").get_json()
    assert [s["title"] for s in asked["sessions"]] == \
           [s["title"] for s in rebuilt["sessions"]]


def test_a_hallucinated_course_is_dropped(student, monkeypatch):
    """Naming a course the student does not have would empty their plan."""
    import app as nexora
    monkeypatch.setattr(nexora, "_gemini_json", lambda *a, **k: {
        "focus": "exams", "courses": ["PHYS9999"], "days": 14,
        "understood": "Planning PHYS9999."})
    body = student.post("/api/study-plan/ask",
                        json={"question": "just physics"}).get_json()
    assert body["courses"] == []


@pytest.mark.parametrize("bad", [{"days": 900}, {"days": "soon"}, {"days": 0}])
def test_a_nonsense_horizon_is_clamped(student, monkeypatch, bad):
    import app as nexora
    monkeypatch.setattr(nexora, "_gemini_json",
                        lambda *a, **k: dict({"focus": "all", "courses": []}, **bad))
    body = student.post("/api/study-plan/ask",
                        json={"question": "plan a while"}).get_json()
    assert 3 <= body["days"] <= 60


def test_an_empty_question_is_refused(student):
    assert student.post("/api/study-plan/ask", json={"question": "  "}).status_code == 400


def test_a_quota_failure_points_at_the_buttons(student, monkeypatch):
    """The buttons do the common case without the AI, so a quota outage must
    not leave the feature dead."""
    import app as nexora
    monkeypatch.setattr(nexora, "_gemini_json",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429 quota")))
    res = student.post("/api/study-plan/ask", json={"question": "midterms only"})
    assert res.status_code == 503
    assert "buttons" in res.get_json()["error"]


def test_the_page_offers_both_ways(student):
    body = student.get("/study-plan").get_data(as_text=True)
    assert "Tests, midterms &amp; finals only" in body
    assert 'id="askBox"' in body
    assert "/api/study-plan/ask" in body
