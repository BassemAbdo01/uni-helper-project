"""The tour a brand-new account gets on its first visit.

Two things decide whether this is a feature or an irritation: it must appear
exactly once, and dismissing it must count as a decision. Both live on the
account rather than in the browser, so signing in on a laptop after seeing it
on a phone does not start it again, and clearing site data does not either.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="new@carleton.ca"):
    return client.post("/signup", data={"name": "New Student", "email": email,
                                        "password": "correct-horse-battery"},
                       follow_redirects=False)


def tour_on(client):
    return 'id="tour"' in client.get("/dashboard").get_data(as_text=True)


def test_a_new_account_gets_the_tour(client):
    signup(client)
    assert tour_on(client)


def test_it_renders_on_an_account_with_no_data_at_all(client):
    """The commonest case by far: no courses, no tasks, nothing synced. Steps
    that point at an empty card are dropped client-side, but the tour itself
    still has to be there."""
    signup(client)
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'id="tourCard"' in body and 'id="tourNext"' in body
    # and the rail hooks the steps point at exist
    for hook in ("nav-brightspace", "nav-settings", "nav-grades",
                 "nav-calendar", "nav-plan", "nav-study"):
        assert f'data-tour="{hook}"' in body


def test_the_tour_covers_the_study_planner(client):
    """It did not. Nine steps walked a new student past connecting, the
    grading scale, tasks, courses, grades, the calendar and the study room --
    and never mentioned the one feature that turns all of that into a week."""
    signup(client)
    body = client.get("/dashboard").get_data(as_text=True)
    assert "'nav-plan'" in body
    assert "Your week, planned around you" in body


def test_finishing_it_puts_it_away(client):
    signup(client)
    assert client.post("/api/tour").status_code == 200
    assert not tour_on(client)


def test_it_stays_away_across_visits(client):
    signup(client)
    client.post("/api/tour")
    for _ in range(3):
        assert not tour_on(client)


def test_skipping_counts_as_a_decision(client):
    """Skip and Finish hit the same endpoint on purpose. Showing it again to
    someone who dismissed it is the app arguing with them."""
    signup(client)
    client.post("/api/tour")
    assert not tour_on(client)


def test_it_stays_away_for_a_second_browser(client):
    """The flag is on the account, so it is not per-device. A student who has
    seen it on their phone should not meet it again on a laptop."""
    import app as nexora
    signup(client)
    client.post("/api/tour")
    client.get("/logout")
    client.post("/login", data={"email": "new@carleton.ca",
                                "password": "correct-horse-battery"})
    assert not tour_on(client)
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email="new@carleton.ca").first()
        assert user.tour_done is True


def test_it_can_be_armed_again(client):
    signup(client)
    client.post("/api/tour")
    assert client.delete("/api/tour").status_code == 200
    assert tour_on(client)


def test_the_profile_page_offers_to_replay_it(client):
    signup(client)
    body = client.get("/profile").get_data(as_text=True)
    assert 'id="replayTour"' in body


def test_one_students_choice_does_not_affect_another(client):
    signup(client, email="first@carleton.ca")
    client.post("/api/tour")
    client.get("/logout")
    signup(client, email="second@carleton.ca")
    assert tour_on(client)


def test_the_endpoint_needs_a_login(client):
    assert client.post("/api/tour").status_code in (302, 401)
    assert client.delete("/api/tour").status_code in (302, 401)


def test_a_get_is_not_allowed_so_a_prefetch_cannot_dismiss_it(client):
    """Browsers and link scanners issue GETs. If a GET marked the tour seen, a
    prefetch would silently spend it."""
    signup(client)
    assert client.get("/api/tour").status_code == 405
    assert tour_on(client)
