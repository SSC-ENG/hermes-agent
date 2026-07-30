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


_LINEAR_UUID = "9a48515e-6535-4393-81e4-6fd6c7dc6023"


def _linear_transport(issue_id: str, uuid: str = _LINEAR_UUID):
    """Hermetic governed-adapter transport attesting one existing issue."""

    def transport(query, variables):
        if variables.get("id") == issue_id:
            return {
                "data": {
                    "issue": {
                        "id": uuid,
                        "identifier": issue_id,
                        "state": {"name": "In Review"},
                    }
                }
            }
        return {"data": {"issue": None}}

    return transport


def _linear_evidence(issue_id: str = "HEL-3112") -> kb.VerifiedEvidence:
    return kb.fetch_linear_issue_evidence(
        issue_id, transport=_linear_transport(issue_id)
    )


def _decision_evidence(conn, ref: str, actor: str = "paul-park") -> kb.VerifiedEvidence:
    kb.record_finding_decision(
        conn, decision_ref=ref, actor_id=actor, rationale="explicit governed decision"
    )
    return kb.fetch_decision_record_evidence(conn, ref)


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

        verified = kb.verify_finding(
            conn,
            finding.finding_key,
            actor_id="paul-park",
            evidence=_linear_evidence("HEL-3112"),
        )
        assert verified.verification_source == "linear_graphql"
        assert verified.verification_evidence_ref == f"linear:HEL-3112:{_LINEAR_UUID}"
        assert verified.verification_observed_state == "In Review"
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
        kb.verify_finding(
            conn,
            key,
            actor_id="paul-park",
            evidence=_decision_evidence(conn, f"decision:{disposition}"),
        )
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
        conn.execute("DROP TRIGGER IF EXISTS trg_telemetry_finding_to_ledger")
        conn.execute("DROP TRIGGER IF EXISTS trg_finding_disposition_bound_immutable")
        conn.execute("DROP TRIGGER IF EXISTS trg_finding_verification_immutable")
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


def test_cli_check_is_machine_readable_and_fail_closed(board, monkeypatch):
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
    # CLI verify consults the governed Linear adapter; pin the transport so
    # the test stays hermetic while exercising the same call path. Bind the
    # real adapter BEFORE patching: the patched callable must not re-enter
    # the (patched) module attribute or it recurses forever.
    real_fetch = kb.fetch_linear_issue_evidence
    monkeypatch.setattr(
        kb,
        "fetch_linear_issue_evidence",
        lambda issue_id, **_: real_fetch(
            issue_id, transport=_linear_transport(issue_id)
        ),
    )
    verified = json.loads(kc.run_slash("findings verify trc:cli-gap --actor paul-park --json"))
    assert verified["verification_source"] == "linear_graphql"
    assert verified["verification_observed_state"] == "In Review"
    checked = json.loads(kc.run_slash("findings check --json"))
    assert checked == {"ok": True, "orphan_count": 0, "orphans": []}


def test_cli_decision_record_path_verifies_without_network(board):
    kc.run_slash(
        "findings open trc:cli-decision "
        f"--work-intent {board} --source trc --source-ref TRC#cli-decision "
        "--title 'CLI decision' --owner paul-park --actor tessa-cole"
    )
    kc.run_slash(
        "findings disposition trc:cli-decision not_applicable "
        "--decision-record decision:cli-na --actor paul-park"
    )
    recorded = json.loads(kc.run_slash(
        "findings decision decision:cli-na --actor paul-park "
        "--rationale 'covered by HEL-2957' --json"
    ))
    assert recorded == {"decision_ref": "decision:cli-na"}
    verified = json.loads(
        kc.run_slash("findings verify trc:cli-decision --actor paul-park --json")
    )
    assert verified["verification_source"] == "decision_record"
    checked = json.loads(kc.run_slash("findings check --json"))
    assert checked["ok"] is True


def test_telemetry_review_finding_is_promoted_into_orphan_gate(board):
    with kb.connect() as conn:
        conn.execute(
            """
            INSERT INTO telemetry_review_findings (
                finding_key, rule_id, board_slug, subject_json,
                first_observed_at, last_observed_at, severity,
                evidence_state, state, report_json
            ) VALUES (?, ?, ?, ?, 1, 1, 'HIGH', 'MEASURED', 'NEW', ?)
            """,
            (
                "telemetry:missing-queue",
                "INTAKE.NOT_QUEUED",
                "default",
                json.dumps({"task_ids": [board]}),
                json.dumps({"title": "Telemetry finding", "owner": "paul-park"}),
            ),
        )
        conn.commit()
        orphans = kb.list_orphan_findings(conn)
        promoted = kb.get_finding(conn, "telemetry:missing-queue")

    assert [row.finding_key for row in orphans] == ["telemetry:missing-queue"]
    # The promotion trigger writes the canonical ledger row directly, so the
    # orphan is dispositionable (not merely synthesized from the source).
    assert promoted is not None and promoted.work_intent_id == board


# ---------------------------------------------------------------------------
# AGA P1 #2 — typed governed-adapter evidence
# ---------------------------------------------------------------------------


def _dispositioned(conn, board, key, issue="HEL-3112"):
    kb.open_finding(
        conn,
        finding_key=key,
        work_intent_id=board,
        source_system="trc",
        source_ref=f"TRC#{key}",
        title=key,
        owner_id="paul-park",
        actor_id="tessa-cole",
    )
    kb.disposition_finding(
        conn,
        finding_key=key,
        disposition="accepted_queued",
        linear_issue_id=issue,
        actor_id="paul-park",
    )


def test_verify_rejects_free_form_string_evidence(board):
    with kb.connect() as conn:
        _dispositioned(conn, board, "trc:free-form")
        with pytest.raises(ValueError, match="typed VerifiedEvidence"):
            kb.verify_finding(
                conn,
                "trc:free-form",
                actor_id="paul-park",
                evidence="linear:HEL-3112:garbage",  # type: ignore[arg-type]
            )


def test_verify_rejects_unknown_verification_source(board):
    with kb.connect() as conn:
        _dispositioned(conn, board, "trc:unknown-source")
        forged = kb.VerifiedEvidence(
            source="not-a-governed-adapter",
            object_type="linear_issue",
            canonical_id="HEL-3112",
            evidence_ref=f"linear:HEL-3112:{_LINEAR_UUID}",
            observed_state="In Review",
            observed_at=1,
        )
        with pytest.raises(ValueError, match="unknown verification source"):
            kb.verify_finding(
                conn, "trc:unknown-source", actor_id="paul-park", evidence=forged
            )


def test_linear_adapter_rejects_syntactically_valid_but_nonexistent_issue():
    # HEL-9999 is well-formed but the (injected) Linear lookup finds nothing.
    with pytest.raises(ValueError, match="does not exist"):
        kb.fetch_linear_issue_evidence(
            "HEL-9999", transport=lambda _q, _v: {"data": {"issue": None}}
        )


def test_linear_adapter_rejects_malformed_issue_id():
    with pytest.raises(ValueError, match="invalid Linear issue id"):
        kb.fetch_linear_issue_evidence(
            "not-a-linear-id", transport=lambda _q, _v: {"data": {"issue": {}}}
        )


def test_linear_adapter_rejects_identifier_mismatch():
    transport = lambda _q, _v: {  # noqa: E731
        "data": {"issue": {"id": _LINEAR_UUID, "identifier": "HEL-1", "state": {"name": "Done"}}}
    }
    with pytest.raises(ValueError, match="for requested issue"):
        kb.fetch_linear_issue_evidence("HEL-3112", transport=transport)


def test_verify_rejects_evidence_for_a_different_issue(board):
    with kb.connect() as conn:
        _dispositioned(conn, board, "trc:cross-issue", issue="HEL-3112")
        with pytest.raises(ValueError, match="must attest Linear issue HEL-3112"):
            kb.verify_finding(
                conn,
                "trc:cross-issue",
                actor_id="paul-park",
                evidence=_linear_evidence("HEL-9999"),
            )


def test_decision_adapter_requires_recorded_decision(board):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="does not exist"):
            kb.fetch_decision_record_evidence(conn, "decision:never-recorded")


def test_accepted_disposition_rejects_decision_record_evidence(board):
    with kb.connect() as conn:
        _dispositioned(conn, board, "trc:wrong-adapter")
        evidence = _decision_evidence(conn, "decision:wrong-adapter")
        with pytest.raises(ValueError, match="require linear_graphql evidence"):
            kb.verify_finding(
                conn, "trc:wrong-adapter", actor_id="paul-park", evidence=evidence
            )


def test_verify_rejects_malformed_external_evidence(board):
    with kb.connect() as conn:
        finding = kb.open_finding(
            conn,
            finding_key="trc:bad-linear",
            work_intent_id=board,
            source_system="trc",
            source_ref="TRC#bad-linear",
            title="Bad Linear reference",
            owner_id="paul-park",
            actor_id="tessa-cole",
        )
        kb.disposition_finding(
            conn,
            finding_key=finding.finding_key,
            disposition="accepted_queued",
            linear_issue_id="not-a-linear-id",
            actor_id="paul-park",
        )
        with pytest.raises(ValueError, match="invalid Linear issue"):
            kb.verify_finding(
                conn,
                finding.finding_key,
                actor_id="paul-park",
                evidence=kb.VerifiedEvidence(
                    source="linear_graphql",
                    object_type="linear_issue",
                    canonical_id="not-a-linear-id",
                    evidence_ref="linear:not-a-linear-id:fake",
                    observed_state="In Review",
                    observed_at=1,
                ),
            )


# ---------------------------------------------------------------------------
# AGA P1 #3 — every bound evidence field is DB-immutable
# ---------------------------------------------------------------------------


def _verified(conn, board, key="trc:immutable"):
    _dispositioned(conn, board, key)
    kb.verify_finding(
        conn, key, actor_id="paul-park", evidence=_linear_evidence("HEL-3112")
    )
    return key


def test_direct_sql_cannot_change_existing_disposition(board):
    with kb.connect() as conn:
        kb.open_finding(
            conn,
            finding_key="trc:immutable",
            work_intent_id=board,
            source_system="trc",
            source_ref="TRC#immutable",
            title="Immutable disposition",
            owner_id="paul-park",
            actor_id="tessa-cole",
        )
        kb.disposition_finding(
            conn,
            finding_key="trc:immutable",
            disposition="accepted_existing",
            linear_issue_id="HEL-3112",
            actor_id="paul-park",
        )
        with pytest.raises(Exception, match="disposition is immutable"):
            conn.execute(
                "UPDATE findings SET disposition = 'deferred', "
                "linear_issue_id = NULL, decision_record_ref = 'decision:x' "
                "WHERE finding_key = 'trc:immutable'"
            )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE findings SET disposition = 'accepted_existing' WHERE finding_key = ?",
        "UPDATE findings SET linear_issue_id = 'HEL-9999' WHERE finding_key = ?",
        "UPDATE findings SET dispositioned_at = 1 WHERE finding_key = ?",
    ],
    ids=["disposition", "linear_issue_id", "dispositioned_at"],
)
def test_direct_sql_cannot_mutate_bound_disposition_fields(board, statement):
    with kb.connect() as conn:
        _dispositioned(conn, board, "trc:bound")
        with pytest.raises(Exception, match="disposition is immutable"):
            conn.execute(statement, ("trc:bound",))


def test_direct_sql_cannot_retarget_decision_record_ref(board):
    with kb.connect() as conn:
        kb.open_finding(
            conn,
            finding_key="trc:bound-decision",
            work_intent_id=board,
            source_system="trc",
            source_ref="TRC#bound-decision",
            title="Bound decision",
            owner_id="paul-park",
            actor_id="tessa-cole",
        )
        kb.disposition_finding(
            conn,
            finding_key="trc:bound-decision",
            disposition="deferred",
            decision_record_ref="decision:original",
            actor_id="paul-park",
        )
        with pytest.raises(Exception, match="disposition is immutable"):
            conn.execute(
                "UPDATE findings SET decision_record_ref = 'decision:retargeted' "
                "WHERE finding_key = ?",
                ("trc:bound-decision",),
            )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE findings SET verified_at = 1 WHERE finding_key = ?",
        "UPDATE findings SET verification_evidence_ref = 'linear:HEL-9999:x' WHERE finding_key = ?",
        "UPDATE findings SET verification_source = 'decision_record' WHERE finding_key = ?",
        "UPDATE findings SET verification_observed_state = 'Cancelled' WHERE finding_key = ?",
    ],
    ids=[
        "verified_at",
        "verification_evidence_ref",
        "verification_source",
        "verification_observed_state",
    ],
)
def test_direct_sql_cannot_mutate_verification_fields(board, statement):
    with kb.connect() as conn:
        key = _verified(conn, board)
        with pytest.raises(Exception, match="verification is immutable"):
            conn.execute(statement, (key,))


def test_governed_updates_from_null_still_pass_immutability_triggers(board):
    # disposition_finding / verify_finding set the bound fields FROM NULL;
    # the triggers must exempt that first governed write.
    with kb.connect() as conn:
        key = _verified(conn, board, key="trc:from-null")
        finding = kb.get_finding(conn, key)
    assert finding is not None
    assert finding.disposition == "accepted_queued"
    assert finding.verified_at is not None


# ---------------------------------------------------------------------------
# AGA P1 #4 — purge/retention with retained telemetry evidence
# ---------------------------------------------------------------------------


def _insert_telemetry_source(conn, key, task_id):
    conn.execute(
        """
        INSERT INTO telemetry_review_findings (
            finding_key, rule_id, board_slug, subject_json,
            first_observed_at, last_observed_at, severity,
            evidence_state, state, report_json
        ) VALUES (?, 'INTAKE.NOT_QUEUED', 'default', ?, 1, 1, 'HIGH',
                  'MEASURED', 'NEW', ?)
        """,
        (
            key,
            json.dumps({"task_ids": [task_id]}),
            json.dumps({"title": "Telemetry finding", "owner": "paul-park"}),
        ),
    )


def test_archived_task_with_finding_can_be_purged(board):
    with kb.connect() as conn:
        kb.open_finding(
            conn,
            finding_key="trc:purge",
            work_intent_id=board,
            source_system="trc",
            source_ref="TRC#purge",
            title="Purge-safe finding",
            owner_id="paul-park",
            actor_id="tessa-cole",
        )
        assert kb.archive_task(conn, board)
        assert kb.delete_archived_task(conn, board)
        assert kb.get_task(conn, board) is None
        assert kb.get_finding(conn, "trc:purge") is None


def test_hard_delete_task_with_finding_cleans_ledger(board):
    with kb.connect() as conn:
        kb.open_finding(
            conn,
            finding_key="trc:hard-delete",
            work_intent_id=board,
            source_system="trc",
            source_ref="TRC#hard-delete",
            title="Hard-delete-safe finding",
            owner_id="paul-park",
            actor_id="tessa-cole",
        )
        assert kb.delete_task(conn, board)
        assert kb.get_task(conn, board) is None
        assert kb.get_finding(conn, "trc:hard-delete") is None


def test_purge_with_retained_telemetry_source_rehomes_finding(board):
    key = "telemetry:purge-retained"
    with kb.connect() as conn:
        _insert_telemetry_source(conn, key, board)  # trigger promotes
        conn.commit()
        assert kb.get_finding(conn, key) is not None
        assert kb.archive_task(conn, board)
        assert kb.delete_archived_task(conn, board)

        # Source retained, ledger row rehomed — never an undispositionable orphan.
        source_count = conn.execute(
            "SELECT COUNT(*) FROM telemetry_review_findings WHERE finding_key = ?",
            (key,),
        ).fetchone()[0]
        assert source_count == 1
        rehomed = kb.get_finding(conn, key)
        assert rehomed is not None
        assert rehomed.work_intent_id == kb.FINDING_RESCUE_TASK_ID

        # Still dispositionable through the governed path.
        kb.disposition_finding(
            conn,
            finding_key=key,
            disposition="accepted_queued",
            linear_issue_id="HEL-3112",
            actor_id="paul-park",
        )
        kb.verify_finding(
            conn, key, actor_id="paul-park", evidence=_linear_evidence("HEL-3112")
        )
        assert kb.list_orphan_findings(conn) == []

        events = _finding_events(conn, kb.FINDING_RESCUE_TASK_ID)
    rehome_events = [e for e in events if e["event_type"] == "finding_rehomed"]
    assert len(rehome_events) == 1
    assert rehome_events[0]["from_state"] == board
    assert rehome_events[0]["to_state"] == kb.FINDING_RESCUE_TASK_ID
    assert rehome_events[0]["reason_code"] == "task_purged_source_retained"


def test_hard_delete_with_retained_telemetry_source_rehomes_finding(board):
    key = "telemetry:delete-retained"
    with kb.connect() as conn:
        _insert_telemetry_source(conn, key, board)
        conn.commit()
        assert kb.delete_task(conn, board)
        rehomed = kb.get_finding(conn, key)
        assert rehomed is not None
        assert rehomed.work_intent_id == kb.FINDING_RESCUE_TASK_ID
        # The retained orphan surfaces through the gate until dispositioned.
        assert [f.finding_key for f in kb.list_orphan_findings(conn)] == [key]


def test_finding_rescue_container_cannot_be_deleted(board):
    key = "telemetry:rescue-durable"
    with kb.connect() as conn:
        _insert_telemetry_source(conn, key, board)
        conn.commit()
        assert kb.delete_task(conn, board)
        assert kb.get_finding(conn, key) is not None
        assert not kb.delete_task(conn, kb.FINDING_RESCUE_TASK_ID)
        assert not kb.delete_archived_task(conn, kb.FINDING_RESCUE_TASK_ID)
        assert kb.get_finding(conn, key) is not None


# ---------------------------------------------------------------------------
# AGA migration gap — legacy backfill and trigger upgrade
# ---------------------------------------------------------------------------


def test_legacy_board_backfill_promotes_and_rehomes_telemetry_findings(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _profile: True)

    kb.init_db()
    with kb.connect() as conn:
        live_task = kb.create_task(conn, title="Live task", assignee="paul-park")
        # Simulate a legacy board: narrow trigger bodies, no attestation
        # column, and telemetry rows that never reached the ledger.
        conn.execute("DROP TRIGGER trg_telemetry_finding_to_ledger")
        conn.execute("DROP TRIGGER trg_finding_disposition_bound_immutable")
        conn.execute("DROP TRIGGER trg_finding_verification_immutable")
        conn.execute(
            "ALTER TABLE findings DROP COLUMN verification_observed_state"
        )
        conn.execute(
            """
            CREATE TRIGGER trg_finding_disposition_immutable
            BEFORE UPDATE OF disposition ON findings
            WHEN OLD.disposition IS NOT NULL
             AND NEW.disposition IS NOT OLD.disposition
            BEGIN
                SELECT RAISE(ABORT, 'disposition is immutable');
            END
            """
        )
        _insert_telemetry_source(conn, "telemetry:legacy-live", live_task)
        _insert_telemetry_source(conn, "telemetry:legacy-gone", "t_gone_task")
        conn.commit()
        assert kb.get_finding(conn, "telemetry:legacy-live") is None

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect() as conn:
        # Backfill: row with a live task joins the ledger against it.
        promoted = kb.get_finding(conn, "telemetry:legacy-live")
        assert promoted is not None
        assert promoted.work_intent_id == live_task
        # Row whose task is absent is rehomed to the rescue container.
        rehomed = kb.get_finding(conn, "telemetry:legacy-gone")
        assert rehomed is not None
        assert rehomed.work_intent_id == kb.FINDING_RESCUE_TASK_ID

        # The legacy narrow trigger was replaced by the current bodies.
        triggers = {
            row["name"]: row["sql"]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert "trg_finding_disposition_immutable" not in triggers
        assert "NOT EXISTS" in triggers["trg_telemetry_finding_to_ledger"]
        assert "linear_issue_id" in triggers["trg_finding_disposition_bound_immutable"]
        assert "verification_observed_state" in triggers["trg_finding_verification_immutable"]

        # The attestation column was re-added and the full governed
        # lifecycle works on the migrated board.
        kb.disposition_finding(
            conn,
            finding_key="telemetry:legacy-live",
            disposition="accepted_queued",
            linear_issue_id="HEL-3112",
            actor_id="paul-park",
        )
        verified = kb.verify_finding(
            conn,
            "telemetry:legacy-live",
            actor_id="paul-park",
            evidence=_linear_evidence("HEL-3112"),
        )
        assert verified.verification_observed_state == "In Review"
        with pytest.raises(Exception, match="disposition is immutable"):
            conn.execute(
                "UPDATE findings SET linear_issue_id = 'HEL-9999' "
                "WHERE finding_key = 'telemetry:legacy-live'"
            )

        # Backfill is idempotent across re-inits.
        count_before = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect() as conn:
        count_after = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    assert count_after == count_before
