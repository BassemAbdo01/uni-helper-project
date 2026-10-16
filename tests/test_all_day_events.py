"""An all-day event is a DAY. It must never be converted into a zone.

Reported from the dashboard: a career fair on **October 7** showed as
**October 6, at 20:00**. Both halves are the same bug. The feed stated
`DTSTART;VALUE=DATE:{_stamp('2026-10-07')}` -- a calendar day, no clock time at all -- and the
`ics` library hands that back as midnight UTC. Midnight UTC on the 7th is 20:00
Eastern on the 6th, so converting it produced a day that was wrong AND a start
time that was never in the feed.

`_ics_floating_uids` already said a VALUE=DATE "is a calendar day already and
needs no shifting", but excluding it from `floating` routed it to the branch that
shifts. The rule was written down and not implemented.

This is the same class as the original day-late bug, in the opposite direction:
confidently wrong, on the screen a student checks to decide what to do today.
"""
import io
from datetime import date, timedelta

import app as nexora


# Anchored to the day the suite RUNS. Written against a fixed 7 October 2026,
# this file passed until the 16th and then failed for a reason with nothing to
# do with all-day events: the importer and the calendar both stop carrying work
# that is far enough past. The third file in a week to go off this way, after
# test_timezone_repair and test_deadline_links.
#
# What these tests pin is that `VALUE=DATE` is NEVER shifted while a real
# timestamp IS -- a relationship between two events in one feed. Which week
# they fall in was never part of it, and hard-coding it made them expire.
_ANCHOR = date(2026, 10, 7)
_SHIFT = (date.today() - _ANCHOR).days


def _d(iso):
    """A fixture date, kept the same distance from today as when it was written."""
    return (date.fromisoformat(iso) + timedelta(days=_SHIFT)).isoformat()


def _stamp(iso):
    return _d(iso).replace("-", "")


ALL_DAY_FEED = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Nexora//Test//EN
BEGIN:VEVENT
UID:fair-1
DTSTAMP:20260901T000000Z
DTSTART;VALUE=DATE:{_stamp('2026-10-07')}
DTEND;VALUE=DATE:{_stamp('2026-10-08')}
SUMMARY:Fall Career Fair 2026 10:00 am- 3:00 pm
END:VEVENT
BEGIN:VEVENT
UID:timed-1
DTSTAMP:20260901T000000Z
DTSTART:{_stamp('2026-10-08')}T020000Z
SUMMARY:Lab 4 - SYSC 2004
END:VEVENT
END:VCALENDAR"""


def _import(client, feed=ALL_DAY_FEED):
    return client.post("/import", data={
        "ics_file": (io.BytesIO(feed.encode("utf-8")), "feed.ics")},
        content_type="multipart/form-data", follow_redirects=True)


def _rows(client):
    with nexora.app.app_context():
        return {t.title: (t.due_raw, t.due_time or "")
                for t in nexora.Task.query.filter_by(source="ics").all()}


def _signed_in(client, zone="America/Toronto"):
    from uuid import uuid4
    client.post("/signup", data={"name": "S", "email": f"ad{uuid4().hex[:6]}@carleton.ca",
                                 "password": "nexorademo1"})
    assert client.post("/api/timezone", json={"timezone": zone}).status_code == 200


def _eastern(client):
    _signed_in(client)


# --------------------------------------------------------------------------
# the detector, on its own
# --------------------------------------------------------------------------

def test_a_value_date_start_is_recognised_as_all_day():
    assert nexora._ics_all_day_uids(ALL_DAY_FEED) == {"fair-1"}


def test_a_timestamped_event_is_not_all_day():
    assert "timed-1" not in nexora._ics_all_day_uids(ALL_DAY_FEED)


def test_a_feed_with_no_all_day_events_yields_nothing():
    assert nexora._ics_all_day_uids("BEGIN:VCALENDAR\nEND:VCALENDAR") == set()


def test_it_survives_a_lowercase_property_name():
    feed = ALL_DAY_FEED.replace("DTSTART;VALUE=DATE:", "dtstart;value=date:")
    assert "fair-1" in nexora._ics_all_day_uids(feed)


# --------------------------------------------------------------------------
# the bug as reported
# --------------------------------------------------------------------------

def test_an_all_day_event_keeps_the_day_the_feed_wrote(client):
    """October 7 is October 7, in Toronto as in UTC."""
    _eastern(client)
    _import(client)
    rows = _rows(client)
    fair = next(v for k, v in rows.items() if "Career Fair" in k)
    assert fair[0] == _d("2026-10-07"), "the day the feed stated, unshifted"


def test_an_all_day_event_is_given_no_start_time(client):
    """The feed stated a day and no clock. "20:00" came from converting
    midnight, and reads as a real start time."""
    _eastern(client)
    _import(client)
    rows = _rows(client)
    fair = next(v for k, v in rows.items() if "Career Fair" in k)
    assert fair[1] == "", "an all-day event has no time"


def test_a_real_timestamp_is_still_converted_to_the_students_day(client):
    """The fix must not undo the original one: a 10pm Eastern deadline is
    02:00Z the next morning and still belongs to the evening before."""
    _eastern(client)
    _import(client)
    rows = _rows(client)
    lab = next(v for k, v in rows.items() if "Lab 4" in k)
    assert lab[0] == _d("2026-10-07"), "02:00Z on the 8th is the 7th in Toronto"
    assert lab[1] == "22:00"


def test_the_same_feed_in_utc_is_unchanged_for_the_all_day_event(client):
    """A bare date has no offset to apply, so the zone cannot move it."""
    _signed_in(client, "UTC")
    _import(client)
    rows = _rows(client)
    fair = next(v for k, v in rows.items() if "Career Fair" in k)
    assert fair == (_d("2026-10-07"), "")


def test_a_far_western_zone_still_cannot_move_an_all_day_event(client):
    """Pacific/Niue is UTC-11; if the day were being converted this would land
    on the 6th."""
    _signed_in(client, "Pacific/Niue")
    _import(client)
    rows = _rows(client)
    fair = next(v for k, v in rows.items() if "Career Fair" in k)
    assert fair[0] == _d("2026-10-07")
