"""The server can turn a lecture link into a download, without an extension release.

Reported a fourth time: "same issue when i click it doesnt download, it just
redirects". Measured on the live site -- all 36 links on one course workspace
were D2L `viewContent` URLs, which is what `_browsable_d2l` renders a stale row
as. It opens, which is why the Forbidden page went away and the lecture still
did not download.

The download route exists and was verified against that student's own
Brightspace: `topics/files/download/<id>/DirectFileTopicDownload` answers
200 `application/pdf` with `Content-Disposition: attachment` for a FILE topic,
and **403** for a Link or a ContentService topic. So it cannot be applied
blindly, and the earlier conclusion was that the server could never know which
rows were files.

It can. `file_ext` has been on every item the extension sends for a long time --
including the build published on the Web Store -- and `_import_content` dropped
it. Scored against D2L's own `TypeIdentifier` over ten of that student's real
courses, 210 topics:

    83  File topics correctly turned into downloads
     0  non-File topics wrongly turned into downloads   <- the property that matters
   127  File topics left alone, 100 of them `.html` course pages

Which is the right trade: this can only ever ADD a download, never break a link.
"""
import io
import os
from uuid import uuid4

import app as nexora

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEW = "https://brightspace.carleton.ca/d2l/le/content/449526/viewContent/4894237/View"
API = "https://brightspace.carleton.ca/d2l/api/le/1.67/449526/content/topics/4894237/file"
DOWNLOAD = ("https://brightspace.carleton.ca/d2l/le/content/449526"
            "/topics/files/download/4894237/DirectFileTopicDownload")


# --------------------------------------------------------------------------
# What becomes a download
# --------------------------------------------------------------------------

def test_a_document_becomes_a_download():
    for ext in ("pdf", "PDF", ".pdf", "pptx", "docx", "xlsx", "zip"):
        assert nexora.content_href(VIEW, ext) == DOWNLOAD, ext


def test_a_stale_api_row_becomes_one_too():
    """`_browsable_d2l` turns the old API URL into the viewer first; the
    download rewrite then runs over that. Every row imported before any of this
    shipped is the API shape."""
    assert nexora.content_href(API, "pdf") == DOWNLOAD


def test_a_row_with_no_extension_is_left_exactly_alone():
    """A Link or ContentService topic records no file extension, and
    DirectFileTopicDownload answers 403 for both."""
    for blank in ("", "   ", None):
        assert nexora.content_href(VIEW, blank) == VIEW, repr(blank)


def test_a_course_page_stays_a_page():
    """100 of the 127 File topics this declines are `.html` -- an EHS training
    module, a welcome note. A student clicking one wants to read it."""
    assert nexora.content_href(VIEW, "html") == VIEW
    assert nexora.content_href(VIEW, "htm") == VIEW


def test_a_recording_stays_something_to_watch():
    for ext in ("mp4", "m4a", "vtt", "png"):
        assert nexora.content_href(VIEW, ext) == VIEW, ext


def test_rubbish_in_the_extension_changes_nothing():
    for ext in ("1", "2", "exe", "../../etc", "pdf; rm -rf"):
        assert nexora.content_href(VIEW, ext) == VIEW, ext


# --------------------------------------------------------------------------
# The ids are read, never invented
# --------------------------------------------------------------------------

def test_the_org_unit_and_topic_come_out_of_the_url():
    out = nexora.content_href(
        "https://brightspace.example.ca/d2l/le/content/777/viewContent/888/View", "pdf")
    assert "/content/777/" in out and "/download/888/" in out
    assert out.startswith("https://brightspace.example.ca/")


def test_a_url_that_is_not_a_topic_view_is_untouched():
    for url in ("https://brightspace.carleton.ca/d2l/home/449526",
                "https://example.edu/notes.pdf",
                "https://brightspace.carleton.ca/d2l/le/content/449526/viewContent/x/View"):
        assert nexora.content_href(url, "pdf") == nexora._lms_link(url), url


# --------------------------------------------------------------------------
# The security gate still runs first
# --------------------------------------------------------------------------

def test_a_javascript_url_is_still_refused():
    """`_lms_link` is a security boundary: a scraped `javascript:` URL in an
    href is cross-site scripting on our own page, delivered by the university
    page we scraped. The rewrite must never be a way round it."""
    assert nexora.content_href("javascript:alert(1)", "pdf") == ""


def test_userinfo_is_still_refused():
    assert nexora.content_href("https://brightspace.example.ca@evil.tld/x", "pdf") == ""


def test_our_own_upload_is_untouched():
    assert nexora.content_href("/static/nexora_uploads/a.pdf", "pdf") \
        == "/static/nexora_uploads/a.pdf"


def test_the_filter_still_works_with_one_argument():
    """Four call sites pass a row's extension; the dashboard's task links have
    none and must keep rendering."""
    assert nexora.content_href(VIEW) == VIEW
    assert nexora.content_href("javascript:alert(1)") == ""


# --------------------------------------------------------------------------
# The value has to survive the import
# --------------------------------------------------------------------------

def _synced(client):
    """A signed-up account with the course on file and the extension's own
    bearer token, which is how the real sync authenticates."""
    client.post("/signup", data={"name": "S", "email": f"fx{uuid4().hex[:7]}@carleton.ca",
                                 "password": "nexorademo1"})
    token = client.post("/api/sync/extension-key").get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/sync/courses", json={"courses": [{
        "source_uid": "449526", "course_code": "ELEC2501B", "course_name": "Circuits",
        "term": "Fall 2026"}], "complete": True}, headers=headers)
    return headers


def _push(client, headers, items):
    return client.post("/api/sync/content",
                       json={"course": "ELEC2501B", "items": items}, headers=headers)


def test_the_import_stores_what_the_extension_sends(client):
    """It has always been on the payload -- including the build on the Web Store
    -- and was dropped here. The same shape as `due_at` before
    `_import_deadlines` existed: a scraped value nothing consumed."""
    headers = _synced(client)
    _push(client, headers, [{"course": "ELEC2501B", "source_uid": "t1", "title": "Lecture 3",
                    "content_type": "lecture", "source_url": VIEW, "file_ext": "PDF"}])
    with nexora.app.app_context():
        row = nexora.CourseContent.query.filter_by(source_uid="t1").first()
        assert row is not None
        assert row.file_ext == "pdf", "stored lower-cased"


def test_a_re_import_updates_it(client):
    """The row survives when the payload carries no `replace_courses`, so the
    update branch has to write it too or an existing row never gains one."""
    headers = _synced(client)
    _push(client, headers, [{"course": "ELEC2501B", "source_uid": "t2", "title": "Lecture 4",
                    "content_type": "lecture", "source_url": VIEW}])
    _push(client, headers, [{"course": "ELEC2501B", "source_uid": "t2", "title": "Lecture 4 (v2)",
                    "content_type": "lecture", "source_url": VIEW, "file_ext": "pptx"}])
    with nexora.app.app_context():
        assert nexora.CourseContent.query.filter_by(source_uid="t2").first().file_ext == "pptx"


def test_the_workspace_passes_the_rows_extension_to_the_filter():
    """Without this the filter can only ever see the URL, which is exactly the
    information that does not say whether the item is a file."""
    with io.open(os.path.join(HERE, "templates", "course_workspace.html"),
                 encoding="utf-8") as fh:
        page = fh.read()
    assert page.count("content_href(entry.row.file_ext)") >= 2, \
        "the lecture and resource lists must pass it"
