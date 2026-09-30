"""Opaque-text chunker, shared line-window engine, and binary refusal.

This module is the third typed pipeline (Phase 2.4) and the home of the
single line-window-chunking implementation palace ships. The Markdown
pipeline at :mod:`palace.index.markdown` predates this module and keeps
its own private ``_split_into_windows`` (preserved byte-identical per the
phase's "no edits to ``markdown.py``" rule); the JSONL pipeline at
:mod:`palace.index.jsonl` calls :func:`chunk_text_body` directly for the
over-cap-record fall-back, so the cross-kind line-window arithmetic
lives in one place.

What the chunker does:

- **Binary refusal.** :func:`looks_binary` examines the first
  :data:`palace.index.config._TEXT_DETECTOR_SAMPLE_BYTES` bytes; if more
  than :data:`palace.index.config._BINARY_THRESHOLD` of them fall
  outside the printable-ASCII range plus ``\\t\\n\\r``, the file is
  binary — **unless** the sample decodes cleanly as UTF-8, in which case
  the file is text regardless of byte-distribution (a valid UTF-8 file
  with high-byte content is text). Defense-in-depth alongside
  :data:`palace.index.config.BINARY_EXTENSIONS`: a misclassification at
  dispatch time must not produce garbage chunks.
- **UTF-8 decode with ``errors="replace"``** so a single bad byte does
  not kill a whole file's index entry — same posture
  :func:`palace.index.markdown.parse_markdown` already takes.
- **Line-window chunking** via :func:`chunk_text_body`. One chunk under
  the section cap (4000 chars); overlapping windows of
  :data:`palace.index.config.MARKDOWN_WINDOW_CHARS` with
  :data:`palace.index.config.MARKDOWN_WINDOW_OVERLAP_CHARS` overlap
  above the cap — same arithmetic
  :func:`palace.index.markdown._split_into_windows` uses.

``TextChunk`` carries ``section_index=0`` for every emitted chunk
(opaque text has no internal section structure), with ``window_index``
enumerating the windows. The two-field-index shape mirrors
:class:`palace.index.markdown.Section` so the writer's shared
``_diff_sections`` engine reads the same field names across all four
typed pipelines.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from palace.index.config import (
    _BINARY_THRESHOLD,
    _TEXT_DETECTOR_SAMPLE_BYTES,
    MARKDOWN_MAX_SECTION_CHARS,
    MARKDOWN_WINDOW_CHARS,
    MARKDOWN_WINDOW_OVERLAP_CHARS,
)

__all__ = ["TextChunk", "chunk_text_body", "looks_binary", "parse_text"]


# The printable-ASCII range plus the three whitespace bytes palace treats
# as plainly textual. Used to populate the byte-distribution counter in
# :func:`looks_binary`.
_PRINTABLE_BYTES: frozenset[int] = frozenset({0x09, 0x0A, 0x0D} | set(range(0x20, 0x7F)))


# NUL bytes are a universal binary indicator even when the surrounding
# bytes are technically valid UTF-8 (a NUL is a 1-byte UTF-8 char).
# The heuristic short-circuits to binary the moment one appears in the
# sample.
_NUL_BYTE: int = 0x00


@dataclass(frozen=True)
class TextChunk:
    """One atomic chunk of an opaque-text file.

    ``section_index`` is always ``0`` (opaque text has no section
    structure); ``window_index`` enumerates the line-windows. ``body``
    is the decoded substring; ``body_hash`` is the SHA-256 hex of
    ``body.encode("utf-8")``.
    """

    section_index: int
    window_index: int
    body: str
    body_hash: str


def parse_text(*, file_bytes: bytes) -> tuple[TextChunk, ...]:
    """Parse ``file_bytes`` as opaque text, returning windowed chunks.

    Decodes UTF-8 with ``errors="replace"``. Refuses bytes whose first
    sample looks binary (returns ``()``). A whitespace-only file also
    returns ``()`` — no chunks land for an empty or all-whitespace file.
    """
    if looks_binary(file_bytes):
        return ()
    body = file_bytes.decode("utf-8", errors="replace")
    if not body.strip():
        return ()
    return chunk_text_body(body)


def chunk_text_body(body: str) -> tuple[TextChunk, ...]:
    """Split ``body`` into one-or-more overlapping line-windows.

    Bodies under :data:`palace.index.config.MARKDOWN_MAX_SECTION_CHARS`
    (4000 chars by default) produce a single chunk with
    ``window_index=0``. Larger bodies are split into windows of
    :data:`palace.index.config.MARKDOWN_WINDOW_CHARS` characters with
    :data:`palace.index.config.MARKDOWN_WINDOW_OVERLAP_CHARS` overlap.

    The constant names retain their ``MARKDOWN_`` prefix from 2.3 —
    renaming to drop the prefix is an Open Question tracked in the
    phase file (3 call sites now, the prefix reads as a stale label).
    """
    if not body:
        return ()
    if len(body) <= MARKDOWN_MAX_SECTION_CHARS:
        return (
            TextChunk(
                section_index=0,
                window_index=0,
                body=body,
                body_hash=_hash_body(body),
            ),
        )
    step = MARKDOWN_WINDOW_CHARS - MARKDOWN_WINDOW_OVERLAP_CHARS
    if step <= 0:
        # Defensive: a misconfigured overlap meeting-or-exceeding the
        # window size would loop forever; fall back to a single window.
        return (
            TextChunk(
                section_index=0,
                window_index=0,
                body=body,
                body_hash=_hash_body(body),
            ),
        )
    chunks: list[TextChunk] = []
    for window_index, start in enumerate(range(0, len(body), step)):
        window = body[start : start + MARKDOWN_WINDOW_CHARS]
        if not window:
            break
        chunks.append(
            TextChunk(
                section_index=0,
                window_index=window_index,
                body=window,
                body_hash=_hash_body(window),
            )
        )
        if start + MARKDOWN_WINDOW_CHARS >= len(body):
            break
    return tuple(chunks)


def looks_binary(file_bytes: bytes) -> bool:
    """Return ``True`` if the leading sample looks like binary bytes.

    The rule: count bytes outside the printable-ASCII range plus
    ``\\t\\n\\r`` in the first
    :data:`palace.index.config._TEXT_DETECTOR_SAMPLE_BYTES` bytes. If
    the non-printable ratio exceeds
    :data:`palace.index.config._BINARY_THRESHOLD`, the file is binary
    — **unless** the sample decodes as valid UTF-8, in which case it
    is text regardless of byte-distribution (a valid UTF-8 file with
    high-byte content is text, not binary).

    An empty buffer is text (returns ``False``) so an empty file is
    legible by the chunker (which then returns an empty tuple for the
    "no body to chunk" case).
    """
    if not file_bytes:
        return False
    sample = file_bytes[:_TEXT_DETECTOR_SAMPLE_BYTES]
    # NUL-byte short-circuit: universal binary indicator that overrides
    # the UTF-8 escape hatch (a NUL is a 1-byte UTF-8 char but no real
    # text file embeds them).
    if _NUL_BYTE in sample:
        return True
    # UTF-8 escape hatch: a valid UTF-8 decode short-circuits to text
    # even when the byte-distribution would otherwise look binary.
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        return False
    non_printable = sum(1 for b in sample if b not in _PRINTABLE_BYTES)
    ratio = non_printable / len(sample)
    return ratio > _BINARY_THRESHOLD


def _hash_body(body: str) -> str:
    """SHA-256 hex of ``body``'s UTF-8 bytes."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
