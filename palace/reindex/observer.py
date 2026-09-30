"""watchdog FSEvents handler and observer builder.

The handler subscribes to one watch root, classifies each inbound
``FileSystemEvent`` via the shared :class:`palace.watch.ignore.IgnoreEngine`,
and forwards survivors to the :class:`palace.reindex.debounce.PathDebouncer`.
Filter pipeline (in order, per ``plan/phase-2.2.md`` line 37):

1. **Event-kind map.** Only the five mapped event types reach
   ``_dispatch``; ``DirModifiedEvent`` and unknown subclasses are silently
   dropped (the leaf change always also fires a ``FileModifiedEvent`` and
   the directory event is just OS noise).
2. **In-tree check** — defense-in-depth. The watchdog observer should
   never deliver out-of-tree paths, but a hand-edited TOML could smuggle
   one in.
3. **``.gitignore`` self-mutation drop.** 2.2 does not re-emit a change
   record for the ignore file's own mutation; 2.3 or a later sub-phase
   will wire :meth:`IgnoreEngine.refresh` to consume these.
4. **:meth:`IgnoreEngine.is_ignored`.** Dotfile and gitignore semantics
   live in 2.1's engine; the daemon delegates without re-implementing.

The observer builder constructs one ``ChangeHandler`` per watch root,
schedules each with ``observer.schedule(handler, str(root), recursive=True)``,
and refuses to schedule roots that resolve under the daemon's
``--store`` (defense-in-depth against a hand-edited config that smuggled
the machine-state root in as a watch root). Missing-on-disk roots are
skipped with an operator-visible stderr line, mirroring the
``palace watch list  (missing on disk)`` posture.
"""

from __future__ import annotations

import platform
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers.api import BaseObserver

from palace.reindex._errors import ReindexError
from palace.reindex.config import ChangeKind
from palace.reindex.debounce import PathDebouncer
from palace.watch.config import WatchRootsConfig
from palace.watch.ignore import GITIGNORE_FILENAME, IgnoreEngine

__all__ = ["EVENT_KIND_MAP", "ChangeHandler", "build_observer"]


# Local copy of America/Boise so the observer does not pull in the
# capture daemon's server module just for a timezone constant.
_BOISE_TZ: ZoneInfo = ZoneInfo("America/Boise")


# ``DirModifiedEvent`` is intentionally absent: the leaf change always
# also fires a ``FileModifiedEvent`` and the directory event is just OS
# noise. Unknown future subclasses fall through and are silently dropped
# in ``_dispatch`` so a new watchdog release does not crash the daemon.
EVENT_KIND_MAP: Mapping[type[FileSystemEvent], ChangeKind] = {
    FileCreatedEvent: "created",
    DirCreatedEvent: "created",
    FileModifiedEvent: "modified",
    FileDeletedEvent: "deleted",
    DirDeletedEvent: "deleted",
}


# The drop-reason string passed to the log callback. ``None`` means
# "accepted" — the server's logger only emits accept lines at verbose
# verbosity; drops carry a reason string verbatim.
DropReason = str


def _observed_at_now() -> str:
    """Return the ISO-8601-with-offset stamp for ``observed_at``."""
    return datetime.now(_BOISE_TZ).isoformat(timespec="seconds")


class ChangeHandler(FileSystemEventHandler):
    """One watchdog handler per watch root.

    Holds the watch root's resolved path, its own :class:`IgnoreEngine`
    instance, and the shared :class:`PathDebouncer`. Refuses construction
    if the watch root is under the machine-state ``store_root``.
    """

    def __init__(
        self,
        *,
        watch_root: Path,
        store_root: Path,
        debouncer: PathDebouncer,
        log: Callable[[str, DropReason | None], None],
    ) -> None:
        super().__init__()
        watch_resolved = watch_root.expanduser().resolve()
        store_resolved = store_root.expanduser().resolve()
        try:
            watch_resolved.relative_to(store_resolved)
        except ValueError:
            pass
        else:
            raise ReindexError(f"watch root {watch_resolved} is under store {store_resolved}")
        self._watch_root = watch_resolved
        self._debouncer = debouncer
        self._engine = IgnoreEngine(watch_resolved)
        self._log = log

    # ---------------------------------------------------------------- callbacks

    def dispatch(self, event: FileSystemEvent) -> None:
        # Wrap the watchdog dispatch with log-and-continue so one buggy
        # event does not kill the Observer thread (the daemon must stay
        # up; subsequent events still need to land). Without this,
        # watchdog's ``BaseObserver.run`` would surface the exception and
        # tear down the thread silently.
        try:
            super().dispatch(event)
        except Exception as exc:  # noqa: BLE001 — log-and-continue posture
            self._log(
                f"palace reindex: handler-failed reason={exc!r}",
                None,
            )

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_simple(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle_simple(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle_simple(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not isinstance(event, (FileMovedEvent, DirMovedEvent)):
            return
        is_directory = isinstance(event, DirMovedEvent)
        src = self._coerce_path(event.src_path)
        dest = self._coerce_path(event.dest_path)
        # Two records, never coalesced. Filter each half independently
        # through ``_should_emit`` (a rename whose source was ignored is
        # not magically un-ignored at the destination, and vice versa).
        self._dispatch(
            absolute_path=src,
            change_kind="deleted",
            is_directory=is_directory,
            rename_src_path=str(src),
        )
        self._dispatch(
            absolute_path=dest,
            change_kind="created",
            is_directory=is_directory,
            rename_dest_path=str(dest),
        )

    # ---------------------------------------------------------------- internals

    def _handle_simple(self, event: FileSystemEvent) -> None:
        kind = EVENT_KIND_MAP.get(type(event))
        if kind is None:
            # DirModifiedEvent and any future subclass — silently drop.
            return
        absolute_path = self._coerce_path(event.src_path)
        self._dispatch(
            absolute_path=absolute_path,
            change_kind=kind,
            is_directory=bool(event.is_directory),
        )

    def _dispatch(
        self,
        *,
        absolute_path: Path,
        change_kind: ChangeKind,
        is_directory: bool,
        rename_src_path: str | None = None,
        rename_dest_path: str | None = None,
    ) -> None:
        accepted, drop_reason = self._should_emit(absolute_path)
        if not accepted:
            try:
                relative = self._relative_for_log(absolute_path)
            except ValueError:
                relative = absolute_path.as_posix()
            self._log(
                f"palace reindex: drop reason={drop_reason} path={relative}",
                drop_reason,
            )
            return
        try:
            relative_path = self._relative_under_root(absolute_path)
        except ValueError:
            # Already filtered by ``_should_emit``; reaching here would be
            # a bug. Treat as drop rather than crashing the handler.
            return
        observed_at = _observed_at_now()
        self._log(
            f"palace reindex: accept change_kind={change_kind} path={relative_path.as_posix()}",
            None,
        )
        self._debouncer.submit(
            watch_root=self._watch_root,
            absolute_path=absolute_path,
            relative_path=relative_path,
            change_kind=change_kind,
            is_directory=is_directory,
            observed_at=observed_at,
            rename_src_path=rename_src_path,
            rename_dest_path=rename_dest_path,
        )

    def _should_emit(self, absolute_path: Path) -> tuple[bool, DropReason | None]:
        # In-tree check: defense-in-depth against a hand-edited TOML that
        # smuggled an unrelated path through. ``Path.absolute()`` does NOT
        # follow symlinks (intentional — see plan line 38).
        try:
            absolute_path.relative_to(self._watch_root)
        except ValueError:
            return False, "outside-root"
        # The watch root itself is never an event subject: it pre-exists by
        # definition when registered, and macOS FSEvents can replay its recent
        # creation to a freshly started stream (observed as a spurious
        # ``relative_path: "."`` created record).
        if absolute_path == self._watch_root:
            return False, "watch-root-itself"
        # ``.gitignore`` self-mutation drop: 2.2 does not re-emit a change
        # record for the ignore file's own mutation; 2.3 or later will
        # wire :meth:`IgnoreEngine.refresh` to consume these.
        if absolute_path.name == GITIGNORE_FILENAME:
            return False, "gitignore-itself"
        if self._engine.is_ignored(absolute_path):
            return False, self._engine.reason(absolute_path) or "ignored"
        return True, None

    def _relative_under_root(self, absolute_path: Path) -> PurePosixPath:
        relative = absolute_path.relative_to(self._watch_root)
        return PurePosixPath(*relative.parts)

    def _relative_for_log(self, absolute_path: Path) -> str:
        relative = absolute_path.relative_to(self._watch_root)
        return PurePosixPath(*relative.parts).as_posix()

    @staticmethod
    def _coerce_path(value: str | bytes) -> Path:
        if isinstance(value, bytes):
            return Path(value.decode("utf-8")).absolute()
        return Path(value).absolute()


def build_observer(
    *,
    config: WatchRootsConfig,
    store_root: Path,
    debouncer: PathDebouncer,
    log: Callable[[str, DropReason | None], None],
) -> tuple[BaseObserver, int]:
    """Build an ``FSEventsObserver`` from a loaded :class:`WatchRootsConfig`.

    Returns the observer plus the count of successfully-scheduled roots.
    Roots under ``store_root`` log ``refuse-root`` and are skipped; roots
    whose path no longer resolves to a directory log ``skip-root`` and
    are skipped. The observer is returned unstarted; the caller invokes
    ``observer.start()``.
    """
    require_fsevents()
    from watchdog.observers.fsevents import FSEventsObserver

    observer = FSEventsObserver()
    store_resolved = store_root.expanduser().resolve()
    scheduled = 0
    for root in config.roots:
        root_path = root.path
        try:
            resolved = root_path.expanduser().resolve()
        except OSError:
            # Startup-time root decisions are operator-visible regardless
            # of verbosity (drop_reason=None forces the line through).
            log(
                f"palace reindex: skip-root path={root_path} reason=missing-on-disk",
                None,
            )
            continue
        try:
            resolved.relative_to(store_resolved)
        except ValueError:
            under_store = False
        else:
            under_store = True
        if under_store:
            log(
                f"palace reindex: refuse-root path={root_path} reason=under-store",
                None,
            )
            continue
        if not resolved.is_dir():
            log(
                f"palace reindex: skip-root path={root_path} reason=missing-on-disk",
                None,
            )
            continue
        handler = ChangeHandler(
            watch_root=resolved,
            store_root=store_resolved,
            debouncer=debouncer,
            log=log,
        )
        observer.schedule(handler, str(resolved), recursive=True)
        scheduled += 1
    return observer, scheduled


def require_fsevents() -> None:
    """Refuse the macOS watcher before importing its extension or writing state."""
    if platform.system() != "Darwin":
        raise ReindexError(
            "FSEvents watching requires macOS; use palace index build for synchronous indexing"
        )
