"""37 courses in the extension, 11 on the dashboard.

Reported 2026-09-08 with a screenshot of each. `_import_courses` treated a
course CODE as an identity: after failing to match on `source_uid` it fell back
to matching on the code, so every offering of the same code collapsed into one
row and whichever was imported last won the name and the term.

The cost was not only the missing cards. The content sync walks the course list
it reads back from the server, so a course with no row is never scraped -- its
outline, materials and announcements never arrive, and nothing anywhere says a
course went missing.

An org unit is the identity. A code is a label, and `_prune_courses` already
said so ("a code can be shared by two org units").
"""

import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"
ORIGIN = "https://brightspace.carleton.ca"


@pytest.fixture()
def synced(client):
    client.post("/signup", data={"name": "S", "email": f"many{uuid4().hex[:6]}@carleton.ca",
                                 "password": PASSWORD})
    token = client.post("/api/sync/extension-key").get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


def offering(uid, code, name, term):
    return {"source_uid": uid, "course_code": code, "course_name": name,
            "term": term, "brightspace_url": f"{ORIGIN}/d2l/home/{uid}"}


def send(client, headers, courses, **extra):
    body = {"courses": courses, "origin": ORIGIN}
    body.update(extra)
    return client.post("/api/sync/courses", json=body, headers=headers)


def stored():
    import app as nexora
    with nexora.app.app_context():
        return nexora.SyncedCourse.query.all()


# ------------------------------------------------- every offering is a row

def test_the_same_code_in_two_terms_is_two_courses(synced, client):
    """A student who took ECOR1031 in first year and again this year has two
    enrolments. One row meant one of them was thrown away."""
    send(client, synced, [
        offering("111", "ECOR1031", "Programming and Data Management", "Fall 2024"),
        offering("222", "ECOR1031", "Programming and Data Management", "Winter 2026"),
    ])
    rows = stored()
    assert len(rows) == 2
    assert {r.term for r in rows} == {"Fall 2024", "Winter 2026"}


def test_nothing_is_lost_from_a_full_enrolment_list(synced, client):
    """The reported shape: many offerings, several sharing a code."""
    courses = [
        offering("1", "SYSC2004A", "Object Oriented Sofware Dvlpmt", "Fall 2026"),
        offering("2", "SYSC2310", "Intro to Digital Systems", "Fall 2026"),
        offering("3", "BIOL1103", "Foundations of Biology I", "Fall 2026"),
        offering("4", "MATH1005B", "Diff Eqs and Series", "Fall 2026"),
        offering("5", "CCDP2100C", "Communication Skills", "Fall 2026"),
        offering("6", "ELEC2501B", "Circuits and Signals", "Fall 2026"),
        offering("7", "COOP1000A", "Co op", "Fall 2026"),
        offering("8", "ECOR1031", "Programming", "Winter 2026"),
        offering("9", "ECOR1031", "Programming", "Fall 2024"),
        offering("10", "PHYS1004", "Electromagnetism", "Winter 2026"),
    ]
    result = send(client, synced, courses).get_json()
    assert len(stored()) == 10
    assert result["found"] == 10
    assert result["stored"] == 10


def test_a_sync_reports_what_arrived_and_what_was_kept(synced, client):
    """37 sat in the extension for weeks beside a dashboard showing 11, because
    only one of the two numbers was ever reported."""
    result = send(client, synced, [
        offering("1", "BIOL1103", "Biology", "Fall 2026"),
        offering("2", "SYSC2310", "Digital", "Fall 2026"),
    ]).get_json()
    assert result["found"] == 2
    assert result["stored"] == 2


def test_re_syncing_the_same_list_creates_nothing_new(synced, client):
    courses = [offering("1", "BIOL1103", "Biology", "Fall 2026"),
               offering("2", "ECOR1031", "Programming", "Winter 2026")]
    send(client, synced, courses)
    second = send(client, synced, courses).get_json()
    assert len(stored()) == 2
    assert second["added"] == 0
    assert second["unchanged"] == 2


def test_an_offering_that_is_gone_is_pruned_and_the_rest_survive(synced, client):
    send(client, synced, [offering("1", "BIOL1103", "Biology", "Fall 2026"),
                          offering("2", "ECOR1031", "Programming", "Winter 2026")],
         replace=True, complete=True)
    assert len(stored()) == 2
    send(client, synced, [offering("1", "BIOL1103", "Biology", "Fall 2026")],
         replace=True, complete=True)
    rows = stored()
    assert [r.source_uid for r in rows] == ["1"]


# ------------------------------------------- one card, owned by the newest

def test_two_offerings_show_as_one_course_named_by_the_newest_term(synced, client):
    """A student looking at ECOR1031 means the one they are in now. Letting
    whichever row was written last win put a 2024 name on a 2026 course."""
    send(client, synced, [
        offering("222", "ECOR1031", "Programming and Data Management", "Winter 2026"),
        offering("111", "ECOR1031", "Old Title From First Year", "Fall 2024"),
    ])
    body = client.get("/dashboard").get_data(as_text=True)
    assert "Programming and Data Management" in body
    assert "Old Title From First Year" not in body


def test_every_imported_course_reaches_the_dashboard(synced, client):
    codes = ["SYSC2004A", "SYSC2310", "BIOL1103", "MATH1005B",
             "CCDP2100C", "ELEC2501B", "COOP1000A"]
    send(client, synced, [offering(str(i), code, f"Course {code}", "Fall 2026")
                          for i, code in enumerate(codes)])
    body = client.get("/dashboard").get_data(as_text=True)
    for code in codes:
        assert code in body, f"{code} never reached the dashboard"


def test_the_extension_is_told_about_every_offering_to_scrape(synced, client):
    """The content sync walks this list. A course missing from it is a course
    whose outline and materials never arrive."""
    send(client, synced, [
        offering("1", "ECOR1031", "Programming", "Winter 2026"),
        offering("2", "ECOR1031", "Programming", "Fall 2024"),
        offering("3", "BIOL1103", "Biology", "Fall 2026"),
    ])
    payload = client.get("/api/sync/status", headers=synced).get_json()
    assert payload["course_count"] == 3
    assert {c["source_uid"] for c in payload["courses"]} == {"1", "2", "3"}


# ------------------------------- one entry per course, named by the newest

def test_the_grade_calculator_lists_a_repeated_course_once_in_its_current_term(synced, client):
    """Found while auditing the change above: storing both offerings made a
    dict-by-code pick whichever the query yielded last, so ECOR1031 -- taken in
    first year and again this year -- appeared under Fall 2025 with its old
    title, on the page a student uses to enter THIS term's marks."""
    send(client, synced, [
        offering("111", "ECOR1031", "Old Title From First Year", "Fall 2025"),
        offering("222", "ECOR1031", "Programming and Data Management", "Winter 2026"),
    ])
    body = client.get("/grade-calculator").get_data(as_text=True)
    assert body.count('value="ECOR1031"') == 1
    assert "Fall 2025" not in body
    assert "Programming and Data Management" in body


def test_the_study_room_lists_it_once_too(synced, client):
    send(client, synced, [
        offering("111", "ECOR1031", "Old Title From First Year", "Fall 2025"),
        offering("222", "ECOR1031", "Programming and Data Management", "Winter 2026"),
    ])
    body = client.get("/study-room").get_data(as_text=True)
    assert "Old Title From First Year" not in body


def test_the_newest_offering_represents_the_course_everywhere():
    """One definition. Two screens disagreeing about which offering a course
    code means is the same class of bug as two disagreeing about a grade."""
    import app as nexora

    class Row:
        def __init__(self, code, term):
            self.course_code, self.term = code, term

    rows = [Row("ECOR1031", "Fall 2025"), Row("ECOR1031", "Winter 2026"),
            Row("BIOL1103", "Fall 2026")]
    best = nexora._newest_offering_per_code(rows)
    assert set(best) == {"ECOR1031", "BIOL1103"}
    assert best["ECOR1031"].term == "Winter 2026"
    # order must not decide it
    assert nexora._newest_offering_per_code(list(reversed(rows)))["ECOR1031"].term == "Winter 2026"


def test_a_deadline_uses_the_course_on_the_envelope_when_the_item_omits_it(synced, client):
    """/api/sync/content takes {"course": X, "items": [...]}. Content honoured
    it and deadlines did not, so a caller filling only the envelope got every
    deadline filed under OTHER -- which costs the outline's weights, the course
    standing, and the plan's ability to say which course a block is for."""
    import app as nexora
    client.post("/api/sync/content", json={"course": "SYSC2004A", "items": [
        {"content_type": "assignment", "title": "Assignment 1", "source_uid": "a1",
         "due_at": "2026-10-07T02:00:00Z"}]}, headers=synced)
    with nexora.app.app_context():
        task = nexora.Task.query.filter_by(title="Assignment 1").first()
        assert task is not None
        assert task.course_full == "SYSC2004A"


def test_a_course_with_no_real_code_does_not_print_a_made_up_one(synced, client):
    """An org unit with no code in its name gets `COURSE<orgUnitId>` from the
    extension's parser -- Carleton's "(MS LAP) Math and Stats Learning
    Assistance Program" is a real one. The code stays, because it is the join
    key for content, deadlines and grades; printing it on a card tells a
    student nothing and reads as a fault."""
    import app as nexora
    assert nexora._display_code("COURSE1006") == ""
    assert nexora._display_code("SYSC2004A") == "SYSC2004A"

    send(client, synced, [
        offering("1006", "COURSE1006", "Math and Stats Learning Assistance Program", "Fall 2026"),
        offering("1", "BIOL1103", "Foundations of Biology I", "Fall 2026"),
    ])
    body = client.get("/dashboard").get_data(as_text=True)
    assert "Math and Stats Learning Assistance Program" in body
    assert ">COURSE1006<" not in body
    assert ">BIOL1103<" in body


def test_a_body_nobody_reads_is_not_stored_in_full(synced, client):
    """Render suspended the production database for exceeding its storage,
    and course_content.content_text was almost all of it: a flat 120,000
    characters a row, across every item in every course in every term, and
    nothing ever removed any of it.

    An outline is analysed and quoted, a lecture is summarised. Nothing
    reads the body of an announcement -- the dashboard shows its title and
    its date. So the cap follows what the body is for.
    """
    import app as nexora
    big = "x" * 200_000
    send(client, synced, [offering("1", "ECOR1031", "Programming", "Fall 2026")])
    r = client.post("/api/sync/content", headers=synced, json={"course": "ECOR1031", "items": [
        {"content_type": "outline", "title": "Outline", "source_uid": "o1", "content_text": big},
        {"content_type": "lecture", "title": "Lecture 1", "source_uid": "l1", "content_text": big},
        {"content_type": "announcement", "title": "Notice", "source_uid": "n1", "content_text": big},
        {"content_type": "assignment", "title": "Lab 1", "source_uid": "a1", "content_text": big},
        {"content_type": "lecture", "title": "Lecture 2", "source_uid": "l2", "content_text": "short body"},
    ]})
    assert r.status_code == 200, r.get_json()
    with nexora.app.app_context():
        kept = {r.source_uid: len(r.content_text or "")
                for r in nexora.CourseContent.query.filter_by(course_full="ECOR1031").all()}
    assert kept["o1"] == 120_000 and kept["l1"] == 120_000   # outline and lecture: read in full
    assert kept["n1"] == 12_000 and kept["a1"] == 12_000     # announcement and assignment: bounded

    # Every cap sits above what any feature reads, so the store is never the
    # binding constraint -- the tutor reads 45,000 characters of a matched
    # item, and study tools read 120,000.
    assert min(nexora._CONTENT_TEXT_CAP[k] for k in ("page", "practice", "resource")) > 45_000

    # And a trimmed body says so. Nothing re-fetches a body of 12,000
    # characters -- that only happens below ~120 -- so a silent trim would be
    # read as the whole item by the summariser and by the student.
    with nexora.app.app_context():
        note = nexora.CourseContent.query.filter_by(course_full="ECOR1031", source_uid="n1").first()
        whole = nexora.CourseContent.query.filter_by(course_full="ECOR1031", source_uid="l2").first()
    assert note.content_text.endswith("[Nexora stored only the first part of this item.]")
    assert whole.content_text == "short body"     # nothing trimmed, so nothing claimed
