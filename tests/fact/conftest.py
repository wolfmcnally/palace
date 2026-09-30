"""Shared fixtures for the ``palace fact`` test suite.

Provides a hermetic tmp vault root and tmp store (kept at *distinct*
directories so the ``--vault-root`` / ``--store`` independence the
store-parametric brief requires is exercised by construction), a
Boise-fixed clock for deterministic event-day selection, and a loader for
the hand-authored seed corpus.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

__all__ = [
    "boise_clock",
    "seed_text",
    "store_root",
    "vault_root",
]


_SEED_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "fact" / "seed-MEMORY.md"

# The fixed day the Boise clock reports, so event-log day files land
# deterministically under ``<store>/events/2026-06-15.jsonl``.
FIXED_DAY = date(2026, 6, 15)


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
def boise_clock() -> Callable[[], date]:
    """Return a clock that always reports :data:`FIXED_DAY`."""
    return lambda: FIXED_DAY


@pytest.fixture
def seed_text() -> str:
    """Return the verbatim text of the hand-authored seed corpus."""
    return _SEED_PATH.read_text(encoding="utf-8")
