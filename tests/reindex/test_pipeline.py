"""Deterministic end-to-end proofs of the reindex pipeline on any platform.

Each proof runs the real :func:`palace.reindex.server.serve`,
:func:`palace.reindex.observer.build_observer`, handler, debouncer, writer
and shutdown path. Only the native FSEvents emitter is replaced, by the
``stand_in_watchdog`` fixture's observer built on watchdog's own observer,
event queue and dispatch. Events are ordinary watchdog events for real
files the test wrote; filtering, dispatch and ordering stay watchdog's and
Palace's own.

Waits are liveness bounds on explicit completion conditions, never latency
claims, and each failure names the pipeline stage it was waiting for.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from watchdog.events import DirCreatedEvent, FileCreatedEvent, FileModifiedEvent

import palace.reindex.server as server_module
from palace.reindex.bootstrap import bootstrap
from palace.reindex.config import events_path
from palace.reindex.debounce import PathDebouncer
from palace.reindex.observer import ChangeHandler
from palace.reindex.schema import build_change_event
from palace.reindex.writer import _default_clock
from palace.watch.config import WatchRootsConfig, default_config_path

from .conftest import StandInWatchdog

_LIVENESS_BOUND_SECONDS = 10.0
_IDLE_PROBE_SECONDS = 0.2


class _Serve:
    """One ``serve()`` run on a thread, stopped through the shutdown seam."""

    def __init__(self, store: Path) -> None:
        self.store = store
        self.shutdown = threading.Event()
        self.result: dict[str, Any] = {}
        self.thread = threading.Thread(target=self._run, name="serve-under-test", daemon=True)

    def _run(self) -> None:
        try:
            self.result["rc"] = server_module.serve(
                store=self.store, verbose=True, _shutdown_event=self.shutdown
            )
        except BaseException as exc:  # noqa: BLE001 — surfaced by stop()
            self.result["exc"] = exc

    def start(self) -> _Serve:
        self.thread.start()
        return self

    def stop(self) -> int:
        self.shutdown.set()
        self.thread.join(timeout=_LIVENESS_BOUND_SECONDS)
        if self.thread.is_alive():
            raise AssertionError("stage=shutdown: serve did not return after the shutdown event")
        if "exc" in self.result:
            raise AssertionError(f"stage=shutdown: serve raised {self.result['exc']!r}")
        rc: int = self.result["rc"]
        return rc


def _events_file(store: Path) -> Path:
    return events_path(store, _default_clock())


def _records(store: Path) -> list[dict[str, Any]]:
    path = _events_file(store)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _wait_until(stage: str, condition: Callable[[], bool], seen: Callable[[], object]) -> None:
    deadline = time.monotonic() + _LIVENESS_BOUND_SECONDS
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"stage={stage}: condition not reached; seen={seen()!r}")


def _wait_for_record(store: Path, relative_path: str) -> list[dict[str, Any]]:
    _wait_until(
        "writer-fsync",
        lambda: any(r["relative_path"] == relative_path for r in _records(store)),
        lambda: _records(store),
    )
    return _records(store)


def _configure(store: Path, *roots: Path) -> None:
    config = WatchRootsConfig()
    for root in roots:
        config = config.with_added(root, store_root=store)
    config.save(default_config_path(store))


def _write(root: Path, name: str) -> Path:
    target = root.resolve() / name
    target.write_text(f"# {name}\n", encoding="utf-8")
    return target


@pytest.fixture
def fast_debouncer(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FastDebouncer(PathDebouncer):
        def __init__(self, sink: Callable[..., None], **kwargs: Any) -> None:
            super().__init__(sink, interval=0.01, poll_interval=0.005, **kwargs)

    monkeypatch.setattr(server_module, "PathDebouncer", _FastDebouncer)


@pytest.fixture
def pipeline(stand_in_watchdog: StandInWatchdog, fast_debouncer: None) -> Iterator[StandInWatchdog]:
    yield stand_in_watchdog


def test_pipeline_records_the_exact_target(
    pipeline: StandInWatchdog, tmp_store: Path, tmp_watch_root: Path
) -> None:
    _configure(tmp_store, tmp_watch_root)
    run = _Serve(tmp_store).start()
    target = _write(tmp_watch_root, "note.md")
    pipeline.inject(tmp_watch_root, FileCreatedEvent(str(target)))
    records = _wait_for_record(tmp_store, "note.md")
    assert run.stop() == 0

    assert len(records) == 1
    (record,) = records
    assert record["relative_path"] == "note.md"
    assert record["change_kind"] == "created"
    assert record["watch_root"] == str(tmp_watch_root.resolve())
    assert record["path"] == str(target)
    recomputed = build_change_event(
        ingest_time=record["ingest_time"],
        change_kind=record["change_kind"],
        watch_root=record["watch_root"],
        path=record["path"],
        relative_path=record["relative_path"],
        is_directory=record["is_directory"],
        observed_at=record["observed_at"],
        rename_src_path=record["rename_src_path"],
        rename_dest_path=record["rename_dest_path"],
    )
    assert record["id"] == recomputed.id


@pytest.mark.parametrize("ignored", ["dotfile", "gitignored", "watch-root-itself"])
def test_pipeline_drops_ignored_changes(
    pipeline: StandInWatchdog, tmp_store: Path, tmp_watch_root: Path, ignored: str
) -> None:
    root = tmp_watch_root.resolve()
    if ignored == "dotfile":
        event: FileCreatedEvent | DirCreatedEvent = FileCreatedEvent(
            str(_write(tmp_watch_root, ".hidden.md"))
        )
    elif ignored == "gitignored":
        (root / ".gitignore").write_text("skipped.md\n", encoding="utf-8")
        event = FileCreatedEvent(str(_write(tmp_watch_root, "skipped.md")))
    else:
        event = DirCreatedEvent(str(root))
    _configure(tmp_store, tmp_watch_root)
    run = _Serve(tmp_store).start()
    pipeline.inject(tmp_watch_root, event)
    # FIFO dispatch: the sentinel's record proves the ignored event was handled first.
    pipeline.inject(tmp_watch_root, FileCreatedEvent(str(_write(tmp_watch_root, "sentinel.md"))))
    records = _wait_for_record(tmp_store, "sentinel.md")
    assert run.stop() == 0

    assert [r["relative_path"] for r in _records(tmp_store)] == ["sentinel.md"], records


def test_pipeline_survives_one_handler_exception(
    pipeline: StandInWatchdog,
    tmp_store: Path,
    tmp_watch_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = ChangeHandler.on_modified
    raised: list[str] = []

    def _raise_once(self: ChangeHandler, event: FileModifiedEvent) -> None:
        if Path(str(event.src_path)).name == "first.md" and not raised:
            raised.append(str(event.src_path))
            raise RuntimeError("injected handler failure")
        original(self, event)

    monkeypatch.setattr(ChangeHandler, "on_modified", _raise_once)
    _configure(tmp_store, tmp_watch_root)
    run = _Serve(tmp_store).start()
    pipeline.inject(tmp_watch_root, FileModifiedEvent(str(_write(tmp_watch_root, "first.md"))))
    pipeline.inject(tmp_watch_root, FileModifiedEvent(str(_write(tmp_watch_root, "second.md"))))
    _wait_for_record(tmp_store, "second.md")
    assert run.stop() == 0

    assert len(raised) == 1
    assert [r["relative_path"] for r in _records(tmp_store)] == ["second.md"]
    assert capsys.readouterr().err.count("handler-failed reason=RuntimeError") == 1


def test_pipeline_shutdown_flush_persists_a_pending_event(
    stand_in_watchdog: StandInWatchdog,
    tmp_store: Path,
    tmp_watch_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = threading.Event()

    class _HeldDebouncer(PathDebouncer):
        """A debouncer whose clock never advances, so only shutdown can emit."""

        def __init__(self, sink: Callable[..., None], **kwargs: Any) -> None:
            super().__init__(sink, interval=0.01, poll_interval=0.005, clock=lambda: 0.0, **kwargs)

        def submit(self, **kwargs: Any) -> None:
            super().submit(**kwargs)
            submitted.set()

    monkeypatch.setattr(server_module, "PathDebouncer", _HeldDebouncer)
    _configure(tmp_store, tmp_watch_root)
    run = _Serve(tmp_store).start()
    target = _write(tmp_watch_root, "pending.md")
    stand_in_watchdog.inject(tmp_watch_root, FileModifiedEvent(str(target)))
    _wait_until("dispatch", submitted.is_set, lambda: _records(tmp_store))
    assert _records(tmp_store) == []
    assert run.stop() == 0

    records = _records(tmp_store)
    assert [(r["relative_path"], r["change_kind"]) for r in records] == [("pending.md", "modified")]


def test_pipeline_skips_a_missing_root_and_observes_the_live_root(
    pipeline: StandInWatchdog,
    tmp_store: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "gone"
    live = tmp_path / "live"
    missing.mkdir()
    live.mkdir()
    _configure(tmp_store, missing, live)
    missing.rmdir()
    run = _Serve(tmp_store).start()
    pipeline.inject(live, FileCreatedEvent(str(_write(live, "kept.md"))))
    _wait_for_record(tmp_store, "kept.md")
    assert run.stop() == 0

    err = capsys.readouterr().err
    assert f"skip-root path={missing.resolve()} reason=missing-on-disk" in err
    assert "observing 1 roots" in err
    assert [r["relative_path"] for r in _records(tmp_store)] == ["kept.md"]


def test_pipeline_and_bootstrap_share_one_events_log(
    pipeline: StandInWatchdog, tmp_store: Path, tmp_watch_root: Path
) -> None:
    _write(tmp_watch_root, "existing.md")
    _configure(tmp_store, tmp_watch_root)
    assert bootstrap(store=tmp_store, log=lambda _line: None) == 0
    run = _Serve(tmp_store).start()
    pipeline.inject(tmp_watch_root, FileCreatedEvent(str(_write(tmp_watch_root, "live.md"))))
    _wait_for_record(tmp_store, "live.md")
    assert run.stop() == 0

    event_files = sorted((tmp_store / "events").glob("*.jsonl"))
    assert event_files == [_events_file(tmp_store)]
    lines = event_files[0].read_text().splitlines()
    parsed = [json.loads(line) for line in lines]
    assert {r["relative_path"] for r in parsed} == {"existing.md", "live.md"}


@pytest.mark.parametrize("idle", ["empty-config", "all-roots-missing"])
def test_pipeline_idles_until_signal(
    pipeline: StandInWatchdog,
    tmp_store: Path,
    tmp_watch_root: Path,
    capsys: pytest.CaptureFixture[str],
    idle: str,
) -> None:
    if idle == "all-roots-missing":
        _configure(tmp_store, tmp_watch_root)
        tmp_watch_root.rmdir()
        expected = "idle (all configured watch roots refused or missing)"
    else:
        expected = "idle (no watch roots configured)"
    captured: list[str] = []

    def _seen() -> str:
        captured.append(capsys.readouterr().err)
        return "".join(captured)

    run = _Serve(tmp_store).start()
    _wait_until("startup", lambda: expected in _seen(), lambda: "".join(captured))
    run.thread.join(timeout=_IDLE_PROBE_SECONDS)
    assert run.thread.is_alive(), "serve returned before the shutdown event"
    assert run.stop() == 0
    assert pipeline.started == []
