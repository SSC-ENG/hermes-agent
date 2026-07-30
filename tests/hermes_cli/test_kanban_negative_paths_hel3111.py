"""Negative-path proof harness for HEL-3111 reclaim/terminal contracts."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb._INITIALIZED_PATHS.discard(str((home / "kanban.db").resolve()))
    kb.init_db()
    return home


def _rewind_task(
    conn: sqlite3.Connection, task_id: str, *, started_seconds: int,
) -> None:
    started = int(time.time()) - started_seconds
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (started, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (started, task_id),
        )


def _event_kinds(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [event.kind for event in kb.list_events(conn, task_id)]


def _task(conn: sqlite3.Connection, task_id: str) -> kb.Task:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def _latest_run(conn: sqlite3.Connection, task_id: str) -> kb.Run:
    run = kb.latest_run(conn, task_id)
    assert run is not None
    return run


def test_missing_worker_pid_is_not_falsely_reaped(kanban_home: Path) -> None:
    """A running row without a PID is not evidence of a crashed worker."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="missing pid", assignee="worker")
        kb.claim_task(conn, task_id)
        assert kb.detect_crashed_workers(conn) == []
        assert _task(conn, task_id).status == "running"
        assert Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent == kanban_home


def test_dead_pid_is_reclaimed_and_run_is_closed(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead host-local PID is reclaimed and recorded as crashed."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="dead pid", assignee="worker")
        kb.claim_task(conn, task_id)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        kb._set_worker_pid(conn, task_id, dead.pid)
        _rewind_task(conn, task_id, started_seconds=120)
        kb._record_worker_exit(dead.pid, 1 << 8)
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

        crashed = kb.detect_crashed_workers(conn)
        task = _task(conn, task_id)
        assert crashed == [task_id]
        assert task.status == "ready"
        assert "crashed" in _event_kinds(conn, task_id)
        assert _latest_run(conn, task_id).outcome == "crashed"


def test_stale_heartbeat_reclaims_alive_pid(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeat staleness reclaims a technically alive worker."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="stale heartbeat", assignee="worker")
        kb.claim_task(
            conn,
            task_id,
            claimer=f"{kb._claimer_id().split(':', 1)[0]}:worker",
        )
        _rewind_task(conn, task_id, started_seconds=5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ?, last_heartbeat_at = ? "
                "WHERE id = ?",
                (99999, int(time.time()) - 2 * 3600, task_id),
            )
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *args, **kwargs: {
                "prev_pid": 99999,
                "host_local": True,
                "termination_attempted": True,
                "terminated": True,
                "sigkill": False,
            },
        )

        assert kb.detect_stale_running(
            conn, stale_timeout_seconds=4 * 3600,
        ) == [task_id]
        assert _task(conn, task_id).status == "ready"
        assert "stale" in _event_kinds(conn, task_id)


def test_expired_claim_without_pid_is_reclaimed(kanban_home: Path) -> None:
    """An expired claim is reclaimed even when no worker PID was recorded."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="expired lease", assignee="worker")
        kb.claim_task(conn, task_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = NULL, claim_expires = ? WHERE id = ?",
                (int(time.time()) - 1, task_id),
            )

        assert kb.release_stale_claims(conn) == 1
        assert _task(conn, task_id).status == "ready"
        reclaimed = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "reclaimed"
        ][-1]
        assert reclaimed.payload is not None
        assert reclaimed.payload["worker_pid"] is None


def test_duplicate_dispatcher_claim_has_one_winner(kanban_home: Path) -> None:
    """Concurrent dispatchers cannot both transition one ready row."""
    with kb.connect() as setup:
        task_id = kb.create_task(setup, title="claim race", assignee="worker")

    barrier = threading.Barrier(2)

    def attempt(claimer: str) -> bool:
        with kb.connect() as conn:
            barrier.wait()
            return kb.claim_task(conn, task_id, claimer=claimer) is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["dispatcher:a", "dispatcher:b"]))

    assert results.count(True) == 1
    with kb.connect() as conn:
        task = _task(conn, task_id)
        assert task.status == "running"
        assert task.claim_lock in {"dispatcher:a", "dispatcher:b"}
        claimed = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "claimed"
        ]
        assert len(claimed) == 1


def test_late_completion_after_gave_up_is_explicit_recovery(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late worker completion records recovery instead of silent conflict."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="late completion",
            assignee="worker",
            max_retries=1,
        )
        kb.claim_task(conn, task_id)
        run_id = _task(conn, task_id).current_run_id
        assert run_id is not None
        kb._set_worker_pid(conn, task_id, 99999)
        _rewind_task(conn, task_id, started_seconds=120)
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(
            kb, "_classify_worker_exit", lambda _pid: ("clean_exit", 0),
        )

        assert kb.detect_crashed_workers(conn) == [task_id]
        assert _task(conn, task_id).status == "blocked"
        assert _event_kinds(conn, task_id)[-2:] == [
            "protocol_violation",
            "gave_up",
        ]

        assert kb.complete_task(
            conn,
            task_id,
            summary="worker completed after dispatcher gave up",
            expected_run_id=run_id,
        )
        assert _task(conn, task_id).status == "done"
        events = kb.list_events(conn, task_id)
        recovery = [event for event in events if event.kind == "recovered"]
        assert len(recovery) == 1
        assert recovery[0].payload is not None
        assert recovery[0].payload["reason"] == "late_completion"
        assert recovery[0].payload["from_outcome"] == "gave_up"
        assert recovery[0].payload["to_outcome"] == "completed"
        assert _event_kinds(conn, task_id)[-3:] == [
            "gave_up",
            "recovered",
            "completed",
        ]
        assert _latest_run(conn, task_id).id == run_id
        assert _latest_run(conn, task_id).outcome == "completed"


def test_current_retry_completion_is_not_mislabeled_as_late_recovery(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful retry after explicit unblock is a normal completion."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="current retry",
            assignee="worker",
            max_retries=1,
        )
        kb.claim_task(conn, task_id)
        kb._set_worker_pid(conn, task_id, 99998)
        _rewind_task(conn, task_id, started_seconds=120)
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(
            kb, "_classify_worker_exit", lambda _pid: ("clean_exit", 0),
        )
        assert kb.detect_crashed_workers(conn) == [task_id]
        assert kb.unblock_task(conn, task_id)

        kb.claim_task(conn, task_id)
        run_id = _task(conn, task_id).current_run_id
        assert run_id is not None
        assert kb.complete_task(
            conn,
            task_id,
            summary="current retry completed",
            expected_run_id=run_id,
        )

        assert "recovered" not in _event_kinds(conn, task_id)


def test_late_completion_through_worker_tool_recovers_exact_gave_up_run(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker tool accepts its own gave-up run without weakening CAS."""
    from tools import kanban_tools

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="tool late completion",
            assignee="worker",
            max_retries=1,
        )
        kb.claim_task(conn, task_id)
        run_id = _task(conn, task_id).current_run_id
        assert run_id is not None
        kb._set_worker_pid(conn, task_id, 99997)
        _rewind_task(conn, task_id, started_seconds=120)
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(
            kb, "_classify_worker_exit", lambda _pid: ("clean_exit", 0),
        )
        assert kb.detect_crashed_workers(conn) == [task_id]

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    result = kanban_tools._handle_complete(
        {
            "summary": "late worker tool completion",
            "task_id": task_id,
        }
    )
    assert '"ok": true' in result.lower()

    with kb.connect() as conn:
        assert _task(conn, task_id).status == "done"
        assert _event_kinds(conn, task_id)[-3:] == [
            "gave_up",
            "recovered",
            "completed",
        ]
