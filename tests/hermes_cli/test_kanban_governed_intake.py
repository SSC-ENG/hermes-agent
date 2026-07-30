"""Tests for the governed intake envelope and typed scope handoff."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_intake import build_envelope, parse_envelope, render_envelope


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_envelope_round_trip_and_digest_is_deterministic(kanban_home):
    first = build_envelope(
        source="haa",
        items=["linear:HEL-1", "notes"],
        notes="internal",
        attachment_refs=["/tmp/spec.pdf"],
        tenant_domain="engineering",
        sensitivity="confidential",
    )
    second = build_envelope(
        source="haa",
        items=["linear:HEL-1", "notes"],
        notes="internal",
        attachment_refs=["/tmp/spec.pdf"],
        tenant_domain="engineering",
        sensitivity="confidential",
    )
    assert first.content_digest == second.content_digest
    assert parse_envelope(render_envelope(first)) == first


def test_governed_intake_create_is_idempotent(kanban_home):
    envelope = build_envelope(
        source="webhook:inbox",
        items=["first", "second"],
        tenant_domain="engineering",
    )
    with kb.connect_closing() as conn:
        first = kb.create_governed_intake_task(
            conn,
            title="raw intake",
            body=render_envelope(envelope),
            tenant=envelope.tenant_domain,
            content_digest=envelope.content_digest,
            idempotency_key=envelope.idempotency_key,
            created_by="haa",
        )
        second = kb.create_governed_intake_task(
            conn,
            title="raw intake",
            body=render_envelope(envelope),
            tenant=envelope.tenant_domain,
            content_digest=envelope.content_digest,
            idempotency_key=envelope.idempotency_key,
            created_by="haa",
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
            (envelope.idempotency_key,),
        ).fetchone()[0]
    assert first[0] == second[0]
    assert first[1] is True
    assert second[1] is False
    assert count == 1


def test_governed_intake_deduplicates_same_digest_with_different_key(kanban_home):
    envelope = build_envelope(
        source="webhook:inbox",
        items=["same content"],
        tenant_domain="engineering",
        idempotency_key="producer-key-1",
    )
    with kb.connect_closing() as conn:
        first = kb.create_governed_intake_task(
            conn,
            title="raw intake",
            body=render_envelope(envelope),
            tenant=envelope.tenant_domain,
            content_digest=envelope.content_digest,
            idempotency_key=envelope.idempotency_key,
            created_by="haa",
        )
        second = kb.create_governed_intake_task(
            conn,
            title="raw intake",
            body=render_envelope(envelope),
            tenant=envelope.tenant_domain,
            content_digest=envelope.content_digest,
            idempotency_key="producer-key-2",
            created_by="haa",
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status != 'archived' AND body LIKE ?",
            (f"%{envelope.content_digest}%",),
        ).fetchone()[0]
    assert first[0] == second[0]
    assert first[1] is True
    assert second[1] is False
    assert count == 1


def test_scope_handoff_emits_typed_events(kanban_home):
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn,
            title="raw intake",
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="paul-park",
            children=[
                {
                    "title": "scope intake",
                    "assignee": "paul-park",
                    "parents": [],
                    "domain": "program-management",
                    "ppma_scope_gate": True,
                },
                {
                    "title": "build intake",
                    "assignee": "cole-espinoza",
                    "parents": [0],
                    "domain": "engineering",
                },
            ],
        )
        gate, execution = child_ids
        scope = kb.record_scope_handoff(
            conn,
            gate,
            author="paul-park",
            body="LINEAR_SCOPE: parent=HEL-3107 subissues=[{key:HEL-3115, cptc:3}]",
        )
        events = kb.list_events(conn, gate)
        handoff_events = [
            event for event in kb.list_events(conn, execution)
            if event.kind == "handoff_emitted"
        ]
    assert scope == {"parent": "HEL-3107", "subissues": [{"key": "HEL-3115", "cptc": 3}]}
    assert {event.kind for event in events} >= {"commented", "scope_recorded"}
    assert len(handoff_events) == 1
    assert handoff_events[0].payload["handoff_id"] == f"{gate}:{execution}:linear_scope"
    assert handoff_events[0].payload["from_owner"] == "paul-park"
    assert handoff_events[0].payload["to_owner"] == "cole-espinoza"


def test_ppma_gate_cannot_complete_or_archive_before_scope(kanban_home):
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="raw intake", triage=True)
        gate, execution = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="paul-park",
            children=[
                {
                    "title": "scope",
                    "assignee": "paul-park",
                    "parents": [],
                    "domain": "program-management",
                    "ppma_scope_gate": True,
                },
                {
                    "title": "build",
                    "assignee": "cole-espinoza",
                    "parents": [0],
                    "domain": "engineering",
                },
            ],
        )
        assert kb.complete_task(conn, gate, result="attempted bypass") is False
        assert kb.archive_task(conn, gate) is False
        assert kb.delete_task(conn, gate) is False
        assert kb.get_task(conn, gate).status == "ready"
        assert kb.get_task(conn, execution).status == "todo"
        assert "completion_blocked_scope" in {
            event.kind for event in kb.list_events(conn, gate)
        }

        kb.add_governed_comment(
            conn,
            gate,
            author="paul-park",
            body=(
                "Scope complete.\n"
                "LINEAR_SCOPE: parent=HEL-3107 "
                "subissues=[{key:HEL-3115, cptc:3}]"
            ),
        )
        assert kb.complete_task(conn, gate, result="scoped") is True
        assert kb.get_task(conn, execution).status == "ready"


def test_cli_comment_records_linear_scope_for_ppma_gate(kanban_home):
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="raw", triage=True)
        gate, execution = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="paul-park",
            children=[
                {
                    "title": "scope",
                    "assignee": "paul-park",
                    "parents": [],
                    "domain": "program-management",
                    "ppma_scope_gate": True,
                },
                {
                    "title": "build",
                    "assignee": "cole-espinoza",
                    "parents": [0],
                    "domain": "engineering",
                },
            ],
        )
    output = kc.run_slash(
        f"comment {gate} 'LINEAR_SCOPE: parent=HEL-3107 "
        "subissues=[{key:HEL-3115, cptc:3}]' --author paul-park"
    )
    assert "Comment added" in output
    with kb.connect_closing() as conn:
        assert "scope_recorded" in {
            event.kind for event in kb.list_events(conn, gate)
        }
        assert "handoff_emitted" in {
            event.kind for event in kb.list_events(conn, execution)
        }
