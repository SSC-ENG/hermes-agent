"""Behavioral contracts for the finding-to-queue disposition gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _profile: True)
    kb.init_db()
    with kb.connect() as conn:
        intent_id = kb.create_task(conn, title="TRC readout", assignee="paul-park")
    return intent_id


def _finding_events(conn, intent_id):
    return [
        event.payload
        for event in kb.list_events(conn, intent_id)
        if event.payload
        and event.payload.get("event_type", "").startswith("finding_")
    ]


def test_accepted_finding_requires_one_issue_and_zero_orphans_after_verification(board):
    with kb.connect() as conn:
        finding = kb.open_finding(
            conn,
            finding_key="trc-2026-07-20:watchdog-proof",
            work_intent_id=board,
            source_system="trc",
            source_ref="TRC-2026-07-20#watchdog-proof",
            title="Watchdog proof is absent",
            owner_id="paul-park",
            actor_id="tessa-cole",
        )
        assert finding.disposition is None
        assert [row.finding_key for row in kb.list_orphan_findings(conn)] == [finding.finding_key]

        kb.disposition_finding(
            conn,
            finding_key=finding.finding_key,
            disposition="accepted_queued",
            linear_issue_id="HEL-3112",
            actor_id="paul-park",
        )
        assert [row.finding_key for row in kb.list_orphan_findings(conn)] == [finding.finding_key]

        kb.verify_finding(conn, finding.finding_key, actor_id="paul-park")
        assert kb.list_orphan_findings(conn) == []
        events = _finding_events(conn, board)

    assert [event["event_type"] for event in events] == [
        "finding_opened",
        "finding_dispositioned",
        "finding_queued",
        "finding_verified",
    ]
    assert {event["work_intent_id"] for event in events} == {board}
    assert {event["finding_key"] for event in events} == {finding.finding_key}
    assert all(event["schema_version"] == 1 for event in events)
    assert all(event["policy_version"] == "HEL-3112-v1" for event in events)


def test_disposition_is_replay_safe_and_conflicting_second_disposition_fails(board):
    with kb.connect() as conn:
        kb.open_finding(
            conn,
            finding_key="readout:built-not-wired",
            work_intent_id=board,
            source_system="readout",
            source_ref="READOUT#built-not-wired",
            title="Capability has no caller",
            owner_id="arturo-gallo",
            actor_id="tessa-cole",
        )
        first = kb.disposition_finding(
            conn,
            finding_key="readout:built-not-wired",
            disposition="accepted_existing",
            linear_issue_id="HEL-2957",
            actor_id="paul-park",
        )
        replay = kb.disposition_finding(
            conn,
            finding_key="readout:built-not-wired",
            disposition="accepted_existing",
            linear_issue_id="HEL-2957",
            actor_id="paul-park",
        )
        with pytest.raises(ValueError, match="already has disposition"):
            kb.disposition_finding(
                conn,
                finding_key="readout:built-not-wired",
                disposition="deferred",
                decision_record_ref="decision:daylight-lane",
                actor_id="paul-park",
            )
        events = _finding_events(conn, board)

    assert replay == first
    assert [event["event_type"] for event in events].count("finding_dispositioned") == 1
    assert [event["event_type"] for event in events].count("finding_queued") == 1


@pytest.mark.parametrize("disposition", ["rejected", "deferred", "not_applicable"])
def test_nonaccepted_dispositions_require_explicit_decision_record(board, disposition):
    key = f"trc:{disposition}"
    with kb.connect() as conn:
        kb.open_finding(
            conn,
            finding_key=key,
            work_intent_id=board,
            source_system="trc",
            source_ref=f"TRC#{disposition}",
            title=f"Finding {disposition}",
            owner_id="paul-park",
            actor_id="tessa-cole",
        )
        with pytest.raises(ValueError, match="decision_record_ref"):
            kb.disposition_finding(
                conn,
                finding_key=key,
                disposition=disposition,
                actor_id="paul-park",
            )
        kb.disposition_finding(
            conn,
            finding_key=key,
            disposition=disposition,
            decision_record_ref=f"decision:{disposition}",
            actor_id="paul-park",
        )
        kb.verify_finding(conn, key, actor_id="paul-park")
        assert kb.list_orphan_findings(conn) == []


def test_accepted_disposition_without_linear_issue_fails_closed(board):
    with kb.connect() as conn:
        kb.open_finding(
            conn,
            finding_key="trc:missing-issue",
            work_intent_id=board,
            source_system="trc",
            source_ref="TRC#missing-issue",
            title="Accepted finding",
            owner_id="paul-park",
            actor_id="tessa-cole",
        )
        with pytest.raises(ValueError, match="linear_issue_id"):
            kb.disposition_finding(
                conn,
                finding_key="trc:missing-issue",
                disposition="accepted_queued",
                actor_id="paul-park",
            )


def test_finding_events_reject_local_path_evidence(board, tmp_path):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="local absolute path"):
            kb.open_finding(
                conn,
                finding_key="trc:unsafe-source",
                work_intent_id=board,
                source_system="trc",
                source_ref=str(tmp_path / "raw-readout.md"),
                title="Unsafe source",
                owner_id="paul-park",
                actor_id="tessa-cole",
            )


def test_finding_key_open_replay_is_idempotent_but_conflicting_source_fails(board):
    kwargs = {
        "finding_key": "trc:stable-key",
        "work_intent_id": board,
        "source_system": "trc",
        "source_ref": "TRC#stable-key",
        "title": "Stable finding",
        "owner_id": "paul-park",
        "actor_id": "tessa-cole",
    }
    with kb.connect() as conn:
        first = kb.open_finding(conn, **kwargs)
        replay = kb.open_finding(conn, **kwargs)
        with pytest.raises(ValueError, match="already exists with different evidence"):
            kb.open_finding(conn, **{**kwargs, "source_ref": "TRC#changed"})
        events = _finding_events(conn, board)

    assert replay == first
    assert [event["event_type"] for event in events] == ["finding_opened"]


def test_direct_sql_cannot_create_invalid_disposition_shape(board):
    with kb.connect() as conn:
        with pytest.raises(Exception, match="CHECK constraint failed"):
            conn.execute(
                """
                INSERT INTO findings (
                    finding_key, work_intent_id, source_system, source_ref,
                    title, owner_id, disposition, opened_at, dispositioned_at
                ) VALUES (?, ?, 'trc', 'TRC#invalid', 'Invalid', 'paul-park',
                          'accepted_queued', 1, 1)
                """,
                ("trc:invalid-shape", board),
            )


def test_existing_board_migrates_findings_table_without_losing_tasks(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    kb.init_db()
    with kb.connect() as conn:
        legacy_id = kb.create_task(conn, title="Existing task")
        conn.execute("DROP TABLE findings")
        conn.commit()
    kb._INITIALIZED_PATHS.clear()

    kb.init_db()
    with kb.connect() as migrated:
        tables = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        task = kb.get_task(migrated, legacy_id)

    assert "findings" in tables
    assert task is not None and task.title == "Existing task"


def test_cli_check_is_machine_readable_and_fail_closed(board):
    opened = kc.run_slash(
        "findings open trc:cli-gap "
        f"--work-intent {board} --source trc --source-ref TRC#cli-gap "
        "--title 'CLI gap' --owner paul-park --actor tessa-cole --json"
    )
    assert json.loads(opened)["finding_key"] == "trc:cli-gap"

    orphaned = json.loads(kc.run_slash("findings check --json"))
    assert orphaned["ok"] is False
    assert orphaned["orphan_count"] == 1

    kc.run_slash(
        "findings disposition trc:cli-gap accepted_queued "
        "--linear-issue HEL-3112 --actor paul-park"
    )
    kc.run_slash("findings verify trc:cli-gap --actor paul-park")
    checked = json.loads(kc.run_slash("findings check --json"))
    assert checked == {"ok": True, "orphan_count": 0, "orphans": []}
