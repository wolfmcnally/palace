"""Append-only writer worker for the change-event events log.

A single dedicated thread drains a ``queue.Queue[ChangeEvent | _Sentinel]``
populated by the debouncer's sink. For each record: serialize to
canonical JSONL bytes, open the day's events file with
``O_WRONLY | O_APPEND | O_CREAT``, write, fsync, close. A module-level
``_events_lock`` serializes appends so concurrent flush threads do not
interleave lines on disk.

One shared day file per America/Boise day at
``<store>/events/<YYYY-MM-DD>.jsonl``. The day is computed via the
``clock`` callable (defaults to the live wall clock) on every flush so a
daemon that survives midnight rolls cleanly into the next day's file.

Log-and-continue posture on write failure: a single failed write logs to
stderr and the thread keeps draining. A loud-failure mode is tracked for
Phase 2.5 once launchd KeepAlive is in play.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from palace.reindex.config import events_path
from palace.reindex.schema import ChangeEvent, to_jsonl_bytes

__all__ = ["WriterWorker", "events_lock"]


# Local copy of America/Boise to avoid importing the capture daemon's
# server module just for a timezone constant.
_BOISE_TZ: ZoneInfo = ZoneInfo("America/Boise")


class _Sentinel:
    """Singleton marker enqueued by :meth:`WriterWorker.stop`."""


_SENTINEL: Final[_Sentinel] = _Sentinel()


# Module-level lock so concurrent flush threads (one capture daemon
# process may host multiple WriterWorker instances in tests) serialize
# appends to the same events file. Held only across one
# open/write/fsync/close sequence.
_events_lock: threading.Lock = threading.Lock()


def events_lock() -> threading.Lock:
    """Return the module-level append lock.

    Exposed so tests can confirm the lock identity is shared across
    instances without reaching for a private name.
    """
    return _events_lock


def _default_clock() -> date:
    """Return the current America/Boise date."""
    return datetime.now(_BOISE_TZ).date()


def _append_line_fsynced(path: Path, line: bytes) -> None:
    """Open with O_APPEND|O_CREAT, write ``line``, fsync, close.

    ``line`` must end in a newline. The caller is responsible for the
    canonical JSON encoding (see :func:`palace.reindex.schema.to_jsonl_bytes`).
    """
    if not line.endswith(b"\n"):
        raise ValueError("line must end with newline")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


class WriterWorker(threading.Thread):
    """Single background thread that drains ``ChangeEvent``\\ s from a queue.

    The debouncer enqueues a record and returns; this thread keeps
    disk-level ordering deterministic (the module-level ``_events_lock``
    serializes appends across worker instances). One failed write logs to
    stderr and the loop continues — the daemon stays up so subsequent
    flushes still land.
    """

    def __init__(
        self,
        *,
        store: Path,
        work_queue: queue.Queue[ChangeEvent | _Sentinel],
        clock: Callable[[], date] | None = None,
    ) -> None:
        super().__init__(name="palace-reindex-writer", daemon=True)
        self._store = store
        self._queue = work_queue
        self._clock = clock if clock is not None else _default_clock

    def stop(self, timeout: float = 5.0) -> None:
        """Enqueue the sentinel and join the worker thread. Idempotent."""
        self._queue.put(_SENTINEL)
        if self.is_alive():
            self.join(timeout=timeout)

    def run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if isinstance(item, _Sentinel):
                    return
                self._handle(item)
            except Exception as exc:  # noqa: BLE001 — log-and-continue posture
                print(
                    f"palace reindex: write-failed reason={exc!r}",
                    file=sys.stderr,
                    flush=True,
                )

    def _handle(self, event: ChangeEvent) -> None:
        day = self._clock()
        path = events_path(self._store, day)
        line = to_jsonl_bytes(event)
        with _events_lock:
            try:
                _append_line_fsynced(path, line)
            except OSError as exc:
                # One bad write must not kill the writer thread; surface
                # the failure to stderr and continue draining.
                print(
                    f"palace reindex: write-failed path={path} reason={exc!r}",
                    file=sys.stderr,
                    flush=True,
                )
