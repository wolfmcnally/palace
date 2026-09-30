"""The human-readable ``<vault-root>`` concept — single home.

Palace's two-root store splits machine state (``<store>``, default
``~/palace-data.noindex/``) from the human-readable Markdown root
(``<vault-root>``, default ``~/Obsidian/Palace/``) per
``policies/storage-layout.md``. This module owns the ``<vault-root>``
half: its default, its environment override, its canonical subdirectory
layout, and the idempotent scaffolder that establishes the structure
later phases write into.

Resolution precedence mirrors the ``<store>`` rule in
:func:`palace.index.cli._resolve_store` exactly:

1. ``--vault-root <path>`` (CLI flag); ``.expanduser()`` applied.
2. ``$PALACE_VAULT_ROOT`` (environment variable); ``.expanduser()`` applied.
3. :data:`DEFAULT_VAULT_ROOT` (``~/Obsidian/Palace/``).

The scaffolder is non-destructive: it creates the three canonical
subdirectories and an empty ``context/working.md`` only when absent. It
never creates ``facts/MEMORY.md`` or ``dreams/DREAMS.md`` — Phase 5 and
Phase 6 own those files' first writes — and never truncates an existing
``context/working.md``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_VAULT_ROOT",
    "VAULT_SUBDIRS",
    "memory_md_path",
    "resolve_vault_root",
    "scaffold_vault",
    "working_md_path",
]


DEFAULT_VAULT_ROOT: Path = Path.home() / "Obsidian" / "Palace"
_PALACE_VAULT_ROOT_ENV_VAR = "PALACE_VAULT_ROOT"
VAULT_SUBDIRS: tuple[str, ...] = ("facts", "dreams", "context")


def resolve_vault_root(args: Any) -> Path:
    """Return the resolved human-readable root per the precedence rule.

    Mirrors :func:`palace.index.cli._resolve_store`: a ``--vault-root``
    flag wins, then ``$PALACE_VAULT_ROOT``, then
    :data:`DEFAULT_VAULT_ROOT`.
    """
    flag = getattr(args, "vault_root", None)
    if flag is not None:
        return Path(flag).expanduser()
    env = os.environ.get(_PALACE_VAULT_ROOT_ENV_VAR)
    if env:
        return Path(env).expanduser()
    return DEFAULT_VAULT_ROOT


def working_md_path(vault_root: Path) -> Path:
    """Return the canonical ``context/working.md`` path under ``vault_root``."""
    return vault_root / "context" / "working.md"


def memory_md_path(vault_root: Path) -> Path:
    """Return the canonical ``facts/MEMORY.md`` path under ``vault_root``."""
    return vault_root / "facts" / "MEMORY.md"


def scaffold_vault(vault_root: Path) -> list[Path]:
    """Idempotently establish the vault structure under ``vault_root``.

    Creates ``facts/``, ``dreams/``, and ``context/`` if absent, plus an
    empty ``context/working.md`` only when it does not already exist
    (never truncates an existing file). Does NOT create
    ``facts/MEMORY.md`` or ``dreams/DREAMS.md`` — those belong to Phase 5
    and Phase 6. Returns the list of paths this call actually created, in
    creation order.
    """
    created: list[Path] = []
    for subdir in VAULT_SUBDIRS:
        target = vault_root / subdir
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            created.append(target)
    working = working_md_path(vault_root)
    if not working.exists():
        working.parent.mkdir(parents=True, exist_ok=True)
        working.write_text("", encoding="utf-8")
        created.append(working)
    return created
