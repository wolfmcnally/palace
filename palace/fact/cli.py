"""``palace fact`` CLI subcommand group.

Four sub-subcommands implementing ``policies/fact-schema.md``'s lifecycle:

- ``palace fact add`` — build a fresh fact section and append it to
  ``<vault-root>/facts/MEMORY.md``. Writes no event (a fresh promotion is
  not a lifecycle mutation), so it takes ``--vault-root`` but not
  ``--store``.
- ``palace fact invalidate <id>`` — flip ``valid: false`` on the named
  section in place and append a ``fact-invalidate`` event to
  ``<store>/events/<YYYY-MM-DD>.jsonl``. Takes both ``--vault-root`` and
  ``--store`` (it writes to both roots).
- ``palace fact supersede <old-id>`` — write a fresh section (fresh id),
  patch the old section's ``superseded_by`` + ``valid: false``, and append a
  ``fact-supersede`` event. The replacement is given via ``--with
  <json-file|->`` or the same inline add-style flags as ``add``; ``--json``
  emits the new replacement fact as a JSON object.
- ``palace fact query`` — bitemporal / tag / subject filters over
  ``MEMORY.md`` (read-only; ``--vault-root`` only).

Root resolution honors ``policies/storage-layout.md`` rule 9: ``--store``
and ``--vault-root`` are independent (per
``briefs/store-parametric-external-consumers.md``). Store resolution mirrors
:func:`palace.index.cli._resolve_store`; vault resolution reuses
:func:`palace.vault.resolve_vault_root`. Every error path emits one
``error: ...`` line to stderr with exit 1 — no Python traceback.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from palace.daemons.capture.config import BOISE_TZ, DEFAULT_STORE
from palace.fact._errors import FactError
from palace.fact.events import (
    EVENT_TYPE_FACT_INVALIDATE,
    EVENT_TYPE_FACT_SUPERSEDE,
    append_fact_event,
    build_fact_event,
)
from palace.fact.schema import EventTime, Fact, build_fact
from palace.fact.store import (
    add_fact,
    invalidate_fact,
    read_memory,
    supersede_fact,
    write_memory,
)
from palace.vault import memory_md_path, resolve_vault_root

__all__ = ["build_subparser", "dispatch_fact"]


def _resolve_store(args: Any) -> Path:
    """Return the resolved machine-state root per the precedence rule.

    Mirrors :func:`palace.index.cli._resolve_store`: ``--store`` flag, then
    ``$PALACE_STORE``, then :data:`DEFAULT_STORE`.
    """
    flag = getattr(args, "store", None)
    if flag is not None:
        return Path(flag).expanduser()
    env = os.environ.get("PALACE_STORE")
    if env:
        return Path(env).expanduser()
    return DEFAULT_STORE


def _now_ingest() -> str:
    """Return the current America/Boise ISO-8601 timestamp, seconds precision.

    Matches the capture / reindex daemons' ``ingest_time`` stamping idiom.
    """
    return datetime.now(BOISE_TZ).isoformat(timespec="seconds")


def _add_inline_arguments(parser: argparse.ArgumentParser) -> None:
    """Wire the shared add-style flags onto ``add`` and ``supersede``."""
    parser.add_argument("--summary", help="Short human-readable section heading.")
    parser.add_argument("--claim", help="The fact itself, as one prose paragraph.")
    parser.add_argument(
        "--event-time",
        default=None,
        help="Scalar ISO-8601 event time (mutually exclusive with --event-start).",
    )
    parser.add_argument(
        "--event-start",
        default=None,
        help="Interval start (ISO-8601); pairs with --event-end.",
    )
    parser.add_argument(
        "--event-end",
        default=None,
        help="Interval end (ISO-8601); omit or 'null' for open-ended.",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=1.0,
        help="Confidence in [0, 1] (default: 1.0).",
    )
    parser.add_argument(
        "--provenance",
        action="append",
        default=None,
        metavar="EVENT_ID",
        help="Source-event id (SHA-256 hex). Repeatable; at least one required.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=None,
        metavar="TAG",
        help="Category tag (repeatable).",
    )
    parser.add_argument(
        "--ref",
        action="append",
        default=None,
        metavar="REF",
        help="Related-note reference, e.g. '[[sample-project/architecture]]' (repeatable).",
    )


def build_subparser(subparsers: Any) -> None:
    """Wire the ``fact`` subparser group into ``palace`` argparse."""
    fact = subparsers.add_parser(
        "fact",
        help="Bitemporal fact controls.",
        description=(
            "Add, invalidate, supersede, and query bitemporal facts in "
            "<vault-root>/facts/MEMORY.md per policies/fact-schema.md. "
            "Lifecycle mutations append fact-invalidate / fact-supersede "
            "events to <store>/events/<YYYY-MM-DD>.jsonl."
        ),
    )
    sub = fact.add_subparsers(dest="fact_command")

    add_p = sub.add_parser("add", help="Add a new fact section to MEMORY.md.")
    _add_inline_arguments(add_p)
    add_p.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Human-readable root (default: $PALACE_VAULT_ROOT or ~/Obsidian/Palace/).",
    )
    add_p.add_argument(
        "--json",
        action="store_true",
        help="Emit the created fact as a JSON object on stdout.",
    )

    inval_p = sub.add_parser(
        "invalidate",
        help="Flip valid:false on a fact and append a fact-invalidate event.",
    )
    inval_p.add_argument("fact_id", help="Fact id (or unambiguous prefix) to invalidate.")
    inval_p.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Human-readable root (default: $PALACE_VAULT_ROOT or ~/Obsidian/Palace/).",
    )
    inval_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )

    super_p = sub.add_parser(
        "supersede",
        help="Replace a fact with a fresh one and append a fact-supersede event.",
    )
    super_p.add_argument("old_id", help="Fact id (or unambiguous prefix) to supersede.")
    super_p.add_argument(
        "--with",
        dest="with_spec",
        default=None,
        metavar="JSON",
        help="Path to a JSON file (or '-' for stdin) describing the replacement fact.",
    )
    _add_inline_arguments(super_p)
    super_p.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Human-readable root (default: $PALACE_VAULT_ROOT or ~/Obsidian/Palace/).",
    )
    super_p.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Machine-state root (default: $PALACE_STORE or ~/palace-data.noindex/).",
    )
    super_p.add_argument(
        "--json",
        action="store_true",
        help="Emit the new replacement fact as a JSON object on stdout.",
    )

    query_p = sub.add_parser("query", help="Filter facts by subject / tag / as-of date.")
    query_p.add_argument(
        "--subject",
        default=None,
        help="Match facts whose id equals/prefixes this, or whose claim contains it.",
    )
    query_p.add_argument("--tag", default=None, help="Match facts carrying this tag.")
    query_p.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Bitemporal filter: facts whose event_time was true on this date.",
    )
    query_p.add_argument(
        "--all",
        dest="include_invalid",
        action="store_true",
        help="Include invalidated facts (default: valid-only).",
    )
    query_p.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Human-readable root (default: $PALACE_VAULT_ROOT or ~/Obsidian/Palace/).",
    )
    query_p.add_argument(
        "--json",
        action="store_true",
        help="Emit matching facts as a JSON array on stdout.",
    )


def dispatch_fact(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch ``palace fact <subcommand>``, translating FactError to ``error:``."""
    command = str(getattr(args, "fact_command", None))
    handlers = {
        "add": _cli_add,
        "invalidate": _cli_invalidate,
        "supersede": _cli_supersede,
        "query": _cli_query,
    }
    handler = handlers.get(command)
    if handler is None:
        parser.parse_args(["fact", "--help"])
        return 2
    try:
        return handler(args)
    except FactError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1


# --------------------------------------------------------------------- handlers


def _cli_add(args: Any) -> int:
    """``palace fact add`` — build + append a fresh fact section."""
    fact = _build_fact_from_args(args)
    vault_root = resolve_vault_root(args)
    path = memory_md_path(vault_root)
    doc = read_memory(path)
    add_fact(doc, fact)
    write_memory(path, doc)
    if getattr(args, "json", False):
        print(json.dumps(_fact_to_dict(fact), sort_keys=True))
    else:
        print(f"added: {fact.id}")
    return 0


def _cli_invalidate(args: Any) -> int:
    """``palace fact invalidate <id>`` — patch valid:false + record event."""
    vault_root = resolve_vault_root(args)
    store = _resolve_store(args)
    path = memory_md_path(vault_root)
    doc = read_memory(path)
    updated = invalidate_fact(doc, args.fact_id)
    write_memory(path, doc)

    event = build_fact_event(
        event_type=EVENT_TYPE_FACT_INVALIDATE,
        ingest_time=_now_ingest(),
        fact_id=updated.id,
    )
    append_fact_event(store, event)
    print(f"invalidated: {updated.id}")
    return 0


def _cli_supersede(args: Any) -> int:
    """``palace fact supersede <old-id>`` — fresh section + patch old + event."""
    new_fact = _build_replacement_fact(args)
    vault_root = resolve_vault_root(args)
    store = _resolve_store(args)
    path = memory_md_path(vault_root)
    doc = read_memory(path)
    updated_old, new = supersede_fact(doc, args.old_id, new_fact)
    write_memory(path, doc)

    event = build_fact_event(
        event_type=EVENT_TYPE_FACT_SUPERSEDE,
        ingest_time=_now_ingest(),
        old_fact_id=updated_old.id,
        new_fact_id=new.id,
    )
    append_fact_event(store, event)
    if getattr(args, "json", False):
        print(json.dumps(_fact_to_dict(new), sort_keys=True))
    else:
        print(f"superseded: {updated_old.id} -> {new.id}")
    return 0


def _cli_query(args: Any) -> int:
    """``palace fact query`` — subject / tag / as-of filters, read-only."""
    vault_root = resolve_vault_root(args)
    path = memory_md_path(vault_root)
    doc = read_memory(path)

    results: list[Fact] = []
    for fact in doc.sections:
        if not args.include_invalid and not fact.valid:
            continue
        if args.subject is not None and not _matches_subject(fact, args.subject):
            continue
        if args.tag is not None and args.tag not in fact.tags:
            continue
        if args.as_of is not None and not _valid_as_of(fact, args.as_of):
            continue
        results.append(fact)

    if getattr(args, "json", False):
        print(json.dumps([_fact_to_dict(f) for f in results], sort_keys=True))
    else:
        for fact in results:
            print(f"{fact.id}  {fact.summary}")
    return 0


# --------------------------------------------------------------------- helpers


def _build_fact_from_args(args: Any) -> Fact:
    """Construct a :class:`Fact` from add-style flags, validating required ones."""
    summary = getattr(args, "summary", None)
    claim = getattr(args, "claim", None)
    if not summary:
        raise FactError("--summary is required")
    if not claim:
        raise FactError("--claim is required")
    provenance = getattr(args, "provenance", None) or []
    if not provenance:
        raise FactError("at least one --provenance is required")

    event_time = _resolve_event_time(args)
    return build_fact(
        summary=summary,
        claim=claim,
        event_time=event_time,
        ingest_time=_now_ingest(),
        confidence=getattr(args, "confidence", 1.0),
        provenance=provenance,
        tags=getattr(args, "tag", None) or [],
        refs=getattr(args, "ref", None) or [],
    )


def _build_replacement_fact(args: Any) -> Fact:
    """Build the supersede replacement from ``--with`` JSON or inline flags."""
    with_spec = getattr(args, "with_spec", None)
    if with_spec is not None:
        return _fact_from_json_spec(with_spec)
    return _build_fact_from_args(args)


def _fact_from_json_spec(spec: str) -> Fact:
    """Load a replacement fact spec from a JSON file path (or '-' for stdin)."""
    try:
        raw = sys.stdin.read() if spec == "-" else Path(spec).read_text(encoding="utf-8")
    except OSError as exc:
        raise FactError(f"cannot read --with spec: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FactError(f"--with spec is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FactError("--with spec must be a JSON object")

    summary = data.get("summary")
    claim = data.get("claim")
    if not summary:
        raise FactError("--with spec missing 'summary'")
    if not claim:
        raise FactError("--with spec missing 'claim'")
    provenance = data.get("provenance") or []
    if not provenance:
        raise FactError("--with spec missing 'provenance'")

    event_time = data.get("event_time")
    if event_time is None:
        raise FactError("--with spec missing 'event_time'")

    return build_fact(
        summary=str(summary),
        claim=str(claim),
        event_time=event_time,
        ingest_time=_now_ingest(),
        confidence=float(data.get("confidence", 1.0)),
        provenance=[str(p) for p in provenance],
        tags=[str(t) for t in data.get("tags", [])],
        refs=[str(r) for r in data.get("refs", [])],
    )


def _resolve_event_time(args: Any) -> EventTime:
    """Resolve the ``event_time`` shape from the mutually exclusive flags."""
    event_time = getattr(args, "event_time", None)
    event_start = getattr(args, "event_start", None)
    event_end = getattr(args, "event_end", None)

    if event_time is not None:
        if event_start is not None or event_end is not None:
            raise FactError("--event-time is mutually exclusive with --event-start/--event-end")
        return str(event_time)
    if event_start is not None:
        end: str | None = None if event_end in (None, "null") else str(event_end)
        return {"start": str(event_start), "end": end}
    raise FactError("one of --event-time or --event-start is required")


def _matches_subject(fact: Fact, subject: str) -> bool:
    """True when ``subject`` is the fact's id/prefix or appears in its claim."""
    if fact.id == subject or fact.id.startswith(subject):
        return True
    return subject.lower() in fact.claim.lower()


def _valid_as_of(fact: Fact, as_of: str) -> bool:
    """True when the fact's ``event_time`` was true on the ``as_of`` date.

    Scalar event_time matches when its date is on-or-before ``as_of``.
    Interval ``{start, end}`` matches when ``start <= as_of`` and either
    ``end`` is open (null) or ``as_of <= end``. Comparison is on the date
    prefix (``YYYY-MM-DD``), which sorts lexicographically for ISO-8601.
    """
    event_time = fact.event_time
    if isinstance(event_time, dict):
        start = _date_prefix(event_time.get("start"))
        end = _date_prefix(event_time.get("end"))
        if start is not None and start > as_of:
            return False
        return not (end is not None and as_of > end)
    scalar = _date_prefix(event_time)
    if scalar is None:
        return False
    return scalar <= as_of


def _date_prefix(value: Any) -> str | None:
    """Return the ``YYYY-MM-DD`` prefix of an ISO-8601 value, or ``None``."""
    if value is None:
        return None
    text = str(value)
    return text[:10]


def _fact_to_dict(fact: Fact) -> dict[str, Any]:
    """Project a :class:`Fact` to a JSON-serializable dict (claim in the body)."""
    out: dict[str, Any] = {
        "id": fact.id,
        "summary": fact.summary,
        "claim": fact.claim,
        "event_time": fact.event_time,
        "ingest_time": fact.ingest_time,
        "confidence": fact.confidence,
        "valid": fact.valid,
        "provenance": fact.provenance,
        "tags": fact.tags,
        "refs": fact.refs,
    }
    if fact.superseded_by is not None:
        out["superseded_by"] = fact.superseded_by
    return out
