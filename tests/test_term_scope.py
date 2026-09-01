"""Importing every term must not mean opening on every term.

The extension now asks, once, whether to bring in everything or a single term.
"Everything" is only a usable answer if the dashboard still opens on the term
the student is actually in, so the two halves are tested together: the page
picks the current term by the calendar, hides the rest behind a picker, and
falls back to the newest term when the calendar's answer is not one the student
is enrolled in.
"""

import os
import re
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="terms@carleton.ca"):
    return client.post("/signup", data={"name": "Term Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


def current_term(today=None):
    today = today or date.today()
    season = "Winter" if today.month <= 4 else "Summer" if today.month <= 8 else "Fall"
    return f"{season} {today.year}"


def add_courses(email, courses):
    """courses: [(code, name, term)]"""
    import app as nexora
    from uuid import uuid4
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        for code, name, term in courses:
            nexora.db.session.add(nexora.SyncedCourse(
                id=uuid4().hex, user_id=user.id, course_code=code,
                course_name=name, term=term, content_count=3))
        nexora.db.session.commit()


@pytest.fixture()
def many_terms(client):
    signup(client)
    add_courses("terms@carleton.ca", [
        ("BIOL1103", "Foundations of Biology I", current_term()),
        ("SYSC2004", "Object-Oriented Software Development", current_term()),
        ("MATH1104", "Linear Algebra", "Winter 2025"),
        ("PHYS1004", "Electromagnetism", "Fall 2024"),
    ])
    return client


def visible_terms(body):
    """The term groups the page renders open, in order."""
    return [m.group(1) for m in
            re.finditer(r'<div class="termgroup" data-term="([^"]+)"(?! hidden)', body)]


def test_the_dashboard_opens_on_the_term_the_student_is_in(many_terms):
    body = many_terms.get("/dashboard").get_data(as_text=True)
    assert visible_terms(body) == [current_term()]


def test_the_other_terms_are_rendered_but_hidden(many_terms):
    body = many_terms.get("/dashboard").get_data(as_text=True)
    # present, so switching needs no round trip
    assert 'data-term="Winter 2025" hidden' in body
    assert 'data-term="Fall 2024" hidden' in body


def test_every_term_is_offered_in_the_picker(many_terms):
    body = many_terms.get("/dashboard").get_data(as_text=True)
    assert 'id="termPick"' in body
    for label in (current_term(), "Winter 2025", "Fall 2024"):
        assert f'<option value="{label}"' in body
    assert 'value="__all__"' in body


def test_a_student_with_one_term_gets_no_picker(client):
    signup(client, email="single@carleton.ca")
    add_courses("single@carleton.ca", [("BIOL1103", "Biology", current_term())])
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'id="termPick"' not in body
    assert visible_terms(body) == [current_term()]


def test_it_falls_back_to_the_newest_term_when_this_one_is_empty(client):
    """A student between terms, or one who only imported finished courses,
    should not be shown an empty page with their courses hidden behind it."""
    signup(client, email="past@carleton.ca")
    add_courses("past@carleton.ca", [
        ("MATH1104", "Linear Algebra", "Winter 2025"),
        ("PHYS1004", "Electromagnetism", "Fall 2024"),
    ])
    body = client.get("/dashboard").get_data(as_text=True)
    assert visible_terms(body) == ["Winter 2025"]


def test_the_picker_sits_in_the_courses_header_next_to_the_count(many_terms):
    """Bassem asked for it in that exact spot, beside the total. Pinned because
    a header this small is easy to reflow by accident."""
    body = many_terms.get("/dashboard").get_data(as_text=True)
    header = body[body.index('<section class="card" id="courses">'):]
    header = header[:header.index("</header>")]
    assert 'id="termPick"' in header
    assert "course" in header.split('id="termPick"')[1]   # the count follows it


def test_every_course_is_still_rendered_so_switching_needs_no_reload(many_terms):
    """The whole point of hiding rather than filtering server-side: picking a
    term is instant and works offline."""
    body = many_terms.get("/dashboard").get_data(as_text=True)
    for code in ("BIOL1103", "SYSC2004", "MATH1104", "PHYS1004"):
        assert code in body


def test_an_all_terms_option_is_offered(many_terms):
    body = many_terms.get("/dashboard").get_data(as_text=True)
    assert 'value="__all__"' in body


def test_courses_with_no_term_are_their_own_group(client):
    """A task-derived card carries no term. It still has to be reachable rather
    than silently dropped by the picker."""
    signup(client, email="noterm@carleton.ca")
    add_courses("noterm@carleton.ca", [
        ("BIOL1103", "Biology", current_term()),
        ("XTRA1000", "Something imported without a term", ""),
    ])
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'data-term="Other courses"' in body
    assert '<option value="Other courses"' in body
