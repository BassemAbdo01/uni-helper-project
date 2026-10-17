"""The dashboard never waits for the university to answer.

Reported as "when i click open dashboard it takes SO LONG to load and open",
and intermittent, which is the shape of the bug: `home()` called
`auto_sync_if_needed()` before rendering, so once every `AUTO_SYNC_MINUTES`
the page fetched the .ics from Carleton, parsed it and imported every event
before sending a single byte.

Measured on this machine against the live feed:

    dashboard, no sync due        0.39s / 0.48s / 0.40s
    the sync it ran inline       12.13s / 11.61s

So 29 minutes of loads were instant and one was twelve seconds -- which from
the outside is a page that is sometimes broken. The page is painted first now
and asks for the sync afterwards.

What these tests pin is the SPLIT, not a duration. A timing assertion here
would measure this machine's network on the day it ran.
"""
import time
from datetime import datetime, timedelta
from uuid import uuid4

import app as nexora

FEED = "https://brightspace.example.ca/d2l/le/calendar/feed/x.ics"
# PRODID is not decoration: the ics library refuses a VCALENDAR without
# one, so a feed missing it parses as a FAILURE rather than as an empty
# calendar -- which is how the first draft of this file read a working
# sync as a sync that had never run.
ICS = ("BEGIN:VCALENDAR\nVERSION:2.0\n"
       "PRODID:-//Nexora//test//EN\nEND:VCALENDAR")


def _with_feed(client, last_attempt=None):
    email = f"dw{uuid4().hex[:7]}@carleton.ca"
    client.post("/signup", data={"name": "D", "email": email, "password": "nexorademo1"})
    with nexora.app.app_context():
        user = nexora.User.query.filter_by(email=email).first()
        user.feed_url = FEED
        user.last_sync = None
        user.last_sync_attempt = last_attempt
        nexora.db.session.commit()
        return user.id


# --------------------------------------------------------------------------
# The render does not touch the network
# --------------------------------------------------------------------------

def test_rendering_the_dashboard_never_fetches_the_feed(client, monkeypatch):
    """The whole point. A sync is overdue, and the page still does not wait."""
    _with_feed(client)
    fetches = []
    monkeypatch.setattr(nexora, "fetch_feed_ics",
                        lambda url, *a, **kw: fetches.append(url) or ICS)
    assert client.get("/dashboard").status_code == 200
    assert fetches == [], "the dashboard waited on the university before rendering"


def test_a_slow_feed_slows_the_request_that_asks_for_it_and_not_the_page(
        client, monkeypatch):
    """The reported failure was not slowness in Nexora, it was Nexora waiting.

    This is the one test here that has to involve a clock, so it pins a
    RELATIONSHIP rather than a duration -- the same shape as the two-timezone
    countdown test. A feed that takes two seconds must make the sync request
    slow and leave the dashboard alone; if the render still waited, both would
    be slow together and no threshold on this machine would mean anything.

    A first version raised AssertionError from inside the fetch instead.
    `auto_sync_if_needed` catches `Exception`, so it was swallowed and logged,
    and the test passed against the unfixed code -- the one thing a test must
    never do.
    """
    _with_feed(client)
    monkeypatch.setattr(nexora, "fetch_feed_ics",
                        lambda *a, **kw: (time.sleep(2), ICS)[1])

    t0 = time.perf_counter()
    assert client.get("/dashboard").status_code == 200
    page = time.perf_counter() - t0

    t0 = time.perf_counter()
    assert client.post("/api/sync/auto").get_json()["ran"] is True
    sync = time.perf_counter() - t0

    assert sync > 1.5, f"the fetch was not actually slow ({sync:.2f}s)"
    assert page < sync / 2, (
        f"the dashboard waited on the feed: page {page:.2f}s, sync {sync:.2f}s")


# --------------------------------------------------------------------------
# ...but the page still asks, and the sync still happens
# --------------------------------------------------------------------------

def test_the_page_asks_for_the_sync_when_one_is_due(client):
    _with_feed(client)
    body = client.get("/dashboard").get_data(as_text=True)
    assert "/api/sync/auto" in body, "nothing would ever trigger the sync"


def test_the_page_does_not_ask_when_nothing_is_due(client):
    """A student who synced five minutes ago must not re-fetch on every load --
    that was the original bug, and moving it to the browser would hide it
    rather than fix it."""
    _with_feed(client, last_attempt=datetime.utcnow())
    body = client.get("/dashboard").get_data(as_text=True)
    assert "/api/sync/auto" not in body


def test_an_account_with_no_feed_is_never_asked(client):
    client.post("/signup", data={"name": "N", "email": f"nf{uuid4().hex[:7]}@carleton.ca",
                                 "password": "nexorademo1"})
    assert "/api/sync/auto" not in client.get("/dashboard").get_data(as_text=True)


def test_the_endpoint_runs_the_sync_and_reports_what_changed(client, monkeypatch):
    _with_feed(client)
    monkeypatch.setattr(nexora, "fetch_feed_ics", lambda *a, **kw: ICS)
    monkeypatch.setattr(nexora, "import_calendar_string", lambda *a, **kw: {
        "added": 3, "updated": 1, "removed": 0, "unchanged": 11})
    data = client.post("/api/sync/auto").get_json()
    assert data["ran"] is True
    assert data["changed"] == 4, "added + updated + removed"
    assert data["counts"]["unchanged"] == 11


def test_a_sync_that_changed_nothing_says_so(client, monkeypatch):
    """`changed: 0` is what stops the page offering a reload for nothing."""
    _with_feed(client)
    monkeypatch.setattr(nexora, "fetch_feed_ics", lambda *a, **kw: ICS)
    monkeypatch.setattr(nexora, "import_calendar_string", lambda *a, **kw: {
        "added": 0, "updated": 0, "removed": 0, "unchanged": 14})
    assert client.post("/api/sync/auto").get_json()["changed"] == 0


# --------------------------------------------------------------------------
# The server decides, not the page
# --------------------------------------------------------------------------

def test_three_open_tabs_run_one_sync(client, monkeypatch):
    """Every tab was told a sync was due when it rendered. The flag says
    whether to ASK; the endpoint says whether to DO -- `auto_sync_if_needed`
    stamps `last_sync_attempt` before the attempt, so the first request
    through claims the slot."""
    _with_feed(client)
    fetches = []
    monkeypatch.setattr(nexora, "fetch_feed_ics",
                        lambda url, *a, **kw: fetches.append(url) or ICS)
    results = [client.post("/api/sync/auto").get_json() for _ in range(3)]
    assert len(fetches) == 1, f"{len(fetches)} tabs reached the university"
    assert [r["ran"] for r in results] == [True, False, False]


def test_a_stale_page_cannot_force_a_sync(client, monkeypatch):
    """A dashboard left open overnight still carries the flag it rendered with.
    The request is re-checked, so it cannot spend a sync that is not due."""
    _with_feed(client, last_attempt=datetime.utcnow())
    fetches = []
    monkeypatch.setattr(nexora, "fetch_feed_ics",
                        lambda url, *a, **kw: fetches.append(url) or ICS)
    assert client.post("/api/sync/auto").get_json()["ran"] is False
    assert fetches == []


def test_the_backoff_still_expires(client, monkeypatch):
    """Backing off is not giving up: once the window passes it tries again."""
    _with_feed(client, last_attempt=datetime.utcnow()
               - timedelta(minutes=nexora.AUTO_SYNC_MINUTES + 5))
    monkeypatch.setattr(nexora, "fetch_feed_ics", lambda *a, **kw: ICS)
    assert client.post("/api/sync/auto").get_json()["ran"] is True


# --------------------------------------------------------------------------
# The endpoint itself
# --------------------------------------------------------------------------

def test_it_is_post_only(client):
    """GET is refused for the reason `/api/tour` refuses it: a prefetch or a
    crawler must not be able to spend a sync."""
    _with_feed(client)
    assert client.get("/api/sync/auto").status_code == 405


def test_it_needs_an_account(client):
    assert client.post("/api/sync/auto").status_code in (302, 401)


def test_a_failing_feed_is_told_apart_from_one_that_was_not_due(client, monkeypatch):
    """`ran` and `ok` are separate, because "nothing was due" and "it tried and
    failed" are different facts and were briefly the same `None` -- the same
    mistake `_evaluate_pass_rules` exists to avoid. A student whose Brightspace
    token has expired has to be distinguishable from one who synced ten minutes
    ago, and this is the only place that knows.

    Found while measuring in the browser: a feed pointed at an unreachable
    address answered `ran: false` in 0.32 seconds, which read as "no sync was
    due" and was really the connection failing."""
    _with_feed(client)
    monkeypatch.setattr(nexora, "fetch_feed_ics",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("403")))
    res = client.post("/api/sync/auto")
    assert res.status_code == 200, "a broken feed is not a broken request"
    body = res.get_json()
    assert body["ran"] is True, "it did reach the university"
    assert body["ok"] is False, "and it did not work"
    assert body["changed"] == 0


def test_a_sync_that_was_not_due_is_not_reported_as_a_failure(client):
    _with_feed(client, last_attempt=datetime.utcnow())
    body = client.post("/api/sync/auto").get_json()
    assert body["ran"] is False and body["ok"] is True
