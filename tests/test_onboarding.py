"""A new account has to be told what to do, by the app.

Everything a student needs was already written on the connect pages -- install
the extension, pin it, import, add the calendar feed -- and nothing ever sent
them there. A new account landed on an empty dashboard and had to work out on
its own that "Brightspace" in the sidebar was the thing it needed, which was
being covered by Bassem explaining it to people in person.
"""
from uuid import uuid4

import app as nexora


def _signup(client):
    email = f"new{uuid4().hex[:7]}@carleton.ca"
    return client.post("/signup", data={"name": "N", "email": email,
                                        "password": "nexorademo1"})


def test_a_new_account_is_sent_to_set_up_not_to_an_empty_dashboard(client):
    res = _signup(client)
    assert res.status_code in (301, 302)
    assert res.headers["Location"].endswith("/start")


def test_the_setup_page_offers_both_systems_and_a_way_to_tell_them_apart(client):
    _signup(client)
    body = client.get("/start").get_data(as_text=True)
    assert "Brightspace" in body and "Canvas" in body
    assert "/connect-brightspace" in body and "/connect-canvas" in body
    # a student who does not know which one they use cannot be left stuck
    assert "address bar" in body.lower()


def test_setup_stops_appearing_once_something_is_connected(client):
    """An onboarding screen that outlives onboarding reads as a broken app."""
    _signup(client)
    assert client.get("/start").status_code == 200
    with nexora.app.app_context():
        user = nexora.User.query.order_by(nexora.User.id.desc()).first()
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user.id, source_uid="u1", course_code="SYSC2004",
            course_name="Software", term="Fall 2026"))
        nexora.db.session.commit()
    res = client.get("/start")
    assert res.status_code in (301, 302)
    assert res.headers["Location"].rstrip("/").endswith(("/dashboard", ""))


def test_the_dashboard_nags_until_something_is_connected(client):
    _signup(client)
    body = client.get("/dashboard").get_data(as_text=True)
    assert "/start" in body
    assert "empty until you connect" in body


def test_the_nag_disappears_once_connected(client):
    _signup(client)
    with nexora.app.app_context():
        user = nexora.User.query.order_by(nexora.User.id.desc()).first()
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user.id, source_uid="u1", course_code="SYSC2004",
            course_name="Software", term="Fall 2026"))
        nexora.db.session.commit()
    body = client.get("/dashboard").get_data(as_text=True)
    assert "empty until you connect" not in body


def test_a_deadline_alone_counts_as_connected(client):
    """A student who added only the Brightspace calendar feed has deadlines and
    no course rows -- they are set up and must not be told otherwise."""
    _signup(client)
    with nexora.app.app_context():
        user = nexora.User.query.order_by(nexora.User.id.desc()).first()
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user.id, title="Lab 1", base_title="Lab 1",
            group_id="g1", due_raw="2026-11-02", course_full="SYSC2004",
            course_prefix="SYSC2004", kind="due", is_deadline=True, source="ics",
            sync_state="active", created_at="2026-10-01 00:00:00"))
        nexora.db.session.commit()
        assert nexora._setup_state(user.id)["connected"] is True


def test_the_extension_link_points_at_the_live_listing(client):
    """An unset env var meant every student was told to unzip a folder and load
    it unpacked -- six steps a first-time user does not finish."""
    assert "chromewebstore.google.com" in nexora.EXTENSION_URL
    _signup(client)
    body = client.get("/connect-brightspace").get_data(as_text=True)
    assert nexora.EXTENSION_URL in body
    assert "Pin it" in body


def test_anyone_can_find_the_extension_without_an_account(client):
    """It is published and free, so a visitor should be able to look at it
    before signing up -- which is the order most people want. The extension was
    named three times on the landing page and linked from nowhere."""
    body = client.get("/").get_data(as_text=True)
    assert nexora.EXTENSION_URL in body
    assert "Chrome Web Store" in body


def test_the_extension_link_is_in_the_public_footer_too(client):
    body = client.get("/").get_data(as_text=True)
    assert "Chrome extension" in body


def test_the_store_link_opens_safely(client):
    """target=_blank without rel=noopener hands the new tab a reference back."""
    body = client.get("/").get_data(as_text=True)
    i = body.index(nexora.EXTENSION_URL)
    around = body[max(0, i - 200):i + 200]
    assert 'rel="noopener"' in around


# --------------------------------------------------------------------------
# The workspace carried nine tabs. Overview was contents[:8] -- a digest of the
# other eight -- and Resources was a tab of its own for material a student looks
# for alongside the lectures. Removing a TAB must not remove the FILES.
# --------------------------------------------------------------------------

def _course_with(client, rows):
    _signup(client)
    with nexora.app.app_context():
        user = nexora.User.query.order_by(nexora.User.id.desc()).first()
        nexora.db.session.add(nexora.SyncedCourse(
            id=uuid4().hex, user_id=user.id, source_uid="u1", course_code="SYSC2004",
            course_name="Software", term="Fall 2026"))
        for title, ctype, module in rows:
            nexora.db.session.add(nexora.CourseContent(
                id=uuid4().hex, user_id=user.id, course_full="SYSC2004",
                content_type=ctype, title=title, content_text="",
                published_at="2026-10-01T00:00:00Z", source_uid=uuid4().hex,
                module_name=module))
        nexora.db.session.commit()
    return client.get("/course/SYSC2004").get_data(as_text=True)


def test_overview_and_resources_are_gone_from_the_tab_strip(client):
    body = _course_with(client, [])
    assert 'data-tab="lectures"' in body
    assert 'data-tab="overview"' not in body
    assert 'data-tab="resources"' not in body


def test_resource_items_are_still_reachable_under_lectures(client):
    """49 files on a real course. Deleting the tab must not delete the way to
    them."""
    body = _course_with(client, [
        ("Lab 3 Manual.pdf", "resource", "Lab Manuals"),
        ("Course policies", "page", "Resources"),
    ])
    assert "Other course material" in body
    assert "Lab 3 Manual.pdf" in body
    assert "Course policies" in body


def test_a_bookmarked_overview_tab_lands_somewhere_real(client):
    """Otherwise every panel is hidden and the page looks broken."""
    import re
    _course_with(client, [])
    body = client.get("/course/SYSC2004?tab=overview").get_data(as_text=True)

    # the lectures panel is the one showing...
    shown = re.search(r'data-panel="lectures"([^>]*)>', body)
    assert shown and "hidden" not in shown.group(1)
    # ...and every other panel is hidden, so the page is never blank
    for panel in ("assignments", "quizzes", "announcements", "outline", "grades", "study"):
        m = re.search(r'data-panel="%s"([^>]*)>' % panel, body)
        assert m and "hidden" in m.group(1), panel
    # ...and Lectures is the tab marked active
    assert re.search(r'class="tab active" data-tab="lectures"', body)
