"""An item is announced once, not once per sync.

This is the bug that took production down. `_import_content` deletes a
course's CourseContent rows when the payload carries `replace_courses`,
then re-imports each item. The `existing` lookup therefore always misses,
the `content_hash` comparison that exists to detect "unchanged" is
unreachable, and every item takes the "new" branch and writes a fresh
AcademicUpdate row -- on every sync, for every item, plus a second row for
every lecture and every outline.

Measured on the live database before the fix: 2,581,147 rows across 33,795
real items. One Brightspace page, "Knowledge Check - Q2", had 638
byte-identical rows -- same title, same detail, same course, differing only
in their timestamp. That table was 786 MB of an 817 MB database, 96% of it,
and Render suspended the database for exceeding its 1 GB limit.

The content row is deleted by `replace_courses`, so it cannot be what
remembers. The update row is what remembers.
"""
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402


@pytest.fixture()
def synced(client):
    email = f"dupe{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "Rana Test", "email": email, "password": "dupe-pass-123"})
    token = client.post("/api/sync/extension-key").get_json()["token"]
    with nexora.app.app_context():
        uid = nexora.User.query.filter_by(email=email).first().id
    return {"Authorization": f"Bearer {token}"}, uid


PAYLOAD = {
    "course": "CHEM1101",
    "replace_courses": ["CHEM1101"],
    "items": [
        {"content_type": "page", "title": "Knowledge Check - Q2", "source_uid": "53862|content|2006893"},
        {"content_type": "lecture", "title": "Chapter 5", "source_uid": "53862|content|2006894"},
        {"content_type": "outline", "title": "Course Outline", "source_uid": "53862|content|2006895"},
    ],
}


def updates(uid):
    with nexora.app.app_context():
        return [(u.related_uid, u.update_type, u.title)
                for u in nexora.AcademicUpdate.query.filter_by(user_id=uid).all()]


def test_the_same_sync_twice_announces_each_item_once(client, synced):
    """The exact shape that wrote 638 rows about one page."""
    headers, uid = synced
    client.post("/api/sync/content", headers=headers, json=PAYLOAD)
    first = updates(uid)
    assert first, "the first sync does announce the items"

    for _ in range(5):
        client.post("/api/sync/content", headers=headers, json=PAYLOAD)
    assert sorted(updates(uid)) == sorted(first), "five more syncs added nothing"


def test_each_item_gets_exactly_one_row_however_many_syncs(client, synced):
    """B7. A lecture used to earn a second row -- "Review recommended:
    Chapter 5" under "New lecture: Chapter 5" -- and an outline a third kind.
    Three items are three rows now, not five, and four syncs do not change
    that."""
    headers, uid = synced
    for _ in range(4):
        client.post("/api/sync/content", headers=headers, json=PAYLOAD)
    rows = updates(uid)
    kinds = [u[1] for u in rows]
    assert kinds.count("new_content") == 3          # one per item, not four
    assert len(rows) == 3, rows                      # and nothing else
    assert "ai_recommendation" not in kinds
    assert "outline_detected" not in kinds


def test_the_pointer_rides_on_the_detail_line(client, synced):
    """The advice was worth saying once and was never worth a row of its own.
    It is the part of the detail that does not vary between two lectures, so
    it goes AFTER the module name, which is the part that does."""
    headers, uid = synced
    client.post("/api/sync/content", headers=headers, json={
        "course": "CHEM1101",
        "replace_courses": ["CHEM1101"],
        "items": [
            {"content_type": "lecture", "title": "Chapter 5",
             "module_name": "Week 3", "source_uid": "u-lec"},
            {"content_type": "outline", "title": "Course Outline", "source_uid": "u-out"},
            {"content_type": "page", "title": "Piazza", "source_uid": "u-page"},
        ],
    })
    with nexora.app.app_context():
        detail = {u.related_uid: u.detail
                  for u in nexora.AcademicUpdate.query.filter_by(user_id=uid).all()}
    assert detail["u-lec"].startswith("Week 3")
    assert "Study Tools" in detail["u-lec"]
    assert "Analyze it once" in detail["u-out"]
    # A page has no pointer and no module name, and must not be left blank.
    assert detail["u-page"] == "Imported from Brightspace."


def test_genuinely_new_content_is_still_announced(client, synced):
    """The guard must not silence a real arrival -- that would trade a full
    database for a student who is never told anything changed."""
    headers, uid = synced
    client.post("/api/sync/content", headers=headers, json=PAYLOAD)
    before = len(updates(uid))

    later = dict(PAYLOAD)
    later["items"] = PAYLOAD["items"] + [
        {"content_type": "page", "title": "Knowledge Check - Q3", "source_uid": "53862|content|2006999"},
    ]
    client.post("/api/sync/content", headers=headers, json=later)

    after = updates(uid)
    assert len(after) == before + 1
    assert any(u[0] == "53862|content|2006999" for u in after)


def test_an_item_with_no_source_uid_is_still_only_announced_once(client, synced):
    """The id falls back to source_url, then to "course|title". The pre-pass
    that loads what has already been announced derives that id separately from
    the loop that looks it up, so if the two ever disagree every item is
    announced afresh on every sync -- the exact bug, back by another door.
    `_content_source_uid` is the single definition; this is what pins it."""
    headers, uid = synced
    payload = {
        "course": "CHEM1101",
        "replace_courses": ["CHEM1101"],
        "items": [
            {"content_type": "page", "title": "No Uid Anywhere"},                    # falls back to course|title
            {"content_type": "page", "title": "Url Only", "source_url": "https://learn.school.ca/x"},
        ],
    }
    client.post("/api/sync/content", headers=headers, json=payload)
    first = sorted(updates(uid))
    for _ in range(4):
        client.post("/api/sync/content", headers=headers, json=payload)
    assert sorted(updates(uid)) == first
    assert len(first) == 2


def test_the_lookup_does_not_load_the_whole_table(client, synced):
    """The first version of the fix read every update row the student had,
    which on the account that caused the outage is 2.9 million -- about half a
    gigabyte of Python objects, on a worker with less than that. A fix for a
    table that grew too big must not itself need the table to be small."""
    headers, uid = synced
    client.post("/api/sync/content", headers=headers, json=PAYLOAD)
    with nexora.app.app_context():
        for n in range(400):                      # unrelated history, none of it in the payload
            nexora.db.session.add(nexora.AcademicUpdate(
                id=uuid4().hex, user_id=uid, course_full="OLD1000",
                update_type="new_content", title=f"Ancient {n}",
                related_uid=f"old|{n}"))
        nexora.db.session.commit()

    from sqlalchemy import event

    selects = []

    def watch(conn, cursor, statement, parameters, context, executemany):
        flat = " ".join(statement.split()).lower()
        if flat.startswith("select") and "from academic_update" in flat:
            selects.append(flat)

    with nexora.app.app_context():
        engine = nexora.db.engine
    event.listen(engine, "before_cursor_execute", watch)
    try:
        client.post("/api/sync/content", headers=headers, json=PAYLOAD)
    finally:
        event.remove(engine, "before_cursor_execute", watch)

    reads = [s for s in selects if "related_uid" in s]
    assert reads, "the sync does look up what was already announced"
    for sql in reads:
        assert "related_uid in (" in sql, f"unscoped read of the whole table: {sql[:120]}"


def test_the_row_count_does_not_grow_with_sync_count(client, synced):
    """The property that actually matters: rows are bounded by items, not by
    how often the extension runs. Nexora synced ~76 times in two months."""
    headers, uid = synced
    for _ in range(20):
        client.post("/api/sync/content", headers=headers, json=PAYLOAD)
    # One row per item -- the invariant, not a count of the rows this fixture
    # happens to produce. It said 5 because a lecture and an outline each
    # earned a second row; B7 made that one, and a test pinned to the number
    # would have read as a regression rather than as the fix.
    assert len(updates(uid)) == len(PAYLOAD["items"])
