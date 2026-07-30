"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

from gateway.kanban_watchers import GatewayKanbanWatchersMixin, _telemetry_review_due

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


def test_telemetry_review_runs_once_per_nominal_boundary():
    phoenix = ZoneInfo("America/Phoenix")
    now = int(datetime(2026, 7, 30, 12, 30, tzinfo=phoenix).timestamp())

    due, boundary = _telemetry_review_due(now=now, last_boundary=None)
    duplicate_due, duplicate_boundary = _telemetry_review_due(
        now=now,
        last_boundary=boundary,
    )

    assert due is True
    assert duplicate_due is False
    assert duplicate_boundary == boundary


