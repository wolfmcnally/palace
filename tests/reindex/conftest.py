"""Shared fixtures for ``tests/reindex/``.

- :func:`tmp_store` — hermetic machine-state root rooted in ``tmp_path``.
- :func:`tmp_watch_root` — hermetic watch root rooted in ``tmp_path``.
- :func:`clean_env` — strips ``PALACE_STORE`` so the CLI tests see only
  the ``--store`` flag they pass.
- :func:`populated_config` — writes a watch-roots TOML carrying
  ``tmp_watch_root`` and returns the store path.
- :func:`fast_debouncer_interval` — 10 ms interval for tests that need a
  real debouncer flush without sleeping a full 100 ms.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from palace.watch.config import WatchRootsConfig, default_config_path

__all__ = [
    "clean_env",
    "fast_debouncer_interval",
    "populated_config",
    "tmp_store",
    "tmp_watch_root",
]


@pytest.fixture
def tmp_store(tmp_path: Path) -> Path:
    """A hermetic machine-state root rooted in pytest's ``tmp_path``."""
    store = tmp_path / "store"
    store.mkdir()
    return store


@pytest.fixture
def tmp_watch_root(tmp_path: Path) -> Path:
    """A hermetic watch root rooted in pytest's ``tmp_path``."""
    root = tmp_path / "watch"
    root.mkdir()
    return root


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip env vars that would override the CLI's ``--store`` flag."""
    monkeypatch.delenv("PALACE_STORE", raising=False)
    yield


@pytest.fixture
def populated_config(tmp_store: Path, tmp_watch_root: Path) -> Path:
    """Write a watch-roots TOML for ``tmp_watch_root`` and return ``tmp_store``."""
    config_path = default_config_path(tmp_store)
    config = WatchRootsConfig().with_added(tmp_watch_root, store_root=tmp_store)
    config.save(config_path)
    return tmp_store


@pytest.fixture
def fast_debouncer_interval() -> float:
    """A debouncer interval short enough for unit tests to flush quickly."""
    return 0.010
