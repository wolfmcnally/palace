"""``palace consolidate`` CLI subcommand.

One subcommand running a full on-demand consolidation pass:

    palace consolidate [--date YYYY-MM-DD] [--dry-run] [--json] [-v]
        [--store <path>] [--vault-root <path>]
        [--index-db <path>] [--lock-path <path>]
        [--source-kind {sessions,captures}] [--captures-root <dir>]
        [--since YYYY-MM-DD]

The four location flags are independent per ``policies/storage-layout.md``
rule 9 and ``briefs/store-parametric-external-consumers.md``: ``--store`` names
the episodic source + events root, ``--vault-root`` names the ``MEMORY.md`` +
``dreams/`` root, ``--lock-path`` names the run-lock file, and ``--index-db``
is accepted, reserved (RESOLUTION 5), and additionally locates the captures
watermark directory (RESOLUTION 3).

The source kind is pluggable (``briefs/consolidator-extraction-source-
pluggability.md``): ``sessions`` (the default) reads Wolf's day-partitioned
session transcripts; ``captures`` reads a flat directory of pre-distilled
Markdown captures named by ``--captures-root``, draining the unconsolidated
backlog (``--since`` narrows it). Absent any captures flag, Wolf's defaults are
unchanged. Every error path emits one ``error: ...`` line to stderr with exit
1 — no Python traceback.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from palace.consolidate._errors import ConsolidationError, EmptySourceError
from palace.consolidate.config import DEFAULT_SOURCE_KIND, SourceKind, consolidator_lock_path
from palace.consolidate.llm import OllamaConsolidatorLLM
from palace.consolidate.pipeline import ConsolidationResult, run_consolidation
from palace.consolidate.sources import SourceSelection
from palace.daemons.capture.config import BOISE_TZ, DEFAULT_STORE
from palace.index.config import EMBEDDER_MODEL, OLLAMA_BASE_URL
from palace.index.embedder import OllamaEmbedder
from palace.vault import memory_md_path, resolve_vault_root

__all__ = ["build_subparser", "dispatch_consolidate"]


def build_subparser(subparsers: Any) -> None:
    """Wire the ``consolidate`` subparser into ``palace`` argparse."""
    parser = subparsers.add_parser(
        "consolidate",
        help="Run the on-demand dreaming pass over a day's captures.",
        description=(
            "Read a day's captured session transcripts (the sole fact source; "
            "fs_change events are a re-index trigger, not fact input), extract "
            "candidate facts with a local LLM, score them on six signals, apply "
            "the three promotion gates, promote winners into "
            "<vault-root>/facts/MEMORY.md (via palace fact's writer), write a "
            "dreams/DREAMS.md audit, and surface contradictions to "
            "dreams/contradictions/. The four location flags are independent "
            "(storage-layout rule 9)."
        ),
    )
    parser.add_argument(
        "--date",
        dest="date",
        default=None,
        help="Day to consolidate, YYYY-MM-DD (default: today, America/Boise).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but write nothing and take no lock.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the run result as a JSON object on stdout.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print a per-candidate breakdown to stderr.",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Episodic-source + events root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="MEMORY.md + dreams/ root (default: $PALACE_VAULT_ROOT or ~/Obsidian/Palace/).",
    )
    parser.add_argument(
        "--index-db",
        type=Path,
        default=None,
        help="Derived-index location (reserved; not load-bearing in this phase).",
    )
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=None,
        help="Run-lock file path (default: <store>/meta/consolidator.lock).",
    )
    parser.add_argument(
        "--source-kind",
        dest="source_kind",
        choices=("sessions", "captures"),
        default=None,
        help=(
            "Episodic source kind (default: sessions, Wolf's path). 'captures' "
            "reads a flat directory of Markdown captures named by --captures-root."
        ),
    )
    parser.add_argument(
        "--captures-root",
        dest="captures_root",
        type=Path,
        default=None,
        help=(
            "Directory of Markdown capture files for the captures source kind "
            "(implies --source-kind captures when --source-kind is omitted)."
        ),
    )
    parser.add_argument(
        "--since",
        dest="since",
        default=None,
        help=(
            "Captures source only: drain only captures with event_time on or "
            "after this YYYY-MM-DD (narrows the whole-inbox default)."
        ),
    )


def dispatch_consolidate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch ``palace consolidate``, translating errors to ``error:`` lines.

    An :class:`EmptySourceError` is loud — it exits non-zero and, in ``--json``
    mode, emits an ``{"empty_source": true, ...}`` object on stdout (so a
    machine consumer sees the flagged status AND a non-zero exit) before the
    ``error:`` stderr line.
    """
    _ = parser
    try:
        return _run(args)
    except EmptySourceError as exc:
        if bool(getattr(args, "json", False)):
            print(json.dumps({"empty_source": True, "error": str(exc)}, sort_keys=True))
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    except ConsolidationError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


def _run(args: Any) -> int:
    store = _resolve_store(args)
    vault_root = resolve_vault_root(args)
    day = _resolve_day(args)
    kind = _resolve_source_kind(args)
    selection = _build_selection(args, kind=kind, store=store, day=day)
    lock_path = (
        Path(args.lock_path).expanduser()
        if getattr(args, "lock_path", None) is not None
        else consolidator_lock_path(store)
    )
    index_db = (
        Path(args.index_db).expanduser() if getattr(args, "index_db", None) is not None else None
    )
    dry_run = bool(getattr(args, "dry_run", False))

    llm = OllamaConsolidatorLLM()
    embedder = OllamaEmbedder(base_url=OLLAMA_BASE_URL, model=EMBEDDER_MODEL)
    try:
        _probe(llm, embedder)
        result = run_consolidation(
            selection=selection,
            vault_root=vault_root,
            lock_path=lock_path,
            index_db=index_db,
            day=day,
            dry_run=dry_run,
            llm=llm,
            embedder=embedder,
            clock=lambda: day,
        )
    finally:
        llm.close()
        embedder.close()

    _emit(args, result, vault_root, dry_run)
    return 0


def _resolve_source_kind(args: Any) -> SourceKind:
    """Resolve the source kind: explicit ``--source-kind`` wins; else inferred.

    Explicit ``--source-kind`` wins. Otherwise ``captures`` is inferred when
    ``--captures-root`` is supplied; absent both, the default is ``sessions``.
    """
    explicit = getattr(args, "source_kind", None)
    if explicit is not None:
        return explicit  # type: ignore[no-any-return]
    if getattr(args, "captures_root", None) is not None:
        return "captures"
    return DEFAULT_SOURCE_KIND


def _build_selection(args: Any, *, kind: SourceKind, store: Path, day: date) -> SourceSelection:
    """Validate the per-kind flag combination and build the source selection."""
    captures_root = getattr(args, "captures_root", None)
    since_raw = getattr(args, "since", None)
    date_given = getattr(args, "date", None) is not None

    if kind == "captures":
        if captures_root is None:
            raise ConsolidationError("--source-kind captures requires --captures-root <dir>")
        if date_given:
            raise ConsolidationError(
                "--date is meaningless for the captures source kind "
                "(captures drain a backlog, not a calendar day); use --since to narrow"
            )
        since = _parse_since(since_raw)
        return SourceSelection(
            kind="captures",
            store=store,
            day=day,
            captures_root=Path(captures_root).expanduser(),
            since=since,
        )

    # sessions kind: the captures-only flags are errors here.
    if captures_root is not None:
        raise ConsolidationError("--captures-root is only valid with --source-kind captures")
    if since_raw is not None:
        raise ConsolidationError("--since is only valid with --source-kind captures")
    return SourceSelection(kind="sessions", store=store, day=day)


def _parse_since(raw: Any) -> date | None:
    """Parse the optional ``--since YYYY-MM-DD`` filter date."""
    if raw is None:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ConsolidationError(f"invalid --since '{raw}': expected YYYY-MM-DD") from exc


def _probe(llm: OllamaConsolidatorLLM, embedder: OllamaEmbedder) -> None:
    """Probe both local models up front; translate IndexError to ConsolidationError."""
    from palace.index._errors import IndexError as _IndexError

    llm.probe()
    try:
        embedder.probe()
    except _IndexError as exc:
        raise ConsolidationError(str(exc)) from exc


def _emit(args: Any, result: ConsolidationResult, vault_root: Path, dry_run: bool) -> None:
    """Print the run outcome on stdout (JSON or human), plus optional verbose."""
    totals = result.totals
    if getattr(args, "verbose", False):
        for breakdown in result.breakdowns:
            verdict = (
                "HELD"
                if any(
                    c.candidate.claim == breakdown.candidate.claim for c in result.contradictions
                )
                else breakdown.verdict
            )
            print(
                f"[{verdict}] {breakdown.candidate.summary} "
                f"(total={breakdown.weighted_total:.3f}, "
                f"failed={','.join(breakdown.failed_gates) or '-'})",
                file=sys.stderr,
            )
    if getattr(args, "json", False):
        payload = {
            "date": result.day.isoformat(),
            "source_kind": result.source_kind,
            "dry_run": dry_run,
            "candidates_total": totals["candidates_total"],
            "promoted": totals["promoted"],
            "discarded": totals["discarded"],
            "held_contradiction": totals["held_contradiction"],
            "promoted_fact_ids": result.promoted_fact_ids,
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        mode = " (dry-run)" if dry_run else ""
        kind = "" if result.source_kind == "sessions" else f" [{result.source_kind}]"
        print(
            f"consolidated {result.day.isoformat()}{kind}{mode}: "
            f"{totals['candidates_total']} candidates, "
            f"{totals['promoted']} promoted, "
            f"{totals['discarded']} discarded, "
            f"{totals['held_contradiction']} held"
        )
        if not dry_run and totals["promoted"]:
            print(f"  facts written to {memory_md_path(vault_root)}")


def _resolve_store(args: Any) -> Path:
    """Resolve the machine-state root: ``--store`` → ``$PALACE_STORE`` → default."""
    flag = getattr(args, "store", None)
    if flag is not None:
        return Path(flag).expanduser()
    env = os.environ.get("PALACE_STORE")
    if env:
        return Path(env).expanduser()
    return DEFAULT_STORE


def _resolve_day(args: Any) -> date:
    """Resolve the target day: ``--date YYYY-MM-DD`` or today (America/Boise)."""
    raw = getattr(args, "date", None)
    if raw is None:
        return datetime.now(BOISE_TZ).date()
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ConsolidationError(f"invalid --date '{raw}': expected YYYY-MM-DD") from exc
