"""Tests for the kanban `promote` verb (issue #28822).

The realistic bug scenario from #28822 is: a child task ends up in
``todo`` with all its parents already ``done`` (because the
auto-promote daemon hasn't run, or a manual close raced it).
Direct-SQL setup is used to construct that state deterministically.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _stuck_todo(conn, *, parents_done=True, n_parents=1):
    """Build the #28822 scenario: child in 'todo' whose parents may
    have closed as 'done' without the auto-promote logic firing.
    """
    parent_ids = [
        kb.create_task(conn, title=f"parent{i}", assignee="setup")
        for i in range(n_parents)
    ]
    child_id = kb.create_task(
        conn, title="child", parents=parent_ids, assignee="setup"
    )
    assert kb.get_task(conn, child_id).status == "todo"
    if parents_done:
        for pid in parent_ids:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (pid,)
            )
    return child_id, parent_ids


def test_promote_stuck_todo_succeeds(conn):
    child, _ = _stuck_todo(conn, parents_done=True)
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert ok and err is None
    assert kb.get_task(conn, child).status == "ready"








# ---------------------------------------------------------------------------
# CLI `_cmd_promote` — bulk via `--ids` (the issue's anti-respawn use case:
# promote all children of a closed parent in one command).
# ---------------------------------------------------------------------------


def _promote_ns(task_id, *, ids=None, reason=None, force=False,
                dry_run=False, as_json=False):
    return argparse.Namespace(
        task_id=task_id,
        reason=list(reason or []),
        ids=list(ids or []) or None,
        force=force,
        dry_run=dry_run,
        json=as_json,
    )


def test_cli_promote_bulk_ids_promotes_all(kanban_home, capsys):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        children = [
            kb.create_task(conn, title=f"c{i}", parents=[parent])
            for i in range(3)
        ]
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    rc = kb_cli._cmd_promote(_promote_ns(children[0], ids=children[1:]))
    assert rc == 0
    out = capsys.readouterr().out
    for c in children:
        assert c in out
    with kb.connect() as conn:
        for c in children:
            assert kb.get_task(conn, c).status == "ready"


# ---------------------------------------------------------------------------
# HEL-3219: task_links.link_type + promotion gating (depends-on vs gates)
# ---------------------------------------------------------------------------


def _link_type(conn, parent_id: str, child_id: str):
    row = conn.execute(
        "SELECT link_type FROM task_links WHERE parent_id = ? AND child_id = ?",
        (parent_id, child_id),
    ).fetchone()
    assert row is not None
    return row["link_type"]


def _force_status(conn, task_id: str, status: str) -> None:
    """Set status without going through lifecycle helpers."""
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))


def _sticky_block_parent(conn, parent_id: str) -> None:
    """Keep a parent blocked across recompute_ready (worker-style sticky block).

    Parentless blocked tasks without a sticky block event are auto-recovered
    by recompute_ready; a real kanban_block leaves a sticky 'blocked' event.
    """
    # block_task only accepts running/ready; ensure ready first.
    _force_status(conn, parent_id, "ready")
    ok = kb.block_task(conn, parent_id, reason="review-required: hold for gates test", kind="needs_input")
    assert ok is True
    assert kb.get_task(conn, parent_id).status == "blocked"


def test_link_type_column_exists_on_fresh_db(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_links)")}
    assert "link_type" in cols


def test_legacy_null_link_type_is_hard_dependency(conn):
    """AC-3: existing NULL rows keep hard-dependency semantics."""
    parent = kb.create_task(conn, title="parent-hard", assignee="setup")
    child = kb.create_task(
        conn, title="child-hard", parents=[parent], assignee="setup"
    )
    assert _link_type(conn, parent, child) is None
    assert kb.get_task(conn, child).status == "todo"

    # Parent still open (todo/ready/running/blocked) must not promote child.
    # Use sticky block for blocked so recompute doesn't auto-recover parent.
    for status in ("todo", "ready", "running"):
        _force_status(conn, parent, status)
        _force_status(conn, child, "todo")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo", status

    _sticky_block_parent(conn, parent)
    _force_status(conn, child, "todo")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, parent).status == "blocked"
    assert kb.get_task(conn, child).status == "todo"

    # Terminal parent statuses satisfy a hard/NULL link.
    _force_status(conn, parent, "done")
    n = kb.recompute_ready(conn)
    assert n >= 1
    assert kb.get_task(conn, child).status == "ready"

    # archived also satisfies
    parent2 = kb.create_task(conn, title="parent-arch", assignee="setup")
    child2 = kb.create_task(
        conn, title="child-arch", parents=[parent2], assignee="setup"
    )
    _force_status(conn, parent2, "archived")
    _force_status(conn, child2, "todo")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child2).status == "ready"


def test_gates_link_promotes_while_parent_blocked_or_running(conn):
    """AC-1: gates link lets child promote while parent is blocked/running."""
    parent = kb.create_task(conn, title="parent-gates", assignee="setup")
    child = kb.create_task(
        conn,
        title="child-gates",
        parents=[parent],
        parent_link_type="gates",
        assignee="setup",
    )
    assert _link_type(conn, parent, child) == "gates"

    # Sticky-blocked parent: child should promote; parent stays blocked.
    _sticky_block_parent(conn, parent)
    _force_status(conn, child, "todo")
    n = kb.recompute_ready(conn)
    assert n >= 1
    assert kb.get_task(conn, parent).status == "blocked"
    assert kb.get_task(conn, child).status == "ready"

    # Running parent (no auto-promote path for running).
    _force_status(conn, child, "todo")
    _force_status(conn, parent, "running")
    n = kb.recompute_ready(conn)
    assert n >= 1
    assert kb.get_task(conn, parent).status == "running"
    assert kb.get_task(conn, child).status == "ready"


def test_gates_link_does_not_promote_for_non_satisfying_parent(conn):
    """Gates still waits when parent is only todo/ready/failed/etc."""
    parent = kb.create_task(conn, title="parent-ns", assignee="setup")
    child = kb.create_task(
        conn,
        title="child-ns",
        parents=[parent],
        parent_link_type="gates",
        assignee="setup",
    )
    # Parentless todo/ready parents get auto-promoted themselves; pin them
    # and assert the child stays todo while parent is non-satisfying.
    for status in ("todo", "ready", "failed", "cancelled", "triage"):
        _force_status(conn, parent, status)
        _force_status(conn, child, "todo")
        # recompute may promote the parent if it is todo/blocked with no
        # parents; after the call, check whether the *child* moved only when
        # the parent ended up in a gates-satisfying status.
        kb.recompute_ready(conn)
        parent_status = kb.get_task(conn, parent).status
        child_status = kb.get_task(conn, child).status
        if kb._parent_satisfies_link(parent_status, "gates"):
            assert child_status == "ready", (status, parent_status)
        else:
            assert child_status == "todo", (status, parent_status)


def test_mixed_hard_and_gates_parents_require_all_satisfied(conn):
    hard = kb.create_task(conn, title="hard-parent", assignee="setup")
    soft = kb.create_task(conn, title="soft-parent", assignee="setup")
    child = kb.create_task(conn, title="mixed-child", assignee="setup")
    assert kb.get_task(conn, child).status == "ready"
    kb.link_tasks(conn, hard, child, link_type=None)
    kb.link_tasks(conn, soft, child, link_type="gates")
    assert kb.get_task(conn, child).status == "todo"

    # Soft parent sticky-blocked satisfies gates; hard still open.
    _sticky_block_parent(conn, soft)
    _force_status(conn, hard, "running")
    _force_status(conn, child, "todo")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, soft).status == "blocked"
    assert kb.get_task(conn, child).status == "todo"

    # Finish hard parent → both edges satisfied.
    _force_status(conn, hard, "done")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"


def test_claim_accepts_gates_child_with_blocked_parent(conn):
    parent = kb.create_task(conn, title="claim-parent", assignee="setup")
    child = kb.create_task(
        conn,
        title="claim-child",
        parents=[parent],
        parent_link_type="gates",
        assignee="setup",
    )
    _sticky_block_parent(conn, parent)
    _force_status(conn, child, "todo")
    assert kb.recompute_ready(conn) >= 1
    assert kb.get_task(conn, child).status == "ready"
    claimed = kb.claim_task(conn, child, claimer="tester")
    assert claimed is not None
    assert claimed.status == "running"


def test_claim_rejects_hard_child_with_running_parent(conn):
    parent = kb.create_task(conn, title="claim-hard-p", assignee="setup")
    child = kb.create_task(
        conn, title="claim-hard-c", parents=[parent], assignee="setup"
    )
    # Force child ready despite open parent (race simulation).
    _force_status(conn, parent, "running")
    _force_status(conn, child, "ready")
    claimed = kb.claim_task(conn, child, claimer="tester")
    assert claimed is None
    assert kb.get_task(conn, child).status == "todo"


def test_promote_task_respects_gates_link(conn):
    parent = kb.create_task(conn, title="promote-p", assignee="setup")
    child = kb.create_task(
        conn,
        title="promote-c",
        parents=[parent],
        parent_link_type="gates",
        assignee="setup",
    )
    _sticky_block_parent(conn, parent)
    _force_status(conn, child, "todo")
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert ok and err is None
    assert kb.get_task(conn, child).status == "ready"


def test_promote_task_refuses_hard_open_parent_unless_forced(conn):
    parent = kb.create_task(conn, title="promote-hard-p", assignee="setup")
    child = kb.create_task(
        conn, title="promote-hard-c", parents=[parent], assignee="setup"
    )
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert not ok
    assert "unsatisfied parent" in (err or "")
    ok, err = kb.promote_task(conn, child, actor="tester", force=True)
    assert ok and err is None
    assert kb.get_task(conn, child).status == "ready"


def test_unblock_task_respects_gates_link(conn):
    parent = kb.create_task(conn, title="unblock-p", assignee="setup")
    child = kb.create_task(
        conn,
        title="unblock-c",
        parents=[parent],
        parent_link_type="gates",
        assignee="setup",
    )
    _force_status(conn, parent, "running")
    # Child must be blocked to unblock; use sticky block then unblock.
    _force_status(conn, child, "ready")
    assert kb.block_task(conn, child, reason="hold", kind="needs_input") is True
    assert kb.get_task(conn, child).status == "blocked"
    assert kb.unblock_task(conn, child) is True
    assert kb.get_task(conn, child).status == "ready"


def test_link_tasks_relink_updates_type(conn):
    parent = kb.create_task(conn, title="relink-p", assignee="setup")
    child = kb.create_task(conn, title="relink-c", assignee="setup")
    kb.link_tasks(conn, parent, child)  # hard
    assert _link_type(conn, parent, child) is None
    assert kb.get_task(conn, child).status == "todo"

    _sticky_block_parent(conn, parent)
    kb.link_tasks(conn, parent, child, link_type="gates")
    assert _link_type(conn, parent, child) == "gates"
    _force_status(conn, child, "todo")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, parent).status == "blocked"
    assert kb.get_task(conn, child).status == "ready"


def test_invalid_link_type_rejected(conn):
    parent = kb.create_task(conn, title="bad-p", assignee="setup")
    child = kb.create_task(conn, title="bad-c", assignee="setup")
    with pytest.raises(ValueError, match="link_type"):
        kb.link_tasks(conn, parent, child, link_type="related")
    with pytest.raises(ValueError, match="link_type"):
        kb.create_task(
            conn,
            title="bad-child2",
            parents=[parent],
            parent_link_type="related",
            assignee="setup",
        )


def test_migration_adds_link_type_to_legacy_task_links(conn):
    """Additive migration path: boards pre-column get link_type on migrate.

    Rebuild task_links without link_type (legacy shape), keep an edge, then
    re-run ``_migrate_add_optional_columns`` and assert the column returns
    with NULL on existing rows (AC-3 hard-dep semantics preserved).
    """
    parent = kb.create_task(conn, title="mig-p", assignee="setup")
    child = kb.create_task(
        conn, title="mig-c", parents=[parent], assignee="setup"
    )
    # Collapse to legacy table shape (no link_type column).
    conn.executescript(
        """
        CREATE TABLE task_links_legacy (
            parent_id TEXT NOT NULL,
            child_id TEXT NOT NULL,
            PRIMARY KEY (parent_id, child_id)
        );
        INSERT INTO task_links_legacy (parent_id, child_id)
            SELECT parent_id, child_id FROM task_links;
        DROP TABLE task_links;
        ALTER TABLE task_links_legacy RENAME TO task_links;
        """
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_links)")}
    assert "link_type" not in cols

    kb._migrate_add_optional_columns(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_links)")}
    assert "link_type" in cols
    row = conn.execute(
        "SELECT link_type FROM task_links WHERE parent_id=? AND child_id=?",
        (parent, child),
    ).fetchone()
    assert row is not None
    assert row["link_type"] is None  # legacy row stays NULL → hard dep

    # Hard semantics still hold: open parent keeps child todo; done promotes.
    _force_status(conn, parent, "running")
    _force_status(conn, child, "todo")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "todo"
    _force_status(conn, parent, "done")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"
