"""Fail-closed capability validation for Kanban dispatch candidates.

The dispatcher calls this module before claiming a task. Validation does not
create a run, workspace, or subprocess. It inspects the assignee profile,
required skill packages, credentials, runtime, workspace configuration, and
skill-declared node boundaries.

Before rejecting a missing/hollow/unreadable certification (or task-scoped)
skill, :func:`validate_dispatch_candidate` best-effort repairs the assignee's
profile skill tree from a known-good canonical package under the Hermes root
``skills/`` tree (and ``HERMES_CANONICAL_SKILLS`` when set). Repairs never invent
policy for unknown certifications, never overwrite a present package that only
fails platform eligibility, and still fail closed when no golden source exists.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from agent.skill_utils import (
    is_excluded_skill_path,
    parse_frontmatter,
    skill_matches_platform_list,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreDispatchFailure:
    """One stable, actionable reason a task cannot be dispatched on this node."""

    code: str
    message: str
    action: str
    requirement: Optional[str] = None


# Failure codes that a best-effort golden-copy sync may repair. Platform and
# node eligibility are routing issues — never overwritten by fleet-wide content.
_REPAIRABLE_SKILL_CODES = frozenset(
    {
        "missing_required_skill",
        "hollow_required_skill",
        "unreadable_required_skill",
    }
)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _profile_skill_files(profile_dir: Path) -> dict[str, Path]:
    """Index active skill packages by directory and frontmatter name."""
    root = profile_dir / "skills"
    if not root.is_dir():
        return {}
    indexed: dict[str, Path] = {}
    try:
        candidates = root.rglob("SKILL.md")
        for skill_file in candidates:
            if is_excluded_skill_path(skill_file, root=root):
                continue
            try:
                raw = skill_file.read_text(encoding="utf-8")
                frontmatter, _ = parse_frontmatter(raw)
            except (OSError, UnicodeError):
                frontmatter = {}
            indexed.setdefault(skill_file.parent.name, skill_file)
            declared = str(frontmatter.get("name") or "").strip()
            if declared:
                indexed.setdefault(declared, skill_file)
    except OSError:
        return indexed
    return indexed


def _index_skill_tree(skills_root: Path) -> dict[str, Path]:
    """Index SKILL.md packages under *skills_root* by dirname and frontmatter name."""
    if not skills_root.is_dir():
        return {}
    indexed: dict[str, Path] = {}
    try:
        for skill_file in skills_root.rglob("SKILL.md"):
            if is_excluded_skill_path(skill_file, root=skills_root):
                continue
            try:
                raw = skill_file.read_text(encoding="utf-8")
                frontmatter, _ = parse_frontmatter(raw)
            except (OSError, UnicodeError):
                frontmatter = {}
            package_dir = skill_file.parent
            indexed.setdefault(package_dir.name, package_dir)
            declared = str(frontmatter.get("name") or "").strip()
            if declared:
                indexed.setdefault(declared, package_dir)
    except OSError:
        return indexed
    return indexed


def _canonical_skills_roots(hermes_root: Optional[Path] = None) -> list[Path]:
    """Ordered roots that may hold golden certification skill packages.

    1. ``HERMES_CANONICAL_SKILLS`` (explicit override; may be os.pathsep-joined)
    2. ``<hermes_root>/skills`` — fleet shared tree (HELIOs wrappers, SSC certs
       when operators place them under e.g. ``skills/ssc-certs/`` or
       ``skills/engineering/``)
    """
    roots: list[Path] = []
    override = os.environ.get("HERMES_CANONICAL_SKILLS", "").strip()
    if override:
        for piece in override.split(os.pathsep):
            piece = piece.strip()
            if piece:
                roots.append(Path(piece).expanduser())
    if hermes_root is None:
        try:
            from hermes_constants import get_default_hermes_root

            hermes_root = get_default_hermes_root()
        except Exception:
            hermes_root = Path.home() / ".hermes"
    roots.append(Path(hermes_root or (Path.home() / ".hermes")) / "skills")
    # De-dupe while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def find_canonical_skill_package(
    skill_name: str,
    *,
    hermes_root: Optional[Path] = None,
) -> Optional[Path]:
    """Return the package directory of a known-good golden skill, or ``None``.

    Lookup is by package directory name or frontmatter ``name``. Callers must
    treat ``None`` as fail-closed — no self-invented policy for unknown certs.
    """
    name = str(skill_name or "").strip()
    if not name:
        return None
    for root in _canonical_skills_roots(hermes_root):
        indexed = _index_skill_tree(root)
        package = indexed.get(name)
        if package is not None and (package / "SKILL.md").is_file():
            return package
    return None


def _skill_content_failure(
    skill_name: str,
    skill_file: Optional[Path],
) -> Optional[PreDispatchFailure]:
    """Return missing/unreadable/hollow failures only (not platform/node)."""
    if skill_file is None:
        return PreDispatchFailure(
            code="missing_required_skill",
            message=f"Required skill package {skill_name!r} is not installed for the assignee profile.",
            action="install_or_sync_required_skill",
            requirement=skill_name,
        )
    try:
        raw = skill_file.read_text(encoding="utf-8")
        _frontmatter, body = parse_frontmatter(raw)
    except (OSError, UnicodeError):
        return PreDispatchFailure(
            code="unreadable_required_skill",
            message=f"Required skill package {skill_name!r} cannot be read.",
            action="repair_required_skill_package",
            requirement=skill_name,
        )
    # A package directory and frontmatter alone are structural evidence. Require
    # actual procedural content before treating the skill as operational.
    meaningful = " ".join(
        line.strip() for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if len(meaningful) < 40:
        return PreDispatchFailure(
            code="hollow_required_skill",
            message=f"Required skill package {skill_name!r} has no substantive instructions.",
            action="restore_required_skill_content",
            requirement=skill_name,
        )
    return None


def _skill_failure(skill_name: str, skill_file: Optional[Path]) -> Optional[PreDispatchFailure]:
    content_failure = _skill_content_failure(skill_name, skill_file)
    if content_failure is not None:
        return content_failure
    assert skill_file is not None
    try:
        raw = skill_file.read_text(encoding="utf-8")
        frontmatter, _body = parse_frontmatter(raw)
    except (OSError, UnicodeError):
        # Already classified by _skill_content_failure; keep diagnostic path safe.
        return PreDispatchFailure(
            code="unreadable_required_skill",
            message=f"Required skill package {skill_name!r} cannot be read.",
            action="repair_required_skill_package",
            requirement=skill_name,
        )
    if not skill_matches_platform_list(frontmatter.get("platforms")):
        return PreDispatchFailure(
            code="wrong_node_platform",
            message=f"Required skill package {skill_name!r} does not support this node platform.",
            action="route_to_eligible_node",
            requirement=skill_name,
        )
    return None


def _install_skill_package(source_package: Path, dest_package: Path) -> None:
    """Copy *source_package* onto *dest_package* via a sibling staging dir."""
    dest_package.parent.mkdir(parents=True, exist_ok=True)
    staging = dest_package.parent / f".{dest_package.name}.skill-sync-tmp"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        shutil.copytree(source_package, staging)
        if dest_package.exists():
            shutil.rmtree(dest_package)
        staging.replace(dest_package)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _destination_for_canonical(
    profile_dir: Path,
    skill_name: str,
    source_package: Path,
    hermes_root: Optional[Path],
) -> Path:
    """Preserve category-relative path under the profile skills tree when known."""
    skills_dest_root = profile_dir / "skills"
    for root in _canonical_skills_roots(hermes_root):
        try:
            rel = source_package.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return skills_dest_root / rel
    # Fallback: install by skill name at the skills root.
    return skills_dest_root / skill_name


def sync_required_skills(
    profile_dir: Path,
    skill_names: Sequence[str],
    *,
    skills_index: Optional[Mapping[str, Path]] = None,
    hermes_root: Optional[Path] = None,
) -> list[str]:
    """Best-effort install/repair of required skill packages from golden copies.

    Only repairs missing, hollow, or unreadable packages when a canonical source
    exists under the shared Hermes skills tree (or ``HERMES_CANONICAL_SKILLS``).
    Returns the skill names successfully repaired. Does not invent unknown certs
    and does not overwrite a present package that fails only on platform/node.
    """
    index = dict(skills_index) if skills_index is not None else _profile_skill_files(profile_dir)
    repaired: list[str] = []
    for raw_name in skill_names:
        skill_name = str(raw_name or "").strip()
        if not skill_name:
            continue
        skill_file = index.get(skill_name)
        failure = _skill_failure(skill_name, skill_file)
        if failure is None:
            continue
        if failure.code not in _REPAIRABLE_SKILL_CODES:
            # Present-but-wrong-platform (or other non-content failure): leave
            # the package untouched so routing policy is preserved.
            continue
        source_package = find_canonical_skill_package(skill_name, hermes_root=hermes_root)
        if source_package is None:
            continue
        # Golden content must itself be substantive before we install it.
        golden_skill = source_package / "SKILL.md"
        if _skill_content_failure(skill_name, golden_skill if golden_skill.is_file() else None):
            continue
        if skill_file is not None:
            dest_package = skill_file.parent
        else:
            dest_package = _destination_for_canonical(
                profile_dir, skill_name, source_package, hermes_root,
            )
        try:
            _install_skill_package(source_package, dest_package)
        except OSError as exc:
            logger.warning(
                "pre_dispatch skill-sync failed for %s → %s: %s",
                skill_name,
                dest_package,
                exc,
            )
            continue
        installed = dest_package / "SKILL.md"
        if _skill_content_failure(skill_name, installed if installed.is_file() else None):
            logger.warning(
                "pre_dispatch skill-sync left %s non-substantive at %s",
                skill_name,
                dest_package,
            )
            continue
        index[skill_name] = installed
        index.setdefault(dest_package.name, installed)
        repaired.append(skill_name)
    return repaired


def _binding_certifications(profile_dir: Path) -> list[str]:
    soul = profile_dir / "SOUL.md"
    if not soul.is_file():
        return []
    try:
        text = soul.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    certifications: list[str] = []
    for line in text.splitlines():
        if "CERTIFICATION (binding):" not in line and "CERTIFICATIONS (binding):" not in line:
            continue
        parts = line.split("`")
        for index in range(1, len(parts), 2):
            name = parts[index].strip()
            if name and name not in certifications:
                certifications.append(name)
    return certifications


def _skill_frontmatter(skill_file: Path) -> Mapping[str, Any]:
    try:
        frontmatter, _ = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return {}
    return frontmatter


def _available_env_names(profile_dir: Path, env: Mapping[str, str]) -> set[str]:
    available = {key for key, value in env.items() if str(value).strip()}
    dotenv_path = profile_dir / ".env"
    if not dotenv_path.is_file():
        return available
    try:
        from dotenv import dotenv_values

        available.update(
            key for key, value in dotenv_values(dotenv_path).items()
            if value is not None and str(value).strip()
        )
    except Exception:
        # Do not hand-roll dotenv parsing around secret values. If the approved
        # parser is unavailable, env-backed requirements remain unresolved.
        pass
    return available


def _credential_failure(
    profile_dir: Path,
    skill_name: str,
    frontmatter: Mapping[str, Any],
    env_names: set[str],
) -> Optional[PreDispatchFailure]:
    required_env = _string_list(frontmatter.get("required_environment_variables"))
    prerequisites = frontmatter.get("prerequisites")
    if isinstance(prerequisites, Mapping):
        required_env.extend(_string_list(prerequisites.get("env_vars")))
    for name in dict.fromkeys(required_env):
        if name not in env_names:
            return PreDispatchFailure(
                code="missing_required_credential",
                message=f"Required credential environment variable {name!r} is unavailable for skill {skill_name!r}.",
                action="provision_profile_credential",
                requirement=name,
            )

    for entry in frontmatter.get("required_credential_files") or []:
        if isinstance(entry, str):
            relative = entry.strip()
        elif isinstance(entry, Mapping):
            relative = str(entry.get("path") or entry.get("name") or "").strip()
        else:
            continue
        if not relative:
            continue
        candidate = profile_dir / relative
        try:
            contained = candidate.resolve(strict=False).is_relative_to(profile_dir.resolve())
        except (OSError, ValueError):
            contained = False
        if not contained or not candidate.is_file():
            return PreDispatchFailure(
                code="missing_required_credential_file",
                message=f"Required credential file {relative!r} is unavailable for skill {skill_name!r}.",
                action="provision_profile_credential_file",
                requirement=relative,
            )
    return None


def _command_failure(skill_name: str, frontmatter: Mapping[str, Any]) -> Optional[PreDispatchFailure]:
    required = _string_list(frontmatter.get("required_commands"))
    prerequisites = frontmatter.get("prerequisites")
    if isinstance(prerequisites, Mapping):
        required.extend(_string_list(prerequisites.get("commands")))
    for command in dict.fromkeys(required):
        if shutil.which(command) is None:
            return PreDispatchFailure(
                code="missing_required_runtime",
                message=f"Required runtime command {command!r} is unavailable for skill {skill_name!r}.",
                action="provision_runtime_command_on_node",
                requirement=command,
            )
    return None


def _node_names() -> set[str]:
    names = {socket.gethostname(), socket.getfqdn()}
    names.update({name.split(".", 1)[0] for name in tuple(names) if name})
    return {name.casefold() for name in names if name}


def _node_failure(skill_name: str, frontmatter: Mapping[str, Any]) -> Optional[PreDispatchFailure]:
    metadata = frontmatter.get("metadata")
    hermes_meta = metadata.get("hermes") if isinstance(metadata, Mapping) else None
    if not isinstance(hermes_meta, Mapping):
        hermes_meta = {}
    allowed = _string_list(frontmatter.get("allowed_nodes"))
    allowed.extend(_string_list(hermes_meta.get("allowed_nodes")))
    boundary = hermes_meta.get("node_boundary")
    if isinstance(boundary, Mapping):
        allowed.extend(_string_list(boundary.get("allowed")))
    allowed = list(dict.fromkeys(allowed))
    if allowed and not (_node_names() & {name.casefold() for name in allowed}):
        return PreDispatchFailure(
            code="wrong_node",
            message=f"Required skill package {skill_name!r} is not eligible on this node.",
            action="route_to_eligible_node",
            requirement=skill_name,
        )
    return None


def _runtime_failure(runtime_argv: Optional[Sequence[str]]) -> Optional[PreDispatchFailure]:
    if not runtime_argv:
        return PreDispatchFailure(
            code="missing_worker_runtime",
            message="No Hermes worker runtime can be resolved on this node.",
            action="install_or_activate_hermes_runtime",
        )
    executable = str(runtime_argv[0])
    executable_available = (
        Path(executable).is_file() and os.access(executable, os.X_OK)
        if os.path.isabs(executable)
        else shutil.which(executable) is not None
    )
    # ``sys.executable -m hermes_cli.main`` is the dispatcher's intentional
    # fallback when no console-script shim is on PATH. The interpreter was
    # already validated above; remaining argv entries are arguments, not
    # executables.
    if not executable_available:
        return PreDispatchFailure(
            code="missing_worker_runtime",
            message="The resolved Hermes worker runtime is not executable on this node.",
            action="repair_hermes_runtime",
        )
    return None


def _workspace_failure(task: Any, *, board_default_workdir: Optional[str], scratch_root: Path) -> Optional[PreDispatchFailure]:
    kind = task.workspace_kind or "scratch"
    raw_path = task.workspace_path
    if kind == "scratch":
        probe = Path(raw_path).expanduser() if raw_path else scratch_root
        if raw_path and not probe.is_absolute():
            return PreDispatchFailure(
                code="invalid_workspace",
                message="The task scratch workspace path is not absolute.",
                action="set_absolute_workspace_path",
            )
        existing = probe
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if not existing.is_dir() or not os.access(existing, os.W_OK | os.X_OK):
            return PreDispatchFailure(
                code="workspace_unavailable",
                message="The task scratch workspace cannot be created on this node.",
                action="provision_writable_workspace",
            )
        return None
    if kind == "dir":
        if not raw_path:
            return PreDispatchFailure(
                code="workspace_unavailable",
                message="The task requires a directory workspace but no path is configured.",
                action="set_existing_workspace_path",
            )
        path = Path(raw_path).expanduser()
        if not path.is_absolute() or not path.is_dir():
            return PreDispatchFailure(
                code="workspace_unavailable",
                message="The configured directory workspace is not present on this node.",
                action="provision_or_correct_workspace_path",
            )
        return None
    if kind == "worktree":
        anchor = raw_path or board_default_workdir
        if not anchor:
            return PreDispatchFailure(
                code="workspace_unavailable",
                message="The task requires a worktree but no repository anchor is configured.",
                action="set_worktree_repo_or_board_default",
            )
        path = Path(anchor).expanduser()
        if not path.is_absolute():
            return PreDispatchFailure(
                code="invalid_workspace",
                message="The configured worktree anchor is not absolute.",
                action="set_absolute_workspace_path",
            )
        existing = path if path.exists() else path.parent
        if not existing.is_dir():
            return PreDispatchFailure(
                code="workspace_unavailable",
                message="The configured worktree repository is not present on this node.",
                action="provision_or_correct_workspace_path",
            )
        repo_probe = existing
        while repo_probe != repo_probe.parent and not (repo_probe / ".git").exists():
            repo_probe = repo_probe.parent
        if not (repo_probe / ".git").exists():
            return PreDispatchFailure(
                code="workspace_unavailable",
                message="The configured worktree anchor is not inside a Git repository on this node.",
                action="provision_or_correct_workspace_path",
            )
        return None
    return PreDispatchFailure(
        code="invalid_workspace",
        message=f"Workspace kind {kind!r} is not supported by this dispatcher.",
        action="set_supported_workspace_kind",
        requirement=kind,
    )


def validate_dispatch_candidate(
    task: Any,
    *,
    runtime_argv: Optional[Sequence[str]],
    board_default_workdir: Optional[str],
    scratch_root: Path,
    env: Optional[Mapping[str, str]] = None,
    hermes_root: Optional[Path] = None,
) -> Optional[PreDispatchFailure]:
    """Return the first actionable failure, or ``None`` when dispatch is safe.

    Before rejecting missing/hollow/unreadable required skills, best-effort
    repairs them from the fleet canonical skills tree when a golden package
    exists. Unknown certifications without a golden source still fail closed.
    """
    from hermes_cli.profiles import get_profile_dir, profile_exists

    assignee = str(task.assignee or "").strip()
    if not assignee or not profile_exists(assignee):
        return PreDispatchFailure(
            code="missing_assignee_profile",
            message=f"Assignee profile {assignee!r} does not exist.",
            action="create_profile_or_reassign",
            requirement=assignee or None,
        )

    profile_dir = get_profile_dir(assignee)
    skills = _profile_skill_files(profile_dir)
    certifications = _binding_certifications(profile_dir)
    soul_path = profile_dir / "SOUL.md"
    if assignee != "default" and soul_path.is_file() and not certifications:
        return PreDispatchFailure(
            code="missing_profile_certification",
            message=f"Assignee profile {assignee!r} declares no binding certification.",
            action="repair_profile_certification",
            requirement=assignee,
        )
    # The binding certification package itself must be installed and
    # substantive, exactly like every task-scoped skill.
    required_skills = list(dict.fromkeys([
        *certifications,
        *(str(name).strip() for name in (task.skills or []) if str(name).strip()),
    ]))
    # Best-effort self-heal for known-good golden certification/skill packages
    # before applying fail-closed checks. Does not invent policy for unknown
    # certs and never overwrites wrong-platform packages.
    if required_skills:
        repaired = sync_required_skills(
            profile_dir,
            required_skills,
            skills_index=skills,
            hermes_root=hermes_root,
        )
        if repaired:
            skills = _profile_skill_files(profile_dir)
    available_env = _available_env_names(profile_dir, env or os.environ)
    for skill_name in required_skills:
        skill_file = skills.get(skill_name)
        failure = _skill_failure(skill_name, skill_file)
        if failure:
            return failure
        assert skill_file is not None
        frontmatter = _skill_frontmatter(skill_file)
        failure = _node_failure(skill_name, frontmatter)
        if failure:
            return failure
        failure = _credential_failure(profile_dir, skill_name, frontmatter, available_env)
        if failure:
            return failure
        failure = _command_failure(skill_name, frontmatter)
        if failure:
            return failure

    failure = _runtime_failure(runtime_argv)
    if failure:
        return failure
    return _workspace_failure(
        task,
        board_default_workdir=board_default_workdir,
        scratch_root=scratch_root,
    )
