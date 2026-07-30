"""Behavioral contracts for governed Kanban intake."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_intake


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_paragraph_intake_is_idempotent_and_emits_one_typed_event(kanban_home):
    with kb.connect_closing() as conn:
        first, created = kanban_intake.receive(conn, text="Build the telemetry contract", received_by="haa")
        second, created_again = kanban_intake.receive(conn, text="Build the telemetry contract", received_by="haa")
        task = kb.get_task(conn, first)
        events = kb.list_events(conn, first)
    assert created is True
    assert created_again is False
    assert second == first
    assert task.status == "triage"
    assert "intake_source_type: paragraph" in task.body
    intake_events = [event for event in events if event.kind == "intake_received"]
    assert len(intake_events) == 1
    assert intake_events[0].payload["idempotency_key"] == task.idempotency_key
    assert intake_events[0].payload["source_ref_hash"]


def test_linear_url_shape_and_url_normalization_deduplicate(kanban_home):
    upper = "HTTPS://LINEAR.APP/acme/issue/HEL-1234/test"
    lower = "https://linear.app/acme/issue/HEL-1234/test"
    with kb.connect_closing() as conn:
        first, _ = kanban_intake.receive(conn, text=upper, received_by="haa")
        second, _ = kanban_intake.receive(conn, text=lower, received_by="haa")
        task = kb.get_task(conn, first)
    assert first == second
    assert "intake_source_type: linear_url" in task.body


def test_same_file_content_from_two_paths_deduplicates_attachment(kanban_home, tmp_path):
    first_path = tmp_path / "one.pdf"
    second_path = tmp_path / "two.pdf"
    first_path.write_bytes(b"same-pdf-content")
    second_path.write_bytes(b"same-pdf-content")
    with kb.connect_closing() as conn:
        first, _ = kanban_intake.receive(conn, files=[first_path], received_by="haa")
        second, created = kanban_intake.receive(conn, files=[second_path], received_by="haa")
        attachments = kb.list_attachments(conn, first)
    assert second == first
    assert created is False
    assert len(attachments) == 1
    assert attachments[0].content_sha256


def test_cli_intake_returns_machine_readable_task(kanban_home):
    payload = json.loads(kc.run_slash("intake --text 'one raw item' --received-by haa --json"))
    assert payload["created"] is True
    assert payload["status"] == "triage"
    assert payload["idempotency_key"]
