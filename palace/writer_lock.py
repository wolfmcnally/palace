"""The per-store writer lock every palace writer of ``chunks.sqlite`` takes.

The lock is an advisory ``fcntl.flock`` on ``<store>/meta/index-writer.lock``.
The kernel releases it when the holder's descriptor closes, including when
the holder is killed, so there is no stale-lock reclaim path. A waiter polls
with ``LOCK_NB`` until a bounded deadline and then fails loudly naming the
holder; it never proceeds unlocked. After acquiring, the holder writes a small
JSON record (pid, host, operation, start time) into the file so a timed-out
waiter can name it; that record is informational and never changes lock
semantics.

This module is standard library only so ``palace.metadata`` can import it
without loading the index package. The one wait default lives here.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import socket
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "WRITER_LOCK_FILENAME",
    "WRITER_LOCK_POLL_SECONDS",
    "WRITER_LOCK_TIMEOUT_SECONDS",
    "WriterLockTimeout",
    "read_lock_holder",
    "writer_lock",
    "writer_lock_path",
]

# Bounded wait before a writer gives up on the lock. A whole-tree build holds
# the lock for its complete reconcile, so a caller that runs long rebuilds
# beside per-file updates passes a longer timeout explicitly.
WRITER_LOCK_TIMEOUT_SECONDS: float = 300.0
WRITER_LOCK_POLL_SECONDS: float = 0.05
WRITER_LOCK_FILENAME: str = "index-writer.lock"


class WriterLockTimeout(TimeoutError):
    """The writer lock was not acquired within the bounded wait."""


def writer_lock_path(store: Path) -> Path:
    """Return the lock file path under ``<store>/meta/``."""
    return store / "meta" / WRITER_LOCK_FILENAME


def read_lock_holder(path: Path) -> dict[str, Any]:
    """Read the holder record, tolerating an absent, empty or partial file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _describe_holder(path: Path) -> str:
    holder = read_lock_holder(path)
    if not holder:
        return "an unidentified holder"
    return (
        f"pid {holder.get('pid', '?')} on {holder.get('host', '?')} "
        f"(operation={holder.get('operation', '?')} since {holder.get('started_at', '?')})"
    )


@contextlib.contextmanager
def writer_lock(
    store: Path, *, operation: str, timeout: float = WRITER_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Hold the store's writer lock for the ``with`` body.

    ``operation`` names the holder's work in the record a timed-out waiter
    reads. ``timeout`` is the bounded wait in seconds; zero means one attempt.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("writer lock timeout must be a finite non-negative number of seconds")
    path = writer_lock_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WriterLockTimeout(
                        f"index writer lock {path} is held by {_describe_holder(path)}; "
                        f"waited {timeout:g}s — retry later or pass a longer lock timeout"
                    ) from None
                time.sleep(WRITER_LOCK_POLL_SECONDS)
        record = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "operation": operation,
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(record, sort_keys=True).encode("utf-8"))
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
