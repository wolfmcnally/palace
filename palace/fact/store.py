"""The ``<vault-root>/facts/MEMORY.md`` document model and mutations.

A ``MEMORY.md`` file is a preamble (everything before the first ``## ``
heading) followed by a sequence of fact sections, one per fact, in
chronological-by-ingest order. :func:`parse_memory` and
:func:`serialize_memory` round-trip the document byte-for-byte for unchanged
sections: each parsed section retains its exact on-disk text in
``Fact.raw``, and serialization concatenates those bytes back with exactly
one blank line between sections and a single trailing newline at EOF.

Mutations are surgical. :func:`add_fact` appends a freshly serialized
section at EOF. :func:`invalidate_fact` and :func:`supersede_fact` patch
only the ``valid:`` / ``superseded_by:`` lines of the target section's
retained ``raw`` — no untouched section is re-serialized, so the only bytes
that change on disk are the ones the lifecycle op semantically changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from palace.fact._errors import FactError
from palace.fact.schema import (
    Fact,
    parse_fact,
    patch_superseded_by,
    patch_valid,
    serialize_fact,
)

__all__ = [
    "MemoryDoc",
    "add_fact",
    "find_fact",
    "invalidate_fact",
    "parse_memory",
    "read_memory",
    "serialize_memory",
    "supersede_fact",
    "write_memory",
]


@dataclass
class MemoryDoc:
    """A parsed ``MEMORY.md``: a preamble plus an ordered list of facts."""

    preamble: str
    sections: list[Fact]


def parse_memory(text: str) -> MemoryDoc:
    """Parse ``text`` into a :class:`MemoryDoc`.

    Splits on ``## `` heading boundaries (line-start). Everything before the
    first heading is the preamble (empty string when the file opens with a
    heading). Each section's exact text is retained in ``Fact.raw`` so the
    document round-trips byte-for-byte.
    """
    lines = text.split("\n")
    heading_indices = [i for i, line in enumerate(lines) if line.startswith("## ")]

    if not heading_indices:
        return MemoryDoc(preamble=text, sections=[])

    first = heading_indices[0]
    preamble = "\n".join(lines[:first])

    sections: list[Fact] = []
    bounds = heading_indices + [len(lines)]
    for start, end in zip(bounds, bounds[1:], strict=False):
        section_text = "\n".join(lines[start:end]).rstrip("\n")
        sections.append(parse_fact(section_text))
    return MemoryDoc(preamble=preamble, sections=sections)


def serialize_memory(doc: MemoryDoc) -> str:
    """Serialize ``doc`` back to text, byte-identical for unchanged sections.

    Concatenates the preamble and each section's retained ``raw`` (or a
    freshly serialized form when ``raw`` is ``None``, i.e. a just-added
    fact), separated by exactly one blank line, with a single trailing
    newline at EOF.
    """
    blocks: list[str] = []
    preamble = doc.preamble.rstrip("\n")
    if preamble:
        blocks.append(preamble)
    for fact in doc.sections:
        blocks.append(fact.raw if fact.raw is not None else serialize_fact(fact).rstrip("\n"))
    return "\n\n".join(blocks) + "\n"


def read_memory(path: Path) -> MemoryDoc:
    """Read and parse ``MEMORY.md`` at ``path`` (empty doc when absent)."""
    if not path.is_file():
        return MemoryDoc(preamble="", sections=[])
    return parse_memory(path.read_text(encoding="utf-8"))


def write_memory(path: Path, doc: MemoryDoc) -> None:
    """Serialize ``doc`` and write it to ``path``, creating ``facts/`` if absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_memory(doc), encoding="utf-8")


def find_fact(doc: MemoryDoc, ref: str) -> Fact:
    """Return the fact whose id is ``ref`` or whose id has ``ref`` as a prefix.

    Raises :class:`FactError` when no fact matches or when a prefix is
    ambiguous (matches more than one fact).
    """
    exact = [f for f in doc.sections if f.id == ref]
    if len(exact) == 1:
        return exact[0]

    prefix_matches = [f for f in doc.sections if f.id.startswith(ref)]
    if not prefix_matches:
        raise FactError(f"no fact matches id '{ref}'")
    if len(prefix_matches) > 1:
        ids = ", ".join(f.id[:12] for f in prefix_matches)
        raise FactError(f"ambiguous fact id prefix '{ref}' matches: {ids}")
    return prefix_matches[0]


def add_fact(doc: MemoryDoc, fact: Fact) -> None:
    """Append ``fact`` as a new section at EOF."""
    doc.sections.append(fact)


def invalidate_fact(doc: MemoryDoc, ref: str) -> Fact:
    """Patch the target section's ``valid:`` line to ``false`` in place.

    Returns the updated :class:`Fact`. Only the target section's ``raw``
    bytes change; every other section is untouched.
    """
    target = find_fact(doc, ref)
    index = doc.sections.index(target)
    raw = target.raw if target.raw is not None else serialize_fact(target).rstrip("\n")
    patched_raw = patch_valid(raw, False)
    updated = parse_fact(patched_raw)
    doc.sections[index] = updated
    return updated


def supersede_fact(doc: MemoryDoc, old_ref: str, new_fact: Fact) -> tuple[Fact, Fact]:
    """Supersede the fact at ``old_ref`` with ``new_fact``.

    Patches the old section's ``superseded_by:`` to ``new_fact.id`` and its
    ``valid:`` to ``false`` (both line-level, every other byte preserved),
    then appends ``new_fact`` as a fresh section at EOF. Returns
    ``(updated_old_fact, new_fact)``.
    """
    old = find_fact(doc, old_ref)
    index = doc.sections.index(old)
    raw = old.raw if old.raw is not None else serialize_fact(old).rstrip("\n")
    patched_raw = patch_superseded_by(raw, new_fact.id)
    patched_raw = patch_valid(patched_raw, False)
    updated_old = parse_fact(patched_raw)
    doc.sections[index] = updated_old
    doc.sections.append(new_fact)
    return updated_old, new_fact
