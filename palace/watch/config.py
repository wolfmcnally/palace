"""Watch-roots config schema and atomic round-trip I/O.

The committed runtime artifact this module reads and writes is the per-store
TOML file at ``<store>/meta/watch-roots.toml`` (canonically
``~/palace-data.noindex/meta/watch-roots.toml`` per
``policies/storage-layout.md``). The file format is one top-level ``[[root]]``
array-of-tables entry per watch root, with ``path = "<absolute path>"`` plus
an optional ``added_at = "<ISO-8601>"`` timestamp the writer populates.

Design notes:

- **Schema is bitemporal-friendly but minimal.** Phase 2.1 only persists
  ``path`` and ``added_at``. Subsequent Phase 2.* sub-phases that need to
  attach per-root state (debounce settings, pipeline overrides) will add
  fields here; the dataclass is frozen and ``replace``-friendly to make the
  extension a pure-data change.
- **Round-trip via `tomlkit`.** Header comments and whitespace survive
  ``load`` → ``save`` byte-for-byte. ``tomlkit.parse`` reads; ``tomlkit.dumps``
  writes. The atomic-write idiom (``NamedTemporaryFile`` + ``os.replace``)
  mirrors :mod:`palace.hooks.codex` so a partial write never leaves the file
  half-mutated.
- **Validation is centralized in :meth:`WatchRootsConfig.with_added`.** The
  CLI shim calls it; the test suite calls it; no other code path mutates the
  in-memory config so the validation rules can never be bypassed.
- **Absolute paths only on disk.** ``WatchRoot.path`` is the resolved
  absolute path. A relative or ``~``-prefixed argument is resolved at
  :meth:`with_added` time; the TOML file never carries a ``~``.
- **Greenfield posture.** No migration paths, no v1 readers. A malformed file
  raises a hard error from :meth:`WatchRootsConfig.load`; the CLI translates
  that into a single-line ``error:`` line.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import AoT, Table

from palace.daemons.capture.config import BOISE_TZ
from palace.watch._errors import WatchError, WatchRootsError

__all__ = [
    "WatchError",
    "WatchRoot",
    "WatchRootsConfig",
    "WatchRootsError",
    "default_config_path",
]


# --------------------------------------------------------------------- helpers


def default_config_path(store: Path) -> Path:
    """Return the canonical watch-roots TOML path under ``store``.

    ``store`` is the daemon's machine-state root (``--store`` flag,
    ``$PALACE_STORE``, or :data:`palace.daemons.capture.config.DEFAULT_STORE`).
    The watch-roots config lives at ``<store>/meta/watch-roots.toml`` per
    ``policies/storage-layout.md`` §"Canonical layout".
    """
    return store / "meta" / "watch-roots.toml"


def _is_subpath_of(child: Path, ancestor: Path) -> bool:
    """True iff ``child`` is ``ancestor`` itself or a strict descendant."""
    try:
        child.relative_to(ancestor)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class WatchRoot:
    """One watch-root entry: an absolute resolved directory + when it was added."""

    path: Path
    added_at: datetime | None = None


@dataclass(frozen=True)
class WatchRootsConfig:
    """The in-memory view of ``<store>/meta/watch-roots.toml``.

    Frozen + tuple-typed so equality is structural and so the mutation API
    returns a new config rather than editing in place.
    """

    roots: tuple[WatchRoot, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------ I/O

    @classmethod
    def load(cls, path: Path) -> WatchRootsConfig:
        """Parse ``path`` into a :class:`WatchRootsConfig`.

        A missing file resolves to an empty config (zero roots), not an error
        — this is the steady state on a fresh ``--store``. The ``meta/``
        directory is **not** created here; lazy creation belongs in
        :meth:`save`.

        A malformed file (invalid TOML, non-array ``root``, non-string
        ``path``) raises :class:`WatchRootsError` carrying the underlying
        parse error.
        """
        if not path.exists():
            return cls()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WatchRootsError(f"cannot read {path}: {exc}") from exc
        if not text.strip():
            return cls()
        try:
            doc = tomlkit.parse(text)
        except Exception as exc:  # tomlkit raises a family of parse errors
            raise WatchRootsError(f"{path} is not valid TOML: {exc}") from exc

        raw = doc.get("root")
        if raw is None:
            return cls()
        if not isinstance(raw, (AoT, list)):
            raise WatchRootsError(
                f"{path}: top-level 'root' must be an array-of-tables, got {type(raw).__name__}"
            )

        parsed: list[WatchRoot] = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise WatchRootsError(f"{path}: entry [[root]] #{index} is not a table")
            path_value = entry.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                raise WatchRootsError(
                    f"{path}: entry [[root]] #{index} is missing a non-empty 'path' string"
                )
            added_at_value = entry.get("added_at")
            added_at: datetime | None
            if added_at_value is None:
                added_at = None
            elif isinstance(added_at_value, datetime):
                added_at = added_at_value
            elif isinstance(added_at_value, str):
                try:
                    added_at = datetime.fromisoformat(added_at_value)
                except ValueError as exc:
                    raise WatchRootsError(
                        f"{path}: entry [[root]] #{index} 'added_at' is not ISO-8601: {exc}"
                    ) from exc
            else:
                raise WatchRootsError(
                    f"{path}: entry [[root]] #{index} 'added_at' must be a string or datetime"
                )
            parsed.append(WatchRoot(path=Path(path_value), added_at=added_at))

        return cls(roots=tuple(parsed))

    def save(self, path: Path) -> None:
        """Write the config to ``path`` atomically (tempfile + ``os.replace``).

        Creates ``path.parent`` (typically ``<store>/meta/``) on demand. The
        emitted TOML carries a header comment naming palace and pointing at
        ``daemons/reindex/README.md`` so an operator opening the file knows
        which surface owns it.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = _dumps_with_header(self)
        if not serialized.endswith("\n"):
            serialized += "\n"

        # NamedTemporaryFile in the same directory keeps the rename atomic
        # (cross-device renames on macOS would not be atomic).
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 — manual close + replace
            mode="w",
            dir=str(path.parent),
            delete=False,
            encoding="utf-8",
            suffix=".tmp",
        )
        try:
            tmp.write(serialized)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, path)

    # ------------------------------------------------------------------ mutation

    def with_added(
        self,
        path: Path,
        *,
        store_root: Path,
        now: datetime | None = None,
    ) -> WatchRootsConfig:
        """Return a new config with ``path`` appended after full validation.

        Validation rules (in order):

        1. ``path`` must exist on disk.
        2. ``path`` must be a directory.
        3. ``path`` must not be ``store_root`` itself or a subpath of it
           — palace's machine state is not a watch root; recursive
           self-indexing is the entire failure-mode the parent Phase 2 calls
           out under "Self-write loop guard".
        4. ``path`` must not equal an existing watch root (duplicate).
        5. ``path`` must not be a strict ancestor or strict descendant of an
           existing watch root (overlap). Mutual ancestry creates ambiguity
           in the eventual FSEvents subscription and pipeline dispatch.

        Symlinks: a watch root that is itself a symlink is added as its
        resolved target. Phase 2.1 does not chase symlinks under a watch
        root; that decision lands in 2.2's FSEvents handler.
        """
        resolved = path.expanduser().resolve()
        store_resolved = store_root.expanduser().resolve()

        if not resolved.exists():
            raise WatchRootsError(f"path does not exist: {resolved}")
        if not resolved.is_dir():
            raise WatchRootsError(f"path is not a directory: {resolved}")
        if _is_subpath_of(resolved, store_resolved):
            raise WatchRootsError(
                f"machine-state root cannot be a watch root: {resolved} is under {store_resolved}"
            )

        for existing in self.roots:
            existing_path = existing.path
            if resolved == existing_path:
                raise WatchRootsError(f"watch root already added: {resolved}")
            if _is_subpath_of(resolved, existing_path):
                raise WatchRootsError(
                    f"overlaps existing watch root: {resolved} is under {existing_path}"
                )
            if _is_subpath_of(existing_path, resolved):
                raise WatchRootsError(
                    f"overlaps existing watch root: {resolved} contains {existing_path}"
                )

        stamp = now if now is not None else datetime.now(BOISE_TZ)
        return WatchRootsConfig(roots=(*self.roots, WatchRoot(path=resolved, added_at=stamp)))

    def with_removed(self, path: Path) -> WatchRootsConfig:
        """Return a new config with ``path`` removed.

        Matches by resolved absolute form so the CLI can accept either the
        original input shape (``~``-prefixed or relative) or the canonical
        stored shape. Raises :class:`WatchRootsError` when the path was not
        present in the config.
        """
        resolved = path.expanduser().resolve()
        retained = tuple(root for root in self.roots if root.path != resolved)
        if len(retained) == len(self.roots):
            raise WatchRootsError(f"no such watch root: {resolved}")
        return WatchRootsConfig(roots=retained)


# --------------------------------------------------------------------- TOML emission

_HEADER_LINES: tuple[str, ...] = (
    " palace watch-roots config.",
    " Generated and updated by `palace watch add` / `palace watch remove`.",
    " See daemons/reindex/README.md for the Phase 2 reindexer that consumes this.",
)


def _root_table(root: WatchRoot) -> Table:
    table = tomlkit.table()
    table.add("path", str(root.path))
    if root.added_at is not None:
        table.add("added_at", root.added_at.isoformat())
    return table


def _dumps_with_header(config: WatchRootsConfig) -> str:
    doc: TOMLDocument = tomlkit.document()
    for line in _HEADER_LINES:
        doc.add(tomlkit.comment(line))
    doc.add(tomlkit.nl())

    aot = tomlkit.aot()
    for root in config.roots:
        aot.append(_root_table(root))
    if config.roots:
        doc["root"] = aot
    return _coerce_str(tomlkit.dumps(doc))


def _coerce_str(value: Any) -> str:
    # tomlkit.dumps returns ``str``; this thin wrapper keeps mypy strict happy
    # without a cast in every call site.
    if isinstance(value, str):
        return value
    return str(value)
