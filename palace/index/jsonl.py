"""JSONL tail-only chunker.

palace's machine state (``events/``, ``sessions/``, ``raw/<source>/``)
is append-only by policy; this chunker enforces the append-only
invariant at parse time. :func:`parse_jsonl_tail` consumes a chunk of
bytes that the writer has handed in *from the prior cursor offset*; it
never re-reads earlier bytes of a file. One JSONL record produces one
:class:`JsonlChunk`; records that exceed
:data:`palace.index.config.MARKDOWN_MAX_SECTION_CHARS` fall back to
opaque line-window splitting via :func:`palace.index.text.chunk_text_body`
(palace ships exactly one line-window engine, see
:mod:`palace.index.text`).

The parser is opaque: it does **not** introspect the JSON shape beyond
extracting a stable :class:`JsonlChunk.record_id` source — preferring
the record's ``id`` field when it is present and matches the canonical
64-char lowercase hex shape, otherwise leaving ``record_id`` as
``None``. Writer-side chunk-id derivation uses
``compute_record_id({"jsonl_record_id": record_id})`` when present so a
retraction event by id can find its chunk; otherwise it falls back to
the standard :func:`palace.index.schema.compute_chunk_id` SHA-256 over
``{path, section_index, window_index, body_hash}``.

Truncated final lines (no trailing ``\\n``) are **not** consumed. The
returned offset points at the start of the truncated line so the next
tail pass picks it up once the producer finishes the write — same
posture :class:`palace.index.tail.EventsTail` already takes against
the events log. Bad-JSON lines log ``palace index: bad-jsonl-line ...``
and are skipped (offset still advances past the bad line so palace
does not loop on it).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from palace.index.config import MARKDOWN_MAX_SECTION_CHARS
from palace.index.text import chunk_text_body

__all__ = ["JsonlChunk", "parse_jsonl_tail"]


# Canonical SHA-256 hex shape — 64 lowercase hex digits.
_HEX_ID_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class JsonlChunk:
    """One atomic chunk of a JSONL record.

    ``record_index`` is the 0-based index of the source record within
    the parsed tail (counted from the writer's resume offset, not from
    byte 0 of the file — record ``0`` after resume is the first record
    the writer has not yet seen). ``window_index`` is ``0`` for records
    that fit under :data:`palace.index.config.MARKDOWN_MAX_SECTION_CHARS`
    and ``>= 0`` for line-window-split records.

    ``section_index`` mirrors ``record_index`` so the writer's shared
    ``_diff_sections`` engine (Protocol-typed over ``section_index``,
    ``window_index``, ``body_hash``, ``body``) reads the same field names
    across all four typed pipelines. ``body_hash`` is the SHA-256 hex of
    ``body.encode("utf-8")`` — the same idiom
    :class:`palace.index.markdown.Section` uses.

    ``record_id`` is the JSON record's ``id`` field iff present and
    parses as a 64-char lowercase hex string (palace's canonical
    SHA-256 shape); otherwise ``None`` and the writer falls back to the
    body-hash-keyed chunk-id derivation.
    """

    record_index: int
    window_index: int
    body: str
    body_hash: str
    record_id: str | None
    # Mirror of ``record_index`` for ``_DiffableChunk`` Protocol parity
    # with :class:`palace.index.markdown.Section`. Populated at
    # construction; never out-of-sync with ``record_index``.
    section_index: int


def parse_jsonl_tail(
    *,
    file_bytes: bytes,
    resume_offset: int,
    log: Callable[[str], None] | None = None,
) -> tuple[tuple[JsonlChunk, ...], int]:
    """Parse the tail of a JSONL file at ``file_bytes[resume_offset:]``.

    Returns ``(chunks, new_absolute_offset)``. The parser splits on
    ``b"\\n"``, parses each non-empty line via :func:`json.loads`, and
    builds one :class:`JsonlChunk` per line. A truncated final line (no
    trailing ``\\n``) is **not** consumed — the offset advances only to
    the start of the truncated line so the next tail pass picks it up
    once the producer finishes the write. A line that fails
    :func:`json.loads` logs ``palace index: bad-jsonl-line ...`` via the
    injected logger (default stderr) and is skipped; the offset still
    advances past the bad line so palace does not loop on it.

    Records whose body exceeds
    :data:`palace.index.config.MARKDOWN_MAX_SECTION_CHARS` (4000 chars
    by default) are window-split via
    :func:`palace.index.text.chunk_text_body`; every emitted chunk
    shares the source record's ``record_index`` and ``record_id`` and
    walks ``window_index`` from ``0``.
    """
    log_fn = log if log is not None else _default_log

    tail = file_bytes[resume_offset:]
    chunks: list[JsonlChunk] = []
    consumed_bytes = 0
    record_index = 0
    cursor = 0

    while cursor < len(tail):
        newline_index = tail.find(b"\n", cursor)
        if newline_index < 0:
            # Truncated final line: do NOT consume; the offset stops
            # at ``cursor`` so the next pass re-reads from here once
            # the producer terminates the line.
            break
        line_bytes = tail[cursor:newline_index]
        cursor = newline_index + 1
        consumed_bytes = cursor

        if not line_bytes.strip():
            # Blank line — no record, no chunk. Offset advances past it.
            continue

        try:
            line_text = line_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            log_fn(f"palace index: bad-jsonl-line offset={resume_offset + cursor} reason={exc!r}")
            continue

        try:
            parsed = json.loads(line_text)
        except json.JSONDecodeError as exc:
            log_fn(f"palace index: bad-jsonl-line offset={resume_offset + cursor} reason={exc!r}")
            continue

        record_id = _extract_record_id(parsed) if isinstance(parsed, dict) else None
        body = line_text
        if len(body) <= MARKDOWN_MAX_SECTION_CHARS:
            chunks.append(
                JsonlChunk(
                    record_index=record_index,
                    window_index=0,
                    body=body,
                    body_hash=_hash_body(body),
                    record_id=record_id,
                    section_index=record_index,
                )
            )
        else:
            for sub in chunk_text_body(body):
                chunks.append(
                    JsonlChunk(
                        record_index=record_index,
                        window_index=sub.window_index,
                        body=sub.body,
                        body_hash=sub.body_hash,
                        record_id=record_id,
                        section_index=record_index,
                    )
                )
        record_index += 1

    return tuple(chunks), resume_offset + consumed_bytes


def _extract_record_id(parsed: dict[str, Any]) -> str | None:
    """Return ``parsed["id"]`` iff present and shaped as 64-char hex.

    Anything else (missing, non-string, wrong length, non-hex) yields
    ``None`` so the writer falls back to its body-hash-keyed chunk id.
    """
    value = parsed.get("id")
    if not isinstance(value, str):
        return None
    if _HEX_ID_RE.match(value) is None:
        return None
    return value


def _hash_body(body: str) -> str:
    """SHA-256 hex of ``body``'s UTF-8 bytes."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _default_log(line: str) -> None:
    """Default log sink for the chunker: stderr, line-flushed.

    The writer always passes ``self._log`` explicitly; tests pass a
    list-appender. This helper exists solely so direct callers of
    :func:`parse_jsonl_tail` (operators in a REPL, future scripts) do
    not need to know the writer's private log shape. Cross-module
    private-symbol imports would create a cycle-risk surface — the
    chunker stays pure.
    """
    print(line, file=sys.stderr, flush=True)
