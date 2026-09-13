"""The extension has to sync into the account that is signed in.

It kept the key from the one time the connect page paired it, so a student who
switched accounts on the site kept pushing courses into the previous one. On a
shared computer that is one student's Brightspace landing in another student's
Nexora. Every signed-in page now offers its own key to the extension, which
ignores it when nothing changed and resets its per-account memory when it did.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"


def test_a_signed_in_page_offers_its_key_to_the_extension(client):
    client.post("/signup", data={"name": "A", "email": "acc-a@carleton.ca", "password": PASSWORD})
    body = client.get("/dashboard").get_data(as_text=True)
    assert "type: 'PAIR'" in body
    assert "/api/sync/extension-key" in body


def test_an_anonymous_page_offers_nothing(client):
    body = client.get("/login").get_data(as_text=True)
    assert "type: 'PAIR'" not in body


def test_the_key_offered_is_the_signed_in_accounts_own(client):
    """Two accounts, one browser: the key must follow the session."""
    client.post("/signup", data={"name": "A", "email": "acc-a@carleton.ca", "password": PASSWORD})
    key_a = client.get("/api/sync/extension-key").get_json()["token"]
    client.get("/logout")
    client.post("/signup", data={"name": "B", "email": "acc-b@carleton.ca", "password": PASSWORD})
    key_b = client.get("/api/sync/extension-key").get_json()["token"]
    assert key_a and key_b and key_a != key_b
    client.get("/logout")
    client.post("/login", data={"email": "acc-a@carleton.ca", "password": PASSWORD})
    assert client.get("/api/sync/extension-key").get_json()["token"] == key_a


def test_reading_the_key_does_not_rotate_it(client):
    """The page reads it on every load; a GET that minted a new key would
    unpair the extension every time the student opened a page."""
    client.post("/signup", data={"name": "A", "email": "acc-a@carleton.ca", "password": PASSWORD})
    first = client.get("/api/sync/extension-key").get_json()["token"]
    second = client.get("/api/sync/extension-key").get_json()["token"]
    assert first == second


def test_the_worker_resets_its_memory_when_the_account_changes():
    """Pinned as text because there is no node here; the browser check pages
    cover the runtime. The point is that a switch clears what the popup would
    otherwise say about the previous account."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "extension", "background.js"),
               encoding="utf-8").read()
    handler = src[src.index("NEXORA_PAIR"):]
    handler = handler[:handler.index("NEXORA_CANVAS_SESSION_SYNC")]
    assert "unchanged: true" in handler
    assert "'detectedCourses'" in handler and "'lastFullSync'" in handler
    assert "contentSummary: null" in handler
    assert "remove(['lastSync'])" in handler
