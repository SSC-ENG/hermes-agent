"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from gateway.kanban_watchers import (
    DISPATCHER_HEALTH_WINDOW,
    GatewayKanbanWatchersMixin,
    _next_dispatcher_health,
    _persist_dispatcher_health,
)

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)


def test_dispatcher_health_becomes_actionable_after_six_zero_spawn_ticks():
    ticks = 0
    signal = {}
    capacity = {
        "dispatchable_count": 2,
        "free_global_slots": 3,
        "running_count": 1,
        "boards": [{"slug": "default", "dispatchable_count": 2}],
    }

    for now in range(DISPATCHER_HEALTH_WINDOW):
        ticks, signal = _next_dispatcher_health(
            ticks, any_spawned=False, capacity=capacity, now=now,
        )

    assert signal["actionable"] is True
    assert signal["status"] == "actionable"
    assert signal["code"] == "dispatcher_zero_spawn_with_capacity"
    assert signal["consecutive_zero_spawn_ticks"] == DISPATCHER_HEALTH_WINDOW
    assert signal["recommended_action"]


def test_dispatcher_health_resets_for_correctly_idle_ticks():
    cases = [
        ({"dispatchable_count": 0, "free_global_slots": 3}, False),
        ({"dispatchable_count": 2, "free_global_slots": 0}, False),
        ({"dispatchable_count": 2, "free_global_slots": 3}, True),
    ]
    for capacity, any_spawned in cases:
        ticks, signal = _next_dispatcher_health(
            DISPATCHER_HEALTH_WINDOW - 1,
            any_spawned=any_spawned,
            capacity=capacity,
            now=100,
        )
        assert ticks == 0
        assert signal["actionable"] is False


def test_dispatcher_health_probe_failure_is_unavailable_and_preserves_window():
    ticks, signal = _next_dispatcher_health(
        DISPATCHER_HEALTH_WINDOW - 1,
        any_spawned=False,
        capacity={
            "probe_ok": False,
            "probe_errors": [{"slug": "broken", "error": "DatabaseError"}],
            "dispatchable_count": 0,
            "free_global_slots": None,
        },
        now=100,
    )

    assert ticks == DISPATCHER_HEALTH_WINDOW - 1
    assert signal["status"] == "unavailable"
    assert signal["degraded"] is True
    assert signal["probe_ok"] is False
    assert signal["probe_errors"][0]["slug"] == "broken"


def test_health_persistence_failure_does_not_change_dispatch_result(
    tmp_path, monkeypatch
):
    """A telemetry write failure cannot erase or alter a successful spawn."""
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)

    spawned = []
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="spawn-me", assignee="worker")
        result = kb.dispatch_once(
            conn, spawn_fn=lambda task, workspace: spawned.append(task.id)
        )

    def fail_write(_snapshot):
        raise OSError("read-only health directory")

    assert _persist_dispatcher_health(fail_write, {"status": "ok"}) is False
    assert spawned == [task_id]
    assert result.spawned[0][0] == task_id
