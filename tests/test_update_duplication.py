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


def test_a_lecture_and_an_outline_still_get_their_second_row_once(client, synced):
    """A lecture earns a review recommendation and an outline earns an
    analysis prompt -- each exactly once, not once per sync."""
    headers, uid = synced
    for _ in range(4):
        client.post("/api/sync/content", headers=headers, json=PAYLOAD)
    kinds = [u[1] for u in updates(uid)]
    assert kinds.count("ai_recommendation") == 1
    assert kinds.count("outline_detected") == 1
    assert kinds.count("new_content") == 3          # one per item, not four


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


def test_the_row_count_does_not_grow_with_sync_count(client, synced):
    """The property that actually matters: rows are bounded by items, not by
    how often the extension runs. Nexora synced ~76 times in two months."""
    headers, uid = synced
    for _ in range(20):
        client.post("/api/sync/content", headers=headers, json=PAYLOAD)
    # 3 items + 1 lecture recommendation + 1 outline prompt
    assert len(updates(uid)) == 5
