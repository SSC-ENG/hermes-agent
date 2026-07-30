"""Deterministic, read-only 48-hour Kanban telemetry-hole review."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

RULE_SET_VERSION = "1.0.0"
REVIEW_WINDOW_SECONDS = 48 * 60 * 60
CADENCE_SECONDS = 12 * 60 * 60
INTAKE_DECISION_SECONDS = 30 * 60
RUNNING_INACTIVITY_SECONDS = 60 * 60
BLOCKED_AGING_SECONDS = 12 * 60 * 60
FINDING_RETENTION_SECONDS = 7 * 24 * 60 * 60

_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "linear_scoped": ("linear_issue_key", "sub_issue_keys", "cptc_estimates"),
    "intake_received": ("intake_id", "source_type", "source_ref_hash", "provenance_refs", "idempotency_key", "received_by"),
    "intake_classified": ("intake_id", "domain", "classification"),
    "routing_decided": ("intake_id", "task_id", "requested_assignee", "resolved_assignee", "fallback_used", "profile_exists"),
    "decomposition_decided": ("intake_id", "root_task_id", "fanout", "child_ids", "dependency_edges"),
    "handoff_emitted": ("handoff_id", "from_owner", "to_owner", "artifact_refs", "acceptance_contract", "next_expected_event", "due_by"),
    "handoff_accepted": ("handoff_id", "receiver", "run_id"),
    "gate_required": ("gate_id", "gate_type", "authority", "candidate_ref", "candidate_version"),
    "gate_decided": ("gate_id", "gate_type", "authority", "verdict", "candidate_ref", "candidate_version"),
    "haa_decision_requested": ("decision_id", "prompt_ref", "requester", "options", "irreversible", "requested_at"),
    "haa_decision_recorded": ("decision_id", "decision", "decided_at", "evidence_ref", "recorder"),
    "estimate_observed": ("issue_id", "issue_role", "parent_issue_id", "estimate", "unit", "observed_at", "linear_updated_at"),
    "review_disposition": ("candidate_ref", "candidate_version", "verdict", "next_owner", "next_expected_event"),
    "merge_disposition": ("pr_ref", "candidate_version", "state", "owner"),
    "infrastructure_exclusion": ("finding_key", "rhea_task_ref", "reason_code", "expires_at"),
}


def validate_event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a governed event payload fail-closed."""
    if kind not in _REQUIRED_FIELDS:
        raise ValueError(f"unsupported governed event kind: {kind}")
    missing = [field for field in _REQUIRED_FIELDS[kind] if field not in payload]
    if missing:
        raise ValueError(f"{kind} missing required field(s): {', '.join(missing)}")
    out = dict(payload)
    out.setdefault("schema_version", 1)
    out.setdefault("actor", "UNKNOWN")
    out.setdefault("source", "cli")
    out.setdefault("correlation_id", None)
    out.setdefault("owner", None)
    out.setdefault("reason_code", None)
    return out


def record_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    run_id: Optional[int] = None,
) -> None:
    from hermes_cli import kanban_db as kb

    if kb.get_task(conn, task_id) is None:
        raise ValueError(f"unknown task {task_id}")
    normalized = validate_event(kind, payload)
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, kind, normalized, run_id=run_id)


def _utc(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat().replace("+00:00", "Z")


def _implementation_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
        return result.stdout.strip() or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def _parse_payload(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _subject(
    task_ids: list[str] | None = None,
    run_ids: list[int] | None = None,
    profiles: list[str] | None = None,
    refs: list[str] | None = None,
) -> dict[str, list]:
    return {
        "task_ids": sorted({str(x) for x in (task_ids or []) if x}),
        "run_ids": sorted({int(x) for x in (run_ids or []) if x is not None}),
        "profile_slugs": sorted({str(x) for x in (profiles or []) if x}),
        "external_refs": sorted({str(x) for x in (refs or []) if x}),
    }


def _finding(
    rule_id: str,
    board: str,
    subject: dict[str, list],
    *,
    severity: str,
    evidence_state: str,
    title: str,
    owner: str,
    recommendation: str,
    evidence: list[dict[str, Any]],
    observed_at: int,
    correlation_id: Optional[str] = None,
    next_expected_event: Optional[str] = None,
    due_by: Optional[int] = None,
) -> dict[str, Any]:
    normalized_subject = _subject(
        subject.get("task_ids"),
        subject.get("run_ids"),
        subject.get("profile_slugs"),
        subject.get("external_refs"),
    )
    subject_ids = json.dumps(normalized_subject, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(
        f"{rule_id}|{subject_ids}|{correlation_id or ''}|{board}".encode()
    ).hexdigest()
    return {
        "finding_key": key,
        "rule_id": rule_id,
        "state": "NEW",
        "severity": severity,
        "evidence_state": evidence_state,
        "title": title,
        "subject": normalized_subject,
        "first_observed_at": _utc(observed_at),
        "last_observed_at": _utc(observed_at),
        "duration_seconds": 0,
        "evidence": evidence,
        "impact": title,
        "owner": owner or "OWNER.UNRESOLVED",
        "recommendation": recommendation,
        "next_expected_event": next_expected_event,
        "due_by": _utc(due_by) if due_by is not None else None,
        "rhea_exclusion": {"excluded": False, "task_ref": None, "reason_code": None},
        "lens_findings": [],
    }


def _capture_snapshot(
    conn: sqlite3.Connection,
    *,
    start: int,
    end: int,
) -> tuple[int, int, list[dict[str, Any]], dict[str, sqlite3.Row], list[sqlite3.Row]]:
    """Capture event watermark and all bounded source rows in one read txn."""
    conn.execute("BEGIN")
    try:
        high = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0])
        low = int(
            conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM task_events "
                "WHERE created_at < ? AND id <= ?",
                (start, high),
            ).fetchone()[0]
        )
        event_rows = conn.execute(
            "SELECT id, task_id, kind, payload, created_at, run_id "
            "FROM task_events WHERE id > ? AND id <= ? "
            "AND created_at >= ? AND created_at < ? ORDER BY id",
            (low, high, start, end),
        ).fetchall()
        events = [
            {
                "id": int(row["id"]),
                "task_id": row["task_id"],
                "kind": row["kind"],
                "payload": _parse_payload(row["payload"]),
                "created_at": int(row["created_at"]),
                "run_id": row["run_id"],
            }
            for row in event_rows
        ]
        task_ids = {event["task_id"] for event in events}
        task_ids.update(
            row["id"]
            for row in conn.execute(
                "SELECT id FROM tasks WHERE status NOT IN ('done', 'archived') "
                "AND created_at < ?",
                (end,),
            ).fetchall()
        )
        run_rows = conn.execute(
            "SELECT id, task_id, profile, step_key, status, started_at, ended_at, "
            "outcome, last_heartbeat_at FROM task_runs "
            "WHERE started_at < ? AND COALESCE(ended_at, ?) >= ? ORDER BY id",
            (end, end, start),
        ).fetchall()
        task_ids.update(row["task_id"] for row in run_rows)
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            task_rows = conn.execute(
                f"SELECT id, status, assignee, created_at, started_at, completed_at, "
                f"current_step_key, workflow_template_id, block_kind, block_recurrences "
                f"FROM tasks WHERE id IN ({placeholders})",
                tuple(sorted(task_ids)),
            ).fetchall()
        else:
            task_rows = []
        tasks = {row["id"]: row for row in task_rows}
        # Pull all evidence for included tasks up to the watermark. This avoids
        # false stalls when a decision happened just before the rolling window.
        all_events: list[dict[str, Any]] = []
        if tasks:
            placeholders = ",".join("?" for _ in tasks)
            all_rows = conn.execute(
                f"SELECT id, task_id, kind, payload, created_at, run_id FROM task_events "
                f"WHERE task_id IN ({placeholders}) AND id <= ? ORDER BY id",
                (*sorted(tasks), high),
            ).fetchall()
            all_events = [
                {
                    "id": int(row["id"]),
                    "task_id": row["task_id"],
                    "kind": row["kind"],
                    "payload": _parse_payload(row["payload"]),
                    "created_at": int(row["created_at"]),
                    "run_id": row["run_id"],
                }
                for row in all_rows
            ]
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    return high, low, all_events, tasks, run_rows


def _apply_prior_states(
    conn: sqlite3.Connection,
    holes: list[dict[str, Any]],
    *,
    board_slug: str,
    observed_at: int,
) -> list[dict[str, Any]]:
    """Apply durable NEW/PERSISTING/WORSENED/IMPROVED/RESOLVED states."""
    cutoff = observed_at - FINDING_RETENTION_SECONDS
    prior_rows = conn.execute(
        "SELECT finding_key, severity, evidence_state, state, first_observed_at, "
        "last_observed_at, report_json FROM telemetry_review_findings "
        "WHERE board_slug = ? AND last_observed_at >= ?",
        (board_slug, cutoff),
    ).fetchall()
    prior = {row["finding_key"]: row for row in prior_rows}
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    active_keys: set[str] = set()
    for hole in holes:
        key = hole["finding_key"]
        active_keys.add(key)
        old = prior.get(key)
        if old is None or old["state"] in {"RESOLVED", "EXCLUDED"}:
            state = "NEW"
            first = observed_at
        else:
            old_rank = severity_order.get(old["severity"], 99)
            new_rank = severity_order.get(hole["severity"], 99)
            if new_rank < old_rank:
                state = "WORSENED"
            elif new_rank > old_rank:
                state = "IMPROVED"
            else:
                state = "PERSISTING"
            first = int(old["first_observed_at"])
        hole["state"] = state
        hole["first_observed_at"] = _utc(first)
        hole["last_observed_at"] = _utc(observed_at)
        hole["duration_seconds"] = max(0, observed_at - first)
    for key, old in prior.items():
        if key in active_keys or old["state"] in {"RESOLVED", "EXCLUDED"}:
            continue
        try:
            report = json.loads(old["report_json"])
        except (TypeError, ValueError):
            continue
        report["state"] = "RESOLVED"
        report["last_observed_at"] = _utc(observed_at)
        report["duration_seconds"] = max(0, observed_at - int(old["first_observed_at"]))
        holes.append(report)
    return holes


def run_review(
    conn: sqlite3.Connection,
    *,
    board_slug: str,
    db_path: Path,
    window_end: Optional[int] = None,
    generated_at: Optional[int] = None,
) -> dict[str, Any]:
    end = int(window_end if window_end is not None else time.time())
    start = end - REVIEW_WINDOW_SECONDS
    generated = int(generated_at if generated_at is not None else time.time())
    high, low, events, tasks, runs = _capture_snapshot(conn, start=start, end=end)
    by_task: dict[str, list[dict[str, Any]]] = {}
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_task.setdefault(event["task_id"], []).append(event)
        by_kind.setdefault(event["kind"], []).append(event)

    holes: list[dict[str, Any]] = []
    included_ids = sorted(tasks)
    if included_ids:
        holes.append(_finding(
            "INSTRUMENTATION.STAGE_UNKNOWN", board_slug,
            _subject(task_ids=included_ids), severity="MEDIUM", evidence_state="UNKNOWN",
            title="Workflow stage telemetry is UNKNOWN", owner="arturo-gallo",
            recommendation="Introduce and prove typed workflow_stage_transition writers before claiming stage metrics.",
            evidence=[{"event_ids": [], "query_id": "Q-STAGE-01", "fact": "No status-to-stage inference was performed."}],
            observed_at=start,
        ))
    haa_requests = {event["payload"].get("decision_id") for event in by_kind.get("haa_decision_requested", [])}
    haa_records = {event["payload"].get("decision_id") for event in by_kind.get("haa_decision_recorded", [])}
    haa_instrumented = bool(haa_requests and haa_requests & haa_records)
    if not haa_instrumented:
        holes.append(_finding(
            "INSTRUMENTATION.HAA_UNINSTRUMENTED", board_slug, _subject(), severity="HIGH",
            evidence_state="UNINSTRUMENTED", title="HAA decision telemetry: UNINSTRUMENTED",
            owner="arturo-gallo", recommendation="Use paired haa_decision_requested and haa_decision_recorded events at the decision surface.",
            evidence=[{"event_ids": [], "query_id": "Q-HAA-01", "fact": "A complete typed request/record pair is unavailable."}],
            observed_at=start, next_expected_event="haa_decision_requested",
        ))
    estimate_instrumented = bool(by_kind.get("estimate_observed"))
    if not estimate_instrumented:
        holes.append(_finding(
            "INSTRUMENTATION.ESTIMATE_UNKNOWN", board_slug, _subject(profiles=["paul-park"]), severity="HIGH",
            evidence_state="UNINSTRUMENTED", title="CPTC estimate coverage: UNKNOWN (UNINSTRUMENTED)",
            owner="paul-park", recommendation="Add the read-only Linear estimate observer; preserve UNKNOWN on API failure.",
            evidence=[{"event_ids": [], "query_id": "Q-EST-01", "fact": "No estimate_observed event exists in the review window."}],
            observed_at=start, next_expected_event="estimate_observed",
        ))

    for intake in by_kind.get("intake_received", []):
        task_events = by_task.get(intake["task_id"], [])
        payload = intake["payload"]
        required = ("source_type", "source_ref_hash", "idempotency_key", "received_by")
        if any(not payload.get(key) for key in required):
            holes.append(_finding(
                "INTAKE.NO_PROVENANCE", board_slug, _subject(task_ids=[intake["task_id"]]), severity="HIGH",
                evidence_state="MEASURED", title="Raw intake is missing durable provenance",
                owner=payload.get("owner") or payload.get("actor") or "OWNER.UNRESOLVED",
                recommendation="Re-emit intake_received through the governed intake adapter with hash and idempotency.",
                evidence=[{"event_ids": [intake["id"]], "query_id": "Q-INTAKE-01", "fact": "Required provenance fields are absent."}], observed_at=intake["created_at"],
            ))
        if end - intake["created_at"] >= INTAKE_DECISION_SECONDS and not any(e["kind"] == "intake_classified" for e in task_events):
            holes.append(_finding(
                "INTAKE.NOT_CLASSIFIED", board_slug, _subject(task_ids=[intake["task_id"]], profiles=["paul-park"]), severity="HIGH",
                evidence_state="MEASURED", title="Intake was not classified within 30 minutes", owner="paul-park",
                recommendation="Repair the existing auto-decomposer classification writer.", evidence=[{"event_ids": [intake["id"]], "query_id": "Q-INTAKE-02", "fact": "No intake_classified event followed intake_received."}], observed_at=intake["created_at"], next_expected_event="intake_classified",
            ))
        if end - intake["created_at"] >= INTAKE_DECISION_SECONDS and not any(e["kind"] == "decomposition_decided" for e in task_events):
            holes.append(_finding(
                "INTAKE.NOT_DECOMPOSED", board_slug, _subject(task_ids=[intake["task_id"]], profiles=["paul-park"]), severity="HIGH",
                evidence_state="MEASURED", title="Intake was not decomposed within 30 minutes", owner="paul-park",
                recommendation="Repair the existing auto-decomposer decision writer.", evidence=[{"event_ids": [intake["id"]], "query_id": "Q-INTAKE-03", "fact": "No decomposition_decided event followed intake_received."}], observed_at=intake["created_at"], next_expected_event="decomposition_decided",
            ))

    accepted = {event["payload"].get("handoff_id") for event in by_kind.get("handoff_accepted", [])}
    for emitted in by_kind.get("handoff_emitted", []):
        due = emitted["payload"].get("due_by")
        if isinstance(due, (int, float)) and due < end and emitted["payload"].get("handoff_id") not in accepted:
            holes.append(_finding(
                "HANDOFF.NOT_ACCEPTED", board_slug, _subject(task_ids=[emitted["task_id"]], profiles=[emitted["payload"].get("to_owner")]), severity="HIGH",
                evidence_state="MEASURED", title="Receiver did not accept producer handoff", owner=emitted["payload"].get("to_owner") or "OWNER.UNRESOLVED",
                recommendation="Have the receiver emit handoff_accepted or repair routing to that receiver.", evidence=[{"event_ids": [emitted["id"]], "query_id": "Q-HANDOFF-01", "fact": "The typed handoff is overdue without acceptance."}], observed_at=emitted["created_at"], next_expected_event="handoff_accepted", due_by=int(due),
            ))

    decided_gates = {event["payload"].get("gate_id") for event in by_kind.get("gate_decided", [])}
    for gate in by_kind.get("gate_required", []):
        if gate["payload"].get("gate_id") not in decided_gates:
            holes.append(_finding(
                "GATE.REQUIRED_NOT_DECIDED", board_slug, _subject(task_ids=[gate["task_id"]], profiles=[gate["payload"].get("authority")], refs=[gate["payload"].get("candidate_ref")]), severity="HIGH",
                evidence_state="MEASURED", title="Required gate has no typed decision", owner=gate["payload"].get("authority") or "OWNER.UNRESOLVED",
                recommendation="Render the complete gate packet and emit gate_decided for the exact candidate version.", evidence=[{"event_ids": [gate["id"]], "query_id": "Q-GATE-01", "fact": "gate_required has no matching gate_decided."}], observed_at=gate["created_at"], next_expected_event="gate_decided",
            ))

    for run in runs:
        heartbeat = run["last_heartbeat_at"] or run["started_at"]
        if run["status"] == "running" and end - int(heartbeat) >= RUNNING_INACTIVITY_SECONDS:
            holes.append(_finding(
                "STALL.RUNNING_NO_ACTIVITY", board_slug, _subject(task_ids=[run["task_id"]], run_ids=[run["id"]], profiles=[run["profile"]]), severity="HIGH",
                evidence_state="MEASURED", title="Running task has no activity for 60 minutes", owner=run["profile"] or "OWNER.UNRESOLVED",
                recommendation="Emit durable progress activity or repair the worker heartbeat path.", evidence=[{"event_ids": [], "query_id": "Q-RUN-01", "fact": "The active run has no recent heartbeat."}], observed_at=int(heartbeat),
            ))
    for task_id, task in tasks.items():
        if task["status"] == "blocked" and (not task["block_kind"] or not task["assignee"]):
            holes.append(_finding(
                "STALL.BLOCKED_NO_OWNER", board_slug, _subject(task_ids=[task_id], profiles=[task["assignee"]]), severity="HIGH",
                evidence_state="MEASURED", title="Blocked task lacks typed ownership", owner=task["assignee"] or "OWNER.UNRESOLVED",
                recommendation="Record a typed block kind, accountable owner, and next action at the block boundary.", evidence=[{"event_ids": [], "query_id": "Q-BLOCK-01", "fact": "Blocked task has no typed block owner."}], observed_at=end,
            ))

    holes = _apply_prior_states(conn, holes, board_slug=board_slug, observed_at=end)
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    holes.sort(key=lambda hole: (0 if hole["state"] != "RESOLVED" else 1, severity_order.get(hole["severity"], 99), hole["rule_id"], hole["finding_key"]))
    summary = {name.lower(): sum(1 for hole in holes if hole["severity"] == name and hole["state"] != "RESOLVED") for name in severity_order}
    summary.update({
        "unknown_domains": sum(1 for hole in holes if hole["evidence_state"] in {"UNKNOWN", "UNINSTRUMENTED"}),
        "new": sum(1 for hole in holes if hole["state"] == "NEW"),
        "persisting": sum(1 for hole in holes if hole["state"] in {"PERSISTING", "WORSENED", "IMPROVED"}),
        "resolved": sum(1 for hole in holes if hole["state"] == "RESOLVED"),
    })
    return {
        "schema_version": 1,
        "review": {
            "review_id": f"trc-hole-review:{board_slug}:{end}",
            "board_slug": board_slug,
            "db_path_fingerprint": hashlib.sha256(str(db_path.resolve()).encode()).hexdigest(),
            "window_start_utc": _utc(start),
            "window_end_utc": _utc(end),
            "event_id_low_exclusive": low,
            "event_id_high_inclusive": high,
            "source_max_event_id_at_start": high,
            "rule_set_version": RULE_SET_VERSION,
            "implementation_sha": _implementation_sha(),
            "generated_at_utc": _utc(generated),
            "status": "COMPLETE",
        },
        "instrumentation": {
            "lifecycle": "MEASURED",
            "workflow_stages": "UNKNOWN",
            "haa_decisions": "MEASURED" if haa_instrumented else "UNINSTRUMENTED",
            "linear_estimates": "MEASURED" if estimate_instrumented else "UNINSTRUMENTED",
            "idle_capacity": "UNKNOWN",
        },
        "summary": summary,
        "holes": holes,
        "council": {"trc_convener": "tessa-cole", "guardian_attendees": [], "waivers": [], "conflicts_resolved": [], "verdict": "GO-WITH-CHANGES" if any(h["state"] != "RESOLVED" for h in holes) else "GO"},
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# TRC 48-Hour Telemetry Hole Review", "", f"Verdict: {report['council']['verdict']}", ""]
    active = [hole for hole in report["holes"] if hole["state"] != "RESOLVED"]
    if active:
        top = active[0]
        lines.extend([f"Top priority: [{top['severity']}] {top['title']}", ""])
    for heading, predicate in (("New and worsened holes", lambda h: h["state"] in {"NEW", "WORSENED"}), ("Persisting holes", lambda h: h["state"] in {"PERSISTING", "IMPROVED"}), ("Resolved holes", lambda h: h["state"] == "RESOLVED")):
        selected = [hole for hole in report["holes"] if predicate(hole)]
        lines.extend([f"## {heading}", ""])
        for hole in selected:
            lines.extend([f"### {hole['rule_id']}", f"- State: {hole['state']}", f"- Severity: {hole['severity']}", f"- Evidence state: {hole['evidence_state']}", f"- Owner: {hole['owner']}", f"- Recommendation: {hole['recommendation']}", f"- Finding key: {hole['finding_key']}", ""])
        if not selected:
            lines.extend(["None.", ""])
    lines.extend(["## UNKNOWN and UNINSTRUMENTED domains", "", f"- Workflow stages: {report['instrumentation']['workflow_stages']}", f"- HAA decisions: {report['instrumentation']['haa_decisions']}", f"- Linear estimates: {report['instrumentation']['linear_estimates']}", f"- Idle capacity: {report['instrumentation']['idle_capacity']}", "", "## Evidence watermark", f"- Window: [{report['review']['window_start_utc']}, {report['review']['window_end_utc']})", f"- Event IDs: ({report['review']['event_id_low_exclusive']}, {report['review']['event_id_high_inclusive']}]", f"- Implementation: {report['review']['implementation_sha']}", ""])
    return "\n".join(lines)


def write_artifacts(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    end_token = report["review"]["window_end_utc"].replace(":", "").replace("-", "")
    json_path = output_dir / f"hole-review-{end_token}.json"
    md_path = output_dir / f"hole-review-{end_token}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def persist_review(conn: sqlite3.Connection, report: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    """Persist the run watermark and finding observations after artifact writes."""
    from hermes_cli import kanban_db as kb

    review = report["review"]
    with kb.write_txn(conn):
        conn.execute(
            "INSERT OR REPLACE INTO telemetry_review_runs "
            "(review_id, board_slug, window_end, event_id_low_exclusive, event_id_high_inclusive, generated_at, status, json_path, markdown_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (review["review_id"], review["board_slug"], int(datetime.fromisoformat(review["window_end_utc"].replace("Z", "+00:00")).timestamp()), review["event_id_low_exclusive"], review["event_id_high_inclusive"], int(datetime.fromisoformat(review["generated_at_utc"].replace("Z", "+00:00")).timestamp()), review["status"], str(json_path), str(markdown_path)),
        )
        for hole in report["holes"]:
            first = int(datetime.fromisoformat(hole["first_observed_at"].replace("Z", "+00:00")).timestamp())
            last = int(datetime.fromisoformat(hole["last_observed_at"].replace("Z", "+00:00")).timestamp())
            conn.execute(
                "INSERT OR REPLACE INTO telemetry_review_findings "
                "(finding_key, rule_id, board_slug, subject_json, first_observed_at, last_observed_at, severity, evidence_state, state, report_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (hole["finding_key"], hole["rule_id"], review["board_slug"], json.dumps(hole["subject"], sort_keys=True), first, last, hole["severity"], hole["evidence_state"], hole["state"], json.dumps(hole, sort_keys=True)),
            )
