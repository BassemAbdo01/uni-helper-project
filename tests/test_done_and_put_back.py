"""Ticking a task off must not shrink the list, and must be reversible.

Reported: "What to do first" showed five things; tick two and it showed three,
nothing moving up. The server sent six and the page showed five. And a wrong
tick was recoverable for nine seconds -- then the task was dropped before any
view was built, a sync never un-completes one, and it was gone from Nexora.
"""

import os
import sys
from datetime import date, timedelta
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "correct-horse-battery"


def seed(client, n=12):
    import app as nexora
    client.post("/signup", data={"name": "B", "email": f"q{uuid4().hex[:6]}@carleton.ca",
                                 "password": PASSWORD})
    with nexora.app.app_context():
        user = nexora.User.query.order_by(nexora.User.id.desc()).first()
        for i in range(n):
            title = f"Task {i + 1:02d}"
            nexora.db.session.add(nexora.Task(
                id=uuid4().hex, user_id=user.id, group_id=nexora.make_group_id("SYSC2004", title),
                title=title, base_title=title, course_full="SYSC2004", course_prefix="SYSC",
                due_raw=(date.today() + timedelta(days=i + 1)).isoformat(), kind="due",
                is_deadline=True, source="ics", sync_state="active", created_at=""))
        nexora.db.session.commit()


def test_the_queue_is_rendered_deeper_than_it_is_shown(client):
    seed(client, 12)
    body = client.get("/dashboard").get_data(as_text=True)
    assert body.count('class="task"') == 12
    # five visible, the rest waiting behind them
    assert body.count('aria-pressed="false" hidden') == 7


def test_a_completed_task_appears_in_the_done_list_with_a_way_back(client):
    import app as nexora
    seed(client, 6)
    with nexora.app.app_context():
        gid = nexora.Task.query.filter_by(title="Task 03").first().group_id
    assert client.post(f"/api/tasks/{gid}/complete", json={"completed": True}).status_code == 200
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'id="doneList"' in body and " hidden" not in body.split('id="doneList"')[1].split(">")[0]
    assert "Task 03" in body.split('id="doneItems"')[1]
    assert "Put back" in body
    # and it is no longer in the queue
    assert "Task 03" not in body.split('id="doneItems"')[0].split('id="tasks"')[1]


def test_put_back_restores_it_to_the_queue(client):
    import app as nexora
    seed(client, 6)
    with nexora.app.app_context():
        gid = nexora.Task.query.filter_by(title="Task 03").first().group_id
    client.post(f"/api/tasks/{gid}/complete", json={"completed": True})
    assert client.post(f"/api/tasks/{gid}/complete", json={"completed": False}).status_code == 200
    body = client.get("/dashboard").get_data(as_text=True)
    queue = body.split('id="tasks"')[1].split('id="doneList"')[0]
    assert "Task 03" in queue
    assert 'id="doneList" hidden' in body


def test_the_done_list_is_hidden_when_nothing_is_done(client):
    seed(client, 3)
    body = client.get("/dashboard").get_data(as_text=True)
    assert 'id="doneList" hidden' in body


def test_build_grouped_cards_default_is_unchanged():
    """Every other caller still gets only the live tasks."""
    import app as nexora
    rows = [{"title": "A", "course_full": "X", "due_raw": "2026-10-01", "kind": "due", "completed": False},
            {"title": "B", "course_full": "X", "due_raw": "2026-10-02", "kind": "due", "completed": True}]
    live = nexora.build_grouped_cards(rows)
    done = nexora.build_grouped_cards(rows, completed=True)
    assert [c["title"] for c in live] == ["A"]
    assert [c["title"] for c in done] == ["B"]


def test_the_hidden_queue_is_actually_hidden(client):
    """`.task{display:grid}` out-ranked the [hidden] attribute, so all twelve
    rendered and the page showed nine, not five. The same trap as
    #courseState: a class that sets display must have a [hidden] rule that
    beats it."""
    seed(client, 12)
    body = client.get("/dashboard").get_data(as_text=True)
    assert ".task[hidden]{display:none}" in body
