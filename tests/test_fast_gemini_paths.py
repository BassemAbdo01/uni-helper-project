"""Speed on the two things a student sits and waits for.

Measured 2026-09-14 on a 1,500-word lecture, live key: the default model chain
took 46.8s to summarise it -- gemini-3.7-flash first in the chain and 503ing,
then gemini-3.6-flash spending 1,241 tokens thinking about a summary. Flash-lite
did the same summary, same eight concepts, in 2.5s; 3.6-flash with low thinking
in 5.0s. The tutor was worse: one hardcoded model, full thinking, and on a 429
it slept and retried the same model three times before failing.

After: summary 3.5s, flashcards 3.3s, a full lecture explanation 13s of which
all is output, a follow-up that uses the conversation 3.6s.
"""

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_fast_mode_starts_from_flash_lite():
    import app as nexora
    assert nexora._FAST_MODELS[0] == "gemini-3.5-flash-lite"


def test_quick_mode_starts_from_a_flash_model_with_low_thinking():
    import app as nexora
    assert nexora._QUICK_MODELS[0] == "gemini-3.6-flash"
    assert nexora._THINKING_LEVEL_MODELS.match("gemini-3.6-flash")
    assert nexora._THINKING_LEVEL_MODELS.match("gemini-3.7-flash")


@pytest.mark.parametrize("model", ["gemini-3.5-flash-lite", "gemini-flash-lite-latest",
                                   "gemini-pro-latest", "gemini-3.1-pro-preview", "gemini-flash-latest"])
def test_thinking_level_is_only_sent_to_models_that_accept_it(model):
    """thinkingBudget: 0 answered 400 on this family; a thinkingConfig sent to
    the wrong model reads as the model failing and burns a candidate."""
    import app as nexora
    assert not nexora._THINKING_LEVEL_MODELS.match(model)


def test_the_default_chain_is_untouched_for_the_outline_pipeline(monkeypatch):
    """Reasoning is the product there. mode=None must still build the chain
    it always did."""
    import app as nexora
    seen = {}

    def fake_candidates(vision=False, reasoning=False):
        seen["reasoning"] = reasoning
        return ["gemini-3.7-flash"]

    monkeypatch.setattr(nexora, "_gemini_model_candidates", fake_candidates)
    monkeypatch.setenv("GEMINI_API_KEY", "x")

    def boom(*a, **k):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(nexora.urllib.request, "urlopen", boom)
    with pytest.raises(Exception):
        nexora._gemini_json("s", "u", 100, 0.1, reasoning=True)
    assert seen == {"reasoning": True}


def test_fast_mode_never_consults_the_reasoning_chain(monkeypatch):
    import app as nexora
    called = []
    monkeypatch.setattr(nexora, "_gemini_model_candidates", lambda **k: called.append(k) or ["x"])
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(nexora.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    with pytest.raises(Exception):
        nexora._gemini_json("s", "u", 100, 0.1, mode="fast")
    assert called == []


def test_the_study_tool_prompt_is_a_brief_not_a_sentence():
    import app as nexora
    p = nexora._study_tool_prompt("summary", "{}", "SYSC2004", "Lecture 7")
    assert "SYSC2004" in p and "Lecture 7" in p
    assert "plain words" in p
    assert "never a fact that is not in it" in p
    for tool in ("flashcards", "quiz", "study_guide"):
        assert len(nexora._study_tool_prompt(tool, "{}", "X", "Y")) > 400


def test_the_copilot_moves_to_the_next_model_on_a_429_instead_of_sleeping(client, monkeypatch):
    """4.5s of sleeping and then an error, on a key where 3.6-flash 429s two
    calls in three. The next model answers in the time a sleep would cost."""
    import json as _json
    import io as _io
    import app as nexora
    client.post("/signup", data={"name": "B", "email": "cp@carleton.ca", "password": "correct-horse-battery"})
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    nexora._gemini_model_cooldowns.clear()
    hits = []

    class Resp:
        def __init__(self, payload): self._b = _json.dumps(payload).encode()
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        model = req.full_url.split("/models/")[1].split(":")[0]
        hits.append(model)
        if model == "gemini-3.6-flash":
            raise urllib.error.HTTPError(req.full_url, 429, "quota", {}, _io.BytesIO(b"{}"))
        return Resp({"candidates": [{"content": {"parts": [{"text": "Dynamic dispatch picks the method at run time (Slide 12)."}]}}]})

    monkeypatch.setattr(nexora.urllib.request, "urlopen", fake_urlopen)
    slept = []
    monkeypatch.setattr(nexora.time, "sleep", lambda s: slept.append(s))
    res = client.post("/api/copilot", json={"message": "explain dynamic dispatch", "history": []})
    assert res.status_code == 200, res.get_json()
    assert hits[0] == "gemini-3.6-flash" and hits[1] == "gemini-3.5-flash-lite"
    assert slept == [], "slept instead of moving on"
    assert res.get_json()["citations"] == [{"label": "Slide 12", "kind": "slide", "number": 12}]


def test_the_copilot_sends_low_thinking_to_a_flash_model(client, monkeypatch):
    import json as _json
    import app as nexora
    client.post("/signup", data={"name": "B", "email": "cp2@carleton.ca", "password": "correct-horse-battery"})
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    nexora._gemini_model_cooldowns.clear()
    bodies = []

    class Resp:
        def read(self): return _json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        bodies.append(_json.loads(req.data))
        return Resp()

    monkeypatch.setattr(nexora.urllib.request, "urlopen", fake_urlopen)
    client.post("/api/copilot", json={"message": "hi", "history": []})
    assert bodies[0]["generationConfig"].get("thinkingConfig") == {"thinkingLevel": "low"}
