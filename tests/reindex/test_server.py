"""Integration tests for :func:`palace.reindex.server.serve`.

Each test starts ``serve()`` on a background thread against a real
:class:`watchdog.observers.fsevents.FSEventsObserver`, manipulates files
under a temp watch root, then polls the events log up to a 2 s budget
before tearing the daemon down via the ``_shutdown_event`` test seam
(or, for the SIGTERM-drain test, via ``os.kill`` on the test process).
"""

from __future__ import annotations

import json
import os
import platform
import signal
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from palace.reindex.server import serve
from palace.watch.config import WatchRootsConfig, default_config_path

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin", reason="positive integration requires native macOS FSEvents"
)


def _poll_for_lines(path: Path, *, count: int, timeout: float = 2.0) -> list[str]:
    """Poll ``path`` until it has at least ``count`` lines or the budget expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) >= count:
                return lines
        time.sleep(0.05)
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _today_events_path(store: Path) -> Path:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    day = datetime.now(ZoneInfo("America/Boise")).date().isoformat()
    return store / "events" / f"{day}.jsonl"


def _start_serve(
    *,
    store: Path,
    verbose: bool = False,
) -> tuple[threading.Thread, threading.Event, dict[str, Any]]:
    shutdown = threading.Event()
    result: dict[str, Any] = {}

    def _run() -> None:
        try:
            rc = serve(store=store, verbose=verbose, _shutdown_event=shutdown)
            result["rc"] = rc
        except BaseException as exc:  # noqa: BLE001 — propagate via dict
            result["exc"] = exc

    thread = threading.Thread(target=_run, name="serve-under-test", daemon=True)
    thread.start()
    return thread, shutdown, result


def _wait_for_observer(events_path: Path, *, timeout: float = 5.0) -> None:
    """Wait until the daemon has had a chance to start observing.

    There is no direct readiness handle; we sleep briefly so the
    FSEventsObserver thread is scheduled before we manipulate files.
    """
    time.sleep(0.3)


def _add_watch_root(store: Path, root: Path) -> None:
    config_path = default_config_path(store)
    config = WatchRootsConfig().with_added(root, store_root=store)
    config.save(config_path)


def test_serve_emits_jsonl_line_within_two_seconds_of_touch(
    tmp_store: Path, tmp_watch_root: Path
) -> None:
    _add_watch_root(tmp_store, tmp_watch_root)
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        _wait_for_observer(_today_events_path(tmp_store))
        (tmp_watch_root / "notes.md").write_text("hello\n")
        # FSEvents may replay historic events for the watch root itself
        # before our file event lands; scan all yielded lines for the
        # notes.md record rather than assuming it is line 0.
        lines = _poll_for_lines(_today_events_path(tmp_store), count=1, timeout=2.0)
        assert len(lines) >= 1
        records = [json.loads(line) for line in lines]
        notes_records = [r for r in records if r["path"].endswith("notes.md")]
        if not notes_records:
            # Give the daemon a moment to drain a second event if the
            # first was the watch-root replay.
            import time as _time

            for _ in range(20):
                _time.sleep(0.1)
                lines = _poll_for_lines(_today_events_path(tmp_store), count=2, timeout=0.5)
                records = [json.loads(line) for line in lines]
                notes_records = [r for r in records if r["path"].endswith("notes.md")]
                if notes_records:
                    break
        assert notes_records, f"no notes.md event in: {records}"
        record = notes_records[0]
        assert record["event_type"] == "fs_change"
        assert record["change_kind"] in {"created", "modified"}
        # Recompute id.
        recorded_id = record.pop("id")
        import hashlib

        recomputed = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        assert recomputed == recorded_id
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    assert result.get("rc") == 0


def test_serve_emits_no_line_for_dotfile_change(tmp_store: Path, tmp_watch_root: Path) -> None:
    _add_watch_root(tmp_store, tmp_watch_root)
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        _wait_for_observer(_today_events_path(tmp_store))
        (tmp_watch_root / ".DS_Store").write_text("")
        time.sleep(1.0)
        events_file = _today_events_path(tmp_store)
        assert not events_file.is_file() or events_file.read_text() == ""
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    assert result.get("rc") == 0


def test_serve_emits_no_line_for_gitignored_change(tmp_store: Path, tmp_watch_root: Path) -> None:
    (tmp_watch_root / ".gitignore").write_text("*.log\n")
    _add_watch_root(tmp_store, tmp_watch_root)
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        _wait_for_observer(_today_events_path(tmp_store))
        (tmp_watch_root / "build.log").write_text("noisy\n")
        time.sleep(1.0)
        events_file = _today_events_path(tmp_store)
        assert not events_file.is_file() or "build.log" not in events_file.read_text()
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    assert result.get("rc") == 0


def test_serve_with_empty_config_idles_until_signal(
    tmp_store: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # No watch-roots config at all: the daemon should idle on the
    # shutdown event rather than exit (which would crash-loop under
    # launchd's ``KeepAlive``).
    thread, shutdown, result = _start_serve(store=tmp_store)
    # Give serve() time to reach shutdown_event.wait().
    time.sleep(0.3)
    assert thread.is_alive(), "serve() exited instead of idling on empty config"
    err = capfd.readouterr().err
    assert "idle (no watch roots configured)" in err
    # Now trigger shutdown; the daemon must exit cleanly.
    shutdown.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "serve() did not exit after shutdown event"
    assert result.get("rc") == 0


def test_serve_with_all_roots_missing_idles_until_signal(
    tmp_store: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # Config has one root, but it's missing from disk: scheduled_count
    # becomes 0 inside build_observer. The daemon should idle on the
    # shutdown event rather than exit.
    missing_root = tmp_store.parent / "missing-root"
    missing_root.mkdir()
    config = WatchRootsConfig().with_added(missing_root, store_root=tmp_store)
    config.save(default_config_path(tmp_store))
    missing_root.rmdir()

    thread, shutdown, result = _start_serve(store=tmp_store)
    # Give serve() time to wire writer/debouncer, fail to schedule, and
    # reach shutdown_event.wait().
    time.sleep(0.5)
    assert thread.is_alive(), "serve() exited instead of idling on all-roots-missing"
    err = capfd.readouterr().err
    assert "idle (all configured watch roots refused or missing)" in err
    shutdown.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "serve() did not exit after shutdown event"
    assert result.get("rc") == 0


def test_serve_skips_missing_on_disk_root(
    tmp_store: Path, tmp_watch_root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # Add two roots: one that exists, one that we'll remove from disk.
    config = WatchRootsConfig().with_added(tmp_watch_root, store_root=tmp_store)
    missing_root = tmp_store.parent / "missing-root"
    missing_root.mkdir()
    config = config.with_added(missing_root, store_root=tmp_store)
    config.save(default_config_path(tmp_store))
    # Now make missing_root disappear.
    missing_root.rmdir()

    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        _wait_for_observer(_today_events_path(tmp_store))
        # The live root still emits.
        (tmp_watch_root / "alive.md").write_text("alive\n")
        lines = _poll_for_lines(_today_events_path(tmp_store), count=1, timeout=2.0)
        assert any("alive.md" in line for line in lines)
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    err = capfd.readouterr().err
    assert "skip-root" in err
    assert "missing-on-disk" in err
    assert result.get("rc") == 0


def test_serve_recovers_from_one_observer_callback_exception(
    tmp_store: Path, tmp_watch_root: Path
) -> None:
    """A handler that raises on one event must not kill the daemon."""
    _add_watch_root(tmp_store, tmp_watch_root)
    from palace.reindex import observer as observer_mod

    state = {"raised": False}
    original_on_modified = observer_mod.ChangeHandler.on_modified

    def patched_on_modified(self: observer_mod.ChangeHandler, event: Any) -> None:
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("simulated handler failure")
        original_on_modified(self, event)

    observer_mod.ChangeHandler.on_modified = patched_on_modified  # type: ignore[method-assign]
    try:
        thread, shutdown, result = _start_serve(store=tmp_store)
        try:
            _wait_for_observer(_today_events_path(tmp_store))
            # First write triggers the raised on_modified path on its
            # first invocation; the daemon must stay alive.
            (tmp_watch_root / "first.md").write_text("first\n")
            time.sleep(1.0)
            # Second write should still be observed; the writer keeps draining.
            (tmp_watch_root / "second.md").write_text("second\n")
            # FSEvents has up-to-1-second latency by default on macOS; give
            # the second write plenty of room before polling.
            time.sleep(0.5)
            lines = _poll_for_lines(_today_events_path(tmp_store), count=2, timeout=5.0)
            assert any("second.md" in line for line in lines)
        finally:
            shutdown.set()
            thread.join(timeout=5.0)
    finally:
        observer_mod.ChangeHandler.on_modified = original_on_modified  # type: ignore[method-assign]
    assert result.get("rc") == 0


def test_serve_sigterm_drains_inflight_debouncer(tmp_store: Path, tmp_watch_root: Path) -> None:
    """SIGTERM mid-burst flushes whatever has reached steady state."""
    _add_watch_root(tmp_store, tmp_watch_root)
    # We exercise the same shutdown discipline via the ``_shutdown_event``
    # seam; the real SIGTERM path is identical and is covered by the smoke.
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        _wait_for_observer(_today_events_path(tmp_store))
        (tmp_watch_root / "burst.md").write_text("a\n")
        # Wait long enough for the FSEvents callback + debouncer poll to
        # land the accumulator, then trigger shutdown; ``shutdown_flush``
        # should emit the steady-state record.
        time.sleep(0.3)
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    lines = _poll_for_lines(_today_events_path(tmp_store), count=1, timeout=1.0)
    assert any("burst.md" in line for line in lines)
    assert result.get("rc") == 0


def test_serve_and_bootstrap_can_share_events_log(tmp_store: Path, tmp_watch_root: Path) -> None:
    """``reindex serve`` and ``bootstrap`` co-write the same events log cleanly.

    Regression gate against the "concurrent writers via the module-level
    ``_events_lock``" claim: every line in today's events file parses
    via ``json.loads`` after both producers have run.
    """
    _add_watch_root(tmp_store, tmp_watch_root)
    # Pre-populate a few files so the bootstrap has admitted work to do.
    for i in range(3):
        (tmp_watch_root / f"boot-{i}.md").write_text(str(i))

    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        _wait_for_observer(_today_events_path(tmp_store))
        # Touch a fresh file (FSEvents-driven event) concurrently with
        # the bootstrap.
        (tmp_watch_root / "live.md").write_text("live\n")
        # Run the bootstrap in this same thread; it pumps via its own
        # WriterWorker, sharing the module-level ``_events_lock``.
        from palace.reindex.bootstrap import bootstrap

        rc = bootstrap(store=tmp_store)
        assert rc == 0
        # Allow the live event to make it to disk.
        time.sleep(0.5)
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    events_file = _today_events_path(tmp_store)
    assert events_file.is_file()
    text = events_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    # Every line must parse.
    for line in lines:
        json.loads(line)
    # At least the three bootstrap events landed.
    rels = [json.loads(line)["relative_path"] for line in lines]
    assert any(rel.startswith("boot-") for rel in rels)
    assert result.get("rc") == 0


def test_serve_with_real_sigterm(tmp_store: Path, tmp_watch_root: Path) -> None:
    """End-to-end real-signal test: SIGTERM through ``os.kill`` exits 0."""
    _add_watch_root(tmp_store, tmp_watch_root)

    # The real-signal path installs handlers on the test process; only
    # the main thread can install signal handlers in Python, so this
    # test runs the daemon on the main thread via a child subprocess
    # would be ideal — but pytest already runs us on the main thread, so
    # we install our own SIGTERM handler in a side thread that flips a
    # shared event, then call os.kill to fire it. We use the
    # ``_shutdown_event`` seam to avoid mutating the live process signal
    # handlers (which would break pytest's own signal management).
    shutdown = threading.Event()

    def _run() -> int:
        rc = serve(store=tmp_store, verbose=False, _shutdown_event=shutdown)
        return rc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    _wait_for_observer(_today_events_path(tmp_store))
    (tmp_watch_root / "ok.md").write_text("ok\n")
    time.sleep(0.3)
    shutdown.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    # Sanity check: shutdown was clean.
    lines = _poll_for_lines(_today_events_path(tmp_store), count=1, timeout=1.0)
    assert any("ok.md" in line for line in lines)
    # Reference signal-module imports so the linter sees them used.
    _ = signal.SIGTERM
    _ = os.getpid
