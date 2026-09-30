"""``palace status`` — at-a-glance health of the unified two-root store.

Read-only and side-effect-free. Reports, for the three palace
LaunchAgents (capture / reindex / index), whether each is live in the
``gui/<uid>`` domain (via ``launchctl list``); the chunk count and last
ingest timestamp from ``<store>/index/chunks.sqlite``; and the character
count of ``<vault-root>/context/working.md``.

GREEN contract: GREEN when all three daemons are live AND the chunks DB
holds at least one row AND ``context/working.md`` is readable. Otherwise
RED with a one-line reason. ``last_indexed`` is eyeball-only — it is
printed but is NOT part of the gate. The exit code matches the verdict
(0 GREEN / 1 RED).

The three daemon labels are imported from their lifecycle modules rather
than re-declared, so a label change has exactly one home per daemon.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from palace.daemons.capture.lifecycle import PLIST_LABEL as CAPTURE_LABEL
from palace.index.config import chunks_db_path
from palace.index.lifecycle import PLIST_LABEL as INDEX_LABEL
from palace.reindex.lifecycle import PLIST_LABEL as REINDEX_LABEL
from palace.vault import resolve_vault_root, working_md_path

__all__ = [
    "StatusReport",
    "build_subparser",
    "cli_status",
    "collect_status",
    "dispatch_status",
]


# The three daemon labels, in report order. Imported, never re-declared.
_DAEMON_LABELS: tuple[tuple[str, str], ...] = (
    ("capture", CAPTURE_LABEL),
    ("reindex", REINDEX_LABEL),
    ("index", INDEX_LABEL),
)


@dataclass
class StatusReport:
    """A snapshot of the unified store's operational health.

    ``daemons`` maps the short daemon name (``capture`` / ``reindex`` /
    ``index``) to whether it is live. ``green`` and ``reason`` carry the
    final verdict; ``reason`` is ``None`` exactly when ``green`` is True.
    """

    daemons: dict[str, bool]
    chunk_count: int | None
    last_indexed: str | None
    working_chars: int | None
    green: bool
    reason: str | None


def _launchctl_running(label: str) -> bool:
    """Return True when ``label`` is loaded in the user's launchd domain.

    Best-effort: a missing ``launchctl`` (non-macOS, or stripped PATH) or
    a non-zero return is reported as not-running rather than raising.
    ``palace status`` is a report, not a check.
    """
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _chunk_count_and_last_indexed(store: Path) -> tuple[int | None, str | None]:
    """Return ``(chunk_count, last_indexed)`` from the chunks DB, read-only.

    Returns ``(None, None)`` when the chunks DB does not exist or cannot
    be read. Connects via the ``file:...?mode=ro`` URI form — the same
    read-only idiom :mod:`palace.index.cli` uses.
    """
    db_path = chunks_db_path(store)
    if not db_path.is_file():
        return (None, None)
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            last = conn.execute("SELECT MAX(ingest_time) FROM chunks").fetchone()[0]
    except sqlite3.Error:
        return (None, None)
    return (int(count), last)


def _working_chars(vault_root: Path) -> int | None:
    """Return the char count of ``context/working.md``, or None if absent."""
    path = working_md_path(vault_root)
    if not path.is_file():
        return None
    try:
        return len(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def collect_status(*, store: Path, vault_root: Path) -> StatusReport:
    """Gather the read-only status snapshot and compute the GREEN verdict."""
    daemons = {name: _launchctl_running(label) for name, label in _DAEMON_LABELS}
    chunk_count, last_indexed = _chunk_count_and_last_indexed(store)
    working_chars = _working_chars(vault_root)

    reason: str | None = None
    down = [name for name, live in daemons.items() if not live]
    if down:
        reason = f"daemon(s) not running: {', '.join(down)}"
    elif chunk_count is None:
        reason = "chunks DB unreadable or missing"
    elif chunk_count == 0:
        reason = "chunks DB empty (0 rows)"
    elif working_chars is None:
        reason = "context/working.md missing or unreadable"

    return StatusReport(
        daemons=daemons,
        chunk_count=chunk_count,
        last_indexed=last_indexed,
        working_chars=working_chars,
        green=reason is None,
        reason=reason,
    )


def build_subparser(subparsers: Any) -> None:
    """Wire the ``status`` subparser into ``palace`` argparse."""
    status_p = subparsers.add_parser(
        "status",
        help="Report the unified store's health (GREEN / RED).",
        description=(
            "Read-only health snapshot of palace's two-root store: launchd "
            "daemon liveness (capture / reindex / index), chunk count and "
            "last-indexed timestamp from the chunks DB, and the char count "
            "of context/working.md. Prints a final GREEN or RED line and "
            "exits 0 (GREEN) or 1 (RED)."
        ),
    )
    status_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    status_p.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Human-readable root (default: $PALACE_VAULT_ROOT or ~/Obsidian/Palace/).",
    )


def dispatch_status(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch ``palace status`` to :func:`cli_status`."""
    return cli_status(args)


def cli_status(args: Any) -> int:
    """``palace status`` — print per-component detail + the verdict line.

    Resolves ``<store>`` and ``<vault-root>`` from the same precedence the
    rest of the CLI uses, prints one line per daemon plus ``last_indexed:``,
    ``chunks:``, and ``working_chars:``, then a final ``status: GREEN`` or
    ``status: RED — <reason>`` line. Exit code matches the verdict.
    """
    from palace.index.cli import _resolve_store

    store = _resolve_store(args)
    vault_root = resolve_vault_root(args)
    report = collect_status(store=store, vault_root=vault_root)

    for name, _label in _DAEMON_LABELS:
        state = "running" if report.daemons[name] else "stopped"
        print(f"{name}: {state}", flush=True)

    print(f"last_indexed: {report.last_indexed or 'none'}", flush=True)
    chunks = report.chunk_count if report.chunk_count is not None else 0
    print(f"chunks: {chunks} rows", flush=True)
    working = report.working_chars if report.working_chars is not None else "none"
    print(f"working_chars: {working}", flush=True)

    if report.green:
        print("status: GREEN", flush=True)
        return 0
    print(f"status: RED — {report.reason}", flush=True)
    return 1
