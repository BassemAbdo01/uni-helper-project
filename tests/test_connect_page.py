"""The page that tells a student how to install the extension.

Two things it must not do: send a student to a university that is not theirs,
and show an Install button that goes nowhere.
"""

import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"
STORE = "https://chromewebstore.google.com/detail/nexora-sync/exampleid"


@pytest.fixture()
def signed_in(client):
    client.post("/signup", data={"name": "S", "email": "connect@carleton.ca",
                                 "password": PASSWORD})
    return client


def add_course(email, brightspace_url):
    import app as nexora
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user.id, course_code="BIOL1103",
            course_name="Biology", term="Fall 2026",
            brightspace_url=brightspace_url))
        nexora.db.session.commit()


# --------------------------------------------------------- the install link

def test_no_dead_install_button_before_the_store_id_is_known(signed_in, monkeypatch):
    """A button that goes nowhere is worse than a sentence saying what to
    search for -- and a working download beats both.

    This asserted the fallback SENTENCE. The page now serves the packaged
    extension instead, because "search the Chrome Web Store" is a first-run
    instruction nobody can follow while there is nothing on the store to find.
    What has to stay true is that the student is never left with a dead link
    and no way forward, which is what this now checks.
    """
    import app as nexora
    monkeypatch.setattr(nexora, "EXTENSION_URL", "")
    body = signed_in.get("/connect-brightspace").get_data(as_text=True)
    assert "Install Nexora Sync</a>" not in body, "no button pointing at a store listing that does not exist"
    assert ("Download Nexora Sync" in body) or ("Search the Chrome Web Store" in body), \
        "with no store link there must still be a way to get the extension"


def test_the_install_button_appears_once_the_url_is_set(signed_in, monkeypatch):
    import app as nexora
    monkeypatch.setattr(nexora, "EXTENSION_URL", STORE)
    body = signed_in.get("/connect-brightspace").get_data(as_text=True)
    assert STORE in body
    assert "Install Nexora Sync" in body
    assert "Search the Chrome Web Store" not in body


def test_the_url_is_configuration_not_a_hardcoded_link():
    """It changes when the store item changes, and it is not known yet."""
    import app as nexora
    assert isinstance(nexora.EXTENSION_URL, str)
    body = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "templates", "connect_brightspace.html"),
        encoding="utf-8").read()
    assert "chromewebstore" not in body, "store URL hardcoded into the template"


# ----------------------------------------------------- whose Brightspace

def test_it_never_sends_a_student_to_another_university(signed_in):
    """The page shipped with a hard link to brightspace.carleton.ca. The
    scraper is deliberately university-agnostic; the page has to be too."""
    body = signed_in.get("/connect-brightspace").get_data(as_text=True)
    assert "carleton.ca" not in body.lower()


def test_with_nothing_imported_it_explains_rather_than_guesses(signed_in):
    body = signed_in.get("/connect-brightspace").get_data(as_text=True)
    assert "any university" in body.lower()
    assert ">\n            Open Brightspace" not in body


def test_it_uses_the_students_own_brightspace_once_known(signed_in):
    add_course("connect@carleton.ca", "https://learn.otheruni.edu/d2l/home/8842")
    body = signed_in.get("/connect-brightspace").get_data(as_text=True)
    assert "https://learn.otheruni.edu/d2l/home" in body
    assert "Open Brightspace" in body


def test_a_course_with_no_url_does_not_produce_a_broken_link(signed_in):
    add_course("connect@carleton.ca", "")
    body = signed_in.get("/connect-brightspace").get_data(as_text=True)
    assert 'href=""' not in body
    assert "any university" in body.lower()


@pytest.mark.parametrize("stored,expected", [
    ("https://learn.school.ca/d2l/le/content/123", "https://learn.school.ca/d2l/home"),
    ("https://brightspace.uni.edu/d2l/home", "https://brightspace.uni.edu/d2l/home"),
    ("not a url", ""),
    ("", ""),
])
def test_the_origin_is_taken_from_the_stored_course_url(client, stored, expected):
    import app as nexora
    client.post("/signup", data={"name": "S", "email": f"o{abs(hash(stored))}@x.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=f"o{abs(hash(stored))}@x.ca").first()
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user.id, course_code="X", course_name="X",
            term="Fall 2026", brightspace_url=stored))
        nexora.db.session.commit()
        assert nexora._known_brightspace_home(user.id) == expected
