"""A lecture link has to save the lecture.

Reported three times, and wrong for a different reason each time:

1. The extension stored D2L's content *API* endpoint
   (`/d2l/api/le/<v>/<ou>/content/topics/<id>/file`) as the link. That is fine
   as a fetch and answers `{ Errors: [ {Message: "Forbidden"} ] }` on a blank
   white page as a top-level navigation.
2. `_browsable_d2l` rewrote that, on read, into D2L's own viewer route -- which
   opens, so the error went away, but a viewer is not a download.
3. D2L had been handing over `TypeIdentifier: "File"` and a real path the whole
   time, and the old code preferred the API endpoint for EVERY topic with an id,
   which is why a stored link says nothing about whether the item is a file.

Measured against a real Carleton lecture before this was written:

    topics/files/download/<id>/DirectFileTopicDownload
        -> 200 application/pdf, Content-Disposition: attachment   <- saves it
    topic.Url  (/content/enforced/.../Lecture_3.pdf)
        -> 200 application/pdf, no disposition -> opens the PDF viewer
    api/le/<v>/<ou>/content/topics/<id>/file
        -> 200 as a fetch, Forbidden as a navigation

The route is read out of the markup of D2L's own content page, so Nexora is
still not constructing a link of its own invention -- origin, org unit and
topic id all come from the LMS.

These are text assertions on the extension sources because there is no node on
this machine, and because the thing most likely to go wrong is the thing
CLAUDE.md warns about: `background.js` and `content.js` carry the same logic
twice and have drifted before.
"""
import io
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(HERE, "extension")
COPIES = ("background.js", "content.js")


def _src(name):
    with io.open(os.path.join(EXT, name), encoding="utf-8") as fh:
        return fh.read()


def test_both_copies_build_the_download_route():
    """The one route that answers with Content-Disposition: attachment."""
    for name in COPIES:
        src = _src(name)
        assert "DirectFileTopicDownload" in src, name
        assert re.search(r"/d2l/le/content/\$\{[^}]+\}/topics/files/download/"
                         r"\$\{topicId\}/DirectFileTopicDownload", src), name


def test_the_download_route_is_gated_on_d2ls_own_type():
    """A topic is just as often an HTML page, a link or a quiz. This route means
    nothing for those, so it must not be built for every topic that has an id --
    which is exactly the mistake that produced the Forbidden links."""
    for name in COPIES:
        src = _src(name)
        m = re.search(r"const fileUrl = \((.*?)\)\s*\n", src)
        assert m, name
        assert "topicId" in m.group(1), name
        assert "TypeIdentifier" in m.group(1), name
        assert "file" in m.group(1).lower(), name


def test_the_download_route_leads_the_stored_link():
    """absoluteUrl opens the browser's PDF viewer rather than saving, so when
    D2L says the topic is a file the download route has to win."""
    for name in COPIES:
        src = _src(name)
        assert "source_url: fileUrl || absoluteUrl || viewUrl" in src, name


def test_a_non_file_topic_still_falls_back_to_something_openable():
    """The gate returns '' for a page, a link or a quiz, and those must keep the
    link they had -- a download button that 404s is worse than no button."""
    for name in COPIES:
        src = _src(name)
        assert re.search(r"const fileUrl = .*?\n.*?\n\s*: '';", src), name
        assert "absoluteUrl || viewUrl" in src, name


def test_classification_still_sees_a_file_extension():
    """classifyContent reads the extension off the URL it is handed, and the
    download route carries no ".pdf". Handing it the download URL would file
    every lecture as a `page` -- which hides it from the Lectures list rather
    than losing it, the failure mode CLAUDE.md calls out for this function."""
    for name in COPIES:
        src = _src(name)
        assert "classify_url: absoluteUrl || viewUrl" in src, name
        assert "classifyContent(title, file.classify_url," in src, name
        assert "classifyContent(title, file.source_url," not in src, name


def test_the_two_copies_have_not_drifted():
    """There are two identical implementations of this. They have drifted before
    -- `flattenToc`'s empty-array bug had to be fixed in both."""
    bodies = []
    for name in COPIES:
        m = re.search(r"function fileInfoFromTopic\(.*?\n\s*\}\n", _src(name), re.S)
        assert m, name
        body = m.group(0)
        # The worker is handed the origin because it has no location of its own;
        # the content script reads it off the page. That, and the names for the
        # org unit, are the ONLY differences the two are allowed to have.
        body = body.replace("location.origin", "ORIGIN").replace("${origin}", "${ORIGIN}")
        body = body.replace("course.source_uid", "OU").replace("orgUnitId", "OU")
        body = body.replace("absolute(origin, url)", "absolute(url)")
        body = body.replace("(topic, course, version, origin)", "(topic, OU, version)")
        bodies.append(re.sub(r"\s+", " ", body).strip())
    assert bodies[0] == bodies[1]


def test_the_version_was_bumped_so_the_store_build_is_not_the_old_one():
    """A fix that ships under the version already in the store reaches nobody."""
    with io.open(os.path.join(EXT, "manifest.json"), encoding="utf-8") as fh:
        version = json.load(fh)["version"]
    assert tuple(int(p) for p in version.split(".")) >= (3, 5, 3), version


def test_an_empty_href_is_not_a_link():
    """`new URL('', base)` resolves to the BASE and that string is truthy, so a
    topic with no URL of its own was linking to Brightspace's front page (the
    worker) or to whatever page the scraper happened to be on (the content
    script) -- a link that confidently opens the wrong thing. Found by running
    `fileInfoFromTopic` against a topic with no Url, not by reading it."""
    for name in COPIES:
        src = _src(name)
        assert "if (!String(href || '').trim()) return '';" in src, name


def test_there_is_a_browser_check_for_this():
    """There is no node on this machine, so the only way the real function gets
    run against real Brightspace JSON is a page served out of extension/."""
    path = os.path.join(EXT, "topicfile-check.html")
    assert os.path.exists(path)
    page = io.open(path, encoding="utf-8").read()
    assert "DirectFileTopicDownload" in page
    assert "new Function(src)" in page          # the whole-file syntax check
