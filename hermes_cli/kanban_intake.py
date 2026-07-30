"""Canonical raw-work envelope for the existing Kanban triage/decomposer path."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from hermes_cli import kanban_db as kb

_PREFIX = "INTAKE-ENVELOPE v1\n"
_SUFFIX = "\nEND-INTAKE-ENVELOPE"
_SENSITIVITIES = frozenset({"public", "internal", "confidential", "restricted"})
_LINEAR_URL_RE = re.compile(r"https?://(?:www\.)?linear\.app/[^\s]+/issue/[A-Za-z][A-Za-z0-9_-]*-\d+(?:/[^\s]*)?", re.I)
_URL_RE = re.compile(r"https?://[^\s]+", re.I)


@dataclass(frozen=True)
class IntakeEnvelope:
    source: str
    items: tuple[str, ...]
    notes: str
    attachment_refs: tuple[str, ...]
    content_digest: str
    tenant_domain: str
    sensitivity: str
    idempotency_key: str

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "items": list(self.items),
            "notes": self.notes,
            "attachment_refs": list(self.attachment_refs),
            "content_digest": self.content_digest,
            "tenant_domain": self.tenant_domain,
            "sensitivity": self.sensitivity,
            "idempotency_key": self.idempotency_key,
        }


def _clean_list(values: Iterable[str], *, field: str, required: bool = False) -> tuple[str, ...]:
    cleaned = tuple(str(value).strip() for value in values if str(value).strip())
    if required and not cleaned:
        raise ValueError(f"intake envelope {field} must contain at least one value")
    return cleaned


def _digest_payload(
    *,
    source: str,
    items: tuple[str, ...],
    notes: str,
    attachment_refs: tuple[str, ...],
    tenant_domain: str,
    sensitivity: str,
) -> str:
    payload = {
        "attachment_refs": list(attachment_refs),
        "items": list(items),
        "notes": notes,
        "sensitivity": sensitivity,
        "source": source,
        "tenant_domain": tenant_domain,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_envelope(
    *,
    source: str,
    items: Iterable[str],
    notes: str = "",
    attachment_refs: Iterable[str] = (),
    tenant_domain: str,
    sensitivity: str = "internal",
    idempotency_key: Optional[str] = None,
) -> IntakeEnvelope:
    source = str(source).strip()
    tenant_domain = str(tenant_domain).strip()
    sensitivity = str(sensitivity).strip().lower()
    if not source:
        raise ValueError("intake envelope source is required")
    if not tenant_domain:
        raise ValueError("intake envelope tenant_domain is required")
    if sensitivity not in _SENSITIVITIES:
        raise ValueError(
            f"intake envelope sensitivity must be one of {sorted(_SENSITIVITIES)}"
        )
    clean_items = _clean_list(items, field="items", required=True)
    clean_refs = _clean_list(attachment_refs, field="attachment_refs")
    clean_notes = str(notes or "").strip()
    digest = _digest_payload(
        source=source,
        items=clean_items,
        notes=clean_notes,
        attachment_refs=clean_refs,
        tenant_domain=tenant_domain,
        sensitivity=sensitivity,
    )
    key = str(idempotency_key or digest).strip()
    if not key:
        raise ValueError("intake envelope idempotency_key is required")
    return IntakeEnvelope(
        source=source,
        items=clean_items,
        notes=clean_notes,
        attachment_refs=clean_refs,
        content_digest=digest,
        tenant_domain=tenant_domain,
        sensitivity=sensitivity,
        idempotency_key=key,
    )


def render_envelope(envelope: IntakeEnvelope) -> str:
    payload = json.dumps(envelope.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    return f"{_PREFIX}{payload}{_SUFFIX}"


def parse_envelope(body: Optional[str]) -> Optional[IntakeEnvelope]:
    text = body or ""
    start = text.find(_PREFIX)
    if start < 0:
        return None
    payload_start = start + len(_PREFIX)
    end = text.find(_SUFFIX, payload_start)
    if end < 0:
        raise ValueError("intake envelope is missing END-INTAKE-ENVELOPE")
    try:
        raw = json.loads(text[payload_start:end])
    except json.JSONDecodeError as exc:
        raise ValueError(f"intake envelope JSON is invalid: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ValueError("intake envelope payload must be an object")
    supplied_digest = str(raw.get("content_digest") or "").strip()
    supplied_key = str(raw.get("idempotency_key") or "").strip()
    raw_items = raw.get("items")
    raw_attachment_refs = raw.get("attachment_refs")
    envelope = build_envelope(
        source=raw.get("source") or "",
        items=raw_items if isinstance(raw_items, list) else (),
        notes=raw.get("notes") or "",
        attachment_refs=(
            raw_attachment_refs if isinstance(raw_attachment_refs, list) else ()
        ),
        tenant_domain=raw.get("tenant_domain") or "",
        sensitivity=raw.get("sensitivity") or "",
        idempotency_key=supplied_key,
    )
    if supplied_digest != envelope.content_digest:
        raise ValueError("intake envelope content_digest does not match canonical content")
    if not supplied_key:
        raise ValueError("intake envelope idempotency_key is required")
    return envelope


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, parsed.fragment))


def normalize_raw_ref(text: str) -> str:
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


def _legacy_envelope(*, kind: str, raw_ref_sha256: str, received_by: str, text: str, attachment_ids: list[int]) -> str:
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
    return f"{header}\n{text}" if text else f"{header}\n"


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
    """Create a canonical governed intake and preserve any attachments."""
    paths = [Path(path).expanduser() for path in files]
    for path in paths:
        if not path.is_file():
            raise ValueError(f"intake file does not exist: {path}")
    if not text.strip() and not paths:
        raise ValueError("intake requires --text and/or --file")
    kind = source_type(text, paths)
    key = idempotency_key(kind, text, paths)
    title = title or f"Raw intake: {kind}"
    normalized_text = normalize_raw_ref(text)
    if kind == "url_pile":
        source_items = [
            _normalize_url(line)
            for line in normalized_text.splitlines()
            if line
        ]
    elif kind == "linear_url":
        source_items = [_normalize_url(normalized_text)]
    else:
        source_items = [normalized_text] if normalized_text else []
    file_refs = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    source_items.extend(f"attachment-sha256:{digest}" for digest in file_refs)
    envelope = build_envelope(
        source=f"cli:{received_by}",
        items=source_items,
        attachment_refs=[f"sha256:{digest}" for digest in file_refs],
        tenant_domain=tenant or "unassigned",
        sensitivity="internal",
        idempotency_key=key,
    )
    body = render_envelope(envelope)
    task_id, created = kb.create_governed_intake_task(
        conn,
        title=title,
        body=body,
        tenant=envelope.tenant_domain,
        content_digest=envelope.content_digest,
        idempotency_key=envelope.idempotency_key,
        created_by=received_by,
        priority=priority,
    )
    if created:
        attachment_ids = [
            kb.store_attachment_bytes(
                conn,
                task_id,
                path.name,
                path.read_bytes(),
                content_type=None,
                uploaded_by=received_by,
                board=board,
            )
            for path in paths
        ]
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "intake_received", {
                "schema_version": 1,
                "actor": received_by,
                "source": "cli",
                "correlation_id": key,
                "intake_id": task_id,
                "source_type": kind,
                "source_ref_hash": envelope.content_digest,
                "raw_ref_sha256": envelope.content_digest,
                "provenance_refs": attachment_ids,
                "attachment_ids": attachment_ids,
                "idempotency_key": key,
                "received_by": received_by,
            })
    return task_id, created
