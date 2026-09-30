"""The consolidation watermark — already-consolidated capture id-set.

The captures source drains an unconsolidated *backlog* (whole-inbox by
default; ``--since`` narrows it), not one calendar day. Without a record of
which captures a prior run already consumed, every run re-extracts the entire
history — correct (the novelty gate still suppresses re-promotion) but wasteful
of LLM calls. The watermark is that record: a flat id-set of the captures
consolidated so far.

It is **rebuildable machine-local state**, not portable ground truth, so it
lives under the derived-index / lock location (see
:func:`palace.consolidate.config.watermark_path`) and is gitignored by the
consumer. A dry run never writes it. The write is atomic (tempfile +
``os.replace``), mirroring :class:`palace.index.cursor.TailCursor`.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from palace.consolidate._errors import ConsolidationError
from palace.consolidate.sources import SourceUnit

__all__ = ["Watermark", "filter_unconsolidated"]

_VERSION = 1


@dataclass(frozen=True)
class Watermark:
    """The set of capture source-ids already consolidated by a prior run."""

    consolidated_ids: frozenset[str]

    @classmethod
    def load(cls, path: Path) -> Watermark:
        """Load the watermark from ``path``.

        A missing file resolves to the empty watermark (a fresh consumer with
        no prior run). A present-but-malformed file raises a
        :class:`ConsolidationError` naming the path — never silently treated as
        empty, which would re-extract the whole backlog.
        """
        if not path.exists():
            return cls(consolidated_ids=frozenset())
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConsolidationError(f"watermark at {path} unreadable: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConsolidationError(f"watermark at {path} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConsolidationError(f"watermark at {path} is not a JSON object")
        ids = parsed.get("consolidated_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ConsolidationError(
                f"watermark at {path}: 'consolidated_ids' must be a list of strings"
            )
        return cls(consolidated_ids=frozenset(ids))

    def save(self, path: Path) -> None:
        """Write the watermark atomically (tempfile + ``os.replace``).

        Creates the parent directory on demand; the tempfile sibling lives in
        the same directory so the rename is atomic. Ids are sorted for a stable
        on-disk form.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": _VERSION, "consolidated_ids": sorted(self.consolidated_ids)},
            sort_keys=True,
            separators=(",", ":"),
        )
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 — manual close + replace
            mode="w",
            dir=str(path.parent),
            delete=False,
            encoding="utf-8",
            suffix=".tmp",
        )
        try:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, path)

    def with_added(self, ids: Iterable[str]) -> Watermark:
        """Return a new watermark with ``ids`` unioned into the consolidated set."""
        return Watermark(consolidated_ids=self.consolidated_ids | frozenset(ids))


def filter_unconsolidated(units: list[SourceUnit], wm: Watermark) -> list[SourceUnit]:
    """Drop units whose ``source_id`` is already in the watermark (pure)."""
    return [u for u in units if u.source_id not in wm.consolidated_ids]
