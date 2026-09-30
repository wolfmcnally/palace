"""``palace reindex`` CLI subcommand group.

Four subcommands:

- ``palace reindex serve [--store <path>] [-v|--verbose]`` — run the
  FSEvents reindex daemon in the foreground (Phase 2.2).
- ``palace reindex install`` — install the daemon as a per-user
  LaunchAgent (Phase 2.5).
- ``palace reindex uninstall`` — uninstall the daemon LaunchAgent
  (Phase 2.5; idempotent).
- ``palace reindex bootstrap [--store <path>] [--watch-root <path>]
  [--force] [-v|--verbose]`` — first-run full-index walk that enumerates
  every file under every configured watch root the shared
  :class:`palace.watch.ignore.IgnoreEngine` admits and emits one
  synthetic ``change_kind="created"`` event per file into the same
  events JSONL log the FSEvents daemon writes (Phase 2.6).

The flag surface for ``serve`` mirrors the precedence rule Phase 2.1's
``palace watch`` settled on — ``--store <path>`` overrides ``$PALACE_STORE``
overrides :data:`palace.daemons.capture.config.DEFAULT_STORE` — plus a single
``--verbose`` / ``-v`` toggle that turns drop lines on. ``install`` /
``uninstall`` take no flags. ``bootstrap`` honors the same store-resolution
precedence; ``--watch-root`` restricts the walk to one configured root;
``--force`` clears any prior ``completed_at`` for the named root(s).

Error translation follows the :class:`palace.watch._errors.WatchError` /
:class:`palace.hooks._common.HooksError` shape: every CLI error path emits
one line to stderr prefixed with ``error:`` and exits 1. No Python
tracebacks reach the operator.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from palace.daemons.capture.config import DEFAULT_STORE
from palace.reindex._errors import ReindexError
from palace.reindex.bootstrap import bootstrap
from palace.reindex.lifecycle import cli_install, cli_uninstall
from palace.reindex.server import serve
from palace.watch._errors import WatchError

__all__ = ["build_subparser", "dispatch_reindex"]


def _resolve_store(args: Any) -> Path:
    """Return the resolved machine-state root per the precedence rule.

    1. ``--store <path>`` (CLI flag); takes the value verbatim.
    2. ``$PALACE_STORE``; fallback for test isolation and the eventual
       reindexer plist's ``EnvironmentVariables``.
    3. :data:`palace.daemons.capture.config.DEFAULT_STORE`
       (``~/palace-data.noindex/``).
    """
    flag = getattr(args, "store", None)
    if flag is not None:
        return Path(flag).expanduser()
    env = os.environ.get("PALACE_STORE")
    if env:
        return Path(env).expanduser()
    return DEFAULT_STORE


def build_subparser(subparsers: Any) -> None:
    """Wire the ``reindex`` subparser group into ``palace`` argparse.

    Called from :func:`palace.cli._build_parser`. Three subcommands:
    ``serve`` (Phase 2.2), ``install`` and ``uninstall`` (Phase 2.5).
    """
    reindex = subparsers.add_parser(
        "reindex",
        help="FSEvents reindex daemon controls.",
        description=(
            "Run the FSEvents-driven reindex daemon. Subscribes to every "
            "watch root in the watch-roots config, debounces 100 ms per "
            "path, and appends one change-event line per surviving change "
            "to <store>/events/<YYYY-MM-DD>.jsonl."
        ),
    )
    sub = reindex.add_subparsers(dest="reindex_command")

    serve_p = sub.add_parser(
        "serve",
        help="Run the reindex daemon in the foreground.",
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
        help="Log every drop with reason= to stderr.",
    )

    sub.add_parser(
        "install",
        help="Install the reindex daemon as a per-user LaunchAgent.",
    )
    sub.add_parser(
        "uninstall",
        help="Uninstall the reindex daemon LaunchAgent.",
    )

    boot_p = sub.add_parser(
        "bootstrap",
        help="First-run full-index walk; emits one created event per admitted file.",
    )
    boot_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    boot_p.add_argument(
        "--watch-root",
        type=Path,
        default=None,
        help="Restrict the walk to one configured watch root (resolved absolute form).",
    )
    boot_p.add_argument(
        "--force",
        action="store_true",
        help="Re-walk even if the cursor reports the root already bootstrapped.",
    )
    boot_p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log every emit as a stderr line.",
    )


def cli_serve(args: Any) -> int:
    """``palace reindex serve`` — boot the daemon."""
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
    except ReindexError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


def cli_bootstrap(args: Any) -> int:
    """``palace reindex bootstrap`` — first-run full-index walk."""
    store = _resolve_store(args)
    if not store.parent.is_dir():
        print(
            f"error: --store parent does not exist: {store.parent}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    watch_root_filter: Path | None
    if args.watch_root is not None:
        watch_root_filter = Path(args.watch_root).expanduser().resolve()
    else:
        watch_root_filter = None
    try:
        return bootstrap(
            store=store,
            watch_root_filter=watch_root_filter,
            force=args.force,
            verbose=args.verbose,
        )
    except (ReindexError, WatchError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


def dispatch_reindex(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch ``palace reindex <subcommand>`` to the right handler."""
    command = getattr(args, "reindex_command", None)
    if command == "serve":
        return cli_serve(args)
    if command == "install":
        return cli_install()
    if command == "uninstall":
        return cli_uninstall()
    if command == "bootstrap":
        return cli_bootstrap(args)
    parser.parse_args(["reindex", "--help"])
    return 2
