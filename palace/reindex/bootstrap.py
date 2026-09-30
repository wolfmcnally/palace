"""First-run full-index bootstrap walk (Phase 2.6).

Enumerates every file under every configured watch root the shared
:class:`palace.watch.ignore.IgnoreEngine` admits and emits one synthetic
``change_kind="created"`` :class:`palace.reindex.schema.ChangeEvent` per
file into the same ``<store>/events/<YYYY-MM-DD>.jsonl`` log the Phase
2.2 FSEvents daemon writes. The bootstrap is a *second producer* into
the events log; the Phase 2.3 + 2.4 ``palace index serve`` consumer is
byte-unchanged and processes the synthetic events through the same
dispatch path it uses for FSEvents-driven events.

Producer/consumer reuse posture:

- Records are byte-indistinguishable in shape from FSEvents-driven
  records. Same ``id`` derivation (canonical-JSON SHA-256), same
  ``event_type = "fs_change"``, same field set.
- The :class:`palace.reindex.writer.WriterWorker` is reused verbatim —
  the module-level ``palace.reindex.writer._events_lock`` already
  serializes appends across worker instances, so the bootstrap and the
  Phase 2.2 daemon can append to the same day file concurrently without
  interleaved-line corruption.
- The :class:`palace.watch.ignore.IgnoreEngine` is the sole filter source
  (no hand-rolled pattern matching). The engine is constructed
  fresh per walked root; :meth:`IgnoreEngine.refresh` is intentionally
  not invoked during the walk — the engine's read-on-every-call posture
  is correct for a one-shot bootstrap.

Resume mechanic: per-resolved-watch-root state lives in
``<store>/meta/bootstrap-cursor.json``, a top-level JSON dict keyed by
the absolute path string of the resolved watch root. Each entry carries
``started_at`` (ISO-8601 America/Boise), ``completed_at`` (null until
the walk finishes), ``last_emitted_relative_path``, ``files_emitted``,
and ``files_skipped``. The cursor is atomically rewritten via
``tempfile.NamedTemporaryFile`` + ``os.replace`` every
:data:`palace.reindex.config.BOOTSTRAP_CHECKPOINT_INTERVAL` emits, so an
interrupted walk loses at most ``CHECKPOINT_INTERVAL - 1`` in-flight
events.

The ``--force`` semantic: when ``force=True`` the orchestrator
:func:`bootstrap` calls :meth:`BootstrapCursor.forget` and
:meth:`BootstrapCursor.start` before invoking :func:`bootstrap_root`
against the affected roots; the per-root walk skips the resume-from
logic entirely and re-emits every admitted file.

Cursor data shape — non-frozen-vs-frozen reconciliation: the per-root
entry :class:`BootstrapCursorEntry` is a frozen dataclass (the shape on
disk is immutable from the file's point of view; updates produce a new
entry value via :func:`dataclasses.replace`). The container
:class:`BootstrapCursor` is non-frozen because its ``entries`` dict
needs to mutate as the walk progresses and because it carries the
``_store`` Path the per-method ``save`` call writes to; a frozen
container would force every mutating call to thread the store through
the public API, which is more ceremony than the surface needs. The
:meth:`BootstrapCursor.checkpoint` and :meth:`BootstrapCursor.complete`
signatures additionally accept ``files_skipped`` so the on-disk shape
the phase-file's data model carries surfaces directly rather than being
reconstructed.

Non-goals (kept out of 2.6 explicitly):

- No index-DB read or write — the bootstrap stays out of the
  ``palace.index`` surface entirely; that subpackage owns the events
  consumer.
- No Ollama probe — the bootstrap makes zero network calls.
- No FSEvents subscription — the bootstrap is a one-shot foreground walk
  that runs to completion and exits; the long-running supervised
  producer remains ``palace reindex serve``.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from palace.reindex._errors import ReindexError
from palace.reindex.config import (
    BOOTSTRAP_CHECKPOINT_INTERVAL,
    EVENTS_QUEUE_MAXSIZE,
    WRITER_QUEUE_PUT_TIMEOUT_SECONDS,
    bootstrap_cursor_path,
)
from palace.reindex.schema import ChangeEvent, build_change_event
from palace.reindex.writer import WriterWorker, _Sentinel
from palace.watch._errors import WatchError
from palace.watch.config import WatchRootsConfig, default_config_path
from palace.watch.ignore import GITIGNORE_FILENAME, IgnoreEngine

__all__ = [
    "BootstrapCursor",
    "BootstrapCursorEntry",
    "BootstrapResult",
    "bootstrap",
    "bootstrap_root",
    "enumerate_root",
]


_BOISE_TZ: ZoneInfo = ZoneInfo("America/Boise")


def _now_boise_iso() -> str:
    """Return the current America/Boise ISO-8601 timestamp.

    Matches :func:`palace.reindex.observer._observed_at_now`'s shape so
    bootstrap-emitted records carry timestamps in the same format the
    FSEvents-driven producer uses.
    """
    return datetime.now(_BOISE_TZ).isoformat(timespec="seconds")


# --------------------------------------------------------------------- dataclasses


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Per-root result returned by :func:`bootstrap_root`.

    ``files_walked`` equals ``files_emitted + files_skipped +
    files_resumed`` by construction. ``completed`` is ``True`` once the
    cursor's ``completed_at`` has been stamped; it is currently always
    ``True`` because :func:`bootstrap_root` only returns after the walk
    finishes (an interrupted walk surfaces as a Python exception, not a
    partial result).
    """

    watch_root: Path
    files_walked: int
    files_emitted: int
    files_skipped: int
    files_resumed: int
    seconds: float
    completed: bool


@dataclass(frozen=True, slots=True)
class BootstrapCursorEntry:
    """One per-root entry in :class:`BootstrapCursor`'s on-disk form."""

    started_at: str
    completed_at: str | None
    last_emitted_relative_path: str | None
    files_emitted: int
    files_skipped: int


@dataclass
class BootstrapCursor:
    """In-memory view of ``<store>/meta/bootstrap-cursor.json``.

    Non-frozen: ``entries`` mutates as the walk progresses, and
    ``_store`` is set by :meth:`load` so subsequent ``save`` /
    ``checkpoint`` / ``complete`` / ``forget`` calls do not have to
    thread the store path through every method signature.
    """

    entries: dict[str, BootstrapCursorEntry]
    _store: Path | None = None

    # ------------------------------------------------------------------ I/O

    @classmethod
    def load(cls, store: Path) -> BootstrapCursor:
        """Read ``<store>/meta/bootstrap-cursor.json`` into memory.

        Missing file resolves to an empty cursor (the steady state on a
        fresh ``--store``). Malformed JSON raises :class:`ReindexError`
        carrying the file path.
        """
        path = bootstrap_cursor_path(store)
        if not path.exists():
            return cls(entries={}, _store=store)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReindexError(f"cannot read bootstrap cursor at {path}: {exc}") from exc
        if not text.strip():
            return cls(entries={}, _store=store)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReindexError(f"bootstrap cursor at {path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ReindexError(f"bootstrap cursor at {path} is not a top-level JSON object")
        entries: dict[str, BootstrapCursorEntry] = {}
        for key, raw in data.items():
            if not isinstance(key, str):  # pragma: no cover — json keys are always str
                raise ReindexError(f"bootstrap cursor at {path}: non-string key")
            if not isinstance(raw, dict):
                raise ReindexError(f"bootstrap cursor at {path}: entry {key!r} is not an object")
            try:
                entry = BootstrapCursorEntry(
                    started_at=str(raw["started_at"]),
                    completed_at=(
                        None if raw.get("completed_at") is None else str(raw["completed_at"])
                    ),
                    last_emitted_relative_path=(
                        None
                        if raw.get("last_emitted_relative_path") is None
                        else str(raw["last_emitted_relative_path"])
                    ),
                    files_emitted=int(raw.get("files_emitted", 0)),
                    files_skipped=int(raw.get("files_skipped", 0)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ReindexError(
                    f"bootstrap cursor at {path}: entry {key!r} is malformed: {exc}"
                ) from exc
            entries[key] = entry
        return cls(entries=entries, _store=store)

    def save(self) -> None:
        """Write the cursor to disk atomically.

        ``tempfile.NamedTemporaryFile`` sibling + ``os.replace`` mirrors
        :meth:`palace.watch.config.WatchRootsConfig.save` so a partial
        write never leaves the file half-mutated. Lazily creates the
        ``<store>/meta/`` directory via
        :func:`palace.reindex.config.bootstrap_cursor_path`.
        """
        if self._store is None:
            raise ReindexError("BootstrapCursor.save: store path not set; load() first")
        path = bootstrap_cursor_path(self._store)
        # Sorted keys + no whitespace so a `diff` between runs is
        # meaningful. LF-terminated for parity with the JSONL surfaces.
        serialized = json.dumps(self._to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 — manual close + replace
            mode="w",
            dir=str(path.parent),
            delete=False,
            encoding="utf-8",
            suffix=".tmp",
        )
        try:
            tmp.write(serialized)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, path)

    def _to_dict(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, entry in self.entries.items():
            out[key] = {
                "started_at": entry.started_at,
                "completed_at": entry.completed_at,
                "last_emitted_relative_path": entry.last_emitted_relative_path,
                "files_emitted": entry.files_emitted,
                "files_skipped": entry.files_skipped,
            }
        return out

    # ------------------------------------------------------------------ mutation

    @staticmethod
    def _key(watch_root: Path) -> str:
        return str(watch_root.expanduser().resolve())

    def start(self, watch_root: Path, started_at: str) -> None:
        """Create (or reset) the per-root entry for a fresh walk."""
        key = self._key(watch_root)
        self.entries[key] = BootstrapCursorEntry(
            started_at=started_at,
            completed_at=None,
            last_emitted_relative_path=None,
            files_emitted=0,
            files_skipped=0,
        )
        self.save()

    def checkpoint(
        self,
        watch_root: Path,
        last_emitted_relative_path: str,
        files_skipped: int,
    ) -> None:
        """Bump the per-root entry after another batch of emits."""
        key = self._key(watch_root)
        previous = self.entries.get(key)
        if previous is None:
            # Should not happen — `start` runs before any checkpoint. Fall
            # back to a fresh entry rather than raising so a quirky test
            # double does not crash a live walk.
            previous = BootstrapCursorEntry(
                started_at=_now_boise_iso(),
                completed_at=None,
                last_emitted_relative_path=None,
                files_emitted=0,
                files_skipped=0,
            )
        updated = replace(
            previous,
            last_emitted_relative_path=last_emitted_relative_path,
            files_emitted=previous.files_emitted + 1,
            files_skipped=files_skipped,
        )
        self.entries[key] = updated
        self.save()

    def complete(self, watch_root: Path, files_skipped: int) -> None:
        """Mark the per-root walk complete."""
        key = self._key(watch_root)
        previous = self.entries.get(key)
        if previous is None:
            previous = BootstrapCursorEntry(
                started_at=_now_boise_iso(),
                completed_at=None,
                last_emitted_relative_path=None,
                files_emitted=0,
                files_skipped=0,
            )
        updated = replace(
            previous,
            completed_at=_now_boise_iso(),
            files_skipped=files_skipped,
        )
        self.entries[key] = updated
        self.save()

    def forget(self, watch_root: Path) -> None:
        """Drop the per-root entry. Idempotent."""
        key = self._key(watch_root)
        if key in self.entries:
            del self.entries[key]
            self.save()

    def entry(self, watch_root: Path) -> BootstrapCursorEntry | None:
        """Return the per-root entry, or ``None`` if absent."""
        return self.entries.get(self._key(watch_root))


# --------------------------------------------------------------------- enumeration


def _walk_admitted_paths(
    root_resolved: Path, engine: IgnoreEngine
) -> tuple[list[tuple[PurePosixPath, Path]], int]:
    """Collect every admitted ``(relative_posix, absolute)`` under ``root_resolved``.

    Returns the sorted-by-POSIX-path list of admitted pairs plus the
    count of files the engine rejected at the file level (subtree
    rejects via pruned directories are silent — counting them honestly
    would require touching the pruned subtree).

    ``os.walk(topdown=True, followlinks=False)`` with sorted ``dirnames``
    + sorted ``filenames`` gives a deterministic *walk* order. The
    result is sorted lexicographically by relative POSIX path before
    return so the resume-from string comparison can use ``<=``.

    Memory: O(admitted-file-count). At ~50 bytes per path string and
    100K files = ~5 MB, well below the bootstrap's budget.
    """
    collected: list[tuple[PurePosixPath, Path]] = []
    files_skipped = 0
    for current_dir, dirnames, filenames in os.walk(root_resolved, topdown=True, followlinks=False):
        dirnames.sort()
        filenames.sort()
        current_path = Path(current_dir)

        # Prune ignored directories in place so the walk never recurses
        # into them. The engine's universal-dotfile rule handles ``.git/``;
        # gitignore-listed directories (e.g. ``build/``) are pruned the
        # same way.
        kept_dirs: list[str] = []
        for name in dirnames:
            child = current_path / name
            try:
                if engine.is_ignored(child):
                    continue
            except WatchError:
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in filenames:
            if name == GITIGNORE_FILENAME:
                # Mirror palace.reindex.observer line 232: the
                # gitignore file's own mutation is not re-emitted. Not
                # counted as ``files_skipped`` because the file is a
                # palace-internal signal, not "this file would normally
                # be indexed but the engine said no."
                continue
            absolute_path = current_path / name
            try:
                if engine.is_ignored(absolute_path):
                    files_skipped += 1
                    continue
            except WatchError:
                files_skipped += 1
                continue
            try:
                relative = absolute_path.relative_to(root_resolved)
            except ValueError:
                continue
            collected.append((PurePosixPath(*relative.parts), absolute_path))

    collected.sort(key=lambda pair: pair[0].as_posix())
    return collected, files_skipped


def enumerate_root(
    *,
    root: Path,
    engine: IgnoreEngine,
    resume_from: str | None,
) -> Iterator[tuple[PurePosixPath, Path]]:
    """Yield ``(relative_posix_path, absolute_path)`` in sorted POSIX order.

    Walk order is deterministic — the walker collects every admitted
    ``(relative, absolute)`` pair via ``os.walk`` with sorted ``dirnames``
    and ``filenames`` (prune-on-engine-reject so ``.git/`` never
    recurses), then sorts globally by relative POSIX path. The sort
    makes the resume-from string comparison meaningful across nested
    trees: a cursor at ``"files/m.md"`` cleanly partitions the walk
    into "before" (skipped) and "after" (emitted).

    The walk skips ``.gitignore`` files themselves (mirroring
    :mod:`palace.reindex.observer` line 232 — palace does not currently
    re-emit a change record for the ignore file's own mutation).

    When ``resume_from`` is non-null, every yielded path whose POSIX
    form sorts at-or-before ``resume_from`` is dropped (the caller
    counts these via the seeded ``files_resumed`` from the cursor).
    """
    root_resolved = root.expanduser().resolve()
    pairs, _skipped = _walk_admitted_paths(root_resolved, engine)
    for relative_posix, absolute_path in pairs:
        if resume_from is not None and relative_posix.as_posix() <= resume_from:
            continue
        yield relative_posix, absolute_path


# --------------------------------------------------------------------- bootstrap_root


def bootstrap_root(
    *,
    watch_root: Path,
    store: Path,
    work_queue: queue.Queue[ChangeEvent | _Sentinel],
    cursor: BootstrapCursor,
    engine: IgnoreEngine,
    log: Callable[[str], None],
    force: bool = False,
    observed_at_clock: Callable[[], str] | None = None,
    ingest_clock: Callable[[], str] | None = None,
    checkpoint_interval: int = BOOTSTRAP_CHECKPOINT_INTERVAL,
) -> BootstrapResult:
    """Walk one watch root end-to-end and emit one event per admitted file.

    Per file: construct a synthetic ``ChangeEvent`` with
    ``change_kind="created"`` and submit it to ``work_queue`` via a
    bounded ``put`` with :data:`WRITER_QUEUE_PUT_TIMEOUT_SECONDS`
    timeout. On backpressure (``queue.Full``), force-emit a drop line to
    stderr and continue; the missed file is picked up on the next
    bootstrap run or on FSEvents-driven future change.

    Every ``checkpoint_interval`` emits the cursor is atomically
    rewritten via :meth:`BootstrapCursor.checkpoint`. The cursor is also
    marked ``completed_at`` at end via :meth:`BootstrapCursor.complete`.

    The caller owns the :class:`WriterWorker` that consumes
    ``work_queue`` and is responsible for stopping + draining it (and so
    for flushing any in-flight emits).
    """
    resolved_root = watch_root.expanduser().resolve()
    t0 = time.monotonic()

    # Both clocks default to America/Boise wall time. Tests override via
    # the parameters for determinism.
    obs_clock = observed_at_clock if observed_at_clock is not None else _now_boise_iso
    ing_clock = ingest_clock if ingest_clock is not None else _now_boise_iso

    entry = cursor.entry(resolved_root)
    if (
        not force
        and entry is not None
        and entry.completed_at is None
        and entry.last_emitted_relative_path is not None
    ):
        resume_from: str | None = entry.last_emitted_relative_path
    else:
        resume_from = None

    files_emitted = 0
    # ``files_resumed`` counts admitted paths the walker dropped because
    # the cursor said they were already emitted in a previous run. It is
    # initialized to zero and incremented per-yielded-path inside the
    # walk loop — the per-file pattern that matches ``files_emitted``.
    # The on-disk ``entry.files_emitted`` value (the count of checkpoint
    # calls, not the count of files emitted) is *not* a sound seed and
    # is intentionally not consulted here.
    files_resumed = 0

    # Collect every admitted (relative, absolute) pair sorted by POSIX
    # relative path. The skip count is surfaced by the helper —
    # rejections at the file level count, subtree prunes are silent
    # (counting them honestly would require touching the pruned subtree
    # the engine just told us to ignore).
    pairs, files_skipped = _walk_admitted_paths(resolved_root, engine)

    for relative_posix, absolute_path in pairs:
        rel_posix_str = relative_posix.as_posix()

        if resume_from is not None and rel_posix_str <= resume_from:
            # Already emitted in a previous run; do not re-emit, do not
            # count as ``files_skipped``. Count as ``files_resumed`` so
            # the per-root summary tells the truth about work the cursor
            # let the walk shortcut.
            files_resumed += 1
            continue

        event = build_change_event(
            ingest_time=ing_clock(),
            change_kind="created",
            watch_root=str(resolved_root),
            path=str(absolute_path),
            relative_path=rel_posix_str,
            is_directory=False,
            observed_at=obs_clock(),
            rename_src_path=None,
            rename_dest_path=None,
        )
        try:
            work_queue.put(event, timeout=WRITER_QUEUE_PUT_TIMEOUT_SECONDS)
        except queue.Full:
            # Force-emit the drop line through stderr regardless of
            # verbosity. The line shape mirrors palace.reindex.server's
            # ``drop reason=queue-full`` convention but uses a
            # ``bootstrap-drop`` discriminator so the operator can
            # grep for bootstrap-specific drops.
            print(
                f"palace reindex: bootstrap-drop reason=queue-full path={rel_posix_str}",
                file=sys.stderr,
                flush=True,
            )
            continue

        files_emitted += 1
        log(f"palace reindex: bootstrap-emit path={rel_posix_str}")

        if checkpoint_interval > 0 and files_emitted % checkpoint_interval == 0:
            cursor.checkpoint(resolved_root, rel_posix_str, files_skipped)

    cursor.complete(resolved_root, files_skipped)
    elapsed = time.monotonic() - t0
    files_walked = files_emitted + files_skipped + files_resumed
    return BootstrapResult(
        watch_root=resolved_root,
        files_walked=files_walked,
        files_emitted=files_emitted,
        files_skipped=files_skipped,
        files_resumed=files_resumed,
        seconds=elapsed,
        completed=True,
    )


# --------------------------------------------------------------------- bootstrap


def _default_log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _force_log(message: str) -> None:
    """Always-print stderr logger; bypasses verbose gating.

    Used for startup, summary, refuse-root, skip-root, and
    already-bootstrapped lines — operator-visible regardless of
    ``--verbose``.
    """
    print(message, file=sys.stderr, flush=True)


def bootstrap(
    *,
    store: Path,
    watch_root_filter: Path | None = None,
    force: bool = False,
    verbose: bool = False,
    log: Callable[[str], None] | None = None,
) -> int:
    """Walk every configured watch root and emit one event per admitted file.

    Returns 0 on success across every walked root. Raises
    :class:`ReindexError` for hard failures (unreadable config,
    malformed cursor); the CLI shim translates these into ``error: ...``
    lines + exit 1.

    ``watch_root_filter`` restricts the walk to one root; the path must
    match (by resolved absolute form) a configured watch root, else
    :class:`ReindexError` is raised.

    ``force=True`` clears each walked root's cursor entry on entry, so
    the walk runs full regardless of any prior ``completed_at``.

    ``verbose=True`` adds one ``palace reindex: bootstrap-emit
    path=<rel>`` line per emit to stderr; without it the bootstrap is
    quiet at steady state (startup line + per-root summary line +
    shutdown).
    """
    store.mkdir(parents=True, exist_ok=True)

    forced_log: Callable[[str], None] = _force_log
    verbose_log: Callable[[str], None]
    if log is not None:
        # Caller-supplied logger: respect it for both verbose and
        # forced lines so tests can capture every line through one sink.
        forced_log = log
        verbose_log = log if verbose else (lambda _msg: None)
    else:
        verbose_log = _default_log if verbose else (lambda _msg: None)

    config_path = default_config_path(store)
    try:
        config = WatchRootsConfig.load(config_path)
    except WatchError as exc:
        raise ReindexError(str(exc)) from exc

    if not config.roots:
        forced_log("palace reindex: no watch roots configured; nothing to bootstrap")
        return 0

    store_resolved = store.expanduser().resolve()
    configured: list[Path] = [root.path.expanduser().resolve() for root in config.roots]

    if watch_root_filter is not None:
        target = watch_root_filter.expanduser().resolve()
        if target not in configured:
            raise ReindexError(
                f"not a configured watch root: {watch_root_filter}; run 'palace watch add' first"
            )
        walk_set: list[Path] = [target]
    else:
        walk_set = configured

    cursor = BootstrapCursor.load(store)

    work_queue: queue.Queue[ChangeEvent | _Sentinel] = queue.Queue(maxsize=EVENTS_QUEUE_MAXSIZE)
    writer = WriterWorker(store=store, work_queue=work_queue)
    writer.start()
    try:
        for resolved_root in walk_set:
            # Under-store check (defense-in-depth: the watch CLI rejects
            # this at add-time, but a hand-edited TOML could smuggle one
            # through).
            try:
                resolved_root.relative_to(store_resolved)
            except ValueError:
                under_store = False
            else:
                under_store = True
            if under_store:
                forced_log(f"palace reindex: refuse-root path={resolved_root} reason=under-store")
                continue

            if not resolved_root.is_dir():
                forced_log(f"palace reindex: skip-root path={resolved_root} reason=missing-on-disk")
                continue

            entry = cursor.entry(resolved_root)
            if entry is not None and entry.completed_at is not None and not force:
                forced_log(
                    f"palace reindex: root={resolved_root} already bootstrapped at"
                    f" {entry.completed_at}; pass --force to redo"
                )
                continue

            if force:
                cursor.forget(resolved_root)

            cursor.start(resolved_root, _now_boise_iso())
            engine = IgnoreEngine(resolved_root)

            result = bootstrap_root(
                watch_root=resolved_root,
                store=store,
                work_queue=work_queue,
                cursor=cursor,
                engine=engine,
                log=verbose_log,
                force=force,
            )

            forced_log(
                "palace reindex:"
                f" bootstrapped root={result.watch_root}"
                f" files={result.files_walked}"
                f" emitted={result.files_emitted}"
                f" skipped={result.files_skipped}"
                f" resumed={result.files_resumed}"
                f" seconds={result.seconds:.2f}"
            )
        return 0
    finally:
        writer.stop()
