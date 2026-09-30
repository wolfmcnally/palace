"""Tests for the exclusive consolidator run lock."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from palace.consolidate.lock import LockHeldError, consolidator_lock


def test_lock_held_refuses_concurrent_acquire(tmp_path: Path) -> None:
    lock = tmp_path / "consolidator.lock"
    with consolidator_lock(lock):
        assert lock.exists()
        with pytest.raises(LockHeldError) as excinfo, consolidator_lock(lock):
            pass
        assert "in progress" in str(excinfo.value)
    # Released on clean exit of the outer context.
    assert not lock.exists()


def test_lock_releases_on_exit(tmp_path: Path) -> None:
    lock = tmp_path / "consolidator.lock"
    with consolidator_lock(lock):
        pass
    assert not lock.exists()
    # A fresh acquire after release succeeds.
    with consolidator_lock(lock):
        assert lock.exists()


def test_stale_lock_with_dead_pid_is_reclaimed(tmp_path: Path) -> None:
    lock = tmp_path / "consolidator.lock"
    # Write a stale lock held by a pid that cannot exist.
    dead_pid = _find_dead_pid()
    lock.write_text(
        json.dumps({"pid": dead_pid, "start_time": "2026-06-15T00:00:00-06:00", "host": "old"}),
        encoding="utf-8",
    )
    with consolidator_lock(lock):
        holder = json.loads(lock.read_text(encoding="utf-8"))
        assert holder["pid"] == os.getpid()
    assert not lock.exists()


def test_live_lock_is_not_reclaimed(tmp_path: Path) -> None:
    lock = tmp_path / "consolidator.lock"
    # A lock held by THIS live process must not be reclaimed.
    lock.write_text(
        json.dumps({"pid": os.getpid(), "start_time": "2026-06-15T00:00:00-06:00", "host": "me"}),
        encoding="utf-8",
    )
    with pytest.raises(LockHeldError), consolidator_lock(lock):
        pass
    # The pre-existing live lock is left intact.
    assert lock.exists()


def _find_dead_pid() -> int:
    """Return a pid that is not currently alive."""
    for candidate in range(999999, 990000, -1):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise RuntimeError("could not find a dead pid for the test")
