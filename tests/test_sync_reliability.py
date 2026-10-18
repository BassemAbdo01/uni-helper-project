"""The five reliability findings from SYNC_AUDIT_V1.md: S3, S2, S6, S5, S1.

Each test names the finding it pins. What they have in common is that every one
of these failures was SILENT: the sync returned 200, the course page said it
was fine, and the student had no way to tell that items had been dropped,
deleted, or never recorded.
"""
from uuid import uuid4

import app as nexora


def _account(client):
    email = f"sr{uuid4().hex[:7]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": "nexorademo1"})
    token = client.post("/api/sync/extension-key").get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/sync/courses", json={"courses": [
        {"source_uid": "1", "course_code": "SYSC2004A", "course_name": "OOP",
         "term": "Fall 2026"},
        {"source_uid": "2", "course_code": "ELEC2501B", "course_name": "Circuits",
         "term": "Fall 2026"}], "complete": True}, headers=headers)
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id, headers


def _items(course, n, start=1):
    return [{"course": course, "source_uid": f"{course}-{i}", "title": f"Item {i}",
             "content_type": "lecture", "module_name": "Lectures",
             "source_url": f"https://b.example.ca/d2l/le/content/1/viewContent/{i}/View"}
            for i in range(start, start + n)]


def _push(client, headers, body):
    return client.post("/api/sync/content", json=body, headers=headers)


def _course(code, user_id):
    return nexora.SyncedCourse.query.filter_by(user_id=user_id, course_code=code).first()


# --------------------------------------------------------------------- S3
# A cap a student cannot see is indistinguishable from a bug.

def test_a_payload_past_the_cap_says_so(client, monkeypatch):
    uid, headers = _account(client)
    monkeypatch.setattr(nexora, "CONTENT_IMPORT_MAX", 10)
    body = _push(client, headers, {"course": "SYSC2004A",
                                   "items": _items("SYSC2004A", 25)}).get_json()
    assert body["received"] == 25
    assert body["stored"] == 10
    assert body["truncated"] is True
    assert body["dropped"] == 15


def test_a_payload_inside_the_cap_says_nothing(client):
    """`truncated` must be absent, not False -- a flag that is always present
    is one nobody reads."""
    uid, headers = _account(client)
    body = _push(client, headers, {"course": "SYSC2004A",
                                   "items": _items("SYSC2004A", 3)}).get_json()
    assert "truncated" not in body
    assert body["stored"] == body["received"] == 3


# --------------------------------------------------------------------- S2
# last_sync_at was stamped for ONE course per payload, and shown nowhere.

def test_every_course_in_the_payload_is_stamped(client):
    uid, headers = _account(client)
    _push(client, headers, {"items": _items("SYSC2004A", 2) + _items("ELEC2501B", 2)})
    with nexora.app.app_context():
        for code in ("SYSC2004A", "ELEC2501B"):
            assert _course(code, uid).last_sync_at is not None, code


def test_a_payload_with_no_envelope_course_still_stamps(client):
    """`scan_course` came from the envelope or from replace_courses. With
    neither, nothing was stamped at all."""
    uid, headers = _account(client)
    _push(client, headers, {"items": _items("SYSC2004A", 2)})
    with nexora.app.app_context():
        assert _course("SYSC2004A", uid).last_sync_at is not None


def test_the_count_is_per_course_not_the_whole_payload(client):
    uid, headers = _account(client)
    _push(client, headers, {"items": _items("SYSC2004A", 5) + _items("ELEC2501B", 2)})
    with nexora.app.app_context():
        assert _course("SYSC2004A", uid).last_sync_items == 5
        assert _course("ELEC2501B", uid).last_sync_items == 2


def test_the_page_shows_when_the_course_last_synced(client):
    uid, headers = _account(client)
    _push(client, headers, {"course": "SYSC2004A", "items": _items("SYSC2004A", 2)})
    body = client.get("/course/SYSC2004A").get_data(as_text=True)
    assert "Last synced" in body


# --------------------------------------------------------------------- S1
# A partial scrape must not replace a whole course.

def test_a_short_payload_is_refused_its_replace(client):
    """Forty items on file, three arrive claiming a complete scan. The
    extension's `complete` is its own opinion; the server knows what it had."""
    uid, headers = _account(client)
    _push(client, headers, {"course": "SYSC2004A", "items": _items("SYSC2004A", 40)})
    _push(client, headers, {"course": "SYSC2004A", "items": _items("SYSC2004A", 3),
                            "replace_courses": ["SYSC2004A"],
                            "scan_meta": {"complete": True}})
    with nexora.app.app_context():
        kept = nexora.CourseContent.query.filter_by(
            user_id=uid, course_full="SYSC2004A").count()
        assert kept == 40, f"{40 - kept} items were deleted by a partial scrape"


def test_the_refusal_is_visible_rather_than_looking_like_a_clean_sync(client):
    uid, headers = _account(client)
    _push(client, headers, {"course": "SYSC2004A", "items": _items("SYSC2004A", 40)})
    body = _push(client, headers, {"course": "SYSC2004A",
                                   "items": _items("SYSC2004A", 3),
                                   "replace_courses": ["SYSC2004A"],
                                   "scan_meta": {"complete": True}}).get_json()
    assert body["partial_courses"]["SYSC2004A"] == {"had": 40, "received": 3}
    assert body["replaced_courses"] == [], "the delete must not have run"
    with nexora.app.app_context():
        row = _course("SYSC2004A", uid)
        assert row.last_sync_state == "partial"
        assert row.last_sync_ok is False, "a short sync must not read as a clean one"
    page = client.get("/course/SYSC2004A").get_data(as_text=True)
    assert "arrived incomplete" in page


def test_a_full_payload_still_replaces(client):
    """The floor must not break the thing it guards. A real re-scrape has to
    clear items the course genuinely removed."""
    uid, headers = _account(client)
    _push(client, headers, {"course": "SYSC2004A", "items": _items("SYSC2004A", 10)})
    _push(client, headers, {"course": "SYSC2004A",
                            "items": _items("SYSC2004A", 8, start=100),
                            "replace_courses": ["SYSC2004A"],
                            "scan_meta": {"complete": True}})
    with nexora.app.app_context():
        titles = {c.title for c in nexora.CourseContent.query.filter_by(
            user_id=uid, course_full="SYSC2004A").all()}
        assert titles == {f"Item {i}" for i in range(100, 108)}


def test_a_first_import_is_never_called_partial(client):
    """Nothing on file means nothing to lose -- `have` is 0, and the floor must
    not turn a brand new course into a warning."""
    uid, headers = _account(client)
    body = _push(client, headers, {"course": "SYSC2004A",
                                   "items": _items("SYSC2004A", 2),
                                   "replace_courses": ["SYSC2004A"],
                                   "scan_meta": {"complete": True}}).get_json()
    assert "partial_courses" not in body
    with nexora.app.app_context():
        assert _course("SYSC2004A", uid).last_sync_state == "ok"


def test_the_items_a_short_payload_did_bring_are_still_stored(client):
    """Refusing the delete must not refuse the update: a new item in a short
    payload is still a new item."""
    uid, headers = _account(client)
    _push(client, headers, {"course": "SYSC2004A", "items": _items("SYSC2004A", 40)})
    _push(client, headers, {"course": "SYSC2004A",
                            "items": _items("SYSC2004A", 2, start=500),
                            "replace_courses": ["SYSC2004A"],
                            "scan_meta": {"complete": True}})
    with nexora.app.app_context():
        assert nexora.CourseContent.query.filter_by(
            user_id=uid, course_full="SYSC2004A").count() == 42


# --------------------------------------------------------------------- S6
# The failure has to be recorded outside the transaction that failed.

def test_a_crash_mid_import_is_recorded_on_the_course(client, monkeypatch):
    uid, headers = _account(client)
    _push(client, headers, {"course": "SYSC2004A", "items": _items("SYSC2004A", 2)})

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(nexora, "_import_content", boom)
    res = _push(client, headers, {"course": "SYSC2004A", "items": _items("SYSC2004A", 2)})
    assert res.status_code == 500
    assert "could not store" in res.get_json()["error"].lower()
    with nexora.app.app_context():
        row = _course("SYSC2004A", uid)
        assert row.last_sync_state == "error"
        assert row.last_sync_ok is False
        assert "database went away" in row.last_sync_error


def test_recording_the_failure_cannot_itself_fail_the_request(client, monkeypatch):
    """A logger that takes down the error path is worse than the error."""
    uid, headers = _account(client)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(nexora, "_import_content", boom)
    monkeypatch.setattr(nexora, "_normalize_course_code", boom)
    assert _push(client, headers, {"course": "SYSC2004A", "items": []}).status_code == 500


# --------------------------------------------------------------------- S5
# One definition of an item's identity, two importers.

def test_both_importers_read_the_same_definition():
    item = {"source_uid": "topic-9", "source_url": "https://x/y"}
    assert nexora._content_source_uid(item, "SYSC2004A", "T") == "topic-9"
    assert nexora._source_uid(item, "SYSC2004A", "T", use_url=False) == "topic-9"


def test_the_url_step_belongs_to_the_content_path_only():
    """Not a style choice. The deadline path has never used the URL, so adding
    it would re-key every existing row that fell back to `course|title` -- the
    item would arrive as new, beside itself. That is S4, and it needs a
    backfill and a database backup."""
    item = {"source_url": "https://b.example.ca/d2l/le/content/1/viewContent/7/View"}
    assert nexora._source_uid(item, "SYSC2004A", "T", use_url=True) == item["source_url"]
    assert nexora._source_uid(item, "SYSC2004A", "T", use_url=False) == "SYSC2004A|T"


def test_an_item_with_nothing_falls_back_to_course_and_title():
    assert nexora._source_uid({}, "SYSC2004A", "Lecture 1") == "SYSC2004A|Lecture 1"
