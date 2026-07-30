"""Terminal conversation-loop errors must never look like a bare protocol
violation (t_8165e956 / truncation give-up burn loop).

When run_conversation returns completed=False/partial=True with an error
string, a kanban worker must:

1. Auto-call block_task(kind=transient) with the loop error as reason, AND/OR
2. Exit non-zero (KANBAN_TERMINAL_LOOP_EXIT_CODE) so the dispatcher records a
   distinct terminal-loop-error, never a clean rc=0 protocol_violation.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    # Isolate from any live board env injected by a parent kanban worker.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    assert str(kb.kanban_db_path()).startswith(str(home)), (
        "probe is not isolated from the live board"
    )
    return home


def _claim_running(conn, title: str = "loop-error-card") -> tuple[str, int]:
    tid = kb.create_task(conn, title=title, assignee="worker")
    host_prefix = kb._claimer_id().split(":", 1)[0]
    lock = f"{host_prefix}:mock"
    kb.claim_task(conn, tid, claimer=lock)
    run_id = kb._current_run_id(conn, tid)
    assert run_id is not None
    return tid, int(run_id)


def test_is_terminal_conversation_loop_result_shapes():
    assert kb.is_terminal_conversation_loop_result(
        {
            "completed": False,
            "partial": True,
            "error": "Response remained truncated after 4 continuation attempts",
        }
    )
    assert kb.is_terminal_conversation_loop_result(
        {"completed": False, "error": "provider blew up", "failed": True}
    )
    assert not kb.is_terminal_conversation_loop_result(
        {"completed": True, "final_response": "done"}
    )
    assert not kb.is_terminal_conversation_loop_result(
        {"failed": True, "failure_reason": "rate_limit", "error": "429"}
    )
    assert not kb.is_terminal_conversation_loop_result(
        {"completed": False, "interrupted": True}
    )


def test_finalize_blocks_task_on_truncation_result(kanban_home, monkeypatch):
    conn = kb.connect()
    try:
        tid, run_id = _claim_running(conn)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        result = {
            "final_response": "partial answer",
            "completed": False,
            "partial": True,
            "error": "Response remained truncated after 4 continuation attempts",
            "api_calls": 5,
        }
        status = kb.finalize_kanban_worker_terminal_loop_error(
            tid, result=result, kind="transient",
        )
        assert status["acted"] is True, status
        assert status["status"] == "blocked"
        assert "terminal loop error:" in status["reason"]
        assert "truncated" in status["reason"]

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "transient"

        run = kb.latest_run(conn, tid)
        assert run is not None
        assert run.outcome == "blocked"
        assert run.error is not None
        assert "truncated" in run.error
        meta = json.loads(run.metadata) if isinstance(run.metadata, str) else run.metadata
        assert meta is not None
        assert meta.get("source") == "worker_terminal_loop_error"

        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "blocked" in kinds
        assert "protocol_violation" not in kinds
    finally:
        conn.close()


def test_finalize_idempotent_when_already_blocked(kanban_home, monkeypatch):
    conn = kb.connect()
    try:
        tid, run_id = _claim_running(conn)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        result = {
            "completed": False,
            "partial": True,
            "error": "Response remained truncated after 4 continuation attempts",
        }
        first = kb.finalize_kanban_worker_terminal_loop_error(tid, result=result)
        assert first["acted"] is True
        second = kb.finalize_kanban_worker_terminal_loop_error(tid, result=result)
        assert second["acted"] is False
        assert second["status"] == "blocked"
        assert second["error"] is None
    finally:
        conn.close()


def test_quiet_main_auto_blocks_and_exits_76_on_partial(kanban_home, monkeypatch):
    """Quiet (-Q) worker path: simulate loop partial result → block + rc=76."""
    import cli as cli_mod

    conn = kb.connect()
    try:
        tid, run_id = _claim_running(conn, title="quiet-partial")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)

    def run_conversation(*, user_message, conversation_history):
        return {
            "final_response": "half a tool call",
            "completed": False,
            "partial": True,
            "error": "Response remained truncated after 4 continuation attempts",
            "failed": False,
        }

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.provider = "test-provider"
            self.model = "test-model"
            self.session_id = "quiet-loop-session"
            self.conversation_history = []
            self._active_agent_route_signature = "same-route"
            self.agent = SimpleNamespace(
                session_id="quiet-loop-session",
                platform="cli",
                quiet_mode=False,
                suppress_status_output=False,
                stream_delta_callback=object(),
                tool_gen_callback=object(),
                run_conversation=run_conversation,
            )

        def _claim_active_session(self, surface, *, stderr=False):
            return True

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, effective_query):
            return {
                "signature": "same-route",
                "model": None,
                "runtime": None,
                "request_overrides": None,
            }

        def _init_agent(self, **kwargs):
            return True

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_k: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda *_a, **_k: None)

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main(query="work kanban task", quiet=True, toolsets="terminal")

    assert exc_info.value.code == kb.KANBAN_TERMINAL_LOOP_EXIT_CODE

    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", task.status
        assert task.block_kind == "transient"
        run = kb.latest_run(conn, tid)
        assert run is not None
        assert "truncated" in (run.error or run.summary or "")
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "blocked" in kinds
        assert "protocol_violation" not in kinds
    finally:
        conn.close()


def test_detect_crashed_workers_terminal_loop_exit_not_bare_protocol_violation(
    kanban_home,
):
    """If the worker only managed exit 76 (block write failed), reap must
    surface ``terminal_loop_error``, not a plain protocol_violation.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="exit-76-only", assignee="worker")
        host_prefix = kb._claimer_id().split(":", 1)[0]
        lock = f"{host_prefix}:mock"
        kb.claim_task(conn, tid, claimer=lock)
        fake_pid = 888776
        kb._set_worker_pid(conn, tid, fake_pid)

        # Encode exit status the way waitpid would (status = code << 8).
        raw = kb.KANBAN_TERMINAL_LOOP_EXIT_CODE << 8
        kb._record_worker_exit(fake_pid, raw)
        original_alive = kb._pid_alive
        kb._pid_alive = lambda p: False
        try:
            crashed = kb.detect_crashed_workers(conn)
        finally:
            kb._pid_alive = original_alive

        assert tid in crashed
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert "terminal loop error" in (task.last_failure_error or "").lower()
        assert "protocol violation" not in (task.last_failure_error or "").lower()

        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "terminal_loop_error" in kinds
        assert "protocol_violation" not in kinds
        assert "gave_up" in kinds
    finally:
        conn.close()


def test_classify_worker_exit_terminal_loop_code():
    fake_pid = 424242
    raw = kb.KANBAN_TERMINAL_LOOP_EXIT_CODE << 8
    kb._record_worker_exit(fake_pid, raw)
    kind, code = kb._classify_worker_exit(fake_pid)
    assert kind == "terminal_loop_error"
    assert code == kb.KANBAN_TERMINAL_LOOP_EXIT_CODE
