"""The configuration report.

Nexora degrades quietly on purpose -- no Gemini key means a deterministic
fallback, no SMTP means a polite message on the reset page. That is right for
the student in front of it and wrong for whoever runs the service, who
otherwise has no way to learn that everyone has been getting the worse product
for a week. These tests hold the report to saying so.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def signup(client, email="health@carleton.ca", password="correct-horse-battery"):
    return client.post("/signup", data={"name": "Health", "email": email,
                                        "password": password}, follow_redirects=False)


def find(findings, title_fragment):
    for f in findings:
        if title_fragment.lower() in f["title"].lower():
            return f
    raise AssertionError(f"no finding mentioning {title_fragment!r} in "
                         f"{[f['title'] for f in findings]}")


# --------------------------------------------------------------------------
# What it notices
# --------------------------------------------------------------------------

def test_a_missing_gemini_key_is_critical_not_a_footnote(client, monkeypatch):
    import app as nexora
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    finding = find(nexora._config_report(), "Gemini")
    assert finding["level"] == "critical"
    # the point is that it explains the consequence, not just the absence
    assert "fall back" in finding["effect"].lower()


def test_a_present_gemini_key_reads_as_ok_and_is_not_printed(client, monkeypatch):
    import app as nexora
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyEXAMPLEKEYVALUE1234")

    finding = find(nexora._config_report(), "Gemini")
    assert finding["level"] == "ok"
    assert "AIzaSyEXAMPLEKEY" not in finding["detail"], "never print the key back"
    assert finding["detail"].endswith("1234.")


def test_missing_email_is_critical_because_lockout_is_permanent(client, monkeypatch):
    import app as nexora
    monkeypatch.setattr(nexora, "SMTP_HOST", "")
    monkeypatch.setattr(nexora, "SMTP_FROM", "")

    finding = find(nexora._config_report(), "Email")
    assert finding["level"] == "critical"
    assert "locked out" in finding["effect"].lower()


def test_findings_are_ordered_worst_first(client, monkeypatch):
    import app as nexora
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    levels = [f["level"] for f in nexora._config_report()]
    rank = {"critical": 0, "warning": 1, "ok": 2}
    assert levels == sorted(levels, key=lambda l: rank[l])


def test_canvas_encryption_is_checked(client):
    """If the key cannot be derived, connecting Canvas fails; say so up front."""
    import app as nexora
    finding = find(nexora._config_report(), "Canvas")
    assert finding["level"] == "ok"


# --------------------------------------------------------------------------
# Who can see it
# --------------------------------------------------------------------------

def test_the_health_page_is_not_for_students(client):
    """It names which secrets are missing, so it stays behind the admin check."""
    signup(client, email="ordinary.student@carleton.ca")
    res = client.get("/admin/health")
    assert res.status_code == 302
    assert "/admin/health" not in res.headers.get("Location", "")


def test_the_health_page_needs_a_login_at_all(client):
    res = client.get("/admin/health")
    assert res.status_code == 302
    assert "/login" in res.headers.get("Location", "")


def test_the_admin_sees_the_report(client, monkeypatch):
    import app as nexora
    monkeypatch.setattr(nexora, "ADMIN_EMAIL", "boss@carleton.ca")
    signup(client, email="boss@carleton.ca")

    body = client.get("/admin/health").get_data(as_text=True)
    assert "Configuration" in body
    assert "Findings" in body


# --------------------------------------------------------------------------
# The address a locked-out student is given
# --------------------------------------------------------------------------

def test_there_is_always_an_address_for_someone_locked_out(client):
    """SUPPORT_EMAIL used to default to empty, which left the one page written
    for a student who cannot sign in offering them no way to reach anyone."""
    import app as nexora
    assert nexora.SUPPORT_EMAIL, "a locked-out student needs somewhere to write"


def test_the_public_pages_show_that_address(client):
    import app as nexora
    for path in ("/privacy", "/support"):
        body = client.get(path).get_data(as_text=True)
        assert nexora.SUPPORT_EMAIL in body, f"{path} should offer a contact address"
