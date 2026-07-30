from __future__ import annotations

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


def _profile(home: Path, name: str = "worker") -> Path:
    profile = home / "profiles" / name
    (profile / "skills").mkdir(parents=True)
    (profile / "SOUL.md").write_text(
        "You are Worker, CEA.\n"
        "CERTIFICATION (binding): your certification is the `worker-cert`.\n",
        encoding="utf-8",
    )
    _skill(profile, "worker-cert")
    return profile


def _skill(profile: Path, name: str, frontmatter: str = "") -> None:
    skill = profile / "skills" / name
    skill.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n{frontmatter}---\n"
        "# Operating procedure\n"
        "Use this package to perform the assigned work safely, verify the result, "
        "and report concrete evidence before completion.\n",
        encoding="utf-8",
    )


def _dispatch(home: Path, *, skill: str, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    profile = _profile(home)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="preflight",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(home),
            skills=[skill],
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        runs = kb.list_runs(conn, task_id)
    assert task is not None
    return profile, task_id, result, task, events, runs, spawned


def test_missing_skill_fails_once_before_claim(kanban_home, monkeypatch):
    _, task_id, result, task, events, runs, spawned = _dispatch(
        kanban_home, skill="missing-skill", monkeypatch=monkeypatch,
    )

    assert result.pre_dispatch_failed == [(task_id, "missing_required_skill")]
    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "capability"
    assert runs == []
    assert spawned == []
    failures = [event for event in events if event.kind == "pre_dispatch_validation_failed"]
    assert len(failures) == 1
    assert failures[0].payload is not None
    assert failures[0].payload["action"] == "install_or_sync_required_skill"


def test_missing_certification_package_fails_before_claim(kanban_home, monkeypatch):
    profile = _profile(kanban_home)
    (profile / "skills" / "worker-cert" / "SKILL.md").unlink()
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="certification preflight",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert result.pre_dispatch_failed == [(task_id, "missing_required_skill")]
    assert task is not None
    assert task.status == "blocked"
    assert runs == []


def test_missing_credential_file_fails_once_before_claim(kanban_home, monkeypatch):
    profile = _profile(kanban_home)
    _skill(
        profile,
        "credentialed",
        "required_credential_files:\n  - service-token.json\n",
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="credential preflight",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
            skills=["credentialed"],
        )
        first = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        second = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert task is not None
    assert first.pre_dispatch_failed == [
        (task_id, "missing_required_credential_file"),
    ]
    assert second.pre_dispatch_failed == []
    assert task is not None
    assert task.status == "blocked"
    assert runs == []
    assert len([
        event for event in events if event.kind == "pre_dispatch_validation_failed"
    ]) == 1


def test_wrong_node_fails_before_claim(kanban_home, monkeypatch):
    profile = _profile(kanban_home)
    _skill(
        profile,
        "node-bound",
        "metadata:\n  hermes:\n    allowed_nodes:\n      - definitely-not-this-node\n",
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="node preflight",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
            skills=["node-bound"],
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert result.pre_dispatch_failed == [(task_id, "wrong_node")]
    assert task is not None
    assert task.status == "blocked"
    assert runs == []
    failure = next(
        event for event in events if event.kind == "pre_dispatch_validation_failed"
    )
    assert failure.payload is not None
    assert failure.payload["action"] == "route_to_eligible_node"


def test_valid_candidate_is_claimed_and_spawned(kanban_home, monkeypatch):
    profile = _profile(kanban_home)
    _skill(profile, "ready-skill")
    monkeypatch.setattr(
        "hermes_cli.kanban_db._resolve_hermes_argv",
        lambda: ["python3", "-m", "hermes_cli.main"],
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="valid preflight",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
            skills=["ready-skill"],
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert result.pre_dispatch_failed == []
    assert spawned == [task_id]
    assert task.status == "running"