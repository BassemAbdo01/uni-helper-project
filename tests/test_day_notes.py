"""The student's own list: what they decided to do today.

Deliberately not a Task. The dashboard queue, the calendar, the .ics feed and
the study planner all read Tasks, so "email the prof" in that table would be
exported to Google Calendar as a deadline and have study hours allocated to it.
"""
from datetime import timedelta
from uuid import uuid4

import app as nexora


def _account(client):
    client.post("/signup", data={"name": "L", "email": f"n{uuid4().hex[:7]}@carleton.ca",
                                 "password": "nexorademo1"})
    with nexora.app.app_context():
        return nexora.User.query.order_by(nexora.User.id.desc()).first().id


def _add(client, text):
    return client.post("/api/day-notes", json={"text": text})


def test_you_can_write_something_down_and_it_comes_back(client):
    _account(client)
    res = _add(client, "Email Prof about the lab report")
    assert res.status_code == 201
    assert [n["text"] for n in res.get_json()["notes"]] == ["Email Prof about the lab report"]


def test_it_appears_on_the_dashboard(client):
    _account(client)
    _add(client, "Print SYSC2310 notes")
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'id="mylist"' in body
    assert "Print SYSC2310 notes" in body


def test_empty_text_is_refused(client):
    _account(client)
    assert _add(client, "   ").status_code == 400


def test_ticking_it_off_and_back_again(client):
    _account(client)
    note = _add(client, "Buy a notebook").get_json()["notes"][0]
    done = client.post(f"/api/day-notes/{note['id']}").get_json()["notes"][0]
    assert done["done"] is True
    again = client.post(f"/api/day-notes/{note['id']}").get_json()["notes"][0]
    assert again["done"] is False


def test_deleting_removes_it(client):
    _account(client)
    note = _add(client, "Something I changed my mind about").get_json()["notes"][0]
    left = client.delete(f"/api/day-notes/{note['id']}").get_json()["notes"]
    assert left == []


def test_another_students_note_cannot_be_touched(client):
    mine = _account(client)
    with nexora.app.app_context():
        other = nexora.User(name="O", email=f"o{uuid4().hex[:6]}@carleton.ca")
        other.password_hash = nexora.generate_password_hash("nexorademo1")
        nexora.db.session.add(other)
        nexora.db.session.commit()
        theirs = nexora.DayNote(id=uuid4().hex, user_id=other.id, text="Not yours",
                                day=nexora._student_today(None).isoformat())
        nexora.db.session.add(theirs)
        nexora.db.session.commit()
        tid = theirs.id
    assert client.post(f"/api/day-notes/{tid}").status_code == 404
    assert client.delete(f"/api/day-notes/{tid}").status_code == 404
    with nexora.app.app_context():
        assert nexora.DayNote.query.filter_by(id=tid).first() is not None


# --------------------------------------------------------------------------
# Carrying over
# --------------------------------------------------------------------------

def _yesterdays(uid, text, done=False):
    with nexora.app.app_context():
        day = (nexora._student_today(None) - timedelta(days=1)).isoformat()
        nexora.db.session.add(nexora.DayNote(id=uuid4().hex, user_id=uid, text=text,
                                             day=day, done=done))
        nexora.db.session.commit()


def test_something_unfinished_yesterday_is_still_there_today(client):
    """A list that empties itself overnight teaches people not to use it."""
    uid = _account(client)
    _yesterdays(uid, "Still not emailed the prof")
    with nexora.app.app_context():
        notes = nexora._day_notes_for(uid, nexora._student_today(None))
    assert [n["text"] for n in notes] == ["Still not emailed the prof"]
    assert notes[0]["carried"] is True


def test_something_finished_yesterday_does_not_come_back(client):
    """Or today's list fills up with last week's ticks."""
    uid = _account(client)
    _yesterdays(uid, "Did this yesterday", done=True)
    with nexora.app.app_context():
        assert nexora._day_notes_for(uid, nexora._student_today(None)) == []


def test_ticking_a_carried_item_moves_it_to_today(client):
    """Otherwise tomorrow shows it again as unfinished."""
    uid = _account(client)
    _yesterdays(uid, "Overdue errand")
    with nexora.app.app_context():
        nid = nexora.DayNote.query.filter_by(user_id=uid).first().id
    client.post(f"/api/day-notes/{nid}")
    with nexora.app.app_context():
        row = nexora.DayNote.query.filter_by(id=nid).first()
        assert row.done is True
        assert row.day == nexora._student_today(None).isoformat()


def test_a_day_has_a_loud_ceiling(client):
    _account(client)
    for i in range(nexora.DAY_NOTE_MAX):
        assert _add(client, f"thing {i}").status_code == 201
    res = _add(client, "one too many")
    assert res.status_code == 400
    assert str(nexora.DAY_NOTE_MAX) in res.get_json()["error"]


def test_it_is_not_a_task_so_it_never_reaches_the_calendar(client):
    """The whole reason for a separate table."""
    _account(client)
    _add(client, "Email Prof about the lab report")
    with nexora.app.app_context():
        assert nexora.Task.query.count() == 0
    assert "Email Prof" not in client.get("/calendar").get_data(as_text=True)


def test_it_needs_an_account(client):
    assert _add(client, "x").status_code in (302, 401)


def test_the_personal_list_lives_inside_the_task_card(client):
    """Two cards on one screen both headed TODAY read as two separate
    features rather than one day, and the owner asked for one card. The merge
    is visual only -- a DayNote is still not a Task -- so what has to hold is
    that `#mylist` renders INSIDE `#tasks` and not as a sibling card after it.

    Pinned because the failure is silent: split back into two cards the page
    still works, still shows every note, and only looks wrong.
    """
    _account(client)
    _add(client, "Email the prof")
    body = client.get("/dashboard").get_data(as_text=True)

    tasks = body.index('id="tasks"')
    mine = body.index('id="mylist"')
    end_of_tasks = body.index("</section>", tasks)
    assert tasks < mine < end_of_tasks, "the personal list escaped the task card"

    # ...and it is a plain block, not a second card with its own header
    assert 'class="mine"' in body
    assert 'class="card" id="mylist"' not in body


def test_the_add_box_is_still_reachable_with_no_notes_yet(client):
    """The empty state replaced a card-level `.empty`, which is centred and
    padded for a whole card. Whatever it looks like, the form has to survive."""
    _account(client)
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'id="noteForm"' in body and 'id="noteText"' in body
    assert 'id="noteEmpty"' in body
