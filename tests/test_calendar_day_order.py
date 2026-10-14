"""A day cell is read top to bottom, so it has to run earliest first.

Reported with both times on screen: a 19:00 "Biology Test 2" sitting above a
14:00 "Midterm exam" in the same cell. The month grid appended entries in
`sort_cards` order -- which is the right order for the task QUEUE, by urgency --
and nothing re-sorted them inside a day.
"""
from datetime import date
from uuid import uuid4

import app as nexora

PASSWORD = "nexorademo1"


def _user():
    with nexora.app.app_context():
        u = nexora.User(name="S", email=f"ord{uuid4().hex[:7]}@carleton.ca")
        u.password_hash = nexora.generate_password_hash(PASSWORD)
        nexora.db.session.add(u)
        nexora.db.session.commit()
        return u.id, u.email


def _task(user_id, title, when, due="2026-12-07", course="SYSC2310"):
    with nexora.app.app_context():
        nexora.db.session.add(nexora.Task(
            id=uuid4().hex, user_id=user_id, title=title, base_title=title,
            group_id=nexora.make_group_id(course, title), due_raw=due,
            due_time=when, course_full=course, course_prefix=course, kind="exam",
            is_deadline=True, source="manual", sync_state="active",
            created_at="2026-10-01 00:00:00"))
        nexora.db.session.commit()


# --------------------------------------------------------------------------
# The key
# --------------------------------------------------------------------------

def test_a_time_sorts_by_minutes_from_midnight():
    k = nexora._time_of_day_key
    assert k({"time": "08:30"}) == 8 * 60 + 30
    assert k({"time": "14:00"}) < k({"time": "19:00"})
    assert k({"time": "00:00"}) == 0
    assert k({"time": "23:59"}) == 23 * 60 + 59


def test_an_unknown_time_goes_last_rather_than_claiming_midnight():
    """An entry with no time is not an all-day event -- it is one whose time
    Nexora was never told. Treating it as 00:00 would put an assignment with no
    stated hour above a 09:00 exam, which is a claim nobody made."""
    k = nexora._time_of_day_key
    for blank in ({"time": ""}, {"time": "   "}, {}, {"time": None}):
        assert k(blank) > k({"time": "23:59"}), blank


def test_rubbish_in_the_field_does_not_crash_or_jump_the_queue():
    k = nexora._time_of_day_key
    for bad in ("nonsense", "25:00", "7pm", "9:5", "14-00"):
        assert k({"time": bad}) > k({"time": "23:59"}), bad


def test_equal_times_keep_the_order_they_arrived_in():
    """Python's sort is stable, so `sort_cards`' own ordering decides ties --
    the queue's urgency rule still applies where the clock cannot."""
    items = [{"time": "14:00", "t": "first"}, {"time": "14:00", "t": "second"},
             {"time": "14:00", "t": "third"}]
    items.sort(key=nexora._time_of_day_key)
    assert [i["t"] for i in items] == ["first", "second", "third"]


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def test_the_reported_pair_renders_the_right_way_round(client):
    import re
    uid, email = _user()
    # inserted LATEST first, so the wrong answer is also the lazy one
    _task(uid, "ZZ Biology Test 2", "19:00")
    _task(uid, "ZZ Midterm exam", "14:00")
    client.post("/login", data={"email": email, "password": PASSWORD})
    html = client.get("/calendar?month=2026-12").get_data(as_text=True)
    order, seen = re.findall(r"ZZ [^<\"]+", html), []
    for name in order:
        if name not in seen:
            seen.append(name)
    assert seen == ["ZZ Midterm exam", "ZZ Biology Test 2"]


def test_a_whole_day_runs_in_order_with_the_untimed_last(client):
    import re
    uid, email = _user()
    for title, when in (("ZZ evening", "19:00"), ("ZZ untimed", ""),
                        ("ZZ afternoon", "14:00"), ("ZZ morning", "08:30")):
        _task(uid, title, when)
    client.post("/login", data={"email": email, "password": PASSWORD})
    html = client.get("/calendar?month=2026-12").get_data(as_text=True)
    order, seen = re.findall(r"ZZ [a-z]+", html), []
    for name in order:
        if name not in seen:
            seen.append(name)
    assert seen == ["ZZ morning", "ZZ afternoon", "ZZ evening", "ZZ untimed"]


def test_days_are_sorted_independently(client):
    """Sorting the whole month by time would interleave days."""
    import re
    uid, email = _user()
    _task(uid, "ZZ day one late", "19:00", due="2026-12-07")
    _task(uid, "ZZ day two early", "08:00", due="2026-12-08")
    client.post("/login", data={"email": email, "password": PASSWORD})
    html = client.get("/calendar?month=2026-12").get_data(as_text=True)
    assert html.index("ZZ day one late") < html.index("ZZ day two early")
