"""The consolidation pipeline — the pure-function brain of dreaming.

:func:`run_consolidation` is the one-shot pass: read the selected source units
(sessions for Wolf's default, captures for an external consumer) → extract
candidates → load existing valid facts from ``MEMORY.md`` → embed those
existing claims ONCE → score every candidate → apply the three gates →
contradiction-check the gate-passers → and (only on a real run) acquire the
lock, promote the non-contradicting passers via Phase 5's ``build_fact`` +
``add_fact`` + ``write_memory``, append the DREAMS audit, append any
contradictions, append the run event, and (captures kind only) advance the
consolidation watermark over the units read this run.

The loud empty-source guard fires on **zero reader units** (before any
watermark filter): a selected source that reads nothing raises
:class:`EmptySourceError`. A captures run whose watermark drains every read
unit is NOT empty — it is a clean zero-promoted result.

The load-bearing invariant: the scoring + gating + contradiction phases are a
pure function of ``(llm, embedder, files, clock)``, computed identically
whether ``dry_run`` is True or False. A dry run takes no lock and performs no
write, but returns the *same* :class:`ConsolidationResult` a real run would —
so dry-run ≡ real-run is provable. ``--index-db`` is accepted, reserved, and
documented (RESOLUTION 5) but not load-bearing in 6.1; the novelty embedding
runs against the live ``MEMORY.md`` claims, not the chunks index.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from palace.consolidate._errors import EmptySourceError
from palace.consolidate.config import SourceKind, watermark_path
from palace.consolidate.contradictions import (
    Contradiction,
    append_contradictions,
    find_contradiction,
)
from palace.consolidate.dreams import RunSummary, append_dreams, render_dreams_entry
from palace.consolidate.events import append_consolidate_event, build_consolidate_event
from palace.consolidate.extract import extract_candidates
from palace.consolidate.llm import ConsolidatorLLM
from palace.consolidate.lock import consolidator_lock
from palace.consolidate.scoring import SignalBreakdown, score_candidate
from palace.consolidate.sources import SourceSelection, read_sources
from palace.consolidate.watermark import Watermark, filter_unconsolidated
from palace.fact.schema import Fact, build_fact
from palace.fact.store import add_fact, read_memory, write_memory
from palace.index.embedder import Embedder
from palace.vault import memory_md_path

__all__ = ["ConsolidationResult", "run_consolidation"]


@dataclass
class ConsolidationResult:
    """The full outcome of one consolidation pass.

    Identical for a dry run and a real run over the same inputs; the only
    difference between the two is whether the writes in
    :func:`run_consolidation` happened.
    """

    day: date
    breakdowns: list[SignalBreakdown]
    contradictions: list[Contradiction]
    source_kind: SourceKind = "sessions"
    promoted_fact_ids: list[str] = field(default_factory=list)
    dreams_text: str = ""

    @property
    def totals(self) -> dict[str, int]:
        """Headline counts: total candidates, promoted, discarded, held."""
        promoted = len(self.promoted_fact_ids)
        held = len(self.contradictions)
        discarded = sum(1 for b in self.breakdowns if b.failed_gates)
        return {
            "candidates_total": len(self.breakdowns),
            "promoted": promoted,
            "discarded": discarded,
            "held_contradiction": held,
        }


def run_consolidation(
    *,
    selection: SourceSelection,
    vault_root: Path,
    lock_path: Path,
    index_db: Path | None,
    day: date,
    dry_run: bool,
    llm: ConsolidatorLLM,
    embedder: Embedder,
    clock: Callable[[], date],
) -> ConsolidationResult:
    """Run one consolidation pass over ``selection``; write nothing on dry-run.

    ``selection`` names the episodic source kind + its parameters; ``day`` is
    still the ingest-stamp / event-stamp / recency anchor, and
    ``selection.store`` is the events + lock store root. The compute (extract →
    score → gate → contradiction-check) is identical across dry-run and
    real-run. Only the real-run path takes the lock and performs the appends /
    writes — and, for the captures kind, advances the watermark over the units
    read this run. ``index_db`` is reserved for the sessions kind (RESOLUTION 5)
    and additionally locates the captures watermark directory (RESOLUTION 3).
    """
    store = selection.store
    source_kind = selection.kind

    # --- read source units; loud empty-source guard ON ZERO READER UNITS ------
    sources = read_sources(selection)
    if not sources:
        raise EmptySourceError(_empty_source_message(selection))

    # For the captures kind, drop already-consolidated units (the backlog
    # filter) AFTER the empty-source guard — a watermark-drained run is a clean
    # zero-promoted result, not an empty source.
    watermark: Watermark | None = None
    wm_path: Path | None = None
    if source_kind == "captures":
        wm_path = watermark_path(index_db=index_db, lock_path=lock_path, store=store)
        watermark = Watermark.load(wm_path)
        sources = filter_unconsolidated(sources, watermark)

    # The ids read + extracted THIS run (post-filter for captures). The real-run
    # watermark advance unions these in so a re-run does not re-extract them —
    # whether or not they ultimately promoted.
    consumed_ids: list[str] = [u.source_id for u in sources]

    # --- compute (pure, dry-run ≡ real-run) -----------------------------------
    candidates = extract_candidates(sources, llm)

    memory_path = memory_md_path(vault_root)
    doc = read_memory(memory_path)
    existing_facts: list[Fact] = [f for f in doc.sections if f.valid]
    existing_claims = [f.claim for f in existing_facts]
    existing_vecs = embedder.embed(existing_claims) if existing_claims else []

    breakdowns: list[SignalBreakdown] = [
        score_candidate(
            candidate,
            llm=llm,
            embedder=embedder,
            existing_claims=existing_claims,
            existing_vecs=existing_vecs,
            run_day=day,
        )
        for candidate in candidates
    ]

    contradictions: list[Contradiction] = []
    held_claims: set[str] = set()
    promotable: list[SignalBreakdown] = []
    for breakdown in breakdowns:
        if breakdown.failed_gates:
            continue
        contradiction = find_contradiction(
            breakdown.candidate,
            llm=llm,
            embedder=embedder,
            existing_facts=existing_facts,
            existing_vecs=existing_vecs,
        )
        if contradiction is not None:
            contradictions.append(contradiction)
            held_claims.add(breakdown.candidate.claim)
        else:
            promotable.append(breakdown)

    # --- materialize the would-be promoted facts (id-stable, no write yet) ----
    ingest_time = _now_ingest(clock)
    promoted: list[Fact] = [
        build_fact(
            summary=breakdown.candidate.summary,
            claim=breakdown.candidate.claim,
            event_time=breakdown.candidate.event_time,
            ingest_time=ingest_time,
            confidence=breakdown.confidence,
            provenance=sorted(breakdown.candidate.source_ids),
            tags=breakdown.candidate.tags,
            refs=breakdown.candidate.refs,
        )
        for breakdown in promotable
    ]

    run_summary = RunSummary(
        day=day,
        candidates_total=len(breakdowns),
        promoted=len(promoted),
        discarded=sum(1 for b in breakdowns if b.failed_gates),
        held_contradiction=len(contradictions),
        dry_run=dry_run,
        source_kind=source_kind,
    )
    dreams_text = render_dreams_entry(run_summary, breakdowns, frozenset(held_claims))

    result = ConsolidationResult(
        day=day,
        breakdowns=breakdowns,
        contradictions=contradictions,
        source_kind=source_kind,
        promoted_fact_ids=[f.id for f in promoted],
        dreams_text=dreams_text,
    )

    if dry_run:
        return result

    # --- real-run writes (under the exclusive lock) ---------------------------
    with consolidator_lock(lock_path):
        for fact in promoted:
            add_fact(doc, fact)
        if promoted:
            write_memory(memory_path, doc)
        append_dreams(_dreams_path(vault_root), dreams_text)
        append_contradictions(_contradictions_path(vault_root, day), contradictions)
        if watermark is not None and wm_path is not None:
            # Captures kind: advance the backlog watermark over every unit read
            # + extracted this run, so a re-run does not re-extract them.
            watermark.with_added(consumed_ids).save(wm_path)
        event = build_consolidate_event(
            ingest_time=ingest_time,
            day=day.isoformat(),
            totals=result.totals,
        )
        append_consolidate_event(store, event, clock=clock)

    return result


# ---------------------------------------------------------------- internals


def _empty_source_message(selection: SourceSelection) -> str:
    """A loud, specific message for a selected source that read zero units."""
    if selection.kind == "captures":
        return (
            f"no readable capture units under {selection.captures_root} — "
            "the captures source is empty (no '<slug>-<12hex>.md' files matched)"
        )
    return (
        f"no captured sessions for {selection.day.isoformat()} under "
        f"{selection.store / 'sessions'} — the sessions source is empty"
    )


def _dreams_path(vault_root: Path) -> Path:
    from palace.consolidate.config import dreams_md_path

    return dreams_md_path(vault_root)


def _contradictions_path(vault_root: Path, day: date) -> Path:
    from palace.consolidate.config import contradictions_path

    return contradictions_path(vault_root, day)


def _now_ingest(clock: Callable[[], date]) -> str:
    """Return an ISO ingest timestamp anchored to the run day.

    The consolidator stamps ``ingest_time`` from the run day (midnight, the
    America/Boise convention) so the promoted facts' ids are a pure function
    of the inputs — a wall-clock now() would break the dry-run ≡ real-run
    id-stability the determinism gate asserts.
    """
    from datetime import datetime, time

    from palace.daemons.capture.config import BOISE_TZ

    return datetime.combine(clock(), time.min, tzinfo=BOISE_TZ).isoformat(timespec="seconds")
