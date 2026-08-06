"""Source gate for block_task: review-required+verdict and bandwidth language.

Forward-facing recurrence prevention (t_25dd3612 / t_02be160f):

* Rule A — reason starts with ``review-required`` AND a reviewer verdict is
  already posted → reject ``needs_input`` / ``capability``; require
  ``kind=dependency`` with a wired parent link.
* Rule B — bare block (kind is None) whose reason contains bandwidth /
  worker-cap language → reject; require ``--kind dependency``.

Isolation: pop HERMES_KANBAN_DB / HERMES_KANBAN_BOARD before setting a temp
HERMES_HOME so dispatcher-injected pins cannot point tests at the live board.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # CRITICAL: dispatcher injects HERMES_KANBAN_DB into worker env. Pop first.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    os.environ.pop("HERMES_KANBAN_DB", None)
    os.environ.pop("HERMES_KANBAN_BOARD", None)
    os.environ.pop("HERMES_KANBAN_HOME", None)

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    resolved = kb.kanban_db_path()
    assert str(resolved).startswith(str(tmp_path)), (
        f"probe is not isolated from the live board: {resolved} not under {tmp_path}"
    )
    kb.init_db()
    return home


def _running_task(conn, title: str = "t") -> str:
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _post_verdict(conn, task_id: str, body: str, author: str = "tessa-cole") -> None:
    kb.add_comment(conn, task_id, author=author, body=body)


# ---------------------------------------------------------------------------
# _has_reviewer_verdict unit tests
# ---------------------------------------------------------------------------


def test_has_reviewer_verdict_gateway_marker(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="verdict-marker", assignee="worker")
        assert kb._has_reviewer_verdict(conn, tid) is False
        _post_verdict(
            conn,
            tid,
            "GATEWAY-VERDICT: TRC=PASS head=abcdef12",
        )
        assert kb._has_reviewer_verdict(conn, tid) is True


def test_has_reviewer_verdict_legacy_pass_go_approved(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        for label, body in (
            ("pass", "Review complete. PASS for this head."),
            ("go", "GO — ready for merge lane."),
            ("approved", "APPROVED after technical review."),
        ):
            tid = kb.create_task(conn, title=f"verdict-{label}", assignee="worker")
            assert kb._has_reviewer_verdict(conn, tid) is False
            _post_verdict(conn, tid, body)
            assert kb._has_reviewer_verdict(conn, tid) is True, body


def test_has_reviewer_verdict_absent(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="no-verdict", assignee="worker")
        kb.add_comment(conn, tid, author="worker", body="still implementing")
        assert kb._has_reviewer_verdict(conn, tid) is False


# ---------------------------------------------------------------------------
# Rule A — review-required + verdict
# ---------------------------------------------------------------------------


def test_rule_a_rejects_needs_input_when_verdict_posted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="rr-needs-input")
        _post_verdict(conn, tid, "GATEWAY-VERDICT: TRC=PASS head=deadbeef")
        with pytest.raises(ValueError, match="dependency") as exc:
            kb.block_task(
                conn,
                tid,
                reason="review-required: PR open, CI green",
                kind="needs_input",
            )
        msg = str(exc.value).lower()
        assert "needs_input" in msg or "capability" in msg
        assert "dependency" in msg
        assert kb.get_task(conn, tid).status == "running"


def test_rule_a_rejects_capability_when_verdict_posted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="rr-capability")
        _post_verdict(conn, tid, "GATEWAY-VERDICT: AGA=PASS head=cafebabe")
        with pytest.raises(ValueError, match="dependency"):
            kb.block_task(
                conn,
                tid,
                reason="Review-Required: waiting on merge lane",
                kind="capability",
            )
        assert kb.get_task(conn, tid).status == "running"


def test_rule_a_accepts_dependency_with_wired_parent(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="reviewer-lane", assignee="rhea-ramos")
        child = _running_task(conn, title="rr-dependency-ok")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        _post_verdict(conn, child, "GATEWAY-VERDICT: TRC=PASS head=12345678")
        ok = kb.block_task(
            conn,
            child,
            reason="review-required: queued for merge after TRC PASS",
            kind="dependency",
        )
        assert ok is True
        assert kb.get_task(conn, child).status == "todo"


def test_rule_a_rejects_dependency_without_parent(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="rr-dependency-no-parent")
        _post_verdict(conn, tid, "PASS")
        with pytest.raises(ValueError, match="link") as exc:
            kb.block_task(
                conn,
                tid,
                reason="review-required: need merge parent",
                kind="dependency",
            )
        msg = str(exc.value).lower()
        assert "link" in msg
        assert kb.get_task(conn, tid).status == "running"


# ---------------------------------------------------------------------------
# Rule B — bandwidth / worker-cap bare blocks
# ---------------------------------------------------------------------------


def test_rule_b_rejects_bare_no_bandwidth(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="bw-no-bandwidth")
        with pytest.raises(ValueError, match="dependency") as exc:
            kb.block_task(conn, tid, reason="no bandwidth left on this profile")
        assert "dependency" in str(exc.value).lower()
        assert kb.get_task(conn, tid).status == "running"


def test_rule_b_rejects_bare_worker_cap(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="bw-worker-cap")
        with pytest.raises(ValueError, match="dependency"):
            kb.block_task(conn, tid, reason="worker cap reached for assignee")
        assert kb.get_task(conn, tid).status == "running"


# ---------------------------------------------------------------------------
# No regression — ordinary needs_input still works
# ---------------------------------------------------------------------------


def test_ordinary_needs_input_still_accepted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="ordinary-needs-input")
        ok = kb.block_task(
            conn,
            tid,
            reason="need product decision on copy tone",
            kind="needs_input",
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
