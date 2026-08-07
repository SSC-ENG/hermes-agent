"""Focused regression tests for Kanban respawn guard behavior."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _pr_state_open(monkeypatch):
    """This file exercises the guard's comment-timestamp / requeue-bypass
    logic, not live PR-state resolution (that's covered separately in
    test_kanban_db.py's ``_resolve_pr_open_state`` tests). Force every
    cited PR to resolve as OPEN so these tests keep isolating the ordering
    logic instead of depending on the real (and mutable) state of the
    fixture PR URL on GitHub."""
    monkeypatch.setattr(kb, "_resolve_pr_open_state", lambda o, r, n: True)


def _make_code_task(conn, task_id: str) -> None:
    """Mark the task as a code/PR-producing task (branch_name set).

    ``check_respawn_guard`` deliberately scopes the ``active_pr`` guard to
    code tasks (worktree workspace or a branch name) so evidence/research
    tasks that merely cite PR URLs are not suppressed. These tests exercise
    the guard itself, so the fixture task must look like a code task.
    """
    conn.execute(
        "UPDATE tasks SET branch_name = 'fix/test-branch' WHERE id = ?",
        (task_id,),
    )


def _add_pr_comment(conn, task_id: str, created_at: int) -> None:
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, 'worker', ?, ?)",
        (
            task_id,
            "Opened https://github.com/totemx-AI/subsidysmart/pull/42",
            created_at,
        ),
    )


def test_respawn_guard_recent_pr_without_requeue_is_active(kanban_home):
    """Recent PR evidence with no later requeue keeps the active_pr guard."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="has-pr", assignee="alice")
        _make_code_task(conn, task_id)
        _add_pr_comment(conn, task_id, int(time.time()) - 10)

        reason = kb.check_respawn_guard(conn, task_id)

    assert reason == "active_pr"


@pytest.mark.parametrize("event_kind", ["promoted", "unblocked", "status", "reclaimed"])
def test_respawn_guard_active_pr_bypassed_by_later_requeue(
    kanban_home, event_kind
):
    """Every explicit requeue event after PR evidence permits more work."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"requeued-after-pr-{event_kind}",
            assignee="alice",
        )
        _make_code_task(conn, task_id)
        now = int(time.time())
        _add_pr_comment(conn, task_id, now - 20)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) VALUES (?, ?, ?)",
            (task_id, event_kind, now - 10),
        )

        reason = kb.check_respawn_guard(conn, task_id)

    assert reason is None


def test_respawn_guard_active_pr_not_bypassed_by_earlier_requeue(kanban_home):
    """A requeue before the PR comment does not supersede newer PR evidence."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="requeued-before-pr",
            assignee="alice",
        )
        _make_code_task(conn, task_id)
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'promoted', ?)",
            (task_id, now - 20),
        )
        _add_pr_comment(conn, task_id, now - 10)

        reason = kb.check_respawn_guard(conn, task_id)

    assert reason == "active_pr"


def test_respawn_guard_active_pr_not_bypassed_by_same_timestamp_requeue(
    kanban_home,
):
    """A requeue at the PR comment timestamp is not strictly later evidence."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="requeued-at-pr-timestamp",
            assignee="alice",
        )
        _make_code_task(conn, task_id)
        created_at = int(time.time()) - 10
        _add_pr_comment(conn, task_id, created_at)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'promoted', ?)",
            (task_id, created_at),
        )

        reason = kb.check_respawn_guard(conn, task_id)

    assert reason == "active_pr"


def test_respawn_guard_merged_pr_does_not_hold_even_without_requeue(
    kanban_home, monkeypatch,
):
    """State-awareness (t_edd7abd5): a merged/closed PR must not hold the
    guard even with zero requeue events — this is the exact production
    failure (PRs #888/#889/#890/#895/#898 already merged, cited in
    comments, held the card for the full window with no requeue in sight).
    The requeue-bypass tested above is a SEPARATE, ADDITIONAL escape hatch
    on top of state-awareness, not a substitute for it."""
    monkeypatch.setattr(kb, "_resolve_pr_open_state", lambda o, r, n: False)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="merged-pr-no-requeue", assignee="alice")
        _make_code_task(conn, task_id)
        _add_pr_comment(conn, task_id, int(time.time()) - 10)

        reason = kb.check_respawn_guard(conn, task_id)

    assert reason is None


def test_respawn_guard_unresolvable_pr_state_still_holds(kanban_home, monkeypatch):
    """When PR state can't be resolved live, the guard preserves the old
    conservative (text-only) behavior — unknown state still holds, never
    silently permits a respawn that might duplicate a genuinely open PR."""
    monkeypatch.setattr(kb, "_resolve_pr_open_state", lambda o, r, n: None)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="unresolvable-pr", assignee="alice")
        _make_code_task(conn, task_id)
        _add_pr_comment(conn, task_id, int(time.time()) - 10)

        reason = kb.check_respawn_guard(conn, task_id)

    assert reason == "active_pr"
