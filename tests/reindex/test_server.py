"""Integration tests for :func:`palace.reindex.server.serve`.

Each test starts ``serve()`` on a background thread against a real
:class:`watchdog.observers.fsevents.FSEventsObserver`, manipulates files
under a temp watch root, then polls the events log within each proof's
original budget before tearing the daemon down via the
``_shutdown_event`` test seam. Positive native proofs first require an
ignored canary to be delivered and dispatched, so their deadlines start
against a stream that is actually delivering. These witnesses do not send a
process signal.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from palace.reindex.server import serve
from palace.watch.config import WatchRootsConfig, default_config_path

pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(
        platform.system() != "Darwin", reason="positive integration requires native macOS FSEvents"
    ),
]


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


_OVERFLOW_FLAGS = (
    # FSEvents event flags that replace or drop per-file events
    # (kFSEventStreamEventFlagMustScanSubDirs, ...UserDropped, ...KernelDropped).
    ("must_scan_subdirs", 0x00000001),
    ("user_dropped", 0x00000002),
    ("kernel_dropped", 0x00000004),
)
_ROOT_CHANGED = 0x00000020  # kFSEventStreamEventFlagRootChanged
_PIPELINE_STAGES = ("dispatch", "submit", "emit", "fsync")
_FLAG_WORDS_PER_NAME = 8
_POST_CANARY_BOUND = 5.0


class _FseventsRecords(logging.Handler):
    """Keep the watcher log records watchdog emits for exceptions its callback swallows."""

    def __init__(self, origin_ns: int) -> None:
        super().__init__(level=logging.WARNING)
        self._origin_ns = origin_ns
        self._guard = threading.Lock()
        self.count = 0
        self.failed = False
        self.rows: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        with self._guard:
            self.count += 1
        try:
            kind = record.exc_info[0] if record.exc_info else None
            exc_type = kind.__name__ if kind else None
            row = {
                "ns": time.monotonic_ns() - self._origin_ns,
                "level": record.levelname,
                "message": record.getMessage(),
                "exc_type": exc_type,
            }
            with self._guard:
                if len(self.rows) < _FLAG_WORDS_PER_NAME:
                    self.rows.append(row)
        except Exception:  # noqa: BLE001 — a log observer must never raise into watchdog
            self.failed = True
            self.handleError(record)


def _record_present(events_path: Path, relative_path: str) -> bool:
    if not events_path.is_file():
        return False
    for line in events_path.read_text().splitlines():
        if json.loads(line)["relative_path"] == relative_path:
            return True
    return False


class _NativeProbe:
    """Delivered-canary readiness barrier plus failure-only native stage evidence.

    Install before ``serve()`` starts. Every hook is observation-only: its
    bookkeeping is guarded and the wrapped callable always runs with its
    original arguments, because an exception escaping the emitter callback
    stops the native run loop. A delivered canary proves the stream is
    delivering and the observer is dispatching, and that later writes carry
    larger FSEvents ids. It bounds no delivery latency and starts no deadline.

    On a deadline failure only, ``annotate`` adds a joint classification from
    emitter and observer liveness read before and after a fresh post-deadline
    canary, the watcher log records watchdog emits for exceptions it swallows,
    and the target's native mark. Scratch fault controls cover every rule
    except observer-not-alive, stop-requested-but-alive and probe errors,
    which rest on code review only. A target write's marks cost two monotonic
    reads and two lock acquisitions; the fsync stage spans the whole
    ``_append_line_fsynced`` call.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, watch_root: Path, *, targets: tuple[str, ...]
    ) -> None:
        import watchdog.observers.fsevents as native

        from palace.reindex import observer, writer
        from palace.reindex.debounce import PathDebouncer

        self._root = watch_root
        self._canary = watch_root / f".palace-ready-{uuid.uuid4().hex}"
        self._canary_real = os.path.realpath(self._canary)
        self._targets = {os.path.realpath(watch_root / name): name for name in targets}
        self._names = set(targets)
        self._origin = time.monotonic_ns()
        self._lock = threading.Lock()
        self._stages: dict[str, dict[str, int]] = {}
        self._events = 0
        self._event_ids: list[int] = []
        self._canary_id: int | None = None
        self._write_ns: int | None = None
        self._flags = {
            window: dict.fromkeys((name for name, _ in _OVERFLOW_FLAGS), 0)
            for window in ("before_ready", "after_ready", "after_write")
        }
        self._window_counts = {
            window: {"events": 0, "root_changed": 0}
            for window in ("before_ready", "after_ready", "after_write")
        }
        self._flag_words: dict[str, list[str]] = {}
        self._emitter: Any = None
        self._observer: Any = None
        self._post_real: str | None = None
        self._post_seen = threading.Event()
        self._bookkeeping_failed = False
        self._dispatched = threading.Event()
        probe = self

        original_callback = native.FSEventsEmitter.events_callback

        def events_callback(
            emitter: Any, paths: list[Any], inodes: list[int], flags: list[int], ids: list[int]
        ) -> None:
            try:
                probe._native(emitter, paths, flags, ids)
            except Exception:  # noqa: BLE001 — observation must never stop the stream
                probe._safe_mark("probe.error")
            try:
                original_callback(emitter, paths, inodes, flags, ids)
            except BaseException as error:
                probe._safe_mark(f"callback.raised.{type(error).__name__}")
                raise
            probe._safe_mark("callback.return")

        monkeypatch.setattr(native.FSEventsEmitter, "events_callback", events_callback)
        # watchdog logs exceptions its callback and emitter run loop swallow on this logger.
        self._logger = logging.getLogger("fsevents")
        self._records = _FseventsRecords(self._origin)
        monkeypatch.setattr(self._logger, "handlers", [*self._logger.handlers, self._records])
        self._wrap(
            monkeypatch,
            native.FSEventsObserver,
            "start",
            lambda observer: self._capture_observer(observer),
        )
        # The native extension is intentionally outside watchdog's typed exports.
        native_api: Any = vars(native)["_fsevents"]
        self._wrap(monkeypatch, native_api, "add_watch", lambda *a, **k: "native.register")
        self._wrap(
            monkeypatch,
            observer.ChangeHandler,
            "dispatch",
            lambda handler, event: self._dispatch(event),
        )
        self._wrap(
            monkeypatch,
            PathDebouncer,
            "submit",
            lambda debouncer, **k: self._target_stage(k["relative_path"].as_posix(), "submit"),
        )
        self._wrap(
            monkeypatch,
            PathDebouncer,
            "_emit",
            lambda debouncer, accumulator: self._target_stage(
                accumulator.relative_path.as_posix(), "emit"
            ),
        )
        self._wrap(
            monkeypatch,
            writer,
            "_append_line_fsynced",
            lambda path, line: self._target_stage(json.loads(line)["relative_path"], "fsync"),
        )

    def mark(self, stage: str) -> None:
        now = time.monotonic_ns() - self._origin
        with self._lock:
            self._mark(stage, now)

    def _safe_mark(self, stage: str) -> None:
        # Bookkeeping must never alter the observed call; a failure stays visible.
        try:
            self.mark(stage)
        except Exception:  # noqa: BLE001
            self._bookkeeping_failed = True

    def write(self, name: str, text: str, *, target: bool = False) -> None:
        """Write one workload file unchanged, marking its enter and return.

        ``target=True`` first records the instant immediately before the
        deadline target's write.
        """
        if target:
            now = time.monotonic_ns() - self._origin
            with self._lock:
                self._write_ns = now
                self._mark("target.write", now)
        self.mark(f"{name}.write.enter")
        (self._root / name).write_text(text)
        self.mark(f"{name}.write.return")

    def _capture_observer(self, observer: Any) -> str:
        with self._lock:
            if self._observer is None:
                self._observer = observer
        return "observer.start"

    def wait_ready(self, timeout: float = 10.0) -> None:
        """Rewrite the ignored canary until the observer has dispatched it.

        A readiness failure is reported as such, never as a target outcome.
        """
        limit = time.monotonic() + timeout
        while True:
            self._canary.write_text(uuid.uuid4().hex)
            remaining = limit - time.monotonic()
            if remaining <= 0:
                break
            if self._dispatched.wait(min(0.1, remaining)):
                self.mark("ready")
                return
        raise AssertionError(f"native stream not ready within {timeout} s; {self._safe_describe()}")

    def annotate(
        self,
        exc: BaseException,
        *,
        target: str,
        deadline: float,
        completed: Callable[[], bool],
        window: float = 10.0,
    ) -> None:
        """Attach bounded failure diagnostics; the caller always re-raises.

        Each step is guarded on its own; a failed step adds an
        ``unavailable`` note and becomes a missing input to the joint rule.
        """
        notes: list[str] = []

        def step(label: str, run: Callable[[], Any]) -> Any:
            try:
                return run()
            except Exception as error:  # noqa: BLE001 — never mask the original failure
                notes.append(f"{label} unavailable: {error!r}")
                return None

        before = step("liveness at failure", self._liveness)
        classification = step(
            "classification", lambda: self._classify(target, deadline, completed, window)
        )
        post = step("post-deadline canary", self._post_canary)
        after = step("liveness after canary", self._liveness)
        logs = step("watcher log capture", self._log_state)
        joint = step("joint classification", lambda: self._joint(target, before, after, logs, post))
        exc.add_note(joint if joint is not None else "joint classification: indeterminate")
        for label, value in (
            ("liveness at failure", before),
            ("liveness after canary", after),
            ("post-deadline canary", post),
            ("watcher log capture", logs),
        ):
            if value is not None:
                exc.add_note(f"{label}: {json.dumps(value, sort_keys=True)}")
        if classification is not None:
            exc.add_note(classification)
        for note in notes:
            exc.add_note(note)
        exc.add_note(self._safe_describe())

    def _liveness(self) -> dict[str, Any]:
        with self._lock:
            emitter, observer = self._emitter, self._observer
        state: dict[str, Any] = {
            "emitter_captured": emitter is not None,
            "observer_captured": observer is not None,
        }
        if emitter is not None:
            state["emitter_alive"] = emitter.is_alive()
            state["emitter_stop_requested"] = emitter.stopped_event.is_set()
        if observer is not None:
            emitters = set(observer.emitters)
            state["observer_alive"] = observer.is_alive()
            state["observer_emitter_count"] = len(emitters)
            if emitter is not None:
                state["emitter_in_observer_set"] = emitter in emitters
        return state

    def _log_state(self) -> dict[str, Any]:
        level = self._logger.getEffectiveLevel()
        attached = self._records in self._logger.handlers
        with self._records._guard:
            count, rows = self._records.count, list(self._records.rows)
        return {
            "logger_disabled": self._logger.disabled,
            "effective_level": logging.getLevelName(level),
            "handler_attached": attached,
            "manager_disable": logging.getLevelName(logging.root.manager.disable),
            "handler_failed": self._records.failed,
            "usable": attached
            and not self._records.failed
            and self._logger.isEnabledFor(logging.ERROR),
            "records": count,
            "rows": rows,
        }

    def _post_canary(self) -> dict[str, Any]:
        """Rewrite a fresh ignored file until its native delivery, within a bound."""
        path = self._root / f".palace-post-{uuid.uuid4().hex}"
        with self._lock:
            self._post_real = os.path.realpath(path)
            ready = self._stages.get("ready")
            register = self._stages.get("native.register.enter")
        start = time.monotonic()
        limit = start + _POST_CANARY_BOUND
        delivered = False
        while True:
            path.write_text(uuid.uuid4().hex)
            remaining = limit - time.monotonic()
            if remaining <= 0:
                break
            if self._post_seen.wait(min(0.1, remaining)):
                delivered = True
                break
        latency = time.monotonic() - start
        with self._lock:
            words = list(self._flag_words.get("post_canary", []))
        readiness = (ready["first_ns"] - register["first_ns"]) / 1e9 if ready and register else None
        return {
            "delivered": delivered,
            "latency_s": round(latency, 3) if delivered else None,
            "bound_s": _POST_CANARY_BOUND,
            "readiness_latency_s": round(readiness, 3) if readiness is not None else None,
            "flag_words": words,
        }

    def _joint(
        self,
        target: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        logs: dict[str, Any] | None,
        post: dict[str, Any] | None,
    ) -> str:
        """First matching rule wins; any missing input is indeterminate."""
        with self._lock:
            raised = sorted(stage for stage in self._stages if stage.startswith("callback.raised."))
            probe_error = "probe.error" in self._stages or self._bookkeeping_failed
            native = f"{target}.native" in self._stages
            overflow = {name: n for name, n in self._flags["after_write"].items() if n}
            root_changed = {
                window: counts["root_changed"]
                for window, counts in self._window_counts.items()
                if counts["root_changed"]
            }
        # The value of each liveness field that indicates an in-process lifecycle fault.
        faulty = {"emitter_alive": False, "emitter_stop_requested": True, "observer_alive": False}
        keys = tuple(faulty)
        context = f"post-write overflow {overflow}; root-changed by window {root_changed}"
        if (
            before is None
            or after is None
            or logs is None
            or post is None
            or not logs.get("usable")
            or probe_error
            or any(key not in state for state in (before, after) for key in keys)
        ):
            return f"joint classification: indeterminate (an observation is missing); {context}"
        lifecycle = [
            f"{name} {key}={state[key]}"
            for name, state in (("at failure", before), ("after canary", after))
            for key in keys
            if state[key] is faulty[key]
        ]
        lifecycle += [
            f"{name} emitter not in observer set"
            for name, state in (("at failure", before), ("after canary", after))
            if state.get("emitter_in_observer_set") is False
        ]
        if any(before[key] != after[key] for key in keys):
            lifecycle.append("liveness changed between readings")
        if lifecycle:
            return f"joint classification: in-process lifecycle ({'; '.join(lifecycle)}); {context}"
        if logs["records"] or raised:
            return (
                f"joint classification: in-process lifecycle (watcher log records "
                f"{logs['records']}, raised {raised}); {context}"
            )
        if not post["delivered"]:
            return f"joint classification: unresolved: stream stall, cause unknown; {context}"
        if not native:
            return f"joint classification: target non-delivery, not causal; {context}"
        return (
            "joint classification: native delivery observed; see stage classification; " + context
        )

    def _safe_describe(self) -> str:
        try:
            return self.describe()
        except Exception as error:  # noqa: BLE001 — never mask the original failure
            return f"watcher stage observations unavailable: {error!r}"

    def describe(self) -> str:
        # Serialise under the lock: observer threads keep adding stage keys.
        with self._lock:
            summary = {
                "stages": self._stages,
                "native_events": self._events,
                "event_id_range": (
                    [min(self._event_ids), max(self._event_ids)] if self._event_ids else None
                ),
                "canary_event_id": self._canary_id,
                "overflow_flags": self._flags,
                "window_counts": self._window_counts,
                "flag_words": self._flag_words,
            }
            text = json.dumps(summary, sort_keys=True)
        return "watcher stage observations (relative ns): " + text

    def _mark(self, stage: str, now: int) -> None:
        row = self._stages.setdefault(stage, {"first_ns": now, "last_ns": now, "count": 0})
        row["last_ns"] = now
        row["count"] += 1

    def _native(self, emitter: Any, paths: list[Any], flags: list[int], ids: list[int]) -> None:
        now = time.monotonic_ns() - self._origin
        post_seen = False
        with self._lock:
            if self._emitter is None:
                self._emitter = emitter
            for raw, flag, event_id in zip(paths, flags, ids, strict=False):
                self._events += 1
                self._event_ids.append(event_id)
                if self._write_ns is not None:
                    window = "after_write"
                elif self._canary_id is not None and event_id > self._canary_id:
                    window = "after_ready"
                else:
                    window = "before_ready"
                for name, bit in _OVERFLOW_FLAGS:
                    if flag & bit:
                        self._flags[window][name] += 1
                self._window_counts[window]["events"] += 1
                if flag & _ROOT_CHANGED:
                    self._window_counts[window]["root_changed"] += 1
                real = os.path.realpath(raw.decode() if isinstance(raw, bytes) else raw)
                if real == self._canary_real:
                    if self._canary_id is None:
                        self._canary_id = event_id
                    self._mark("canary.native", now)
                    self._flag_word("canary", flag)
                elif real in self._targets:
                    self._mark(f"{self._targets[real]}.native", now)
                    self._flag_word(self._targets[real], flag)
                elif self._post_real is not None and real == self._post_real:
                    self._mark("post_canary.native", now)
                    self._flag_word("post_canary", flag)
                    post_seen = True
        if post_seen:
            self._post_seen.set()

    def _flag_word(self, name: str, flag: int) -> None:
        words = self._flag_words.setdefault(name, [])
        if len(words) < _FLAG_WORDS_PER_NAME:
            words.append(f"0x{flag:08x}")

    def _dispatch(self, event: Any) -> str | None:
        source = event.src_path
        real = os.path.realpath(source.decode() if isinstance(source, bytes) else source)
        if real == self._canary_real:
            self._dispatched.set()
            return "canary.dispatch"
        name = self._targets.get(real)
        return f"{name}.dispatch" if name else None

    def _target_stage(self, relative_path: str, stage: str) -> str | None:
        return f"{relative_path}.{stage}" if relative_path in self._names else None

    def _wrap(
        self,
        monkeypatch: pytest.MonkeyPatch,
        owner: Any,
        name: str,
        label_for: Callable[..., str | None],
    ) -> None:
        original = getattr(owner, name)
        probe = self

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                label = label_for(*args, **kwargs)
            except Exception:  # noqa: BLE001 — observation must never alter the call
                label = None
                probe.mark("probe.error")
            if label:
                probe.mark(label + ".enter")
            value = original(*args, **kwargs)
            if label:
                probe.mark(label + ".return")
            return value

        monkeypatch.setattr(owner, name, call)

    def _classify(
        self, target: str, deadline: float, completed: Callable[[], bool], window: float
    ) -> str:
        limit = time.monotonic() + window
        done_ns: int | None = None
        while done_ns is None and time.monotonic() < limit:
            try:
                if completed():
                    done_ns = time.monotonic_ns() - self._origin
                    break
            except (OSError, ValueError):  # a missing file or torn JSON line is not completion
                pass
            time.sleep(0.05)
        deadline_ns = int(deadline * 1_000_000_000) - self._origin
        with self._lock:
            write_ns = self._write_ns
            native = self._stages.get(f"{target}.native")
            reached = [s for s in _PIPELINE_STAGES if f"{target}.{s}.enter" in self._stages]
            overflow = dict(self._flags["after_write"])
        if write_ns is None:
            return f"classification unavailable: no target write mark for {target}"
        if native is None:
            verdict = "native absent"
        else:
            offset = (native["first_ns"] - write_ns) / 1e9
            timing = "late" if native["first_ns"] > deadline_ns else "on time"
            verdict = f"native {timing} +{offset:.3f} s after write"
            if timing == "on time" and done_ns is None:
                verdict += f", stalled after {reached[-1] if reached else 'native'}"
        later = (
            f"completed +{(done_ns - write_ns) / 1e9:.3f} s after write (still a failure)"
            if done_ns is not None
            else f"not completed within {window} s diagnostic window"
        )
        return f"classification: {verdict}; {later}; post-write overflow flags {overflow}"


def _add_watch_root(store: Path, root: Path) -> None:
    config_path = default_config_path(store)
    config = WatchRootsConfig().with_added(root, store_root=store)
    config.save(config_path)


def test_serve_emits_jsonl_line_within_two_seconds_of_touch(
    tmp_store: Path, tmp_watch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_watch_root(tmp_store, tmp_watch_root)
    probe = _NativeProbe(monkeypatch, tmp_watch_root, targets=("notes.md",))
    events_file = _today_events_path(tmp_store)
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        probe.wait_ready()
        deadline = time.monotonic() + 2.0
        probe.write("notes.md", "hello\n", target=True)
        try:
            record = _poll_for_event(events_file, relative_path="notes.md", deadline=deadline)
        except AssertionError as exc:
            probe.annotate(
                exc,
                target="notes.md",
                deadline=deadline,
                completed=lambda: _record_present(events_file, "notes.md"),
            )
            raise
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


def test_serve_emits_no_line_for_dotfile_change(
    tmp_store: Path, tmp_watch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_watch_root(tmp_store, tmp_watch_root)
    probe = _NativeProbe(monkeypatch, tmp_watch_root, targets=(".DS_Store",))
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        probe.wait_ready()
        probe.write(".DS_Store", "")
        time.sleep(1.0)
        events_file = _today_events_path(tmp_store)
        assert not events_file.is_file() or events_file.read_text() == ""
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    assert result.get("rc") == 0


def test_serve_emits_no_line_for_gitignored_change(
    tmp_store: Path, tmp_watch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_watch_root / ".gitignore").write_text("*.log\n")
    _add_watch_root(tmp_store, tmp_watch_root)
    probe = _NativeProbe(monkeypatch, tmp_watch_root, targets=("build.log",))
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        probe.wait_ready()
        probe.write("build.log", "noisy\n")
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
    tmp_store: Path,
    tmp_watch_root: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Add two roots: one that exists, one that we'll remove from disk.
    config = WatchRootsConfig().with_added(tmp_watch_root, store_root=tmp_store)
    missing_root = tmp_store.parent / "missing-root"
    missing_root.mkdir()
    config = config.with_added(missing_root, store_root=tmp_store)
    config.save(default_config_path(tmp_store))
    # Now make missing_root disappear.
    missing_root.rmdir()

    probe = _NativeProbe(monkeypatch, tmp_watch_root, targets=("alive.md",))
    events_file = _today_events_path(tmp_store)
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        probe.wait_ready()
        # The live root still emits.
        deadline = time.monotonic() + 2.0
        probe.write("alive.md", "alive\n", target=True)
        try:
            _poll_for_event(events_file, relative_path="alive.md", deadline=deadline)
        except AssertionError as exc:
            probe.annotate(
                exc,
                target="alive.md",
                deadline=deadline,
                completed=lambda: _record_present(events_file, "alive.md"),
            )
            raise
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    err = capfd.readouterr().err
    assert "skip-root" in err
    assert "missing-on-disk" in err
    assert result.get("rc") == 0


def test_serve_recovers_from_one_observer_callback_exception(
    tmp_store: Path, tmp_watch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler that raises on one workload event must not kill the daemon.

    The injection is armed only after readiness and fires only for first.md:
    watchdog also queues a watch-root directory event for the canary, which
    enters ``on_modified`` before the handler's ignore filter drops it.
    """
    _add_watch_root(tmp_store, tmp_watch_root)
    from palace.reindex import observer as observer_mod

    armed = threading.Event()
    raised: list[str] = []
    original_on_modified = observer_mod.ChangeHandler.on_modified

    def patched_on_modified(self: observer_mod.ChangeHandler, event: Any) -> None:
        source = event.src_path
        name = Path(source.decode() if isinstance(source, bytes) else source).name
        if armed.is_set() and not raised and name == "first.md":
            raised.append(name)
            raise RuntimeError("simulated handler failure")
        original_on_modified(self, event)

    probe = _NativeProbe(monkeypatch, tmp_watch_root, targets=("first.md", "second.md"))
    events_file = _today_events_path(tmp_store)
    observer_mod.ChangeHandler.on_modified = patched_on_modified  # type: ignore[method-assign]
    try:
        thread, shutdown, result = _start_serve(store=tmp_store)
        try:
            probe.wait_ready()
            armed.set()
            # First write triggers the raised on_modified path once; the
            # daemon must stay alive.
            probe.write("first.md", "first\n")
            time.sleep(1.0)
            # Second write should still be observed; the writer keeps draining.
            probe.write("second.md", "second\n", target=True)
            # FSEvents has up-to-1-second latency by default on macOS; give
            # the second write plenty of room before polling.
            time.sleep(0.5)
            deadline = time.monotonic() + 5.0
            try:
                _poll_for_event(events_file, relative_path="second.md", deadline=deadline)
            except AssertionError as exc:
                probe.annotate(
                    exc,
                    target="second.md",
                    deadline=deadline,
                    completed=lambda: _record_present(events_file, "second.md"),
                )
                raise
        finally:
            shutdown.set()
            thread.join(timeout=5.0)
    finally:
        observer_mod.ChangeHandler.on_modified = original_on_modified  # type: ignore[method-assign]
    assert raised == ["first.md"], f"injected failure raised on {raised}, not once on first.md"
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
    probe = _NativeProbe(monkeypatch, tmp_watch_root, targets=("burst.md",))
    thread, shutdown, result = _start_serve(store=tmp_store)
    events_file = _today_events_path(tmp_store)
    try:
        probe.wait_ready()
        deadline = time.monotonic() + 2.0
        probe.write("burst.md", "a\n", target=True)
        try:
            assert received.wait(timeout=max(0.0, deadline - time.monotonic())), (
                "native burst event did not enter debouncer within two seconds"
            )
        except AssertionError as exc:
            probe.annotate(exc, target="burst.md", deadline=deadline, completed=received.is_set)
            raise
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


def test_serve_and_bootstrap_can_share_events_log(
    tmp_store: Path, tmp_watch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reindex serve`` and ``bootstrap`` co-write the same events log cleanly.

    Regression gate against the "concurrent writers via the module-level
    ``_events_lock``" claim: every line in today's events file parses
    via ``json.loads`` after both producers have run.
    """
    _add_watch_root(tmp_store, tmp_watch_root)
    # Pre-populate a few files so the bootstrap has admitted work to do.
    for i in range(3):
        (tmp_watch_root / f"boot-{i}.md").write_text(str(i))

    probe = _NativeProbe(monkeypatch, tmp_watch_root, targets=("live.md",))
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        probe.wait_ready()
        # Touch a fresh file (FSEvents-driven event) concurrently with
        # the bootstrap.
        probe.write("live.md", "live\n")
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
    process-signal witness. Synchronize on a delivered and dispatched native
    canary rather than on registration or an assumed kernel latency.
    """
    _add_watch_root(tmp_store, tmp_watch_root)
    probe = _NativeProbe(monkeypatch, tmp_watch_root, targets=("ok.md",))
    events_file = _today_events_path(tmp_store)
    thread, shutdown, result = _start_serve(store=tmp_store)
    try:
        probe.wait_ready()
        probe.write("ok.md", "ok\n", target=True)
        deadline = time.monotonic() + 5.0
        try:
            _poll_for_event(events_file, relative_path="ok.md", deadline=deadline)
        except AssertionError as exc:
            probe.annotate(
                exc,
                target="ok.md",
                deadline=deadline,
                completed=lambda: _record_present(events_file, "ok.md"),
            )
            raise
    finally:
        shutdown.set()
        thread.join(timeout=5.0)
    assert not thread.is_alive(), "daemon did not stop after shutdown request"
    assert result.get("rc") == 0, result
