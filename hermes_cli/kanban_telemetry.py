"""Append-only governed Kanban telemetry and deterministic 48-hour hole review."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

RULE_SET_VERSION = "1.0.0"
REVIEW_WINDOW_SECONDS = 48 * 60 * 60
INTAKE_DECISION_SECONDS = 30 * 60

_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "linear_scoped": ("linear_issue_key", "sub_issue_keys", "cptc_estimates"),
    "intake_classified": ("intake_id", "domain", "classification"),
    "routing_decided": ("task_id", "requested_assignee", "resolved_assignee", "fallback_used", "profile_exists"),
    "decomposition_decided": ("root_task_id", "fanout", "child_ids", "dependency_edges"),
    "handoff_emitted": ("handoff_id", "from_owner", "to_owner", "artifact_refs", "acceptance_contract", "next_expected_event", "due_by"),
    "handoff_accepted": ("handoff_id", "receiver", "run_id"),
    "gate_required": ("gate_id", "gate_type", "authority", "candidate_ref", "candidate_version"),
    "gate_decided": ("gate_id", "gate_type", "authority", "verdict", "candidate_ref", "candidate_version"),
    "haa_decision_requested": ("decision_id", "prompt_ref", "requester", "options", "irreversible", "requested_at"),
    "haa_decision_recorded": ("decision_id", "decision", "decided_at", "evidence_ref", "recorder"),
    "estimate_observed": ("issue_id", "issue_role", "estimate", "unit", "observed_at", "linear_updated_at"),
    "review_disposition": ("candidate_ref", "candidate_version", "verdict", "next_owner", "next_expected_event"),
    "merge_disposition": ("pr_ref", "candidate_version", "state", "owner"),
    "infrastructure_exclusion": ("finding_key", "rhea_task_ref", "reason_code", "expires_at"),
}


def validate_event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
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


def record_event(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict[str, Any], *, run_id: Optional[int] = None) -> None:
    from hermes_cli import kanban_db as kb
    if kb.get_task(conn, task_id) is None:
        raise ValueError(f"unknown task {task_id}")
    normalized = validate_event(kind, payload)
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, kind, normalized, run_id=run_id)


def _utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _implementation_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        return result.stdout.strip() or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def _finding(rule_id: str, board: str, subject: dict[str, list], *, severity: str, evidence_state: str, title: str, owner: str, recommendation: str, evidence: list[dict], correlation_id: Optional[str] = None, next_expected_event: Optional[str] = None) -> dict[str, Any]:
    subject_ids = json.dumps(subject, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(f"{rule_id}|{subject_ids}|{correlation_id or ''}|{board}".encode()).hexdigest()
    return {
        "finding_key": key,
        "rule_id": rule_id,
        "state": "NEW",
        "severity": severity,
        "evidence_state": evidence_state,
        "title": title,
        "subject": subject,
        "evidence": evidence,
        "impact": title,
        "owner": owner,
        "recommendation": recommendation,
        "next_expected_event": next_expected_event,
        "rhea_exclusion": {"excluded": False, "task_ref": None, "reason_code": None},
        "lens_findings": [],
    }


def run_review(conn: sqlite3.Connection, *, board_slug: str, db_path: Path, window_end: Optional[int] = None, generated_at: Optional[int] = None) -> dict[str, Any]:
    end = int(window_end if window_end is not None else time.time())
    start = end - REVIEW_WINDOW_SECONDS
    generated = int(generated_at if generated_at is not None else time.time())
    conn.execute("BEGIN")
    try:
        high = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0])
        low_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE created_at < ? AND id <= ?",
            (start, high),
        ).fetchone()
        low = int(low_row[0])
        rows = conn.execute(
            "SELECT id, task_id, kind, payload, created_at, run_id FROM task_events "
            "WHERE id > ? AND id <= ? AND created_at >= ? AND created_at < ? ORDER BY id",
            (low, high, start, end),
        ).fetchall()
    finally:
        conn.execute("ROLLBACK")
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            payload = {}
        events.append({"id": int(row["id"]), "task_id": row["task_id"], "kind": row["kind"], "payload": payload, "created_at": int(row["created_at"]), "run_id": row["run_id"]})
    by_task: dict[str, list[dict[str, Any]]] = {}
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_task.setdefault(event["task_id"], []).append(event)
        by_kind.setdefault(event["kind"], []).append(event)

    holes: list[dict[str, Any]] = []
    holes.append(_finding("INSTRUMENTATION.STAGE_UNKNOWN", board_slug, {"task_ids": sorted(by_task), "run_ids": [], "profile_slugs": [], "external_refs": []}, severity="MEDIUM", evidence_state="UNKNOWN", title="Workflow stage telemetry is UNKNOWN", owner="arturo-gallo", recommendation="Introduce and prove typed workflow_stage_transition writers before claiming stage metrics.", evidence=[{"event_ids": [], "query_id": "Q-STAGE-01", "fact": "No status-to-stage inference was performed."}]))

    haa_instrumented = bool(by_kind.get("haa_decision_requested")) and bool(by_kind.get("haa_decision_recorded"))
    if not haa_instrumented:
        holes.append(_finding("INSTRUMENTATION.HAA_UNINSTRUMENTED", board_slug, {"task_ids": [], "run_ids": [], "profile_slugs": [], "external_refs": []}, severity="HIGH", evidence_state="UNINSTRUMENTED", title="HAA decision telemetry: UNINSTRUMENTED", owner="arturo-gallo", recommendation="Use paired haa_decision_requested and haa_decision_recorded events at the decision surface.", evidence=[{"event_ids": [], "query_id": "Q-HAA-01", "fact": "A complete typed request/record pair is unavailable."}], next_expected_event="haa_decision_requested"))

    estimate_instrumented = bool(by_kind.get("estimate_observed"))
    if not estimate_instrumented:
        holes.append(_finding("INSTRUMENTATION.ESTIMATE_UNKNOWN", board_slug, {"task_ids": [], "run_ids": [], "profile_slugs": ["paul-park"], "external_refs": []}, severity="HIGH", evidence_state="UNINSTRUMENTED", title="CPTC estimate coverage: UNKNOWN (UNINSTRUMENTED)", owner="paul-park", recommendation="Add the read-only Linear estimate observer; preserve UNKNOWN on API failure.", evidence=[{"event_ids": [], "query_id": "Q-EST-01", "fact": "No estimate_observed event exists in the review window."}], next_expected_event="estimate_observed"))

    for intake in by_kind.get("intake_received", []):
        task_events = by_task.get(intake["task_id"], [])
        payload = intake["payload"]
        required = ("source_type", "source_ref_hash", "idempotency_key", "received_by")
        if any(not payload.get(key) for key in required):
            holes.append(_finding("INTAKE.NO_PROVENANCE", board_slug, {"task_ids": [intake["task_id"]], "run_ids": [], "profile_slugs": [], "external_refs": []}, severity="HIGH", evidence_state="MEASURED", title="Raw intake is missing durable provenance", owner=payload.get("owner") or payload.get("actor") or "arturo-gallo", recommendation="Re-emit intake_received through the governed intake adapter with hash and idempotency.", evidence=[{"event_ids": [intake["id"]], "query_id": "Q-INTAKE-01", "fact": "Required provenance fields are absent."}]))
        if end - intake["created_at"] >= INTAKE_DECISION_SECONDS and not any(e["kind"] == "intake_classified" for e in task_events):
            holes.append(_finding("INTAKE.NOT_CLASSIFIED", board_slug, {"task_ids": [intake["task_id"]], "run_ids": [], "profile_slugs": ["paul-park"], "external_refs": []}, severity="HIGH", evidence_state="MEASURED", title="Intake was not classified within 30 minutes", owner="paul-park", recommendation="Repair the existing auto-decomposer classification writer.", evidence=[{"event_ids": [intake["id"]], "query_id": "Q-INTAKE-02", "fact": "No intake_classified event followed intake_received."}], next_expected_event="intake_classified"))
        if end - intake["created_at"] >= INTAKE_DECISION_SECONDS and not any(e["kind"] == "decomposition_decided" for e in task_events):
            holes.append(_finding("INTAKE.NOT_DECOMPOSED", board_slug, {"task_ids": [intake["task_id"]], "run_ids": [], "profile_slugs": ["paul-park"], "external_refs": []}, severity="HIGH", evidence_state="MEASURED", title="Intake was not decomposed within 30 minutes", owner="paul-park", recommendation="Repair the existing auto-decomposer decision writer.", evidence=[{"event_ids": [intake["id"]], "query_id": "Q-INTAKE-03", "fact": "No decomposition_decided event followed intake_received."}], next_expected_event="decomposition_decided"))

    accepted = {e["payload"].get("handoff_id") for e in by_kind.get("handoff_accepted", [])}
    for emitted in by_kind.get("handoff_emitted", []):
        due = emitted["payload"].get("due_by")
        overdue = isinstance(due, (int, float)) and due < end
        if overdue and emitted["payload"].get("handoff_id") not in accepted:
            holes.append(_finding("HANDOFF.NOT_ACCEPTED", board_slug, {"task_ids": [emitted["task_id"]], "run_ids": [], "profile_slugs": [emitted["payload"].get("to_owner")], "external_refs": []}, severity="HIGH", evidence_state="MEASURED", title="Receiver did not accept producer handoff", owner=emitted["payload"].get("to_owner") or "arturo-gallo", recommendation="Have the receiver emit handoff_accepted or repair routing to that receiver.", evidence=[{"event_ids": [emitted["id"]], "query_id": "Q-HANDOFF-01", "fact": "The typed handoff is overdue without acceptance."}], next_expected_event="handoff_accepted"))

    decided_gates = {e["payload"].get("gate_id") for e in by_kind.get("gate_decided", [])}
    for gate in by_kind.get("gate_required", []):
        if gate["payload"].get("gate_id") not in decided_gates:
            holes.append(_finding("GATE.REQUIRED_NOT_DECIDED", board_slug, {"task_ids": [gate["task_id"]], "run_ids": [], "profile_slugs": [gate["payload"].get("authority")], "external_refs": [gate["payload"].get("candidate_ref")]}, severity="HIGH", evidence_state="MEASURED", title="Required gate has no typed decision", owner=gate["payload"].get("authority") or "arturo-gallo", recommendation="Render the complete gate packet and emit gate_decided for the exact candidate version.", evidence=[{"event_ids": [gate["id"]], "query_id": "Q-GATE-01", "fact": "gate_required has no matching gate_decided."}], next_expected_event="gate_decided"))

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    holes.sort(key=lambda h: (severity_order[h["severity"]], h["rule_id"], h["finding_key"]))
    summary = {name.lower(): sum(1 for h in holes if h["severity"] == name) for name in severity_order}
    summary.update({"unknown_domains": sum(1 for h in holes if h["evidence_state"] in {"UNKNOWN", "UNINSTRUMENTED"}), "new": len(holes), "persisting": 0, "resolved": 0})
    return {
        "schema_version": 1,
        "review": {"review_id": f"trc-hole-review:{board_slug}:{end}", "board_slug": board_slug, "db_path_fingerprint": hashlib.sha256(str(db_path.resolve()).encode()).hexdigest(), "window_start_utc": _utc(start), "window_end_utc": _utc(end), "event_id_low_exclusive": low, "event_id_high_inclusive": high, "source_max_event_id_at_start": high, "rule_set_version": RULE_SET_VERSION, "implementation_sha": _implementation_sha(), "generated_at_utc": _utc(generated), "status": "COMPLETE"},
        "instrumentation": {"lifecycle": "MEASURED", "workflow_stages": "UNKNOWN", "haa_decisions": "MEASURED" if haa_instrumented else "UNINSTRUMENTED", "linear_estimates": "MEASURED" if estimate_instrumented else "UNINSTRUMENTED", "idle_capacity": "UNKNOWN"},
        "summary": summary,
        "holes": holes,
        "council": {"trc_convener": "tessa-cole", "guardian_attendees": [], "waivers": [], "conflicts_resolved": [], "verdict": "GO-WITH-CHANGES" if holes else "GO"},
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# TRC 48-Hour Telemetry Hole Review", "", f"Verdict: {report['council']['verdict']}", ""]
    if report["holes"]:
        top = report["holes"][0]
        lines.extend([f"Top priority: [{top['severity']}] {top['title']}", ""])
    for hole in report["holes"]:
        lines.extend([f"## {hole['rule_id']}", f"- Severity: {hole['severity']}", f"- Evidence state: {hole['evidence_state']}", f"- Owner: {hole['owner']}", f"- Recommendation: {hole['recommendation']}", f"- Finding key: {hole['finding_key']}", ""])
    review = report["review"]
    lines.extend(["## Evidence watermark", f"- Window: [{review['window_start_utc']}, {review['window_end_utc']})", f"- Event IDs: ({review['event_id_low_exclusive']}, {review['event_id_high_inclusive']}]", f"- Implementation: {review['implementation_sha']}"])
    return "\n".join(lines) + "\n"


def write_artifacts(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    end_token = report["review"]["window_end_utc"].replace(":", "").replace("-", "")
    json_path = output_dir / f"hole-review-{end_token}.json"
    md_path = output_dir / f"hole-review-{end_token}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path
