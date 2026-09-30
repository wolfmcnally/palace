"""Shared fixtures for the ``palace consolidate`` test suite.

Everything here is hermetic and offline — no test touches a live model.

- :func:`store_root` / :func:`vault_root` — kept at *distinct* tmp directories
  so the rule-9 ``--store`` / ``--vault-root`` independence is exercised by
  construction.
- :func:`boise_clock` — a fixed America/Boise day so event-log files and
  promoted-fact ids are deterministic.
- :class:`FakeConsolidatorLLM` — a :class:`ConsolidatorLLM` returning canned
  structured outputs keyed by a stable hash of the input, loaded from
  ``tests/fixtures/consolidate/fake-llm-outputs.json``.
- ``FakeEmbedder`` — re-exported from :mod:`tests.index.conftest` so the
  novelty + contradiction-shortlist embeddings are the same deterministic
  content-hashed vectors the index suite uses.
- :func:`seeded_vault` — copies the fixture seed ``MEMORY.md`` into the tmp
  vault and copies the fixture session/events days into the tmp store.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from palace.consolidate.llm import (
    ClaimLike,
    ContradictionVerdict,
    ExtractionContext,
    RawCandidate,
    SignalScores,
)
from tests.index.conftest import FakeEmbedder

__all__ = [
    "FIXED_DAY",
    "FakeConsolidatorLLM",
    "FakeEmbedder",
    "boise_clock",
    "fake_llm",
    "seeded_captures",
    "seeded_vault",
    "store_root",
    "vault_root",
]


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "consolidate"
FIXED_DAY = date(2026, 6, 15)


def _hash(text: str) -> str:
    """Stable short hash used to key canned LLM outputs by input text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


class FakeConsolidatorLLM:
    """Deterministic :class:`ConsolidatorLLM` driven by a canned-output map.

    The map (``fake-llm-outputs.json``) has three sub-dicts: ``extract``
    (keyed by a hash of the source text), ``score`` (keyed by a hash of the
    candidate claim), and ``contradiction`` (keyed by a hash of
    ``candidate_claim || existing_claim``). Missing keys fall back to a
    documented default so the fake never raises on an unseen input.
    """

    def __init__(self, outputs: dict[str, Any]) -> None:
        self._extract: dict[str, list[dict[str, Any]]] = outputs.get("extract", {})
        self._score: dict[str, dict[str, Any]] = outputs.get("score", {})
        self._contradiction: dict[str, dict[str, Any]] = outputs.get("contradiction", {})
        self.extract_calls: list[str] = []
        self.score_calls: list[str] = []
        self.contradiction_calls: list[tuple[str, str]] = []
        self.closed = False

    def extract_candidates(
        self, session_text: str, *, context: ExtractionContext
    ) -> list[RawCandidate]:
        self.extract_calls.append(session_text)
        entry = self._extract.get(_hash(session_text), [])
        out: list[RawCandidate] = []
        for item in entry:
            out.append(
                RawCandidate(
                    summary=str(item["summary"]),
                    claim=str(item["claim"]),
                    tags=[str(t) for t in item.get("tags", [])],
                    refs=[str(r) for r in item.get("refs", [])],
                )
            )
        return out

    def score_signals(self, candidate: ClaimLike, *, existing_facts: list[str]) -> SignalScores:
        self.score_calls.append(candidate.claim)
        entry = self._score.get(
            _hash(candidate.claim),
            {"importance": 0.5, "confidence": 0.5, "novelty": 0.5, "relevance": 0.5},
        )
        return SignalScores(
            importance=float(entry["importance"]),
            confidence=float(entry["confidence"]),
            novelty_llm=float(entry["novelty"]),
            relevance=float(entry["relevance"]),
        )

    def detect_contradiction(
        self, candidate_claim: str, existing_claim: str
    ) -> ContradictionVerdict:
        self.contradiction_calls.append((candidate_claim, existing_claim))
        key = _hash(candidate_claim + "||" + existing_claim)
        entry = self._contradiction.get(
            key,
            {"contradicts": False, "rationale": "", "recommended_resolution": ""},
        )
        return ContradictionVerdict(
            contradicts=bool(entry["contradicts"]),
            rationale=str(entry["rationale"]),
            recommended_resolution=str(entry["recommended_resolution"]),
        )

    def probe(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def boise_clock() -> Callable[[], date]:
    """A clock that always reports :data:`FIXED_DAY`."""
    return lambda: FIXED_DAY


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    """A hermetic ``<vault-root>`` under pytest's tmp_path."""
    root = tmp_path / "vault"
    root.mkdir()
    return root


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    """A hermetic ``<store>`` under pytest's tmp_path, distinct from the vault."""
    root = tmp_path / "store"
    root.mkdir()
    return root


@pytest.fixture
def fake_llm() -> FakeConsolidatorLLM:
    """A :class:`FakeConsolidatorLLM` loaded from the fixture output map."""
    outputs = json.loads((FIXTURES / "fake-llm-outputs.json").read_text(encoding="utf-8"))
    return FakeConsolidatorLLM(outputs)


@pytest.fixture
def seeded_vault(store_root: Path, vault_root: Path) -> tuple[Path, Path]:
    """Copy fixture seed MEMORY.md + session/events days into tmp roots.

    Returns ``(store_root, vault_root)``.
    """
    facts_dir = vault_root / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "seed-MEMORY.md", facts_dir / "MEMORY.md")

    day = FIXED_DAY.isoformat()
    sessions_dst = store_root / "sessions" / day
    sessions_dst.mkdir(parents=True, exist_ok=True)
    for src in sorted((FIXTURES / "sessions" / day).glob("*.jsonl")):
        shutil.copyfile(src, sessions_dst / src.name)

    events_dst = store_root / "events"
    events_dst.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "events" / f"{day}.jsonl", events_dst / f"{day}.jsonl")

    return store_root, vault_root


@pytest.fixture
def seeded_captures(tmp_path: Path, vault_root: Path) -> tuple[Path, Path]:
    """Copy the fixture captures inbox to a tmp dir, alongside a fresh vault.

    Returns ``(captures_root, vault_root)``. The vault starts empty (no seed
    MEMORY.md), so a captures run promotes against a clean fact base.
    """
    captures_root = tmp_path / "captures"
    captures_root.mkdir()
    for src in sorted((FIXTURES / "captures").glob("*.md")):
        shutil.copyfile(src, captures_root / src.name)
    facts_dir = vault_root / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    return captures_root, vault_root
