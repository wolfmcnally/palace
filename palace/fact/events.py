"""``fact-invalidate`` / ``fact-supersede`` event records and their appender.

Lifecycle mutations to ``MEMORY.md`` are recorded as new events in the
append-only machine-state stream at ``<store>/events/<YYYY-MM-DD>.jsonl``,
per ``policies/fact-schema.md`` §Lifecycle. The records are stamped with a
canonical-JSON ``id`` via :func:`palace.daemons.capture.ids.compute_record_id`
(no second SHA-256 implementation) and appended with the exact
``O_WRONLY | O_APPEND | O_CREAT`` + ``fsync`` idiom the reindex writer uses,
so the append-only invariant holds and the recompute-from-``jq`` snippet
works against any emitted line.

Record shapes:

- ``fact-invalidate``: ``{id, ingest_time, event_type, fact_id}``
- ``fact-supersede``:  ``{id, ingest_time, event_type, old_fact_id, new_fact_id}``
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from palace.daemons.capture.config import BOISE_TZ
from palace.daemons.capture.ids import canonical_json_bytes, compute_record_id
from palace.reindex.config import events_path

__all__ = [
    "EVENT_TYPE_FACT_INVALIDATE",
    "EVENT_TYPE_FACT_SUPERSEDE",
    "append_fact_event",
    "build_fact_event",
]


EVENT_TYPE_FACT_INVALIDATE: str = "fact-invalidate"
EVENT_TYPE_FACT_SUPERSEDE: str = "fact-supersede"


def build_fact_event(
    *,
    event_type: str,
    ingest_time: str,
    **payload: Any,
) -> dict[str, Any]:
    """Assemble a fact-lifecycle event dict and stamp its canonical-JSON id.

    The body is built without an ``id`` field, handed to
    :func:`compute_record_id`, then the result is stamped on. ``payload``
    carries the type-specific fields (``fact_id`` for invalidate;
    ``old_fact_id`` / ``new_fact_id`` for supersede).
    """
    body: dict[str, Any] = {
        "ingest_time": ingest_time,
        "event_type": event_type,
        **payload,
    }
    record_id = compute_record_id(body)
    return {"id": record_id, **body}


def append_fact_event(
    store: Path,
    event: dict[str, Any],
    *,
    clock: Callable[[], date] | None = None,
) -> Path:
    """Append ``event`` as one canonical-JSON line to the day's events file.

    Resolves ``<store>/events/<YYYY-MM-DD>.jsonl`` (America/Boise day via
    ``clock``, defaulting to the live wall clock), then writes the
    sorted-key, whitespace-free line with ``O_WRONLY | O_APPEND | O_CREAT``
    and ``fsync`` — the same durable-append idiom as the reindex writer.
    Returns the path written.
    """
    day = (clock or _default_clock)()
    path = events_path(store, day)
    line = canonical_json_bytes(event) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def _default_clock() -> date:
    """Return the current America/Boise date."""
    return datetime.now(BOISE_TZ).date()
