"""Four things a student could not do, or could not read.

  * "Active courses" counted every course Nexora had ever imported -- 33 by
    third year -- above a list showing the five of this term. A stat that
    disagrees with what is directly under it is worse than no stat.
  * The course workspace could only take an outline as a FILE. The upload
    route has always accepted pasted text and sent it down the same road;
    only the page never offered it, so a student whose outline is a web page
    with nothing to download had to screenshot it.
  * Canvas hands over the syllabus in `syllabus_body`, but only if you ask
    for it in `include[]`, and nothing did. A Canvas student therefore had no
    outline at all -- no grade intelligence, no pass rules, no Ask -- on a
    platform that had been offering the text the whole time.
  * There was no way to GET the extension. With no store listing the connect
    page said "search the Chrome Web Store", which is a first-run instruction
    nobody can follow.
"""
import io
import json
import os
import sys
import zipfile
from datetime import timedelta
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402


@pytest.fixture()
def student(client):
    email = f"intake{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "Rana Test", "email": email, "password": "intake-pass-1"})
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first().id


# ---------------------------------------------------------------- the stat

def test_active_courses_counts_this_term_not_every_term(client, student):
    """A third-year student read 33 above a list showing five."""
    token = client.post("/api/sync/extension-key").get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    current = nexora._current_term_label()
    client.post("/api/sync/courses", headers=headers, json={"courses": [
        {"source_uid": "1", "course_code": "BIOL1103", "course_name": "Biology", "term": current},
        {"source_uid": "2", "course_code": "SYSC2004", "course_name": "Software", "term": current},
        {"source_uid": "3", "course_code": "HIST1001", "course_name": "History", "term": "Fall 2023"},
        {"source_uid": "4", "course_code": "MATH1007", "course_name": "Calculus", "term": "Winter 2024"},
    ]})
    html = client.get("/dashboard").get_data(as_text=True)
    import re
    cell = re.search(r'<span class="k">Active courses</span>\s*<div class="v">(\d+)</div>', html)
    assert cell, "the stat still renders"
    assert cell.group(1) == "2", f"should count this term's two, counted {cell.group(1)}"
    assert "4" != cell.group(1)


# ---------------------------------------------------------------- pasting

def test_an_outline_can_be_pasted_not_only_uploaded(client, student):
    # The route refuses anything under 200 characters, which is right: a
    # two-line paste is not an outline, and analysing it would invent a scheme.
    outline = (
        "BIOL 1103 Introductory Biology I - Course Outline\n"
        "Instructor: Dr. A. Example. Lectures Monday and Wednesday.\n\n"
        "Evaluation:\n"
        "  Midterm Examination ............ 25%\n"
        "  Laboratory Reports ............. 20%\n"
        "  Assignments .................... 15%\n"
        "  Final Examination .............. 40%\n\n"
        "Students must obtain at least 50% on the final examination in order to "
        "pass the course, regardless of their overall standing in the course.")
    assert len(outline) >= 200
    r = client.post("/api/course/BIOL1103/outline/upload", json={"text": outline})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json().get("source_uid"), "pasted text is stored like an uploaded file"


def test_the_workspace_offers_the_paste_box(client, student):
    html = client.get("/course/BIOL1103").get_data(as_text=True)
    assert 'id="outlinePaste"' in html, "the page has to offer what the route already accepted"


def test_the_amber_buttons_are_readable(client, student):
    """#F7F4EF on #E8A94A measures 1.88:1 against a WCAG minimum of 4.5 -- the
    label was barely on the button. The fix is the text, not the amber the
    whole app is built on."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "course_workspace.html")
    src = io.open(path, encoding="utf-8").read()
    assert ".action{border:0;background:#E8A94A;color:#17140F" in src
    assert ".action{border:0;background:#E8A94A;color:#F7F4EF" not in src


# ---------------------------------------------------------------- Canvas

def test_canvas_asks_for_the_syllabus(client):
    """It only comes back if you request it in include[]."""
    seen = {}

    def fake_fetch(path, params=None):
        seen[path] = params or {}
        if path == "courses":
            return [{"id": 9, "course_code": "PSYC1001", "name": "Psychology",
                     "syllabus_body": "<p>" + ("Evaluation: Midterm 30%. Final 70%. " * 12) + "</p>",
                     "created_at": "2026-09-01T00:00:00Z"}]
        return []

    with nexora.app.app_context():
        user = nexora.User(email=f"canvas{uuid4().hex[:6]}@x.ca", name="C",
                           password_hash="x", timezone="America/Toronto")
        nexora.db.session.add(user)
        nexora.db.session.commit()
        nexora.canvas_sync(user, fetch_list=fake_fetch, base_url="https://school.instructure.com")
        uid = user.id

    assert "syllabus_body" in (seen.get("courses", {}).get("include[]") or []), \
        "the syllabus has to be asked for by name"
    with nexora.app.app_context():
        row = nexora.CourseContent.query.filter_by(user_id=uid, content_type="outline").first()
        assert row is not None, "the syllabus is stored as an outline"
        assert "Midterm 30%" in (row.content_text or ""), "and carries its text"


def test_a_one_line_syllabus_is_not_an_outline(client):
    """Plenty of courses put "see the PDF" in that field."""
    def fake_fetch(path, params=None):
        if path == "courses":
            return [{"id": 4, "course_code": "CHEM1001", "name": "Chem",
                     "syllabus_body": "<p>See the attached PDF.</p>"}]
        return []

    with nexora.app.app_context():
        user = nexora.User(email=f"canvas{uuid4().hex[:6]}@x.ca", name="C",
                           password_hash="x", timezone="America/Toronto")
        nexora.db.session.add(user)
        nexora.db.session.commit()
        nexora.canvas_sync(user, fetch_list=fake_fetch, base_url="https://school.instructure.com")
        assert nexora.CourseContent.query.filter_by(user_id=user.id, content_type="outline").count() == 0


# ---------------------------------------------------------------- the zip

def test_the_extension_can_actually_be_downloaded(client, student):
    r = client.get("/extension/nexora-sync.zip")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("application/zip")
    assert "attachment" in r.headers.get("Content-Disposition", "")
    with zipfile.ZipFile(io.BytesIO(r.data)) as bundle:
        names = bundle.namelist()
        assert "manifest.json" in names and "classify.js" in names
        assert not [n for n in names if "check" in n.lower()], "check pages never ship"
        version = json.loads(bundle.read("manifest.json"))["version"]
    assert version in r.headers["Content-Disposition"], \
        "the filename carries the version, read from the manifest rather than a second constant"


def test_the_download_needs_an_account(client):
    """Not secret, but it is what pushes a student's courses into an account,
    and a public download link is a different object -- indexed and hotlinked."""
    r = client.get("/extension/nexora-sync.zip", follow_redirects=False)
    assert r.status_code in (301, 302, 401, 403)


# ---------------------------------------------------------------- the clock

def test_a_countdown_runs_on_the_students_clock_not_the_servers(client):
    """`date.today()` is the SERVER's date, and on Render the server is UTC.

    For a student in Eastern that is already tomorrow from 8pm -- exactly when
    someone opens Nexora to decide what to do tonight -- so work due at 11:59
    computed to -1 and rendered Overdue, and "in 2 days" read as tomorrow.

    Pinned with Kiritimati (UTC+14) and Niue (UTC-11): 25 hours apart, so the
    two are on different calendar days at every instant of the year. A test
    that only fails in the evening is a test that passes by accident all day.
    """
    from datetime import date as _date

    def student_in(zone):
        # Made directly, not through /signup: the client is signed in after the
        # first one, and /signup redirects an authenticated visitor rather than
        # creating a second account -- so the second student came back None.
        with nexora.app.app_context():
            user = nexora.User(email=f"tz{uuid4().hex[:6]}@carleton.ca", name="R",
                               password_hash="x", timezone=zone)
            nexora.db.session.add(user)
            nexora.db.session.commit()
            return user.id

    ahead = student_in("Pacific/Kiritimati")
    behind = student_in("Pacific/Niue")
    with nexora.app.app_context():
        today_ahead = nexora._student_today(ahead)
        today_behind = nexora._student_today(behind)
    assert today_ahead != today_behind, "the two zones are always on different days"

    # The same deadline is a different number of days away for each of them,
    # and neither answer is the server's unless the server happens to share
    # their zone.
    target = today_ahead.isoformat()
    assert nexora.days_until(target, today_ahead) == 0
    assert nexora.days_until(target, today_behind) != 0
    # and the default still works outside a request, for the feed and the
    # public reader, which have no student to ask
    assert nexora.days_until(_date.today().isoformat()) == 0
