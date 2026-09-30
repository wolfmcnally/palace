"""Obsidian-native Markdown parser and section chunker.

Pure functions from ``(absolute_path: Path, file_bytes: bytes) ->
ParsedMarkdown``. No I/O beyond the input bytes.

What "Obsidian-native" means in 2.3:

- **YAML frontmatter**, parsed via :func:`yaml.safe_load`. Malformed
  frontmatter logs-and-continues — the body still indexes with
  ``frontmatter_json = "{}"``. Empty / absent frontmatter resolves to
  ``{}``.
- **Wikilinks** (``[[name|alias#anchor]]``) and **embeds**
  (``![[name]]``) survive verbatim in ``body``. The negative lookbehind
  on the wikilink regex excludes embeds; the inner text is captured as
  the operator wrote it (no alias stripping, no anchor normalization).
- **Hierarchical tags** (``#projects/sample-project/architecture``) are
  captured via an anchored matcher that rejects ``foo#bar``,
  ``# heading``, ``#1``. Stored without the leading ``#``.
- **Dataview inline fields** (``status:: active``) captured in document
  order, value runs to end-of-line.

Section chunking walks the body line-by-line for headings at depth
``MARKDOWN_HEADING_DEPTH`` (3 by default — H1/H2/H3). H4+ join their
parent H3. Pre-heading body lives in a section with ``heading is None``,
``heading_depth == 0``. Sections whose body exceeds
``MARKDOWN_MAX_SECTION_CHARS`` split into overlapping windows of
``MARKDOWN_WINDOW_CHARS`` with ``MARKDOWN_WINDOW_OVERLAP_CHARS`` overlap.
``body_hash`` is ``sha256(body.encode("utf-8")).hexdigest()`` per
section.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from palace.index.config import (
    MARKDOWN_HEADING_DEPTH,
    MARKDOWN_MAX_SECTION_CHARS,
    MARKDOWN_WINDOW_CHARS,
    MARKDOWN_WINDOW_OVERLAP_CHARS,
)

__all__ = ["ParsedMarkdown", "Section", "chunk_sections", "parse_markdown"]


# --------------------------------------------------------------------- regexes


# Matches a YAML frontmatter block at the start of the file: the file
# starts with ``---\n``, then any YAML, then ``\n---\n``. DOTALL so the
# inner YAML can span lines. Anchored at the start so a stray ``---``
# mid-file does not register as frontmatter.
_FRONTMATTER_RE: re.Pattern[str] = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Wikilink: ``[[...]]`` whose leading char is not ``!`` (embeds use the
# bang prefix). The inner capture is the verbatim contents — alias /
# anchor stripping happens downstream, not here.
_WIKILINK_RE: re.Pattern[str] = re.compile(r"(?<!\!)\[\[([^\]\n]+)\]\]")

# Embed: ``![[...]]``. Same inner-content rule as wikilinks.
_EMBED_RE: re.Pattern[str] = re.compile(r"\!\[\[([^\]\n]+)\]\]")

# Hierarchical tag. Anchored: the prior char must not be a word char,
# the tag must start with an ASCII letter, and the body is word / slash
# / hyphen characters. Stored without the leading ``#``.
_TAG_RE: re.Pattern[str] = re.compile(r"(?<!\w)#([a-zA-Z][\w/-]*)")

# Dataview inline field. Anchored to a non-letter-non-digit-non-underscore
# character followed by ``:: ``. The leading-character group is
# non-capturing; the value runs to EOL.
_DATAVIEW_RE: re.Pattern[str] = re.compile(
    r"(?:^|[^A-Za-z0-9_])([A-Za-z][\w-]*)::\s+(.*?)(?=\n|$)", re.MULTILINE
)

# Heading line. Captures the depth (1-6 hashes) and the rest of the
# line; we then filter to depth <= MARKDOWN_HEADING_DEPTH.
_HEADING_RE: re.Pattern[str] = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


# --------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class Section:
    """One atomic chunk of a parsed Markdown file."""

    heading: str | None
    heading_depth: int
    body: str
    section_index: int
    window_index: int
    body_hash: str


@dataclass(frozen=True)
class ParsedMarkdown:
    """The full parse of one Markdown file."""

    frontmatter: dict[str, Any]
    sections: tuple[Section, ...]
    wikilinks: tuple[str, ...]
    tags: tuple[str, ...]
    dataview_fields: tuple[tuple[str, str], ...]
    embeds: tuple[str, ...]


# --------------------------------------------------------------------- public API


def parse_markdown(*, absolute_path: Path, file_bytes: bytes) -> ParsedMarkdown:
    """Parse ``file_bytes`` as Markdown, returning a :class:`ParsedMarkdown`.

    Decodes UTF-8 with ``errors="replace"`` so a single bad byte does
    not kill the whole file's pipeline (the body still indexes; the
    replacement character is a visible signal to the operator).
    Malformed frontmatter is log-and-continue: the body still indexes
    with ``frontmatter = {}``.

    ``absolute_path`` is taken for symmetry with the writer's
    call shape; the parser does not use it beyond the malformed-YAML
    stderr line.
    """
    text = file_bytes.decode("utf-8", errors="replace")
    frontmatter, body = _split_frontmatter(text, source_path=absolute_path)

    wikilinks = _extract_wikilinks(body)
    embeds = _extract_embeds(body)
    tags = _extract_tags(body)
    dataview_fields = _extract_dataview(body)

    sections = chunk_sections(body)

    return ParsedMarkdown(
        frontmatter=frontmatter,
        sections=sections,
        wikilinks=wikilinks,
        tags=tags,
        dataview_fields=dataview_fields,
        embeds=embeds,
    )


def chunk_sections(body: str) -> tuple[Section, ...]:
    """Split ``body`` into sections at headings ≤ ``MARKDOWN_HEADING_DEPTH``.

    Pre-heading body lives in a section with ``heading is None``,
    ``heading_depth == 0``. Sections whose body exceeds the section cap
    split into overlapping windows. Window-split section indexes are
    *shared* across the split — the document-order index is the section,
    and ``window_index`` disambiguates within it.
    """
    lines = body.splitlines(keepends=True)
    raw_sections: list[tuple[str | None, int, str]] = []

    current_heading: str | None = None
    current_depth: int = 0
    current_lines: list[str] = []

    def _flush() -> None:
        # Trailing blank lines (the inter-section separator) carry no
        # semantic content. Stripping them keeps the section's body
        # stable when a later section is appended — the phase's
        # "append a section preserves prior chunk_ids" property
        # depends on this normalization.
        body_text = "".join(current_lines).rstrip()
        if body_text or current_heading is not None:
            raw_sections.append((current_heading, current_depth, body_text))

    for line in lines:
        heading_match = _HEADING_RE.match(line.rstrip("\r\n"))
        if heading_match is None:
            current_lines.append(line)
            continue
        depth = len(heading_match.group(1))
        if depth > MARKDOWN_HEADING_DEPTH:
            # H4+ joins the parent section's body verbatim.
            current_lines.append(line)
            continue
        # New top-level section: flush the accumulator if it had any
        # content (or any prior heading) before starting fresh.
        _flush()
        current_heading = heading_match.group(2).strip()
        current_depth = depth
        # The heading line itself is included in the new section's body
        # so the on-disk text round-trips visually for the operator.
        current_lines = [line]

    _flush()

    sections: list[Section] = []
    for section_index, (heading, depth, section_body) in enumerate(raw_sections):
        windows = _split_into_windows(section_body)
        for window_index, window_body in enumerate(windows):
            sections.append(
                Section(
                    heading=heading,
                    heading_depth=depth,
                    body=window_body,
                    section_index=section_index,
                    window_index=window_index,
                    body_hash=_hash_body(window_body),
                )
            )
    return tuple(sections)


# --------------------------------------------------------------------- internals


def _split_frontmatter(text: str, *, source_path: Path) -> tuple[dict[str, Any], str]:
    """Strip a YAML frontmatter block from the start of ``text``.

    Returns ``({}, text)`` when no block matches or the YAML parses to a
    non-mapping. Malformed YAML logs a single stderr line naming the
    source path and resolves to ``{}`` — the body still indexes.

    The returned dict is JSON-safe: ``yaml.safe_load`` decodes bare
    ISO-8601 literals into ``datetime.date`` / ``datetime.datetime`` /
    ``datetime.time`` values that ``json.dumps`` cannot serialize, so we
    walk the parse tree once and replace those values with their
    ``.isoformat()`` strings via :func:`_make_json_safe`.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    yaml_text = match.group(1)
    body = text[match.end() :]
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        print(
            f"palace index: malformed-frontmatter path={source_path} reason={exc!r}",
            file=sys.stderr,
            flush=True,
        )
        return {}, body
    if not isinstance(parsed, dict):
        return {}, body
    safe = _make_json_safe(parsed)
    assert isinstance(safe, dict)
    return safe, body


def _make_json_safe(value: Any) -> Any:
    """Recursively convert YAML-parsed Python values into JSON-safe shapes.

    PyYAML's ``safe_load`` returns ``datetime.date`` / ``datetime.datetime``
    / ``datetime.time`` for bare ISO-8601 literals; the chunks-DB writer
    JSON-serializes the frontmatter and crashes on these. Converted to
    ``.isoformat()`` strings so the dict round-trips through
    ``json.dumps``. Dict keys are coerced via ``str()`` for the same
    reason — JSON requires string keys, and a date key serializes to its
    ISO form. Walks dicts and lists recursively; scalars pass through.
    """
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _make_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_make_json_safe(v) for v in value]
    return value


def _extract_wikilinks(body: str) -> tuple[str, ...]:
    matches = _WIKILINK_RE.findall(body)
    return tuple(dict.fromkeys(matches))


def _extract_embeds(body: str) -> tuple[str, ...]:
    matches = _EMBED_RE.findall(body)
    return tuple(dict.fromkeys(matches))


def _extract_tags(body: str) -> tuple[str, ...]:
    matches = _TAG_RE.findall(body)
    return tuple(dict.fromkeys(matches))


def _extract_dataview(body: str) -> tuple[tuple[str, str], ...]:
    return tuple((m.group(1), m.group(2)) for m in _DATAVIEW_RE.finditer(body))


def _split_into_windows(body: str) -> list[str]:
    """Return one element if ``body`` fits under the cap; else overlapping windows."""
    if len(body) <= MARKDOWN_MAX_SECTION_CHARS:
        return [body]
    step = MARKDOWN_WINDOW_CHARS - MARKDOWN_WINDOW_OVERLAP_CHARS
    if step <= 0:
        # Defensive: a misconfigured overlap that meets-or-exceeds the
        # window size would loop forever; fall back to a single window.
        return [body]
    windows: list[str] = []
    for start in range(0, len(body), step):
        window = body[start : start + MARKDOWN_WINDOW_CHARS]
        if not window:
            break
        windows.append(window)
        if start + MARKDOWN_WINDOW_CHARS >= len(body):
            break
    return windows


def _hash_body(body: str) -> str:
    """SHA-256 hex of ``body``'s UTF-8 bytes."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
