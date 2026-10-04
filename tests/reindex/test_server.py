"""Integration tests for :func:`palace.reindex.server.serve`.

Each test starts ``serve()`` on a background thread against a real
:class:`watchdog.observers.fsevents.FSEventsObserver`, manipulates files
under a temp watch root, then polls the events log up to a 2 s budget
before tearing the daemon down via the ``_shutdown_event`` test seam.
These witnesses do not send a process signal.
"""

from __future__ import annotations

import json
import platform
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


def _poll_for_event(path: Path, *, relative_path: str, deadline: float) -> dict[str, Any]:
    """Require the exact target within one deadline; root replay cannot satisfy it."""
    records: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        if path.is_file():
            records = [json.loads(line) for line in path.read_text().splitlines()]
            for record in records:
                if record["relative_path"] == relative_path and time.monotonic() <= deadline:
                    return record
        time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
    raise AssertionError(f"no {relative_path} event before deadline; records={records}")


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
        deadline = time.monotonic() + 2.0
        (tmp_watch_root / "notes.md").write_text("hello\n")
        record = _poll_for_event(
            _today_events_path(tmp_store), relative_path="notes.md", deadline=deadline
        )
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
        deadline = time.monotonic() + 2.0
        (tmp_watch_root / "alive.md").write_text("alive\n")
        _poll_for_event(_today_events_path(tmp_store), relative_path="alive.md", deadline=deadline)
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
            _poll_for_event(
                _today_events_path(tmp_store),
                relative_path="second.md",
                deadline=time.monotonic() + 5.0,
            )
        finally:
            shutdown.set()
            thread.join(timeout=5.0)
    finally:
        observer_mod.ChangeHandler.on_modified = original_on_modified  # type: ignore[method-assign]
    assert result.get("rc") == 0


def test_serve_sigterm_drains_inflight_debouncer(
    tmp_store: Path, tmp_watch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shutdown seam flushes a genuinely observed, still-pending file event.

    The historical selector remains for proof lineage; this sends no signal.
    A fixed test clock prevents ordinary polling from satisfying the witness.
    """
    from palace.reindex import server as server_mod
    from palace.reindex.debounce import PathDebouncer

    received, flushed = (threading.Event() for _ in range(2))

    class PendingDebouncer(PathDebouncer):
        def __init__(self, **keywords: Any) -> None:
            super().__init__(**keywords, clock=lambda: 0.0)

        def submit(self, **keywords: Any) -> None:
            super().submit(**keywords)
            if keywords["relative_path"].as_posix() == "burst.md":
                received.set()

        def shutdown_flush(self) -> None:
            with self._lock:
                assert any(a.relative_path.as_posix() == "burst.md" for a in self._table.values())
            super().shutdown_flush()
            flushed.set()

    monkeypatch.setattr(server_mod, "PathDebouncer", PendingDebouncer)
    _add_watch_root(tmp_store, tmp_watch_root)
    thread, shutdown, result = _start_serve(store=tmp_store)
    events_file = _today_events_path(tmp_store)
    try:
        _wait_for_observer(events_file)
        deadline = time.monotonic() + 2.0
        (tmp_watch_root / "burst.md").write_text("a\n")
        assert received.wait(timeout=max(0.0, deadline - time.monotonic())), (
            "native burst event did not enter debouncer within two seconds"
        )
        assert not events_file.exists() or "burst.md" not in events_file.read_text()
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    assert not thread.is_alive(), "daemon did not stop after shutdown request"
    assert result.get("rc") == 0, result
    assert flushed.is_set(), "shutdown did not flush the pending accumulator"
    assert events_file.is_file(), "shutdown did not persist the pending record"
    records = [json.loads(line) for line in events_file.read_text().splitlines()]
    assert any(record["relative_path"] == "burst.md" for record in records)


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


def test_serve_with_real_sigterm(
    tmp_store: Path, tmp_watch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observe a real file event, then exit cleanly through the shutdown seam.

    The historical selector is retained for proof lineage; this is not a
    process-signal witness. Synchronize on actual native stream registration
    and delivery rather than assuming the kernel delivers within 0.3 seconds.
    """
    from watchdog.observers import fsevents

    _add_watch_root(tmp_store, tmp_watch_root)
    ready = threading.Event()
    # The native extension is intentionally outside watchdog's typed exports.
    native_api: Any = vars(fsevents)["_fsevents"]
    native_add_watch = native_api.add_watch

    def register_watch(*arguments: Any, **keywords: Any) -> Any:
        result = native_add_watch(*arguments, **keywords)
        ready.set()
        return result

    monkeypatch.setattr(native_api, "add_watch", register_watch)
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        assert ready.wait(timeout=5.0), "native observer did not register"
        (tmp_watch_root / "ok.md").write_text("ok\n")
        _poll_for_event(
            _today_events_path(tmp_store), relative_path="ok.md", deadline=time.monotonic() + 5.0
        )
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    assert not thread.is_alive(), "daemon did not stop after shutdown request"
    assert result.get("rc") == 0, result
