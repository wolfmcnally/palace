"""Change-event → writer-queue dispatch.

Two pure functions:

- :func:`classify_kind` maps an absolute path to a :data:`FileKind`
  via its filename extension. Phase 2.4 routes Markdown, JSONL, code,
  and opaque text through to the writer; only ``"binary"`` and
  ``"unknown"`` are logged-and-skipped (the cursor still advances).
- :func:`process_change_event` takes one parsed :class:`ChangeEvent`
  (decoded by :mod:`palace.index.tail`) and either enqueues a
  :class:`_WorkItem` for the writer thread or drops the event with a
  log line. Deletion events are cross-kind: any path with chunks in
  the DB has them dropped, regardless of the inferred kind.

The self-write-loop guard refuses events whose ``watch_root`` resolves
under the daemon's ``--store``. Phase 2.1's CLI rejects this at config
time and Phase 2.2's observer refuses it at startup, but defense-in-depth
matters here too — a hand-edited line on the events log must not be able
to make the index daemon write chunks for paths under its own store.
"""

from __future__ import annotations

import queue
from collections.abc import Callable
from pathlib import Path

from palace.index.config import (
    BINARY_EXTENSIONS,
    CODE_EXTENSIONS,
    JSONL_EXTENSIONS,
    MARKDOWN_EXTENSIONS,
    TEXT_EXTENSIONS,
    FileKind,
)
from palace.index.writer import _Sentinel, _WorkItem
from palace.reindex.schema import ChangeEvent

__all__ = ["classify_kind", "process_change_event"]


# Kinds the dispatcher refuses to route through to the writer's per-kind
# pipelines. These still get a cursor-advancing no-op enqueue in
# :func:`palace.index.server._enqueue_with_position` so the daemon's
# tail cursor moves past the event, but no DB write happens.
_SKIP_KINDS: frozenset[str] = frozenset({"binary", "unknown"})


def classify_kind(path: Path) -> FileKind:
    """Return the :data:`FileKind` palace assigns to ``path``.

    Suffix-keyed; the extension sets are frozen literals in
    :mod:`palace.index.config`. ``BINARY_EXTENSIONS`` short-circuits
    before the code/text checks so a ``.zip`` never reaches the
    chunkers. The fallback for unrecognized suffixes is ``"unknown"``
    — meaning *truly* unknown, not the catch-all 2.3 used it as.
    """
    suffix = path.suffix.lower()
    if suffix in MARKDOWN_EXTENSIONS:
        return "markdown"
    if suffix in JSONL_EXTENSIONS:
        return "jsonl"
    if suffix in BINARY_EXTENSIONS:
        return "binary"
    if suffix in CODE_EXTENSIONS:
        return "code"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    return "unknown"


def process_change_event(
    *,
    event: ChangeEvent,
    store: Path,
    work_queue: queue.Queue[_WorkItem | _Sentinel],
    log: Callable[[str], None],
) -> None:
    """Enqueue (or drop) ``event`` for the writer thread.

    Decisions in order:

    1. **Self-write guard.** ``event.watch_root`` under ``store`` is
       silently dropped with a single ``drop reason=under-store`` log
       line.
    2. **Deletion.** Always enqueued (cross-kind), so the writer can
       drop any existing chunks for the path.
    3. **Routable kinds** (``markdown`` / ``jsonl`` / ``code`` /
       ``text``). Enqueued for the matching per-kind pipeline.
    4. **Skip kinds** (``binary`` / ``unknown``). Logged and skipped;
       no enqueue, no DB write (the server's enqueue helper uses a
       no-op work item to advance the cursor past these events).
    """
    event_path = Path(event.path)
    watch_root = Path(event.watch_root)
    try:
        watch_resolved = watch_root.resolve()
        store_resolved = store.resolve()
        watch_resolved.relative_to(store_resolved)
    except (OSError, ValueError):
        pass
    else:
        rel_log = _relative_for_log(event_path, watch_root)
        log(f"palace index: drop reason=under-store path={rel_log}")
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
            )
        )
        return

    if kind in _SKIP_KINDS:
        rel_log = _relative_for_log(event_path, watch_root)
        log(f"palace index: skip kind={kind} path={rel_log}")
        return

    work_queue.put(
        _WorkItem(
            change_kind=event.change_kind,
            path=event_path,
            watch_root=watch_root,
            file_kind=kind,
            event_id=event.id,
        )
    )


def _relative_for_log(path: Path, watch_root: Path) -> str:
    try:
        return path.relative_to(watch_root).as_posix()
    except ValueError:
        return path.as_posix()
