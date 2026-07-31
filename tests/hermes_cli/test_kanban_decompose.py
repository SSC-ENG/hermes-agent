"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp
from hermes_cli.kanban_intake import build_envelope, render_envelope


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def test_decompose_with_fanout_creates_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "scope", "body": "scope it", "assignee": "paul-park", "domain": "program-management", "parents": []},
            {"title": "research", "body": "look it up", "assignee": "researcher", "domain": "engineering", "parents": [0]},
            {"title": "build", "body": "code it", "assignee": "engineer", "domain": "engineering", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer", "paul-park"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 3

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
        c2 = kb.get_task(conn, outcome.child_ids[2])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c2.status == "todo"
    assert c0.assignee == "paul-park"
    assert c1.assignee == "researcher"
    assert c2.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"
    with kb.connect() as conn:
        routing = [
            event for event in kb.list_events(conn, tid)
            if event.kind == "routing_decided"
        ]
    assert routing[-1].payload["reason"] == "legacy_default_assignee"


def test_multi_item_envelope_overrides_fanout_false_with_ppma_gate(kanban_home):
    envelope = build_envelope(
        source="haa",
        items=["build item", "review item"],
        tenant_domain="engineering",
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="two items",
            body=render_envelope(envelope),
            tenant="engineering",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "incorrectly single",
        "title": "single",
        "body": "bypass",
        "assignee": "engineer",
    })
    patches = _patch_list_profiles(["orchestrator", "engineer", "paul-park"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 3
    gate, first, second = outcome.child_ids
    with kb.connect() as conn:
        assert kb.get_task(conn, gate).assignee == "paul-park"
        assert kb.get_task(conn, gate).status == "ready"
        assert kb.get_task(conn, first).status == "todo"
        assert kb.get_task(conn, second).status == "todo"
        assert gate in kb.parent_ids(conn, first)
        assert gate in kb.parent_ids(conn, second)
        assert kb.is_ppma_scope_gate(conn, gate)
        root_events = {event.kind for event in kb.list_events(conn, tid)}
    assert {"intake_classified", "decomposition_decided"} <= root_events


def test_malformed_envelope_returns_failure_and_records_event(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="bad envelope",
            body="INTAKE-ENVELOPE v1\n{not-json}\nEND-INTAKE-ENVELOPE",
            triage=True,
        )
    patches = _patch_list_profiles(["paul-park"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "invalid intake envelope" in outcome.reason
    with kb.connect() as conn:
        failures = [
            event for event in kb.list_events(conn, tid)
            if event.kind == "intake_validation_failed"
        ]
        assert kb.get_task(conn, tid).status == "triage"
    assert len(failures) == 1


def test_final_assignee_must_hold_required_certification(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="certified work", triage=True)
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "tasks": [
            {
                "title": "scope",
                "assignee": "paul-park",
                "domain": "program-management",
                "required_certification": "helios-agent-ppma",
                "parents": [],
            },
            {
                "title": "secure",
                "assignee": "engineer",
                "domain": "engineering",
                "required_certification": "security-cert",
                "parents": [0],
            },
        ],
    })
    patches = _patch_list_profiles(["engineer", "paul-park"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "final assignee 'paul-park' does not hold" in outcome.reason


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


def test_decompose_skips_triage_card_from_block_loop_detected(kanban_home):
    """A triage card whose most recent event is ``block_loop_detected`` was
    routed there specifically to force a human decision (see
    ``block_task``/``BLOCK_RECURRENCE_LIMIT`` in kanban_db.py — t_e2b1f62a).
    ``decompose_task`` must refuse to auto-specify/promote it, and must not
    invoke the auxiliary LLM at all for such a card.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review-required card", triage=True)
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "REJECTED-INTAKE", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client("{}") as mock_call_llm, _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "block_loop_detected" in outcome.reason
    mock_call_llm.assert_not_called()
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "triage", (
        "a block_loop_detected card must stay in triage untouched, "
        "not be re-specified or promoted"
    )



