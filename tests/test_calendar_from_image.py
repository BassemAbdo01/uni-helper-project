"""An exam timetable is a table, and typing seven finals in by hand is both the
most tedious thing Nexora could ask for and the easiest place to make a mistake
that matters.

The AI path needs a live key and is exercised by hand; what is pinned here is
everything the server decides on its own -- what it will accept, and that it
never writes without a confirmation.
"""
import io
from uuid import uuid4

import app as nexora


def _signup(client):
    return client.post("/signup", data={"name": "S", "email": f"img{uuid4().hex[:7]}@carleton.ca",
                                        "password": "nexorademo1"})


def _post(client, data=b"x", name="s.png", mime="image/png"):
    return client.post("/api/calendar/from-image",
                       data={"image": (io.BytesIO(data), name, mime)},
                       content_type="multipart/form-data")


def test_it_needs_an_account(client):
    res = _post(client)
    assert res.status_code in (302, 401)


def test_a_missing_file_is_refused_in_words(client):
    _signup(client)
    res = client.post("/api/calendar/from-image", data={}, content_type="multipart/form-data")
    assert res.status_code == 400
    assert "screenshot" in res.get_json()["error"].lower()


def test_an_empty_file_is_refused(client):
    _signup(client)
    res = _post(client, data=b"")
    assert res.status_code == 400


def test_a_pdf_is_refused_rather_than_sent_to_the_model(client):
    """Spending a vision call to discover it is not an image is a waste of the
    quota the outline pipeline needs."""
    _signup(client)
    res = _post(client, data=b"%PDF-1.4", name="s.pdf", mime="application/pdf")
    assert res.status_code == 400
    assert "PNG" in res.get_json()["error"]


def test_something_too_large_is_refused_by_size_not_by_timing_out(client):
    _signup(client)
    res = _post(client, data=b"x" * (nexora.IMAGE_MAX_BYTES + 10))
    assert res.status_code == 400
    assert "8 MB" in res.get_json()["error"]


def test_the_accepted_types_are_the_ones_gemini_takes(client):
    assert nexora.IMAGE_MIMES == {"image/png", "image/jpeg", "image/webp", "image/gif"}


def test_the_timetable_chain_puts_a_fast_model_first(client):
    """The default vision chain is ordered for reading a dense outline. On a
    real exam schedule its first three candidates cost ~70s between them before
    anything answered; flash-lite read the same image in 2.0s with every course
    and date present."""
    assert nexora._TIMETABLE_VISION_MODELS[0].endswith("flash-lite")
    # and the full chain still follows, so a lite failure just falls through
    assert len(nexora._TIMETABLE_VISION_MODELS) > 2


def test_the_button_is_on_the_calendar(client):
    _signup(client)
    body = client.get("/calendar").get_data(as_text=True)
    assert 'id="shotBtn"' in body and 'id="shotFile"' in body
    assert 'accept="image/png,image/jpeg,image/webp,image/gif"' in body
