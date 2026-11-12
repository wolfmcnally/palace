"""Shared fixtures for ``tests/reindex/``.

- :func:`tmp_store` — hermetic machine-state root rooted in ``tmp_path``.
- :func:`tmp_watch_root` — hermetic watch root rooted in ``tmp_path``.
- :func:`clean_env` — strips ``PALACE_STORE`` so the CLI tests see only
  the ``--store`` flag they pass.
- :func:`populated_config` — writes a watch-roots TOML carrying
  ``tmp_watch_root`` and returns the store path.
- :func:`fast_debouncer_interval` — 10 ms interval for tests that need a
  real debouncer flush without sleeping a full 100 ms.
- :func:`stand_in_watchdog` — replaces only the native FSEvents emitter
  with :class:`StandInObserver`, built on watchdog's own observer, event
  queue and dispatch, so the reindex pipeline runs on any platform.
"""

from __future__ import annotations

import queue
import sys
import threading
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
from watchdog.events import FileSystemEvent
from watchdog.observers.api import BaseObserver, EventEmitter

from palace.watch.config import WatchRootsConfig, default_config_path

__all__ = [
    "StandInObserver",
    "StandInWatchdog",
    "clean_env",
    "fast_debouncer_interval",
    "populated_config",
    "stand_in_watchdog",
    "tmp_store",
    "tmp_watch_root",
]

_STAND_IN_TIMEOUT_SECONDS = 0.05


class StandInWatchdog:
    """Per-test registry: one inbox per scheduled watch path, plus started observers."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._inboxes: dict[str, queue.Queue[FileSystemEvent]] = {}
        self.started: list[StandInObserver] = []

    def inbox(self, watch_path: str) -> queue.Queue[FileSystemEvent]:
        with self._guard:
            return self._inboxes.setdefault(watch_path, queue.Queue())

    def inject(self, root: Path, event: FileSystemEvent) -> None:
        """Deliver ``event`` to the emitter scheduled for ``root`` (resolved)."""
        self.inbox(str(root.resolve())).put(event)


_ACTIVE: list[StandInWatchdog] = []


class _TestEmitter(EventEmitter):
    """Emitter that reads injected events instead of a native stream."""

    def queue_events(self, timeout: float) -> None:
        inbox = _ACTIVE[-1].inbox(self.watch.path)
        try:
            event = inbox.get(timeout=timeout)
        except queue.Empty:
            return
        self.queue_event(event)


class StandInObserver(BaseObserver):
    """watchdog's real observer with the test emitter in place of FSEvents."""

    def __init__(self) -> None:
        super().__init__(_TestEmitter, timeout=_STAND_IN_TIMEOUT_SECONDS)

    def start(self) -> None:
        _ACTIVE[-1].started.append(self)
        super().start()


@pytest.fixture
def tmp_store(tmp_path: Path) -> Path:
    """A hermetic machine-state root rooted in pytest's ``tmp_path``."""
    store = tmp_path / "store"
    store.mkdir()
    return store


@pytest.fixture
def tmp_watch_root(tmp_path: Path) -> Path:
    """A hermetic watch root rooted in pytest's ``tmp_path``."""
    root = tmp_path / "watch"
    root.mkdir()
    return root


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip env vars that would override the CLI's ``--store`` flag."""
    monkeypatch.delenv("PALACE_STORE", raising=False)
    yield


@pytest.fixture
def populated_config(tmp_store: Path, tmp_watch_root: Path) -> Path:
    """Write a watch-roots TOML for ``tmp_watch_root`` and return ``tmp_store``."""
    config_path = default_config_path(tmp_store)
    config = WatchRootsConfig().with_added(tmp_watch_root, store_root=tmp_store)
    config.save(config_path)
    return tmp_store


@pytest.fixture
def fast_debouncer_interval() -> float:
    """A debouncer interval short enough for unit tests to flush quickly."""
    return 0.010


@pytest.fixture
def stand_in_watchdog(monkeypatch: pytest.MonkeyPatch) -> Iterator[StandInWatchdog]:
    """Replace only the native emitter; every other pipeline stage stays real.

    ``watchdog.observers.fsevents`` is shadowed by a module whose
    ``FSEventsObserver`` is :class:`StandInObserver` (on Linux this supplies
    the module the function-local import needs), and both bindings of
    ``require_fsevents`` become no-ops for the test's duration.
    """
    import palace.reindex.observer as observer_module
    import palace.reindex.server as server_module

    registry = StandInWatchdog()
    stub = types.ModuleType("watchdog.observers.fsevents")
    stub.FSEventsObserver = StandInObserver  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "watchdog.observers.fsevents", stub)
    monkeypatch.setattr(server_module, "require_fsevents", lambda: None)
    monkeypatch.setattr(observer_module, "require_fsevents", lambda: None)
    _ACTIVE.append(registry)
    try:
        yield registry
    finally:
        _ACTIVE.remove(registry)
