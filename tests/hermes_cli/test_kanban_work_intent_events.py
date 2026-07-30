"""Regression coverage for removal of HEL-3110 lifecycle correlation."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


_TYPED_EVENT_TYPES = {
    "task_claimed",
    "worker_spawn_requested",
    "worker_spawned",
    "worker_started",
    "heartbeat",
    "worker_exited",
}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _profile: True)
    kb.init_db()
    return home


def test_fresh_schema_does_not_add_hel3110_event_columns(board):
    with kb.connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_events)")
        }

    assert "work_intent_id" not in columns
    assert "idempotency_key" not in columns


def test_dispatch_lifecycle_keeps_legacy_events_without_typed_envelopes(board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy lifecycle", assignee="worker")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: 4321,
            max_spawn=1,
        )
        claimed = kb.get_task(conn, task_id)
        assert result.spawned and claimed is not None
        assert kb.heartbeat_worker(
            conn, task_id, expected_run_id=claimed.current_run_id,
        )
        assert kb.complete_task(
            conn, task_id, summary="finished", expected_run_id=claimed.current_run_id,
        )
        events = kb.list_events(conn, task_id)
        final_task = kb.get_task(conn, task_id)

    kinds = [event.kind for event in events]
    assert kinds[0:2] == ["created", "claimed"]
    assert kinds[-3:] == ["spawned", "heartbeat", "completed"]
    assert all(
        not event.payload or event.payload.get("event_type") not in _TYPED_EVENT_TYPES
        for event in events
    )
    assert final_task is not None
    assert final_task.current_step_key is None
