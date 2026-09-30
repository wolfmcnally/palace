"""Contradiction detection and the append-only contradictions surface.

A candidate that would otherwise promote but contradicts an existing
``valid: true`` fact is HELD (not promoted, RESOLUTION 2) and routed to
``<vault-root>/dreams/contradictions/<YYYY-MM-DD>.md`` for Wolf to adjudicate.
NEITHER fact is mutated — no auto-invalidate, no auto-supersede.

Detection is two-stage to keep the slow chat confirmation bounded: an
embedding-cosine shortlist of the top-``CONTRADICTION_SHORTLIST_K`` most
similar existing facts, then a local-chat-model ``detect_contradiction``
confirmation on each shortlisted pair. The first confirmed contradiction wins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from palace.consolidate.config import CONTRADICTION_SHORTLIST_K
from palace.consolidate.extract import Candidate
from palace.consolidate.llm import ConsolidatorLLM
from palace.fact.schema import Fact
from palace.index.embedder import Embedder

__all__ = [
    "Contradiction",
    "append_contradictions",
    "find_contradiction",
    "render_contradictions_md",
]


@dataclass(frozen=True)
class Contradiction:
    """A confirmed candidate-vs-existing-fact contradiction held for review."""

    candidate: Candidate
    existing_fact_id: str
    existing_claim: str
    rationale: str
    recommended_resolution: str


def find_contradiction(
    candidate: Candidate,
    *,
    llm: ConsolidatorLLM,
    embedder: Embedder,
    existing_facts: list[Fact],
    existing_vecs: list[list[float]],
) -> Contradiction | None:
    """Return the first confirmed contradiction for ``candidate``, or None.

    Shortlists the top-K most-similar existing facts by embedding cosine,
    then asks the LLM to confirm each in shortlist order. ``existing_vecs``
    is the pre-embedded existing-claim corpus (one vector per fact, aligned
    by index) so the existing facts are embedded once per run.
    """
    if not existing_facts:
        return None
    cand_vec = embedder.embed([candidate.claim])[0]
    scored = [(i, _cosine(cand_vec, existing_vecs[i])) for i in range(len(existing_facts))]
    scored.sort(key=lambda pair: (-pair[1], existing_facts[pair[0]].id))
    shortlist = scored[:CONTRADICTION_SHORTLIST_K]

    for index, _similarity in shortlist:
        fact = existing_facts[index]
        verdict = llm.detect_contradiction(candidate.claim, fact.claim)
        if verdict.contradicts:
            return Contradiction(
                candidate=candidate,
                existing_fact_id=fact.id,
                existing_claim=fact.claim,
                rationale=verdict.rationale,
                recommended_resolution=verdict.recommended_resolution,
            )
    return None


def render_contradictions_md(contradictions: list[Contradiction], day: date) -> str:
    """Render a per-run contradictions section for the day's file.

    The section is a dated H2 followed by one H3 per contradiction carrying
    the candidate claim, the existing fact (id + claim), the rationale, and
    the recommended resolution — all plain Markdown a headless reader parses.
    """
    lines: list[str] = []
    lines.append(f"## Contradictions surfaced {day.isoformat()}")
    lines.append("")
    for con in contradictions:
        lines.append(f"### {con.candidate.summary}")
        lines.append("")
        lines.append("**Candidate claim:**")
        lines.append("")
        lines.append(con.candidate.claim)
        lines.append("")
        lines.append(f"**Existing fact `{con.existing_fact_id}`:**")
        lines.append("")
        lines.append(con.existing_claim)
        lines.append("")
        lines.append(f"**Rationale:** {con.rationale}")
        lines.append("")
        lines.append(f"**Recommended resolution:** {con.recommended_resolution}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def append_contradictions(path: Path, contradictions: list[Contradiction]) -> None:
    """Append a rendered contradictions section to ``path``, never overwriting.

    Creates the parent ``contradictions/`` directory on first write. A blank
    line separates appended sections; the file is only opened when there is at
    least one contradiction to record.
    """
    if not contradictions:
        return
    day = _day_from_path(path)
    section = render_contradictions_md(contradictions, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > 0:
        existing = path.read_text(encoding="utf-8").rstrip("\n")
        path.write_text(existing + "\n\n" + section, encoding="utf-8")
    else:
        path.write_text(section, encoding="utf-8")


def _day_from_path(path: Path) -> date:
    """Recover the ISO day from a ``<YYYY-MM-DD>.md`` contradictions path."""
    return date.fromisoformat(path.stem)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 on a zero vector."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
