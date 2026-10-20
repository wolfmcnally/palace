"""Synchronous, non-daemon project indexer (Phase 3.1).

``palace index build`` walks a source tree, reconciles the chunks DB to
match it (re-embedding only what changed), reconciles deletions, prints a
per-root summary, and exits. It is the synchronous-tool half of the
two-mode model in ``briefs/project-index-generator.md`` §"Two modes" —
the daemon's always-on FSEvents sibling lives in
:mod:`palace.index.server`.

Two facts shape this module:

- **It bypasses the events log entirely.** The events JSONL stream
  exists only to keep palace's hot FSEvents callback from blocking; a
  batch tool has no such constraint, so it walks → diffs → re-embeds →
  commits → exits, writing the chunks DB directly. No events log, no
  FSEvents subscription, no cursor tailing.
- **It shares the per-file core with the daemon.** The builder composes
  :func:`palace.index.core.plan_one` and :func:`palace.index.core.commit_plan`
  around a bounded embed stage; the daemon calls the same composition through
  :func:`palace.index.core.index_one`. The calling thread remains the only
  SQLite reader/writer. Bounded turns rotate in manifest order and completed
  documents publish at deterministic fold points, before earlier giants finish.

Path representation is **watch-root-relative** (Phase 3.2): the chunks DB
stores each file's POSIX path relative to its watch root, with the
absolute root in the ``watch_root`` column. The deletion-reconciliation
sweep below scopes by the absolute ``watch_root`` column (a bare relative
``path`` like ``src/foo.rs`` could otherwise collide across roots).
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

from palace.index._errors import IndexError
from palace.index.config import (
    EMBED_CONCURRENCY_DEFAULT,
    OLLAMA_BASE_URL,
    OPENROUTER_BASE_URL,
    REMOTE_EMBED_MAX_CONCURRENCY,
    WRITER_BUSY_TIMEOUT_SECONDS,
    WRITER_LOCK_TIMEOUT_SECONDS,
    EmbeddingIdentity,
    prepare_chunks_db_path,
)
from palace.index.egress import (
    EmbeddingUsage,
    EmbeddingUsageSummary,
    usage_scope,
)
from palace.index.embedder import Embedder
from palace.index.embedder_config import (
    PROVIDER_ENDPOINT,
    PROVIDER_OPENROUTER,
    assert_remote_corpus_boundary,
    identity_for,
    load_embedder_config,
    resolve_embedder,
    resolve_identity,
)
from palace.index.scheduler import BuildLimits, BuildProgress, RootStats, schedule
from palace.index.schema import (
    assert_identity,
    init_db,
    read_data_version,
    stamp_identity,
)
from palace.watch.config import WatchRootsConfig, default_config_path
from palace.watch.ignore import IgnoreEngine
from palace.writer_lock import writer_lock

__all__ = ["BuildReport", "BuildResult", "build", "build_root"]


@dataclass(frozen=True)
class BuildResult:
    """Per-root result returned by :func:`build_root`.

    - ``files`` is the count of admitted files walked under the root.
    - ``embedded`` is files that had at least one new/changed section
      re-embedded.
    - ``unchanged`` is files the file-hash short-circuit skipped.
    - ``deleted`` is files dropped by the deletion-reconciliation sweep.
    - ``embedding`` is what this root's embedding requests used.
    """

    watch_root: Path
    files: int
    embedded: int
    unchanged: int
    deleted: int
    embed_concurrency: int
    embed_batching: str
    seconds: float
    embedding: EmbeddingUsageSummary = EmbeddingUsageSummary()


@dataclass(frozen=True)
class BuildReport:
    """What :func:`build` did: each walked root's result and the whole invocation's embedding
    usage, the startup probe included (also when no root was walked)."""

    roots: tuple[BuildResult, ...]
    embedding: EmbeddingUsageSummary


def _stderr_log(line: str) -> None:
    """Default log sink: stderr, line-flushed."""
    print(line, file=sys.stderr, flush=True)


def _resolve_concurrency(*, requested: int | None, embedder: Embedder, provider: str) -> int:
    capability = embedder.max_concurrency
    if capability < 1:
        raise IndexError(f"embedder reported invalid max_concurrency={capability}")
    if requested is None:
        return min(EMBED_CONCURRENCY_DEFAULT, capability)
    if requested < 1:
        raise IndexError(f"--embed-concurrency must be at least 1; got {requested}")
    if requested > capability:
        if provider == PROVIDER_OPENROUTER:
            reason = (
                f"the OpenRouter embedder is configured for {capability} callers; palace has "
                f"burst measurements only through {REMOTE_EMBED_MAX_CONCURRENCY}"
            )
        else:
            reason = "local Ollama concurrency is measured slower than one caller"
        raise IndexError(
            f"--embed-concurrency {requested} exceeds the {provider} embedder capability "
            f"of {capability}; {reason}"
        )
    return requested


def _render_batching(provider: str) -> str:
    return (
        "per-turn(max=1024,budget=30000)"
        if provider == PROVIDER_OPENROUTER
        else "per-turn(max=32,bytes=131072)"
    )


def _root_result(state: RootStats, concurrency: int, batching: str) -> BuildResult:
    return BuildResult(
        watch_root=state.root,
        files=state.files,
        embedded=state.embedded,
        unchanged=state.unchanged,
        deleted=state.deleted,
        embed_concurrency=concurrency,
        embed_batching=batching,
        seconds=state.seconds,
        embedding=state.usage.summary(),
    )


def build_root(
    *,
    conn: sqlite3.Connection,
    embedder: Embedder,
    identity: EmbeddingIdentity,
    watch_root: Path,
    engine: IgnoreEngine,
    full: bool,
    verbose: bool,
    log: Callable[[str], None],
    embed_concurrency: int = 1,
    embed_batching: str = "per-turn(max=32,bytes=131072)",
    limits: BuildLimits | None = None,
    progress: Callable[[BuildProgress], None] | None = None,
) -> BuildResult:
    """Index one root under the caller's lock, with durable bounded turns.

    A file-backed chunks database is required for its private derived staging.
    Full callers retain completion receipts until they certify the full store.
    """
    database = next(
        (row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), ""
    )
    if not database:
        raise IndexError("build_root requires a file-backed chunks database")
    scheduled = schedule(
        conn=conn,
        store=Path(database).parent.parent,
        roots=[(watch_root, engine)],
        embedder=embedder,
        identity=identity,
        full=full,
        concurrency=embed_concurrency,
        limits=limits or BuildLimits(),
        log=log,
        progress=progress,
        remote=identity.provider.startswith("openrouter:"),
        verbose=verbose,
    )
    if not full:
        scheduled.clear_completed()
    return _root_result(scheduled.roots[0], embed_concurrency, embed_batching)


def build(
    *,
    store: Path,
    watch_root_filter: Path | None = None,
    full: bool = False,
    verbose: bool = False,
    embedder: Embedder | None = None,
    log: Callable[[str], None] | None = None,
    embed_concurrency: int | None = None,
    lock_timeout: float = WRITER_LOCK_TIMEOUT_SECONDS,
    limits: BuildLimits | None = None,
    progress: Callable[[BuildProgress], None] | None = None,
) -> BuildReport:
    """Reconcile one or more watch roots to the chunks DB in one pass; see :func:`_build`.

    Returns a :class:`BuildReport`: each walked root's result and the invocation's embedding
    usage, the startup probe included. An exception raised after any embedding request
    carries that usage as ``embedding_usage``, so a caller can count what a failed build spent.
    """
    usage = EmbeddingUsage()
    roots: list[BuildResult] = []
    out_log: Callable[[str], None] = log if log is not None else _stderr_log
    try:
        with usage_scope(usage):
            _build(
                store=store,
                watch_root_filter=watch_root_filter,
                full=full,
                verbose=verbose,
                embedder=embedder,
                log=out_log,
                embed_concurrency=embed_concurrency,
                lock_timeout=lock_timeout,
                roots=roots,
                limits=limits or BuildLimits(),
                progress=progress,
            )
        report = BuildReport(roots=tuple(roots), embedding=usage.summary())
        out_log(
            f"palace index build: total roots={len(report.roots)}" + report.embedding.log_fields()
        )
    except BaseException as exc:  # the final summary too: a failure after spending still reports it
        exc.embedding_usage = usage.summary()  # type: ignore[attr-defined]
        raise
    return report


def _build(
    *,
    store: Path,
    watch_root_filter: Path | None,
    full: bool,
    verbose: bool,
    embedder: Embedder | None,
    log: Callable[[str], None],
    embed_concurrency: int | None,
    lock_timeout: float,
    roots: list[BuildResult],
    limits: BuildLimits,
    progress: Callable[[BuildProgress], None] | None,
) -> None:
    """Reconcile one or more watch roots to the chunks DB in one pass.

    Appends each walked root's result to ``roots``. Raises
    :class:`palace.index._errors.IndexError` for hard failures (embedder
    unreachable) and :class:`palace.watch._errors.WatchError` for a
    malformed config; the CLI shim translates both into ``error: ...``
    lines + exit 1. The store's writer lock is held from before the
    connection opens until after the final stamp, so a second build waits
    (up to ``lock_timeout`` seconds, then :class:`palace.writer_lock.WriterLockTimeout`)
    and then finds the files unchanged.

    ``watch_root_filter`` indexes exactly that directory **ad-hoc** — it
    need not be in ``watch-roots.toml`` (the project-index-generator
    ergonomic). Omitted, every configured root is walked; an empty config
    with no filter logs ``no watch roots configured; nothing to build``
    and returns.

    ``embedder`` is the test seam: the CLI never threads one. Store identity
    is still resolved and enforced when a test injects a fake.
    """
    store.mkdir(parents=True, exist_ok=True)
    if not math.isfinite(lock_timeout) or lock_timeout < 0:
        raise IndexError("lock_timeout must be a finite non-negative number of seconds")

    out_log: Callable[[str], None] = log if log is not None else _stderr_log

    config = load_embedder_config(store)
    identity = identity_for(config)
    store_resolved = store.expanduser().resolve()
    watch_config = WatchRootsConfig.load(default_config_path(store))

    if watch_root_filter is not None:
        walk_set: list[Path] = [watch_root_filter.expanduser().resolve()]
    else:
        walk_set = [root.path.expanduser().resolve() for root in watch_config.roots]

    assert_remote_corpus_boundary(config=config, store=store, watch_roots=walk_set)

    base_url = (
        "configured-private-endpoint"
        if config.provider == PROVIDER_ENDPOINT
        else OPENROUTER_BASE_URL
        if config.provider == PROVIDER_OPENROUTER
        else OLLAMA_BASE_URL
    )
    out_log(f"palace index build: probing embedder provider={identity.provider} base={base_url}")
    # Close only what we construct. A caller that injects an embedder keeps
    # ownership of it and may go on using it after the build returns — the
    # same convention `palace.search._vector_search` follows.
    owned_embedder = embedder is None
    if embedder is None:
        embedder, identity = resolve_embedder(store)
    else:
        identity = resolve_identity(store)
    conn: sqlite3.Connection | None = None
    held_lock: contextlib.AbstractContextManager[None] | None = None
    try:
        embedder.probe()
        out_log(
            "palace index build: embedder OK "
            f"(provider={identity.provider} model={identity.model} dim={identity.dim})"
        )
        effective_concurrency = _resolve_concurrency(
            requested=embed_concurrency,
            embedder=embedder,
            provider=config.provider,
        )
        embed_batching = _render_batching(config.provider)

        if not walk_set:
            out_log("palace index build: no watch roots configured; nothing to build")
            return

        # The whole reconcile runs under the store's writer lock: a concurrent
        # build waits here and then finds the files unchanged, and per-file
        # updates wait for their commits until the walk and stamp complete.
        lock = writer_lock(store, operation="build", timeout=lock_timeout)
        lock.__enter__()
        held_lock = lock

        # One foreground connection — no writer thread here, so the
        # connection lives on the calling thread. Mirrors the bootstrap
        # ``serve`` runs (sqlite-vec load + ``init_db``).
        conn = sqlite3.connect(
            str(prepare_chunks_db_path(store)),
            isolation_level=None,
            timeout=WRITER_BUSY_TIMEOUT_SECONDS,
        )
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        init_db(conn, store=store)
        if not full:
            assert_identity(conn, identity)
        data_version_start = read_data_version(conn)
        walked_roots: set[str] = set()

        admitted_roots: list[tuple[Path, IgnoreEngine]] = []
        for resolved_root in walk_set:
            # Under-store check (defense-in-depth: ``palace watch add``
            # rejects this at add-time, but an ad-hoc ``--watch-root`` or
            # a hand-edited TOML could smuggle one through).
            try:
                resolved_root.relative_to(store_resolved)
            except ValueError:
                under_store = False
            else:
                under_store = True
            if under_store:
                out_log(f"palace index build: refuse-root path={resolved_root} reason=under-store")
                continue

            if not resolved_root.is_dir():
                out_log(
                    f"palace index build: skip-root path={resolved_root} reason=missing-on-disk"
                )
                continue

            admitted_roots.append((resolved_root, IgnoreEngine(resolved_root)))
        scheduled = schedule(
            conn=conn,
            store=store,
            roots=admitted_roots,
            embedder=embedder,
            identity=identity,
            full=full,
            concurrency=effective_concurrency,
            limits=limits,
            log=out_log,
            progress=progress,
            remote=config.provider == PROVIDER_OPENROUTER,
            verbose=verbose,
        )
        for state in scheduled.roots:
            result = _root_result(state, effective_concurrency, embed_batching)
            roots.append(result)
            walked_roots.add(str(result.watch_root))
            out_log(
                "palace index build:"
                f" root={result.watch_root}"
                f" files={result.files}"
                f" embedded={result.embedded}"
                f" unchanged={result.unchanged}"
                f" deleted={result.deleted}"
                f" embed_concurrency={result.embed_concurrency}"
                f" embed_batching={result.embed_batching}"
                f" seconds={result.seconds:.2f}" + result.embedding.log_fields()
            )
        if full:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                out_log(
                    "palace index build: embed-identity not-stamped "
                    "reason=concurrent-writer-lock — another connection holds the write lock; "
                    "the vectors are correct but uncertified. Stop the index daemon "
                    "(launchctl bootout gui/$(id -u)/ai.palace.index) and re-run "
                    "'palace index build --full'"
                )
            else:
                try:
                    data_version_now = read_data_version(conn)
                    indexed_roots = {
                        str(row[0])
                        for row in conn.execute("SELECT DISTINCT watch_root FROM chunks").fetchall()
                    }
                    remaining = indexed_roots - walked_roots
                    if data_version_now != data_version_start:
                        conn.execute("ROLLBACK")
                        out_log(
                            "palace index build: embed-identity not-stamped "
                            f"reason=external-writer data_version_start={data_version_start} "
                            f"data_version_now={data_version_now} — another connection committed "
                            "to this index during the rebuild; the vectors are correct but "
                            "uncertified. Stop the index daemon "
                            "(launchctl bootout gui/$(id -u)/ai.palace.index) and re-run "
                            "'palace index build --full'"
                        )
                    elif remaining:
                        conn.execute("ROLLBACK")
                        out_log(
                            "palace index build: embed-identity not-stamped "
                            f"reason=partial-rebuild unwalked-roots={len(remaining)} — "
                            "incremental 'palace index build' will refuse and "
                            "'palace index serve' will park until 'palace index build --full' "
                            "completes over every configured root"
                        )
                    else:
                        stamp_identity(conn, identity)
                        conn.execute("COMMIT")
                        scheduled.clear_completed()
                        out_log(
                            "palace index build: embed-identity stamped="
                            f"convention={identity.convention} model={identity.model} "
                            f"provider={identity.provider} dim={identity.dim}"
                        )
                except BaseException:
                    if conn.in_transaction:
                        with contextlib.suppress(sqlite3.Error):
                            conn.execute("ROLLBACK")
                    raise
        if not full:
            scheduled.clear_completed()
        return
    finally:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        if held_lock is not None:
            held_lock.__exit__(None, None, None)
        if owned_embedder:
            embedder.close()
