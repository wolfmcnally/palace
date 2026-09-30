"""Configuration constants for the reindex daemon.

The constants below are the single source of truth for Phase 2.2; a
configuration file (env / TOML) is intentionally deferred — the per-path
100 ms debounce window is hard-coded per the parent phase. Tests inject
overrides at construction time (``PathDebouncer(interval=0.010)``) rather
than via a CLI flag.

The ``events_path`` helper resolves the canonical per-day events file under
the daemon's machine-state root, and lazily creates the parent ``events/``
directory. The America/Boise day-boundary rule from
``policies/storage-layout.md`` §4 governs filename selection; the writer
worker calls this on every flush so a daemon that survives midnight rolls
cleanly into the next day's file.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from palace.daemons.capture.config import DEFAULT_STORE

__all__ = [
    "BOOTSTRAP_CHECKPOINT_INTERVAL",
    "BOOTSTRAP_CURSOR_FILENAME",
    "DEBOUNCE_INTERVAL_SECONDS",
    "DEBOUNCE_POLL_INTERVAL_SECONDS",
    "DEFAULT_STORE",
    "EVENTS_QUEUE_MAXSIZE",
    "EVENT_TYPE_FS_CHANGE",
    "ChangeKind",
    "WRITER_QUEUE_PUT_TIMEOUT_SECONDS",
    "bootstrap_cursor_path",
    "default_store",
    "events_path",
]


# Per-path coalescing window. Inbound events for the same
# ``(watch_root, relative_path)`` within this window collapse to a single
# emitted record. Fixed at 100 ms per ``briefs/sota-memory-and-recall.md``
# §B.1 and the parent phase.
DEBOUNCE_INTERVAL_SECONDS: float = 0.1

# Polling cadence for the debouncer's flush thread. 25 ms keeps flush
# latency bounded by ``DEBOUNCE_INTERVAL_SECONDS + 25 ms`` worst-case
# without burning a measurable fraction of a CPU core.
DEBOUNCE_POLL_INTERVAL_SECONDS: float = 0.025

# Bounded backpressure between the debouncer's sink and the writer worker.
# Bursts that exceed this cap surface a stderr drop line rather than block
# the FSEvents callback thread forever.
EVENTS_QUEUE_MAXSIZE: int = 4096

# How long the debouncer's sink waits for queue space before logging a
# drop. Mirrors the capture daemon's POST-side timeout so an operator
# eyeballing the two surfaces sees the same shape.
WRITER_QUEUE_PUT_TIMEOUT_SECONDS: float = 1.0

# The on-disk discriminator for change-event records. Distinct from the
# capture daemon's ``stop`` / ``subagent_stop`` event types so a future
# unified-stream consumer can route by ``event_type`` alone.
EVENT_TYPE_FS_CHANGE: str = "fs_change"

# One of ``"created"`` / ``"modified"`` / ``"deleted"``. Move events emit
# two records — one ``"deleted"`` at the source path, one ``"created"`` at
# the destination — so consumers never need to special-case renames.
ChangeKind = Literal["created", "modified", "deleted"]


def events_path(store: Path, day: date) -> Path:
    """Return the canonical day-file path under ``store/events/``.

    Lazily creates the parent ``events/`` directory so the writer worker
    does not have to. The America/Boise ``date`` is the caller's
    responsibility; ``WriterWorker`` injects it via its ``clock`` callable.
    """
    events_dir = store / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    return events_dir / f"{day.isoformat()}.jsonl"


def default_store() -> Path:
    """Re-export of :data:`palace.daemons.capture.config.DEFAULT_STORE`.

    Exposed here so callers can ``from palace.reindex.config import
    default_store`` without reaching across subpackages. Returns the same
    ``~/palace-data.noindex/`` path the capture daemon defaults to.
    """
    return DEFAULT_STORE


# How many emits between cursor writes during a bootstrap walk (Phase 2.6).
#
# Trade-off: smaller values write the cursor more often (more I/O, more
# durability); larger values amortize the cost over more emits but lose
# more in-flight emits on interrupt. 100 ≈ one cursor write per ~10 s at
# steady state. Tests may override via the function-level
# ``checkpoint_interval`` parameter on
# :func:`palace.reindex.bootstrap.bootstrap_root`.
BOOTSTRAP_CHECKPOINT_INTERVAL: int = 100

# Filename of the per-store bootstrap cursor under ``<store>/meta/``.
BOOTSTRAP_CURSOR_FILENAME: str = "bootstrap-cursor.json"


def bootstrap_cursor_path(store: Path) -> Path:
    """Return ``<store>/meta/bootstrap-cursor.json``.

    Lazily creates ``<store>/meta/`` so callers do not have to. Mirrors
    :func:`events_path`'s lazy-parent posture.
    """
    meta = store / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    return meta / BOOTSTRAP_CURSOR_FILENAME
