"""The exclusive consolidator run lock.

A non-dry-run consolidation pass holds an exclusive lock at
``<store>/meta/consolidator.lock`` for its duration so two concurrent
invocations cannot both write. Acquisition uses ``O_CREAT | O_EXCL`` so the
create-and-claim is atomic; the lock file carries ``{pid, start_time, host}``
so a stale lock left by a crashed run can be detected and reclaimed.

Stale recovery: on ``FileExistsError`` the holder's pid is probed with
``os.kill(pid, 0)``. A dead pid (``ProcessLookupError``) means the previous
run crashed without releasing — the lock is reclaimed and acquisition retried
once. A live pid means a real concurrent run — acquisition raises
:class:`LockHeldError` with a clear message and exit-1-worthy text.

Dry-run takes NO lock — the pipeline acquires this context manager only on the
real-write path.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from palace.consolidate._errors import ConsolidationError
from palace.daemons.capture.config import BOISE_TZ

__all__ = ["LockHeldError", "consolidator_lock"]


class LockHeldError(ConsolidationError):
    """Raised when the consolidator lock is held by a live process."""


@contextlib.contextmanager
def consolidator_lock(path: Path) -> Iterator[None]:
    """Hold the exclusive consolidator lock at ``path`` for the ``with`` body.

    Atomic create-or-fail via ``O_CREAT | O_EXCL``; a stale lock (held by a
    dead pid) is reclaimed and acquisition retried once; a live holder raises
    :class:`LockHeldError`. The lock is unlinked in ``finally`` so a clean run
    always releases.
    """
    _acquire(path, allow_reclaim=True)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _acquire(path: Path, *, allow_reclaim: bool) -> None:
    """Attempt the atomic claim once; reclaim-and-retry on a stale lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        if allow_reclaim and _is_stale(path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            _acquire(path, allow_reclaim=False)
            return
        holder = _read_holder(path)
        raise LockHeldError(
            f"consolidator lock held by pid {holder.get('pid', '?')} "
            f"on {holder.get('host', '?')} since {holder.get('start_time', '?')} "
            f"({path}) — another run is in progress"
        ) from exc
    else:
        payload = {
            "pid": os.getpid(),
            "start_time": datetime.now(BOISE_TZ).isoformat(timespec="seconds"),
            "host": socket.gethostname(),
        }
        try:
            os.write(fd, json.dumps(payload, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


def _is_stale(path: Path) -> bool:
    """Return True when the lock's holder pid is no longer alive."""
    holder = _read_holder(path)
    pid = holder.get("pid")
    if not isinstance(pid, int):
        # A lock we cannot read a pid from is treated as stale — better to
        # reclaim a malformed lock than to deadlock the nightly run forever.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # The pid exists but is owned by another user — treat as live.
        return False
    return False


def _read_holder(path: Path) -> dict[str, object]:
    """Read the lock's holder metadata, tolerating a malformed/empty file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
