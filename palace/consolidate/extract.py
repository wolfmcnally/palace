"""Candidate-fact extraction over the day's source units.

Runs the LLM extractor once per source unit, then merges identical claims
across units (unioning their source ids) so corroboration counting and the
final provenance list are exact. Subagent turns are folded into the
extraction text via the source unit's ``harness`` context (RESOLUTION 4); the
authoritative provenance is the set of source ids, not anything the model
returns.

The output is sorted by claim for determinism — the pipeline's dry-run ≡
real-run invariant requires the candidate order to be a pure function of the
inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from palace.consolidate.llm import ConsolidatorLLM, ExtractionContext
from palace.consolidate.sources import SourceUnit

__all__ = ["Candidate", "extract_candidates"]


@dataclass
class Candidate:
    """One merged candidate fact, ready for scoring.

    ``source_ids`` is the union of every source unit the claim was extracted
    from; ``authored`` is True when ANY contributing unit was an explicit
    authored source (one such source satisfies the corroboration gate). The
    "authored?" predicate is source-kind-aware — sessions key it on the
    Wolf-authored heuristic, captures on the ``source:`` prefix.
    """

    summary: str
    claim: str
    source_ids: set[str]
    event_time: str
    tags: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    authored: bool = False


def extract_candidates(sources: list[SourceUnit], llm: ConsolidatorLLM) -> list[Candidate]:
    """Extract and merge candidate facts from the day's source units.

    One extract call per source unit; identical claims are merged across
    units (source-id union, authored OR, earliest event_time kept).
    Returns candidates sorted by claim for determinism.
    """
    merged: dict[str, Candidate] = {}
    for unit in sources:
        if not unit.text.strip():
            continue
        context = ExtractionContext(
            harness=unit.harness,
            authored=unit.authored,
            day=unit.event_time[:10],
        )
        for raw in llm.extract_candidates(unit.text, context=context):
            key = raw.claim.strip()
            if not key:
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = Candidate(
                    summary=raw.summary,
                    claim=raw.claim,
                    source_ids={unit.source_id},
                    event_time=unit.event_time,
                    tags=list(raw.tags),
                    refs=list(raw.refs),
                    authored=unit.authored,
                )
            else:
                existing.source_ids.add(unit.source_id)
                existing.authored = existing.authored or unit.authored
                if unit.event_time and (
                    not existing.event_time or unit.event_time < existing.event_time
                ):
                    existing.event_time = unit.event_time
                _merge_unique(existing.tags, raw.tags)
                _merge_unique(existing.refs, raw.refs)

    return sorted(merged.values(), key=lambda c: c.claim)


def _merge_unique(target: list[str], incoming: list[str]) -> None:
    """Append items from ``incoming`` not already present in ``target``."""
    for item in incoming:
        if item not in target:
            target.append(item)
