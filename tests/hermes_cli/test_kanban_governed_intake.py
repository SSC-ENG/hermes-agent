"""Tests for the governed intake envelope and typed scope handoff."""
from __future__ import annotations

from pathlib import Path

import pytest

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
            idempotency_key=envelope.idempotency_key,
            created_by="haa",
        )
        second = kb.create_governed_intake_task(
            conn,
            title="raw intake",
            body=render_envelope(envelope),
            tenant=envelope.tenant_domain,
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


def test_scope_handoff_emits_typed_events(kanban_home):
    with kb.connect_closing() as conn:
        gate = kb.create_task(
            conn,
            title="scope intake",
            assignee="paul-park",
            triage=False,
        )
        scope = kb.record_scope_handoff(
            conn,
            gate,
            author="paul-park",
            body="LINEAR_SCOPE: parent=HEL-3107 subissues=[{key:HEL-3115, cptc:3}]",
        )
        events = kb.list_events(conn, gate)
    assert scope == {"parent": "HEL-3107", "subissues": [{"key": "HEL-3115", "cptc": 3}]}
    assert {event.kind for event in events} >= {"scope_recorded", "handoff_emitted"}
