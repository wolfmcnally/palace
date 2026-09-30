"""Append-only JSONL writer worker.

The HTTP handler enqueues a ``WriteRequest`` and returns 202 immediately; a
single dedicated thread drains the queue. Per-session locks ensure concurrent
POSTs targeting the same ``session_id`` serialize at the file level. fsync
fires after every line so the canonical-store invariant ("the bytes are on
disk before the queue advances") holds even on a sudden power loss.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from datetime import date
from pathlib import Path
from typing import NamedTuple

__all__ = [
    "WriteRequest",
    "WriterWorker",
    "session_path",
]


class WriteRequest(NamedTuple):
    """One line to append to one session file."""

    store: Path
    day: date
    session_id: str
    line: bytes


# Per-session locks live process-wide. ``_locks_lock`` guards the dict
# itself; the per-session Lock guards the actual file open/append/fsync
# sequence. Locks are never removed — the population is bounded by the
# number of distinct session_ids a single daemon lifetime sees.
_session_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _acquire_session_lock(session_id: str) -> threading.Lock:
    with _locks_lock:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
    return lock


def session_path(store: Path, day: date, session_id: str) -> Path:
    """Return the on-disk path for a session's JSONL file.

    ``store/sessions/<day>/<session_id>.jsonl``; the per-day directory is
    created on demand.
    """
    daily_dir = store / "sessions" / day.isoformat()
    daily_dir.mkdir(parents=True, exist_ok=True)
    return daily_dir / f"{session_id}.jsonl"


def _append_line_fsynced(path: Path, line: bytes) -> None:
    """Open with O_APPEND|O_CREAT, write ``line``, fsync, close.

    ``line`` must end in a newline; the caller is responsible for the
    canonical JSON encoding.
    """
    if not line.endswith(b"\n"):
        raise ValueError("line must end with newline")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


class WriterWorker(threading.Thread):
    """Single background thread that drains ``WriteRequest``\\ s from a queue.

    The server enqueues a request and returns 202 immediately; this thread
    keeps disk-level ordering deterministic per session_id (concurrent POSTs
    for the same session serialize on the per-session lock).
    """

    _SENTINEL: object = object()

    def __init__(self, work_queue: queue.Queue[object]) -> None:
        super().__init__(name="palace-capture-writer", daemon=True)
        self._queue = work_queue

    def stop(self) -> None:
        """Signal the worker to drain and exit. Idempotent."""
        self._queue.put(self._SENTINEL)

    def flush(self, timeout: float = 5.0) -> None:
        """Block until the queue has fully drained.

        Used by tests (and the smoke) to wait deterministically before reading
        files back. Implementation is ``Queue.join()`` with a timeout watchdog
        thread so we never hang the test runner if the worker died.
        """
        done = threading.Event()

        def _waiter() -> None:
            self._queue.join()
            done.set()

        waiter = threading.Thread(target=_waiter, daemon=True)
        waiter.start()
        if not done.wait(timeout=timeout):
            raise TimeoutError(f"writer queue did not drain within {timeout}s")

    def run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._SENTINEL:
                    return
                if not isinstance(item, WriteRequest):
                    # Defensive: a foreign object on the queue is a bug, not a
                    # condition to silently ignore. Log and drop the item.
                    print(
                        f"warning: writer received non-WriteRequest item: {type(item)!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                self._handle(item)
            except Exception as exc:  # noqa: BLE001 — log-and-continue is intentional
                # A failure on one record must not kill the writer thread;
                # the daemon stays up so subsequent POSTs continue to land.
                print(
                    f"error: writer failed to append: {exc!r}",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                self._queue.task_done()

    @staticmethod
    def _handle(req: WriteRequest) -> None:
        lock = _acquire_session_lock(req.session_id)
        with lock:
            path = session_path(req.store, req.day, req.session_id)
            _append_line_fsynced(path, req.line)
