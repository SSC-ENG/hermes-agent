"""BEL closed-loop disposition gate + extract helpers (t_0cef6b6e)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_extract_bel_disposition_from_metadata():
    assert (
        kb.extract_bel_disposition(metadata={"disposition": "TRUE_BLOCK"})
        == "TRUE_BLOCK"
    )
    assert (
        kb.extract_bel_disposition(metadata={"disposition": "already-unblocked"})
        == "UNBLOCKED"
    )
    assert (
        kb.extract_bel_disposition(
            metadata={"disposition": "LINKED_FIX_PENDING"}
        )
        == "LINKED_FIX_PENDING"
    )


def test_extract_bel_disposition_from_prose_precedence():
    # TRUE_BLOCK outranks UNBLOCKED when both appear
    text = "Confirmed TRUE_BLOCK; earlier attempt said UNBLOCKED by mistake"
    assert kb.extract_bel_disposition(text) == "TRUE_BLOCK"
    assert kb.extract_bel_disposition("Disposition: LINKED_FIX_PENDING") == (
        "LINKED_FIX_PENDING"
    )
    assert kb.extract_bel_disposition("ALREADY-UNBLOCKED + VERIFY-RESPAWN") == (
        "UNBLOCKED"
    )
    assert kb.extract_bel_disposition("closed with no tag") is None


def test_is_bel_disposition_card():
    assert kb.is_bel_disposition_card(
        "BEL-ESCALATION: t_abc [infra] something"
    )
    assert kb.is_bel_disposition_card(
        "BEL-MISSED-HANDOFF: t_abc still blocked"
    )
    assert not kb.is_bel_disposition_card("ordinary task")
    assert not kb.is_bel_disposition_card(None)


def test_complete_bel_without_disposition_raises(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="BEL-ESCALATION: t_deadbeef [test] parent still blocked",
            assignee="rhea-ramos",
            body="test",
        )
        with pytest.raises(kb.MissingDispositionError):
            kb.complete_task(conn, tid, summary="looked at it, all good")
        # Still not done
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status != "done"
        # Audit event landed
        ev = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id=? AND kind='completion_blocked_disposition'",
            (tid,),
        ).fetchone()
        assert ev is not None
        payload = json.loads(ev["payload"])
        assert payload["reason"] == "bel_disposition_required"


def test_complete_bel_with_summary_tag_succeeds(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="BEL-ESCALATION: t_deadbeef [test] parent cleared",
            assignee="rhea-ramos",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="UNBLOCKED: parent left blocked, live pid verified",
        )
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "done"


def test_complete_bel_with_metadata_tag_succeeds(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="BEL-MISSED-HANDOFF: t_deadbeef still blocked after esc",
            assignee="cornelius-corey-vale",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="park stands",
            metadata={"disposition": "TRUE_BLOCK"},
        )
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "done"


def test_complete_non_bel_without_tag_still_works(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="ordinary work item", assignee="rhea-ramos"
        )
        assert kb.complete_task(conn, tid, summary="done without disposition")
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "done"
