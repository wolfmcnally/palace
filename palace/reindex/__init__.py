"""FSEvents change-event daemon (Phase 2.2).

Subscribes to macOS FSEvents for every watch root in the watch-roots
config Phase 2.1 ships, debounces 100 ms per path, filters every event
through the shared :class:`palace.watch.ignore.IgnoreEngine`, and appends
a structured change-event line to ``<store>/events/<YYYY-MM-DD>.jsonl``.

Typed file-type pipelines, index writes, the launchd plist, and the
first-run full-index pass are intentionally out of scope for 2.2 (Phases
2.3, 2.5, and 2.6 respectively).
"""

from __future__ import annotations

from palace.reindex._errors import ReindexError
from palace.reindex.bootstrap import (
    BootstrapCursor,
    BootstrapCursorEntry,
    BootstrapResult,
    bootstrap,
    bootstrap_root,
    enumerate_root,
)
from palace.reindex.config import (
    BOOTSTRAP_CHECKPOINT_INTERVAL,
    DEBOUNCE_INTERVAL_SECONDS,
    EVENT_TYPE_FS_CHANGE,
    EVENTS_QUEUE_MAXSIZE,
    ChangeKind,
    events_path,
)
from palace.reindex.debounce import PathDebouncer
from palace.reindex.lifecycle import (
    PLIST_FILENAME,
    PLIST_LABEL,
    PLIST_TEMPLATE_PATH,
    RESTART_NOT_LOADED,
    RESTART_RESTARTED,
    cli_install,
    cli_uninstall,
    install,
    restart,
    uninstall,
)
from palace.reindex.observer import EVENT_KIND_MAP, ChangeHandler, build_observer
from palace.reindex.schema import (
    EVENT_FIELDS,
    ChangeEvent,
    build_change_event,
    to_jsonl_bytes,
)
from palace.reindex.server import serve
from palace.reindex.writer import WriterWorker

__all__ = [
    "BOOTSTRAP_CHECKPOINT_INTERVAL",
    "BootstrapCursor",
    "BootstrapCursorEntry",
    "BootstrapResult",
    "DEBOUNCE_INTERVAL_SECONDS",
    "EVENTS_QUEUE_MAXSIZE",
    "EVENT_FIELDS",
    "EVENT_KIND_MAP",
    "EVENT_TYPE_FS_CHANGE",
    "PLIST_FILENAME",
    "PLIST_LABEL",
    "PLIST_TEMPLATE_PATH",
    "RESTART_NOT_LOADED",
    "RESTART_RESTARTED",
    "ChangeEvent",
    "ChangeHandler",
    "ChangeKind",
    "PathDebouncer",
    "ReindexError",
    "WriterWorker",
    "bootstrap",
    "bootstrap_root",
    "build_change_event",
    "build_observer",
    "cli_install",
    "cli_uninstall",
    "enumerate_root",
    "events_path",
    "install",
    "restart",
    "serve",
    "to_jsonl_bytes",
    "uninstall",
]
