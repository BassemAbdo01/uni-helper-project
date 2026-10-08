"""A lecture link has to open a page, not an API endpoint.

Reported from a phone: tapping any lecture in Nexora opened a blank white page
reading `{ Errors: [ {Message: "Forbidden"} ] }`. The extension had stored D2L's
content *API* URL as the item's link. That endpoint answers a fetch carrying the
student's Brightspace session; as a top-level navigation it is refused.

The rewrite lives in `_lms_link`, the single gate every rendered link passes
through, so rows imported before the fix are corrected on read.
"""
import app as nexora


API = "https://brightspace.carleton.ca/d2l/api/le/1.67/412755/content/topics/5398211/file"
VIEW = "https://brightspace.carleton.ca/d2l/le/content/412755/viewContent/5398211/View"


def test_the_api_file_endpoint_becomes_the_page_a_browser_can_open():
    assert nexora._lms_link(API) == VIEW


def test_the_rewrite_invents_nothing_every_part_comes_from_the_original():
    out = nexora._lms_link(API)
    # host, org unit and topic id are all read back out of the URL D2L gave us
    assert "brightspace.carleton.ca" in out
    assert "412755" in out and "5398211" in out
    assert "/d2l/api/" not in out


def test_a_trailing_slash_is_still_recognised():
    assert nexora._lms_link(API + "/") == VIEW


def test_an_ordinary_lms_link_is_left_exactly_as_it_was():
    plain = "https://brightspace.carleton.ca/d2l/le/content/412755/Home"
    assert nexora._lms_link(plain) == plain
    enforced = "https://brightspace.carleton.ca/content/enforced/412755/Lecture%2004.pdf"
    assert nexora._lms_link(enforced) == enforced


def test_a_quiz_or_dropbox_link_is_untouched():
    quiz = "https://brightspace.carleton.ca/d2l/lms/quizzing/user/quiz_summary.d2l?qi=9&ou=412755"
    assert nexora._lms_link(quiz) == quiz


def test_a_different_d2l_api_route_is_not_rewritten():
    # only the content-topic file endpoint is known to be unopenable; nothing
    # else may be reshaped on a guess
    other = "https://brightspace.carleton.ca/d2l/api/le/1.67/412755/content/root/"
    assert nexora._lms_link(other) == other


def test_the_security_gate_still_refuses_what_it_always_refused():
    assert nexora._lms_link("javascript:alert(1)") == ""
    assert nexora._lms_link("https://brightspace.carleton.ca@evil.tld/") == ""
    assert nexora._lms_link("/d2l/le/content/1/viewContent/2/View") == ""
    assert nexora._lms_link("") == ""
    assert nexora._lms_link(None) == ""


def test_a_rewritten_link_still_passes_the_gate_it_came_through():
    # the rewrite happens inside _lms_link, so its output must itself be a URL
    # the gate would accept -- otherwise the fix would smuggle something past it
    assert nexora._lms_link(nexora._lms_link(API)) == VIEW


# ---------------------------------------------------------------------------
# The course workspace rendered row.source_url straight into an href, so it
# never reached _lms_link at all: the D2L rewrite could not run there, and a
# scraped javascript: URL would have become an anchor on our own page. Every
# link now goes through content_href.
# ---------------------------------------------------------------------------
import re
import pathlib


def test_content_href_applies_the_rewrite_the_workspace_was_missing():
    assert nexora.content_href(API) == VIEW


def test_content_href_still_refuses_what_the_gate_refuses():
    assert nexora.content_href("javascript:alert(1)") == ""
    assert nexora.content_href("https://brightspace.carleton.ca@evil.tld/") == ""
    assert nexora.content_href("") == ""
    assert nexora.content_href(None) == ""


def test_a_file_the_student_uploaded_is_served_by_us_and_allowed_through():
    # stored relative by url_for('static', ...), which _lms_link rejects for
    # having no host -- correctly, so it is allowed here explicitly instead of
    # by loosening the gate
    own = "/static/nexora_uploads/7/BIOL1103/outline.pdf"
    assert nexora.content_href(own) == own


def test_an_unrelated_relative_path_is_still_refused():
    assert nexora.content_href("/etc/passwd") == ""
    assert nexora.content_href("/static/../secret") == ""


def test_the_filter_is_registered_so_the_templates_can_use_it():
    assert nexora.app.jinja_env.filters.get("content_href") is nexora.content_href


def test_no_template_puts_a_scraped_url_into_an_href_ungated():
    """The regression itself: an href built from a raw source_url or link."""
    bad = []
    for path in pathlib.Path("templates").glob("*.html"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in re.finditer(r'href="\{\{\s*([^}]+?)\s*\}\}"', line):
                expr = m.group(1)
                if re.search(r"source_url|source_link|\.link\b", expr) and "content_href" not in expr:
                    bad.append("%s:%d  %s" % (path.name, n, expr))
    assert not bad, "ungated LMS URL in an href:\n" + "\n".join(bad)
