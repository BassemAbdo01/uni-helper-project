"""Keeping the calendar in Google Calendar is one click on the calendar page.

The .ics feed existed, but the only way to it was a six-step copy-and-paste
on the Settings page, under a heading that said "Grading scale". Nobody
found it. The calendar page now carries a Sync button whose sheet offers
Google's own subscribe link, a webcal:// link for Apple and Outlook, and
the address itself.

Two rules worth pinning:

- Visiting the calendar never mints a token. The address is a credential,
  and creating one on every page view would hand every account a live feed
  it never asked for. It is created on the first press of Sync, through the
  same route Settings uses.
- Once one exists, the page ships it in so the sheet opens with the links
  ready instead of round-tripping for an address the server already knows.
"""
import os
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402


@pytest.fixture()
def signed_in(client):
    email = f"cal{uuid4().hex[:6]}@carleton.ca"
    client.post("/signup", data={"name": "S", "email": email, "password": "calendar-pass-1"})
    return email


def _user(email):
    with nexora.app.app_context():
        return nexora.User.query.filter_by(email=email).first()


def test_the_calendar_page_offers_the_sync_button(client, signed_in):
    html = client.get("/calendar").get_data(as_text=True)
    assert 'id="syncCal"' in html
    assert "Sync to Google Calendar" in html
    assert 'id="gcalLink"' in html
    assert 'id="webcalLink"' in html
    # honest about what "auto sync" means on Google's side
    assert "on its own schedule" in html


def test_visiting_the_calendar_does_not_mint_a_feed_token(client, signed_in):
    client.get("/calendar")
    assert not (_user(signed_in).calendar_token or "")


def test_pressing_sync_creates_the_feed_through_the_shared_route(client, signed_in):
    data = client.get("/api/calendar/feed").get_json()
    assert data["ok"] and "/calendar/feed/" in data["url"] and data["url"].endswith(".ics")
    token = _user(signed_in).calendar_token
    assert token and token in data["url"]
    # and the feed that address serves is a real calendar
    feed = client.get(f"/calendar/feed/{token}.ics")
    assert feed.status_code == 200
    assert feed.get_data(as_text=True).startswith("BEGIN:VCALENDAR")


def test_an_existing_address_is_shipped_with_the_page(client, signed_in):
    url = client.get("/api/calendar/feed").get_json()["url"]
    html = client.get("/calendar").get_data(as_text=True)
    assert "showSyncLinks(" in html and url in html


def test_a_fresh_account_page_carries_no_address(client, signed_in):
    html = client.get("/calendar").get_data(as_text=True)
    assert "showSyncLinks(\"" not in html and "/calendar/feed/" not in html.replace("/api/calendar/feed", "")
