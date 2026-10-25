"""A row's link belongs to the university; Nexora's copy gets its own field.

`/api/sync/file` used to do two destructive things to the row it had just
cached a file for:

    content.source_url  = stored_url                       # /static/nexora_uploads/...
    content.content_text = json.dumps({..., "stored_file": True})

Render's filesystem is wiped by every deploy, so the first left the row
pointing at a file that no longer existed -- a broken link where a working
Brightspace link used to be. The second threw away the extracted text, which
is the only thing the tutor, the summariser and the outline analyser can read,
and replaced it with a six-word stub.

Nothing calls that route today: the extension posts to
`/api/sync/process-content`, which extracts text and writes no file. So this
was a loaded gun rather than a live fire -- but the repair script exists
because older builds did call it.
"""
import json
import os
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest

import app as nexora

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOADS = os.path.join(HERE, "static", "nexora_uploads")


@pytest.fixture(autouse=True)
def _no_files_left_behind():
    """/api/sync/file writes under `app.root_path`, which is the repo, so a
    test run left real files in the working tree -- and two of them reached a
    commit before `.gitignore` learned about the folder. The route is the one
    under test, so it cannot be redirected; the files are swept instead."""
    yield
    shutil.rmtree(UPLOADS, ignore_errors=True)


BS = "https://brightspace.carleton.ca/d2l/le/content/1/viewContent/9/View"
STORED = "/static/nexora_uploads/4/SYSC2004A/abc123-Lecture_3.pdf"


def _row(user_id, source_url=BS, text="real extracted lecture text " * 20,
         stored_url="", uid=None):
    uid = uid or uuid4().hex[:10]
    with nexora.app.app_context():
        row = nexora.CourseContent(
            id=uuid4().hex, user_id=user_id, course_full="SYSC2004A",
            title="Lecture 3", content_type="lecture", source_uid=uid,
            source_url=source_url, content_text=text, stored_url=stored_url,
            module_name="Lectures", file_ext="pdf")
        nexora.db.session.add(row)
        nexora.db.session.commit()
        return uid


def _file_record(user_id, uid, original=BS):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.SyncedFile(
            id=uuid4().hex, user_id=user_id, source_uid=uid,
            course_full="SYSC2004A", original_source_url=original,
            stored_url=STORED, file_name="Lecture_3.pdf"))
        nexora.db.session.commit()


def _get(uid):
    with nexora.app.app_context():
        return nexora.CourseContent.query.filter_by(source_uid=uid).first()


def _account(client):
    email = f"su{uuid4().hex[:7]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": "nexorademo1"})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


# --------------------------------------------------------------------------
# The route no longer destroys anything
# --------------------------------------------------------------------------

def test_caching_a_file_leaves_the_university_link_alone(client):
    """The whole bug. The LMS link is the durable one -- Nexora's copy lives on
    a filesystem that every deploy erases."""
    uid = _account(client)
    token = client.post("/api/sync/extension-key").get_json()["token"]
    item = _row(uid)
    client.post("/api/sync/file",
                data={"metadata": json.dumps({"course": "SYSC2004A", "source_uid": item,
                                              "file_name": "Lecture_3.pdf"}),
                      "file": (open(os.path.join(HERE, "requirements.txt"), "rb"),
                               "Lecture_3.pdf")},
                content_type="multipart/form-data",
                headers={"Authorization": f"Bearer {token}"})
    row = _get(item)
    assert row.source_url == BS, "the university's link was overwritten"
    assert row.stored_url.startswith("/static/nexora_uploads/"), \
        "the copy must still be recorded, in its own field"


def test_caching_a_file_does_not_destroy_the_extracted_text(client):
    """It replaced the body with {"stored_file": true}. That text is the only
    thing the tutor, the summariser and the outline analyser can read."""
    uid = _account(client)
    token = client.post("/api/sync/extension-key").get_json()["token"]
    item = _row(uid, text="Ohm's law states that current is proportional to voltage. " * 10)
    client.post("/api/sync/file",
                data={"metadata": json.dumps({"course": "SYSC2004A", "source_uid": item}),
                      "file": (open(os.path.join(HERE, "requirements.txt"), "rb"), "x.pdf")},
                content_type="multipart/form-data",
                headers={"Authorization": f"Bearer {token}"})
    body = _get(item).content_text
    assert "Ohm's law" in body
    assert "stored_file" not in body


# --------------------------------------------------------------------------
# The repair
# --------------------------------------------------------------------------

def _repair(*flags):
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "scripts", "repair_stored_urls.py")] + list(flags),
        capture_output=True, text=True, cwd=HERE,
        env={**os.environ, "PYTHONPATH": HERE})


def test_plan_changes_nothing(client):
    uid = _account(client)
    item = _row(uid, source_url=STORED, text='{"original_source_url": "%s", "stored_file": true}' % BS)
    _file_record(uid, item)
    out = _repair("--plan")
    assert out.returncode == 0, out.stderr[-800:]
    assert "Nothing was changed" in out.stdout
    assert _get(item).source_url == STORED, "--plan must not write"


def test_apply_puts_the_link_back_and_keeps_the_copy(client):
    uid = _account(client)
    item = _row(uid, source_url=STORED,
                text='{"original_source_url": "%s", "stored_file": true}' % BS)
    _file_record(uid, item)
    out = _repair("--apply")
    assert out.returncode == 0, out.stderr[-800:]
    row = _get(item)
    assert row.source_url == BS, "the original was not restored"
    assert row.stored_url == STORED, "the copy's path must be kept, not dropped"


def test_the_stub_is_cleared_so_the_body_can_be_fetched_again(client):
    """Blanking it puts the row back under the ~120 character threshold, so the
    next feature that needs the body triggers `needs_content` and re-reads it
    from Brightspace."""
    uid = _account(client)
    item = _row(uid, source_url=STORED,
                text='{"original_source_url": "%s", "stored_file": true}' % BS)
    _file_record(uid, item)
    _repair("--apply")
    assert _get(item).content_text == ""


def test_real_text_is_never_cleared(client):
    """Only the exact stub. A row that was cached AND still holds real text
    must keep it -- re-fetching costs a Brightspace round trip the student has
    to be present for."""
    uid = _account(client)
    item = _row(uid, source_url=STORED, text="Kirchhoff's current law. " * 20)
    _file_record(uid, item)
    _repair("--apply")
    assert "Kirchhoff" in _get(item).content_text


def test_a_row_with_no_recoverable_original_is_left_alone(client):
    """A dead link at least names the item. Blanking it renders no link at
    all, which is worse and irreversible."""
    uid = _account(client)
    item = _row(uid, source_url=STORED, text="body text " * 30)
    _file_record(uid, item, original="")      # nothing to restore
    out = _repair("--apply")
    assert "left alone" in out.stdout or "Nothing to repair" in out.stdout
    assert _get(item).source_url == STORED


def test_a_javascript_original_is_refused(client):
    """`_lms_link` is the gate. A restore must not be a way to put an unsafe
    URL into an href that the gate would otherwise have refused."""
    uid = _account(client)
    item = _row(uid, source_url=STORED, text="body " * 40)
    _file_record(uid, item, original="javascript:alert(1)")
    _repair("--apply")
    assert _get(item).source_url == STORED, "it must not have been 'restored'"


def test_an_ordinary_row_is_never_touched(client):
    """Only rows whose link points at Nexora's own copy are candidates."""
    uid = _account(client)
    item = _row(uid, source_url=BS, text="normal lecture text " * 20)
    _repair("--apply")
    row = _get(item)
    assert row.source_url == BS
    assert "normal lecture text" in row.content_text


def test_running_it_twice_changes_nothing_the_second_time(client):
    uid = _account(client)
    item = _row(uid, source_url=STORED,
                text='{"original_source_url": "%s", "stored_file": true}' % BS)
    _file_record(uid, item)
    _repair("--apply")
    first = (_get(item).source_url, _get(item).stored_url)
    out = _repair("--apply")
    assert "Nothing to repair" in out.stdout
    assert (_get(item).source_url, _get(item).stored_url) == first


# --------------------------------------------------------------------------
# The column has to exist in production
# --------------------------------------------------------------------------

def test_stored_url_is_in_the_migration_map():
    """A new column on an EXISTING table. Without this line every query
    against course_content raises UndefinedColumn on Postgres while SQLite
    carries on locally -- which is exactly how it reaches production."""
    import inspect
    src = inspect.getsource(nexora.ensure_schema_columns)
    block = src.split('"course_content"')[1].split("}")[0]
    assert "stored_url" in block
