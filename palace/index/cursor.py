"""Durable tail cursor for the events-log consumer.

The cursor lives at ``<store>/meta/index-cursor.json`` and carries three
fields:

- ``day``: the ISO-8601 ``YYYY-MM-DD`` (America/Boise) of the events
  file the consumer is currently tailing.
- ``byte_offset``: where to resume in that file (the next line starts at
  this offset).
- ``last_event_id``: the SHA-256 hex id of the most-recently processed
  event, or ``None`` on first run. Carried for diagnostic value — the
  consumer's idempotency story is the file-hash short-circuit in
  :mod:`palace.index.writer`, not cursor identity, so a re-run that
  re-reads an already-processed event is harmless.

Atomic write via tempfile + ``os.replace``; the idiom mirrors
:mod:`palace.watch.config`. A malformed cursor on disk raises
:class:`palace.index._errors.IndexError` with the file path in the
message so the operator can correct or delete it.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from palace.daemons.capture.config import BOISE_TZ
from palace.index._errors import IndexError
from palace.index.config import index_cursor_path

__all__ = ["TailCursor", "today_default"]


@dataclass(frozen=True)
class TailCursor:
    """The persisted tail position for the events-log consumer."""

    day: str
    byte_offset: int
    last_event_id: str | None

    @classmethod
    def load(cls, store: Path) -> TailCursor:
        """Load the cursor from ``<store>/meta/index-cursor.json``.

        A missing file resolves to :func:`today_default` (today's
        America/Boise date at offset 0). A malformed file raises.
        """
        path = index_cursor_path(store)
        if not path.exists():
            return today_default()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise IndexError(f"index cursor at {path} unreadable: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise IndexError(f"index cursor at {path} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise IndexError(
                f"index cursor at {path} is not a JSON object: {type(parsed).__name__}"
            )
        try:
            day = parsed["day"]
            byte_offset = parsed["byte_offset"]
            last_event_id = parsed.get("last_event_id")
        except KeyError as exc:
            raise IndexError(f"index cursor at {path} missing field: {exc}") from exc
        if not isinstance(day, str):
            raise IndexError(f"index cursor at {path}: 'day' must be a string")
        if not isinstance(byte_offset, int):
            raise IndexError(f"index cursor at {path}: 'byte_offset' must be an integer")
        if last_event_id is not None and not isinstance(last_event_id, str):
            raise IndexError(f"index cursor at {path}: 'last_event_id' must be a string or null")
        return cls(day=day, byte_offset=byte_offset, last_event_id=last_event_id)

    def save(self, store: Path) -> None:
        """Write the cursor atomically (tempfile + ``os.replace``).

        Creates ``<store>/meta/`` on demand; the tempfile sibling lives
        in the same directory so the rename is atomic.
        """
        path = index_cursor_path(store)
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 — manual close + replace
            mode="w",
            dir=str(path.parent),
            delete=False,
            encoding="utf-8",
            suffix=".tmp",
        )
        try:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, path)


def today_default() -> TailCursor:
    """Return the first-run cursor: today's America/Boise date, offset 0."""
    day = datetime.now(BOISE_TZ).date().isoformat()
    return TailCursor(day=day, byte_offset=0, last_event_id=None)
