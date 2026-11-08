"""B6: a training shell is not a course, and a real course is never a shell.

EHS, AODA, WHMIS, orientation and training shells are real Brightspace org
units and arrive through the same import as a course. They have no outline, no
grades and no deadlines, and on a four-year account they outnumber the five
courses the student is actually taking.

The two failure modes are not symmetric and the whole design follows from it.
Missing a shell leaves one extra card, which is exactly what happens today.
Filing a real course as non-academic hides its outline, its grades, its
deadlines and its study plan. So the signal is POSITIVE with a safe default,
it is split into a tier that is safe with any course code and a tier that is
trusted only where the org unit has none, the row is kept rather than deleted,
the dashboard says what was filed away, and one click puts it back.
"""

import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

ORIGIN = "https://brightspace.carleton.ca"
PASSWORD = "correct-horse-battery"


@pytest.fixture()
def synced(client):
    client.post("/signup", data={"name": "S", "email": f"na{uuid4().hex[:6]}@carleton.ca",
                                 "password": PASSWORD})
    token = client.post("/api/sync/extension-key").get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


def course(code, name, uid=None, term="Fall 2026"):
    return {"source_uid": uid or f"ou-{uuid4().hex[:8]}", "course_code": code,
            "course_name": name, "term": term,
            "brightspace_url": f"{ORIGIN}/d2l/home/1"}


def push(client, headers, courses):
    return client.post("/api/sync/courses",
                       json={"courses": courses, "complete": True}, headers=headers)


def stored():
    with nexora.app.app_context():
        return {c.course_code: bool(c.is_hidden)
                for c in nexora.SyncedCourse.query.all()}


# ------------------------------------------------- the rule, unit level

@pytest.mark.parametrize("name", [
    "WHMIS 2015",
    "Mandatory Safety Training",
    "Welcome to Brightspace",
    "Getting Started with D2L",
    "Brightspace Tutorial",
    "Sandbox",
    "Template Course",
    "Employee Training Module",
])
def test_the_strong_tier_holds_whatever_the_code_says(name):
    """None of these can be the name of a taught course: they are about the
    LMS itself, or they say "training module" in so many words. So a code
    attached to one does not rescue it."""
    assert nexora._looks_non_academic("SAFE1000", name) is True


@pytest.mark.parametrize("name", [
    "EHS Laboratory Safety",
    "AODA Customer Service Standard",
    "Health and Safety Awareness",
    "Occupational Health Orientation",
    "Academic Integrity Tutorial",
    "Respectful Workplace",
    "Workplace Violence Prevention",
    "Student Orientation 2026",
    "New Staff Onboarding",
    "Career Services",
    "Library Resources",
])
def test_the_weak_tier_needs_an_org_unit_with_no_course_code(name):
    """The shape a service shell has. Carleton's "(MS LAP) Math and Stats
    Learning Assistance Program" is a real one -- no code at all, so
    `_normalize_course_code` gives it COURSE<orgUnitId>."""
    assert nexora._looks_non_academic("COURSE1234567", name) is True


@pytest.mark.parametrize("code,name", [
    ("SYSC2004", "Object-Oriented Software Development"),
    ("BIOL1103", "Biology I"),
    ("CHEM1101", "General Chemistry I"),
    # Every one of these matched the FIRST cut of this list, which is why the
    # tiers exist. A whole Environmental Health Sciences department, an
    # occupational health and safety law course, a nursing course called
    # Orientation to Professional Practice.
    ("EHS2100", "Environmental Health Sciences II"),
    ("OHSA3000", "Occupational Health and Safety Law"),
    ("NURS1000", "Orientation to Professional Practice"),
    ("LAWS3305", "Law and Workplace Harassment"),
    ("SOWK4000", "Violence Prevention in Families"),
    ("COOP1000", "Co-op Work Term"),
    ("PHIL2001", "Ethics and Academic Life"),
])
def test_a_real_course_is_never_filed_as_a_shell(code, name):
    assert nexora._looks_non_academic(code, name) is False


def test_a_codeless_org_unit_is_not_hidden_for_being_codeless(client):
    """The absence of a code is a PRECONDITION for the weak tier, never a
    signal on its own. A cross-listed course whose code the parser missed, and
    Carleton's own learning-assistance shell, both land here."""
    assert nexora._looks_non_academic(
        "COURSE999", "MS LAP Math and Stats Learning Assistance Program") is False
    assert nexora._looks_non_academic("COURSE888", "Engineering Design Studio") is False


# ------------------------------------------------- through the import

def test_a_shell_is_stored_and_hidden_not_dropped(client, synced):
    """A HiddenCourse tombstone would mean no row at all -- and with no row
    there is nothing for the student to restore, which is the whole reason
    this is allowed to guess."""
    push(client, synced, [course("SYSC2004", "Object-Oriented Software"),
                          course("COURSE55", "WHMIS Training")])
    rows = stored()
    assert rows == {"SYSC2004": False, "COURSE55": True}, rows


def test_the_dashboard_lists_what_was_filed_away(client, synced):
    push(client, synced, [course("SYSC2004", "Object-Oriented Software"),
                          course("COURSE55", "EHS Laboratory Safety")])
    page = client.get("/dashboard").get_data(as_text=True)
    assert "filed as non-academic" in page
    assert "EHS Laboratory Safety" in page
    assert "Put back" in page


def test_a_dashboard_with_nothing_filed_away_says_nothing(client, synced):
    """A panel that is always there reads as a problem that is always there."""
    push(client, synced, [course("SYSC2004", "Object-Oriented Software")])
    assert "filed as non-academic" not in client.get("/dashboard").get_data(as_text=True)


def test_a_shell_is_off_the_course_list_and_out_of_the_count(client, synced):
    push(client, synced, [course("SYSC2004", "Object-Oriented Software"),
                          course("COURSE55", "WHMIS Training")])
    page = client.get("/dashboard").get_data(as_text=True)
    assert "Object-Oriented Software" in page
    # The card itself is gone, not merely restyled.
    assert 'data-course="COURSE55"><a' not in page.replace("\n", "").replace(" ", "")


# ------------------------------------------------- the guess is reversible

def test_putting_one_back_survives_every_later_sync(client, synced):
    """The `sync_state='dismissed'` rule, the other way up: a decision the
    student made must not be undone by the next import. `_looks_non_academic`
    runs only when the row is CREATED, so re-importing the same shell cannot
    re-hide it."""
    payload = [course("COURSE55", "Student Orientation 2026", uid="ou-fixed")]
    push(client, synced, payload)
    assert stored()["COURSE55"] is True

    assert client.post("/api/course/COURSE55/visibility",
                       json={"action": "restore"}).status_code == 200
    assert stored()["COURSE55"] is False

    for _ in range(3):
        push(client, synced, payload)
    assert stored()["COURSE55"] is False, "a later sync re-hid a course the student kept"
    assert "Student Orientation 2026" in client.get("/dashboard").get_data(as_text=True)


def test_nothing_is_deleted_to_hide_a_shell(client, synced):
    """Restore has to produce the course with its content, not an empty one.
    Hiding is a flag on the row; `/api/course/<c>/visibility` with
    action=hide is the destructive path and this is not it."""
    push(client, synced, [course("COURSE55", "WHMIS Training", uid="ou-whmis")])
    client.post("/api/sync/content", headers=synced, json={
        "course": "COURSE55",
        "items": [{"content_type": "page", "title": "Module 1", "source_uid": "w1"}]})
    with nexora.app.app_context():
        assert nexora.CourseContent.query.filter_by(course_full="COURSE55").count() == 1

    client.post("/api/course/COURSE55/visibility", json={"action": "restore"})
    with nexora.app.app_context():
        assert nexora.CourseContent.query.filter_by(course_full="COURSE55").count() == 1


def test_a_course_the_student_removed_is_not_offered_back_here(client, synced):
    """Its content, grades and tasks were deleted by the remove. Listing it
    beside the shells would promise a restore that cannot return any of that."""
    push(client, synced, [course("SYSC2004", "Object-Oriented Software"),
                          course("COURSE55", "WHMIS Training")])
    client.post("/api/course/SYSC2004/visibility", json={"action": "hide"})
    page = client.get("/dashboard").get_data(as_text=True)
    assert "WHMIS Training" in page
    assert "Object-Oriented Software" not in page
