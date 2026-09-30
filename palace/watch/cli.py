"""``palace watch`` CLI subcommand group.

Four subcommands, each a thin shim around :class:`WatchRootsConfig` mutation
or :class:`IgnoreEngine` inspection:

- ``palace watch add <path> [--activate]`` — append a watch root after the
  validation rules in :meth:`WatchRootsConfig.with_added`. With
  ``--activate``, also make the *running* system pick the root up in one
  step: restart the reindex daemon (so its FSEvents observer schedules the
  new subtree for live changes) and bootstrap-walk the root (so the files
  already on disk are seeded into the index). Without ``--activate`` the
  root is only registered in the config; the running daemon keeps ignoring
  it until its next restart.
- ``palace watch remove <path>`` — drop the matching watch root.
- ``palace watch list`` — print one absolute path per line in insertion
  order; entries whose ``path`` no longer resolves to a directory are
  suffixed with ``  (missing on disk)``.
- ``palace watch check <path>`` — classify ``<path>`` as ``indexed:``,
  ``ignored: ... (reason: dotfile|gitignore)``, or ``not-watched:`` via the
  shared :class:`IgnoreEngine`.

Store-resolution precedence (mirrors the pattern Phase 1.4 settled on but
adds env-var fallback, which is what Phase 2.5's launchd plist will reuse):

1. ``--store <path>`` (CLI flag); if set, takes the value verbatim.
2. ``$PALACE_STORE`` (environment variable); fallback for test isolation
   and the eventual reindexer plist's ``EnvironmentVariables``.
3. :data:`palace.daemons.capture.config.DEFAULT_STORE`
   (``~/palace-data.noindex/``).

Note: ``palace capture serve`` is **not** retroactively rewired in Phase
2.1 — its ``--store`` argument still defaults to :data:`DEFAULT_STORE`
without env-var fallback. Phase 2.5 will harmonize the two surfaces.

Error translation follows the :class:`palace.hooks._common.HooksError`
shape: every CLI error path emits one line to stderr prefixed with
``error:`` and exits 1. No Python tracebacks reach the operator.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from palace.daemons.capture.config import DEFAULT_STORE
from palace.watch._errors import WatchError, WatchRootsError
from palace.watch.config import WatchRoot, WatchRootsConfig, default_config_path
from palace.watch.ignore import IgnoreEngine

__all__ = [
    "CheckResult",
    "build_subparser",
    "cli_add",
    "cli_check",
    "cli_list",
    "cli_remove",
    "dispatch_watch",
    "format_check_result",
]


Verdict = Literal["indexed", "ignored:dotfile", "ignored:gitignore", "not-watched"]


@dataclass(frozen=True)
class CheckResult:
    """The outcome of ``palace watch check <path>``."""

    verdict: Verdict
    path: Path
    root: Path | None


# --------------------------------------------------------------------- resolution


def _resolve_store(args: Any) -> Path:
    """Return the resolved machine-state root per the precedence rule."""
    flag = getattr(args, "store", None)
    if flag is not None:
        return Path(flag).expanduser().resolve()
    env = os.environ.get("PALACE_STORE")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_STORE


def _resolve_path(value: str) -> Path:
    """Resolve a CLI path argument: expand ``~`` and absolutize."""
    return Path(value).expanduser().resolve()


# --------------------------------------------------------------------- classification


def _find_covering_root(path: Path, roots: Iterable[WatchRoot]) -> Path | None:
    """Return the first watch root that contains ``path``, or ``None``.

    Insertion order is the tie-breaker; the validation in
    :meth:`WatchRootsConfig.with_added` already rejects overlapping roots so
    in practice at most one matches.
    """
    for root in roots:
        try:
            path.relative_to(root.path)
        except ValueError:
            continue
        return root.path
    return None


def _classify(path: Path, config: WatchRootsConfig) -> CheckResult:
    root = _find_covering_root(path, config.roots)
    if root is None:
        return CheckResult(verdict="not-watched", path=path, root=None)
    engine = IgnoreEngine(root)
    reason = engine.reason(path)
    if reason == "dotfile":
        return CheckResult(verdict="ignored:dotfile", path=path, root=root)
    if reason == "gitignore":
        return CheckResult(verdict="ignored:gitignore", path=path, root=root)
    return CheckResult(verdict="indexed", path=path, root=root)


# --------------------------------------------------------------------- formatting


def format_check_result(result: CheckResult) -> str:
    """Return the single-line ``palace watch check`` output for ``result``.

    No trailing newline; the CLI shim's :func:`print` adds it.
    """
    if result.verdict == "indexed":
        return f"indexed: {result.path} (root: {result.root})"
    if result.verdict == "ignored:dotfile":
        return f"ignored: {result.path} (root: {result.root}, reason: dotfile)"
    if result.verdict == "ignored:gitignore":
        return f"ignored: {result.path} (root: {result.root}, reason: gitignore)"
    return f"not-watched: {result.path}"


# --------------------------------------------------------------------- CLI handlers


def cli_add(args: Any) -> int:
    """``palace watch add <path> [--activate]`` — append a watch root.

    With ``--activate`` (see :func:`_activate`), restart the reindex daemon
    and bootstrap-walk the new root so a single command both registers the
    root and makes the running system index it. Without it, the historical
    register-only behaviour is preserved: the config is updated but the
    live daemon does not see the root until it next restarts.
    """
    try:
        store = _resolve_store(args)
        target = _resolve_path(args.path)
        config_path = default_config_path(store)
        config = WatchRootsConfig.load(config_path)
        updated = config.with_added(target, store_root=store)
        updated.save(config_path)
    except WatchError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"watch: added {target}", flush=True)
    if getattr(args, "activate", False):
        return _activate(store=store, target=target)
    return 0


def _activate(*, store: Path, target: Path) -> int:
    """Restart the reindex daemon and bootstrap-walk ``target``.

    Order is **restart-then-bootstrap** on purpose: once the observer is
    live on the new subtree, any edit that lands during the (possibly long)
    bootstrap walk is also caught by FSEvents. The duplicate is harmless —
    the index pipeline keys on a content hash and short-circuits an
    unchanged file. Bootstrapping first would leave a gap between the walk
    finishing and the observer starting in which live edits are missed.

    The daemon-not-loaded case is a warning, not a failure: the root is
    still bootstrapped (existing files are seeded), and the operator is
    told to ``palace reindex install`` for live updates. Returns 0 when the
    walk completes; 1 on a hard launchctl error or a bootstrap failure.
    """
    # Deferred imports: the reindex package pulls in the writer/embedder
    # stack, which the common non-activating `palace watch` paths must not
    # pay for. Importing here also confines the watch->reindex dependency
    # to the one branch that needs it.
    from palace.daemons.launchd import LifecycleError
    from palace.reindex._errors import ReindexError
    from palace.reindex.bootstrap import bootstrap
    from palace.reindex.lifecycle import RESTART_NOT_LOADED, restart

    # The installed daemon runs against the canonical store. With a custom
    # --store, restarting it would touch an unrelated service AND still not
    # live-watch this root, so skip the restart entirely — warn, then seed.
    canonical_store = store.expanduser().resolve() == DEFAULT_STORE.expanduser().resolve()
    if not canonical_store:
        print(
            f"watch: --store {store} is not the canonical store ({DEFAULT_STORE}); "
            f"the reindex daemon won't live-watch it. Seeding existing files only.",
            file=sys.stderr,
            flush=True,
        )
    else:
        try:
            outcome = restart()
        except LifecycleError as exc:
            print(f"error: {exc}", file=sys.stderr, flush=True)
            return 1
        if outcome == RESTART_NOT_LOADED:
            print(
                "watch: reindex daemon not loaded; live updates need "
                "'palace reindex install'. Seeding existing files anyway.",
                file=sys.stderr,
                flush=True,
            )
        else:
            print("watch: reindex daemon restarted", flush=True)

    try:
        rc = bootstrap(store=store, watch_root_filter=target)
    except (ReindexError, WatchError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    if rc != 0:
        return rc
    print(f"watch: activated {target}", flush=True)
    return 0


def cli_remove(args: Any) -> int:
    """``palace watch remove <path>`` — drop a watch root."""
    try:
        store = _resolve_store(args)
        target = _resolve_path(args.path)
        config_path = default_config_path(store)
        config = WatchRootsConfig.load(config_path)
        updated = config.with_removed(target)
        updated.save(config_path)
    except WatchError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"watch: removed {target}", flush=True)
    return 0


def cli_list(args: Any) -> int:
    """``palace watch list`` — print one absolute path per line, in insertion order.

    Entries whose ``path`` no longer resolves to a directory are suffixed
    with ``  (missing on disk)``. An absent or empty config prints
    ``(no watch roots configured)`` and exits 0. A malformed config file
    raises :class:`WatchRootsError`, which the shim translates into a
    single-line stderr ``error:`` line with exit 1.
    """
    try:
        store = _resolve_store(args)
        config_path = default_config_path(store)
        config = WatchRootsConfig.load(config_path)
    except WatchError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1

    if not config.roots:
        print("(no watch roots configured)", flush=True)
        return 0

    for root in config.roots:
        if root.path.is_dir():
            print(str(root.path), flush=True)
        else:
            print(f"{root.path}  (missing on disk)", flush=True)
    return 0


def cli_check(args: Any) -> int:
    """``palace watch check <path>`` — inspection-only classification.

    Returns exit 0 for every successful classification (``indexed:``,
    ``ignored: ...``, ``not-watched:``). Failures (config unreadable,
    ``--path`` resolution error) emit ``error:`` to stderr with exit 1.
    """
    try:
        store = _resolve_store(args)
        target = _resolve_path(args.path)
        config_path = default_config_path(store)
        config = WatchRootsConfig.load(config_path)
        result = _classify(target, config)
    except WatchError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    print(format_check_result(result), flush=True)
    return 0


# --------------------------------------------------------------------- argparse


def build_subparser(subparsers: Any) -> None:
    """Wire the ``watch`` subparser group into ``palace`` argparse.

    Called from :func:`palace.cli._build_parser`. The four subcommands each
    take ``--store <path>`` for hermetic test isolation and an env-var
    fallback handled in :func:`_resolve_store`.
    """
    watch = subparsers.add_parser(
        "watch",
        help="Watch-roots config and ignore-engine CLI.",
        description=(
            "Manage the watch-roots config the FSEvents reindexer (Phase 2.2 "
            "onwards) will consume, and inspect a path's classification via "
            "the shared ignore engine."
        ),
    )
    sub = watch.add_subparsers(dest="watch_command")

    add_p = sub.add_parser("add", help="Add a watch root.")
    add_p.add_argument("path", help="Directory to add as a watch root.")
    add_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    add_p.add_argument(
        "--activate",
        action="store_true",
        help=(
            "After adding, restart the reindex daemon and bootstrap-walk the new "
            "root so it is live-watched and seeded into the index in one step."
        ),
    )

    remove_p = sub.add_parser("remove", help="Remove a watch root.")
    remove_p.add_argument("path", help="Directory to remove from the watch-roots config.")
    remove_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )

    list_p = sub.add_parser("list", help="List configured watch roots, one per line.")
    list_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )

    check_p = sub.add_parser(
        "check",
        help="Classify a path as indexed / ignored / not-watched.",
    )
    check_p.add_argument("path", help="Path to classify.")
    check_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )


def dispatch_watch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch ``palace watch <subcommand>`` to the right handler."""
    command = getattr(args, "watch_command", None)
    if command == "add":
        return cli_add(args)
    if command == "remove":
        return cli_remove(args)
    if command == "list":
        return cli_list(args)
    if command == "check":
        return cli_check(args)
    parser.parse_args(["watch", "--help"])
    return 2


# Re-export so callers don't need a second import.
__all__ = [
    "CheckResult",
    "WatchError",
    "WatchRootsError",
    "build_subparser",
    "cli_add",
    "cli_check",
    "cli_list",
    "cli_remove",
    "dispatch_watch",
    "format_check_result",
]
