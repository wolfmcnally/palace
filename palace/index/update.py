"""Per-file index update: reflect named files in a store's index.

``palace index update`` is the third writer of ``chunks.sqlite`` beside the
daemon and the synchronous build. It indexes each named file if present and
removes it if absent, planning and embedding outside the store's writer lock
and holding the lock only to commit (see :func:`palace.index.core.index_one`).
It asserts the store's recorded embedding identity and never stamps it, so an
unstamped or mismatched store refuses and names the full build that fixes it.
"""

from __future__ import annotations

import math
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import sqlite_vec

from palace.index._errors import IndexError
from palace.index.config import WRITER_BUSY_TIMEOUT_SECONDS, chunks_db_path
from palace.index.core import index_one
from palace.index.egress import EmbeddingUsage, EmbeddingUsageSummary, usage_scope
from palace.index.embedder import Embedder
from palace.index.embedder_config import (
    assert_remote_corpus_boundary,
    load_embedder_config,
    resolve_embedder,
    resolve_identity,
)
from palace.index.pipeline import classify_kind
from palace.index.schema import assert_identity, assert_schema_version
from palace.reindex.config import ChangeKind
from palace.watch._errors import WatchError
from palace.watch.ignore import GITIGNORE_FILENAME, IgnoreEngine
from palace.writer_lock import WRITER_LOCK_TIMEOUT_SECONDS

__all__ = ["UpdateResult", "update"]


@dataclass(frozen=True)
class UpdateResult:
    """Per-call result returned by :func:`update`.

    - ``paths`` is the number of named files.
    - ``indexed`` counts present files whose commit changed rows.
    - ``removed`` counts absent files whose passages were dropped.
    - ``unchanged`` counts present files the file-hash short-circuit skipped.
    - ``skipped`` counts present files the indexer does not chunk.
    - ``replanned`` counts plans re-made because another writer committed first.
    - ``embedding`` is what this call's embedding requests used.
    """

    watch_root: Path
    paths: int
    indexed: int
    removed: int
    unchanged: int
    skipped: int
    replanned: int
    seconds: float
    embedding: EmbeddingUsageSummary = EmbeddingUsageSummary()


def _stderr_log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _validate_paths(*, paths: Sequence[Path], root: Path, engine: IgnoreEngine) -> list[Path]:
    """Resolve every named path and refuse the whole call on any bad one."""
    if not paths:
        raise IndexError("palace index update requires at least one path")
    refusals: list[str] = []
    accepted: list[Path] = []
    for raw in paths:
        # Non-strict resolution canonicalizes the existing prefix of an absent
        # path too, so a file named through a symlinked root alias resolves
        # the same way whether it is present or already deleted.
        resolved = Path(raw).expanduser().resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            refusals.append(f"{raw}: not under watch root {root}")
            continue
        if not relative.parts:
            refusals.append(f"{raw}: is the watch root itself")
            continue
        if resolved.is_dir():
            refusals.append(f"{raw}: is a directory")
            continue
        if resolved.name == GITIGNORE_FILENAME:
            refusals.append(f"{raw}: ignore files are never indexed")
            continue
        # The build prunes an ignored directory before it looks inside, so a
        # child negation can never admit a file under it; judge every
        # ancestor directory first, then the file, with the same engine.
        ignored_reason: str | None = None
        try:
            for depth in range(1, len(relative.parts)):
                ancestor = root.joinpath(*relative.parts[:depth])
                reason = engine.reason(ancestor)
                if reason is not None:
                    ignored_reason = f"{reason} on {ancestor.relative_to(root).as_posix()}/"
                    break
            if ignored_reason is None:
                ignored_reason = engine.reason(resolved)
        except WatchError as exc:
            refusals.append(f"{raw}: {exc}")
            continue
        if ignored_reason is not None:
            refusals.append(f"{raw}: ignored by the root's rules (reason={ignored_reason})")
            continue
        accepted.append(root / relative)
    if refusals:
        raise IndexError("refused paths: " + "; ".join(refusals))
    return accepted


def update(
    *,
    store: Path,
    watch_root: Path,
    paths: Sequence[Path],
    embedder: Embedder | None = None,
    lock_timeout: float = WRITER_LOCK_TIMEOUT_SECONDS,
    verbose: bool = False,
    log: Callable[[str], None] | None = None,
) -> UpdateResult:
    """Index each named file if present, or remove it if absent; see :func:`_update`.

    The result carries what the call's embedding requests used. An exception raised after
    any embedding request carries that usage as ``embedding_usage``, so a caller can count
    what a failed call spent.
    """
    out_log: Callable[[str], None] = log if log is not None else _stderr_log
    usage = EmbeddingUsage()
    try:
        with usage_scope(usage):
            result = _update(
                store=store,
                watch_root=watch_root,
                paths=paths,
                embedder=embedder,
                lock_timeout=lock_timeout,
                verbose=verbose,
                log=out_log,
            )
        result = replace(result, embedding=usage.summary())
        out_log(
            "palace index update:"
            f" root={result.watch_root}"
            f" paths={result.paths}"
            f" indexed={result.indexed}"
            f" removed={result.removed}"
            f" unchanged={result.unchanged}"
            f" skipped={result.skipped}"
            f" replanned={result.replanned}"
            f" seconds={result.seconds:.2f}" + result.embedding.log_fields()
        )
    except BaseException as exc:  # the final summary too: a failure after spending still reports it
        exc.embedding_usage = usage.summary()  # type: ignore[attr-defined]
        raise
    return result


def _update(
    *,
    store: Path,
    watch_root: Path,
    paths: Sequence[Path],
    embedder: Embedder | None,
    lock_timeout: float,
    verbose: bool,
    log: Callable[[str], None],
) -> UpdateResult:
    """Index each named file if present, or remove it if absent.

    Raises :class:`palace.index._errors.IndexError` for every refusal before
    any write, and lets :class:`palace.writer_lock.WriterLockTimeout` propagate
    when the commit lock is not acquired within ``lock_timeout`` seconds. The
    store is never created or stamped here, and a recorded identity that no
    longer matches this embedder's is a refusal, not a retry.
    """
    t0 = time.monotonic()
    if not math.isfinite(lock_timeout) or lock_timeout < 0:
        raise IndexError("lock_timeout must be a finite non-negative number of seconds")
    out_log: Callable[[str], None] = log if log is not None else _stderr_log
    verbose_log: Callable[[str], None] = out_log if verbose else (lambda _msg: None)

    root = watch_root.expanduser().resolve()
    store_resolved = store.expanduser().resolve()
    if not root.is_dir():
        raise IndexError(f"watch root is not a directory: {watch_root}")
    try:
        root.relative_to(store_resolved)
    except ValueError:
        pass
    else:
        raise IndexError(f"watch root {watch_root} resolves under the store; refused")
    config = load_embedder_config(store)
    assert_remote_corpus_boundary(config=config, store=store, watch_roots=[root])
    engine = IgnoreEngine(root)
    targets = _validate_paths(paths=paths, root=root, engine=engine)

    db_path = chunks_db_path(store)
    if not db_path.is_file():
        raise IndexError(f"no index at {db_path}; build it first with 'palace index build --full'")
    assert_schema_version(store)

    owned_embedder = embedder is None
    if embedder is None:
        embedder, identity = resolve_embedder(store)
    else:
        identity = resolve_identity(store)
    conn: sqlite3.Connection | None = None
    indexed = removed = unchanged = skipped = replanned = 0
    try:
        conn = sqlite3.connect(
            str(db_path), isolation_level=None, timeout=WRITER_BUSY_TIMEOUT_SECONDS
        )
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        assert_identity(conn, identity)

        def count_replan(line: str) -> None:
            nonlocal replanned
            if " reason=stale-plan " in line:
                replanned += 1
            verbose_log(line)

        for path in targets:
            # Each file's operation follows the disk as it is when its turn
            # comes, not when the call was validated: an earlier file's
            # embedding may have taken long enough for this one to change.
            change_kind: ChangeKind = "created" if path.is_file() else "deleted"
            outcome = index_one(
                conn=conn,
                embedder=embedder,
                identity=identity,
                file_kind=classify_kind(path),
                change_kind=change_kind,
                path=path,
                watch_root=root,
                store=store,
                lock_timeout=lock_timeout,
                verbose=verbose,
                log=count_replan,
            )
            if outcome.kind == "deleted":
                removed += 1
            elif outcome.noop and outcome.noop_reason == "file-hash-unchanged":
                unchanged += 1
            elif outcome.noop:
                skipped += 1
            else:
                indexed += 1
    finally:
        if conn is not None:
            conn.close()
        if owned_embedder:
            embedder.close()
    elapsed = time.monotonic() - t0
    result = UpdateResult(
        watch_root=root,
        paths=len(targets),
        indexed=indexed,
        removed=removed,
        unchanged=unchanged,
        skipped=skipped,
        replanned=replanned,
        seconds=elapsed,
    )
    return result
