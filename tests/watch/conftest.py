"""Shared fixtures for ``tests/watch/``.

- :func:`tmp_store` returns a hermetic machine-state root (``<tmp_path>/store``).
- :func:`clean_env` strips ``PALACE_STORE`` so the CLI tests see only the
  ``--store`` flag they pass.
- :func:`fixture_root` resolves the repo-relative ``tests/fixtures/watch/``
  directory so per-corpus fixtures can import it without each test computing
  its own anchor.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

__all__ = ["clean_env", "fixture_root", "tmp_store"]


@pytest.fixture
def tmp_store(tmp_path: Path) -> Path:
    """A hermetic machine-state root rooted under pytest's ``tmp_path``."""
    store = tmp_path / "store"
    store.mkdir()
    return store


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip env vars that would override the CLI's ``--store`` flag."""
    monkeypatch.delenv("PALACE_STORE", raising=False)
    yield


@pytest.fixture(scope="session")
def fixture_root() -> Path:
    """Resolved path to ``tests/fixtures/watch/``."""
    return Path(__file__).resolve().parent.parent / "fixtures" / "watch"
