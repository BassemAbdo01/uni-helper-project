"""An assignment deadline links to the assignment, not to its upload form.

Reported with a screenshot: clicking "Assignment 8c Innovation Proposal
Assignment Guidelines - Final Submission (Homework)" on the dashboard, due that
day, opened Carleton's **Internal Error** page.

`folder_submit_files.d2l` is D2L's file-UPLOAD form. A folder that takes no
file -- submitted on paper, or observed in person, which is what "(Homework)"
was -- has no such page, and D2L 500s. Same shape as the lecture links one
layer over: the ACTION endpoint linked instead of the VIEW endpoint.

**The target was measured, not guessed, and the first guess was wrong.** Probed
against Carleton unauthenticated, where a route that exists redirects to login
and one that does not returns the same 303 to /d2l/error/404 as a deliberately
invented control:

    folder_submit_files.d2l       302 -> login    exists (and 500s signed in)
    folder_user_view.d2l          303 -> 404      does not exist
    folder_history.d2l            303 -> 404      does not exist
    folder.d2l                    303 -> 404      does not exist
    /d2l/le/dropbox/<ou>/<db>/View                exists

`folder_user_view.d2l` was the first proposal and would have swapped a 500 for
a 404. The survivor was then confirmed by the owner against his own session.
"""
import app as nexora

OU, DB = "448728", "304846"
BROKEN = (f"https://brightspace.carleton.ca/d2l/lms/dropbox/user/folder_submit_files"
          f".d2l?db={DB}&grpid=0&isprv=0&bp=0&ou={OU}")
FIXED = f"https://brightspace.carleton.ca/d2l/le/dropbox/{OU}/{DB}/View"


# --------------------------------------------------------------------------
# The reported URL
# --------------------------------------------------------------------------

def test_the_reported_link_becomes_the_assignment():
    """Verbatim from the report."""
    assert nexora.content_href(BROKEN) == FIXED


def test_the_ids_are_read_out_of_the_url_never_invented():
    out = nexora.content_href(
        "https://b.example.ca/d2l/lms/dropbox/user/folder_submit_files.d2l"
        "?db=777&grpid=0&isprv=0&bp=0&ou=999")
    assert out == "https://b.example.ca/d2l/le/dropbox/999/777/View"


def test_the_query_order_does_not_matter():
    """D2L writes these in its own order and the extension in another."""
    for query in (f"ou={OU}&db={DB}", f"db={DB}&ou={OU}",
                  f"grpid=0&ou={OU}&isprv=0&db={DB}&bp=0"):
        url = f"https://brightspace.carleton.ca/d2l/lms/dropbox/user/folder_submit_files.d2l?{query}"
        assert nexora.content_href(url) == FIXED, query


# --------------------------------------------------------------------------
# What it must not touch
# --------------------------------------------------------------------------

def test_a_url_missing_either_id_is_left_exactly_alone():
    """A half-built link is worse than the upload form we already have: at
    least that one is honest about which assignment it means."""
    for query in ("db=304846", "ou=448728", "", "db=&ou=",
                  "db=abc&ou=448728", "db=304846&ou=../etc"):
        url = ("https://brightspace.carleton.ca/d2l/lms/dropbox/user/"
               "folder_submit_files.d2l?" + query)
        assert nexora.content_href(url) == nexora._lms_link(url), query


def test_a_quiz_link_is_untouched():
    """Quizzes use quiz_summary.d2l, a different route, which the same probe
    found alive. There is no evidence it is broken and no reason to move it."""
    quiz = f"https://brightspace.carleton.ca/d2l/lms/quizzing/user/quiz_summary.d2l?qi=55&ou={OU}"
    assert nexora.content_href(quiz) == quiz


def test_a_lecture_link_still_becomes_a_download():
    """Two rewrites now share the chain; neither may eat the other."""
    view = "https://brightspace.carleton.ca/d2l/le/content/449526/viewContent/4894237/View"
    assert nexora.content_href(view, "pdf").endswith(
        "/d2l/le/content/449526/topics/files/download/4894237/DirectFileTopicDownload")
    assert nexora.content_href(view) == view


def test_an_already_fixed_link_is_stable():
    """Rows imported after the extension ships already carry this shape, and a
    rewrite that moved it again would be a loop waiting to happen."""
    assert nexora.content_href(FIXED) == FIXED


def test_our_own_upload_is_untouched():
    assert nexora.content_href("/static/nexora_uploads/a.pdf") == "/static/nexora_uploads/a.pdf"


# --------------------------------------------------------------------------
# The security gate still runs first
# --------------------------------------------------------------------------

def test_a_javascript_url_cannot_reach_the_rewrite():
    """`_lms_link` is a security boundary and every rewrite runs over what it
    allowed, never beside it."""
    assert nexora.content_href(
        "javascript:alert(1)/d2l/lms/dropbox/user/folder_submit_files.d2l?db=1&ou=2") == ""


def test_userinfo_is_still_refused():
    assert nexora.content_href(
        "https://brightspace.example.ca@evil.tld/d2l/lms/dropbox/user/"
        "folder_submit_files.d2l?db=1&ou=2") == ""


def test_the_host_is_carried_over_not_assumed():
    """Another university's Brightspace must stay on its own host -- the
    scraper is generic by design."""
    out = nexora.content_href(
        "https://learn.example.edu/d2l/lms/dropbox/user/folder_submit_files.d2l?db=5&ou=6")
    assert out.startswith("https://learn.example.edu/")


# --------------------------------------------------------------------------
# The extension builds the same shape
# --------------------------------------------------------------------------

def test_both_extension_copies_build_the_assignment_url():
    """The server repairs stored rows; the extension stops creating bad ones.
    Both copies have to change -- the worker and the content script each build
    this URL, and one of them being missed is a bug that only shows on the
    path that happens to run."""
    import io as _io
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("background.js", "content.js"):
        src = _io.open(os.path.join(here, "extension", name), encoding="utf-8").read()
        assert "/d2l/le/dropbox/" in src, name
        # The CONSTRUCTED url, not any mention: both files now carry a comment
        # explaining why the upload form was wrong, and naming it there is the
        # point of the comment.
        assert "folder_submit_files.d2l?db=" not in src,             f"{name} still builds the upload form"
