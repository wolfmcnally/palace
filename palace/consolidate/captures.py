"""The captures-directory episodic source reader (external-consumer kind).

A non-default source kind for consumers whose episodic ground truth is a flat
directory of pre-distilled Markdown capture files rather than day-partitioned
session transcripts. Each capture is YAML frontmatter (``id``, ``event_time``,
``ingest_time``, ``source``) plus a verbatim prose body — the distilled claim.
This module reads each into the same :class:`palace.consolidate.sources.SourceUnit`
the extractor already consumes, mapping directly and losslessly:

- ``source_id`` ← the capture's frontmatter ``id`` (already a palace-canonical
  64-hex SHA-256; no transformation);
- ``text`` ← the capture's Markdown **body, verbatim** — no ``transcript_path``
  resolution, no char-budget windowing (captures are already distilled);
- ``event_time`` / ``ingest_time`` ← the frontmatter fields;
- ``authored`` ← derived from the ``source:`` field per
  :func:`source_is_user_authored`.

Loud, not silent (``briefs/consolidator-extraction-source-pluggability.md``):
a file whose name does not match the capture filename shape is **skipped** (it
is some other Markdown file in the inbox, not an error), but a file that *does*
match yet is malformed — bad frontmatter, missing/invalid ``id`` — raises a
:class:`ConsolidationError` naming the path, never silently dropped.

See ``policies/consolidation.md`` §"Extraction sources" and
``briefs/consolidator-extraction-source-pluggability.md`` for the capture format.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from palace.consolidate._errors import ConsolidationError
from palace.consolidate.config import CAPTURE_FILENAME_RE, USER_TRUST_PREFIXES
from palace.consolidate.sources import SourceUnit

# Reuse the fact-schema's date/datetime → ISO-string normalizer so a YAML
# ``2026-06-15`` event_time hashes/compares identically whether it arrived as a
# Python string or a YAML-decoded date. One canonicalizer, no shared util.
from palace.fact.schema import _json_safe

__all__ = ["parse_capture", "read_captures_dir", "source_is_user_authored"]

# A capture id is a 64-char lowercase SHA-256 hex string.
_ID_LEN = 64


def read_captures_dir(captures_root: Path, *, since: date | None = None) -> list[SourceUnit]:
    """Read a flat directory of Markdown captures into source units.

    Globs ``<captures_root>/*.md``, skipping any file whose name does not match
    the ``<slug>-<12hex>.md`` capture shape, parses each matching file, applies
    an optional ``event_time >= since`` filter (the ``--since`` backlog
    narrowing), and returns the units sorted by ``source_id`` for determinism.

    A missing directory is an empty read (zero units); the loud empty-source
    guard fires in the pipeline, not here.
    """
    if not captures_root.is_dir():
        return []
    units: list[SourceUnit] = []
    for path in sorted(captures_root.glob("*.md")):
        if not CAPTURE_FILENAME_RE.match(path.name):
            continue
        unit = parse_capture(path.read_text(encoding="utf-8"), path=path)
        if since is not None and not _on_or_after(unit.event_time, since):
            continue
        units.append(unit)
    units.sort(key=lambda u: u.source_id)
    return units


def parse_capture(text: str, *, path: Path) -> SourceUnit:
    """Parse one capture file's text into a :class:`SourceUnit`.

    Splits the leading ``---``-delimited YAML frontmatter from the verbatim
    body, ``yaml.safe_load``s the frontmatter, normalizes its date/datetime
    scalars to ISO strings, validates ``id`` is a 64-hex string, and maps the
    fields directly. The body is taken verbatim (trailing newlines stripped) —
    captures are pre-distilled, so there is no transcript resolution or
    windowing. Any genuinely malformed capture raises a
    :class:`ConsolidationError` naming ``path``.
    """
    frontmatter, body = _split_frontmatter(text, path=path)
    try:
        data = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise ConsolidationError(f"malformed capture frontmatter {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConsolidationError(f"capture frontmatter {path} is not a YAML mapping")
    data = _json_safe(data)

    source_id = data.get("id")
    if not isinstance(source_id, str) or not _is_hex64(source_id):
        raise ConsolidationError(f"capture {path} has a missing or non-64-hex 'id': {source_id!r}")

    return SourceUnit(
        source_id=source_id,
        text=body.rstrip("\n"),
        event_time=str(data.get("event_time") or ""),
        ingest_time=str(data.get("ingest_time") or ""),
        harness=None,
        authored=source_is_user_authored(data.get("source")),
    )


def source_is_user_authored(source: str | None) -> bool:
    """Return True when a capture's ``source:`` marks it as user-authored.

    The captures-kind source-trust signal (enhancement 3): a capture is
    authored when its ``source:`` begins with one of
    :data:`palace.consolidate.config.USER_TRUST_PREFIXES` (default ``user:``),
    so a single direct-user capture clears the corroboration-via-one-authored-
    source gate. A ``journal:*`` source or a missing source is **not** authored.
    """
    if not isinstance(source, str):
        return False
    return any(source.startswith(prefix) for prefix in USER_TRUST_PREFIXES)


# --------------------------------------------------------------------- internals


def _split_frontmatter(text: str, *, path: Path) -> tuple[str, str]:
    """Split a capture into ``(frontmatter_yaml, body)`` at the ``---`` fences.

    Expects a leading ``---`` fence, the YAML block, a closing ``---`` fence,
    then the body. Raises :class:`ConsolidationError` (naming ``path``) on a
    shape with no closing fence.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ConsolidationError(f"capture {path} has no leading '---' frontmatter fence")
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        raise ConsolidationError(f"capture {path} frontmatter has no closing '---' fence")
    frontmatter = "\n".join(lines[1:close])
    body_lines = lines[close + 1 :]
    # Drop a single blank line immediately after the closing fence.
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    return frontmatter, "\n".join(body_lines)


def _is_hex64(value: str) -> bool:
    """Return True when ``value`` is a 64-char lowercase hex string."""
    if len(value) != _ID_LEN:
        return False
    return all(c in "0123456789abcdef" for c in value)


def _on_or_after(event_time: str, since: date) -> bool:
    """Return True when ``event_time``'s ISO date prefix is on or after ``since``.

    An empty or unparseable ``event_time`` is treated as *not* on-or-after, so
    a ``--since`` filter never silently admits a capture with no usable date.
    """
    prefix = event_time[:10]
    try:
        when = date.fromisoformat(prefix)
    except ValueError:
        return False
    return when >= since
