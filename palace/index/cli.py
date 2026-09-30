"""``palace index`` CLI subcommand group.

Subcommands:

- ``palace index serve [--store <path>] [-v|--verbose]`` — run the
  index daemon in the foreground.
- ``palace index update --store <path> --watch-root <root>
  [--lock-timeout S] [-v|--verbose] <path>...`` — reflect each named
  file in the index: index it if present, remove it if absent. Plans and
  embeds outside the store's writer lock and holds the lock only to
  commit; refuses paths outside or ignored by the root and refuses an
  unstamped or mismatched store without writing. One summary line per
  call; nonzero exit on any failure.
- ``palace index check <path> [--store <path>]`` — inspection-only;
  classify a path as ``indexed:``, ``not-indexed:``, or
  ``not-watched:``. Reads the chunks DB read-only; maps the absolute
  argument to the stored watch-root-relative ``(watch_root, path)`` key
  for the lookup.
- ``palace index status [--store <path>]`` — report daemon liveness,
  cursor position, chunk counts, embed convention, and events-log lag.
  Read-only; exits 0 even when the daemon is stopped.
- ``palace index build [--store <path>] [--watch-root <path>] [--full]
  [--embed-concurrency N] [--lock-timeout S] [-v|--verbose]`` — the
  synchronous, non-daemon walk-diff-reembed tool (Phase 3.1). Walks the
  target root(s) while holding the store's writer lock, reconciles the
  chunks DB (re-embedding only what changed, reconciling deletions),
  prints a per-root summary, and exits. Bypasses the events log entirely.
- ``palace index embedder show|set`` — inspect or change the store-level
  embedding provider selection and its published-corpus assertion.
- ``palace index publishing show|set`` — inspect or record the store's explicit
  published-corpus opt-in for vector publication.
- ``palace index publish-vectors --bucket B --index I --profile P --region R
  [--text-metadata] [--json]`` — publish the store's chunk vectors to an Amazon S3
  Vectors index incrementally by chunk key and record the publication receipt.
- ``palace index vector-backend show|set`` — inspect or select the store's vector
  search leg (sqlite-vec, the default, or its published S3 Vectors index).
- ``palace index install`` — install the daemon as a per-user
  LaunchAgent (Phase 2.5).
- ``palace index uninstall`` — uninstall the daemon LaunchAgent
  (Phase 2.5; idempotent).

Store-resolution precedence (mirrors Phase 2.1 / 2.2):

1. ``--store <path>`` (CLI flag); takes the value verbatim.
2. ``$PALACE_STORE`` (environment variable).
3. :data:`palace.daemons.capture.config.DEFAULT_STORE`
   (``~/palace-data.noindex/``).

Error translation: every CLI error path emits one ``error: ...`` line
to stderr with exit 1. No Python tracebacks reach the operator.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from palace.daemons.capture.config import BOISE_TZ, DEFAULT_STORE
from palace.index._errors import IndexError
from palace.index.build import build
from palace.index.config import (
    EMBED_DIM,
    EMBEDDER_MODEL,
    PARK_RECHECK_SECONDS,
    WRITER_LOCK_TIMEOUT_SECONDS,
    chunks_db_path,
)
from palace.index.cursor import TailCursor
from palace.index.daemon_state import StateUnreadable, read_state
from palace.index.egress import EmbeddingUsageSummary
from palace.index.embedder_config import (
    PROVIDER_OLLAMA,
    PROVIDER_OPENROUTER,
    EmbedderConfig,
    embedder_config_path,
    identity_for,
    load_embedder_config,
    resolve_identity,
    save_embedder_config,
)
from palace.index.lifecycle import cli_install, cli_uninstall
from palace.index.s3vectors import (
    BACKEND_S3VECTORS,
    BACKEND_SQLITE_VEC,
    VectorBackendConfig,
    VectorUsageSummary,
    load_publishing_config,
    load_vector_backend_config,
    publish_vectors,
    publishing_config_path,
    read_receipt,
    save_publishing_config,
    save_vector_backend_config,
    vector_backend_config_path,
)
from palace.index.schema import identity_divergences, read_identity
from palace.index.server import serve
from palace.index.store_ops import copy_paths, remove_paths
from palace.index.update import update
from palace.reindex.config import events_path
from palace.watch._errors import WatchError
from palace.watch.config import WatchRootsConfig, default_config_path
from palace.writer_lock import WriterLockTimeout

__all__ = ["build_subparser", "dispatch_index"]


def _lock_timeout_seconds(value: str) -> float:
    """argparse type for ``--lock-timeout``: a finite, non-negative number."""
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {value!r}") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError(
            f"must be a finite non-negative number of seconds, not {value!r}"
        )
    return seconds


def _resolve_store(args: Any) -> Path:
    """Return the resolved machine-state root per the precedence rule."""
    flag = getattr(args, "store", None)
    if flag is not None:
        return Path(flag).expanduser()
    env = os.environ.get("PALACE_STORE")
    if env:
        return Path(env).expanduser()
    return DEFAULT_STORE


def build_subparser(subparsers: Any) -> None:
    """Wire the ``index`` subparser group into ``palace`` argparse."""
    index = subparsers.add_parser(
        "index",
        help="Index daemon controls.",
        description=(
            "Run the events-log-consuming index daemon, inspect indexed paths, "
            "and report daemon liveness. Subscribes to "
            "<store>/events/<YYYY-MM-DD>.jsonl, classifies each surviving "
            "change as a typed file kind, and writes the chunks DB at "
            "<store>/index/chunks.sqlite."
        ),
    )
    sub = index.add_subparsers(dest="index_command")

    serve_p = sub.add_parser(
        "serve",
        help="Run the index daemon in the foreground.",
    )
    serve_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    serve_p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log per-section deltas and file-hash short-circuits to stderr.",
    )

    check_p = sub.add_parser(
        "check",
        help="Classify a path as indexed / not-indexed / not-watched.",
    )
    check_p.add_argument("path", help="Path to classify.")
    check_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )

    status_p = sub.add_parser(
        "status",
        help="Report daemon liveness, cursor position, and chunk counts.",
    )
    status_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )

    build_p = sub.add_parser(
        "build",
        help="Synchronously walk + reconcile a tree's chunks DB, then exit.",
    )
    build_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )

    embedder_p = sub.add_parser(
        "embedder",
        help="Inspect or change the store-level embedding provider.",
    )
    embedder_sub = embedder_p.add_subparsers(dest="embedder_command")
    embedder_show = embedder_sub.add_parser("show", help="Show the selected embedder.")
    embedder_show.add_argument("--store", type=Path, default=None)
    embedder_set = embedder_sub.add_parser("set", help="Set the selected embedder.")
    embedder_set.add_argument("--store", type=Path, default=None)
    embedder_set.add_argument(
        "--provider", required=True, choices=(PROVIDER_OLLAMA, PROVIDER_OPENROUTER)
    )
    embedder_set.add_argument("--model")
    embedder_set.add_argument("--upstream")
    embedder_set.add_argument("--dim", type=int)
    embedder_set.add_argument("--published-corpus", action="store_true")
    embedder_set.add_argument("--published-corpus-note")
    embedder_set.add_argument("--i-will-rebuild", action="store_true")
    build_p.add_argument(
        "--watch-root",
        type=Path,
        default=None,
        help="Ad-hoc directory to index (need not be in watch-roots.toml).",
    )
    build_p.add_argument(
        "--full",
        action="store_true",
        help="Replace every admitted file atomically, bypassing incremental short-circuits.",
    )
    build_p.add_argument(
        "--embed-concurrency",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Embedding requests in flight (default: sustained-measured remote setting; "
            "values above the selected provider's capability are refused)."
        ),
    )
    build_p.add_argument(
        "--lock-timeout",
        type=_lock_timeout_seconds,
        default=WRITER_LOCK_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=(
            "Seconds to wait for the store's writer lock before failing and naming its "
            f"holder (default: {WRITER_LOCK_TIMEOUT_SECONDS:g}). The build holds the lock "
            "for its whole reconcile."
        ),
    )
    build_p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log per-file noop / indexed lines to stderr.",
    )
    build_p.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print the result as one JSON object on stdout, embedding usage included (requests, "
            "failed requests, prompt tokens, provider-reported cost); on failure, an object with "
            "ok=false, the error and the usage spent before it. Progress stays on stderr."
        ),
    )

    update_p = sub.add_parser(
        "update",
        help="Reflect named files in the index: index each if present, remove it if absent.",
        description=(
            "Index each named file if it exists, or drop its passages if it does not. "
            "Planning and embedding run outside the store's writer lock; only the commit "
            "holds it, so many processes may update distinct files concurrently. A path "
            "outside the watch root or ignored by its rules is refused before any write. "
            "The store's recorded embedding identity is asserted, never stamped: an "
            "unstamped or mismatched store refuses and names 'palace index build --full'."
        ),
    )
    update_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    update_p.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print the result as one JSON object on stdout, embedding usage included (requests, "
            "failed requests, prompt tokens, provider-reported cost); on failure, an object with "
            "ok=false, the error and the usage spent before it. Progress stays on stderr."
        ),
    )
    update_p.add_argument(
        "--watch-root",
        type=Path,
        required=True,
        help="The root every named path must fall under; its ignore rules decide admission.",
    )
    update_p.add_argument(
        "--lock-timeout",
        type=_lock_timeout_seconds,
        default=WRITER_LOCK_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=(
            "Seconds to wait for the store's writer lock at each commit before failing and "
            f"naming its holder (default: {WRITER_LOCK_TIMEOUT_SECONDS:g}). A whole-tree "
            "build holds the lock for its whole reconcile, so pass a longer wait beside one."
        ),
    )
    update_p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log per-file indexed / dropped / noop / replan lines to stderr.",
    )
    update_p.add_argument(
        "paths",
        nargs="+",
        help="Files to reflect, absolute or relative to the working directory.",
    )

    lock_help = (
        "Seconds to wait for each store's writer lock before failing and naming its holder "
        f"(default: {WRITER_LOCK_TIMEOUT_SECONDS:g})."
    )
    copy_p = sub.add_parser(
        "copy",
        help="Copy named files' index rows from one store into another without embedding.",
        description=(
            "Replace the destination's rows for each named file with the source's (files, "
            "chunks, vectors, keyword rows, JSONL cursors, document metadata); a file the "
            "source does not hold is removed from the destination. Nothing is embedded. "
            "Both stores' writer locks are held, in a fixed order. Both stores must record "
            "the same complete embedding identity, the source must index --watch-root, and "
            "the destination may index no other root; otherwise nothing is written."
        ),
    )
    copy_p.add_argument("--from", dest="source", type=Path, required=True, help="Source store.")
    copy_p.add_argument(
        "--to", dest="destination", type=Path, required=True, help="Destination store."
    )
    copy_p.add_argument(
        "--watch-root", type=Path, required=True, help="The root the named paths belong to."
    )
    copy_p.add_argument(
        "--lock-timeout",
        type=_lock_timeout_seconds,
        default=WRITER_LOCK_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=lock_help,
    )
    copy_p.add_argument(
        "paths", nargs="+", help="Files, relative to the watch root or absolute under it."
    )

    remove_p = sub.add_parser(
        "remove",
        help="Delete every index row of named files, in one transaction.",
        description=(
            "Delete each named file's files, chunks, vector, keyword, cursor and document "
            "metadata rows in one transaction under the store's writer lock. A file with no "
            "rows is skipped."
        ),
    )
    remove_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    remove_p.add_argument(
        "--watch-root", type=Path, required=True, help="The root the named paths belong to."
    )
    remove_p.add_argument(
        "--lock-timeout",
        type=_lock_timeout_seconds,
        default=WRITER_LOCK_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=lock_help,
    )
    remove_p.add_argument(
        "paths", nargs="+", help="Files, relative to the watch root or absolute under it."
    )

    publishing_p = sub.add_parser(
        "publishing",
        help="Inspect or record the store's published-corpus opt-in for vector publication.",
    )
    publishing_sub = publishing_p.add_subparsers(dest="publishing_command")
    publishing_show = publishing_sub.add_parser("show", help="Show the opt-in and last receipt.")
    publishing_show.add_argument("--store", type=Path, default=None)
    publishing_set = publishing_sub.add_parser(
        "set",
        help="Assert that the store's corpus is published, recording what and where.",
        description=(
            "Record the explicit per-store opt-in that lets 'palace index publish-vectors' "
            "publish this store's derived vector index to an operator-controlled cloud "
            "account (policies/local-first.md). The default personal store and watch roots "
            "overlapping the personal vault are refused whatever this records."
        ),
    )
    publishing_set.add_argument("--store", type=Path, default=None)
    publishing_set.add_argument("--published-corpus", action="store_true", required=True)
    publishing_set.add_argument(
        "--published-corpus-note",
        required=True,
        help="What is published and where (recorded verbatim with the time of the assertion).",
    )

    publish_p = sub.add_parser(
        "publish-vectors",
        help="Publish the store's chunk vectors to an Amazon S3 Vectors index, incrementally.",
        description=(
            "Create the vector bucket and index when absent (the store's embedding dimension, "
            "cosine distance), refuse an existing index whose data type, dimension or distance "
            "differs, put every chunk vector the index lacks and then delete keys the store no "
            "longer holds, so a query never sees a gap. The receipt in <store>/meta/"
            "vector-publication.json records the index ARN, the embedding identity, the "
            "published key-set digest and counts; a later publish diffs against it without "
            "listing the index. Every S3 Vectors request is audited in "
            "<store>/events/cloud-egress/. Requires 'palace index publishing set'; the "
            "default personal store is refused."
        ),
    )
    publish_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    publish_p.add_argument("--bucket", required=True, help="S3 Vectors vector bucket name.")
    publish_p.add_argument("--index", required=True, help="S3 Vectors index name.")
    publish_p.add_argument("--profile", required=True, help="AWS profile to publish with.")
    publish_p.add_argument("--region", required=True, help="AWS region of the vector bucket.")
    publish_p.add_argument(
        "--text-metadata",
        action="store_true",
        help=(
            "Also publish each chunk's text, watch-root-relative path and heading as "
            "non-filterable metadata (changing this republishes every vector)."
        ),
    )
    publish_p.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print the result as one JSON object on stdout, S3 Vectors usage included "
            "(requests, failed requests, request bytes, per-operation counts); on failure, an "
            "object with ok=false, the error and the usage spent before it."
        ),
    )

    backend_p = sub.add_parser(
        "vector-backend",
        help="Inspect or select the store's vector search leg.",
    )
    backend_sub = backend_p.add_subparsers(dest="vector_backend_command")
    backend_show = backend_sub.add_parser("show", help="Show the selected vector search leg.")
    backend_show.add_argument("--store", type=Path, default=None)
    backend_set = backend_sub.add_parser(
        "set",
        help="Select sqlite-vec (the default) or the store's published S3 Vectors index.",
    )
    backend_set.add_argument("--store", type=Path, default=None)
    backend_set.add_argument(
        "--backend", required=True, choices=(BACKEND_SQLITE_VEC, BACKEND_S3VECTORS)
    )
    backend_set.add_argument(
        "--profile",
        default=None,
        help=(
            "AWS profile for s3vectors queries; omit to use the runtime's credential chain "
            "(for example a function's execution role)."
        ),
    )

    sub.add_parser(
        "install",
        help="Install the index daemon as a per-user LaunchAgent.",
    )
    sub.add_parser(
        "uninstall",
        help="Uninstall the index daemon LaunchAgent.",
    )


def dispatch_index(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch ``palace index <subcommand>`` to the right handler."""
    command = getattr(args, "index_command", None)
    if command == "serve":
        return cli_serve(args)
    if command == "check":
        return cli_check(args)
    if command == "status":
        return cli_status(args)
    if command == "build":
        return cli_build(args)
    if command == "update":
        return cli_update(args)
    if command == "copy":
        return cli_copy(args)
    if command == "remove":
        return cli_remove(args)
    if command == "embedder":
        embedder_command = getattr(args, "embedder_command", None)
        if embedder_command == "show":
            return cli_embedder_show(args)
        if embedder_command == "set":
            return cli_embedder_set(args)
        parser.parse_args(["index", "embedder", "--help"])
        return 2
    if command == "publish-vectors":
        return cli_publish_vectors(args)
    if command == "publishing":
        publishing_command = getattr(args, "publishing_command", None)
        if publishing_command == "show":
            return cli_publishing_show(args)
        if publishing_command == "set":
            return cli_publishing_set(args)
        parser.parse_args(["index", "publishing", "--help"])
        return 2
    if command == "vector-backend":
        backend_command = getattr(args, "vector_backend_command", None)
        if backend_command == "show":
            return cli_vector_backend_show(args)
        if backend_command == "set":
            return cli_vector_backend_set(args)
        parser.parse_args(["index", "vector-backend", "--help"])
        return 2
    if command == "install":
        return cli_install()
    if command == "uninstall":
        return cli_uninstall()
    parser.parse_args(["index", "--help"])
    return 2


def cli_serve(args: Any) -> int:
    """``palace index serve`` — boot the daemon."""
    store = _resolve_store(args)
    if not store.parent.is_dir():
        print(
            f"error: --store parent does not exist: {store.parent}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    try:
        return serve(store=store, verbose=args.verbose)
    except (IndexError, WriterLockTimeout) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


def cli_build(args: Any) -> int:
    """``palace index build`` — the synchronous walk-diff-reembed tool."""
    store = _resolve_store(args)
    if not store.parent.is_dir():
        message = f"--store parent does not exist: {store.parent}"
        print(f"error: {message}", file=sys.stderr, flush=True)
        _print_failure(args, message)
        return 1
    watch_root_filter: Path | None
    if args.watch_root is not None:
        watch_root_filter = Path(args.watch_root).expanduser().resolve()
    else:
        watch_root_filter = None
    try:
        report = build(
            store=store,
            watch_root_filter=watch_root_filter,
            full=args.full,
            verbose=args.verbose,
            embed_concurrency=args.embed_concurrency,
            lock_timeout=args.lock_timeout,
        )
    except (IndexError, WatchError, WriterLockTimeout, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        _print_failure(args, str(exc), getattr(exc, "embedding_usage", None))
        return 1
    if getattr(args, "json", False):
        _print_json(
            {
                "ok": True,
                "roots": [_result_dict(result) for result in report.roots],
                "embedding": report.embedding.as_dict(),
            }
        )
    return 0


def cli_update(args: Any) -> int:
    """``palace index update`` — reflect named files; exit 0, or 1 with one ``error:`` line."""
    store = _resolve_store(args)
    if not store.is_dir():
        message = f"--store does not exist: {store}"
        print(f"error: {message}", file=sys.stderr, flush=True)
        _print_failure(args, message)
        return 1
    try:
        result = update(
            store=store,
            watch_root=Path(args.watch_root),
            paths=[Path(item) for item in args.paths],
            lock_timeout=args.lock_timeout,
            verbose=args.verbose,
        )
    except (IndexError, WatchError, WriterLockTimeout, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        _print_failure(args, str(exc), getattr(exc, "embedding_usage", None))
        return 1
    if getattr(args, "json", False):
        _print_json({"ok": True, **_result_dict(result)})
    return 0


def _result_dict(result: Any) -> dict[str, Any]:
    """A build root's or an update's result as JSON values, its usage as a nested object."""
    values = {name: getattr(result, name) for name in result.__dataclass_fields__}
    values["watch_root"] = str(values["watch_root"])
    values["embedding"] = result.embedding.as_dict()
    return values


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def _print_failure(args: Any, message: str, usage: EmbeddingUsageSummary | None = None) -> None:
    """With --json, the failure and what was spent before it (nothing, for a refusal made
    before any request), as the one stdout object."""
    if not getattr(args, "json", False):
        return
    _print_json(
        {
            "ok": False,
            "error": message,
            "embedding": (usage if usage is not None else EmbeddingUsageSummary()).as_dict(),
        }
    )


def cli_publish_vectors(args: Any) -> int:
    """``palace index publish-vectors`` — one summary line; exit 0, or 1 with one error."""
    store = _resolve_store(args)
    try:
        result = publish_vectors(
            store=store,
            bucket=args.bucket,
            index=args.index,
            region=args.region,
            profile=args.profile,
            text_metadata=args.text_metadata,
        )
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        if getattr(args, "json", False):
            usage = getattr(exc, "vector_usage", None) or VectorUsageSummary()
            _print_json({"ok": False, "error": str(exc), "usage": usage.as_dict()})
        return 1
    print(
        f"palace index publish-vectors: put={result.put} deleted={result.deleted} "
        f"unchanged={result.unchanged} vectors={result.vector_count} "
        f"listed_remote={'yes' if result.listed_remote else 'no'}" + result.usage.log_fields(),
        file=sys.stderr,
        flush=True,
    )
    if getattr(args, "json", False):
        _print_json({"ok": True, **result.as_dict()})
    return 0


def cli_publishing_show(args: Any) -> int:
    """Print the opt-in and the last publication receipt, if any."""
    store = _resolve_store(args)
    try:
        config = load_publishing_config(store)
        receipt = read_receipt(store)
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    if config is None:
        print(f"opt-in: none ({publishing_config_path(store)} absent)")
    else:
        print(f"opt-in: {publishing_config_path(store)}")
        print(f"published_corpus_note: {config.published_corpus_note}")
        print(f"published_corpus_asserted_at: {config.published_corpus_asserted_at}")
    if receipt is None:
        print("receipt: none")
    else:
        print(f"receipt: {receipt.state} index_arn={receipt.index_arn}")
        print(f"vectors: {receipt.vector_count if receipt.vector_count is not None else 'unknown'}")
        print(f"text_metadata: {str(receipt.text_metadata).lower()}")
    return 0


def cli_publishing_set(args: Any) -> int:
    """Record the published-corpus opt-in with the time of the assertion."""
    store = _resolve_store(args)
    try:
        config = save_publishing_config(store, note=str(args.published_corpus_note))
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"opt-in recorded at {config.published_corpus_asserted_at}")
    return 0


def cli_vector_backend_show(args: Any) -> int:
    store = _resolve_store(args)
    try:
        config = load_vector_backend_config(store)
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    selector = vector_backend_config_path(store)
    print(f"source: {selector if selector.exists() else 'default (vector-backend.toml absent)'}")
    print(f"backend: {config.backend}")
    print(f"profile: {config.profile or 'none'}")
    return 0


def cli_vector_backend_set(args: Any) -> int:
    store = _resolve_store(args)
    try:
        save_vector_backend_config(
            store, VectorBackendConfig(backend=args.backend, profile=args.profile)
        )
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"vector backend: {args.backend}")
    return 0


def cli_copy(args: Any) -> int:
    """``palace index copy`` — one summary line; exit 0, or 1 with one ``error:`` line."""
    try:
        result = copy_paths(
            source=Path(args.source).expanduser(),
            destination=Path(args.destination).expanduser(),
            watch_root=Path(args.watch_root),
            paths=list(args.paths),
            lock_timeout=args.lock_timeout,
        )
    except (IndexError, WriterLockTimeout, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    print(
        f"palace index copy: copied={result.copied} removed={result.removed} "
        f"metadata={'changed' if result.metadata_changed else 'unchanged'}",
        flush=True,
    )
    return 0


def cli_remove(args: Any) -> int:
    """``palace index remove`` — one summary line; exit 0, or 1 with one ``error:`` line."""
    try:
        result = remove_paths(
            store=_resolve_store(args),
            watch_root=Path(args.watch_root),
            paths=list(args.paths),
            lock_timeout=args.lock_timeout,
        )
    except (IndexError, WriterLockTimeout, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    print(
        f"palace index remove: removed={result.removed} "
        f"metadata={'changed' if result.metadata_changed else 'unchanged'}",
        flush=True,
    )
    return 0


def cli_check(args: Any) -> int:
    """``palace index check <path>`` — classification.

    Reports one of ``indexed:`` / ``not-indexed:`` / ``not-watched:``.
    Reads the chunks DB read-only (URI form ``file:...?mode=ro``).
    """
    try:
        store = _resolve_store(args)
        target = Path(args.path).expanduser().resolve()
        config = WatchRootsConfig.load(default_config_path(store))
    except WatchError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1

    covering = _find_covering_root(target, config)
    if covering is None:
        print(f"not-watched: {target}", flush=True)
        return 0

    # Map the absolute argument to the stored watch-root-relative key.
    # The stored ``watch_root`` is the absolute resolved root, and the
    # stored ``path`` is ``target`` relative to it.
    covering_resolved = covering.expanduser().resolve()
    try:
        rel = target.relative_to(covering_resolved).as_posix()
    except ValueError:
        # ``target`` is covered by ``covering`` per ``_find_covering_root``
        # but did not resolve under its resolved form (symlink edge);
        # treat as not-watched rather than guessing a key.
        print(f"not-watched: {target}", flush=True)
        return 0

    db_path = store / "index" / "chunks.sqlite"
    if not db_path.is_file():
        print(f"not-indexed: {target}", flush=True)
        return 0

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            cur = conn.execute(
                """
                SELECT COUNT(*), MAX(ingest_time), MAX(kind)
                FROM chunks WHERE watch_root = ? AND path = ?
                """,
                (str(covering_resolved), rel),
            )
            row = cur.fetchone()
    except sqlite3.Error as exc:
        print(f"error: cannot read chunks DB: {exc}", file=sys.stderr, flush=True)
        return 1

    count = int(row[0]) if row and row[0] is not None else 0
    last_indexed = row[1] if row else None
    kind = row[2] if row else None
    if count == 0:
        print(f"not-indexed: {target}", flush=True)
        return 0
    print(
        f"indexed: {target} kind={kind} chunks={count} last_indexed_at={last_indexed}",
        flush=True,
    )
    return 0


def cli_embedder_show(args: Any) -> int:
    """Print the validated selector and the identity a full build would stamp."""
    store = _resolve_store(args)
    try:
        config = load_embedder_config(store)
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    selector = embedder_config_path(store)
    source = str(selector) if selector.exists() else "default (embedder.toml absent)"
    identity = identity_for(config)
    print(f"source: {source}")
    print(f"provider: {config.provider}")
    print(f"model: {config.model}")
    print(f"dim: {config.dim}")
    print(f"upstream: {config.upstream if config.upstream is not None else 'none'}")
    if config.provider == PROVIDER_OPENROUTER:
        print(f"published_corpus: {str(config.published_corpus).lower()}")
        print(f"published_corpus_note: {config.published_corpus_note or 'none'}")
        print(f"published_corpus_asserted_at: {config.published_corpus_asserted_at or 'none'}")
    print(
        "stamped_identity: "
        f"convention={identity.convention} model={identity.model} "
        f"provider={identity.provider} dim={identity.dim}"
    )
    return 0


def cli_embedder_set(args: Any) -> int:
    """Validate and atomically persist one explicit store-level selector."""
    store = _resolve_store(args)
    if args.provider == PROVIDER_OLLAMA:
        remote_flags = [
            name
            for name, value in (
                ("--model", args.model),
                ("--upstream", args.upstream),
                ("--dim", args.dim),
                ("--published-corpus", args.published_corpus),
                ("--published-corpus-note", args.published_corpus_note),
            )
            if value is not None and value is not False
        ]
        if remote_flags:
            print(
                "error: --provider ollama does not accept " + ", ".join(remote_flags),
                file=sys.stderr,
                flush=True,
            )
            return 1
        new_config = EmbedderConfig(
            provider=PROVIDER_OLLAMA,
            model=EMBEDDER_MODEL,
            dim=EMBED_DIM,
        )
    else:
        missing = [
            flag
            for flag, absent in (
                ("--model", not args.model or not str(args.model).strip()),
                ("--upstream", not args.upstream or not str(args.upstream).strip()),
                ("--published-corpus", not args.published_corpus),
                (
                    "--published-corpus-note",
                    not args.published_corpus_note or not str(args.published_corpus_note).strip(),
                ),
            )
            if absent
        ]
        if missing:
            print(
                "error: --provider openrouter requires " + ", ".join(missing),
                file=sys.stderr,
                flush=True,
            )
            return 1
        if args.dim is not None and args.dim <= 0:
            print("error: --dim must be positive", file=sys.stderr, flush=True)
            return 1
        new_config = EmbedderConfig(
            provider=PROVIDER_OPENROUTER,
            model=str(args.model),
            upstream=str(args.upstream),
            dim=args.dim if args.dim is not None else EMBED_DIM,
            published_corpus=True,
            published_corpus_note=str(args.published_corpus_note),
            published_corpus_asserted_at=datetime.now(BOISE_TZ).isoformat(timespec="seconds"),
        )

    # ``set`` is the one path that must tolerate an unreadable selector: it is
    # the command every other path's error message tells the operator to run,
    # so refusing here would leave a broken selector unfixable except by hand.
    # Every read path keeps refusing.
    old_identity = None
    selector_problem: str | None = None
    try:
        old_identity = resolve_identity(store)
    except IndexError as exc:
        selector_problem = str(exc)
    try:
        has_chunks = _store_has_chunks(store)
    except (IndexError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    if selector_problem is not None:
        print(
            f"warning: replacing an unreadable embedder config at "
            f"{embedder_config_path(store)} — {selector_problem}",
            file=sys.stderr,
            flush=True,
        )
    new_identity = identity_for(new_config)
    # An unreadable prior selector means the prior identity is unknown, not
    # unchanged. Skip the advisory comparison rather than guess: the chunks
    # DB's own stamped identity is the authority, and the build- and
    # serve-time guards check the new config against it.
    identity_changed = old_identity is not None and new_identity != old_identity
    if has_chunks and identity_changed and not args.i_will_rebuild:
        print(
            "error: switching provider or model requires 'palace index build --full'; "
            "until it runs, incremental builds refuse and 'palace index serve' parks "
            "(pass --i-will-rebuild to confirm)",
            file=sys.stderr,
            flush=True,
        )
        return 1
    try:
        save_embedder_config(store, new_config)
    except (IndexError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"embedder config written: {embedder_config_path(store)}")
    if has_chunks and identity_changed:
        print(
            "switching provider or model requires 'palace index build --full'; "
            "until it runs, incremental builds refuse and 'palace index serve' parks"
        )
    elif has_chunks and selector_problem is not None:
        print(
            "the previous selector was unreadable, so this store's recorded identity "
            "could not be compared; 'palace index build' verifies the new selector "
            "against the identity stamped in the chunks DB and refuses on a mismatch"
        )
    return 0


def cli_status(args: Any) -> int:
    """``palace index status`` — daemon liveness + cursor + counts."""
    try:
        store = _resolve_store(args)
    except WatchError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1

    # daemon: line
    try:
        daemon_pids = _pgrep_daemon()
        liveness_error: str | None = None
    except RuntimeError as exc:
        daemon_pids = frozenset()
        liveness_error = str(exc)
    if daemon_pids:
        rendered_pids = ",".join(str(pid) for pid in sorted(daemon_pids))
        label = "pid" if len(daemon_pids) == 1 else "pids"
        print(f"daemon: running ({label}={rendered_pids})", flush=True)
    elif liveness_error is not None:
        print(f"daemon: unknown ({liveness_error})", flush=True)
    else:
        print("daemon: stopped (pid=none)", flush=True)

    # cursor: line
    try:
        cursor = TailCursor.load(store)
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    last_id = cursor.last_event_id[:12] if cursor.last_event_id else "none"
    print(
        f"cursor: day={cursor.day} byte_offset={cursor.byte_offset} last_event_id={last_id}",
        flush=True,
    )

    # chunks: line
    db_path = chunks_db_path(store)
    identity_ok: bool | None = None
    identity_axes: tuple[str, ...] = ()
    if db_path.is_file():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                counts = dict(
                    conn.execute("SELECT kind, COUNT(*) FROM chunks GROUP BY kind").fetchall()
                )
                files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                recorded_identity = read_identity(conn)
            markdown = counts.get("markdown", 0)
            jsonl = counts.get("jsonl", 0)
            code = counts.get("code", 0)
            text = counts.get("text", 0)
            print(
                f"chunks: {total} rows; {markdown} markdown; {jsonl} jsonl; "
                f"{code} code; {text} text; {files} files indexed",
                flush=True,
            )
            _print_identity_axes(recorded_identity)
            try:
                expected_identity = resolve_identity(store)
                identity_axes = identity_divergences(
                    found=recorded_identity, expected=expected_identity
                )
                identity_ok = not identity_axes
                _print_index_identity(identity_axes)
            except IndexError as exc:
                print(f"index_identity: error resolving store identity: {exc}", flush=True)
        except sqlite3.Error as exc:
            print(f"chunks: error reading chunks DB: {exc}", flush=True)
            _print_identity_error(exc)
            print(f"index_identity: error reading chunks DB: {exc}", flush=True)
    else:
        print(
            "chunks: 0 rows; 0 markdown; 0 jsonl; 0 code; 0 text; 0 files indexed",
            flush=True,
        )
        from palace.index.schema import RecordedIdentity

        recorded_identity = RecordedIdentity(None, None, None, None)
        _print_identity_axes(recorded_identity)
        try:
            identity_axes = identity_divergences(
                found=recorded_identity, expected=resolve_identity(store)
            )
            identity_ok = False
            _print_index_identity(identity_axes)
        except IndexError as exc:
            print(f"index_identity: error resolving store identity: {exc}", flush=True)

    _print_indexing_verdict(
        store=store,
        daemon_pids=daemon_pids,
        liveness_error=liveness_error,
        identity_ok=identity_ok,
        identity_axes=identity_axes,
    )

    # last_event_lag: line
    today = datetime.now(BOISE_TZ).date()
    today_events = events_path(store, today)
    if today_events.is_file():
        try:
            mtime = today_events.stat().st_mtime
            lag = max(0, int(time.time() - mtime))
            print(f"last_event_lag: {lag} seconds", flush=True)
        except OSError:
            print("last_event_lag: none", flush=True)
    else:
        print("last_event_lag: none", flush=True)

    return 0


# --------------------------------------------------------------------- helpers


def _find_covering_root(path: Path, config: WatchRootsConfig) -> Path | None:
    """Return the first watch root that contains ``path``, or ``None``."""
    for root in config.roots:
        try:
            path.relative_to(root.path)
        except ValueError:
            continue
        return root.path
    return None


def _store_has_chunks(store: Path) -> bool:
    db_path = chunks_db_path(store)
    if not db_path.exists():
        return False
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks'"
        ).fetchone()
        if table is None:
            return False
        return int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]) > 0


def _print_identity_axes(recorded: Any) -> None:
    for label, value in (
        ("embed_convention", recorded.convention),
        ("embed_model", recorded.model),
        ("embed_provider", recorded.provider),
        ("embed_dim", recorded.dim),
    ):
        rendered = value if value is not None else "UNSTAMPED (run 'palace index build --full')"
        print(f"{label}: {rendered}", flush=True)


def _print_identity_error(exc: sqlite3.Error) -> None:
    for label in ("embed_convention", "embed_model", "embed_provider", "embed_dim"):
        print(f"{label}: error reading chunks DB: {exc}", flush=True)


def _print_index_identity(divergences: tuple[str, ...]) -> None:
    if not divergences:
        print("index_identity: OK", flush=True)
        return
    print(
        "index_identity: MISMATCH — "
        + "; ".join(divergences)
        + " — writes are refused; run 'palace index build --full'",
        flush=True,
    )


def _print_indexing_verdict(
    *,
    store: Path,
    daemon_pids: frozenset[int],
    liveness_error: str | None,
    identity_ok: bool | None,
    identity_axes: tuple[str, ...],
) -> None:
    state = read_state(store)
    if isinstance(state, StateUnreadable):
        print(f"indexing: UNKNOWN (unreadable {state.path})", flush=True)
        return
    if liveness_error is not None:
        print(f"indexing: UNKNOWN ({liveness_error})", flush=True)
        return
    if state is None:
        if not daemon_pids:
            print("indexing: STOPPED", flush=True)
        else:
            print("indexing: UNKNOWN (daemon running without state file)", flush=True)
        return
    if state.pid not in daemon_pids:
        print(f"indexing: STALE (pid={state.pid} not running)", flush=True)
        return
    if state.state == "parked":
        if state.park_kind == "identity" and identity_ok:
            print(
                "indexing: PARKED-REPAIRED — store identity is OK; the daemon resumes "
                f"within {PARK_RECHECK_SECONDS:g}s (or: launchctl kickstart -k "
                "gui/$(id -u)/ai.palace.index)",
                flush=True,
            )
        elif state.park_kind == "identity":
            reason = "; ".join(identity_axes) if identity_axes else (state.reason or "unknown")
            print(
                f"indexing: PARKED ({reason}) — run 'palace index build --full'",
                flush=True,
            )
        else:
            print(
                f"indexing: PARKED ({state.reason}) — correct the writer startup "
                "failure and restart the daemon",
                flush=True,
            )
        return
    if state.state == "indexing" and identity_ok:
        print("indexing: ACTIVE", flush=True)
        return
    print(f"indexing: UNKNOWN (daemon state={state.state})", flush=True)


def _pgrep_daemon() -> frozenset[int]:
    """Return every PID matching a running ``palace index serve`` process.

    Exit 1 means no match. Missing tooling, timeout, and all other failures
    stay distinct so status cannot turn an unreadable liveness signal into a
    reassuring ``STOPPED`` verdict.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "palace index serve"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot determine daemon liveness: {exc}") from exc
    if result.returncode == 1:
        return frozenset()
    if result.returncode != 0:
        raise RuntimeError(f"pgrep failed with exit {result.returncode}")
    raw_pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not raw_pids:
        return frozenset()
    # Exclude our own pid (so `palace index status` doesn't see itself).
    self_pid = os.getpid()
    try:
        return frozenset(int(pid) for pid in raw_pids if int(pid) != self_pid)
    except ValueError as exc:
        raise RuntimeError("pgrep returned a non-numeric pid") from exc
