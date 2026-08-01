"""Reproduces: ready+spawnable task is skipped by check_respawn_guard's
active_pr branch even though (a) global headroom exists and (b) the task
is not a code/PR-producing task (workspace_kind='dir', no branch_name).

Root cause: hermes_cli/kanban_db.py check_respawn_guard() step 4 (the
active_pr guard) applies to EVERY ready task unconditionally on the
pre-fix main. It has no restriction to code tasks (workspace_kind == 'worktree'
or branch_name set). Any ready task with a GitHub PR URL anywhere in a
comment within the guard window is deferred, even for review/triage/
dir-workspace tasks that legitimately cite PR URLs in status comments
(a common pattern -- see e.g. t_543dce5d's own review-handoff comments).

This test is folded into PR #13 (branch
fix/t-543dce5d-dispatcher-consolidated), which scopes the guard to
code_task only. It is the regression test proving the dispatcher-level
symptom (task not spawned despite headroom) is actually fixed, on top of
the unit-level fixtures already in test_kanban_db.py
(test_respawn_guard_ignores_pr_evidence_on_dir_task_without_branch, etc).

NOTE (merge-lane, PR #13 vs main): the "dir" workspace target must exist
on disk before dispatch_once claims the task. fork/main's
kanban_preflight capability-gate framework (adopted by the conflict
resolution merging this branch onto main) runs validate_pre_dispatch()
-- which rejects a "dir" workspace whose configured path is not an
existing directory (code "workspace_unavailable") -- BEFORE
check_respawn_guard() ever runs. Pre-merge, this branch had no such
precondition, so a nonexistent tmp_path subdirectory was fine. Create
the directory so the test still reaches the guard logic under test
rather than tripping the (unrelated, correctly-behaving) new gate.
"""
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


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def test_active_pr_guard_wrongly_skips_non_code_ready_task_despite_headroom(
    kanban_home, all_assignees_spawnable, tmp_path,
):
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect() as conn:
        # A non-code task: dir workspace, no branch_name. This is the
        # shape used by review/triage/decomposer handoff tasks that
        # legitimately reference a GitHub PR URL in a status comment
        # without themselves being the PR-producing branch.
        # workspace_path must be an absolute path for workspace_kind="dir"
        # (resolve_workspace raises otherwise) -- unrelated to the guard
        # logic under test, just satisfying dispatch_once's spawn path.
        # It must also exist on disk: kanban_preflight.validate_dispatch_candidate
        # rejects a "dir" workspace whose path isn't a real directory
        # (code "workspace_unavailable") before the guard under test ever runs.
        dir_workspace = tmp_path / "review-handoff-workspace"
        dir_workspace.mkdir()
        tid = kb.create_task(
            conn,
            title="review handoff citing a PR URL",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(dir_workspace),
        )
        kb.add_comment(
            conn, tid, "some-worker",
            "Handoff: see https://github.com/SSC-ENG/hermes-agent/pull/13 "
            "for the reviewed branch. Needs another pass.",
        )
        # Ample global headroom: nothing else running, cap far above 0.
        result = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, max_in_progress=8,
        )

    # EXPECTED (correct) behavior: headroom exists, task is spawnable,
    # task is NOT a code/PR-producing task -> it should be spawned.
    assert tid in spawns, (
        f"task {tid} was NOT spawned despite global headroom; "
        f"respawn_guarded={result.respawn_guarded!r}"
    )
    assert (tid, "active_pr") not in result.respawn_guarded
