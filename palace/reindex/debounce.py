"""Per-path 100 ms debouncer for FSEvents callbacks.

The debouncer coalesces bursts of events targeting the same
``(watch_root, relative_path)`` into a single ``ChangeEvent`` per the five
rules in ``plan/phase-2.2.md`` lines 32-36:

- ``None + created``  → ``created``
- ``None + modified`` → ``modified``
- ``None + deleted``  → ``deleted``
- ``created + modified`` → ``created``  (file is new from consumer view)
- ``created + deleted``  → drop on flush (file flickered out of existence)
- ``deleted + created``  → ``modified`` (atomic-rename idiom at same path)
- ``modified + modified`` → ``modified``

Rename events bypass the accumulator entirely — a move always emits two
records (``deleted`` at the source, ``created`` at the destination) and
neither is coalesced with same-path modifications in the same window. The
path identity changed and the consumer needs both halves.

A background poller thread runs every
:data:`DEBOUNCE_POLL_INTERVAL_SECONDS` (25 ms) and flushes any accumulator
whose ``last_seen + interval`` has elapsed. ``shutdown_flush()``
synchronously flushes whatever is in-flight so SIGTERM mid-burst loses at
most events whose final state was indeterminate.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from palace.reindex.config import (
    DEBOUNCE_INTERVAL_SECONDS,
    DEBOUNCE_POLL_INTERVAL_SECONDS,
    ChangeKind,
)
from palace.reindex.schema import ChangeEvent, build_change_event

__all__ = ["PathDebouncer"]


# Local copy of America/Boise so the debouncer does not pull in the
# capture daemon's server module just for a timezone constant.
_BOISE_TZ: ZoneInfo = ZoneInfo("America/Boise")


def _default_ingest_clock() -> str:
    """Return the ISO-8601-with-offset stamp for ``ingest_time``."""
    return datetime.now(_BOISE_TZ).isoformat(timespec="seconds")


# Coalescing transition table. The key is the in-flight accumulator's
# current kind (``None`` means "no event yet"); the value maps the
# incoming kind to the resulting kind, or ``False`` for the special
# "drop on flush" branch (created→deleted).
_TRANSITIONS: dict[ChangeKind | None, dict[ChangeKind, ChangeKind | None]] = {
    None: {"created": "created", "modified": "modified", "deleted": "deleted"},
    "created": {
        "created": "created",
        "modified": "created",
        # Sentinel: created+deleted → flickered out before any consumer
        # could index it; flush as a no-op. Represented as ``None`` so
        # the accumulator is purged from the table at flush time.
        "deleted": None,
    },
    "modified": {
        "created": "created",
        "modified": "modified",
        "deleted": "deleted",
    },
    "deleted": {
        # Atomic-rename idiom: the file appears to have been replaced at
        # the same path. Collapse to a single ``modified`` record so the
        # downstream pipeline re-reads the surviving content.
        "created": "modified",
        "modified": "modified",
        "deleted": "deleted",
    },
}


@dataclass
class _ChangeAccumulator:
    """In-flight coalesced state for one ``(watch_root, relative_path)`` pair.

    Only mutated under the debouncer's table lock. ``kind`` may be
    ``None`` transiently when ``_merge`` resolves a created+deleted
    sequence — the flush loop deletes such entries without emitting.
    """

    watch_root: Path
    absolute_path: Path
    relative_path: PurePosixPath
    is_directory: bool
    observed_at: str
    kind: ChangeKind | None
    last_seen: float = field(default=0.0)


class PathDebouncer:
    """Per-path 100 ms coalescing layer for FSEvents callbacks.

    Construct with a ``sink`` callable; the sink receives one fully-built
    :class:`ChangeEvent` per flushed accumulator (and per half of every
    rename, which bypasses the accumulator). The poller thread starts on
    :meth:`start` and stops on :meth:`stop`.

    Tests inject ``interval``, ``poll_interval``, ``clock``, and
    ``ingest_clock`` so they do not need real time to elapse.
    """

    def __init__(
        self,
        sink: Callable[[ChangeEvent], None],
        *,
        interval: float = DEBOUNCE_INTERVAL_SECONDS,
        poll_interval: float = DEBOUNCE_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        ingest_clock: Callable[[], str] = _default_ingest_clock,
    ) -> None:
        self._sink = sink
        self._interval = interval
        self._poll_interval = poll_interval
        self._clock = clock
        self._ingest_clock = ingest_clock
        self._table: dict[tuple[Path, PurePosixPath], _ChangeAccumulator] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Spawn the poller thread. Idempotent: a second call is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="palace-reindex-debouncer",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Signal the poller to exit and join it.

        ``stop`` does **not** flush in-flight accumulators — call
        :meth:`shutdown_flush` first if you want every steady-state record
        to land before the thread exits.
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def shutdown_flush(self) -> None:
        """Emit every in-flight accumulator synchronously.

        Used at SIGTERM time so a record that had reached steady state and
        was waiting on the 100 ms window is not lost on shutdown. Records
        with ``kind is None`` (the created+deleted flicker branch) are
        dropped silently — emitting either half would mislead the
        consumer.
        """
        with self._lock:
            pending = list(self._table.values())
            self._table.clear()
        for accumulator in pending:
            self._emit(accumulator)

    # ---------------------------------------------------------------- ingest

    def submit(
        self,
        *,
        watch_root: Path,
        absolute_path: Path,
        relative_path: PurePosixPath,
        change_kind: ChangeKind,
        is_directory: bool,
        observed_at: str,
        rename_src_path: str | None = None,
        rename_dest_path: str | None = None,
    ) -> None:
        """Accept one observed event.

        Move events (``rename_src_path`` or ``rename_dest_path`` set)
        bypass the accumulator and are emitted directly — the path
        identity changed and the consumer needs both halves. Everything
        else merges into the per-path accumulator under the coalescing
        rules in this module's docstring.
        """
        if rename_src_path is not None or rename_dest_path is not None:
            event = build_change_event(
                ingest_time=self._ingest_clock(),
                change_kind=change_kind,
                watch_root=str(watch_root),
                path=str(absolute_path),
                relative_path=relative_path.as_posix(),
                is_directory=is_directory,
                observed_at=observed_at,
                rename_src_path=rename_src_path,
                rename_dest_path=rename_dest_path,
            )
            self._sink(event)
            return

        key = (watch_root, relative_path)
        now = self._clock()
        with self._lock:
            existing = self._table.get(key)
            if existing is None:
                merged_kind: ChangeKind | None = _merge(None, change_kind)
                self._table[key] = _ChangeAccumulator(
                    watch_root=watch_root,
                    absolute_path=absolute_path,
                    relative_path=relative_path,
                    is_directory=is_directory,
                    observed_at=observed_at,
                    kind=merged_kind,
                    last_seen=now,
                )
                return
            merged_kind = _merge(existing.kind, change_kind)
            # Refresh the timestamp and the most-recently-observed
            # metadata; only ``observed_at`` from the first event is
            # preserved (it is the "started observing this burst" moment).
            self._table[key] = replace(
                existing,
                absolute_path=absolute_path,
                is_directory=is_directory,
                kind=merged_kind,
                last_seen=now,
            )

    # ---------------------------------------------------------------- internals

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._poll_interval)
            if self._stop_event.is_set():
                return
            self._drain_ready()

    def _drain_ready(self) -> None:
        cutoff = self._clock() - self._interval
        ready: list[_ChangeAccumulator] = []
        with self._lock:
            for key, accumulator in list(self._table.items()):
                if accumulator.last_seen <= cutoff:
                    ready.append(accumulator)
                    del self._table[key]
        for accumulator in ready:
            self._emit(accumulator)

    def _emit(self, accumulator: _ChangeAccumulator) -> None:
        # ``kind is None`` means the coalescing rules cancelled the
        # accumulator (created+deleted). Drop without emitting.
        if accumulator.kind is None:
            return
        event = build_change_event(
            ingest_time=self._ingest_clock(),
            change_kind=accumulator.kind,
            watch_root=str(accumulator.watch_root),
            path=str(accumulator.absolute_path),
            relative_path=accumulator.relative_path.as_posix(),
            is_directory=accumulator.is_directory,
            observed_at=accumulator.observed_at,
        )
        self._sink(event)


def _merge(current: ChangeKind | None, incoming: ChangeKind) -> ChangeKind | None:
    """Apply the coalescing transition table.

    Returns ``None`` for the created+deleted "flicker" branch so the
    accumulator can be flushed as a drop without emitting a record.
    """
    return _TRANSITIONS[current][incoming]
