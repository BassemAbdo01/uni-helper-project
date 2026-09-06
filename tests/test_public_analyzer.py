"""The public outline reader — the one endpoint a stranger can spend money on.

It exists because nobody creates an account to find out whether an app reads
their outline correctly. They will paste one in, though, and that is the whole
funnel.

Which makes the cap the important part of this file. A per-IP limit does not
bound the bill: a thousand people asking once each is a thousand Gemini calls.
The daily ceiling is in the database so a worker restart cannot hand out a
fresh allowance, and it is tested from both sides.
"""

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUTLINE = """Evaluation
The lecture component accounts for 60% of your final grade, the laboratory 40%.
In-class worksheets (best 5 of 8) 10
Unit tests (3 x 10) 30
Final exam 20
In-lab assignments (4 x 6) 24
Tutorial assignments (4 x 1) 4
Final lab exam 12
To pass the course a student must receive a combined passing grade and must also
receive a passing grade in the lecture portion of the course.
"""

ANALYSIS = {
    "components": [
        {"name": "In-class worksheets", "weight": 10, "notes": "Best 5 of 8"},
        {"name": "Unit tests", "weight": 30, "notes": "3 x 10%"},
        {"name": "Final exam", "weight": 20, "notes": ""},
        {"name": "In-lab assignments", "weight": 24, "notes": ""},
        {"name": "Tutorial assignments", "weight": 4, "notes": ""},
        {"name": "Final lab exam", "weight": 12, "notes": ""},
    ],
    "pass_mark": 50,
    "rules": ["Must pass the lecture portion as well as the combined mark."],
    "grading_options": [], "ambiguities": [], "confidence": "high",
}


@pytest.fixture()
def stubbed(client, monkeypatch):
    """A working analyzer without spending quota."""
    import app as nexora
    monkeypatch.setattr(nexora, "_gemini_json", lambda *a, **k: dict(ANALYSIS))
    with nexora.app.app_context():
        nexora.PublicUsage.query.delete()
        nexora.db.session.commit()
    return client


def analyze(client, text=OUTLINE):
    return client.post("/api/public/analyze-outline", json={"text": text})


# ------------------------------------------------------------- no account

def test_the_page_needs_no_account(client):
    res = client.get("/what-do-i-need")
    assert res.status_code == 200
    assert "What do I" in res.get_data(as_text=True)


def test_the_endpoint_needs_no_account(stubbed):
    assert analyze(stubbed).status_code == 200


def test_it_returns_the_grading_scheme(stubbed):
    body = analyze(stubbed).get_json()
    assert body["ok"] is True
    assert len(body["components"]) == 6
    assert body["total_weight"] == 100
    names = [c["name"] for c in body["components"]]
    assert "Unit tests" in names
    assert any("lecture portion" in r for r in body["rules"])


def test_it_says_it_might_be_wrong(stubbed):
    """The whole product rests on people trusting the grading engine. Claiming
    certainty about an AI reading of a PDF would be the fastest way to lose
    that."""
    body = analyze(stubbed).get_json()
    assert "not guaranteed" in body["notice"]


def test_nothing_is_stored_for_an_anonymous_visitor(stubbed):
    import app as nexora
    analyze(stubbed)
    with nexora.app.app_context():
        assert nexora.CourseOutlineProfile.query.count() == 0
        assert nexora.CourseContent.query.count() == 0
        assert nexora.GradeEntry.query.count() == 0


def test_a_page_of_prose_with_no_scheme_is_refused_clearly(stubbed, monkeypatch):
    import app as nexora
    monkeypatch.setattr(nexora, "_gemini_json",
                        lambda *a, **k: {"components": [], "rules": []})
    res = analyze(stubbed, "This course explores the history of the novel." * 6)
    assert res.status_code == 422
    assert "percentages" in res.get_json()["error"]


@pytest.mark.parametrize("text", ["", "   ", "Midterm"])
def test_too_little_text_is_refused_before_a_call_is_made(client, text, monkeypatch):
    import app as nexora

    def explode(*a, **k):
        raise AssertionError("spent a Gemini call on nothing")
    monkeypatch.setattr(nexora, "_gemini_json", explode)
    res = client.post("/api/public/analyze-outline", json={"text": text})
    assert res.status_code == 400


# ------------------------------------------------------------- the budget

def usage_today():
    import app as nexora
    with nexora.app.app_context():
        row = nexora.PublicUsage.query.filter_by(day=date.today().isoformat()).first()
        return row.count if row else 0


def test_a_successful_analysis_spends_one_unit(stubbed):
    assert usage_today() == 0
    analyze(stubbed)
    assert usage_today() == 1
    analyze(stubbed)
    assert usage_today() == 2


def test_a_failed_analysis_costs_nothing(stubbed, monkeypatch):
    """If Gemini did not answer, the visitor should not have been charged for
    it -- and neither should tomorrow's budget."""
    import app as nexora
    monkeypatch.setattr(nexora, "_gemini_json",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503")))
    assert analyze(stubbed).status_code == 503
    assert usage_today() == 0


def test_the_daily_ceiling_stops_it(stubbed):
    import app as nexora
    with nexora.app.app_context():
        nexora.db.session.add(nexora.PublicUsage(
            day=date.today().isoformat(), count=nexora.PUBLIC_ANALYSES_PER_DAY))
        nexora.db.session.commit()
    res = analyze(stubbed)
    assert res.status_code == 429
    body = res.get_json()
    assert body["capped"] is True
    assert "limit" in body["error"]


def test_the_ceiling_costs_no_gemini_call(stubbed, monkeypatch):
    import app as nexora
    with nexora.app.app_context():
        nexora.db.session.add(nexora.PublicUsage(
            day=date.today().isoformat(), count=nexora.PUBLIC_ANALYSES_PER_DAY))
        nexora.db.session.commit()

    def explode(*a, **k):
        raise AssertionError("called Gemini after the cap")
    monkeypatch.setattr(nexora, "_gemini_json", explode)
    assert analyze(stubbed).status_code == 429


def test_the_ceiling_survives_a_restart(stubbed):
    """In-memory counting would give every worker its own allowance, and a
    redeploy a fresh one."""
    import app as nexora
    analyze(stubbed)
    with nexora.app.app_context():
        row = nexora.PublicUsage.query.filter_by(day=date.today().isoformat()).first()
        assert row is not None and row.count == 1


def test_yesterdays_usage_does_not_count_against_today(stubbed):
    import app as nexora
    with nexora.app.app_context():
        nexora.db.session.add(nexora.PublicUsage(
            day=(date.today() - timedelta(days=1)).isoformat(),
            count=nexora.PUBLIC_ANALYSES_PER_DAY * 5))
        nexora.db.session.commit()
    assert analyze(stubbed).status_code == 200


def test_the_remaining_budget_is_reported(stubbed):
    import app as nexora
    body = analyze(stubbed).get_json()
    assert body["budget_left"] == nexora.PUBLIC_ANALYSES_PER_DAY - 1


# --------------------------------------------------------------- the funnel

def test_the_result_offers_an_account_rather_than_a_dead_end(client):
    body = client.get("/what-do-i-need").get_data(as_text=True)
    assert "/signup" in body
    assert "not stored unless" in body


def test_the_landing_page_links_to_it(client):
    assert "/what-do-i-need" in client.get("/").get_data(as_text=True)


def test_the_cap_message_can_be_made_quiet(stubbed, monkeypatch):
    """A rate limit dressed as a sales pitch reads as manufactured scarcity.
    One env var turns the signup mention off."""
    import app as nexora
    monkeypatch.setattr(nexora, "PUBLIC_CAP_MENTIONS_SIGNUP", False)
    with nexora.app.app_context():
        nexora.db.session.add(nexora.PublicUsage(
            day=date.today().isoformat(), count=nexora.PUBLIC_ANALYSES_PER_DAY))
        nexora.db.session.commit()
    body = analyze(stubbed).get_json()
    assert "account" not in body["error"]
    assert "tomorrow" in body["error"]
