"""Capture record schema and payload validation.

A capture record is the on-disk shape of one captured agent turn. The HTTP
endpoint accepts a Stop-shaped or SubagentStop-shaped JSON payload (the two
are distinguished by the required top-level ``event_type`` discriminator),
validates the required fields, and ``build_record`` produces the final dict
that gets serialized to JSONL (with ``id`` and ``ingest_time`` stamped by
the daemon).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from palace.daemons.capture.ids import compute_record_id

__all__ = [
    "ALLOWED_EVENT_TYPES",
    "ALLOWED_HARNESSES",
    "EVENT_STOP",
    "EVENT_SUBAGENT_STOP",
    "REQUIRED_FIELDS",
    "CaptureRecord",
    "PayloadError",
    "build_record",
    "to_jsonl_bytes",
    "validate_payload",
]


ALLOWED_HARNESSES: frozenset[str] = frozenset({"claude-code", "codex"})

EVENT_STOP: str = "stop"
EVENT_SUBAGENT_STOP: str = "subagent_stop"
ALLOWED_EVENT_TYPES: frozenset[str] = frozenset({EVENT_STOP, EVENT_SUBAGENT_STOP})

# Fields required on every inbound payload regardless of ``event_type``.
# ``event_type`` leads because it routes the rest of validation.
REQUIRED_FIELDS: tuple[str, ...] = ("event_type", "session_id", "harness")

# Additional fields required when ``event_type == "subagent_stop"``. These
# mirror Claude Code's SubagentStop hook payload field names verbatim — no
# palace-internal aliases.
_SUBAGENT_REQUIRED_FIELDS: tuple[str, ...] = ("agent_id", "agent_type")


class PayloadError(Exception):
    """Raised when an inbound payload fails validation.

    Carries an HTTP status code and a single-line message so the server can
    surface a clean JSON error without leaking a traceback.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class CaptureRecord:
    """One captured agent turn, exactly as it lands on disk.

    The field set matches ``briefs/sota-memory-and-recall.md`` §B.4
    "Conversational capture" plus the ``harness`` and ``event_type``
    discriminators the parent and 1.2 sub-phase require. SubagentStop
    records additionally carry ``agent_id`` and ``agent_type`` (parent
    linkage), an optional ``effort`` pass-through, and a reserved
    ``parent_transcript_path`` placeholder for Claude Code's future
    "Expose Agent Context" feature. ``id`` and ``ingest_time`` are
    daemon-set; everything else comes from the payload.
    """

    id: str
    ingest_time: str
    event_type: str
    harness: str
    session_id: str
    agent_id: str | None = None
    agent_type: str | None = None
    parent_transcript_path: str | None = None
    effort: dict[str, Any] | None = None
    transcript_path: str | None = None
    cwd: str | None = None
    assistant_message: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    files_touched: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    timing: dict[str, Any] = field(default_factory=dict)
    token_usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_session_id(s: Any) -> str:
    """Reject ``session_id`` values that could traverse the filesystem.

    Resolves Open Question #5 from the approved Phase 1.1 plan: ``/``,
    ``\\``, ``..``, and null bytes are rejected with a 400.
    """
    if not isinstance(s, str) or not s:
        raise PayloadError(status=400, message="invalid session_id")
    if "/" in s or "\\" in s or ".." in s or "\x00" in s:
        raise PayloadError(status=400, message="invalid session_id")
    return s


def _require_non_empty_string(payload: dict[str, Any], name: str, *, context: str) -> None:
    if name not in payload:
        raise PayloadError(
            status=400,
            message=f"missing required field for {context}: {name}",
        )
    value = payload[name]
    if not isinstance(value, str) or not value:
        raise PayloadError(
            status=400,
            message=f"invalid {name}: must be a non-empty string",
        )


def validate_payload(payload: Any) -> dict[str, Any]:
    """Return a payload dict that has the required fields, or raise PayloadError."""
    if not isinstance(payload, dict):
        raise PayloadError(status=400, message="payload must be a JSON object")
    for required in REQUIRED_FIELDS:
        if required not in payload:
            raise PayloadError(status=400, message=f"missing required field: {required}")
    _validate_session_id(payload.get("session_id"))
    harness = payload.get("harness")
    if harness not in ALLOWED_HARNESSES:
        raise PayloadError(
            status=400,
            message=f"invalid harness: must be one of {sorted(ALLOWED_HARNESSES)}",
        )
    event_type = payload.get("event_type")
    if event_type not in ALLOWED_EVENT_TYPES:
        raise PayloadError(
            status=400,
            message=f"invalid event_type: must be one of {sorted(ALLOWED_EVENT_TYPES)}",
        )
    if event_type == EVENT_SUBAGENT_STOP:
        for required in _SUBAGENT_REQUIRED_FIELDS:
            _require_non_empty_string(payload, required, context=EVENT_SUBAGENT_STOP)
    return payload


def build_record(payload: dict[str, Any], now_iso: str) -> CaptureRecord:
    """Build a CaptureRecord from a validated payload and the daemon's clock.

    ``id`` is computed as SHA-256 hex of the canonical JSON form of the
    record-minus-id; ``ingest_time`` is the caller-supplied ISO-8601 string.
    Subagent-only fields (``agent_id``, ``agent_type``,
    ``parent_transcript_path``, ``effort``) appear with ``null`` defaults on
    Stop records so canonicalization is byte-stable across event types.
    """
    body: dict[str, Any] = {
        "ingest_time": now_iso,
        "event_type": payload["event_type"],
        "harness": payload["harness"],
        "session_id": payload["session_id"],
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "parent_transcript_path": payload.get("parent_transcript_path"),
        "effort": payload.get("effort"),
        "transcript_path": payload.get("transcript_path"),
        "cwd": payload.get("cwd"),
        "assistant_message": payload.get("assistant_message"),
        "tool_calls": list(payload.get("tool_calls", [])),
        "files_touched": list(payload.get("files_touched", [])),
        "stop_reason": payload.get("stop_reason"),
        "timing": dict(payload.get("timing", {})),
        "token_usage": dict(payload.get("token_usage", {})),
    }
    record_id = compute_record_id(body)
    return CaptureRecord(id=record_id, **body)


def to_jsonl_bytes(record: CaptureRecord) -> bytes:
    """Serialize a record as one canonical JSON line, LF-terminated.

    Matches ``policies/storage-layout.md`` §5 — UTF-8, LF, no pretty-printing,
    canonical sorted-key form on disk so a reader can recompute the id by
    parsing and re-encoding without ordering surprises.
    """
    line = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
    return (line + "\n").encode("utf-8")
