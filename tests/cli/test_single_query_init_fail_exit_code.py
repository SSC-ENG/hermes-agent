"""Single-query (-q) init/run failures must exit non-zero.

Kanban workers are spawned with ``hermes -p <profile> --cli chat -q ...``.
When agent init fails (missing provider package, bad credentials, etc.)
the human-facing -q path historically always returned to the shell with
rc=0 after printing "Goodbye!". The dispatcher then counted that as a
clean-exit protocol_violation after one dispatch tick (~60s with the
default ``dispatch_interval_seconds``).

This test pins the contract at main()'s return path without booting a
real model.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_human_single_query_exits_1_when_chat_returns_none(monkeypatch):
    import cli as cli_mod

    # Minimal fake CLI: chat() returns None (init / credentials failure)
    # and the exit-summary / finalize paths are no-ops.
    fake = SimpleNamespace(
        tool_progress_mode="off",
        session_id="sq-test",
        console=SimpleNamespace(print=lambda *a, **k: None),
        agent=None,
        conversation_history=[],
    )
    fake._claim_active_session = lambda *a, **k: True
    fake._release_active_session = lambda: None
    fake._ensure_runtime_credentials = lambda: True
    fake._show_security_advisories = lambda: None
    fake._print_exit_summary = lambda *a, **k: None
    fake.chat = lambda *a, **k: None  # init failure shape

    monkeypatch.setattr(cli_mod, "HermesCLI", lambda *a, **k: fake)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda c: None)
    monkeypatch.setattr(cli_mod, "_collect_query_images", lambda q, img: (q, []))
    # Avoid real signal install / profile resolve noise
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *a, **k: None)

    with pytest.raises(SystemExit) as ei:
        cli_mod.main(query="work kanban task t_x", quiet=False)
    assert ei.value.code == 1


def test_human_single_query_exits_0_when_chat_returns_text(monkeypatch):
    import cli as cli_mod

    fake = SimpleNamespace(
        tool_progress_mode="off",
        session_id="sq-test-ok",
        console=SimpleNamespace(print=lambda *a, **k: None),
        agent=None,
        conversation_history=[],
    )
    fake._claim_active_session = lambda *a, **k: True
    fake._release_active_session = lambda: None
    fake._ensure_runtime_credentials = lambda: True
    fake._show_security_advisories = lambda: None
    fake._print_exit_summary = lambda *a, **k: None
    fake.chat = lambda *a, **k: "done"

    monkeypatch.setattr(cli_mod, "HermesCLI", lambda *a, **k: fake)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda c: None)
    monkeypatch.setattr(cli_mod, "_collect_query_images", lambda q, img: (q, []))
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *a, **k: None)

    # main() returns normally (no SystemExit) on success for the human -q path
    cli_mod.main(query="hello", quiet=False)
