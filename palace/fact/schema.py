"""Fact dataclass, id computer, and section serializer / parser.

Implements the on-disk encoding of ``policies/fact-schema.md`` exactly: a
Markdown ``## <summary>`` heading, a blank line, a YAML frontmatter block
delimited by ``---``, a blank line, then the claim body.

The fact ``id`` is the SHA-256 hex of the canonical JSON form of
``{event_time, ingest_time, confidence, provenance, tags, refs, claim}`` —
``id``, ``valid``, and ``superseded_by`` are excluded so they can be patched
in place without breaking the id. The hash reuses
:func:`palace.daemons.capture.ids.compute_record_id` verbatim — there is no
second canonical-JSON implementation in palace.

The frontmatter field order is fixed: ``id, event_time, ingest_time,
confidence, valid, [superseded_by when set], provenance, tags, refs``;
``tags`` / ``refs`` are omitted entirely when empty. ``event_time`` renders
as a scalar ISO-8601 string or, for intervals, as a ``{start, end}`` block
(``end`` may be ``null``). ``provenance`` and ``refs`` render as block-style
YAML lists; ``tags`` renders as a flow list ``[a, b]`` — matching the
example in the policy.

Lifecycle mutation is line-level: :func:`patch_valid` and
:func:`patch_superseded_by` rewrite only the one frontmatter line they
target, preserving every other byte of the section so unchanged sections
round-trip byte-for-byte.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from palace.daemons.capture.ids import compute_record_id
from palace.fact._errors import FactError

__all__ = [
    "EventTime",
    "Fact",
    "build_fact",
    "fact_id",
    "parse_fact",
    "patch_superseded_by",
    "patch_valid",
    "serialize_fact",
]


# A scalar ISO-8601 string, or an interval object ``{"start": ..., "end": ...}``
# where ``end`` may be ``None`` for an open-ended interval.
EventTime = str | dict[str, Any]


@dataclass(frozen=True)
class Fact:
    """One bitemporal fact, as stored in ``<vault-root>/facts/MEMORY.md``.

    ``raw`` retains the exact on-disk section text when the fact was parsed
    from disk; it is ``None`` for freshly built facts. Lifecycle ops patch
    ``raw`` line-by-line so unchanged sections round-trip byte-for-byte.
    """

    id: str
    summary: str
    claim: str
    event_time: EventTime
    ingest_time: str
    confidence: float
    valid: bool
    provenance: list[str]
    tags: list[str]
    refs: list[str]
    superseded_by: str | None
    raw: str | None


def fact_id(
    *,
    event_time: EventTime,
    ingest_time: str,
    confidence: float,
    provenance: Sequence[str],
    tags: Sequence[str],
    refs: Sequence[str],
    claim: str,
) -> str:
    """Compute the SHA-256 hex id over the fact's canonical JSON form.

    Builds exactly the seven-key dict
    ``{event_time, ingest_time, confidence, provenance, tags, refs, claim}``
    (``tags`` / ``refs`` normalized to ``[]``) and hands it to
    :func:`compute_record_id`, which sorts keys, strips whitespace, and
    encodes with ``ensure_ascii=True``. ``id``, ``valid``, and
    ``superseded_by`` are deliberately excluded.
    """
    body: dict[str, Any] = {
        "event_time": _json_safe(event_time),
        "ingest_time": ingest_time,
        "confidence": confidence,
        "provenance": list(provenance),
        "tags": list(tags),
        "refs": list(refs),
        "claim": claim,
    }
    return compute_record_id(body)


def build_fact(
    *,
    summary: str,
    claim: str,
    event_time: EventTime,
    ingest_time: str,
    confidence: float,
    provenance: Sequence[str],
    tags: Sequence[str] = (),
    refs: Sequence[str] = (),
) -> Fact:
    """Construct a fresh, valid :class:`Fact`, stamping its id.

    ``valid`` is ``True``, ``superseded_by`` is ``None``, and ``raw`` is
    ``None`` (the serializer renders the on-disk text on demand).
    """
    fid = fact_id(
        event_time=event_time,
        ingest_time=ingest_time,
        confidence=confidence,
        provenance=provenance,
        tags=tags,
        refs=refs,
        claim=claim,
    )
    return Fact(
        id=fid,
        summary=summary,
        claim=claim,
        event_time=event_time,
        ingest_time=ingest_time,
        confidence=confidence,
        valid=True,
        provenance=list(provenance),
        tags=list(tags),
        refs=list(refs),
        superseded_by=None,
        raw=None,
    )


def serialize_fact(fact: Fact) -> str:
    """Render ``fact`` as canonical section text per ``policies/fact-schema.md``.

    Emits the fixed field order with ``event_time`` as scalar or block,
    ``provenance`` / ``refs`` as block-style lists, ``tags`` as a flow list,
    ``superseded_by`` only when set, and ``tags`` / ``refs`` omitted when
    empty. The section is heading + blank + ``---`` frontmatter ``---`` +
    blank + claim body, with no trailing blank line (the document
    serializer owns inter-section spacing).
    """
    lines: list[str] = []
    lines.append(f"## {fact.summary}")
    lines.append("")
    lines.append("---")
    lines.append(f"id: {fact.id}")
    lines.extend(_render_event_time(fact.event_time))
    lines.append(f"ingest_time: {fact.ingest_time}")
    lines.append(f"confidence: {_render_float(fact.confidence)}")
    lines.append(f"valid: {_render_bool(fact.valid)}")
    if fact.superseded_by is not None:
        lines.append(f"superseded_by: {fact.superseded_by}")
    lines.append("provenance:")
    lines.extend(f"  - {item}" for item in fact.provenance)
    if fact.tags:
        lines.append(f"tags: [{', '.join(fact.tags)}]")
    if fact.refs:
        lines.append("refs:")
        lines.extend(f'  - "{ref}"' for ref in fact.refs)
    lines.append("---")
    lines.append("")
    lines.append(fact.claim)
    return "\n".join(lines) + "\n"


def parse_fact(section_text: str) -> Fact:
    """Parse one section's text into a :class:`Fact`, retaining ``raw``.

    Splits the heading, the ``---``-delimited YAML frontmatter, and the
    claim body; loads the frontmatter via :func:`yaml.safe_load`; and keeps
    ``raw=section_text`` so lifecycle patches can edit it line-by-line.
    """
    heading, frontmatter_text, body = _split_section(section_text)
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise FactError(f"malformed frontmatter in fact section: {exc}") from exc
    if not isinstance(data, dict):
        raise FactError("fact section frontmatter is not a YAML mapping")

    # Normalize YAML-decoded date/datetime scalars (event_time, ingest_time)
    # to their ISO string form up front, so the in-memory Fact carries the
    # exact strings that were hashed into the id at promotion time. ``str()``
    # on a datetime uses a space separator and would break the id; isoformat
    # restores the ``T``.
    data = _json_safe(data)

    try:
        fid = str(data["id"])
        event_time = data["event_time"]
        ingest_time = str(data["ingest_time"])
        confidence = float(data["confidence"])
        valid = bool(data["valid"])
        provenance = [str(p) for p in data["provenance"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise FactError(f"fact section missing or malformed field: {exc}") from exc

    superseded_by = data.get("superseded_by")
    tags = [str(t) for t in data.get("tags") or []]
    refs = [str(r) for r in data.get("refs") or []]

    return Fact(
        id=fid,
        summary=heading,
        claim=body,
        event_time=event_time,
        ingest_time=ingest_time,
        confidence=confidence,
        valid=valid,
        provenance=provenance,
        tags=tags,
        refs=refs,
        superseded_by=str(superseded_by) if superseded_by is not None else None,
        raw=section_text,
    )


def patch_valid(section_text: str, valid: bool) -> str:
    """Rewrite only the ``valid:`` line in ``section_text``, preserving the rest."""
    return _patch_scalar_line(section_text, "valid", _render_bool(valid))


def patch_superseded_by(section_text: str, new_id: str) -> str:
    """Set ``superseded_by: <new_id>`` in ``section_text``.

    Rewrites the existing line if present, else inserts a fresh line
    immediately after the ``valid:`` line (the policy's field order). Every
    other byte is preserved.
    """
    lines = section_text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith("superseded_by:"):
            lines[index] = f"superseded_by: {new_id}"
            return "\n".join(lines)
    for index, line in enumerate(lines):
        if line.startswith("valid:"):
            lines.insert(index + 1, f"superseded_by: {new_id}")
            return "\n".join(lines)
    raise FactError("cannot patch superseded_by: no 'valid:' line in section")


# --------------------------------------------------------------------- internals


def _json_safe(value: Any) -> Any:
    """Convert YAML-parsed date/datetime values to their ``isoformat`` strings.

    ``yaml.safe_load`` decodes a bare ``2026-04-01`` event_time into a
    ``datetime.date`` that ``json.dumps`` cannot serialize. The id hash must
    be identical whether ``event_time`` arrived as a Python string (from a
    CLI flag) or as a date (from a disk re-parse), so both paths normalize
    to the ISO string form here — the same trick
    :func:`palace.index.markdown._make_json_safe` uses. Walks dicts and
    lists; scalars other than date/datetime/time pass through unchanged.
    """
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _render_event_time(event_time: EventTime) -> list[str]:
    """Render ``event_time`` as a scalar line or a ``{start, end}`` block."""
    if isinstance(event_time, dict):
        start = event_time.get("start")
        end = event_time.get("end")
        return [
            "event_time:",
            f"  start: {_render_scalar(start)}",
            f"  end: {_render_scalar(end)}",
        ]
    return [f"event_time: {_render_scalar(event_time)}"]


def _render_scalar(value: Any) -> str:
    """Render a YAML scalar: ``null`` for ``None``, otherwise the bare string."""
    if value is None:
        return "null"
    return str(value)


def _render_bool(value: bool) -> str:
    return "true" if value else "false"


def _render_float(value: float) -> str:
    """Render a confidence float, dropping a trailing ``.0`` only when integral.

    ``1.0`` renders as ``1.0`` and ``0.82`` as ``0.82`` — the on-disk form
    the round-trip gate expects (Python's ``repr`` of these floats).
    """
    return repr(value)


def _split_section(section_text: str) -> tuple[str, str, str]:
    """Split section text into ``(heading, frontmatter_yaml, body)``.

    Expects ``## <summary>`` heading, a blank line, ``---`` ... ``---``
    frontmatter, a blank line, then the body. Raises :class:`FactError` on
    a shape the parser cannot read.
    """
    if not section_text.startswith("## "):
        raise FactError("fact section does not start with an '## ' heading")
    lines = section_text.split("\n")
    heading = lines[0][len("## ") :].strip()

    # Locate the frontmatter fences (the first two bare '---' lines after
    # the heading).
    fence_indices = [i for i, line in enumerate(lines) if line == "---"]
    if len(fence_indices) < 2:
        raise FactError("fact section has no '---'-delimited frontmatter block")
    open_fence, close_fence = fence_indices[0], fence_indices[1]
    frontmatter_text = "\n".join(lines[open_fence + 1 : close_fence])

    body_lines = lines[close_fence + 1 :]
    # Drop the single blank line that follows the closing fence.
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines).rstrip("\n")
    return heading, frontmatter_text, body


def _patch_scalar_line(section_text: str, key: str, value: str) -> str:
    """Rewrite the single ``<key>: ...`` line, preserving every other byte."""
    prefix = f"{key}:"
    lines = section_text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}: {value}"
            return "\n".join(lines)
    raise FactError(f"cannot patch '{key}': no matching line in section")
