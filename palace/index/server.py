"""Index daemon with a fail-closed identity gate and observable park lifecycle.

The daemon refuses remote-configured stores, publishes ``starting`` / ``parked`` /
``indexing`` state, and compares all four embedding-identity axes before it creates
an embedder, writer, or event tail. An identity mismatch parks without holding
SQLite open, periodically rechecks the certificate read-only, and automatically
resumes after a certified full rebuild. Writer readiness is bounded and observed
before the tail is constructed; a startup failure parks terminally until operator
restart, so it cannot become an unbounded retry loop.
"""

from __future__ import annotations

import contextlib
import queue
import signal
import sqlite3
import sys
import threading
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from types import FrameType
from typing import Any

import sqlite_vec

from palace.index._errors import IndexError
from palace.index.config import (
    PARK_RECHECK_SECONDS,
    WRITER_BUSY_TIMEOUT_SECONDS,
    WRITER_READY_TIMEOUT_SECONDS,
    prepare_chunks_db_path,
)
from palace.index.cursor import TailCursor
from palace.index.daemon_state import ParkKind, clear_state, new_state, write_state
from palace.index.embedder import Embedder
from palace.index.embedder_config import (
    load_embedder_config,
    resolve_embedder,
    resolve_identity,
)
from palace.index.schema import (
    RecordedIdentity,
    identity_divergences,
    identity_mismatch_line,
    init_db,
    read_identity,
)
from palace.index.tail import EventsTail
from palace.index.writer import WriterWorker, _Sentinel, _WorkItem
from palace.writer_lock import writer_lock

__all__ = ["serve"]


def _stderr_log(line: str) -> None:
    """Default log sink: stderr, line-flushed."""
    print(line, file=sys.stderr, flush=True)


class _ParkOutcome(Enum):
    RESUME = "resume"
    SHUTDOWN = "shutdown"


def _bootstrap_schema(store: Path) -> RecordedIdentity:
    """Open a one-shot sqlite connection and run :func:`init_db` on it.

    Run synchronously in :func:`serve` *before* the writer thread starts
    so a schema-version mismatch raises before the daemon prints the
    ``tailing`` line. The writer thread opens its own connection on its
    body and re-runs :func:`init_db`; idempotent via
    ``CREATE … IF NOT EXISTS`` and the schema-version round-trip.
    """
    conn = sqlite3.connect(str(prepare_chunks_db_path(store)), timeout=WRITER_BUSY_TIMEOUT_SECONDS)
    try:
        # Bootstrap may stamp an empty store, so it takes the writer lock like
        # every other palace writer; a build holding it makes startup wait.
        with writer_lock(store, operation="daemon-startup"):
            init_db(conn, store=store)
            return read_identity(conn)
    finally:
        conn.close()


def _recorded_identity_readonly(store: Path) -> RecordedIdentity:
    """Read the four-row certificate without taking a write connection."""
    db_path = store / "index" / "chunks.sqlite"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return read_identity(conn)
    finally:
        conn.close()


def _park(
    *,
    store: Path,
    kind: ParkKind,
    reason: str,
    shutdown_event: threading.Event,
    log: Callable[[str], None],
    recheck_seconds: float,
) -> _ParkOutcome:
    """Publish a cause-specific parked state and wait for its recovery condition."""
    write_state(store, new_state("parked", park_kind=kind, reason=reason))
    if kind == "writer-startup":
        log(
            "palace index: PARKED — "
            f"{reason} — writer startup retries are disabled; correct the cause "
            "and restart the daemon"
        )
        shutdown_event.wait()
        return _ParkOutcome.SHUTDOWN

    if "found='" in reason and "expected='" in reason:
        rendered_reason = identity_mismatch_line(tuple(reason.split("; ")))
    else:
        rendered_reason = reason
    log(
        "palace index: PARKED — "
        f"{rendered_reason} — no events will be indexed until "
        "'palace index build --full' completes; "
        f"re-checking every {recheck_seconds:g}s"
    )
    while True:
        if shutdown_event.wait(timeout=recheck_seconds):
            return _ParkOutcome.SHUTDOWN
        try:
            recorded = _recorded_identity_readonly(store)
            expected = resolve_identity(store)
            divergences = identity_divergences(found=recorded, expected=expected)
        except (IndexError, sqlite3.Error, OSError) as exc:
            log(f"palace index: park recheck failed — {exc}")
            continue
        if not divergences:
            log("palace index: RESUMING — store identity re-stamped")
            return _ParkOutcome.RESUME


def serve(
    *,
    store: Path,
    verbose: bool = False,
    embedder: Embedder | None = None,
    _shutdown_event: threading.Event | None = None,
) -> int:
    """Run the index daemon in the foreground until SIGINT/SIGTERM.

    Returns the exit code (always 0 on clean shutdown). Raises
    :class:`IndexError` on the unrecoverable startup paths
    (Ollama unreachable, model not pulled, schema mismatch); the CLI
    shim translates those into ``error:`` lines.

    ``embedder`` and ``_shutdown_event`` are test seams.
    """
    store.mkdir(parents=True, exist_ok=True)

    log: Callable[[str], None] = _stderr_log
    write_state(store, new_state("starting"))

    config = load_embedder_config(store)
    if config.provider != "ollama":
        clear_state(store)
        raise IndexError(
            "palace index serve does not support a remote embedding provider "
            "(store selects a remote provider) — remote "
            "embedding is build-only in this phase; run 'palace index build'"
        )

    shutdown_event = _shutdown_event if _shutdown_event is not None else threading.Event()

    installed_signals: list[tuple[int, Any]] = []
    if _shutdown_event is None:

        def _on_signal(signum: int, _frame: FrameType | None) -> None:
            print(
                f"palace index: received signal {signum}; shutting down",
                file=sys.stderr,
                flush=True,
            )
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.signal(sig, _on_signal)
            installed_signals.append((int(sig), previous))

    # The writer is single-threaded, so cursor saves serialize naturally
    # on its body. Saving inline in ``on_completed`` (rather than via a
    # completion queue drained by the main loop) keeps the on-disk
    # cursor current even when the tail goes idle for long stretches
    # while the writer continues to chew through queued work — the bug
    # that left the cursor stale across the first Obsidian-vault walk.
    def on_completed(item: _WorkItem) -> None:
        if item.day is None or item.byte_offset is None:
            return
        try:
            TailCursor(
                day=item.day,
                byte_offset=item.byte_offset,
                last_event_id=item.event_id,
            ).save(store)
        except OSError as exc:
            log(f"palace index: cursor-save failed reason={exc!r}")

    injected_embedder = embedder
    try:
        while not shutdown_event.is_set():
            write_state(store, new_state("starting"))
            recorded = _bootstrap_schema(store)
            expected = resolve_identity(store)
            divergences = identity_divergences(found=recorded, expected=expected)
            if divergences:
                outcome = _park(
                    store=store,
                    kind="identity",
                    reason="; ".join(divergences),
                    shutdown_event=shutdown_event,
                    log=log,
                    recheck_seconds=PARK_RECHECK_SECONDS,
                )
                if outcome is _ParkOutcome.SHUTDOWN:
                    return 0
                continue

            owned_embedder = injected_embedder is None
            active_embedder: Embedder
            if injected_embedder is None:
                active_embedder, expected = resolve_embedder(store)
            else:
                active_embedder = injected_embedder
            log(
                "palace index: probing embedder "
                f"provider={expected.provider} model={expected.model}"
            )
            try:
                active_embedder.probe()
            except IndexError:
                if owned_embedder:
                    active_embedder.close()
                raise
            log(
                "palace index: embedder OK "
                f"(provider={expected.provider} model={expected.model} dim={expected.dim})"
            )

            cursor = TailCursor.load(store)
            log(f"palace index: starting from {cursor.day}:{cursor.byte_offset}")
            work_queue: queue.Queue[_WorkItem | _Sentinel] = queue.Queue(maxsize=4096)
            writer = WriterWorker(
                store=store,
                embedder=active_embedder,
                identity=expected,
                work_queue=work_queue,
                on_completed=on_completed,
                log=log,
                verbose=verbose,
            )
            writer.start()
            if not writer.wait_ready(timeout=WRITER_READY_TIMEOUT_SECONDS):
                reason = f"writer did not become ready within {WRITER_READY_TIMEOUT_SECONDS:g}s"
                writer.stop()
                if owned_embedder:
                    active_embedder.close()
                outcome = _park(
                    store=store,
                    kind="writer-startup",
                    reason=reason,
                    shutdown_event=shutdown_event,
                    log=log,
                    recheck_seconds=PARK_RECHECK_SECONDS,
                )
                if outcome is _ParkOutcome.SHUTDOWN:
                    return 0
                continue
            if writer.halt_reason is not None:
                reason = writer.halt_reason.removeprefix("embedding-identity mismatch: ")
                reason = reason.split(" — rebuild it with", maxsplit=1)[0]
                writer.stop()
                if owned_embedder:
                    active_embedder.close()
                outcome = _park(
                    store=store,
                    kind="identity",
                    reason=reason,
                    shutdown_event=shutdown_event,
                    log=log,
                    recheck_seconds=PARK_RECHECK_SECONDS,
                )
                if outcome is _ParkOutcome.SHUTDOWN:
                    return 0
                continue

            tail = EventsTail(store=store, cursor=cursor, stop_event=shutdown_event)
            write_state(store, new_state("indexing"))
            log(f"palace index: tailing {store}/events/ from {cursor.day}:{cursor.byte_offset}")
            midrun_halt: str | None = None
            try:
                for event, new_offset in tail.follow():
                    if writer.halt_reason is not None:
                        midrun_halt = writer.halt_reason
                        break
                    day = tail.position().day
                    _enqueue_with_position(
                        event=event,
                        store=store,
                        work_queue=work_queue,
                        log=log,
                        day=day,
                        byte_offset=new_offset,
                    )
            finally:
                writer.stop()
                if owned_embedder:
                    with contextlib.suppress(Exception):
                        active_embedder.close()
            if midrun_halt is not None and not shutdown_event.is_set():
                reason = midrun_halt.removeprefix("embedding-identity mismatch: ")
                reason = reason.split(" — rebuild it with", maxsplit=1)[0]
                outcome = _park(
                    store=store,
                    kind="identity",
                    reason=reason,
                    shutdown_event=shutdown_event,
                    log=log,
                    recheck_seconds=PARK_RECHECK_SECONDS,
                )
                if outcome is _ParkOutcome.SHUTDOWN:
                    return 0
                continue
            return 0
        return 0
    finally:
        clear_state(store)
        if injected_embedder is not None:
            try:
                injected_embedder.close()
            except Exception:  # noqa: BLE001
                log("palace index: embedder close failed")
        for signum, previous in installed_signals:
            signal.signal(signum, previous)


def _enqueue_with_position(
    *,
    event: Any,
    store: Path,
    work_queue: queue.Queue[_WorkItem | _Sentinel],
    log: Callable[[str], None],
    day: str,
    byte_offset: int,
) -> None:
    """Like :func:`process_change_event` but stamps cursor position on the work item.

    The work item carries ``day`` + ``byte_offset`` so the writer's
    ``on_completed`` callback can hand them back to the cursor.
    """
    # Inline the relevant pieces of process_change_event so we can stamp
    # the cursor data on the work item before it lands on the queue.
    # ``process_change_event`` does not return the queued item, hence the
    # local wrapper.
    from palace.index.pipeline import classify_kind  # local: avoid cycle at import time

    event_path = Path(event.path)
    watch_root = Path(event.watch_root)
    try:
        watch_resolved = watch_root.resolve()
        store_resolved = store.resolve()
        watch_resolved.relative_to(store_resolved)
    except (OSError, ValueError):
        pass
    else:
        try:
            rel = event_path.relative_to(watch_root).as_posix()
        except ValueError:
            rel = event_path.as_posix()
        log(f"palace index: drop reason=under-store path={rel}")
        return

    kind = classify_kind(event_path)

    if event.change_kind == "deleted":
        work_queue.put(
            _WorkItem(
                change_kind="deleted",
                path=event_path,
                watch_root=watch_root,
                file_kind=kind,
                event_id=event.id,
                day=day,
                byte_offset=byte_offset,
            )
        )
        return

    if kind in {"binary", "unknown"}:
        try:
            rel = event_path.relative_to(watch_root).as_posix()
        except ValueError:
            rel = event_path.as_posix()
        log(f"palace index: skip kind={kind} path={rel}")
        # We still want the cursor to advance past skipped events; put a
        # no-op work item so on_completed fires.
        work_queue.put(
            _WorkItem(
                change_kind=event.change_kind,
                path=event_path,
                watch_root=watch_root,
                file_kind=kind,
                event_id=event.id,
                day=day,
                byte_offset=byte_offset,
            )
        )
        return

    work_queue.put(
        _WorkItem(
            change_kind=event.change_kind,
            path=event_path,
            watch_root=watch_root,
            file_kind=kind,
            event_id=event.id,
            day=day,
            byte_offset=byte_offset,
        )
    )
