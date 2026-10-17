"""Recordings get their own heading, below the slides.

Reported from a real ELEC2501B workspace: "Lecture_3" sat directly above
"Lecture 3 Recording", as did Lecture 4, Lecture 05 and both parts of Lecture
06 -- eighteen rows alternating between the thing you read and the thing you
watch. They classify identically (the extension calls both `lecture`) and
carry the same breadcrumb, so nothing downstream could tell them apart.

The split is on the item, server-side. It cannot be a new `content_type` from
the extension: that needs a Web Store release and would leave every row already
imported mixed up for ever, the same trap as the deadline dates and the lecture
links.
"""
from uuid import uuid4

import app as nexora


class Row:
    """Only the three attributes the splitter reads."""
    def __init__(self, title, file_ext="", module_name="Lectures"):
        self.title, self.file_ext, self.module_name = title, file_ext, module_name


# --------------------------------------------------------------------------
# What counts as a recording
# --------------------------------------------------------------------------

def test_the_titles_that_prompted_this_are_recognised():
    """Taken verbatim from the reported screenshot."""
    for title in ("Lecture 3 Recording", "Lecture 4 Recording", "Lecture 05 recording",
                  "Lecture 06 Recording (Part 1)", "Lecture 06 Recording (Part 2)",
                  "Lecture 10 Recording"):
        assert nexora._is_lecture_recording(Row(title)), title


def test_the_slides_beside_them_are_not():
    for title in ("Lecture_01", "Lecture_2", "Lecture_3", "Lecture 4",
                  "Lecture 5", "Lecture 07", "Lecture 02 Notes"):
        assert not nexora._is_lecture_recording(Row(title)), title


def test_a_video_file_is_a_recording_whatever_it_is_called():
    """The half that cannot be wrong: a video sitting in the lecture list IS
    the recording, however the lecturer titled it."""
    for ext in ("mp4", "MP4", ".mov", "webm", "m4a", "mp3"):
        assert nexora._is_lecture_recording(Row("Week 4", ext)), ext


def test_a_document_is_not():
    for ext in ("pdf", "pptx", "docx", "html", ""):
        assert not nexora._is_lecture_recording(Row("Week 4", ext)), ext


def test_the_word_video_alone_does_not_move_a_slide_deck():
    """"Video" is a topic as often as it is a format. Misfiling a deck as a
    recording is a small cost, but it is a cost paid for nothing -- a real
    video lecture carries a video file and is caught by its extension."""
    assert not nexora._is_lecture_recording(Row("Lecture 5: Video Transmission"))
    assert not nexora._is_lecture_recording(Row("Lecture 9 - Video Coding"))


def test_a_module_named_for_recordings_moves_its_items():
    """Some courses put them in their own Brightspace module instead of
    naming each file."""
    assert nexora._is_lecture_recording(Row("Week 7", "", "Lecture Recordings"))
    assert nexora._is_lecture_recording(Row("Week 7", "", "Lecture Capture"))


# --------------------------------------------------------------------------
# The split itself
# --------------------------------------------------------------------------

def test_the_split_keeps_every_row_and_the_order_they_arrived_in():
    """A row that falls into no section is a lecture a student cannot find,
    which is worse than the interleaving this fixes."""
    rows = [Row("Lecture_3"), Row("Lecture 3 Recording"), Row("Lecture 4"),
            Row("Lecture 4 Recording"), Row("Lecture 5")]
    out = nexora._section_course_material(rows, [])
    assert sum(len(v) for v in out.values()) == len(rows)
    assert [r.title for r in out["lecture"]] == ["Lecture_3", "Lecture 4", "Lecture 5"]
    assert [r.title for r in out["recording"]] == ["Lecture 3 Recording",
                                                   "Lecture 4 Recording"]


def test_nothing_in_is_nothing_out():
    assert nexora._section_course_material([], []) == {
        "lecture": [], "recording": [], "lab": [], "other": []}
    assert nexora._section_course_material(None, None)["lecture"] == []


# --------------------------------------------------------------------------
# On the page
# --------------------------------------------------------------------------

def _workspace_with(client, items):
    email = f"lr{uuid4().hex[:7]}@carleton.ca"
    client.post("/signup", data={"name": "L", "email": email, "password": "nexorademo1"})
    token = client.post("/api/sync/extension-key").get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/sync/courses", json={"courses": [{
        "source_uid": "449526", "course_code": "ELEC2501B", "course_name": "Circuits",
        "term": "Fall 2026"}], "complete": True}, headers=headers)
    client.post("/api/sync/content", json={"course": "ELEC2501B", "items": items},
                headers=headers)
    return client.get("/course/ELEC2501B?tab=lectures").get_data(as_text=True)


def _item(uid, title, ext="", module="Lectures"):
    return {"course": "ELEC2501B", "source_uid": uid, "title": title,
            "content_type": "lecture", "module_name": module,
            "source_url": "https://brightspace.carleton.ca/d2l/le/content/449526"
                          f"/viewContent/{uid}/View", "file_ext": ext}


def test_the_page_grows_a_recordings_heading(client):
    body = _workspace_with(client, [_item("1", "Lecture_3"),
                                    _item("2", "Lecture 3 Recording")])
    assert "Lecture recordings" in body


def test_the_recordings_heading_comes_after_the_lectures(client):
    """He asked for it "downwards to keep everything organized" -- the slides
    are what you open first."""
    body = _workspace_with(client, [_item("1", "Lecture_3"),
                                    _item("2", "Lecture 3 Recording")])
    assert body.index("Lecture_3") < body.index("Lecture recordings")


def test_a_recording_is_not_also_listed_among_the_lectures(client):
    """The whole complaint. Rendering it in both places would leave the
    interleaving exactly where it was and add a duplicate.

    Counted inside the lectures panel, not across the document: the title also
    appears in the study-tool source picker and twice in the updates sidebar,
    all of them correct, so a count over the whole page could never pass.
    """
    body = _workspace_with(client, [_item("1", "Lecture_3"),
                                    _item("2", "Lecture 3 Recording")])
    panel = body[body.index('data-panel="lectures"'):]
    panel = panel[:panel.index("</article>")]
    assert panel.count("<strong>Lecture 3 Recording</strong>") == 1
    assert panel.count("<strong>Lecture_3</strong>") == 1


def test_a_course_with_no_recordings_grows_no_empty_heading(client):
    """An empty section is a second empty state on a screen that already has
    one, which reads as two broken things."""
    body = _workspace_with(client, [_item("1", "Lecture_3"), _item("2", "Lecture 4")])
    assert "Lecture recordings" not in body


def test_the_lecture_count_still_counts_both(client):
    """The stat is how many lectures the course has, not how many are slides.
    Splitting the LIST must not quietly halve the NUMBER beside it."""
    body = _workspace_with(client, [_item("1", "Lecture_3"),
                                    _item("2", "Lecture 3 Recording"),
                                    _item("3", "Lecture 4")])
    assert "<small>Lectures</small><strong>3</strong>" in body


def test_a_recording_still_links_to_its_file(client):
    """It is still a lecture row: the download rewrite and the security gate
    both have to keep applying to it."""
    body = _workspace_with(client, [_item("2", "Lecture 3 Recording", "pdf")])
    assert "topics/files/download/2/DirectFileTopicDownload" in body


# --------------------------------------------------------------------------
# The Lectures list is teaching material, not everything in a week folder
#
# Reported against a real SYSC2004A workspace: "in the lectures section theres
# like so many types recordings, then labs, then lectures again which arent
# lectures like theres SO much useless stuff".
#
# Measured against the published `classify.js`: `LECTURE_NUMBERED` is tested on
# the item's text PLUS its module name, and "Week 1" matches `week\s*\d+`, so
# EVERY item in a week-numbered module was stored as `lecture` -- the Piazza
# link, the welcome note, the integrity policy, a Zoom link. The extension fix
# only helps what is scraped after it ships; these pin the server half, which
# fixes the rows already in the database.
# --------------------------------------------------------------------------

def test_a_link_in_a_week_folder_is_not_a_lecture():
    """Stored as content_type `lecture` by the published extension. The page
    must not show it as one."""
    for title in ("Piazza", "Welcome!", "Grades FAQ", "Academic Integrity Policy",
                  "Java Install Guide", "Zoom link for help sessions"):
        assert nexora._material_section(Row(title, module_name="Week 1")) == "other", title


def test_a_real_lecture_in_that_same_week_folder_survives():
    """Dropping these to fix the six above would be the opposite failure and
    the worse one: losing the material a student came for."""
    for title in ("Lecture 5 - Inheritance", "05 Notes", "Topic 4 slides",
                  "Week 5 Overview"):
        assert nexora._material_section(Row(title, module_name="Week 5")) == "lecture", title


def test_a_module_that_names_the_material_vouches_for_a_file_or_a_number():
    """"Lectures" says what the thing IS; "Week 1" says when it happened. A
    course that files "Inheritance.pdf" under a module called Lectures has told
    us what it is -- but the module cannot vouch on its own, or a course that
    keeps everything in one "Course material" folder would empty that folder
    back into the Lectures list, Piazza link and all."""
    assert nexora._material_section(Row("Inheritance", "pdf", "Lectures")) == "lecture"
    assert nexora._material_section(Row("04", module_name="Lecture Slides")) == "lecture"
    # neither a number nor a file behind it: the one shape that is never a lecture
    assert nexora._material_section(Row("Piazza", module_name="Lectures")) == "other"
    assert nexora._material_section(Row("Inheritance", "pdf", "Week 1")) == "other"


def test_a_deck_is_lecture_material_whatever_it_is_called():
    assert nexora._material_section(Row("Inheritance", "pptx", "Week 1")) == "lecture"


def test_labs_and_tutorials_get_their_own_section():
    for title in ("Lab 3 - ArrayLists", "Tutorial 7", "Laboratory 2",
                  "Workshop 4", "Seminar 1", "Lab 4 Submission Instructions"):
        assert nexora._material_section(Row(title)) == "lab", title


def test_a_lab_module_claims_its_items():
    assert nexora._material_section(Row("Cheek cell protocol", module_name="Lab 1")) == "lab"


def test_a_code_archive_is_not_a_lab():
    """`\blab\b` must not match "lab2_starter" -- a zip belongs with the
    resources, which is where a student looks for starter code."""
    assert nexora._material_section(Row("lab2_starter.zip", "zip")) != "lab"


def test_a_lab_sitting_in_the_resource_bucket_is_still_surfaced():
    """The extension has no lab rule at all, so "Tutorial 7" falls through to
    `page` and lands among the resources. A student looking for it does not
    care which bucket it was in."""
    out = nexora._section_course_material([], [Row("Tutorial 7"), Row("Course Handbook")])
    assert [r.title for r in out["lab"]] == ["Tutorial 7"]
    assert [r.title for r in out["other"]] == ["Course Handbook"]


def test_a_resource_is_not_promoted_into_the_lectures():
    """Only lab and recording material is pulled out of the resource bucket.
    Hoovering every pdf that says "notes" into the Lectures list would be the
    same over-reach in the other direction."""
    out = nexora._section_course_material([], [Row("Revision notes.pdf", "pdf")])
    assert out["lecture"] == []
    assert len(out["other"]) == 1


def test_nothing_is_ever_dropped():
    """This can only MOVE a row. A section that loses one is worse than the
    mess it replaces."""
    lectures = [Row("Piazza", module_name="Week 1"), Row("Lecture 2"),
                Row("Lecture 2 Recording"), Row("Lab 1")]
    resources = [Row("Handbook.pdf", "pdf"), Row("Tutorial 3")]
    out = nexora._section_course_material(lectures, resources)
    assert sum(len(v) for v in out.values()) == 6
    seen = {r.title for v in out.values() for r in v}
    assert seen == {"Piazza", "Lecture 2", "Lecture 2 Recording", "Lab 1",
                    "Handbook.pdf", "Tutorial 3"}


def test_the_page_renders_the_four_sections_in_order(client):
    body = _workspace_with(client, [
        _item("1", "Lecture 3"), _item("2", "Lecture 3 Recording"),
        _item("3", "Lab 2"), _item("4", "Piazza", module="Week 1")])
    panel = body[body.index('data-panel="lectures"'):]
    panel = panel[:panel.index("</article>")]
    order = [panel.index(h) for h in
             ("Lecture 3", "Lecture recordings", "Labs and tutorials",
              "Other course material")]
    assert order == sorted(order), "the headings are out of order"


def test_the_stat_agrees_with_the_list(client):
    """Four rows arrive as `lecture`; two of them are not teaching material.
    A stat reading 4 above a list of 2 is the "37 courses in the extension, 11
    on the dashboard" bug one screen down."""
    body = _workspace_with(client, [
        _item("1", "Lecture 3"), _item("2", "Lecture 3 Recording"),
        _item("3", "Piazza", module="Week 1"),
        _item("4", "Academic Integrity Policy", module="Week 1")])
    assert "<small>Lectures</small><strong>2</strong>" in body
