"""The determinism gate + write/no-write contract for the pipeline.

The load-bearing assertions:

- A dry run and a real run over the same fixtures produce identical per-
  candidate breakdowns and verdicts.
- A dry run leaves MEMORY.md, DREAMS.md, contradictions/, the events log, and
  the lock untouched.
- A real run promotes at least one fact whose provenance equals the exact set
  of source ids the candidate was extracted from.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from palace.consolidate._errors import EmptySourceError
from palace.consolidate.config import (
    consolidator_lock_path,
    contradictions_path,
    dreams_md_path,
    watermark_path,
)
from palace.consolidate.pipeline import ConsolidationResult, run_consolidation
from palace.consolidate.sources import SourceSelection
from palace.consolidate.watermark import Watermark
from palace.fact.store import read_memory
from palace.vault import memory_md_path
from tests.consolidate.conftest import FakeConsolidatorLLM, FakeEmbedder


def _run(
    store: Path,
    vault: Path,
    llm: FakeConsolidatorLLM,
    clock: Callable[[], date],
    *,
    dry_run: bool,
) -> ConsolidationResult:
    return run_consolidation(
        selection=SourceSelection(kind="sessions", store=store, day=clock()),
        vault_root=vault,
        lock_path=consolidator_lock_path(store),
        index_db=None,
        day=clock(),
        dry_run=dry_run,
        llm=llm,
        embedder=FakeEmbedder(),
        clock=clock,
    )


def _signature(result: ConsolidationResult) -> list[tuple[str, str, float, tuple[str, ...], float]]:
    return [
        (
            b.candidate.summary,
            b.verdict,
            round(b.weighted_total, 9),
            tuple(b.failed_gates),
            round(b.novelty, 9),
        )
        for b in result.breakdowns
    ]


def test_dry_run_equals_real_run_breakdowns(
    seeded_vault: tuple[Path, Path], fake_llm: FakeConsolidatorLLM, boise_clock: Callable[[], date]
) -> None:
    store, vault = seeded_vault
    dry = _run(store, vault, fake_llm, boise_clock, dry_run=True)
    real = _run(store, vault, fake_llm, boise_clock, dry_run=False)
    assert _signature(dry) == _signature(real)
    assert dry.promoted_fact_ids == real.promoted_fact_ids
    # The audit bodies are identical bar the deliberate "(dry-run)" header tag.
    assert dry.dreams_text.replace(" (dry-run)", "", 1) == real.dreams_text
    # Sessions-default regression: the default kind is reported, and the audit
    # header carries no source-kind tag (byte-unchanged from before captures).
    assert dry.source_kind == "sessions"
    assert real.source_kind == "sessions"
    assert "## Consolidation 2026-06-15\n" in real.dreams_text


def test_dry_run_writes_nothing(
    seeded_vault: tuple[Path, Path], fake_llm: FakeConsolidatorLLM, boise_clock: Callable[[], date]
) -> None:
    store, vault = seeded_vault
    memory = memory_md_path(vault)
    before = memory.read_bytes()

    result = _run(store, vault, fake_llm, boise_clock, dry_run=True)
    assert result.totals["candidates_total"] >= 1

    assert memory.read_bytes() == before
    assert not dreams_md_path(vault).exists()
    assert not contradictions_path(vault, boise_clock()).exists()
    assert not consolidator_lock_path(store).exists()
    # No events file created for the run day beyond what the fixture seeded.
    events = store / "events" / f"{boise_clock().isoformat()}.jsonl"
    seeded_lines = events.read_text(encoding="utf-8").splitlines()
    assert all('"consolidate"' not in ln for ln in seeded_lines)


def test_real_run_promotes_with_exact_provenance(
    seeded_vault: tuple[Path, Path], fake_llm: FakeConsolidatorLLM, boise_clock: Callable[[], date]
) -> None:
    store, vault = seeded_vault
    result = _run(store, vault, fake_llm, boise_clock, dry_run=False)

    assert len(result.promoted_fact_ids) >= 1

    doc = read_memory(memory_md_path(vault))
    promoted = [f for f in doc.sections if f.id in result.promoted_fact_ids]
    assert promoted

    # The demonstration-model candidate (A) is single-source but operator-authored, so it
    # promotes; its provenance is exactly its one source id.
    by_summary = {f.summary: f for f in promoted}
    fact_a = by_summary["Observatory extraction uses a local demonstration model"]
    assert len(fact_a.provenance) == 1
    assert fact_a.provenance[0].startswith("ed20fc19712f")


def test_real_run_holds_contradiction_without_mutating_facts(
    seeded_vault: tuple[Path, Path], fake_llm: FakeConsolidatorLLM, boise_clock: Callable[[], date]
) -> None:
    store, vault = seeded_vault
    memory = memory_md_path(vault)
    before = memory.read_text(encoding="utf-8")

    result = _run(store, vault, fake_llm, boise_clock, dry_run=False)

    assert len(result.contradictions) == 1
    con = result.contradictions[0]
    held_id = con.existing_fact_id
    assert held_id.startswith("f1eed1a9")

    # The held candidate's claim is NOT among the promoted facts.
    doc = read_memory(memory)
    promoted_claims = {f.claim for f in doc.sections if f.id in result.promoted_fact_ids}
    assert con.candidate.claim not in promoted_claims

    # The existing target fact's section bytes are unchanged — its exact
    # on-disk text survives intact (still valid:true, never superseded).
    target_section_before = _section_for(before, held_id)
    after = memory.read_text(encoding="utf-8")
    target_section_after = _section_for(after, held_id)
    assert target_section_after == target_section_before
    target = next(f for f in doc.sections if f.id == held_id)
    assert target.valid is True
    assert target.superseded_by is None


def _run_captures(
    captures_root: Path,
    store: Path,
    vault: Path,
    llm: FakeConsolidatorLLM,
    clock: Callable[[], date],
    *,
    dry_run: bool,
    lock_path: Path | None = None,
    index_db: Path | None = None,
) -> ConsolidationResult:
    return run_consolidation(
        selection=SourceSelection(
            kind="captures", store=store, day=clock(), captures_root=captures_root
        ),
        vault_root=vault,
        lock_path=lock_path or consolidator_lock_path(store),
        index_db=index_db,
        day=clock(),
        dry_run=dry_run,
        llm=llm,
        embedder=FakeEmbedder(),
        clock=clock,
    )


def test_captures_backlog_promotes_and_writes_watermark(
    seeded_captures: tuple[Path, Path],
    store_root: Path,
    fake_llm: FakeConsolidatorLLM,
    boise_clock: Callable[[], date],
) -> None:
    captures_root, vault = seeded_captures
    result = _run_captures(captures_root, store_root, vault, fake_llm, boise_clock, dry_run=False)

    assert result.source_kind == "captures"
    # The user:* capture is single-source but authored → it clears the
    # corroboration gate and promotes. The journal:* capture is single-source
    # and NOT authored → it fails corroboration and is discarded.
    assert result.totals["promoted"] == 1
    doc = read_memory(memory_md_path(vault))
    promoted = [f for f in doc.sections if f.id in result.promoted_fact_ids]
    assert len(promoted) == 1
    assert "The fictional observatory stores sample captures" in promoted[0].claim
    # Provenance is exactly the capture's frontmatter id.
    assert promoted[0].provenance == [
        "67cb5bccc877d880d63429d78e975368ec7270e9b44c3005625c22f875ccbfb1"
    ]

    # The watermark records BOTH read capture ids (read + extracted, not only
    # promoted), under <store>/meta/ by default.
    wm_path = watermark_path(index_db=None, lock_path=None, store=store_root)
    wm = Watermark.load(wm_path)
    assert len(wm.consolidated_ids) == 2


def test_captures_second_run_drains_to_clean_zero(
    seeded_captures: tuple[Path, Path],
    store_root: Path,
    fake_llm: FakeConsolidatorLLM,
    boise_clock: Callable[[], date],
) -> None:
    captures_root, vault = seeded_captures
    first = _run_captures(captures_root, store_root, vault, fake_llm, boise_clock, dry_run=False)
    assert first.totals["promoted"] == 1

    # Second run over the same dir: the reader still returns all units, but the
    # watermark filters them → post-filter empty → clean zero-promoted result.
    # This is NOT an EmptySourceError (the guard fires only on zero READER units).
    second = _run_captures(captures_root, store_root, vault, fake_llm, boise_clock, dry_run=False)
    assert second.totals["candidates_total"] == 0
    assert second.totals["promoted"] == 0


def test_captures_dry_run_writes_no_watermark(
    seeded_captures: tuple[Path, Path],
    store_root: Path,
    fake_llm: FakeConsolidatorLLM,
    boise_clock: Callable[[], date],
) -> None:
    captures_root, vault = seeded_captures
    _run_captures(captures_root, store_root, vault, fake_llm, boise_clock, dry_run=True)
    wm_path = watermark_path(index_db=None, lock_path=None, store=store_root)
    assert not wm_path.exists()


def test_empty_captures_source_raises(
    store_root: Path,
    vault_root: Path,
    fake_llm: FakeConsolidatorLLM,
    boise_clock: Callable[[], date],
    tmp_path: Path,
) -> None:
    (vault_root / "facts").mkdir(parents=True, exist_ok=True)
    empty = tmp_path / "empty-captures"
    empty.mkdir()
    # Only a non-matching file: zero reader units → loud empty-source guard.
    (empty / "NOTES.md").write_text("# notes\n", encoding="utf-8")

    with pytest.raises(EmptySourceError):
        _run_captures(empty, store_root, vault_root, fake_llm, boise_clock, dry_run=True)


def test_empty_sessions_day_raises(
    store_root: Path,
    vault_root: Path,
    fake_llm: FakeConsolidatorLLM,
    boise_clock: Callable[[], date],
) -> None:
    (vault_root / "facts").mkdir(parents=True, exist_ok=True)
    # No sessions directory for the day → zero reader units → guard fires.
    with pytest.raises(EmptySourceError):
        _run(store_root, vault_root, fake_llm, boise_clock, dry_run=True)


def _section_for(text: str, fact_id: str) -> str:
    """Return the raw section block of the fact with ``fact_id`` from ``text``."""
    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    bounds = starts + [len(lines)]
    for start, end in zip(bounds, bounds[1:], strict=False):
        block = "\n".join(lines[start:end])
        if f"id: {fact_id}" in block:
            return block.rstrip("\n")
    raise AssertionError(f"section for {fact_id} not found")
