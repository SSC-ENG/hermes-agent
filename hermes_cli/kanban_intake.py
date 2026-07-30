"""Governed raw-work intake for the Kanban triage column.

This module deliberately normalizes input only. Classification and routing remain
owned by ``kanban_decompose`` and the gateway's existing auto-decomposer.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from hermes_cli import kanban_db as kb

_LINEAR_URL_RE = re.compile(r"https?://(?:www\.)?linear\.app/[^\s]+/issue/[A-Za-z][A-Za-z0-9_-]*-\d+(?:/[^\s]*)?", re.I)
_URL_RE = re.compile(r"https?://[^\s]+", re.I)


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, parsed.fragment))


def normalize_raw_ref(text: str) -> str:
    """Normalize raw text for stable idempotency without changing stored input."""
    lines = [line.strip() for line in (text or "").splitlines()]
    return "\n".join(lines).strip()


def source_type(text: str, files: Iterable[Path]) -> str:
    paths = list(files)
    if paths and text.strip():
        return "mixed"
    if paths:
        return "document_path"
    normalized = normalize_raw_ref(text)
    if _LINEAR_URL_RE.fullmatch(normalized):
        return "linear_url"
    lines = [line for line in normalized.splitlines() if line]
    if len(lines) > 1 and all(_URL_RE.fullmatch(line) for line in lines):
        return "url_pile"
    return "paragraph"


def raw_hash(text: str, files: Iterable[Path]) -> str:
    paths = list(files)
    digest = hashlib.sha256()
    if paths:
        for path in paths:
            digest.update(path.read_bytes())
    else:
        normalized = normalize_raw_ref(text)
        if all(_URL_RE.fullmatch(line) for line in normalized.splitlines() if line):
            normalized = "\n".join(_normalize_url(line) for line in normalized.splitlines())
        digest.update(normalized.encode("utf-8"))
    return digest.hexdigest()


def idempotency_key(kind: str, text: str, files: Iterable[Path]) -> str:
    paths = list(files)
    if paths:
        ref = raw_hash(text, paths)
    else:
        ref = normalize_raw_ref(text)
        if kind in {"linear_url", "url_pile"}:
            ref = "\n".join(_normalize_url(line) for line in ref.splitlines())
    return hashlib.sha256(f"{kind}\n{ref}".encode("utf-8")).hexdigest()


def build_envelope(*, kind: str, raw_ref_sha256: str, received_by: str, text: str, attachment_ids: list[int]) -> str:
    received_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    header = "\n".join([
        "---",
        f"intake_source_type: {kind}",
        f"intake_raw_ref_sha256: {raw_ref_sha256}",
        f"intake_received_at: {received_at}",
        f"intake_received_by: {received_by}",
        f"intake_attachment_ids: {attachment_ids}",
        "---",
    ])
    return f"{header}\n{ text }" if text else f"{header}\n"


def receive(
    conn,
    *,
    text: str = "",
    files: Iterable[Path] = (),
    received_by: str = "haa",
    title: Optional[str] = None,
    priority: int = 0,
    tenant: Optional[str] = None,
    board: Optional[str] = None,
) -> tuple[str, bool]:
    paths = [Path(p).expanduser() for p in files]
    for path in paths:
        if not path.is_file():
            raise ValueError(f"intake file does not exist: {path}")
    if not text.strip() and not paths:
        raise ValueError("intake requires --text and/or --file")
    kind = source_type(text, paths)
    digest = raw_hash(text, paths)
    key = idempotency_key(kind, text, paths)
    title = title or (f"Raw intake: {kind}")
    existing_before = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' LIMIT 1", (key,)
    ).fetchone()
    task_id = kb.create_task(
        conn, title=title, body=build_envelope(
            kind=kind, raw_ref_sha256=digest, received_by=received_by,
            text=text, attachment_ids=[],
        ), created_by=received_by, priority=priority, tenant=tenant,
        triage=True, idempotency_key=key,
    )
    created = existing_before is None
    attachment_ids: list[int] = []
    for path in paths:
        attachment_ids.append(kb.store_attachment_bytes(
            conn, task_id, path.name, path.read_bytes(),
            content_type=None, uploaded_by=received_by, board=board,
        ))
    if created:
        envelope = build_envelope(
            kind=kind, raw_ref_sha256=digest, received_by=received_by,
            text=text, attachment_ids=attachment_ids,
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (envelope, task_id))
            kb._append_event(conn, task_id, "intake_received", {
                "schema_version": 1,
                "actor": received_by,
                "source": "cli",
                "correlation_id": key,
                "intake_id": task_id,
                "source_type": kind,
                "source_ref_hash": digest,
                "raw_ref_sha256": digest,
                "provenance_refs": attachment_ids,
                "attachment_ids": attachment_ids,
                "idempotency_key": key,
                "received_by": received_by,
            })
    return task_id, created
