"""Behavioral contracts for governed Kanban telemetry review."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_telemetry as telemetry


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _insert_event(conn, task_id, kind, payload, created_at):
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        (task_id, kind, json.dumps(payload), created_at),
    )


def test_window_is_half_open_and_preserves_unknown_states(board):
    end = 1_800_000_000
    start = end - telemetry.REVIEW_WINDOW_SECONDS
    with kb.connect_closing() as conn:
        at_start = kb.create_task(conn, title="start", triage=True)
        at_end = kb.create_task(conn, title="end", triage=True)
        _insert_event(conn, at_start, "intake_received", {"source_type": "paragraph", "source_ref_hash": "a", "idempotency_key": "a", "received_by": "haa"}, start)
        _insert_event(conn, at_end, "intake_received", {"source_type": "paragraph", "source_ref_hash": "b", "idempotency_key": "b", "received_by": "haa"}, end)
        report = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end, generated_at=end)
    intake_holes = [h for h in report["holes"] if h["rule_id"].startswith("INTAKE.")]
    assert intake_holes
    assert all(at_end not in h["subject"]["task_ids"] for h in intake_holes)
    assert report["instrumentation"]["workflow_stages"] == "UNKNOWN"
    assert report["instrumentation"]["haa_decisions"] == "UNINSTRUMENTED"
    assert report["instrumentation"]["linear_estimates"] == "UNINSTRUMENTED"
    assert "haa_decision" not in report["summary"]


def test_complete_typed_decisions_suppress_intake_and_handoff_holes(board):
    end = 1_800_000_000
    received = end - 4000
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="governed", triage=True)
        for kind, payload in [
            ("intake_received", {"source_type": "paragraph", "source_ref_hash": "abc", "idempotency_key": "abc", "received_by": "haa"}),
            ("intake_classified", {"intake_id": task, "domain": "engineering", "classification": "build"}),
            ("decomposition_decided", {"root_task_id": task, "fanout": False, "child_ids": [], "dependency_edges": []}),
            ("handoff_emitted", {"handoff_id": "h1", "from_owner": "paul-park", "to_owner": "cole-espinoza", "artifact_refs": [], "acceptance_contract": "build", "next_expected_event": "handoff_accepted", "due_by": end - 1}),
            ("handoff_accepted", {"handoff_id": "h1", "receiver": "cole-espinoza", "run_id": 1}),
        ]:
            _insert_event(conn, task, kind, payload, received)
        report = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end, generated_at=end)
    rule_ids = {hole["rule_id"] for hole in report["holes"]}
    assert "INTAKE.NOT_CLASSIFIED" not in rule_ids
    assert "INTAKE.NOT_DECOMPOSED" not in rule_ids
    assert "HANDOFF.NOT_ACCEPTED" not in rule_ids


def test_finding_keys_are_stable_and_markdown_is_derived(board, tmp_path):
    end = 1_800_000_000
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="hole", triage=True)
        _insert_event(conn, task, "intake_received", {"source_type": "paragraph", "source_ref_hash": "abc", "idempotency_key": "abc", "received_by": "haa"}, end - 4000)
        first = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end, generated_at=end)
        second = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end, generated_at=end + 100)
    assert [h["finding_key"] for h in first["holes"]] == [h["finding_key"] for h in second["holes"]]
    json_path, md_path = telemetry.write_artifacts(first, tmp_path / "out")
    loaded = json.loads(json_path.read_text())
    markdown = md_path.read_text()
    for hole in loaded["holes"]:
        assert hole["rule_id"] in markdown
        assert hole["finding_key"] in markdown
    assert "leaderboard" not in markdown.lower()
    assert "throughput" not in markdown.lower()


def test_watermark_excludes_late_event_from_current_artifact(board):
    end = 1_800_000_000
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="late", triage=True)
        _insert_event(conn, task, "intake_received", {"source_type": "paragraph", "source_ref_hash": "abc", "idempotency_key": "abc", "received_by": "haa"}, end - 4000)
        before = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end, generated_at=end)
        before_keys = [h["finding_key"] for h in before["holes"]]
        before_high = before["review"]["event_id_high_inclusive"]
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)", (task, "intake_classified", json.dumps({"intake_id": task, "domain": "engineering", "classification": "build"}), end - 100))
        assert int(conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]) > before_high
    assert before_keys == [h["finding_key"] for h in before["holes"]]


def test_persisted_findings_become_persisting_on_next_cycle(board, tmp_path):
    end = 1_800_000_000
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="persistent", triage=True)
        _insert_event(conn, task, "intake_received", {"source_type": "paragraph", "source_ref_hash": "abc", "idempotency_key": "abc", "received_by": "haa"}, end - 4000)
        first = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end, generated_at=end)
        paths = telemetry.write_artifacts(first, tmp_path / "one")
        telemetry.persist_review(conn, first, *paths)
        second = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end + telemetry.CADENCE_SECONDS, generated_at=end + telemetry.CADENCE_SECONDS)
    states = {h["rule_id"]: h["state"] for h in second["holes"]}
    assert states["INTAKE.NOT_CLASSIFIED"] == "PERSISTING"
    assert states["INSTRUMENTATION.HAA_UNINSTRUMENTED"] == "PERSISTING"


def test_governed_event_validation_fails_closed(board):
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="event")
        with pytest.raises(ValueError, match="missing required"):
            telemetry.record_event(conn, task, "gate_required", {"gate_id": "g1"})
        telemetry.record_event(conn, task, "linear_scoped", {"linear_issue_key": "HEL-1", "sub_issue_keys": ["HEL-2"], "cptc_estimates": {"HEL-2": 3}})
        events = kb.list_events(conn, task)
    scoped = [event for event in events if event.kind == "linear_scoped"]
    assert len(scoped) == 1
    assert scoped[0].payload["schema_version"] == 1
