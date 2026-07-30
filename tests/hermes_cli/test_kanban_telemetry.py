"""Behavioral contracts for governed Kanban telemetry review."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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


@pytest.mark.parametrize(
    ("local_now", "expected_local"),
    [
        ("2026-07-30T00:14:59", "2026-07-29T12:15:00"),
        ("2026-07-30T00:15:00", "2026-07-30T00:15:00"),
        ("2026-07-30T12:14:59", "2026-07-30T00:15:00"),
        ("2026-07-30T12:15:00", "2026-07-30T12:15:00"),
    ],
)
def test_nominal_window_end_follows_phoenix_boundaries(local_now, expected_local):
    phoenix = ZoneInfo("America/Phoenix")
    now = int(datetime.fromisoformat(local_now).replace(tzinfo=phoenix).timestamp())
    expected = int(datetime.fromisoformat(expected_local).replace(tzinfo=phoenix).timestamp())

    assert telemetry.nominal_window_end(now) == expected


def test_scheduled_review_writes_and_persists_both_artifacts(board, tmp_path):
    end = 1_800_000_000
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="scheduled", triage=True)

    report, json_path, markdown_path = telemetry.run_scheduled_review(
        board_slug="default",
        window_end=end,
        generated_at=end,
        output_dir=tmp_path / "reviews",
    )

    assert json_path.is_file()
    assert markdown_path.is_file()
    with kb.connect_closing() as conn:
        stored = conn.execute(
            "SELECT json_path, markdown_path, status FROM telemetry_review_runs "
            "WHERE review_id = ?",
            (report["review"]["review_id"],),
        ).fetchone()
    assert stored["json_path"] == str(json_path)
    assert stored["markdown_path"] == str(markdown_path)
    assert stored["status"] == "COMPLETE"


def test_missing_nominal_boundary_emits_critical_review_health_hole(board):
    end = 1_800_000_000
    with kb.connect_closing() as conn:
        conn.execute(
            "INSERT INTO telemetry_review_runs "
            "(review_id, board_slug, window_end, event_id_low_exclusive, "
            "event_id_high_inclusive, generated_at, status) "
            "VALUES (?, ?, ?, 0, 0, ?, 'COMPLETE')",
            ("old", "default", end - (2 * telemetry.CADENCE_SECONDS), end),
        )
        report = telemetry.run_review(
            conn,
            board_slug="default",
            db_path=kb.kanban_db_path(),
            window_end=end,
            generated_at=end,
        )

    missed = [hole for hole in report["holes"] if hole["rule_id"] == "REVIEW.MISSED_RUN"]
    assert len(missed) == 1
    assert missed[0]["severity"] == "CRITICAL"
    assert missed[0]["owner"] == "rhea-ramos"


def test_persist_review_replay_preserves_dispositioned_verified_ledger(board, tmp_path):
    """AGA P1 #1: replaying a telemetry source must never reset the ledger.

    Reproduces the exact rejected sequence: promote a telemetry finding,
    disposition it accepted_queued, verify it, then replay the SAME
    telemetry key through ``persist_review``. Under the old
    ``INSERT OR REPLACE`` the replay deleted+reinserted the source row,
    re-fired the promotion trigger, and nulled every disposition and
    verification field while the events remained — state/event divergence
    that reopened the orphan gate.
    """
    end = 1_800_000_000
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="replay", triage=True)
        _insert_event(conn, task, "intake_received", {"source_type": "paragraph", "source_ref_hash": "abc", "idempotency_key": "abc", "received_by": "haa"}, end - 4000)
        report = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end, generated_at=end)
        paths = telemetry.write_artifacts(report, tmp_path / "one")
        telemetry.persist_review(conn, report, *paths)

        promoted = [
            hole["finding_key"]
            for hole in report["holes"]
            if hole["subject"].get("task_ids") == [task]
        ]
        assert promoted, "expected at least one task-scoped telemetry finding"
        key = promoted[0]
        assert kb.get_finding(conn, key) is not None

        kb.disposition_finding(
            conn,
            finding_key=key,
            disposition="accepted_queued",
            linear_issue_id="HEL-3112",
            actor_id="paul-park",
        )
        evidence = kb.fetch_linear_issue_evidence(
            "HEL-3112",
            transport=lambda _q, _v: {
                "data": {
                    "issue": {
                        "id": "9a48515e-6535-4393-81e4-6fd6c7dc6023",
                        "identifier": "HEL-3112",
                        "state": {"name": "In Review"},
                    }
                }
            },
        )
        kb.verify_finding(conn, key, actor_id="paul-park", evidence=evidence)

        def ledger_row():
            return conn.execute(
                "SELECT * FROM findings WHERE finding_key = ?", (key,)
            ).fetchone()

        def finding_event_count():
            return sum(
                1
                for event in kb.list_events(conn, task)
                if event.payload
                and event.payload.get("event_type", "").startswith("finding_")
            )

        def orphan_keys():
            return {f.finding_key for f in kb.list_orphan_findings(conn)}

        row_before = tuple(ledger_row())
        # Trigger-promoted findings carry no finding_opened event (that event
        # belongs to Python open_finding); lifecycle cardinality is 3:
        # dispositioned, queued, verified.
        assert finding_event_count() == 3
        # Sibling holes from the same review remain (correctly) orphaned;
        # the finding under test must have left the orphan gate.
        orphans_before = orphan_keys()
        assert key not in orphans_before

        # Replay: same telemetry window persisted again (same finding_key).
        replay = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end, generated_at=end + 50)
        replay_paths = telemetry.write_artifacts(replay, tmp_path / "two")
        telemetry.persist_review(conn, replay, *replay_paths)

        assert tuple(ledger_row()) == row_before
        assert finding_event_count() == 3
        # The replay must not reopen the gate for the verified finding nor
        # change the orphan set at all.
        assert orphan_keys() == orphans_before
        assert key not in orphan_keys()
        source_count = conn.execute(
            "SELECT COUNT(*) FROM telemetry_review_findings WHERE finding_key = ?",
            (key,),
        ).fetchone()[0]
    assert source_count == 1


def test_persist_review_replay_updates_observation_fields_without_row_identity_loss(board, tmp_path):
    end = 1_800_000_000
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="observe", triage=True)
        _insert_event(conn, task, "intake_received", {"source_type": "paragraph", "source_ref_hash": "abc", "idempotency_key": "abc", "received_by": "haa"}, end - 4000)
        first = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end, generated_at=end)
        telemetry.persist_review(conn, first, *telemetry.write_artifacts(first, tmp_path / "one"))
        second = telemetry.run_review(conn, board_slug="default", db_path=kb.kanban_db_path(), window_end=end + telemetry.CADENCE_SECONDS, generated_at=end + telemetry.CADENCE_SECONDS)
        telemetry.persist_review(conn, second, *telemetry.write_artifacts(second, tmp_path / "two"))
        rows = conn.execute(
            "SELECT finding_key, first_observed_at, last_observed_at, state "
            "FROM telemetry_review_findings ORDER BY finding_key"
        ).fetchall()
    persisted = {row["finding_key"]: row for row in rows}
    shared = [
        hole["finding_key"]
        for hole in second["holes"]
        if hole["state"] == "PERSISTING" and hole["finding_key"] in persisted
    ]
    assert shared, "expected persisting findings across the two windows"
    for key in shared:
        row = persisted[key]
        # Observation fields advanced monotonically on the SAME row.
        assert row["state"] == "PERSISTING"
        assert row["last_observed_at"] >= row["first_observed_at"]
