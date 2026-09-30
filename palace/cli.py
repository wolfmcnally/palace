"""Palace command-line entry point.

The CLI exposes ``--version`` and six subcommand groups:

- ``capture`` (``serve`` / ``post`` / ``install`` / ``uninstall``) — the
  loopback capture daemon, its CLI client, and its LaunchAgent lifecycle.
- ``hooks`` (``install`` / ``uninstall`` / ``status``) — register palace's
  Stop hook scripts in the per-user settings of every supported agent
  harness (Claude Code, Codex). ``install`` and ``uninstall`` take an
  optional ``--harness {claude-code,codex,all}`` selector defaulting to
  ``all``; ``status`` always reports every harness.
- ``watch`` (``add`` / ``remove`` / ``list`` / ``check``) — manage the
  watch-roots config the Phase 2 FSEvents reindexer will consume, and
  inspect a path's classification via the shared ignore engine.
- ``reindex`` (``serve`` / ``install`` / ``uninstall`` /
  ``bootstrap``) — run the FSEvents reindex daemon in the foreground
  (subscribes to every watch root and appends change-event records to
  ``<store>/events/<YYYY-MM-DD>.jsonl``), manage its per-user
  LaunchAgent lifecycle, or one-shot the first-run full-index walk
  (``bootstrap`` enumerates every admitted file and emits one synthetic
  ``created`` event per file through the same events log).
- ``index`` (``serve`` / ``check`` / ``status`` / ``build`` / ``update`` /
  ``embedder`` / ``install`` / ``uninstall``) — tail the events log,
  parse + chunk + embed Markdown files, and write the hybrid retrieval
  primitives (sqlite-vec + FTS5) at ``<store>/index/chunks.sqlite``;
  ``build`` is the synchronous, non-daemon walk-diff-reembed tool that
  reconciles a tree's chunks DB and exits; ``update`` reflects named
  files one at a time under the store's writer lock; ``install`` /
  ``uninstall`` manage the daemon's per-user LaunchAgent lifecycle.
- ``search`` (bare query / ``expand`` / ``transcript``) — the three
  recall-escalation tiers. The bare query (``palace search "<query>"``)
  is L1: hybrid BM25 + vector retrieval over the indexed chunks, fused
  via Reciprocal Rank Fusion, optionally refined by a local cross-encoder
  under ``--rerank`` (default on; ``--no-rerank`` opts out). Per-query ``--hyde`` and
  ``--multi-query [N]`` expansion applies on both reranked and un-reranked
  paths. ``expand <chunk_id>`` is L2 (widen a hit to its full Markdown
  section); ``transcript <session_id>`` is L3 (return a captured session's
  raw JSONL). Index/content reads remain read-only; remote inference appends audit records.
- ``status`` — read-only health snapshot of the unified two-root store:
  launchd daemon liveness, chunk count + last-indexed timestamp, and the
  char count of ``context/working.md``. Prints a final GREEN / RED line
  and exits 0 (GREEN) or 1 (RED).
- ``vault`` (``scaffold``) — establish the human-readable ``<vault-root>``
  structure (``facts/``, ``dreams/``, ``context/working.md``)
  idempotently and non-destructively.
- ``fact`` (``add`` / ``invalidate`` / ``supersede`` / ``query``) — manage
  bitemporal facts in ``<vault-root>/facts/MEMORY.md`` per
  ``policies/fact-schema.md``. ``add`` appends a fresh section; ``invalidate``
  and ``supersede`` patch ``valid`` / ``superseded_by`` in place and append a
  ``fact-invalidate`` / ``fact-supersede`` event to
  ``<store>/events/<YYYY-MM-DD>.jsonl``; ``query`` filters by subject, tag, or
  ``--as-of`` event date. ``--store`` and ``--vault-root`` are independent.
- ``consolidate`` — run one on-demand dreaming pass over a day's captures:
  extract candidate facts with a local LLM, score them on six signals, apply
  the three promotion gates, promote winners into
  ``<vault-root>/facts/MEMORY.md`` (via the fact writer), append a
  ``dreams/DREAMS.md`` audit, and surface contradictions to
  ``dreams/contradictions/``. ``--dry-run`` writes nothing and takes no lock.
  ``--store`` / ``--vault-root`` / ``--index-db`` / ``--lock-path`` are
  independent (storage-layout rule 9).

POST payloads carry an explicit ``event_type`` discriminator
(``"stop"`` | ``"subagent_stop"``) per the daemon wire contract.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from palace import __version__
from palace.consolidate.cli import build_subparser as _consolidate_build_subparser
from palace.consolidate.cli import dispatch_consolidate as _consolidate_dispatch
from palace.daemons.capture.client import post_payload
from palace.daemons.capture.config import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_STORE
from palace.daemons.capture.lifecycle import cli_install, cli_uninstall
from palace.daemons.capture.server import serve
from palace.fact.cli import build_subparser as _fact_build_subparser
from palace.fact.cli import dispatch_fact as _fact_dispatch
from palace.hooks import (
    HARNESSES,
)
from palace.hooks import (
    cli_install as cli_hooks_install,
)
from palace.hooks import (
    cli_uninstall as cli_hooks_uninstall,
)
from palace.hooks import (
    status as cli_hooks_status,
)
from palace.index.cli import build_subparser as _index_build_subparser
from palace.index.cli import dispatch_index as _index_dispatch
from palace.metadata_cli import build_subparser as _metadata_build_subparser
from palace.metadata_cli import dispatch as _metadata_dispatch
from palace.providers import build_subparser as _providers_build_subparser
from palace.providers import dispatch as _providers_dispatch
from palace.reindex.cli import build_subparser as _reindex_build_subparser
from palace.reindex.cli import dispatch_reindex as _reindex_dispatch
from palace.search import SEARCH_ESCALATION_COMMANDS as _SEARCH_ESCALATION_COMMANDS
from palace.search import build_escalation_parser as _search_build_escalation_parser
from palace.search import build_subparser as _search_build_subparser
from palace.search import dispatch_search as _search_dispatch
from palace.status import build_subparser as _status_build_subparser
from palace.status import dispatch_status as _status_dispatch
from palace.vault import resolve_vault_root, scaffold_vault
from palace.watch.cli import build_subparser as _watch_build_subparser
from palace.watch.cli import dispatch_watch as _watch_dispatch

__all__ = ["main"]


_HARNESS_CHOICES: tuple[str, ...] = (*HARNESSES, "all")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="palace",
        description="Palace — Wolf McNally's personal memory substrate.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print palace's version and exit.",
    )

    subparsers = parser.add_subparsers(dest="command")

    capture = subparsers.add_parser(
        "capture",
        help="Capture daemon controls.",
        description="HTTP loopback capture daemon (Stop + SubagentStop).",
    )
    capture_sub = capture.add_subparsers(dest="capture_command")

    serve_p = capture_sub.add_parser(
        "serve",
        help="Run the capture daemon in the foreground.",
    )
    serve_p.add_argument("--host", default=DEFAULT_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_p.add_argument("--store", type=Path, default=DEFAULT_STORE)

    post_p = capture_sub.add_parser(
        "post",
        help=(
            "POST a JSON payload (Stop or SubagentStop, routed by the "
            "top-level event_type discriminator) to a running capture daemon."
        ),
    )
    post_p.add_argument(
        "payload",
        help="Path to a JSON file, or '-' to read from stdin.",
    )
    post_p.add_argument("--host", default=DEFAULT_HOST)
    post_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    post_p.add_argument("--timeout", type=float, default=2.0)

    capture_sub.add_parser(
        "install",
        help="Install the capture daemon as a per-user LaunchAgent.",
    )
    capture_sub.add_parser(
        "uninstall",
        help="Uninstall the capture daemon LaunchAgent.",
    )

    hooks = subparsers.add_parser(
        "hooks",
        help="Per-harness hook registration controls.",
        description=(
            "Register / unregister / inspect palace's Stop hook scripts in "
            "the per-user settings of every supported agent harness "
            "(Claude Code, Codex)."
        ),
    )
    hooks_sub = hooks.add_subparsers(dest="hooks_command")
    install_p = hooks_sub.add_parser(
        "install",
        help="Register palace's hook scripts in the selected harness(es).",
    )
    install_p.add_argument(
        "--harness",
        choices=_HARNESS_CHOICES,
        default="all",
        help="Which harness to register; default 'all'.",
    )
    uninstall_p = hooks_sub.add_parser(
        "uninstall",
        help="Remove palace's hook scripts from the selected harness(es).",
    )
    uninstall_p.add_argument(
        "--harness",
        choices=_HARNESS_CHOICES,
        default="all",
        help="Which harness to deregister; default 'all'.",
    )
    hooks_sub.add_parser(
        "status",
        help="Report registered hooks for every harness, hook-log tail, and /health.",
    )

    _watch_build_subparser(subparsers)
    _reindex_build_subparser(subparsers)
    _index_build_subparser(subparsers)
    _metadata_build_subparser(subparsers)
    _providers_build_subparser(subparsers)
    _search_build_subparser(subparsers)
    _status_build_subparser(subparsers)
    _fact_build_subparser(subparsers)
    _consolidate_build_subparser(subparsers)

    vault = subparsers.add_parser(
        "vault",
        help="Human-readable vault-root controls.",
        description=(
            "Establish and inspect the human-readable <vault-root> "
            "(default ~/Obsidian/Palace/): facts/, dreams/, and "
            "context/working.md."
        ),
    )
    vault_sub = vault.add_subparsers(dest="vault_command")
    scaffold_p = vault_sub.add_parser(
        "scaffold",
        help="Idempotently create the vault structure (non-destructive).",
    )
    scaffold_p.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Human-readable root (default: $PALACE_VAULT_ROOT or ~/Obsidian/Palace/).",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    tokens = sys.argv[1:] if argv is None else argv

    # argv-peek: ``palace search expand ...`` / ``palace search transcript ...``
    # cannot be expressed as sub-subparsers of the L1 ``search`` parser (its
    # ``nargs="+"`` query positional would swallow the sub-subcommand token), so
    # route those two to a dedicated parser. A bare ``palace search "<query>"``
    # falls through to the main parser unchanged.
    if len(tokens) >= 2 and tokens[0] == "search" and tokens[1] in _SEARCH_ESCALATION_COMMANDS:
        escalation_parser = _search_build_escalation_parser()
        escalation_args = escalation_parser.parse_args(tokens[1:])
        return _search_dispatch(escalation_args, escalation_parser)

    parser = _build_parser()
    args = parser.parse_args(argv)

    # ``palace --version`` / ``palace`` — Phase 0 behavior, preserved.
    if args.command is None:
        print(f"palace {__version__}")
        return 0

    if args.command == "capture":
        if args.capture_command == "serve":
            return serve(host=args.host, port=args.port, store=args.store)
        if args.capture_command == "post":
            return post_payload(
                args.payload,
                host=args.host,
                port=args.port,
                timeout=args.timeout,
            )
        if args.capture_command == "install":
            return cli_install()
        if args.capture_command == "uninstall":
            return cli_uninstall()
        parser.parse_args(["capture", "--help"])
        return 2

    if args.command == "hooks":
        if args.hooks_command == "install":
            return cli_hooks_install(args)
        if args.hooks_command == "uninstall":
            return cli_hooks_uninstall(args)
        if args.hooks_command == "status":
            return cli_hooks_status(args)
        parser.parse_args(["hooks", "--help"])
        return 2

    if args.command == "watch":
        return _watch_dispatch(args, parser)

    if args.command == "reindex":
        return _reindex_dispatch(args, parser)

    if args.command == "index":
        return _index_dispatch(args, parser)

    if args.command == "metadata":
        return _metadata_dispatch(args)

    if args.command == "providers":
        return _providers_dispatch(args)

    if args.command == "search":
        return _search_dispatch(args, parser)

    if args.command == "status":
        return _status_dispatch(args, parser)

    if args.command == "fact":
        return _fact_dispatch(args, parser)

    if args.command == "consolidate":
        return _consolidate_dispatch(args, parser)

    if args.command == "vault":
        if args.vault_command == "scaffold":
            created = scaffold_vault(resolve_vault_root(args))
            if created:
                for path in created:
                    print(f"created: {path}")
            else:
                print("already scaffolded")
            return 0
        parser.parse_args(["vault", "--help"])
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; argparse.error raises SystemExit.


if __name__ == "__main__":
    sys.exit(main())
