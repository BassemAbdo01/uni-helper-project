"""An assignment deadline links where D2L itself links it.

Reported twice, and the second report was caused by the first fix.

**Round one.** Clicking "Assignment 8c Innovation Proposal Assignment
Guidelines - Final Submission (Homework)" opened Carleton's **Internal
Error**. `folder_submit_files.d2l` is D2L's file-upload form, and a folder
that takes no file has no such page, so the conclusion was that the ACTION
endpoint was linked where the VIEW endpoint belonged. The replacement,
`/d2l/le/dropbox/<ou>/<db>/View`, was probed unauthenticated and answered 200
where invented controls answered 303 to /d2l/error/404.

**Round two.** *"I keeeeeeeep clicking the external link to be directed to
brightspace for the assignments and stuff but i keep getting this error msg"*
-- Carleton's **Page Not Found**, on every assignment.

That 200 proved nothing. The route defers auth to JavaScript, so it answers
200 for a nonsense `ou` AND a nonsense `db` -- which the probe itself recorded
and the conclusion then ignored. Signed in, it redirects to /d2l/error/404.

**Measured in the owner's own session, which is what settled it.** D2L's own
Assignments list (`folders_list.d2l?ou=`) was opened for two real courses and
every folder in both -- including the "(Homework)" kind that started round one
-- links to `folder_submit_files.d2l`:

| folder | folder_submit_files.d2l | /d2l/le/dropbox/<ou>/<db>/View |
|---|---|---|
| SYSC2004A Lab 09, db 304803 | **opens the assignment** | 404 |
| CCDP2100C #11b (Homework), db 304833 | 500 | 404 |

So the original link was right for the great majority and the rewrite broke
all of them to avoid a 500 on a few. A folder that accepts no file still
answers Internal Error -- and D2L's own list sends a student to exactly the
same page, so that is Brightspace's behaviour and not ours to invent around.

**Nexora never constructs a link.** This is the rule the lecture-download work
already wrote down, applied to the one place that broke it.
"""
import app as nexora

OU, DB = "448728", "304846"
# What the LMS gives us, and what must survive untouched.
REAL = (f"https://brightspace.carleton.ca/d2l/lms/dropbox/user/folder_submit_files"
        f".d2l?db={DB}&grpid=0&isprv=0&bp=0&ou={OU}")
# What Nexora invented for one release, and which 404s at Carleton.
DEAD = f"https://brightspace.carleton.ca/d2l/le/dropbox/{OU}/{DB}/View"


# --------------------------------------------------------------------------
# The link the LMS gave is the link we render
# --------------------------------------------------------------------------

def test_the_assignment_url_is_passed_through_untouched():
    """Verbatim from the dashboard. Rewriting this is what sent every
    assignment to Page Not Found."""
    assert nexora.content_href(REAL) == REAL


def test_no_assignment_url_is_rewritten_whatever_its_query_order():
    """D2L writes these in its own order and the extension in another; neither
    may be turned into something else."""
    for query in (f"ou={OU}&db={DB}", f"db={DB}&ou={OU}",
                  f"grpid=0&ou={OU}&isprv=0&db={DB}&bp=0"):
        url = ("https://brightspace.carleton.ca/d2l/lms/dropbox/user/"
               f"folder_submit_files.d2l?{query}")
        assert nexora.content_href(url) == url, query


def test_a_folder_that_takes_no_file_is_still_linked_the_same_way():
    """The (Homework) case that started round one. It answers Internal Error,
    and D2L's own Assignments list links there too -- so Nexora linking
    somewhere else is inventing a page, which is how every other assignment
    got broken."""
    homework = ("https://brightspace.carleton.ca/d2l/lms/dropbox/user/"
                "folder_submit_files.d2l?db=304833&grpid=0&isprv=0&bp=0&ou=448728")
    assert nexora.content_href(homework) == homework


# --------------------------------------------------------------------------
# A row already stored with the invented URL is repaired
# --------------------------------------------------------------------------

def test_the_invented_url_is_turned_back_into_the_real_one():
    """The bad rewrite ran at render time, so dropping it repairs every row
    scraped by the published extension. The unreleased builds put the invented
    URL into the payload itself, and only a re-import would clear those --
    so it is reversed here instead, which costs nothing."""
    assert nexora.content_href(DEAD) == REAL


def test_the_repair_keeps_the_ids_the_right_way_round():
    """`/d2l/le/dropbox/<ou>/<db>/View` is org then folder; the query is db
    then ou. Swapping them silently links to a different assignment."""
    out = nexora.content_href("https://b.example.ca/d2l/le/dropbox/999/777/View")
    assert "db=777" in out and "ou=999" in out


def test_the_repair_carries_the_host_over():
    """The scraper is generic by design; another university's Brightspace must
    stay on its own host."""
    out = nexora.content_href("https://learn.example.edu/d2l/le/dropbox/6/5/View")
    assert out.startswith("https://learn.example.edu/d2l/lms/dropbox/")


def test_the_repair_is_stable():
    """Once repaired it is an ordinary assignment URL and nothing moves it
    again -- two rewrites pointing at each other is a loop waiting to happen."""
    assert nexora.content_href(nexora.content_href(DEAD)) == REAL


# --------------------------------------------------------------------------
# What it must not touch
# --------------------------------------------------------------------------

def test_a_real_content_view_url_is_not_mistaken_for_the_dead_one():
    """`/d2l/le/content/.../View` is a real route and looks alike. Catching it
    here would break every lecture link on the page."""
    view = "https://brightspace.carleton.ca/d2l/le/content/449526/viewContent/4894237/View"
    assert nexora.content_href(view) == view


def test_a_quiz_link_is_untouched():
    """Quizzes use quiz_summary.d2l, a different route, which the same session
    confirmed alive. There is no evidence it is broken."""
    quiz = ("https://brightspace.carleton.ca/d2l/lms/quizzing/user/"
            f"quiz_summary.d2l?qi=55&ou={OU}")
    assert nexora.content_href(quiz) == quiz


def test_a_lecture_link_still_becomes_a_download():
    """Two rewrites share the chain; neither may eat the other."""
    view = "https://brightspace.carleton.ca/d2l/le/content/449526/viewContent/4894237/View"
    assert nexora.content_href(view, "pdf").endswith(
        "/d2l/le/content/449526/topics/files/download/4894237/DirectFileTopicDownload")


def test_our_own_upload_is_untouched():
    assert nexora.content_href("/static/nexora_uploads/a.pdf") == "/static/nexora_uploads/a.pdf"


# --------------------------------------------------------------------------
# The security gate still runs first
# --------------------------------------------------------------------------

def test_a_javascript_url_cannot_reach_the_repair():
    """`_lms_link` is a security boundary and every rewrite runs over what it
    allowed, never beside it."""
    assert nexora.content_href("javascript:alert(1)/d2l/le/dropbox/1/2/View") == ""


def test_userinfo_is_still_refused():
    assert nexora.content_href(
        "https://brightspace.example.ca@evil.tld/d2l/le/dropbox/1/2/View") == ""


# --------------------------------------------------------------------------
# The extension builds the same shape
# --------------------------------------------------------------------------

def test_neither_extension_copy_builds_the_invented_url():
    """Both copies have to change -- the worker and the content script each
    build this URL, and one of them being missed is a bug that only shows on
    the path that happens to run. A row scraped with the invented URL is
    stored broken, which the server can repair but should never receive."""
    import io as _io
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("background.js", "content.js"):
        src = _io.open(os.path.join(here, "extension", name), encoding="utf-8").read()
        assert "folder_submit_files.d2l" in src, f"{name} must link where D2L links"
        assert "/d2l/le/dropbox/${" not in src, f"{name} still builds the invented URL"
