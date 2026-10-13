"""Which course a calendar entry belongs to, and being able to fix it.

Reported with a screenshot: tests added from a schedule landed under OTHER, and
asking the assistant "change the course names for Test 2,3,4 and 5 from OTHER to
ELEC2501" produced the sentence *"I will update the course for Test 2, Test 3,
Test 4, and Test 5 from OTHER to ELEC2501"* directly above *"Nexora found nothing
to change"* -- a confident promise and a flat contradiction, on one screen.

The model had understood perfectly. `_calendar_plan` accepted add, move, delete,
retime and rename, so there was simply no verb for it, and an op the server did
not recognise was dropped with `continue` -- no note, no error, nothing.

OTHER is not a cosmetic problem. Every feature downstream joins on the course
code: the outline's weights, `_course_standing`, which course the study plan says
a block is for. An entry stranded there is invisible to all of them.
"""
from datetime import date
from uuid import uuid4

import app as nexora

TODAY = date(2026, 10, 12)
PASSWORD = "nexorademo1"


def _user():
    with nexora.app.app_context():
        u = nexora.User(name="S", email=f"cc{uuid4().hex[:7]}@carleton.ca")
        u.password_hash = nexora.generate_password_hash(PASSWORD)
        nexora.db.session.add(u)
        nexora.db.session.commit()
        return u.id


def _course(user_id, code, term="Fall 2026"):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user_id, source_uid="ou-" + uuid4().hex[:6],
            course_code=code, course_name=code + " course", term=term))
        nexora.db.session.commit()


def _task(user_id, title="Test 2", course="OTHER", due="2026-11-09",
          group=None, source="manual"):
    with nexora.app.app_context():
        t = nexora.Task(id=uuid4().hex, user_id=user_id, title=title, base_title=title,
                        group_id=group or nexora.make_group_id(course, title),
                        due_raw=due, course_full=course, course_prefix=course,
                        kind="exam", is_deadline=True, source=source,
                        sync_state="active", created_at="2026-10-01 00:00:00")
        nexora.db.session.add(t)
        nexora.db.session.commit()
        return t.id


def _row(task_id):
    with nexora.app.app_context():
        return nexora.Task.query.filter_by(id=task_id).first()


# --------------------------------------------------------------------------
# Resolving what the student typed against what they actually have
# --------------------------------------------------------------------------

def test_a_loose_code_becomes_the_students_own(client):
    """Typing "ELEC2501" when the course is ELEC2501B and storing it verbatim
    moves the entry off OTHER and onto a code that still matches no course they
    have. The label changes and nothing else does, which reads as fixed."""
    uid = _user()
    _course(uid, "ELEC2501B")
    with nexora.app.app_context():
        assert nexora._resolve_known_course("ELEC2501", uid) == "ELEC2501B"
        assert nexora._resolve_known_course("elec", uid) == "ELEC2501B"
        assert nexora._resolve_known_course("ELEC2501B", uid) == "ELEC2501B"


def test_two_offerings_that_both_match_are_left_alone(client):
    """Picking a section for someone is worse than keeping what they typed."""
    uid = _user()
    _course(uid, "ELEC2501A")
    _course(uid, "ELEC2501B")
    with nexora.app.app_context():
        assert nexora._resolve_known_course("ELEC2501", uid) == "ELEC2501"


def test_a_course_they_do_not_have_is_kept_not_invented(client):
    uid = _user()
    _course(uid, "SYSC2310")
    with nexora.app.app_context():
        assert nexora._resolve_known_course("MATH1005", uid) == "MATH1005"


def test_nothing_in_means_nothing_out(client):
    uid = _user()
    with nexora.app.app_context():
        assert nexora._resolve_known_course("", uid) == ""
        assert nexora._resolve_known_course("OTHER", uid) == ""


# --------------------------------------------------------------------------
# Selecting the entries. "not given" and "literally OTHER" are different.
# --------------------------------------------------------------------------

def test_other_can_be_matched_on_purpose(client):
    """_normalize_course_code("") returns "OTHER", so the guard against an unset
    field also threw away a deliberate one -- and OTHER is the single course a
    student ever asks about by name, because it is where everything unnamed
    lands."""
    uid = _user()
    _task(uid, title="Test 2", course="OTHER")
    _task(uid, title="Midterm", course="SYSC2310")
    with nexora.app.app_context():
        rows, _ = nexora._calendar_match({"match": {"course": "OTHER"}}, uid)
    assert [r.title for r in rows] == ["Test 2"]


def test_an_unset_course_still_filters_nothing(client):
    """A filter that names nothing must match nothing -- "delete my events" with
    an empty filter would otherwise clear the calendar."""
    uid = _user()
    _task(uid, title="Test 2", course="OTHER")
    with nexora.app.app_context():
        rows, _ = nexora._calendar_match({"match": {"course": ""}}, uid)
    assert rows == []


# --------------------------------------------------------------------------
# The operation
# --------------------------------------------------------------------------

def test_the_reported_request_produces_changes(client):
    uid = _user()
    _course(uid, "ELEC2501B")
    for n in (2, 3, 4, 5):
        _task(uid, title=f"Test {n}", course="OTHER", due=f"2026-11-0{n}")
    with nexora.app.app_context():
        changes, notes = nexora._calendar_plan(
            [{"op": "recourse", "course_new": "ELEC2501",
              "match": {"course": "OTHER", "title_contains": "Test"}}], uid, TODAY)
    assert len(changes) == 4
    assert {c["course"] for c in changes} == {"ELEC2501B"}
    assert all(c["op"] == "recourse" for c in changes)
    assert "OTHER to ELEC2501B" in changes[0]["label"]


def test_applying_it_writes_the_course_and_the_prefix(client):
    uid = _user()
    _course(uid, "ELEC2501B")
    tid = _task(uid, title="Test 2", course="OTHER")
    with nexora.app.app_context():
        done = nexora._calendar_apply(
            [{"op": "recourse", "id": tid, "course": "ELEC2501B"}], uid)
    assert done["recoursed"] == 1
    row = _row(tid)
    assert row.course_full == "ELEC2501B"
    assert row.course_prefix == "ELEC2501B"


def test_the_group_id_follows_the_course(client):
    """group_id is derived from the course, so a stale one leaves the entry
    grouped under the course it just left."""
    uid = _user()
    _course(uid, "ELEC2501B")
    tid = _task(uid, title="Test 2", course="OTHER")
    with nexora.app.app_context():
        nexora._calendar_apply([{"op": "recourse", "id": tid, "course": "ELEC2501B"}], uid)
        row = nexora.Task.query.filter_by(id=tid).first()
        assert row.group_id == nexora.make_group_id("ELEC2501B", row.base_title)


def test_every_row_of_one_entry_moves_together(client):
    """An imported assignment is several Tasks sharing a group_id. Moving one of
    them leaves the entry split across two courses and drawn twice."""
    uid = _user()
    _course(uid, "ELEC2501B")
    group = "g-" + uuid4().hex[:6]
    a = _task(uid, title="Lab 1", course="OTHER", due="2026-11-02",
              group=group, source="ext")
    b = _task(uid, title="Lab 1", course="OTHER", due="2026-11-05",
              group=group, source="ext")
    with nexora.app.app_context():
        nexora._calendar_apply([{"op": "recourse", "id": a, "course": "ELEC2501B"}], uid)
    assert _row(a).course_full == "ELEC2501B"
    assert _row(b).course_full == "ELEC2501B", "the sibling row was left behind"


def test_apply_re_resolves_rather_than_trusting_the_browser(client):
    """The plan makes a round trip, so the code that comes back is re-resolved
    against the student's own courses exactly as it was on the way out."""
    uid = _user()
    _course(uid, "ELEC2501B")
    tid = _task(uid, title="Test 2", course="OTHER")
    with nexora.app.app_context():
        nexora._calendar_apply([{"op": "recourse", "id": tid, "course": "ELEC2501"}], uid)
    assert _row(tid).course_full == "ELEC2501B"


def test_someone_elses_entry_is_not_recoursed(client):
    mine, theirs = _user(), _user()
    tid = _task(theirs, title="Their test", course="OTHER")
    with nexora.app.app_context():
        done = nexora._calendar_apply(
            [{"op": "recourse", "id": tid, "course": "ELEC2501B"}], mine)
    assert done["recoursed"] == 0
    assert _row(tid).course_full == "OTHER"


def test_an_entry_already_on_that_course_is_not_offered(client):
    """A change that changes nothing is noise in a list the student has to read
    before confirming."""
    uid = _user()
    _course(uid, "ELEC2501B")
    _task(uid, title="Test 2", course="ELEC2501B")
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan(
            [{"op": "recourse", "course_new": "ELEC2501B",
              "match": {"title_contains": "Test"}}], uid, TODAY)
    assert changes == []


def test_naming_no_destination_says_so(client):
    uid = _user()
    _task(uid, title="Test 2", course="OTHER")
    with nexora.app.app_context():
        changes, notes = nexora._calendar_plan(
            [{"op": "recourse", "match": {"title_contains": "Test"}}], uid, TODAY)
    assert changes == []
    assert any("did not name a course" in n for n in notes)


# --------------------------------------------------------------------------
# An op the server cannot do must say so
# --------------------------------------------------------------------------

def test_an_unknown_operation_is_reported_not_swallowed(client):
    """This is what turned a missing feature into "it's not thinking": the
    model promised the change in `understood`, the op was dropped with a bare
    `continue`, and the student read "Nexora found nothing to change"."""
    uid = _user()
    _task(uid, title="Test 2", course="OTHER")
    with nexora.app.app_context():
        changes, notes = nexora._calendar_plan(
            [{"op": "recolour", "match": {"title_contains": "Test"}}], uid, TODAY)
    assert changes == []
    assert any("cannot do this" in n and "recolour" in n for n in notes)


def test_the_note_lists_what_it_CAN_do(client):
    uid = _user()
    with nexora.app.app_context():
        _, notes = nexora._calendar_plan([{"op": "teleport"}], uid, TODAY)
    joined = " ".join(notes)
    for verb in ("add", "move", "retime", "rename", "change the course", "delete"):
        assert verb in joined


# --------------------------------------------------------------------------
# Seeing the course before agreeing to it
# --------------------------------------------------------------------------

def test_an_added_entry_shows_which_course_it_lands_on(client):
    """A series of tests was confirmed and only afterwards found to have been
    filed under OTHER, because the add label never said where they were going.
    The image path already named it; this one did not."""
    uid = _user()
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan(
            [{"op": "add", "title": "Test 1", "kind": "exam", "start": "2026-11-02"}],
            uid, TODAY)
    assert changes
    assert "OTHER" in changes[0]["label"]
