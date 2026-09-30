"""HTTP loopback capture daemon.

Accepts Stop and SubagentStop payloads from Claude Code (and the equivalent
Codex finish signals), validates the wire contract, and appends each turn as
one JSON object per line to ``~/palace-data.noindex/sessions/YYYY-MM-DD/<session_id>.jsonl``.
"""

from __future__ import annotations

from palace.daemons.capture.config import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_STORE
from palace.daemons.capture.ids import canonical_json_bytes, compute_record_id
from palace.daemons.capture.schema import (
    ALLOWED_EVENT_TYPES,
    EVENT_STOP,
    EVENT_SUBAGENT_STOP,
    CaptureRecord,
)
from palace.daemons.capture.server import serve

__all__ = [
    "ALLOWED_EVENT_TYPES",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_STORE",
    "EVENT_STOP",
    "EVENT_SUBAGENT_STOP",
    "CaptureRecord",
    "canonical_json_bytes",
    "compute_record_id",
    "serve",
]
