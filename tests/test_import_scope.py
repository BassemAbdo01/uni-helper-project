"""Choosing one term has to mean Nexora holds one term.

Importing only ever added and updated. A student who picked Fall 2026 kept
every course a previous sync had stored -- four years of them on the dashboard,
and the content sync walked all of them, because it reads the course list back
from the server rather than from what was just discovered. From the outside it
looked exactly like the choice being ignored.

The dangerous half of the fix is the deleting, so most of what is pinned here
is what must NOT be deleted: another LMS, the student's own marks, and anything
at all when discovery only managed to read the page.
"""

import json
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BS = "https://learn.school.ca"
CANVAS = "https://school.instructure.com"


def signup(client, email="scope@carleton.ca", password="correct-horse-battery"):
    return client.post("/signup", data={"name": "Scope Student", "email": email,
                                        "password": password}, follow_redirects=False)


@pytest.fixture()
def token(client):
    signup(client)
    res = client.post("/api/sync/extension-key")
    assert res.status_code == 200
    return res.get_json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def course(uid, code, name, term, origin=BS):
    return {"source_uid": f"{origin}/d2l/home/{uid}", "course_code": code,
            "course_name": name, "term": term,
            "brightspace_url": f"{origin}/d2l/home/{uid}"}


FALL = [course("1", "BIOL1103", "Foundations of Biology I", "Fall 2026"),
        course("2", "SYSC2004", "Object-Oriented Software Development", "Fall 2026")]
OLDER = [course("3", "MATH1104", "Linear Algebra", "Winter 2026"),
         course("4", "PHYS1004", "Electromagnetism", "Fall 2025")]


def stored_codes(email="scope@carleton.ca"):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        return sorted(c.course_code for c in
                      nexora.SyncedCourse.query.filter_by(user_id=user.id).all())


def push(client, token, courses, **extra):
    body = {"courses": courses}
    body.update(extra)
    res = client.post("/api/sync/courses", json=body, headers=auth(token))
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def test_choosing_one_term_drops_the_others(client, token):
    push(client, token, FALL + OLDER, replace=True, complete=True, origin=BS)
    assert stored_codes() == ["BIOL1103", "MATH1104", "PHYS1004", "SYSC2004"]

    result = push(client, token, FALL, replace=True, complete=True, origin=BS)
    assert result["removed"] == 2
    assert stored_codes() == ["BIOL1103", "SYSC2004"]


def test_an_incomplete_discovery_never_deletes_anything(client, token):
    """The DOM fallback sees one term tab. Treating that as the whole enrolment
    list would delete every other term the student is actually taking."""
    push(client, token, FALL + OLDER, replace=True, complete=True, origin=BS)
    result = push(client, token, FALL, replace=True, complete=False, origin=BS)
    assert result.get("removed", 0) == 0
    assert len(stored_codes()) == 4


def test_an_ordinary_import_still_only_adds(client, token):
    """Nothing that does not ask to replace may lose a course."""
    push(client, token, FALL + OLDER)
    push(client, token, FALL)
    assert len(stored_codes()) == 4


def test_a_brightspace_sync_leaves_canvas_alone(client, token):
    """Both LMSs write through the same importer, so the only thing separating
    them is the origin the payload came from."""
    push(client, token, FALL + [course("9", "CHEM1101", "Chemistry", "Fall 2026", CANVAS)])
    push(client, token, FALL, replace=True, complete=True, origin=BS)
    assert "CHEM1101" in stored_codes()


def test_the_students_own_marks_survive_a_pruned_course(client, token):
    """Course material is the LMS's and can be re-fetched. Grades and the
    grading policy they confirmed are theirs, and re-importing the term has to
    bring them back to work already entered."""
    import app as nexora
    push(client, token, FALL + OLDER, replace=True, complete=True, origin=BS)
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="scope@carleton.ca").first()
        nexora.db.session.add(nexora.GradeEntry(
            id=uuid4().hex, user_id=user.id, course_full="MATH1104",
            name="Midterm", weight=30, score=80, max_score=100))
        nexora.db.session.add(nexora.CourseOutlineProfile(
            id=uuid4().hex, user_id=user.id, course_full="MATH1104",
            grading_json=json.dumps({"components": []}), pass_mark=50))
        nexora.db.session.commit()

    push(client, token, FALL, replace=True, complete=True, origin=BS)

    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="scope@carleton.ca").first()
        assert nexora.GradeEntry.query.filter_by(user_id=user.id, course_full="MATH1104").count() == 1
        assert nexora.CourseOutlineProfile.query.filter_by(user_id=user.id, course_full="MATH1104").first()


def test_material_from_a_dropped_course_goes_with_it(client, token):
    """A dashboard card is built from tasks as well as courses, so leaving the
    content behind would leave the course on screen after removing it."""
    import app as nexora
    push(client, token, FALL + OLDER, replace=True, complete=True, origin=BS)
    client.post("/api/sync/content", json={"items": [{
        "course": "MATH1104", "title": "Assignment 3", "content_type": "assignment",
        "source_uid": "d2l:3:folder:9", "source_url": f"{BS}/d2l/le/9",
    }]}, headers=auth(token))

    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="scope@carleton.ca").first()
        assert nexora.CourseContent.query.filter_by(user_id=user.id, course_full="MATH1104").count() == 1

    push(client, token, FALL, replace=True, complete=True, origin=BS)

    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="scope@carleton.ca").first()
        assert nexora.CourseContent.query.filter_by(user_id=user.id, course_full="MATH1104").count() == 0
        assert nexora.CourseContent.query.filter_by(user_id=user.id, course_full="BIOL1103").count() == 0


def test_an_empty_payload_cannot_wipe_an_account(client, token):
    """A discovery that found nothing is a failure, not an instruction."""
    push(client, token, FALL, replace=True, complete=True, origin=BS)
    push(client, token, [], replace=True, complete=True, origin=BS)
    assert stored_codes() == ["BIOL1103", "SYSC2004"]


def test_a_missing_origin_cannot_wipe_an_account(client, token):
    push(client, token, FALL + OLDER, replace=True, complete=True, origin=BS)
    result = push(client, token, FALL, replace=True, complete=True)
    assert result.get("removed", 0) == 0
    assert len(stored_codes()) == 4
