"""sqlite write worker for the chunks DB.

A single background thread drains a ``queue.Queue[_WorkItem | _Sentinel]``
of "re-index this path" tasks. The thread:

1. Opens one :class:`sqlite3.Connection` to ``<store>/index/chunks.sqlite``
   on the thread body (sqlite's threading model wants the connection to
   live on its owning thread).
2. Loads the sqlite-vec extension and runs :func:`init_db` to bootstrap
   (idempotent — the server's one-shot connection has typically already
   asserted the schema version).
3. Per work item: delegate to :func:`palace.index.core.index_one`
   (parse → diff → embed only new/changed sections → hold the store's
   writer lock → wrap the deletes + inserts in one transaction). The
   per-file logic and its log lines live in :mod:`palace.index.core`,
   shared verbatim with the synchronous :mod:`palace.index.build` walker
   and the per-file :mod:`palace.index.update`. The daemon's startup write
   (schema creation and the empty-store stamp) is the server's bootstrap
   under that lock; the thread's own ``init_db`` re-run is idempotent and
   takes no lock.

Failure posture:

- **Per-file atomicity.** Any exception inside the ``BEGIN..COMMIT``
  block triggers a ``ROLLBACK``; the file's row is left in its previous
  state. The next restart re-reads the change event and the file-hash
  short-circuit makes a successful run idempotent.
- **Log-and-continue at the queue level.** A failed work item logs the
  exception and the thread keeps draining — one bad file must not kill
  the daemon.
- **``on_completed`` callback** is the test seam the server uses to
  advance the cursor only after the writer commits.
"""

from __future__ import annotations

import contextlib
import queue
import sqlite3
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import sqlite_vec

from palace.index._errors import IndexError
from palace.index.config import (
    WRITER_BUSY_TIMEOUT_SECONDS,
    EmbeddingIdentity,
    prepare_chunks_db_path,
)
from palace.index.core import _log_relative_path, index_one
from palace.index.embedder import Embedder
from palace.index.schema import assert_identity, init_db
from palace.reindex.config import ChangeKind

__all__ = ["WriterWorker", "_Sentinel", "_WorkItem"]


@dataclass(frozen=True)
class _WorkItem:
    """One unit of writer work — re-index this path."""

    change_kind: ChangeKind
    path: Path
    watch_root: Path
    file_kind: str
    event_id: str | None = None
    day: str | None = None
    byte_offset: int | None = None


class _Sentinel:
    """Singleton marker enqueued by :meth:`WriterWorker.stop`."""


_SENTINEL: Final[_Sentinel] = _Sentinel()


def _default_log(line: str) -> None:
    """Default log sink: stderr, line-flushed."""
    print(line, file=sys.stderr, flush=True)


class WriterWorker(threading.Thread):
    """Single background thread that drains chunk-write work."""

    def __init__(
        self,
        *,
        store: Path,
        embedder: Embedder,
        identity: EmbeddingIdentity,
        work_queue: queue.Queue[_WorkItem | _Sentinel],
        on_completed: Callable[[_WorkItem], None] | None = None,
        log: Callable[[str], None] | None = None,
        verbose: bool = False,
    ) -> None:
        super().__init__(name="palace-index-writer", daemon=True)
        self._store = store
        self._embedder = embedder
        # The identity the embedder was constructed for; every commit asserts
        # the store still carries it, so the pair stays bound for the thread's
        # whole life rather than being re-read from the selector.
        self._identity = identity
        self._queue = work_queue
        self._on_completed = on_completed
        self._log = log if log is not None else _default_log
        self._verbose = verbose
        # Created on the worker thread in ``run()`` so the connection
        # lives on its owning thread per sqlite's threading model.
        self._conn: sqlite3.Connection | None = None
        self._ready = threading.Event()
        self.halt_reason: str | None = None

    # ------------------------------------------------------------------ lifecycle

    def stop(self, timeout: float = 5.0) -> None:
        """Enqueue the sentinel and join the worker thread. Idempotent."""
        self._queue.put(_SENTINEL)
        if self.is_alive():
            self.join(timeout=timeout)

    def wait_ready(self, timeout: float) -> bool:
        """Wait until startup has either succeeded or halted."""
        return self._ready.wait(timeout=timeout)

    def is_ready(self) -> bool:
        """Return whether startup has produced a success-or-halt outcome."""
        return self._ready.is_set()

    def run(self) -> None:
        """Drain the queue until the sentinel arrives."""
        conn = sqlite3.connect(
            str(prepare_chunks_db_path(self._store)),
            isolation_level=None,
            timeout=WRITER_BUSY_TIMEOUT_SECONDS,
        )
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            # The daemon's startup write is the server's bootstrap under the
            # store's writer lock; this idempotent re-run creates nothing on a
            # bootstrapped store, so it takes no lock and cannot outwait the
            # server's readiness deadline behind a build.
            init_db(conn, store=self._store)
            try:
                assert_identity(conn, self._identity)
            except IndexError as exc:
                self.halt_reason = str(exc)
                self._log(f"palace index: halted — {exc}")
                self._ready.set()
                return
            self._conn = conn
            self._ready.set()
            while True:
                item = self._queue.get()
                if isinstance(item, _Sentinel):
                    return
                try:
                    index_one(
                        conn=self._require_conn(),
                        embedder=self._embedder,
                        identity=self._identity,
                        file_kind=item.file_kind,
                        change_kind=item.change_kind,
                        path=item.path,
                        watch_root=item.watch_root,
                        store=self._store,
                        verbose=self._verbose,
                        log=self._log,
                    )
                except Exception as exc:  # noqa: BLE001 — log-and-continue
                    rel = _log_relative_path(item.path, item.watch_root)
                    self._log(f"palace index: write-failed path={rel} reason={exc!r}")
                    continue
                if self._on_completed is not None:
                    self._on_completed(item)
        finally:
            self._conn = None
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    # ------------------------------------------------------------------ helpers

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("WriterWorker._conn is unset; thread not started")
        return self._conn
