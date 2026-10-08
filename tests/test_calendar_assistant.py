"""The calendar assistant acts on real rows, so what it may touch is the point.

The model chooses WHAT to do and to WHICH entries, picking ids out of a list the
server gave it. The server resolves every id against the student's own rows, both
when it builds the preview and again when the confirmed plan comes back through
the browser. A plan is not trusted for having been previewed.
"""
from datetime import date
from uuid import uuid4

import app as nexora


TODAY = date(2026, 10, 8)
PASSWORD = "nexorademo1"


def _user(client, email=None):
    """A second account, made directly.

    Signing up through the route a second time does nothing while the test
    client is already signed in, so both calls returned the SAME id and the
    cross-account tests were quietly checking that a user cannot act on their
    own rows. Which they can.
    """
    with nexora.app.app_context():
        u = nexora.User(name="S", email=email or f"c{uuid4().hex[:7]}@carleton.ca")
        u.password_hash = nexora.generate_password_hash(PASSWORD)
        nexora.db.session.add(u)
        nexora.db.session.commit()
        return u.id


def _task(user_id, title="Midterm", due="2026-10-20", source="manual", group=None, **kw):
    with nexora.app.app_context():
        t = nexora.Task(id=uuid4().hex, user_id=user_id, title=title, base_title=title,
                        group_id=group or ("g-" + uuid4().hex[:6]), due_raw=due,
                        course_full="SYSC2004", course_prefix="SYSC2004",
                        kind="exam", is_deadline=True, source=source,
                        sync_state="active", created_at="2026-10-01 00:00:00", **kw)
        nexora.db.session.add(t)
        nexora.db.session.commit()
        return t.id


def _row(task_id):
    with nexora.app.app_context():
        return nexora.Task.query.filter_by(id=task_id).first()


# --------------------------------------------------------------------------
# what it may touch
# --------------------------------------------------------------------------

def test_an_id_belonging_to_someone_else_is_not_planned(client):
    mine = _user(client)
    theirs_id = _task(_user(client, "other@carleton.ca"), title="Their exam")
    with nexora.app.app_context():
        changes, notes = nexora._calendar_plan(
            [{"op": "delete", "ids": [theirs_id]}], mine, TODAY)
    assert changes == []
    assert notes and "no longer exist" in notes[0]


def test_an_id_belonging_to_someone_else_is_not_applied_either(client):
    """The plan makes a round trip through the browser, so the preview having
    been built correctly proves nothing about what comes back."""
    mine = _user(client)
    theirs_id = _task(_user(client, "other2@carleton.ca"), title="Their exam")
    with nexora.app.app_context():
        done = nexora._calendar_apply(
            [{"op": "delete", "id": theirs_id}], mine)
    assert done["deleted"] == 0
    assert _row(theirs_id) is not None, "their event must still be there"


def test_an_invented_id_simply_does_not_resolve(client):
    mine = _user(client)
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan(
            [{"op": "move", "ids": ["made-up-id"], "to": "2026-11-02"}], mine, TODAY)
    assert changes == []


# --------------------------------------------------------------------------
# the operations
# --------------------------------------------------------------------------

def test_move_changes_the_date_and_nothing_else(client):
    uid = _user(client)
    tid = _task(uid, title="Midterm", due="2026-10-20")
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan(
            [{"op": "move", "ids": [tid], "to": "2026-11-02"}], uid, TODAY)
        assert changes[0]["label"] == 'Move “Midterm” from 2026-10-20 to 2026-11-02'
        nexora._calendar_apply(changes, uid)
    row = _row(tid)
    assert row.due_raw == "2026-11-02"
    assert row.title == "Midterm"


def test_move_by_days_is_counted_from_the_entry_not_from_today(client):
    uid = _user(client)
    tid = _task(uid, due="2026-10-20")
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan(
            [{"op": "move", "ids": [tid], "by_days": 7}], uid, TODAY)
        nexora._calendar_apply(changes, uid)
    assert _row(tid).due_raw == "2026-10-27"


def test_retime_sets_a_time_and_refuses_one_that_is_not_a_time(client):
    uid = _user(client)
    tid = _task(uid)
    with nexora.app.app_context():
        good, _ = nexora._calendar_plan([{"op": "retime", "ids": [tid], "time": "09:00"}], uid, TODAY)
        nexora._calendar_apply(good, uid)
        bad, notes = nexora._calendar_plan(
            [{"op": "retime", "ids": [tid], "time": "half four"}], uid, TODAY)
    assert _row(tid).due_time == "09:00"
    assert bad == [] and any("24-hour" in n for n in notes)


def test_rename_changes_the_title(client):
    uid = _user(client)
    tid = _task(uid, title="Midterm")
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan(
            [{"op": "rename", "ids": [tid], "title": "Midterm (rescheduled)"}], uid, TODAY)
        nexora._calendar_apply(changes, uid)
    assert _row(tid).title == "Midterm (rescheduled)"


def test_add_still_goes_through_the_date_arithmetic(client):
    uid = _user(client)
    with nexora.app.app_context():
        changes, notes = nexora._calendar_plan([{
            "op": "add", "title": "Test", "course": "SYSC2004", "kind": "exam",
            "start": "2026-10-13", "repeat": "biweekly", "count": 3,
            "skip_ranges": [["2026-10-23", "2026-10-28"]]}], uid, TODAY)
    assert [c["date"] for c in changes] == ["2026-10-13", "2026-11-03", "2026-11-17"]
    assert any("moved" in n for n in notes)


# --------------------------------------------------------------------------
# deleting, which has rules of its own
# --------------------------------------------------------------------------

def test_deleting_an_import_dismisses_it_so_the_next_sync_does_not_undo_it(client):
    uid = _user(client)
    tid = _task(uid, source="ics")
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan([{"op": "delete", "ids": [tid]}], uid, TODAY)
        nexora._calendar_apply(changes, uid)
    row = _row(tid)
    assert row is not None, "an imported row is kept"
    assert row.sync_state == "dismissed"


def test_deleting_a_manual_event_removes_it(client):
    uid = _user(client)
    tid = _task(uid, source="manual")
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan([{"op": "delete", "ids": [tid]}], uid, TODAY)
        nexora._calendar_apply(changes, uid)
    assert _row(tid) is None


def test_deleting_acts_on_the_whole_group_not_one_row(client):
    """An imported assignment can be three rows -- opens, due, closes -- sharing
    a group. Deleting only the clicked row let the entry reappear on its next
    date, which is why /api/events DELETE works on the group."""
    uid = _user(client)
    opens = _task(uid, title="A1 (opens)", due="2026-10-18", source="ics", group="shared")
    due = _task(uid, title="A1 (due)", due="2026-10-20", source="ics", group="shared")
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan([{"op": "delete", "ids": [due]}], uid, TODAY)
        nexora._calendar_apply(changes, uid)
    assert _row(opens).sync_state == "dismissed"
    assert _row(due).sync_state == "dismissed"


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

def test_the_change_cap_is_loud(client):
    uid = _user(client)
    with nexora.app.app_context():
        changes, notes = nexora._calendar_plan([{
            "op": "add", "title": "Daily", "start": "2026-10-09",
            "repeat": "weekly", "count": nexora.RECURRENCE_CAP}] * 4, uid, TODAY)
    assert len(changes) == nexora.CALENDAR_CHANGE_CAP
    assert any("changes" in n for n in notes)


def test_rubbish_operations_are_ignored_rather_than_crashing(client):
    uid = _user(client)
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan(
            ["nonsense", 5, {}, {"op": "explode"}, None], uid, TODAY)
    assert changes == []


def test_operations_that_are_not_a_list_are_refused(client):
    uid = _user(client)
    with nexora.app.app_context():
        changes, notes = nexora._calendar_plan({"op": "delete"}, uid, TODAY)
    assert changes == [] and notes


# --------------------------------------------------------------------------
# Selecting by FILTER, not by enumerated ids
#
# Every operation used to require the model to name ids out of a listing capped
# at 180 rows. On an account with thirty-five courses that listing never reached
# December, and "the exams of ELEC2501" is a filter rather than a list -- so the
# model refused outright: "renaming requires specific entry IDs".
# --------------------------------------------------------------------------

def _series(uid):
    return [_task(uid, title=f"Elec Lab {n}", due=d, source="manual")
            for n, d in ((3, "2026-11-03"), (4, "2026-12-01"), (5, "2026-12-15"))]


def test_a_filter_selects_every_matching_entry_with_no_ids_at_all(client):
    uid = _user(client)
    _series(uid)
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan([{
            "op": "rename", "match": {"course": "SYSC2004", "kind": "exam"},
            "replace": {"from": "Elec Lab", "to": "Elec Test"}}], uid, TODAY)
    assert [c["title"] for c in changes] == ["Elec Test 3", "Elec Test 4", "Elec Test 5"]


def test_renaming_by_substitution_keeps_the_numbering(client):
    """Setting one title across a series would collapse it into identical rows."""
    uid = _user(client)
    _series(uid)
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan([{
            "op": "rename", "match": {"title_contains": "Elec Lab"},
            "replace": {"from": "Elec Lab", "to": "Midterm"}}], uid, TODAY)
        nexora._calendar_apply(changes, uid)
        titles = sorted(t.title for t in nexora.Task.query.filter_by(user_id=uid).all())
    assert titles == ["Midterm 3", "Midterm 4", "Midterm 5"]


def test_one_new_name_across_many_entries_is_refused_with_a_reason(client):
    uid = _user(client)
    _series(uid)
    with nexora.app.app_context():
        changes, notes = nexora._calendar_plan([{
            "op": "rename", "match": {"title_contains": "Elec Lab"},
            "title_new": "Exam"}], uid, TODAY)
    assert changes == []
    assert any("identical" in n for n in notes)


def test_a_filter_naming_nothing_matches_nothing(client):
    """"Delete everything" with an empty filter must not clear the calendar."""
    uid = _user(client)
    _series(uid)
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan([{"op": "delete", "match": {}}], uid, TODAY)
        assert changes == []
        changes, _ = nexora._calendar_plan([{"op": "delete"}], uid, TODAY)
    assert changes == []


def test_a_filter_cannot_reach_another_students_entries(client):
    uid = _user(client)
    other = _user(client, "filter-other@carleton.ca")
    _task(other, title="Elec Lab 9", due="2026-11-03")
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan([{
            "op": "delete", "match": {"title_contains": "Elec Lab"}}], uid, TODAY)
    assert changes == []


def test_moving_many_entries_onto_one_date_is_refused_not_stacked(client):
    uid = _user(client)
    _series(uid)
    with nexora.app.app_context():
        changes, notes = nexora._calendar_plan([{
            "op": "move", "match": {"title_contains": "Elec Lab"},
            "to": "2026-12-20"}], uid, TODAY)
    assert changes == []
    assert any("one date" in n for n in notes)


def test_a_later_operation_sees_a_rename_made_by_an_earlier_one(client):
    """The model chains: it renames, then matches the NEW name to move it. Against
    the stored titles that found nothing and half the request was dropped."""
    uid = _user(client)
    _series(uid)
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan([
            {"op": "rename", "match": {"title_contains": "Elec Lab 3"},
             "title_new": "Elec Final"},
            {"op": "move", "match": {"title_contains": "Elec Final"},
             "to": "2026-12-20"},
        ], uid, TODAY)
        nexora._calendar_apply(changes, uid)
        row = nexora.Task.query.filter_by(user_id=uid, title="Elec Final").first()
    assert [c["op"] for c in changes] == ["rename", "move"]
    assert row is not None and row.due_raw == "2026-12-20"


def test_one_operation_carrying_two_intents_does_both(client):
    """The model also collapses them into a single op; dispatching on the op
    name alone silently dropped the second half."""
    uid = _user(client)
    tid = _task(uid, title="Elec Lab 3", due="2026-11-03")
    with nexora.app.app_context():
        changes, _ = nexora._calendar_plan([{
            "op": "rename", "ids": [tid], "title_new": "Elec Final",
            "to": "2026-12-20"}], uid, TODAY)
    assert sorted(c["op"] for c in changes) == ["move", "rename"]
