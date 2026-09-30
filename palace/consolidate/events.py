"""The per-run ``consolidate`` event record and its appender.

Each non-dry-run consolidation pass appends one ``consolidate`` event to the
append-only machine-state stream at ``<store>/events/<YYYY-MM-DD>.jsonl``,
recording the run's totals so a later audit (or a future scheduler health
check) can see what each pass did without reparsing DREAMS.md. The record's
``id`` is the canonical-JSON SHA-256 via
:func:`palace.daemons.capture.ids.compute_record_id`, and the append reuses
:func:`palace.fact.events.append_fact_event` — there is one durable-append
idiom for fact-surface events.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from palace.daemons.capture.ids import compute_record_id
from palace.fact.events import append_fact_event

__all__ = [
    "EVENT_TYPE_CONSOLIDATE",
    "append_consolidate_event",
    "build_consolidate_event",
]


EVENT_TYPE_CONSOLIDATE: str = "consolidate"


def build_consolidate_event(
    *,
    ingest_time: str,
    day: str,
    totals: dict[str, int],
) -> dict[str, Any]:
    """Assemble a ``consolidate`` event dict and stamp its canonical-JSON id.

    ``totals`` carries ``candidates_total`` / ``promoted`` / ``discarded`` /
    ``held_contradiction``; the body is built without ``id``, hashed, then the
    id is stamped on.
    """
    body: dict[str, Any] = {
        "ingest_time": ingest_time,
        "event_type": EVENT_TYPE_CONSOLIDATE,
        "date": day,
        "candidates_total": int(totals.get("candidates_total", 0)),
        "promoted": int(totals.get("promoted", 0)),
        "discarded": int(totals.get("discarded", 0)),
        "held_contradiction": int(totals.get("held_contradiction", 0)),
    }
    record_id = compute_record_id(body)
    return {"id": record_id, **body}


def append_consolidate_event(
    store: Path,
    event: dict[str, Any],
    *,
    clock: Callable[[], date] | None = None,
) -> Path:
    """Append ``event`` to the day's events file via the shared fact appender."""
    return append_fact_event(store, event, clock=clock)
