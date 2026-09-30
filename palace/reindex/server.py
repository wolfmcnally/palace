"""Reindex daemon top-level: load config, wire observer, block on signal.

The lifecycle mirrors :mod:`palace.daemons.capture.server` shape:

1. Ensure the machine-state ``store`` exists; load
   :class:`palace.watch.config.WatchRootsConfig`.
2. Print ``palace reindex: loaded N roots; config changes require daemon
   restart`` to stderr immediately after load (Minor Correction #3:
   ordering matters for the smoke and integration tests).
3. Install SIGINT/SIGTERM handlers that set a :class:`threading.Event`.
   The handlers are installed *before* the pipeline is wired so the
   0-watch-roots branch can idle on the same shutdown event rather than
   exit-and-relaunch under launchd's ``KeepAlive``.
4. If ``config.roots`` is empty: print ``palace reindex: idle (no
   watch roots configured)`` and block on the shutdown event. No
   pipeline threads are started. The daemon stays alive until a signal;
   ``launchctl kickstart -k`` after a ``palace watch add`` restarts it
   so the new config is picked up.
5. Otherwise, construct the writer queue, the writer worker, and the
   debouncer (whose sink puts events onto the queue with a bounded
   :data:`WRITER_QUEUE_PUT_TIMEOUT_SECONDS` timeout — Minor Correction #1).
6. Construct the observer via :func:`build_observer`. If every root was
   refused or missing (``scheduled_count == 0``), print ``palace reindex:
   idle (all configured watch roots refused or missing)`` and block on
   the shutdown event with the writer/debouncer kept alive but unused.
7. Otherwise, start the observer and print ``palace reindex: observing
   N roots`` (Minor Correction #3: this line comes AFTER the load line;
   smoke and tests poll for this exact text).
8. Block on the shutdown event; on signal, stop the observer (if
   running), flush in-flight debouncer accumulators (if started), drain
   the writer queue (if started), restore prior signal handlers, and
   return 0.

Tests inject ``_shutdown_event`` via the optional keyword argument so
they do not need real signals; the smoke and the live daemon use the
default real-signal path.
"""

from __future__ import annotations

import queue
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

from palace.reindex._errors import ReindexError
from palace.reindex.config import (
    EVENTS_QUEUE_MAXSIZE,
    WRITER_QUEUE_PUT_TIMEOUT_SECONDS,
)
from palace.reindex.debounce import PathDebouncer
from palace.reindex.observer import build_observer, require_fsevents
from palace.reindex.schema import ChangeEvent
from palace.reindex.writer import WriterWorker, _Sentinel
from palace.watch._errors import WatchError
from palace.watch.config import WatchRootsConfig, default_config_path

__all__ = ["serve"]


def _stderr_logger(verbose: bool) -> Callable[[str, str | None], None]:
    """Return a closure that prints to stderr, gated by verbosity.

    Drop lines (``drop_reason is not None``) are quiet by default and
    appear only under ``--verbose``. Accept and startup lines
    (``drop_reason is None``) always print.
    """

    def _log(message: str, drop_reason: str | None) -> None:
        if drop_reason is None or verbose:
            print(message, file=sys.stderr, flush=True)

    return _log


def _enqueue(
    work_queue: queue.Queue[ChangeEvent | _Sentinel],
    event: ChangeEvent,
    log: Callable[[str, str | None], None],
) -> None:
    """Bounded put onto the writer queue; log-and-drop on backpressure.

    Minor Correction #1: never block forever, never raise. A full queue
    surfaces as a stderr drop line (not gated by ``--verbose`` — this is a
    real drop the operator must see).
    """
    try:
        work_queue.put(event, timeout=WRITER_QUEUE_PUT_TIMEOUT_SECONDS)
    except queue.Full:
        # Force the drop line through regardless of verbosity by passing
        # ``drop_reason=None`` (the logger gates only on
        # ``drop_reason is None`` for "always print"; the message itself
        # carries the reason for the operator).
        log(
            f"palace reindex: drop reason=queue-full path={event.relative_path}",
            None,
        )


def serve(
    *,
    store: Path,
    verbose: bool = False,
    _shutdown_event: threading.Event | None = None,
) -> int:
    """Run the reindex daemon in the foreground until SIGINT/SIGTERM.

    Returns 0 on clean shutdown. The 0-watch-roots and the
    ``scheduled_count == 0`` paths idle on the shutdown event rather
    than exit, so launchd's ``KeepAlive`` does not spin a restart loop
    on a freshly-installed daemon that has not yet been configured.
    Raises :class:`ReindexError` only for the unreadable-config path,
    which the CLI entry point translates into ``error:``. ``_shutdown_event``
    is a test seam — production callers leave it unset and the function
    installs real signal handlers.
    """
    require_fsevents()
    store.mkdir(parents=True, exist_ok=True)
    config_path = default_config_path(store)
    try:
        config = WatchRootsConfig.load(config_path)
    except WatchError as exc:
        raise ReindexError(str(exc)) from exc

    log = _stderr_logger(verbose)
    print(
        f"palace reindex: loaded {len(config.roots)} roots; config changes require daemon restart",
        file=sys.stderr,
        flush=True,
    )

    shutdown_event = _shutdown_event if _shutdown_event is not None else threading.Event()
    # ``signal.signal`` returns ``Callable | int | Handlers | None`` — too
    # narrow to type usefully without conditional imports. We store as
    # ``Any`` and pass back to ``signal.signal`` verbatim.
    installed_signals: list[tuple[int, Any]] = []
    if _shutdown_event is None:

        def _on_signal(signum: int, _frame: FrameType | None) -> None:
            print(
                f"palace reindex: received signal {signum}; shutting down",
                file=sys.stderr,
                flush=True,
            )
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.signal(sig, _on_signal)
            installed_signals.append((int(sig), previous))

    writer: WriterWorker | None = None
    debouncer: PathDebouncer | None = None
    observer: Any | None = None

    try:
        if not config.roots:
            print(
                "palace reindex: idle (no watch roots configured); waiting for signal",
                file=sys.stderr,
                flush=True,
            )
            shutdown_event.wait()
            return 0

        work_queue: queue.Queue[ChangeEvent | _Sentinel] = queue.Queue(maxsize=EVENTS_QUEUE_MAXSIZE)
        writer = WriterWorker(store=store, work_queue=work_queue)
        writer.start()

        debouncer = PathDebouncer(
            sink=lambda event: _enqueue(work_queue, event, log),
        )
        debouncer.start()

        observer_built, scheduled_count = build_observer(
            config=config,
            store_root=store,
            debouncer=debouncer,
            log=log,
        )

        if scheduled_count == 0:
            print(
                "palace reindex: idle (all configured watch roots refused or missing);"
                " waiting for signal",
                file=sys.stderr,
                flush=True,
            )
            shutdown_event.wait()
            return 0

        observer_built.start()
        observer = observer_built
        print(
            f"palace reindex: observing {scheduled_count} roots",
            file=sys.stderr,
            flush=True,
        )
        shutdown_event.wait()
        return 0
    finally:
        if observer is not None:
            observer.stop()
            observer.join()
        if debouncer is not None:
            debouncer.shutdown_flush()
            debouncer.stop()
        if writer is not None:
            writer.stop()
        for signum, previous in installed_signals:
            # Restore prior signal handlers so a test process that imports
            # ``serve`` directly does not leak handlers into the next test.
            signal.signal(signum, previous)
