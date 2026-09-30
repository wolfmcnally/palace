"""Reading episodic source units from the machine-state store.

The consolidator's default and only Wolf-store source is the day's captured
**session transcripts** (``<store>/sessions/<YYYY-MM-DD>/<id>.jsonl``). This
module reads those records into an ordered list of :class:`SourceUnit` records
the extractor consumes, carrying the source-event id (for exact provenance +
corroboration counting), the unit text, its bitemporal times, and the generic
``authored`` trust flag.

A second, non-default source kind exists for external consumers whose episodic
ground truth is a flat directory of pre-distilled Markdown captures rather than
day-partitioned session transcripts: :func:`read_sources` dispatches on a
:class:`SourceSelection` to either :func:`read_day_sources` (the sessions kind)
or :func:`palace.consolidate.captures.read_captures_dir` (the captures kind).
Only the reader differs; scoring, gating, contradiction, promotion, audit, and
lock stages all operate on the same :class:`SourceUnit` abstraction. See
``briefs/consolidator-extraction-source-pluggability.md``.

Filesystem-change events (``<store>/events/<YYYY-MM-DD>.jsonl``'s ``fs_change``
records) are deliberately **not** a fact-extraction source. They are a
re-index trigger handled by the index daemon, not durable-fact input. This
matches the 2026 BCP consensus — OpenClaw re-indexes on file change but
extracts facts from sessions; Zep/Graphiti, Mem0, and Microsoft Foundry
extract from the interaction stream only — and palace's own A/B finding that
``fs_change`` records produce only near-duplicate noise that fails every gate.
See ``policies/consolidation.md`` §"Extraction sources".

A capture record stores session *metadata* plus a ``transcript_path`` pointer
— the conversation itself lives in the referenced Claude Code transcript, not
the record's inline fields. The session reader resolves that pointer to
turn-by-turn dialogue text (``_resolve_transcript_text``, capped by
``TRANSCRIPT_CHAR_BUDGET`` with head+tail windowing) and falls back to the
inline fields (``_session_text``) when the transcript is absent or carries no
dialogue. Resolution never raises: the transcript is upstream data palace does
not own, so a missing file or a malformed line degrades gracefully.

The Wolf-authored heuristic (RESOLUTION 1) is deliberately conservative and
tunable: a unit counts as Wolf-authored when its harness is one of the
interactive coding harnesses (``claude-code`` / ``codex``) AND its
``event_type`` is ``"stop"`` — i.e. a top-level turn Wolf drove, not a
subagent turn or a filesystem event. ``policies/consolidation.md`` documents
the heuristic and that it is a knob, not a law.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from palace.consolidate._errors import ConsolidationError
from palace.consolidate.config import TRANSCRIPT_CHAR_BUDGET, SourceKind, sessions_dir
from palace.daemons.capture.schema import ALLOWED_HARNESSES, EVENT_STOP

__all__ = [
    "SourceSelection",
    "SourceUnit",
    "is_wolf_authored",
    "read_day_sources",
    "read_sources",
]


@dataclass(frozen=True)
class SourceUnit:
    """One episodic source unit — a captured session record or a capture file.

    ``authored`` is the generic source-trust flag the corroboration gate reads:
    a single authored unit clears ``corroboration ≥ 2``. Its value is supplied
    per source kind — the sessions reader keys it on the Wolf-authored heuristic
    (:func:`is_wolf_authored`); the captures reader keys it on the capture's
    ``source:`` prefix (:func:`palace.consolidate.captures.source_is_user_authored`).
    """

    source_id: str
    text: str
    event_time: str
    ingest_time: str
    harness: str | None
    authored: bool


@dataclass(frozen=True)
class SourceSelection:
    """Which episodic source to read, and the parameters its reader needs.

    ``kind`` selects the reader; ``store`` + ``day`` drive the sessions reader;
    ``captures_root`` + optional ``since`` drive the captures reader. The CLI
    validates that the right fields are present for the chosen kind before
    constructing this (e.g. captures requires ``captures_root``).
    """

    kind: SourceKind
    store: Path
    day: date
    captures_root: Path | None = None
    since: date | None = None


def is_wolf_authored(record: dict[str, Any]) -> bool:
    """Return True when a session record counts as Wolf-authored (RESOLUTION 1).

    Wolf-authored ≡ harness in the interactive coding set AND a top-level
    ``stop`` turn. This is the **sessions-kind** source-trust signal; it
    populates the generic :attr:`SourceUnit.authored` field for that kind.
    Tunable; see ``policies/consolidation.md``.
    """
    harness = record.get("harness")
    event_type = record.get("event_type")
    return harness in ALLOWED_HARNESSES and event_type == EVENT_STOP


def read_sources(selection: SourceSelection) -> list[SourceUnit]:
    """Read source units for ``selection``, dispatching on its source kind.

    ``sessions`` (the default) reads the day's captured session transcripts
    via :func:`read_day_sources`. ``captures`` reads a flat directory of
    pre-distilled Markdown captures via
    :func:`palace.consolidate.captures.read_captures_dir`. Only the reader
    differs — both return the same :class:`SourceUnit` list.
    """
    if selection.kind == "sessions":
        return read_day_sources(selection.store, selection.day)
    if selection.kind == "captures":
        # Imported lazily to keep the captures reader's deps out of the
        # sessions-only path and avoid an import cycle through config.
        from palace.consolidate.captures import read_captures_dir

        if selection.captures_root is None:
            raise ConsolidationError("captures source selected without a captures root")
        return read_captures_dir(selection.captures_root, since=selection.since)
    raise ConsolidationError(f"unknown source kind: {selection.kind!r}")


def read_day_sources(store: Path, day: date) -> list[SourceUnit]:
    """Read the day's captured session records into source units.

    Globs ``<store>/sessions/<day>/*.jsonl`` (one record per file, the capture
    daemon's shape). Session transcripts are the **sole** fact-extraction
    source; the day's events stream (``fs_change`` records) is a re-index
    trigger, not fact input, so it is not read here. Returns units sorted by
    ``source_id`` for determinism.
    """
    units = _read_session_units(store, day)
    units.sort(key=lambda u: u.source_id)
    return units


# ---------------------------------------------------------------- internals


def _read_session_units(store: Path, day: date) -> list[SourceUnit]:
    """Read each ``<store>/sessions/<day>/<id>.jsonl`` capture record."""
    day_dir = sessions_dir(store, day)
    if not day_dir.is_dir():
        return []
    units: list[SourceUnit] = []
    for path in sorted(day_dir.glob("*.jsonl")):
        record = _load_single_record(path)
        source_id = str(record.get("id") or "")
        if not source_id:
            raise ConsolidationError(f"session record missing 'id': {path}")
        transcript_path = record.get("transcript_path")
        text = None
        if isinstance(transcript_path, str):
            text = _resolve_transcript_text(transcript_path, budget=TRANSCRIPT_CHAR_BUDGET)
        if text is None:
            text = _session_text(record)
        units.append(
            SourceUnit(
                source_id=source_id,
                text=text,
                event_time=str(record.get("ingest_time") or ""),
                ingest_time=str(record.get("ingest_time") or ""),
                harness=record.get("harness"),
                authored=is_wolf_authored(record),
            )
        )
    return units


def _load_single_record(path: Path) -> dict[str, Any]:
    """Load the single JSON record from a one-record session JSONL file."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ConsolidationError(f"empty session record: {path}")
    # A session file holds exactly one record; take the first non-blank line.
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    try:
        record = json.loads(first)
    except json.JSONDecodeError as exc:
        raise ConsolidationError(f"malformed session record {path}: {exc}") from exc
    if not isinstance(record, dict):
        raise ConsolidationError(f"session record {path} is not an object")
    return record


def _session_text(record: dict[str, Any]) -> str:
    """Render a capture record into the text the extractor reads.

    Concatenates the assistant message, a compact list of tool-call names,
    and touched file paths — the fields ``briefs/sota-memory-and-recall.md``
    §B.4 names — so the model sees what the turn actually did.
    """
    parts: list[str] = []
    message = record.get("assistant_message")
    if isinstance(message, str) and message:
        parts.append(message)
    tool_calls = record.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        names = [str(tc.get("name", "")) for tc in tool_calls if isinstance(tc, dict)]
        names = [n for n in names if n]
        if names:
            parts.append("tools: " + ", ".join(names))
    files_touched = record.get("files_touched")
    if isinstance(files_touched, list) and files_touched:
        paths = [str(ft.get("path", "")) for ft in files_touched if isinstance(ft, dict)]
        paths = [p for p in paths if p]
        if paths:
            parts.append("files: " + ", ".join(paths))
    return "\n".join(parts)


def _resolve_transcript_text(transcript_path: str, *, budget: int) -> str | None:
    """Resolve a capture record's ``transcript_path`` to dialogue text.

    Capture records store session metadata plus a pointer to the Claude Code
    transcript (``~/.claude/projects/<proj>/<uuid>.jsonl``); the actual
    conversation lives there, not in the record's inline fields. This reads
    that transcript and renders a turn-by-turn ``User:`` / ``Assistant:``
    dialogue the extractor can read.

    Resolution is a **graceful fallback**, never a hard error: if the pointer
    is falsy, the file is absent, or it is unreadable, this returns ``None``
    and the caller falls back to the record's inline fields. The transcript is
    upstream data palace does not own, so a line that fails to parse is
    skipped rather than aborting the run.

    Returns ``None`` (not an empty string) when no user/assistant text is
    recovered, so the caller's fallback fires.
    """
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    turns: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            # Upstream data palace doesn't own: skip a bad line, never abort.
            continue
        if not isinstance(obj, dict):
            continue
        rendered = _render_transcript_turn(obj)
        if rendered:
            turns.append(rendered)

    if not turns:
        return None
    return _window_to_budget("\n\n".join(turns), budget=budget)


def _render_transcript_turn(obj: dict[str, Any]) -> str | None:
    """Render one Claude Code transcript line into a ``User:``/``Assistant:`` turn.

    ``user`` lines carry either a string message or a list of content items;
    ``assistant`` lines carry a list of content items whose prose is in
    ``{"type":"text"}`` items — ``thinking`` and ``tool_use`` items are noise
    and are skipped. Every other line type is ignored.
    """
    line_type = obj.get("type")
    if line_type not in ("user", "assistant"):
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    text = _extract_content_text(content)
    if not text:
        return None
    label = "User" if line_type == "user" else "Assistant"
    return f"{label}: {text}"


def _extract_content_text(content: Any) -> str:
    """Pull the prose out of a transcript message ``content`` value.

    A string is taken verbatim. A list yields the concatenation of its
    ``{"type":"text","text": ...}`` items, in order, skipping ``thinking`` and
    ``tool_use`` items (and any other non-text item).
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            value = item.get("text")
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        return "\n".join(parts)
    return ""


def _window_to_budget(text: str, *, budget: int) -> str:
    """Cap ``text`` to ``budget`` chars with a deterministic head+tail window.

    A whole day's transcript can run past the extractor's useful context, so
    over-budget text keeps the first 60% (early task framing) and the last 40%
    (late conclusions), joined by a ``…[truncated]…`` marker — both ends of
    the conversation survive. Under-budget text is returned unchanged.
    """
    if budget <= 0 or len(text) <= budget:
        return text
    head_len = int(budget * 0.6)
    tail_len = budget - head_len
    head = text[:head_len]
    tail = text[len(text) - tail_len :]
    return f"{head}\n…[truncated]…\n{tail}"
