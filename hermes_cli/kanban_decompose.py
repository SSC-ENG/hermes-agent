"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the
auto-decompose path in the gateway dispatcher loop. Reads the user's
profile roster (with descriptions) and asks the auxiliary LLM to
return a task graph in JSON. Then atomically creates the children,
links them under the root, and flips the root ``triage -> todo``.

The root task stays alive and becomes the parent of every leaf child,
so when the whole graph completes the root wakes back up — its
assignee (the orchestrator profile) gets a chance to judge completion
and add more tasks if the work isn't done yet.

Design notes
------------

* Mirrors the shape of ``hermes_cli/kanban_specify.py``: lazy aux
  client import inside the function, lenient response parse, never
  raises on expected failure modes.

* The system prompt sees the *configured* profile roster — names plus
  descriptions plus the default fallback. Profiles without a
  description are still listed (with a note) so the decomposer can
  match on name as a fallback, but the user has an obvious incentive
  to describe them.

* ``fanout=false`` collapses to the same effect as ``kanban specify``:
  we tighten the body and flip ``triage -> todo`` as a single task,
  no children created. This makes ``decompose`` a strict superset of
  ``specify`` from the user's perspective.

* LLM output is advisory. Tenant, domain, certification, profile,
  assignee, graph, fan-out, and PPMA-gate invariants are validated
  deterministically before any DB mutation.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_intake
from hermes_cli import profiles as profiles_mod

logger = logging.getLogger(__name__)

_PPMA_PROFILE = "paul-park"
_DEFAULT_FANOUT_CAP = 6
_DEFAULT_DOMAINS = frozenset({
    "program-management",
    "engineering",
    "security",
    "finance",
    "marketing",
    "operations",
    "information-technology",
    "customer-service",
    "product",
    "legal",
    "procurement",
    "people",
})


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "domain": "<one allowed domain supplied in the intake request>",
        "required_certification": "<binding skill name or null>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - For fanout=true, index 0 is the PPMA scoping gate: assign paul-park,
    domain program-management, no parents, certification helios-agent-ppma.
  - Every execution task depends directly on index 0. PPMA records the Linear
    parent and technical CPTC sub-issues before those tasks become eligible.
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism after the shared PPMA gate. Independent execution tasks
    should list only index 0 as a parent.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When assignment is ambiguous, use null;
    deterministic validation routes the item to PPMA with the reason recorded.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Ambiguous-assignment owner: {default_assignee}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "decomposer"
    )


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _resolve_orchestrator_profile(cfg: dict) -> str:
    """Resolve which profile owns the root/orchestration task after fan-out.

    Ambiguous orchestration is a PPMA concern, never a launch-profile concern.
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("orchestrator_profile") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    return _PPMA_PROFILE


def _resolve_default_assignee(cfg: dict) -> str:
    """Resolve the PPMA owner for ambiguous child assignment."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("default_assignee") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    return _PPMA_PROFILE


def _profile_certifications(profile_path) -> set[str]:
    """Return binding skill-directory names installed for one profile."""
    if profile_path is None:
        return set()
    skills_root = profile_path / "skills"
    if not skills_root.is_dir():
        return set()
    certifications: set[str] = set()
    for skill_md in skills_root.rglob("SKILL.md"):
        certifications.add(skill_md.parent.name)
    return certifications


def _build_roster() -> tuple[list[dict], dict[str, set[str]]]:
    """Return (roster_for_prompt, installed certifications by profile).

    Each roster entry is ``{name, description, has_description}``. The
    The certification map is used by deterministic post-LLM validation.
    """
    roster: list[dict] = []
    certifications: dict[str, set[str]] = {}
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return roster, certifications
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
        certifications[p.name] = _profile_certifications(getattr(p, "path", None))
        if p.name == _PPMA_PROFILE:
            # The PPMA profile's identity is the authoritative certification
            # holder. This also keeps isolated test/profile fixtures honest
            # when they model the profile without copying its skill tree.
            certifications[p.name].add("helios-agent-ppma")
    return roster, certifications


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    lines = []
    for entry in roster:
        tag = "" if entry["has_description"] else " ⚠ undescribed"
        lines.append(f"  - {entry['name']}{tag}: {entry['description']}")
    return "\n".join(lines)


def _normalize_assignee_choice(
    assignee: object,
    *,
    default_assignee: str,
    valid_names: set[str],
) -> str:
    """Return a valid assignee, falling back to ``default_assignee``.

    Fan-out children and the single-task fallback should share the same
    routing guarantee: promoted work must not be left unassigned.
    """
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    if chosen not in valid_names:
        return default_assignee
    return chosen


def _allowed_domains(cfg: dict, envelope: Optional[kanban_intake.IntakeEnvelope]) -> set[str]:
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    configured = kanban_cfg.get("intake_allowed_domains") or []
    allowed = {
        str(value).strip()
        for value in configured
        if isinstance(value, str) and value.strip()
    }
    allowed = allowed or set(_DEFAULT_DOMAINS)
    if envelope:
        allowed.add(envelope.tenant_domain)
    allowed.add("program-management")
    return allowed


def _validate_children(
    raw_tasks: list,
    *,
    cfg: dict,
    task: kb.Task,
    envelope: Optional[kanban_intake.IntakeEnvelope],
    certifications: dict[str, set[str]],
) -> tuple[list[dict], list[dict]]:
    """Validate and normalize all LLM graph output before any durable write."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    fanout_cap = int(kanban_cfg.get("intake_fanout_cap") or _DEFAULT_FANOUT_CAP)
    if len(raw_tasks) < 2:
        raise ValueError("fanout graph must contain PPMA task 0 plus an execution task")
    if len(raw_tasks) > fanout_cap:
        raise ValueError(f"fanout graph exceeds configured cap of {fanout_cap}")
    if _PPMA_PROFILE not in certifications:
        raise ValueError("required PPMA profile 'paul-park' is not installed")
    if envelope and task.tenant and envelope.tenant_domain != task.tenant:
        raise ValueError(
            "intake envelope tenant_domain does not match the task tenant"
        )

    allowed_domains = _allowed_domains(cfg, envelope)
    allowed_assignees = set(certifications)
    configured_assignees = kanban_cfg.get("intake_allowed_assignees") or []
    if configured_assignees:
        allowed_assignees &= {
            str(value).strip()
            for value in configured_assignees
            if isinstance(value, str) and value.strip()
        }
        allowed_assignees.add(_PPMA_PROFILE)

    children: list[dict] = []
    decisions: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            raise ValueError(f"tasks[{idx}] is not an object")
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"tasks[{idx}].title is missing or empty")
        body = entry.get("body") if isinstance(entry.get("body"), str) else ""
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            raise ValueError(f"tasks[{idx}].parents must be a list")
        if any(not isinstance(parent, int) for parent in parents):
            raise ValueError(f"tasks[{idx}].parents contains a non-integer index")
        if any(parent < 0 or parent >= len(raw_tasks) or parent == idx for parent in parents):
            raise ValueError(f"tasks[{idx}].parents contains an invalid index")
        if len(set(parents)) != len(parents):
            raise ValueError(f"tasks[{idx}].parents contains duplicate indices")

        requested = entry.get("assignee")
        requested_name = requested.strip() if isinstance(requested, str) else None
        domain = str(entry.get("domain") or "").strip()
        required_certification = str(entry.get("required_certification") or "").strip() or None
        reason = "validated"

        if idx == 0:
            requested_name = _PPMA_PROFILE
            domain = "program-management"
            required_certification = "helios-agent-ppma"
            parents = []
            reason = "ppma_gate_enforced"
            gate_text = (
                "Record scope before execution with: LINEAR_SCOPE: parent=<KEY> "
                "subissues=[{key:<KEY>, cptc:<N>}]. Then complete this gate so "
                "the dependent domain tasks can become eligible."
            )
            body = f"{body.strip()}\n\n{gate_text}".strip()
        elif 0 not in parents:
            raise ValueError(f"tasks[{idx}] is not directly gated by PPMA task 0")

        if domain not in allowed_domains:
            raise ValueError(f"tasks[{idx}].domain {domain!r} is not allowed")

        resolved = requested_name
        if not resolved or resolved not in allowed_assignees:
            resolved = _PPMA_PROFILE
            reason = "ambiguous_or_disallowed_assignee"
        if required_certification:
            holder_skills = certifications.get(resolved, set())
            if required_certification not in holder_skills:
                resolved = _PPMA_PROFILE
                reason = "required_certification_unverified"
        if resolved not in certifications:
            raise ValueError(f"tasks[{idx}] resolved to missing profile {resolved!r}")
        if (
            required_certification
            and required_certification not in certifications.get(resolved, set())
        ):
            raise ValueError(
                f"tasks[{idx}] final assignee {resolved!r} does not hold "
                f"required certification {required_certification!r}"
            )

        children.append({
            "title": title.strip()[:200],
            "body": body.strip(),
            "assignee": resolved,
            "parents": parents,
            "domain": domain,
            "required_certification": required_certification,
            "ppma_scope_gate": idx == 0,
        })
        decisions.append({
            "index": idx,
            "requested_assignee": requested,
            "resolved_assignee": resolved,
            "required_certification": required_certification,
            "reason": reason,
        })

    # DB also checks cycles atomically. Detect here so the complete post-LLM
    # validation result is known before attempting the durable decomposition.
    indegree = [0] * len(children)
    adjacent: list[list[int]] = [[] for _ in children]
    for child_idx, child in enumerate(children):
        for parent_idx in child["parents"]:
            adjacent[parent_idx].append(child_idx)
            indegree[child_idx] += 1
    queue = [idx for idx, degree in enumerate(indegree) if degree == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for neighbor in adjacent[node]:
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                queue.append(neighbor)
    if visited != len(children):
        raise ValueError("fanout graph contains a dependency cycle")
    return children, decisions


def _validate_single_assignee(
    parsed: dict,
    *,
    cfg: dict,
    task: kb.Task,
    envelope: Optional[kanban_intake.IntakeEnvelope],
    certifications: dict[str, set[str]],
) -> tuple[str, dict]:
    """Validate the final assignee for a true one-item intake."""
    if _PPMA_PROFILE not in certifications:
        raise ValueError("required PPMA profile 'paul-park' is not installed")
    if envelope and task.tenant and envelope.tenant_domain != task.tenant:
        raise ValueError("intake envelope tenant_domain does not match the task tenant")

    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    allowed_assignees = set(certifications)
    configured_assignees = kanban_cfg.get("intake_allowed_assignees") or []
    if configured_assignees:
        allowed_assignees &= {
            str(value).strip()
            for value in configured_assignees
            if isinstance(value, str) and value.strip()
        }
        allowed_assignees.add(_PPMA_PROFILE)

    requested = parsed.get("assignee")
    requested_name = requested.strip() if isinstance(requested, str) else None
    required_certification = (
        str(parsed.get("required_certification") or "").strip() or None
    )
    resolved = requested_name
    reason = "validated"
    if not resolved or resolved not in allowed_assignees:
        resolved = _PPMA_PROFILE
        reason = "ambiguous_or_disallowed_assignee"
    if required_certification:
        if required_certification not in certifications.get(resolved, set()):
            resolved = _PPMA_PROFILE
            reason = "required_certification_unverified"
        if required_certification not in certifications.get(resolved, set()):
            raise ValueError(
                f"final assignee {resolved!r} does not hold required "
                f"certification {required_certification!r}"
            )
    return resolved, {
        "requested_assignee": requested,
        "resolved_assignee": resolved,
        "required_certification": required_certification,
        "reason": reason,
    }


def _multi_item_fallback_graph(
    envelope: kanban_intake.IntakeEnvelope,
    task: kb.Task,
) -> list[dict]:
    """Fail safely when advisory model output declines mandatory fan-out."""
    children = [{
        "title": f"Scope {task.title or 'raw intake'}",
        "body": (
            "The model declined mandatory fan-out for a canonical multi-item "
            "intake. Scope every raw item into Linear and record LINEAR_SCOPE."
        ),
        "assignee": _PPMA_PROFILE,
        "domain": "program-management",
        "required_certification": "helios-agent-ppma",
        "parents": [],
    }]
    for index, item in enumerate(envelope.items, start=1):
        children.append({
            "title": f"Route intake item {index}",
            "body": (
                f"Raw intake item:\n{item}\n\n"
                "Assignment remained ambiguous after model decomposition. "
                "PPMA must route it explicitly; do not infer an execution owner."
            ),
            "assignee": _PPMA_PROFILE,
            "domain": envelope.tenant_domain,
            "required_certification": None,
            "parents": [0],
        })
    return children


def _record_validation_failure(task_id: str, reason: str, *, author: str) -> None:
    """Persist a bounded validation failure for watcher/operator diagnosis."""
    try:
        with kb.connect_closing() as conn:
            with kb.write_txn(conn):
                kb._append_event(
                    conn,
                    task_id,
                    "intake_validation_failed",
                    {
                        "schema_version": 1,
                        "actor": author,
                        "source": "decomposer",
                        "reason": reason,
                    },
                )
    except Exception:
        logger.exception("decompose: failed to record validation failure for %s", task_id)


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks.

    Returns an outcome describing what happened. Never raises for
    expected failure modes (task not in triage, no aux client
    configured, API error, malformed response, decomposer returned
    fanout=true with empty task list) — those surface via ``ok=False``.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        if task is None:
            return DecomposeOutcome(task_id, False, "unknown task id")
        if task.status != "triage":
            return DecomposeOutcome(
                task_id, False, f"task is not in triage (status={task.status!r})"
            )
        # A triage card whose most recent event is ``block_loop_detected`` was
        # routed here specifically to force a HUMAN decision (see
        # ``block_task``/``BLOCK_RECURRENCE_LIMIT`` in kanban_db.py) — the
        # unblock<->reblock loop breaker tripped because a worker kept
        # re-blocking it for the same cause. Blindly re-"specifying" and
        # promoting such a card back to ``todo``/``ready`` hands it straight
        # back into the same loop the breaker exists to interrupt (t_e2b1f62a):
        # the card's disposition is already correct and signed, it just needs a
        # human triage call, not another decomposer pass. Skip it here; a human
        # (or an explicit ``kanban specify``/``kanban promote`` call) is the
        # only legitimate way out of this state.
        most_recent_kind = kb._most_recent_event_kind(conn, task_id)
    if most_recent_kind == "block_loop_detected":
        return DecomposeOutcome(
            task_id, False,
            "skipped: most recent event is block_loop_detected — "
            "awaiting human triage decision, not auto-decompose",
        )

    cfg = _load_config()
    orchestrator = _resolve_orchestrator_profile(cfg)
    default_assignee = _resolve_default_assignee(cfg)
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    auto_promote = bool(kanban_cfg.get("auto_promote_children", True))
    roster, certifications = _build_roster()
    valid_names = set(certifications)
    audit_author = author or _profile_author()
    try:
        envelope = kanban_intake.parse_envelope(task.body)
    except ValueError as exc:
        reason = f"invalid intake envelope: {exc}"
        _record_validation_failure(task_id, reason, author=audit_author)
        return DecomposeOutcome(task_id, False, reason)

    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception as exc:
        logger.debug("decompose: auxiliary client import failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
        roster=_format_roster(roster),
        default_assignee=default_assignee,
    )

    try:
        # Route through call_llm so auxiliary.kanban_decomposer.* config
        # (provider/model/base_url, extra_body, reasoning_effort, retries)
        # all apply — the previous direct client.chat.completions.create()
        # path dropped auxiliary.<task>.extra_body entirely (#35566).
        resp = call_llm(
            task="kanban_decomposer",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=4000,
            timeout=timeout or 180,
        )
    except Exception as exc:
        logger.info(
            "decompose: API call failed for %s (%s)", task_id, exc,
        )
        return DecomposeOutcome(task_id, False, f"LLM error: {type(exc).__name__}")

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    fanout = bool(parsed.get("fanout"))

    if not fanout:
        if envelope and len(envelope.items) > 1:
            fanout = True
            parsed["tasks"] = _multi_item_fallback_graph(envelope, task)
            parsed["rationale"] = "mandatory_multi_item_fanout"
        elif envelope:
            try:
                resolved_assignee, routing_decision = _validate_single_assignee(
                    parsed,
                    cfg=cfg,
                    task=task,
                    envelope=envelope,
                    certifications=certifications,
                )
            except ValueError as exc:
                reason = f"post-LLM validation failed: {exc}"
                _record_validation_failure(task_id, reason, author=audit_author)
                return DecomposeOutcome(task_id, False, reason)
        else:
            requested = parsed.get("assignee")
            resolved_assignee = task.assignee or _normalize_assignee_choice(
                requested,
                default_assignee=default_assignee,
                valid_names=valid_names,
            )
            routing_decision = {
                "requested_assignee": requested,
                "resolved_assignee": resolved_assignee,
                "required_certification": None,
                "reason": (
                    "validated"
                    if requested == resolved_assignee
                    else "legacy_default_assignee"
                ),
            }

    if not fanout:
        # Fall back to single-task spec promotion (same effect as specify).
        new_title = parsed.get("title")
        new_body = parsed.get("body")
        title_val = new_title.strip() if isinstance(new_title, str) and new_title.strip() else None
        body_val = new_body if isinstance(new_body, str) and new_body.strip() else None
        assignee_val = resolved_assignee if not task.assignee else None
        if title_val is None and body_val is None:
            return DecomposeOutcome(
                task_id, False, "decomposer returned fanout=false with no title/body",
            )
        with kb.connect_closing() as conn:
            ok = kb.specify_triage_task(
                conn,
                task_id,
                title=title_val,
                body=body_val,
                assignee=assignee_val,
                author=audit_author,
            )
        if not ok:
            return DecomposeOutcome(
                task_id, False, "task moved out of triage before promotion",
            )
        with kb.connect_closing() as conn:
            with kb.write_txn(conn):
                kb._append_event(
                    conn,
                    task_id,
                    "intake_classified",
                    {
                        "schema_version": 1,
                        "actor": audit_author,
                        "source": "decomposer",
                        "correlation_id": task_id,
                        "intake_id": task_id,
                        "domain": envelope.tenant_domain if envelope else "UNKNOWN",
                        "classification": "single_task",
                    },
                )
                kb._append_event(
                    conn,
                    task_id,
                    "routing_decided",
                    {
                        "schema_version": 1,
                        "actor": audit_author,
                        "source": "decomposer",
                        "correlation_id": task_id,
                        "intake_id": task_id,
                        "task_id": task_id,
                        **routing_decision,
                        "fallback_used": routing_decision["reason"] != "validated",
                        "profile_exists": routing_decision["resolved_assignee"] in valid_names,
                    },
                )
                kb._append_event(
                    conn,
                    task_id,
                    "decomposition_decided",
                    {
                        "schema_version": 1,
                        "actor": audit_author,
                        "source": "decomposer",
                        "correlation_id": task_id,
                        "intake_id": task_id,
                        "root_task_id": task_id,
                        "fanout": False,
                        "child_ids": [],
                        "dependency_edges": [],
                        "rationale_code": "single_task",
                    },
                )
        return DecomposeOutcome(
            task_id, True, "single task (no fanout)",
            fanout=False, new_title=title_val,
        )

    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(
            task_id, False, "decomposer returned fanout=true with empty tasks list",
        )

    try:
        children, routing_decisions = _validate_children(
            raw_tasks,
            cfg=cfg,
            task=task,
            envelope=envelope,
            certifications=certifications,
        )
    except ValueError as exc:
        reason = f"post-LLM validation failed: {exc}"
        _record_validation_failure(task_id, reason, author=audit_author)
        return DecomposeOutcome(task_id, False, reason)

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee=orchestrator,
                children=children,
                author=audit_author,
                auto_promote=auto_promote,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")

    if child_ids is None:
        return DecomposeOutcome(
            task_id, False, "task moved out of triage before decomposition",
        )

    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "intake_classified",
                {
                    "schema_version": 1,
                    "actor": audit_author,
                    "source": "decomposer",
                    "correlation_id": task_id,
                    "intake_id": task_id,
                    "domain": envelope.tenant_domain if envelope else task.tenant or "UNKNOWN",
                    "classification": "fanout",
                },
            )
            for child_id, decision in zip(child_ids, routing_decisions):
                kb._append_event(
                    conn,
                    child_id,
                    "routing_decided",
                    {
                        "schema_version": 1,
                        "actor": audit_author,
                        "source": "decomposer",
                        "correlation_id": task_id,
                        "intake_id": task_id,
                        "task_id": child_id,
                        **decision,
                        "fallback_used": decision["reason"] != "validated",
                        "profile_exists": decision["resolved_assignee"] in valid_names,
                    },
                )
            kb._append_event(
                conn,
                task_id,
                "decomposition_decided",
                {
                    "schema_version": 1,
                    "actor": audit_author,
                    "source": "decomposer",
                    "correlation_id": task_id,
                    "intake_id": task_id,
                    "root_task_id": task_id,
                    "fanout": True,
                    "child_ids": child_ids,
                    "dependency_edges": [
                        {"parent_index": parent, "child_index": index}
                        for index, child in enumerate(children)
                        for parent in child.get("parents", [])
                    ],
                    "rationale_code": parsed.get("rationale") or "fanout",
                },
            )

    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children",
        fanout=True, child_ids=child_ids,
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column."""
    with kb.connect_closing() as conn:
        rows = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            limit=1000,
        )
    return [row.id for row in rows]
