"""Regression tests for #28712 — kanban dispatcher must not auto-promote
worker-initiated ``kanban_block`` (sticky blocks), but must keep
auto-recovering circuit-breaker blocks.

The bug: when a worker called ``kanban_block(reason="review-required:
...")`` to hand off to a human, the dispatcher's ``recompute_ready``
would promote the task back to ``ready`` on the next tick.  The fresh
worker found nothing to do (work already applied), exited cleanly, and
got recorded as a ``protocol_violation`` → ``gave_up`` → promote → loop
until manual intervention.

These tests pin down:

* Worker / operator-initiated blocks are sticky and survive
  ``recompute_ready``.
* Circuit-breaker blocks (``gave_up`` event, status flipped via
  ``_record_task_failure``) still auto-recover — the original intent
  of #40c1decb3 is preserved.
* An explicit ``kanban_unblock`` clears the sticky state.
* The full block → promote → crash → ``gave_up`` loop is broken after
  this fix: subsequent ticks leave the task blocked.

The tangentially related schema-init ordering bug originally reported
in #28712 (``init_db`` crashing on legacy DBs that pre-dated the
``session_id`` migration) is covered separately by
``test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes``,
landed via #28754 / #28781 ahead of this fix.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Worker-initiated kanban_block must be sticky
# ---------------------------------------------------------------------------


def test_worker_block_is_not_auto_promoted_by_recompute_ready(kanban_home: Path) -> None:
    """A standalone task that a worker explicitly blocks for review
    must stay blocked across an arbitrary number of dispatcher ticks.
    Before #28712's fix, ``recompute_ready`` would silently flip it
    back to ``ready`` on the very next tick."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs human review")
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid,
            reason="review-required: please verify ACL change",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Hammer the promotion code — exactly the dispatcher loop's
        # behaviour, just compressed in time.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "worker-blocked task must not auto-promote"
            assert kb.get_task(conn, tid).status == "blocked"




# ---------------------------------------------------------------------------
# Circuit-breaker blocks still auto-recover (preserve #40c1decb3 intent)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# unblock_task clears the sticky state
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Full bug-shaped loop: block → promote → crash → gave_up → next tick
# ---------------------------------------------------------------------------


def test_protocol_violation_loop_is_broken(kanban_home: Path) -> None:
    """Reproduces the exact #28712 loop and asserts the dispatcher
    leaves the task blocked instead of cycling.

    Loop shape from the issue:

    1. Worker calls ``kanban_block`` → status='blocked',
       ``task_runs.outcome='blocked'``, ``blocked`` event.
    2. (Bug) Dispatcher promotes back to ``ready``.
    3. Fresh worker exits cleanly without terminal tool call →
       ``protocol_violation`` event.
    4. ``_record_task_failure(failure_limit=1)`` → ``gave_up`` event,
       status='blocked' again.
    5. (Bug) Dispatcher promotes again → infinite loop.

    With the fix in place, step 2 never happens — the test simulates
    one would-be loop cycle by faking the crash-then-gave_up entries
    that *would* have been written and asserts the *next* tick still
    leaves the task blocked.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="loop reproducer")
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-required: human eyes please",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # First dispatcher tick — must NOT promote.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"

        # Simulate the (hypothetical) protocol_violation + gave_up
        # entries that the dispatcher would have written if the bug
        # were still present.  Even with those event rows in place,
        # the worker-initiated ``blocked`` event is the most recent
        # of the ``{blocked, unblocked}`` pair, so the sticky guard
        # still fires.
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'protocol_violation', NULL, ?)",
            (tid, now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (tid, now + 1),
        )
        conn.commit()

        # Subsequent ticks must still leave it blocked.
        for _ in range(3):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0
            assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# Schema-init recovery on legacy DBs is covered by
# tests/hermes_cli/test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes
# (landed via #28754 / #28781).  The original PR shipped a duplicate test
# here; dropped during salvage to avoid two assertions of the same contract.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# consecutive_failures must be floored at the tripping effective_limit
# (t_21f59f6d) — otherwise a same-tick re-resolution of a lower/unrelated
# effective_limit under-reads the counter and undoes the trip immediately.
# ---------------------------------------------------------------------------


def test_force_trip_floors_consecutive_failures_at_effective_limit(
    kanban_home: Path,
) -> None:
    """``_record_task_failure(force_trip=True)`` must persist
    ``max(failures, effective_limit)`` on ``consecutive_failures``, not the
    raw per-call ``failures`` count.

    Bug shape (t_21f59f6d / t_d1994b5b / t_342c4c9f): a force-trip caller
    (e.g. the protocol-violation streak) trips the breaker on a threshold
    unrelated to the unified ``consecutive_failures`` column. If the column
    was reset to 0 by a prior unblock, the very first force-trip call
    computes ``failures=1`` even though it is tripping on, say,
    ``failure_limit=3``. Storing that raw ``1`` let ``recompute_ready`` —
    invoked later in the SAME dispatch tick with its own (often lower)
    resolved ``effective_limit`` — see ``1 < effective_limit`` and
    immediately promote the task back to ``ready``, undoing the trip and
    producing an infinite blocked -> ready -> crash loop surfaced as a
    ``promoted`` event with ``trigger=parents_terminal`` and
    ``satisfied_parent_ids=[]`` on a zero-parent task.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="force-trip floor reproducer")
        kb.claim_task(conn, tid)

        # Force-trip with a higher threshold than the raw per-call
        # ``failures`` count (starts at 0 -> 1 on this first call).
        tripped = kb._record_task_failure(
            conn, tid,
            error="protocol violation streak",
            outcome="crashed",
            failure_limit=3,
            force_trip=True,
            release_claim=True,
            end_run=True,
        )
        assert tripped is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        # The critical assertion: the stored counter must be floored at
        # the effective_limit that tripped the breaker (3), not the raw
        # per-call failures count (1).
        assert task.consecutive_failures == 3, (
            "consecutive_failures must be floored at effective_limit "
            f"(3), got {task.consecutive_failures} — under-reporting "
            "lets a later lower-limit recompute_ready call re-promote "
            "the task within the same dispatch tick"
        )

        # Simulate the SAME dispatch tick re-resolving a lower/unrelated
        # default failure_limit via recompute_ready. Before the fix, the
        # under-reported counter (1) would satisfy `1 < 2` and promote
        # the task straight back to ready.
        promoted = kb.recompute_ready(conn, failure_limit=2)
        assert promoted == 0, (
            "task must NOT be re-promoted within the same tick when its "
            "floored failure count still exceeds a lower effective_limit"
        )
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# block_task's recurrence counter must not treat a dispatcher-side
# auto-promotion (parents_terminal or any other non-unblock_task exit from
# ``blocked``) the same as a genuine human/cron unblock -> worker re-block
# cycle (t_e2b1f62a).
# ---------------------------------------------------------------------------


def test_dispatcher_repromotion_does_not_inflate_block_recurrences(
    kanban_home: Path,
) -> None:
    """Reproduces the t_342c4c9f loop: a card is correctly re-blocked for
    the SAME cause after the dispatcher (not a human/cron) flipped it back
    to ready/running via a ``parents_terminal``-triggered ``promoted``
    event. This must NOT count toward ``block_recurrences`` — only an
    explicit ``unblock_task`` call re-arms the same-cause counter.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t_342c4c9f style review-required card")
        kb.claim_task(conn, tid)

        assert kb.block_task(
            conn, tid, kind="needs_input",
            reason="REJECTED-INTAKE: awaiting producer resubmission",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_recurrences == 1

        # Simulate the dispatcher-side bug: something (parents_terminal
        # sweep, or any non-unblock_task path) flips the card back to
        # ready/running WITHOUT going through unblock_task — so no
        # "unblocked" event is emitted, only a "promoted" one.
        for cycle in range(4):
            conn.execute(
                "UPDATE tasks SET status = 'running' WHERE id = ?", (tid,),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'promoted', ?, ?)",
                (
                    tid,
                    json.dumps({
                        "from_status": "blocked", "to_status": "ready",
                        "trigger": "parents_terminal", "satisfied_parent_ids": [],
                    }),
                    int(time.time()) + cycle,
                ),
            )
            conn.commit()

            assert kb.block_task(
                conn, tid, kind="needs_input",
                reason="REJECTED-INTAKE: awaiting producer resubmission",
                expected_run_id=None,
            )
            task = kb.get_task(conn, tid)
            # The card must land back in 'blocked' (never 'triage') and the
            # recurrence counter must stay at 1 — each re-block after a
            # dispatcher auto-promotion is treated as a fresh same-cause
            # block (recurrences reset to 1), not an accumulating loop.
            assert task.status == "blocked", (
                f"cycle {cycle}: dispatcher-side re-promotion inflated the "
                f"loop-breaker into routing to {task.status!r} instead of "
                "staying blocked"
            )
            assert task.block_recurrences == 1, (
                f"cycle {cycle}: block_recurrences={task.block_recurrences}, "
                "expected 1 — a parents_terminal-triggered re-entry must not "
                "count toward the same-cause loop-breaker counter"
            )


def test_genuine_unblock_reblock_loop_still_trips_breaker(
    kanban_home: Path,
) -> None:
    """Sanity check that the fix above does not defang the original
    loop-breaker: a REAL unblock_task -> re-block cycle for the same cause
    must still accumulate recurrences and eventually route to triage.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="genuine ping-pong reproducer")
        kb.claim_task(conn, tid)

        assert kb.block_task(
            conn, tid, kind="needs_input", reason="waiting on X",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).block_recurrences == 1

        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        conn.commit()

        assert kb.block_task(
            conn, tid, kind="needs_input", reason="waiting on X",
            expected_run_id=None,
        )
        task = kb.get_task(conn, tid)
        # BLOCK_RECURRENCE_LIMIT is 2, so the loop breaker trips on THIS
        # (second) same-cause re-block, routing straight to triage rather
        # than back to blocked — it does not take a third cycle.
        assert task.status == "triage", (
            "a genuine repeated unblock -> re-block cycle for the same "
            "cause must still trip the loop breaker and route to triage"
        )
        assert task.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT == 2
